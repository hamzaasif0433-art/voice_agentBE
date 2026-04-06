from django.urls import path, include
from .views import orders, menu, categories, call_list, call_status
from .voice_ai import get_signed_url, health_check
from Analytics.urls import *
urlpatterns = [
    path("menu/", menu, name="menu"),
    path("menu/categories/", categories, name="categories"),
    path("orders/", orders, name="orders"),
    # Voice AI endpoints for secure ElevenLabs integration
    path("voice-ai/signed-url/", get_signed_url, name="get_signed_url"),
    path("voice-ai/health/", health_check, name="voice_ai_health"),
    # Call detail endpoints
    path("calls/", call_list, name="call-list"),
    path("calls/status/<str:conversation_id>/", call_status, name="call-status"),
    path('',include('Analytics.urls')),
]
