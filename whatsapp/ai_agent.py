"""
ai_agent.py — WhatsApp AI agent using Groq LLM.

Tool calls are parsed from LLM text output using the pattern:
    TOOL_CALL|<tool_name>|<json_payload>

Flow per turn:
    1. Send conversation history + system prompt to Groq.
    2. Parse LLM reply for a TOOL_CALL line.
    3. Execute the tool, inject result as a system message.
    4. Re-call the LLM so it can continue the conversation naturally.
    5. Return the final reply to the customer.
"""

import json
import logging
import re
from google import genai

from whatsapp.prompt_builder import (
    build_router_prompt, 
    build_restaurant_prompt, 
    build_healthcare_prompt
)
from whatsapp.tools import (
    menu, place_order, 
    get_schedule, get_available_slots, book_appointment
)

log = logging.getLogger(__name__)

# ── In-memory state ──────────────────────────────────────────────────────────
conversation_history: dict[str, list] = {}
user_context: dict[str, str] = {}  # stores "router", "restaurant", or "healthcare"

MAX_HISTORY = 20          # keep last N messages to stay within context limits
MAX_TOOL_LOOPS = 3        # safety cap: how many tool calls allowed per turn


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
        try:
            from google.genai import types
            gemini_contents = []
            for msg in history:
                role = "user" if msg["role"] == "user" else "model"
                gemini_contents.append(
                    types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])])
                )
                
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=gemini_contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_msg,
                    temperature=0.6,
                )
            )
        except Exception as e:
            log.error("Gemini API error: %s", e)
            return "Sorry, abhi system mein masla hai. Thori der baad try karein.", None, ""

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
