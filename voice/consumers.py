"""
TwilioMediaConsumer — Django Channels WebSocket consumer for Twilio Media Streams.

Twilio sends JSON messages with event types: connected, start, media, stop.
This consumer:
  - On 'start': creates a CallSession, starts Deepgram
  - On 'media': decodes audio, resamples, sends to Deepgram
  - On 'stop':  cleans up the session
  - Sends audio back as Twilio 'media' events (via CallSession.speak_fn)
"""
import asyncio
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer

from .session import CallSession
from .audio import twilio_payload_to_pcm16k

logger = logging.getLogger(__name__)

VOICE_SESSION_START_FAILED = 4001


class TwilioMediaConsumer(AsyncWebsocketConsumer):
    """One instance per Twilio media stream (i.e., per phone call)."""

    async def connect(self):
        """Accept the WebSocket from Twilio."""
        await self.accept()
        self._session = None
        logger.info("[TwilioWS] Connection accepted")

    async def receive(self, text_data=None, bytes_data=None):
        """
        Twilio sends all messages as text (JSON).
        Binary frames are never sent by Twilio media streams.
        """
        if not text_data:
            return

        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            logger.warning("[TwilioWS] Non-JSON message received")
            return

        event = msg.get("event")

        if event == "connected":
            logger.info("[TwilioWS] Twilio connected: protocol=%s", msg.get("protocol"))

        elif event == "start":
            await self._handle_start(msg)

        elif event == "media":
            await self._handle_media(msg)

        elif event == "stop":
            await self._handle_stop(msg)

        elif event == "mark":
            logger.debug("[TwilioWS] Mark event: %s", msg.get("mark", {}).get("name"))

        else:
            logger.debug("[TwilioWS] Unknown event: %s", event)

    async def disconnect(self, close_code):
        """Clean up when WebSocket closes."""
        logger.info("[TwilioWS] Disconnecting (code=%s)", close_code)
        if self._session:
            await asyncio.to_thread(self._session.cleanup)
            self._session = None

    # ══════════════════════════════════════════════════════════════════
    # Event handlers
    # ══════════════════════════════════════════════════════════════════

    async def _handle_start(self, msg):
        """
        Twilio 'start' event. Contains streamSid, callSid, mediaFormat, etc.

        msg["start"] = {
            "streamSid": "...",
            "accountSid": "...",
            "callSid": "...",
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {}
        }
        """
        start_data = msg.get("start", {})
        stream_sid = start_data.get("streamSid", msg.get("streamSid", ""))
        call_sid = start_data.get("callSid", "")

        logger.info(
            "[TwilioWS] Stream started: CallSid=%s StreamSid=%s",
            call_sid, stream_sid,
        )

        try:
            self._session = CallSession(
                call_sid=call_sid,
                stream_sid=stream_sid,
                ws_send_fn=self._send_json,
            )
            self._session.set_event_loop(asyncio.get_running_loop())

            # Start Deepgram in a thread (synchronous SDK)
            await asyncio.to_thread(self._session.start_deepgram)
        except Exception as exc:
            logger.exception("[TwilioWS] Failed to start call session: %s", exc)
            self._session = None
            await self.close(code=VOICE_SESSION_START_FAILED)

    async def _handle_media(self, msg):
        """
        Twilio 'media' event. Contains base64-encoded mulaw audio.

        msg["media"] = {
            "track": "inbound",
            "chunk": "1",
            "timestamp": "5",
            "payload": "<base64 mulaw>"
        }
        """
        if not self._session:
            return

        media = msg.get("media", {})
        payload = media.get("payload", "")

        if not payload:
            return

        # Convert Twilio mulaw 8kHz → PCM 16kHz (with streaming state)
        pcm_16k, self._session.ratecv_state_in = twilio_payload_to_pcm16k(
            payload, self._session.ratecv_state_in
        )

        # Forward PCM to Deepgram
        await asyncio.to_thread(self._session.send_audio_to_deepgram, pcm_16k)

    async def _handle_stop(self, msg):
        """Twilio 'stop' event. The caller hung up or stream ended."""
        logger.info("[TwilioWS] Stream stopped")
        if self._session:
            await asyncio.to_thread(self._session.cleanup)
            self._session = None

    # ══════════════════════════════════════════════════════════════════
    # WebSocket send helper
    # ══════════════════════════════════════════════════════════════════

    async def _send_json(self, data: dict):
        """Send a JSON message back to Twilio over the WebSocket."""
        try:
            await self.send(text_data=json.dumps(data))
        except Exception as e:
            logger.error("[TwilioWS] Failed to send: %s", e)
