"""Intentional standby, saved preferences, completion wake and privacy."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.era_live.cm_flow import CMFlowMixin
from app.era_live.local_fallback import LocalFallbackMixin
from app.era_live.reconnect_mixin import ReconnectMixin
from core import standby_policy as policy
from core.era_state import EraState


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "POLICY_PATH", tmp_path / "standby.json")
    # These cases test the OPTIONAL standby mode. Shipped default is OFF while
    # the one-mode listening session is evaluated (core/session.py).
    monkeypatch.setattr("core.session.standby_enabled", lambda: True)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("enable auto-standby", True),
        ("Era, disable auto standby!", False),
        ("please turn on automatic standby", True),
        ("turn off auto standby", False),
        ("explain how to enable auto-standby", None),
        ("don't enable auto-standby", None),
        ("enable auto-standby and delete files", None),
    ],
)
def test_deliberate_toggle_only(text, expected):
    assert policy.auto_standby_order(text) is expected


def test_off_default_saved_toggle_and_other_keys_survive():
    assert not policy.auto_standby_enabled()
    policy.POLICY_PATH.write_text(json.dumps({"future": 123}))
    policy.set_auto_standby(True)
    assert policy.auto_standby_enabled()
    assert json.loads(policy.POLICY_PATH.read_text())["future"] == 123
    policy.set_auto_standby(False)
    assert not policy.auto_standby_enabled()


@pytest.mark.parametrize(
    "raw", ["{bad", "[]", "null", '{"auto_standby": "true"}', '{"auto_standby": 1}']
)
def test_invalid_policy_cannot_enable_idle_sleep(raw):
    policy.POLICY_PATH.write_text(raw)
    assert not policy.auto_standby_enabled()


def test_failed_save_preserves_previous_preference(monkeypatch):
    policy.set_auto_standby(False)

    def fail(*args):
        raise OSError("locked")

    monkeypatch.setattr(policy.os, "replace", fail)
    with pytest.raises(OSError):
        policy.set_auto_standby(True)
    assert not policy.auto_standby_enabled()
    assert len(list(policy.POLICY_PATH.parent.iterdir())) == 1


def era_stub():
    class Era(ReconnectMixin, CMFlowMixin, LocalFallbackMixin):
        pass

    era = Era()
    era._standby_active = False
    era._standby_entering = False
    era._mic_muted = False
    era._browser_mic_active = True
    era._is_speaking = False
    era._last_interaction_timestamp = 0
    era._idle_timeout_seconds = 300
    era._go_away_event = asyncio.Event()
    era._shutdown_requested = False
    era._pending_cm_announcement = None
    era._local_turn_task = None
    era.session = object()
    era._loop = None
    era.logs, era.spoken, era.acks = [], [], []
    era.ui = SimpleNamespace(
        muted=False,
        write_log=era.logs.append,
        set_mute=lambda value: None,
        set_state=lambda state: None,
    )
    era.snapshot = {"current_state": EraState.LISTENING}
    era.session_manager = SimpleNamespace(
        get_state_snapshot=lambda: era.snapshot, transition_to=lambda *a, **k: None
    )
    era._stop_wake_word_listener = lambda: None
    era._flush_audio_in_queue = lambda: 0
    era._tg_ack = era.acks.append
    era.speak = era.spoken.append
    era.speak_dispatched = era.spoken.append
    era._is_abort_order = lambda text: False
    era._is_reboot_order = lambda text: False
    return era


@pytest.mark.parametrize(
    "guard",
    [
        "disabled",
        "standby",
        "entering",
        "hard_mic_off",
        "speaking",
        "processing",
        "approval",
        "turn",
        "reconnecting",
    ],
)
def test_idle_transition_matrix(guard):
    era = era_stub()
    policy.set_auto_standby(guard != "disabled")
    if guard == "standby":
        era._standby_active = True
    elif guard == "entering":
        era._standby_entering = True
    elif guard == "mic_muted":
        era._mic_muted = True
    elif guard == "hard_mic_off":
        era._browser_mic_active = False
    elif guard == "speaking":
        era._is_speaking = True
    elif guard == "processing":
        era.snapshot["current_state"] = EraState.PROCESSING
    elif guard == "approval":
        era._pending_store = SimpleNamespace(peek=lambda: object())
    elif guard == "turn":
        era._local_turn_task = SimpleNamespace(done=lambda: False)
    elif guard == "reconnecting":
        era.snapshot["is_reconnecting"] = True
    assert not era._auto_standby_due()


def test_enabled_idle_can_enter_and_toggle_resets_clock():
    era = era_stub()
    assert "enabled" in era._handle_auto_standby_order("enable auto-standby")
    assert not era._auto_standby_due()
    era._last_interaction_timestamp = 0
    assert era._auto_standby_due()
    assert "disabled" in era._handle_auto_standby_order("disable auto-standby")
    assert not era._standby_active and not era._auto_standby_due()


def test_voice_mute_can_idle_into_local_wake_mode_when_enabled():
    era = era_stub()
    era._mic_muted = True
    policy.set_auto_standby(True)
    assert era._auto_standby_due()
    era._standby_active = True
    era._prepare_standby_wake_input()
    assert era._browser_mic_active and era._mic_muted
    policy.set_auto_standby(False)
    assert not era._auto_standby_due()


def test_independent_hard_mic_off_is_not_reopened_for_standby():
    era = era_stub()
    era._browser_mic_active = False
    era._prepare_standby_wake_input()
    assert not era._browser_mic_active


@pytest.mark.parametrize("mic_open", [False, True])
def test_backup_standby_routes_only_to_local_wake_even_when_voice_muted(mic_open):
    era = era_stub()
    era._standby_active = True
    era._mic_muted = True
    era._browser_mic_active = mic_open
    era._wake_queue = asyncio.Queue()
    frame = bytes(2048)
    asyncio.run(era._local_on_frame(frame))
    assert era._wake_queue.qsize() == int(mic_open)


@pytest.mark.parametrize("channel", ["typed", "backup"])
def test_toggle_works_without_cloud_and_does_not_wake_standby(channel):
    era = era_stub()
    era._standby_active = True
    if channel == "typed":
        import main

        era._tg_reply_pending = False
        main.EraLive._handle_text_command(era, "enable auto-standby")
    else:
        asyncio.run(era._local_on_utterance("enable auto-standby", "en", 0))
    assert policy.auto_standby_enabled() and era._standby_active
    assert era.spoken == []


@pytest.mark.parametrize("reason", ["background_task", "remote_approval"])
@pytest.mark.parametrize("mic_muted,mic_open", [(True, True), (False, False), (False, True)])
def test_completion_resume_keeps_microphone_privacy(mic_muted, mic_open, reason):
    era = era_stub()
    era._standby_active = True
    era._mic_muted, era._browser_mic_active = mic_muted, mic_open
    opened = []
    era._handle_set_microphone = lambda args: opened.append(args)
    asyncio.run(era._resume_from_standby(reason, speak_ack=False))
    assert not era._standby_active
    assert era._mic_muted is mic_muted and era._browser_mic_active is mic_open
    assert opened == [] and era.spoken == []


def test_two_completion_reports_wake_once_and_drain_both():
    async def run():
        era = era_stub()
        era._loop = asyncio.get_running_loop()
        era._standby_active = True
        era._deliver_or_queue_cm_report("Job A completed.", verbatim=True)
        era._deliver_or_queue_cm_report("Job B failed.", verbatim=True)
        await asyncio.sleep(0)
        await era._completion_wake_task
        assert not era._standby_active
        assert era._pending_cm_announcement is None
        assert len(era.spoken) == 1
        assert "Job A completed." in era.spoken[0] and "Job B failed." in era.spoken[0]

    asyncio.run(run())


def test_completion_can_be_spoken_while_input_stays_voice_muted():
    async def run():
        era = era_stub()
        era._loop = asyncio.get_running_loop()
        era._standby_active = True
        era._mic_muted = True
        era._deliver_or_queue_cm_report("Your task is finished.", verbatim=True)
        await asyncio.sleep(0)
        await era._completion_wake_task
        assert era._mic_muted and not era._standby_active
        assert len(era.spoken) == 1 and "Your task is finished." in era.spoken[0]

    asyncio.run(run())


def test_disconnected_completion_is_held_without_claiming_wake():
    era = era_stub()
    era._standby_active = True
    era._deliver_or_queue_cm_report("A task finished.")
    assert not era._request_completion_wake()
    assert era._pending_cm_announcement == "A task finished."
    assert era.spoken == []


def test_repeated_standby_does_not_start_a_second_transition():
    import main

    era = era_stub()
    era._standby_entering = True
    main.EraLive.enter_standby(era)
    assert era._standby_entering and era.spoken == []


def test_standby_disabled_closes_the_session_and_keeps_the_mic_warm(monkeypatch):
    """The shipped one-mode model (2026-11-11): no teardown, no wake listener."""
    import main

    monkeypatch.setattr("core.session.standby_enabled", lambda: False)
    era = era_stub()
    closed: list[bool] = []
    states: list[str] = []
    transitions: list[EraState] = []
    era._listening_state = lambda: SimpleNamespace(close_session=lambda: closed.append(True))
    era.ui.set_state = states.append
    era.session_manager.transition_to = lambda state, **kw: transitions.append(state)

    main.EraLive.enter_standby(era)

    assert closed == [True], "the conversation window must close"
    assert transitions == [EraState.ONLINE_IDLE], (
        "EraState.STANDBY sets standby_active in the snapshot, which makes the "
        "audio loop drop every frame and the input gate refuse typed orders"
    )
    assert "STANDBY" not in states, "the HUD must not show STANDBY"
    assert era._browser_mic_active is True, "the microphone keeps capturing locally"
    assert era._mic_muted is False, "closing the session is not a mute"
    assert era._standby_active is False, "no standby teardown happens"
    assert any("Session closed" in line for line in era.logs)


@pytest.mark.parametrize(
    "status,wake",
    [
        ("ok", True),
        ("failed", True),
        ("partial", True),
        ("awaiting_confirmation", False),
        ("skipped", False),
        (None, False),
    ],
)
def test_scheduled_completion_wakes_but_approval_or_alert_only_notifies(monkeypatch, status, wake):
    from app.era_live.tool_dispatch import ToolDispatchMixin
    from core.task_scheduler import current_report

    era = era_stub()
    era._standby_active = True
    era.snapshot.update(standby_active=True, is_muted=True)
    delivered, telegram = [], []
    era._deliver_or_queue_cm_report = lambda text, **kw: delivered.append(text)
    monkeypatch.setattr("app.notify._tg_notify", telegram.append)
    token = current_report.set({"status": status} if status else None)
    try:
        assert ToolDispatchMixin._report_scheduled_task(era, "Task report.", False) is False
    finally:
        current_report.reset(token)
    assert bool(delivered) is wake
    assert telegram == ["Task report."]
