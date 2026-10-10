"""The cancel/correction grammar, and the four vocabularies it must not steal.

Most of this file is negative. The grammar's whole risk is collision: "stop"
is a media command, "cancel" is a confirmation answer, "cancel the shutdown"
is the power countdown, and "stop everything" is the panic latch. Each already
means something the owner relies on, so the tests that matter are the ones
proving those still mean it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cancel_intent import correction_target, is_cancel_task_order  # noqa: E402

WAKE = ("shazam",)


# ── it does fire ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "said",
    [
        "cancel that",
        "cancel that task",
        "cancel the task",
        "stop that",
        "stop that task",
        "stop the search",
        "drop it",
        "drop that",
        "forget that",
        "abandon that job",
        "era cancel that",
        "hey era, stop that one",
        "cancel what you're doing",
        "annule ça",
        "laisse tomber ça",
    ],
)
def test_these_cancel_the_running_task(said):
    assert is_cancel_task_order(said, WAKE) is True


# ── the reserved vocabularies ────────────────────────────────────────────


@pytest.mark.parametrize("said", ["stop", "cancel", "abort", "pause", "yes", "no", "wait"])
def test_a_bare_verb_is_never_a_task_cancel(said):
    """A bare "stop" is a media command and "cancel" is a confirmation answer.

    A demonstrative or a task noun is mandatory, which is the single rule that
    keeps both of those working.
    """
    assert is_cancel_task_order(said, WAKE) is False


@pytest.mark.parametrize(
    "said",
    ["stop the music", "stop playing", "stop listening", "pause the video", "stop the song"],
)
def test_media_commands_are_not_task_cancels(said):
    assert is_cancel_task_order(said, WAKE) is False


@pytest.mark.parametrize(
    "said",
    ["cancel the shutdown", "abort the reboot", "stop the restart", "cancel the power off"],
)
def test_the_power_countdown_keeps_its_own_words(said):
    """safety_guard._POWER_ABORT_ACTIONS owns these; stealing them would mean
    "cancel the shutdown" cancelled a search and let the machine power off."""
    assert is_cancel_task_order(said, WAKE) is False
    assert correction_target(said, WAKE) is None


def test_every_abort_order_still_belongs_to_the_panic_latch():
    """Looped over panic_stop's own vocabulary, so a future edit there cannot
    silently downgrade an abort into a one-task cancel."""
    from core.panic_stop import is_abort_order

    for said in (
        "abort mission",
        "abort the mission",
        "stop everything",
        "abort abort",
        "abort abort abort",
        "emergency stop",
        "stand down",
        "panic button",
        "arrête tout",
        "abandonne la mission",
        "mission abort",
    ):
        assert is_abort_order(said, WAKE) is True, f"{said!r} must stay an abort"
        assert is_cancel_task_order(said, WAKE) is False, f"{said!r} must not downgrade"
        assert correction_target(said, WAKE) is None


def test_talking_about_a_cancel_is_not_ordering_one():
    for said in (
        "why did you cancel that task",
        "did you stop that search",
        "cancel the nine am schedule",
        "i told you to cancel that yesterday",
    ):
        assert is_cancel_task_order(said, WAKE) is False


# ── corrections ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "said,want",
    [
        ("no, I meant the next track", "the next track"),
        ("no I meant the next track", "the next track"),
        ("not that, play Daft Punk", "play Daft Punk"),
        ("I said the next one", "the next one"),
        ("no, play the next track", "play the next track"),
        ("actually, open my calendar", "open my calendar"),
        ("wait, I wanted the other playlist", "the other playlist"),
        ("hold on, search for something else", "search for something else"),
    ],
)
def test_the_replacement_command_is_extracted(said, want):
    assert correction_target(said, WAKE) == want


@pytest.mark.parametrize("said", ["no", "nope", "no, cancel that", "no, stop that task"])
def test_a_correction_naming_nothing_is_a_bare_cancel(said):
    """ "" is not None: it means drop the task and run nothing in its place."""
    assert correction_target(said, WAKE) == ""


@pytest.mark.parametrize("said", ["no, uh", "no, um", "no, I meant the"])
def test_a_half_heard_correction_is_not_dispatched(said):
    """A partial transcript must never be run as a command."""
    assert correction_target(said, WAKE) is None


@pytest.mark.parametrize(
    "said",
    ["play the next track", "open spotify", "what's the weather", "era what time is it"],
)
def test_an_ordinary_command_is_not_a_correction(said):
    """Without the leading negation every sentence spoken during a task would
    look like a correction, and Era would cancel her own work constantly."""
    assert correction_target(said, WAKE) is None


def test_empty_input_is_inert():
    assert is_cancel_task_order("", WAKE) is False
    assert correction_target("", WAKE) is None
    assert is_cancel_task_order("   ", WAKE) is False
