# voice/consumers_browser.py
#
# Browser WebSocket variant — dynamic multi-agent, raw PCM16 in/out.
# Agent, voice, and language are resolved from:
#   - URL path: /ws/voice/<agent_id>/
#   - Query string: ?voice=Aoede&language=ur-PK

from pathlib import Path
from google.genai import types
from websockets.exceptions import ConnectionClosed
import asyncio
import json
import logging
import urllib.parse

from .consumers1 import VoiceAgentConsumer, MIC_RATE, OUT_RATE, _save_wav
from .agents.registry import get_agent

logger = logging.getLogger(__name__)

BROWSER_PCM_CHUNK = 4800  # ~100ms at 24kHz PCM16


def _parse_query(scope) -> dict:
    """Parse ?key=value pairs from the WebSocket scope query string."""
    qs = scope.get("query_string", b"").decode("utf-8")
    return dict(urllib.parse.parse_qsl(qs))


class BrowserVoiceConsumer(VoiceAgentConsumer):
    """
    One instance per browser WebSocket connection.
    Resolves agent config from URL path + query params at connect time.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._agent_cfg = None
        self._voice = "Aoede"
        self._language = "ur-PK"

    # ------------------------------------------------------------------
    # WebSocket lifecycle — resolve agent config first
    # ------------------------------------------------------------------

    async def connect(self):
        # Resolve agent_id from URL kwargs (set by routing.py)
        agent_id = self.scope["url_route"]["kwargs"].get("agent_id", "healthcare")
        self._agent_cfg = get_agent(agent_id)

        if self._agent_cfg is None:
            print(f"[BrowserWS] Unknown agent_id='{agent_id}', closing.", flush=True)
            await self.close(code=4004)
            return

        # Resolve voice and language from query string, with per-agent defaults
        params = _parse_query(self.scope)
        self._voice = params.get("voice", self._agent_cfg["default_voice"])
        self._language = params.get("language", self._agent_cfg["default_language"])

        print(
            f"[BrowserWS] Agent='{agent_id}' Voice='{self._voice}' Language='{self._language}'",
            flush=True,
        )

        # Delegate to parent (creates Gemini client, starts session task)
        await super().connect()

    # ------------------------------------------------------------------
    # Override: send session_ready JSON to browser after Gemini connects
    # ------------------------------------------------------------------

    async def _on_gemini_ready(self):
        """Called by parent after Gemini Live session opens. Notify browser."""
        try:
            msg = json.dumps({
                "event": "session_ready",
                "output_sample_rate": OUT_RATE,
                "input_sample_rate": MIC_RATE,
                "agent_id": self._agent_cfg["id"],
                "agent_name": self._agent_cfg["name"],
                "voice": self._voice,
                "language": self._language,
            })
            print(f"[BrowserWS] Sending session_ready: {msg}", flush=True)
            await self.send(text_data=msg)
            print("[BrowserWS] Sent session_ready to browser", flush=True)
        except Exception as e:
            print(f"[BrowserWS] ERROR Failed to send session_ready: {e}", flush=True)

    # ------------------------------------------------------------------
    # Override: expose dynamic config to parent _run_gemini_session
    # ------------------------------------------------------------------

    def _get_system_prompt(self, has_cached_greeting: bool = False) -> str:
        return self._agent_cfg["build_system_prompt"](
            language=self._language,
            voice=self._voice,
            has_cached_greeting=has_cached_greeting
        )

    def _get_tools(self):
        return self._agent_cfg["tools_fn"]()

    def _get_voice_name(self) -> str:
        return self._voice

    def _get_language_code(self) -> str:
        return self._language

    def _get_greeting_path(self) -> Path:
        # Use language+voice-aware greeting path if available
        fn = self._agent_cfg.get("greeting_path_fn")
        if fn:
            return fn(self._language, self._voice)
        return self._agent_cfg["greeting_path"]

    def _get_greeting_prompt(self) -> str:
        # Use language-aware greeting prompt if available
        fn = self._agent_cfg.get("greeting_prompt_fn")
        if fn:
            return fn(self._language)
        return self._agent_cfg["greeting_prompt"]

    def _get_generate_greeting_prompt(self) -> str:
        """Prompt used when NO cached greeting exists — model must greet the user."""
        fn = self._agent_cfg.get("generate_greeting_prompt_fn")
        if fn:
            return fn(self._language, self._voice)
        # Fallback: use the regular greeting prompt (backward compat)
        return self._get_greeting_prompt()

    async def _execute_tool(self, tool_name: str, tool_args: dict) -> dict:
        """Delegate tool execution to the active agent's executor."""
        return await self._agent_cfg["execute_tool"](tool_name, tool_args)

    # ------------------------------------------------------------------
    # Override: receive raw PCM16 from browser, no mulaw decode
    # ------------------------------------------------------------------

    async def receive(self, bytes_data=None, text_data=None):
        if self._disconnecting or not bytes_data:
            return
        if len(bytes_data) % 2 != 0:
            return
        if not self._session_ready.is_set():
            return

        session = self.gemini_session
        if session is None:
            self._clear_session_state()
            return

        # Debug: accumulate mic audio for WAV dump on disconnect
        if not hasattr(self, '_debug_mic_buffer'):
            self._debug_mic_buffer = bytearray()
        self._debug_mic_buffer.extend(bytes_data)

        try:
            if not hasattr(self, '_recv_count'):
                self._recv_count = 0
            self._recv_count += 1
            if self._recv_count % 50 == 0:
                print(f"[BrowserWS] Processed {self._recv_count} audio frames from browser...", flush=True)

            await session.send_realtime_input(
                audio=types.Blob(
                    data=bytes_data,
                    mime_type=f"audio/pcm;rate={MIC_RATE}",
                )
            )
        except ConnectionClosed as exc:
            print(f">>> [BrowserWS] Gemini session closed while forwarding audio: {exc}", flush=True)
            self._clear_session_state()
        except Exception as e:
            print(f">>> [BrowserWS] Error forwarding audio to Gemini: {e}", flush=True)

    async def disconnect(self, close_code):
        if hasattr(self, '_debug_mic_buffer') and len(self._debug_mic_buffer) > 0:
            debug_path = Path("media/debug_mic.wav")
            _save_wav(bytes(self._debug_mic_buffer), debug_path, MIC_RATE)
            print(f"[BrowserWS] Saved {len(self._debug_mic_buffer)} bytes of microphone audio to {debug_path}")
        await super().disconnect(close_code)

    # ------------------------------------------------------------------
    # Override: stream raw PCM16 to browser (no mulaw)
    # ------------------------------------------------------------------

    async def _stream_pcm_to_sip(self, pcm_24k: bytes):
        """Stream cached greeting PCM directly to browser in chunks."""
        print(f"[BrowserWS] Streaming cached greeting ({len(pcm_24k)} bytes)", flush=True)
        try:
            for i in range(0, len(pcm_24k), BROWSER_PCM_CHUNK):
                await self.send(bytes_data=pcm_24k[i: i + BROWSER_PCM_CHUNK])
                await asyncio.sleep(0.1)
            print("[BrowserWS] Finished streaming greeting", flush=True)
        except Exception as e:
            print(f">>> [BrowserWS] Error during _stream_pcm_to_sip: {e}", flush=True)
            raise

    # ------------------------------------------------------------------
    # Override: receive loop — sends raw PCM16 and handles tool calls
    # ------------------------------------------------------------------

    async def _receive_loop(self, session):
        greeting_buffer = bytearray()
        greeting_path = self._get_greeting_path()

        try:
            while not self._disconnecting:
                async for response in session.receive():
                    sc = getattr(response, "server_content", None)
                    tc = getattr(response, "tool_call", None)

                    if sc and getattr(sc, "model_turn", None):
                        for p in sc.model_turn.parts:
                            if getattr(p, "text", None):
                                print(f"[BrowserWS DEBUG] Model Text: {p.text}", flush=True)

                    # ── Tool call handling ──────────────────────────────────
                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        function_responses = []
                        for fc in tool_call.function_calls:
                            tool_name = fc.name
                            tool_args = dict(fc.args) if fc.args else {}
                            print(f"[BrowserWS] [Tool Call] {tool_name}({tool_args})", flush=True)

                            result = await self._execute_tool(tool_name, tool_args)
                            print(f"[BrowserWS] [Tool Result] {tool_name} → {result}", flush=True)

                            function_responses.append(
                                types.FunctionResponse(
                                    name=tool_name,
                                    id=fc.id,
                                    response={"result": result},
                                )
                            )

                        try:
                            await session.send_tool_response(function_responses=function_responses)
                            print(f"[BrowserWS] Successfully sent tool responses for {len(function_responses)} calls", flush=True)
                        except Exception as e:
                            print(f">>> [BrowserWS ERROR] Failed to send tool response: {repr(e)}", flush=True)
                        continue

                    # ── Audio + transcription handling ──────────────────────
                    sc = getattr(response, "server_content", None)
                    if sc is None:
                        continue

                    if getattr(sc, "input_transcription", None):
                        t = sc.input_transcription
                        if hasattr(t, "text") and t.text:
                            print(f"[BrowserWS] [User] {t.text}", flush=True)

                    if getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                if self._save_as_greeting:
                                    greeting_buffer.extend(inline.data)
                                await self.send(bytes_data=inline.data)

                    if getattr(sc, "interrupted", False):
                        import json
                        print("[BrowserWS] Gemini interrupted — sending clear queue command", flush=True)
                        await self.send(text_data=json.dumps({"event": "clear"}))

                    if getattr(sc, "turn_complete", False):
                        if self._save_as_greeting and greeting_buffer:
                            _save_wav(bytes(greeting_buffer), greeting_path, OUT_RATE)
                            print(f"[BrowserWS] Greeting saved to {greeting_path}", flush=True)
                            self._save_as_greeting = False
                            greeting_buffer.clear()

        except ConnectionClosed as exc:
            print(f"[BrowserWS] Browser receive loop closed: {exc}", flush=True)