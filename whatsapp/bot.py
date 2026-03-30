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

# ── Lazy-initialized Gemini client ─────────────────────────────────────────────
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

def handle_webhook(body: dict):
    """
    Process a Green API webhook payload.
    Called from the Django view in a background thread.

    body: the full JSON body from Green API's webhook POST.
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

        if type_message == "textMessage":
            user_message = message_data.get("textMessageData", {}).get("textMessage", "")

        elif type_message == "extendedTextMessage":
            user_message = message_data.get("extendedTextMessageData", {}).get("text", "")

        elif type_message == "audioMessage":
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

        log.info("WhatsApp from %s: %s", chat_id, user_message)

        # ── Check office hours ───────────────────────────────────────────
        if not is_office_hours():
            send_message(
                chat_id,
                "Thank you for contacting BlenSpark! 🍔\n\n"
                "We are currently closed.\n"
                "Hours: 9 AM - 1 AM PKT\n\n"
                "Please message again during business hours."
            )
            return

        # ── Mark as seen + typing ────────────────────────────────────────
        mark_seen(chat_id)
        show_typing(chat_id, 5)

        # ── Generate AI response ─────────────────────────────────────────
        client = _get_gemini_client()
        reply, result_data, result_type = generate_reply(chat_id, user_message, client)

        # ── Send reply ───────────────────────────────────────────────────
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
