"""Voice mic-mute: intents, the offline unmute grammar, and the mixin state machine."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from core.mic_mute import (
    MIC_OFF_ACK,
    MIC_ON_ACK,
    UNMUTE_DECOYS,
    UNMUTE_PHRASES,
    is_mic_off_order,
    is_mic_on_order,
    match_unmute_final,
    unmute_grammar,
    vosk_confidence,
)
from core.utils import BASE_DIR

# ── Intents ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "mute your mic",
        "Mute your microphone.",
        "Era, stop listening",
        "stop listening for a bit",
        "turn off your mic please",
        "turn your mic off",
        "mic off",
        "don't listen to me for now",
        "hey era can you mute the mic",
        "coupe ton micro",
        "arrête d'écouter",
    ],
)
def test_mic_off_orders(text):
    assert is_mic_off_order(text)
    assert not is_mic_on_order(text)


@pytest.mark.parametrize(
    "text",
    [
        "activate mic",
        "Activate your microphone",
        "start listening",
        "Era, start listening again",
        "resume listening",
        "turn your mic back on",
        "turn on the mic",
        "unmute your mic",
        "mic on",
        "you can listen again",
        "rallume ton micro",
    ],
)
def test_mic_on_orders(text):
    assert is_mic_on_order(text)
    assert not is_mic_off_order(text)


@pytest.mark.parametrize(
    "text",
    [
        "why did you mute your mic earlier",
        "I will stop listening to that podcast",
        "start listening to some jazz",
        "mute",  # system/media mute, not Era's mic
        "mute the tv",
        "turn off the lights",
        "my mic is broken",
        "",
    ],
)
def test_conversation_is_not_a_mic_order(text):
    assert not is_mic_off_order(text)
    assert not is_mic_on_order(text)


# ── Offline unmute grammar ───────────────────────────────────────────────────


def test_grammar_has_phrases_decoys_and_unk():
    vocab = json.loads(unmute_grammar())
    assert vocab[-1] == "[unk]"
    assert set(UNMUTE_PHRASES) <= set(vocab)
    # The mute order must have its own landing spot, or repeating it while muted
    # gets force-fitted onto "start listening".
    assert "stop listening" in vocab
    assert not set(UNMUTE_PHRASES) & set(UNMUTE_DECOYS)


def test_no_unmute_phrase_is_a_mute_order_and_vice_versa():
    for p in UNMUTE_PHRASES:
        assert is_mic_on_order(p), p
        assert not is_mic_off_order(p), p


_VOSK_MODEL = BASE_DIR / "models" / "vosk-model-small-en-us-0.15"


@pytest.mark.skipif(not _VOSK_MODEL.exists(), reason="Vosk model not installed")
def test_every_grammar_word_is_in_the_vosk_vocabulary():
    """Vosk silently DROPS a phrase with an unknown word ("unmute" is one).

    Run in a subprocess: Vosk's native stderr bypasses pytest's capfd on Windows,
    so an in-process check would pass vacuously.
    """
    pytest.importorskip("vosk")
    script = (
        "import sys, vosk\n"
        "vosk.SetLogLevel(0)\n"
        "m = vosk.Model(sys.argv[1])\n"
        "vosk.KaldiRecognizer(m, 16000, sys.argv[2])\n"
    )
    for grammar, expect_ok in (
        (unmute_grammar(), True),
        (json.dumps(["unmute mic", "[unk]"]), False),  # proves the check can fail
    ):
        proc = subprocess.run(
            [sys.executable, "-c", script, str(_VOSK_MODEL), grammar],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert "UpdateGrammarFst" in proc.stderr  # the log we inspect was emitted
        assert ("missing in vocabulary" not in proc.stderr) is expect_ok, proc.stderr[-2000:]


def test_match_unmute_final():
    assert match_unmute_final("start listening", 0.95)
    assert match_unmute_final("activate mic", 0.9)
    assert not match_unmute_final("start listening", 0.5)  # low confidence
    assert not match_unmute_final("start listening", None)  # unscored: fail closed
    assert not match_unmute_final("[unk] start listening", 0.99)  # inside a sentence
    assert not match_unmute_final("stop listening", 0.99)  # decoy
    assert not match_unmute_final("", 0.99)


def test_vosk_confidence():
    r = {"result": [{"word": "start", "conf": 1.0}, {"word": "listening", "conf": 0.8}]}
    assert vosk_confidence(r) == pytest.approx(0.9)
    assert vosk_confidence({"text": "x"}) is None


# ── Mixin state machine ──────────────────────────────────────────────────────


class _UI:
    def __init__(self):
        self.logs: list[str] = []
        self.broadcasts: list[dict] = []

    def write_log(self, text):
        self.logs.append(text)

    def _broadcast(self, msg):
        self.broadcasts.append(msg)


@pytest.fixture
def era(monkeypatch, tmp_path):
    import core.user_prefs as prefs
    from app.era_live.mic_mute import MicMuteMixin

    monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "user_prefs.json")

    class Stub(MicMuteMixin):
        def __init__(self):
            self.ui = _UI()
            self._browser_mic_active = True
            self._vosk_model = None  # no local listener in unit tests
            self._loop = None
            self._is_speaking = False
            self._last_interaction_timestamp = 0.0
            self.out_queue = None
            self.spoken: list[str] = []
            self.tg: list[str] = []

        def speak(self, text):
            self.spoken.append(text)

        def _tg_ack(self, message):
            self.tg.append(message)

    return Stub()


def test_mute_then_unmute(era):
    from core.user_prefs import get_mic_muted

    assert era.mic_effective_active()
    assert era.mute_microphone("voice") is True
    assert era._mic_muted and not era.mic_effective_active()
    assert get_mic_muted() is True
    assert MIC_OFF_ACK in era.spoken[-1]
    assert {"type": "browser_mic", "active": False} in era.ui.broadcasts

    assert era.mute_microphone("voice") is False  # idempotent: no second ack
    assert len(era.spoken) == 1

    assert era.unmute_microphone("voice") is True
    assert not era._mic_muted and era.mic_effective_active()
    assert get_mic_muted() is False
    assert MIC_ON_ACK in era.spoken[-1]
    assert era.unmute_microphone("voice") is False


def test_mute_drains_queued_audio(era):
    import asyncio

    q: asyncio.Queue = asyncio.Queue()
    for _ in range(3):
        q.put_nowait({"data": b"\0\0"})
    era.out_queue = q
    era.mute_microphone("voice")
    assert q.empty()


def test_mute_survives_restart(era):
    from app.era_live.mic_mute import MicMuteMixin

    era.mute_microphone("voice")

    class Fresh(type(era)):
        pass

    fresh = Fresh()
    assert not fresh._mic_muted
    fresh.restore_mic_mute()
    assert fresh._mic_muted
    assert isinstance(fresh, MicMuteMixin)


def test_tool_handler(era):
    assert "Say nothing" in era._handle_set_microphone({"state": "off"})
    assert era._mic_muted
    assert "already off" in era._handle_set_microphone({"state": "off"})
    era._handle_set_microphone({"state": "on"})
    assert not era._mic_muted
    assert "Unknown" in era._handle_set_microphone({"state": "sideways"})


def test_typed_orders_ack_telegram_once(era):
    assert era._handle_typed_mic_order("stop listening")
    assert era._mic_muted
    assert era.tg == [MIC_OFF_ACK]
    assert era._handle_typed_mic_order("start listening")
    assert not era._mic_muted
    assert era.tg[-1] == MIC_ON_ACK
    assert not era._handle_typed_mic_order("what's the weather")


def test_hard_off_keeps_indicator_off_after_unmute(era):
    era._browser_mic_active = False
    era.mute_microphone("voice")
    era.unmute_microphone("typed")
    assert not era.mic_effective_active()


def test_hint_mentions_fallback_without_vosk(era):
    era.mute_microphone("voice")
    assert any("Vosk model missing" in line for line in era.ui.logs)
