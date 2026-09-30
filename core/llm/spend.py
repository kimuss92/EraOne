"""Daily spend ledger for metered model calls.

WHY THIS EXISTS BEFORE DISPATCH DOES
------------------------------------
`config/llm.yaml` has carried `daily_budget_usd: 2.00` since phase 2 with
nothing enforcing it. That was harmless while no code could reach a paid
provider. Phase 10 makes Claude dispatch reachable, so the ceiling has to become
real in the same change — a retry loop against a $0.06/call provider is a
three-figure mistake that takes minutes.

WHAT IT GUARDS, AND WHAT IT DOES NOT
------------------------------------
This is a runaway guard, not an accounting system. The authority is the
IN-MEMORY total, which climbs monotonically within a process; the file exists
only so a restart does not hand back a fresh budget.

That distinction decides how it fails. A corrupt or unreadable ledger file
starts the day at zero and logs loudly, rather than refusing every call — a
bookkeeping problem should not brick the assistant. The cost of that choice is
bounded: it loses restart-continuity, not the guard, because a runaway loop
lives inside one process and its in-memory total still trips the ceiling.

Writes are atomic (temp file + replace) so a crash mid-write cannot leave a
half-written ledger that reads as "nothing spent today".
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.utils import BASE_DIR

DEFAULT_PATH = BASE_DIR / "runtime" / "llm_spend.json"

# Env escape hatch, matching ERA_LLM_CONFIG's role for the registry: a test
# points this at a tmp file so it never reads or writes the real ledger.
PATH_ENV = "ERA_LLM_SPEND_PATH"


def _ledger_path() -> Path:
    override = os.environ.get(PATH_ENV, "").strip()
    return Path(override) if override else DEFAULT_PATH


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# Windows-only retry budget. Chosen to ride out a scan or an indexer touching
# the file, and to stay far below anything a caller would notice: the whole
# ladder is ~35ms worst case, against a model call measured at ~2900ms.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_S = 0.004


def _atomic_replace(tmp: Path, dest: Path) -> None:
    """os.replace, retried briefly on the transient Windows lock errors.

    WHY THIS IS NOT JUST os.replace
      On Windows the replace can fail even when nothing is wrong with our code:
      if ANY handle is open on the destination at that instant, it raises
      WinError 5 (access denied) or WinError 32 (in use). Antivirus, the search
      indexer, and a concurrent reader all do that, briefly and unpredictably.

      Measured here: 1 failure in ~1200 writes under thread pressure. Rare
      enough to look like a flaky test, frequent enough that a long-running Era
      would silently stop persisting its spend total — the in-memory guard would
      still hold, but a restart would hand back a budget that had already been
      partly spent.

      Unique temp names (above) fixed writer-vs-writer collisions. This fixes
      the other half: the DESTINATION being held by someone else.

    POSIX has no such behaviour, so the retry simply never triggers there.
    """
    last: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, dest)  # atomic: a crash cannot leave a partial file
            return
        except PermissionError as e:  # WinError 5 / 32 both surface as this
            last = e
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
    if last is not None:
        raise last


@dataclass
class SpendState:
    date: str = field(default_factory=_today)
    total_usd: float = 0.0
    calls: int = 0
    by_alias: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "total_usd": round(self.total_usd, 6),
            "calls": self.calls,
            "by_alias": {k: round(v, 6) for k, v in self.by_alias.items()},
        }


class SpendLedger:
    """Thread-safe daily spend total with a hard ceiling."""

    def __init__(
        self,
        *,
        budget_usd: float = 0.0,
        path: Path | None = None,
        on_warning: Any = None,
    ) -> None:
        self.budget_usd = max(0.0, float(budget_usd or 0.0))
        self._path = path
        self._lock = threading.RLock()
        self._on_warning = on_warning
        self._state = self._load()

    # ── persistence ───────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else _ledger_path()

    def _warn(self, message: str) -> None:
        try:
            if self._on_warning:
                self._on_warning(message)
            else:
                print(f"[era] [SPEND] {message}")
        except Exception:
            pass

    def _load(self) -> SpendState:
        path = self.path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SpendState()
        except Exception as e:
            # Loud, not silent: this resets today's recorded total, and the
            # user should know the ceiling is being measured from zero again.
            self._warn(f"ledger at {path} is unreadable ({e}); starting today from zero")
            return SpendState()

        if not isinstance(raw, dict):
            self._warn(f"ledger at {path} is not an object; starting today from zero")
            return SpendState()

        state = SpendState(
            date=str(raw.get("date") or _today()),
            total_usd=float(raw.get("total_usd") or 0.0),
            calls=int(raw.get("calls") or 0),
            by_alias={str(k): float(v) for k, v in (raw.get("by_alias") or {}).items()},
        )
        if state.date != _today():
            return SpendState()  # a new day; the old total is history
        return state

    def _save(self) -> None:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # A UNIQUE temp name, not a shared `<path>.tmp`. Writers inside one
            # process are serialised by the lock below, but a second Era
            # instance — or anything else pointed at the same ledger — would
            # collide on a fixed name, and on Windows the loser's os.replace
            # fails outright with "used by another process". Measured: 12
            # threads sharing one temp name lost 2448 of 3000 writes.
            tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(self._state.to_dict(), indent=1), encoding="utf-8")
            _atomic_replace(tmp, path)
        except Exception as e:
            # The in-memory total is the real guard, so a write failure costs
            # restart-continuity, not the ceiling.
            self._warn(f"could not persist the spend ledger ({e}); guard still active in-process")

    def _roll_if_new_day(self) -> None:
        if self._state.date != _today():
            self._state = SpendState()

    # ── the guard ─────────────────────────────────────────────────────────

    def today_total(self) -> float:
        with self._lock:
            self._roll_if_new_day()
            return self._state.total_usd

    def remaining(self) -> float:
        """Budget left today. Infinite when no budget is configured."""
        if not self.budget_usd:
            return float("inf")
        return max(0.0, self.budget_usd - self.today_total())

    def can_afford(self, estimate_usd: float) -> tuple[bool, str]:
        """May a call costing roughly this much proceed?

        Checks the PROJECTED total, not the current one — approving a call that
        is already known to breach the ceiling would make the budget advisory.
        """
        if not self.budget_usd:
            return True, ""
        estimate = max(0.0, float(estimate_usd or 0.0))
        with self._lock:
            self._roll_if_new_day()
            projected = self._state.total_usd + estimate
            if projected > self.budget_usd:
                return False, (
                    f"the daily model budget is spent — ${self._state.total_usd:.2f} "
                    f"of ${self.budget_usd:.2f} used, and this would add "
                    f"${estimate:.2f}"
                )
        return True, ""

    def record(self, amount_usd: float, *, alias: str = "") -> float:
        """Add a real cost. Returns the new day total."""
        amount = max(0.0, float(amount_usd or 0.0))
        with self._lock:
            self._roll_if_new_day()
            self._state.total_usd += amount
            self._state.calls += 1
            if alias:
                self._state.by_alias[alias] = self._state.by_alias.get(alias, 0.0) + amount
            self._save()
            return self._state.total_usd

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._roll_if_new_day()
            out = self._state.to_dict()
            out["budget_usd"] = self.budget_usd
            out["remaining_usd"] = self.remaining()
            out["path"] = str(self.path)
            return out

    def reset(self) -> None:
        """Clear today's total. For tests and for a deliberate manual override."""
        with self._lock:
            self._state = SpendState()
            self._save()


def from_registry() -> SpendLedger:
    """A ledger configured from `routing.daily_budget_usd`.

    Degrades to an unlimited ledger when the registry cannot be read — the
    caller is already gated on a feature flag, and failing to construct a
    budget object should not be a second way for dispatch to break.
    """
    budget = 0.0
    try:
        from core.llm.registry import routing_policy

        budget = float(routing_policy().get("daily_budget_usd") or 0.0)
    except Exception:
        budget = 0.0
    return SpendLedger(budget_usd=budget)
