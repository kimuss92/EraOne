"""Media routing works from the REAL per-device sessions, per app (core/media_routing.py).

2026-10-16: the glasses were the default output, yet Spotify and Edge played on the
PC speakers (Windows' per-app "Output device"). Era answered "the music should be
playing there" and re-ran set_default five times. No real audio is touched here:
sessions, endpoints and the Windows per-app call are all fakes.
"""

from __future__ import annotations

import pytest

from core import audio_endpoints as ae
from core import media_routing as mr

GLASS = ae.Endpoint(id="{0.0.0.00000000}.{glass}", name="Star glass", flow="output", kind="x")
PC = ae.Endpoint(id="{0.0.0.00000000}.{pc}", name="pc - speakers", flow="output", kind="x")


@pytest.fixture
def world(monkeypatch):
    """Spotify + Edge playing on the PC speakers; the glasses are the default."""
    state = {
        "sessions": [
            mr.MediaSession(10, "spotify", PC.id, PC.name, True),
            mr.MediaSession(20, "msedge", PC.id, PC.name, True),
            mr.MediaSession(30, "ledkeeper2", PC.id, PC.name, False),
        ],
        "calls": [],
    }

    def set_persisted(pids, endpoint_id):
        state["calls"].append((list(pids), endpoint_id))
        target = next((e for e in (GLASS, PC) if e.id == endpoint_id), GLASS)
        state["sessions"] = [
            mr.MediaSession(s.pid, s.stem, target.id, target.name, s.active) if s.pid in pids else s
            for s in state["sessions"]
        ]
        return {p: 0 for p in pids}

    monkeypatch.setattr(mr, "media_sessions", lambda: list(state["sessions"]))
    monkeypatch.setattr(mr, "_set_persisted", set_persisted)
    monkeypatch.setattr(ae, "_system_endpoint", lambda: GLASS)
    monkeypatch.setattr(
        ae,
        "resolve",
        lambda q, flow=None: (
            (GLASS, None)
            if "glass" in q.lower()
            else (PC, None)
            if "speaker" in q.lower() or "pc" in q.lower()
            else (None, ae.AudioResult(False, f"No device matches '{q}', sir."))
        ),
    )
    return state


def test_where_names_the_real_device_not_the_default(world):
    msg = mr.where_media_plays().message
    assert "Spotify and Microsoft Edge on pc - speakers" in msg
    assert "default output is Star glass" in msg
    assert "Ledkeeper" not in msg  # idle sessions are not "playing"


def test_send_the_music_moves_everything_playing(world):
    out = mr.route_media("glasses")
    assert out.ok and "are now on Star glass" in out.message
    assert world["calls"] == [([10, 20], GLASS.id)]


def test_one_named_app_moves_alone(world):
    out = mr.route_media("glasses", "spotify")
    assert out.ok and out.message.startswith("Spotify is now on Star glass")
    assert world["calls"] == [([10], GLASS.id)]


def test_youtube_means_the_browser(world):
    mr.route_media("glasses", "youtube")
    assert world["calls"] == [([20], GLASS.id)]


def test_default_clears_the_pin_instead_of_naming_a_device(world):
    mr.route_media("default", "spotify")
    assert world["calls"] == [([10], None)]  # None = empty HSTRING = follow default


def test_already_there_changes_nothing(world):
    mr.route_media("speakers", "spotify")
    assert world["calls"] == []


def test_nothing_playing_still_remembers_the_choice(world):
    world["sessions"] = []
    out = mr.route_media("glasses")
    assert out.ok and "from now on" in out.message and world["calls"] == []


def test_an_app_that_does_not_switch_live_is_reported_honestly(world, monkeypatch):
    monkeypatch.setattr(mr, "_set_persisted", lambda pids, eid: {p: 0 for p in pids})
    monkeypatch.setattr(mr, "_settled", lambda pids, eid, timeout_s=2.0: set())
    out = mr.route_media("glasses", "spotify")
    assert "hasn't switched yet" in out.message


def test_a_refusal_is_a_failure(world, monkeypatch):
    monkeypatch.setattr(mr, "_set_persisted", lambda pids, eid: {p: -1 for p in pids})
    out = mr.route_media("glasses", "spotify")
    assert not out.ok and "refused" in out.message


def test_set_default_names_media_held_elsewhere(world):
    note = mr.held_elsewhere_note(GLASS)
    assert "Spotify and Microsoft Edge are still playing on pc - speakers" in note
    assert "send the music to Star glass" in note


def test_the_tool_routes_through_audio_control(world):
    from actions.audio_control import audio_control

    out = audio_control({"action": "send_media", "device": "glasses", "app": "spotify"})
    assert out.startswith("Spotify is now on Star glass")
    assert "Microsoft Edge on pc - speakers" in audio_control({"action": "where_media"})


def test_persisted_device_id_format():
    assert mr._persisted_id(GLASS.id) == (
        r"\\?\SWD#MMDEVAPI#{0.0.0.00000000}.{glass}#{e6327cad-dcec-4949-ae8a-991e976a79d2}"
    )


# ── The remembered media device: speakers by default, the owner's choice after ──
def test_unset_media_device_means_the_pc_speakers(world, monkeypatch):
    monkeypatch.setattr(mr, "_media_pref", lambda: None)
    assert mr.media_target()[0] is PC


def test_routing_remembers_the_choice(world, monkeypatch):
    stored = {}
    monkeypatch.setattr(mr, "_store_media_pref", lambda pref: stored.update(pref=pref))
    mr.route_media("glasses", "spotify")
    assert stored["pref"]["id"] == GLASS.id
    mr.route_media("default")
    assert stored["pref"].get("follow") is True


def test_new_playback_is_pulled_onto_the_remembered_device(world, monkeypatch):
    monkeypatch.setattr(mr, "media_target", lambda: (PC, "default for media"))
    world["sessions"] = [mr.MediaSession(10, "spotify", GLASS.id, GLASS.name, True)]
    assert mr.enforce_media_target("spotify", wait_s=0) == "moved to pc - speakers"
    assert world["calls"] == [([10], PC.id)]


def test_playback_already_on_the_device_is_left_alone(world, monkeypatch):
    monkeypatch.setattr(mr, "media_target", lambda: (PC, "default for media"))
    assert mr.enforce_media_target("spotify", wait_s=0) == ""
    assert world["calls"] == []


def test_follow_default_never_enforces(world, monkeypatch):
    monkeypatch.setattr(mr, "media_target", lambda: (None, "follows the default"))
    assert mr.enforce_media_target("spotify", wait_s=0) == ""


def test_background_enforcement_never_runs_under_pytest(monkeypatch):
    started = []
    monkeypatch.setattr(mr.threading, "Thread", lambda *a, **k: started.append(1))
    mr.enforce_media_target_async("spotify")
    assert started == []


def test_the_hud_snapshot_shows_target_and_real_devices(world, monkeypatch):
    monkeypatch.setattr(mr, "media_target", lambda: (PC, "default for media"))
    snap = mr.hud_snapshot()
    assert snap["target"] == "pc - speakers"
    assert {"app": "Spotify", "device": "pc - speakers"} in snap["playing"]


def test_the_hud_audio_route_carries_the_media_block(world, monkeypatch):
    from app.hud import audio_route

    monkeypatch.setattr(mr, "media_target", lambda: (PC, "default for media"))
    assert audio_route.snapshot()["media"]["target"] == "pc - speakers"
