from typing import Any, Dict, List, Optional, Tuple
import json, re, requests, os

# Import shared helpers (room/device synonyms + normalizers)
try:
    from .helpers import norm_room, norm_device, VALID_ROOMS, VALID_DEVICES
except ImportError:
    from helpers import norm_room, norm_device, VALID_ROOMS, VALID_DEVICES

# Import weather module
try:
    from . import weather
except ImportError:
    import weather

# ---------------- Config ----------------
LOCAL_BOARD_IP = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")
LOCAL_BOARD_PORT = os.getenv("LOCAL_BOARD_PORT", "8080")
LLM_SERVER_URL = f"http://{LOCAL_BOARD_IP}:{LOCAL_BOARD_PORT}/v1/chat/completions"
MODEL = "Qwen2.5-3B-Instruct"

API_DEVICE = "http://127.0.0.1:5000/api/device"
API_STATE = "http://127.0.0.1:5000/api/state"

DEBUG = False
MAX_HISTORY = 8
TIMEOUT = (10, 60)


def get_system_prompt(filler_mode='auto'):
    """
    Generate system prompt based on filler mode.
    filler_mode: 'on' (required), 'off' (none), 'auto' (optional)
    """
    
    filler_instruction = ""
    light_on_filler = ""
    light_off_filler = ""
    thermo_increase_filler = ""
    thermo_decrease_filler = ""
    weather_filler = ""
    
    if filler_mode == 'on':
        filler_instruction = '\n\nFILLER (required): Always include a brief contextually appropriate phrase in "filler" field. Examples: lights="Turning that on/off", thermostat increase="Warming that up", thermostat decrease="Cooling it down", weather="Let me check"'
        light_on_filler = ',"filler":"One moment"'
        light_off_filler = ',"filler":"Turning that off"'
        thermo_increase_filler = ',"filler":"Warming that up"'
        thermo_decrease_filler = ',"filler":"Cooling it down"'
        weather_filler = ',"filler":"Let me check"'
    elif filler_mode == 'auto':
        filler_instruction = '\n\nFILLER (optional): You may include a brief contextually appropriate phrase in "filler" field. Examples: lights="Turning that on/off", thermostat increase="Warming that up", thermostat decrease="Cooling it down", weather="Let me check"'
        light_on_filler = ',"filler":"One moment"'
        light_off_filler = ',"filler":"Turning that off"'
        thermo_increase_filler = ',"filler":"Warming that up"'
        thermo_decrease_filler = ',"filler":"Cooling it down"'
        weather_filler = ',"filler":"Let me check"'
    # else 'off' - no filler instruction or examples
    
    return f"""
You are Butler, a helpful British AI assistant.

WHEN TO USE JSON: Only for device control, state queries, or weather questions.
WHEN TO USE PLAIN TEXT: For greetings, casual conversation, questions about yourself, or anything not related to devices/weather.

JSON TOOL FORMAT (use ONLY when controlling devices or checking weather/state):
Device control: {{"tool":"manage_device","room":"all","device":"light","action":"turn_on"{light_on_filler}}}
State query: {{"tool":"query_state","room":"house","device":"thermostat"}}
Weather: {{"tool":"get_weather"{weather_filler}}}{filler_instruction}

ROOMS: living room, dining room, kitchen, bathroom, bedroom, office, all, upstairs, downstairs
DEVICES: light, thermostat
LIGHT ACTIONS: turn_on, turn_off (only these two)
THERMOSTAT ACTIONS: increase, decrease, set_value

THERMOSTAT RULES - SIMPLE:
User says COLD/CHILLY/FREEZING → action: "increase"
User says HOT/WARM/BOILING → action: "decrease"
Increase = make warmer. Decrease = make cooler.

VALUE GUIDE (if not specified):
a bit/little/slightly = 1
quite/pretty/fairly = 2
very/really/too = 3
extremely/way too = 4

WEATHER QUESTIONS include: "What's the weather?", "Do I need a jacket?", "Can I wear shorts?", "Should I bring an umbrella?", "Is it nice out?", "Can I go for a run?", "Good weather for running?"

EXAMPLES:

User: Hello!
Hello! How can I help you today?

User: How are you?
I'm doing well, thank you! How can I assist you?

User: I am a bit cold
{{"tool":"manage_device","room":"all","device":"thermostat","action":"increase","value":"1"{thermo_increase_filler}}}

User: It is really cold
{{"tool":"manage_device","room":"all","device":"thermostat","action":"increase","value":"3"{thermo_increase_filler}}}

User: It's too hot
{{"tool":"manage_device","room":"all","device":"thermostat","action":"decrease","value":"3"{thermo_decrease_filler}}}

User: Turn off the office light
{{"tool":"manage_device","room":"office","device":"light","action":"turn_off"{light_off_filler}}}

User: Turn on the lights
{{"tool":"manage_device","room":"all","device":"light","action":"turn_on"{light_on_filler}}}

User: Do I need a jacket?
{{"tool":"get_weather"{weather_filler}}}

User: Should I go for a run today?
{{"tool":"get_weather"{weather_filler}}}

User: Is it good weather for running?
{{"tool":"get_weather"{weather_filler}}}
"""


# Default system prompt (auto mode)
SYSTEM_PROMPT = get_system_prompt('auto')

# VALID_ROOMS and VALID_DEVICES are imported from helpers

VALID_ACTIONS = {
    "light": {"turn_on", "turn_off"},

    "thermostat": {"increase", "decrease", "set_value", "turn_on", "turn_off"},
}

INTENSITY_STEPS: List[Tuple[re.Pattern[str], float]] = [
    (re.compile(r"\b(way too|extremely|super|so)\b", re.I), 4.0),
    (re.compile(r"\b(very|really|too)\b", re.I), 3.0),
    (re.compile(r"\b(quite|pretty|fairly|somewhat)\b", re.I), 2.0),
    (re.compile(r"\b(a little|a bit|slightly|bit|little)\b", re.I), 1.0),
]

BY_NUMBER = re.compile(r"\bby\s*(\d{1,2})(?:\s*degrees|\s*°)?\b", re.I)
TO_NUMBER = re.compile(r"\bto\s*(\d{1,2})(?:\s*degrees|\s*°)?\b", re.I)

# debug print helper removed (unused) — use logging if needed


# norm_room and norm_device imported from helpers


def post_chat(history: List[Dict[str, str]]) -> Dict[str, Any]:
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history[-MAX_HISTORY:],
        "stream": False,
        "temperature": 0,
        "max_tokens": 96,
    }
    resp = requests.post(LLM_SERVER_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_message_content(resp_json: Dict[str, Any]) -> str:
    try:
        msg = resp_json["choices"][0]["message"]
        content = msg.get("content", "")
        if content is None:
            return ""
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    if "text" in item:
                        parts.append(str(item["text"]))
                    elif "content" in item:
                        parts.append(str(item["content"]))
            return "\n".join(parts).strip()
        return str(content).strip()
    except Exception:
        return ""


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    if start == -1:
        return None

    for end in range(len(text), start, -1):
        chunk = text[start:end].strip()
        if chunk.endswith("}"):
            try:
                return json.loads(chunk)
            except Exception:
                continue

    return None


def validate(js: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if not isinstance(js, dict):
        return False, "not a JSON object"

    tool = js.get("tool", None)

    if tool is None:
        if not isinstance(js.get("reply", ""), str):
            return False, "missing reply"
        return True, None

    if tool == "manage_device":
        # Accept either a single room string, a comma/and-separated room list, or a 'rooms' list
        raw_room = js.get("room", "") or ""
        rooms_list = js.get("rooms", None)

        # Normalize comma/and separated room strings into a list for validation
        if isinstance(raw_room, str) and ("," in raw_room or " and " in raw_room):
            parts = [norm_room(r.strip()) for r in re.split(r",| and ", raw_room) if r.strip()]
        else:
            parts = [norm_room(raw_room)] if raw_room else []

        if isinstance(rooms_list, list):
            list_to_check = [norm_room(str(r)) for r in rooms_list if r]
        else:
            list_to_check = parts

        device = norm_device(js.get("device", "") or "")
        action = (js.get("action", "") or "").strip().lower()

        # Ensure all specified rooms are valid (exclude "house" from device control)
        invalid = [r for r in list_to_check if r and r not in (VALID_ROOMS - {"house"})]
        if invalid:
            return False, f"invalid room(s): {invalid}"

        if device not in VALID_DEVICES:
            return False, f"invalid device: {device}"

        if action not in VALID_ACTIONS.get(device, set()):
            return False, f"invalid action for {device}: {action}"

        if device == "thermostat" and action == "set_value":
            if "value" not in js or js.get("value") is None:
                return False, "thermostat set_value requires numeric 'value'"
            try:
                float(js.get("value"))
            except (TypeError, ValueError):
                return False, "thermostat set_value requires numeric 'value'"

        return True, None

    if tool == "query_state":
        room = norm_room(js.get("room", "") or "")
        device = norm_device(js.get("device", "all") or "all")

        if room not in VALID_ROOMS:
            return False, f"invalid room for query: {js.get('room', '')}"

        if device != "all" and device not in VALID_DEVICES:
            return False, f"invalid device for query: {device}"

        return True, None

    if tool == "get_weather":
        # Weather tool requires no parameters
        return True, None

    return False, f"unknown tool: {tool}"


def infer_thermo_step(user_text: str) -> Optional[float]:
    if not user_text:
        return None

    text = user_text.strip()

    # Explicit numeric 'by N' first
    match = BY_NUMBER.search(text)
    if match:
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            pass

    # Regex patterns (ordered by intensity)
    for pattern, step in INTENSITY_STEPS:
        if pattern.search(text):
            return step

    return None


def infer_thermo_target(user_text: str) -> Optional[float]:
    if not user_text:
        return None

    match = TO_NUMBER.search(user_text)
    if match:
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            pass
    return None


def try_autocorrect(js: Dict[str, Any], user_text: str) -> Dict[str, Any]:
    """Try to autocorrect obvious invalid tool calls based on the user's text.

    This will map brightness-related intents to sensible actions (e.g. "a bit dim" -> turn_on light,
    "a bit bright" -> turn_off light) and translate increase/decrease for lights into
    turn_on/turn_off where appropriate.
    """
    if not isinstance(js, dict):
        return js

    tool = js.get("tool")
    if tool != "manage_device":
        return js

    user_text = (user_text or "").lower()

    device = norm_device(js.get("device", "") or "")
    action = (js.get("action", "") or "").strip().lower()

    # Map brightness hints to actions
    if device == "light":
        if any(w in user_text for w in ["dim", "a bit dim", "a little dim", "too dark", "dark", "a bit dark"]):
            action = "turn_on"
        elif any(w in user_text for w in ["bright", "too bright", "a bit bright", "very bright", "so bright"]):
            action = "turn_off"

    # Translate generic increase/decrease intents into on/off for lights
    if action in {"increase", "decrease"} and device == "light":
        action = "turn_on" if action == "increase" else "turn_off"

    # Apply corrections back to js
    js["device"] = device
    js["action"] = action

    return js


def execute_manage_device(
    room: str,
    device: str,
    action: str,
    value: Optional[float] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "room": norm_room(room),
        "device": norm_device(device),
        "action": (action or "").strip().lower(),
    }

    if value is not None:
        try:
            payload["value"] = float(value)
        except Exception:
            pass

    try:
        resp = requests.post(API_DEVICE, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        print("[Web page updated]")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def execute_query_state(room: str, device: Optional[str]) -> Dict[str, Any]:
    try:
        resp = requests.get(API_STATE, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    house = data.get("house", {})
    rooms = data.get("rooms", {})

    room = norm_room(room)
    device = norm_device(device or "all")

    if room == "house" or (device == "thermostat" and room in {"house", "all"}):
        return {
            "ok": True,
            "query": True,
            "room": "house",
            "device": "thermostat",
            "house": house,
        }

    if room in {"all", "upstairs", "downstairs"}:
        upstairs = {"bathroom", "bedroom", "office"}
        downstairs = {"living room", "dining room", "kitchen"}

        if room == "all":
            targets = list(rooms.keys())
        elif room == "upstairs":
            targets = [r for r in rooms if r in upstairs]
        else:
            targets = [r for r in rooms if r in downstairs]
    else:
        targets = [room] if room in rooms else []

    result_rooms: Dict[str, Dict[str, Any]] = {}

    for target in targets:
        state = rooms.get(target, {})
        if device == "all":
            result_rooms[target] = {
                "light": state.get("light"),
            }
        else:
            result_rooms[target] = {
                device: state.get(device)
            }

    return {
        "ok": True,
        "query": True,
        "scope": room,
        "device": device,
        "rooms": result_rooms,
        "house": house,
    }


def friendly_control_reply(result: Dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"Sorry, I couldn't complete that. {result.get('error', 'Something went wrong.')}"

    if result.get("device") == "thermostat":
        house = result.get("house", {})
        target = house.get("target")
        mode = str(house.get("mode", "")).upper()

        try:
            return f"Okay, thermostat set to {mode} • Target {float(target):.0f}°C."
        except Exception:
            return "Okay, thermostat updated."

    if result.get("bulk"):
        device = result.get("device", "device")
        action = str(result.get("action", "updated")).replace("_", " ")
        rooms = ", ".join(item.get("room", "") for item in result.get("applied", [])) or "no rooms"
        return f"Okay, {action} {device} in {rooms}."

    room = result.get("room", "the room")
    device = result.get("device", "device")
    new_state = result.get("new_state", "updated")
    return f"Okay, the {room} {device} is now {new_state}."


def friendly_query_reply(result: Dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"Sorry, I couldn't read the state. {result.get('error', '')}".strip()

    if result.get("room") == "house" or (
        result.get("device") == "thermostat" and result.get("scope") in {"house", None}
    ):
        house = result.get("house", {})
        return (
            f"Thermostat: mode {str(house.get('mode', '-')).upper()}, "
            f"target {house.get('target', '-')}°C."
        )

    device = result.get("device", "all")
    rooms = result.get("rooms", {})

    if not rooms:
        return "I couldn't find matching rooms."

    lines: List[str] = []

    if device == "all":
        for room_name, devs in rooms.items():
            parts: List[str] = []
            if devs.get("light") is not None:
                parts.append(f"light={devs['light']}")
            lines.append(f"{room_name}: {', '.join(parts) if parts else '(no data)'}")
    else:
        for room_name, devs in rooms.items():
            lines.append(f"{room_name}: {device}={devs.get(device)}")

    return "; ".join(lines)


def handle(resp_json: Dict[str, Any], last_user_text: str) -> Tuple[Optional[str], Optional[str]]:
    content = get_message_content(resp_json)

    if not content:
        print("[No assistant reply]")
        return None, None

    parsed = extract_json(content)

    if not parsed:
        print("[Model reply was not valid JSON]")
        print(content)
        return None, None

    # Allow the backend to autocorrect common mismatches (e.g. "a bit dim" -> turn_on light,
    # or "a bit bright" -> turn_off light) before strict validation.
    parsed = try_autocorrect(parsed, last_user_text)

    ok, err = validate(parsed)

    if not ok:
        print("[Assistant JSON failed validation]")
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        if err:
            print("Reason:", err)
        return content, None

    tool = parsed.get("tool")
    
    # Check for optional filler response and speak it before executing tool
    filler = parsed.get("filler", "").strip()
    if filler:
        print("Butler:", filler)

    if tool == "manage_device":
        room = parsed.get("room", "")
        device = parsed.get("device", "")
        action = parsed.get("action", "")
        value = parsed.get("value", None)

        if device == "thermostat":
            if value is None:
                absolute_target = infer_thermo_target(last_user_text)
                if absolute_target is not None:
                    action = "set_value"
                    value = absolute_target

            if action in {"increase", "decrease"} and value is None:
                inferred = infer_thermo_step(last_user_text)
                if inferred is not None:
                    value = inferred

            # Uncomment if you want the client to auto-correct obvious hot/cold inversions
            # direction_hint = infer_comfort_direction(last_user_text)
            # if direction_hint and action in {"increase", "decrease"} and direction_hint != action:
            #     action = direction_hint

        print("TOOL CALL:", json.dumps(parsed, ensure_ascii=False))
        result = execute_manage_device(room, device, action, value)
        reply = friendly_control_reply(result)
        
        # Combine filler and final reply for TTS
        if filler:
            combined_reply = filler  # Return filler to be spoken first
            print("Butler:", reply)  # Print final result
        else:
            combined_reply = reply
            print("\n")
            print("Butler:", reply)
        
        return content, combined_reply

    if tool == "query_state":
        room = parsed.get("room", "")
        device = parsed.get("device", None)
        print("TOOL CALL:", json.dumps(parsed, ensure_ascii=False))
        result = execute_query_state(room, device)
        reply = friendly_query_reply(result)
        
        # Combine filler and final reply for TTS
        if filler:
            combined_reply = filler  # Return filler to be spoken first
            print("Butler:", reply)  # Print final result
        else:
            combined_reply = reply
            print("\n")
            print("Butler:", reply)
        
        return content, combined_reply

    if tool == "get_weather":
        print("TOOL CALL:", json.dumps(parsed, ensure_ascii=False))
        weather_data = weather.get_current_weather()
        weather_summary = weather.format_weather_for_llm(weather_data)
        print("\n")
        # Don't print raw weather here, let LLM respond naturally
        # Return weather summary as tool result for second LLM call
        return content, weather_summary

    reply = parsed.get("reply", "")
    print("Butler:", reply)
    return content, reply


def main() -> None:
    print("Butler is ready.")
    print("Type /exit to quit, /clear to reset.\n")

    history: List[Dict[str, str]] = []

    while True:
        try:
            user = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nButler: Goodbye.")
            break

        if not user:
            continue

        lower = user.lower()

        if lower in ("/exit", "exit", "quit"):
            print("Butler: Goodbye.")
            break

        if lower == "/clear":
            history = []
            print("Butler: Chat history cleared.")
            continue

        history.append({"role": "user", "content": user})

        try:
            data = post_chat(history)
            assistant_json, tool_result = handle(data, user)
            if assistant_json:
                history.append({"role": "assistant", "content": assistant_json})
            
            # If there's a tool result (weather data), use stream_weather_summary for natural response
            # Other tools already generate friendly replies.
            if tool_result and "{\"tool\":\"get_weather\"}" in assistant_json:
                # Use the existing stream_weather_summary function with the user's question
                weather_data = weather.get_current_weather()
                
                # Collect the streaming response
                natural_response = ""
                for chunk in weather.stream_weather_summary(weather_data, user_context=user):
                    natural_response += chunk
                
                if natural_response:
                    print("Butler:", natural_response)
                    history.append({"role": "assistant", "content": natural_response})
                else:
                    # Fallback to formatted weather
                    print("Butler:", tool_result)
                    history.append({"role": "assistant", "content": tool_result})
            elif tool_result:
                # For other tools (device/state), just add the friendly reply to history
                history.append({"role": "assistant", "content": tool_result})
        except requests.exceptions.RequestException as e:
            print("[HTTP error contacting LLM server]")
            print(e)
        except Exception as e:
            print("[Unexpected error]")
            print(e)


if __name__ == "__main__":
    main()

