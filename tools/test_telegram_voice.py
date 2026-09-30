"""Telegram voice notes — authorization, routing, and failure reporting.

The security property under test is the same one the text channel has: audio
from anyone but the owner's chat id must never reach on_command. The behavioural
property is that a voice note ends up as TEXT on the ordinary typed entry point,
so it inherits standby handling, the safety gate and the confirmation flow
rather than growing a parallel set of half-rules.
"""

from __future__ import annotations

import pytest

import era_telegram


@pytest.fixture(autouse=True)
def owner_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "424242")
    return "424242"


def _voice_update(chat_id: str, file_id: str = "AwACAgQ") -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": int(chat_id)},
            "voice": {"file_id": file_id, "duration": 3, "mime_type": "audio/ogg"},
        },
    }


class TestAuthorization:
    def test_owner_voice_note_is_accepted(self, owner_chat):
        ok, msg = era_telegram._authorized(_voice_update(owner_chat))
        assert ok
        assert era_telegram._voice_payload(msg)["file_id"] == "AwACAgQ"

    def test_stranger_voice_note_is_refused(self):
        ok, msg = era_telegram._authorized(_voice_update("999"))
        assert not ok
        assert msg == {}

    def test_a_forwarded_document_is_not_a_command(self):
        # A document can hold audio, but a forwarded file is data to look at,
        # not an order to obey. Accepting it would let anything the owner
        # forwards into the chat issue commands on this machine.
        msg = {"document": {"file_id": "x", "mime_type": "audio/ogg"}}
        assert era_telegram._voice_payload(msg) is None


class TestVoiceRouting:
    def test_transcript_is_echoed_then_run_as_a_command(self, monkeypatch):
        sent, ran = [], []
        monkeypatch.setattr(era_telegram, "transcribe_voice_message", lambda msg: "close brave")
        monkeypatch.setattr(era_telegram, "send_message", lambda t, **k: sent.append(t) or True)

        era_telegram._handle_voice_message({"voice": {"file_id": "x"}}, ran.append)

        # The echo matters: a voice command can be HEARD WRONG, and without
        # seeing the transcript a mistranscription is indistinguishable from
        # Era refusing the order.
        assert sent == ["Heard: close brave"]
        assert ran == ["close brave"]

    def test_silence_reports_back_instead_of_vanishing(self, monkeypatch):
        sent, ran = [], []
        monkeypatch.setattr(era_telegram, "transcribe_voice_message", lambda msg: "")
        monkeypatch.setattr(era_telegram, "send_message", lambda t, **k: sent.append(t) or True)

        era_telegram._handle_voice_message({"voice": {"file_id": "x"}}, ran.append)

        assert ran == []
        assert sent and "couldn't hear" in sent[0]

    def test_transcription_failure_is_reported_not_swallowed(self, monkeypatch):
        sent, ran = [], []

        def _boom(msg):
            raise RuntimeError("ffmpeg isn't on PATH")

        monkeypatch.setattr(era_telegram, "transcribe_voice_message", _boom)
        monkeypatch.setattr(era_telegram, "send_message", lambda t, **k: sent.append(t) or True)

        era_telegram._handle_voice_message({"voice": {"file_id": "x"}}, ran.append)

        assert ran == []
        assert sent and "ffmpeg isn't on PATH" in sent[0]

    def test_a_command_that_fails_still_answers_the_sender(self, monkeypatch):
        sent = []

        def _explode(text):
            raise RuntimeError("the close action blew up")

        monkeypatch.setattr(era_telegram, "transcribe_voice_message", lambda msg: "close brave")
        monkeypatch.setattr(era_telegram, "send_message", lambda t, **k: sent.append(t) or True)

        era_telegram._handle_voice_message({"voice": {"file_id": "x"}}, _explode)

        assert any("blew up" in s for s in sent)
