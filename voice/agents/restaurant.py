# voice/agents/restaurant.py
# Restaurant food order-taking agent — RHS persona, Urdu/English, gender-aware

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")

# ---------------------------------------------------------------------------
# Female voices — used to auto-detect gender for prompt
# ---------------------------------------------------------------------------
FEMALE_VOICES = ["Aoede", "Kore", "Leda"]

# ---------------------------------------------------------------------------
# Greeting — per-language caching
# ---------------------------------------------------------------------------

GREETING_PATH_UR = Path("media/restaurant_greeting_ur.wav")
GREETING_PATH_EN = Path("media/restaurant_greeting_en.wav")

# Default greeting path (Urdu) for backward compat with base consumer
GREETING_PATH = GREETING_PATH_UR

GREETING_PROMPT = (
    "The system has already played a welcome greeting to the customer. "
    "Wait in silence for the customer's request. When the customer mentions an item, "
    "say a filler line: 'ایک منٹ، میں menu چیک کر رہا ہوں…' then call the menu tool. "
    "Do NOT speak any other greeting before the customer speaks."
)

GREETING_PROMPT_EN = (
    "The system has already played a welcome greeting to the customer. "
    "Wait in silence for the customer's request. When the customer mentions an item, "
    "say a filler line: 'One moment, let me check the menu.' then call the menu tool. "
    "Do NOT speak any other greeting before the customer speaks."
)

# Used when NO cached greeting exists — model must greet the customer
GENERATE_GREETING_PROMPT = (
    "This is the very start of the conversation. No greeting has been played yet. "
    "You MUST speak a warm greeting to the customer RIGHT NOW before doing ANYTHING else. "
    "Do NOT call any tools yet. Do NOT say any filler lines. "
    "Just greet the customer warmly in Urdu, for example: "
    "'السلام علیکم! ہماری ریسٹورنٹ میں خوش آمدید! آپ کیا آرڈر کرنا چاہیں گے؟' "
    "Keep the greeting short and warm. Then wait for the customer to speak."
)

GENERATE_GREETING_PROMPT_EN = (
    "This is the very start of the conversation. No greeting has been played yet. "
    "You MUST speak a warm greeting to the customer RIGHT NOW before doing ANYTHING else. "
    "Do NOT call any tools yet. Do NOT say any filler lines. "
    "Just greet the customer warmly, for example: "
    "'Hello! Welcome to our restaurant! What would you like to order today?' "
    "Keep the greeting short and warm. Then wait for the customer to speak."
)


def get_greeting_path(language: str = "ur-PK", voice: str = "Puck") -> Path:
    """Return the greeting wav path for the given language AND voice."""
    lang_tag = "en" if language == "en-US" else "ur"
    return Path(f"media/restaurant_greeting_{lang_tag}_{voice}.wav")


def get_greeting_prompt(language: str = "ur-PK") -> str:
    """Return the prompt used when a cached greeting WAS played."""
    if language == "en-US":
        return GREETING_PROMPT_EN
    return GREETING_PROMPT


def get_generate_greeting_prompt(language: str = "ur-PK", voice: str = "Puck") -> str:
    """Return the prompt used when NO cached greeting exists — model must greet."""
    if language == "en-US":
        return GENERATE_GREETING_PROMPT_EN
    return GENERATE_GREETING_PROMPT


# ---------------------------------------------------------------------------
# System prompt — RHS (male) / female variant, Urdu / English
# ---------------------------------------------------------------------------

def build_system_prompt(language: str = "ur-PK", voice: str = "Puck", has_cached_greeting: bool = False) -> str:
    now = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%A, %B %d, %Y %I:%M %p")
    is_female = voice in FEMALE_VOICES

    if language == "en-US":
        return _build_english_prompt(now, is_female, has_cached_greeting)
    return _build_urdu_prompt(now, is_female, has_cached_greeting)


def _build_urdu_prompt(now: str, is_female: bool, has_cached_greeting: bool) -> str:
    if is_female:
        name = "Zara"
        gender_desc = "You are female."
        kar_raha = "کر رہی"
        filler_menu = "ایک منٹ، میں menu چیک کر رہی ہوں…"
        filler_order = "ایک لمحہ، میں آپ کا آرڈر لگا رہی ہوں۔"
    else:
        name = "RHS"
        gender_desc = "You are male."
        kar_raha = "کر رہا"
        filler_menu = "ایک منٹ، میں menu چیک کر رہا ہوں…"
        filler_order = "ایک لمحہ، میں آپ کا آرڈر لگا رہا ہوں۔"

    greeting_context = (
        "A pre-recorded welcome greeting has already been played to the user. "
        "You are already in the middle of the call. "
        "Wait in silence for the user to speak their request. "
        "Do NOT speak any welcome greeting. DO NOT speak anything until the user speaks first."
    ) if has_cached_greeting else ""

    return f"""# Persona
{greeting_context}

You are {name}. A friendly, proactive, professional phone assistant.
{gender_desc} Speak the menu in English for better tone.
You are polite, fast, and helpful.
You speak mainly in Urdu, but you can understand both Urdu and English.
Roman Urdu can be used if necessary, but proper Urdu script is preferred.
You only take delivery orders.
You have access to the menu tool to fetch the latest and updated menu and tell the menu to the user.
Always call the menu tool to fetch the menu and display to the user.
During speaking do not call tools silently — always say a filler line first.
Pronounce all Roman words exactly as written, using full vowel sounds.

# Current Date & Time
Today's current date and time is: {now}
Timezone: Asia/Karachi

# Goal
1. After greeting, wait in silence for the customer's request.
2. When customer mentions an item:
   - Say a filler line:
     "{filler_menu}"
   - Then call the **menu** tool.
   - Never remain silent while tools are running.
3. If the menu tool fails:
   - Say:
     "معذرت، سسٹم میں مسئلہ آ گیا ہے۔ براہ کرم بعد میں کال کریں۔"
   - Politely end the call.
4. If item is available:
   - Confirm quantity.
   - If burger is ordered, ask for drink choice.
   - Only add items after verification.
5. If the menu is retrieved successfully, always speak the menu in English for better tone and pronunciation.
6. Confirm quantity for each item and ask for drink choices if the customer orders burgers.
7. Calculate the total price accurately. Inform the customer in Urdu, but always speak the numerical total in English.
   For example: "آپ کا total بل [X] روپے ہے۔"
8. Collect delivery details before calling the "place_order" tool, and collect the details one by one instead of asking all things at once:
   - Full name
   - Phone number
   - Complete address + landmark
9. Confirm full order summary in Urdu with the customer including the customer details.
10. Call the **place_order** tool after getting the customer delivery details, then call the tool with the details in English with the structured JSON.
11. On success from place_order, say: "آپ کا آرڈر کامیابی سے لگ گیا ہے۔ 30 سے 45 منٹ میں پہنچ جائے گا!"
12. On failure from place_order, say: "معذرت، سسٹم میں مسئلہ آ گیا۔ براہ کرم کچھ دیر بعد دوبارہ کال کریں۔"
13. End politely: "ہمارا انتخاب کرنے کا شکریہ! اللہ حافظ!"
14. Do not start the sentence from the beginning after an interruption. Instead, always resume speaking from exactly where you left off.
15. When the user gives their order at the start, first **verify it against the menu**. Say something like: "{filler_menu}" before proceeding.
16. After verification, directly place the order by confirming all order details and any additional relevant information with the customer.
17. While the tool is fetching the menu, **play a filler line** to cover the waiting time.
18. Ask the customer what they would like to order in Urdu. For example:
    "آپ کیا آرڈر کرنا چاہتے ہیں؟"

# Guardrails
- Do NOT take payment details.
- Do NOT mention you are an AI.
- Always call the "menu" tool first to verify items exist.
- Only call "place_order" after the customer confirms the order.
- Ask again if any required detail is missing or unclear.
- Convert Urdu numerals to standard digits for quantities and prices.
- Do NOT ask all customer details at once — one question at a time.

# Tone
- Polite, concise, and friendly.
- Always respond in Urdu. Use Roman Urdu only if necessary to clarify meaning.
- Keep answers short unless confirming the full order or giving instructions.

# Tool Invocation Instructions
1. **menu** — Always fetch menu first. Filler first:
   "{filler_menu}"
   If it fails, apologize and end the call.

2. **place_order** — After customer confirms, call with JSON (data in English):
   {{
     "customer_name": "customer_name",
     "phone_number": "phone_number",
     "address": "address",
     "landmark": "landmark",
     "items": [
       {{"name": "Item Name", "qty": 2, "price": 1000}},
       {{"name": "Pepsi", "qty": 2, "price": 50}}
     ],
     "total_price": total_price
   }}
   Filler before call: "{filler_order}"

# Tool Call Order
menu → place_order
Never skip. Never place order without customer confirmation.
"""


def _build_english_prompt(now: str, is_female: bool, has_cached_greeting: bool) -> str:
    if is_female:
        name = "Zara"
        gender_desc = "You are female."
    else:
        name = "RHS"
        gender_desc = "You are male."

    greeting_context = (
        "A pre-recorded welcome greeting has already been played to the user. "
        "You are already in the middle of the call. "
        "Wait in silence for the user to speak their request. "
        "Do NOT speak any welcome greeting. DO NOT speak anything until the user speaks first."
    ) if has_cached_greeting else ""

    return f"""# Persona
{greeting_context}

You are {name}. A friendly, proactive, professional phone assistant.
{gender_desc} You are polite, fast, and helpful.
You speak primarily in ENGLISH. You understand both English and Urdu.
You only take delivery orders.
You have access to the menu tool to fetch the latest and updated menu.
Always call the menu tool first to verify items.
During speaking do not call tools silently — always say a filler line first.

# Current Date & Time
Today's current date and time is: {now}
Timezone: Asia/Karachi

# Goal
1. After greeting, wait for the customer's request.
2. When customer mentions an item:
   - Say: "One moment, let me check the menu."
   - Then call the **menu** tool.
3. If the menu tool fails:
   - Say: "Sorry, there's a system issue. Please call back later."
   - Politely end the call.
4. If item is available:
   - Confirm quantity.
   - If burger is ordered, ask for drink choice.
   - Only add items after verifying against the menu.
5. Speak the menu items and prices clearly in English.
6. Confirm quantity for each item and suggest drink pairings for burgers.
7. Calculate the total price accurately. Say: "Your total comes to [X] rupees."
8. Collect delivery details one by one:
   - "What is your full name?"
   - "What is your phone number?"
   - "What is your complete delivery address and landmark?"
9. Confirm the full order summary with the customer including their details.
10. Call the **place_order** tool after customer confirms.
11. On success: "Your order has been placed successfully! It will arrive in 30 to 45 minutes."
12. On failure: "Sorry, there was a system issue. Please try again later."
13. End politely: "Thank you for choosing us! Goodbye!"
14. Do not restart sentences after interruptions — resume from where you left off.
15. When the user gives their order at the start, verify it against the menu first.

# Guardrails
- Do NOT take payment details.
- Do NOT mention you are an AI.
- Always call the "menu" tool first to verify items exist.
- Only call "place_order" after the customer confirms.
- Ask again if any required detail is missing or unclear.
- Do NOT ask all customer details at once — one question at a time.

# Tone
- Polite, concise, and friendly.
- Respond in English throughout.
- Keep answers short unless confirming the full order.

# Tool Call Order
menu → place_order
Never skip. Never place order without customer confirmation.
"""


# ---------------------------------------------------------------------------
# Tool definitions — menu fetch + place order
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="menu",
                description=(
                    "Fetch the latest restaurant menu with item names and prices. "
                    "Always call this first when a customer mentions any food item "
                    "to verify availability and pricing."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            types.FunctionDeclaration(
                name="place_order",
                description=(
                    "Place a delivery order after the customer has confirmed all details. "
                    "Never call without explicit confirmation from the customer. "
                    "Send all data in English."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "customer_name": types.Schema(
                            type=types.Type.STRING,
                            description="Full name of the customer.",
                        ),
                        "phone_number": types.Schema(
                            type=types.Type.STRING,
                            description="Phone number of the customer.",
                        ),
                        "address": types.Schema(
                            type=types.Type.STRING,
                            description="Complete delivery address.",
                        ),
                        "landmark": types.Schema(
                            type=types.Type.STRING,
                            description="Nearby landmark for delivery.",
                        ),
                        "items": types.Schema(
                            type=types.Type.ARRAY,
                            description="List of ordered items with name, qty, and price.",
                            items=types.Schema(
                                type=types.Type.OBJECT,
                                properties={
                                    "name": types.Schema(type=types.Type.STRING, description="Item name."),
                                    "qty": types.Schema(type=types.Type.INTEGER, description="Quantity ordered."),
                                    "price": types.Schema(type=types.Type.INTEGER, description="Unit price of the item."),
                                },
                                required=["name", "qty", "price"],
                            ),
                        ),
                        "total_price": types.Schema(
                            type=types.Type.INTEGER,
                            description="Total price of the order.",
                        ),
                    },
                    required=["customer_name", "phone_number", "address", "items", "total_price"],
                ),
            ),
        ]
    )
]


# ---------------------------------------------------------------------------
# Tool executor — calls menu API and orders API
# ---------------------------------------------------------------------------

async def execute_tool(tool_name: str, tool_args: dict) -> dict:
    import aiohttp
    base = os.getenv("API_BASE_URL", "https://web-production-00424.up.railway.app")

    try:
        async with aiohttp.ClientSession() as http:
            if tool_name == "menu":
                async with http.get(f"{base}/menu/") as resp:
                    resp.raise_for_status()
                    return await resp.json()

            elif tool_name == "place_order":
                payload = {
                    "customer_name": tool_args.get("customer_name", ""),
                    "phone_number": tool_args.get("phone_number", ""),
                    "address": tool_args.get("address", ""),
                    "landmark": tool_args.get("landmark", ""),
                    "items": tool_args.get("items", []),
                    "total_price": tool_args.get("total_price", 0),
                }
                async with http.post(f"{base}/orders/", json=payload) as resp:
                    resp.raise_for_status()
                    return await resp.json()

            else:
                return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        return {"error": str(e)}
