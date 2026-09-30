"""Typed voice-switch intent.

This is Era's offline lifeline. During a quota outage there is no Live session,
and the mic lives inside that session's TaskGroup — so a SPOKEN "switch to your
fish voice" can never be heard, and the set_voice tool is unreachable. Typing
it is the only route back to a working voice, which makes this regex the last
thing standing between a quota outage and a silent assistant.

It therefore has to be both reliable on real orders and strictly non-greedy: a
false positive would hijack an ordinary sentence and bounce the session for no
reason.
"""

import pytest

from app.text_intents import _voice_switch_intent


@pytest.mark.parametrize(
    "text",
    [
        "switch to fish voice",
        "switch to your fish voice",
        "Era, switch to your fish voice",
        "era switch to the fish voice",
        "use your fish voice",
        "go to fish",
        "change to primary voice",
        "switch to the cloned voice",
        "use my own voice",
        "please switch to your fish audio voice.",
        "  SWITCH TO FISH VOICE  ",
    ],
)
def test_recognises_fish_orders(text):
    assert _voice_switch_intent(text) == "fish"


@pytest.mark.parametrize(
    "text",
    [
        "switch to normal voice",
        "switch back to your normal voice",
        "Era, go back to the cloud voice",
        "change to gemini voice",
        "use your usual voice",
        "switch to your regular voice",
    ],
)
def test_recognises_cloud_orders(text):
    assert _voice_switch_intent(text) == "gemini"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "what do you think of my voice",
        "the fish restaurant was good",
        "can you switch to the other tab",
        "I like your voice",
        "tell me about fish news",
        "switch to fish voice and then open chrome",  # compound: not standalone
        "why did you switch to your fish voice",
        "remind me to switch to fish voice tomorrow",
    ],
)
def test_ignores_non_orders(text):
    """A false positive would bounce the session mid-conversation for nothing."""
    assert _voice_switch_intent(text) is None


def test_returns_none_for_none_safe():
    assert _voice_switch_intent(None) is None
