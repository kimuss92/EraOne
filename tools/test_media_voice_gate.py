"""Playback discrimination preserves quiet owner requests and quiet-room input."""

import json

import numpy as np
import pytest

from core.media_voice_gate import MediaVoiceGate, _MediaVerifier
from core.speaker_recognition import SpeakerProfile, voiced_segment
from core.voice_gate import UtteranceGate
from tools.test_speaker_gate import FakeEmbedder, make_verifier, run, tone


@pytest.fixture
def verifier(tmp_path):
    embedder = FakeEmbedder()
    vector = embedder.embed(np.frombuffer(tone(200, 2), "<i2").astype(np.float32) / 32768)
    path = tmp_path / "profile.json"
    SpeakerProfile("fake.onnx", vector, command_threshold=0.8, wake_threshold=0.8).save(path)
    return make_verifier(path, enabled=True)


def test_quiet_room_passes_immediately_without_identity_check(verifier, monkeypatch):
    monkeypatch.setattr(
        verifier, "verify", lambda *a: pytest.fail("quiet input must not wait for identity")
    )
    gate = MediaVoiceGate(verifier=verifier, media_active=lambda: False, synchronous=True)
    frame = tone(200, 0.064, amp=0.006)
    assert gate.feed(frame) == [frame]


@pytest.mark.parametrize("pitch,accepted", [(200, True), (520, False)])
def test_media_playback_checks_identity_and_keeps_quiet_owner(verifier, pitch, accepted):
    gate = MediaVoiceGate(
        verifier=verifier, media_active=lambda: True, synchronous=True, wearable=True
    )
    pcm = tone(pitch, 2.0, amp=0.007)
    output = run(gate, pcm)
    if accepted:
        assert output == pcm  # Prefix and all quiet speech survive in order.
    else:
        assert not any(output)
    assert verifier.threshold("command") == 0.8


def test_stopping_media_removes_a_previous_identity_rejection(verifier):
    active = [True]
    gate = MediaVoiceGate(verifier=verifier, media_active=lambda: active[0], synchronous=True)
    assert not any(run(gate, tone(520, 2)))
    active[0] = False
    frame = tone(200, 0.064)
    assert gate.feed(frame) == [frame]


def test_media_stopping_mid_request_preserves_buffered_words(verifier):
    active = [True]
    gate = MediaVoiceGate(verifier=verifier, media_active=lambda: active[0], synchronous=True)
    frame = tone(200, 0.064)
    first = gate.feed(frame)
    active[0] = False
    rest = gate.feed(frame)
    assert b"".join(first + rest) == frame + frame


def test_unknown_identity_is_reported_and_retained_not_silently_blocked(verifier, monkeypatch):
    events = []
    monkeypatch.setattr("core.media_voice_gate.log_event", lambda *a, **k: events.append(k))

    def fail(*args):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(verifier, "verify", fail)
    verdict = _MediaVerifier(verifier).verify(bytes(2048))
    assert verdict.accepted and verdict.score is None
    assert events[0]["identity_known"] is False


def test_quiet_voiced_frames_reach_identity_scoring_but_silence_does_not():
    speech = np.frombuffer(tone(200, 1.2, amp=0.007), "<i2").astype(np.float32) / 32768
    samples = np.concatenate([np.zeros(16000, dtype=np.float32), speech])
    assert len(voiced_segment(samples)) >= 16000
    assert len(voiced_segment(np.zeros(16000, dtype=np.float32))) == 0


def test_glasses_detection_floor_returns_to_pc_baseline(verifier):
    gate = UtteranceGate(verifier, synchronous=True)
    quiet = tone(200, 0.064, amp=0.007)
    gate.configure_input(True)
    assert gate._is_speech(quiet)
    gate.configure_input(False)
    assert not gate._is_speech(quiet)


@pytest.mark.parametrize(
    "word,confidence,accepted",
    [
        ("era", 0.96, True),
        ("era", 0.3, False),
        ("music", 0.99, False),
    ],
)
def test_explicit_local_address_rescues_mismatch_but_not_media_dialogue(
    verifier,
    word,
    confidence,
    accepted,
):
    class Recognizer:
        def AcceptWaveform(self, pcm):
            return False

        def FinalResult(self):
            return json.dumps({"result": [{"word": word, "conf": confidence}]})

    gate = MediaVoiceGate(
        verifier=verifier,
        media_active=lambda: True,
        synchronous=True,
        recognizer_factory=Recognizer,
    )
    pcm = tone(520, 4)  # Deliberate mismatch, also exercises rolling rechecks.
    output = run(gate, pcm)
    assert bool(any(output)) is accepted
    if accepted:
        assert output == pcm
        # The next utterance must be checked anew, not inherit the first address.
        run(gate, bytes(16000 * 2))
        gate._proxy._make_rec = None
        assert not any(run(gate, tone(520, 2)))


def test_broken_local_name_recognizer_never_overrides_mismatch(verifier):
    def broken():
        raise RuntimeError("unavailable")

    result = _MediaVerifier(verifier, broken).verify(tone(520, 2))
    assert not result.accepted and result.score is not None


def test_optional_media_filter_cannot_override_enabled_global_security_gate(monkeypatch):
    from types import SimpleNamespace

    from app.era_live.audio import AudioIOMixin

    monkeypatch.setattr(
        "core.speaker_recognition.get_verifier", lambda: SimpleNamespace(enabled=True)
    )
    era = SimpleNamespace(
        _attention_gate=lambda: pytest.fail("no second media check for locked input")
    )
    frame = tone(200, 0.064)
    assert AudioIOMixin._media_voice_frames(era, frame) == [frame]
