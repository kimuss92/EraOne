"""
Tests for pending brand-ask tool-suppression (main.py EraLive).

While Era is waiting for the user to say which brand a CM request is for, Gemini
must not run off and guess/act (it was calling screen_process to read the brand
off-screen). An ALLOW-LIST in _execute_tool_raw blocks every tool except a minimal
escape-hatch set, gated on _pending_cm_ask_active() so suppression lifts the moment
the ask resolves OR its 45s window expires — never stuck.

Binds the real methods onto a bare stub (no EraLive.__init__).

Run:  pytest test_pending_ask_suppression.py -v
"""

import sys
import time
import types

import pytest

import main as main_module


def _make_era(pending=None):
    class _Stub:
        pass

    era = _Stub()
    era.session_manager = types.SimpleNamespace(get_state_snapshot=lambda: {})
    era._pending_cm_ask = pending
    era._standby_called = False
    era.enter_standby = lambda: setattr(era, "_standby_called", True)
    era._pending_cm_ask_active = main_module.EraLive._pending_cm_ask_active.__get__(era)
    era._execute_tool_raw = main_module.EraLive._execute_tool_raw.__get__(era)
    era._consume_pending_cm_ask = main_module.EraLive._consume_pending_cm_ask.__get__(era)
    return era


def _ask(ttl_s=45):
    return {
        "text": "prepare this week's content plan",
        "task_type": "weekly",
        "expires_at": time.time() + ttl_s,
    }


# --------------------------------------------------------------------------- #
# Predicate: the single self-expiring source of truth (stuck-state prevention).
# --------------------------------------------------------------------------- #
def test_active_true_within_window():
    era = _make_era(_ask())
    assert era._pending_cm_ask_active() is True


def test_inactive_when_no_pending():
    assert _make_era(None)._pending_cm_ask_active() is False


def test_expired_ask_self_clears_and_goes_inactive():
    era = _make_era(_ask(ttl_s=-1))  # already past its window
    assert era._pending_cm_ask_active() is False
    assert era._pending_cm_ask is None  # lazily cleared — no timer needed


# --------------------------------------------------------------------------- #
# Guard: block everything except the allow-list while the ask is active.
# --------------------------------------------------------------------------- #
async def test_blocked_tool_is_suppressed_while_ask_active():
    era = _make_era(_ask())
    result = await era._execute_tool_raw("screen_process", {"text": "which brand?"})
    assert "waiting for their answer" in result.lower()
    assert era._pending_cm_ask_active() is True  # ask still open, not consumed


async def test_allowlisted_tool_still_runs_while_ask_active():
    era = _make_era(_ask())
    result = await era._execute_tool_raw("enter_standby", {})
    assert "waiting for their answer" not in result.lower()
    assert era._standby_called is True  # allow-list escape hatch works


# (The "expired ask no longer blocks tools" case is covered by
# test_expired_ask_self_clears_and_goes_inactive: the guard condition is
# `tool not in ALLOWED and _pending_cm_ask_active()`, so once the predicate goes
# False the guard short-circuits and the tool runs — no separate exec test needed.)


# --------------------------------------------------------------------------- #
# Resolution paths flip the predicate to inactive (un-suppress).
# --------------------------------------------------------------------------- #
def _fake_ed(monkeypatch, brand_for):
    mod = types.ModuleType("era_dispatch")
    mod.resolve_brand = lambda text, cfg=None: brand_for
    monkeypatch.setitem(sys.modules, "era_dispatch", mod)


def test_valid_reply_consumes_and_unsuppresses(monkeypatch):
    _fake_ed(monkeypatch, brand_for="delta_cuisine")
    era = _make_era(_ask())
    payload = era._consume_pending_cm_ask("Delta Cuisine")
    assert payload is not None and payload["brand_key"] == "delta_cuisine"
    assert era._pending_cm_ask_active() is False  # tools restored


def test_nonmatch_reply_consumes_and_unsuppresses(monkeypatch):
    _fake_ed(monkeypatch, brand_for=None)
    era = _make_era(_ask())
    payload = era._consume_pending_cm_ask("the weather please")
    assert payload is None  # not a brand -> falls through
    assert era._pending_cm_ask_active() is False  # one-shot consumed -> tools restored
