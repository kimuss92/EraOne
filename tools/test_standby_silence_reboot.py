"""PF-045 guards: strict standby words, "Era silent", and a heard reboot line.

Live 2026-11-18: (1) at 23:28 Era went to standby on "mes amis" because the
model's enter_standby call was grounded by "Thank you" said 23 s earlier;
(2) the owner wants "Era silent" to work like "stay silent"; (3) at 17:50
"reboot Era One" restarted her in silence: the restart line went through
Gemini, whose audio is dropped once a shutdown is under way.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from types import SimpleNamespace

import pytest

from core.attention import is_silence_order, is_standby_order

_RealThread = threading.Thread  # the exit test patches threading.Thread globally

# ── 1. standby words ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "standby",
        "Stand by.",
        "Era, standby",
        "stand-by please",
        "take a break",
        "Era take a break now",
        "Take a break, sir.",
        "standby mode",
        "enter standby mode",
        "okay go to standby",
    ],
)
def test_standby_orders_are_recognised(text):
    assert is_standby_order(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "mes amis",
        "go offline",
        "thank you",
        "go to sleep",
        "goodbye",
        "stay silent",
        "what does standby do",
        "take a break from work tomorrow",
        "the server is on standby",
    ],
)
def test_other_sentences_are_not_standby_orders(text):
    assert not is_standby_order(text)


def test_typed_standby_uses_the_same_strict_matcher():
    import main

    assert main._STANDBY_INTENT.match("take a break")
    assert not main._STANDBY_INTENT.match("go offline")


# ── 2. "Era silent" ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["Era silent", "era, silent", "Era One silent now", "Hey Era, be silent", "Era silent, sir."],
)
def test_era_silent_is_a_silence_order(text):
    assert is_silence_order(text)


@pytest.mark.parametrize(
    "text", ["silent", "silent night", "my phone is on silent", "Era silent mode on my phone"]
)
def test_silent_without_her_name_or_with_more_words_is_not(text):
    assert not is_silence_order(text)


# ── dispatcher: enter_standby only on the words ─────────────────────────────


def _dispatch_era(*said: str, age_s: float = 1.0):
    now = time.time()
    calls = []
    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        ui=None,
        _recent_user_texts=[(now - age_s, t) for t in said],
        _inflight_user_text="",
        _last_user_text=said[-1] if said else "",
        enter_standby=lambda *a, **k: calls.append("standby"),
        _enter_inactivity=lambda source, **k: calls.append(f"inactivity:{source}"),
    )
    return era, calls


def _run(era):
    from app.era_live import tool_dispatch

    return asyncio.run(tool_dispatch.ToolDispatchMixin._execute_tool_raw(era, "enter_standby", {}))


def test_mes_amis_after_a_thank_you_does_not_sleep_her():
    """The exact live sequence of 2026-11-18 23:28."""
    era, calls = _dispatch_era("Everything is set up. Thank you.", "mes amis")
    out = _run(era)
    assert calls == []
    assert out.startswith("Not done")


@pytest.mark.parametrize("said", ["standby", "Era, take a break"])
def test_a_real_standby_order_sleeps_her(said):
    era, calls = _dispatch_era(said)
    _run(era)
    assert calls == ["standby"]


@pytest.mark.parametrize("said", ["stay silent", "Era silent"])
def test_a_silence_order_is_inactivity_not_standby(said):
    era, calls = _dispatch_era(said)
    _run(era)
    assert calls == ["inactivity:voice"]


# ── 3. a reboot line is heard ───────────────────────────────────────────────


def _speak_during_shutdown(
    monkeypatch, *, uses_speech_out, gemini_audio_arrives, rest_pcm=b"\x01\x00" * 8
):
    """Drive the real EraLive.speak() for a reboot line; returns (era, sent, local, out).

    `local` collects what the LOCAL voice (Fish/Kokoro via _local_say) was asked to say;
    `era._played` collects PCM played from Gemini's REST voice."""
    import main
    from core import tts_gemini

    rest_texts = []

    async def fake_rest(text):
        rest_texts.append(text)
        return rest_pcm

    monkeypatch.setattr(tts_gemini, "synthesize", fake_rest)

    monkeypatch.setattr(main.VOICE_CFG, "uses_speech_out", lambda: uses_speech_out)
    monkeypatch.setattr(main, "_EXIT_LINE_GEMINI_WAIT_S", 0.05)
    monkeypatch.setattr(main, "_EXIT_LINE_SYNTH_WAIT_S", 0.05)
    sent, local, out = [], [], []

    async def fake_send(era, text):
        sent.append(text)
        if gemini_audio_arrives:  # what audio.py's gate does when Gemini answers
            era._exit_audio_seen = True

    monkeypatch.setattr(main, "send_model_text", fake_send)

    async def scenario():
        async def local_say(text):
            local.append(text)

        played = []

        async def local_play(pcm):
            played.append(pcm)

        era = SimpleNamespace(
            session=object(),
            _played=played,
            _rest_texts=rest_texts,
            _local_play=local_play,
            _loop=asyncio.get_running_loop(),
            _shutdown_requested=True,
            _is_speaking=False,
            _local_say=local_say,
            local_speak=lambda t: False,
            _tg_ack=lambda t: None,
            _note_era_question=lambda t: None,
            speech_out=SimpleNamespace(say=lambda t, **k: out.append(t)),
        )
        main.EraLive.speak(era, "I'm restarting now, sir.")
        assert era._exit_speech_pending is True  # set before the exit thread looks
        for _ in range(30):
            await asyncio.sleep(0.01)
        return era

    return asyncio.run(scenario()), sent, local, out


def test_gemini_selected_says_the_exit_line_in_geminis_voice(monkeypatch):
    era, sent, local, out = _speak_during_shutdown(
        monkeypatch, uses_speech_out=False, gemini_audio_arrives=True
    )
    assert len(sent) == 1 and "I'm restarting now, sir." in sent[0]
    assert "Say aloud exactly this" in sent[0]  # said verbatim, not a reaction
    assert era._exit_line_audio_open is True  # the gate lets that turn's audio through
    assert local == [] and out == []  # NOT Fish / Kokoro
    assert era._exit_speech_pending is False


def test_gemini_silence_falls_back_to_geminis_rest_voice_never_fish(monkeypatch):
    """Live made no sound: the SAME sentence is synthesized with the SAME Gemini
    voice over the independent REST TTS - the local (Fish/Kokoro) voice is never used."""
    era, sent, local, out = _speak_during_shutdown(
        monkeypatch, uses_speech_out=False, gemini_audio_arrives=False
    )
    assert era._rest_texts == ["I'm restarting now, sir."]
    assert era._played == [b"\x01\x00" * 8]
    assert local == [] and out == []  # NOT Fish / Kokoro
    assert era._exit_line_audio_open is False  # no double voice if Live wakes up late
    assert era._exit_speech_pending is False


def test_gemini_everything_failing_is_silence_not_another_voice(monkeypatch):
    era, sent, local, out = _speak_during_shutdown(
        monkeypatch, uses_speech_out=False, gemini_audio_arrives=False, rest_pcm=b""
    )
    assert era._played == [] and local == [] and out == []
    assert era._exit_speech_pending is False  # the exit is never held open


def test_live_audio_means_no_rest_fallback(monkeypatch):
    era, sent, local, out = _speak_during_shutdown(
        monkeypatch, uses_speech_out=False, gemini_audio_arrives=True
    )
    assert era._rest_texts == [] and era._played == []  # one voice only


def test_fish_or_kokoro_selected_says_it_through_that_backend_only(monkeypatch):
    era, sent, local, out = _speak_during_shutdown(
        monkeypatch, uses_speech_out=True, gemini_audio_arrives=False
    )
    assert out == ["I'm restarting now, sir."]  # SpeechOut = the selected backend
    assert sent == [] and local == []
    assert era._exit_speech_pending is False  # the timer released the wait


def test_exit_gate_passes_only_the_exit_lines_own_turn():
    from app.era_live.audio import _exit_line_passes

    audio = SimpleNamespace(data=b"x", tool_call=None, server_content=None)
    done = SimpleNamespace(
        data=None, tool_call=None, server_content=SimpleNamespace(turn_complete=True)
    )
    call = SimpleNamespace(data=b"x", tool_call=object(), server_content=None)

    closed = SimpleNamespace(_exit_line_audio_open=False)
    assert not _exit_line_passes(closed, audio)  # PF-036: late chatter still dropped

    era = SimpleNamespace(_exit_line_audio_open=True, _exit_audio_seen=False)
    assert not _exit_line_passes(era, call)  # a tool call never runs while leaving
    assert _exit_line_passes(era, audio) and era._exit_audio_seen is True
    assert _exit_line_passes(era, done) and era._exit_line_audio_open is False
    assert not _exit_line_passes(era, audio)  # the turn ended: gate shut again


def test_the_exit_waits_for_the_local_exit_line(monkeypatch):
    from app.era_live import tool_dispatch
    from core import era_relaunch

    targets = []

    class _Capture:
        def __init__(self, target=None, **k):
            targets.append(target)

        def start(self):
            pass

    monkeypatch.setattr(tool_dispatch.threading, "Thread", _Capture)
    monkeypatch.setattr(era_relaunch, "hard_exit", lambda code=0: None)

    class _Exited(BaseException):
        pass

    exited_at = []

    def fake_exit(code):
        exited_at.append(time.time())
        raise _Exited

    monkeypatch.setattr(os, "_exit", fake_exit)

    era = SimpleNamespace(_is_speaking=False, _local_speaking=False, _exit_speech_pending=True)
    tool_dispatch.ToolDispatchMixin._exit_after_speaking(era, close_hud=False)

    done_at = []

    def finish_line():
        time.sleep(0.6)
        era._local_speaking = True
        era._exit_speech_pending = False
        time.sleep(0.6)
        era._local_speaking = False
        done_at.append(time.time())

    _RealThread(target=finish_line, daemon=True).start()
    with pytest.raises(_Exited):
        targets[0]()
    assert exited_at[0] >= done_at[0]
