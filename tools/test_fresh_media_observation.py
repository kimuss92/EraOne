"""No API keys: stale, late and concurrent native media observations."""
import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest
from core import media_observation as obs
from core.live_app_state import refresh_after_command
from core.tool_result import ToolResult
from core.verification import verify_tool_result

@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(obs, "_latest", {})
    monkeypatch.setattr("actions.spotify_gsmtc_host.request", Mock(side_effect=AssertionError("unit tests never read desktop")))

def row(title="Requested Song", playing=True, started=None):
    return {"source":"windows_gsmtc", "sample_started_at":time.time() if started is None else started,
            "observed_at":time.time(), "title":title, "artist":"Artist", "available":True,
            "playback":"playing" if playing else "paused"}

def test_native_observation_reads_metadata_and_status_together(monkeypatch):
    request=Mock(return_value={"ok":True,"title":"New song","artist":"Artist","is_playing":True,"position_ms":25})
    monkeypatch.setattr("actions.spotify_gsmtc_host.request",request)
    result=obs.spotify()
    assert result["title"]=="New song" and result["playback"]=="playing"
    assert result["sample_started_at"]<=result["observed_at"]
    request.assert_called_once_with({"op":"current_track"},timeout=2.0)

def test_late_old_read_cannot_replace_newer_observation():
    obs.publish("spotify",row("new",started=20))
    obs.publish("spotify",row("old",started=10))
    assert obs.snapshot("spotify")["title"]=="new"

def test_expired_and_clock_reversed_data_is_not_current(monkeypatch):
    stale=row();stale["observed_at"]=100
    obs.publish("spotify",stale)
    monkeypatch.setattr(obs.time,"time",lambda:106)
    assert obs.snapshot("spotify") is None
    monkeypatch.setattr(obs.time,"time",lambda:99)
    assert obs.snapshot("spotify") is None

def test_old_track_then_new_track_resolves_without_action_replay(monkeypatch):
    rows=iter(["Previous Song","Requested Song"])
    reader=Mock(side_effect=lambda:row(next(rows)))
    monkeypatch.setattr(obs,"spotify",reader)
    monkeypatch.setattr(asyncio,"sleep",AsyncMock())
    result=asyncio.run(obs.settle_spotify("Requested Song"))
    assert result["matched"] and reader.call_count==2

def test_cached_precommand_match_is_rejected(monkeypatch):
    monkeypatch.setattr(obs,"spotify",lambda:row(started=0))
    result=asyncio.run(obs.settle_spotify("Requested Song",timeout=.01))
    assert not result["matched"]

def test_late_playback_corrects_earlier_unverified_result(monkeypatch):
    monkeypatch.setattr(obs,"settle_spotify",AsyncMock(return_value={**row(),"matched":True}))
    raw=ToolResult.fail("Could not start; previous track was old",error_type="ActionError")
    result=asyncio.run(refresh_after_command("spotify_control",{"action":"search","value":"Requested Song"},raw))
    result=verify_tool_result(result,tool="spotify_control",parameters={"action":"search"})
    assert result.success and result.verified and result.verifier_name=="spotify_native"
    assert "old" not in result.message and "Requested Song" in result.message

def test_missing_fresh_evidence_does_not_claim_playback_failed(monkeypatch):
    monkeypatch.setattr(obs,"settle_spotify",AsyncMock(return_value={**row("Previous Song"),"matched":False}))
    result=asyncio.run(refresh_after_command("spotify_control",{"action":"search","value":"Requested Song"},"Clicked once"))
    result=verify_tool_result(result,tool="spotify_control")
    assert not result.verified and result.verification_status=="NOT_VERIFIED"
    assert "not yet verified" in result.message and "could not start" not in result.message
    assert result.data["media_observation"]["title"]=="Previous Song"

def test_explicit_status_reads_fresh_native_state(monkeypatch):
    obs.publish("spotify",row("Old"))
    read=Mock(return_value=row("New"))
    monkeypatch.setattr(obs,"spotify",read)
    result=asyncio.run(refresh_after_command("spotify_control",{"action":"status"},"old cached text"))
    assert "New" in result.message and "Old" not in result.message
    read.assert_called_once()

def test_gate_is_not_overridden_even_if_requested_song_is_playing(monkeypatch):
    read=AsyncMock(side_effect=AssertionError("gate must not sample"))
    monkeypatch.setattr(obs,"settle_spotify",read)
    gate=ToolResult.fail("Needs confirmation",error_type="ConfirmationRequired")
    assert asyncio.run(refresh_after_command("spotify_control",{"action":"search","value":"Requested Song"},gate)) is gate
    read.assert_not_called()
