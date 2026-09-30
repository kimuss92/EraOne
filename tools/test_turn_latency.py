"""core/turn_latency.py — the turn-latency breakdown. (Board item 22.)

Driven entirely by a FAKE CLOCK. The subject is arithmetic over timestamps, and
a test that needs a real microphone, a real Live session or a real 800ms wait to
check subtraction is a test that will be deleted the first time it flakes.
"""

from __future__ import annotations

import pytest

from core.turn_latency import TurnLatency, enabled


class FakeClock:
    """Advances only when a test says so, so every duration below is exact."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def turn(clock):
    return TurnLatency(clock=clock)


def test_the_headline_number_is_last_word_to_first_sound(turn, clock):
    """`response_ms` is the one that matches the complaint. Everything else in
    the summary exists to say WHERE that number went."""
    turn.note_user_delta()  # user starts
    clock.tick(2.0)
    turn.note_user_delta()  # user's last word
    clock.tick(0.8)  # the endpointing wait
    turn.note_user_finished()
    clock.tick(1.2)  # the model thinking
    turn.note_model_output()
    clock.tick(0.3)  # audio reaching the speakers
    turn.note_audio_played()
    clock.tick(2.0)
    turn.note_turn_complete()

    s = turn.summary(silence_ms=800)
    assert s["response_ms"] == 2300, "0.8 + 1.2 + 0.3 is what the user waits"
    assert s["endpoint_wait_ms"] == 800
    assert s["model_ms"] == 1200
    assert s["playback_ms"] == 300
    assert s["total_ms"] == 6300
    assert s["configured_silence_ms"] == 800


def test_the_breakdown_adds_up_to_the_headline(turn, clock):
    """If the parts stop summing to the whole, the breakdown is lying about
    where the time went — which is the only thing it is for."""
    turn.note_user_delta()  # user starts talking
    clock.tick(1.0)
    turn.note_user_delta()  # their last word
    clock.tick(0.45)
    turn.note_user_finished()
    clock.tick(0.90)
    turn.note_model_output()
    clock.tick(0.25)
    turn.note_audio_played()

    s = turn.summary()
    assert (s["endpoint_wait_ms"], s["model_ms"], s["playback_ms"]) == (450, 900, 250)
    assert s["response_ms"] == 1600
    assert s["endpoint_wait_ms"] + s["model_ms"] + s["playback_ms"] == s["response_ms"]


def test_a_missing_mark_is_none_not_zero(turn, clock):
    """A turn where no audio ever played and a turn where it played instantly are
    different findings. Zero would merge them silently."""
    turn.note_user_delta()
    clock.tick(0.5)
    turn.note_user_finished()
    clock.tick(1.0)
    turn.note_model_output()
    # nothing ever reaches the speakers
    s = turn.summary()
    assert s["model_ms"] == 1000
    assert s["response_ms"] is None
    assert s["playback_ms"] is None


def test_only_the_first_output_counts(turn, clock):
    """Audio arrives as a stream of chunks. Latency is to the FIRST one; taking
    the last would report the length of the answer instead."""
    turn.note_user_delta()
    turn.note_user_finished()
    clock.tick(0.4)
    turn.note_model_output()
    clock.tick(5.0)
    turn.note_model_output()  # later chunks of the same answer
    turn.note_audio_played()
    turn.note_audio_played()
    assert turn.summary()["model_ms"] == 400


def test_the_last_user_delta_wins(turn, clock):
    """The transcript streams while the user talks; the proxy for "they stopped"
    is the LAST piece, not the first."""
    turn.note_user_delta()
    clock.tick(3.0)
    turn.note_user_delta()
    clock.tick(0.6)
    turn.note_user_finished()
    assert turn.summary()["endpoint_wait_ms"] == 600


def test_tool_time_accumulates_across_a_turn(turn, clock):
    turn.note_user_delta()
    turn.note_user_finished()
    turn.note_tool(120)
    turn.note_tool(80.6)
    turn.note_tool(None)  # a tool that never reported a duration
    turn.note_tool("nonsense")
    s = turn.summary()
    assert s["tool_ms"] == 200 and s["tool_count"] == 2


def test_a_barge_in_is_flagged_not_averaged_in(turn, clock):
    """An interrupted turn's numbers describe the interruption, not the latency.
    Flagged so whoever reads these can exclude them."""
    turn.note_user_delta()
    turn.note_user_finished()
    turn.note_model_output()
    turn.note_interrupted()
    assert turn.summary()["interrupted"] is True


def test_a_turn_with_no_user_speech_logs_nothing(turn, clock):
    """Typed messages and wake-ups produce no user speech. A log full of empty
    turns is how a measurement stops being read."""
    assert turn.summary() is None
    turn.note_turn_complete()
    assert turn.summary() is None


def test_reset_leaves_no_bleed_between_turns(turn, clock):
    """The tracker is reused for every turn. One turn's marks leaking into the
    next would make every number after the first wrong."""
    turn.note_user_delta()
    clock.tick(1.0)
    turn.note_user_finished()
    turn.note_model_output()
    turn.note_tool(500)
    turn.note_interrupted()
    turn.note_turn_complete()
    assert turn.summary() is not None

    turn.reset()
    assert turn.summary() is None
    turn.note_user_delta()
    clock.tick(0.2)
    turn.note_user_finished()
    s = turn.summary()
    assert s["endpoint_wait_ms"] == 200
    assert s["tool_ms"] == 0 and s["tool_count"] == 0
    assert s["interrupted"] is False


def test_a_broken_clock_never_takes_the_voice_down(clock):
    """This sits in the audio receive loop. Instrumentation that can throw is
    instrumentation that takes the voice with it."""

    def boom():
        raise RuntimeError("no clock")

    t = TurnLatency(clock=boom)
    t.note_user_delta()
    t.note_user_finished()
    t.note_model_output()
    t.note_audio_played()
    t.note_turn_complete()
    assert t.summary() is not None  # degraded numbers, but no exception


def test_durations_are_never_negative(clock):
    """A monotonic clock is used precisely so an NTP correction cannot produce a
    negative latency — but the floor is asserted rather than assumed."""
    t = TurnLatency(clock=clock)
    t.note_user_delta()
    clock.tick(5.0)
    t.note_user_finished()
    clock.t -= 10.0  # a clock that went backwards anyway
    t.note_model_output()
    assert t.summary()["model_ms"] >= 0


def test_the_off_switch(monkeypatch):
    monkeypatch.setenv("ERA_TURN_LATENCY", "0")
    assert enabled() is False
    monkeypatch.delenv("ERA_TURN_LATENCY")
    assert enabled() is True


# ── the wiring: one line per turn, and no bleed into the next ────────────────


class _Host:
    """The real `_log_turn_latency` borrowed onto a bare object.

    AudioIOMixin's own __init__ needs a Live session, a mic and a speaker; the
    method under test needs none of them. Borrowing it keeps this a test of the
    logging contract rather than of how much of EraLive can be faked.
    """

    def __init__(self, tracker):
        from app.era_live.audio import AudioIOMixin

        self.turn_latency = tracker
        self._log_turn_latency = AudioIOMixin._log_turn_latency.__get__(self)


def _capture(monkeypatch):
    import app.era_live.audio as audio_mod

    events = []
    monkeypatch.setattr(
        audio_mod, "log_event", lambda _logger, event, level, **kw: events.append((event, kw))
    )
    return events


def test_a_turn_logs_one_latency_line(monkeypatch, clock):
    events = _capture(monkeypatch)
    t = TurnLatency(clock=clock)
    t.note_user_delta()
    clock.tick(0.8)
    t.note_user_finished()
    clock.tick(1.5)
    t.note_model_output()
    t.note_audio_played()
    t.note_turn_complete()

    _Host(t)._log_turn_latency()

    assert len(events) == 1
    event, fields = events[0]
    assert event == "turn.latency"
    assert fields["endpoint_wait_ms"] == 800
    assert fields["model_ms"] == 1500
    assert fields["response_ms"] == 2300


def test_the_configured_silence_rides_along(monkeypatch, clock):
    """endpoint_wait_ms is a proxy; it is only interpretable next to the setting
    it is meant to reflect, so the two must be logged together."""
    import app.era_live.audio as audio_mod

    events = _capture(monkeypatch)
    import core.vad as vad_mod

    monkeypatch.setattr(
        vad_mod, "get_endpointing_config", lambda: {"enabled": True, "silence_ms": 650}
    )
    assert audio_mod  # the module under test imports it lazily, inside the method

    t = TurnLatency(clock=clock)
    t.note_user_delta()
    clock.tick(0.65)
    t.note_user_finished()
    _Host(t)._log_turn_latency()

    assert events[0][1]["configured_silence_ms"] == 650


def test_endpointing_disabled_logs_no_configured_value(monkeypatch, clock):
    """With endpointing off, Gemini's own defaults apply and Era does not know
    the number. Reporting a stale one would be worse than reporting none."""
    events = _capture(monkeypatch)
    import core.vad as vad_mod

    monkeypatch.setattr(
        vad_mod, "get_endpointing_config", lambda: {"enabled": False, "silence_ms": 800}
    )
    t = TurnLatency(clock=clock)
    t.note_user_delta()
    t.note_user_finished()
    _Host(t)._log_turn_latency()
    assert "configured_silence_ms" not in events[0][1]


def test_the_tracker_is_reset_even_when_nothing_is_logged(monkeypatch, clock):
    """THE bleed guard. A turn that logs nothing must still clear its marks, or
    the next turn inherits them and reports a latency spanning both."""
    events = _capture(monkeypatch)
    t = TurnLatency(clock=clock)
    t.note_user_delta()  # a turn with no finish and no output: summary() is None
    host = _Host(t)
    host._log_turn_latency()
    assert events == []

    clock.tick(30.0)  # the user says nothing for half a minute
    t.note_user_delta()
    clock.tick(0.4)
    t.note_user_finished()
    t.note_model_output()
    t.note_turn_complete()
    host._log_turn_latency()

    fields = events[0][1]
    # total_ms is the field that actually detects the bleed, and it is asserted
    # here because the obvious one does NOT: endpoint_wait_ms measures from the
    # LAST delta, which the new turn refreshes, so it reads 400ms either way.
    # total_ms measures from the FIRST, which is exactly what a missed reset
    # leaves stale — 30400ms, the gap between two separate turns reported as one.
    assert fields["total_ms"] == 400, "the previous turn's start bled through"
    assert fields["endpoint_wait_ms"] == 400


def test_the_off_switch_logs_nothing_and_still_resets(monkeypatch, clock):
    events = _capture(monkeypatch)
    monkeypatch.setenv("ERA_TURN_LATENCY", "0")
    t = TurnLatency(clock=clock)
    t.note_user_delta()
    clock.tick(1.0)
    t.note_user_finished()
    t.note_model_output()
    _Host(t)._log_turn_latency()
    assert events == []
    assert t.summary() is None, "disabled must not mean the marks pile up forever"
