"""
meta_views.py — Django views for WhatsApp Business (Meta Cloud API) webhook.

Meta sends:
  GET  /whatsapp/meta/webhook/  — verification challenge (one-time setup)
  POST /whatsapp/meta/webhook/  — incoming messages

Green API code is NOT touched. This is a parallel integration.

Environment variables used:
  META_VERIFY_TOKEN      — secret token you set in Meta dashboard
  META_ACCESS_TOKEN      — permanent or system-user access token
  META_PHONE_NUMBER_ID   — your WhatsApp Business phone number ID

Voice reply flow (matches Green API behaviour):
  User sends voice note
    -> meta_transcribe_voice()    downloads + transcribes with Gemini
    -> generate_reply()           AI agent produces text reply
    -> synthesize_voice_sana()    Gemini TTS -> WAV bytes
    -> wav_to_ogg()               ffmpeg converts WAV -> OGG/Opus
    -> meta_upload_media()        uploads OGG to Meta Media API
    -> meta_send_voice()          sends audio message via Meta
"""

import io
import json
import logging
import os
import tempfile
import threading
import requests

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from whatsapp.ai_agent import generate_reply
from whatsapp.bot import _get_gemini_client, synthesize_voice_sana

log = logging.getLogger(__name__)


# ── Meta API helpers ──────────────────────────────────────────────────────────

def _meta_headers() -> dict:
    token = os.getenv("META_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _phone_number_id() -> str:
    return os.getenv("META_PHONE_NUMBER_ID", "")


def meta_send_text(to: str, message: str):
    """Send a plain-text WhatsApp message via Meta Cloud API."""
    url = f"https://graph.facebook.com/v19.0/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }
    try:
        resp = requests.post(url, json=payload, headers=_meta_headers(), timeout=15)
        log.info("Meta send_text to=%s status=%d", to, resp.status_code)
        if resp.status_code not in (200, 201):
            log.warning("Meta send_text error: %s", resp.text)
    except Exception as exc:
        log.error("meta_send_text error: %s", exc)


def meta_mark_read(message_id: str):
    """Mark a message as read (shows double blue ticks)."""
    url = f"https://graph.facebook.com/v19.0/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        requests.post(url, json=payload, headers=_meta_headers(), timeout=10)
    except Exception as exc:
        log.warning("meta_mark_read error: %s", exc)


def meta_download_media(media_id: str) -> bytes:
    """Download a media file (voice note) from Meta servers."""
    try:
        meta_url = f"https://graph.facebook.com/v19.0/{media_id}"
        resp = requests.get(meta_url, headers=_meta_headers(), timeout=15)
        if resp.status_code != 200:
            log.warning("Meta media URL fetch failed: %s", resp.text)
            return b""
        download_url = resp.json().get("url", "")
        if not download_url:
            return b""

        auth_header = {"Authorization": f"Bearer {os.getenv('META_ACCESS_TOKEN', '')}"}
        audio_resp = requests.get(download_url, headers=auth_header, timeout=30)
        if audio_resp.status_code == 200:
            return audio_resp.content
        log.warning("Meta media download failed: %d", audio_resp.status_code)
        return b""
    except Exception as exc:
        log.error("meta_download_media error: %s", exc)
        return b""


def wav_to_ogg(wav_bytes: bytes) -> bytes:
    """
    Convert WAV bytes -> OGG/Opus bytes using pydub.
    Meta only accepts OGG Opus for audio messages.
    Returns empty bytes on failure.
    """
    if not wav_bytes:
        return b""
    try:
        import pydub
        from pydub import AudioSegment

        # Fallback for Windows if ffmpeg was just installed and PATH isn't refreshed
        ffmpeg_path = r"C:\Users\MADIHA\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
        if os.path.exists(ffmpeg_path):
            pydub.AudioSegment.converter = ffmpeg_path

        # Load WAV from bytes
        audio = AudioSegment.from_file(io.BytesIO(wav_bytes), format="wav")

        # Export as OGG Opus into a BytesIO buffer
        buf = io.BytesIO()
        audio.export(buf, format="ogg", codec="libopus", bitrate="32k")
        buf.seek(0)
        ogg_bytes = buf.read()
        log.info("wav_to_ogg: converted %d WAV bytes -> %d OGG bytes", len(wav_bytes), len(ogg_bytes))
        return ogg_bytes

    except ImportError:
        log.warning("pydub not installed — voice reply falls back to text")
        return b""
    except Exception as exc:
        log.error("wav_to_ogg error: %s", exc)
        return b""


def meta_upload_media(ogg_bytes: bytes) -> str:
    """
    Upload OGG audio to Meta Media API.
    Returns media_id string, or empty string on failure.
    """
    if not ogg_bytes:
        return ""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(ogg_bytes)
            tmp_path = f.name

        url = f"https://graph.facebook.com/v19.0/{_phone_number_id()}/media"
        token = os.getenv("META_ACCESS_TOKEN", "")

        with open(tmp_path, "rb") as f:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                files={"file": ("voice.ogg", f, "audio/ogg; codecs=opus")},
                timeout=30,
            )

        if resp.status_code in (200, 201):
            media_id = resp.json().get("id", "")
            log.info("Meta media uploaded — id=%s", media_id)
            return media_id

        log.warning("Meta media upload failed (%d): %s", resp.status_code, resp.text)
        return ""

    except Exception as exc:
        log.error("meta_upload_media error: %s", exc)
        return ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def meta_send_voice(to: str, text: str) -> bool:
    """
    Full voice reply pipeline:
      text -> Gemini TTS -> WAV -> OGG (ffmpeg) -> Meta upload -> audio message.
    Returns True if voice sent, False if fell back to text.
    """
    # Detect language for voice (same heuristic as Green API bot)
    english_ratio = sum(1 for c in text if c.isascii()) / max(len(text), 1)
    voice_lang = "en-US" if english_ratio > 0.7 else "ur-PK"

    wav_bytes = synthesize_voice_sana(text, voice_lang)
    if not wav_bytes:
        log.warning("TTS returned no audio — falling back to text reply")
        return False

    ogg_bytes = wav_to_ogg(wav_bytes)
    if not ogg_bytes:
        log.warning("OGG conversion failed — falling back to text reply")
        return False

    media_id = meta_upload_media(ogg_bytes)
    if not media_id:
        log.warning("Media upload failed — falling back to text reply")
        return False

    url = f"https://graph.facebook.com/v19.0/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "audio",
        "audio": {"id": media_id},
    }
    try:
        resp = requests.post(url, json=payload, headers=_meta_headers(), timeout=15)
        if resp.status_code in (200, 201):
            log.info("Meta voice reply sent to %s", to)
            return True
        log.warning("Meta send audio failed (%d): %s", resp.status_code, resp.text)
        return False
    except Exception as exc:
        log.error("meta_send_voice error: %s", exc)
        return False


def meta_transcribe_voice(media_id: str) -> str:
    """Download voice note from Meta and transcribe with Gemini."""
    audio_bytes = meta_download_media(media_id)
    if not audio_bytes:
        return ""

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        from google.genai import types

        client = _get_gemini_client()
        with open(tmp_path, "rb") as f:
            raw = f.read()

        result = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=raw, mime_type="audio/ogg"),
                "Transcribe the following audio exactly as spoken, without adding any extra commentary.",
            ],
        )
        text = result.text.strip()
        log.info("Meta transcribed voice: %s", text)
        return text
    except Exception as exc:
        log.error("meta_transcribe_voice error: %s", exc)
        return ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ── Confirmation helpers ──────────────────────────────────────────────────────

def meta_send_order_confirmation(to: str, order: dict):
    items_text = "\n".join(
        [f"  • {item['name']} x{item['qty']}" for item in order.get("items", [])]
    )
    order_type = order.get("order_type", "delivery").capitalize()
    eta = (
        "45-60 mins (Delivery)"
        if order.get("order_type") == "delivery"
        else "20-30 mins (Pickup)"
    )
    message = (
        f"🍔 *Order Confirmed — BlenSpark Restaurant*\n\n"
        f"📋 *Order ID:* {order.get('order_id', 'N/A')}\n"
        f"👤 *Name:* {order.get('customer_name', '')}\n"
        f"📦 *Type:* {order_type}\n\n"
        f"🛒 *Items:*\n{items_text}\n\n"
        f"💰 *Total:* Rs. {order.get('total_price', 0)}\n\n"
        f"📍 *Address:* {order.get('address', 'Pickup')}\n\n"
        f"⏱️ *Estimated Time:* {eta}\n\n"
        f"Thank you for ordering with BlenSpark! ❤️"
    )
    meta_send_text(to, message)


def meta_send_appointment_confirmation(to: str, booking: dict):
    message = (
        f"🩺 *Appointment Confirmed — BlenSpark Clinic*\n\n"
        f"👤 *Patient:* {booking.get('patient_name', '')}\n"
        f"👨‍⚕️ *Doctor:* {booking.get('doctor_name', '')}\n"
        f"📅 *Date:* {booking.get('date', '')}\n"
        f"⏰ *Time:* {booking.get('start_time', '')}\n\n"
        f"📍 *Location:* BlenSpark Clinic\n\n"
        f"Please arrive 10 minutes before your appointment time. Thank you! ❤️"
    )
    meta_send_text(to, message)


# ── Core message processor ────────────────────────────────────────────────────

def _process_meta_message(entry: dict):
    """Process a single webhook entry in a background thread."""
    try:
        for change in entry.get("changes", []):
            value = change.get("value", {})

            for msg in value.get("messages", []):
                message_id = msg.get("id", "")
                from_number = msg.get("from", "")
                msg_type    = msg.get("type", "")

                if not from_number:
                    continue

                if message_id:
                    meta_mark_read(message_id)

                user_message   = ""
                is_voice_input = False

                if msg_type == "text":
                    user_message = msg.get("text", {}).get("body", "")

                elif msg_type == "audio":
                    is_voice_input = True
                    media_id = msg.get("audio", {}).get("id", "")
                    if not media_id:
                        meta_send_text(from_number, "Voice note samajh nahi aayi. Please text message bhejein.")
                        continue
                    user_message = meta_transcribe_voice(media_id)
                    if not user_message:
                        meta_send_text(from_number, "Voice note clear nahi thi. Please text mein likhein.")
                        continue
                else:
                    # Ignore stickers, images, reactions, etc.
                    continue

                if not user_message.strip():
                    continue

                log.info("Meta WhatsApp from=%s (voice=%s): %s", from_number, is_voice_input, user_message)

                # Shared conversation memory with Green API (same key format)
                chat_id = f"{from_number}@s.whatsapp.net"
                client  = _get_gemini_client()
                reply, result_data, result_type = generate_reply(chat_id, user_message, client)

                # ── Reply with voice if user sent voice (mirrors Green API) ──
                if is_voice_input:
                    voice_sent = meta_send_voice(from_number, reply)
                    if not voice_sent:
                        # Fallback: send as text if TTS/conversion/upload failed
                        meta_send_text(from_number, reply)
                else:
                    meta_send_text(from_number, reply)

                # ── Confirmation messages always sent as text ─────────────
                if result_data:
                    if result_type == "order":
                        meta_send_order_confirmation(from_number, result_data)
                    elif result_type == "booking":
                        meta_send_appointment_confirmation(from_number, result_data)

    except Exception as exc:
        log.error("_process_meta_message error: %s", exc, exc_info=True)


# ── Django Views ──────────────────────────────────────────────────────────────

@csrf_exempt
def meta_webhook(request):
    """
    Handles both GET (verification) and POST (messages) from Meta.

    GET  ?hub.mode=subscribe&hub.verify_token=TOKEN&hub.challenge=XYZ
         → returns XYZ as plain text (Meta verification handshake)

    POST → incoming WhatsApp messages, processed in background thread
    """

    # ── GET: Meta verification handshake ─────────────────────────────────────
    if request.method == "GET":
        mode      = request.GET.get("hub.mode", "")
        token     = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")

        expected = os.getenv("META_VERIFY_TOKEN", "")

        log.info(
            "Meta verify attempt → mode=%s token_match=%s challenge=%s",
            mode, token == expected, challenge,
        )

        if mode == "subscribe" and token == expected:
            log.info("✅ Meta webhook verified successfully")
            # MUST return challenge as plain text with status 200
            return HttpResponse(challenge, content_type="text/plain", status=200)

        log.warning("❌ Meta webhook verification failed — token mismatch or wrong mode")
        return HttpResponse("Forbidden", status=403)

    # ── POST: Incoming messages ───────────────────────────────────────────────
    if request.method == "POST":
        try:
            body = json.loads(request.body)
            log.info("Meta POST object=%s", body.get("object", ""))

            if body.get("object") != "whatsapp_business_account":
                return JsonResponse({"status": "ignored"})

            for entry in body.get("entry", []):
                t = threading.Thread(
                    target=_process_meta_message,
                    args=(entry,),
                    daemon=True,
                )
                t.start()

            return JsonResponse({"status": "ok"})

        except json.JSONDecodeError:
            log.warning("Meta POST — invalid JSON")
            return HttpResponse(status=400)
        except Exception as exc:
            log.error("Meta POST error: %s", exc)
            return JsonResponse({"status": "error"}, status=500)

    return HttpResponse(status=405)


@csrf_exempt
def meta_health(request):
    """Health check — also useful for manually testing the endpoint."""
    return JsonResponse({
        "status": "active",
        "service": "BlenSpark Meta WhatsApp Bot",
        "phone_number_id": _phone_number_id(),
        "verify_token_configured": bool(os.getenv("META_VERIFY_TOKEN")),
        "access_token_configured": bool(os.getenv("META_ACCESS_TOKEN")),
        "webhook_url_hint": "/whatsapp/meta/webhook/",
    })
