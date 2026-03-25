import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
import warnings
warnings.filterwarnings("ignore")

_kfc_api_dir = Path(__file__).resolve().parent
_env_file = _kfc_api_dir / ".env"
load_dotenv(str(_env_file), override=True)

_creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
if _creds_path and not os.path.isabs(_creds_path):
    _abs_creds = str(_kfc_api_dir / _creds_path)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _abs_creds

from google import genai
from google.genai import types

async def main():
    _proj = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    _loc = os.getenv("GOOGLE_CLOUD_REGION", "europe-west4").strip()

    client = genai.Client(vertexai=True, project=_proj, location=_loc)
    
    live_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text="Say hello short.")]),
    )

    try:
        async with client.aio.live.connect(model="gemini-2.5-flash-native-audio-preview-12-2025", config=live_config) as session:
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
                
                if hasattr(response, "usage_metadata"):
                    print("Found usage_metadata:", dir(response.usage_metadata))
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
