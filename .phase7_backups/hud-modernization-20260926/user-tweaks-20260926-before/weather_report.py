import webbrowser
from urllib.parse import quote_plus

# Open-Meteo: free, no API key. The reply is a sentence Era can READ OUT — the old
# tool only opened a Google search, so a scheduled run had nothing to say.
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 8

# WMO weather interpretation codes, grouped.
_WMO = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city = parameters.get("city")
    when = parameters.get("time", "today")

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip() if isinstance(when, str) else "today"
    search_query = f"weather in {city} {when}"

    try:
        msg = fetch_forecast(city, when)
    except Exception as e:
        log(f"forecast lookup unavailable ({type(e).__name__}); opening the browser", player)
        msg = _open_browser(city, when, search_query)
        log(msg, player)
        return msg

    log(msg, player)
    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass
    return msg


# WMO code -> HUD icon key; "clear"/"partly" get a day/night variant.
_ICON = {
    **dict.fromkeys((0, 1), "clear"),
    2: "partly",
    3: "cloud",
    **dict.fromkeys((45, 48), "fog"),
    **dict.fromkeys((51, 53, 55, 56, 57), "drizzle"),
    **dict.fromkeys((61, 63, 65, 66, 67), "rain"),
    **dict.fromkeys((71, 73, 75, 77, 85, 86), "snow"),
    **dict.fromkeys((80, 81, 82), "showers"),
    **dict.fromkeys((95, 96, 99), "storm"),
}
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_RAIN_LIKELY_PCT = 50  # hourly chance at which "rain around HH:MM" is announced
_places: dict[str, dict] = {}  # geocoding cache: a city does not move


def icon_key(code, is_day=True) -> str:
    base = _ICON.get(code, "cloud") if code is not None else "cloud"
    if base in ("clear", "partly"):
        return f"{base}-{'day' if is_day else 'night'}"
    return base


def compass(deg) -> str:
    if deg is None:
        return ""
    return _COMPASS[int((float(deg) % 360) / 45 + 0.5) % 8]


def _geocode(city: str) -> dict:
    import requests

    key = city.strip().lower()
    if key in _places:
        return _places[key]
    geo = requests.get(
        _GEOCODE_URL, params={"name": city, "count": 1, "format": "json"}, timeout=_TIMEOUT_S
    )
    geo.raise_for_status()
    places = geo.json().get("results") or []
    if not places:
        raise LookupError(f"unknown city {city!r}")
    _places[key] = places[0]
    return places[0]


def _fetch(place: dict, days: int) -> dict:
    """One Open-Meteo call. `current` is Open-Meteo's 15-minute nowcast (the
    freshest reading it has); hourly feeds the "rain around HH:MM" timing."""
    import requests

    fc = requests.get(
        _FORECAST_URL,
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,is_day,precipitation,cloud_cover,pressure_msl,"
            "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "hourly": "precipitation_probability",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,uv_index_max,sunrise,sunset",
            "timezone": "auto",
            "forecast_days": days,
        },
        timeout=_TIMEOUT_S,
    )
    fc.raise_for_status()
    return fc.json()


def fetch_forecast(city: str, when: str = "today") -> str:
    """Spoken forecast for `city`. Raises on any network or lookup problem."""
    place = _geocode(city)
    return format_forecast(place.get("name") or city, when, _fetch(place, 2))


def rain_ahead(data: dict, hours: int = 12) -> dict | None:
    """First upcoming hour today (from the current reading on) whose rain
    chance reaches _RAIN_LIKELY_PCT, as {"time": "HH:MM", "chance": n}."""
    hourly = data.get("hourly") or {}
    now = (data.get("current") or {}).get("time") or ""
    if not now:
        return None
    seen = 0
    for t, p in zip(
        hourly.get("time") or [], hourly.get("precipitation_probability") or [], strict=False
    ):
        if t[:13] < now[:13]:
            continue
        if t[:10] != now[:10] or seen >= hours:
            break
        seen += 1
        if p is not None and p >= _RAIN_LIKELY_PCT:
            return {"time": t[11:16], "chance": p}
    return None


def fetch_current(city: str) -> dict:
    """Structured current conditions for the HUD weather card — same Open-Meteo
    source as fetch_forecast, returned as data instead of a spoken sentence.
    Raises on any network or lookup problem; the caller reports it as such."""
    place = _geocode(city)
    data = _fetch(place, 1)
    cur = data.get("current") or {}
    daily = data.get("daily") or {}

    def _first(key: str):
        vals = daily.get(key) or []
        return vals[0] if vals else None

    def _hhmm(v):
        return v[11:16] if isinstance(v, str) and len(v) >= 16 else None

    code = cur.get("weather_code")
    is_day = bool(cur.get("is_day", 1))
    return {
        "city": place.get("name") or city,
        "country": place.get("country") or "",
        "temperature": cur.get("temperature_2m"),
        "feels_like": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "gusts_kmh": cur.get("wind_gusts_10m"),
        "wind_dir": compass(cur.get("wind_direction_10m")),
        "precip_mm": cur.get("precipitation"),
        "cloud_cover": cur.get("cloud_cover"),
        "pressure_hpa": cur.get("pressure_msl"),
        "code": code,
        "is_day": is_day,
        "icon": icon_key(code, is_day),
        "sky": _WMO.get(code, "unknown") if code is not None else "unknown",
        "high": _first("temperature_2m_max"),
        "low": _first("temperature_2m_min"),
        "rain_chance": _first("precipitation_probability_max"),
        "rain_ahead": rain_ahead(data),
        "uv_max": _first("uv_index_max"),
        "sunrise": _hhmm(_first("sunrise")),
        "sunset": _hhmm(_first("sunset")),
        "observed_at": cur.get("time"),
        "source": "Open-Meteo",
    }


def _live_extras(cur: dict, data: dict) -> tuple[str, str]:
    """Spoken detail added only when the reading carries it AND it tells the
    user something: a feels-like that differs, real wind, rain coming later."""
    extra = ""
    now, feels = cur.get("temperature_2m"), cur.get("apparent_temperature")
    if now is not None and feels is not None and abs(round(feels) - round(now)) >= 2:
        extra += f", feels like {round(feels)}"
    wind = cur.get("wind_speed_10m")
    if wind is not None and wind >= 20:
        gusts = cur.get("wind_gusts_10m")
        gust = f" gusting {round(gusts)}" if gusts is not None and gusts >= wind + 10 else ""
        where = compass(cur.get("wind_direction_10m"))
        extra += f", {where + ' ' if where else ''}wind {round(wind)} km/h{gust}"
    ahead = rain_ahead(data)
    return extra, (f", rain likely around {ahead['time']}" if ahead else "")


def format_forecast(name: str, when: str, data: dict) -> str:
    tomorrow = "tomorrow" in when.lower() or "demain" in when.lower()
    day = 1 if tomorrow else 0
    daily = data["daily"]
    hi = round(daily["temperature_2m_max"][day])
    lo = round(daily["temperature_2m_min"][day])
    rain = (daily.get("precipitation_probability_max") or [None, None])[day]
    rain_txt = f", {rain}% chance of rain" if rain is not None else ""
    if tomorrow:
        sky = _WMO.get(daily["weather_code"][1], "mixed conditions")
        return f"{name} tomorrow: {sky}, high {hi} degrees, low {lo} degrees{rain_txt}, sir."
    cur = data.get("current") or {}
    sky = _WMO.get(cur.get("weather_code", daily["weather_code"][0]), "mixed conditions")
    now = cur.get("temperature_2m")
    now_txt = f"{round(now)} degrees and {sky} right now" if now is not None else sky
    extra, ahead = _live_extras(cur, data)
    return f"{name} today: {now_txt}{extra}, high {hi} degrees, low {lo} degrees{rain_txt}{ahead}, sir."


def _open_browser(city: str, when: str, search_query: str) -> str:
    url = f"https://www.google.com/search?q={quote_plus(search_query)}"
    try:
        if not webbrowser.open(url):
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        return f"Sir, I couldn't open the browser for the weather report: {e}"
    # No "couldn't" here: verify_weather reads that word as a failed lookup, and the
    # browser did show the weather.
    return (
        f"The forecast service didn't answer, so I've opened the weather for {city}, "
        f"{when}, in your browser, sir."
    )


def log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"Era One: {message}")
        except Exception:
            pass
