"""weather_report live detail: icon keys, rain timing, and the extras that are
spoken only when the reading carries them (the minimal forecast is covered in
test_morning_routine_reliability.py and must stay word-for-word unchanged)."""

from actions import weather_report as wr


def _data(**cur):
    return {
        "current": {"time": "2026-09-26T13:00", "temperature_2m": 24.0, "weather_code": 3, **cur},
        "hourly": {
            "time": [f"2026-09-26T{h:02d}:00" for h in range(12, 24)]
            + ["2026-09-27T00:00", "2026-09-27T01:00"],
            "precipitation_probability": [90, 10, 20, 35, 70, 80, 5, 0, 0, 0, 0, 0, 95, 95],
        },
        "daily": {
            "weather_code": [3, 61],
            "temperature_2m_max": [26.0, 22.0],
            "temperature_2m_min": [18.0, 17.0],
            "precipitation_probability_max": [80, 60],
        },
    }


class TestIcon:
    def test_day_night_variants(self):
        assert wr.icon_key(0, True) == "clear-day"
        assert wr.icon_key(1, False) == "clear-night"
        assert wr.icon_key(2, False) == "partly-night"

    def test_precipitation_groups(self):
        assert wr.icon_key(63) == "rain"
        assert wr.icon_key(53) == "drizzle"
        assert wr.icon_key(81) == "showers"
        assert wr.icon_key(75) == "snow"
        assert wr.icon_key(95) == "storm"
        assert wr.icon_key(45) == "fog"

    def test_unknown_is_cloud(self):
        assert wr.icon_key(None) == "cloud"
        assert wr.icon_key(12345) == "cloud"


class TestCompass:
    def test_points(self):
        assert wr.compass(0) == "N"
        assert wr.compass(350) == "N"
        assert wr.compass(95) == "E"
        assert wr.compass(225) == "SW"
        assert wr.compass(None) == ""


class TestRainAhead:
    def test_skips_past_hours_and_finds_first_likely(self):
        # 12:00 is past (reading is 13:00); 16:00 is the first >= 50 %.
        assert wr.rain_ahead(_data()) == {"time": "16:00", "chance": 70}

    def test_never_crosses_into_tomorrow(self):
        data = _data()
        data["hourly"]["precipitation_probability"] = [0] * 12 + [95, 95]
        assert wr.rain_ahead(data) is None

    def test_no_hourly_or_no_reading(self):
        assert wr.rain_ahead({"current": {"time": "2026-09-26T13:00"}}) is None
        assert wr.rain_ahead({"hourly": _data()["hourly"]}) is None


class TestSpokenExtras:
    def test_rain_timing_is_spoken(self):
        text = wr.format_forecast("Tunis", "today", _data())
        assert text == (
            "Tunis today: 24 degrees and overcast right now, high 26 degrees, "
            "low 18 degrees, 80% chance of rain, rain likely around 16:00, sir."
        )

    def test_feels_like_only_when_it_differs(self):
        same = wr.format_forecast("T", "today", _data(apparent_temperature=24.6))
        assert "feels like" not in same
        hot = wr.format_forecast("T", "today", _data(apparent_temperature=28.2))
        assert ", feels like 28," in hot

    def test_wind_only_when_real(self):
        calm = wr.format_forecast("T", "today", _data(wind_speed_10m=8, wind_direction_10m=90))
        assert "wind" not in calm
        windy = wr.format_forecast(
            "T",
            "today",
            _data(wind_speed_10m=32, wind_gusts_10m=55, wind_direction_10m=315),
        )
        assert ", NW wind 32 km/h gusting 55," in windy

    def test_extras_never_look_like_a_failure(self):
        # verify_weather reads "missing"/"couldn't" as a failed lookup.
        text = wr.format_forecast(
            "T",
            "today",
            _data(apparent_temperature=30, wind_speed_10m=40, wind_gusts_10m=70),
        )
        assert "missing" not in text and "couldn't" not in text

    def test_tomorrow_is_unchanged_by_live_detail(self):
        text = wr.format_forecast("Tunis", "tomorrow", _data(apparent_temperature=30))
        assert (
            text
            == "Tunis tomorrow: light rain, high 22 degrees, low 17 degrees, 60% chance of rain, sir."
        )


class TestNextSun:
    DAILY = {
        "sunrise": ["2026-09-26T06:10", "2026-09-27T06:11"],
        "sunset": ["2026-09-26T18:10", "2026-09-27T18:08"],
    }

    def test_daytime_is_todays_sunset(self):
        data = {"current": {"time": "2026-09-26T13:00"}, "daily": self.DAILY}
        assert wr.next_sun(data) == {"kind": "sunset", "time": "18:10"}

    def test_after_dark_is_tomorrows_sunrise(self):
        data = {"current": {"time": "2026-09-26T21:00"}, "daily": self.DAILY}
        assert wr.next_sun(data) == {"kind": "sunrise", "time": "06:11"}

    def test_before_dawn_is_todays_sunrise(self):
        data = {"current": {"time": "2026-09-26T04:45"}, "daily": self.DAILY}
        assert wr.next_sun(data) == {"kind": "sunrise", "time": "06:10"}

    def test_no_data(self):
        assert wr.next_sun({"current": {"time": "2026-09-26T04:45"}}) is None
        assert wr.next_sun({"daily": self.DAILY}) is None


class TestGeocodeCache:
    def test_city_is_looked_up_once(self, monkeypatch):
        calls = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [{"name": "Tunis", "latitude": 36.8, "longitude": 10.2}]}

        import requests

        monkeypatch.setattr(wr, "_places", {})
        monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(a) or Resp())
        assert wr._geocode("Tunis")["name"] == "Tunis"
        assert wr._geocode(" tunis ")["name"] == "Tunis"
        assert len(calls) == 1
