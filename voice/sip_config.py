"""
SIP Configuration — loaded from environment variables.

Supports two modes:
  - "local"    : pyVoIP acts as a SIP server; softphones (MicroSIP) connect directly.
  - "multinet" : pyVoIP registers as a SIP client to Multinet's SIP trunk.
"""
import os

# ── SIP Mode ─────────────────────────────────────────────────────────
SIP_MODE = os.environ.get("SIP_MODE", "local")  # "local" or "multinet"

# ── Local-mode settings (pyVoIP acts as SIP server) ──────────────────
SIP_BIND_IP = os.environ.get("SIP_BIND_IP", "0.0.0.0")
SIP_BIND_PORT = int(os.environ.get("SIP_BIND_PORT", "5061"))

# ── Multinet trunk settings ──────────────────────────────────────────
SIP_SERVER = os.environ.get("SIP_SERVER", "")         # Multinet SIP IP/hostname
SIP_SERVER_PORT = int(os.environ.get("SIP_SERVER_PORT", "5060"))
SIP_USERNAME = os.environ.get("SIP_USERNAME", "")
SIP_PASSWORD = os.environ.get("SIP_PASSWORD", "")

# ── Local test credentials (for softphone auth) ─────────────────────
SIP_TEST_USERNAME = os.environ.get("SIP_TEST_USERNAME", "100")
SIP_TEST_PASSWORD = os.environ.get("SIP_TEST_PASSWORD", "testpassword")

# ── Agent to use for SIP calls ───────────────────────────────────────
SIP_AGENT_ID = os.environ.get("SIP_AGENT_ID", "healthcare")
SIP_VOICE = os.environ.get("SIP_VOICE", "Aoede")
SIP_LANGUAGE = os.environ.get("SIP_LANGUAGE", "ur-PK")

# ── RTP port range ───────────────────────────────────────────────────
SIP_RTP_PORT_LOW = int(os.environ.get("SIP_RTP_PORT_LOW", "10000"))
SIP_RTP_PORT_HIGH = int(os.environ.get("SIP_RTP_PORT_HIGH", "20000"))

