"""
urls.py — URL routes for the WhatsApp bot webhook.

  Green API  → /whatsapp/webhook/
              /whatsapp/health/

  Meta (WhatsApp Business Cloud API)
             → /whatsapp/meta/webhook/   (GET = verify, POST = messages)
               /whatsapp/meta/health/
"""

from django.urls import path
from whatsapp.views import webhook, health
from whatsapp.meta_views import meta_webhook, meta_health

urlpatterns = [
    # ── Green API (unchanged) ──────────────────────────────────────────────
    path("webhook/", webhook, name="whatsapp_webhook"),
    path("health/",  health,  name="whatsapp_health"),

    # ── Meta WhatsApp Business Cloud API ──────────────────────────────────
    path("meta/webhook/", meta_webhook, name="meta_whatsapp_webhook"),
    path("meta/health/",  meta_health,  name="meta_whatsapp_health"),
]
