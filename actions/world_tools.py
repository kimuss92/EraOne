"""Explicit geospatial queries, circuit guidance and Home Assistant control."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from urllib.parse import quote


def _coordinates(arguments: dict, prefix: str = "") -> tuple[float, float]:
    lat, lon = arguments.get(prefix + "latitude"), arguments.get(prefix + "longitude")
    if (
        type(lat) not in {int, float}
        or type(lon) not in {int, float}
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        raise ValueError("Provide numeric latitude (-90..90) and longitude (-180..180)")
    return float(lat), float(lon)


def geography(action: str, arguments: dict) -> dict:
    import requests

    observed = datetime.now(UTC).isoformat()
    if action == "map":
        lat, lon = _coordinates(arguments)
        return {
            "success": True,
            "url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}",
            "latitude": lat,
            "longitude": lon,
        }
    if action == "distance":
        lat1, lon1 = _coordinates(arguments)
        lat2, lon2 = _coordinates(arguments, "to_")
        a, b = math.radians(lat1), math.radians(lat2)
        h = (
            math.sin((b - a) / 2) ** 2
            + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
        )
        distance = 6371.0088 * 2 * math.asin(math.sqrt(min(1, h)))
        return {
            "success": True,
            "distance_km": round(distance, 2),
            "note": "Great-circle distance, not a driving route or live flight.",
            "geojson": {"type": "LineString", "coordinates": [[lon1, lat1], [lon2, lat2]]},
        }
    if action == "earthquakes":
        url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
        response = requests.get(url, timeout=12)
        response.raise_for_status()
        entries = [
            {
                "magnitude": f["properties"]["mag"],
                "place": f["properties"]["place"],
                "time": f["properties"]["time"],
                "coordinates": f["geometry"]["coordinates"],
                "url": f["properties"]["url"],
            }
            for f in response.json().get("features", [])[:100]
        ]
        return {"success": True, "source": url, "retrieved_at": observed, "events": entries}
    if action == "iss":
        url = "https://api.wheretheiss.at/v1/satellites/25544"
        response = requests.get(url, timeout=12)
        response.raise_for_status()
        return {
            "success": True,
            "source": url,
            "retrieved_at": observed,
            "position": response.json(),
        }
    raise ValueError("Use map, distance, earthquakes or iss")


def smart_home(action: str, arguments: dict) -> dict:
    import requests

    from core.empower_config import read, validate_url

    cfg = read()
    if not cfg.get("home_assistant_url") or not cfg.get("home_assistant_token"):
        raise ValueError("Configure Home Assistant in HUD Settings first")
    base = validate_url(cfg["home_assistant_url"]) + "/api"
    headers = {"Authorization": "Bearer " + cfg["home_assistant_token"]}
    entity = arguments.get("entity", "")
    if action == "states":
        response = requests.get(
            base + "/states", headers=headers, timeout=10, allow_redirects=False
        )
    elif action in {"turn_on", "turn_off", "toggle"}:
        if not isinstance(entity, str) or not re.fullmatch(
            r"(?:light|switch|fan|climate|media_player|input_boolean)\.[a-z0-9_]+", entity
        ):
            raise ValueError("Choose a supported entity from states")
        domain = entity.split(".")[0]
        response = requests.post(
            f"{base}/services/{domain}/{action}",
            headers=headers,
            json={"entity_id": entity},
            timeout=10,
            allow_redirects=False,
        )
    else:
        raise ValueError("Use states, turn_on, turn_off or toggle")
    if response.status_code != 200:
        raise ValueError(f"Home Assistant returned HTTP {response.status_code}")
    if action != "states":
        check = requests.get(
            base + "/states/" + quote(entity, safe=""),
            headers=headers,
            timeout=10,
            allow_redirects=False,
        )
        if check.status_code != 200:
            return {
                "success": False,
                "message": "Service accepted but state read-back failed",
                "entity": entity,
            }
        return {
            "success": True,
            "service_accepted": True,
            "observed_state": check.json(),
            "note": "Reported Home Assistant state; physical device behavior is not independently verified.",
        }
    return {
        "success": True,
        "states": [
            {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "name": s.get("attributes", {}).get("friendly_name", ""),
            }
            for s in response.json()
        ][:300],
    }


def circuit(arguments: dict) -> dict:
    board, sensor = arguments.get("board"), arguments.get("sensor")
    if board not in {"arduino_uno", "esp32"} or sensor != "dht11":
        raise ValueError(
            "Built-in verified template names: arduino_uno or esp32 with dht11. For other parts, use screen_process and consult the exact datasheets."
        )
    pin = "2" if board == "arduino_uno" else "4"
    voltage = "5V" if board == "arduino_uno" else "3V3"
    return {
        "success": True,
        "board": board,
        "sensor": sensor,
        "connections": [
            {"from": "DHT11 VCC", "to": voltage},
            {"from": "DHT11 DATA", "to": "GPIO " + pin},
            {"from": "DHT11 GND", "to": "GND"},
        ],
        "firmware": f'#include <DHT.h>\nDHT sensor({pin}, DHT11);\nvoid setup() {{ Serial.begin(115200); sensor.begin(); }}\nvoid loop() {{ delay(2000); float t = sensor.readTemperature(); float h = sensor.readHumidity(); if (isnan(t) || isnan(h)) {{ Serial.println("Read failed"); return; }} Serial.print(t); Serial.print(" C / "); Serial.println(h); }}\n',
        "requirements": ["Adafruit DHT sensor library", "Adafruit Unified Sensor library"],
        "notes": [
            "Disconnect power before wiring. Check the exact module pin labels and datasheet; pin order varies.",
            "A bare sensor needs a 4.7–10 kΩ data pull-up to the logic supply; modules may include it.",
            "ESP32 GPIO is not 5V tolerant; keep the data pull-up at 3.3V. Confirm the sensor/module supports the chosen supply.",
            "Template guidance only. No hardware recognition, firmware flashing or physical electrical verification has been performed.",
        ],
    }
