import json
import re
import time
import threading
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# ── BACKEND URL ───────────────────────────────────────────────────────
BACKEND_URL = "http://localhost:8000"

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """# Personality
You are Ali, a warm and professional appointment scheduling assistant for a healthcare practice.
You are male. You are polite, patient, and helpful.
You speak mainly in Urdu script, but Roman Urdu can be used if necessary.
You can understand both Urdu and English.
You only schedule appointments — nothing else.
You have access to live scheduling tools to fetch schedule and available slots.
Always call get_schedule first before saying anything about availability.
During speaking, do not call tools silently without a filler line.
# Current Date & Time
Today's current date and time is: {time}
Timezone: Asia/Karachi
ALWAYS use {time} as your only reference for:
- Knowing today's exact date and year
- Calculating "tomorrow", "next Monday", "this Friday" etc.
- Validating that patient's chosen date is NOT in the past
- Validating that patient's chosen date is NOT more than 7 days from today
- Passing correct YYYY-MM-DD dates to tools
NEVER guess or assume any date from memory.
NEVER use any year other than what {time} shows.
# Booking Window Rule — CRITICAL
- Appointments can ONLY be booked from TODAY up to 7 days ahead.
- Example: if today is March 6 2026 → valid range is March 6 to March 12 2026 only.
- If patient requests a date beyond 7 days:
  "معذرت، ہم صرف آج سے 7 دنوں کے اندر appointment book کر سکتے ہیں۔ آج [today] ہے، تو آخری available تاریخ [today+7] ہے۔ کیا آپ اس range میں کوئی دن بتا سکتے ہیں؟"
- If patient requests a past date:
  "معذرت، گزرے ہوئے دنوں کی appointment نہیں ہو سکتی۔ آج [date from system__time] ہے۔ کوئی آنے والا دن بتائیں۔"
# Goal
1. After greeting, wait for the patient's request.
2. Immediately after greeting, call **get_schedule** tool:
   - Filler before tool call:
     "ایک لمحہ، میں schedule چیک کر رہا ہوں۔"
   - Then call the **get_schedule** tool.
   - From the response, read each day's is_active field:
     - is_active: true → day is OPEN
     - is_active: false → day is CLOSED/OFF, do NOT offer this day to patient
   - Build a list of ONLY open days to share with patient.
   - Note open hours and slot duration for each open day.
   - If tool fails:
     "معذرت، سسٹم میں مسئلہ آ گیا ہے۔ براہ کرم بعد میں کال کریں۔"
     Then politely end the call.
3. Gather patient details ONE question at a time:
   - "آپ کا پورا نام کیا ہے؟"
   - "آپ کا فون نمبر بتائیں please۔"
   - Then ask for email:
     "آپ کا email address کیا ہے؟"
   ## Email Handling — IMPORTANT
   - If patient gives a full email (contains @ symbol) → use it as-is
   - If patient gives only the part before @ (example: "hamza123" or "hamza.asif") → 
     automatically append @gmail.com and confirm:
     "کیا آپ کا email hamza123@gmail.com ہے؟"
   - If patient confirms → use that email
   - If patient says different domain (yahoo, hotmail etc.) → ask:
     "آپ کا پورا email address بتائیں، جیسے hamza@yahoo.com"
   - NEVER pass an email without @ symbol to book_appointment tool
   - NEVER assume domain other than gmail unless patient specifies
   - "آج کس وجہ سے appointment چاہیے آپ کو؟"
4. Inform the patient of available days using ONLY is_active: true days from get_schedule:
   "ہمارے پاس [only open days] کو، صبح [start_time] سے شام [end_time] تک appointments available ہیں۔ ہر slot [slot_duration] منٹ کا ہوتا ہے۔"
   
   Also inform about booking window:
   "آپ آج سے اگلے 7 دنوں تک appointment book کر سکتے ہیں۔"
   
   Then ask: "آپ کو کون سا دن ٹھیک لگتا ہے؟"
5. When patient gives a preferred date, validate ALL of these:
    Check 1 — Not in the past:
   If date < today from {time}:
   "معذرت، یہ تاریخ گزر چکی ہے۔ کوئی آنے والا دن بتائیں۔"
    Check 2 — Within 7 days:
   If date > today + 7 days:
   "معذرت، ہم صرف 7 دنوں کے اندر appointment book کرتے ہیں۔ آخری تاریخ [today+7] ہے۔"
    Check 3 — Is an open day (is_active: true):
   If patient picks a day where is_active is false:
   "معذرت، [day name] کو ہماری چھٹی ہوتی ہے۔ ہمارے open days ہیں: [list only is_active: true days]۔ کوئی اور دن بتائیں؟"
    All checks passed → call get_available_slots:
   Filler: "ایک لمحہ، میں اس دن کے slots چیک کر رہا ہوں۔"
   Call **get_available_slots** with date in YYYY-MM-DD format.
   - If slots available → present 3 to 5 options:
     "اس دن یہ slots available ہیں: [slot1]، [slot2]، [slot3]۔ کون سا وقت suit کرتا ہے؟"
   - If no slots:
     "افسوس، اس دن تمام slots بھر گئے ہیں۔ کیا میں اگلا open دن چیک کروں؟"
     → auto call get_available_slots with next is_active: true date (within 7 days only)
6. When patient says "tomorrow", "next Monday" etc.:
   - Calculate correct date using {time}
   - Apply all 3 checks above before calling get_available_slots
   - Confirm with patient: "تو آپ [calculated date] کو appointment چاہتے ہیں؟"
7. Once patient selects a slot, confirm all details:
   "تو میں confirm کرنا چاہتا ہوں — [naam] کے لیے [date] کو [time] بجے appointment book کروں؟ کیا یہ ٹھیک ہے؟"
   - Wait for explicit YES before proceeding.
8. After patient confirms:
   - Filler: "ایک لمحہ، میں آپ کی appointment book کر رہا ہوں۔"
   - Call **book_appointment**
   - On success:
     "آپ کی appointment کامیابی سے book ہو گئی ہے! [date] کو [time] بجے۔"
     If meet_link returned:
     "آپ کے email پر ایک Google Meet link بھی بھیج دیا گیا ہے۔"
   - On failure:
     "معذرت، سسٹم میں مسئلہ آ گیا۔ براہ کرم کچھ دیر بعد دوبارہ کال کریں۔"
9. Close warmly:
   "ہمیں call کرنے کا شکریہ! اللہ حافظ!"
# Edge Cases
- **Past date:** Reject → ask for future date within 7 days
- **Beyond 7 days:** Reject → tell patient the valid date range
- **Closed day (is_active: false):** Reject → list only open days from get_schedule
- **No slots:** Auto check next open day within 7-day window
- **Patient unsure:** Suggest tomorrow or next open day within 7 days
- **Patient says "today":** Extract from {time} → validate → call get_available_slots
- **Partial email:** Auto append @gmail.com → confirm with patient before using
# Guardrails
- Do NOT give medical advice or diagnose anything.
- Do NOT offer days where is_active is false — ever.
- Do NOT allow bookings beyond 7 days from today.
- Do NOT allow bookings in the past.
- Do NOT call book_appointment without patient's verbal YES.
- Do NOT skip filler lines while tools are running.
- Do NOT ask all patient details at once — one question at a time.
- Do NOT pass incomplete email (without @) to book_appointment.
- Do NOT assume gmail — confirm with patient first.
- Always protect patient confidentiality.
- Never say you are an AI.
# Tone
- Polite, warm, and concise.
- Always respond in Urdu. Use Roman Urdu only if needed.
- Keep answers short unless confirming full appointment details.
# Tool Invocation Instructions
1. **get_schedule** — Call immediately after greeting. Filler first:
   "ایک لمحہ، میں schedule چیک کر رہا ہوں۔"
   Read is_active for each day. Only offer days where is_active: true.
2. **get_available_slots** — Call after date is validated. Filler first:
   "ایک لمحہ، میں اس دن کے slots چیک کر رہا ہوں۔"
   Pass date as: YYYY-MM-DD (year must match {time})
3. **book_appointment** — Call only after verbal YES. Filler first:
   "ایک لمحہ، میں آپ کی appointment book کر رہا ہوں۔"
   Pass as JSON:
   {
     "name": "{patient_name}",
     "phone": "{phone_number}",
     "email": "{valid_email_with_@}",
     "date": "{YYYY-MM-DD}",
     "start_time": "{HH:MM}",
     "notes": "{reason_for_visit}"
   }
# Tool Call Order
get_schedule → get_available_slots → book_appointment
Never skip. Never reverse. Never book without verbal confirmation."""


# ── GROQ TOOL DEFINITIONS ────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_schedule",
            "description": "Get the weekly schedule showing which days are open or closed",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_slots",
            "description": "Get available time slots for a specific date",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format",
                    }
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book an appointment for a patient",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":       {"type": "string", "description": "Patient full name"},
                    "phone":      {"type": "string", "description": "Patient phone number"},
                    "email":      {"type": "string", "description": "Patient email address (must contain @)"},
                    "date":       {"type": "string", "description": "YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "HH:MM (24-hour format)"},
                    "end_time":   {"type": "string", "description": "HH:MM (24-hour format, always start_time + 30 minutes)"},
                    "notes":      {"type": "string", "description": "Reason for appointment"},
                },
                "required": ["name", "phone", "email", "date", "start_time", "end_time"],
            },
        },
    },
]

# ── GREETING ──────────────────────────────────────────────────────────
# GREETING = "Assalam o Alaikum! Main Ali hoon, aapka appointment assistant. aaj aapki kya khidmat kar sakta hoon?"
GREETING = "السلام علیکم! میں علی ہوں، آپ کا اپائنٹمنٹ اسسٹنٹ۔ آج آپ کی کیا خدمت کر سکتا ہوں؟"


# ══════════════════════════════════════════════════════════════════════
# TOOL EXECUTORS
# ══════════════════════════════════════════════════════════════════════

def execute_tool(session, name: str, args: dict) -> str:
    """Call Django backend tools, with per-session caching."""
    cache_key = f"{name}:{json.dumps(args, sort_keys=True)}"

    if cache_key in session.tool_cache:
        logger.info("[Call %s][Tool Cache HIT] %s", session.call_sid, name)
        return session.tool_cache[cache_key]

    logger.info("[Call %s][Tool Cache MISS] %s — calling API", session.call_sid, name)
    try:
        if name == "get_schedule":
            r = requests.get(f"{BACKEND_URL}/appointment/schedule/", timeout=5)
            result = json.dumps(r.json())

        elif name == "get_available_slots":
            r = requests.get(
                f"{BACKEND_URL}/appointment/slots/",
                params={"date": args["date"]},
                timeout=5,
            )
            result = json.dumps(r.json())

        elif name == "book_appointment":
            # NEVER cache booking — always hit the API
            r = requests.post(
                f"{BACKEND_URL}/appointment/create/",
                json=args,
                timeout=10,
            )
            return json.dumps(r.json())

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # Cache get_schedule and get_available_slots only
        session.tool_cache[cache_key] = result
        return result

    except Exception as e:
        return json.dumps({"error": str(e)})


# ══════════════════════════════════════════════════════════════════════
# GROQ API CALL WITH RETRY
# Legacy Groq path is kept below for reference and rollback.
# ══════════════════════════════════════════════════════════════════════

def call_groq(session, messages, use_tools=True, max_retries=3):
    """Call Groq with retry + backoff for 429 rate limits."""
    for attempt in range(max_retries):
        try:
            kwargs = dict(
                model="llama-3.3-70b-versatile",
                messages=messages,
                stream=False,
                max_completion_tokens=500,
                temperature=0.5,
            )
            if use_tools:
                kwargs["tools"] = TOOLS
                kwargs["tool_choice"] = "auto"
            return session.groq_client.chat.completions.create(**kwargs)
        except Exception as e:
            err = str(e)
            if "429" in err:
                wait = 5 * (attempt + 1)
                logger.warning(
                    "[Call %s][Rate Limit] Waiting %ds (%d/%d)...",
                    session.call_sid, wait, attempt + 1, max_retries,
                )
                session.speak_fn("ایک لمحہ، سسٹم مصروف ہے۔")
                time.sleep(wait)
            elif "tool_use_failed" in err:
                logger.warning("[Call %s] tool_use_failed — retrying without tools", session.call_sid)
                return call_groq(session, messages, use_tools=False, max_retries=1)
            else:
                raise
    raise Exception("Groq: max retries exceeded (rate limited)")


def _deserialize_tool_result(result: str):
    try:
        return json.loads(result)
    except Exception:
        return {"result": result}


def _build_gemini_history(messages):
    history = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role not in {"user", "assistant"} or not content:
            continue

        history.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": content}],
            }
        )

    return history


def call_gemini(session, transcript: str, system_content: str):
    def get_schedule() -> dict:
        """Get the weekly schedule showing which days are open or closed."""
        return _deserialize_tool_result(execute_tool(session, "get_schedule", {}))

    def get_available_slots(date: str) -> dict:
        """Get available time slots for a specific date."""
        return _deserialize_tool_result(
            execute_tool(session, "get_available_slots", {"date": date})
        )

    def book_appointment(
        name: str,
        phone: str,
        email: str,
        date: str,
        start_time: str,
        end_time: str,
        notes: str,
    ) -> dict:
        """Book an appointment for a patient."""
        return _deserialize_tool_result(
            execute_tool(
                session,
                "book_appointment",
                {
                    "name": name,
                    "phone": phone,
                    "email": email,
                    "date": date,
                    "start_time": start_time,
                    "end_time": end_time,
                    "notes": notes,
                },
            )
        )

    prior_messages = session.conversation[:-1] if session.conversation else []
    history = _build_gemini_history(prior_messages[-10:])

    logger.info(
        "[Call %s][LLM] Sending transcript to Gemini (%d chars): %s",
        session.call_sid,
        len(transcript),
        transcript[:160],
    )

    chat = session.gemini_client.chats.create(
        model=GEMINI_MODEL,
        history=history,
        config={
            "system_instruction": system_content,
            "tools": [get_schedule, get_available_slots, book_appointment],
            "automatic_function_calling": {"ignore_call_history": True},
        },
    )
    response = chat.send_message(transcript)
    logger.info(
        "[Call %s][LLM] Gemini response received (%d chars): %s",
        session.call_sid,
        len(response.text or ""),
        (response.text or "")[:160],
    )
    return response


# ══════════════════════════════════════════════════════════════════════
# CONVERSATION TRIMMING — 5 turns (10 messages) to save tokens
# ══════════════════════════════════════════════════════════════════════

def get_trimmed_messages(session, system_content):
    """Build messages list with system prompt + last 5 turns (10 messages)."""
    trimmed = session.conversation[-10:] if len(session.conversation) > 10 else session.conversation
    return [
        {"role": "system", "content": system_content},
        *trimmed,
    ]


# ══════════════════════════════════════════════════════════════════════
# LLM — agentic loop with tool call handling
# ══════════════════════════════════════════════════════════════════════

def llm_and_speak(session, transcript: str):
    """
    Core LLM logic. Runs in a thread.

    Identical to main2.py llm_and_speak but uses session.* for all state
    and session.speak_fn() instead of speak().
    """
    from .session import State

    session.state = State.THINKING
    session.stop_speaking.clear()
    logger.info("[Call %s][LLM] Thinking...", session.call_sid)

    now = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y-%m-%d %H:%M %A")

    with session.llm_lock:
        session.conversation.append({"role": "user", "content": transcript})

    messages = get_trimmed_messages(session, SYSTEM_PROMPT.replace("{time}", now))

    try:
        spoken_filler = None

        while True:
            if session.stop_speaking.is_set():
                logger.info("[Call %s][LLM] Cancelled before API call.", session.call_sid)
                break

            # response = call_groq(session, messages)
            response = call_gemini(session, transcript, SYSTEM_PROMPT.replace("{time}", now))

            # ── Normal text response — stream to TTS ──
            full_text = response.text or ""

            # Strip leaked function tags
            full_text = re.sub(r'<function=.*?</function>', '', full_text)
            full_text = re.sub(r'<function=.*?>', '', full_text)

            # Strip Hindi/Devanagari characters
            full_text = re.sub(r'[\u0900-\u097F]+', '', full_text)
            # Strip Cyrillic characters
            full_text = re.sub(r'[\u0400-\u04FF]+', '', full_text)
            # Strip CJK characters
            full_text = re.sub(r'[\u4E00-\u9FFF]+', '', full_text)

            # Strip duplicate filler
            if spoken_filler and full_text.strip().startswith(spoken_filler):
                full_text = full_text.strip()[len(spoken_filler):].strip()
                logger.debug("[Call %s][Dedup] Stripped repeated filler", session.call_sid)

            if full_text.strip():
                logger.info("[Call %s][LLM] Handing response text to TTS", session.call_sid)
                buffer = ""
                for char in full_text:
                    if session.stop_speaking.is_set():
                        break
                    buffer += char
                    if any(p in buffer for p in ["۔", "!", "?", ".", "\n"]):
                        sentence = buffer.strip()
                        buffer = ""
                        if sentence and not session.stop_speaking.is_set():
                            session.speak_fn(sentence)

                if buffer.strip() and not session.stop_speaking.is_set():
                    session.speak_fn(buffer.strip())

                # ALWAYS save assistant response — even if interrupted
                with session.llm_lock:
                    session.conversation.append({"role": "assistant", "content": full_text})

            break

    except Exception as e:
        logger.error("[Call %s][LLM Error]: %s", session.call_sid, e)
        with session.llm_lock:
            if session.conversation and session.conversation[-1].get("role") == "user":
                removed = session.conversation.pop()
                logger.info(
                    "[Call %s][Cleanup] Removed orphaned user message: %s...",
                    session.call_sid, removed["content"][:50],
                )
    finally:
        session.state = State.LISTENING

        # Process any queued transcript
        queued = session.pending_transcript
        session.pending_transcript = None
        if queued:
            logger.info("[Call %s][Queue] Processing pending: %s", session.call_sid, queued)
            session.current_llm_thread = threading.Thread(
                target=llm_and_speak,
                args=(session, queued),
                daemon=True,
            )
            session.current_llm_thread.start()
