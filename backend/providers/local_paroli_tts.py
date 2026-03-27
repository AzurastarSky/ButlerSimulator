"""
Local Paroli TTS Provider
Connects to Paroli TTS server running on local board.
"""

import os
import requests
from typing import Optional


# Configuration: Paroli TTS server
# Primary IP address - change LOCAL_BOARD_IP for office (192.168.0.222) vs home (192.168.1.245)
LOCAL_BOARD_IP = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")

PAROLI_HOST = os.getenv("PAROLI_HOST", LOCAL_BOARD_IP)
PAROLI_PORT = os.getenv("PAROLI_PORT", "3500")
PAROLI_BASE_URL = f"http://{PAROLI_HOST}:{PAROLI_PORT}"
# Configurable endpoint - adjust this to match your Paroli API
PAROLI_ENDPOINT = os.getenv("PAROLI_ENDPOINT", "/api/v1/synthesise")


def synthesize_speech(text: str, voice: Optional[str] = None) -> bytes:
    """
    Synthesize speech using local Paroli TTS server.
    
    Args:
        text: Text to synthesize
        voice: Optional voice parameter (may not be used by Paroli, included for API compatibility)
    
    Returns:
        Audio bytes (MP3 format)
    
    Raises:
        Exception: If TTS synthesis fails
    """
    try:
        # Use configured endpoint
        endpoint = f"{PAROLI_BASE_URL}{PAROLI_ENDPOINT}"
        
        print(f"[Paroli TTS] Attempting request to: {endpoint}")
        print(f"[Paroli TTS] Text length: {len(text)} chars")
        
        # Prepare request payload
        # Try multiple common payload formats
        payload = {
            "text": text,
        }
        
        # Add voice parameter if provided and supported by your Paroli installation
        if voice:
            payload["voice"] = voice
        
        print(f"[Paroli TTS] Payload: {payload}")
        
        # Make TTS request with timeout
        response = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30.0  # 30 second timeout for TTS synthesis
        )
        
        # Check for errors
        if response.status_code != 200:
            error_msg = f"Paroli TTS failed with status {response.status_code}"
            try:
                error_detail = response.json()
                error_msg += f": {error_detail}"
            except:
                error_msg += f": {response.text[:200]}"
            raise Exception(error_msg)
        
        # Return audio bytes
        audio_bytes = response.content
        
        if not audio_bytes:
            raise Exception("Paroli TTS returned empty audio")
        
        return audio_bytes
        
    except requests.exceptions.Timeout:
        raise Exception(f"Paroli TTS request timed out (server: {PAROLI_BASE_URL})")
    except requests.exceptions.ConnectionError:
        raise Exception(f"Cannot connect to Paroli TTS server at {PAROLI_BASE_URL}")
    except Exception as e:
        if "Paroli TTS" in str(e):
            raise  # Re-raise our custom exceptions
        raise Exception(f"Paroli TTS synthesis failed: {repr(e)}")


def check_paroli_health() -> bool:
    """
    Check if Paroli TTS server is reachable.
    
    Returns:
        True if server is healthy, False otherwise
    """
    # Try multiple endpoints in order of preference
    endpoints_to_try = [
        "/health",
        "/api/health",
        "/ping",
        "/status",
        PAROLI_ENDPOINT,  # The actual synthesis endpoint
        "/",  # Base URL as last resort
    ]
    
    for endpoint in endpoints_to_try:
        try:
            url = f"{PAROLI_BASE_URL}{endpoint}" if not endpoint.startswith("http") else endpoint
            response = requests.get(url, timeout=3.0)
            
            # Accept any response that indicates server is alive:
            # 200 OK, 404 Not Found (server up, route doesn't exist),
            # 405 Method Not Allowed (server up, wrong method),
            # 501 Not Implemented (server up, endpoint exists but not fully implemented)
            if response.status_code in [200, 404, 405, 501]:
                return True
        except requests.exceptions.ConnectionError:
            # Connection refused = server definitely down, try next endpoint
            continue
        except requests.exceptions.Timeout:
            # Timeout = server might be slow, but likely up
            return True
        except:
            # Other errors, try next endpoint
            continue
    
    # If all endpoints failed, server is down
    return False
