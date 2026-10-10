"""Cancelling a running task: the HUD row, the late result, the side effects.

Everything here runs against stubs. No EraLive is constructed -- a real one
holds ~/.era/era.lock and would make the suite look like a 30-minute hang --
and every HudBus is a fresh instance rather than the `hud` singleton, so these
tests cannot pollute each other or anything else in the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.era_live.cancel import CANCEL_ACK, CancelMixin  # noqa: E402
from app.hud.events import HudBus  # noqa: E402
from core.cancel_registry import CancelRegistry  # noqa: E402
from core.llm.exec_ledger import ExecLedger  # noqa: E402
from core.pending_confirmation import PendingConfirmationStore  # noqa: E402
from core.tool_result import ToolResult  # noqa: E402


def _bus() -> tuple[HudBus, list[dict]]:
    """A fresh bus with its emitted events captured."""
    bus = HudBus()
    seen: list[dict] = []
    bus.attach(lambda msg: seen.append(msg))
    return bus, seen


def _types(seen: list[dict]) -> list[str]:
    return [m["event"]["type"] for m in seen if m.get("type") == "event"]


# ── the HUD row ──────────────────────────────────────────────────────────


def test_tool_cancelled_closes_the_row_and_emits_once():
    bus, seen = _bus()
    cid = bus.tool_started("c1", "spotify_control", {"action": "search_and_play"})
    seen.clear()

    bus.tool_cancelled(cid, "Cancelled by you.")

    # Exactly one cancel event. The agent.state.changed alongside it is the
    # bus re-deriving its state now that nothing is running -- wanted, not noise.
    assert _types(seen).count("tool.cancelled") == 1
    row = next(r for r in bus.recent_events(50) if r["type"] == "tool.cancelled")
    assert row["metadata"]["status"] == "cancelled"
    assert row["metadata"]["summary"] == "Cancelled by you."
    assert row["metadata"]["duration"] is not None


def test_a_cancelled_row_drops_out_of_active_tools():
    """The HUD's "N RUNNING" chip and the global button both read this."""
    bus, _ = _bus()
    cid = bus.tool_started("c1", "web_search", {"query": "x"})
    assert [r["id"] for r in bus.active_tools()] == [cid]
    bus.tool_cancelled(cid)
    assert bus.active_tools() == []


def test_a_late_result_for_a_cancelled_row_is_ignored():
    """THE soft-cancel contract.

    Most tool bodies sit in run_in_executor, a thread Python cannot interrupt,
    so the work finishes anyway -- up to 76s later for code_helper. The row the
    owner abandoned must stay terminal and the result must not be reported.
    """
    bus, seen = _bus()
    cid = bus.tool_started("c1", "spotify_control", {"query": "hard track"})
    bus.tool_cancelled(cid)
    seen.clear()

    bus.tool_finished(
        cid, "spotify_control", {"query": "hard track"}, ToolResult.ok(message="Playing")
    )

    assert _types(seen) == [], "a cancelled row must not be reopened by its own late result"
    assert bus._tools[cid]["status"] == "cancelled"
    assert bus._tools[cid]["summary"] != "Playing"


def test_cancelling_an_already_finished_row_does_nothing():
    bus, seen = _bus()
    cid = bus.tool_started("c1", "web_search", {"query": "x"})
    bus.tool_finished(cid, "web_search", {"query": "x"}, ToolResult.ok(message="Found it"))
    seen.clear()

    bus.tool_cancelled(cid)

    assert _types(seen) == []
    assert bus._tools[cid]["status"] == "success"


def test_cancelling_an_unknown_row_is_silent():
    bus, seen = _bus()
    bus.tool_cancelled("never-existed")
    assert _types(seen) == []


# ── the ledger: a cancelled call must stay retryable ─────────────────────


def test_a_cancelled_call_reopens_its_ledger_claim():
    """Otherwise the owner's correction comes back as "already ran".

    tool_batch caches a completed call's result and refuses a repeat of the
    same key inside one request. If a cancel were recorded as completed, the
    re-ask would replay the cancel ack instead of running -- the precise
    failure this feature exists to remove. ledger.fail reopens the key.
    """
    ledger = ExecLedger()
    args = {"action": "search_and_play", "query": "hard track"}

    first = ledger.begin("req-1", "spotify_control", args, ordinal=0)
    assert first.execute is True
    ledger.fail(first, "cancelled")

    again = ledger.begin("req-1", "spotify_control", args, ordinal=0)
    assert again.execute is True, "a cancelled call must stay retryable"


def test_a_completed_call_is_still_deduped():
    """The guard above must not weaken replay suppression for normal calls."""
    ledger = ExecLedger()
    args = {"action": "send", "to": "someone@example.com"}

    first = ledger.begin("req-1", "gmail_control", args, ordinal=0)
    ledger.complete(first, "sent")

    again = ledger.begin("req-1", "gmail_control", args, ordinal=0)
    assert again.execute is False, "a completed send must never run twice"


# ── CancelMixin ──────────────────────────────────────────────────────────


class FakeEra(CancelMixin):
    """The smallest surface CancelMixin touches."""

    def __init__(self, store: PendingConfirmationStore | None = None) -> None:
        self._loop = None  # no engine loop: _finalize_cancel is driven directly
        self._pending_store = store
        self.logs: list[str] = []
        self.spoken: list[str] = []
        self.ui = SimpleNamespace(write_log=self.logs.append)

    def speak(self, text: str) -> None:
        self.spoken.append(text)


def test_cancel_task_reports_whether_anything_was_cancelled(monkeypatch):
    import app.era_live.cancel as cancel_mod

    reg = CancelRegistry()
    monkeypatch.setattr(cancel_mod, "REGISTRY", reg)
    era = FakeEra()

    assert era.cancel_task("missing") is False, "nothing to cancel is not a failure"

    reg.register("c1", "spotify_control")
    assert era.cancel_task("c1") is True
    assert era.cancel_task("c1") is False, "a second click must not re-cancel"


def test_cancel_current_task_targets_the_newest(monkeypatch):
    import app.era_live.cancel as cancel_mod

    reg = CancelRegistry()
    monkeypatch.setattr(cancel_mod, "REGISTRY", reg)
    era = FakeEra()

    reg.register("old", "web_search")
    reg.register("new", "spotify_control")
    assert era.cancel_current_task("voice") is True
    assert reg.get("new").cancelled() is True
    assert reg.get("old").cancelled() is False


def test_has_running_task_tracks_the_registry(monkeypatch):
    import app.era_live.cancel as cancel_mod

    reg = CancelRegistry()
    monkeypatch.setattr(cancel_mod, "REGISTRY", reg)
    era = FakeEra()

    assert era.has_running_task() is False
    reg.register("c1", "web_search")
    assert era.has_running_task() is True
    reg.cancel("c1", source="hud")
    assert era.has_running_task() is False, "already cancelled is not still running"


def test_cancelling_releases_the_confirmation_that_call_armed(monkeypatch):
    """core/pending_confirmation.py holds exactly ONE question.

    Leaving a cancelled call's question in the slot means the owner's next
    action is refused as busy, and a later "yes" meant for something else runs
    the abandoned action. Same release agent/task_queue.cancel performs.
    """
    import app.era_live.cancel as cancel_mod

    reg = CancelRegistry()
    monkeypatch.setattr(cancel_mod, "REGISTRY", reg)
    store = PendingConfirmationStore()
    era = FakeEra(store)

    token = reg.register("c1", "file_controller")
    token.confirmation_id = store.store(
        tool="file_controller",
        parameters={"action": "delete", "path": "C:/tmp/example.txt"},
        risk_level="HIGH",
        reason="deletes a file",
    )
    assert store.peek() is not None

    era.cancel_task("c1")

    assert store.peek() is None, "the slot must be free for the owner's next order"


def test_cancelling_leaves_someone_elses_confirmation_alone(monkeypatch):
    import app.era_live.cancel as cancel_mod

    reg = CancelRegistry()
    monkeypatch.setattr(cancel_mod, "REGISTRY", reg)
    store = PendingConfirmationStore()
    era = FakeEra(store)

    store.store(
        tool="gmail_control",
        parameters={"action": "send", "to": "someone@example.com"},
        risk_level="HIGH",
        reason="sends mail",
    )
    reg.register("c1", "web_search")  # a DIFFERENT call, armed nothing

    era.cancel_task("c1")

    assert store.peek() is not None, "an unrelated question must survive"


# ── the ack policy ───────────────────────────────────────────────────────


def test_a_spoken_cancel_gets_a_spoken_ack():
    reg = CancelRegistry()
    era = FakeEra()
    token = reg.register("c1", "spotify_control")
    reg.cancel("c1", source="voice")

    era._finalize_cancel(token)

    # Routed through _say_exactly, which wraps it in the "say exactly this and
    # nothing more" note -- so the model cannot embellish a cancel ack.
    assert len(era.spoken) == 1
    assert CANCEL_ACK in era.spoken[0]
    assert "nothing more" in era.spoken[0], "the ack must stay pinned verbatim"


def test_a_clicked_cancel_stays_silent():
    """The row going grey IS the feedback; narrating it is noise."""
    reg = CancelRegistry()
    era = FakeEra()
    token = reg.register("c1", "spotify_control")
    reg.cancel("c1", source="hud")

    era._finalize_cancel(token)

    assert era.spoken == []


def test_an_abort_does_not_ack_twice():
    """panic.py already said ABORT_ACK; repeating it recreates the noise."""
    reg = CancelRegistry()
    era = FakeEra()
    token = reg.register("c1", "netflix_control")
    reg.cancel("c1", source="abort")

    era._finalize_cancel(token)

    assert era.spoken == []


def test_a_correction_does_not_ack_either():
    """She is about to do the corrected thing -- that is the confirmation."""
    reg = CancelRegistry()
    era = FakeEra()
    token = reg.register("c1", "spotify_control")
    reg.cancel("c1", source="correction")

    era._finalize_cancel(token)

    assert era.spoken == []


def test_finalize_cancels_a_detached_background_task():
    reg = CancelRegistry()
    era = FakeEra()
    token = reg.register("c1", "crew")
    cancelled: list[bool] = []
    token.task = SimpleNamespace(cancel=lambda: cancelled.append(True))
    reg.cancel("c1", source="hud")

    era._finalize_cancel(token)

    assert cancelled == [True]


def test_finalize_survives_a_broken_ui_and_a_broken_speak():
    """A cancel that half-fails because one subsystem is wedged defeats it."""

    def boom(*_args):
        raise RuntimeError("wedged")

    reg = CancelRegistry()
    era = FakeEra()
    era.ui = SimpleNamespace(write_log=boom)
    era.speak = boom
    token = reg.register("c1", "web_search")
    reg.cancel("c1", source="voice")

    era._finalize_cancel(token)  # must not raise
