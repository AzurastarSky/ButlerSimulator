import os
import json
import requests
from typing import List

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

try:
    from . import llm_toolcall_test as local_llm
except Exception:
    try:
        import llm_toolcall_test as local_llm
    except Exception:
        local_llm = None

try:
    from . import llm_toolcall_test as llm_helper
except Exception:
    try:
        import llm_toolcall_test as llm_helper
    except Exception:
        llm_helper = None

try:
    from . import state
except Exception:
    import state

try:
    from . import weather
except Exception:
    import weather

try:
    from .helpers import norm_room, norm_device, clamp
except ImportError:
    from helpers import norm_room, norm_device, clamp


def post_chat_openai(history: List[dict]) -> dict:
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    sys_prompt = None
    if llm_helper and hasattr(llm_helper, 'SYSTEM_PROMPT'):
        sys_prompt = llm_helper.SYSTEM_PROMPT
    else:
        sys_prompt = "You are Butler. Output JSON only as specified by the assistant's device-control schema."

    messages = [{"role": "system", "content": sys_prompt}]
    try:
        max_hist = llm_helper.MAX_HISTORY if llm_helper and hasattr(llm_helper, 'MAX_HISTORY') else 8
        if max_hist and isinstance(max_hist, int):
            messages += history[-max_hist:]
        else:
            messages += history
    except Exception:
        messages += history

    payload = {"model": "gpt-5.2", "messages": messages, "temperature": 0}
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    resp = requests.post(OPENAI_URL, json=payload, headers=headers, timeout=(5, 60))
    resp.raise_for_status()
    return resp.json()


def apply_toolcall(js: dict, target: str = 'local', last_user_text: str = None) -> dict:
    if not isinstance(js, dict):
        return {"ok": False, "error": "invalid payload"}

    try:
        print(f"[apply_toolcall] target={target} js={json.dumps(js, ensure_ascii=False)}")
    except Exception:
        print(f"[apply_toolcall] target={target} js={str(js)}")

    tool = js.get("tool")
    if tool not in {"manage_device", "query_state", "get_weather"}:
        return {"ok": False, "error": "unsupported tool"}

    # Handle weather tool
    if tool == "get_weather":
        weather_data = weather.get_current_weather()
        weather_summary = weather.format_weather_for_llm(weather_data)
        return {
            "ok": weather_data.get("ok", True),
            "tool": "get_weather",
            "data": weather_data,
            "summary": weather_summary
        }

    raw_room = js.get("room", "") or ""
    rooms_field = js.get("rooms", None)
    
    # Normalize room input
    if isinstance(raw_room, list) and not rooms_field:
        rooms_field = raw_room
        room = ""
    else:
        room = norm_room(raw_room) if not isinstance(raw_room, list) else ""
    
    device = norm_device(js.get("device", ""))
    action = (js.get("action", "") or "").strip().lower()
    value = js.get("value", None)

    rooms_store = state.STATE_LOCAL if target == 'local' else state.STATE_CLOUD
    house_store = state.HOUSE_LOCAL if target == 'local' else state.HOUSE_CLOUD

    if tool == 'query_state':
        if device == 'thermostat':
            return {'ok': True, 'query': {'device': 'thermostat', 'target_store': target}, 'result': {'thermostat': dict(house_store)}}

        if isinstance(rooms_field, list) and len(rooms_field) > 0:
            normed = [norm_room(str(r)) for r in rooms_field]
            if any(r == 'all' for r in normed):
                targets = list(rooms_store.keys())
            else:
                targets = normed
        else:
            if room in { 'all', 'upstairs', 'downstairs' }:
                targets = list(rooms_store.keys()) if room == 'all' else ([r for r in rooms_store if (r in state.UPSTAIRS if room == 'upstairs' else r in state.DOWNSTAIRS)])
            else:
                targets = [room]

        result = {'ok': True, 'query': {'targets': targets, 'device': device, 'target_store': target}}
        data = {}
        for r in targets:
            if not r or r not in rooms_store:
                data[r] = None
                continue
            if device:
                data[r] = rooms_store[r].get(device)
            else:
                data[r] = dict(rooms_store[r])
        result['result'] = data
        return result

    if device == "thermostat":
        if action not in state.VALID_THERMO_ACTIONS:
            return {"ok": False, "error": f"Unknown thermostat action: {action}"}

        if action in {"increase", "decrease"} and (value is None):
            # Only infer step for local target, not cloud
            if target == 'local' and local_llm and hasattr(local_llm, 'infer_thermo_step') and last_user_text:
                inferred = local_llm.infer_thermo_step(last_user_text)
                if inferred is not None:
                    value = inferred
                    js['value'] = inferred
                    js['_inferred_value'] = True

        step = 1.0
        if value is not None:
            try:
                step = float(value)
            except (TypeError, ValueError):
                pass
        
        prev_target = float(house_store.get("target", 20.0))
        if action == "increase":
            house_store["target"] = clamp(prev_target + step, 10.0, 28.0)
        elif action == "decrease":
            house_store["target"] = clamp(prev_target - step, 10.0, 28.0)
        elif action == "set_value":
            try:
                num = float(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": "set_value requires numeric 'value'"}
            house_store["target"] = clamp(num, 10.0, 28.0)
        elif action == "turn_on":
            house_store["mode"] = "heat"
        elif action == "turn_off":
            house_store["mode"] = "off"

        if target == 'local':
            state._update_house_temp_local()
        elif target == 'cloud':
            state._update_house_temp_cloud()
        else:
            state._update_house_temp()

        state.publish_state_event()

        # Return the numeric step/value used and previous/new targets for clarity
        used_value = float(value) if value is not None else step
        return {"ok": True, "device": "thermostat", "action": action, "used_value": used_value, "prev_target": prev_target, "new_target": float(house_store.get("target", prev_target)), "house": house_store}

    if device not in {"light"}:
        return {"ok": False, "error": f"Unsupported device for target: {device}"}

    if isinstance(rooms_field, list) and len(rooms_field) > 0:
        normed = [norm_room(str(r)) for r in rooms_field]
        if any(r == 'all' for r in normed):
            targets = list(rooms_store.keys())
        else:
            targets = normed
    else:
        if isinstance(raw_room, str) and ("," in raw_room or " and " in raw_room):
            parts = [norm_room(p) for p in __import__('re').split(r",| and ", raw_room) if p.strip()]
            targets = parts
        else:
            if room in {"all", "upstairs", "downstairs"}:
                targets = list(rooms_store.keys()) if room == "all" else [r for r in rooms_store if (r in state.UPSTAIRS if room == "upstairs" else r in state.DOWNSTAIRS)]
            else:
                targets = [room]

    applied, skipped = [], []
    for r in targets:
        if not r or r not in rooms_store or device not in rooms_store[r]:
            skipped.append(r)
            continue
        new_state = "on" if action == "turn_on" else "off"
        rooms_store[r][device] = new_state
        applied.append({"room": r, "new_state": new_state})

    if applied:
        print(f"[apply_toolcall] applied={json.dumps(applied, ensure_ascii=False)} target={target}")
    if skipped:
        print(f"[apply_toolcall] skipped={json.dumps(skipped, ensure_ascii=False)} target={target}")

    if applied:
        state.publish_state_event()

    if len(applied) > 1 or (len(skipped) > 0 and len(applied) > 0):
        return {"ok": True, "bulk": True, "scope": rooms_field or room, "device": device, "action": action, "applied": applied, "skipped": skipped}

    if room not in rooms_store or device not in rooms_store[room]:
        return {"ok": False, "error": f"Unsupported room/device. Known rooms: {list(rooms_store.keys())}"}

    new_state = "on" if action == "turn_on" else "off"
    rooms_store[room][device] = new_state
    state.publish_state_event()
    return {"ok": True, "room": room, "device": device, "action": action, "new_state": new_state}
