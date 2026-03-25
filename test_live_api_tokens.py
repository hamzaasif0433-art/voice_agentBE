import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
import warnings
warnings.filterwarnings("ignore")

_kfc_api_dir = Path(__file__).resolve().parent
_env_file = _kfc_api_dir / ".env"
load_dotenv(str(_env_file), override=True)

import json
from google.oauth2 import service_account
import vertexai
from google import genai
from google.genai import types

async def main():
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not service_account_json:
        raise EnvironmentError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.")
    sa_info = json.loads(service_account_json)
    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    _proj = os.getenv("VERTEX_PROJECT", "").strip()
    _loc = os.getenv("VERTEX_LOCATION", "europe-west4").strip()
    vertexai.init(
        project=_proj,
        location=_loc,
        credentials=credentials,
    )
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
