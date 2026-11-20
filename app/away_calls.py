"""Bounded notification calls, authorized only by fresh outside-home evidence."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.home_presence import HomePresence, get_home_presence
from core.protected_config import read_config, write_config
from core.telegram_call_coordinator import CallCoordinator
from core.telegram_call_diagnostics import record
from core.telegram_call_setup import private_directory


class AwayCalls:
    def __init__(
        self,
        service: CallCoordinator,
        presence: HomePresence,
        *,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.service, self.presence, self.clock = service, presence, clock
        self.path = path or private_directory() / "away-call-budget.json"
        self._budget: dict[str, Any] = {"last": 0, "day": "", "count": 0, "seen": {}}
        if self.path.exists():
            value = json.loads(read_config(self.path)["away_call_budget_secret"])
            if (
                not isinstance(value, dict)
                or type(value.get("last")) not in (int, float)
                or type(value.get("count")) is not int
                or not isinstance(value.get("seen"), dict)
            ):
                raise ValueError("Invalid automatic call budget.")
            self._budget = value

    def offer(self, text: str, kind: str, event_id: str = "") -> bool:
        """Loop-owned, one attempt per event. Does not replay missed notifications."""
        if kind not in {"background", "scheduled", "approval", "urgent"} or not text.strip():
            return False
        if (
            self.presence.owner_id != self.service.owner_id
            or not self.presence.permitted()
            or not self.service.snapshot()["canDial"]
        ):
            return False
        now = self.clock()
        local = datetime.fromtimestamp(now, ZoneInfo("Africa/Lagos"))
        day = local.date().isoformat()
        identity = hashlib.sha256(
            f"{kind}:{event_id or ' '.join(text.split())}".encode()
        ).hexdigest()
        seen = {
            key: timestamp
            for key, timestamp in self._budget["seen"].items()
            if 0 <= now - timestamp < 86400
        }
        count = self._budget["count"] if self._budget.get("day") == day else 0
        if (
            local.hour < 8
            or local.hour >= 22
            or now - self._budget["last"] < 900
            or identity in seen
            or count >= 6
        ):
            return False
        # Persist the attempt before queuing an external action. Fail closed if the
        # journal cannot be saved; restarting must not reset cooldown/dedupe.
        seen[identity] = now
        budget = {"last": now, "day": day, "count": count + 1, "seen": seen}
        write_config(self.path, {"away_call_budget_secret": json.dumps(budget)})
        self._budget = budget
        self.service.request_call_owner(notification=text, permitted=self.presence.permitted)
        record("automatic_notification_call_requested", self.service.generation)
        return True


def automatic_report(engine: Any, text: str, kind: str, event_id: str = "") -> None:
    """Optional hook: keep the existing report even if dialing is unavailable."""
    loop = getattr(engine, "_loop", None)
    running = getattr(loop, "is_running", None)
    post = getattr(loop, "call_soon_threadsafe", None)
    if not callable(running) or not callable(post) or not running():
        return

    def apply() -> None:
        service = getattr(engine, "_telegram_calls", None)
        if service is None:
            return
        try:
            notifier = getattr(service, "_away_calls", None)
            if notifier is None:
                notifier = AwayCalls(service, get_home_presence())
                service._away_calls = notifier
            if notifier.offer(text, kind, event_id):
                # Keep a readable fallback even if the phone never answers.
                from app.notify import _tg_notify

                _tg_notify(text)
        except Exception as exc:
            record("automatic_notification_call_failed", service.generation, exc)

    try:
        post(apply)
    except RuntimeError:
        record("automatic_notification_loop_closed", "")
