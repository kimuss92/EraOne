"""Speaker gate: only the enrolled voice reaches Gemini or wakes Era.

Uses a fake embedder whose "voice" is encoded in the audio itself (a sine
frequency), so the real model is not needed and nothing touches config/.
"""

import json

import numpy as np
import pytest

import core.user_prefs as up
from core.speaker_recognition import (
    SAMPLE_RATE,
    SpeakerProfile,
    SpeakerVerifier,
    load_settings,
)
from core.voice_gate import (
    SHORT_REPLY_HINT_EVERY_S,
    UtteranceGate,
    WakeAudioTap,
    pending_followup_allowed,
    short_reply_allowed,
    short_reply_hint_due,
    verify_wake_utterance,
)

FRAME = 1024  # matches app/audio_config.CHUNK_SIZE
OWNER_HZ, STRANGER_HZ = 200.0, 520.0


class FakeEmbedder:
    """Embedding = normalised FFT band energies, so different pitches ≈ orthogonal."""

    model_name = "fake.onnx"
    model_path = None

    def __init__(self):
        self.calls = 0

    def embed(self, samples):
        self.calls += 1
        spec = np.abs(np.fft.rfft(samples, n=4096))[:1024]
        bands = spec.reshape(32, 32).sum(axis=1).astype(np.float32)
        return bands / np.linalg.norm(bands)


def tone(hz, seconds, amp=0.3):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * hz * t) * 32767).astype("<i2").tobytes()


def frames(pcm):
    return [pcm[i : i + FRAME * 2] for i in range(0, len(pcm), FRAME * 2)]


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    """Pin the persisted voice-lock preference to ON for the whole module.

    load_settings() now reads voice_lock_enabled back from user_prefs.json, and
    conftest seeds the test memory dir with a COPY of the real store — so with the
    owner running with the lock off, every verifier here was built `disabled` and
    each "stranger is rejected" assertion passed vacuously. The gate under test
    must not depend on the developer's own preference.
    """
    path = tmp_path / "user_prefs.json"
    path.write_text(json.dumps({"version": 1, "voice_lock_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(up, "PREFS_PATH", path)
    return path


@pytest.fixture
def profile_path(tmp_path):
    emb = FakeEmbedder().embed(np.frombuffer(tone(OWNER_HZ, 2.0), "<i2") / 32768.0)
    path = tmp_path / "profile.json"
    SpeakerProfile("fake.onnx", emb, command_threshold=0.8, wake_threshold=0.8).save(path)
    return path


def make_verifier(profile_path, **overrides):
    settings = load_settings(profile_path.parent / "no-settings.json")
    settings.update(overrides)
    return SpeakerVerifier(settings, profile_path=profile_path, embedder=FakeEmbedder())


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += FRAME / SAMPLE_RATE
        return self.t


def run(gate, pcm):
    out = []
    for f in frames(pcm):
        out.extend(gate.feed(f))
    return b"".join(out)


def gate_for(verifier):
    return UtteranceGate(verifier, synchronous=True, clock=Clock(), on_event=lambda m: None)


def speech_bytes(out: bytes) -> int:
    return int(np.count_nonzero(np.frombuffer(out, "<i2")))


# ── verifier ─────────────────────────────────────────────────────────────────
def test_owner_accepted_stranger_rejected(profile_path):
    v = make_verifier(profile_path)
    assert v.verify(tone(OWNER_HZ, 1.5)).accepted
    r = v.verify(tone(STRANGER_HZ, 1.5))
    assert not r.accepted and "mismatch" in r.reason


def test_fail_closed_without_profile(tmp_path):
    v = make_verifier(tmp_path / "missing.json")
    assert v.readiness()[0] is False
    assert not v.verify(tone(OWNER_HZ, 1.5)).accepted


def test_fail_closed_on_model_mismatch(profile_path):
    v = make_verifier(profile_path)
    v._embedder.model_name = "other.onnx"
    assert not v.verify(tone(OWNER_HZ, 1.5)).accepted


def test_fail_closed_on_embedder_error(profile_path):
    v = make_verifier(profile_path)

    def boom(_):
        raise RuntimeError("onnx exploded")

    v._embedder.embed = boom
    r = v.verify(tone(OWNER_HZ, 1.5))
    assert not r.accepted and "error" in r.reason


def test_too_little_speech_rejected(profile_path):
    assert not make_verifier(profile_path).verify(tone(OWNER_HZ, 0.2)).accepted


def test_hand_edited_threshold_cannot_go_below_floor(tmp_path, profile_path):
    s = tmp_path / "settings.json"
    s.write_text('{"command_threshold": 0.0, "enabled": "no"}', encoding="utf-8")
    settings = load_settings(s)
    assert settings["command_threshold"] >= 0.40
    assert settings["enabled"] is True  # only a real JSON false disables it


# ── live mic gate ────────────────────────────────────────────────────────────
def test_stranger_speech_never_forwarded(profile_path):
    gate = gate_for(make_verifier(profile_path))
    out = run(gate, tone(0, 0.5, 0) + tone(STRANGER_HZ, 3.0) + tone(0, 1.2, 0))
    assert speech_bytes(out) == 0


def test_owner_speech_forwarded_complete_and_in_order(profile_path):
    gate = gate_for(make_verifier(profile_path))
    speech = tone(OWNER_HZ, 2.5)
    out = run(gate, tone(0, 0.5, 0) + speech + tone(0, 1.2, 0))
    assert speech in out  # nothing lost, nothing reordered


def test_stranger_taking_over_accepted_turn_is_cut(profile_path):
    gate = gate_for(make_verifier(profile_path))
    stranger = tone(STRANGER_HZ, 4.0)
    out = run(gate, tone(OWNER_HZ, 2.0) + stranger + tone(0, 1.2, 0))
    arr = np.frombuffer(out, "<i2")
    # Owner's audio passes; at most one re-verify window of the stranger leaks.
    leaked = speech_bytes(out) - int(2.0 * SAMPLE_RATE)
    assert leaked <= int(2.8 * SAMPLE_RATE)
    assert not np.any(arr[-int(1.0 * SAMPLE_RATE) :])


def test_not_ready_blocks_everything(tmp_path):
    gate = gate_for(make_verifier(tmp_path / "missing.json"))
    assert speech_bytes(run(gate, tone(OWNER_HZ, 3.0))) == 0


def test_disabled_passes_through(tmp_path):
    gate = gate_for(make_verifier(tmp_path / "missing.json", enabled=False))
    pcm = tone(OWNER_HZ, 1.0)
    assert run(gate, pcm) == pcm


def test_gate_never_raises(profile_path):
    gate = gate_for(make_verifier(profile_path))
    gate._verifier.readiness = None  # break it
    assert gate.feed(b"\x01\x02\x03") == [bytes(3)]


# ── standby wake word ────────────────────────────────────────────────────────
def _wake(profile_path, hz):
    tap = WakeAudioTap()
    for f in frames(tone(0, 1.0, 0) + tone(hz, 0.8) + tone(0, 0.5, 0)):
        tap.append(f)
    vosk = {"text": "era", "result": [{"word": "era", "start": 1.0, "end": 1.8, "conf": 1.0}]}
    return verify_wake_utterance(make_verifier(profile_path), tap, vosk)


def test_wake_word_from_owner_wakes(profile_path):
    assert _wake(profile_path, OWNER_HZ).accepted


def test_wake_word_from_stranger_does_not_wake(profile_path):
    assert not _wake(profile_path, STRANGER_HZ).accepted


def test_wake_without_enrollment_does_not_wake(tmp_path):
    tap = WakeAudioTap()
    tap.append(tone(OWNER_HZ, 1.0))
    assert not verify_wake_utterance(make_verifier(tmp_path / "x.json"), tap, {}).accepted


def test_wake_tap_slices_exact_timings():
    tap = WakeAudioTap()
    tap.append(tone(0, 1.0, 0))
    tap.append(tone(OWNER_HZ, 1.0))
    assert np.count_nonzero(np.frombuffer(tap.slice(0.0, 1.0), "<i2")) == 0
    assert len(tap.slice(1.0, 2.0)) == SAMPLE_RATE * 2


def test_cohort_centering_round_trips_and_scores(tmp_path):
    rng = np.random.default_rng(0)
    common = rng.normal(size=32).astype(np.float32) * 3
    owner = rng.normal(size=32).astype(np.float32)
    mu = common
    centroid = (owner) / np.linalg.norm(owner)
    path = tmp_path / "p.json"
    SpeakerProfile("fake.onnx", centroid, cohort_mean=mu).save(path)
    prof = SpeakerProfile.load(path)
    assert prof.cohort_mean is not None
    # Raw cosine is dominated by the shared component; centered scoring is not.
    assert prof.score(common + owner) > 0.99
    assert prof.score(common + rng.normal(size=32).astype(np.float32)) < 0.6


def test_corrupt_cohort_mean_fails_closed(tmp_path, profile_path):
    import json

    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    raw["cohort_mean"] = [1.0, 2.0]
    profile_path.write_text(json.dumps(raw), encoding="utf-8")
    v = make_verifier(profile_path)
    assert v.readiness()[0] is False
    assert not v.verify(tone(OWNER_HZ, 1.5)).accepted


# ── short replies ("ok", "yeah", "no") ───────────────────────────────────────
def _gate_with(profile_path, allowed=lambda: True, **overrides):
    v = make_verifier(profile_path, **overrides)
    return UtteranceGate(
        v, synchronous=True, clock=Clock(), on_event=lambda m: None, followup_allowed=allowed
    )


QUIET = tone(0, 1.2, 0)


def test_short_reply_right_after_verified_command_passes(profile_path):
    gate = _gate_with(profile_path)
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    short = tone(OWNER_HZ, 0.3)
    assert short[:4096] in run(gate, tone(0, 0.5, 0) + short + QUIET)


def test_short_reply_without_verified_context_is_ignored(profile_path):
    gate = _gate_with(profile_path)
    assert speech_bytes(run(gate, tone(0, 0.5, 0) + tone(OWNER_HZ, 0.3) + QUIET)) == 0


def test_short_reply_after_window_expires_is_ignored(profile_path):
    gate = _gate_with(profile_path, followup_window_s=2.0)
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    out = run(gate, tone(0, 3.0, 0) + tone(OWNER_HZ, 0.3) + QUIET)
    assert speech_bytes(out) == 0


def test_short_stranger_voice_in_window_is_refused(profile_path):
    gate = _gate_with(profile_path)
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    assert speech_bytes(run(gate, tone(STRANGER_HZ, 0.35) + QUIET)) == 0


def test_short_replies_cannot_chain(profile_path):
    gate = _gate_with(profile_path, followup_window_s=3.0)
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    assert speech_bytes(run(gate, tone(OWNER_HZ, 0.3) + QUIET)) > 0
    # the follow-up did not renew the window
    assert speech_bytes(run(gate, tone(0, 1.5, 0) + tone(OWNER_HZ, 0.3) + QUIET)) == 0


def test_no_short_reply_while_confirmation_pending(profile_path):
    gate = _gate_with(profile_path, allowed=lambda: False)
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    assert speech_bytes(run(gate, tone(OWNER_HZ, 0.3) + QUIET)) == 0


class _Pending:
    def __init__(self, tool="window_control", action="close", risk="MEDIUM", cid="c1", **params):
        self.tool = tool
        self.parameters = {"action": action, **params}
        self.risk_level = risk
        self.confirmation_id = cid


WINDOW_CLOSE = _Pending()


@pytest.mark.parametrize(
    ("pending", "allowed"),
    [
        (None, True),
        (_Pending("window_control", "close"), True),
        (_Pending("desktop_control", " Close "), True),  # normalised like the dispatchers
        (_Pending("file_controller", "create_folder"), True),
        (_Pending("file_controller", "copy"), True),
        # bounded action, but overwriting: needs a verifiable answer
        (_Pending("file_controller", "copy", overwrite=True), False),
        # MEDIUM, yet NOT bounded (security review 2026-09-26): verified answer required
        (_Pending("claude_bridge", "ask"), False),
        (_Pending("manage_preference", "waive_close_confirmation"), False),
        (_Pending("manage_preference", "waive_file_creation_confirmation"), False),
        (_Pending("generated_code", "execute"), False),
        (_Pending("file_controller", "write"), False),
        (_Pending("browser_control", "fill_form"), False),
        (_Pending("gmail_control", "authorize"), False),
        # risk above MEDIUM never, even for an allowlisted action
        (_Pending("window_control", "close", risk="HIGH"), False),
        (_Pending("window_control", "close", risk="BLOCKED"), False),
        (_Pending("window_control", "close", risk=""), False),  # unknown fails closed
        (_Pending("window_control", "close", risk=None), False),
    ],
)
def test_short_reply_policy(pending, allowed):
    assert short_reply_allowed(pending) is allowed


class _Store:
    def __init__(self, pending=None, boom=False):
        self._p, self._boom = pending, boom

    def peek(self):
        if self._boom:
            raise RuntimeError("store lock broken")
        return self._p


def test_followup_policy_fails_closed_when_the_store_errors():
    assert pending_followup_allowed(None) is True  # no store: nothing can be pending
    assert pending_followup_allowed(_Store()) is True
    assert pending_followup_allowed(_Store(WINDOW_CLOSE)) is True
    assert pending_followup_allowed(_Store(_Pending("claude_bridge", "ask"))) is False
    assert pending_followup_allowed(_Store(boom=True)) is False


def test_short_yes_answers_a_window_close_confirmation(profile_path):
    # 2026-09-26: every short "yes" during ANY confirmation was dropped by the gate
    gate = _gate_with(profile_path, allowed=lambda: pending_followup_allowed(_Store(WINDOW_CLOSE)))
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    assert speech_bytes(run(gate, tone(OWNER_HZ, 0.3) + QUIET)) > 0


def test_short_yes_cannot_answer_an_unbounded_confirmation(profile_path):
    risky = _Store(_Pending("claude_bridge", "ask"))
    gate = _gate_with(profile_path, allowed=lambda: pending_followup_allowed(risky))
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    assert speech_bytes(run(gate, tone(OWNER_HZ, 0.3) + QUIET)) == 0


def test_short_stranger_reply_to_a_window_close_is_refused(profile_path):
    gate = _gate_with(profile_path, allowed=lambda: pending_followup_allowed(_Store(WINDOW_CLOSE)))
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    assert speech_bytes(run(gate, tone(STRANGER_HZ, 0.35) + QUIET)) == 0


def test_short_reply_hint_timing():
    risky = _Pending("claude_bridge", "ask", cid="a")
    assert short_reply_hint_due(None, None, 0.0) is False
    assert short_reply_hint_due(WINDOW_CLOSE, None, 0.0) is False  # short "yes" works there
    assert short_reply_hint_due(risky, None, 100.0) is True
    assert short_reply_hint_due(risky, ("a", 100.0), 105.0) is False  # just shown
    assert short_reply_hint_due(risky, ("a", 100.0), 100.0 + SHORT_REPLY_HINT_EVERY_S) is True
    assert (
        short_reply_hint_due(_Pending("claude_bridge", "ask", cid="b"), ("a", 100.0), 101.0) is True
    )


def test_window_renews_after_era_reply(profile_path):
    clock = Clock()
    v = make_verifier(profile_path, followup_window_s=3.0)
    gate = UtteranceGate(v, synchronous=True, clock=clock, on_event=lambda m: None)
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)
    clock.t += 20.0  # Era speaks for 20 s: mic frames pause
    assert speech_bytes(run(gate, tone(OWNER_HZ, 0.3) + QUIET)) > 0


# ── wake retry ───────────────────────────────────────────────────────────────
def _vosk(start, end):
    return {"result": [{"word": "shazam", "start": start, "end": end, "conf": 1.0}]}


def test_wake_retry_verifies_both_takes_together(profile_path):
    from core.voice_gate import WakeVerifier

    t = [0.0]
    v = make_verifier(profile_path)
    calls = []
    real = v.verify

    def spy(pcm, purpose="command"):
        calls.append((purpose, len(pcm)))
        return real(pcm, purpose) if purpose == "command" else real(b"", purpose)

    v.verify = spy  # first-take wake checks always fail
    wv = WakeVerifier(v, clock=lambda: t[0])
    tap = WakeAudioTap()
    tap.append(tone(OWNER_HZ, 1.0))
    assert not wv.check(tap, _vosk(0.2, 0.8)).accepted
    t[0] = 3.0
    r = wv.check(tap, _vosk(0.2, 0.8))
    assert r.accepted and "repeat" in r.reason
    assert calls[-1][0] == "command" and calls[-1][1] > calls[0][1]


def test_wake_retry_still_rejects_stranger(profile_path):
    from core.voice_gate import WakeVerifier

    t = [0.0]
    wv = WakeVerifier(make_verifier(profile_path), clock=lambda: t[0])
    tap = WakeAudioTap()
    tap.append(tone(STRANGER_HZ, 1.0))
    assert not wv.check(tap, _vosk(0.1, 0.9)).accepted
    t[0] = 2.0
    assert not wv.check(tap, _vosk(0.1, 0.9)).accepted


def test_wake_retry_expires(profile_path):
    from core.voice_gate import WakeVerifier

    t = [0.0]
    v = make_verifier(profile_path)
    real = v.verify
    v.verify = lambda pcm, purpose="command": real(b"", purpose)  # everything fails
    wv = WakeVerifier(v, clock=lambda: t[0])
    tap = WakeAudioTap()
    tap.append(tone(OWNER_HZ, 1.0))
    wv.check(tap, _vosk(0.2, 0.8))
    t[0] = 30.0
    assert "repeat" not in wv.check(tap, _vosk(0.2, 0.8)).reason


def _verifier_hears_short(monkeypatch):
    """Make the verifier's speech measure (used by the gate to pick the path) report
    0.3 s, while the gate's own per-frame energy count sees a long utterance.

    That disagreement is the real bug: the gate's 64 ms energy frames counted a
    short word as >= 0.5 s, sent it for a FULL check, and the verifier — whose
    30 ms measure found less — refused it as "too little speech to verify",
    so the short-reply path never ran.
    """
    import core.voice_gate as vg

    monkeypatch.setattr(vg, "voiced_segment", lambda x, *a, **k: x[: int(0.3 * SAMPLE_RATE)])


def test_short_word_uses_short_reply_path_by_verifier_measure(profile_path, monkeypatch):
    gate = _gate_with(profile_path)
    run(gate, tone(OWNER_HZ, 2.0) + QUIET)  # verified command opens the window
    _verifier_hears_short(monkeypatch)
    reasons, purposes = [], []
    gate._on_event = reasons.append
    submit = gate._submit
    gate._submit = lambda pcm, purpose="command": (purposes.append(purpose), submit(pcm, purpose))
    assert speech_bytes(run(gate, tone(0, 0.3, 0) + tone(OWNER_HZ, 0.7) + QUIET)) > 0
    assert purposes == ["followup"]
    assert not any("too little speech" in r for r in reasons)


def test_short_word_by_verifier_measure_still_needs_verified_context(profile_path, monkeypatch):
    gate = _gate_with(profile_path)
    _verifier_hears_short(monkeypatch)
    assert speech_bytes(run(gate, tone(0, 0.3, 0) + tone(OWNER_HZ, 0.7) + QUIET)) == 0
