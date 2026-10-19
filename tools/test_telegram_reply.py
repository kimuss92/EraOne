"""Owned reply delivery: all HTTP mocked, no owner account traffic."""

import asyncio
from types import SimpleNamespace

import pytest
import requests

import era_telegram
from core.input_envelope import InputEnvelope
from core.telegram_reply import TelegramReply, split_reply
from era_telegram import ReplyDeliveryResult


def owner():
    return InputEnvelope.create("Hello", "telegram")


def test_final_unicode_chunks_are_lossless_and_bounded():
    text = "😀" * 4000 + "e\u0301" * 1500 + "終"
    chunks = split_reply(text)
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-16-le")) // 2 <= 4000 for c in chunks)
    assert split_reply("") == []
    with pytest.raises(ValueError):
        split_reply(text, 1)


def test_rejects_local_input_owner():
    with pytest.raises(ValueError):
        TelegramReply(InputEnvelope.create("private", "typed"), "42")


def test_coalesces_preview_and_sends_final_once():
    async def scenario():
        calls = []
        preview_sent = asyncio.Event()

        async def sender(method, payload):
            calls.append((method, dict(payload)))
            preview_sent.set()
            return ReplyDeliveryResult("sent", message_id=12)

        reply = TelegramReply(owner(), "42", sender=sender, interval=0)
        reply.reveal("One.")
        reply.reveal("Two.")
        await asyncio.wait_for(preview_sent.wait(), 1)
        assert calls[0][1]["text"] == "One. Two."
        assert calls[0][1]["draft_id"] > 0
        assert await reply.finish() == "sent"
        assert await reply.finish() == "sent"
        assert [m for m, _ in calls] == ["sendMessageDraft", "sendMessage"]
        assert calls[-1][1]["text"] == "One. Two."
        with pytest.raises(RuntimeError):
            reply.reveal("late")

    asyncio.run(scenario())


def test_two_remote_owners_do_not_mix_replies():
    async def scenario():
        calls = []

        async def sender(method, payload):
            calls.append((method, dict(payload)))
            return ReplyDeliveryResult("sent")

        a, b = (
            TelegramReply(owner(), "42", sender=sender),
            TelegramReply(owner(), "42", sender=sender),
        )
        a.reveal("First")
        b.reveal("Second")
        await asyncio.gather(a.finish(), b.finish())
        assert sorted(c["text"] for m, c in calls if m == "sendMessage") == ["First", "Second"]
        assert a.events[-1]["request_id"] != b.events[-1]["request_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["unknown", "failed", "unsupported"])
def test_failed_preview_preserves_final_delivery(status):
    async def scenario():
        calls = []
        preview_sent = asyncio.Event()

        async def sender(method, payload):
            calls.append(method)
            if method == "sendMessageDraft":
                preview_sent.set()
                return ReplyDeliveryResult(status, "draft_failed")
            return ReplyDeliveryResult("sent")

        reply = TelegramReply(owner(), "42", sender=sender)
        reply.reveal("Answer")
        await asyncio.wait_for(preview_sent.wait(), 1)
        assert await reply.finish() == "sent"
        assert calls == ["sendMessageDraft", "sendMessage"]
        assert reply.events[0]["status"] == status

    asyncio.run(scenario())


def test_flood_control_waits_then_retries_explicit_rejection():
    async def scenario():
        calls, delays = [], []

        async def sender(method, payload):
            calls.append(method)
            return (
                ReplyDeliveryResult("limited", retry_after=7)
                if len(calls) == 1
                else ReplyDeliveryResult("sent")
            )

        async def sleep(delay):
            delays.append(delay)

        reply = TelegramReply(owner(), "42", sender=sender, sleep=sleep)
        reply.reveal("Answer")
        assert await reply.finish() == "sent"
        assert calls == ["sendMessage", "sendMessage"]
        assert delays == [7]

    asyncio.run(scenario())


def test_exhausted_flood_budget_stops_after_three_attempts():
    async def scenario():
        calls, delays = [], []

        async def sender(method, payload):
            calls.append(method)
            return ReplyDeliveryResult("limited", retry_after=3)

        async def sleep(delay):
            delays.append(delay)

        reply = TelegramReply(owner(), "42", sender=sender, sleep=sleep)
        reply.reveal("Answer")
        assert await reply.finish() == "limited"
        assert len(calls) == 3 and delays == [3, 3]

    asyncio.run(scenario())


@pytest.mark.parametrize("raises", [False, True])
def test_ambiguous_final_never_retries_or_sends_later_chunks(raises):
    async def scenario():
        calls = []

        async def sender(method, payload):
            calls.append(payload)
            if raises:
                raise requests.Timeout("secret authenticated url")
            return ReplyDeliveryResult("unknown", "network_or_acknowledgement")

        reply = TelegramReply(owner(), "42", sender=sender)
        reply.reveal("X" * 8001)
        assert await reply.finish() == "unknown"
        assert len(calls) == 1
        assert "secret" not in repr(reply.events)

    asyncio.run(scenario())


def test_long_final_sends_all_chunks_in_order():
    async def scenario():
        calls = []

        async def sender(method, payload):
            calls.append(payload["text"])
            return ReplyDeliveryResult("sent")

        reply = TelegramReply(owner(), "42", sender=sender)
        text = "😀" * 5001
        reply.reveal(text)
        assert await reply.finish() == "sent"
        assert "".join(calls) == text
        assert len(calls) == 3

    asyncio.run(scenario())


def test_cancel_suppresses_queued_final_and_reports_interruption():
    async def scenario():
        calls = []

        async def sender(method, payload):
            calls.append(method)
            return ReplyDeliveryResult("sent")

        reply = TelegramReply(owner(), "42", sender=sender)
        reply.reveal("Answer")
        await reply.cancel()
        assert await reply.finish() == "cancelled"
        assert calls == []
        assert reply.events[-1]["reason"] == "interrupted"

    asyncio.run(scenario())


def test_cancel_interrupts_backoff_without_later_sends():
    async def scenario():
        sleeping, forever = asyncio.Event(), asyncio.Event()
        calls = []

        async def sender(method, payload):
            calls.append(method)
            return ReplyDeliveryResult("limited", retry_after=30)

        async def sleep(delay):
            sleeping.set()
            await forever.wait()

        reply = TelegramReply(owner(), "42", sender=sender, sleep=sleep)
        reply.reveal("Answer")
        await asyncio.wait_for(sleeping.wait(), 1)
        await reply.cancel()
        assert calls == ["sendMessageDraft"]
        assert await reply.finish() == "cancelled"

    asyncio.run(scenario())


def test_capacity_failure_is_explicit_and_preserves_existing_text():
    async def scenario():
        calls = []

        async def sender(method, payload):
            calls.append(payload["text"])
            return ReplyDeliveryResult("sent")

        reply = TelegramReply(owner(), "42", sender=sender, max_chars=3)
        reply.reveal("abc")
        with pytest.raises(ValueError):
            reply.reveal("d")
        assert any(e["reason"] == "reply_capacity" for e in reply.events)
        assert await reply.finish() == "sent"
        assert calls == ["abc"]

    asyncio.run(scenario())


def test_reporter_failure_does_not_duplicate_final():
    async def scenario():
        calls = []

        async def sender(method, payload):
            calls.append(method)
            return ReplyDeliveryResult("sent")

        def reporter(event):
            raise RuntimeError("HUD offline")

        reply = TelegramReply(owner(), "42", sender=sender, reporter=reporter)
        reply.reveal("Answer")
        assert await reply.finish() == "sent"
        assert calls == ["sendMessage"]
        assert any(e["stage"] == "reporter" for e in reply.events)

    asyncio.run(scenario())


@pytest.fixture
def mock_http(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-real-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls = []
    response = SimpleNamespace(
        status_code=200, json=lambda: {"ok": True, "result": {"message_id": 9}}
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append(kwargs)
            return response

    monkeypatch.setattr(era_telegram.requests, "Session", Session)
    return response, calls


def test_transport_validates_final_acknowledgement(mock_http):
    response, calls = mock_http
    assert (
        era_telegram.send_reply_update("sendMessage", {"chat_id": "42", "text": "Hi"}).message_id
        == 9
    )
    response.json = lambda: {"ok": True, "result": {}}
    assert (
        era_telegram.send_reply_update("sendMessage", {"chat_id": "42", "text": "Hi"}).status
        == "unknown"
    )
    assert len(calls) == 2


def test_transport_blocks_non_owner_destination(mock_http):
    _, calls = mock_http
    result = era_telegram.send_reply_update("sendMessage", {"chat_id": "999", "text": "Hi"})
    assert result.reason == "unauthorized_destination"
    assert calls == []


def test_transport_exposes_retry_after(mock_http):
    response, _ = mock_http
    response.status_code = 429
    response.json = lambda: {"ok": False, "parameters": {"retry_after": 12}}
    result = era_telegram.send_reply_update("sendMessage", {"chat_id": "42", "text": "Hi"})
    assert result.status == "limited" and result.retry_after == 12


def test_transport_validates_draft_acknowledgement(mock_http):
    response, _ = mock_http
    response.json = lambda: {"ok": True, "result": True}
    result = era_telegram.send_reply_update(
        "sendMessageDraft", {"chat_id": "42", "text": "Hi", "draft_id": 1}
    )
    assert result.status == "sent"


def test_throttled_preview_coalesces_arrivals_during_wait():
    async def scenario():
        calls = []
        waiting, release, first = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def sender(method, payload):
            calls.append((method, dict(payload)))
            first.set()
            return ReplyDeliveryResult("sent")

        async def sleep(delay):
            assert delay == 1
            waiting.set()
            await release.wait()

        reply = TelegramReply(owner(), "42", sender=sender, sleep=sleep, clock=lambda: 0)
        reply.reveal("One")
        await asyncio.wait_for(first.wait(), 1)
        reply.reveal("Two")
        await asyncio.wait_for(waiting.wait(), 1)
        reply.reveal("Three")
        release.set()
        # Await a deterministic acknowledgement of the second preview.
        while len(calls) < 2:
            await asyncio.sleep(0)
        assert calls[1][1]["text"] == "One Two Three"
        assert await reply.finish() == "sent"
        assert calls[-1][1]["text"] == "One Two Three"

    asyncio.run(scenario())


def test_persistent_send_runs_off_event_loop(monkeypatch):
    async def scenario():
        import threading

        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()

        def blocking_send(method, payload):
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(2)
            return ReplyDeliveryResult("sent")

        monkeypatch.setattr("core.telegram_reply.send_reply_update", blocking_send)
        reply = TelegramReply(owner(), "42")
        reply.reveal("Answer")
        final = asyncio.create_task(reply.finish())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            # The loop remains usable while the HTTP worker is blocked.
            await asyncio.wait_for(asyncio.sleep(0), 1)
        finally:
            release.set()
        assert await final == "sent"

    asyncio.run(scenario())


def test_transport_timeout_returns_unknown_without_retry_or_secret_log(
    mock_http, monkeypatch, capsys
):
    calls = []

    class BrokenSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            calls.append(1)
            raise requests.Timeout("https://api.telegram.org/botSECRET/sendMessage")

    monkeypatch.setattr(era_telegram.requests, "Session", BrokenSession)
    result = era_telegram.send_reply_update("sendMessage", {"chat_id": "42", "text": "Hi"})
    assert result.status == "unknown" and len(calls) == 1
    assert "SECRET" not in repr(result) + capsys.readouterr().out


def test_empty_reply_does_not_send_anything():
    async def scenario():
        async def sender(method, payload):
            pytest.fail("Empty replies must not create Telegram messages")

        reply = TelegramReply(owner(), "42", sender=sender)
        assert await reply.finish() == "empty"

    asyncio.run(scenario())
