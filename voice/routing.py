"""WebSocket URL patterns for Twilio media streams."""
from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/voice/stream/$", consumers.TwilioMediaConsumer.as_asgi()),
]
