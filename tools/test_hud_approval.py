"""HUD approval state: it mirrors the REAL pending confirmation and nothing else.

Every test gets its own PendingConfirmationStore (the process-wide one is never
touched), patched in where app/hud/bus_approval.py looks it up.
"""

import time

import pytest

import app.hud.bus_approval as bus_approval
from app.hud.events import HudBus
from core.pending_confirmation import PendingConfirmationStore
from core.tool_result import ToolResult


@pytest.fixture
def store(monkeypatch):
    s = PendingConfirmationStore()
    monkeypatch.setattr("core.pending_confirmation.get_shared_store", lambda: s)
    return s


@pytest.fixture
def bus(store):
    b = HudBus()
    sent: list[dict] = []
    b.attach(sent.append)
    b.sent = sent  # type: ignore[attr-defined]
    b.connection("Connected")
    return b


def events(bus, etype):
    return [
        m["event"] for m in bus.sent if m.get("type") == "event" and m["event"]["type"] == etype
    ]


GATED = ToolResult(success=False, error="needs ok", error_type="ConfirmationRequired")


def arm(bus, store, tool="computer_settings", params=None, risk="HIGH"):
    store.store(tool, params or {"action": "restart"}, risk, "Restarting the computer.")
    cid = bus.tool_started(None, tool, params or {"action": "restart"})
    bus.tool_finished(cid, tool, params or {"action": "restart"}, GATED)


def test_a_real_pending_confirmation_becomes_the_approval_state(bus, store):
    arm(bus, store)
    assert bus.state()["state"] == "awaiting_approval"
    req = events(bus, "approval.requested")
    assert len(req) == 1
    meta = req[0]["metadata"]
    assert meta["tool"] == "computer_settings"
    assert meta["risk"] == "HIGH"
    assert meta["reason"] == "Restarting the computer."
    assert meta["expiresAt"] > time.time()
    assert bus.snapshot()["approval"]["id"] == meta["id"]


def test_no_pending_confirmation_means_no_approval_state(bus, store):
    # The result SAYS it needs confirmation, but nothing is armed: show nothing.
    cid = bus.tool_started(None, "send_message", {"message": "hi"})
    bus.tool_finished(cid, "send_message", {}, GATED)
    assert bus.state()["state"] != "awaiting_approval"
    assert not events(bus, "approval.requested")
    assert bus.snapshot()["approval"] is None


def test_raw_parameters_never_reach_the_hud(bus, store):
    arm(bus, store, tool="send_message", params={"message": "hi", "api_key": "sk-SECRET-123"})
    public = str(bus.snapshot()["approval"]) + str(events(bus, "approval.requested"))
    assert "sk-SECRET-123" not in public
    assert "parameters" not in bus.snapshot()["approval"]


def test_confirming_clears_the_approval(bus, store):
    arm(bus, store)
    store.confirm("", allow_sole=True)
    cid = bus.tool_started(None, "confirm_pending_confirmation", {})
    bus.tool_finished(
        cid, "confirm_pending_confirmation", {}, ToolResult(success=True, message="done")
    )
    assert events(bus, "approval.resolved")
    assert bus.state()["state"] != "awaiting_approval"
    assert bus.snapshot()["approval"] is None


def test_a_resolution_on_another_path_is_picked_up_by_the_recheck(bus, store, monkeypatch):
    monkeypatch.setattr(bus_approval, "APPROVAL_RECHECK_S", 0.05)
    arm(bus, store)
    # The store listener normally settles this at once; detach it to prove the
    # recheck still catches a resolution on its own (the fallback path).
    bus._confirmation_unsubscribe()
    store.clear()  # e.g. expired, or cancelled by the agent executor — no tool event here
    with bus._lock:
        bus._approval["_next"] = time.monotonic()  # due now
        bus._poke()
    deadline = time.time() + 2
    while bus.state()["state"] == "awaiting_approval" and time.time() < deadline:
        time.sleep(0.02)
    assert bus.state()["state"] != "awaiting_approval"
    assert events(bus, "approval.resolved")


def _row(bus, tool):
    return next(r for r in bus._tools.values() if r["tool"] == tool)


@pytest.mark.parametrize(
    ("resolve", "status"),
    [
        (lambda s: s.confirm("", allow_sole=True), "success"),
        (lambda s: s.cancel(s.peek().confirmation_id), "cancelled"),
        (lambda s: s.clear(), "cancelled"),
    ],
)
def test_a_resolved_confirmation_settles_its_needs_approval_row(bus, store, resolve, status):
    """2026-10-16: the 'waive close confirmation' row kept NEEDS APPROVAL forever."""
    arm(bus, store)
    assert _row(bus, "computer_settings")["status"] == "awaiting"
    resolve(store)  # store listener fires at once — no recheck wait
    assert _row(bus, "computer_settings")["status"] == status
    assert bus.state()["state"] != "awaiting_approval"


def test_an_expired_confirmation_settles_its_row_as_expired(bus, store):
    arm(bus, store)
    with store._lock:
        store._pending.expires_at = time.time() - 1
    assert store.peek() is None  # expiry fires the listener
    row = _row(bus, "computer_settings")
    assert row["status"] == "cancelled" and "expired" in row["summary"].lower()


def test_the_live_row_is_not_settled_while_its_confirmation_is_armed(bus, store):
    arm(bus, store)
    cid = bus.tool_started(None, "weather_report", {"city": "Tunis"})
    bus.tool_finished(cid, "weather_report", {}, ToolResult(success=True, message="sunny"))
    assert _row(bus, "computer_settings")["status"] == "awaiting"


def test_the_users_spoken_answer_still_shows_as_live_speech(bus, store):
    arm(bus, store)
    bus.transcript_partial("yes")
    assert bus.state()["state"] == "transcribing"
    assert bus.snapshot()["approval"] is not None  # still pending underneath
