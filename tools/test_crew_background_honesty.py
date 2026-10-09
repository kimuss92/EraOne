"""The crew `ask` runs off the receive loop and always reports its real outcome.

Regression for 2026-10-06: a crew `ask` was awaited inline on the Gemini Live
receive loop. A crew turn runs 24-240s, during which the loop never drains the
socket, so Gemini's keepalive ping timed out (1011) and the session died
mid-call. The result then reached the HUD (fed straight from the ToolResult) but
not the model -- so the HUD showed the failure while Era said the opposite, or
nothing at all ("the crew dont respond and era dont report back when task
failed").

The fix hands a crew `ask` to a background task (like the CM lane's long jobs)
and answers the model immediately with an ack. When the crew finishes, the
worker flips the HUD row to the REAL outcome and speaks it through
`_deliver_or_queue_cm_report`, which survives a reconnect. These tests pin that
the same outcome reaches BOTH panels, for success AND failure, and that a
confirmation-required crew result is announced rather than read as an outcome.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.era_live import tool_dispatch as td  # noqa: E402
from app.era_live.tool_dispatch import ToolDispatchMixin  # noqa: E402
from core.confirmation_preview import PREVIEW_KEY  # noqa: E402
from core.tool_result import ToolResult  # noqa: E402


class _Era:
    """Smallest object the crew-background methods can run on."""

    def __init__(self):
        self._inflight_user_text = ""
        self._last_user_text = "ask the crew about the plan"
        self._trace_routed = ("crew", {"action": "ask", "task": "x"})
        self._trace_safety = "LOW: Crew request"
        self.delivered: list[str] = []
        self.announced: list[str] = []
        self._captured_coro = None
        # Bind the real methods under test.
        self._crew_ask_should_background = ToolDispatchMixin._crew_ask_should_background.__get__(
            self
        )
        self._start_background_crew_ask = ToolDispatchMixin._start_background_crew_ask.__get__(self)
        self._deliver_crew_outcome = ToolDispatchMixin._deliver_crew_outcome.__get__(self)

    # stubbed collaborators
    def _spawn_background_task(self, coro, label):
        self._captured_coro = coro  # run it deterministically from the test

    def _deliver_or_queue_cm_report(self, text, *, verbatim=False):
        self.delivered.append(text)

    def _announce_pending_plan(self, preview_text):
        self.announced.append(preview_text)
        return True


class _FC:
    def __init__(self, cid="c1", name="crew"):
        self.id = cid
        self.name = name


def _run_worker(era):
    assert era._captured_coro is not None, "background worker was never spawned"
    asyncio.run(era._captured_coro)


# ── _crew_ask_should_background ───────────────────────────────────────────────


def test_crew_ask_is_backgrounded():
    era = _Era()
    assert era._crew_ask_should_background("crew", {"action": "ask", "task": "x"}) is True


def test_crew_status_and_list_stay_inline():
    era = _Era()
    assert era._crew_ask_should_background("crew", {"action": "status"}) is False
    assert era._crew_ask_should_background("crew", {"action": "list"}) is False


def test_non_crew_tool_is_never_backgrounded_here():
    era = _Era()
    assert era._crew_ask_should_background("web_search", {"action": "ask"}) is False


def test_a_scheduled_crew_ask_stays_inline():
    """A scheduled chain may feed the crew's answer to its next step, so the
    immediate ack must not replace the real result there."""
    from core import task_scheduler

    token = task_scheduler.current_task.set({"id": "nightly"})
    try:
        era = _Era()
        assert era._crew_ask_should_background("crew", {"action": "ask", "task": "x"}) is False
    finally:
        task_scheduler.current_task.reset(token)


# ── the background worker reports the TRUE outcome to both panels ─────────────


def test_a_crew_failure_is_spoken_and_flipped_onto_the_hud(monkeypatch):
    finished = []
    monkeypatch.setattr(td.hud, "tool_finished", lambda *a, **k: finished.append((a, k)))
    import core.task_trace as tt

    monkeypatch.setattr(tt, "trace", lambda *a, **k: None)
    monkeypatch.setattr(
        "crew.runtime.run_tool",
        lambda params: ToolResult.fail(
            message="drew used all 6 steps without finishing.",
            error="drew used all 6 steps without finishing.",
            error_type="StepLimit",
        ),
    )

    era = _Era()
    era._start_background_crew_ask(_FC(), "hud-row", {"action": "ask", "task": "x"})
    _run_worker(era)

    # HUD got the REAL failure, not the ack.
    assert finished, "HUD row was never finished"
    hud_result = finished[0][0][3]
    assert hud_result.success is False
    assert hud_result.error_type == "StepLimit"
    # Era reported the failure to the user, through the reconnect-safe path.
    assert era.delivered, "the failure was never reported to the user"
    assert "couldn't finish" in era.delivered[0]
    assert "drew used all 6 steps" in era.delivered[0]


def test_a_crew_success_is_reported(monkeypatch):
    monkeypatch.setattr(td.hud, "tool_finished", lambda *a, **k: None)
    import core.task_trace as tt

    monkeypatch.setattr(tt, "trace", lambda *a, **k: None)
    monkeypatch.setattr(
        "crew.runtime.run_tool",
        lambda params: ToolResult.ok(message="The design philosophy favours stone and wood."),
    )

    era = _Era()
    era._start_background_crew_ask(_FC(), "hud-row", {"action": "ask", "task": "x"})
    _run_worker(era)

    assert era.delivered and "finished" in era.delivered[0]
    assert "stone and wood" in era.delivered[0]
    assert era.announced == []  # a plain answer is not a confirmation


def test_a_confirmation_required_crew_result_is_announced_not_read_as_an_outcome(monkeypatch):
    monkeypatch.setattr(td.hud, "tool_finished", lambda *a, **k: None)
    import core.task_trace as tt

    monkeypatch.setattr(tt, "trace", lambda *a, **k: None)
    monkeypatch.setattr(
        "crew.runtime.run_tool",
        lambda params: ToolResult.fail(
            message="Replace the intro paragraph. Should I proceed?",
            error_type="ConfirmationRequired",
            data={PREVIEW_KEY: "Replace the intro paragraph of the brief. Should I proceed?"},
        ),
    )

    era = _Era()
    era._start_background_crew_ask(_FC(), "hud-row", {"action": "ask", "task": "x"})
    _run_worker(era)

    # The plan was put in front of the user; no separate "outcome" was spoken.
    assert era.announced and "intro paragraph" in era.announced[0]
    assert era.delivered == []


def test_a_crashing_crew_still_reports_a_failure(monkeypatch):
    monkeypatch.setattr(td.hud, "tool_finished", lambda *a, **k: None)
    import core.task_trace as tt

    monkeypatch.setattr(tt, "trace", lambda *a, **k: None)

    def _boom(params):
        raise RuntimeError("executor died")

    monkeypatch.setattr("crew.runtime.run_tool", _boom)

    era = _Era()
    era._start_background_crew_ask(_FC(), "hud-row", {"action": "ask", "task": "x"})
    _run_worker(era)

    assert era.delivered and "couldn't finish" in era.delivered[0]


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("delivery", ["connected", "disconnected", "standby"])
def test_undelivered_crew_approval_uses_reconnect_safe_report(monkeypatch, raises, delivery):
    from app.era_live.cm_flow import CMFlowMixin
    from app.hud.events import HudBus
    from app.text_intents import _say_exactly
    from core.confirmation_preview import ANNOUNCED_KEY

    bus = HudBus()
    monkeypatch.setattr(td, "hud", bus)
    monkeypatch.setattr("core.task_trace.trace", lambda *a, **k: None)
    preview = "Replace the intro paragraph of the brief. Should I proceed?"
    result = ToolResult.fail(
        message=preview,
        error_type="ConfirmationRequired",
        data={PREVIEW_KEY: preview},
    )
    monkeypatch.setattr("crew.runtime.run_tool", lambda params: result)
    era = _Era()
    spoken = []
    era._loop = object()
    era.session = None if delivery == "disconnected" else object()
    era._standby_active = delivery == "standby"
    era._pending_cm_announcement = None
    era.ui = SimpleNamespace(write_log=lambda text: None)
    era.speak = spoken.append
    era._deliver_or_queue_cm_report = CMFlowMixin._deliver_or_queue_cm_report.__get__(era)

    def unavailable(text):
        if raises:
            raise RuntimeError("announcement unavailable")
        return False

    era._announce_pending_plan = unavailable
    args = {"action": "ask", "task": "revise the brief"}
    call_id = bus.tool_started("approval-fallback", "crew", args)
    era._start_background_crew_ask(_FC(), call_id, args)
    _run_worker(era)

    if delivery == "connected":
        assert spoken == [_say_exactly(preview)]
        assert era._pending_cm_announcement is None
    else:
        assert spoken == []
        assert era._pending_cm_announcement == preview
    assert result.data[ANNOUNCED_KEY] is False
    assert bus.activity()["tools"][-1]["status"] == "awaiting"
    events = bus.recent_events(20)
    assert any(e["type"] == "tool.progress" for e in events)
    assert not any(e["type"] in ("tool.completed", "tool.failed") for e in events)


# ── _deliver_crew_outcome phrasing ───────────────────────────────────────────


def test_deliver_outcome_phrasing():
    era = _Era()
    era._deliver_crew_outcome(ToolResult.ok(message="All three brands are covered."))
    era._deliver_crew_outcome(
        ToolResult.fail(message="No bot named 'dura'.", error_type="UnknownBot")
    )
    assert era.delivered[0].startswith("The crew's finished")
    assert "couldn't finish" in era.delivered[1] and "dura" in era.delivered[1]
