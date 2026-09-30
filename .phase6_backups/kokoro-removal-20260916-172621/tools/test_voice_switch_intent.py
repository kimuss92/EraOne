"""Typed voice-switch intent.

This is Era's offline lifeline. During a quota outage there is no Live session,
and the mic lives inside that session's TaskGroup — so a SPOKEN "switch to your
local voice" can never be heard, and the set_voice tool is unreachable. Typing
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
        "switch to local voice",
        "switch to your local voice",
        "Era, switch to your local voice",
        "era switch to the local voice",
        "change to offline voice",
        "use your local voice",
        "go to kokoro",
        "switch to on-device voice",
        "switch to ondevice voice",
        "please switch to your offline voice.",
        "  SWITCH TO LOCAL VOICE  ",
    ],
)
def test_recognises_local_orders(text):
    assert _voice_switch_intent(text) == "local"


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
    assert _voice_switch_intent(text) == "normal"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "what do you think of my voice",
        "the local restaurant was good",
        "can you switch to the other tab",
        "I like your voice",
        "tell me about local news",
        "switch to local voice and then open chrome",  # compound: not standalone
        "why did you switch to your local voice",
        "remind me to switch to local voice tomorrow",
    ],
)
def test_ignores_non_orders(text):
    """A false positive would bounce the session mid-conversation for nothing."""
    assert _voice_switch_intent(text) is None


def test_returns_none_for_none_safe():
    assert _voice_switch_intent(None) is None
