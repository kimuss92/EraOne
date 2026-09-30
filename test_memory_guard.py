"""
Regression tests for the save_memory authorization guard in main.py
(EraLive._execute_tool_raw).

Context: a live test showed Era calling save_memory UNPROMPTED to persist a
guessed brand "lock" (delta_cuisine_lock) while confused. The old guard only
warned ("called without explicit user trigger — allowing with audit") and wrote
anyway. The tightened rule: a save is allowed ONLY when the user explicitly asked
OR a deliberate CM-pipeline step opened the gate (self._memory_write_authorized);
otherwise it is BLOCKED and nothing is persisted.

Binds the real _execute_tool_raw onto a bare stub (no EraLive.__init__ / mic /
Vosk) and monkeypatches the module-level update_memory + audit_event so no real
files are touched.

The patch target is `app.era_live.tool_dispatch`, NOT `main`. The 2026-07-23
refactor moved _execute_tool_raw into the ToolDispatchMixin, so that module is
where update_memory/audit_event are now resolved; patching `main` silently
stopped intercepting them (and then failed outright once main no longer
re-exported the names). `main.EraLive` is still the right handle for the
methods themselves — main re-exports the class.

Run:  pytest test_memory_guard.py -v
"""

import types

import pytest

import main as main_module
from app.era_live import tool_dispatch as dispatch_module


def _make_era(monkeypatch, *, last_user_text="", authorized=False):
    saves = []
    monkeypatch.setattr(dispatch_module, "update_memory", lambda payload: saves.append(payload))
    monkeypatch.setattr(dispatch_module, "audit_event", lambda **kw: None)

    class _Stub:
        pass

    era = _Stub()
    era.session_manager = types.SimpleNamespace(get_state_snapshot=lambda: {})
    era._last_user_text = last_user_text
    era._memory_write_authorized = authorized
    # The gate is no longer a bare read of _last_user_text: it now also consults
    # a rolling window of recent utterances and a post-block affirmation (the
    # ask and the save often land several tool-turns apart on voice). Bind the
    # real method and give it empty history, so `last_user_text` alone still
    # decides — which is exactly what these tests vary.
    era._recent_user_texts = []
    era._blocked_memory_save = None
    era._memory_save_authorized = main_module.EraLive._memory_save_authorized.__get__(era)
    # Second gate path: Era saving on her own initiative. With no recent user
    # speech to ground a value in, this always declines — which keeps these
    # tests measuring the explicit-request path they were written for.
    era._auto_memory_saves = []
    era._autonomous_memory_save_allowed = (
        main_module.EraLive._autonomous_memory_save_allowed.__get__(era)
    )
    # No brand-ask pending, so the new pending-ask tool guard is a no-op here.
    era._pending_cm_ask = None
    era._pending_cm_ask_active = main_module.EraLive._pending_cm_ask_active.__get__(era)
    # No CM hand-off window open, so that tool guard is a no-op here too.
    era._cm_handoff_until = 0.0
    era._cm_handoff_active = main_module.EraLive._cm_handoff_active.__get__(era)
    era._execute_tool_raw = main_module.EraLive._execute_tool_raw.__get__(era)
    return era, saves


# --------------------------------------------------------------------------- #
# 1. The exact failing case: a brand "lock" save while the last user text was a
#    plain CM request (no "remember/save" trigger) must be BLOCKED.
# --------------------------------------------------------------------------- #
async def test_unprompted_brand_lock_save_is_blocked(monkeypatch):
    era, saves = _make_era(monkeypatch, last_user_text="prepare this week's content plan")
    result = await era._execute_tool_raw(
        "save_memory",
        {
            "category": "operational_instruction",
            "key": "delta_cuisine_lock",
            "value": "Instagram @delta_cuisine_group, Facebook DELTA CUISINE. Notify kimuss92@gmail.com.",
        },
    )
    assert "not saved" in result.lower()
    assert saves == []  # nothing persisted


# --------------------------------------------------------------------------- #
# 2. An explicit user request ("remember that ...") is allowed and persisted.
# --------------------------------------------------------------------------- #
async def test_explicit_request_is_allowed(monkeypatch):
    era, saves = _make_era(monkeypatch, last_user_text="remember that my wife's name is Nesrine")
    result = await era._execute_tool_raw(
        "save_memory",
        {
            "category": "relationships",
            "key": "wife_name",
            "value": "Nesrine",
        },
    )
    assert result == "ok"
    assert saves == [{"relationships": {"wife_name": {"value": "Nesrine"}}}]


# --------------------------------------------------------------------------- #
# 3. A deliberate CM-pipeline step may open the gate explicitly.
# --------------------------------------------------------------------------- #
async def test_cm_pipeline_authorized_flag_is_allowed(monkeypatch):
    era, saves = _make_era(monkeypatch, last_user_text="anything at all", authorized=True)
    result = await era._execute_tool_raw(
        "save_memory",
        {
            "category": "projects",
            "key": "delta_cuisine_plan",
            "value": "week 27 plan",
        },
    )
    assert result == "ok"
    assert saves == [{"projects": {"delta_cuisine_plan": {"value": "week 27 plan"}}}]


# --------------------------------------------------------------------------- #
# record_feedback echo guard: Era's own spoken status lines (looped back through
# Gemini) must NOT be recorded as user feedback; genuine feedback must pass.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        'Got it — running "prepare this week\'s content plan" for delta cuisine.',
        "Which brand is this for?",
        '[System note: Ask the user aloud, exactly: "Which brand is this for?"]',
        "Ceram Quartz's weekly is running right now, sir; yours will start right after.",
        # era_dispatch inactive-brand error, as Gemini re-recorded it (prefix stripped):
        'Brand "ceram_quartz" is not active yet — finish its brands.yaml setup.',
        "That community-management task failed: unknown error",
    ],
)
def test_is_era_own_echo_true_for_eras_own_lines(text):
    assert main_module._is_era_own_echo(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "always minimize Spotify when you finish with them",
        "The correct spelling on WhatsApp is 'Bachboucha'.",
        "prepare this week's content plan",
        "",
    ],
)
def test_is_era_own_echo_false_for_genuine_feedback(text):
    assert main_module._is_era_own_echo(text) is False
