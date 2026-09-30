"""core.state_store + core.loop_rate_limit: the in-memory system-state cache
that the Windows sync loops write and the rest of Era reads.

Everything here is process-local and async - no OS calls, no hardware. What is
worth pinning is the behaviour that only shows up under a running loop: that
every write stamps `last_sync_ts` (a stale stamp is how "Era can't see my
windows" presents), that the drift log cannot grow without bound, that the
debounce actually throttles a 20 Hz poll down to its interval, and that
`snapshot()` hands back the LIVE state object rather than a copy - an aliasing
detail a caller can corrupt state through, so it is documented as a test.
"""

import asyncio
import time

import pytest

from core.loop_rate_limit import Debounce
from core.state_store import (
    BrowserTabInfo,
    ForegroundInfo,
    MediaSessionInfo,
    StateStore,
    SystemState,
)


# ── dataclass defaults: an unpopulated store must be safe to read ────────────
def test_state_dataclasses_default_to_empty_not_none():
    s = SystemState()
    assert s.foreground.hwnd == 0
    assert s.foreground.window_title == ""
    assert s.open_apps == {} and s.browser_tabs == {} and s.media_sessions == {}
    assert s.drift_flags == []
    assert s.last_sync_ts == 0.0


def test_each_system_state_gets_its_own_containers():
    """default_factory, not a shared mutable default - otherwise two stores
    would silently share one dict."""
    a, b = SystemState(), SystemState()
    a.open_apps["spotify.exe"] = {"pid": 1}
    a.drift_flags.append({"x": 1})
    assert b.open_apps == {} and b.drift_flags == []


def test_media_session_info_defaults():
    m = MediaSessionInfo()
    assert m.is_playing is False and m.is_paused is False
    assert m.playback_rate == 1.0


# ── writes stamp the sync clock ──────────────────────────────────────────────
async def test_update_foreground_stores_and_stamps():
    store = StateStore()
    before = time.time()
    info = ForegroundInfo(hwnd=42, process_name="brave.exe", window_title="Netflix")
    await store.update_foreground(info)
    snap = await store.snapshot()
    assert snap.foreground.hwnd == 42
    assert snap.foreground.process_name == "brave.exe"
    assert snap.last_sync_ts >= before


async def test_update_open_apps_replaces_wholesale():
    store = StateStore()
    await store.update_open_apps({"spotify.exe": {"pid": 1}})
    await store.update_open_apps({"brave.exe": {"pid": 2}})
    snap = await store.snapshot()
    assert snap.open_apps == {"brave.exe": {"pid": 2}}, "a fresh scan replaces, not merges"


async def test_update_browser_tabs_replaces_wholesale():
    store = StateStore()
    await store.update_browser_tabs({"t1": BrowserTabInfo(url="https://a", browser="brave")})
    await store.update_browser_tabs({"t2": BrowserTabInfo(url="https://b", browser="brave")})
    snap = await store.snapshot()
    assert list(snap.browser_tabs) == ["t2"]


@pytest.mark.parametrize(
    "method,arg",
    [
        ("update_foreground", ForegroundInfo(hwnd=1)),
        ("update_open_apps", {"a.exe": {"pid": 1}}),
        ("update_browser_tabs", {"t": BrowserTabInfo(url="u")}),
        ("update_media_session", MediaSessionInfo(session_id="s")),
    ],
)
async def test_every_update_path_advances_last_sync_ts(method, arg):
    """Staleness is judged off this one field; a writer that forgets it makes
    live state look dead."""
    store = StateStore()
    await getattr(store, method)(arg)
    assert (await store.snapshot()).last_sync_ts > 0


# ── media sessions are keyed, not replaced ───────────────────────────────────
async def test_media_sessions_accumulate_by_id():
    store = StateStore()
    await store.update_media_session(MediaSessionInfo(session_id="spotify", is_playing=True))
    await store.update_media_session(MediaSessionInfo(session_id="brave", is_playing=False))
    snap = await store.snapshot()
    assert set(snap.media_sessions) == {"spotify", "brave"}
    assert snap.media_sessions["spotify"].is_playing is True


async def test_same_session_id_is_updated_in_place():
    store = StateStore()
    await store.update_media_session(MediaSessionInfo(session_id="spotify", is_playing=True))
    await store.update_media_session(MediaSessionInfo(session_id="spotify", is_paused=True))
    snap = await store.snapshot()
    assert len(snap.media_sessions) == 1
    assert snap.media_sessions["spotify"].is_paused is True
    assert snap.media_sessions["spotify"].is_playing is False


# ── drift flags are bounded ──────────────────────────────────────────────────
async def test_drift_flags_append_in_order():
    store = StateStore()
    for i in range(3):
        await store.add_drift_flag({"n": i})
    assert [f["n"] for f in (await store.snapshot()).drift_flags] == [0, 1, 2]


async def test_drift_flags_are_capped_at_200_keeping_the_newest():
    """A long-running session must not leak memory through the drift log, and
    the flags worth keeping are the recent ones."""
    store = StateStore()
    for i in range(250):
        await store.add_drift_flag({"n": i})
    flags = (await store.snapshot()).drift_flags
    assert len(flags) == 200
    assert flags[0]["n"] == 50 and flags[-1]["n"] == 249


async def test_clear_drift_flags_empties_without_replacing_the_list():
    store = StateStore()
    await store.add_drift_flag({"n": 1})
    snap = await store.snapshot()
    held = snap.drift_flags
    await store.clear_drift_flags()
    assert held == [], "cleared in place, so an existing reader sees the reset"


# ── locking ──────────────────────────────────────────────────────────────────
async def test_concurrent_writers_do_not_lose_flags():
    """All four loops write through one lock; 200 concurrent appends must all land."""
    store = StateStore()
    await asyncio.gather(*(store.add_drift_flag({"n": i}) for i in range(200)))
    flags = (await store.snapshot()).drift_flags
    assert len(flags) == 200
    assert {f["n"] for f in flags} == set(range(200))


async def test_concurrent_mixed_updates_settle_consistently():
    store = StateStore()
    await asyncio.gather(
        store.update_foreground(ForegroundInfo(hwnd=7)),
        store.update_open_apps({"a.exe": {"pid": 1}}),
        store.update_media_session(MediaSessionInfo(session_id="s1")),
        store.update_browser_tabs({"t": BrowserTabInfo(url="u")}),
    )
    snap = await store.snapshot()
    assert snap.foreground.hwnd == 7
    assert snap.open_apps and snap.media_sessions and snap.browser_tabs


async def test_snapshot_returns_the_live_state_not_a_copy():
    """Documented as-is: `snapshot()` hands out the real SystemState, so a
    caller that mutates it mutates the store. Readers must treat it as
    read-only; if this ever starts returning a deepcopy, this test is the
    place that says so."""
    store = StateStore()
    a = await store.snapshot()
    b = await store.snapshot()
    assert a is b
    a.open_apps["injected.exe"] = {"pid": 999}
    assert "injected.exe" in (await store.snapshot()).open_apps


# ── Debounce ─────────────────────────────────────────────────────────────────
def test_debounce_allows_the_first_call_on_wall_clock():
    """_last_ts starts at 0.0, so against a real timestamp the first poll passes."""
    assert Debounce(interval_s=1.5).allow() is True


def test_debounce_blocks_until_the_interval_elapses():
    d = Debounce(interval_s=1.5)
    assert d.allow(now=100.0) is True
    assert d.allow(now=100.4) is False
    assert d.allow(now=101.49) is False
    assert d.allow(now=101.5) is True, "boundary is inclusive (>=)"


def test_debounce_measures_from_the_last_allowed_call():
    """Blocked calls must not push the window forward, or a busy loop could
    starve the sync permanently."""
    d = Debounce(interval_s=2.0)
    d.allow(now=0.0)  # arms at 0.0
    for t in (0.5, 1.0, 1.5, 1.9):
        assert d.allow(now=t) is False
    assert d.allow(now=2.0) is True


def test_debounce_throttles_a_fast_poll_to_its_interval():
    """The real shape of the sync loop: a 20 Hz tick against a 1.5 s debounce
    must fire once per interval, not on every tick."""
    d = Debounce(interval_s=1.5)
    ticks = [i * 0.05 for i in range(200)]  # 10 s at 20 Hz, starting at t=0.0
    allowed = [t for t in ticks if d.allow(now=t)]
    # t=0.0 is NOT allowed: _last_ts also starts at 0.0, so the very first tick
    # is one interval short. (Against a real wall clock the first poll does
    # pass - see test_debounce_allows_the_first_call_on_wall_clock.)
    assert allowed == [1.5, 3.0, 4.5, 6.0, 7.5, 9.0]


def test_debounce_instances_are_independent():
    a, b = Debounce(interval_s=1.0), Debounce(interval_s=1.0)
    assert a.allow(now=50.0) is True
    assert b.allow(now=50.0) is True, "b must not inherit a's timestamp"


def test_zero_interval_never_blocks():
    d = Debounce(interval_s=0.0)
    assert all(d.allow(now=t) for t in (10.0, 10.0, 10.0))
