"""HUD approval card: clears itself after 15 s without touching the confirmation.

The mixin is driven through a tiny fake host, a fake clock and a fake store
lookup, so the process-wide PendingConfirmationStore is never used.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.hud import bus_approval as ba


class Host(ba.ApprovalMixin):
    def __init__(self):
        self._approval = None
        self.events = []

    def _emit(self, kind, **kw):
        self.events.append((kind, kw))

    def kinds(self):
        return [k for k, _ in self.events]


def _pending(cid="c1"):
    return SimpleNamespace(
        confirmation_id=cid,
        tool="file_controller",
        parameters={"path": "/secret"},
        reason="delete a file",
        risk_level="high",
        created_at=0.0,
        expires_at=9999.0,
    )


@pytest.fixture
def env(monkeypatch):
    clock = {"t": 100.0}
    state = {"p": None}
    monkeypatch.setattr(ba, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    monkeypatch.setattr(ba, "_live_pending", lambda: state["p"])
    monkeypatch.setattr(
        ba.tool_adapter,
        "describe",
        lambda tool, params: {"label": "Files", "activity": "Delete a file", "category": "files"},
    )
    return Host(), clock, state


def test_card_is_visible_before_the_timeout(env):
    host, clock, state = env
    state["p"] = _pending()
    host._sync_approval()
    clock["t"] += ba.APPROVAL_VISIBLE_S - 1
    host._sync_approval()
    assert host.approval_public() is not None
    assert host._approval_label().startswith("Approval required")
    assert host.kinds() == ["approval.requested"]


def test_card_clears_after_15_seconds_but_confirmation_stays_armed(env):
    host, clock, state = env
    assert ba.APPROVAL_VISIBLE_S == 15.0
    state["p"] = _pending()
    host._sync_approval()
    clock["t"] += 15.0
    host._sync_approval()
    assert host.approval_public() is None
    assert host._approval_label() == ""
    kind, kw = host.events[-1]
    assert kind == "approval.resolved"
    assert kw["metadata"] == {"id": "c1", "reason": "display_timeout"}
    # The store was never touched, so the user's own answer can still resolve it.
    assert state["p"] is not None
    # Further syncs neither re-show the card nor re-emit anything.
    clock["t"] += 30.0
    host._sync_approval()
    assert host.approval_public() is None
    assert host.kinds() == ["approval.requested", "approval.resolved"]


def test_deadline_wakes_the_bus_at_the_hide_time(env):
    host, clock, state = env
    state["p"] = _pending()
    host._sync_approval()
    assert host._approval_next_deadline() <= clock["t"] + ba.APPROVAL_VISIBLE_S
    clock["t"] += 15.0
    host._sync_approval()
    # Hidden: still re-checking the store, no longer waking for the display timer.
    assert host._approval_next_deadline() == clock["t"] + ba.APPROVAL_RECHECK_S


def test_resolution_while_hidden_emits_no_duplicate(env):
    host, clock, state = env
    state["p"] = _pending()
    host._sync_approval()
    clock["t"] += 15.0
    host._sync_approval()
    state["p"] = None
    host._sync_approval()
    assert host._approval is None
    assert host.kinds().count("approval.resolved") == 1


def test_a_new_confirmation_after_the_clear_is_shown_again(env):
    host, clock, state = env
    state["p"] = _pending("c1")
    host._sync_approval()
    clock["t"] += 15.0
    host._sync_approval()
    state["p"] = _pending("c2")
    host._sync_approval()
    shown = host.approval_public()
    assert shown is not None and shown["id"] == "c2"
    assert host.kinds()[-1] == "approval.requested"
    clock["t"] += 14.0
    host._sync_approval()
    assert host.approval_public() is not None


def test_raw_parameters_still_never_published(env):
    host, _clock, state = env
    state["p"] = _pending()
    host._sync_approval()
    assert "/secret" not in repr(host.approval_public())
    assert all(not k.startswith("_") for k in host.approval_public())
