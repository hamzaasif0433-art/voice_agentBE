# voice/views.py
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .agents.registry import list_agents_public


# ---------------------------------------------------------------------------
# Twilio webhook views (kept for URL compatibility)
# ---------------------------------------------------------------------------

@csrf_exempt
def incoming_call(request):
    """Twilio incoming call webhook — returns TwiML to connect to media stream."""
    # Railway domain, dev tunnel, or ngrok URL
    # fallback to the dev tunnel URL if the host isn't properly forwarded
    host = request.META.get('HTTP_X_FORWARDED_HOST', request.get_host())
    if "127.0.0.1" in host or "localhost" in host:
        host = "8rc8g56h-8000.asse.devtunnels.ms"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/ws/voice/voice-agent/?transport=twilio" />
  </Connect>
</Response>"""
    return HttpResponse(twiml, content_type="text/xml")


@csrf_exempt
def call_status(request):
    """Twilio call status callback — logs status updates."""
    return HttpResponse("OK", status=200)


# ---------------------------------------------------------------------------
# Voice agent list API
# ---------------------------------------------------------------------------

@api_view(["GET"])
def agents_list(request):
    """Return public list of all available voice agents with their config."""
    return Response(list_agents_public())
