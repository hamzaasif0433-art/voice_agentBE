"""
Django management command to run the WhatsApp bot.

Usage:
    python manage.py run_whatsapp_bot
"""

import logging
from django.core.management.base import BaseCommand

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Start the BlenSpark WhatsApp bot (Green API long-polling)"

    def handle(self, *args, **options):
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            handlers=[logging.StreamHandler()],
        )

        self.stdout.write(self.style.SUCCESS("🍔 Starting BlenSpark WhatsApp Bot..."))

        try:
            from whatsapp.bot import create_bot
            bot, _ = create_bot()
            self.stdout.write(self.style.SUCCESS("✅ Bot connected. Listening for messages..."))
            bot.run_forever()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\n⏹️  Bot stopped by user."))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Bot error: {e}"))
            raise
