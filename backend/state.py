import time
import threading
import copy
import json
from pathlib import Path
import os

# Minimal helpers fallback if helpers.py isn't importable
try:
    from .helpers import norm_room, norm_device, clamp, ROOM_SYNONYMS, DEVICE_SYNONYMS, VALID_ROOMS, VALID_DEVICES
except Exception:
    ROOM_SYNONYMS = {}
    DEVICE_SYNONYMS = {}
    def norm_room(v: str) -> str: return (v or "").strip().lower()
    def norm_device(v: str) -> str: return (v or "").strip().lower()
    VALID_ROOMS = set()
    VALID_DEVICES = set()
    def clamp(v: float, lo: float, hi: float) -> float: return max(lo, min(hi, v))

# ---------------- In-memory State ----------------
STATE = {
    "living room": {"light": "off"},
    "dining room": {"light": "off"},
    "kitchen":     {"light": "off"},
    "bathroom": {"light": "off"},
    "bedroom":  {"light": "off"},
    "office":   {"light": "off"},
}

HOUSE = {"target": 20.0, "mode": "heat"}

STATE_LOCAL = copy.deepcopy(STATE)
STATE_CLOUD = copy.deepcopy(STATE)
HOUSE_LOCAL = copy.deepcopy(HOUSE)
HOUSE_CLOUD = copy.deepcopy(HOUSE)

AMBIENT = 18.0
LAST_UPDATE = time.time()
LAST_UPDATE_LOCAL = time.time()
LAST_UPDATE_CLOUD = time.time()

_state_subscribers = []
_state_sub_lock = threading.Lock()

DOWNSTAIRS = {"living room", "dining room", "kitchen"}
UPSTAIRS   = {"bathroom", "bedroom", "office"}

VALID_LIGHT_ACTIONS  = {"turn_on", "turn_off"}
VALID_THERMO_ACTIONS = {"increase", "decrease", "set_value", "turn_on", "turn_off"}


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


_state_publisher_started = False

def _update_house_temp():
    global LAST_UPDATE
    LAST_UPDATE = time.time()

def _update_house_temp_local():
    global LAST_UPDATE_LOCAL
    LAST_UPDATE_LOCAL = time.time()

def _update_house_temp_cloud():
    global LAST_UPDATE_CLOUD
    LAST_UPDATE_CLOUD = time.time()


def _state_publisher_loop(poll_interval: float = 1.0):
    while True:
        try:
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


# Start when module imported
try:
    ensure_state_publisher()
except Exception:
    pass
