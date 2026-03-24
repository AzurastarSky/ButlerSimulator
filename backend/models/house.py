from dataclasses import dataclass, asdict
from typing import Dict, Any
import threading

@dataclass
class TemperatureSensor:
    name: str
    current_temp_c: float

@dataclass
class Room:
    name: str
    devices: Dict[str, Any]

class HouseState:
    def __init__(self):
        self._lock = threading.Lock()
        self._rooms: Dict[str, Room] = {
            "Living Room": Room(
                name="Living Room",
                devices={
                    "temperature_sensor": TemperatureSensor(name="Temp Sensor", current_temp_c=22.5)
                }
            )
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out = {}
            for room_name, room in self._rooms.items():
                out[room_name] = {
                    "name": room.name,
                    "devices": {
                        dev_key: asdict(dev_val) if hasattr(dev_val, "__dataclass_fields__") else dev_val
                        for dev_key, dev_val in room.devices.items()
                    }
                }
            return out

    def get_temperature(self, room: str) -> float:
        with self._lock:
            r = self._rooms[room]
            sensor = r.devices["temperature_sensor"]
            return sensor.current_temp_c