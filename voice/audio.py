"""
Audio conversion between Twilio mulaw/8000Hz and Deepgram/ElevenLabs PCM/16000Hz.

Twilio media stream sends: base64-encoded mulaw audio at 8000Hz, mono
Deepgram expects:          raw PCM (linear16) at 16000Hz, mono
ElevenLabs produces:       raw PCM at 16000Hz, mono
Twilio media stream wants: base64-encoded mulaw audio at 8000Hz, mono
"""
import base64
import audioop


def twilio_payload_to_pcm16k(payload_b64: str, ratecv_state=None):
    """
    Convert Twilio media payload (base64 mulaw 8kHz) to PCM linear16 at 16kHz.

    Returns (pcm_16k_bytes, new_ratecv_state) for streaming continuity.

    Steps:
    1. base64 decode → raw mulaw bytes (8000Hz)
    2. mulaw → PCM linear16 (still 8000Hz) via audioop.ulaw2lin
    3. Resample 8000Hz → 16000Hz via audioop.ratecv (with state)
    """
    mulaw_8k = base64.b64decode(payload_b64)
    pcm_8k = audioop.ulaw2lin(mulaw_8k, 2)  # 2 = sample width bytes (16-bit)
    pcm_16k, new_state = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, ratecv_state)
    return pcm_16k, new_state


def pcm16k_to_twilio_payload(pcm_16k: bytes, ratecv_state=None):
    """
    Convert PCM linear16 at 16kHz to Twilio media payload (base64 mulaw 8kHz).

    Returns (base64_mulaw_str, new_ratecv_state) for streaming continuity.

    Steps:
    1. Resample 16000Hz → 8000Hz via audioop.ratecv (with state)
    2. PCM linear16 → mulaw via audioop.lin2ulaw
    3. base64 encode
    """
    pcm_8k, new_state = audioop.ratecv(pcm_16k, 2, 1, 16000, 8000, ratecv_state)
    mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
    return base64.b64encode(mulaw_8k).decode("ascii"), new_state
