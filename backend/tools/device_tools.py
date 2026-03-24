from typing import Dict, Any
from backend.models.house import house_state

def get_temperature_tool(room: str) -> Dict[str, Any]:
    """Return temperature for a room using the shared house_state singleton."""
    temp = house_state.get_temperature(room)
    return {"room": room, "temperature_c": temp}