"""Voice activation after a manual mute (app/era_live/mic_mute.py).

The owner's rule (changed 2026-11-16, was 2026-11-11): EVERY mute, whether by
voice, typing or the mic button, can be undone by voice ("activate mic"),
typing or the button. Ambient noise still cannot reopen it: the local listener
knows only the unmute phrases and the phrase must pass owner verification.
Live 2026-11-16 18:03: after "microphone off" the owner said "activate mic" for
40 s and nothing listened; only the mic button worked.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app.era_live.mic_mute as mic_module
from app.era_live.mic_mute import MicMuteMixin


class _UI:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.logs: list[str] = []

    def _broadcast(self, event: dict) -> None:
        self.events.append(event)

    def write_log(self, text: str) -> None:
        self.logs.append(text)


class _Recognizer:
    def __init__(self, model, rate, grammar) -> None:
        assert model == "offline-model"
        assert rate == 16000
        assert "activate" in grammar
        self.words = False

    def SetWords(self, enabled: bool) -> None:
        self.words = enabled

    def AcceptWaveform(self, data: bytes) -> bool:
        assert self.words is True
        return bool(data)

    def Result(self) -> str:
        return json.dumps(
            {
                "text": "activate mic",
                "result": [
                    {"word": "activate", "conf": 0.99, "start": 0.0, "end": 0.3},
                    {"word": "mic", "conf": 0.99, "start": 0.3, "end": 0.55},
                ],
            }
        )


class _Verifier:
    def check(self, tap, result):
        assert result["text"] == "activate mic"
        return SimpleNamespace(accepted=True, reason="owner verified")


class _Era(MicMuteMixin):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._mic_muted = False
        self._mic_muted_source = ""
        self._browser_mic_active = True
        self._vosk_model = "offline-model"
        self._unmute_verifier = _Verifier()
        self._unmute_queue = None
        self._unmute_task = None
        self._unmute_ignore_until = 0.0
        self._is_speaking = False
        self._last_interaction_timestamp = 0.0
        self.ui = _UI()
        self.out_queue: asyncio.Queue = asyncio.Queue()
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def _tg_ack(self, text: str) -> None:
        pass


@pytest.fixture(autouse=True)
def _quiet_persistence(monkeypatch):
    monkeypatch.setattr(
        mic_module,
        "vosk",
        SimpleNamespace(KaldiRecognizer=_Recognizer),
    )
    seen: list[tuple[bool, str]] = []
    monkeypatch.setattr(
        MicMuteMixin,
        "_persist_mic_muted",
        staticmethod(lambda value, source="": seen.append((value, source))),
    )


@pytest.mark.asyncio
async def test_a_voice_mute_is_reopened_by_the_owner_voice() -> None:
    """PF-008 (owner, 2026-11-16): "microphone off" by voice, then "activate mic"."""
    era = _Era(asyncio.get_running_loop())

    response = era._handle_set_microphone({"state": "off"})

    assert response.startswith("Microphone muted")
    assert era._mic_muted is True
    assert era._mic_muted_source == "voice"
    assert era._unmute_task is not None

    era._unmute_ignore_until = 0.0
    era._feed_unmute_audio(era._loop, b"\x01\x00" * 8000)
    await asyncio.wait_for(era._unmute_task, timeout=1.0)
    assert era._mic_muted is False


@pytest.mark.asyncio
async def test_an_unverified_voice_cannot_reopen_any_mute() -> None:
    """Noise protection: a stranger or the TV saying the phrase stays muted."""
    era = _Era(asyncio.get_running_loop())
    era._unmute_verifier = SimpleNamespace(
        check=lambda tap, result: SimpleNamespace(accepted=False, reason="not owner")
    )
    era._handle_set_microphone({"state": "off"})
    era._unmute_ignore_until = 0.0
    era._feed_unmute_audio(era._loop, b"\x01\x00" * 8000)
    await asyncio.sleep(0.1)
    assert era._mic_muted is True


@pytest.mark.asyncio
async def test_a_typed_order_unmutes_a_voice_mute_without_a_session() -> None:
    era = _Era(asyncio.get_running_loop())
    era._handle_set_microphone({"state": "off"})

    assert era._handle_typed_mic_order("Activate mic.") is True
    assert era._mic_muted is False
    assert era._handle_typed_mic_order("what time is it") is False


@pytest.mark.asyncio
async def test_a_ui_mute_is_reopened_by_the_owner_voice() -> None:
    era = _Era(asyncio.get_running_loop())

    assert era.mute_microphone("ui") is True
    assert era._mic_muted_source == "ui"
    assert era._unmute_queue is not None
    assert era._unmute_task is not None

    era._unmute_ignore_until = 0.0
    era._feed_unmute_audio(era._loop, b"\x01\x00" * 8000)
    await asyncio.wait_for(era._unmute_task, timeout=1.0)

    assert era._mic_muted is False
    assert era.mic_effective_active() is True
    assert any(event.get("type") == "mic_muted" and not event["muted"] for event in era.ui.events)
    assert any("Microphone on (voice)" in line for line in era.ui.logs)


@pytest.mark.asyncio
async def test_repeated_voice_off_repairs_the_listener() -> None:
    era = _Era(asyncio.get_running_loop())
    era._mic_muted = True
    era._mic_muted_source = "voice"

    response = era._handle_set_microphone({"state": "off"})

    assert response == "The microphone is already off. Say nothing."
    # A listener that died (recognizer error) is re-armed so voice unmute stays possible.
    assert era._unmute_task is not None
    era._stop_unmute_listener()


@pytest.mark.asyncio
async def test_disabled_verification_still_allows_voice_unmute(tmp_path) -> None:
    """Owner decision 2026-11-16 (dev-review finding on PF-008): with speaker
    verification DISABLED the real verifier accepts every voice, and the owner
    chose to keep voice unmute; only the strict grammar + confidence protect it.
    Real SpeakerVerifier + WakeVerifier, not a mocked verdict."""
    from core.speaker_recognition import SpeakerVerifier
    from core.voice_gate import WakeVerifier

    disabled = SpeakerVerifier(
        settings={"enabled": False, "model_path": "unused"},
        profile_path=tmp_path / "none.json",
        embedder=object(),
    )
    era = _Era(asyncio.get_running_loop())
    era._unmute_verifier = WakeVerifier(disabled)
    era._handle_set_microphone({"state": "off"})

    era._unmute_ignore_until = 0.0
    era._feed_unmute_audio(era._loop, b"\x01\x00" * 16000)
    await asyncio.wait_for(era._unmute_task, timeout=1.0)
    assert era._mic_muted is False
