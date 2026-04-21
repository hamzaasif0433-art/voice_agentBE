"""WebSocket URL patterns for voice agents."""
from django.urls import re_path

from . import consumers
from voice.consumers_browser import BrowserVoiceConsumer  # Browser FE (PCM16)

websocket_urlpatterns = [
    # Inbound Twilio now uses the Gemini Live consumer route.
    re_path(r"^ws/voice/voice-agent/$",     consumers.VoiceAgentConsumer.as_asgi()),

    # Dynamic browser route — agent_id resolved per-connection
    # e.g. ws://host/ws/voice/healthcare/  or  ws://host/ws/voice/restaurant/
    re_path(r"^ws/voice/(?P<agent_id>[a-z0-9_-]+)/$", BrowserVoiceConsumer.as_asgi()),
]
