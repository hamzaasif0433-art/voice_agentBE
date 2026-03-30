"""
urls.py — URL routes for the WhatsApp bot webhook.
"""

from django.urls import path
from whatsapp.views import webhook, health

urlpatterns = [
    path("webhook/", webhook, name="whatsapp_webhook"),
    path("health/",  health,  name="whatsapp_health"),
]
