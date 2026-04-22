"""
bot.py — BlenSpark WhatsApp Bot using Green API (Webhook Mode).

Instead of long-polling, this runs as a Django view inside the existing server.
Green API sends webhooks to /whatsapp/webhook/ when messages arrive.

No extra process needed — activates only when a message hits your WhatsApp number.

Setup:
    1. Add GREENAPI_INSTANCE_ID, GREENAPI_API_TOKEN, GEMINI_API_KEY to env
    2. In Green API dashboard, set webhook URL to:
       https://your-railway-url/whatsapp/webhook/
    3. Enable: incomingMessageReceived webhook
"""

import os
import logging
import tempfile
import threading
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from google import genai

from whatsapp.ai_agent import generate_reply, _get_vertex_client, _is_retriable_error

log = logging.getLogger(__name__)

# TTS retry settings — separate from LLM retries (TTS is more forgiving)
_TTS_MAX_RETRIES   = 2
_TTS_RETRY_BACKOFF = [1, 3]  # seconds

# ── Lazy-initialized clients ──────────────────────────────────────────────────
_gemini_client = None


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY must be set in environment.")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _get_green_api_config() -> tuple:
    """Return (instance_id, api_token, base_url)."""
    instance_id = os.getenv("GREENAPI_INSTANCE_ID", "")
    api_token   = os.getenv("GREENAPI_API_TOKEN", "")
    if not instance_id or not api_token:
        raise EnvironmentError(
            "GREENAPI_INSTANCE_ID and GREENAPI_API_TOKEN must be set."
        )
    base_url = f"https://api.green-api.com/waInstance{instance_id}"
    return instance_id, api_token, base_url


# ─────────────────────────────────────────────────────────────────────────────
# Green API Helpers
# ─────────────────────────────────────────────────────────────────────────────

def send_message(chat_id: str, message: str):
    """Send a text message via Green API."""
    try:
        _, api_token, base_url = _get_green_api_config()
        url = f"{base_url}/sendMessage/{api_token}"
        resp = requests.post(url, json={
            "chatId": chat_id,
            "message": message
        }, timeout=15)
        log.info("Message sent to %s (status=%d)", chat_id, resp.status_code)
    except Exception as e:
        log.error("Send message error: %s", e)


def mark_seen(chat_id: str):
    """Mark messages as read."""
    try:
        _, api_token, base_url = _get_green_api_config()
        url = f"{base_url}/readChat/{api_token}"
        requests.post(url, json={"chatId": chat_id}, timeout=10)
    except Exception as e:
        log.warning("Mark seen error: %s", e)


def show_typing(chat_id: str, duration: int = 5):
    """Show typing indicator."""
    try:
        _, api_token, base_url = _get_green_api_config()
        url = f"{base_url}/sendTyping/{api_token}"
        requests.post(url, json={
            "chatId": chat_id,
            "timeoutSeconds": duration,
        }, timeout=10)
    except Exception as e:
        log.warning("Typing indicator error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Text-to-Speech using Gemini Live API (Vertex AI GenAI SDK)
# Uses existing Vertex AI authentication - no extra quota project needed
# ─────────────────────────────────────────────────────────────────────────────

async def synthesize_voice_sana_async(text: str, language: str = "ur-PK") -> bytes:
    """
    Synthesize speech using Gemini TTS API.
    Returns audio bytes in WAV format.

    Resilience:
      - Retries AI Studio up to _TTS_MAX_RETRIES times on 503/quota errors.
      - Falls back to Vertex AI if all retries fail.
      - Returns empty bytes if everything fails (caller will send text instead).
    """
    from google.genai import types
    import asyncio
    import io
    import wave

    voice_name = "Aoede"  # Female voice (Aoede, Kore, or Leda)

    tts_config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name,
                )
            )
        ),
    )

    def _extract_wav(response) -> bytes:
        pcm_data = response.candidates[0].content.parts[0].inline_data.data
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm_data)
        return wav_buffer.getvalue()

    # ── Stage 1: Retry primary AI Studio client ──────────────────────────
    primary_client = _get_gemini_client()
    last_exc: Exception | None = None

    for attempt in range(_TTS_MAX_RETRIES):
        try:
            response = await primary_client.aio.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=text,
                config=tts_config,
            )
            if attempt > 0:
                log.info("[WhatsApp TTS] AI Studio succeeded on retry %d", attempt + 1)
            return _extract_wav(response)
        except Exception as exc:
            last_exc = exc
            if _is_retriable_error(exc):
                wait = _TTS_RETRY_BACKOFF[min(attempt, len(_TTS_RETRY_BACKOFF) - 1)]
                log.warning(
                    "[WhatsApp TTS] AI Studio attempt %d/%d failed (%s). Retrying in %ds…",
                    attempt + 1, _TTS_MAX_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)
            else:
                log.error("[WhatsApp TTS] Non-retriable TTS error: %s", exc)
                return b""

    # ── Stage 2: Vertex AI TTS fallback ─────────────────────────────────
    log.warning(
        "[WhatsApp TTS] All AI Studio retries exhausted (%s). Switching to Vertex AI TTS.",
        last_exc,
    )
    vertex_client = _get_vertex_client()
    if vertex_client is None:
        log.error("[WhatsApp TTS] Vertex AI fallback unavailable.")
        return b""

    try:
        response = await vertex_client.aio.models.generate_content(
            model="gemini-2.5-flash-preview-tts",
            contents=text,
            config=tts_config,
        )
        log.info("[WhatsApp TTS] Vertex AI TTS fallback succeeded.")
        return _extract_wav(response)
    except Exception as vertex_exc:
        log.error(
            "[WhatsApp TTS] Vertex AI TTS fallback ALSO failed: %s",
            vertex_exc,
        )
        return b""


def synthesize_voice_sana(text: str, language: str = "ur-PK") -> bytes:
    """Synchronous wrapper for async TTS."""
    import asyncio
    try:
        return asyncio.run(synthesize_voice_sana_async(text, language))
    except Exception as e:
        log.error("TTS run error: %s", e)
        return b""


def send_voice_message(chat_id: str, audio_bytes: bytes):
    """Send a voice note via Green API."""
    try:
        _, api_token, base_url = _get_green_api_config()
        url = f"{base_url}/sendFileByUpload/{api_token}"

        # Save WAV to temporary file for upload
        # Green API accepts WAV files directly
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            with open(tmp_path, "rb") as f:
                files = {"file": ("voice.wav", f, "audio/wav")}
                data = {"chatId": chat_id}
                resp = requests.post(url, files=files, data=data, timeout=(10, 120))
            log.info("Voice sent to %s (status=%d)", chat_id, resp.status_code)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except Exception as e:
        log.error("Send voice error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Voice Note Processing
# ─────────────────────────────────────────────────────────────────────────────

def download_and_transcribe(download_url: str) -> str:
    """Download a voice note and transcribe using Gemini (with retry + Vertex AI fallback)."""
    if not download_url:
        return ""

    tmp_path = None
    try:
        # Download the audio file
        resp = requests.get(download_url, timeout=30)
        if resp.status_code != 200:
            log.warning("Voice download failed: %d", resp.status_code)
            return ""

        tmp_path = tempfile.mktemp(suffix=".ogg")
        with open(tmp_path, "wb") as f:
            f.write(resp.content)

        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()

        from google.genai import types

        def _do_transcribe(client: genai.Client) -> str:
            result = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
                    "Transcribe the following audio exactly as spoken, without adding any extra commentary.",
                ],
            )
            return result.text.strip()

        # ── Stage 1: Retry AI Studio ──────────────────────────────────────
        primary_client = _get_gemini_client()
        last_exc: Exception | None = None
        for attempt in range(2):  # 2 attempts before Vertex fallback
            try:
                text = _do_transcribe(primary_client)
                if attempt > 0:
                    log.info("[WhatsApp STT] AI Studio succeeded on retry %d", attempt + 1)
                log.info("Transcribed voice: %s", text)
                return text
            except Exception as exc:
                last_exc = exc
                if _is_retriable_error(exc):
                    log.warning(
                        "[WhatsApp STT] AI Studio attempt %d failed (%s). Retrying…",
                        attempt + 1, exc,
                    )
                    time.sleep(2)
                else:
                    log.error("[WhatsApp STT] Non-retriable transcription error: %s", exc)
                    return ""

        # ── Stage 2: Vertex AI transcription fallback ─────────────────────
        log.warning(
            "[WhatsApp STT] AI Studio transcription retries exhausted (%s). Trying Vertex AI.",
            last_exc,
        )
        vertex_client = _get_vertex_client()
        if vertex_client is not None:
            try:
                text = _do_transcribe(vertex_client)
                log.info("[WhatsApp STT] Vertex AI transcription succeeded: %s", text)
                return text
            except Exception as vertex_exc:
                log.error("[WhatsApp STT] Vertex AI transcription also failed: %s", vertex_exc)

        return ""

    except Exception as e:
        log.error("Transcribe error: %s", e)
        return ""

    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Office Hours
# ─────────────────────────────────────────────────────────────────────────────

def is_office_hours() -> bool:
    """Check if current time is within office hours (9 AM – 1 AM PKT)."""
    try:
        pk_time = datetime.now(ZoneInfo("Asia/Karachi"))
        hour = pk_time.hour
        return hour >= 9 or hour < 1
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Order Confirmation
# ─────────────────────────────────────────────────────────────────────────────

def send_order_confirmation(chat_id: str, order: dict):
    """Send formatted WhatsApp order confirmation."""
    items_text = "\n".join(
        [f"  • {item['name']} x{item['qty']}" for item in order.get("items", [])]
    )
    order_type = order.get("order_type", "delivery").capitalize()
    eta = ("45-60 mins (Delivery)" if order.get("order_type") == "delivery"
           else "20-30 mins (Pickup)")

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
    send_message(chat_id, message)
    log.info("Order confirmation sent to %s", chat_id)


def send_appointment_confirmation(chat_id: str, booking: dict):
    """Send formatted WhatsApp appointment confirmation."""
    message = (
        f"🩺 *Appointment Confirmed — BlenSpark Clinic*\n\n"
        f"👤 *Patient:* {booking.get('patient_name', '')}\n"
        f"👨‍⚕️ *Doctor:* {booking.get('doctor_name', '')}\n"
        f"📅 *Date:* {booking.get('date', '')}\n"
        f"⏰ *Time:* {booking.get('start_time', '')}\n\n"
        f"📍 *Location:* BlenSpark Clinic\n\n"
        f"Please arrive 10 minutes before your appointment time. Thank you! ❤️"
    )
    send_message(chat_id, message)
    log.info("Booking confirmation sent to %s", chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# Main Webhook Handler (called from Django view)
# ─────────────────────────────────────────────────────────────────────────────

def handle_webhook(body: dict, reply_with_voice: bool = True):
    """
    Process a Green API webhook payload.
    Called from the Django view in a background thread.

    body: the full JSON body from Green API's webhook POST.
    reply_with_voice: If True and user sent voice, reply with voice.
    """
    try:
        type_webhook = body.get("typeWebhook", "")

        # We only care about incoming messages
        if type_webhook != "incomingMessageReceived":
            return

        message_data = body.get("messageData", {})
        sender_data  = body.get("senderData", {})
        chat_id      = sender_data.get("chatId", "")

        if not chat_id:
            return

        # Skip group messages
        if "@g.us" in chat_id:
            return

        # ── Determine message type and extract text ──────────────────────
        type_message = message_data.get("typeMessage", "")
        user_message = ""
        is_voice_input = False

        if type_message == "textMessage":
            user_message = message_data.get("textMessageData", {}).get("textMessage", "")

        elif type_message == "extendedTextMessage":
            user_message = message_data.get("extendedTextMessageData", {}).get("text", "")

        elif type_message == "audioMessage":
            is_voice_input = True
            download_url = message_data.get("fileMessageData", {}).get("downloadUrl", "")
            if not download_url:
                send_message(chat_id, "Voice note samajh nahi aayi. Please text message bhejein. 📝")
                return
            user_message = download_and_transcribe(download_url)
            if not user_message:
                send_message(chat_id, "Voice note clear nahi thi. Please text mein likhein. 📝")
                return
        else:
            # Unsupported message type — ignore silently
            return

        if not user_message.strip():
            return

        log.info("WhatsApp from %s (voice=%s): %s", chat_id, is_voice_input, user_message)

        # ── Check office hours ───────────────────────────────────────────
        if not is_office_hours():
            msg = (
                "Thank you for contacting BlenSpark! 🍔\n\n"
                "We are currently closed.\n"
                "Hours: 9 AM - 1 AM PKT\n\n"
                "Please message again during business hours."
            )
            if is_voice_input and reply_with_voice:
                audio = synthesize_voice_sana(msg, "en-US")
                if audio:
                    send_voice_message(chat_id, audio)
                else:
                    send_message(chat_id, msg)
            else:
                send_message(chat_id, msg)
            return

        # ── Mark as seen + typing ────────────────────────────────────────
        mark_seen(chat_id)
        show_typing(chat_id, 5)

        # ── Generate AI response ─────────────────────────────────────────
        client = _get_gemini_client()
        reply, result_data, result_type = generate_reply(chat_id, user_message, client)

        # ── Send reply (voice if user sent voice, else text) ──────────────
        if is_voice_input and reply_with_voice:
            # Detect language for voice reply
            # Simple heuristic: if reply contains mostly English characters, use English voice
            english_ratio = sum(1 for c in reply if c.isascii()) / max(len(reply), 1)
            voice_lang = "en-US" if english_ratio > 0.7 else "ur-PK"

            audio = synthesize_voice_sana(reply, voice_lang)
            if audio:
                send_voice_message(chat_id, audio)
                log.info("Voice reply sent to %s", chat_id)
            else:
                # Fallback to text if TTS fails
                send_message(chat_id, reply)
        else:
            send_message(chat_id, reply)

        log.info("Replied to %s", chat_id)

        # ── Send confirmation if transaction was completed ───────────────
        if result_data:
            if result_type == "order":
                send_order_confirmation(chat_id, result_data)
            elif result_type == "booking":
                send_appointment_confirmation(chat_id, result_data)

    except Exception as e:
        log.error("Webhook handler error: %s", e, exc_info=True)


def handle_webhook_async(body: dict):
    """
    Process webhook in a background thread so the Django view
    can return 200 immediately (Green API expects fast response).
    """
    thread = threading.Thread(target=handle_webhook, args=(body,), daemon=True)
    thread.start()
