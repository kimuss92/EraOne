"""PF-027: Era says what was asked and whether it really happened, never raw telemetry.

Live 2026-11-15 23:06: "Open Spotify" was answered "spotify: process running;
window minimized, sir — though I couldn't confirm it took effect."
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.era_live.speak_once import spoken_outcome
from core import live_app_state as live
from core.completion_contract import annotate as attach
from core.tool_result import ToolResult


def state(app="spotify", running=True, windows=("visible",), playback="unknown"):
    return {
        "app": app,
        "observed_at": 0.0,
        "source": "live_os",
        "process_running": running,
        "pids": [1] if running else [],
        "windows": None if windows is None else [{"title": app, "state": s} for s in windows],
        "audio_session": "active" if playback == "playing" else "inactive",
        "playback": playback,
        "errors": [],
    }


def refresh(monkeypatch, tool, params, observed, raw="Done."):
    monkeypatch.setattr(live, "collect_live_state", lambda target: observed)
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    return asyncio.run(live.refresh_after_command(tool, params, raw))


def test_open_with_a_visible_window_is_verified_and_said_plainly(monkeypatch):
    r = refresh(monkeypatch, "open_app", {"app_name": "Spotify"}, state())
    assert r.verified
    assert r.message.startswith("Spotify is open on screen.")
    assert spoken_outcome("open_app", r) == "Spotify is open on screen, sir."


def test_open_but_only_minimized_is_not_done_and_says_why(monkeypatch):
    r = refresh(monkeypatch, "open_app", {"app_name": "Spotify"}, state(windows=("minimized",)))
    assert not r.verified
    said = spoken_outcome("open_app", attach(r, "open_app"))
    assert "minimized" in said and "not open on screen" in said
    assert "process running" not in said


def test_play_that_stayed_paused_says_so_without_telemetry(monkeypatch):
    r = refresh(monkeypatch, "spotify_control", {"action": "play"}, state(playback="paused"))
    assert not r.verified and r.verification_status == "NOT_VERIFIED"
    said = spoken_outcome("spotify_control", attach(r, "spotify_control"))
    assert said == "I sent play to Spotify, but it is paused, so it did not take effect, sir."
    assert "couldn't confirm it took effect" not in said  # the verdict already says it
    assert "Observed:" in r.message  # the model still gets the raw observation


def test_unreadable_playback_is_not_confirmed(monkeypatch):
    r = refresh(monkeypatch, "spotify_control", {"action": "play"}, state(playback="unknown"))
    assert "not confirmed" in spoken_outcome("spotify_control", attach(r, "spotify_control"))


@pytest.mark.parametrize(
    "tool, params, observed, verified, sentence",
    [
        (
            "spotify_control",
            {"action": "pause"},
            state(playback="paused"),
            True,
            "Spotify is paused.",
        ),
        (
            "spotify_control",
            {"action": "minimize"},
            state(windows=("minimized",)),
            True,
            "Spotify is minimized.",
        ),
        (
            "open_app",
            {"app_name": "Spotify", "mode": "close"},
            state(running=False, windows=()),
            None,
            None,
        ),
        ("open_app", {"app_name": "Spotify"}, state(running=False, windows=()), False, None),
    ],
)
def test_every_app_command_gets_a_verdict(monkeypatch, tool, params, observed, verified, sentence):
    r = refresh(monkeypatch, tool, params, observed)
    if verified is not None:
        assert r.verified is verified
    if sentence:
        assert r.data["outcome_sentence"] == sentence
    said = spoken_outcome(tool, attach(r, tool))
    assert not said.lower().startswith("spotify: process")


def test_a_failed_command_keeps_its_own_failure_first(monkeypatch):
    raw = ToolResult.fail("Could not open Spotify.", error_type="ActionError")
    r = refresh(
        monkeypatch, "open_app", {"app_name": "Spotify"}, state(running=False, windows=()), raw
    )
    assert r.message.startswith("Could not open Spotify.")
    assert "outcome_sentence" not in r.data


def test_minimize_rule_never_hides_an_app_the_owner_just_opened(monkeypatch):
    from core.user_prefs import minimize_rule as mr

    monkeypatch.setattr(mr, "_opened_at", {})
    assert not mr.opened_on_request("spotify")
    mr.note_opened_on_request("Spotify")
    assert mr.opened_on_request("spotify")
    assert not mr.opened_on_request("spotify", within_s=-1)


def test_spotify_control_checks_the_open_request_before_minimizing():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "actions" / "spotify_control.py").read_text(
        encoding="utf-8"
    )
    assert 'is_post_action_minimize("spotify") and not opened_on_request("spotify")' in src
    opener = (Path(__file__).resolve().parents[1] / "actions" / "open_app.py").read_text(
        encoding="utf-8"
    )
    assert "note_opened_on_request(app_name)" in opener


# ── root cause: the playback reader and the verification window ─────────────


@pytest.mark.parametrize(
    "reply, playback",
    [
        ({"ok": True, "is_playing": True}, "playing"),
        ({"ok": True, "is_playing": False, "is_paused": True}, "paused"),
        ({"ok": True, "is_stopped": True}, "stopped"),
        ({"ok": False, "error": "no session"}, "unknown"),
        (None, "unknown"),
    ],
)
def test_playback_is_read_through_the_working_gsmtc_host(monkeypatch, reply, playback):
    """The `winrt` binding has no Python 3.14 wheel: the old reader ALWAYS said unknown."""
    from actions import spotify_gsmtc_host

    calls = []
    monkeypatch.setattr(
        spotify_gsmtc_host, "request", lambda op, timeout: calls.append(op) or reply
    )
    assert live._host_spotify_playback() == playback
    assert calls == [{"op": "playback_state"}]  # a read, never a command


def test_without_the_winrt_binding_playback_comes_from_the_host(monkeypatch):
    """Era's Python 3.14 has no `winrt`; the native reader must use the host, not die."""
    import builtins

    real_import = builtins.__import__

    def no_winrt(name, *a, **k):
        if name.startswith("winrt"):
            raise ModuleNotFoundError("No module named 'winrt'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_winrt)
    monkeypatch.setattr(live, "_host_spotify_playback", lambda timeout=1.0: "paused")
    assert asyncio.run(live._native_spotify_playback()) == "paused"


def test_an_unreadable_playback_is_named_not_silent(monkeypatch):
    monkeypatch.setattr(live, "_identity", lambda app: ("spotify", {"spotify.exe"}))
    monkeypatch.setattr(live, "_processes", lambda names: [1])
    monkeypatch.setattr(live, "_windows", lambda names: [])
    monkeypatch.setattr(live, "_spotify_media", lambda: ("unknown", "unknown"))
    assert "playback_unreadable" in live.collect_live_state("spotify")["errors"]


def test_a_late_reporting_play_is_caught_within_the_wider_window(monkeypatch):
    """Spotify reports a new state 2-3 s late: the 3rd read (after 2 x 1 s) sees it."""
    reads = iter(["paused", "paused", "playing"])
    monkeypatch.setattr(live, "collect_live_state", lambda target: state(playback=next(reads)))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(live.asyncio, "sleep", fake_sleep)
    r = asyncio.run(live.refresh_after_command("spotify_control", {"action": "play"}, "ok"))
    assert r.verified and slept == [1.0, 1.0]


# ── PF-028: music starts only when the owner asked for it ────────────────────


def _era_said(*texts):
    import time as _t
    from types import SimpleNamespace

    now = _t.time()
    return SimpleNamespace(
        _recent_user_texts=[(now - 2, t) for t in texts], _inflight_user_text="", _last_user_text=""
    )


@pytest.mark.parametrize("said", ["Open Spotify", "ouvre spotify", "", "what time is it"])
def test_play_nobody_asked_for_is_refused(said):
    from app.era_live.tool_dispatch import _ungrounded_media_start

    era = _era_said(said) if said else _era_said()
    refusal = _ungrounded_media_start(era, "spotify_control", {"action": "play"})
    assert refusal and refusal.startswith("Not done")
    assert _ungrounded_media_start(era, "media_coordinator", {"action": "play"})


@pytest.mark.parametrize(
    "said",
    [
        "Play me some chill music",
        "put on something relaxing",
        "resume",
        "mets de la musique",
        "شغل اغنية",
        "continue the song",
    ],
)
def test_play_the_owner_asked_for_goes_through(said):
    from app.era_live.tool_dispatch import _ungrounded_media_start

    assert _ungrounded_media_start(_era_said(said), "spotify_control", {"action": "play"}) is None


@pytest.mark.parametrize(
    "tool, args",
    [
        ("spotify_control", {"action": "pause"}),
        ("spotify_control", {"action": "minimize"}),
        ("open_app", {"app_name": "Spotify"}),
        ("media_coordinator", {"action": "play", "target": "youtube"}),
    ],
)
def test_other_actions_are_untouched(tool, args):
    from app.era_live.tool_dispatch import _ungrounded_media_start

    assert _ungrounded_media_start(_era_said("Open Spotify"), tool, args) is None


def test_the_music_gate_runs_beside_the_state_gate_and_spares_scheduled_runs():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "era_live" / "tool_dispatch.py").read_text(
        encoding="utf-8"
    )
    block = src.split(
        "if not scheduled_run:\n            _ungrounded = _ungrounded_state_change(", 1
    )
    if len(block) == 1:
        block = src.split(
            "if not scheduled_run:\r\n            _ungrounded = _ungrounded_state_change(", 1
        )
    assert len(block) == 2
    assert "_ungrounded_media_start(self, tool_name, args)" in block[1][:600]


def test_a_window_move_keeps_its_own_result_and_never_claims_not_running(monkeypatch):
    """Live 2026-11-16 02:02: moving the ERA ONE window was answered "Era one is not
    running" - the title matched no process, and the move's own result was lost."""
    observed = state(app="era one", running=False, windows=())
    r = refresh(
        monkeypatch,
        "window_control",
        {"action": "move_to_monitor", "title": "ERA ONE", "screen": "primary"},
        observed,
        raw="Moved 'ERA ONE' to screen 1.",
    )
    assert r.message.startswith("Moved 'ERA ONE' to screen 1.")
    assert "outcome_sentence" not in r.data
    said = spoken_outcome("window_control", r)
    assert said.startswith("Moved 'ERA ONE' to screen 1")
    assert "is not running" not in r.message


# ── PF-047: plain action words; a timed-out re-read keeps what was seen ──────


@pytest.mark.parametrize(
    "tool, action, app",
    [
        ("youtube_video", "youtube_pause", "youtube"),
        ("netflix_control", "netflix_pause", "netflix"),
        ("spotify_control", "spotify_pause", "spotify"),
    ],
)
def test_a_failed_media_command_is_spoken_without_its_internal_name(monkeypatch, tool, action, app):
    r = refresh(monkeypatch, tool, {"action": action}, state(app=app, playback="playing"))
    assert not r.verified
    said = spoken_outcome(tool, attach(r, tool))
    assert said.startswith(f"I sent pause to {live._display(app)}, but it is playing")
    assert action not in said


def test_a_timed_out_reread_keeps_the_earlier_observation(monkeypatch):
    """Read 1 saw "paused"; read 2 times out. Era says paused, not "couldn't read"."""
    reads = iter([state(playback="paused")])

    def collect(target):
        try:
            return next(reads)
        except StopIteration:
            raise TimeoutError("probe too slow") from None

    monkeypatch.setattr(live, "collect_live_state", collect)
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    r = asyncio.run(live.refresh_after_command("spotify_control", {"action": "play"}, "ok"))
    assert not r.verified and r.verification_status == "NOT_VERIFIED"
    assert r.data["live_app_state"]["playback"] == "paused"
    assert "probe_unavailable" in r.data["live_app_state"]["errors"]
    assert r.data["outcome_sentence"] == (
        "I sent play to Spotify, but it is paused, so it did not take effect."
    )


def test_a_first_read_that_times_out_is_still_unknown(monkeypatch):
    def collect(target):
        raise TimeoutError("probe too slow")

    monkeypatch.setattr(live, "collect_live_state", collect)
    r = asyncio.run(live.refresh_after_command("spotify_control", {"action": "play"}, "ok"))
    assert not r.verified
    assert r.data["live_app_state"]["errors"] == ["probe_unavailable"]
    assert "not confirmed" in r.data["outcome_sentence"]
