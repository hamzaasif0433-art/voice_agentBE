"""
HTTP endpoints for Twilio webhook integration.

POST /voice/incoming/  — Twilio calls this when a phone call comes in.
                         Returns TwiML that tells Twilio to open a media stream.
POST /voice/status/    — Twilio calls this when call status changes.
"""
import logging


from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from twilio.twiml.voice_response import VoiceResponse, Connect

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def incoming_call(request):
    """
    Twilio webhook for incoming calls.

    Returns TwiML:
      <Response>
        <Connect>
          <Stream url="wss://{host}/ws/voice/stream/" />
        </Connect>
      </Response>
    """
    request_data = request.POST if request.method == "POST" else request.GET
    call_sid = request_data.get("CallSid", "unknown")
    from_number = request_data.get("From", "unknown")
    logger.info("[Twilio] Incoming call: CallSid=%s From=%s", call_sid, from_number)

    response = VoiceResponse()
    connect = Connect()

    # Build the WebSocket URL from the request host
    host = request.get_host()
    # Twilio requires wss:// (TLS). In dev with ngrok this is handled automatically.
    stream_url = "wss://8rc8g56h-8000.asse.devtunnels.ms/ws/voice/stream/"

    connect.stream(url=stream_url)
    response.append(connect)

    logger.info("[Twilio] Returning TwiML with stream URL: %s", stream_url)
    return HttpResponse(str(response), content_type="application/xml")


@csrf_exempt
@require_http_methods(["GET", "POST"])
def call_status(request):
    """
    Twilio status callback. Called when call status changes
    (ringing, in-progress, completed, failed, etc.).
    """
    request_data = request.POST if request.method == "POST" else request.GET
    call_sid = request_data.get("CallSid", "unknown")
    status = request_data.get("CallStatus", "unknown")
    logger.info("[Twilio] Status: CallSid=%s Status=%s", call_sid, status)
    return HttpResponse("OK", status=200)
