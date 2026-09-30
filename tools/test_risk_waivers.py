"""Per-category HIGH-risk confirmation waivers ("stop asking before you shut down").

The user chose per-category over one global switch (2026-09-18). These tests pin the
boundary: a waived category skips the prompt for the LIVE user's own direct call, and
nothing else ever does — not other actions, not scheduled tasks, not the autonomous
agent, not a call the router rewrote.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.user_prefs as up
from app.era_live.tool_dispatch import _NEGATION_BEFORE, _RISK_WAIVE_COMMAND
from core import task_store
from core.main_tool_bridge import _build_suggested_confirmation_question
from core.safety_guard import classify_action
from core.tool_result import ToolResult
from core.user_prefs import risk_waiver as rw


@pytest.fixture(autouse=True)
def prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "PREFS_PATH", tmp_path / "user_prefs.json")
    return tmp_path / "user_prefs.json"


def live(tool, params):
    return classify_action(tool, params, honor_risk_waivers=True)


def _shutdown(**kw):
    return classify_action("computer_settings", {"action": "shutdown"}, **kw)


def test_default_shutdown_is_confirmed():
    s = _shutdown(honor_risk_waivers=True)
    assert s["risk_level"] == "HIGH" and s["requires_confirmation"] is True


def test_waived_shutdown_skips_the_prompt_for_the_live_user_only():
    up.set_risk_confirmation_waiver("shutdown", True)
    s = _shutdown(honor_risk_waivers=True)
    assert s["risk_level"] == "HIGH" and s["requires_confirmation"] is False
    assert live("computer_control", {"action": "power_off"})["requires_confirmation"] is False
    # Default (scheduler, autonomous agent): the waiver does not apply.
    assert _shutdown()["requires_confirmation"] is True


def test_scheduled_shutdown_keeps_its_own_gate():
    """Review finding: an injected scheduled task must not become an unattended shutdown."""
    up.set_risk_confirmation_waiver("shutdown", True)
    assert task_store.assess_step("computer_settings", {"action": "shutdown"})[
        "requires_confirmation"
    ]


def test_waiver_is_per_category():
    up.set_risk_confirmation_waiver("shutdown", True)
    for action in ("restart", "reboot", "restart_computer"):
        assert live("computer_settings", {"action": action})["requires_confirmation"]


def test_power_waiver_never_reaches_other_tools():
    """Review finding: gmail_control sign_out used to share the power branch by name."""
    for cat in rw.WAIVABLE:
        up.set_risk_confirmation_waiver(cat, True)
    assert live("gmail_control", {"action": "sign_out"})["requires_confirmation"]
    assert live("some_future_tool", {"action": "shutdown"})["requires_confirmation"]


def test_revoking_restores_the_gate():
    up.set_risk_confirmation_waiver("shutdown", True)
    msg = up.set_risk_confirmation_waiver("shutdown", False)
    assert "confirmation again" in msg
    assert _shutdown(honor_risk_waivers=True)["requires_confirmation"] is True


@pytest.mark.parametrize(
    ("spoken", "cat"),
    [
        ("shut down", "shutdown"),
        ("Power", "shutdown"),
        ("reboot", "restart"),
        ("restart", "restart"),
    ],
)
def test_spoken_aliases(spoken, cat):
    assert rw.normalize_category(spoken) == cat


@pytest.mark.parametrize(
    "category", ["delete", "kill", "email", "all", "high", "*", "", "sleep", "lock_screen"]
)
def test_unwaivable_categories_are_refused_and_stay_gated(category):
    msg = up.set_risk_confirmation_waiver(category, True)
    assert "can't have its confirmation waived" in msg
    assert live("file_controller", {"action": "delete", "path": r"D:\x.txt"})[
        "requires_confirmation"
    ]
    assert live("computer_control", {"action": "kill", "name": "notepad"})["requires_confirmation"]
    assert live("gmail_control", {"action": "send"})["requires_confirmation"]


def test_every_category_waived_still_leaves_destructive_actions_gated():
    for cat in rw.WAIVABLE:
        up.set_risk_confirmation_waiver(cat, True)
    assert live("file_controller", {"action": "delete", "path": r"D:\x"})["requires_confirmation"]
    assert live("computer_control", {"action": "kill_process", "name": "x"})[
        "requires_confirmation"
    ]


def test_granting_is_confirmation_gated_revoking_is_not():
    grant = classify_action("manage_preference", {"action": "waive_high_risk_confirmation"})
    revoke = classify_action("manage_preference", {"action": "require_high_risk_confirmation"})
    assert grant["requires_confirmation"] is True and revoke["requires_confirmation"] is False


def test_grant_prompt_names_the_category():
    r = ToolResult.fail(message="x", tool_name="manage_preference")
    r.error_type = "ConfirmationRequired"
    r.data = {"action": "waive_high_risk_confirmation", "category": "shutdown"}
    assert "shutdown without asking" in _build_suggested_confirmation_question(r)


def test_corrupt_prefs_fail_closed(prefs):
    prefs.write_text('{"risk_confirmation_waived": {"shutdown": true}}', encoding="utf-8")
    assert _shutdown(honor_risk_waivers=True)["requires_confirmation"] is True
    prefs.write_text("{not json", encoding="utf-8")
    assert _shutdown(honor_risk_waivers=True)["requires_confirmation"] is True


def test_media_restart_is_untouched():
    assert (
        "power" not in classify_action("netflix_control", {"action": "restart"})["reason"].lower()
    )


def test_prompt_block_names_only_the_waived_category():
    assert "HIGH-risk" not in up.format_prefs_for_prompt()
    up.set_risk_confirmation_waiver("shutdown", True)
    block = up.format_prefs_for_prompt()
    assert "categories ONLY: shutdown." in block and "still requires confirmation" in block


# ── pre-existing power-gate gaps closed alongside (review finding 5) ─────────
@pytest.mark.parametrize("action", ["shut down", "Shut-Down", "power off", "Power-Off"])
def test_spaced_or_hyphenated_power_actions_are_gated(action):
    s = classify_action("computer_settings", {"action": action})
    assert s["risk_level"] == "HIGH" and s["requires_confirmation"]


@pytest.mark.parametrize("desc", ["shut down the pc", "please reboot", "éteins l'ordinateur"])
def test_description_only_power_request_is_gated(desc):
    assert classify_action("computer_settings", {"action": "", "description": desc})[
        "requires_confirmation"
    ]


def test_description_without_power_words_is_not_escalated():
    s = classify_action("computer_settings", {"action": "", "description": "turn the volume up"})
    assert "power" not in s["reason"].lower()


# ── the grant must be grounded in the user's own words ───────────────────────
def _said(text):
    low = text.lower()
    return any(
        not _NEGATION_BEFORE.search(low[: m.start()]) for m in _RISK_WAIVE_COMMAND.finditer(low)
    )


@pytest.mark.parametrize(
    "text",
    [
        "stop asking before you shut down the computer",
        "don't ask me before shutting down",
        "shut down without asking from now on",
        "ne me demande plus avant d'éteindre",
        "arrête de me demander avant de redémarrer",
    ],
)
def test_waive_phrases_count(text):
    assert _said(text)


@pytest.mark.parametrize(
    "text", ["please ask me before you shut down", "don't stop asking before shutdown", "shut down"]
)
def test_other_phrases_do_not_count(text):
    assert not _said(text)


# ── close: blanket window-close waiver (owner-approved downgrade, 2026-09-19) ─
def _close(**kw):
    return classify_action("window_control", {"action": "close", "name": "notepad"}, **kw)


def test_default_window_close_is_confirmed():
    s = _close(honor_risk_waivers=True)
    assert s["risk_level"] == "MEDIUM" and s["requires_confirmation"] is True


def test_waived_close_skips_the_prompt_for_the_live_user_only():
    up.set_risk_confirmation_waiver("close", True)
    s = _close(honor_risk_waivers=True)
    # Still MEDIUM — only the prompt is dropped; the residual risk stays on the record.
    assert s["risk_level"] == "MEDIUM" and s["requires_confirmation"] is False
    assert live("desktop_control", {"action": "close", "name": "x"})["requires_confirmation"] is (
        False
    )
    # Default path (scheduler, autonomous agent): the waiver does not apply.
    assert _close()["requires_confirmation"] is True


def test_close_waiver_does_not_lift_the_shutdown_gate():
    up.set_risk_confirmation_waiver("close", True)
    assert _shutdown(honor_risk_waivers=True)["requires_confirmation"] is True


def test_close_waiver_never_reaches_force_kill_or_close_app():
    """Force-kill and process close_app keep their gate whatever "close" is waived to."""
    up.set_risk_confirmation_waiver("close", True)
    assert live("computer_control", {"action": "close_app", "name": "notepad"})[
        "requires_confirmation"
    ]
    assert live("computer_control", {"action": "kill", "name": "notepad"})["requires_confirmation"]


@pytest.mark.parametrize("spoken", ["close", "close window", "close windows", "fermer"])
def test_close_spoken_aliases(spoken):
    assert rw.normalize_category(spoken) == "close"


def test_close_grant_is_grounded_in_the_users_words():
    """Both halves of the grant gate: a waive phrase AND a close-naming token."""
    from app.era_live.tool_dispatch import _RISK_CATEGORY_WORDS

    assert "close" in _RISK_CATEGORY_WORDS
    text = "stop asking before you close windows"
    assert _said(text)  # waive-command half
    assert any(tok in text.lower() for tok in _RISK_CATEGORY_WORDS["close"])  # category half
