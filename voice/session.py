"""
Per-call session object that isolates state for each Twilio phone call.

Replaces the module-level globals in main2.py:
  conversation, state, stop_speaking, llm_lock, pending_transcript,
  tool_cache, current_llm_thread, last_transcript

One CallSession is created per active phone call and destroyed when the call ends.
"""
import asyncio
import base64
import threading
import logging
import time
from enum import Enum
from typing import Optional, Callable

from groq import Groq
from deepgram.core.events import EventType
from elevenlabs import ElevenLabs, VoiceSettings
from deepgram import DeepgramClient
from django.conf import settings

from .audio import pcm16k_to_twilio_payload

logger = logging.getLogger(__name__)

_GREETING_AUDIO_CACHE = {}
_GREETING_AUDIO_CACHE_LOCK = threading.Lock()


def _require_voice_settings() -> None:
    missing = [
        setting_name
        for setting_name in ("GROQ_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "DEEPGRAM_API_KEY")
        if not getattr(settings, setting_name, None)
    ]
    if missing:
        raise RuntimeError(
            "Missing voice configuration: " + ", ".join(missing)
        )


class State(Enum):
    IDLE      = "idle"
    LISTENING = "listening"
    THINKING  = "thinking"
    SPEAKING  = "speaking"


class CallSession:
    """
    One instance per active phone call.

    Created when Twilio's 'start' event arrives on the media stream WebSocket.
    Destroyed when Twilio sends 'stop' or the WebSocket disconnects.
    """

    def __init__(self, call_sid: str, stream_sid: str, ws_send_fn: Callable):
        """
        Args:
            call_sid:   Twilio CallSid for logging/correlation
            stream_sid: Twilio StreamSid for media messages
            ws_send_fn: async callable to send a JSON dict over the Twilio WS
        """
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self._ws_send_fn = ws_send_fn
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        _require_voice_settings()

        # -- Per-call state (replaces main2.py globals) --
        self.conversation: list = []
        self.state = State.LISTENING
        self.stop_speaking = threading.Event()
        self.llm_lock = threading.Lock()
        self.pending_transcript: Optional[str] = None
        self.current_llm_thread: Optional[threading.Thread] = None
        self.last_transcript: str = ""
        self.tool_cache: dict = {}

        # -- Audio conversion state for glitch-free streaming --
        self.ratecv_state_in = None    # Twilio→Deepgram resampling
        self.ratecv_state_out = None   # ElevenLabs→Twilio resampling

        # -- API clients (per-session for thread safety) --
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)
        self.eleven_client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
        self.deepgram_client = DeepgramClient(api_key=settings.DEEPGRAM_API_KEY)
        self.dg_connection = None
        self._dg_connection_context = None

        # -- Deepgram keepalive flag --
        self._keep_running = True

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Called by the consumer to give the session a reference to the async event loop."""
        self._loop = loop

    # ══════════════════════════════════════════════════════════════════
    # speak_fn: TTS → Twilio WebSocket
    # ══════════════════════════════════════════════════════════════════

    def speak_fn(self, text: str):
        """
        Synchronous function (called from threaded llm_and_speak).

        1. Call ElevenLabs streaming TTS (pcm_16000)
        2. For each chunk: convert PCM 16kHz → mulaw 8kHz → base64
        3. Send Twilio 'media' message over WebSocket
        4. Respect self.stop_speaking for barge-in
        """
        self.state = State.SPEAKING
        logger.info("[Call %s][Ali]: %s", self.call_sid, text)

        try:
            audio_generator = self.eleven_client.text_to_speech.stream(
                voice_id=settings.ELEVENLABS_VOICE_ID,
                text=text,
                model_id="eleven_multilingual_v2",
                output_format="pcm_16000",
                voice_settings=VoiceSettings(
                    stability=0.45,
                    similarity_boost=0.85,
                    style=0.35,
                    use_speaker_boost=True,
                    speed=0.95,
                ),
            )

            for chunk in audio_generator:
                if self.stop_speaking.is_set():
                    logger.info("[Call %s] Interrupted", self.call_sid)
                    break
                if chunk:
                    payload, self.ratecv_state_out = pcm16k_to_twilio_payload(
                        chunk, self.ratecv_state_out
                    )
                    msg = {
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {"payload": payload},
                    }
                    # Bridge sync thread → async WS send
                    future = asyncio.run_coroutine_threadsafe(
                        self._ws_send_fn(msg), self._loop
                    )
                    future.result(timeout=5)

        except Exception as e:
            logger.error("[Call %s][TTS Error]: %s", self.call_sid, e)
        finally:
            if not self.stop_speaking.is_set():
                self.state = State.LISTENING

    def _send_twilio_payload(self, payload: str):
        msg = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": payload},
        }
        future = asyncio.run_coroutine_threadsafe(
            self._ws_send_fn(msg), self._loop
        )
        future.result(timeout=5)

    def _build_cached_audio(self, text: str):
        cached_chunks = []
        ratecv_state_out = None

        audio_generator = self.eleven_client.text_to_speech.stream(
            voice_id=settings.ELEVENLABS_VOICE_ID,
            text=text,
            model_id="eleven_multilingual_v2",
            output_format="pcm_16000",
            voice_settings=VoiceSettings(
                stability=0.45,
                similarity_boost=0.85,
                style=0.35,
                use_speaker_boost=True,
                speed=0.95,
            ),
        )

        for chunk in audio_generator:
            if not chunk:
                continue

            payload, ratecv_state_out = pcm16k_to_twilio_payload(
                chunk, ratecv_state_out
            )
            duration_seconds = len(base64.b64decode(payload)) / 8000.0
            cached_chunks.append((payload, duration_seconds))

        return tuple(cached_chunks)

    def _get_cached_audio(self, cache_key: tuple[str, str], text: str):
        cached_audio = _GREETING_AUDIO_CACHE.get(cache_key)
        if cached_audio is not None:
            return cached_audio

        with _GREETING_AUDIO_CACHE_LOCK:
            cached_audio = _GREETING_AUDIO_CACHE.get(cache_key)
            if cached_audio is None:
                logger.info("[VoiceCache] Building cached audio for key=%s", cache_key[0])
                cached_audio = self._build_cached_audio(text)
                _GREETING_AUDIO_CACHE[cache_key] = cached_audio

        return cached_audio

    def play_cached_text(self, text: str, cache_key: tuple[str, str]):
        self.state = State.SPEAKING

        try:
            cached_audio = self._get_cached_audio(cache_key, text)

            for payload, duration_seconds in cached_audio:
                if self.stop_speaking.is_set():
                    logger.info("[Call %s] Interrupted cached audio", self.call_sid)
                    break

                self._send_twilio_payload(payload)
                time.sleep(duration_seconds)

        except Exception as e:
            logger.error("[Call %s][Cached Audio Error]: %s", self.call_sid, e)
        finally:
            if not self.stop_speaking.is_set():
                self.state = State.LISTENING

    def clear_twilio_audio_buffer(self):
        """Send Twilio 'clear' event to stop playing queued audio (barge-in)."""
        msg = {"event": "clear", "streamSid": self.stream_sid}
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._ws_send_fn(msg), self._loop
            )

    # ══════════════════════════════════════════════════════════════════
    # Deepgram connection management
    # ══════════════════════════════════════════════════════════════════

    def start_deepgram(self):
        """Open Deepgram live STT connection."""
        self._dg_connection_context = self.deepgram_client.listen.v1.connect(
            model="nova-3",
            language="ur",
            punctuate="true",
            interim_results="true",
            endpointing="400",
            smart_format="true",
            encoding="linear16",
            sample_rate="16000",
            channels="1",
        )
        self.dg_connection = self._dg_connection_context.__enter__()

        self.dg_connection.on(EventType.OPEN, self._on_dg_open)
        self.dg_connection.on(EventType.MESSAGE, self._on_dg_message)
        self.dg_connection.on(EventType.ERROR, self._on_dg_error)
        self.dg_connection.on(EventType.CLOSE, self._on_dg_close)

        threading.Thread(target=self.dg_connection.start_listening, daemon=True).start()

        # Keepalive thread
        def keepalive():
            while self._keep_running:
                time.sleep(5)
                try:
                    if self.dg_connection:
                        self.dg_connection.send_keep_alive()
                except Exception:
                    pass

        threading.Thread(target=keepalive, daemon=True).start()
        logger.info("[Call %s] Deepgram started", self.call_sid)

    def send_audio_to_deepgram(self, pcm_16k: bytes):
        """Send PCM audio to Deepgram for transcription."""
        if self.dg_connection:
            self.dg_connection.send_media(pcm_16k)

    def stop_deepgram(self):
        """Close Deepgram connection."""
        self._keep_running = False
        if self.dg_connection:
            try:
                self.dg_connection.send_close_stream()
            except Exception:
                pass
            finally:
                self.dg_connection = None
        if self._dg_connection_context:
            try:
                self._dg_connection_context.__exit__(None, None, None)
            except Exception:
                pass
            finally:
                self._dg_connection_context = None

    # ══════════════════════════════════════════════════════════════════
    # Deepgram event handlers
    # ══════════════════════════════════════════════════════════════════

    def _on_dg_open(self, _open_event=None):
        logger.info("[Call %s] Deepgram connected", self.call_sid)
        # Play greeting in a separate thread
        threading.Thread(target=self._greeting_thread, daemon=True).start()

    def _greeting_thread(self):
        from .agent import GREETING

        self.play_cached_text(
            GREETING,
            cache_key=(settings.ELEVENLABS_VOICE_ID, GREETING),
        )
        with self.llm_lock:
            self.conversation.append({"role": "assistant", "content": GREETING})

    def _on_dg_message(self, message):
        if getattr(message, "type", None) == "Results":
            self._on_dg_transcript(message)

    def _on_dg_transcript(self, result):
        """Handle Deepgram transcript — mirrors main2.py on_transcript logic."""
        from .agent import llm_and_speak

        try:
            alt = result.channel.alternatives[0]
            transcript = alt.transcript.strip()
            is_final = result.is_final
        except Exception:
            return

        if not transcript:
            return

        if not is_final:
            # For phone calls, only act on final transcripts
            return

        logger.info("[Call %s][Caller FINAL]: %s", self.call_sid, transcript)

        # Minimum length filter
        if len(transcript.split()) < 3:
            logger.debug("[Call %s] Skipped too short (%d words)", self.call_sid, len(transcript.split()))
            return

        # Duplicate filter
        if transcript == self.last_transcript:
            return
        self.last_transcript = transcript

        # State guard — queue transcript if still thinking
        if self.state == State.THINKING:
            logger.info("[Call %s][Guard] Still thinking, queuing: %s", self.call_sid, transcript)
            self.pending_transcript = transcript
            return

        # Barge-in: interrupt speaking and clear Twilio audio buffer
        if self.state == State.SPEAKING:
            self.stop_speaking.set()
            self.clear_twilio_audio_buffer()

        # Wait for old LLM thread to exit
        if self.current_llm_thread and self.current_llm_thread.is_alive():
            self.current_llm_thread.join(timeout=1.5)

        self.state = State.THINKING
        self.current_llm_thread = threading.Thread(
            target=llm_and_speak,
            args=(self, transcript),
            daemon=True,
        )
        self.current_llm_thread.start()

    def _on_dg_error(self, error):
        logger.error("[Call %s][Deepgram Error]: %s", self.call_sid, error)

    def _on_dg_close(self, _close_event=None):
        logger.info("[Call %s] Deepgram closed", self.call_sid)

    # ══════════════════════════════════════════════════════════════════
    # Cleanup
    # ══════════════════════════════════════════════════════════════════

    def cleanup(self):
        """Release all resources for this call."""
        self._keep_running = False
        self.stop_speaking.set()
        self.stop_deepgram()
        logger.info("[Call %s] Session cleaned up", self.call_sid)
