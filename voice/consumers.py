# voice_agent/consumers.py
import asyncio
import base64
import audioop
import json
import logging
import os
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from channels.generic.websocket import AsyncWebsocketConsumer
from websockets.exceptions import ConnectionClosed
from google.oauth2 import service_account
import vertexai
import truststore
import uuid
import time
import urllib.parse
truststore.inject_into_ssl()
from asgiref.sync import sync_to_async
from Analytics.models import GeminiSessionCost, CallHistory
from .audio import twilio_payload_to_pcm16k

# ---------------------------------------------------------------------------
# Load service account credentials from environment variable
# Railway double-escapes \n in the private_key — fix it before parsing
# ---------------------------------------------------------------------------
_raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if not _raw_json:
    raise EnvironmentError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.")

try:
    _sa_info = json.loads(_raw_json)
except json.JSONDecodeError as e:
    raise ValueError(f"Invalid JSON in GOOGLE_SERVICE_ACCOUNT_JSON: {e}") from e

# Fix Railway's double-escaped newlines in private_key
_sa_info["private_key"] = _sa_info["private_key"].replace("\\n", "\n")

_credentials = service_account.Credentials.from_service_account_info(
    _sa_info,
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
)

_PROJECT  = os.environ.get("VERTEX_PROJECT") 
_LOCATION = os.environ.get("VERTEX_LOCATION") 
if not _PROJECT:
    raise EnvironmentError("VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT environment variable must be set.")

# Initialize Vertex AI globally with explicit credentials — bypasses ADC entirely
vertexai.init(
    project=_PROJECT,
    location=_LOCATION,
    credentials=_credentials,
)

# Kill any stale file-based credential env vars so SDK never falls back to them
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)

# ---------------------------------------------------------------------------

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audio format constants
# ---------------------------------------------------------------------------

SIP_RATE = 8000
MIC_RATE = 16000
OUT_RATE = 24000

from django.conf import settings
GREETING_PATH = settings.BASE_DIR / "media/sara_greeting.wav"

GREETING_PROMPT = (
    "The system has already played a welcome greeting to the user. "
    "Your very first action must be to call the get_schedule tool immediately and silently to fetch available days. "
    "Do NOT speak any greeting or filler text before calling the tool."
)

# ---------------------------------------------------------------------------
# System prompt — Sara persona
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    now = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%A, %B %d, %Y %I:%M %p")
    return f"""# Persona
You are Sara, a warm and professional appointment scheduling assistant for a healthcare practice.
You are female. You are polite, patient, and helpful.
You speak primarily in Urdu (Urdu script). Use Roman Urdu only when necessary.
You understand both Urdu and English.
You ONLY schedule appointments — nothing else.

# Current Date & Time
Today's current date and time is: {now}
Timezone: Asia/Karachi
ALWAYS use this as your only reference for:
- Knowing today's exact date and year
- Calculating "tomorrow", "next Monday", "this Friday" etc.
- Validating that the patient's chosen date is NOT in the past
- Validating that the patient's chosen date is NOT more than 7 days from today
- Passing correct YYYY-MM-DD dates to tools
NEVER guess or assume any date from memory.

# Booking Window Rule — CRITICAL
- Appointments can ONLY be booked from TODAY up to 7 days ahead.
- If patient requests a date beyond 7 days:
  Say in Urdu: "Sorry, we can only book within 7 days from today. Today is [today], so the last available date is [today+7]. Can you pick a date within this range?"
- If patient requests a past date:
  Say in Urdu: "Sorry, past dates cannot be booked. Today is [today]. Please give a future date."

# Conversation Flow

## Step 1 — Call get_schedule Immediately
- The system has already greeted the user for you.
- Do NOT say any filler lines. Do NOT speak.
- Call get_schedule tool (GET, no parameters) IMMEDIATELY and silently.
- From the response, read each day's is_active field:
  - is_active: true → day is OPEN
  - is_active: false → day is CLOSED — NEVER offer this day
- Store open days, start_time, end_time, and slot_duration for each open day.
- If tool fails, say: "Sorry, there is a system issue. Please call back later." Then end the call.

## Step 2 — Natural Small Talk (IMPORTANT)
After the schedule is fetched, before asking for the patient's name:
- If the user says something casual like "alhumdullilah", "I'm fine", "theek hoon", "how are you", "shukriya" etc., respond WARMLY and BRIEFLY in Urdu first. Example: "Alhamdulillah, bahut shukriya! Main bhi theek hoon. Aao main aapki appointment schedule karney mein madad karti hoon."
- Only THEN proceed to ask for the name.
- Do NOT jump straight to asking the name if the user is engaged in small talk.

## Step 3 — Collect Patient Details (One Question at a Time)
Ask in this exact order, one per turn:
1. "Aapka poora naam kya hai?"
2. "Aapka phone number batayein please."
3. "Aapka email address kya hai?"
4. "Aaj kis wajah se appointment chahiye aapko?"

### Email Handling — CRITICAL
- If patient gives a full email (contains @) → use as-is.
- If patient gives only the part before @ (e.g. "hamza123") →
  auto-append @gmail.com and confirm:
  "Kya aapka email hamza123@gmail.com hai?"
- If patient confirms → use that email.
- If patient mentions a different domain → ask:
  "Aapka poora email address batayein, jaise hamza@yahoo.com"
- NEVER pass an email without @ to book_appointment.
- NEVER assume gmail unless patient confirms.

## Step 3 — Share Available Days
After collecting details, share ONLY is_active: true days:
"Hamare paas [only open days] ko, subah [start_time] se shaam [end_time] tak appointments available hain. Har slot [slot_duration] minute ka hota hai."

Also say: "Aap aaj se agle 7 dinon tak appointment book kar sakti/sakte hain."

Then ask: "Aapko kaunsa din theek lagta hai?"

## Step 4 — Validate the Date
When patient gives a date, run ALL three checks:

Check 1 — Not in the past:
If date < today: "Sorry, yeh taareekh guzar chuki hai. Koi aane wala din batayein."

Check 2 — Within 7 days:
If date > today + 7: "Sorry, hum sirf 7 dinon ke andar appointment book karti hain. Aakhri taareekh [today+7] hai."

Check 3 — Is an open day (is_active: true):
If patient picks a closed day: "Sorry, [day name] ko hamaari chutti hoti hai. Hamare open days hain: [list only open days]. Koi aur din batayein?"

All checks passed → call get_available_slots:
- Filler (say OUT LOUD): "Ek lamha, main is din ke slots check kar rahi hoon."
- Call get_available_slots with date in YYYY-MM-DD format.
- If slots available → present 3–5 options:
  "Is din yeh slots available hain: [slot1], [slot2], [slot3]. Kaunsa waqt suit karta hai?"
- If no slots:
  "Afsos, is din tamaam slots bhar gaye hain. Kya main agla open din check karoon?"
  → Auto call get_available_slots for next is_active: true date (within 7 days only).

## Step 5 — Handle Relative Dates
When patient says "kal", "next Monday" etc.:
- Calculate correct date using today's date above.
- Apply all 3 checks.
- Confirm: "Toh aap [calculated date] ko appointment chahte hain?"

## Step 6 — Confirm Before Booking
Once slot is selected, confirm all details:
"Toh main confirm karna chahti hoon — [name] ke liye [date] ko [time] baje appointment book karoon? Kya yeh theek hai?"
- Wait for explicit YES before proceeding.

## Step 7 — Book the Appointment — **MANDATORY API CALL**
After patient's YES, you MUST call the book_appointment tool. This is NON-NEGOTIABLE.
- Filler (say OUT LOUD FIRST): "Ek lamha, main aapki appointment book kar rahi hoon."
- IMMEDIATELY call book_appointment tool with ALL required fields:
  {{
    "name": "patient_name",
    "phone": "phone_number",
    "email": "valid_email_with_@",
    "date": "YYYY-MM-DD",
    "start_time": "HH:MM",
    "end_time": "HH:MM",
    "notes": "reason_for_visit"
  }}
- end_time = start_time + slot_duration minutes (from get_schedule response)
- DO NOT say booking is done before calling the tool. ALWAYS call the tool first.
- On success response:
  "Aapki appointment kamiyabi se book ho gayi hai! [date] ko [time] baje."
  If meet_link returned: "Aapke email par ek Google Meet link bhi bhej diya gaya hai."
- On failure response:
  "Sorry, system mein masla aa gaya. Kuch der baad dobara call karein."

## Step 8 — Warm Goodbye
"Humein call karne ka shukriya! Allah Hafiz!"

# Tool Call Order — NEVER SKIP OR REVERSE
get_schedule → get_available_slots → book_appointment

# CRITICAL: book_appointment is a REAL API CALL
- After patient says YES to confirm their appointment, you MUST invoke the book_appointment TOOL.
- Do NOT just say the booking is confirmed without actually calling the tool.
- The tool sends data to the backend. Without calling it, no appointment is saved.
- If you skip this tool call, the appointment will NOT be booked.

# Guardrails
- Do NOT give medical advice or diagnose anything.
- Do NOT offer days where is_active is false — ever.
- Do NOT allow bookings beyond 7 days from today.
- Do NOT allow bookings in the past.
- Do NOT call book_appointment without patient's verbal YES.
- Do NOT skip filler lines while tools are running.
- Do NOT ask all patient details at once — one question at a time.
- Do NOT pass incomplete email (without @) to book_appointment.
- Always protect patient confidentiality.
- Never say you are an AI.
- ALWAYS call book_appointment tool after YES — never skip it.

# Tone
- Warm, polite, and concise.
- Always respond in Urdu. Use Roman Urdu only if needed.
- Keep answers short unless confirming full appointment details.
"""

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="get_schedule",
                description=(
                    "Fetch the full weekly schedule of the practice. "
                    "Returns each day with is_active (bool), start_time, end_time, and slot_duration. "
                    "Call this immediately after greeting — before asking anything else."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Fetch available appointment slots for a specific date. "
                    "Only call after validating the date is not in the past, "
                    "within 7 days, and is an open day (is_active: true)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "date": types.Schema(
                            type=types.Type.STRING,
                            description="Date to check slots for, in YYYY-MM-DD format. e.g. 2026-03-16",
                        ),
                    },
                    required=["date"],
                ),
            ),
            types.FunctionDeclaration(
                name="book_appointment",
                description=(
                    "Book an appointment after the patient has verbally confirmed all details. "
                    "Never call without explicit YES from the patient."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "name": types.Schema(
                            type=types.Type.STRING,
                            description="Full name of the patient.",
                        ),
                        "phone": types.Schema(
                            type=types.Type.STRING,
                            description="Phone number of the patient.",
                        ),
                        "email": types.Schema(
                            type=types.Type.STRING,
                            description="Valid email address of the patient (must contain @).",
                        ),
                        "date": types.Schema(
                            type=types.Type.STRING,
                            description="Appointment date in YYYY-MM-DD format.",
                        ),
                        "start_time": types.Schema(
                            type=types.Type.STRING,
                            description="Appointment start time in HH:MM format. e.g. 10:00",
                        ),
                        "end_time": types.Schema(
                            type=types.Type.STRING,
                            description="Appointment end time in HH:MM format. e.g. 10:30",
                        ),
                        "notes": types.Schema(
                            type=types.Type.STRING,
                            description="Reason for the appointment or additional notes.",
                        ),
                    },
                    required=["name", "phone", "email", "date", "start_time", "end_time"],
                ),
            ),
        ]
    )
]

LIVE_MODEL = "gemini-3.1-flash-live-preview"
VOICE_NAME = "Aoede"


# ---------------------------------------------------------------------------
# Tool execution — calls your Django backend APIs
# ---------------------------------------------------------------------------

# execute_tool removed — tool calls now go through self._execute_tool()
# which delegates to the agent-specific execute_tool (e.g. healthcare.execute_tool)
# that queries Django ORM directly instead of making HTTP requests.


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------

class VoiceAgentConsumer(AsyncWebsocketConsumer):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gemini_session    = None
        self.client            = None
        self._session_ready    = asyncio.Event()
        self._disconnecting    = False
        self._tasks: list[asyncio.Task] = []
        self._save_as_greeting = False
        self._upsample_state   = None
        self._downsample_state = None
        self._session_uuid     = str(uuid.uuid4())
        self._usage_metrics    = {
            "prompt": 0, "response": 0, "total": 0,
            "input_text": 0, "input_audio": 0,
            "output_text": 0, "output_audio": 0,
        }
        self._start_time       = None
        self._call_history     = []
        self._current_agent_turn = ""
        self._should_end_call  = False
        self._last_session_handle = None
        self._booking_state = ""  # Tracks appointment/order booking status (e.g., "booked", "confirmed")
        self._pending_tool_calls = 0  # Track pending tool call count
        self._transport = "browser"
        self._twilio_stream_sid = None
        self._twilio_call_sid = None
        self._twilio_ready = asyncio.Event()
        self._twilio_media_count = 0

    def _parse_query_params(self) -> dict:
        qs = self.scope.get("query_string", b"").decode("utf-8")
        return dict(urllib.parse.parse_qsl(qs))

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        self._start_time = time.time()
        await self.accept()

        params = self._parse_query_params()
        self._transport = params.get("transport", "browser").lower()
        
        # Log transport detection for debugging
        qs = self.scope.get("query_string", b"").decode("utf-8")
        print(f"[WS Connect] transport={self._transport}, query_string='{qs}'", flush=True)

        # print(
        #     f"[WS Connect] SA email='{_sa_info.get('client_email')}', "
        #     f"project='{_PROJECT}', location='{_LOCATION}'",
        #     flush=True,
        # )

        # Use Direct Google AI Studio API for Gemini 3.1 Flash Live Preview
        self.client = await sync_to_async(
            lambda: genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        )()
        print(f"[WS Connect] Gemini client created (Direct API)", flush=True)

        task = asyncio.create_task(self._run_gemini_session())
        self._tasks.append(task)

    async def receive(self, bytes_data=None, text_data=None):
        if self._disconnecting:
            return

        # ── Auto-detect Twilio transport from JSON text messages ──
        # Dev tunnels often strip query params, so ?transport=twilio may be lost.
        # Detect Twilio by checking for its event-based JSON protocol.
        if text_data and self._transport != "twilio":
            try:
                probe = json.loads(text_data)
                if probe.get("event") in ("connected", "start", "media", "stop"):
                    print(f"[WS] ⚡ Auto-detected Twilio transport from '{probe.get('event')}' event", flush=True)
                    self._transport = "twilio"
            except (json.JSONDecodeError, AttributeError):
                pass

        if self._transport == "twilio" and text_data:
            try:
                msg = json.loads(text_data)
            except json.JSONDecodeError:
                print("[TwilioWS] Non-JSON message received", flush=True)
                return

            event = msg.get("event")
            # Log first few events + every 100th media event
            if event != "media" or self._twilio_media_count < 3 or self._twilio_media_count % 100 == 0:
                print(f"[TwilioWS] Event received: {event} (media_count={self._twilio_media_count})", flush=True)

            if event == "connected":
                print(f"[TwilioWS] Twilio connected: protocol={msg.get('protocol')}", flush=True)
                return
            if event == "start":
                start_data = msg.get("start", {})
                self._twilio_stream_sid = start_data.get("streamSid", msg.get("streamSid"))
                self._twilio_call_sid = start_data.get("callSid", "")
                self._twilio_ready.set()
                print(
                    f"[TwilioWS] Stream started: CallSid={self._twilio_call_sid} StreamSid={self._twilio_stream_sid}",
                    flush=True,
                )
                return
            if event == "media":
                self._twilio_media_count += 1
                media = msg.get("media", {})
                payload = media.get("payload", "")
                if not payload:
                    return
                pcm_16k, self._upsample_state = twilio_payload_to_pcm16k(
                    payload, self._upsample_state
                )
                if not self._session_ready.is_set():
                    return
                session = self.gemini_session
                if session is None:
                    self._clear_session_state()
                    return
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=pcm_16k,
                            mime_type=f"audio/pcm;rate={MIC_RATE}",
                        )
                    )
                except Exception as exc:
                    logger.error("Gemini session error while forwarding Twilio audio: %s", exc, exc_info=True)
                    self._clear_session_state()
                    # await self.disconnect(1011) # Just clear state, no need to disconnect ws implicitly here
                return
            if event == "stop":
                print("[TwilioWS] Stream stopped by Twilio", flush=True)
                await self.disconnect(1000)
                return
            if event == "mark":
                logger.debug("[TwilioWS] Mark event: %s", msg.get("mark", {}).get("name"))
                return
            logger.debug("[TwilioWS] Unknown event: %s", event)
            return

        if not bytes_data:
            return
        if not self._session_ready.is_set():
            return

        session = self.gemini_session
        if session is None:
            self._clear_session_state()
            return

        pcm_8k = audioop.ulaw2lin(bytes_data, 2)
        pcm_16k, self._upsample_state = audioop.ratecv(
            pcm_8k, 2, 1, SIP_RATE, MIC_RATE, self._upsample_state
        )

        try:
            await session.send_realtime_input(
                audio=types.Blob(
                    data=pcm_16k,
                    mime_type=f"audio/pcm;rate={MIC_RATE}",
                )
            )
        except Exception as exc:
            logger.info("Gemini session error while forwarding audio: %s", exc)
            self._clear_session_state()

    async def disconnect(self, close_code):
        print(f"[WS] Connection closed (code {close_code}), cancelling {len(self._tasks)} tasks...", flush=True)
        self._disconnecting = True
        self._clear_session_state()
        self._twilio_ready.set() # Unblock any pending tasks waiting on this Event
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()

    def _clear_session_state(self):
        self.gemini_session = None
        self._session_ready.clear()

    async def _on_gemini_ready(self):
        """Hook for subclasses to run after Gemini session opens. Default: no-op."""
        pass

    # ------------------------------------------------------------------
    # Override hooks — subclasses implement these for dynamic config
    # ------------------------------------------------------------------

    async def _fetch_schedule_data(self) -> list:
        from appointment.models import Schedule
        from appointment.serializers import ScheduleSerializer
        schedules = await sync_to_async(lambda: list(Schedule.objects.all()))()
        return ScheduleSerializer(schedules, many=True).data


    def _get_system_prompt(self, has_cached_greeting: bool = False, schedule_data: list = None) -> str:
        from .agents.healthcare import build_system_prompt
        return build_system_prompt(
            language=self._get_language_code(),
            voice=self._get_voice_name(),
            has_cached_greeting=has_cached_greeting,
            schedule_data=schedule_data
        )

    def _get_tools(self):
        from .agents.healthcare import TOOLS
        return TOOLS

    def _get_voice_name(self) -> str:
        return "Aoede"

    def _get_language_code(self) -> str:
        return "en-US"

    def _get_greeting_path(self):
        from .agents.healthcare import get_greeting_path
        return get_greeting_path(self._get_language_code(), self._get_voice_name())

    def _get_greeting_prompt(self) -> str:
        from .agents.healthcare import get_greeting_prompt
        return get_greeting_prompt(self._get_language_code())

    def _get_generate_greeting_prompt(self) -> str:
        from .agents.healthcare import get_generate_greeting_prompt
        return get_generate_greeting_prompt(self._get_language_code(), self._get_voice_name())

    async def _execute_tool(self, tool_name: str, tool_args: dict) -> dict:
        from .agents.healthcare import execute_tool
        return await execute_tool(tool_name, tool_args)

    # ------------------------------------------------------------------
    # Gemini Live session
    # ------------------------------------------------------------------

    async def _run_gemini_session(self):
        t_start = time.time()
        voice_name    = self._get_voice_name()
        language_code = self._get_language_code()
        
        greeting_path = self._get_greeting_path()
        has_cached_greeting = greeting_path.exists()
        
        print(f"[WS DEBUG] Building config: voice={voice_name}, lang={language_code}, greeting_cached={has_cached_greeting}", flush=True)
        
        t1 = time.time()
        schedule_data = await self._fetch_schedule_data()
        print(f"[WS DEBUG] _fetch_schedule_data took {time.time()-t1:.2f}s", flush=True)
        
        t2 = time.time()
        system_prompt = self._get_system_prompt(has_cached_greeting=has_cached_greeting, schedule_data=schedule_data)
        print(f"[WS DEBUG] _get_system_prompt took {time.time()-t2:.2f}s", flush=True)
        
        t3 = time.time()
        tools         = self._get_tools()
        print(f"[WS DEBUG] _get_tools took {time.time()-t3:.2f}s", flush=True)

        live_config = types.LiveConnectConfig(
            system_instruction=types.Content(
                parts=[types.Part(text=system_prompt)]
            ),
            response_modalities=["AUDIO"],
            tools=tools,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                ),
                language_code=language_code,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            session_resumption=types.SessionResumptionConfig(handle=self._last_session_handle),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    # LOW start sensitivity = ignore background noise, only react to clear speech
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    # LOW end sensitivity = allow natural pauses without cutting off mid-sentence
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                )
            ),
        )

        print(f"[WS DEBUG] Config built. Model={LIVE_MODEL}. Connecting to Gemini Live...", flush=True)
        t0 = time.time()

        try:
            async with self.client.aio.live.connect(
                model=LIVE_MODEL, config=live_config
            ) as session:
                elapsed = time.time() - t0
                print(f"[WS] ✅ Gemini Live session OPEN in {elapsed:.2f}s", flush=True)
                self.gemini_session = session
                self._session_ready.set()

                await self._on_gemini_ready()
                print(f"[WS DEBUG] _on_gemini_ready done, starting greeting...", flush=True)
                await self._handle_greeting(session)
                print(f"[WS DEBUG] Greeting done, entering receive loop...", flush=True)
                await self._receive_loop(session)

                if not self._disconnecting:
                    print("[WS INFO] Gemini Live receive loop ended cleanly — closing WebSocket", flush=True)
                    await self.close()

        except asyncio.CancelledError:
            elapsed = time.time() - t0
            print(f"[WS] Gemini session task CANCELLED after {elapsed:.2f}s (call ended / Twilio disconnected)", flush=True)
        except Exception as e:
            elapsed = time.time() - t0
            print(f">>> [WS ERROR] Gemini Live FAILED after {elapsed:.2f}s: {type(e).__name__}: {str(e)}", flush=True)
            import traceback
            traceback.print_exc()
            self._clear_session_state()
            await self.close()
        finally:
            await self._save_session_cost()
            self._clear_session_state()

    # ------------------------------------------------------------------
    # Greeting logic
    # ------------------------------------------------------------------

    async def _handle_greeting(self, session):
        greeting_path   = self._get_greeting_path()
        greeting_prompt = self._get_greeting_prompt()

        if greeting_path.exists():
            # Cached greeting exists — play it to the SIP or Browser
            print(f"[WS] Playing cached greeting from {greeting_path}", flush=True)
            pcm_data = _load_wav_pcm(greeting_path)
            await self._stream_pcm_to_sip(pcm_data)
            print("[WS] Finished playing cached greeting. Model will wait in silence.", flush=True)
            # We explicitly do NOT send a user text message here, because doing so
            # forces Gemini Live to generate a verbal text/audio response immediately.
            # The context is now provided via system_instruction!
        else:
            # Ask model to greet the user warmly via realtime input
            generate_prompt = self._get_generate_greeting_prompt()
            print("[WS] No greeting file — asking Gemini to generate greeting", flush=True)
            self._save_as_greeting = True
            await session.send_realtime_input(text=generate_prompt)
        print("[WS] _handle_greeting completed", flush=True)

    async def _stream_pcm_to_sip(self, pcm_24k: bytes):
        pcm_8k, self._downsample_state = audioop.ratecv(
            pcm_24k, 2, 1, OUT_RATE, SIP_RATE, self._downsample_state
        )
        sip_audio = audioop.lin2ulaw(pcm_8k, 2)
        await self._send_audio_chunk(sip_audio)

    async def _send_audio_chunk(self, sip_audio: bytes):
        chunk_size = 160
        if self._transport == "twilio":
            if not self._twilio_ready.is_set():
                print("[TwilioWS] Waiting for _twilio_ready...", flush=True)
                await self._twilio_ready.wait()
                print(f"[TwilioWS] _twilio_ready SET. StreamSid={self._twilio_stream_sid}", flush=True)
            if not self._twilio_stream_sid:
                print("[TwilioWS] ⚠️ No streamSid — cannot send audio!", flush=True)
                return

            total_chunks = (len(sip_audio) + chunk_size - 1) // chunk_size
            print(f"[TwilioWS] Sending {total_chunks} audio chunks ({len(sip_audio)} bytes) to StreamSid={self._twilio_stream_sid}", flush=True)
            sent = 0
            for i in range(0, len(sip_audio), chunk_size):
                chunk = sip_audio[i : i + chunk_size]
                payload = base64.b64encode(chunk).decode("ascii")
                try:
                    await self.send(text_data=json.dumps({
                        "event": "media",
                        "streamSid": self._twilio_stream_sid,
                        "media": {"payload": payload},
                    }))
                    sent += 1
                except Exception as e:
                    print(f"[TwilioWS] ❌ Send failed at chunk {sent}/{total_chunks}: {e}", flush=True)
                    break
                await asyncio.sleep(0.01)
            print(f"[TwilioWS] ✅ Sent {sent}/{total_chunks} chunks", flush=True)
            return

        for i in range(0, len(sip_audio), chunk_size):
            await self.send(bytes_data=sip_audio[i : i + chunk_size])
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------------
    # Receive loop (Gemini → SIP) — handles audio + tool calls
    # ------------------------------------------------------------------

    async def _receive_loop(self, session):
        greeting_buffer = bytearray()
        self._pending_tool_calls = 0  # Track pending tool calls

        try:
            while not self._disconnecting:
                async for response in session.receive():
                    sc = getattr(response, "server_content", None)
                    tc = getattr(response, "tool_call", None)
                    
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        self._usage_metrics["prompt"] = max(self._usage_metrics["prompt"], getattr(usage, "prompt_token_count", 0) or 0)
                        self._usage_metrics["response"] = max(self._usage_metrics["response"], getattr(usage, "response_token_count", 0) or 0)
                        self._usage_metrics["total"] = max(self._usage_metrics["total"], getattr(usage, "total_token_count", 0) or 0)
                        
                        # Thinking tokens are billed at the text output rate
                        thoughts_count = getattr(usage, "thoughts_token_count", 0) or 0
                        if thoughts_count > 0:
                            self._usage_metrics["output_text"] = max(self._usage_metrics["output_text"], thoughts_count)

                        # Parse prompt details (modality-specific in)
                        prompt_details = getattr(usage, "prompt_tokens_details", None) or []
                        for detail in prompt_details:
                            modality = getattr(detail, "modality", None)
                            token_count = getattr(detail, "token_count", 0) or 0
                            modality_str = str(modality).upper() if modality else ""
                            if "TEXT" in modality_str:
                                self._usage_metrics["input_text"] = max(self._usage_metrics["input_text"], token_count)
                            elif "AUDIO" in modality_str:
                                self._usage_metrics["input_audio"] = max(self._usage_metrics["input_audio"], token_count)

                        # Parse response details (modality-specific out)
                        response_details = getattr(usage, "response_tokens_details", None) or []
                        for detail in response_details:
                            modality = getattr(detail, "modality", None)
                            token_count = getattr(detail, "token_count", 0) or 0
                            modality_str = str(modality).upper() if modality else ""
                            if "TEXT" in modality_str:
                                self._usage_metrics["output_text"] = max(self._usage_metrics["output_text"], token_count)
                            elif "AUDIO" in modality_str:
                                self._usage_metrics["output_audio"] = max(self._usage_metrics["output_audio"], token_count)

                        if not getattr(self, "_logged_modality_sample", False) and self._usage_metrics["total"] > 0:
                            self._logged_modality_sample = True
                            print(f"[WS DEBUG] Gemini 3.1 Usage Metadata Captured: {self._usage_metrics}", flush=True)

                    print(f"[WS] Recv event: server_content={bool(sc)}, tool_call={bool(tc)}", flush=True)

                    if getattr(response, "session_resumption_update", None):
                        update = response.session_resumption_update
                        if update.resumable and update.new_handle:
                            print(f"[WS] Received new session resumption handle: {update.new_handle[:8]}...", flush=True)
                            self._last_session_handle = update.new_handle

                    if getattr(response, "go_away", None):
                        go_away = response.go_away
                        logger.warning(f"[WS] Received GoAway message. Time left: {go_away.time_left}s")
                        # You could trigger a wrap-up here if time_left is very small

                    if sc:
                        if getattr(sc, "generation_complete", False):
                            print("[WS DEBUG] Generation complete", flush=True)

                        print(f"[WS DEBUG] turn_complete={getattr(sc, 'turn_complete', False)}, interrupted={getattr(sc, 'interrupted', False)}", flush=True)
                        if getattr(sc, "model_turn", None):
                            for p in sc.model_turn.parts:
                                if getattr(p, "text", None):
                                    print(f"[WS DEBUG] Model Text: {p.text}", flush=True)

                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        self._pending_tool_calls += len(tool_call.function_calls)
                        function_responses = []
                        for fc in tool_call.function_calls:
                            tool_name = fc.name
                            tool_args = dict(fc.args) if fc.args else {}
                            print(f"[WS] [Tool Call] {tool_name}({tool_args})", flush=True)

                            result = await self._execute_tool(tool_name, tool_args)
                            print(f"[WS] [Tool Result] {tool_name} → {result}", flush=True)

                            self._call_history.append({
                                "role": "tool",
                                "tool_name": tool_name,
                                "tool_args": tool_args,
                                "tool_result": result,
                            })

                            function_responses.append(
                                types.FunctionResponse(
                                    name=tool_name,
                                    id=fc.id,
                                    response={"result": result},
                                )
                            )

                        try:
                            await session.send_tool_response(
                                function_responses=function_responses
                            )
                            self._pending_tool_calls = 0  # Tool calls completed
                            print(f"[WS] Successfully sent tool responses for {len(function_responses)} calls", flush=True)
                        except Exception as e:
                            self._pending_tool_calls = 0
                            print(f">>> [WS ERROR] Failed to send tool response to Gemini: {repr(e)}", flush=True)
                        # Continue to wait for Gemini's response after tool results
                        continue

                    sc = getattr(response, "server_content", None)
                    if sc is None:
                        continue

                    if getattr(sc, "input_transcription", None):
                        t = sc.input_transcription
                        if hasattr(t, "text") and getattr(t, "text", None):
                            print(f"[WS] [User] {t.text}", flush=True)
                            self._call_history.append({"role": "user", "text": t.text})

                    if getattr(sc, "output_transcription", None):
                        t = sc.output_transcription
                        if hasattr(t, "text") and t.text:
                            print(f"[WS] [Agent Voice] {t.text}", flush=True)
                            # Favor output_transcription for high-fidelity audio history
                            self._current_agent_turn += t.text

                    if getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            if getattr(part, "text", None):
                                # Only add if it's not already covered by transcription
                                # Note: In Live API, if response_modalities includes AUDIO,
                                # text parts are usually redundant or empty.
                                if not self._current_agent_turn.endswith(part.text):
                                    self._current_agent_turn += part.text

                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                if self._save_as_greeting:
                                    greeting_buffer.extend(inline.data)
                                await self._stream_pcm_to_sip(inline.data)

                    if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
                        if self._save_as_greeting and greeting_buffer:
                            save_path = self._get_greeting_path()
                            _save_wav(bytes(greeting_buffer), save_path, OUT_RATE)
                            logger.info(f"Greeting saved to {save_path}")
                            self._save_as_greeting = False
                            greeting_buffer.clear()
                            
                        if self._current_agent_turn:
                            self._call_history.append({"role": "agent", "text": self._current_agent_turn.strip()})
                            idx = self._current_agent_turn.lower()
                            goodbye_detected = any(phrase in idx for phrase in ["allah hafiz", "اللہ حافظ", "khuda hafiz", "goodbye", "bye"])
                            terminal_tool_called = any(
                                h.get("tool_name") in ["book_appointment", "place_order"]
                                for h in self._call_history
                            )
                            
                            if goodbye_detected:
                                print(f"[WS] Detected call end greeting (Allah Hafiz / Goodbye) — scheduling disconnect.", flush=True)
                                self._should_end_call = True
                            self._current_agent_turn = ""
                            
                        if self._should_end_call:
                            asyncio.create_task(self._delayed_close(6.0))

        except ConnectionClosed as exc:
            logger.info(
                "Gemini receive loop closed: code=%s reason=%s",
                getattr(exc, "code", None),
                getattr(exc, "reason", ""),
            )

    async def _delayed_close(self, delay: float):
        await asyncio.sleep(delay)
        if not self._disconnecting:
            print(f"[WS] Auto-closing session {self._session_uuid} after saying goodbye.", flush=True)
            self._disconnecting = True
            await self.close(code=1000)

    async def _save_session_cost(self):
        duration = 0
        if self._start_time:
            duration = int(time.time() - self._start_time)
            
        agent_type = "healthcare"
        if hasattr(self, "_agent_cfg") and self._agent_cfg:
            agent_type = self._agent_cfg.get("id", "healthcare")

        if self._usage_metrics["total"] > 0 or duration > 0:
            try:
                # Gemini 3.1 Flash pricing (estimated):
                # Input: $0.75/1M text, $3.00/1M audio
                # Output: $4.50/1M text, $12.00/1M audio
                input_text_cost   = float(self._usage_metrics["input_text"])  * 0.00000075  # $0.75/1M
                input_audio_cost  = float(self._usage_metrics["input_audio"]) * 0.000003    # $3.00/1M
                output_text_cost  = float(self._usage_metrics["output_text"]) * 0.0000045   # $4.50/1M
                output_audio_cost = float(self._usage_metrics["output_audio"]) * 0.000012   # $12.00/1M
                total_cost = input_text_cost + input_audio_cost + output_text_cost + output_audio_cost

                await sync_to_async(GeminiSessionCost.objects.create)(
                    session_id=self._session_uuid,
                    agent_type=agent_type,
                    prompt_tokens=self._usage_metrics["prompt"],
                    response_tokens=self._usage_metrics["response"],
                    total_tokens=self._usage_metrics["total"],
                    input_text_tokens=self._usage_metrics["input_text"],
                    input_audio_tokens=self._usage_metrics["input_audio"],
                    output_text_tokens=self._usage_metrics["output_text"],
                    output_audio_tokens=self._usage_metrics["output_audio"],
                    call_duration_seconds=duration,
                    estimated_cost_usd=total_cost,
                )
                print(
                    f"[WS] Session cost: "
                    f"in_text={self._usage_metrics['input_text']}, "
                    f"in_audio={self._usage_metrics['input_audio']}, "
                    f"out_text={self._usage_metrics['output_text']}, "
                    f"out_audio={self._usage_metrics['output_audio']}, "
                    f"total={self._usage_metrics['total']} "
                    f"(${total_cost:.6f}) duration={duration}s",
                    flush=True,
                )
            except Exception as e:
                logger.error(f"Failed to save Gemini session cost: {e}")
                
        if self._call_history:
            try:
                await sync_to_async(CallHistory.objects.create)(
                    session_id=self._session_uuid,
                    agent_type=agent_type,
                    duration_seconds=duration,
                    transcript=self._call_history
                )
                print(f"[WS] Saved CallHistory for {self._session_uuid} (Duration: {duration}s, Turns: {len(self._call_history)})", flush=True)
            except Exception as e:
                logger.error(f"Failed to save CallHistory: {e}")


# ---------------------------------------------------------------------------
# Audio utility functions
# ---------------------------------------------------------------------------

def _pcm_to_mulaw(pcm_24k: bytes) -> bytes:
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, OUT_RATE, SIP_RATE, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def _load_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def _save_wav(pcm_data: bytes, path: Path, sample_rate: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    logger.info(f"Saved WAV: {path} ({sample_rate}Hz, {len(pcm_data)} bytes)")