# voice/agents/registry.py
# Central registry of all voice agents.
# Each entry defines the agent's config — system prompt, tools, greeting, voices, languages.

from pathlib import Path
from . import healthcare, restaurant

# Available Gemini voices
ALL_VOICES = ["Aoede", "Puck", "Charon", "Kore", "Fenrir", "Leda"]
FEMALE_VOICES = ["Aoede", "Kore", "Leda"]       # warm female voices
MALE_VOICES   = ["Puck", "Charon", "Fenrir"]     # male voices

SUPPORTED_LANGUAGES = [
    {"code": "ur-PK", "label": "Urdu"},
    {"code": "en-US", "label": "English"},
]

# ---------------------------------------------------------------------------
# Registry: agent_id → config dict
# ---------------------------------------------------------------------------
AGENTS = {
    "healthcare": {
        "id": "healthcare",
        "name": "Healthcare Appointment",
        "description": "Book medical appointments with Ali (or Sara), your scheduling assistant.",
        "icon": "🏥",
        "default_voice": "Puck",
        "default_language": "en-US",
        "voices": ALL_VOICES,
        "languages": SUPPORTED_LANGUAGES,
        "greeting_path": healthcare.GREETING_PATH,
        "greeting_path_fn": healthcare.get_greeting_path,
        "greeting_prompt": healthcare.GREETING_PROMPT,
        "greeting_prompt_fn": healthcare.get_greeting_prompt,
        "generate_greeting_prompt_fn": healthcare.get_generate_greeting_prompt,
        "build_system_prompt": healthcare.build_system_prompt,
        "tools": None,          # populated lazily per-language in get_agent_tools()
        "tools_fn": lambda: healthcare.TOOLS,
        "execute_tool": healthcare.execute_tool,
        # Google Calendar — read from env at runtime
        "calendar_creds_env": "HEALTHCARE_CALENDAR_CREDS",
        "calendar_id_env":    "HEALTHCARE_CALENDAR_ID",
    },
    "restaurant": {
        "id": "restaurant",
        "name": "Restaurant Order",
        "description": "Order food delivery with RHS (or Zara), your friendly order assistant.",
        "icon": "🍔",
        "default_voice": "Puck",
        "default_language": "en-US",
        "voices": ALL_VOICES,
        "languages": SUPPORTED_LANGUAGES,
        "greeting_path": restaurant.GREETING_PATH,
        "greeting_path_fn": restaurant.get_greeting_path,
        "greeting_prompt": restaurant.GREETING_PROMPT,
        "greeting_prompt_fn": restaurant.get_greeting_prompt,
        "generate_greeting_prompt_fn": restaurant.get_generate_greeting_prompt,
        "build_system_prompt": restaurant.build_system_prompt,
        "tools_fn": lambda: restaurant.TOOLS,
        "execute_tool": restaurant.execute_tool,
    },
}


def get_agent(agent_id: str) -> dict | None:
    """Return the agent config dict, or None if not found."""
    return AGENTS.get(agent_id)


def list_agents_public() -> list:
    """Return a list of public-facing agent info (for the /voice/agents/ endpoint)."""
    return [
        {
            "id": cfg["id"],
            "name": cfg["name"],
            "description": cfg["description"],
            "icon": cfg["icon"],
            "default_voice": cfg["default_voice"],
            "default_language": cfg["default_language"],
            "voices": cfg["voices"],
            "languages": cfg["languages"],
        }
        for cfg in AGENTS.values()
    ]
