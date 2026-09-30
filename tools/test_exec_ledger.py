"""Tests for core/llm/exec_ledger.py — the tool-execution idempotency ledger.

The property this module exists to guarantee: a replayed request cannot run
`send_message` (or a shutdown) a second time, while a genuine second ask still
works. Most of what follows is about that second half — a dedupe that eats real
commands would be a worse bug than the one it prevents.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.llm.exec_ledger import (  # noqa: E402
    DUPLICATE,
    IN_FLIGHT,
    NEW,
    Claim,
    ExecLedger,
    assign_ordinals,
    canonical_args,
    derive_request_id,
    signature,
)


class FakeClock:
    """Manual clock so TTL behaviour is tested, not waited on."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def ledger() -> ExecLedger:
    return ExecLedger()


# ── canonical_args / signature ────────────────────────────────────────────


def test_canonical_args_is_key_order_independent():
    assert canonical_args({"a": 1, "b": 2}) == canonical_args({"b": 2, "a": 1})


def test_canonical_args_distinguishes_values():
    assert canonical_args({"volume": 10}) != canonical_args({"volume": 20})


def test_canonical_args_never_raises_on_unserializable():
    """A ledger that throws on an odd argument takes down the call it protects."""

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    assert canonical_args({"obj": Weird()})  # no exception, non-empty


def test_canonical_args_survives_a_repr_that_explodes():
    """json's default=str fails here, and so does the repr() fallback. The
    function must still not propagate: a malformed tool argument becoming a
    crash in the live voice path would be worse than any dedupe it costs."""

    class Hostile:
        def __repr__(self):
            raise RuntimeError("nope")

    try:
        canonical_args({"x": Hostile()})
    except Exception as exc:  # pragma: no cover - documents the failure
        pytest.fail(f"canonical_args raised {exc!r}")


def test_uncanonicalizable_args_fail_open_rather_than_collide():
    """Two DIFFERENT calls that cannot be canonicalized must not hash alike.

    A shared placeholder would make the ledger suppress the second as a
    duplicate of the first — silently dropping a real action. Failing open
    (both execute) is the only safe direction to be wrong in.
    """

    class Hostile:
        def __repr__(self):
            raise RuntimeError("nope")

    assert canonical_args({"x": Hostile()}) != canonical_args({"y": Hostile()})


def test_uncanonicalizable_args_always_execute():
    class Hostile:
        def __repr__(self):
            raise RuntimeError("nope")

    led = ExecLedger()
    led.complete(led.begin("r1", "t", {"x": Hostile()}))
    assert led.begin("r1", "t", {"x": Hostile()}).execute is True


def test_signature_differs_by_tool_name():
    assert signature("volume_up", {}) != signature("volume_down", {})


def test_signature_is_stable_across_calls():
    assert signature("send_message", {"to": "x"}) == signature("send_message", {"to": "x"})


def test_signature_cannot_be_confused_by_delimiter_injection():
    """Tool name and args are separated by NUL, so neither can spoof the other."""
    assert signature("a", {"b": 1}) != signature('a\x00{"b": 1}', {})


# ── assign_ordinals ───────────────────────────────────────────────────────


def test_assign_ordinals_distinguishes_genuine_repeats():
    """'volume up, volume up' is two real actions, not a duplicate."""
    assert assign_ordinals([("volume_up", {}), ("volume_up", {})]) == [0, 1]


def test_assign_ordinals_is_per_signature_not_positional():
    """Interleaving must not shift a repeat's ordinal — that is what makes a
    replayed batch map call-for-call onto the original."""
    calls = [("volume_up", {}), ("mute", {}), ("volume_up", {})]
    assert assign_ordinals(calls) == [0, 0, 1]


def test_assign_ordinals_separates_same_tool_different_args():
    calls = [("set_volume", {"v": 1}), ("set_volume", {"v": 2})]
    assert assign_ordinals(calls) == [0, 0]


def test_assign_ordinals_empty():
    assert assign_ordinals([]) == []


def test_assign_ordinals_is_deterministic_for_a_replay():
    calls = [("a", {"x": 1}), ("b", {}), ("a", {"x": 1}), ("a", {"x": 1})]
    assert assign_ordinals(calls) == assign_ordinals(list(calls)) == [0, 0, 1, 2]


# ── derive_request_id ─────────────────────────────────────────────────────


def test_derive_request_id_is_stable_for_the_same_ids():
    assert derive_request_id(["a", "b"]) == derive_request_id(["a", "b"])


def test_derive_request_id_ignores_order():
    """A replay may deliver the same calls in a different order."""
    assert derive_request_id(["a", "b"]) == derive_request_id(["b", "a"])


def test_derive_request_id_differs_for_a_fresh_generation():
    """New call ids mean a new ask, which must be free to act again."""
    assert derive_request_id(["a", "b"]) != derive_request_id(["c", "d"])


def test_derive_request_id_falls_back_when_ids_are_missing():
    assert derive_request_id([None, ""]) == "anonymous"
    assert derive_request_id([]) == "anonymous"


def test_derive_request_id_custom_fallback():
    assert derive_request_id([None], fallback="turn-7") == "turn-7"


# ── begin / complete / fail ───────────────────────────────────────────────


def test_first_call_is_new_and_executes(ledger):
    claim = ledger.begin("r1", "send_message", {"to": "bob"})
    assert claim.status == NEW
    assert claim.execute is True


def test_replay_after_completion_is_refused(ledger):
    """The whole point: the second delivery must not send the message again."""
    first = ledger.begin("r1", "send_message", {"to": "bob"})
    ledger.complete(first, "sent")

    second = ledger.begin("r1", "send_message", {"to": "bob"})
    assert second.status == DUPLICATE
    assert second.execute is False
    assert second.cached_result == "sent"


def test_concurrent_delivery_sees_in_flight(ledger):
    ledger.begin("r1", "send_message", {"to": "bob"})
    second = ledger.begin("r1", "send_message", {"to": "bob"})
    assert second.status == IN_FLIGHT
    assert second.execute is False


def test_failure_reopens_the_key(ledger):
    """A transient error must stay retryable — otherwise one blip disables the
    action for the rest of the request."""
    first = ledger.begin("r1", "web_search", {"q": "x"})
    ledger.fail(first, "network down")

    second = ledger.begin("r1", "web_search", {"q": "x"})
    assert second.status == NEW
    assert second.execute is True
    assert second.attempts == 2


def test_retry_after_failure_can_then_complete(ledger):
    first = ledger.begin("r1", "web_search", {"q": "x"})
    ledger.fail(first, "boom")
    second = ledger.begin("r1", "web_search", {"q": "x"})
    ledger.complete(second, "results")

    third = ledger.begin("r1", "web_search", {"q": "x"})
    assert third.status == DUPLICATE
    assert third.cached_result == "results"


def test_different_requests_do_not_dedupe(ledger):
    """A genuine second ask in a new turn must act."""
    ledger.complete(ledger.begin("r1", "volume_up", {}))
    claim = ledger.begin("r2", "volume_up", {})
    assert claim.execute is True


def test_ordinals_let_a_repeat_in_one_batch_both_run(ledger):
    ledger.complete(ledger.begin("r1", "volume_up", {}, ordinal=0))
    second = ledger.begin("r1", "volume_up", {}, ordinal=1)
    assert second.execute is True


def test_replayed_batch_dedupes_both_repeats(ledger):
    """The same batch delivered twice must run zero of it the second time."""
    for ordinal in (0, 1):
        ledger.complete(ledger.begin("r1", "volume_up", {}, ordinal=ordinal))

    replay = [ledger.begin("r1", "volume_up", {}, ordinal=o) for o in (0, 1)]
    assert [c.execute for c in replay] == [False, False]


def test_complete_without_a_result_stores_none(ledger):
    claim = ledger.begin("r1", "t", {})
    ledger.complete(claim)
    assert ledger.begin("r1", "t", {}).cached_result is None


def test_complete_on_an_unknown_claim_is_ignored_not_raised(ledger):
    """Degrade, never raise — this runs in the live path."""
    stray = Claim("ghost", "key", "t", 0, NEW)
    ledger.complete(stray, "x")
    ledger.fail(stray, "y")
    assert ledger.stats()["stale_completions"] == 2


def test_double_complete_is_ignored(ledger):
    claim = ledger.begin("r1", "t", {})
    ledger.complete(claim, "first")
    ledger.complete(claim, "second")
    assert ledger.begin("r1", "t", {}).cached_result == "first"


def test_fail_after_complete_does_not_reopen(ledger):
    """A late failure report must not un-do a recorded success."""
    claim = ledger.begin("r1", "send_message", {})
    ledger.complete(claim, "sent")
    ledger.fail(claim, "late error")
    assert ledger.begin("r1", "send_message", {}).execute is False


# ── bookkeeping ───────────────────────────────────────────────────────────


def test_forget_drops_a_request(ledger):
    ledger.complete(ledger.begin("r1", "t", {}))
    assert ledger.forget("r1") is True
    assert ledger.begin("r1", "t", {}).execute is True


def test_forget_unknown_returns_false(ledger):
    assert ledger.forget("nope") is False


def test_clear_drops_everything(ledger):
    ledger.complete(ledger.begin("r1", "t", {}))
    ledger.clear()
    assert ledger.stats()["tracked_requests"] == 0


def test_stats_counts_executions_and_duplicates(ledger):
    ledger.complete(ledger.begin("r1", "t", {}))
    ledger.begin("r1", "t", {})
    stats = ledger.stats()
    assert stats["executed"] == 1
    assert stats["duplicates"] == 1
    assert stats["tracked_requests"] == 1
    assert stats["tracked_calls"] == 1


def test_describe_reports_entry_state(ledger):
    claim = ledger.begin("r1", "send_message", {})
    ledger.fail(claim, "boom")
    rows = ledger.describe("r1")
    assert len(rows) == 1
    assert rows[0]["tool"] == "send_message"
    assert rows[0]["state"] == "failed"
    assert rows[0]["error"] == "boom"


def test_describe_unknown_request_is_empty(ledger):
    assert ledger.describe("nope") == []


def test_error_text_is_truncated(ledger):
    claim = ledger.begin("r1", "t", {})
    ledger.fail(claim, "x" * 5000)
    assert len(ledger.describe("r1")[0]["error"]) == 500


# ── bounds ────────────────────────────────────────────────────────────────


def test_ttl_expires_a_request():
    clock = FakeClock()
    led = ExecLedger(ttl_seconds=100.0, clock=clock)
    led.complete(led.begin("r1", "t", {}))

    clock.advance(101.0)
    assert led.begin("r1", "t", {}).execute is True
    assert led.stats()["expired_requests"] == 1


def test_ttl_does_not_expire_early():
    clock = FakeClock()
    led = ExecLedger(ttl_seconds=100.0, clock=clock)
    led.complete(led.begin("r1", "t", {}))

    clock.advance(99.0)
    assert led.begin("r1", "t", {}).execute is False


def test_max_requests_evicts_oldest_first():
    led = ExecLedger(max_requests=2)
    for rid in ("r1", "r2", "r3"):
        led.complete(led.begin(rid, "t", {}))

    assert led.begin("r1", "t", {}).execute is True  # evicted
    assert led.begin("r3", "t", {}).execute is False  # retained
    assert led.stats()["evicted_requests"] >= 1


def test_touching_a_request_protects_it_from_eviction():
    """Recency, not insertion order — an active request must not be evicted
    out from under an in-progress batch."""
    led = ExecLedger(max_requests=2)
    led.complete(led.begin("r1", "t", {}))
    led.complete(led.begin("r2", "t", {}))
    led.begin("r1", "other", {})  # touch r1
    led.complete(led.begin("r3", "t", {}))

    assert led.begin("r1", "t", {}).execute is False  # survived
    assert led.begin("r2", "t", {}).execute is True  # evicted


def test_rejects_nonsense_bounds():
    with pytest.raises(ValueError):
        ExecLedger(max_requests=0)
    with pytest.raises(ValueError):
        ExecLedger(ttl_seconds=0)


# ── concurrency ───────────────────────────────────────────────────────────


def test_only_one_thread_wins_the_claim():
    """Tools run in threads; two of them must never both execute one call."""
    led = ExecLedger()
    winners: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()
        claim = led.begin("r1", "send_message", {"to": "bob"})
        with lock:
            winners.append(claim.execute)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(winners) == 1, f"expected exactly one executor, got {sum(winners)}"


def test_parallel_distinct_keys_all_execute():
    """The lock must not serialize away legitimate distinct work."""
    led = ExecLedger()
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        claim = led.begin("r1", "tool", {"i": i})
        with lock:
            results.append(claim.execute)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results)
    assert len(results) == 16
