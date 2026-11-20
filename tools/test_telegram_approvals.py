"""No real Telegram traffic, microphone access, or destructive tool execution."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import era_telegram as tg
from core.pending_confirmation import PendingConfirmationStore
from core.telegram_approvals import TelegramApprovals, execute_approved
from core.tool_result import ToolResult


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-only")
    calls = []

    def transport(method, payload, **kwargs):
        calls.append((method, deepcopy(payload)))
        return tg.ReplyDeliveryResult("sent", message_id=100 + len(calls))

    store = PendingConfirmationStore()
    engine = SimpleNamespace(_loop=None)
    engine._pending_store = store
    adapter = TelegramApprovals(store, lambda: engine, transport=transport, start_worker=False)
    yield store, engine, adapter, calls
    adapter.close()


def drain(adapter):
    while not adapter._jobs.empty():
        adapter._deliver(adapter._jobs.get_nowait())


def offer(setup):
    store, engine, adapter, calls = setup
    store.store(
        "file_controller", {"action": "delete", "path": "C:/test-only.txt"}, "HIGH", "delete"
    )
    drain(adapter)
    card = next(iter(adapter._cards.values()))
    callback = {
        "id": "query-1",
        "from": {"id": 123},
        "message": {
            "message_id": card.message_id,
            "date": 1234,
            "chat": {"type": "private", "id": 123},
        },
        "data": f"era:approve:{card.token}",
    }
    return card, callback


def test_cancel_exact_action_and_clear_buttons(setup):
    card, cb = offer(setup)
    store, _, adapter, calls = setup
    cb["data"] = f"era:cancel:{card.token}"
    adapter.handle_callback(cb)
    drain(adapter)
    assert not store.is_pending()
    assert any(p.get("text") == "Action cancelled." for _, p in calls)
    assert any(m == "answerCallbackQuery" and p["text"] == "Cancelled." for m, p in calls)


@pytest.mark.parametrize(
    "change", ["sender", "chat", "group", "message", "token", "inline", "date"]
)
def test_wrong_owner_or_card_cannot_consume(setup, change):
    _, cb = offer(setup)
    store, _, adapter, _ = setup
    if change == "sender":
        cb["from"]["id"] = 999
    if change == "chat":
        cb["message"]["chat"]["id"] = 999
    if change == "group":
        cb["message"]["chat"]["type"] = "group"
    if change == "message":
        cb["message"]["message_id"] += 1
    if change == "token":
        cb["data"] = "era:approve:not-a-real-token"
    if change == "inline":
        cb.pop("message")
    if change == "date":
        cb["message"]["date"] = 0
    adapter.handle_callback(cb)
    assert store.is_pending()


def test_unavailable_engine_leaves_action_retryable(setup):
    card, cb = offer(setup)
    store, _, adapter, calls = setup
    adapter.handle_callback(cb)
    drain(adapter)
    assert store.is_pending() and not card.claimed
    assert "reconnecting" in calls[-1][1]["text"]


@pytest.mark.parametrize("resolution", ["expire", "voice", "replace", "restart"])
def test_stale_buttons_refuse_execution(setup, resolution):
    card, cb = offer(setup)
    store, _, adapter, _ = setup
    if resolution == "expire":
        store._pending.expires_at = 0
    elif resolution == "voice":
        store.confirm(card.pending.confirmation_id)
    elif resolution == "replace":
        store.store("other", {}, "HIGH", "other", replace=True)
    else:
        adapter.close()
        adapter = TelegramApprovals(
            store, lambda: None, transport=setup[2].transport, start_worker=False
        )
    before = store.peek()
    adapter.handle_callback(cb)
    assert store.peek() == before
    adapter.close()


def test_unknown_send_has_no_retry_or_usable_card(setup):
    store, _, adapter, _ = setup
    calls = []
    adapter.transport = lambda *a, **k: (
        calls.append(a) or tg.ReplyDeliveryResult("unknown", "network")
    )
    store.store("tool", {}, "HIGH", "reason")
    drain(adapter)
    assert len(calls) == 1 and not adapter._cards and store.is_pending()


def test_no_secret_payload_in_card(setup):
    store, _, adapter, calls = setup
    store.store(
        "send_message", {"password": "secret-value", "message_text": "private-body"}, "HIGH", "send"
    )
    drain(adapter)
    text = calls[0][1]["text"]
    assert "secret-value" not in text and "private-body" not in text
    assert all(
        len(b["callback_data"].encode()) <= 64
        for b in calls[0][1]["reply_markup"]["inline_keyboard"][0]
    )


def test_repeated_click_schedules_once_and_reports_outcome(setup):
    card, cb = offer(setup)
    store, engine, adapter, calls = setup
    ran = []

    async def execute(fc):
        pending = store.confirm(card.pending.confirmation_id)
        ran.append(pending)
        result = ToolResult.ok("done")
        result.verification_status = "VERIFIED"
        return result

    engine._execute_tool_result = execute

    async def run():
        engine._loop = asyncio.get_running_loop()
        adapter.handle_callback(cb)
        adapter.handle_callback(cb)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if any(j[0] == "result" for j in list(adapter._jobs.queue)):
                break

    asyncio.run(run())
    drain(adapter)
    assert len(ran) == 1
    assert any("Verification: VERIFIED" in p.get("text", "") for _, p in calls)


def test_atomic_generation_check_cannot_take_reused_id():
    store = PendingConfirmationStore()
    ident = store.store("old", {}, "HIGH", "old")
    old = store.peek()
    store.store("new", {}, "HIGH", "new", replace=True)
    store._pending.confirmation_id = ident
    assert store.confirm(ident, expected_created_at=old.created_at) is None
    assert not store.cancel(ident, expected_created_at=old.created_at)
    assert store.peek().tool == "new"


def result_engine(monkeypatch):
    import core.vision_context as vc
    from app.era_live import tool_dispatch as td

    async def no_capture(*a, **k):
        pass

    monkeypatch.setattr(vc, "jit_vision_capture", no_capture)
    era = SimpleNamespace(
        _last_user_text="restart the pc",
        _inflight_user_text="",
        _recent_user_texts=[],
        _pending_store=PendingConfirmationStore(),
        speak=lambda *a, **k: None,
        ran=[],
    )

    async def raw(tool, args, fc=None):
        era.ran.append((tool, args))
        return ToolResult.ok("Executed test stub only.")

    era._execute_tool_raw = raw
    era._execute_tool_result = td.ToolDispatchMixin._execute_tool_result.__get__(era)
    era._log_caught_exception = lambda *a: None
    return era, td


def test_remote_action_uses_real_classifier_verifier_and_continuation(monkeypatch):
    era, td = result_engine(monkeypatch)
    era._pending_store.store(
        "file_controller", {"action": "delete", "path": "C:/test-only.txt"}, "HIGH", "delete"
    )
    pending = era._pending_store.peek()
    verified, continued = [], []

    def verify(result, *, tool, parameters):
        verified.append((tool, parameters))
        result.verification_status = "VERIFIED"
        return result

    monkeypatch.setattr(td, "verify_tool_result", verify)
    monkeypatch.setattr(
        "core.approval_execution.publish_approved_result", lambda cid, r: continued.append((cid, r))
    )
    result = asyncio.run(execute_approved(era, pending))
    assert result.success and result.verification_status == "VERIFIED"
    assert era.ran[0][0] == "file_controller"  # unrelated last text cannot reroute approval
    assert verified[0][0] == "file_controller" and continued[0][0] == pending.confirmation_id
    assert not era._pending_store.is_pending()
    again = asyncio.run(execute_approved(era, pending))
    assert not again.success and len(era.ran) == 1


def test_replaced_pending_cannot_use_remote_grant(monkeypatch):
    era, _ = result_engine(monkeypatch)
    era._pending_store.store(
        "file_controller", {"action": "delete", "path": "C:/old.txt"}, "HIGH", "delete"
    )
    old = era._pending_store.peek()
    era._pending_store.store(
        "file_controller",
        {"action": "delete", "path": "C:/new.txt"},
        "HIGH",
        "delete",
        replace=True,
    )
    result = asyncio.run(execute_approved(era, old))
    assert not result.success and not era.ran and era._pending_store.is_pending()


def test_newly_blocked_action_never_executes(monkeypatch):
    era, td = result_engine(monkeypatch)
    era._pending_store.store(
        "file_controller", {"action": "delete", "path": "C:/test.txt"}, "HIGH", "delete"
    )
    monkeypatch.setattr(
        td,
        "classify_action",
        lambda *a, **k: {
            "risk_level": "BLOCKED",
            "requires_confirmation": False,
            "reason": "policy changed",
        },
    )
    result = asyncio.run(execute_approved(era, era._pending_store.peek()))
    assert result.error_type == "BlockedAction" and not era.ran


def test_callback_transport_is_owner_restricted(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    assert tg.send_approval_update("sendMessage", {"chat_id": "999"}).status == "failed"
    assert (
        tg.send_approval_update("answerCallbackQuery", {"callback_query_id": "x"}).status
        == "failed"
    )


def test_voice_consumption_wins_before_button_execution(setup):
    _, cb = offer(setup)
    store, engine, adapter, _ = setup
    ran = []

    async def execute(fc):
        pending = store.confirm(fc.args["confirmation_id"])
        if pending is None:
            return ToolResult.fail("Already answered.", error_type="ConfirmationFailed")
        ran.append(pending)
        return ToolResult.ok("done")

    engine._execute_tool_result = execute

    async def run():
        engine._loop = asyncio.get_running_loop()
        adapter.handle_callback(cb)
        assert store.confirm(store.peek().confirmation_id) is not None
        await asyncio.sleep(0.03)

    asyncio.run(run())
    drain(adapter)
    assert not ran


def test_remote_grant_is_scoped_and_downgraded_risk_still_consumes(monkeypatch):
    from core.approval_execution import REMOTE_APPROVAL

    era, td = result_engine(monkeypatch)
    era._pending_store.store(
        "file_controller", {"action": "delete", "path": "C:/test.txt"}, "HIGH", "delete"
    )
    monkeypatch.setattr(
        td,
        "classify_action",
        lambda *a, **k: {
            "risk_level": "LOW",
            "requires_confirmation": False,
            "reason": "policy changed",
        },
    )
    pending = era._pending_store.peek()
    result = asyncio.run(execute_approved(era, pending))
    assert result.success and len(era.ran) == 1
    assert not era._pending_store.is_pending() and REMOTE_APPROVAL.get() is None


def test_poll_dispatches_callback_and_advances_cursor(monkeypatch, setup):
    import threading

    card, cb = offer(setup)
    stopped = threading.Event()
    offsets, seen, parameters = [], [], []

    class Response:
        status_code = 200

        def json(self):
            stopped.set()
            return {
                "ok": True,
                "result": [
                    {"update_id": 12, "callback_query": cb},
                    {"update_id": 13, "callback_query": {"from": {"id": 999}}},
                ],
            }

    class Session:
        def get(self, url, **kwargs):
            parameters.append(kwargs["params"])
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(tg, "preflight", lambda: {})
    monkeypatch.setattr(tg, "_load_offset", lambda: 0)
    monkeypatch.setattr(tg, "_save_offset", offsets.append)
    monkeypatch.setattr(tg.requests, "Session", Session)
    tg.poll_forever(
        lambda text: pytest.fail("callback became command"),
        stop_event=stopped,
        on_callback=seen.append,
    )
    assert seen == [cb] and offsets == [14]
    assert "callback_query" in parameters[0]["allowed_updates"]


def test_interrupted_approved_action_resolves_parked_job(monkeypatch):
    from core.approval_execution import REMOTE_APPROVAL

    era, _ = result_engine(monkeypatch)
    era._pending_store.store(
        "file_controller", {"action": "delete", "path": "C:/test.txt"}, "HIGH", "delete"
    )
    pending, outcomes = era._pending_store.peek(), []

    async def cancelled(*a, **k):
        raise asyncio.CancelledError

    era._execute_tool_raw = cancelled
    monkeypatch.setattr(
        "core.approval_execution.publish_approved_result", lambda cid, r: outcomes.append((cid, r))
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute_approved(era, pending))
    assert len(outcomes) == 1 and outcomes[0][0] == pending.confirmation_id
    assert outcomes[0][1].error_type == "ApprovalExecutionInterrupted"
    assert REMOTE_APPROVAL.get() is None and not era._pending_store.is_pending()


def test_remote_approval_resumes_standby_without_voice_unmute(monkeypatch):
    era, _ = result_engine(monkeypatch)
    era._pending_store.store(
        "file_controller", {"action": "delete", "path": "C:/test.txt"}, "HIGH", "delete"
    )
    era._standby_active, era._mic_muted = True, True
    resumed = []

    async def resume(reason, *, speak_ack):
        resumed.append((reason, speak_ack))
        era._standby_active = False

    era._resume_from_standby = resume
    result = asyncio.run(execute_approved(era, era._pending_store.peek()))
    assert result.success and resumed == [("remote_approval", False)] and era._mic_muted
