import asyncio
import os
import truststore
import logging
from pathlib import Path
from dotenv import load_dotenv
import warnings
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.DEBUG)
truststore.inject_into_ssl()

_kfc_api_dir = Path(__file__).resolve().parent
_env_file = _kfc_api_dir / ".env"
load_dotenv(str(_env_file), override=True)

import json
from google.oauth2 import service_account
import vertexai
from google import genai
from google.genai import types

async def main():
    # Use Direct Google AI Studio API for Gemini 3.1 Flash Live Preview
    _api_key = os.getenv("GEMINI_API_KEY", "").strip()
    client = genai.Client(api_key=_api_key)
    
    live_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text="Say hello short.")]),
    )

    try:
        # Use the correct 3.1 Live identifier for AI Studio
        async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=live_config) as session:
            print("[TEST] Connected to Gemini 3.1 Flash Live Preview!", flush=True)
            await session.send_realtime_input(text="Hi.")
            count = 0
            async for response in session.receive():
                
                sc = getattr(response, "server_content", None)
                if sc:
                    print("Received response!")
                    if hasattr(sc, "model_turn") and sc.model_turn is not None:
                        for part in sc.model_turn.parts:
                            if part.text:
                                print("Text:", part.text)
                
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                        um = response.usage_metadata
                        print(f"""
                    ─── Token Usage ───────────────────────
                    Prompt tokens:    {um.prompt_token_count}
                    Response tokens:  {um.response_token_count}
                    Thoughts tokens:  {um.thoughts_token_count}
                    Total tokens:     {um.total_token_count}
                    Cached tokens:    {um.cached_content_token_count}
                    ───────────────────────────────────────
                    """)
                elif hasattr(sc, "usage_metadata") if sc else False:
                    print("Found sc.usage_metadata")
                elif hasattr(response, "token_count"):
                    print("Found token_count")

                count += 1
                if count > 5 or (sc and getattr(sc, "turn_complete", False)):
                    break
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
