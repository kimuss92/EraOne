"""Provider renewal must respect audio still buffered by the output driver."""

import time
from types import SimpleNamespace

import pytest

from app.era_live.reconnect_mixin import ReconnectMixin
from core.live_turn_state import LiveTurnState


@pytest.mark.parametrize("speaking", [False, True])
def test_idle_generation_is_not_idle_playback_until_speaker_finishes(speaking: bool) -> None:
    turn = LiveTurnState()
    turn.note_text("This final phrase is still in the device buffer.")
    turn.note_audio_received(64)
    turn.note_audio_played(64)
    turn.note_generation_complete()
    era = SimpleNamespace(
        live_turn=turn,
        _renewal_deadline=time.time() + 60,
        _is_speaking=speaking,
        RENEWAL_MARGIN_S=2,
    )
    ready, _ = ReconnectMixin._should_take_renewal_now(era)
    assert ready is (not speaking)


def test_provider_hard_deadline_still_allows_renewal_with_speech_outstanding() -> None:
    era = SimpleNamespace(
        live_turn=LiveTurnState(),
        _renewal_deadline=time.time() + 0.5,
        _is_speaking=True,
        RENEWAL_MARGIN_S=2,
    )
    ready, reason = ReconnectMixin._should_take_renewal_now(era)
    assert ready and "deadline" in reason
