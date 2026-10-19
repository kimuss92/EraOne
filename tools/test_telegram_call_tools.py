"""Offline remote execution, approval ownership and local/remote admission tests."""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.approval_execution import CURRENT_TASK_ID, REMOTE_APPROVAL
from core.input_envelope import InputEnvelope, current_input
from core.pending_confirmation import (
    SCOPED_APPROVAL,
    PendingConfirmationStore,
    user_approved,
)
from core.telegram_call_activation import (
    calling_enabled,
)
from core.telegram_call_activation import (
    test_settings as activation_settings,
)
from core.telegram_call_gemini import CallCommand
from core.telegram_call_tools import SUPPORTED, RemoteTools, declarations
from core.tool_execution_lane import LANE, ExecutionLane, ToolLaneBusy, resource_tool
from core.tool_result import ToolResult
from tools.test_telegram_call_gemini import Session, until

GEN = "a" * 32


def command(name="open_app", text="Open Brave", args=None, identifier="t1"):
    return CallCommand(
        GEN,
        42,
        identifier,
        name,
        args or {"app": "brave"},
        InputEnvelope.create(text, "telegram"),
    )


@pytest.fixture
def policies(monkeypatch):
    import agent.executor as executor

    store = PendingConfirmationStore()
    monkeypatch.setattr(executor, "_pending_store", store)
    monkeypatch.setattr("core.telegram_call_tools.get_shared_store", lambda: store)
    monkeypatch.setattr("core.pending_confirmation.get_shared_store", lambda: store)
    monkeypatch.setattr("core.pending_confirmation._user_approval_probe", lambda p: True)
    monkeypatch.setattr(
        "core.self_healing_router.route_tool_call",
        lambda name, args, *rest: (name, args),
    )
    monkeypatch.setattr(executor, "route_tool_call", lambda name, args, *rest: (name, args))
    monkeypatch.setattr(executor, "verify_tool_result", lambda result, **kwargs: result)
    monkeypatch.setattr(
        executor,
        "classify_action",
        lambda name, args: {
            "risk_level": "HIGH" if name == "send_message" else "SAFE",
            "requires_confirmation": name == "send_message",
            "reason": "Synthetic classification",
        },
    )
    monkeypatch.setattr(
        executor,
        "build_confirmation_preview",
        lambda name, args: ("Send the exact message?", {}),
    )
    monkeypatch.setattr("core.approval_execution.publish_approved_result", lambda *args: None)
    driver = Mock(return_value=ToolResult.ok("Opened Brave.", tool_name="open_app"))
    monkeypatch.setattr("actions.open_app.open_app", driver)
    return store, driver


def test_declared_tools_match_supported_real_executor_tools():
    exposed = declarations()
    assert exposed and {item["name"] for item in exposed} <= SUPPORTED
    assert not {"generated_code", "agent_task", "crew_dispatch", "enter_standby"} & {
        item["name"] for item in exposed
    }
    exposed[0]["name"] = "mutated"
    assert declarations()[0]["name"] != "mutated"


@pytest.mark.asyncio
async def test_remote_action_context_and_provider_replay_are_isolated(policies):
    store, driver = policies
    actor = RemoteTools(42)
    captured = []

    def run(**kwargs):
        captured.append((current_input.get(), CURRENT_TASK_ID.get()))
        return ToolResult.ok("Opened Brave.", tool_name="open_app")

    driver.side_effect = run
    req = command()
    result = await actor.dispatch(req)
    assert result["success"]
    assert await actor.dispatch(req) == result
    assert driver.call_count == 1
    assert captured[0] == (req.input, "telegram-call:" + GEN)
    assert current_input.get() is None and CURRENT_TASK_ID.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"peer_id": 43},
        {"peer_id": 42.0},
        {"generation": "old"},
        {"provider_id": ""},
        {"input": InputEnvelope.create("Open Brave", "typed")},
    ],
)
async def test_invalid_remote_identity_never_executes(policies, change):
    _, driver = policies
    assert not (await RemoteTools(42).dispatch(replace(command(), **change)))["success"]
    assert not driver.called


@pytest.mark.asyncio
async def test_unknown_tools_and_memory_hallucinations_are_refused(policies):
    actor = RemoteTools(42)
    result = await actor.dispatch(command(name="generated_code"))
    assert result["error_type"] == "UnsupportedRemoteTool"
    result = await actor.dispatch(
        command(
            name="save_memory",
            text="Hello",
            args={"category": "notes", "key": "test", "value": "made-up preference"},
        )
    )
    assert result["error_type"] == "MemoryNotAuthorized"


@pytest.mark.asyncio
async def test_cross_channel_confirmation_cancel_and_consumption_blocked(policies):
    store, _ = policies
    cid = store.store(
        "send_message",
        {"platform": "telegram", "receiver": "owner", "message_text": "hello"},
        "HIGH",
        "test",
    )
    actor = RemoteTools(42)
    for name in ("cancel_pending_confirmation", "confirm_pending_confirmation"):
        result = await actor.dispatch(
            command(name=name, text="yes", args={"confirmation_id": cid}, identifier=name)
        )
        assert result["error_type"] == "ConfirmationOwnerMismatch"
        assert store.peek(cid)


@pytest.mark.asyncio
async def test_call_high_risk_requires_authenticated_button_and_uses_exact_preview(
    policies,
):
    store, _ = policies
    actor = RemoteTools(42)
    result = await actor.dispatch(
        command(
            "send_message",
            "Send this message",
            {"platform": "telegram", "receiver": "owner", "message_text": "hello"},
        )
    )
    assert result["error_type"] == "ConfirmationRequired"
    pending = store.peek(result["confirmation_id"])
    assert pending.task_id == "telegram-call:" + GEN
    result = await actor.dispatch(
        command(
            "confirm_pending_confirmation",
            "yes",
            {"confirmation_id": pending.confirmation_id},
            "t2",
        )
    )
    assert result["error_type"] == "ConfirmationChannelRequired"
    assert store.peek(pending.confirmation_id)
    assert not user_approved(pending)  # Local probe says yes, but cannot approve this call.
    grant = REMOTE_APPROVAL.set(pending)
    try:
        assert user_approved(pending)
    finally:
        REMOTE_APPROVAL.reset(grant)


def test_scoped_approval_requires_current_generation_and_fresh_user_reply(policies):
    store, _ = policies
    scope = "telegram-call:" + GEN
    task = CURRENT_TASK_ID.set(scope)
    cid = store.store("file_controller", {"action": "copy"}, "MEDIUM", "test")
    CURRENT_TASK_ID.reset(task)
    pending = store.peek(cid)
    old = replace(
        command(text="yes"),
        input=replace(command(text="yes").input, received_at=pending.created_at - 1),
    )
    assert not RemoteTools._approved(old, pending)
    assert not RemoteTools._approved(command(text="yes but do not do that"), pending)
    assert RemoteTools._approved(command(text="yes"), pending)
    probe = SCOPED_APPROVAL.set((scope, lambda p: True))
    try:
        assert user_approved(pending)
        assert not user_approved(replace(pending, task_id=None))
    finally:
        SCOPED_APPROVAL.reset(probe)
    assert not user_approved(pending)


@pytest.mark.asyncio
async def test_cancel_call_approval_only_cancels_matching_generation(policies):
    store, _ = policies
    task = CURRENT_TASK_ID.set("telegram-call:" + GEN)
    cid = store.store("file_controller", {"action": "copy"}, "MEDIUM", "test")
    CURRENT_TASK_ID.reset(task)
    result = await RemoteTools(42).dispatch(
        command(
            "cancel_pending_confirmation",
            "Cancel that approval",
            {"confirmation_id": cid},
        )
    )
    assert result["success"] and store.peek() is None


@pytest.mark.asyncio
async def test_cancelled_wrapper_keeps_worker_lease_and_suppresses_late_success(
    policies,
):
    _, driver = policies
    started, release = threading.Event(), threading.Event()

    def run(**kwargs):
        started.set()
        release.wait(2)
        return ToolResult.ok("Opened Brave.")

    driver.side_effect = run
    actor = RemoteTools(42)
    req = command()
    task = asyncio.create_task(actor.dispatch(req))
    await until(started.is_set)
    assert (await actor.dispatch(req))["error_type"] == "AlreadyRunning"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ToolLaneBusy):
        with LANE.enter("open_app"):
            pass
    release.set()
    await until(lambda: not actor._running)
    result = await actor.dispatch(req)
    assert not result["success"] and result["error_type"] == "Cancelled"


@pytest.mark.asyncio
async def test_local_machine_work_returns_busy_without_racing_focus(policies):
    _, driver = policies
    with LANE.enter("open_app"):
        result = await RemoteTools(42).dispatch(command())
    assert result["error_type"] == "ExecutionBusy" and not driver.called


def test_lane_preserves_local_concurrency_and_remote_reentrancy():
    lane = ExecutionLane()
    with lane.enter("open_app"), lane.enter("window_control"):
        with pytest.raises(ToolLaneBusy):
            with lane.enter("open_app", "remote"):
                pass
    with lane.enter("open_app", "remote"), lane.enter("open_app", "remote"):
        with lane.enter("web_search"):
            pass
        with pytest.raises(ToolLaneBusy):
            with lane.enter("open_app", "other"):
                pass
    with lane.enter("open_app"):
        pass


def test_same_call_generation_cannot_reenter_from_another_worker_thread():
    lane = ExecutionLane()
    refused = []

    def other():
        try:
            with lane.enter("open_app", "remote"):
                refused.append(False)
        except ToolLaneBusy:
            refused.append(True)

    with lane.enter("open_app", "remote"):
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(1)
    assert refused == [True]


@pytest.mark.parametrize("argument", ["--enable", "--disable", "fail"])
def test_test_mode_cli_places_no_calls_or_replays_login(monkeypatch, capsys, argument):
    import sys

    import tools.telegram_call_test_mode as cli

    monkeypatch.setattr(sys, "argv", ["test_mode", "--enable" if argument == "fail" else argument])
    monkeypatch.setattr(cli, "load_settings", lambda directory: account())
    saved = Mock(side_effect=ValueError("private") if argument == "fail" else None)
    monkeypatch.setattr(cli, "save_settings", saved)
    assert cli.main() == (1 if argument == "fail" else 0)
    output = capsys.readouterr().out
    assert (
        "private" not in output
        and "No call was placed" in output
        or argument in {"--disable", "fail"}
    )


def test_confirmation_resource_is_its_actual_stored_action(policies):
    store, _ = policies
    cid = store.store("open_app", {"app": "brave"}, "MEDIUM", "test")
    assert resource_tool("confirm_pending_confirmation", {"confirmation_id": cid}) == "open_app"
    assert (
        resource_tool("confirm_pending_confirmation", {"confirmation_id": "old"})
        == "confirm_pending_confirmation"
    )


def test_registry_follows_reachable_wrappers_without_counting_dead_handlers():
    from core.tool_registry_validator import extract_tool_names_from_executor_dispatch

    source = """
def _call_tool_result(tool):
    return _call_tool_result_owned(tool)
def _call_tool_result_owned(tool):
    return _call_tool_result_inner(tool)
def _call_tool_result_inner(tool):
    if tool == "confirm_pending_confirmation":
        return _call_tool_result(tool)
    elif tool == "cancel_pending_confirmation":
        return None
def _call_tool_result_unused(tool):
    if tool == "not_reachable":
        return None
"""
    assert extract_tool_names_from_executor_dispatch(source) == {
        "confirm_pending_confirmation",
        "cancel_pending_confirmation",
    }


def account():
    return {
        "era_phone": "+10000000",
        "owner_phone": "+10000001",
        "api_id": 1234,
        "api_hash": "a" * 32,
        "session_secret": "synthetic",
        "owner_user_id": 42,
        "expected_owner_user_id": 42,
        "era_user_id": 41,
        "authenticated": True,
        "account_authenticated": True,
        "owner_bound": True,
    }


def test_activation_is_explicit_bounded_and_preserves_credentials():
    settings = account()
    active = activation_settings(settings, True, now=100)
    assert active["session_secret"] == settings["session_secret"]
    assert not calling_enabled(settings, now=100)
    assert calling_enabled(active, now=100)
    assert not calling_enabled(active, now=1900)
    assert not calling_enabled({**active, "attended_test_until": float("nan")}, now=100)
    assert not calling_enabled({**active, "attended_test_until": True}, now=100)
    assert not calling_enabled(activation_settings(active, False), now=100)
    assert calling_enabled({"calls_enabled": True, "duplex_validated": True}, now=100)


def test_daily_opt_in_does_not_expire_or_fake_duplex_validation():
    from core.telegram_call_activation import daily_settings

    settings = account()
    enabled = daily_settings(settings, True)
    assert calling_enabled(enabled, now=1e20)
    assert enabled["auto_receive_calls"] is True
    assert enabled.get("duplex_validated") is None
    assert enabled["session_secret"] == settings["session_secret"]
    assert not calling_enabled(daily_settings(enabled, False))
    assert not calling_enabled({**enabled, "authenticated": False})
    assert not calling_enabled({**enabled, "owner_user_id": 43})


@pytest.mark.asyncio
async def test_daily_runtime_arms_incoming_on_startup_without_opening_a_provider(
    monkeypatch,
):
    from app.telegram_call_runtime import create_service
    from core.telegram_call_activation import daily_settings

    settings = daily_settings(account(), True)
    monkeypatch.setattr("core.telegram_call_setup.load_settings", lambda path: settings)
    begin = Mock()
    monkeypatch.setattr("app.telegram_call_runtime.CallCoordinator.begin", begin)
    service = await create_service()
    assert service.ready and service._expiry is None
    begin.assert_called_once_with("listen")
    await service.close()


@pytest.mark.asyncio
async def test_daily_receiving_pause_preference_is_persisted_without_disabling_calls(
    monkeypatch,
):
    from app.telegram_call_runtime import create_service, save_receiving_preference
    from core.telegram_call_activation import daily_settings

    settings = {**daily_settings(account(), True), "auto_receive_calls": False}
    monkeypatch.setattr("core.telegram_call_setup.load_settings", lambda path: settings)
    save = Mock()
    monkeypatch.setattr("core.telegram_call_setup.save_settings", save)
    service = await create_service()
    for enabled in (True, False):
        await save_receiving_preference(service, enabled)
        saved = save.call_args.args[1]
        assert saved["auto_receive_calls"] is enabled and saved["calls_enabled"] is True
        assert saved["session_secret"] == settings["session_secret"]
    await service.close()


@pytest.mark.asyncio
async def test_hud_renews_expired_test_window_without_restart_or_duplicate_save(
    monkeypatch,
):
    from app.telegram_call_runtime import create_service, renew_test_window

    settings = account()
    monkeypatch.setattr("core.telegram_call_setup.load_settings", lambda path: settings)
    save = Mock()
    monkeypatch.setattr("core.telegram_call_setup.save_settings", save)
    service = await create_service()
    assert service.snapshot()["canRenewTest"] and not service.ready
    await asyncio.gather(renew_test_window(service, ""), renew_test_window(service, ""))
    assert save.call_count == 1 and service.snapshot()["canStart"]
    saved = save.call_args.args[1]
    assert saved["session_secret"] == settings["session_secret"]
    assert calling_enabled(saved) and not service.snapshot()["canRenewTest"]
    service.set_test_expiry(0.01)
    await asyncio.sleep(0.03)
    assert service.snapshot()["canRenewTest"]
    await renew_test_window(service, "")
    assert save.call_count == 2 and service.ready
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stale", "owner", "auth", "save"])
async def test_test_window_renewal_failure_never_enables_calls(monkeypatch, failure):
    from app.telegram_call_runtime import create_service, renew_test_window

    settings = account()
    monkeypatch.setattr("core.telegram_call_setup.load_settings", lambda path: settings)
    save = Mock(side_effect=OSError("synthetic secret") if failure == "save" else None)
    monkeypatch.setattr("core.telegram_call_setup.save_settings", save)
    service = await create_service()
    if failure == "owner":
        settings["owner_user_id"] = 43
    elif failure == "auth":
        settings["authenticated"] = False
    with pytest.raises((PermissionError, ValueError, OSError)):
        await renew_test_window(service, "b" * 32 if failure == "stale" else "")
    assert not service.enabled
    if failure != "save":
        save.assert_not_called()
    await service.close()


@pytest.mark.parametrize(
    "change",
    [
        {"authenticated": False},
        {"era_user_id": 42},
        {"expected_owner_user_id": 43},
        {"owner_user_id": True},
    ],
)
def test_test_mode_cannot_enable_unverified_or_self_bound_account(change):
    with pytest.raises(ValueError):
        activation_settings({**account(), **change}, True, now=100)


@pytest.mark.asyncio
async def test_expiry_updates_idle_hud_readiness():
    from tools.test_telegram_call_coordinator import fixture

    call, _, _, _, _ = fixture()
    call.set_test_expiry(0.01)
    await asyncio.sleep(0.02)
    assert not call.ready and call.state == "disabled"


@pytest.mark.asyncio
async def test_phone_music_command_without_finished_reaches_existing_spotify_driver(
    monkeypatch, policies
):
    from google.genai import types

    from tools.test_telegram_call_gemini import finish, fixture

    driver = Mock(return_value="Spotify is playing.")
    monkeypatch.setattr("actions.spotify_control.spotify_control", driver)
    actor = RemoteTools(42)
    _, _, adapter, _, session, stop = fixture(dispatch=actor.dispatch, timeout=1)
    running = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                input_transcription=types.Transcription(text="Play music on Spotify")
            )
        )
    )
    await session.messages.put(
        types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(
                function_calls=[
                    types.FunctionCall(id="music", name="spotify_control", args={"action": "play"})
                ]
            )
        )
    )
    await until(lambda: session.responses)
    driver.assert_called_once_with(parameters={"action": "play"}, player=None)
    assert session.responses[0].response["success"] is True
    assert not running.done()
    await finish(running, stop)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_status", [None, 429, 503])
async def test_runtime_provider_passes_owner_audio_without_local_voiceprint(
    monkeypatch,
    first_status,
):
    from app.telegram_call_runtime import create_service
    from core.telegram_call_audio import CallPCM

    settings = {**account(), "calls_enabled": True, "duplex_validated": True}
    monkeypatch.setattr("core.telegram_call_setup.load_settings", lambda path: settings)
    gate_factory = Mock(side_effect=AssertionError("No PC biometric gate on phone calls"))
    monkeypatch.setattr("core.voice_gate.UtteranceGate", gate_factory)
    monkeypatch.setattr("core.utils.get_api_keys", lambda: ["synthetic", "backup"])
    monkeypatch.setattr("core.voice_mode.gemini_voice_name", lambda: "Algenib")
    monkeypatch.setattr("app.era_live.session_runtime.live_model", lambda default: "selected-model")
    client = Mock()
    monkeypatch.setattr("google.genai.Client", Mock(return_value=client))
    session = Session()
    observed = []

    @asynccontextmanager
    async def connection(passed, **kwargs):
        observed.append((passed, kwargs))
        try:
            if first_status is not None and len(observed) == 1:
                failure = RuntimeError("synthetic provider rejection")
                failure.code = first_status
                raise failure
            yield session
        finally:
            client.close()

    monkeypatch.setattr("app.era_live.session_runtime.managed_live_connection", connection)
    service = await create_service()
    assert service.ready
    with pytest.raises(RuntimeError):
        service.input_policy(CallPCM(GEN, "", 0, 16000, bytes(640)))
    async with service.provider() as connected:
        assert connected is session
        assert service.input_policy(CallPCM(GEN, "", 0, 16000, bytes(640))) == [bytes(640)]
        quiet_phone_pcm = b"\x01\x00" * 160
        assert service.input_policy(CallPCM(GEN, "", 1, 16000, quiet_phone_pcm)) == [
            quiet_phone_pcm
        ]
        for rate, data in ((24000, bytes(320)), (16000, b"x"), (16000, bytes(3202))):
            with pytest.raises(ValueError):
                service.input_policy(CallPCM(GEN, "", 2, rate, data))
    assert not gate_factory.called and client.close.called
    with pytest.raises(RuntimeError):
        service.input_policy(CallPCM(GEN, "", 0, 16000, bytes(640)))
    assert len(observed) == (1 if first_status is None else 2)
    assert observed[0][1]["model"] == "selected-model"
    assert {
        d.name for d in observed[0][1]["config"].tools[0].function_declarations
    } <= SUPPORTED | {"end_telegram_call"}


@pytest.mark.asyncio
async def test_speaker_policy_buffering_and_fail_closed_pcm_validation():
    from tools.test_telegram_call_gemini import finish, fixture

    audio, generation, adapter, _, session, stop = fixture(policy=lambda p: [p.data])
    audio.receive(42, generation, 0, bytes(640))
    task = asyncio.create_task(adapter.run(session, stop))
    await until(lambda: session.inputs)
    await finish(task, stop)
    audio, generation, adapter, _, session, stop = fixture(policy=lambda p: [b"x"])
    audio.receive(42, generation, 0, bytes(640))
    with pytest.raises(ValueError):
        await adapter.run(session, stop)
