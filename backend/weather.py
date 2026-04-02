"""
Weather module using Open-Meteo API for Brentwood, England.
Provides current weather conditions and forecast data.
"""
import requests
import json
import os
from typing import Dict, Any, Optional, Generator

# Brentwood, England coordinates
LOCATION = {
    "name": "Brentwood, England",
    "latitude": 51.6185,
    "longitude": 0.2989
}

# LLM configuration for weather summary streaming  
# Use the same board IP configuration as the main LLM
LOCAL_BOARD_IP = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")
LOCAL_BOARD_PORT = os.getenv("LOCAL_BOARD_PORT", "8080")
LLM_SERVER_URL = f"http://{LOCAL_BOARD_IP}:{LOCAL_BOARD_PORT}/v1/chat/completions"
MODEL = "Qwen2.5-3B-Instruct"

# System prompt for weather summarization with personality and examples
WEATHER_SUMMARY_PROMPT = """You are Butler, a British AI assistant helping with weather information. 
Provide natural, conversational responses that directly answer the user's question using the weather data.

IMPORTANT: Pay attention to what the user asked and answer THAT specific question:
- If they asked about clothing (jacket, shorts, coat), give advice based on temperature
- If they asked about an umbrella, mention rain/conditions
- If they asked a general weather question, give a summary
- Keep responses brief and conversational

Examples of good responses:

User context: Do I need a jacket today?
Weather data: {"condition": "overcast", "temperature": 7, "wind_speed": 12, "humidity": 85}
Response: "It's around 7 degrees with overcast skies, so I'd definitely recommend a jacket."

User context: Can I wear shorts?
Weather data: {"condition": "clear sky", "temperature": 22, "wind_speed": 5, "humidity": 45}
Response: "Absolutely! It's 22 degrees with clear skies - perfect shorts weather."

User context: Should I bring an umbrella?
Weather data: {"condition": "heavy rain", "temperature": 12, "wind_speed": 15, "humidity": 85}
Response: "Yes, definitely bring an umbrella. It's rather wet out there with heavy rain."

User context: What's the weather like?
Weather data: {"condition": "overcast", "temperature": 8, "wind_speed": 20, "humidity": 70, "feels_like": 3}
Response: "Rather grim out there - overcast and chilly at 8 degrees, though it feels more like 3 with the wind. Best wrap up warm."

User context: Is it nice out?
Weather data: {"condition": "partly cloudy", "temperature": 15, "wind_speed": 10, "humidity": 60}
Response: "Fairly pleasant - partly cloudy and 15 degrees. Not bad at all for a walk."

Keep responses concise, conversational, and helpful. Use British terms and phrasing. Don't repeat the location name unless asked specifically about it."""


def get_current_weather() -> Dict[str, Any]:
    """
    Fetch current weather for Brentwood, England from Open-Meteo API.
    
    Returns:
        dict with weather data including temperature, conditions, precipitation, etc.
    """
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LOCATION["latitude"],
            "longitude": LOCATION["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,rain,showers,snowfall,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m",
            "timezone": "Europe/London",
            "temperature_unit": "celsius",
            "wind_speed_unit": "mph",
            "precipitation_unit": "mm"
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        current = data.get("current", {})
        
        # Map weather codes to descriptions
        weather_code = current.get("weather_code", 0)
        condition = get_weather_condition(weather_code)
        
        return {
            "ok": True,
            "location": LOCATION["name"],
            "temperature": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation", 0),
            "rain": current.get("rain", 0),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_direction": current.get("wind_direction_10m"),
            "cloud_cover": current.get("cloud_cover"),
            "condition": condition,
            "weather_code": weather_code
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "location": LOCATION["name"]
        }


def get_weather_condition(code: int) -> str:
    """
    Convert WMO weather code to human-readable condition.
    
    WMO Weather interpretation codes (WW):
    0 - Clear sky
    1, 2, 3 - Mainly clear, partly cloudy, and overcast
    45, 48 - Fog
    51, 53, 55 - Drizzle
    61, 63, 65 - Rain
    71, 73, 75 - Snow
    80, 81, 82 - Rain showers
    95 - Thunderstorm
    96, 99 - Thunderstorm with hail
    """
    conditions = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "foggy",
        48: "foggy with rime",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        56: "light freezing drizzle",
        57: "dense freezing drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        66: "light freezing rain",
        67: "heavy freezing rain",
        71: "slight snow",
        73: "moderate snow",
        75: "heavy snow",
        77: "snow grains",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        85: "slight snow showers",
        86: "heavy snow showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail"
    }
    return conditions.get(code, "unknown")


def format_weather_for_llm(weather_data: Dict[str, Any]) -> str:
    """
    Format weather data into a natural, TTS-friendly description for the LLM to use.
    Avoids symbols and formats numbers for natural speech.
    
    Returns:
        Human-readable weather summary optimized for text-to-speech
    """
    if not weather_data.get("ok"):
        return f"Unable to fetch weather data: {weather_data.get('error', 'Unknown error')}."
    
    temp = weather_data.get("temperature")
    feels_like = weather_data.get("feels_like")
    condition = weather_data.get("condition", "unknown")
    rain = weather_data.get("rain", 0)
    wind = weather_data.get("wind_speed")
    humidity = weather_data.get("humidity")
    
    # Round temperatures for cleaner TTS
    temp_rounded = round(temp) if temp is not None else None
    feels_rounded = round(feels_like) if feels_like is not None else None
    
    # Build summary with proper sentence structure
    parts = []
    parts.append(f"In {weather_data['location']} it is {condition}")
    
    if temp_rounded is not None:
        parts.append(f"{temp_rounded} degrees")
    
    if feels_rounded and temp_rounded and abs(temp_rounded - feels_rounded) > 2:
        parts.append(f"feels like {feels_rounded}")
    
    if wind:
        wind_rounded = round(wind)
        parts.append(f"wind speed {wind_rounded} miles per hour")
    
    if rain > 0:
        rain_rounded = round(rain, 1)
        parts.append(f"{rain_rounded} millimeters of rain")
    
    if humidity:
        humidity_rounded = round(humidity)
        parts.append(f"humidity {humidity_rounded} percent")
    
    # Join with commas and end with period for proper sentence extraction
    summary = ", ".join(parts) + "."
    return summary


def stream_weather_summary(weather_data: Dict[str, Any], user_context: str = "") -> Generator[str, None, None]:
    """
    Stream a natural language weather summary using a dedicated LLM call.
    
    Args:
        weather_data: Weather data dict from get_current_weather()
        user_context: Optional user message for context
    
    Yields:
        Token deltas from the LLM streaming response
    """
    if not weather_data.get("ok"):
        # For errors, just yield the error message directly
        error_msg = f"I'm sorry, I couldn't fetch the weather information: {weather_data.get('error', 'Unknown error')}."
        for char in error_msg:
            yield char
        return
    
    # Prepare simplified weather data for the prompt
    weather_summary = {
        "condition": weather_data.get("condition"),
        "temperature": round(weather_data.get("temperature", 0)),
        "wind_speed": round(weather_data.get("wind_speed", 0)),
        "humidity": round(weather_data.get("humidity", 0))
    }
    
    # Include feels_like if significantly different
    temp = weather_data.get("temperature")
    feels = weather_data.get("feels_like")
    if temp is not None and feels is not None and abs(temp - feels) > 2:
        weather_summary["feels_like"] = round(feels)
    
    # Include rain if present
    rain = weather_data.get("rain", 0)
    if rain > 0:
        weather_summary["rain"] = round(rain, 1)
    
    # Build the user message for the LLM
    user_msg = f"User context: {user_context or 'Current weather request'}\nWeather data: {json.dumps(weather_summary)}"
    
    messages = [
        {"role": "system", "content": WEATHER_SUMMARY_PROMPT},
        {"role": "user", "content": user_msg}
    ]
    
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,  # Slightly higher for more natural variety
        "max_tokens": 150
    }
    
    try:
        response = requests.post(LLM_SERVER_URL, json=payload, timeout=(5, 60), stream=True)
        response.raise_for_status()
        
        for line in response.iter_lines(decode_unicode=True):
            if line is None:
                continue
            s = line.strip()
            if not s:
                continue
            
            # Handle standard SSE format
            if s.startswith('data:'):
                s = s[len('data:'):].strip()
            
            if s == '[DONE]':
                break
            
            try:
                obj = json.loads(s)
            except Exception:
                continue
            
            # Extract delta content
            try:
                delta = obj.get('choices', [])[0].get('delta', {}).get('content', '')
            except Exception:
                delta = ''
            
            if delta:
                yield delta
                
    except Exception as e:
        # On error, yield a fallback message
        fallback = f"The weather in {weather_data.get('location', 'your area')} is {weather_data.get('condition', 'unknown')} with temperatures around {round(weather_data.get('temperature', 0))} degrees."
        for char in fallback:
            yield char
