"""Inactivity enforces the existing clock even without an utterance boundary."""
import json

import numpy as np
import pytest

from core import attention
from core.attention import AttentionGate, ListeningState


class Recognizer:
    def __init__(self, text):
        self.text = text

    def AcceptWaveform(self, frame):
        pass

    def PartialResult(self):
        return json.dumps({"partial": self.text})

    def FinalResult(self):
        return json.dumps({"result": [
            {"word": word, "conf": 0.99} for word in self.text.split()
        ]})


@pytest.fixture
def setup_gate(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(attention, "_names", lambda: {"era"})
    state = ListeningState(clock=lambda: now[0], window_s=120)
    text = ["ordinary background speech"]
    gate = AttentionGate(state, lambda: Recognizer(text[0]))
    frame = (np.ones(640) * 9000).astype("<i2").tobytes()
    return now, state, text, gate, frame


def test_continuous_background_cannot_bypass_expiry(setup_gate):
    now, state, text, gate, frame = setup_gate
    assert gate.feed(frame) == [frame]
    now[0] = 121
    for _ in range(100):
        assert all(not any(out) for out in gate.feed(frame))
    assert not state.window_open()
    # A later quiet tail cannot re-anchor a formerly passing background segment.
    for _ in range(30):
        gate.feed(bytes(len(frame)))
    assert not state.window_open()


@pytest.mark.parametrize("wake", ["era", "era one"])
def test_wake_followup_and_next_expiry(setup_gate, wake):
    now, state, text, gate, frame = setup_gate
    now[0] = 121
    text[0] = wake
    assert gate.feed(frame) == [bytes(len(frame))]
    assert any(any(out) for out in gate.feed(frame))
    assert state.window_open()
    for _ in range(20):
        gate.feed(bytes(len(frame)))
    now[0] += 20
    text[0] = "open my calendar"
    assert gate.feed(frame) == [frame]
    now[0] += 121
    assert all(not any(out) for out in gate.feed(frame))
    assert not state.window_open()


@pytest.mark.parametrize("broken", [False, True])
def test_unavailable_recognition_never_opens_inactivity(setup_gate, broken):
    now, state, text, gate, frame = setup_gate
    now[0] = 121
    def fail():
        raise RuntimeError("recognizer offline")
    gate._make_rec = fail if broken else None
    for _ in range(8):
        assert gate.feed(frame) == [bytes(len(frame))]
    assert not state.window_open()


def test_dynamic_duration_still_controls_inactivity(setup_gate, monkeypatch):
    now, _, text, _, frame = setup_gate
    duration = [120.0]
    monkeypatch.setattr("core.session.window_seconds", lambda: duration[0])
    state = ListeningState(clock=lambda: now[0])
    gate = AttentionGate(state, lambda: Recognizer(text[0]))
    assert gate.feed(frame) == [frame]
    now[0] = 61
    duration[0] = 60
    assert gate.feed(frame) == [bytes(len(frame))]
    assert not state.window_open()


@pytest.mark.parametrize("text", ["new era", "an era of progress", "error", "eras"])
def test_mention_or_near_miss_is_not_a_wake(setup_gate, text):
    now, state, heard, gate, frame = setup_gate
    now[0] = 121
    heard[0] = text
    for _ in range(60):
        assert all(not any(out) for out in gate.feed(frame))
    assert not state.window_open()
    assert not attention.addressed_to_era(text, {"era"})
