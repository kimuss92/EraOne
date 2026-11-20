"""Post-command telemetry regressions; all OS probes use isolated fakes."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import live_app_state as live
from core import process_awareness as processes
from core.action_verifiers import verify_action_result
from core.tool_result import ToolResult


@pytest.fixture
def probes(monkeypatch):
    monkeypatch.setattr(live, "_identity", lambda app: (app.lower(), {app.lower() + ".exe"}))
    monkeypatch.setattr(live, "_processes", Mock(return_value=[12]))
    monkeypatch.setattr(live, "_windows", Mock(return_value=[]))
    monkeypatch.setattr(live, "_spotify_media", Mock(return_value=("active", "playing")))


def test_post_launch_bypasses_a_fresh_but_precommand_empty_process_cache(monkeypatch, probes):
    monkeypatch.setattr(processes, "_PSUTIL", True)
    monkeypatch.setattr(processes, "_CACHE", (time.time(), []))
    monkeypatch.setattr(
        processes,
        "psutil",
        SimpleNamespace(
            process_iter=lambda _: [
                SimpleNamespace(info={"pid": 44, "name": "Photoshop.exe", "exe": ""})
            ]
        ),
    )
    # Exercise the real cache-bypassing reader, not its fixture replacement.
    assert processes.list_processes() == []
    names = {"photoshop.exe"}
    assert _REAL_PROCESSES(names) == [44]
    assert processes.list_processes()[0].pid == 44


_REAL_PROCESSES = live._processes


def test_spotify_can_play_without_a_window(probes):
    state = live.collect_live_state("spotify")
    assert state["process_running"] is True
    assert state["windows"] == []
    assert state["playback"] == "playing"
    assert "not running" not in live.format_live_state(state)


def test_probe_failure_is_unknown_not_absent(monkeypatch, probes):
    monkeypatch.setattr(live, "_processes", Mock(side_effect=OSError))
    monkeypatch.setattr(live, "_windows", Mock(side_effect=OSError))
    monkeypatch.setattr(live, "_spotify_media", Mock(side_effect=OSError))
    state = live.collect_live_state("spotify")
    assert state["process_running"] is None
    assert state["windows"] is None
    assert state["playback"] == "unknown"
    assert len(state["errors"]) == 3


def test_late_window_is_polled_without_replaying_launch(monkeypatch, probes):
    monkeypatch.setattr(
        live, "_windows", Mock(side_effect=[[], [{"hwnd": 33, "pid": 12, "state": "visible"}]])
    )
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    result = asyncio.run(
        live.refresh_after_command("open_app", {"app_name": "Photoshop"}, "Opened Photoshop.")
    )
    assert result.verified
    assert result.data["live_app_state"]["windows"][0]["hwnd"] == 33
    assert result.data["command_result"] == "Opened Photoshop."
    assert live._windows.call_count == 2


def test_live_paused_overrides_claimed_play_success_and_survives_verifier(monkeypatch, probes):
    monkeypatch.setattr(live, "_spotify_media", Mock(return_value=("inactive", "paused")))
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    result = asyncio.run(
        live.refresh_after_command("spotify_control", {"action": "play"}, "Spotify is playing.")
    )
    result = verify_action_result("spotify_control", result, {"action": "play"})
    assert "Playback paused" in result.message
    assert "playing" not in result.message
    assert not result.verified
    assert result.verification_status == "NOT_VERIFIED"
    assert live._spotify_media.call_count == 3


def test_failed_command_keeps_failure_separate_from_live_state(probes):
    raw = ToolResult.fail("Could not select requested song.", error_type="ActionError")
    result = asyncio.run(live.refresh_after_command("spotify_control", {"action": "search"}, raw))
    assert not result.success
    assert result.error_type == "ActionError"
    assert "Playback playing" in result.message
    assert result.data["command_result"] == "Could not select requested song."
    assert not result.verified  # existing playback does not prove the requested song


def test_running_process_does_not_verify_an_open_window(monkeypatch, probes):
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    result = asyncio.run(
        live.refresh_after_command("open_app", {"app_name": "Photoshop"}, "Opened Photoshop.")
    )
    assert "process running" in result.message
    assert "window not observed" in result.message
    assert not result.verified
    assert verify_action_result("open_app", result, {"app_name": "Photoshop"}) is result


def test_unrelated_tools_and_status_reads_do_not_trigger_postcommand_probe(monkeypatch):
    monkeypatch.setattr(live, "collect_live_state", Mock(side_effect=AssertionError))
    for tool, params in [
        ("web_search", {}),
        ("open_app", {"app_name": "https://example.com"}),
    ]:
        assert asyncio.run(live.refresh_after_command(tool, params, "receipt")) == "receipt"


def test_cancelled_verification_propagates_cancellation(monkeypatch):
    monkeypatch.setattr(live, "collect_live_state", Mock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(live.refresh_after_command("spotify_control", {"action": "play"}, "Done."))


def test_spotify_status_uses_live_evidence_even_without_window(monkeypatch, probes):
    from actions import spotify_control as control

    monkeypatch.setattr(control, "_get_spotify_hwnd_and_title", lambda: (0, ""))
    assert "Playback playing" in control._spotify_status()


def test_song_title_never_proves_playback(monkeypatch):
    from actions import spotify_control as control

    monkeypatch.setattr(control, "_get_spotify_audio_session_state", lambda: None)
    monkeypatch.setattr(control, "_get_spotify_hwnd_and_title", lambda: (12, "Artist - Song"))
    assert control._spotify_is_playing() is False


@pytest.mark.parametrize(
    "audio,native,expected",
    [
        (False, "unknown", "unknown"),
        (False, "paused", "paused"),
        (True, "unknown", "playing"),
        (True, "paused", "conflicting"),
    ],
)
def test_audio_inactivity_is_not_a_pause_and_conflicts_remain_explicit(
    monkeypatch, audio, native, expected
):
    from core import audio_endpoints, media_routing

    monkeypatch.setattr(audio_endpoints, "_ensure_com", lambda: None)
    monkeypatch.setattr(
        media_routing, "media_sessions", lambda: [SimpleNamespace(stem="spotify", active=audio)]
    )
    monkeypatch.setattr(live, "_native_spotify_playback", AsyncMock(return_value=native))
    assert live._spotify_media() == ("active" if audio else "inactive", expected)


def test_real_dispatch_observes_only_after_handler_finishes(monkeypatch, probes):
    from app.era_live import tool_dispatch as dispatch

    events = []

    def launch(**kwargs):
        events.append("executed")
        return "Opened Photoshop."

    def windows(_):
        assert events == ["executed"]
        events.append("observed")
        return [{"hwnd": 90, "pid": 12, "state": "visible"}]

    monkeypatch.setattr(dispatch, "open_app", launch)
    monkeypatch.setattr(live, "_windows", windows)
    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        ui=None,
    )
    result = asyncio.run(
        dispatch.ToolDispatchMixin._execute_tool_raw(era, "open_app", {"app_name": "Photoshop"})
    )
    assert events == ["executed", "observed"]
    assert result.verified


def test_browser_read_is_domain_specific_and_does_not_trust_navigation():
    good = SimpleNamespace(
        url="https://www.youtube.com/watch?v=1",
        is_closed=lambda: False,
        evaluate=AsyncMock(
            return_value={"url": "https://www.youtube.com/watch?v=1", "status": "playing"}
        ),
    )
    wrong = SimpleNamespace(
        url="https://www.netflix.com/watch/1",
        is_closed=lambda: False,
        evaluate=AsyncMock(side_effect=AssertionError),
    )
    session = SimpleNamespace(_context=SimpleNamespace(pages=[wrong, good]))
    assert asyncio.run(live._read_browser_pages(session, "youtube")) == ["playing"]
    wrong.evaluate.assert_not_called()
    good.evaluate.return_value = {"url": "https://example.com", "status": "playing"}
    assert asyncio.run(live._read_browser_pages(session, "youtube")) == ["unknown"]


def test_pause_all_keeps_independent_source_states(monkeypatch, probes):
    monkeypatch.setattr(live, "_spotify_media", Mock(return_value=("inactive", "paused")))
    monkeypatch.setattr(
        live, "_browser_playback", lambda app: "paused" if app == "youtube" else "unknown"
    )
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    result = asyncio.run(
        live.refresh_after_command(
            "media_coordinator", {"target": "all", "action": "pause"}, "All paused."
        )
    )
    assert result.data["live_app_states"]["spotify"]["playback"] == "paused"
    assert result.data["live_app_states"]["netflix"]["playback"] == "unknown"
    assert not result.verified
    assert verify_action_result("media_coordinator", result, {"action": "pause"}) is result


def test_probe_timeout_reports_unknown_without_replaying(monkeypatch):
    async def timeout(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(live.asyncio, "wait_for", timeout)
    result = asyncio.run(
        live.refresh_after_command("spotify_control", {"action": "play"}, "Playing.")
    )
    assert result.data["live_app_state"]["errors"] == ["probe_unavailable"]
    assert result.verification_status == "NOT_VERIFIED"
    assert "Playback unknown" in result.message

def test_close_action_polls_until_termination(monkeypatch, probes):
    """Close action should poll _processes until the process is gone."""
    from unittest.mock import Mock
    
    # First call returns process, second call returns empty (process terminated)
    monkeypatch.setattr(live, "_processes", Mock(side_effect=[[12], []]))
    monkeypatch.setattr(live, "_windows", Mock(return_value=[]))
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    
    result = asyncio.run(
        live.refresh_after_command("window_control", {"action": "close", "title": "Spotify"}, "Closed Spotify.")
    )
    
    # Should have polled twice
    assert live._processes.call_count == 2
    # Final state should show process not running
    assert result.data["live_app_state"]["process_running"] is False
    assert result.data["live_app_state"]["pids"] == []


def test_close_verified_when_process_gone(monkeypatch, probes):
    """Close action should be verified when process is confirmed terminated."""
    from unittest.mock import Mock
    
    # Simulate process still running on first check, then gone
    monkeypatch.setattr(live, "_processes", Mock(side_effect=[[12], []]))
    monkeypatch.setattr(live, "_windows", Mock(return_value=[]))
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    
    result = asyncio.run(
        live.refresh_after_command("window_control", {"action": "close", "title": "Photoshop"}, "Closed Photoshop.")
    )
    
    # Should be verified (process_running == False matches expected)
    assert result.verified
    assert result.verification_status == "VERIFIED"
    assert result.data["live_app_state"]["process_running"] is False


def test_close_not_verified_when_process_persists(monkeypatch, probes):
    """Close action should NOT be verified when process is still running."""
    from unittest.mock import Mock
    
    # Process never terminates (simulating a stubborn process)
    monkeypatch.setattr(live, "_processes", Mock(return_value=[12]))
    monkeypatch.setattr(live, "_windows", Mock(return_value=[]))
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    
    result = asyncio.run(
        live.refresh_after_command("window_control", {"action": "close", "title": "Photoshop"}, "Closed Photoshop.")
    )
    
    # Should NOT be verified (process_running is True, expected False)
    assert not result.verified
    assert result.verification_status == "NOT_VERIFIED"
    assert result.data["live_app_state"]["process_running"] is True

