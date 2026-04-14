"""
Django management command to start the SIP server.

Usage:
  python manage.py run_sip_server                          # Local test mode (default)
  python manage.py run_sip_server --mode multinet           # Multinet trunk mode
  python manage.py run_sip_server --agent restaurant        # Use restaurant agent
  python manage.py run_sip_server --voice Puck --lang en-US # English with male voice
"""
import os
import signal
import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the pyVoIP SIP server that bridges calls to the Gemini Live voice agent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            type=str,
            default=None,
            choices=["local", "multinet"],
            help="SIP mode: 'local' for MicroSIP testing, 'multinet' for Multinet trunk. "
                 "Overrides SIP_MODE env var.",
        )
        parser.add_argument(
            "--agent",
            type=str,
            default=None,
            help="Agent ID to use: 'healthcare' or 'restaurant'. "
                 "Overrides SIP_AGENT_ID env var. Default: healthcare",
        )
        parser.add_argument(
            "--voice",
            type=str,
            default=None,
            help="Gemini voice name (e.g., Aoede, Puck, Charon). "
                 "Overrides SIP_VOICE env var.",
        )
        parser.add_argument(
            "--lang",
            type=str,
            default=None,
            help="Language code: 'ur-PK' (Urdu) or 'en-US' (English). "
                 "Overrides SIP_LANGUAGE env var.",
        )

    def handle(self, *args, **options):
        # Override env vars from CLI args
        if options["mode"]:
            os.environ["SIP_MODE"] = options["mode"]
        if options["agent"]:
            os.environ["SIP_AGENT_ID"] = options["agent"]
        if options["voice"]:
            os.environ["SIP_VOICE"] = options["voice"]
        if options["lang"]:
            os.environ["SIP_LANGUAGE"] = options["lang"]

        # Load config (after env override)
        from voice.sip_config import SIP_AGENT_ID, SIP_VOICE, SIP_LANGUAGE

        agent_id = options["agent"] or SIP_AGENT_ID
        voice = options["voice"] or SIP_VOICE
        language = options["lang"] or SIP_LANGUAGE

        # Validate agent exists
        from voice.agents.registry import get_agent
        agent_cfg = get_agent(agent_id)
        if not agent_cfg:
            self.stderr.write(
                self.style.ERROR(f"Unknown agent: '{agent_id}'. Available: healthcare, restaurant")
            )
            sys.exit(1)

        # Validate GEMINI_API_KEY
        if not os.environ.get("GEMINI_API_KEY"):
            self.stderr.write(
                self.style.ERROR("GEMINI_API_KEY environment variable is not set!")
            )
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS(
            f"Starting SIP server with agent={agent_id}, voice={voice}, lang={language}"
        ))

        from voice.sip_client import start_sip_server

        # Handle SIGINT gracefully
        def sigint_handler(signum, frame):
            self.stdout.write("\nReceived SIGINT, shutting down...")
            sys.exit(0)

        signal.signal(signal.SIGINT, sigint_handler)

        try:
            start_sip_server(
                agent_id=agent_id,
                voice=voice,
                language=language,
            )
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nSIP server stopped."))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"SIP server error: {e}"))
            import traceback
            traceback.print_exc()
            sys.exit(1)
