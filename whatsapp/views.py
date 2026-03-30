"""
views.py — Django views for WhatsApp webhook.

Green API sends POST requests here when messages arrive.
The view returns 200 immediately and processes the message in a background thread.
"""

import json
import logging
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from whatsapp.bot import handle_webhook_async

log = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["POST"])
def webhook(request):
    """
    Green API Webhook endpoint.

    Green API POSTs JSON here for every incoming message.
    We return 200 immediately and process asynchronously.
    """
    try:
        body = json.loads(request.body)
        log.info("Webhook received: type=%s", body.get("typeWebhook", "unknown"))

        # Process in background thread (non-blocking)
        handle_webhook_async(body)

        return JsonResponse({"status": "ok"})

    except json.JSONDecodeError:
        log.warning("Webhook received invalid JSON")
        return HttpResponse(status=400)
    except Exception as e:
        log.error("Webhook view error: %s", e)
        return JsonResponse({"status": "error"}, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def health(request):
    """Health check endpoint for the WhatsApp bot."""
    return JsonResponse({
        "status": "active",
        "service": "BlenSpark WhatsApp Bot",
    })
