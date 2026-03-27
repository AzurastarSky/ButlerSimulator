from typing import Dict

# Shared room/device synonyms and normalizers used by backend modules
ROOM_SYNONYMS: Dict[str, str] = {
    "lounge": "living room",
    "livingroom": "living room",
    "lr": "living room",
    "diner": "dining room",
    "kit": "kitchen",
    # scopes
    "whole house": "all",
    "entire house": "all",
    "all rooms": "all",
    "everywhere": "all",
    "down stairs": "downstairs",
    "ground floor": "downstairs",
    "first floor": "upstairs",
    "upper floor": "upstairs",
    # common shorthand
    "house": "all",
}

DEVICE_SYNONYMS: Dict[str, str] = {
    "lamp": "light",
    "lights": "light",
    "ceiling light": "light",
}

VALID_ROOMS = {
    "living room",
    "dining room",
    "kitchen",
    "bathroom",
    "bedroom",
    "office",
    "all",
    "upstairs",
    "downstairs",
    "house",
}

VALID_DEVICES = {"light", "thermostat"}


def norm_room(value: str) -> str:
    try:
        v = (value or "").strip().lower()
        return ROOM_SYNONYMS.get(v, v)
    except Exception:
        return (value or "").strip().lower()


def norm_device(value: str) -> str:
    try:
        v = (value or "").strip().lower()
        return DEVICE_SYNONYMS.get(v, v)
    except Exception:
        return (value or "").strip().lower()


def clamp(v: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, v))
    except Exception:
        return v
