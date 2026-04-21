import os
from django.apps import AppConfig
from django.conf import settings


class VoiceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "voice"
    verbose_name = "Twilio Voice"
    
    def ready(self):
        # Get MEDIA_ROOT with fallback, ensuring it's not empty
        media_root = getattr(settings, "MEDIA_ROOT", None)
        if not media_root:
            base_dir = getattr(settings, "BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
            media_root = os.path.join(base_dir, "media")

        # Ensure media directory exists
        if media_root and not os.path.exists(media_root):
            os.makedirs(media_root, exist_ok=True)
            print(f"[Voice] Created media directory: {media_root}")
