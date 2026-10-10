"""What Era listens to (core/attention.py): echo hold + attention window.

Karim, 2026-10-10: Era answered her own voice and media, and answered phone
calls. A fake recognizer and synthetic frames drive the gate deterministically.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from core import attention
from core.attention import AttentionGate, ListeningState, addressed_to_era


def _speech() -> bytes:
    t = np.arange(1024) / 16000
    return (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()


SILENT = bytes(2048)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeRec:
    """Vosk stand-in: the partial reveals `text` after `after` frames."""

    def __init__(self, text: str, after: int = 3):
        self.text, self.after, self.n = text, after, 0

    def AcceptWaveform(self, _f):
        self.n += 1

    def PartialResult(self):
        return json.dumps({"partial": self.text if self.n >= self.after else ""})

    def FinalResult(self):
        return json.dumps({"text": self.text})


@pytest.fixture(autouse=True)
def _config(monkeypatch, tmp_path):
    monkeypatch.setattr(attention, "_VOICE_JSON", tmp_path / "voice.json")
    monkeypatch.setattr(attention, "_names", lambda: {"era", "shazam"})


def _closed_gate(text: str, after: int = 3):
    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 60  # well past the 20 s window: only named speech gets through
    events = []
    gate = AttentionGate(state, lambda: FakeRec(text, after), lambda k, h: events.append((k, h)))
    return gate, state, clock, events


# ── echo hold ────────────────────────────────────────────────────────────────
def test_the_mic_stays_closed_while_eras_last_words_still_play():
    clock = Clock()
    s = ListeningState(clock=clock)
    s.era_finished_speaking(interrupted=False)
    assert s.echo_hold_active()
    clock.t += 0.69
    assert s.echo_hold_active()
    clock.t += 0.02
    assert not s.echo_hold_active()


def test_no_hold_after_a_barge_in_the_user_is_mid_sentence():
    s = ListeningState(clock=Clock())
    s.era_finished_speaking(interrupted=True)
    assert not s.echo_hold_active()


# ── the conversation window ──────────────────────────────────────────────────
def test_inside_the_window_speech_passes_untouched():
    gate = AttentionGate(ListeningState(clock=Clock()), lambda: pytest.fail("no check needed"))
    frame = _speech()
    assert gate.feed(frame) == [frame]


def test_the_window_closes_after_its_length_and_reopens_on_activity():
    clock = Clock()
    s = ListeningState(clock=clock)
    clock.t += 44.5
    assert s.window_open()
    clock.t += 1.0
    assert not s.window_open()
    s.note_activity()
    assert s.window_open()


def test_named_speech_is_held_then_sent_whole_and_in_order():
    gate, state, _clock, events = _closed_gate("era what time is it")
    frames = [_speech() for _ in range(6)]
    first = [gate.feed(f) for f in frames[:3]]
    zero = bytes(len(frames[0]))
    assert first[0] == [zero] and first[1] == [zero], "silence while checking"
    assert first[2] == frames[:3], "then the held audio, whole and in order"
    assert [gate.feed(f) for f in frames[3:]] == [[f] for f in frames[3:]]
    assert [k for k, _ in events] == ["accepted"] and events[0][1].startswith("era:1.00")
    assert state.window_open(), "being addressed opens the conversation again"


def test_speech_not_addressed_to_era_never_leaves_the_machine():
    gate, state, _clock, events = _closed_gate("i'll call you back tomorrow")
    frames = [_speech() for _ in range(8)] + [SILENT] * 12
    out = [o for f in frames for o in gate.feed(f)]
    assert all(o == bytes(len(o)) for o in out), "a phone call reaches Gemini as silence only"
    assert [k for k, _ in events] == ["ignored"]
    assert not state.window_open()
    assert gate.mode == attention.IDLE, "the next utterance is judged afresh"


def test_quiet_frames_go_as_zeros_while_closed():
    gate, *_ = _closed_gate("")
    faint = np.full(1024, 50, dtype="<i2").tobytes()  # an earpiece across the room
    assert gate.feed(faint) == [bytes(len(faint))]


def test_a_pending_confirmation_keeps_era_listening():
    gate, *_ = _closed_gate("yes", after=99)
    frame = _speech()
    assert gate.feed(frame, keep_open=True) == [frame]


def test_the_wake_word_counts_as_her_name():
    gate, _s, _c, events = _closed_gate("shazam open notepad")
    for _ in range(3):
        gate.feed(_speech())
    assert events[0][0] == "accepted"


def test_no_vosk_model_means_fail_open():
    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 60
    gate = AttentionGate(state, None)
    frame = _speech()
    assert gate.feed(frame) == [frame]


def test_a_recognizer_crash_never_deafens_era():
    gate, *_ = _closed_gate("x")

    def boom():
        raise RuntimeError("vosk died")

    gate._make_rec = boom
    frame = _speech()
    assert gate.feed(frame) == [frame]


def test_window_zero_turns_the_rule_off(tmp_path, monkeypatch):
    vj = tmp_path / "voice.json"
    vj.write_text(json.dumps({"listening": {"attention_window_s": 0}}))
    monkeypatch.setattr(attention, "_VOICE_JSON", vj)
    clock = Clock()
    s = ListeningState(clock=clock)
    clock.t += 9999
    assert s.window_open()


def test_config_is_clamped(tmp_path, monkeypatch):
    vj = tmp_path / "voice.json"
    vj.write_text(json.dumps({"listening": {"echo_hold_ms": 99999, "name_check_s": 0}}))
    monkeypatch.setattr(attention, "_VOICE_JSON", vj)
    cfg = attention.get_listening_config()
    assert cfg["echo_hold_ms"] == 3000 and cfg["name_check_s"] == 0.6


@pytest.mark.parametrize(
    "text, expected",
    [
        ("era what time is it", True),
        ("hey era open spotify", True),
        ("[unk] era", True),
        ("shazam", True),
        ("i'll call you back tomorrow", False),
        ("here a minute", False),
        ("error in the report", False),
        ("tell my wife era is busy", False),  # her name later on: talking ABOUT her
        ("", False),
    ],
)
def test_addressed_to_era(text, expected):
    assert addressed_to_era(text, {"era", "shazam"}) is expected


# ── the model narrating its own silence is never spoken ─────────────────────
@pytest.mark.parametrize(
    "line",
    [
        "Organic noise detected.",
        "Say nothing.",
        "Silence maintained.",
        "(silence)",
        "Noise ignored.",
    ],
)
def test_narrated_silence_is_not_voiced(line):
    from app.era_live.tts_text import sanitize_for_tts

    assert sanitize_for_tts(line) == ""


def test_real_speech_about_noise_is_still_spoken():
    from app.era_live.tts_text import sanitize_for_tts

    line = "There is a lot of background noise in the kitchen, sir."
    assert sanitize_for_tts(line) == line


# ── the local loop (Gemini down) applies the same rule to its transcript ─────
def test_local_loop_ignores_speech_not_meant_for_era():
    from app.era_live.local_fallback import LocalFallbackMixin

    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 60

    class Era(LocalFallbackMixin):
        def _listening_state(self):
            return state

        def _attention_keep_open(self):
            return False

    era = Era.__new__(Era)
    assert era._local_addressed("I'll call you back tomorrow") is False
    assert era._local_addressed("Era, what time is it?") is True
    assert era._local_addressed("and set an alarm") is True, "the window reopened"


class ConfRec(FakeRec):
    """Vosk with SetWords(True): the final result carries per-word confidence."""

    def __init__(self, words):
        super().__init__(" ".join(w for w, _ in words), after=1)
        self.words = words

    def FinalResult(self):
        return json.dumps({"result": [{"word": w, "conf": c} for w, c in self.words]})


def test_a_near_miss_heard_as_era_at_low_confidence_is_not_her_name():
    """Live 2026-10-10: "Error in the report" decoded as era:0.51 / era:0.87."""
    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 60
    events = []
    gate = AttentionGate(
        state, lambda: ConfRec([("era", 0.51), ("era", 0.87)]), lambda k, h: events.append((k, h))
    )
    out = [o for _ in range(4) for o in gate.feed(_speech())]
    assert [k for k, _ in events] == ["ignored"]
    assert all(o == bytes(len(o)) for o in out)


def test_a_confident_era_is_her_name():
    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 60
    events = []
    gate = AttentionGate(
        state, lambda: ConfRec([("era", 0.95), ("[unk]", 0.54)]), lambda k, h: events.append((k, h))
    )
    gate.feed(_speech())
    gate.feed(_speech())
    assert [k for k, _ in events] == ["accepted"]


# ── live 2026-10-10: the window ran out while Era was still busy answering ───
class _BusyEra:
    def __init__(self, state="listening", local_task=None, local_speaking=False, pending=None):
        from types import SimpleNamespace

        from app.era_live.audio import AudioIOMixin

        self._attention_keep_open = AudioIOMixin._attention_keep_open.__get__(self)
        self._pending_store = SimpleNamespace(peek=lambda: pending)
        self.session_manager = SimpleNamespace(get_state_snapshot=lambda: {"current_state": state})
        self._local_turn_task = local_task
        self._local_speaking = local_speaking


class _Task:
    def __init__(self, done):
        self._done = done

    def done(self):
        return self._done


@pytest.mark.parametrize(
    "kwargs, open_",
    [
        ({}, False),
        ({"state": "processing"}, True),  # thinking / running a tool
        ({"local_task": _Task(done=False)}, True),  # local brain still answering
        ({"local_task": _Task(done=True)}, False),
        ({"local_speaking": True}, True),
        ({"pending": object()}, True),  # a confirmation waits for the answer
    ],
)
def test_era_keeps_listening_while_she_is_busy(kwargs, open_):
    assert _BusyEra(**kwargs)._attention_keep_open() is open_


def test_speech_the_gates_silence_is_not_evidence_that_the_user_spoke():
    """It made a healthy Gemini look mute and switched Era to the Claude voice."""
    from app.era_live.audio import AudioIOMixin

    seen = []

    class Health:
        def note_voice(self, rms, dt):
            seen.append((rms, dt))

    era = AudioIOMixin.__new__(AudioIOMixin)
    era.live_health = Health()
    era._note_sent_voice(bytes(2048))  # an ignored utterance reaches Gemini as zeros
    era._note_sent_voice(_speech())  # an accepted one is real audio
    assert seen[0] == (0.0, 1024 / 16000)
    assert seen[1][0] > 0.1


def test_finishing_a_local_reply_restarts_the_window():
    import inspect

    from app.era_live.local_fallback import LocalFallbackMixin

    src = inspect.getsource(LocalFallbackMixin._local_play_locked)
    assert "note_activity()" in src.split("finally:", 1)[1]


# ── 2026-10-10: Gemini gets time; the Claude rescue waits for a real failure ──
def test_the_watchdog_patience_comes_from_config(monkeypatch):
    import main

    monkeypatch.setattr(
        "core.llm.router.claude_voice_policy",
        lambda: {"deaf_after_s": 30.0, "mute_after_s": 60.0, "flaky_errors": 6},
    )
    h = main._build_live_health()
    assert (h._hear_timeout_s, h._reply_timeout_s, h._flaky_errors) == (30.0, 60.0, 6)


def test_a_broken_watchdog_config_keeps_the_defaults(monkeypatch):
    import main

    monkeypatch.setattr("core.llm.router.claude_voice_policy", lambda: {})
    h = main._build_live_health()
    assert (h._hear_timeout_s, h._reply_timeout_s, h._flaky_errors) == (12.0, 25.0, 3)


def test_handover_zero_means_only_after_every_retry():
    from app.era_live.local_fallback import handover_reason

    kw = {"offline": False, "max_attempts": 6, "upstream_after": 0}
    assert handover_reason(attempts=2, **kw) is None
    assert handover_reason(attempts=5, **kw) is None
    assert handover_reason(attempts=6, **kw) == "upstream"
    assert handover_reason(attempts=1, offline=True, max_attempts=6, upstream_after=0) == "network"
