# """
# SIP Client — pyVoIP to Gemini Live bridge.

# Replaces the entire Asterisk + Docker + ARI + UDP pipeline with a direct
# Python SIP client that bridges calls to the Gemini Live API.

# Flow:
#   Phone call → pyVoIP (SIP + RTP) → SIPCallBridge → Gemini Live API
#                                    ← SIPCallBridge ←
# """

# import asyncio
# import audioop
# import json
# import logging
# import os
# import socket
# import threading
# import time
# import uuid
# import wave
# from datetime import datetime
# from pathlib import Path
# from zoneinfo import ZoneInfo

# from pyVoIP.VoIP import VoIPPhone, InvalidStateError, CallState

# logger = logging.getLogger(__name__)

# # ── Audio format constants (same as consumers.py) ───────────────────
# SIP_RATE = 8000      # G.711 µ-law from SIP
# MIC_RATE = 16000     # What Gemini expects for input
# OUT_RATE = 24000     # What Gemini produces for output
# FRAME_DURATION = 0.02  # 20ms frames for RTP

# # ── Import Gemini client ─────────────────────────────────────────────
# try:
#     from google import genai
#     from google.genai import types
# except ImportError:
#     raise ImportError("pip install google-genai")


# def _get_local_ip() -> str:
#     """Get the local IP address of this machine (not 127.0.0.1)."""
#     try:
#         s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         s.connect(("8.8.8.8", 80))
#         ip = s.getsockname()[0]
#         s.close()
#         return ip
#     except Exception:
#         return "127.0.0.1"


# # ─────────────────────────────────────────────────────────────────────
# # SIPCallBridge — one per active call
# # ─────────────────────────────────────────────────────────────────────

# class SIPCallBridge:
#     """
#     Bridges a single SIP call to the Gemini Live API.

#     - Reads G.711 µ-law audio from the SIP call (pyVoIP)
#     - Transcodes to PCM 16kHz and sends to Gemini
#     - Receives PCM 24kHz audio from Gemini
#     - Transcodes to G.711 µ-law 8kHz and writes back to SIP
#     """

#     def __init__(self, call, agent_id: str = "healthcare",
#                  voice: str = "Aoede", language: str = "ur-PK"):
#         self.call = call
#         self.agent_id = agent_id
#         self.voice = voice
#         self.language = language

#         self._session_uuid = str(uuid.uuid4())
#         self._running = False
#         self._gemini_session = None
#         self._loop = None

#         # Audio resampling state (for glitch-free streaming)
#         self._upsample_state = None    # 8kHz → 16kHz
#         self._downsample_state = None  # 24kHz → 8kHz

#         # Usage tracking
#         self._start_time = time.time()
#         self._usage_metrics = {
#             "prompt": 0, "response": 0, "total": 0,
#             "input_text": 0, "input_audio": 0,
#             "output_text": 0, "output_audio": 0,
#         }
#         self._call_history = []
#         self._current_agent_turn = ""

#         logger.info(
#             "[SIP Call %s] Bridge created: agent=%s voice=%s lang=%s",
#             self._session_uuid[:8], agent_id, voice, language,
#         )

#     def start(self):
#         """Start the call bridge in a new thread with its own event loop."""
#         self._running = True
#         thread = threading.Thread(target=self._run_async_loop, daemon=True)
#         thread.start()
#         logger.info("[SIP Call %s] Bridge thread started", self._session_uuid[:8])

#     def _run_async_loop(self):
#         """Create a new event loop and run the Gemini session in it."""
#         self._loop = asyncio.new_event_loop()
#         asyncio.set_event_loop(self._loop)
#         try:
#             self._loop.run_until_complete(self._run_gemini_session())
#         except Exception as e:
#             logger.error("[SIP Call %s] Bridge error: %s", self._session_uuid[:8], e)
#             import traceback
#             traceback.print_exc()
#         finally:
#             self._running = False
#             self._loop.close()
#             logger.info("[SIP Call %s] Bridge thread ended", self._session_uuid[:8])

#     async def _run_gemini_session(self):
#         """Open Gemini Live session and run audio I/O concurrently."""
#         from .agents.registry import get_agent

#         agent_cfg = get_agent(self.agent_id)
#         if not agent_cfg:
#             logger.error("[SIP Call %s] Unknown agent: %s", self._session_uuid[:8], self.agent_id)
#             return

#         # Build system prompt with schedule data
#         schedule_data = await self._fetch_schedule_data()
#         greeting_path_fn = agent_cfg.get("greeting_path_fn")
#         if greeting_path_fn:
#             greeting_path = greeting_path_fn(self.language, self.voice)
#         else:
#             greeting_path = agent_cfg["greeting_path"]

#         has_cached_greeting = greeting_path.exists()

#         system_prompt = agent_cfg["build_system_prompt"](
#             language=self.language,
#             voice=self.voice,
#             has_cached_greeting=has_cached_greeting,
#             schedule_data=schedule_data,
#         )
#         tools = agent_cfg["tools_fn"]()
#         self._execute_tool_fn = agent_cfg["execute_tool"]

#         # Create Gemini client
#         api_key = os.environ.get("GEMINI_API_KEY")
#         if not api_key:
#             logger.error("[SIP Call %s] GEMINI_API_KEY not set!", self._session_uuid[:8])
#             return

#         client = genai.Client(api_key=api_key)

#         live_config = types.LiveConnectConfig(
#             system_instruction=types.Content(
#                 parts=[types.Part(text=system_prompt)]
#             ),
#             response_modalities=["AUDIO"],
#             tools=tools,
#             speech_config=types.SpeechConfig(
#                 voice_config=types.VoiceConfig(
#                     prebuilt_voice_config=types.PrebuiltVoiceConfig(
#                         voice_name=self.voice
#                     )
#                 ),
#                 language_code=self.language,
#             ),
#             input_audio_transcription=types.AudioTranscriptionConfig(),
#             output_audio_transcription=types.AudioTranscriptionConfig(),
#             context_window_compression=types.ContextWindowCompressionConfig(
#                 sliding_window=types.SlidingWindow(),
#             ),
#             realtime_input_config=types.RealtimeInputConfig(
#                 automatic_activity_detection=types.AutomaticActivityDetection(
#                     disabled=False,
#                     start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
#                     end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
#                 )
#             ),
#         )

#         logger.info("[SIP Call %s] Connecting to Gemini Live...", self._session_uuid[:8])
#         t0 = time.time()

#         try:
#             async with client.aio.live.connect(
#                 model="gemini-3.1-flash-live-preview", config=live_config
#             ) as session:
#                 elapsed = time.time() - t0
#                 logger.info(
#                     "[SIP Call %s] Gemini Live connected in %.2fs",
#                     self._session_uuid[:8], elapsed,
#                 )
#                 self._gemini_session = session

#                 # Play greeting
#                 await self._handle_greeting(session, agent_cfg, greeting_path, has_cached_greeting)

#                 # Run audio read and Gemini receive concurrently
#                 tasks = [
#                     asyncio.create_task(self._sip_to_gemini(session)),
#                     asyncio.create_task(self._gemini_to_sip(session, agent_cfg)),
#                 ]

#                 done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
#                 for t in pending:
#                     t.cancel()

#         except Exception as e:
#             logger.error("[SIP Call %s] Gemini session error: %s", self._session_uuid[:8], e)
#             import traceback
#             traceback.print_exc()
#         finally:
#             self._gemini_session = None
#             await self._save_session_cost()
#             self._cleanup_call()

#     async def _fetch_schedule_data(self) -> list:
#         """Fetch schedule from Django ORM."""
#         try:
#             from appointment.models import Schedule
#             from appointment.serializers import ScheduleSerializer
#             from asgiref.sync import sync_to_async

#             schedules = await sync_to_async(lambda: list(Schedule.objects.all()))()
#             return ScheduleSerializer(schedules, many=True).data
#         except Exception as e:
#             logger.warning("[SIP Call %s] Could not fetch schedule: %s", self._session_uuid[:8], e)
#             return []

#     async def _handle_greeting(self, session, agent_cfg, greeting_path, has_cached_greeting):
#         """Play cached greeting or ask Gemini to generate one."""
#         if has_cached_greeting:
#             logger.info("[SIP Call %s] Playing cached greeting from %s", self._session_uuid[:8], greeting_path)
#             pcm_data = self._load_wav_pcm(greeting_path)
#             self._write_pcm24k_to_sip(pcm_data)
#             logger.info("[SIP Call %s] Cached greeting played", self._session_uuid[:8])
#         else:
#             # Ask Gemini to generate greeting
#             generate_fn = agent_cfg.get("generate_greeting_prompt_fn")
#             if generate_fn:
#                 prompt = generate_fn(self.language, self.voice)
#             else:
#                 prompt = agent_cfg.get("greeting_prompt", "Greet the user warmly.")

#             logger.info("[SIP Call %s] Asking Gemini to generate greeting", self._session_uuid[:8])
#             self._save_as_greeting = True
#             self._greeting_buffer = bytearray()
#             self._greeting_save_path = greeting_path
#             await session.send_realtime_input(text=prompt)

#     def _load_wav_pcm(self, path: Path) -> bytes:
#         """Load raw PCM from a WAV file."""
#         with wave.open(str(path), "rb") as wf:
#             return wf.readframes(wf.getnframes())

#     def _write_pcm24k_to_sip(self, pcm_24k: bytes):
#         """Transcode PCM 24kHz → G.711 µ-law 8kHz and write to SIP call."""
#         try:
#             # Downsample 24kHz → 8kHz
#             pcm_8k, self._downsample_state = audioop.ratecv(
#                 pcm_24k, 2, 1, OUT_RATE, SIP_RATE, self._downsample_state
#             )
#             # PCM linear → µ-law
#             ulaw_8k = audioop.lin2ulaw(pcm_8k, 2)

#             # Write in 160-byte chunks (20ms at 8kHz µ-law)
#             chunk_size = 160
#             for i in range(0, len(ulaw_8k), chunk_size):
#                 if not self._running:
#                     break
#                 chunk = ulaw_8k[i:i + chunk_size]
#                 try:
#                     self.call.write_audio(chunk)
#                 except (InvalidStateError, OSError):
#                     logger.info("[SIP Call %s] Call ended during audio write", self._session_uuid[:8])
#                     self._running = False
#                     break
#                 time.sleep(FRAME_DURATION)  # Pace audio at real-time
#         except Exception as e:
#             logger.error("[SIP Call %s] Audio write error: %s", self._session_uuid[:8], e)

#     async def _sip_to_gemini(self, session):
#         """Read audio from SIP call and send to Gemini Live."""
#         logger.info("[SIP Call %s] SIP→Gemini audio loop started", self._session_uuid[:8])
#         frames_sent = 0

#         try:
#             while self._running:
#                 # Check call state
#                 try:
#                     if self.call.state != CallState.ANSWERED:
#                         logger.info("[SIP Call %s] Call no longer answered, stopping", self._session_uuid[:8])
#                         break
#                 except Exception:
#                     break

#                 # Read audio from SIP (blocking, runs in executor)
#                 try:
#                     ulaw_data = await asyncio.get_event_loop().run_in_executor(
#                         None, self._read_sip_audio
#                     )
#                 except Exception:
#                     break

#                 if not ulaw_data:
#                     await asyncio.sleep(0.01)
#                     continue

#                 # Transcode: µ-law 8kHz → PCM 16kHz
#                 pcm_8k = audioop.ulaw2lin(ulaw_data, 2)
#                 pcm_16k, self._upsample_state = audioop.ratecv(
#                     pcm_8k, 2, 1, SIP_RATE, MIC_RATE, self._upsample_state
#                 )

#                 # Send to Gemini
#                 try:
#                     await session.send_realtime_input(
#                         audio=types.Blob(
#                             data=pcm_16k,
#                             mime_type=f"audio/pcm;rate={MIC_RATE}",
#                         )
#                     )
#                     frames_sent += 1
#                     if frames_sent == 1:
#                         logger.info("[SIP Call %s] First audio frame sent to Gemini", self._session_uuid[:8])
#                     elif frames_sent % 500 == 0:
#                         logger.info("[SIP Call %s] Sent %d audio frames to Gemini", self._session_uuid[:8], frames_sent)
#                 except Exception as e:
#                     logger.error("[SIP Call %s] Gemini send error: %s", self._session_uuid[:8], e)
#                     break

#         except asyncio.CancelledError:
#             pass
#         finally:
#             self._running = False
#             logger.info("[SIP Call %s] SIP→Gemini loop ended (%d frames)", self._session_uuid[:8], frames_sent)

#     def _read_sip_audio(self) -> bytes:
#         """Read a chunk of audio from the SIP call (blocking)."""
#         try:
#             data = self.call.read_audio(length=160, blocking=True)
#             return data if data else b""
#         except InvalidStateError:
#             self._running = False
#             return b""
#         except Exception:
#             return b""

#     async def _gemini_to_sip(self, session, agent_cfg):
#         """Receive audio/tool-calls from Gemini and send to SIP."""
#         logger.info("[SIP Call %s] Gemini→SIP receive loop started", self._session_uuid[:8])
#         greeting_buffer = bytearray()
#         save_as_greeting = getattr(self, "_save_as_greeting", False)

#         try:
#             while self._running:
#                 async for response in session.receive():
#                     if not self._running:
#                         break

#                     # ── Usage metrics ────────────────────────────────
#                     usage = getattr(response, "usage_metadata", None)
#                     if usage:
#                         self._usage_metrics["prompt"] = max(
#                             self._usage_metrics["prompt"],
#                             getattr(usage, "prompt_token_count", 0) or 0,
#                         )
#                         self._usage_metrics["response"] = max(
#                             self._usage_metrics["response"],
#                             getattr(usage, "response_token_count", 0) or 0,
#                         )
#                         self._usage_metrics["total"] = max(
#                             self._usage_metrics["total"],
#                             getattr(usage, "total_token_count", 0) or 0,
#                         )

#                     # ── Tool calls ───────────────────────────────────
#                     tool_call = getattr(response, "tool_call", None)
#                     if tool_call:
#                         function_responses = []
#                         for fc in tool_call.function_calls:
#                             tool_name = fc.name
#                             tool_args = dict(fc.args) if fc.args else {}
#                             logger.info(
#                                 "[SIP Call %s] Tool call: %s(%s)",
#                                 self._session_uuid[:8], tool_name, tool_args,
#                             )

#                             result = await self._execute_tool_fn(tool_name, tool_args)
#                             logger.info(
#                                 "[SIP Call %s] Tool result: %s → %s",
#                                 self._session_uuid[:8], tool_name, result,
#                             )

#                             self._call_history.append({
#                                 "role": "tool",
#                                 "tool_name": tool_name,
#                                 "tool_args": tool_args,
#                                 "tool_result": result,
#                             })

#                             function_responses.append(
#                                 types.FunctionResponse(
#                                     name=tool_name,
#                                     id=fc.id,
#                                     response={"result": result},
#                                 )
#                             )

#                         try:
#                             await session.send_tool_response(
#                                 function_responses=function_responses
#                             )
#                             logger.info(
#                                 "[SIP Call %s] Sent %d tool responses",
#                                 self._session_uuid[:8], len(function_responses),
#                             )
#                         except Exception as e:
#                             logger.error(
#                                 "[SIP Call %s] Tool response send error: %s",
#                                 self._session_uuid[:8], e,
#                             )
#                         continue

#                     # ── Server content (audio + transcription) ───────
#                     sc = getattr(response, "server_content", None)
#                     if sc is None:
#                         continue

#                     # Input transcription (user speech)
#                     if getattr(sc, "input_transcription", None):
#                         t = sc.input_transcription
#                         if hasattr(t, "text") and t.text:
#                             logger.info("[SIP Call %s] [User] %s", self._session_uuid[:8], t.text)
#                             self._call_history.append({"role": "user", "text": t.text})

#                     # Output transcription (agent speech)
#                     if getattr(sc, "output_transcription", None):
#                         t = sc.output_transcription
#                         if hasattr(t, "text") and t.text:
#                             logger.info("[SIP Call %s] [Agent] %s", self._session_uuid[:8], t.text)
#                             if not self._current_agent_turn.endswith(t.text):
#                                 self._current_agent_turn += t.text

#                     # Model turn — audio data
#                     if getattr(sc, "model_turn", None):
#                         for part in sc.model_turn.parts:
#                             if getattr(part, "text", None):
#                                 if not self._current_agent_turn.endswith(part.text):
#                                     self._current_agent_turn += part.text

#                             inline = getattr(part, "inline_data", None)
#                             if inline and inline.data:
#                                 if save_as_greeting:
#                                     greeting_buffer.extend(inline.data)
#                                 # Write Gemini audio to SIP call
#                                 await asyncio.get_event_loop().run_in_executor(
#                                     None,
#                                     self._write_pcm24k_to_sip,
#                                     inline.data,
#                                 )

#                     # Turn complete / interrupted
#                     if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
#                         # Save greeting if first generated
#                         if save_as_greeting and greeting_buffer:
#                             self._save_wav(bytes(greeting_buffer), getattr(self, "_greeting_save_path", None))
#                             save_as_greeting = False
#                             self._save_as_greeting = False
#                             greeting_buffer.clear()

#                         if self._current_agent_turn:
#                             self._call_history.append({
#                                 "role": "agent",
#                                 "text": self._current_agent_turn.strip(),
#                             })
#                             # Check for end condition
#                             idx = self._current_agent_turn.lower()
#                             goodbye_detected = any(phrase in idx for phrase in ["allah hafiz", "اللہ حافظ", "khuda hafiz", "goodbye", "bye"])
#                             terminal_tool_called = any(
#                                 h.get("tool_name") in ["book_appointment", "place_order"]
#                                 for h in self._call_history
#                             )
                            
#                             if goodbye_detected:
#                                 logger.info(
#                                     "[SIP Call %s] Goodbye detected, ending call in 5s",
#                                     self._session_uuid[:8]
#                                 )
#                                 await asyncio.sleep(5)
#                                 self._running = False
#                                 break
#                             self._current_agent_turn = ""

#         except asyncio.CancelledError:
#             pass
#         except Exception as e:
#             logger.error("[SIP Call %s] Gemini receive error: %s", self._session_uuid[:8], e)
#         finally:
#             self._running = False
#             logger.info("[SIP Call %s] Gemini→SIP loop ended", self._session_uuid[:8])

#     def _save_wav(self, pcm_data: bytes, path):
#         """Save PCM data as a WAV file."""
#         if not path:
#             return
#         try:
#             path = Path(path)
#             path.parent.mkdir(parents=True, exist_ok=True)
#             with wave.open(str(path), "wb") as wf:
#                 wf.setnchannels(1)
#                 wf.setsampwidth(2)
#                 wf.setframerate(OUT_RATE)
#                 wf.writeframes(pcm_data)
#             logger.info("[SIP Call %s] Saved greeting: %s", self._session_uuid[:8], path)
#         except Exception as e:
#             logger.error("[SIP Call %s] WAV save error: %s", self._session_uuid[:8], e)

#     def _cleanup_call(self):
#         """Hang up the SIP call if still active."""
#         try:
#             if self.call.state == CallState.ANSWERED:
#                 self.call.hangup()
#                 logger.info("[SIP Call %s] Call hung up", self._session_uuid[:8])
#         except (InvalidStateError, Exception) as e:
#             logger.debug("[SIP Call %s] Hangup during cleanup: %s", self._session_uuid[:8], e)

#     async def _save_session_cost(self):
#         """Save session cost and call history to the database."""
#         duration = int(time.time() - self._start_time)

#         if self._usage_metrics["total"] > 0 or duration > 0:
#             try:
#                 from asgiref.sync import sync_to_async
#                 from Analytics.models import GeminiSessionCost, CallHistory

#                 # Gemini 3.1 Flash pricing
#                 input_text_cost = float(self._usage_metrics["input_text"]) * 0.00000075
#                 input_audio_cost = float(self._usage_metrics["input_audio"]) * 0.000003
#                 output_text_cost = float(self._usage_metrics["output_text"]) * 0.0000045
#                 output_audio_cost = float(self._usage_metrics["output_audio"]) * 0.000012
#                 total_cost = input_text_cost + input_audio_cost + output_text_cost + output_audio_cost

#                 await sync_to_async(GeminiSessionCost.objects.create)(
#                     session_id=self._session_uuid,
#                     agent_type=self.agent_id,
#                     prompt_tokens=self._usage_metrics["prompt"],
#                     response_tokens=self._usage_metrics["response"],
#                     total_tokens=self._usage_metrics["total"],
#                     input_text_tokens=self._usage_metrics["input_text"],
#                     input_audio_tokens=self._usage_metrics["input_audio"],
#                     output_text_tokens=self._usage_metrics["output_text"],
#                     output_audio_tokens=self._usage_metrics["output_audio"],
#                     call_duration_seconds=duration,
#                     estimated_cost_usd=total_cost,
#                 )
#                 logger.info(
#                     "[SIP Call %s] Session cost saved: $%.6f, duration=%ds",
#                     self._session_uuid[:8], total_cost, duration,
#                 )
#             except Exception as e:
#                 logger.error("[SIP Call %s] Failed to save cost: %s", self._session_uuid[:8], e)

#         if self._call_history:
#             try:
#                 from asgiref.sync import sync_to_async
#                 from Analytics.models import CallHistory

#                 await sync_to_async(CallHistory.objects.create)(
#                     session_id=self._session_uuid,
#                     agent_type=self.agent_id,
#                     duration_seconds=duration,
#                     transcript=self._call_history,
#                 )
#                 logger.info(
#                     "[SIP Call %s] Call history saved: %d turns, %ds",
#                     self._session_uuid[:8], len(self._call_history), duration,
#                 )
#             except Exception as e:
#                 logger.error("[SIP Call %s] Failed to save history: %s", self._session_uuid[:8], e)


# # ─────────────────────────────────────────────────────────────────────
# # SIP Server — manages pyVoIP and inbound calls
# # ─────────────────────────────────────────────────────────────────────

# class SIPServer:
#     """
#     Manages a pyVoIP phone instance or raw UDP SIP server
#     for each inbound call.
#     """

#     def __init__(self, agent_id="healthcare", voice="Aoede", language="ur-PK"):
#         from .sip_config import (
#             SIP_MODE, SIP_BIND_IP, SIP_BIND_PORT,
#             SIP_SERVER, SIP_SERVER_PORT,
#             SIP_USERNAME, SIP_PASSWORD,
#             SIP_TEST_USERNAME, SIP_TEST_PASSWORD,
#             SIP_RTP_PORT_LOW, SIP_RTP_PORT_HIGH,
#         )

#         self.agent_id = agent_id
#         self.voice = voice
#         self.language = language
#         self.mode = SIP_MODE
#         self.phone = None

#         from .sip_config import SIP_BIND_IP
#         local_ip = SIP_BIND_IP if SIP_BIND_IP and SIP_BIND_IP != "0.0.0.0" else _get_local_ip()
#         print(f"[DEBUG] Using IP: {local_ip}", flush=True)

#         if SIP_MODE == "multinet":
#             # Register as a SIP client to Multinet
#             if not SIP_SERVER or not SIP_USERNAME:
#                 raise ValueError(
#                     "SIP_SERVER and SIP_USERNAME must be set for multinet mode. "
#                     "Set SIP_MODE=local for local testing."
#                 )
#             self.phone = VoIPPhone(
#                 SIP_SERVER,
#                 SIP_SERVER_PORT,
#                 SIP_USERNAME,
#                 SIP_PASSWORD,
#                 sipPort=SIP_BIND_PORT,
#                 callCallback=self._on_incoming_call,
#                 myIP=local_ip,
#                 rtpPortLow=SIP_RTP_PORT_LOW,
#                 rtpPortHigh=SIP_RTP_PORT_HIGH,
#             )
#             logger.info(
#                 "[SIP Server] Multinet mode: registering to %s:%d as %s",
#                 SIP_SERVER, SIP_SERVER_PORT, SIP_USERNAME,
#             )
#         else:
#             # Local mode — use raw UDP SIP registrar (handles MicroSIP REGISTER + INVITE)
#             bind_ip = local_ip  # bind to LAN IP so MicroSIP on same machine can reach it
#             self._local_server = RawSIPServer(
#                 bind_ip=bind_ip,
#                 bind_port=SIP_BIND_PORT,
#                 username=SIP_TEST_USERNAME,
#                 password=SIP_TEST_PASSWORD,
#                 on_call=self._on_incoming_call,
#                 agent_id=agent_id,
#                 voice=voice,
#                 language=language,
#                 rtp_port_low=SIP_RTP_PORT_LOW,
#                 rtp_port_high=SIP_RTP_PORT_HIGH,
#             )
#             self._local_ip = local_ip
#             self._bind_port = SIP_BIND_PORT
#             self._test_username = SIP_TEST_USERNAME
#             self._test_password = SIP_TEST_PASSWORD
#             logger.info(
#                 "[SIP Server] Local mode: Raw SIP server on %s:%d (user=%s)",
#                 bind_ip, SIP_BIND_PORT, SIP_TEST_USERNAME,
#             )

#     def start(self):
#         """Start the SIP phone (runs SIP registration + RTP in background threads)."""
#         print("\n" + "=" * 60)
#         print("  SIP Server Started Successfully!")
#         print("=" * 60)

#         if self.mode == "local":
#             self._local_server.start()
#             print(f"\n  Mode:     LOCAL (for MicroSIP / softphone testing)")
#             print(f"  SIP Host: {self._local_ip}:{self._bind_port}")
#             print(f"  Username: {self._test_username}")
#             print(f"  Password: {self._test_password}")
#             print(f"\n  MicroSIP Account Setup:")
#             print(f"    SIP Server:  {self._local_ip}")
#             print(f"    Username:    {self._test_username}")
#             print(f"    Password:    {self._test_password}")
#             print(f"    Domain:      {self._local_ip}")
#             print(f"\n  After adding the account, just dial: {self._test_username}")
#         else:
#             logger.info("[SIP Server] Starting pyVoIP phone...")
#             self.phone.start()
#             from .sip_config import SIP_SERVER, SIP_SERVER_PORT, SIP_USERNAME
#             print(f"\n  Mode:     MULTINET TRUNK")
#             print(f"  Server:   {SIP_SERVER}:{SIP_SERVER_PORT}")
#             print(f"  Username: {SIP_USERNAME}")

#         print(f"\n  Agent:    {self.agent_id}")
#         print(f"  Voice:    {self.voice}")
#         print(f"  Language: {self.language}")
#         print(f"\n  Waiting for incoming calls...")
#         print("=" * 60 + "\n")

#     def stop(self):
#         """Stop the SIP phone."""
#         if self.mode == "local":
#             self._local_server.stop()
#         elif self.phone:
#             logger.info("[SIP Server] Stopping pyVoIP phone...")
#             self.phone.stop()
#             logger.info("[SIP Server] Phone stopped")

#     def _on_incoming_call(self, call):
#         """
#         Callback — called in a new thread for each incoming call.
#         Auto-answers and bridges to Gemini.
#         """
#         caller = getattr(call, "caller", "unknown")
#         logger.info("[SIP Server] Incoming call from: %s", caller)
#         print(f"\n📞 Incoming call from: {caller}", flush=True)

#         try:
#             call.answer()
#             logger.info("[SIP Server] Call answered")
#             print(f"✅ Call answered, bridging to {self.agent_id} agent...", flush=True)

#             # Create bridge and start it
#             bridge = SIPCallBridge(
#                 call=call,
#                 agent_id=self.agent_id,
#                 voice=self.voice,
#                 language=self.language,
#             )
#             bridge.start()

#             # Wait for the bridge to finish (poll call state)
#             while bridge._running:
#                 try:
#                     if call.state != CallState.ANSWERED:
#                         logger.info("[SIP Server] Call state changed to %s, stopping bridge", call.state)
#                         bridge._running = False
#                         break
#                 except Exception:
#                     break
#                 time.sleep(0.5)

#             logger.info("[SIP Server] Call ended")
#             print(f"📴 Call ended\n", flush=True)

#         except InvalidStateError:
#             logger.info("[SIP Server] Call was already disconnected")
#             print(f"📴 Call was already disconnected\n", flush=True)
#         except Exception as e:
#             logger.error("[SIP Server] Call handling error: %s", e)
#             import traceback
#             traceback.print_exc()
#             try:
#                 call.hangup()
#             except Exception:
#                 pass


# # ─────────────────────────────────────────────────────────────────────
# # RawSIPServer — minimal SIP registrar + INVITE handler for local testing
# # ─────────────────────────────────────────────────────────────────────

# class RawSIPServer:
#     """
#     A minimal raw-UDP SIP server for local testing with MicroSIP.

#     - Responds 200 OK to REGISTER so MicroSIP shows as "registered"
#     - Responds 100 Trying + 180 Ringing + 200 OK to INVITE
#     - Creates a RawSIPCall shim and calls the on_call callback
#     """

#     def __init__(self, bind_ip, bind_port, username, password,
#                  on_call, agent_id, voice, language,
#                  rtp_port_low=10000, rtp_port_high=20000):
#         self.bind_ip = bind_ip
#         self.bind_port = bind_port
#         self.username = username
#         self.password = password
#         self.on_call = on_call
#         self.agent_id = agent_id
#         self.voice = voice
#         self.language = language
#         self.rtp_port_low = rtp_port_low
#         self.rtp_port_high = rtp_port_high
#         self._running = False
#         self._sock = None
#         self._thread = None
#         self._rtp_port_counter = rtp_port_low

#     def _next_rtp_port(self):
#         port = self._rtp_port_counter
#         self._rtp_port_counter += 2
#         if self._rtp_port_counter > self.rtp_port_high:
#             self._rtp_port_counter = self.rtp_port_low
#         return port

#     def start(self):
#         self._running = True
#         self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
#         self._sock.bind((self.bind_ip, self.bind_port))
#         self._sock.settimeout(1.0)
#         self._thread = threading.Thread(target=self._listen_loop, daemon=True)
#         self._thread.start()
#         logger.info("[RawSIP] Listening on %s:%d", self.bind_ip, self.bind_port)

#     def stop(self):
#         self._running = False
#         if self._sock:
#             try:
#                 self._sock.close()
#             except Exception:
#                 pass

#     def _listen_loop(self):
#         logger.info("[RawSIP] UDP listener started")
#         while self._running:
#             try:
#                 data, addr = self._sock.recvfrom(65535)
#             except socket.timeout:
#                 continue
#             except Exception as e:
#                 if self._running:
#                     logger.error("[RawSIP] Socket error: %s", e)
#                 break
#             try:
#                 msg = data.decode("utf-8", errors="replace")
#                 self._handle_message(msg, addr)
#             except Exception as e:
#                 logger.error("[RawSIP] Message handling error: %s", e)
#         logger.info("[RawSIP] UDP listener stopped")

#     def _handle_message(self, msg: str, addr):
#         first_line = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]
#         logger.debug("[RawSIP] From %s:%d — %s", addr[0], addr[1], first_line)

#         if first_line.startswith("REGISTER"):
#             self._handle_register(msg, addr)
#         elif first_line.startswith("INVITE"):
#             self._handle_invite(msg, addr)
#         elif first_line.startswith("ACK"):
#             logger.debug("[RawSIP] ACK received from %s", addr)
#         elif first_line.startswith("BYE"):
#             self._handle_bye(msg, addr)
#         elif first_line.startswith("CANCEL"):
#             self._handle_cancel(msg, addr)
#         elif first_line.startswith("OPTIONS"):
#             self._handle_options(msg, addr)

#     def _parse_header(self, msg: str, header: str) -> str:
#         """Extract a SIP header value (case-insensitive)."""
#         for line in msg.split("\r\n"):
#             if line.lower().startswith(header.lower() + ":"):
#                 return line.split(":", 1)[1].strip()
#         return ""

#     def _send(self, response: str, addr):
#         try:
#             self._sock.sendto(response.encode("utf-8"), addr)
#         except Exception as e:
#             logger.error("[RawSIP] Send error: %s", e)

#     def _handle_register(self, msg: str, addr):
#         """Respond 200 OK to REGISTER — makes MicroSIP show as registered."""
#         call_id = self._parse_header(msg, "Call-ID")
#         cseq = self._parse_header(msg, "CSeq")
#         from_h = self._parse_header(msg, "From")
#         to_h = self._parse_header(msg, "To")
#         via = self._parse_header(msg, "Via")

#         response = (
#             f"SIP/2.0 200 OK\r\n"
#             f"Via: {via}\r\n"
#             f"From: {from_h}\r\n"
#             f"To: {to_h};tag=pyvoip{int(time.time())}\r\n"
#             f"Call-ID: {call_id}\r\n"
#             f"CSeq: {cseq}\r\n"
#             f"Contact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\n"
#             f"Expires: 3600\r\n"
#             f"Content-Length: 0\r\n\r\n"
#         )
#         self._send(response, addr)
#         logger.info("[RawSIP] REGISTER 200 OK → %s:%d", addr[0], addr[1])

#     def _handle_options(self, msg: str, addr):
#         """Respond 200 OK to OPTIONS (keepalive ping)."""
#         call_id = self._parse_header(msg, "Call-ID")
#         cseq = self._parse_header(msg, "CSeq")
#         from_h = self._parse_header(msg, "From")
#         to_h = self._parse_header(msg, "To")
#         via = self._parse_header(msg, "Via")

#         response = (
#             f"SIP/2.0 200 OK\r\n"
#             f"Via: {via}\r\n"
#             f"From: {from_h}\r\n"
#             f"To: {to_h}\r\n"
#             f"Call-ID: {call_id}\r\n"
#             f"CSeq: {cseq}\r\n"
#             f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER\r\n"
#             f"Content-Length: 0\r\n\r\n"
#         )
#         self._send(response, addr)

#     def _handle_bye(self, msg: str, addr):
#         """Respond 200 OK to BYE."""
#         call_id = self._parse_header(msg, "Call-ID")
#         cseq = self._parse_header(msg, "CSeq")
#         from_h = self._parse_header(msg, "From")
#         to_h = self._parse_header(msg, "To")
#         via = self._parse_header(msg, "Via")

#         response = (
#             f"SIP/2.0 200 OK\r\n"
#             f"Via: {via}\r\n"
#             f"From: {from_h}\r\n"
#             f"To: {to_h}\r\n"
#             f"Call-ID: {call_id}\r\n"
#             f"CSeq: {cseq}\r\n"
#             f"Content-Length: 0\r\n\r\n"
#         )
#         self._send(response, addr)
#         logger.info("[RawSIP] BYE 200 OK → %s:%d", addr[0], addr[1])

#     def _handle_cancel(self, msg: str, addr):
#         """Respond 200 OK to CANCEL."""
#         call_id = self._parse_header(msg, "Call-ID")
#         cseq = self._parse_header(msg, "CSeq")
#         from_h = self._parse_header(msg, "From")
#         to_h = self._parse_header(msg, "To")
#         via = self._parse_header(msg, "Via")

#         response = (
#             f"SIP/2.0 200 OK\r\n"
#             f"Via: {via}\r\n"
#             f"From: {from_h}\r\n"
#             f"To: {to_h}\r\n"
#             f"Call-ID: {call_id}\r\n"
#             f"CSeq: {cseq}\r\n"
#             f"Content-Length: 0\r\n\r\n"
#         )
#         self._send(response, addr)

#     def _handle_invite(self, msg: str, addr):
#         """
#         Handle INVITE: respond 100 Trying + 180 Ringing + 200 OK with SDP,
#         then spawn a thread to run the Gemini bridge via a RawSIPCall shim.
#         """
#         call_id = self._parse_header(msg, "Call-ID")
#         cseq = self._parse_header(msg, "CSeq")
#         from_h = self._parse_header(msg, "From")
#         to_h = self._parse_header(msg, "To")
#         via = self._parse_header(msg, "Via")
#         caller = from_h

#         logger.info("[RawSIP] INVITE from %s:%d (Call-ID=%s)", addr[0], addr[1], call_id)
#         print(f"\n📞 Incoming SIP call from: {caller}", flush=True)

#         # Parse remote RTP info from SDP
#         remote_rtp_ip, remote_rtp_port = self._parse_sdp_rtp(msg, addr[0])
#         local_rtp_port = self._next_rtp_port()
#         tag = f"pyvoip{int(time.time())}"

#         # 100 Trying
#         trying = (
#             f"SIP/2.0 100 Trying\r\n"
#             f"Via: {via}\r\n"
#             f"From: {from_h}\r\n"
#             f"To: {to_h}\r\n"
#             f"Call-ID: {call_id}\r\n"
#             f"CSeq: {cseq}\r\n"
#             f"Content-Length: 0\r\n\r\n"
#         )
#         self._send(trying, addr)

#         # 180 Ringing
#         ringing = (
#             f"SIP/2.0 180 Ringing\r\n"
#             f"Via: {via}\r\n"
#             f"From: {from_h}\r\n"
#             f"To: {to_h};tag={tag}\r\n"
#             f"Call-ID: {call_id}\r\n"
#             f"CSeq: {cseq}\r\n"
#             f"Content-Length: 0\r\n\r\n"
#         )
#         self._send(ringing, addr)

#         # Build SDP answer
#         sdp = self._build_sdp_answer(local_rtp_port)

#         # 200 OK with SDP
#         ok_200 = (
#             f"SIP/2.0 200 OK\r\n"
#             f"Via: {via}\r\n"
#             f"From: {from_h}\r\n"
#             f"To: {to_h};tag={tag}\r\n"
#             f"Call-ID: {call_id}\r\n"
#             f"CSeq: {cseq}\r\n"
#             f"Contact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\n"
#             f"Content-Type: application/sdp\r\n"
#             f"Content-Length: {len(sdp)}\r\n\r\n"
#             f"{sdp}"
#         )
#         self._send(ok_200, addr)
#         logger.info("[RawSIP] 200 OK → %s:%d, local RTP port=%d", addr[0], addr[1], local_rtp_port)

#         # Create a RawSIPCall shim and start the Gemini bridge
#         call = RawSIPCall(
#             sip_sock=self._sock,
#             remote_addr=addr,
#             remote_rtp_ip=remote_rtp_ip,
#             remote_rtp_port=remote_rtp_port,
#             local_rtp_port=local_rtp_port,
#             caller=caller,
#             call_id=call_id,
#             via=via,
#             from_h=from_h,
#             to_h=to_h,
#             tag=tag,
#             cseq=cseq,
#         )

#         thread = threading.Thread(
#             target=self.on_call, args=(call,), daemon=True
#         )
#         thread.start()

#     def _parse_sdp_rtp(self, msg: str, fallback_ip: str):
#         """Extract remote RTP IP and port from SDP body."""
#         rtp_ip = fallback_ip
#         rtp_port = 0
#         in_sdp = False
#         for line in msg.split("\r\n"):
#             if line == "":
#                 in_sdp = True
#             if in_sdp:
#                 if line.startswith("c=IN IP4 "):
#                     rtp_ip = line.split("c=IN IP4 ")[1].strip()
#                 elif line.startswith("m=audio "):
#                     parts = line.split()
#                     if len(parts) >= 2:
#                         try:
#                             rtp_port = int(parts[1])
#                         except ValueError:
#                             pass
#         return rtp_ip, rtp_port

#     def _build_sdp_answer(self, local_rtp_port: int) -> str:
#         """Build a minimal SDP answer for G.711 µ-law."""
#         return (
#             f"v=0\r\n"
#             f"o=pyvoip 0 0 IN IP4 {self.bind_ip}\r\n"
#             f"s=BlenSpark Voice Agent\r\n"
#             f"c=IN IP4 {self.bind_ip}\r\n"
#             f"t=0 0\r\n"
#             f"m=audio {local_rtp_port} RTP/AVP 0\r\n"
#             f"a=rtpmap:0 PCMU/8000\r\n"
#             f"a=ptime:20\r\n"
#         )


# # ─────────────────────────────────────────────────────────────────────
# # RawSIPCall — shim that wraps a raw SIP call for use with SIPCallBridge
# # ─────────────────────────────────────────────────────────────────────

# class RawSIPCall:
#     """
#     A shim object that mimics the pyVoIP call interface used by SIPCallBridge,
#     but operates directly over raw UDP RTP sockets.
#     """

#     def __init__(self, sip_sock, remote_addr, remote_rtp_ip, remote_rtp_port,
#                  local_rtp_port, caller, call_id, via, from_h, to_h, tag, cseq):
#         self.caller = caller
#         self.call_id = call_id
#         self._sip_sock = sip_sock
#         self._remote_addr = remote_addr
#         self._remote_rtp_ip = remote_rtp_ip
#         self._remote_rtp_port = remote_rtp_port
#         self._local_rtp_port = local_rtp_port
#         self._via = via
#         self._from_h = from_h
#         self._to_h = to_h
#         self._tag = tag
#         self._cseq = cseq
#         self._state = CallState.ANSWERED  # pre-answered
#         self._rtp_sock = None
#         self._audio_buffer = bytearray()
#         self._buffer_lock = threading.Lock()
#         self._running = False
#         self._seq = 0
#         self._ts = 0
#         self._ssrc = int(uuid.uuid4()) & 0xFFFFFFFF

#     @property
#     def state(self):
#         return self._state

#     def answer(self):
#         """Open RTP socket and start receiving — already answered at SIP level."""
#         self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         self._rtp_sock.bind(("0.0.0.0", self._local_rtp_port))
#         self._rtp_sock.settimeout(0.1)
#         self._running = True
#         self._recv_thread = threading.Thread(target=self._rtp_recv_loop, daemon=True)
#         self._recv_thread.start()
#         logger.info("[RawSIPCall] RTP active: local=%d remote=%s:%d",
#                     self._local_rtp_port, self._remote_rtp_ip, self._remote_rtp_port)

#     def _rtp_recv_loop(self):
#         """Receive RTP packets and store payload in buffer."""
#         while self._running and self._state == CallState.ANSWERED:
#             try:
#                 data, _ = self._rtp_sock.recvfrom(4096)
#                 if len(data) > 12:  # strip 12-byte RTP header
#                     payload = data[12:]
#                     with self._buffer_lock:
#                         self._audio_buffer.extend(payload)
#             except socket.timeout:
#                 continue
#             except Exception:
#                 break

#     def read_audio(self, length=160, blocking=True):
#         """Read `length` bytes of µ-law audio from the RTP buffer."""
#         waited = 0
#         while blocking and self._running:
#             with self._buffer_lock:
#                 if len(self._audio_buffer) >= length:
#                     chunk = bytes(self._audio_buffer[:length])
#                     del self._audio_buffer[:length]
#                     return chunk
#             time.sleep(0.005)
#             waited += 5
#             if waited > 1000:  # 1s timeout
#                 return b""
#         with self._buffer_lock:
#             if len(self._audio_buffer) >= length:
#                 chunk = bytes(self._audio_buffer[:length])
#                 del self._audio_buffer[:length]
#                 return chunk
#         return b""

#     def write_audio(self, data: bytes):
#         """Send µ-law audio bytes as an RTP packet to MicroSIP."""
#         if not self._running or not self._rtp_sock:
#             raise InvalidStateError("Call not active")
#         import struct
#         self._seq = (self._seq + 1) & 0xFFFF
#         self._ts = (self._ts + len(data)) & 0xFFFFFFFF
#         header = struct.pack(
#             "!BBHII",
#             0x80,        # V=2, P=0, X=0, CC=0
#             0x00,        # M=0, PT=0 (PCMU)
#             self._seq,
#             self._ts,
#             self._ssrc,
#         )
#         packet = header + data
#         try:
#             self._rtp_sock.sendto(packet, (self._remote_rtp_ip, self._remote_rtp_port))
#         except Exception as e:
#             raise OSError(f"RTP send failed: {e}")

#     def hangup(self):
#         """Send BYE and close RTP socket."""
#         self._state = CallState.ENDED
#         self._running = False
#         if self._rtp_sock:
#             try:
#                 self._rtp_sock.close()
#             except Exception:
#                 pass
#         # Send BYE
#         try:
#             cseq_num = int(self._cseq.split()[0]) + 1
#             bye = (
#                 f"BYE sip:{self._remote_addr[0]} SIP/2.0\r\n"
#                 f"Via: {self._via}\r\n"
#                 f"From: {self._from_h}\r\n"
#                 f"To: {self._to_h};tag={self._tag}\r\n"
#                 f"Call-ID: {self.call_id}\r\n"
#                 f"CSeq: {cseq_num} BYE\r\n"
#                 f"Content-Length: 0\r\n\r\n"
#             )
#             self._sip_sock.sendto(bye.encode(), self._remote_addr)
#             logger.info("[RawSIPCall] BYE sent")
#         except Exception as e:
#             logger.debug("[RawSIPCall] BYE send error: %s", e)


# # ─────────────────────────────────────────────────────────────────────
# # Entry point
# # ─────────────────────────────────────────────────────────────────────

# def start_sip_server(agent_id="healthcare", voice="Aoede", language="ur-PK"):
#     """Start the SIP server. Called from Django management command."""
#     server = SIPServer(agent_id=agent_id, voice=voice, language=language)
#     server.start()

#     try:
#         # Keep running until Ctrl+C
#         while True:
#             time.sleep(1)
#     except KeyboardInterrupt:
#         print("\nShutting down SIP server...")
#     finally:
#         server.stop()





"""
SIP Client — pyVoIP to Gemini Live bridge.

Replaces the entire Asterisk + Docker + ARI + UDP pipeline with a direct
Python SIP client that bridges calls to the Gemini Live API.

Flow:
  Phone call → RawSIPServer (SIP + RTP) → SIPCallBridge → Gemini Live API
                                         ← SIPCallBridge ←

Auth modes (multinet):
  1. IP-based  — no password needed, Multinet whitelists your public IP
  2. Digest    — full RFC 3261 qop=auth MD5 (fallback if IP auth fails)
"""

import asyncio
import audioop
import hashlib
import json
import logging
import os
import random
import socket
import string
import struct
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# pyVoIP only needed for CallState enum — we no longer use VoIPPhone
from pyVoIP.VoIP import InvalidStateError, CallState

logger = logging.getLogger(__name__)

# ── Audio format constants ────────────────────────────────────────────
SIP_RATE      = 8000    # G.711 µ-law from SIP
MIC_RATE      = 16000   # Gemini input
OUT_RATE      = 24000   # Gemini output
FRAME_DURATION = 0.02   # 20 ms RTP frames

# ── Gemini ────────────────────────────────────────────────────────────
try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")


# ─────────────────────────────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────────────────────────────

def _log_sip_tx(label: str, msg: str, addr):
    """Log every outbound SIP message line by line."""
    lines = msg.strip().split("\r\n") if "\r\n" in msg else msg.strip().split("\n")
    logger.debug(
        "[SIP TX ▶ %s:%d] ─────────────────────────────────── %s",
        addr[0], addr[1], label,
    )
    for line in lines:
        if line.strip():
            logger.debug("[SIP TX]  %s", line)
    logger.debug("[SIP TX] ───────────────────────────────────────────")


def _log_sip_rx(label: str, msg: str, addr):
    """Log every inbound SIP message line by line."""
    lines = msg.strip().split("\r\n") if "\r\n" in msg else msg.strip().split("\n")
    logger.debug(
        "[SIP RX ◀ %s:%d] ─────────────────────────────────── %s",
        addr[0], addr[1], label,
    )
    for line in lines:
        if line.strip():
            logger.debug("[SIP RX]  %s", line)
    logger.debug("[SIP RX] ───────────────────────────────────────────")


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ─────────────────────────────────────────────────────────────────────
# MultinetRegistrar — outbound REGISTER with IP-auth + Digest fallback
# ─────────────────────────────────────────────────────────────────────

class MultinetRegistrar:
    """
    Registers to Multinet SIP trunk and keeps registration alive.

    Strategy (auto-detected on first attempt):
      1. IP-based auth   — send REGISTER, Multinet replies 200 OK directly
      2. Digest auth     — Multinet replies 401, we compute RFC 3261 qop=auth
                           MD5 and retry. If 403 → wrong password.

    Re-registers every 55 s (before 60 s expiry).
    """

    AUTH_IP     = "ip"
    AUTH_DIGEST = "digest"
    AUTH_NONE   = "none"   # not yet determined

    def __init__(
        self,
        server: str,
        port: int,
        username: str,
        password: str,
        local_ip: str,
        local_port: int,
        public_ip: str = None,
        on_registered=None,
        on_failed=None,
    ):
        self.server     = server
        self.port       = port
        self.username   = username
        self.password   = password
        self.local_ip   = local_ip
        self.local_port = local_port
        self.public_ip  = public_ip or local_ip   # IP advertised in Contact/Via
        self.on_registered = on_registered
        self.on_failed     = on_failed

        self._running   = False
        self._sock      = None
        self._thread    = None
        self._cseq      = 0
        self._call_id   = self._gen_call_id()
        self._tag       = self._rand_str(8)
        self._auth_mode = self.AUTH_NONE   # discovered on first attempt
        self.registered = False

        logger.info(
            "[Registrar] Init: server=%s:%d user=%s local=%s:%d public=%s",
            server, port, username, local_ip, local_port, self.public_ip,
        )

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _rand_str(n=8) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    def _gen_call_id(self) -> str:
        return f"{self._rand_str(16)}@{self.local_ip}"

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()

    def _build_digest_auth(self, realm, nonce, opaque, qop) -> str:
        """Compute RFC 3261 Digest Authorization header (with qop=auth support)."""
        ha1  = self._md5(f"{self.username}:{realm}:{self.password}")
        uri  = f"sip:{self.server}"
        ha2  = self._md5(f"REGISTER:{uri}")
        cnonce = self._rand_str(16)
        nc     = "00000001"

        if qop:
            resp = self._md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            auth = (
                f'Digest username="{self.username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", '
                f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                f'response="{resp}", algorithm=MD5'
            )
        else:
            resp = self._md5(f"{ha1}:{nonce}:{ha2}")
            auth = (
                f'Digest username="{self.username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", '
                f'response="{resp}", algorithm=MD5'
            )

        if opaque:
            auth += f', opaque="{opaque}"'

        logger.debug(
            "[Registrar] Digest computed: realm=%s nonce=%s qop=%s nc=%s cnonce=%s",
            realm, nonce, qop, nc, cnonce,
        )
        return auth

    def _parse_header(self, msg: str, header: str) -> str:
        for line in msg.split("\r\n"):
            if line.lower().startswith(header.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def _parse_www_auth(self, msg: str) -> dict:
        """Parse WWW-Authenticate → {realm, nonce, opaque, qop}."""
        raw = self._parse_header(msg, "WWW-Authenticate")
        if not raw:
            raw = self._parse_header(msg, "Proxy-Authenticate")
        if not raw:
            return {}
        raw = raw[7:].strip() if raw.lower().startswith("digest ") else raw
        result = {}
        for part in raw.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip().strip('"')
        logger.debug("[Registrar] WWW-Auth parsed: %s", result)
        return result

    def _build_register(self, expires=3600, auth_header=None) -> str:
        self._cseq += 1
        branch = f"z9hG4bK{self._rand_str(16)}"
        lines = [
            f"REGISTER sip:{self.server} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={branch};rport",
            f"Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.server}>;tag={self._tag}",
            f"To: <sip:{self.username}@{self.server}>",
            f"Call-ID: {self._call_id}",
            f"CSeq: {self._cseq} REGISTER",
            f"Contact: <sip:{self.username}@{self.public_ip}:{self.local_port}>",
            f"Expires: {expires}",
            f"User-Agent: BlenSpark-VoiceAgent/1.0",
        ]
        if auth_header:
            lines.append(f"Authorization: {auth_header}")
        lines += ["Content-Length: 0", "", ""]
        return "\r\n".join(lines)

    # ── network ───────────────────────────────────────────────────────

    def _send_recv(self, msg: str, timeout=5.0) -> str:
        dest = (self.server, self.port)
        _log_sip_tx("REGISTER", msg, dest)
        self._sock.sendto(msg.encode(), dest)
        self._sock.settimeout(timeout)
        try:
            data, addr = self._sock.recvfrom(4096)
            resp = data.decode("utf-8", errors="replace")
            status_line = resp.split("\r\n")[0] if resp else "(empty)"
            _log_sip_rx(status_line, resp, addr)
            return resp
        except socket.timeout:
            logger.warning("[Registrar] ⏱ No response from %s:%d (timeout=%ss)", self.server, self.port, timeout)
            return ""

    # ── core registration logic ───────────────────────────────────────

    def _do_register(self, expires=3600) -> bool:
        """
        Attempt one full registration cycle.
        Returns True on success, False on failure.
        """
        logger.info(
            "[Registrar] ── Attempting REGISTER (mode=%s, expires=%d) ──",
            self._auth_mode, expires,
        )

        # ── Step 1: send without auth ─────────────────────────────────
        msg1 = self._build_register(expires=expires)
        resp1 = self._send_recv(msg1)

        if not resp1:
            logger.error("[Registrar] ✗ No response — server unreachable or IP blocked")
            return False

        status = self._status_code(resp1)
        logger.info("[Registrar] Step 1 response: %s", status)

        # ── 200 OK immediately → IP-based auth ───────────────────────
        if status == "200":
            if self._auth_mode != self.AUTH_IP:
                logger.info("[Registrar] ✅ IP-based auth confirmed (no password needed)")
                self._auth_mode = self.AUTH_IP
            return True

        # ── 401/407 → Digest auth challenge ──────────────────────────
        if status in ("401", "407"):
            logger.info("[Registrar] 🔐 Digest challenge received (status=%s)", status)
            self._auth_mode = self.AUTH_DIGEST

            auth_params = self._parse_www_auth(resp1)
            realm  = auth_params.get("realm",  self.server)
            nonce  = auth_params.get("nonce",  "")
            opaque = auth_params.get("opaque", "")
            qop    = auth_params.get("qop",    "")

            if not nonce:
                logger.error("[Registrar] ✗ No nonce in 401 — cannot compute Digest")
                return False

            auth_header = self._build_digest_auth(realm, nonce, opaque, qop)

            # ── Step 2: resend with auth ──────────────────────────────
            logger.info("[Registrar] Step 2: sending REGISTER with Digest auth...")
            msg2  = self._build_register(expires=expires, auth_header=auth_header)
            resp2 = self._send_recv(msg2)

            if not resp2:
                logger.error("[Registrar] ✗ No response to authenticated REGISTER")
                return False

            status2 = self._status_code(resp2)
            logger.info("[Registrar] Step 2 response: %s", status2)

            if status2 == "200":
                logger.info("[Registrar] ✅ Digest auth successful — registered!")
                return True
            elif status2 == "403":
                logger.error(
                    "[Registrar] ✗ 403 Forbidden — wrong password OR IP not whitelisted. "
                    "Check SIP_PASSWORD and ask Multinet to whitelist your IP."
                )
            elif status2 in ("401", "407"):
                logger.error(
                    "[Registrar] ✗ Still getting %s after Digest — likely wrong password. "
                    "Double-check SIP_PASSWORD=%s", status2, self.password,
                )
            else:
                logger.error("[Registrar] ✗ Unexpected status: %s", status2)
            return False

        # ── 403 on first request → IP blocked ─────────────────────────
        if status == "403":
            logger.error(
                "[Registrar] ✗ 403 Forbidden on first REGISTER — "
                "your IP (%s) is blocked by Multinet. "
                "Wait ~30min or ask them to unblock it.", self.public_ip,
            )
            return False

        logger.error("[Registrar] ✗ Unexpected first-response status: %s", status)
        return False

    @staticmethod
    def _status_code(resp: str) -> str:
        parts = resp.split(" ", 2)
        return parts[1] if len(parts) >= 2 else "?"

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.local_ip, self.local_port))
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="MultinetRegistrar")
        self._thread.start()
        logger.info("[Registrar] Started on %s:%d → %s:%d", self.local_ip, self.local_port, self.server, self.port)

    def stop(self):
        logger.info("[Registrar] Stopping...")
        self._running = False
        try:
            self._do_register(expires=0)   # de-register
            logger.info("[Registrar] De-registered (Expires: 0)")
        except Exception as e:
            logger.debug("[Registrar] De-register error: %s", e)
        if self._sock:
            self._sock.close()

    def get_socket(self):
        """Return the bound socket so RawSIPServer can share it."""
        return self._sock

    def _loop(self):
        """Registration keep-alive loop — re-registers every 55 s."""
        retry_delay = 5
        while self._running:
            ok = self._do_register()
            self.registered = ok

            if ok:
                retry_delay = 5   # reset backoff on success
                if self.on_registered:
                    try:
                        self.on_registered()
                    except Exception:
                        pass
                # Wait 55 s before next re-register
                logger.info("[Registrar] 💤 Next re-register in 55s")
                for _ in range(55):
                    if not self._running:
                        break
                    time.sleep(1)
            else:
                if self.on_failed:
                    try:
                        self.on_failed()
                    except Exception:
                        pass
                logger.warning("[Registrar] ⚠ Registration failed, retrying in %ds", retry_delay)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)   # exponential backoff, max 60s


# ─────────────────────────────────────────────────────────────────────
# SIPCallBridge — one per active call
# ─────────────────────────────────────────────────────────────────────

class SIPCallBridge:
    """
    Bridges a single SIP call to Gemini Live.

    Audio path:
      SIP (G.711 µ-law 8kHz) ↔ transcode ↔ Gemini (PCM 16kHz in / 24kHz out)
    """

    def __init__(self, call, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        self.call      = call
        self.agent_id  = agent_id
        self.voice     = voice
        self.language  = language

        self._session_uuid    = str(uuid.uuid4())
        self._running         = False
        self._gemini_session  = None
        self._loop            = None

        self._upsample_state   = None   # 8kHz → 16kHz
        self._downsample_state = None   # 24kHz → 8kHz

        self._start_time    = time.time()
        self._usage_metrics = {
            "prompt": 0, "response": 0, "total": 0,
            "input_text": 0, "input_audio": 0,
            "output_text": 0, "output_audio": 0,
        }
        self._call_history        = []
        self._current_agent_turn  = ""

        logger.info(
            "[Bridge %s] Created: agent=%s voice=%s lang=%s",
            self._session_uuid[:8], agent_id, voice, language,
        )

    def start(self):
        self._running = True
        t = threading.Thread(target=self._run_async_loop, daemon=True,
                             name=f"Bridge-{self._session_uuid[:8]}")
        t.start()
        logger.info("[Bridge %s] Thread started", self._session_uuid[:8])

    def _run_async_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_gemini_session())
        except Exception as e:
            logger.error("[Bridge %s] Fatal: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._running = False
            self._loop.close()
            logger.info("[Bridge %s] Thread ended", self._session_uuid[:8])

    async def _run_gemini_session(self):
        from .agents.registry import get_agent

        agent_cfg = get_agent(self.agent_id)
        if not agent_cfg:
            logger.error("[Bridge %s] Unknown agent: %s", self._session_uuid[:8], self.agent_id)
            return

        schedule_data = await self._fetch_schedule_data()

        greeting_path_fn = agent_cfg.get("greeting_path_fn")
        greeting_path    = (
            greeting_path_fn(self.language, self.voice)
            if greeting_path_fn
            else agent_cfg["greeting_path"]
        )
        has_cached_greeting = greeting_path.exists()

        system_prompt = agent_cfg["build_system_prompt"](
            language=self.language,
            voice=self.voice,
            has_cached_greeting=has_cached_greeting,
            schedule_data=schedule_data,
        )
        tools = agent_cfg["tools_fn"]()
        self._execute_tool_fn = agent_cfg["execute_tool"]

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.error("[Bridge %s] GEMINI_API_KEY not set!", self._session_uuid[:8])
            return

        client = genai.Client(api_key=api_key)

        live_config = types.LiveConnectConfig(
            system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
            response_modalities=["AUDIO"],
            tools=tools,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                ),
                language_code=self.language,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                )
            ),
        )

        logger.info("[Bridge %s] Connecting to Gemini Live...", self._session_uuid[:8])
        t0 = time.time()

        try:
            async with client.aio.live.connect(
                model="gemini-2.5-flash-preview-native-audio-dialog", config=live_config
            ) as session:
                logger.info(
                    "[Bridge %s] Gemini Live connected in %.2fs",
                    self._session_uuid[:8], time.time() - t0,
                )
                self._gemini_session = session

                await self._handle_greeting(session, agent_cfg, greeting_path, has_cached_greeting)

                tasks = [
                    asyncio.create_task(self._sip_to_gemini(session)),
                    asyncio.create_task(self._gemini_to_sip(session, agent_cfg)),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()

        except Exception as e:
            logger.error("[Bridge %s] Gemini session error: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._gemini_session = None
            await self._save_session_cost()
            self._cleanup_call()

    async def _fetch_schedule_data(self) -> list:
        try:
            from appointment.models import Schedule
            from appointment.serializers import ScheduleSerializer
            from asgiref.sync import sync_to_async
            schedules = await sync_to_async(lambda: list(Schedule.objects.all()))()
            return ScheduleSerializer(schedules, many=True).data
        except Exception as e:
            logger.warning("[Bridge %s] Schedule fetch failed: %s", self._session_uuid[:8], e)
            return []

    async def _handle_greeting(self, session, agent_cfg, greeting_path, has_cached):
        if has_cached:
            logger.info("[Bridge %s] Playing cached greeting: %s", self._session_uuid[:8], greeting_path)
            pcm = self._load_wav_pcm(greeting_path)
            self._write_pcm24k_to_sip(pcm)
        else:
            gen_fn = agent_cfg.get("generate_greeting_prompt_fn")
            prompt = gen_fn(self.language, self.voice) if gen_fn else agent_cfg.get("greeting_prompt", "Greet warmly.")
            logger.info("[Bridge %s] Generating greeting via Gemini", self._session_uuid[:8])
            self._save_as_greeting    = True
            self._greeting_buffer     = bytearray()
            self._greeting_save_path  = greeting_path
            await session.send_realtime_input(text=prompt)

    def _load_wav_pcm(self, path: Path) -> bytes:
        with wave.open(str(path), "rb") as wf:
            return wf.readframes(wf.getnframes())

    def _write_pcm24k_to_sip(self, pcm_24k: bytes):
        try:
            pcm_8k, self._downsample_state = audioop.ratecv(
                pcm_24k, 2, 1, OUT_RATE, SIP_RATE, self._downsample_state
            )
            ulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
            for i in range(0, len(ulaw_8k), 160):
                if not self._running:
                    break
                try:
                    self.call.write_audio(ulaw_8k[i:i + 160])
                except (InvalidStateError, OSError):
                    logger.info("[Bridge %s] Call ended during audio write", self._session_uuid[:8])
                    self._running = False
                    break
                time.sleep(FRAME_DURATION)
        except Exception as e:
            logger.error("[Bridge %s] Audio write error: %s", self._session_uuid[:8], e)

    async def _sip_to_gemini(self, session):
        logger.info("[Bridge %s] ▶ SIP→Gemini loop started", self._session_uuid[:8])
        frames = 0
        try:
            while self._running:
                try:
                    if self.call.state != CallState.ANSWERED:
                        logger.info("[Bridge %s] Call state: %s — stopping", self._session_uuid[:8], self.call.state)
                        break
                except Exception:
                    break

                try:
                    ulaw = await asyncio.get_event_loop().run_in_executor(None, self._read_sip_audio)
                except Exception:
                    break

                if not ulaw:
                    await asyncio.sleep(0.01)
                    continue

                pcm_8k  = audioop.ulaw2lin(ulaw, 2)
                pcm_16k, self._upsample_state = audioop.ratecv(
                    pcm_8k, 2, 1, SIP_RATE, MIC_RATE, self._upsample_state
                )

                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=pcm_16k, mime_type=f"audio/pcm;rate={MIC_RATE}")
                    )
                    frames += 1
                    if frames == 1:
                        logger.info("[Bridge %s] ▶ First audio frame sent to Gemini", self._session_uuid[:8])
                    elif frames % 500 == 0:
                        logger.debug("[Bridge %s] ▶ %d frames sent to Gemini", self._session_uuid[:8], frames)
                except Exception as e:
                    logger.error("[Bridge %s] Gemini send error: %s", self._session_uuid[:8], e)
                    break

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("[Bridge %s] ▶ SIP→Gemini loop ended (%d frames)", self._session_uuid[:8], frames)

    def _read_sip_audio(self) -> bytes:
        try:
            data = self.call.read_audio(length=160, blocking=True)
            return data if data else b""
        except InvalidStateError:
            self._running = False
            return b""
        except Exception:
            return b""

    async def _gemini_to_sip(self, session, agent_cfg):
        logger.info("[Bridge %s] ◀ Gemini→SIP loop started", self._session_uuid[:8])
        greeting_buffer  = bytearray()
        save_as_greeting = getattr(self, "_save_as_greeting", False)

        try:
            while self._running:
                async for response in session.receive():
                    if not self._running:
                        break

                    # usage metrics
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        for attr, key in [
                            ("prompt_token_count",       "prompt"),
                            ("response_token_count",     "response"),
                            ("total_token_count",        "total"),
                        ]:
                            val = getattr(usage, attr, 0) or 0
                            self._usage_metrics[key] = max(self._usage_metrics[key], val)

                    # tool calls
                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        fn_responses = []
                        for fc in tool_call.function_calls:
                            args   = dict(fc.args) if fc.args else {}
                            logger.info("[Bridge %s] 🔧 Tool: %s(%s)", self._session_uuid[:8], fc.name, args)
                            result = await self._execute_tool_fn(fc.name, args)
                            logger.info("[Bridge %s] 🔧 Tool result: %s → %s", self._session_uuid[:8], fc.name, result)
                            self._call_history.append({"role": "tool", "tool_name": fc.name, "tool_args": args, "tool_result": result})
                            fn_responses.append(
                                types.FunctionResponse(name=fc.name, id=fc.id, response={"result": result})
                            )
                        try:
                            await session.send_tool_response(function_responses=fn_responses)
                        except Exception as e:
                            logger.error("[Bridge %s] Tool response error: %s", self._session_uuid[:8], e)
                        continue

                    sc = getattr(response, "server_content", None)
                    if sc is None:
                        continue

                    if getattr(sc, "input_transcription", None):
                        t = sc.input_transcription
                        if hasattr(t, "text") and t.text:
                            logger.info("[Bridge %s] 👤 User: %s", self._session_uuid[:8], t.text)
                            self._call_history.append({"role": "user", "text": t.text})

                    if getattr(sc, "output_transcription", None):
                        t = sc.output_transcription
                        if hasattr(t, "text") and t.text:
                            logger.info("[Bridge %s] 🤖 Agent: %s", self._session_uuid[:8], t.text)
                            if not self._current_agent_turn.endswith(t.text):
                                self._current_agent_turn += t.text

                    if getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            if getattr(part, "text", None):
                                if not self._current_agent_turn.endswith(part.text):
                                    self._current_agent_turn += part.text
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                if save_as_greeting:
                                    greeting_buffer.extend(inline.data)
                                await asyncio.get_event_loop().run_in_executor(
                                    None, self._write_pcm24k_to_sip, inline.data
                                )

                    if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
                        if save_as_greeting and greeting_buffer:
                            self._save_wav(bytes(greeting_buffer), getattr(self, "_greeting_save_path", None))
                            save_as_greeting = self._save_as_greeting = False
                            greeting_buffer.clear()

                        if self._current_agent_turn:
                            self._call_history.append({"role": "agent", "text": self._current_agent_turn.strip()})
                            idx = self._current_agent_turn.lower()
                            if any(p in idx for p in ["allah hafiz", "اللہ حافظ", "khuda hafiz", "goodbye", "bye"]):
                                logger.info("[Bridge %s] 👋 Goodbye detected — ending in 5s", self._session_uuid[:8])
                                await asyncio.sleep(5)
                                self._running = False
                                break
                            self._current_agent_turn = ""

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[Bridge %s] Receive error: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._running = False
            logger.info("[Bridge %s] ◀ Gemini→SIP loop ended", self._session_uuid[:8])

    def _save_wav(self, pcm: bytes, path):
        if not path:
            return
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(OUT_RATE)
                wf.writeframes(pcm)
            logger.info("[Bridge %s] Greeting saved: %s", self._session_uuid[:8], path)
        except Exception as e:
            logger.error("[Bridge %s] WAV save error: %s", self._session_uuid[:8], e)

    def _cleanup_call(self):
        try:
            if self.call.state == CallState.ANSWERED:
                self.call.hangup()
                logger.info("[Bridge %s] Call hung up", self._session_uuid[:8])
        except Exception as e:
            logger.debug("[Bridge %s] Hangup error: %s", self._session_uuid[:8], e)

    async def _save_session_cost(self):
        duration = int(time.time() - self._start_time)
        m = self._usage_metrics
        if m["total"] > 0 or duration > 0:
            try:
                from asgiref.sync import sync_to_async
                from Analytics.models import GeminiSessionCost
                total_cost = (
                    m["input_text"]  * 0.00000075 +
                    m["input_audio"] * 0.000003   +
                    m["output_text"] * 0.0000045  +
                    m["output_audio"]* 0.000012
                )
                await sync_to_async(GeminiSessionCost.objects.create)(
                    session_id=self._session_uuid,
                    agent_type=self.agent_id,
                    prompt_tokens=m["prompt"], response_tokens=m["response"],
                    total_tokens=m["total"],
                    input_text_tokens=m["input_text"], input_audio_tokens=m["input_audio"],
                    output_text_tokens=m["output_text"], output_audio_tokens=m["output_audio"],
                    call_duration_seconds=duration,
                    estimated_cost_usd=total_cost,
                )
                logger.info("[Bridge %s] Cost saved: $%.6f, %ds", self._session_uuid[:8], total_cost, duration)
            except Exception as e:
                logger.error("[Bridge %s] Cost save failed: %s", self._session_uuid[:8], e)

        if self._call_history:
            try:
                from asgiref.sync import sync_to_async
                from Analytics.models import CallHistory
                await sync_to_async(CallHistory.objects.create)(
                    session_id=self._session_uuid,
                    agent_type=self.agent_id,
                    duration_seconds=duration,
                    transcript=self._call_history,
                )
                logger.info("[Bridge %s] History saved: %d turns", self._session_uuid[:8], len(self._call_history))
            except Exception as e:
                logger.error("[Bridge %s] History save failed: %s", self._session_uuid[:8], e)


# ─────────────────────────────────────────────────────────────────────
# RawSIPServer — UDP SIP listener (handles REGISTER + INVITE from Multinet/MicroSIP)
# ─────────────────────────────────────────────────────────────────────

class RawSIPServer:
    """
    Minimal UDP SIP server.
    - Local mode  : accepts REGISTER from MicroSIP, auto-200 OK
    - Multinet mode: just listens for INVITE (registration handled by MultinetRegistrar)
    """

    def __init__(self, bind_ip, bind_port, username, password,
                 on_call, agent_id, voice, language,
                 rtp_port_low=10000, rtp_port_high=20000,
                 shared_sock=None):
        self.bind_ip   = bind_ip
        self.bind_port = bind_port
        self.username  = username
        self.password  = password
        self.on_call   = on_call
        self.agent_id  = agent_id
        self.voice     = voice
        self.language  = language
        self.rtp_port_low  = rtp_port_low
        self.rtp_port_high = rtp_port_high
        self._shared_sock  = shared_sock   # reuse registrar's socket if provided
        self._running  = False
        self._sock     = None
        self._thread   = None
        self._rtp_port_counter = rtp_port_low

    def _next_rtp_port(self):
        port = self._rtp_port_counter
        self._rtp_port_counter += 2
        if self._rtp_port_counter > self.rtp_port_high:
            self._rtp_port_counter = self.rtp_port_low
        return port

    def start(self):
        self._running = True
        if self._shared_sock:
            self._sock = self._shared_sock
            logger.info("[RawSIP] Reusing shared socket on %s:%d", self.bind_ip, self.bind_port)
        else:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.bind_ip, self.bind_port))
            self._sock.settimeout(1.0)
            logger.info("[RawSIP] Listening on %s:%d", self.bind_ip, self.bind_port)
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="RawSIPServer")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._sock and not self._shared_sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _listen_loop(self):
        logger.info("[RawSIP] UDP listener running")
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error("[RawSIP] Socket error: %s", e)
                break
            try:
                msg = data.decode("utf-8", errors="replace")
                first = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]
                _log_sip_rx(first, msg, addr)
                self._handle_message(msg, addr)
            except Exception as e:
                logger.error("[RawSIP] Message handling error: %s", e, exc_info=True)
        logger.info("[RawSIP] UDP listener stopped")

    def _handle_message(self, msg: str, addr):
        first = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]
        method = first.split()[0] if first else ""

        dispatch = {
            "REGISTER": self._handle_register,
            "INVITE":   self._handle_invite,
            "BYE":      self._handle_bye,
            "CANCEL":   self._handle_cancel,
            "OPTIONS":  self._handle_options,
            "ACK":      lambda m, a: logger.debug("[RawSIP] ACK from %s:%d", a[0], a[1]),
        }
        handler = dispatch.get(method)
        if handler:
            handler(msg, addr)
        else:
            logger.debug("[RawSIP] Unhandled SIP method: %s from %s:%d", method, addr[0], addr[1])

    def _parse_header(self, msg: str, header: str) -> str:
        for line in msg.split("\r\n"):
            if line.lower().startswith(header.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def _send(self, response: str, addr, label="response"):
        _log_sip_tx(label, response, addr)
        try:
            self._sock.sendto(response.encode("utf-8"), addr)
        except Exception as e:
            logger.error("[RawSIP] Send error: %s", e)

    def _common_headers(self, msg: str):
        return {
            "call_id": self._parse_header(msg, "Call-ID"),
            "cseq":    self._parse_header(msg, "CSeq"),
            "from_h":  self._parse_header(msg, "From"),
            "to_h":    self._parse_header(msg, "To"),
            "via":     self._parse_header(msg, "Via"),
        }

    def _handle_register(self, msg: str, addr):
        h = self._common_headers(msg)
        resp = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {h['via']}\r\n"
            f"From: {h['from_h']}\r\n"
            f"To: {h['to_h']};tag=blenspark{int(time.time())}\r\n"
            f"Call-ID: {h['call_id']}\r\n"
            f"CSeq: {h['cseq']}\r\n"
            f"Contact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\n"
            f"Expires: 3600\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._send(resp, addr, "200 OK (REGISTER)")
        logger.info("[RawSIP] REGISTER 200 OK → %s:%d", addr[0], addr[1])

    def _handle_options(self, msg: str, addr):
        h = self._common_headers(msg)
        resp = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {h['via']}\r\n"
            f"From: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\n"
            f"Call-ID: {h['call_id']}\r\n"
            f"CSeq: {h['cseq']}\r\n"
            f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._send(resp, addr, "200 OK (OPTIONS)")

    def _handle_bye(self, msg: str, addr):
        h = self._common_headers(msg)
        resp = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {h['via']}\r\n"
            f"From: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\n"
            f"Call-ID: {h['call_id']}\r\n"
            f"CSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._send(resp, addr, "200 OK (BYE)")
        logger.info("[RawSIP] BYE 200 OK → %s:%d", addr[0], addr[1])

    def _handle_cancel(self, msg: str, addr):
        h = self._common_headers(msg)
        resp = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {h['via']}\r\n"
            f"From: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\n"
            f"Call-ID: {h['call_id']}\r\n"
            f"CSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._send(resp, addr, "200 OK (CANCEL)")

    def _handle_invite(self, msg: str, addr):
        h = self._common_headers(msg)
        logger.info("[RawSIP] 📞 INVITE from %s:%d (Call-ID=%s)", addr[0], addr[1], h["call_id"])

        remote_rtp_ip, remote_rtp_port = self._parse_sdp_rtp(msg, addr[0])
        local_rtp_port = self._next_rtp_port()
        tag = f"blenspark{int(time.time())}"

        logger.info(
            "[RawSIP] SDP negotiation: remote RTP=%s:%d  local RTP port=%d",
            remote_rtp_ip, remote_rtp_port, local_rtp_port,
        )

        # 100 Trying
        self._send(
            f"SIP/2.0 100 Trying\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n",
            addr, "100 Trying",
        )

        # 180 Ringing
        self._send(
            f"SIP/2.0 180 Ringing\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']};tag={tag}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n",
            addr, "180 Ringing",
        )

        # 200 OK + SDP
        sdp = self._build_sdp_answer(local_rtp_port)
        self._send(
            f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']};tag={tag}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Contact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\n"
            f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}",
            addr, "200 OK (INVITE)",
        )

        call = RawSIPCall(
            sip_sock=self._sock, remote_addr=addr,
            remote_rtp_ip=remote_rtp_ip, remote_rtp_port=remote_rtp_port,
            local_rtp_port=local_rtp_port, caller=h["from_h"],
            call_id=h["call_id"], via=h["via"], from_h=h["from_h"],
            to_h=h["to_h"], tag=tag, cseq=h["cseq"],
        )
        threading.Thread(target=self.on_call, args=(call,), daemon=True).start()

    def _parse_sdp_rtp(self, msg: str, fallback_ip: str):
        rtp_ip, rtp_port = fallback_ip, 0
        in_sdp = False
        for line in msg.split("\r\n"):
            if line == "":
                in_sdp = True
            if in_sdp:
                if line.startswith("c=IN IP4 "):
                    rtp_ip = line.split("c=IN IP4 ")[1].strip()
                elif line.startswith("m=audio "):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            rtp_port = int(parts[1])
                        except ValueError:
                            pass
        return rtp_ip, rtp_port

    def _build_sdp_answer(self, local_rtp_port: int) -> str:
        return (
            f"v=0\r\n"
            f"o=blenspark 0 0 IN IP4 {self.bind_ip}\r\n"
            f"s=BlenSpark Voice Agent\r\n"
            f"c=IN IP4 {self.bind_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {local_rtp_port} RTP/AVP 0\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=ptime:20\r\n"
        )


# ─────────────────────────────────────────────────────────────────────
# RawSIPCall — RTP shim (mimics pyVoIP call interface)
# ─────────────────────────────────────────────────────────────────────

class RawSIPCall:

    def __init__(self, sip_sock, remote_addr, remote_rtp_ip, remote_rtp_port,
                 local_rtp_port, caller, call_id, via, from_h, to_h, tag, cseq):
        self.caller   = caller
        self.call_id  = call_id
        self._sip_sock        = sip_sock
        self._remote_addr     = remote_addr
        self._remote_rtp_ip   = remote_rtp_ip
        self._remote_rtp_port = remote_rtp_port
        self._local_rtp_port  = local_rtp_port
        self._via    = via
        self._from_h = from_h
        self._to_h   = to_h
        self._tag    = tag
        self._cseq   = cseq
        self._state  = CallState.ANSWERED
        self._rtp_sock     = None
        self._audio_buffer = bytearray()
        self._buffer_lock  = threading.Lock()
        self._running = False
        self._seq  = 0
        self._ts   = 0
        self._ssrc = int(uuid.uuid4()) & 0xFFFFFFFF
        self._rtp_rx_count = 0
        self._rtp_tx_count = 0

    @property
    def state(self):
        return self._state

    def answer(self):
        self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp_sock.bind(("0.0.0.0", self._local_rtp_port))
        self._rtp_sock.settimeout(0.1)
        self._running = True
        threading.Thread(target=self._rtp_recv_loop, daemon=True, name="RTPRecv").start()
        logger.info(
            "[RawSIPCall] RTP active: local_port=%d  remote=%s:%d",
            self._local_rtp_port, self._remote_rtp_ip, self._remote_rtp_port,
        )

    def _rtp_recv_loop(self):
        logger.info("[RawSIPCall] RTP recv loop started")
        while self._running and self._state == CallState.ANSWERED:
            try:
                data, _ = self._rtp_sock.recvfrom(4096)
                if len(data) > 12:
                    payload = data[12:]
                    with self._buffer_lock:
                        self._audio_buffer.extend(payload)
                    self._rtp_rx_count += 1
                    if self._rtp_rx_count == 1:
                        logger.info("[RawSIPCall] 🎤 First RTP packet received")
                    elif self._rtp_rx_count % 500 == 0:
                        logger.debug("[RawSIPCall] 🎤 RTP RX: %d packets", self._rtp_rx_count)
            except socket.timeout:
                continue
            except Exception:
                break
        logger.info("[RawSIPCall] RTP recv loop ended (rx=%d tx=%d)", self._rtp_rx_count, self._rtp_tx_count)

    def read_audio(self, length=160, blocking=True) -> bytes:
        waited = 0
        while blocking and self._running:
            with self._buffer_lock:
                if len(self._audio_buffer) >= length:
                    chunk = bytes(self._audio_buffer[:length])
                    del self._audio_buffer[:length]
                    return chunk
            time.sleep(0.005)
            waited += 5
            if waited > 1000:
                return b""
        with self._buffer_lock:
            if len(self._audio_buffer) >= length:
                chunk = bytes(self._audio_buffer[:length])
                del self._audio_buffer[:length]
                return chunk
        return b""

    def write_audio(self, data: bytes):
        if not self._running or not self._rtp_sock:
            raise InvalidStateError("Call not active")
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts  = (self._ts  + len(data)) & 0xFFFFFFFF
        header = struct.pack("!BBHII", 0x80, 0x00, self._seq, self._ts, self._ssrc)
        try:
            self._rtp_sock.sendto(header + data, (self._remote_rtp_ip, self._remote_rtp_port))
            self._rtp_tx_count += 1
            if self._rtp_tx_count == 1:
                logger.info("[RawSIPCall] 🔊 First RTP packet sent")
            elif self._rtp_tx_count % 500 == 0:
                logger.debug("[RawSIPCall] 🔊 RTP TX: %d packets", self._rtp_tx_count)
        except Exception as e:
            raise OSError(f"RTP send failed: {e}")

    def hangup(self):
        self._state   = CallState.ENDED
        self._running = False
        if self._rtp_sock:
            try:
                self._rtp_sock.close()
            except Exception:
                pass
        try:
            cseq_num = int(self._cseq.split()[0]) + 1
            bye = (
                f"BYE sip:{self._remote_addr[0]} SIP/2.0\r\n"
                f"Via: {self._via}\r\n"
                f"From: {self._from_h}\r\n"
                f"To: {self._to_h};tag={self._tag}\r\n"
                f"Call-ID: {self.call_id}\r\n"
                f"CSeq: {cseq_num} BYE\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            _log_sip_tx("BYE", bye, self._remote_addr)
            self._sip_sock.sendto(bye.encode(), self._remote_addr)
            logger.info("[RawSIPCall] BYE sent")
        except Exception as e:
            logger.debug("[RawSIPCall] BYE send error: %s", e)


# ─────────────────────────────────────────────────────────────────────
# SIPServer — top-level orchestrator
# ─────────────────────────────────────────────────────────────────────

class SIPServer:

    def __init__(self, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        from .sip_config import (
            SIP_MODE, SIP_BIND_IP, SIP_BIND_PORT,
            SIP_SERVER, SIP_SERVER_PORT,
            SIP_USERNAME, SIP_PASSWORD,
            SIP_PUBLIC_IP,
            SIP_TEST_USERNAME, SIP_TEST_PASSWORD,
            SIP_RTP_PORT_LOW, SIP_RTP_PORT_HIGH,
        )

        self.agent_id = agent_id
        self.voice    = voice
        self.language = language
        self.mode     = SIP_MODE

        local_ip  = SIP_BIND_IP if SIP_BIND_IP and SIP_BIND_IP != "0.0.0.0" else _get_local_ip()
        public_ip = SIP_PUBLIC_IP if SIP_PUBLIC_IP else local_ip

        logger.info("[SIPServer] local_ip=%s  public_ip=%s  mode=%s", local_ip, public_ip, SIP_MODE)

        if SIP_MODE == "multinet":
            if not SIP_SERVER or not SIP_USERNAME:
                raise ValueError("SIP_SERVER and SIP_USERNAME required for multinet mode.")

            # MultinetRegistrar handles outbound REGISTER (IP auth OR Digest)
            self._registrar = MultinetRegistrar(
                server=SIP_SERVER,
                port=SIP_SERVER_PORT,
                username=SIP_USERNAME,
                password=SIP_PASSWORD,
                local_ip=local_ip,
                local_port=SIP_BIND_PORT,
                public_ip=public_ip,
                on_registered=self._on_registered,
                on_failed=self._on_registration_failed,
            )

            # RawSIPServer listens for incoming INVITEs from Multinet
            # shares the same socket as the registrar to avoid port conflict
            self._sip_server = RawSIPServer(
                bind_ip=local_ip,
                bind_port=SIP_BIND_PORT,
                username=SIP_USERNAME,
                password=SIP_PASSWORD,
                on_call=self._on_incoming_call,
                agent_id=agent_id,
                voice=voice,
                language=language,
                rtp_port_low=SIP_RTP_PORT_LOW,
                rtp_port_high=SIP_RTP_PORT_HIGH,
            )
            self._local_ip    = local_ip
            self._public_ip   = public_ip
            self._bind_port   = SIP_BIND_PORT
            self._sip_server_addr = SIP_SERVER
            self._sip_server_port = SIP_SERVER_PORT
            self._username    = SIP_USERNAME

        else:
            # Local mode — MicroSIP softphone testing
            self._registrar  = None
            self._sip_server = RawSIPServer(
                bind_ip=local_ip,
                bind_port=SIP_BIND_PORT,
                username=SIP_TEST_USERNAME,
                password=SIP_TEST_PASSWORD,
                on_call=self._on_incoming_call,
                agent_id=agent_id,
                voice=voice,
                language=language,
                rtp_port_low=SIP_RTP_PORT_LOW,
                rtp_port_high=SIP_RTP_PORT_HIGH,
            )
            self._local_ip        = local_ip
            self._bind_port       = SIP_BIND_PORT
            self._test_username   = SIP_TEST_USERNAME
            self._test_password   = SIP_TEST_PASSWORD

    def _on_registered(self):
        logger.info("[SIPServer] ✅ Registered to Multinet — ready for calls")

    def _on_registration_failed(self):
        logger.error("[SIPServer] ❌ Registration failed — calls will NOT arrive")

    def start(self):
        print("\n" + "=" * 60)
        print("  BlenSpark SIP Server")
        print("=" * 60)

        if self.mode == "multinet":
            # Start registrar first — it owns the socket
            self._registrar.start()

            # Wait up to 15s for first successful registration
            logger.info("[SIPServer] Waiting for Multinet registration...")
            for i in range(15):
                if self._registrar.registered:
                    break
                time.sleep(1)
                if i % 5 == 4:
                    logger.info("[SIPServer] Still waiting for registration (%ds)...", i + 1)

            if not self._registrar.registered:
                logger.warning(
                    "[SIPServer] ⚠ Not yet registered — starting SIP listener anyway. "
                    "Registrar will keep retrying in background."
                )

            # Share the registrar's socket with RawSIPServer (same port)
            self._sip_server._shared_sock = self._registrar.get_socket()
            self._sip_server.start()

            auth_mode = self._registrar._auth_mode
            print(f"\n  Mode:       MULTINET TRUNK")
            print(f"  Server:     {self._sip_server_addr}:{self._sip_server_port}")
            print(f"  Username:   {self._username}")
            print(f"  Local IP:   {self._local_ip}")
            print(f"  Public IP:  {self._public_ip}")
            print(f"  Auth mode:  {auth_mode.upper()}")
            print(f"  Registered: {'✅ YES' if self._registrar.registered else '⚠ PENDING'}")

        else:
            self._sip_server.start()
            print(f"\n  Mode:     LOCAL (MicroSIP testing)")
            print(f"  SIP Host: {self._local_ip}:{self._bind_port}")
            print(f"  Username: {self._test_username}")
            print(f"  Password: {self._test_password}")
            print(f"\n  MicroSIP setup:")
            print(f"    SIP Server: {self._local_ip}")
            print(f"    Username:   {self._test_username}")
            print(f"    Password:   {self._test_password}")
            print(f"    Domain:     {self._local_ip}")

        print(f"\n  Agent:    {self.agent_id}")
        print(f"  Voice:    {self.voice}")
        print(f"  Language: {self.language}")
        print(f"\n  Waiting for incoming calls...")
        print("=" * 60 + "\n")

    def stop(self):
        logger.info("[SIPServer] Stopping...")
        self._sip_server.stop()
        if self._registrar:
            self._registrar.stop()

    def _on_incoming_call(self, call):
        caller = getattr(call, "caller", "unknown")
        logger.info("[SIPServer] 📞 Incoming call from: %s", caller)
        print(f"\n📞 Incoming call from: {caller}", flush=True)

        try:
            call.answer()
            logger.info("[SIPServer] ✅ Call answered")
            print(f"✅ Bridging to {self.agent_id} agent...", flush=True)

            bridge = SIPCallBridge(
                call=call,
                agent_id=self.agent_id,
                voice=self.voice,
                language=self.language,
            )
            bridge.start()

            while bridge._running:
                try:
                    if call.state != CallState.ANSWERED:
                        bridge._running = False
                        break
                except Exception:
                    break
                time.sleep(0.5)

            logger.info("[SIPServer] 📴 Call ended")
            print("📴 Call ended\n", flush=True)

        except InvalidStateError:
            logger.info("[SIPServer] Call already disconnected")
        except Exception as e:
            logger.error("[SIPServer] Call handling error: %s", e, exc_info=True)
            try:
                call.hangup()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────

def start_sip_server(agent_id="healthcare", voice="Aoede", language="ur-PK"):
    """Called from Django management command."""
    server = SIPServer(agent_id=agent_id, voice=voice, language=language)
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.stop()
