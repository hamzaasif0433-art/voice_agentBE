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
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from google import genai

from whatsapp.ai_agent import generate_reply

log = logging.getLogger(__name__)

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
    Synthesize speech using Gemini Live API with Vertex AI.
    Returns audio bytes in WAV format.
    """
    from google.genai import types
    import io
    import wave

    client = _get_gemini_client()

    # Female voice configuration
    voice_name = "Aoede"  # Female voice (Aoede, Kore, or Leda)

    # Audio buffer to collect response (PCM data)
    pcm_buffer = bytearray()

    try:
        # Connect to Gemini Live API for TTS
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            ),
        )

        async with client.aio.live.connect(model="gemini-live-2.5-flash-native-audio", config=config) as session:
            # Send text for TTS
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]}
            )

            # Collect audio response
            async for response in session.receive():
                server_content = getattr(response, "server_content", None)
                if server_content and getattr(server_content, "model_turn", None):
                    for part in server_content.model_turn.parts:
                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and inline_data.data:
                            pcm_buffer.extend(inline_data.data)

                # Check if turn is complete
                if getattr(server_content, "turn_complete", False):
                    break

        # Wrap PCM data in WAV header (24kHz, 16-bit, mono)
        if not pcm_buffer:
            return b""

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(24000)  # 24kHz
            wav_file.writeframes(bytes(pcm_buffer))

        return wav_buffer.getvalue()

    except Exception as e:
        log.error("TTS synthesis error: %s", e)
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
                resp = requests.post(url, files=files, data=data, timeout=30)
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
    """Download a voice note and transcribe via Groq Whisper."""
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

        client = _get_gemini_client()
        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()
            
        from google.genai import types
        
        # We use Gemini 2.5 Flash to transcribe the audio natively
        result = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type='audio/ogg'),
                "Transcribe the following audio exactly as spoken, without adding any extra commentary."
            ]
        )
        
        transcribed_text = result.text.strip()
        log.info("Transcribed voice: %s", transcribed_text)
        return transcribed_text

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
