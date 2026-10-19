"""Session-state tools (shutdown / standby / mic) need a matching user order.

Regression: ambient media audio made Gemini Live call shutdown_era with no user
input (task_trace user_text ""), enter_standby on an Arabic recitation, and flip
the mic. The model's tool choice alone must never be enough.
"""

import time
from types import SimpleNamespace

import pytest

from app.era_live.tool_dispatch import _ungrounded_state_change


def _era(*texts: str, age_s: float = 1.0):
    now = time.time()
    return SimpleNamespace(
        _recent_user_texts=[(now - age_s, t) for t in texts],
        _inflight_user_text="",
        _last_user_text=texts[-1] if texts else "",
    )


@pytest.mark.parametrize("tool", ["shutdown_era", "enter_standby", "set_microphone"])
def test_no_user_input_is_refused(tool):
    assert _ungrounded_state_change(_era(), tool) is not None


@pytest.mark.parametrize(
    "tool,heard",
    [
        ("enter_standby", "استغفر الله العظيم"),
        ("shutdown_era", "استغفر الله العظيم"),
        ("shutdown_era", "the weather tomorrow looks fine"),
        ("set_microphone", "the weather tomorrow looks fine"),
    ],
)
def test_unrelated_speech_is_refused(tool, heard):
    assert _ungrounded_state_change(_era(heard), tool) is not None


@pytest.mark.parametrize(
    "tool,heard",
    [
        ("shutdown_era", "Okay, good bye and shut down yourself also."),
        ("shutdown_era", "I mean shut down yourself."),
        ("shutdown_era", "reboot Era One"),
        ("shutdown_era", "ferme-toi, au revoir"),
        ("enter_standby", "take a break for now era"),
        ("enter_standby", "nothing for now my friend just take a break"),
        ("enter_standby", "Resume Netflix and stand by."),
        ("set_microphone", "Microphone off."),
        ("set_microphone", "stop listening"),
    ],
)
def test_real_orders_pass(tool, heard):
    assert _ungrounded_state_change(_era(heard), tool) is None


def test_stale_order_does_not_ground():
    assert _ungrounded_state_change(_era("shut down yourself", age_s=300), "shutdown_era")


def test_other_tools_are_untouched():
    assert _ungrounded_state_change(_era(), "spotify_control") is None


def test_inflight_voice_turn_grounds():
    era = _era()
    era._inflight_user_text = "okay go to standby"
    era._inflight_started_at = time.time()
    assert _ungrounded_state_change(era, "enter_standby") is None
