# backend/web.py
from flask import Flask, send_from_directory, request, jsonify, Response, stream_with_context
from pathlib import Path
import time
import os
import json
import requests
import concurrent.futures
import copy
import queue
import threading

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
# Duplicate per-model state (local vs cloud) so we can simulate two independent houses
STATE_LOCAL = copy.deepcopy(STATE)
STATE_CLOUD = copy.deepcopy(STATE)
HOUSE_LOCAL = copy.deepcopy(HOUSE)
HOUSE_CLOUD = copy.deepcopy(HOUSE)
AMBIENT = 18.0               # °C when heating is off, drift target
HEAT_RATE_C_PER_SEC = 0.02   # how fast current approaches target when heating
COOL_RATE_C_PER_SEC = 0.01   # how fast current moves toward ambient when off
LAST_UPDATE = time.time()
LAST_UPDATE_LOCAL = time.time()
LAST_UPDATE_CLOUD = time.time()

# Simple pubsub for state change events (SSE)
_state_subscribers = []
_state_sub_lock = threading.Lock()

def _current_state_snapshot():
    return {
        "house": HOUSE,
        "local": {"house": HOUSE_LOCAL, "rooms": STATE_LOCAL},
        "cloud": {"house": HOUSE_CLOUD, "rooms": STATE_CLOUD},
    }

def publish_state_event():
    payload = json.dumps(_current_state_snapshot())
    with _state_sub_lock:
        subs = list(_state_subscribers)
    for q in subs:
        try:
            q.put_nowait(payload)
        except Exception:
            pass


# Background loop to update temperatures and publish periodic state events
_state_publisher_started = False

def _state_publisher_loop(poll_interval: float = 1.0):
    while True:
        try:
            # advance simulated temps
            try:
                _update_house_temp_local()
            except Exception:
                pass
            try:
                _update_house_temp_cloud()
            except Exception:
                pass
            try:
                _update_house_temp()
            except Exception:
                pass
            # publish snapshot so clients see changing temps
            try:
                publish_state_event()
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(poll_interval)

def ensure_state_publisher():
    global _state_publisher_started
    if _state_publisher_started:
        return
    _state_publisher_started = True
    t = threading.Thread(target=_state_publisher_loop, args=(1.0,), daemon=True)
    t.start()


# Start background state publisher immediately
try:
    ensure_state_publisher()
except Exception:
    pass

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

# OpenAI config (optional)
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

# Reuse local CLI helper where convenient
try:
    from . import llm_toolcall_test as local_llm
except Exception:
    try:
        import llm_toolcall_test as local_llm
    except Exception:
        local_llm = None

# Import parsing/helper utilities even if local LLM instance isn't available
try:
    from . import llm_toolcall_test as llm_helper
except Exception:
    try:
        import llm_toolcall_test as llm_helper
    except Exception:
        llm_helper = None

# Optional local STT provider (SenseVoice)
try:
    from .providers import local_sensevoice_stt as stt_provider
except Exception:
    try:
        from providers import local_sensevoice_stt as stt_provider
    except Exception as e:
        stt_provider = None
        print(f"[web] local STT provider not available: {e}")

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


def _update_house_temp_local():
    global LAST_UPDATE_LOCAL
    now = time.time()
    dt = now - LAST_UPDATE_LOCAL
    if dt <= 0:
        return
    cur = HOUSE_LOCAL.get("current", 0.0)
    tgt = HOUSE_LOCAL.get("target", 20.0)
    mode = HOUSE_LOCAL.get("mode", "off")

    if mode == "heat":
        if abs(cur - tgt) > 1e-3:
            step = HEAT_RATE_C_PER_SEC * dt
            if cur < tgt:
                cur = min(tgt, cur + step)
            else:
                cur = max(tgt, cur - step)
    else:
        if abs(cur - AMBIENT) > 1e-3:
            step = COOL_RATE_C_PER_SEC * dt
            if cur < AMBIENT:
                cur = min(AMBIENT, cur + step)
            else:
                cur = max(AMBIENT, cur - step)

    HOUSE_LOCAL["current"] = round(cur, 2)
    LAST_UPDATE_LOCAL = now


def _update_house_temp_cloud():
    global LAST_UPDATE_CLOUD
    now = time.time()
    dt = now - LAST_UPDATE_CLOUD
    if dt <= 0:
        return
    cur = HOUSE_CLOUD.get("current", 0.0)
    tgt = HOUSE_CLOUD.get("target", 20.0)
    mode = HOUSE_CLOUD.get("mode", "off")

    if mode == "heat":
        if abs(cur - tgt) > 1e-3:
            step = HEAT_RATE_C_PER_SEC * dt
            if cur < tgt:
                cur = min(tgt, cur + step)
            else:
                cur = max(tgt, cur - step)
    else:
        if abs(cur - AMBIENT) > 1e-3:
            step = COOL_RATE_C_PER_SEC * dt
            if cur < AMBIENT:
                cur = min(AMBIENT, cur + step)
            else:
                cur = max(AMBIENT, cur - step)

    HOUSE_CLOUD["current"] = round(cur, 2)
    LAST_UPDATE_CLOUD = now

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
    # Update the appropriate house temps when returning state so UI sees drifting temps
    which = (request.args.get('which') or '').lower()
    if which == 'local':
        _update_house_temp_local()
    elif which == 'cloud':
        _update_house_temp_cloud()
    else:
        _update_house_temp()
    # Return structure with house + rooms. Query param 'which' selects local/cloud/default
    which = (request.args.get('which') or '').lower()
    if which == 'local':
        return jsonify({"house": HOUSE_LOCAL, "rooms": STATE_LOCAL})
    if which == 'cloud':
        return jsonify({"house": HOUSE_CLOUD, "rooms": STATE_CLOUD})
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

        # Mirror thermostat change to local/cloud houses
        HOUSE_LOCAL["target"] = HOUSE["target"]
        HOUSE_CLOUD["target"] = HOUSE["target"]
        HOUSE_LOCAL["mode"] = HOUSE["mode"]
        HOUSE_CLOUD["mode"] = HOUSE["mode"]

        _update_house_temp()
        # notify listeners
        try:
            publish_state_event()
        except Exception:
            pass
        return jsonify({"ok": True, "device": "thermostat", "action": action, "house": HOUSE})

    # ------- Lights / Blinds (room or scoped) -------
    if device not in {"light", "blinds"}:
        return jsonify({"ok": False, "error": f"Unsupported device: {device}"}), 400

    # Allow helper autocorrections (maps brightness/intents and increase/decrease for lights)
    try:
        if 'llm_helper' in globals() and llm_helper and hasattr(llm_helper, 'try_autocorrect'):
            try:
                corrected = llm_helper.try_autocorrect(data, None)
                # update local variables from corrected map
                device = norm_device(corrected.get('device', device))
                action = (corrected.get('action', action) or '').strip().lower()
                value = corrected.get('value', value)
            except Exception:
                pass
        else:
            # fallback: map light increase/decrease -> turn_on/turn_off
            if device == 'light' and action in {'increase', 'decrease'}:
                action = 'turn_on' if action == 'increase' else 'turn_off'
    except Exception:
        pass

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
            # Mirror into per-model stores if they have the room/device
            try:
                if device in STATE_LOCAL.get(r, {}):
                    STATE_LOCAL[r][device] = new_state
            except Exception:
                pass
            try:
                if device in STATE_CLOUD.get(r, {}):
                    STATE_CLOUD[r][device] = new_state
            except Exception:
                pass
            # publish state change so UI updates immediately
            try:
                publish_state_event()
            except Exception:
                pass
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
    # Mirror single-room change to local/cloud if present
    try:
        if device in STATE_LOCAL.get(room, {}):
            STATE_LOCAL[room][device] = new_state
    except Exception:
        pass
    try:
        if device in STATE_CLOUD.get(room, {}):
            STATE_CLOUD[room][device] = new_state
    except Exception:
        pass
    return jsonify({"ok": True, "room": room, "device": device, "action": action, "new_state": new_state})


@app.route('/api/stt', methods=['POST'])
def api_stt():
    """Accept an audio file upload (multipart form 'audio') or raw bytes and return transcription."""
    audio_bytes = None
    if 'audio' in request.files:
        f = request.files['audio']
        audio_bytes = f.read()
    else:
        audio_bytes = request.get_data()

    if not audio_bytes:
        return jsonify({'ok': False, 'error': 'no audio provided'}), 400

    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500

    language = request.args.get('lang', 'en')
    # Check the remote HTTP health endpoint. Do NOT attempt to start it here; user will manage it manually.
    try:
        healthy = False
        try:
            healthy = stt_provider.check_sensevoice_health()
        except Exception:
            healthy = False

        if not healthy:
            return jsonify({'ok': False, 'error': 'SenseVoice STT HTTP not reachable; please start the server manually and retry'}), 502

        # Now call transcribe and normalize timing values to integer ms
        res = stt_provider.transcribe_audio(audio_bytes, language=language)
        def _int(v):
            try:
                return int(round(float(v)))
            except Exception:
                return None

        timings = {
            'inference_ms': _int(res.get('inference_ms')),
            'upload_ms': _int(res.get('upload_ms')),
            'convert_ms': _int(res.get('convert_ms')),
            'total_ms': _int(res.get('total_ms')),
        }
        return jsonify({'ok': True, 'transcript': res.get('text'), 'timings': timings, 'raw': res})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/stt/warmup', methods=['POST', 'GET'])
def api_stt_warmup():
    """Warm up SSH connection and optionally run a dummy inference to warm model."""
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500

    ok_conn = False
    ok_model = False
    try:
        ok_conn = stt_provider.warmup_connections()
    except Exception as e:
        print(f"[web] STT warmup connections failed: {e}")

    try:
        ok_model = stt_provider.warmup_model()
    except Exception as e:
        print(f"[web] STT warmup model failed: {e}")

    return jsonify({'ok': True, 'connections_ready': ok_conn, 'model_warmed': ok_model})


@app.route('/api/stt/cloud', methods=['POST'])
def api_stt_cloud():
    """Send audio to OpenAI's transcription endpoint and return the transcript.
    Accepts multipart form 'audio' or raw bytes.
    """
    if not OPENAI_KEY:
        return jsonify({'ok': False, 'error': 'OPENAI_API_KEY not set'}), 500

    audio_bytes = None
    if 'audio' in request.files:
        audio_bytes = request.files['audio'].read()
        filename = request.files['audio'].filename or 'audio.webm'
    else:
        audio_bytes = request.get_data() or b''
        filename = 'audio.webm'

    if not audio_bytes:
        return jsonify({'ok': False, 'error': 'no audio provided'}), 400

    language = request.args.get('lang', 'en')

    # Post to OpenAI Audio Transcriptions
    try:
        # measure upload + inference times
        t_start = time.time()
        files = {
            'file': (filename, audio_bytes, 'application/octet-stream')
        }
        data = {'model': 'whisper-1', 'language': language}
        headers = {'Authorization': f'Bearer {OPENAI_KEY}'}

        t_upload_start = time.time()
        resp = requests.post('https://api.openai.com/v1/audio/transcriptions', headers=headers, files=files, data=data, timeout=60)
        t_upload_end = time.time()

        if resp.status_code != 200:
            return jsonify({'ok': False, 'error': f'OpenAI transcription failed: {resp.status_code} {resp.text[:500]}'}), 500

        t_infer_end = time.time()
        j = resp.json()
        text = j.get('text') or j.get('transcript') or ''

        upload_ms = int((t_upload_end - t_upload_start) * 1000)
        inference_ms = int((t_infer_end - t_upload_end) * 1000)
        total_ms = int((t_infer_end - t_start) * 1000)

        return jsonify({'ok': True, 'transcript': text, 'timings': {'upload_ms': upload_ms, 'inference_ms': inference_ms, 'total_ms': total_ms}, 'raw': j})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/stt/start', methods=['POST'])
def api_stt_start():
    """Start SenseVoice server on remote board. Accepts optional JSON {"force": true} to kill existing instances first."""
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500

    data = request.get_json(silent=True) or {}
    force = bool(data.get('force'))
    # Provider may be HTTP-only and not support server lifecycle management
    if not hasattr(stt_provider, 'start_server'):
        return jsonify({'ok': False, 'error': 'server management not available; start SenseVoice manually on the device (e.g. 192.168.1.245:4500)'}), 501
    res = stt_provider.start_server(force=force)
    return jsonify(res)


@app.route('/api/stt/stop', methods=['POST', 'GET'])
def api_stt_stop():
    """Stop SenseVoice server on remote board (kills all matching processes)."""
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500

    if not hasattr(stt_provider, 'stop_server'):
        return jsonify({'ok': False, 'error': 'server management not available; stop SenseVoice manually on the device'}), 501
    res = stt_provider.stop_server()
    return jsonify(res)


@app.route('/api/stt/status', methods=['GET'])
def api_stt_status():
    """Return whether SenseVoice server appears to be running and any PIDs."""
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500

    # Minimal provider may not support process inspection; fall back to HTTP health check
    proc = {'running': None, 'pids': []}
    try:
        http_ok = stt_provider.check_sensevoice_health()
    except Exception:
        http_ok = False

    out = {'ok': True, 'proc': proc, 'http_ok': bool(http_ok)}
    out.update({'running': bool(http_ok), 'pids': []})
    return jsonify(out)


@app.route('/api/stt/logs', methods=['GET'])
def api_stt_logs():
    """Return tail of remote SenseVoice server log. Query: ?lines=200"""
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500
    try:
        lines = int(request.args.get('lines', '200'))
    except Exception:
        lines = 200

    if not hasattr(stt_provider, 'fetch_remote_log'):
        return jsonify({'ok': False, 'error': 'remote log access not available (provider is HTTP-only)'}), 501

    try:
        res = stt_provider.fetch_remote_log(lines=lines)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    if not res.get('ok'):
        return jsonify({'ok': False, 'error': res.get('error', 'unknown')}), 500
    # Return log as plain text for convenience
    return Response(res.get('log', ''), mimetype='text/plain')


@app.route('/api/stt/netstat', methods=['GET'])
def api_stt_netstat():
    """Return remote listening sockets (ss/netstat) to help debug binding/ports."""
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500
    try:
        res = stt_provider.fetch_listening_sockets()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    if not res.get('ok'):
        return jsonify({'ok': False, 'error': res.get('error', 'unknown')}), 500
    return Response(res.get('out', '') + "\n" + (res.get('err') or ''), mimetype='text/plain')


@app.route('/api/stt/start_probe', methods=['POST'])
def api_stt_start_probe():
    """Start the SenseVoice server briefly on the remote board and return captured startup output."""
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500
    data = request.get_json(silent=True) or {}
    timeout = int(data.get('timeout', 6))
    if not hasattr(stt_provider, 'start_server_probe'):
        return jsonify({'ok': False, 'error': 'start_probe not available (provider is HTTP-only)'}), 501
    try:
        res = stt_provider.start_server_probe(timeout=timeout)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    if not res.get('ok'):
        return jsonify({'ok': False, 'error': res.get('error', 'unknown')}), 500
    return Response(res.get('log', ''), mimetype='text/plain')


def apply_toolcall(js: dict, target: str = 'local', last_user_text: str = None) -> dict:
    """Apply a parsed assistant toolcall (manage_device) to either the local or cloud in-memory state.

    `target` should be 'local' or 'cloud'. Returns a result dict similar to /api/device.
    """
    if not isinstance(js, dict):
        return {"ok": False, "error": "invalid payload"}

    tool = js.get("tool")
    if tool != "manage_device":
        return {"ok": False, "error": "unsupported tool"}

    # Support multiple-room specifications: 'rooms' list or comma/and-separated 'room' string
    raw_room = js.get("room", "") or ""
    rooms_field = js.get("rooms", None)
    # If the model returned a list in 'room' (or returned rooms as list), treat as multi-room
    if isinstance(raw_room, list) and not rooms_field:
        rooms_field = raw_room
        room = ""
    else:
        # safe normalization: only call norm_room for string inputs
        room = norm_room(raw_room) if not isinstance(raw_room, list) else ""
    device = norm_device(js.get("device", ""))
    action = (js.get("action", "") or "").strip().lower()
    value = js.get("value", None)

    # Select target stores
    rooms_store = STATE_LOCAL if target == 'local' else STATE_CLOUD
    house_store = HOUSE_LOCAL if target == 'local' else HOUSE_CLOUD

    # Map blinds to light if needed (frontend no longer has blinds)
    if device == "blinds":
        device = "light"
        if action == "open":
            action = "turn_on"
        elif action == "close":
            action = "turn_off"

    # Thermostat applied to house_store
    if device == "thermostat":
        if action not in VALID_THERMO_ACTIONS:
            return {"ok": False, "error": f"Unknown thermostat action: {action}"}

        # If increase/decrease without explicit value, try to infer from user text
        if action in {"increase", "decrease"} and (value is None):
            try:
                if local_llm and hasattr(local_llm, 'infer_thermo_step') and last_user_text:
                    inferred = local_llm.infer_thermo_step(last_user_text)
                    if inferred is not None:
                        value = inferred
            except Exception:
                pass

        step = 1.0
        if value is not None:
            try:
                step = float(value)
            except:
                pass

        if action == "increase":
            house_store["target"] = clamp(house_store["target"] + step, 10.0, 28.0)
        elif action == "decrease":
            house_store["target"] = clamp(house_store["target"] - step, 10.0, 28.0)
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

        # update current temp slightly for that target house
        if target == 'local':
            _update_house_temp_local()
        elif target == 'cloud':
            _update_house_temp_cloud()
        else:
            _update_house_temp()

        # publish state change so SSE clients see updated target/mode
        try:
            publish_state_event()
        except Exception:
            pass

        return {"ok": True, "device": "thermostat", "action": action, "house": house_store}

    # Lights
    if device not in {"light"}:
        return {"ok": False, "error": f"Unsupported device for target: {device}"}

    # Multi-room: if 'rooms' list provided, apply to those rooms
    if isinstance(rooms_field, list) and len(rooms_field) > 0:
        targets = [norm_room(str(r)) for r in rooms_field]
    else:
        # If room is a comma/and-separated list, split it
        if isinstance(raw_room, str) and ("," in raw_room or " and " in raw_room):
            parts = [norm_room(p) for p in __import__('re').split(r",| and ", raw_room) if p.strip()]
            targets = parts
        else:
            # Keep existing scope handling for 'all', 'upstairs', 'downstairs'
            if room in {"all", "upstairs", "downstairs"}:
                targets = list(rooms_store.keys()) if room == "all" else \
                          [r for r in rooms_store if (r in UPSTAIRS if room == "upstairs" else r in DOWNSTAIRS)]
            else:
                targets = [room]

    # Apply action to each target room where possible
    applied, skipped = [], []
    for r in targets:
        if not r or r not in rooms_store or device not in rooms_store[r]:
            skipped.append(r)
            continue
        cur = rooms_store[r][device]
        new_state = ("on" if action == "turn_on"
                     else "off" if action == "turn_off"
                     else ("off" if cur == "on" else "on"))
        rooms_store[r][device] = new_state
        applied.append({"room": r, "new_state": new_state})

    # If multiple targets were specified, return bulk result
    if len(applied) > 1 or (len(skipped) > 0 and len(applied) > 0):
        return {"ok": True, "bulk": True, "scope": rooms_field or room, "device": device, "action": action, "applied": applied, "skipped": skipped}

    # Single room
    if room not in rooms_store or device not in rooms_store[room]:
        return {"ok": False, "error": f"Unsupported room/device. Known rooms: {list(rooms_store.keys())}"}

    cur = rooms_store[room][device]
    new_state = ("on" if action == "turn_on"
                 else "off" if action == "turn_off"
                 else ("off" if cur == "on" else "on"))
    rooms_store[room][device] = new_state
    # publish state change for SSE clients
    try:
        publish_state_event()
    except Exception:
        pass
    return {"ok": True, "room": room, "device": device, "action": action, "new_state": new_state}


def post_chat_openai(history: list) -> dict:
    """Send chat to OpenAI Chat Completions API and return the JSON response.

    Requires OPENAI_KEY environment variable. Returns dict or raises.
    """
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    # Ensure the cloud model receives the same system prompt guidance as the local helper
    sys_prompt = None
    if llm_helper and hasattr(llm_helper, 'SYSTEM_PROMPT'):
        sys_prompt = llm_helper.SYSTEM_PROMPT
    elif local_llm and hasattr(local_llm, 'SYSTEM_PROMPT'):
        sys_prompt = local_llm.SYSTEM_PROMPT
    else:
        sys_prompt = "You are Butler. Output JSON only as specified by the assistant's device-control schema."

    messages = [{"role": "system", "content": sys_prompt}]
    # include recent history if helper exposes MAX_HISTORY
    try:
        max_hist = llm_helper.MAX_HISTORY if llm_helper and hasattr(llm_helper, 'MAX_HISTORY') else (local_llm.MAX_HISTORY if local_llm and hasattr(local_llm, 'MAX_HISTORY') else None)
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


@app.post('/api/chat/dual')
def api_chat_dual():
    """POST endpoint to send same prompt/history to both local and cloud LLMs and return both results with timings."""
    data = request.get_json(force=True) or {}
    history = data.get('history') or data.get('messages') or []
    user = data.get('user')
    if user and not history:
        history = [{"role": "user", "content": user}]

    results = {}
    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futures = {}
        if local_llm:
            futures[ex.submit(local_llm.post_chat, history)] = 'local'
        else:
            results['local'] = {"ok": False, "error": "no local LLM available", "ms": None}

        if OPENAI_KEY:
            futures[ex.submit(post_chat_openai, history)] = 'cloud'
        else:
            results['cloud'] = {"ok": False, "error": "OPENAI_API_KEY not set", "ms": None}

        for fut in concurrent.futures.as_completed(futures):
            who = futures[fut]
            t = time.time() - start
            try:
                resp = fut.result()
            except Exception as e:
                results[who] = {"ok": False, "error": str(e), "ms": int(t*1000)}
                continue
            # extract assistant content (use llm_helper for generic parsing)
            try:
                if who == 'local' and local_llm:
                    content = local_llm.get_message_content(resp)
                elif llm_helper:
                    content = llm_helper.get_message_content(resp)
                else:
                    content = resp.get('choices', [])[0].get('message', {}).get('content', '')
            except Exception:
                content = ''

            # parse JSON and optionally apply (use llm_helper.extract_json when available)
            parsed = None
            applied = None
            try:
                parsed = llm_helper.extract_json(content) if llm_helper else None
            except Exception:
                parsed = None

            if parsed:
                last_text = history[-1]['content'] if history else (user or '')
                applied = apply_toolcall(parsed, target=who, last_user_text=last_text)

            results[who] = {"ok": True, "resp": resp, "content": content, "parsed": parsed, "applied": applied, "ms": int(t*1000)}

    # determine first
    times = []
    for k, v in results.items():
        if k not in ('local', 'cloud'): continue
        ms = v.get('ms')
        ms = ms if isinstance(ms, int) else 999999
        times.append((k, ms))
    first = min(times, key=lambda x: x[1])[0] if times else None
    return jsonify({"results": results, "first": first})


@app.post('/api/chat/local')
def api_chat_local():
    """Send a prompt/history to the local LLM only and return its response."""
    if local_llm is None:
        return jsonify({'ok': False, 'error': 'no local LLM available'}), 500
    data = request.get_json(force=True) or {}
    history = data.get('history') or data.get('messages') or []
    user = data.get('user')
    if user and not history:
        history = [{"role": "user", "content": user}]
    try:
        t0 = time.time()
        resp = local_llm.post_chat(history)
        t1 = time.time()
        content = local_llm.get_message_content(resp) if local_llm else ''
        parsed = None
        applied = None
        try:
            parsed = local_llm.extract_json(content) if local_llm else None
        except Exception:
            parsed = None
        if parsed:
            last_text = history[-1]['content'] if history else (user or '')
            applied = apply_toolcall(parsed, target='local', last_user_text=last_text)
        ms = int((t1 - t0) * 1000)
        return jsonify({'ok': True, 'resp': resp, 'content': content, 'parsed': parsed, 'applied': applied, 'ms': ms})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.post('/api/chat/cloud')
def api_chat_cloud():
    """Send a prompt/history to the cloud (OpenAI) LLM only and return its response."""
    if not OPENAI_KEY:
        return jsonify({'ok': False, 'error': 'OPENAI_API_KEY not set'}), 500
    data = request.get_json(force=True) or {}
    history = data.get('history') or data.get('messages') or []
    user = data.get('user')
    if user and not history:
        history = [{"role": "user", "content": user}]
    try:
        t0 = time.time()
        resp = post_chat_openai(history)
        t1 = time.time()
        # extract content and parse using helper utilities
        content = ''
        try:
            if llm_helper:
                content = llm_helper.get_message_content(resp)
            else:
                content = resp.get('choices', [])[0].get('message', {}).get('content', '')
        except Exception:
            content = ''
        parsed = None
        applied = None
        try:
            parsed = llm_helper.extract_json(content) if llm_helper else None
        except Exception:
            parsed = None
        if parsed:
            last_text = history[-1]['content'] if history else (user or '')
            applied = apply_toolcall(parsed, target='cloud', last_user_text=last_text)
        ms = int((t1 - t0) * 1000)
        return jsonify({'ok': True, 'resp': resp, 'content': content, 'parsed': parsed, 'applied': applied, 'ms': ms})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.get('/api/chat/stream')
def api_chat_stream():
    """SSE endpoint that races local and cloud and streams each model's arrival as an event.

    Usage: GET /api/chat/stream?user=... or &history=... (history not implemented in querystring)
    """
    user = request.args.get('user', '')
    history = [{"role": "user", "content": user}] if user else []

    def generate():
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            if local_llm:
                futures[ex.submit(local_llm.post_chat, history)] = 'local'
            if OPENAI_KEY:
                futures[ex.submit(post_chat_openai, history)] = 'cloud'

            for fut in concurrent.futures.as_completed(futures):
                who = futures[fut]
                t = int((time.time() - start) * 1000)
                try:
                    resp = fut.result()
                except Exception as e:
                    payload = {"model": who, "ok": False, "error": str(e), "ms": t}
                    yield f"event: model\ndata: {json.dumps(payload)}\n\n"
                    continue

                # get content and parse via helper
                try:
                    if who == 'local' and local_llm:
                        content = local_llm.get_message_content(resp)
                    elif llm_helper:
                        content = llm_helper.get_message_content(resp)
                    else:
                        content = resp.get('choices', [])[0].get('message', {}).get('content', '')
                except Exception:
                    content = ''

                parsed = None
                applied = None
                try:
                    parsed = llm_helper.extract_json(content) if llm_helper else None
                except Exception:
                    parsed = None

                if parsed:
                    last_text = history[-1]['content'] if history else (user or '')
                    applied = apply_toolcall(parsed, target=who, last_user_text=last_text)

                payload = {"model": who, "ok": True, "content": content, "parsed": parsed, "applied": applied, "ms": t}
                yield f"event: model\ndata: {json.dumps(payload, default=str)}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.get('/api/state/stream')
def api_state_stream():
    """SSE endpoint that streams state snapshots whenever the server state changes."""
    def gen(q: queue.Queue):
        # send initial snapshot
        try:
            init = json.dumps(_current_state_snapshot())
            yield f"event: state\ndata: {init}\n\n"
        except Exception:
            pass

        while True:
            try:
                payload = q.get(timeout=30)
            except Exception:
                # send a ping comment to keep connection alive
                try:
                    yield ": ping\n\n"
                except Exception:
                    break
                continue
            try:
                yield f"event: state\ndata: {payload}\n\n"
            except GeneratorExit:
                break

    q = queue.Queue()
    with _state_sub_lock:
        _state_subscribers.append(q)

    def stream_and_cleanup():
        try:
            return gen(q)
        finally:
            with _state_sub_lock:
                try:
                    _state_subscribers.remove(q)
                except Exception:
                    pass

    return Response(stream_with_context(stream_and_cleanup()), mimetype='text/event-stream')

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)