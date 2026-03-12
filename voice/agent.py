import json
import re
import time
import threading
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"

# ── BACKEND URL ───────────────────────────────────────────────────────
BACKEND_URL = "http://localhost:8000"

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Ali, a male Urdu-speaking appointment scheduling assistant. ONLY schedule appointments. Never say you are an AI.

## LANGUAGE
- Respond ONLY in Urdu script. Never use Devanagari/Hindi characters.
- English loan words (slots, schedule, email, appointment) stay in English letters.
- Understand both Urdu and English from patient.

## CURRENT TIME
{time} (Asia/Karachi). Use ONLY this for all date calculations. Never guess.

## STRICT STATE MACHINE — YOU ARE ALWAYS IN ONE STATE

### STATE 0 — GREETING
- If message is ONLY greeting/small talk with NO appointment mention → reply warmly, stay in STATE 0. Call NO tools.
- If message contains appointment request → go to STATE 1.

### STATE 1 — GET SCHEDULE (ONCE ONLY)
- Say filler: "ایک لمحہ، میں schedule چیک کر رہا ہوں۔"
- Call get_schedule tool.
- From response, find days where is_active=true ONLY.
- Tell patient open days in 1 sentence. Group consecutive days. Use 12-hour time.
- Example: "منگل سے ہفتہ تک صبح 9 بجے سے شام 5 بجے تک کھلا ہے، اتوار اور پیر چھٹی ہے۔"
- Say: "آپ آج سے 7 دنوں تک appointment book کر سکتے ہیں۔ آپ کو کون سا دن ٹھیک لگتا ہے؟"
- NEVER call get_schedule again after this. Ever.
- Go to STATE 2.

### STATE 2 — GET PREFERRED DATE
- When patient says a date/day:
  a. Calculate exact YYYY-MM-DD from {time}
  b. Check: not in past, not beyond today+7, is_active=true from STATE 1 data
  c. If invalid → explain why → ask again → stay in STATE 2
  d. If valid → go to STATE 3

### STATE 3 — GET AVAILABLE SLOTS
- Say filler: "ایک لمحہ، میں اس دن کے slots چیک کر رہا ہوں۔"
- Call get_available_slots with date as YYYY-MM-DD.
- Show EXACTLY 3-5 available times like:
  "اس دن یہ slots available ہیں: صبح 9:00، صبح 9:30، صبح 10:00، صبح 10:30۔ کون سا وقت suit کرتا ہے؟"
- If no slots → "افسوس، اس دن تمام slots بھر گئے ہیں۔ کوئی اور دن بتائیں؟" → go back to STATE 2.
- Do NOT call get_available_slots again for same date (use cache).
- When patient picks a slot → confirm: "[date] کو [time] بجے — ٹھیک ہے؟"
- Wait for YES → go to STATE 4.
- If NO → show slots again or ask for another day.

### STATE 4 — COLLECT NAME
- Ask ONLY: "آپ کا نام بتائیں؟"
- Wait. Save name. Go to STATE 5.
- Do NOT call any tool in this state.

### STATE 5 — COLLECT PHONE
- Ask ONLY: "آپ کا فون نمبر بتائیں؟"
- Wait. Save phone. Go to STATE 6.
- Do NOT call any tool in this state.
- STT garbles numbers. Accept whatever digits the patient gives.

### STATE 6 — COLLECT EMAIL
- Ask ONLY: "آپ کی email بتائیں؟"
- Wait. Save email.
- If no @ symbol → append @gmail.com → confirm: "کیا آپ کی email [x]@gmail.com ہے؟" → wait for confirmation.
- Go to STATE 7.
- Do NOT call any tool in this state.

### STATE 7 — COLLECT REASON
- Ask ONLY: "appointment کی وجہ بتائیں؟"
- Wait. Save reason. Go to STATE 8.
- Do NOT call any tool in this state.

### STATE 8 — CONFIRM ALL DETAILS
- Read back ALL details:
  "تو میں confirm کرتا ہوں — [name] کے لیے [date] کو [time] بجے appointment۔ فون: [phone]، email: [email]، وجہ: [reason]۔ کیا یہ سب ٹھیک ہے؟"
- WAIT for explicit YES / ہاں / جی before proceeding.
- If NO → ask what to change → go back to relevant state.

### STATE 9 — BOOK APPOINTMENT
- ONLY enter after explicit patient YES in STATE 8.
- Say filler: "ایک لمحہ، میں آپ کی appointment book کر رہا ہوں۔"
- Call book_appointment with:
  - name, phone, email, date (YYYY-MM-DD), start_time (HH:MM), notes
  - end_time = start_time + 30 mins (calculate yourself, e.g. 09:00 → 09:30, 11:00 → 11:30)
- On success: "آپ کی appointment کامیابی سے book ہو گئی! [date] کو [time] بجے۔"
- If meet_link returned: "آپ کی email پر Google Meet link بھیج دیا گیا ہے۔"
- On failure: "معذرت، سسٹم میں مسئلہ آ گیا۔ بعد میں کال کریں۔"
- Go to STATE 10.

### STATE 10 — CLOSE
- Say: "ہمیں call کرنے کا شکریہ! اللہ حافظ!"

## FILLER RULES
- ALWAYS say the filler line BEFORE calling a tool.
- NEVER repeat the filler in the same response as the tool result.
- NEVER call a tool without saying its filler first.

## GUARDRAILS
- NEVER fabricate name, phone, email, or reason. Only use what patient explicitly says.
- NEVER call get_schedule more than once.
- NEVER call get_available_slots during STATE 4, 5, 6, or 7.
- NEVER book without explicit verbal YES.
- No medical advice. No past/future-beyond-7 bookings. No closed day bookings.
- One question per response. Never ask multiple questions at once.

## STT NOISE HANDLING
- STT may garble Urdu words. Use context to understand intent.
- "ہلکے" → likely "kal ke" (tomorrow)
- "وینس" → likely "Wednesday"
- Numbers may be garbled — accept best guess from context.

## TOOL ORDER
STATE 1: get_schedule (once only)
STATE 3: get_available_slots
STATE 9: book_appointment"""


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
