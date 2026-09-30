"""Typed commands must behave like spoken ones — the EraLive half.

The gate's own rules are covered in test_input_safety_gate.py. What is proved
here is the wiring around it:

  * a question Era asks arms the window that makes the next short message an
    ANSWER rather than an unclear fragment, and a statement disarms it;
  * every early return in _on_text_command answers a Telegram sender, instead of
    leaving them staring at silence while the mirror stays armed and relays Era's
    next unrelated remark into the chat.
"""

from __future__ import annotations

import time
import types

import pytest

import main as main_module


@pytest.fixture()
def era():
    """An EraLive-shaped stub carrying only what the methods under test touch.
    EraLive.__init__ opens a mic, loads Vosk and warms a file index — none of
    which this has any business exercising."""
    stub = types.SimpleNamespace()
    stub._awaiting_reply_until = 0.0
    stub._tg_reply_pending = False
    stub._pending_store = types.SimpleNamespace(is_pending=lambda: False)
    stub.session_manager = types.SimpleNamespace(get_state_snapshot=lambda: {"is_muted": False})

    for name in (
        "_note_era_question",
        "_awaiting_reply_active",
        "_text_state_snapshot",
        "_tg_ack",
    ):
        setattr(stub, name, getattr(main_module.EraLive, name).__get__(stub))
    return stub


class TestAwaitingReplyWindow:
    def test_a_question_arms_the_window(self, era):
        era._note_era_question("Should I try again to close Brave?")
        assert era._awaiting_reply_active()
        assert era._text_state_snapshot()["awaiting_reply"] is True

    def test_a_statement_disarms_it(self, era):
        era._note_era_question("Should I close Brave?")
        era._note_era_question("Brave is closed, sir.")
        # A stale question left armed would keep waving short fragments through
        # to the model long after there was anything to answer.
        assert not era._awaiting_reply_active()

    def test_trailing_punctuation_does_not_hide_the_question(self, era):
        era._note_era_question('"Should I proceed?"')
        assert era._awaiting_reply_active()

    def test_the_window_expires(self, era, monkeypatch):
        era._note_era_question("Which brand?")
        later = time.time() + main_module._AWAITING_REPLY_WINDOW_S + 1
        monkeypatch.setattr(main_module.time, "time", lambda: later)
        assert not era._awaiting_reply_active()


class TestTelegramAcknowledgement:
    def test_ack_replies_and_disarms_the_mirror(self, era, monkeypatch):
        sent = []
        monkeypatch.setattr(main_module, "_tg_notify", sent.append)
        era._tg_reply_pending = True

        era._tg_ack("Switched to dark mode, sir.")

        assert sent == ["Switched to dark mode, sir."]
        # Left armed, the next thing Era says to whoever is in the room would be
        # mirrored into the chat as if it answered this order.
        assert era._tg_reply_pending is False

    def test_ack_is_silent_when_the_order_came_from_the_desk(self, era, monkeypatch):
        sent = []
        monkeypatch.setattr(main_module, "_tg_notify", sent.append)

        era._tg_ack("Switched to dark mode, sir.")

        assert sent == []

    def test_ack_only_fires_once(self, era, monkeypatch):
        sent = []
        monkeypatch.setattr(main_module, "_tg_notify", sent.append)
        era._tg_reply_pending = True

        era._tg_ack("first")
        era._tg_ack("second")

        assert sent == ["first"]


class TestSayExactlyUnwrap:
    def test_a_wrapper_relays_only_its_payload(self):
        from app.text_intents import _say_exactly, _unwrap_say_exactly

        wrapped = _say_exactly("Back online, sir.")
        # The wrapper is an instruction addressed to Gemini. Relaying it verbatim
        # would show the user Era's internal scaffolding instead of the sentence.
        assert _unwrap_say_exactly(wrapped) == "Back online, sir."

    def test_ordinary_text_passes_through_untouched(self):
        from app.text_intents import _unwrap_say_exactly

        assert _unwrap_say_exactly("Brave is closed, sir.") == "Brave is closed, sir."
