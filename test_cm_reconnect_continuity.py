"""
test_cm_reconnect_continuity.py — a community-management status report must
never silently vanish into a dead connection, and reconnecting mid-job must
never mislead the user into thinking something needs "continuing".

Covers the two instance-level pieces added alongside the pure
reconnect_acknowledgment_text policy (test_reconnect_acknowledgment.py):

  1. _deliver_or_queue_cm_report — speaks now if a session is live, otherwise
     holds the report (speak() itself silently no-ops with no session, so
     without this a finished 30-minute run would just disappear).
  2. _build_reconnect_acknowledgment — priority order on reconnect:
       a. a held CM report is delivered as "what you missed", unconditionally
       b. a still-running CM job is announced honestly, but ONLY on a genuine
          (non-rotation) reconnect — rotation fires every ~2.5 min and must
          stay silent, or a long job would repeat "still working" a dozen
          times before it finishes
       c. otherwise, the generic last-utterance policy

`era_dispatch` is faked in sys.modules before `main` is imported, same
isolation style as test_lane_wiring.py — current_job() is looked up lazily
inside the method under test, so faking it works regardless of import order.

Run:  pytest test_cm_reconnect_continuity.py -v
"""

import sys
import types

import pytest


@pytest.fixture()
def fake_era_dispatch(monkeypatch):
    state = types.SimpleNamespace(current_job_return=None)
    fake_module = types.ModuleType("era_dispatch")
    fake_module.current_job = lambda: state.current_job_return
    monkeypatch.setitem(sys.modules, "era_dispatch", fake_module)
    return state


@pytest.fixture(scope="module")
def main_module():
    import main

    return main


@pytest.fixture()
def era(main_module):
    """A bare EraLive-shaped stub carrying only the state the two methods
    under test touch — avoids running EraLive.__init__."""

    class _Stub:
        pass

    stub = _Stub()
    stub._pending_cm_announcement = None
    stub._recent_user_texts = []
    stub._loop = object()  # truthy stand-in for "an event loop exists"
    stub.session = object()  # truthy stand-in for "a live session exists"
    stub.spoken = []
    stub.logged = []
    stub.speak = lambda text: stub.spoken.append(text)
    stub.ui = types.SimpleNamespace(write_log=lambda line: stub.logged.append(line))

    stub._deliver_or_queue_cm_report = main_module.EraLive._deliver_or_queue_cm_report.__get__(stub)
    stub._build_reconnect_acknowledgment = (
        main_module.EraLive._build_reconnect_acknowledgment.__get__(stub)
    )
    return stub


# ── _deliver_or_queue_cm_report ──────────────────────────────────────────────


def test_speaks_immediately_when_session_is_live(era):
    era._deliver_or_queue_cm_report("Delta Cuisine's weekly plan is ready.")
    assert era.spoken == ["Delta Cuisine's weekly plan is ready."]
    assert era._pending_cm_announcement is None


def test_queues_instead_of_vanishing_with_no_session(era):
    era.session = None
    era._deliver_or_queue_cm_report("Ceram Quartz's catalog update finished.")
    assert era.spoken == []  # never silently dropped without a trace
    assert era._pending_cm_announcement == "Ceram Quartz's catalog update finished."
    assert era.logged  # held report is at least visible in the UI log


def test_queues_instead_of_vanishing_with_no_loop(era):
    era._loop = None
    era._deliver_or_queue_cm_report("Report while loop is down.")
    assert era.spoken == []


def test_normal_completion_is_spoken_plain_not_wrapped(era):
    # The everyday case (CM finishes while already connected) must keep
    # Gemini's natural reaction — only reconnect-triggered delivery gets the
    # verbatim lockdown.
    era._deliver_or_queue_cm_report("Delta Cuisine's weekly plan is ready.")
    assert era.spoken == ["Delta Cuisine's weekly plan is ready."]


def test_verbatim_delivery_is_wrapped_so_gemini_cannot_act_on_it(era):
    from main import _say_exactly

    era._deliver_or_queue_cm_report("While you were away, sir: the job finished.", verbatim=True)
    assert era.spoken == [_say_exactly("While you were away, sir: the job finished.")]


def test_verbatim_queue_still_stores_the_plain_text(era):
    # Even when verbatim=True, what gets HELD for a future reconnect must stay
    # plain — the _say_exactly wrapper is applied only at the moment of
    # speaking, not baked into the stored/logged copy.
    era.session = None
    era._deliver_or_queue_cm_report("Held while away.", verbatim=True)
    assert era._pending_cm_announcement == "Held while away."


# ── _build_reconnect_acknowledgment ──────────────────────────────────────────


def test_held_report_is_delivered_and_cleared(era, fake_era_dispatch):
    era._pending_cm_announcement = "Delta Cuisine's showroom launch posted 3 reels."
    fake_era_dispatch.current_job_return = None
    msg = era._build_reconnect_acknowledgment("transient")
    assert (
        msg == "While you were disconnected, sir: Delta Cuisine's showroom launch posted 3 reels."
    )
    assert era._pending_cm_announcement is None  # one-shot, not repeated next time


def test_held_report_wins_even_during_a_rotation_reconnect(era, fake_era_dispatch):
    # One-shot information must reach the user the moment ANY session comes
    # up — rotation reconnects happen every ~2.5 min and are otherwise silent,
    # but a finished report should not be stuck waiting for a "genuine" outage.
    era._pending_cm_announcement = "Ceram Quartz's ads campaign is live."
    msg = era._build_reconnect_acknowledgment("rotation")
    assert msg is not None
    assert "Ceram Quartz's ads campaign is live." in msg


def test_busy_job_announced_on_genuine_reconnect(era, fake_era_dispatch):
    fake_era_dispatch.current_job_return = {
        "brand": "delta_cuisine",
        "task_type": "weekly",
    }
    msg = era._build_reconnect_acknowledgment("quota")
    assert msg is not None
    assert "still working" in msg.lower()
    assert "delta cuisine" in msg.lower()
    assert "continue" not in msg.lower()  # must not imply it's waiting on conversation


def test_busy_job_stays_silent_on_rotation_reconnect(era, fake_era_dispatch):
    # A 30-minute job would otherwise repeat "still working" roughly every
    # 2.5 minutes across a routine session rotation — exactly the chatter the
    # silent-rotation policy exists to prevent.
    fake_era_dispatch.current_job_return = {
        "brand": "ceram_quartz",
        "task_type": "catalog update",
    }
    era._recent_user_texts = []  # nothing for the generic fallback to name either
    msg = era._build_reconnect_acknowledgment("rotation")
    assert msg is None


def test_falls_back_to_silence_when_nothing_cm_related(era, fake_era_dispatch):
    # reconnect_acknowledgment_text always returns None now (see
    # test_reconnect_acknowledgment.py) — an ordinary conversation gap with no
    # CM report pending and no CM job running stays fully silent.
    import time

    fake_era_dispatch.current_job_return = None
    era._recent_user_texts = [(time.time(), "check my calendar for tomorrow")]
    msg = era._build_reconnect_acknowledgment("auth")
    assert msg is None


def test_era_dispatch_import_failure_degrades_to_silence(era, monkeypatch):
    import time

    monkeypatch.setitem(sys.modules, "era_dispatch", types.ModuleType("era_dispatch"))
    # No current_job attribute at all — must degrade, never raise into the
    # reconnect path (a vault/module hiccup must not crash the session loop).
    era._recent_user_texts = [(time.time(), "open spotify")]
    msg = era._build_reconnect_acknowledgment("transient")
    assert msg is None
