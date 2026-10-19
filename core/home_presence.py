"""Owner-shared location evidence. No network access or location history."""

from __future__ import annotations

import json
import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.protected_config import read_config, write_config
from core.telegram_call_setup import private_directory


def number(value: Any, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Invalid location metadata.")
    return float(value)


def coordinates(location: dict[str, Any]) -> tuple[float, float]:
    return number(location.get("latitude"), -90, 90), number(location.get("longitude"), -180, 180)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = map(math.radians, (a[0], b[0]))
    x = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    )
    return 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, x))))


class HomePresence:
    def __init__(self, *, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = path or private_directory() / "home-presence.json"
        self.clock = clock
        self._lock = threading.RLock()
        self.home: tuple[float, float] | None = None
        self.owner_id: int | None = None
        self.enabled = False
        self.mode = "location"
        self._manual_state = "home"
        self._manual_until = 0.0
        self._arm_until = 0.0
        self._sample_at = self._expires = self._outside_since = 0.0
        self._stream_date = self._edit_at = 0.0
        self._stream_id = 0
        self._state = "unknown"
        if self.path.exists():
            settings = json.loads(read_config(self.path)["home_presence_secret"])
            self.mode = settings.get("mode", "location")
            if self.mode == "manual":
                self._manual_state = settings.get("state", "unknown")
                self._manual_until = number(settings.get("until", 0), 0, 1e20)
            else:
                self.home = coordinates(settings)
            owner = settings.get("owner_id")
            if type(owner) is not int or owner <= 0:
                raise ValueError("Invalid home owner binding.")
            self.owner_id = owner
            self.enabled = settings.get("enabled") is True

    def _save(self) -> None:
        if self.mode == "manual":
            settings = {
                "mode": "manual",
                "state": self._manual_state,
                "until": self._manual_until,
                "enabled": self.enabled,
                "owner_id": self.owner_id,
            }
        else:
            assert self.home is not None
            settings = {
                "latitude": self.home[0],
                "longitude": self.home[1],
                "enabled": self.enabled,
                "owner_id": self.owner_id,
            }
        write_config(
            self.path,
            {"home_presence_secret": json.dumps(settings)},
        )

    def state(self) -> str:
        with self._lock:
            now = self.clock()
            if self.mode == "manual":
                if self._manual_state == "home":
                    return "home"
                return (
                    "away"
                    if self._manual_state == "away" and 0 < self._manual_until - now <= 28800
                    else "unknown"
                )
            if not self.home or not 0 <= now - self._sample_at <= 300 or now >= self._expires:
                return "unknown"
            return self._state

    def permitted(self) -> bool:
        with self._lock:
            return self.enabled and self.state() == "away"

    def use_manual(self, owner: int, state: str = "home") -> str:
        if type(owner) is not int or owner <= 0 or state not in {"home", "away"}:
            raise ValueError("Verified owner and explicit presence required.")
        with self._lock:
            old = (
                self.mode,
                self.home,
                self.owner_id,
                self.enabled,
                self._manual_state,
                self._manual_until,
            )
            self.mode, self.home, self.owner_id, self.enabled = (
                "manual",
                None,
                owner,
                True,
            )
            self._manual_state, self._manual_until = (
                state,
                (self.clock() + 28800 if state == "away" else 0),
            )
            try:
                self._save()
            except Exception:
                (
                    self.mode,
                    self.home,
                    self.owner_id,
                    self.enabled,
                    self._manual_state,
                    self._manual_until,
                ) = old
                raise
            return (
                "Away mode enabled for eight hours. Important notifications may call you on Telegram. Tell me when you return; no location sharing is needed."
                if state == "away"
                else "Home mode enabled. Automatic calls are blocked; no location sharing is needed."
            )

    def command(self, text: str, owner: int) -> str | None:
        state = presence_intent(text)
        return self.use_manual(owner, state) if state else None

    def local_activity(self) -> bool:
        """Physical PC use vetoes Away after a short departure grace period."""
        with self._lock:
            if self.mode != "manual" or self._manual_state != "away" or self.owner_id is None:
                return False
            if self.clock() < self._manual_until - 28800 + 30:
                return False
            self.use_manual(self.owner_id, "home")
            return True

    def handle(self, message: dict[str, Any], owner: str) -> tuple[bool, str | None]:
        """Defense in depth after bot authorization; no response to foreign input."""
        with self._lock:
            chat, sender = message.get("chat") or {}, message.get("from") or {}
            if (
                not owner.isdecimal()
                or int(owner) <= 0
                or chat.get("type") != "private"
                or type(sender.get("id")) is not int
                or sender["id"] != int(owner)
                or chat.get("id") != int(owner)
            ):
                return False, None
            text = (message.get("text") or "").strip().lower()
            now = self.clock()
            manual = self.command(text, int(owner))
            if manual:
                return True, manual
            if text == "/sethome":
                self._arm_until = now + 300
                return (
                    True,
                    "While at home, send your current location within five minutes. This will set your home boundary and enable away-only calls. Then share Live Location so I can detect departures.",
                )
            if text == "/presence":
                return (
                    True,
                    f"Presence: {self.state()}. Away-only automatic calls: {'on' if self.enabled else 'off'}. "
                    + (
                        "Manual mode: tell me when you leave or return; Away expires after eight hours."
                        if self.mode == "manual"
                        else (
                            "Share fresh Live Location for automatic detection."
                            if self.home
                            else "Send /sethome while at home, then your current location."
                        )
                    ),
                )
            if text == "/forgethome":
                self.path.unlink(missing_ok=True)
                self._manual_state, self._manual_until = "unknown", 0
                self.home, self.owner_id, self.enabled, self._state, self._arm_until = (
                    None,
                    None,
                    False,
                    "unknown",
                    0,
                )
                return (
                    True,
                    "Home location removed. Automatic calls are off. Incoming calls still work.",
                )
            if text in {"/autocalls on", "/autocalls off"}:
                if not self.home and self.mode != "manual":
                    return (
                        True,
                        "Set home first with /sethome and your current location.",
                    )
                if self.owner_id != int(owner):
                    return (
                        True,
                        "Home owner binding changed. Use /forgethome and /sethome first.",
                    )
                old = self.enabled
                self.enabled = text.endswith(" on")
                try:
                    self._save()
                except Exception:
                    self.enabled = old
                    raise
                return (
                    True,
                    f"Away-only automatic calls {'enabled' if self.enabled else 'disabled'}. Incoming calls are unchanged.",
                )
            location = message.get("location")
            if not isinstance(location, dict):
                return False, None
            if self.mode == "manual" and self._arm_until <= now:
                return (
                    True,
                    "Manual presence mode is active. You can stop Telegram live sharing; tell me when you leave or return instead.",
                )
            if any(
                message.get(key) is not None
                for key in ("forward_origin", "forward_date", "forward_from", "via_bot")
            ):
                return (
                    True,
                    "Send your own current or live location, not a forwarded map.",
                )
            try:
                point = coordinates(location)
                accuracy = number(location.get("horizontal_accuracy", 100), 0, 1500)
                stamp = number(message.get("edit_date", message.get("date")), 1, now + 5)
                date = number(message.get("date"), 1, now + 5)
            except ValueError:
                self._state, self._outside_since = "unknown", 0
                raise
            identifier = message.get("message_id")
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("Invalid location message.")
            if now - stamp > 300:
                return True, "That location is old. Share a fresh Live Location."
            if self._arm_until > now and not location.get("live_period"):
                if accuracy > 100 or message.get("edit_date") is not None:
                    return (
                        True,
                        "Send a fresh current location with precise location enabled to set home.",
                    )
                previous_home = self.home, self.enabled, self.owner_id, self.mode
                self.mode = "location"
                self.home, self.enabled, self.owner_id = point, True, int(owner)
                try:
                    self._save()
                except Exception:
                    self.home, self.enabled, self.owner_id, self.mode = previous_home
                    raise
                self._arm_until = 0
                self._state, self._sample_at, self._expires = "home", stamp, now + 300
                self._stream_date = self._edit_at = self._outside_since = 0
                self._stream_id = 0
                return (
                    True,
                    "Home saved securely; away-only calls enabled. Now share Live Location in this chat. At home or with stale location I will not call automatically.",
                )
            period = location.get("live_period")
            if not self.home:
                return (
                    True,
                    "Send /sethome while at home, then your current location first.",
                )
            if self.owner_id != int(owner):
                return (
                    True,
                    "Home belongs to a different owner binding. Use /forgethome, then /sethome to configure your own home.",
                )
            if (
                date < self._stream_date
                or (date == self._stream_date and identifier < self._stream_id)
                or (identifier == self._stream_id and stamp <= self._edit_at)
            ):
                return True, None
            if period is None or period == 0:
                if identifier == self._stream_id:
                    self._state, self._outside_since, self._expires = "unknown", 0, 0
                return True, (
                    "Use Share Live Location for continuous detection."
                    if not message.get("edit_date")
                    else None
                )
            period_value = number(period, 1, 2147483647)
            if date + period_value <= now:
                if identifier == self._stream_id:
                    self._state, self._expires, self._outside_since = "unknown", 0, 0
                return True, None
            fresh_stream = identifier != self._stream_id
            if fresh_stream or not 0 <= stamp - self._sample_at <= 300:
                self._outside_since = 0
            self._stream_date, self._stream_id, self._edit_at = date, identifier, stamp
            self._sample_at, self._expires = stamp, date + period_value
            meters = distance(self.home, point)
            if accuracy > 100:
                self._state, self._outside_since = "unknown", 0
            elif meters + accuracy <= 150:
                self._state, self._outside_since = "home", 0
            elif meters - accuracy >= 250:
                if not self._outside_since:
                    self._outside_since = stamp
                self._state = "away" if stamp - self._outside_since >= 30 else "unknown"
            else:
                self._state, self._outside_since = "unknown", 0
            return True, (
                f"Live location received. Presence: {self.state()}. I call automatically only when confirmed away."
                if fresh_stream
                else None
            )


_service: HomePresence | None = None
_service_lock = threading.Lock()


def presence_intent(text: str) -> str | None:
    if "?" in text:
        return None
    normalized = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    normalized = " ".join(re.sub(r"[^a-z\s/]", " ", normalized).split())
    normalized = re.sub(r"^(?:(?:hey|era|please|ok|okay|s il te plait) )+", "", normalized)
    if re.search(
        r"\b(?:tomorrow|demain|later today|next week|next month|yesterday|hier|not|don t|dont|never|would|might|maybe|if|how|explain|said|thinking|plan|planning|pas)\b",
        normalized,
    ):
        return None
    if normalized in {
        "/away",
        "i m leaving home",
        "i am leaving home",
        "i m going out",
        "i am going out",
        "je quitte la maison",
        "je sors de la maison",
        "je sors",
        "je pars de la maison",
    }:
        return "away"
    if normalized in {
        "/home",
        "i m back home",
        "i am back home",
        "i m home",
        "i am home",
        "i m back",
        "je suis rentre",
        "je suis rentre a la maison",
        "je suis a la maison",
        "je suis de retour a la maison",
    }:
        return "home"
    away = re.fullmatch(
        r"(?:i (?:m|am) (?:leaving (?:home|the house)|heading (?:out|outside)|going (?:out|outside|to work|shopping)|off (?:to work|out)|stepping (?:out|outside))|"
        r"i (?:m|am) (?:outside|away from home)|leaving (?:home|the house)|heading out|"
        r"je (?:sors|quitte la maison|pars (?:de la maison|au travail)))"
        r"(?: (?:now|right now|for a while|for work|to work|shopping|for groceries|for a walk|see you later|maintenant|faire des courses))*",
        normalized,
    )
    home = re.fullmatch(
        r"(?:i (?:m|am) (?:back (?:home|at home|at my place)|home|at home)|"
        r"i (?:just )?(?:got|arrived) home|i (?:have|ve) (?:just )?(?:returned|arrived) home|"
        r"back home|home again|je (?:suis (?:rentre|de retour|a la maison)|viens de rentrer))"
        r"(?: (?:now|again|already|at last|a la maison|maintenant))*",
        normalized,
    )
    if away:
        return "away"
    if home:
        return "home"
    return None


def get_home_presence() -> HomePresence:
    global _service
    with _service_lock:
        if _service is None:
            _service = HomePresence()
        return _service
