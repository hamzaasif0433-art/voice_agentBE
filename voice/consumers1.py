# voice_agent/consumers.py
import asyncio
import audioop
import logging
import os
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from channels.generic.websocket import AsyncWebsocketConsumer
from dotenv import load_dotenv
from websockets.exceptions import ConnectionClosed

# ---------------------------------------------------------------------------
# Load .env FIRST — before any Google SDK code runs, so that
# GOOGLE_APPLICATION_CREDENTIALS is available for service account auth.
# ---------------------------------------------------------------------------
_kfc_api_dir = Path(__file__).resolve().parent.parent  # voice/consumers1.py -> voice -> kfc_api
_env_file = _kfc_api_dir / ".env"
load_dotenv(str(_env_file), override=True)

# Resolve GOOGLE_APPLICATION_CREDENTIALS to an absolute path if relative.
# When Django runs from a different CWD, relative paths won't find the file.
_creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
if _creds_path and not os.path.isabs(_creds_path):
    _abs_creds = str(_kfc_api_dir / _creds_path)
    if os.path.exists(_abs_creds):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _abs_creds
        logging.getLogger(__name__).info(
            "Resolved GOOGLE_APPLICATION_CREDENTIALS to: %s", _abs_creds
        )
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

GREETING_PATH = Path("media/sara_greeting.wav")

GREETING_PROMPT = (
    "The system has already played a welcome greeting to the user. "
    "Your very first action must be to call the get_schedule tool immediately and silently to fetch available days. "
    "Do NOT speak any greeting or filler text before calling the tool."
)

# ---------------------------------------------------------------------------
# System prompt — English, Sara persona
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

BASE_URL = os.getenv("API_BASE_URL", "https://8rc8g56h-8000.asse.devtunnels.ms")

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

LIVE_MODEL = "gemini-live-2.5-flash-native-audio"
VOICE_NAME = "Aoede"


# ---------------------------------------------------------------------------
# Tool execution — calls your Django backend APIs
# ---------------------------------------------------------------------------

async def execute_tool(tool_name: str, tool_args: dict) -> dict:
    """Execute tool calls by hitting the backend REST APIs."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as http:

            # Hit the local Django server directly to bypass Dev Tunnel blocking/latency
            base = "http://127.0.0.1:8000"
            
            if tool_name == "get_schedule":
                async with http.get(f"{base}/appointment/schedule/") as resp:
                    resp.raise_for_status()
                    return await resp.json()

            elif tool_name == "get_available_slots":
                date = tool_args.get("date", "")
                async with http.get(
                    f"{base}/appointment/slots/",
                    params={"date": date}
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()

            elif tool_name == "book_appointment":
                async with http.post(
                    f"{base}/appointment/create/",
                    json=tool_args,
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()

            else:
                return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        logger.error(f"Tool execution error [{tool_name}]: {e}")
        return {"error": str(e)}


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

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        await self.accept()

        # Use GOOGLE_SERVICE_ACCOUNT_JSON from environment for cloud compatibility
        import json
        from google.oauth2 import service_account
        import vertexai

        service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not service_account_json:
            raise EnvironmentError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.")
        try:
            sa_info = json.loads(service_account_json)
        except json.JSONDecodeError as e:
            raise ValueError("Invalid JSON in GOOGLE_SERVICE_ACCOUNT_JSON environment variable.") from e

        credentials = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

        vertex_project = os.environ.get("VERTEX_PROJECT")
        vertex_location = os.environ.get("VERTEX_LOCATION")
        if not vertex_project or not vertex_location:
            raise EnvironmentError("VERTEX_PROJECT and VERTEX_LOCATION environment variables must be set.")

        vertexai.init(
            project=vertex_project,
            location=vertex_location,
            credentials=credentials,
        )

        self.client = genai.Client(
            vertexai=True,
            project=vertex_project,
            location=vertex_location,
        )
        print("[WS Connect] Gemini client created OK", flush=True)

        task = asyncio.create_task(self._run_gemini_session())
        self._tasks.append(task)

    async def receive(self, bytes_data=None, text_data=None):
        if self._disconnecting or not bytes_data:
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
        except ConnectionClosed as exc:
            logger.info("Gemini session closed while forwarding audio: %s", exc)
            self._clear_session_state()

    async def disconnect(self, close_code):
        print(f"[WS] Browser connection closed (code {close_code}), cancelling {len(self._tasks)} tasks...", flush=True)
        self._disconnecting = True
        self._clear_session_state()
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

    def _get_system_prompt(self) -> str:
        """Return the system prompt string for this session."""
        from .agents.healthcare import build_system_prompt
        return build_system_prompt()

    def _get_tools(self):
        """Return the Gemini TOOLS list for this session."""
        from .agents.healthcare import TOOLS
        return TOOLS

    def _get_voice_name(self) -> str:
        return "Aoede"

    def _get_language_code(self) -> str:
        return "ur-PK"

    def _get_greeting_path(self):
        from .agents.healthcare import GREETING_PATH
        return GREETING_PATH

    def _get_greeting_prompt(self) -> str:
        from .agents.healthcare import GREETING_PROMPT
        return GREETING_PROMPT

    async def _execute_tool(self, tool_name: str, tool_args: dict) -> dict:
        """Execute a tool call. Subclasses override for per-agent routing."""
        from .agents.healthcare import execute_tool
        return await execute_tool(tool_name, tool_args)

    # ------------------------------------------------------------------
    # Gemini Live session
    # ------------------------------------------------------------------

    async def _run_gemini_session(self):
        voice_name    = self._get_voice_name()
        language_code = self._get_language_code()
        system_prompt = self._get_system_prompt()
        tools         = self._get_tools()

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
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                )
            ),
        )

        try:
            async with self.client.aio.live.connect(
                model=LIVE_MODEL, config=live_config
            ) as session:
                self.gemini_session = session
                self._session_ready.set()
                print("[WS] Gemini Live session open successfully!", flush=True)

                # Notify subclass (e.g. BrowserVoiceConsumer sends session_ready JSON)
                await self._on_gemini_ready()

                await self._handle_greeting(session)
                await self._receive_loop(session)

                if not self._disconnecting:
                    print("[WS INFO] Gemini Live session receive loop ended cleanly — closing WebSocket", flush=True)
                    await self.close()
        except Exception as e:
            print(f">>> [WS ERROR] Failed to connect to Gemini Live: {type(e).__name__}: {str(e)}", flush=True)
            self._clear_session_state()
            await self.close()

        except asyncio.CancelledError:
            logger.info("Gemini session task cancelled (call ended)")
        except Exception as e:
            logger.error(f"Gemini session error: {e}")
        finally:
            self._clear_session_state()

    # ------------------------------------------------------------------
    # Greeting logic
    # ------------------------------------------------------------------

    async def _handle_greeting(self, session):
        greeting_path  = self._get_greeting_path()
        greeting_prompt = self._get_greeting_prompt()

        if greeting_path.exists():
            print(f"[WS] Playing cached greeting from {greeting_path}", flush=True)
            pcm_data = _load_wav_pcm(greeting_path)
            await self._stream_pcm_to_sip(pcm_data)

            print("[WS] Telling model that greeting is cached and to start", flush=True)
            await session.send_client_content(
                turns=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text=greeting_prompt)],
                    )
                ],
                turn_complete=True,
            )
        else:
            print("[WS] No greeting file — asking Gemini to generate one", flush=True)
            self._save_as_greeting = True
            await session.send_client_content(
                turns=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text=greeting_prompt)],
                    )
                ],
                turn_complete=True,
            )
        print("[WS] _handle_greeting completed", flush=True)

    async def _stream_pcm_to_sip(self, pcm_24k: bytes):
        sip_audio = _pcm_to_mulaw(pcm_24k)
        chunk_size = 160
        for i in range(0, len(sip_audio), chunk_size):
            await self.send(bytes_data=sip_audio[i : i + chunk_size])
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------------
    # Receive loop (Gemini → SIP) — handles audio + tool calls
    # ------------------------------------------------------------------

    async def _receive_loop(self, session):
        greeting_buffer = bytearray()

        try:
            while not self._disconnecting:
                async for response in session.receive():
                    sc = getattr(response, "server_content", None)
                    tc = getattr(response, "tool_call", None)
                    print(f"[WS] Recv event: server_content={bool(sc)}, tool_call={bool(tc)}", flush=True)

                    if sc:
                        print(f"[WS DEBUG] turn_complete={getattr(sc, 'turn_complete', False)}, interrupted={getattr(sc, 'interrupted', False)}", flush=True)
                        if getattr(sc, "model_turn", None):
                            for p in sc.model_turn.parts:
                                if getattr(p, "text", None):
                                    print(f"[WS DEBUG] Model Text: {p.text}", flush=True)
                    
                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        function_responses = []
                        for fc in tool_call.function_calls:
                            tool_name = fc.name
                            tool_args = dict(fc.args) if fc.args else {}
                            print(f"[WS] [Tool Call] {tool_name}({tool_args})", flush=True)

                            result = await execute_tool(tool_name, tool_args)
                            print(f"[WS] [Tool Result] {tool_name} → {result}", flush=True)

                            function_responses.append(
                                types.FunctionResponse(
                                    name=tool_name,
                                    id=fc.id,
                                    response={"result": result},
                                )
                            )
                        
                        try:
                            # Send result back to Gemini
                            await session.send_tool_response(
                                function_responses=function_responses
                            )
                            print(f"[WS] Successfully sent tool responses for {len(function_responses)} calls", flush=True)
                        except Exception as e:
                            print(f">>> [WS ERROR] Failed to send tool response to Gemini: {repr(e)}", flush=True)
                        continue

                    # ── Audio + transcription handling ──────────────────────
                    sc = getattr(response, "server_content", None)
                    if sc is None:
                        continue

                    if getattr(sc, "input_transcription", None):
                        t = sc.input_transcription
                        if hasattr(t, "text") and t.text:
                            print(f"[WS] [User] {t.text}", flush=True)

                    if getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                if self._save_as_greeting:
                                    greeting_buffer.extend(inline.data)

                                sip_audio = _pcm_to_mulaw(inline.data)
                                await self.send(bytes_data=sip_audio)

                    if getattr(sc, "turn_complete", False):
                        if self._save_as_greeting and greeting_buffer:
                            _save_wav(bytes(greeting_buffer), GREETING_PATH, OUT_RATE)
                            logger.info(f"Greeting saved to {GREETING_PATH}")
                            self._save_as_greeting = False
                            greeting_buffer.clear()

        except ConnectionClosed as exc:
            logger.info(
                "Gemini receive loop closed: code=%s reason=%s",
                getattr(exc, "code", None),
                getattr(exc, "reason", ""),
            )


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