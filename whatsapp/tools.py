"""
tools.py — WhatsApp bot tools that call the BlenSpark backend APIs.

menu()        → fetches the live menu from the backend
place_order() → places an order via the backend API

Uses the same API endpoints as the voice agent.
"""

import json
import logging
import os
import requests

log = logging.getLogger(__name__)

BASE_URL = os.getenv("API_BASE_URL", "https://web-production-00424.up.railway.app")


def menu() -> str:
    """
    Fetch the live restaurant menu from the backend API.
    Returns a formatted string for the LLM to read.
    """
    try:
        resp = requests.get(f"{BASE_URL}/menu/", timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Extract the actual menu array if wrapped in {"success": true, "menu": [...]}
        if isinstance(data, dict) and "menu" in data:
            data = data["menu"]
            
        if isinstance(data, list):
            # Group items by category_name for the LLM
            from collections import defaultdict
            grouped = defaultdict(list)
            for item in data:
                cat = item.get("category_name")
                if not cat:
                    cat = "General"
                grouped[cat].append(item)

            lines = ["=== BlenSpark Restaurant Menu ==="]
            for cat, items in grouped.items():
                lines.append(f"\n[ {cat} ]")
                for item in items:
                    name = item.get("name", item.get("item_name", "Unknown"))
                    price = item.get("cost", item.get("price", "?"))
                    lines.append(f"  • {name} — Rs. {price}")
            lines.append("\n=================================")
            return "\n".join(lines)

        elif isinstance(data, dict):
            # Categorized dict (fallback just in case)
            lines = ["=== BlenSpark Restaurant Menu ==="]
            for category, items in data.items():
                if category == "success": continue
                lines.append(f"\n[ {category} ]")
                if isinstance(items, list):
                    for item in items:
                        name = item.get("name", "Unknown")
                        price = item.get("cost", item.get("price", "?"))
                        lines.append(f"  • {name} — Rs. {price}")
            lines.append("\n=================================")
            return "\n".join(lines)

        return f"Menu data (unrecognized format): {json.dumps(data, ensure_ascii=False)}"

    except Exception as e:
        log.error("Menu fetch error: %s", e)
        return f"TOOL_ERROR: Could not fetch menu — {e}"


def place_order(payload: dict) -> str:
    """
    Place an order via the backend API.

    Expected payload keys:
        customer_name, phone_number, order_type (delivery/pickup),
        address, landmark, items (list of {name, qty, price}), total_price

    Returns a human-readable result string.
    """
    # ── Validate required fields ─────────────────────────────────────────────
    required = ["customer_name", "phone_number", "order_type", "address", "items", "total_price"]
    missing = [k for k in required if not payload.get(k)]
    if missing:
        log.warning("place_order missing fields: %s", missing)
        return f"ORDER_FAILED: missing fields — {', '.join(missing)}"

    order_type = payload.get("order_type", "delivery").lower()
    if order_type not in ("delivery", "pickup"):
        return "ORDER_FAILED: order_type must be 'delivery' or 'pickup'"

    if not isinstance(payload.get("items"), list) or len(payload["items"]) == 0:
        return "ORDER_FAILED: items list is empty"

    # ── Recalculate total for safety ─────────────────────────────────────────
    calculated_total = sum(
        item.get("qty", 1) * item.get("price", 0)
        for item in payload["items"]
    )
    if abs(calculated_total - int(payload.get("total_price", 0))) > 5:
        log.warning(
            "Total mismatch: stated=%s calculated=%s",
            payload["total_price"], calculated_total
        )
        payload["total_price"] = calculated_total

    # ── Call the backend API ─────────────────────────────────────────────────
    try:
        api_payload = {
            "customer_name": payload.get("customer_name", ""),
            "phone_number":  payload.get("phone_number", ""),
            "order_type":    order_type,
            "address":       payload.get("address", ""),
            "landmark":      payload.get("landmark", ""),
            "items":         payload.get("items", []),
            "total_price":   payload.get("total_price", 0),
        }

        resp = requests.post(f"{BASE_URL}/orders/", json=api_payload, timeout=15)

        if resp.status_code >= 400:
            error_detail = resp.text[:200]
            log.error("Order API error %d: %s", resp.status_code, error_detail)
            return f"ORDER_FAILED: API returned {resp.status_code}"

        result = resp.json()
        order_id = result.get("id", result.get("order_id", "UNKNOWN"))

        return (
            f"ORDER_SUCCESS: order_id={order_id} "
            f"| customer={payload['customer_name']} "
            f"| type={order_type} "
            f"| total=Rs.{payload['total_price']}"
        )

    except Exception as e:
        log.error("place_order error: %s", e)
        return f"ORDER_FAILED: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Healthcare Tools
# ─────────────────────────────────────────────────────────────────────────────

def get_schedule(payload: dict) -> str:
    """Fetch all doctor schedules."""
    try:
        resp = requests.get(f"{BASE_URL}/appointment/schedule/", timeout=15)
        resp.raise_for_status()
        raw_data = resp.json()

        # Backend returns {"success": true, "data": [...]}
        data = raw_data.get("data", []) if isinstance(raw_data, dict) else raw_data

        if not data:
            return "SCHEDULE_ERROR: Hamare paas doctors ka data available nahi hai."

        docs = []
        for s in data:
            doc_name = s.get("doctor_name", "Unknown Doctor")
            spec = s.get("speciality", "General")
            fee = s.get("consultation_fee", "?")
            docs.append(f"  • {doc_name} ({spec}) — Rs. {fee}")

        lines = [
            "=== Healthcare Doctors ===",
            *docs,
            "========================="
        ]
        return "\n".join(lines)
    except Exception as e:
        log.error("Schedule list err: %s", e)
        return f"ACHEDULE_ERROR: {e}"


def get_available_slots(payload: dict) -> str:
    """Fetch slots for a specific date."""
    date_str = payload.get("date", "").strip()
    
    if not date_str:
        return "SLOT_ERROR: Date (YYYY-MM-DD) required"

    try:
        url = f"{BASE_URL}/appointment/slots/?date={date_str}"
        resp = requests.get(url, timeout=15)
        
        if resp.status_code == 404:
            return f"SLOT_MSG: {date_str} ko appointment available nahi hai (closed)."
            
        resp.raise_for_status()
        raw_data = resp.json()
        
        slots_data = raw_data.get("slots", []) if isinstance(raw_data, dict) else raw_data

        if not slots_data:
            return f"SLOT_MSG: {date_str} ko koi slot available nahi hai."

        slots = []
        for s in slots_data:
            # slots returned are just {"start": "10:00", "end": "10:30"}
            time = s.get("start", "??")
            slots.append(f"  • {time}")

        return f"--- Available Slots for {date_str} ---\n" + "\n".join(slots)

    except Exception as e:
        log.error("Slot check err: %s", e)
        return f"SLOT_ERROR: API Error — {e}"


def book_appointment(payload: dict) -> str:
    """Book an appointment using patient_name, phone, date, start_time, notes."""
    required = ["patient_name", "phone", "date", "start_time"]
    for req in required:
        if not payload.get(req):
            return f"BOOKING_FAILED: missing field {req}."

    date_str = payload.get("date")
    start_time = payload.get("start_time")

    # Simple logic to add 30 mins for end_time if needed
    from datetime import datetime, timedelta
    try:
        st = datetime.strptime(start_time, "%H:%M")
        et = st + timedelta(minutes=30)
        end_time = et.strftime("%H:%M")
    except Exception:
        end_time = start_time

    try:
        book_url = f"{BASE_URL}/appointment/create/"
        book_payload = {
            "name": payload.get("patient_name"),
            "phone": payload.get("phone"),
            "email": payload.get("email", "nomail@example.com"),
            "date": date_str,
            "start_time": start_time,
            "end_time": end_time,
            "notes": payload.get("notes", "WhatsApp Booking")  # Reason/symptoms from patient
        }

        book_resp = requests.post(book_url, json=book_payload, timeout=15)
        if book_resp.status_code == 200 or book_resp.status_code == 201:
            return (
                f"BOOKING_SUCCESS: patient={payload['patient_name']} "
                f"| date={date_str} "
                f"| time={start_time}"
            )
        else:
            log.warning("Booking API denied: %s", book_resp.text)
            return f"BOOKING_FAILED: The API returned {book_resp.status_code} - {book_resp.text[:100]}"

    except Exception as e:
        log.error("Booking error: %s", e)
        return f"BOOKING_FAILED: {e}"
