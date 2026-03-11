"""HTTP URL patterns for Twilio webhooks."""
from django.urls import path

from . import views

app_name = "voice"

urlpatterns = [
    path("incoming/", views.incoming_call, name="incoming"),
    path("status/", views.call_status, name="status"),
]
