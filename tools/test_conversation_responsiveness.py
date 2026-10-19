"""Regressions from 2026-10-13: slow tools and unanswered typed requests."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.input_envelope import InputEnvelope, current_input
from core.live_health import LiveHealth
from core.model_reply import send_model_text


@pytest.mark.parametrize("name", ["system_performance", "media_coordinator"])
def test_slow_live_tool_releases_receive_path_and_reports_actual_result(monkeypatch, name):
    from app.era_live import tool_dispatch as dispatch
    from core.tool_result import ToolResult

    assert 0 < dispatch._BG_GRACE_S[name] <= 2
    monkeypatch.setitem(dispatch._BG_GRACE_S, name, 0.02)
    finished, delivered, workers = [], [], []
    monkeypatch.setattr(
        dispatch,
        "hud",
        SimpleNamespace(
            tool_started=lambda *a: f"responsiveness-{name}",
            tool_finished=lambda *a, **kw: finished.append(a),
        ),
    )

    class Era(dispatch.ToolDispatchMixin):
        ui = SimpleNamespace(muted=False, set_state=lambda *a: None)
        session_manager = SimpleNamespace(transition_to=lambda *a, **kw: None)
        _standby_active = False

        def _crew_ask_should_background(self, *a):
            return False

        def _pending_cm_ask_active(self):
            return False

        def _cm_handoff_active(self):
            return False

        def _spawn_background_task(self, coro, label):
            workers.append(asyncio.create_task(coro))

        def _deliver_tool_outcome(self, name, result):
            delivered.append(result.message)

    async def scenario():
        era, release = Era(), asyncio.Event()

        async def slow(fc):
            await release.wait()
            return ToolResult.ok("Actual completion")

        era._execute_tool_result = slow
        fc = SimpleNamespace(name=name, args={"action": "pc_boost"}, id=f"slow-{name}")
        response = await asyncio.wait_for(era._execute_tool(fc), 0.5)
        assert "STILL RUNNING" in json.dumps(response.response)
        assert not finished and not delivered  # Never invent completion.
        assert era._tools_in_flight == 0
        # Incoming messages can now be read while the work remains pending.
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*workers)
        assert delivered == ["Actual completion"]
        assert len(finished) == 1

    asyncio.run(scenario())


def test_pc_boost_cancelled_during_discovery_never_presses_button(monkeypatch):
    from actions import system_performance as perf
    from core.cancel_registry import CancelledTask, CancelRegistry, bind

    token = CancelRegistry().register("boost-cancel", "system_performance")
    pressed = []
    button = SimpleNamespace(window_text=lambda: "Boost", invoke=lambda: pressed.append(True))

    def discover(**kwargs):
        token.event.set()
        return [button]

    window = SimpleNamespace(handle=1, descendants=discover)
    monkeypatch.setattr(perf, "is_windows", lambda: True)
    monkeypatch.setattr(perf, "_co_init", lambda: None)
    monkeypatch.setattr(perf, "_find_windows", lambda *a, **kw: [window])
    monkeypatch.setattr(perf, "_wake_webview_a11y", lambda *a: None)
    with bind(token), pytest.raises(CancelledTask):
        perf.pc_boost()
    assert not pressed


@pytest.mark.parametrize("name", ["system_performance", "media_coordinator"])
def test_real_dispatch_carries_cancel_token_into_the_worker(monkeypatch, name):
    from app.era_live import tool_dispatch as dispatch
    from core.cancel_registry import CURRENT_TOKEN, CancelRegistry, cancelled

    token = CancelRegistry().register("worker-cancel", name)
    token.event.set()
    seen = []

    def action(**kwargs):
        seen.append(cancelled())
        return "cancel observed"

    monkeypatch.setattr(dispatch, name, action)
    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        ui=None,
    )

    async def scenario():
        reset = CURRENT_TOKEN.set(token)
        try:
            await dispatch.ToolDispatchMixin._execute_tool_raw(era, name, {"action": "pc_boost"})
        finally:
            CURRENT_TOKEN.reset(reset)

    asyncio.run(scenario())
    assert seen == [True]


@pytest.mark.parametrize("source", ["typed", "telegram"])
def test_sent_text_is_monitored_for_silence_even_without_mic(source):
    now = [100.0]
    health = LiveHealth(clock=lambda: now[0])
    owner = InputEnvelope.create("next track", source)

    async def scenario():
        async def sdk(**kwargs):
            pass

        era = SimpleNamespace(
            session=SimpleNamespace(send_realtime_input=sdk),
            live_health=health,
            _input_envelope=owner,
        )
        token = current_input.set(owner)
        try:
            await send_model_text(era, owner.text)
        finally:
            current_input.reset(token)

    asyncio.run(scenario())
    assert health.pending_text
    now[0] += 60
    assert health.verdict().kind == "mute"
    health.note_reply()
    health.note_delivered()  # a reply only counts once its output was actually written
    assert not health.pending_text and health.verdict() is None


def test_repeated_text_does_not_postpone_detection_of_a_dead_connection():
    now = [100.0]
    health = LiveHealth(clock=lambda: now[0])
    health.note_text()
    now[0] += 20
    health.note_text()
    now[0] += 40
    assert health.verdict().kind == "mute"


def test_internal_text_does_not_create_a_user_silence_verdict():
    async def scenario():
        health = LiveHealth()

        async def sdk(**kwargs):
            pass

        era = SimpleNamespace(session=SimpleNamespace(send_realtime_input=sdk), live_health=health)
        await send_model_text(era, "background status", internal=True)
        assert not health.pending_text and health.verdict() is None

    asyncio.run(scenario())


def test_muted_mic_does_not_hide_an_unanswered_typed_request(monkeypatch):
    from app.era_live.claude_voice import ClaudeVoiceMixin

    now = [100.0]
    health = LiveHealth(clock=lambda: now[0])
    health.note_text()
    now[0] += 60
    verdicts = []
    era = SimpleNamespace(
        live_health=health,
        _standby_active=False,
        _mic_muted=True,
        _shutdown_requested=False,
        _go_away_event=asyncio.Event(),
    )

    def enter(kind, detail):
        verdicts.append(kind)
        return True

    era.enter_claude_voice = enter

    async def tick(_):
        # End the loop even if a regression skips the verdict branch.
        era._shutdown_requested = True

    monkeypatch.setattr("core.live_health.enabled", lambda: True)
    monkeypatch.setattr("app.era_live.claude_voice.asyncio.sleep", tick)
    asyncio.run(ClaudeVoiceMixin._live_health_watch(era))
    assert not verdicts  # User prefers Gemini recovery, not an automatic brain switch.
    assert era._go_away_event.is_set()


def test_cooperative_cancellation_is_not_logged_as_a_crash(monkeypatch):
    from app.era_live import tool_dispatch as dispatch
    from core.cancel_registry import CancelledTask

    monkeypatch.setattr(
        dispatch,
        "classify_action",
        lambda *a, **kw: {
            "risk_level": "LOW",
            "requires_confirmation": False,
            "reason": "test",
        },
    )
    monkeypatch.setattr(
        dispatch, "log_crash", lambda *a, **kw: pytest.fail("cancel is not a crash")
    )

    async def raw(*args):
        raise CancelledTask("User stopped the task")

    era = SimpleNamespace(_execute_tool_raw=raw)
    fc = SimpleNamespace(name="system_performance", args={"action": "pc_boost"})
    result = asyncio.run(dispatch.ToolDispatchMixin._execute_tool_result_inner(era, fc))
    assert not result.success and result.error_type == "Cancelled"
