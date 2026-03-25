# voice/consumers_browser.py
#
# Browser WebSocket variant — raw PCM16 in/out, no mulaw conversion.
# Inherits all Gemini Live session logic from VoiceAgentConsumer.

from pathlib import Path
from .consumers1 import VoiceAgentConsumer, GREETING_PATH, MIC_RATE, OUT_RATE, _save_wav, execute_tool
from google.genai import types
from websockets.exceptions import ConnectionClosed
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

BROWSER_PCM_CHUNK = 4800  # ~100ms at 24kHz PCM16

class BrowserVoiceConsumer(VoiceAgentConsumer):

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
            })
            print(f"[BrowserWS] Sending session_ready: {msg}", flush=True)
            await self.send(text_data=msg)
            print("[BrowserWS] Sent session_ready to browser", flush=True)
        except Exception as e:
            print(f"[BrowserWS] ERROR Failed to send session_ready: {e}", flush=True)

    # ------------------------------------------------------------------
    # Override: receive raw PCM16 from browser, no mulaw decode
    # ------------------------------------------------------------------

    async def receive(self, bytes_data=None, text_data=None):
        if self._disconnecting or not bytes_data:
            return
        if len(bytes_data) % 2 != 0:
            print(f"[BrowserWS] Dropping odd length payload: {len(bytes_data)} bytes", flush=True)
            return
        if not self._session_ready.is_set():
            return

        session = self.gemini_session
        if session is None:
            self._clear_session_state()
            return

        # ---- DEBUG: DUMP MIC AUDIO ----
        if not hasattr(self, '_debug_mic_buffer'):
            self._debug_mic_buffer = bytearray()
        self._debug_mic_buffer.extend(bytes_data)
        # -------------------------------

        # Browser sends raw PCM16 16kHz — forward directly
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
        # Save accumulated mic audio to verify
        if hasattr(self, '_debug_mic_buffer') and len(self._debug_mic_buffer) > 0:
            debug_path = Path("media/debug_mic.wav")
            _save_wav(bytes(self._debug_mic_buffer), debug_path, MIC_RATE)
            print(f"[BrowserWS] Saved {len(self._debug_mic_buffer)} bytes of microphone audio to {debug_path}")
        await super().disconnect(close_code)

    # ------------------------------------------------------------------
    # Override: send raw PCM16 to browser, no mulaw encode
    # ------------------------------------------------------------------

    async def _stream_pcm_to_sip(self, pcm_24k: bytes):
        """Stream cached greeting PCM directly to browser in chunks."""
        print(f"[BrowserWS] Streaming cached greeting ({len(pcm_24k)} bytes)", flush=True)
        try:
            for i in range(0, len(pcm_24k), BROWSER_PCM_CHUNK):
                await self.send(bytes_data=pcm_24k[i : i + BROWSER_PCM_CHUNK])
                await asyncio.sleep(0.1)
            print("[BrowserWS] Finished streaming greeting", flush=True)
        except Exception as e:
            print(f">>> [BrowserWS] Error during _stream_pcm_to_sip: {e}", flush=True)
            raise

    async def _receive_loop(self, session):
        """
        Same as parent but sends raw PCM16 instead of mulaw to the client,
        AND handles tool calls so Gemini doesn't hang.
        """
        greeting_buffer = bytearray()

        try:
            while not self._disconnecting:
                async for response in session.receive():
                    sc = getattr(response, "server_content", None)
                    tc = getattr(response, "tool_call", None)
                    # print(f"[BrowserWS] Recv event: server_content={bool(sc)}, tool_call={bool(tc)}", flush=True)

                    if sc:
                        # print(f"[BrowserWS DEBUG] turn_complete={getattr(sc, 'turn_complete', False)}, interrupted={getattr(sc, 'interrupted', False)}", flush=True)
                        if getattr(sc, "model_turn", None):
                            for p in sc.model_turn.parts:
                                if getattr(p, "text", None):
                                    print(f"[BrowserWS DEBUG] Model Text: {p.text}", flush=True)
                                if getattr(p, "executable_code", None):
                                    print(f"[BrowserWS DEBUG] Exec Code: {p.executable_code}", flush=True)
                                if getattr(p, "execution_result", None):
                                    print(f"[BrowserWS DEBUG] Exec Result: {p.execution_result}", flush=True)

                    # ── Tool call handling ──────────────────────────────────
                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        function_responses = []
                        for fc in tool_call.function_calls:
                            tool_name = fc.name
                            tool_args = dict(fc.args) if fc.args else {}
                            print(f"[BrowserWS] [Tool Call] {tool_name}({tool_args})", flush=True)

                            result = await execute_tool(tool_name, tool_args)
                            print(f"[BrowserWS] [Tool Result] {tool_name} → {result}", flush=True)

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

                                # Raw PCM16 24kHz → browser handles it natively
                                await self.send(bytes_data=inline.data)

                    if getattr(sc, "turn_complete", False):
                        if self._save_as_greeting and greeting_buffer:
                            _save_wav(bytes(greeting_buffer), GREETING_PATH, OUT_RATE)
                            print(f"[BrowserWS] Greeting saved to {GREETING_PATH}", flush=True)
                            self._save_as_greeting = False
                            greeting_buffer.clear()

        except ConnectionClosed as exc:
            print(f"[BrowserWS] Browser receive loop closed: {exc}", flush=True)