# core/gmail_watcher.py
"""Background inbox watcher that turns high-priority mail into actionable alerts.

Rules this thread lives by, because it shares a process with a live voice loop:

* **It never raises.** Every cycle is wrapped; a failure backs off instead of
  propagating. A dead network must not take the microphone down with it.
* **It never speaks over Era.** Alerts are delivered through a caller-supplied
  callback that is only invoked when the caller's own `can_notify` predicate
  says the voice channel is free. When it isn't, the alert stays queued.
* **It is quiet by default.** Only messages that clear the user's own
  importance threshold produce an alert, at most a few per cycle, and never
  twice for the same message.
* **It is a daemon.** It cannot keep the process alive at shutdown.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.gmail_priority import load_criteria, rank_messages, summarize_for_voice
from core.gmail_service import gmail_state_dir, list_messages, load_json, save_json
from core.utils import log

MIN_INTERVAL_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 180
MAX_ALERTS_PER_CYCLE = 3
MAX_SEEN_IDS = 500
MAX_PENDING = 25
# Consecutive failures after which the watcher stops rather than hammering a
# broken endpoint. Recorded so `status()` can explain why it went quiet.
MAX_CONSECUTIVE_FAILURES = 5

_lock = threading.RLock()
_watcher: GmailWatcher | None = None


def state_path() -> Path:
    return gmail_state_dir() / "watcher_state.json"


class GmailWatcher:
    """Polls the inbox and queues alerts for messages that clear the threshold."""

    def __init__(
        self,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        notify: Callable[[str, dict], None] | None = None,
        can_notify: Callable[[], bool] | None = None,
        user_address: str = "",
    ) -> None:
        self.interval = max(MIN_INTERVAL_SECONDS, int(interval_seconds or DEFAULT_INTERVAL_SECONDS))
        self._notify = notify
        self._can_notify = can_notify
        self.user_address = user_address
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: list[dict] = []
        self._seen: list[str] = []
        self._failures = 0
        self.last_error = ""
        self.last_poll_ts = 0.0
        self.cycles = 0
        self.alerts_raised = 0
        self._load_state()

    # -- state ---------------------------------------------------------------

    def _load_state(self) -> None:
        data = load_json(state_path(), {})
        if isinstance(data, dict):
            self._seen = [str(x) for x in (data.get("seen_ids") or [])][-MAX_SEEN_IDS:]
            pending = data.get("pending") or []
            if isinstance(pending, list):
                self._pending = [p for p in pending if isinstance(p, dict)][-MAX_PENDING:]

    def _save_state(self) -> None:
        save_json(
            state_path(),
            {
                "seen_ids": self._seen[-MAX_SEEN_IDS:],
                "pending": self._pending[-MAX_PENDING:],
                "updated": time.time(),
            },
        )

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._failures = 0
        self._thread = threading.Thread(target=self._run, name="era-gmail-watcher", daemon=True)
        self._thread.start()
        log("GMAIL", f"inbox watcher started (every {self.interval}s)")
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None
        log("GMAIL", "inbox watcher stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- polling -------------------------------------------------------------

    def _run(self) -> None:
        # A short delay so starting the watcher never competes with whatever
        # the user asked for in the same breath.
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            try:
                self.poll_once()
                self._failures = 0
            except Exception as e:  # defensive: the loop must never die
                self._failures += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log("GMAIL", f"watcher cycle failed ({self._failures}): {type(e).__name__}")
                if self._failures >= MAX_CONSECUTIVE_FAILURES:
                    self.last_error = (
                        f"stopped after {self._failures} consecutive failures ({type(e).__name__})"
                    )
                    log("GMAIL", "watcher stopping — too many consecutive failures")
                    return
            # Back off on failure so a broken token is not retried every cycle.
            wait_for = self.interval * (1 + min(self._failures, 4))
            if self._stop.wait(wait_for):
                return

    def poll_once(self) -> list[dict]:
        """One inbox sweep. Returns the alerts raised this cycle."""
        self.cycles += 1
        self.last_poll_ts = time.time()
        criteria = load_criteria()
        result = list_messages(
            query="is:unread" if criteria.get("unread_only") else "",
            max_results=15,
            label_ids=["INBOX"],
            include_body=False,
        )
        if not result.get("ok"):
            self.last_error = result.get("message", "unknown error")
            # Not connected is a resting state, not a failure to back off from.
            if "not connected" not in self.last_error.lower():
                raise RuntimeError(self.last_error)
            return []

        self.last_error = ""
        messages = result.get("messages") or []
        ranked = rank_messages(messages, criteria, self.user_address)

        fresh: list[dict] = []
        for m in ranked:
            mid = m.get("id", "")
            if not mid or mid in self._seen:
                continue
            self._seen.append(mid)
            if m["priority"]["important"]:
                fresh.append(m)

        # Cap per cycle: a 40-message backlog must not become 40 spoken alerts.
        alerts = [self._build_alert(m) for m in fresh[:MAX_ALERTS_PER_CYCLE]]
        overflow = max(0, len(fresh) - MAX_ALERTS_PER_CYCLE)
        if overflow:
            alerts.append(
                {
                    "id": f"overflow-{int(time.time())}",
                    "summary": f"and {overflow} more high-priority message(s) waiting",
                    "message_id": "",
                    "from": "",
                    "subject": "",
                    "score": 0,
                    "reasons": [],
                    "actions": ["list important emails"],
                    "created": time.time(),
                }
            )

        if alerts:
            self._pending.extend(alerts)
            self._pending = self._pending[-MAX_PENDING:]
            self.alerts_raised += len(alerts)

        self._seen = self._seen[-MAX_SEEN_IDS:]
        self._save_state()

        for alert in alerts:
            self._deliver(alert)
        return alerts

    def _build_alert(self, m: dict) -> dict:
        priority = m.get("priority") or {}
        sender = m.get("from_address", "")
        return {
            "id": f"alert-{m.get('id', '')}",
            "message_id": m.get("id", ""),
            "thread_id": m.get("thread_id", ""),
            "from": m.get("from", ""),
            "from_address": sender,
            "subject": m.get("subject", ""),
            "score": priority.get("score", 0),
            "reasons": priority.get("reasons", []),
            "summary": summarize_for_voice(m),
            # Actionable: each string is a phrase the user can say straight back.
            "actions": [
                f"read email {m.get('id', '')}",
                f"reply to {sender}" if sender else "reply",
                f"mute {sender}" if sender else "mute this sender",
            ],
            "created": time.time(),
        }

    def _deliver(self, alert: dict) -> None:
        """Hand one alert to the caller's notifier, if the channel is free."""
        if self._notify is None:
            return
        try:
            if self._can_notify is not None and not self._can_notify():
                return  # stays queued; the user can ask for it later
            self._notify(alert.get("summary", ""), alert)
        except Exception as e:
            log("GMAIL", f"alert delivery failed: {type(e).__name__}")

    # -- queue ---------------------------------------------------------------

    def pending(self) -> list[dict]:
        return list(self._pending)

    def acknowledge(self, alert_id: str = "") -> int:
        """Clear one alert, or all of them. Returns how many were cleared."""
        before = len(self._pending)
        if alert_id:
            self._pending = [a for a in self._pending if a.get("id") != alert_id]
        else:
            self._pending = []
        self._save_state()
        return before - len(self._pending)

    def status(self) -> dict:
        return {
            "running": self.running,
            "interval_seconds": self.interval,
            "cycles": self.cycles,
            "alerts_raised": self.alerts_raised,
            "pending": len(self._pending),
            "last_poll_ts": self.last_poll_ts,
            "last_error": self.last_error,
            "consecutive_failures": self._failures,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def get_watcher() -> GmailWatcher | None:
    with _lock:
        return _watcher


def start_watcher(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    notify: Callable[[str, dict], None] | None = None,
    can_notify: Callable[[], bool] | None = None,
    user_address: str = "",
) -> dict:
    """Start (or restart) the shared watcher. Never raises."""
    global _watcher
    with _lock:
        try:
            if _watcher is not None and _watcher.running:
                _watcher.stop()
            _watcher = GmailWatcher(
                interval_seconds=interval_seconds,
                notify=notify,
                can_notify=can_notify,
                user_address=user_address,
            )
            started = _watcher.start()
            return {
                "ok": started,
                "message": (
                    f"Watching the inbox every {_watcher.interval} seconds."
                    if started
                    else "The inbox watcher was already running."
                ),
                "status": _watcher.status(),
            }
        except Exception as e:
            return {"ok": False, "message": f"Could not start the inbox watcher: {e}"}


def stop_watcher() -> dict:
    global _watcher
    with _lock:
        if _watcher is None or not _watcher.running:
            return {"ok": True, "message": "The inbox watcher was not running."}
        try:
            _watcher.stop()
            return {"ok": True, "message": "Inbox watching stopped."}
        except Exception as e:
            return {"ok": False, "message": f"Could not stop the inbox watcher: {e}"}


def watcher_status() -> dict:
    with _lock:
        if _watcher is None:
            return {"running": False, "pending": 0, "message": "The inbox watcher is not running."}
        status: dict[str, Any] = _watcher.status()
        status["message"] = (
            "Inbox watching is active." if status["running"] else "The inbox watcher is stopped."
        )
        return status
