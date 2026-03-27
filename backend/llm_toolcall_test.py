from typing import Any, Dict, List, Optional, Tuple
import json, re, requests

# Try to import shared helpers (room/device synonyms + normalizers)
try:
    from .helpers import norm_room, norm_device, ROOM_SYNONYMS, DEVICE_SYNONYMS, VALID_ROOMS, VALID_DEVICES
except Exception:
    try:
        from helpers import norm_room, norm_device, ROOM_SYNONYMS, DEVICE_SYNONYMS, VALID_ROOMS, VALID_DEVICES
    except Exception:
        # Fallbacks if helpers not importable (very defensive)
        ROOM_SYNONYMS = {}
        DEVICE_SYNONYMS = {}
        def norm_room(v: str) -> str:
            return (v or "").strip().lower()
        def norm_device(v: str) -> str:
            return (v or "").strip().lower()
        VALID_ROOMS = set()
        VALID_DEVICES = set()

# ---------------- Config ----------------
# LLM_SERVER_URL = "http://192.168.0.222:8080/v1/chat/completions"
LLM_SERVER_URL = "http://192.168.1.245:8080/v1/chat/completions"
MODEL = "Qwen2.5-3B-Instruct"

API_DEVICE = "http://127.0.0.1:5000/api/device"
API_STATE = "http://127.0.0.1:5000/api/state"

DEBUG = False
MAX_HISTORY = 8
TIMEOUT = (10, 60)


SYSTEM_PROMPT = """
You are Butler. Only use JSON tool outputs for the following two supported tools: `manage_device` and `query_state`.

When controlling devices, produce JSON exactly in this shape:
{"tool":"manage_device","room":"<living room|dining room|kitchen|bathroom|bedroom|office|all|upstairs|downstairs>","device":"<light|thermostat>","action":"<turn_on|turn_off|toggle|increase|decrease|set_value>","value":"<number optional>"}

If the user refers to multiple rooms, emit a `rooms` array instead of a single `room` string. Example:
{"tool":"manage_device","rooms":["dining room","kitchen"],"device":"light","action":"turn_on"}

IMPORTANT: For device `light`, only use actions `turn_on`, `turn_off`, or `toggle`. Do NOT use `increase` or `decrease` for lights — those are for the thermostat only.

When querying state, produce JSON exactly in this shape:
{"tool":"query_state","room":"<house|living room|dining room|kitchen|bathroom|bedroom|office|all|upstairs|downstairs>","device":"<light|thermostat|all optional>"}

If the user's request is NOT about device control or state (for example: storytelling, general chat, summaries, creative writing, or other conversational replies), DO NOT emit JSON or tool invocations. Instead, respond in plain natural language (no JSON) with a helpful assistant reply.

If there is ambiguity about whether the user intends a device action, prefer a short clarifying natural-language question rather than emitting a tool call.

Temperature intent rules:
- cold/chilly/freezing -> thermostat increase; hot/warm/boiling/roasting -> decrease
- numeric phrasing ('increase/decrease by X' or 'set to X') must include value=X
- intensity (no number): a bit/slightly=1; quite/pretty/fairly/somewhat=2; very/really/too=3; extremely/way too=4

Examples:
User: I am a little cold
{"tool":"manage_device","room":"all","device":"thermostat","action":"increase"}
User: tell me a 100 word story
Respond with a plain natural-language story. Do NOT include the words "Plain text", "not JSON", or any formatting hints in your reply. Never echo prompt examples or formatting instructions verbatim.
"""

# VALID_ROOMS and VALID_DEVICES are imported from helpers

VALID_ACTIONS = {
    "light": {"turn_on", "turn_off", "toggle"},
    # blinds removed from setup — only thermostat and light supported
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

HOT_WORDS = {
    "hot",
    "warm",
    "boiling",
    "roasting",
    "sweltering",
    "stuffy",
    "sweaty",
    "too hot",
    "very hot",
    "way too hot",
}

COLD_WORDS = {
    "cold",
    "chilly",
    "freezing",
    "nippy",
    "drafty",
    "too cold",
    "very cold",
    "way too cold",
}


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
        room = (raw_room or "").strip().lower()
        rooms_list = js.get("rooms", None)

        # normalize comma/and separated room strings into a list for validation if provided
        if isinstance(room, str) and ("," in room or " and " in room):
            parts = [r.strip().lower() for r in re.split(r",| and ", room) if r.strip()]
        else:
            parts = [room] if room else []

        if isinstance(rooms_list, list):
            list_to_check = [str(r).strip().lower() for r in rooms_list]
        else:
            list_to_check = parts
        device = (js.get("device", "") or "").strip().lower()
        action = (js.get("action", "") or "").strip().lower()

        # Ensure all specified rooms are valid
        invalid = [r for r in list_to_check if r and r not in (VALID_ROOMS - {"house"})]
        if invalid:
            return False, f"invalid room(s): {invalid}"

        if device not in VALID_DEVICES:
            return False, f"invalid device: {device}"

        if action not in VALID_ACTIONS.get(device, set()):
            return False, f"invalid action for {device}: {action}"

        if device == "thermostat" and action == "set_value":
            try:
                float(js.get("value", ""))
            except Exception:
                return False, "thermostat set_value requires numeric 'value'"

        return True, None

    if tool == "query_state":
        room = (js.get("room", "") or "").strip().lower()
        device = (js.get("device", "all") or "all").strip().lower()

        if room not in VALID_ROOMS:
            return False, f"invalid room for query: {room}"

        if device != "all" and device not in VALID_DEVICES:
            return False, f"invalid device for query: {device}"

        return True, None

    return False, f"unknown tool: {tool}"


def infer_thermo_step(user_text: str) -> Optional[float]:
    if not user_text:
        return None

    text = (user_text or "").strip()

    # explicit numeric 'by N' first
    match = BY_NUMBER.search(text)
    if match:
        try:
            return float(match.group(1))
        except Exception:
            pass

    # regex patterns (ordered)
    for pattern, step in INTENSITY_STEPS:
        try:
            if pattern.search(text):
                return step
        except Exception:
            continue

    # Fallback substring checks for common adjectives (catch simple cases)
    low = text.lower()
    if any(w in low for w in ("way too", "extremely", "super")):
        return 4.0
    if any(w in low for w in ("very", "too", "really")):
        return 3.0
    if any(w in low for w in ("quite", "pretty", "fairly", "somewhat")):
        return 2.0
    if any(w in low for w in ("a little", "a bit", "slightly", "bit", "little")):
        return 1.0

    return None


def infer_thermo_target(user_text: str) -> Optional[float]:
    if not user_text:
        return None

    match = TO_NUMBER.search(user_text)
    if not match:
        return None

    try:
        return float(match.group(1))
    except Exception:
        return None


def infer_comfort_direction(user_text: str) -> Optional[str]:
    text = (user_text or "").lower()

    if any(word in text for word in HOT_WORDS):
        return "decrease"

    if any(word in text for word in COLD_WORDS):
        return "increase"

    return None


BRIGHT_WORDS = {"bright", "too bright", "a bit bright", "very bright", "so bright"}
DIM_WORDS = {"dim", "a bit dim", "a little dim", "too dark", "dark", "a bit dark"}


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

    # If model suggested an action that isn't valid for the device, attempt correction
    valid_for_device = VALID_ACTIONS.get(device, set())

    # Map brightness hints to actions
    if device == "light":
        if any(w in user_text for w in DIM_WORDS) and "turn_on" not in valid_for_device:
            # If the user says it's dim, prefer turning on the light (or increase)
            action = "turn_on"

        if any(w in user_text for w in DIM_WORDS) and "turn_on" in valid_for_device:
            action = "turn_on"

        if any(w in user_text for w in BRIGHT_WORDS):
            # No blinds in this setup — prefer turning lights off when user mentions brightness
            action = "turn_off"

    # Translate generic increase/decrease intents into on/off or open/close
    if action in {"increase", "decrease"}:
        if device == "light":
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
        print("Butler:", reply)
        return content, reply

    if tool == "query_state":
        room = parsed.get("room", "")
        device = parsed.get("device", None)
        print("TOOL CALL:", json.dumps(parsed, ensure_ascii=False))
        result = execute_query_state(room, device)
        reply = friendly_query_reply(result)
        print("Butler:", reply)
        return content, reply

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
            assistant_json, _ = handle(data, user)
            if assistant_json:
                history.append({"role": "assistant", "content": assistant_json})
        except requests.exceptions.RequestException as e:
            print("[HTTP error contacting LLM server]")
            print(e)
        except Exception as e:
            print("[Unexpected error]")
            print(e)


if __name__ == "__main__":
    main()

