"""
Simple OpenAI TTS provider wrapper using REST calls.
Exports: `synthesize_speech(text, voice)` and `check_openai_health()`

This uses the `OPENAI_API_KEY` env var to authenticate. The exact OpenAI TTS endpoint
and parameter names may vary; errors from the API are raised as Exceptions.
"""
import os
import requests
from typing import Optional

OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"


def check_openai_health() -> bool:
    """Return True if API key is set (basic health check)."""
    return bool(OPENAI_KEY)


def synthesize_speech(text: str, voice: Optional[str] = None) -> bytes:
    """
    Synthesize speech via OpenAI's TTS endpoint.

    Args:
        text: text to synthesize
        voice: voice name (e.g., 'onyx', 'alloy', 'echo')

    Returns bytes (audio/mpeg) or raises Exception on failure.
    """
    if not OPENAI_KEY:
        raise Exception("OPENAI_API_KEY not set")

    payload = {
        "model": "tts-1",
        "voice": voice or "onyx",
        "input": text,
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(OPENAI_TTS_URL, json=payload, headers=headers, timeout=60)
    except requests.exceptions.RequestException as e:
        raise Exception(f"OpenAI TTS request failed: {e}")

    if resp.status_code != 200:
        # Try to include response text for easier debugging
        txt = resp.text[:1000] if resp.text else ""
        raise Exception(f"OpenAI TTS failed: {resp.status_code} {txt}")

    # Return raw audio bytes
    return resp.content
