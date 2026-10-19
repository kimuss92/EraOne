"""Read-only watcher contracts with no real media/device mutations."""

import asyncio
import json

import pytest

from core.state_store import MediaSessionInfo, StateStore
from core.system_awareness import FRESH_S, MediaAwareness, parse_snapshot


def packet(status="Playing", app="Spotify.exe", ident="1"):
    return {"version": 1, "sessions": [{"id": ident, "app": app, "status": status}]}


async def test_complete_snapshot_removes_closed_sessions_atomically():
    store = StateStore()
    await store.replace_media_sessions([MediaSessionInfo(session_id="old")])
    before = await store.snapshot()
    await store.replace_media_sessions([MediaSessionInfo(session_id="new")])
    after = await store.wait_for_change(before.revision, timeout=0.1)
    assert list(after.media_sessions) == ["new"] and after.media_health == "ready"
    await store.replace_media_sessions([])
    assert (await store.snapshot()).media_sessions == {}


async def test_manual_pause_is_observed_and_snapshot_is_detached():
    views = []
    service = MediaAwareness(publisher=lambda v, **kw: views.append((v, kw)))
    await service.observe(packet(), "generation")
    await service.observe(packet("Paused"), "generation")
    view = service.snapshot()
    assert view["revision"] == 2 and view["sessions"][0]["status"] == "paused"
    assert views[-1][1]["changed"]
    views[-1][0]["sessions"][0]["status"] = "playing"
    assert service.snapshot()["sessions"][0]["status"] == "paused"


async def test_restart_identity_does_not_merge_old_sessions():
    service = MediaAwareness()
    await service.observe(packet(), "old")
    await service.observe(packet("Paused"), "new")
    assert service.snapshot()["sessions"] == [
        {"id": "new:1", "app": "Spotify.exe", "status": "paused"}
    ]


async def test_staleness_and_failure_are_unknown_not_paused():
    clock = [100.0]
    service = MediaAwareness(clock=lambda: clock[0])
    await service.observe(packet(), "g")
    clock[0] += FRESH_S + 1
    assert service.snapshot()["health"] == "stale" and service.snapshot()["sessions"] == []
    await service.unavailable()
    assert service.snapshot()["health"] == "unavailable"
    assert (await service.state.snapshot()).media_sessions == {}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"version": 2, "sessions": []},
        {"version": 1, "sessions": "not-a-list"},
        {"version": 1, "sessions": [None]},
        packet("invented"),
        packet(ident=""),
        {"version": 1, "sessions": packet()["sessions"] * 2},
    ],
)
def test_malformed_payload_cannot_be_accepted(bad):
    with pytest.raises(ValueError):
        parse_snapshot(bad, "g", 100)


async def test_bad_payload_does_not_partially_replace_state():
    service = MediaAwareness()
    await service.observe(packet(), "g")
    before = service.snapshot()
    bad = packet()
    bad["sessions"].append({"id": "2", "app": "a", "status": "invalid"})
    with pytest.raises(ValueError):
        await service.observe(bad, "g")
    assert service.snapshot() == before


async def test_duplicate_app_labels_have_separate_sessions_and_aggregate(monkeypatch):
    from core import system_awareness as sa

    service = MediaAwareness()
    rows = packet()["sessions"] + packet("Paused", ident="2")["sessions"]
    await service.observe({"version": 1, "sessions": rows}, "g")
    monkeypatch.setattr(sa, "get_media_awareness", lambda: service)
    assert sa.observed_source_status("spotify") == "playing"
    await service.observe(packet("Paused"), "g")
    assert sa.observed_source_status("spotify") == "paused"
    await service.observe(packet(app="chrome.exe"), "g")
    assert sa.observed_source_status("youtube") is None
    assert sa.observed_source_status("netflix") is None


async def test_manual_observation_supersedes_intention_without_overwriting_it(monkeypatch):
    from core import media_state
    from core import system_awareness as sa

    service = MediaAwareness()
    monkeypatch.setattr(sa, "get_media_awareness", lambda: service)
    monkeypatch.setattr(media_state, "MEDIA_STATE", {"spotify": {"status": "playing"}})
    await service.observe(packet("Paused"), "g")
    assert not media_state.is_playing("spotify")
    assert media_state.get_state()["spotify"]["status"] == "playing"
    await service.observe(packet(), "g")
    assert media_state.is_playing("spotify")
    await service.observe({"version": 1, "sessions": []}, "g")
    assert not media_state.is_playing("spotify")
    await service.unavailable()
    assert not media_state.is_playing("spotify")


async def test_misleading_source_label_is_not_spotify_evidence(monkeypatch):
    from core import system_awareness as sa

    service = MediaAwareness()
    await service.observe(packet(app="not-spotify.exe"), "g")
    monkeypatch.setattr(sa, "get_media_awareness", lambda: service)
    assert sa.observed_source_status("spotify") is None


async def test_heartbeat_updates_freshness_without_false_change_event():
    changes, clock = [], [100.0]
    service = MediaAwareness(
        clock=lambda: clock[0], publisher=lambda v, **kw: changes.append(kw["changed"])
    )
    await service.observe(packet(), "g")
    clock[0] = 115
    await service.observe(packet(), "g")
    assert changes == [True, False] and service.snapshot()["observedAt"] == 115


async def test_publisher_failure_does_not_destroy_observation():
    def fail(*a, **k):
        raise RuntimeError("broken UI")

    service = MediaAwareness(publisher=fail)
    await service.observe(packet(), "g")
    assert service.snapshot()["health"] == "ready"


async def test_hud_gets_live_events_and_detached_snapshot():
    from app.hud.events import HudBus

    bus, events = HudBus(), []
    bus.attach(events.append)
    service = MediaAwareness(publisher=bus.media_observed)
    await service.observe(packet("Paused"), "g")
    snap = bus.snapshot()
    assert snap["mediaObservation"]["sessions"][0]["status"] == "paused"
    snap["mediaObservation"]["sessions"].clear()
    assert bus.snapshot()["mediaObservation"]["sessions"]
    assert any(e.get("event", {}).get("type") == "system.media.observed" for e in events)


async def test_transport_failure_clears_state_and_stops_cleanly():
    service = MediaAwareness(launcher=lambda: (_ for _ in ()).throw(OSError("offline")))
    await service.observe(packet(), "g")
    service._stop.set()
    await service._watch()
    assert service.snapshot()["health"] == "stopped" and not service.snapshot()["sessions"]


async def test_dead_transport_is_reaped_and_reported():
    class Stream:
        def readline(self):
            return ""

        def close(self):
            pass

    class Process:
        stdout = Stream()

        def poll(self):
            return 1

        def wait(self, timeout):
            service._stop.set()
            return 1

    health = []
    service = MediaAwareness(launcher=Process, publisher=lambda v, **kw: health.append(v["health"]))
    await service._watch()
    assert health == ["unavailable", "stopped"]


async def test_valid_transport_snapshot_is_processed_before_eof():
    class Stream:
        def __init__(self):
            self.lines = iter([json.dumps(packet()) + "\n", ""])

        def readline(self):
            return next(self.lines)

        def close(self):
            pass

    class Process:
        stdout = Stream()

        def poll(self):
            return 0

        def wait(self, timeout):
            service._stop.set()
            return 0

    health = []
    service = MediaAwareness(launcher=Process, publisher=lambda v, **kw: health.append(v["health"]))
    await service._watch()
    assert health == ["ready", "unavailable", "stopped"]


def test_no_media_mutation_api_in_helper():
    from core.system_awareness import _HELPER

    script = _HELPER.read_text(encoding="utf-8")
    assert "PlaybackInfoChanged" in script and "SessionsChanged" in script
    assert "TryPause" not in script and "TryPlay" not in script
    assert "SetDefault" not in script and "TryChange" not in script
