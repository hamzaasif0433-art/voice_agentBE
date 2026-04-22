"""
ai_agent.py — WhatsApp AI agent using Gemini.

Tool calls are parsed from LLM text output using the pattern:
    TOOL_CALL|<tool_name>|<json_payload>

Flow per turn:
    1. Send conversation history + system prompt to Gemini (AI Studio).
    2. Parse LLM reply for a TOOL_CALL line.
    3. Execute the tool, inject result as a system message.
    4. Re-call the LLM so it can continue the conversation naturally.
    5. Return the final reply to the customer.

Fallback (503 / quota errors):
    - Retries the AI Studio call up to MAX_RETRIES times with exponential backoff.
    - If all retries fail, transparently switches to Vertex AI for the same call.
    - Conversation history is fully preserved across the switch.
"""

import json
import logging
import os
import re
import time
from google import genai
from google.oauth2 import service_account

from whatsapp.prompt_builder import (
    build_router_prompt,
    build_restaurant_prompt,
    build_healthcare_prompt,
)
from whatsapp.tools import (
    menu, place_order,
    get_schedule, get_available_slots, book_appointment,
)

log = logging.getLogger(__name__)

# ── Vertex AI fallback client (lazy, built once) ──────────────────────────────
_vertex_client: genai.Client | None = None


def _get_vertex_client() -> genai.Client:
    """Lazy-initialise and return the Vertex AI genai.Client."""
    global _vertex_client
    if _vertex_client is not None:
        return _vertex_client

    # Reuse the service-account JSON that the voice consumer already reads.
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    project   = os.environ.get("VERTEX_PROJECT", "")
    location  = os.environ.get("VERTEX_LOCATION", "us-central1")

    if raw_json and project:
        try:
            sa_info = json.loads(raw_json)
            sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
            credentials = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            _vertex_client = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                credentials=credentials,
            )
            log.info("[WhatsApp AI] Vertex AI client initialised (project=%s, location=%s)", project, location)
            return _vertex_client
        except Exception as exc:
            log.warning("[WhatsApp AI] Vertex AI client init failed: %s", exc)

    log.warning("[WhatsApp AI] Vertex AI credentials unavailable — fallback will not work")
    return None  # type: ignore[return-value]


# ── Fallback error classification ─────────────────────────────────────────────

MAX_RETRIES    = 3   # AI Studio retries before switching to Vertex AI
RETRY_BACKOFF  = [1, 2, 4]  # seconds between each retry attempt

_FALLBACK_TRIGGERS = (
    "503",
    "service unavailable",
    "unavailable",
    "model not available",
    "overloaded",
    "resource has been exhausted",
    "quota",
    "rate limit",
    "429",
)


def _is_retriable_error(exc: Exception) -> bool:
    """Return True if *exc* warrants a retry or Vertex AI fallback."""
    err_str  = str(exc).lower()
    err_type = type(exc).__name__.lower()
    return (
        any(t in err_str for t in _FALLBACK_TRIGGERS)
        or "serviceunavailable" in err_type
        or "resourceexhausted" in err_type
    )

# ── In-memory state ──────────────────────────────────────────────────────────
conversation_history: dict[str, list] = {}
user_context: dict[str, str] = {}  # stores "router", "restaurant", or "healthcare"

MAX_HISTORY = 20          # keep last N messages to stay within context limits
MAX_TOOL_LOOPS = 6        # safety cap: how many tool calls allowed per turn (increased for healthcare flow)


# ─────────────────────────────────────────────────────────────────────────────
# History helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_history(phone: str) -> list:
    if phone not in conversation_history:
        conversation_history[phone] = []
    return conversation_history[phone]

def update_history(phone: str, role: str, content: str):
    history = get_or_create_history(phone)
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY:
        conversation_history[phone] = history[-MAX_HISTORY:]

def clear_history(phone: str):
    conversation_history[phone] = []


def get_context(phone: str) -> str:
    return user_context.get(phone, "router")

def set_context(phone: str, mode: str):
    user_context[phone] = mode
    clear_history(phone)


# ─────────────────────────────────────────────────────────────────────────────
# Tool dispatcher
# ─────────────────────────────────────────────────────────────────────────────

TOOL_REGISTRY = {
    # Restaurant
    "menu":                lambda _: menu(),
    "place_order":         lambda payload: place_order(payload),
    # Healthcare
    "get_schedule":        lambda payload: get_schedule(payload),
    "get_available_slots": lambda payload: get_available_slots(payload),
    "book_appointment":    lambda payload: book_appointment(payload),
}

def _parse_tool_call(text: str):
    match = re.search(r"TOOL_CALL\|(\w+)\|(\{.*\})", text, re.DOTALL)
    if not match:
        return None
    tool_name = match.group(1)
    try:
        payload = json.loads(match.group(2))
    except json.JSONDecodeError:
        payload = {}
    return tool_name, payload

def _strip_tool_call_line(text: str) -> str:
    # Match TOOL_CALL anywhere, starting at the beginning of a line or the string
    return re.sub(r"(?:^|\n)TOOL_CALL\|.*", "", text, flags=re.DOTALL).strip()

def _execute_tool(tool_name: str, payload: dict) -> str:
    if tool_name not in TOOL_REGISTRY:
        return f"TOOL_ERROR: unknown tool '{tool_name}'"
    try:
        return TOOL_REGISTRY[tool_name](payload)
    except Exception as e:
        log.error("Tool '%s' raised: %s", tool_name, e)
        return f"TOOL_ERROR: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Gemini call with retry + Vertex AI fallback
# ─────────────────────────────────────────────────────────────────────────────

def _call_gemini_with_fallback(client: genai.Client, history: list, system_msg: str):
    """
    Call Gemini generate_content with:
      1. Up to MAX_RETRIES retries on the primary AI Studio client (with backoff).
      2. If all retries fail with a 503/quota error, fall back to Vertex AI.
      3. Return the response object, or None if everything failed.

    The *history* list and *system_msg* are forwarded unchanged — no context is lost.
    """
    from google.genai import types

    def _build_contents():
        contents = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])])
            )
        return contents

    config = types.GenerateContentConfig(
        system_instruction=system_msg,
        temperature=0.6,
    )

    # ── Stage 1: Retry primary AI Studio client ────────────────────────────
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=_build_contents(),
                config=config,
            )
            if attempt > 0:
                log.info("[WhatsApp AI] AI Studio succeeded on retry %d", attempt + 1)
            return response
        except Exception as exc:
            last_exc = exc
            if _is_retriable_error(exc):
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.warning(
                    "[WhatsApp AI] AI Studio attempt %d/%d failed (%s: %s). Retrying in %ds…",
                    attempt + 1, MAX_RETRIES, type(exc).__name__, exc, wait,
                )
                time.sleep(wait)
            else:
                # Non-transient error — no point retrying
                log.error("[WhatsApp AI] Non-retriable AI Studio error: %s", exc)
                return None

    # ── Stage 2: Vertex AI fallback ────────────────────────────────────────
    log.warning(
        "[WhatsApp AI] All %d AI Studio retries exhausted (%s). Switching to Vertex AI.",
        MAX_RETRIES, last_exc,
    )
    vertex_client = _get_vertex_client()
    if vertex_client is None:
        log.error("[WhatsApp AI] Vertex AI fallback unavailable — no credentials configured.")
        return None

    try:
        response = vertex_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=_build_contents(),
            config=config,
        )
        log.info("[WhatsApp AI] Vertex AI fallback succeeded.")
        return response
    except Exception as vertex_exc:
        log.error(
            "[WhatsApp AI] Vertex AI fallback ALSO failed: %s: %s",
            type(vertex_exc).__name__, vertex_exc,
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_reply(phone: str, user_message: str, client: genai.Client) -> tuple:
    """
    Generate the assistant's reply.
    Returns:
        (reply_text: str, result_data: dict | None, result_type: str)
        result_type can be "order" or "booking".
    """
    context_mode = get_context(phone)
    
    # 1) Pick the right prompt
    if context_mode == "router":
        system_msg = build_router_prompt()
    elif context_mode == "restaurant":
        system_msg = build_restaurant_prompt()
    else:
        system_msg = build_healthcare_prompt()

    update_history(phone, "user", user_message)
    history = get_or_create_history(phone)
    result_data = None
    result_type = ""
    accumulated_reply = []

    # ── Agentic loop: keep going until no more tool calls or ROUTE tags ──────
    for loop_count in range(MAX_TOOL_LOOPS):
        response = _call_gemini_with_fallback(
            client=client,
            history=history,
            system_msg=system_msg,
        )
        if response is None:
            # All retries and Vertex AI fallback failed
            return "Sorry, abhi service temporarily unavailable hai. Thori der baad try karein. 🙏", None, ""

        raw_reply = response.text.strip()

        # DEBUG: Log raw LLM response
        log.info("DEBUG LLM raw_reply: %s", raw_reply[:200] if raw_reply else "EMPTY")

        # ── Check for ROUTER dispatch ──────────────────────────────────────
        if "ROUTE|restaurant" in raw_reply:
            log.info("Dispatching %s to Restaurant agent", phone)
            set_context(phone, "restaurant")
            return generate_reply(phone, user_message, client)
            
        elif "ROUTE|healthcare" in raw_reply:
            log.info("Dispatching %s to Healthcare agent", phone)
            set_context(phone, "healthcare")
            return generate_reply(phone, user_message, client)

        # ── Check for a tool call ─────────────────────────────────────────
        parsed = _parse_tool_call(raw_reply)

        if parsed is None:
            # No tool call — this is the final reply
            update_history(phone, "assistant", raw_reply)
            if raw_reply:
                accumulated_reply.append(raw_reply)
            final_text = "\n\n".join(accumulated_reply)
            return final_text, result_data, result_type

        tool_name, payload = parsed
        spoken_so_far = _strip_tool_call_line(raw_reply)
        if spoken_so_far:
            accumulated_reply.append(spoken_so_far)

        log.info("[loop %d] Tool call: %s | payload: %s", loop_count + 1, tool_name, payload)

        # ── Execute the tool ──────────────────────────────────────────────
        tool_result = _execute_tool(tool_name, payload)
        log.info("Tool result: %s", tool_result[:200])

        # ── Check for successful completion ───────────────────────────────
        if tool_name == "place_order" and tool_result.startswith("ORDER_SUCCESS"):
            result_data = _parse_order_success(tool_result, payload)
            result_type = "order"
            set_context(phone, "router") # Return to router base state
            
        elif tool_name == "book_appointment" and tool_result.startswith("BOOKING_SUCCESS"):
            result_data = _parse_booking_success(tool_result, payload)
            result_type = "booking"
            set_context(phone, "router") # Return to router base state

        # ── Inject tool result into history so LLM can continue ───────────
        if spoken_so_far:
            update_history(phone, "assistant", spoken_so_far)

        tool_injection = (
            f"[TOOL_RESULT for {tool_name}]\n{tool_result}\n"
            "[Continue the conversation naturally based on this result. "
            "Do NOT output another TOOL_CALL unless genuinely needed.]"
        )
        update_history(phone, "user", tool_injection)

    # ── Safety: too many loops ────────────────────────────────────────────────
    log.warning("Max tool loops (%d) reached for phone %s", MAX_TOOL_LOOPS, phone)
    fallback = "Sorry, thora zyada time lag gaya hai. Dobara try karein."
    if accumulated_reply:
        return "\n\n".join(accumulated_reply) + "\n\n" + fallback, result_data, result_type
    return fallback, None, ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: extract structured data from success string
# ─────────────────────────────────────────────────────────────────────────────

def _parse_order_success(result_str: str, original_payload: dict) -> dict:
    order_id_match = re.search(r"order_id=([\w-]+)", result_str)
    order_id = order_id_match.group(1) if order_id_match else "UNKNOWN"
    return {
        "order_id":      order_id,
        "customer_name": original_payload.get("customer_name", ""),
        "phone_number":  original_payload.get("phone_number", ""),
        "order_type":    original_payload.get("order_type", "delivery"),
        "address":       original_payload.get("address", ""),
        "landmark":      original_payload.get("landmark", ""),
        "items":         original_payload.get("items", []),
        "total_price":   original_payload.get("total_price", 0),
    }

def _parse_booking_success(result_str: str, original_payload: dict) -> dict:
    return {
        "patient_name": original_payload.get("patient_name", ""),
        "phone":        original_payload.get("phone", ""),
        "doctor_name":  original_payload.get("doctor_name", ""),
        "date":         original_payload.get("date", ""),
        "start_time":   original_payload.get("start_time", ""),
    }
