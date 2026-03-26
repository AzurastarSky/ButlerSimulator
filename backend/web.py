# backend/web.py
from flask import Flask, send_from_directory, request, jsonify
from pathlib import Path
import time

app = Flask(__name__, static_folder="../frontend", static_url_path="")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# ---------------- In-memory State ----------------
# Rooms: lights + blinds
STATE = {
    # Downstairs
    "living room": {"light": "off", "blinds": "closed"},
    "dining room": {"light": "off", "blinds": "closed"},
    "kitchen":     {"light": "off", "blinds": "closed"},
    # Upstairs
    "bathroom": {"light": "off", "blinds": "closed"},
    "bedroom":  {"light": "off", "blinds": "closed"},
    "office":   {"light": "off", "blinds": "closed"},
}

# House thermostat (single, house-level)
HOUSE = {
    "target": 20.0,   # °C
    "current": 19.0,  # °C
    "mode": "heat",   # "heat" | "off"
}
AMBIENT = 18.0               # °C when heating is off, drift target
HEAT_RATE_C_PER_SEC = 0.02   # how fast current approaches target when heating
COOL_RATE_C_PER_SEC = 0.01   # how fast current moves toward ambient when off
LAST_UPDATE = time.time()

VALID_LIGHT_ACTIONS  = {"turn_on", "turn_off", "toggle"}
VALID_BLINDS_ACTIONS = {"open", "close", "toggle"}
VALID_THERMO_ACTIONS = {"increase", "decrease", "set_value", "turn_on", "turn_off"}

DOWNSTAIRS = {"living room", "dining room", "kitchen"}
UPSTAIRS   = {"bathroom", "bedroom", "office"}

ROOM_SYNONYMS = {
    "lounge": "living room", "livingroom": "living room", "lr": "living room",
    "diner": "dining room", "kit": "kitchen",
    # scopes
    "whole house": "all", "entire house": "all", "all rooms": "all", "everywhere": "all",
    "down stairs": "downstairs", "ground floor": "downstairs",
    "first floor": "upstairs", "upper floor": "upstairs",
    "house": "all",  # allow "house" to mean whole house
}
DEVICE_SYNONYMS = {
    "lamp": "light", "lights": "light", "ceiling light": "light",
    "blind": "blinds", "shade": "blinds", "shades": "blinds",
}

def norm_room(name: str) -> str:
    n = (name or "").strip().lower()
    return ROOM_SYNONYMS.get(n, n)

def norm_device(name: str) -> str:
    n = (name or "").strip().lower()
    return DEVICE_SYNONYMS.get(n, n)

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _update_house_temp():
    global LAST_UPDATE
    now = time.time()
    dt = now - LAST_UPDATE
    if dt <= 0:
        return
    cur = HOUSE["current"]
    tgt = HOUSE["target"]
    mode = HOUSE["mode"]

    if mode == "heat":
        # Approach target
        if abs(cur - tgt) > 1e-3:
            step = HEAT_RATE_C_PER_SEC * dt
            if cur < tgt:
                cur = min(tgt, cur + step)
            else:
                cur = max(tgt, cur - step)
    else:
        # Drift toward ambient when off
        if abs(cur - AMBIENT) > 1e-3:
            step = COOL_RATE_C_PER_SEC * dt
            if cur < AMBIENT:
                cur = min(AMBIENT, cur + step)
            else:
                cur = max(AMBIENT, cur - step)

    HOUSE["current"] = round(cur, 2)
    LAST_UPDATE = now

# ---------------- Static routes ----------------
@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.get("/styles.css")
def styles_css():
    return send_from_directory(FRONTEND_DIR, "styles.css")

@app.get("/app.js")
def app_js():
    return send_from_directory(FRONTEND_DIR, "app.js")

# ---------------- API ----------------
@app.get("/api/state")
def get_state():
    _update_house_temp()
    # Return structure with house + rooms
    return jsonify({"house": HOUSE, "rooms": STATE})

@app.post("/api/device")
def device():
    data = request.get_json(force=True) or {}
    room = norm_room(data.get("room", ""))
    device = norm_device(data.get("device", ""))
    action = (data.get("action", "") or "").strip().lower()
    value  = data.get("value", None)

    # ------- Thermostat (house-level) -------
    if device == "thermostat":
        if action not in VALID_THERMO_ACTIONS:
            return jsonify({"ok": False, "error": f"Unknown thermostat action: {action}"}), 400

        step = 1.0
        if value is not None:
            try:
                step = float(value)
            except:
                pass

        if action == "increase":
            HOUSE["target"] = clamp(HOUSE["target"] + step, 10.0, 28.0)
        elif action == "decrease":
            HOUSE["target"] = clamp(HOUSE["target"] - step, 10.0, 28.0)
        elif action == "set_value":
            try:
                num = float(value)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "set_value requires numeric 'value'"}), 400
            HOUSE["target"] = clamp(num, 10.0, 28.0)
        elif action == "turn_on":
            HOUSE["mode"] = "heat"
        elif action == "turn_off":
            HOUSE["mode"] = "off"

        _update_house_temp()
        return jsonify({"ok": True, "device": "thermostat", "action": action, "house": HOUSE})

    # ------- Lights / Blinds (room or scoped) -------
    if device not in {"light", "blinds"}:
        return jsonify({"ok": False, "error": f"Unsupported device: {device}"}), 400

    valid_actions = VALID_LIGHT_ACTIONS if device == "light" else VALID_BLINDS_ACTIONS
    if action not in valid_actions:
        return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400

    # Scope operations
    if room in {"all", "upstairs", "downstairs"}:
        targets = list(STATE.keys()) if room == "all" else \
                  [r for r in STATE if (r in UPSTAIRS if room == "upstairs" else r in DOWNSTAIRS)]
        applied, skipped = [], []
        for r in targets:
            if device not in STATE[r]:
                skipped.append(r)
                continue
            cur = STATE[r][device]
            if device == "light":
                new_state = ("on" if action == "turn_on"
                             else "off" if action == "turn_off"
                             else ("off" if cur == "on" else "on"))
            else:  # blinds
                new_state = ("open" if action == "open"
                             else "closed" if action == "close"
                             else ("closed" if cur == "open" else "open"))
            STATE[r][device] = new_state
            applied.append({"room": r, "new_state": new_state})

        return jsonify({
            "ok": True, "bulk": True, "scope": room,
            "device": device, "action": action,
            "applied": applied, "skipped": skipped
        })

    # Single room
    if room not in STATE or device not in STATE[room]:
        return jsonify({"ok": False, "error": f"Unsupported room/device. Known rooms: {list(STATE.keys())}"}), 400

    cur = STATE[room][device]
    if device == "light":
        new_state = ("on" if action == "turn_on"
                     else "off" if action == "turn_off"
                     else ("off" if cur == "on" else "on"))
    else:  # blinds
        new_state = ("open" if action == "open"
                     else "closed" if action == "close"
                     else ("closed" if cur == "open" else "open"))

    STATE[room][device] = new_state
    return jsonify({"ok": True, "room": room, "device": device, "action": action, "new_state": new_state})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)