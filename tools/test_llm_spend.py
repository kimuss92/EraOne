"""Tests for core/llm/spend.py — the daily budget guard.

WHAT THIS IS PROTECTING AGAINST
-------------------------------
A retry loop against a $0.06/call provider is a three-figure mistake that takes
minutes. The ceiling has to hold under the conditions that actually occur: a
crash mid-write, a corrupt file, concurrent threads, a day boundary, and a call
whose estimate would breach a budget that is not yet breached.

THE DIRECTION OF FAILURE IS THE DESIGN
--------------------------------------
The IN-MEMORY total is the guard; the file only carries it across a restart.
That is why a corrupt ledger starts the day at zero and logs loudly rather than
refusing every call — a bookkeeping problem should not brick the assistant, and
the cost of that choice is bounded because a runaway loop lives inside one
process where the in-memory total still trips. Both halves of that are tested.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.llm.spend import SpendLedger, SpendState, _today  # noqa: E402


@pytest.fixture
def ledger_path(tmp_path) -> Path:
    return tmp_path / "spend.json"


# ── the ceiling ───────────────────────────────────────────────────────────


def test_a_fresh_ledger_can_afford_a_call(ledger_path):
    ok, why = SpendLedger(budget_usd=2.0, path=ledger_path).can_afford(0.06)
    assert ok is True
    assert why == ""


def test_a_call_that_would_breach_the_ceiling_is_refused(ledger_path):
    """Checked against the PROJECTED total. Approving a call already known to
    breach it would make the budget advisory."""
    led = SpendLedger(budget_usd=1.0, path=ledger_path)
    led.record(0.98)
    ok, why = led.can_afford(0.06)
    assert ok is False
    assert "budget" in why


def test_a_call_that_exactly_reaches_the_ceiling_is_allowed(ledger_path):
    led = SpendLedger(budget_usd=1.0, path=ledger_path)
    led.record(0.94)
    assert led.can_afford(0.06)[0] is True


def test_no_budget_means_no_ceiling(ledger_path):
    led = SpendLedger(budget_usd=0.0, path=ledger_path)
    led.record(9999.0)
    assert led.can_afford(1000.0)[0] is True
    assert led.remaining() == float("inf")


def test_the_refusal_names_the_numbers(ledger_path):
    """A refusal the user cannot act on is just a failure."""
    led = SpendLedger(budget_usd=2.0, path=ledger_path)
    led.record(1.99)
    _ok, why = led.can_afford(0.06)
    assert "$1.99" in why and "$2.00" in why and "$0.06" in why


def test_remaining_never_goes_negative(ledger_path):
    led = SpendLedger(budget_usd=1.0, path=ledger_path)
    led.record(5.0)
    assert led.remaining() == 0.0


def test_a_negative_cost_is_clamped(ledger_path):
    """A provider reporting nonsense must not credit the budget back."""
    led = SpendLedger(budget_usd=1.0, path=ledger_path)
    led.record(0.5)
    led.record(-10.0)
    assert led.today_total() == pytest.approx(0.5)


# ── recording ─────────────────────────────────────────────────────────────


def test_totals_accumulate(ledger_path):
    led = SpendLedger(budget_usd=2.0, path=ledger_path)
    led.record(0.05)
    led.record(0.06)
    assert led.today_total() == pytest.approx(0.11)


def test_per_alias_totals_are_tracked(ledger_path):
    led = SpendLedger(budget_usd=2.0, path=ledger_path)
    led.record(0.05, alias="era.reason")
    led.record(0.20, alias="era.deep")
    led.record(0.05, alias="era.reason")
    stats = led.stats()
    assert stats["by_alias"]["era.reason"] == pytest.approx(0.10)
    assert stats["by_alias"]["era.deep"] == pytest.approx(0.20)


def test_call_count_is_tracked(ledger_path):
    led = SpendLedger(budget_usd=2.0, path=ledger_path)
    for _ in range(4):
        led.record(0.01)
    assert led.stats()["calls"] == 4


def test_stats_reports_the_budget_and_what_is_left(ledger_path):
    led = SpendLedger(budget_usd=2.0, path=ledger_path)
    led.record(0.50)
    stats = led.stats()
    assert stats["budget_usd"] == 2.0
    assert stats["remaining_usd"] == pytest.approx(1.5)


def test_reset_clears_the_day(ledger_path):
    led = SpendLedger(budget_usd=2.0, path=ledger_path)
    led.record(1.0)
    led.reset()
    assert led.today_total() == 0.0


# ── persistence ───────────────────────────────────────────────────────────


def test_the_total_survives_a_restart(ledger_path):
    """Otherwise relaunching Era hands back a fresh budget."""
    SpendLedger(budget_usd=2.0, path=ledger_path).record(0.42, alias="era.reason")
    reopened = SpendLedger(budget_usd=2.0, path=ledger_path)
    assert reopened.today_total() == pytest.approx(0.42)


def test_the_written_file_is_valid_json(ledger_path):
    SpendLedger(budget_usd=2.0, path=ledger_path).record(0.42, alias="era.reason")
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert data["total_usd"] == pytest.approx(0.42)
    assert data["date"] == _today()


def test_no_temp_file_is_left_behind(ledger_path):
    """The write is temp + atomic replace; a stray .tmp would mean it did not
    complete."""
    SpendLedger(budget_usd=2.0, path=ledger_path).record(0.1)
    assert list(ledger_path.parent.glob("*.tmp")) == []


def test_a_missing_file_starts_at_zero(ledger_path):
    assert SpendLedger(budget_usd=2.0, path=ledger_path).today_total() == 0.0


def test_a_corrupt_ledger_starts_at_zero_and_warns(ledger_path):
    """Loud, not silent: this resets the recorded total, and the user should
    know the ceiling is being measured from zero again."""
    ledger_path.write_text("{not json", encoding="utf-8")
    warnings: list[str] = []
    led = SpendLedger(budget_usd=2.0, path=ledger_path, on_warning=warnings.append)
    assert led.today_total() == 0.0
    assert warnings and "unreadable" in warnings[0]


def test_a_corrupt_ledger_does_not_brick_dispatch(ledger_path):
    """A bookkeeping problem must not refuse every call."""
    ledger_path.write_text("[]", encoding="utf-8")
    led = SpendLedger(budget_usd=2.0, path=ledger_path, on_warning=lambda _m: None)
    assert led.can_afford(0.06)[0] is True


def test_an_unwritable_path_still_guards_in_process(tmp_path):
    """The in-memory total is the real guard, so a write failure costs
    restart-continuity, not the ceiling."""
    unwritable = tmp_path / "nope.json"
    warnings: list[str] = []
    led = SpendLedger(budget_usd=1.0, path=unwritable, on_warning=warnings.append)
    # Make the parent a FILE so mkdir/replace cannot succeed.
    (tmp_path / "sub").write_text("x", encoding="utf-8")
    led._path = tmp_path / "sub" / "spend.json"
    led.record(0.99)
    assert led.can_afford(0.06)[0] is False, "the ceiling stopped holding"
    assert warnings


def test_yesterdays_total_does_not_count_against_today(ledger_path):
    stale = SpendState(date="2000-01-01", total_usd=99.0, calls=5)
    ledger_path.write_text(json.dumps(stale.to_dict()), encoding="utf-8")
    led = SpendLedger(budget_usd=2.0, path=ledger_path)
    assert led.today_total() == 0.0
    assert led.can_afford(0.06)[0] is True


def test_a_non_object_ledger_is_rejected(ledger_path):
    ledger_path.write_text('"a string"', encoding="utf-8")
    warnings: list[str] = []
    led = SpendLedger(budget_usd=2.0, path=ledger_path, on_warning=warnings.append)
    assert led.today_total() == 0.0
    assert warnings


def test_the_env_var_redirects_the_path(monkeypatch, tmp_path):
    """So a test never reads or writes the real ledger."""
    target = tmp_path / "custom.json"
    monkeypatch.setenv("ERA_LLM_SPEND_PATH", str(target))
    led = SpendLedger(budget_usd=1.0)
    led.record(0.1)
    assert target.is_file()


# ── the transient Windows lock ────────────────────────────────────────────


def test_a_transient_lock_on_the_destination_is_retried(monkeypatch, ledger_path):
    """os.replace can fail on Windows through no fault of ours.

    If ANY handle is open on the destination at that instant — antivirus, the
    search indexer, a concurrent reader — it raises WinError 5 or 32. Measured
    at ~1 in 1200 writes under thread pressure, which is rare enough to read as
    a flaky test and frequent enough that a long-running Era would silently stop
    persisting its spend total.
    """
    import core.llm.spend as spend_mod

    calls = {"n": 0}
    real_replace = spend_mod.os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:  # fail twice, then succeed
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(spend_mod.os, "replace", flaky_replace)
    warnings: list[str] = []
    led = SpendLedger(budget_usd=1.0, path=ledger_path, on_warning=warnings.append)
    led.record(0.25)

    assert warnings == [], f"a retryable lock was reported as a failure: {warnings}"
    assert calls["n"] == 3, "the replace was not retried"
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["total_usd"] == pytest.approx(0.25)


def test_a_persistent_lock_still_reports_rather_than_hanging(monkeypatch, ledger_path):
    """The retry is bounded. A destination locked forever must give up and warn,
    not spin — the in-memory guard is what actually protects the budget."""
    import core.llm.spend as spend_mod

    def always_locked(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(spend_mod.os, "replace", always_locked)
    warnings: list[str] = []
    led = SpendLedger(budget_usd=1.0, path=ledger_path, on_warning=warnings.append)
    led.record(0.99)

    assert warnings, "a permanent lock was swallowed silently"
    assert led.can_afford(0.10)[0] is False, "the in-process ceiling stopped holding"


def test_the_retry_is_quick_enough_to_be_invisible():
    """~35ms worst case, against a model call measured at ~2900ms."""
    from core.llm.spend import _REPLACE_ATTEMPTS, _REPLACE_BACKOFF_S

    worst = sum(_REPLACE_BACKOFF_S * (i + 1) for i in range(_REPLACE_ATTEMPTS - 1))
    assert worst < 0.1, f"the retry ladder costs {worst * 1000:.0f}ms"


# ── concurrency ───────────────────────────────────────────────────────────


def test_concurrent_records_keep_the_ledger_intact(ledger_path):
    """Concurrency must not break PERSISTENCE.

    The obvious assertion — "no counter updates are lost" — cannot detect a
    missing lock at all: CPython's GIL makes `total += amount` effectively
    atomic here, and 12 threads x 250 records lost exactly zero either way
    (measured, not assumed). What DOES break without serialised writes is the
    save: threads racing on os.replace lost 2448 of 3000 writes on Windows
    ("used by another process"), so the persisted total silently stopped
    tracking reality while every in-memory number looked perfect.

    So this asserts the thing that actually breaks: no write failures, and a
    reopened ledger sees every cent.
    """
    warnings: list[str] = []
    led = SpendLedger(budget_usd=1_000_000.0, path=ledger_path, on_warning=warnings.append)
    threads_n, per_thread = 12, 100
    barrier = threading.Barrier(threads_n)

    def worker() -> None:
        barrier.wait()
        for _ in range(per_thread):
            led.record(0.01, alias="era.reason")

    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force preemption inside the critical section
    try:
        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(previous_interval)

    expected = threads_n * per_thread
    assert warnings == [], f"{len(warnings)} write(s) failed under concurrency"
    assert led.stats()["calls"] == expected
    assert led.today_total() == pytest.approx(expected * 0.01)

    reopened = SpendLedger(budget_usd=1_000_000.0, path=ledger_path)
    assert reopened.today_total() == pytest.approx(expected * 0.01), (
        "the persisted total drifted from the in-memory one"
    )


def test_no_temp_files_survive_concurrent_writes(ledger_path):
    """Unique temp names must still be cleaned up by the atomic replace."""
    led = SpendLedger(budget_usd=1000.0, path=ledger_path, on_warning=lambda _m: None)
    threads = [
        threading.Thread(target=lambda: [led.record(0.01) for _ in range(50)]) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert list(ledger_path.parent.glob("*.tmp")) == []
