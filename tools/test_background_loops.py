"""core.background_manager + the two windows/ sync loops.

These are the long-lived tasks Era starts at boot: one mirrors the foreground
window and process table into the state store, the other enforces exclusive
media playback through the Windows GSMTC session manager. Both are driven here
against fakes - no real window is read, no real player is paused - and each loop
is started, observed for a beat, then cancelled, so nothing outlives a test.

Note for anyone reading the sources: both windows/ modules define their loop
TWICE, an `if os.name != "nt"` stub followed by an unconditional real
definition. The second definition always wins, so the non-Windows stub is dead
code and the real loop's own ImportError fallback is what actually runs off
Windows. That fallback is exercised below.
"""

import asyncio
import sys
import time

import pytest

import core.media_state as media_state
from core.background_manager import BackgroundManager
from core.state_store import StateStore


# ── helpers ──────────────────────────────────────────────────────────────────
async def _run_briefly(coro_fn, *args, seconds=0.35):
    """Start a never-ending loop, let it work, then cancel it cleanly."""
    task = asyncio.create_task(coro_fn(*args))
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return task


# ── BackgroundManager lifecycle ──────────────────────────────────────────────
@pytest.fixture
def stub_loops(monkeypatch):
    """Replace both real loops with ones that just park."""
    started = []

    async def _fake(state):
        started.append(state)
        while True:
            await asyncio.sleep(3600)

    monkeypatch.setattr("windows.state_sync.run_state_sync_loop", _fake, raising=False)
    monkeypatch.setattr(
        "windows.media_exclusivity.run_media_exclusivity_loop", _fake, raising=False
    )
    return started


async def test_manager_creates_its_own_store_when_none_is_given():
    assert isinstance(BackgroundManager().state, StateStore)


async def test_manager_uses_the_store_it_is_handed():
    store = StateStore()
    assert BackgroundManager(store).state is store


async def test_start_all_launches_both_named_loops(stub_loops):
    mgr = BackgroundManager()
    await mgr.start_all()
    await asyncio.sleep(0)  # let the freshly created tasks actually enter
    try:
        assert {t.get_name() for t in mgr._tasks} == {"state_sync_loop", "media_exclusivity_loop"}
        assert len(stub_loops) == 2, "both loops actually entered"
        assert all(s is mgr.state for s in stub_loops), "both share the one store"
    finally:
        await mgr.stop_all()


async def test_start_all_is_idempotent(stub_loops):
    mgr = BackgroundManager()
    await mgr.start_all()
    await asyncio.sleep(0)
    try:
        await mgr.start_all()
        await asyncio.sleep(0)
        assert len(mgr._tasks) == 2, "a second start must not double the loops"
        assert len(stub_loops) == 2
    finally:
        await mgr.stop_all()


async def test_stop_all_cancels_every_loop(stub_loops):
    mgr = BackgroundManager()
    await mgr.start_all()
    running = list(mgr._tasks)
    await mgr.stop_all()
    assert all(t.cancelled() or t.done() for t in running)
    assert mgr._tasks == []
    assert mgr._started is False


async def test_stop_all_is_safe_before_anything_started():
    await BackgroundManager().stop_all()  # must not raise


async def test_the_manager_can_be_restarted(stub_loops):
    mgr = BackgroundManager()
    await mgr.start_all()
    await asyncio.sleep(0)
    await mgr.stop_all()
    await mgr.start_all()
    await asyncio.sleep(0)
    try:
        assert len(mgr._tasks) == 2 and len(stub_loops) == 4
    finally:
        await mgr.stop_all()


async def test_a_loop_that_dies_does_not_block_shutdown(monkeypatch):
    """stop_all gathers with return_exceptions, so one crashed loop must not
    stop the others being cleaned up."""

    async def _boom(state):
        raise RuntimeError("loop died")

    async def _park(state):
        while True:
            await asyncio.sleep(3600)

    monkeypatch.setattr("windows.state_sync.run_state_sync_loop", _boom, raising=False)
    monkeypatch.setattr(
        "windows.media_exclusivity.run_media_exclusivity_loop", _park, raising=False
    )
    mgr = BackgroundManager()
    await mgr.start_all()
    await asyncio.sleep(0)
    await mgr.stop_all()
    assert mgr._tasks == []


async def test_non_windows_fallback_starts_idle_loops(monkeypatch):
    """When the windows/ imports fail the idle fallback must accept the state
    argument the real loops take (it once raised TypeError instead)."""
    monkeypatch.setitem(sys.modules, "windows.state_sync", None)
    monkeypatch.setitem(sys.modules, "windows.media_exclusivity", None)
    mgr = BackgroundManager()
    await mgr.start_all()
    try:
        assert len(mgr._tasks) == 2
    finally:
        await mgr.stop_all()


# ── state_sync loop ──────────────────────────────────────────────────────────
class _Gui:
    def __init__(self, hwnd=77, title="Netflix - Brave", cls="Chrome_WidgetWin_1"):
        self.hwnd, self.title, self.cls = hwnd, title, cls

    def GetForegroundWindow(self):
        return self.hwnd

    def GetWindowText(self, _h):
        return self.title

    def GetClassName(self, _h):
        return self.cls


class _Proc:
    def GetWindowThreadProcessId(self, _h):
        return (0, 4242)


class _Psutil:
    def __init__(self, name="brave.exe", procs=(("brave.exe", 4242), ("Spotify.exe", 99))):
        self._name = name
        self._procs = procs

    def Process(self, _pid):
        if self._name is None:
            raise RuntimeError("gone")
        return type("P", (), {"name": lambda _s: self._name})()

    def process_iter(self, _attrs=None):
        for name, pid in self._procs:
            yield type("R", (), {"info": {"pid": pid, "name": name}})()


@pytest.fixture
def win_env(monkeypatch):
    def _install(gui=None, proc=None, ps=None):
        monkeypatch.setitem(sys.modules, "win32gui", gui or _Gui())
        monkeypatch.setitem(sys.modules, "win32process", proc or _Proc())
        monkeypatch.setitem(sys.modules, "psutil", ps or _Psutil())

    return _install


async def test_state_sync_mirrors_the_foreground_window(win_env):
    from windows.state_sync import run_state_sync_loop

    win_env()
    store = StateStore()
    await _run_briefly(run_state_sync_loop, store, seconds=0.2)
    fg = (await store.snapshot()).foreground
    assert fg.hwnd == 77
    assert fg.window_title == "Netflix - Brave"
    assert fg.process_id == 4242
    assert fg.process_name == "brave.exe"
    assert fg.class_name == "Chrome_WidgetWin_1"
    assert fg.timestamp > 0


async def test_state_sync_mirrors_the_process_table(win_env):
    from windows.state_sync import run_state_sync_loop

    win_env()
    store = StateStore()
    await _run_briefly(run_state_sync_loop, store, seconds=0.2)
    apps = (await store.snapshot()).open_apps
    assert set(apps) == {"brave.exe", "spotify.exe"}, "keyed by lowercase name"
    assert apps["brave.exe"] == {"pid": 4242, "name": "brave.exe"}


async def test_state_sync_falls_back_to_the_pid_when_the_process_is_gone(win_env):
    """A window whose process already exited must still report, with the PID
    standing in for the name rather than blanking the record."""
    from windows.state_sync import run_state_sync_loop

    win_env(ps=_Psutil(name=None))
    store = StateStore()
    await _run_briefly(run_state_sync_loop, store, seconds=0.2)
    assert (await store.snapshot()).foreground.process_name == "4242"


async def test_state_sync_handles_no_foreground_window(win_env):
    """hwnd 0 (nothing focused) records an empty stamped entry, not a crash."""
    from windows.state_sync import run_state_sync_loop

    win_env(gui=_Gui(hwnd=0))
    store = StateStore()
    await _run_briefly(run_state_sync_loop, store, seconds=0.2)
    fg = (await store.snapshot()).foreground
    assert fg.hwnd == 0 and fg.window_title == ""
    assert fg.timestamp > 0


async def test_state_sync_is_cancellable(win_env):
    from windows.state_sync import run_state_sync_loop

    win_env()
    task = await _run_briefly(run_state_sync_loop, StateStore(), seconds=0.1)
    assert task.cancelled()


async def test_state_sync_idles_instead_of_crashing_without_win32(monkeypatch):
    """The real non-Windows path: the in-function ImportError fallback."""
    from windows.state_sync import run_state_sync_loop

    monkeypatch.setitem(sys.modules, "win32gui", None)
    store = StateStore()
    await _run_briefly(run_state_sync_loop, store, seconds=0.1)
    assert (await store.snapshot()).last_sync_ts == 0.0, "idle: nothing written"


# ── media exclusivity loop ───────────────────────────────────────────────────
class _Status:
    PLAYING = 4
    PAUSED = 5
    STOPPED = 2


class _Playback:
    def __init__(self, owner):
        self.owner = owner

    async def pause_async(self):
        self.owner.paused = True


class _Session:
    def __init__(self, sid, app_id, status):
        self.id = sid
        self.source_app_user_model_id = app_id
        self._status = status
        self.paused = False

    def get_playback_info(self):
        return type("I", (), {"playback_status": self._status})()

    async def try_get_media_playback(self):
        return _Playback(self)


def _install_gsmtc(monkeypatch, sessions):
    """Fake the winrt GSMTC module tree the loop imports."""

    class _Manager:
        @classmethod
        async def request_async(cls):
            return cls()

        def get_sessions(self):
            return sessions

    import types

    control = types.ModuleType("winrt.windows.media.control")
    control.GlobalSystemMediaTransportControlsSessionManager = _Manager
    control.GlobalSystemMediaTransportControlsSessionPlaybackStatus = _Status
    for name, mod in (
        ("winrt", types.ModuleType("winrt")),
        ("winrt.windows", types.ModuleType("winrt.windows")),
        ("winrt.windows.media", types.ModuleType("winrt.windows.media")),
        ("winrt.windows.media.control", control),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


async def test_media_loop_records_every_session_in_the_store(monkeypatch):
    from windows.media_exclusivity import run_media_exclusivity_loop

    sessions = [
        _Session("s1", "Spotify.exe", _Status.PLAYING),
        _Session("s2", "Chrome.YouTube", _Status.PAUSED),
    ]
    _install_gsmtc(monkeypatch, sessions)
    monkeypatch.setattr(media_state, "reconcile_from_os_sessions", lambda items: None)

    store = StateStore()
    await _run_briefly(run_media_exclusivity_loop, store, seconds=0.3)
    recorded = (await store.snapshot()).media_sessions
    assert set(recorded) == {"s1", "s2"}
    assert recorded["s1"].is_playing is True and recorded["s1"].is_paused is False
    assert recorded["s2"].is_paused is True and recorded["s2"].is_playing is False
    assert recorded["s1"].display_name == "Spotify.exe"
    assert recorded["s1"].last_change_ts <= time.time()


async def test_only_one_source_is_left_playing(monkeypatch):
    """The point of the loop: a second player starting must pause the others."""
    from windows.media_exclusivity import run_media_exclusivity_loop

    first = _Session("s1", "Spotify.exe", _Status.PLAYING)
    second = _Session("s2", "Chrome.YouTube", _Status.PLAYING)
    _install_gsmtc(monkeypatch, [first, second])
    monkeypatch.setattr(media_state, "reconcile_from_os_sessions", lambda items: None)

    await _run_briefly(run_media_exclusivity_loop, StateStore(), seconds=0.3)
    assert first.paused is False, "the canonical (first playing) session keeps playing"
    assert second.paused is True, "every other playing session is paused"


async def test_nothing_is_paused_when_only_one_source_plays(monkeypatch):
    from windows.media_exclusivity import run_media_exclusivity_loop

    playing = _Session("s1", "Spotify.exe", _Status.PLAYING)
    idle = _Session("s2", "Chrome.YouTube", _Status.STOPPED)
    _install_gsmtc(monkeypatch, [playing, idle])
    monkeypatch.setattr(media_state, "reconcile_from_os_sessions", lambda items: None)

    await _run_briefly(run_media_exclusivity_loop, StateStore(), seconds=0.3)
    assert playing.paused is False


async def test_os_sessions_are_mapped_to_era_source_names(monkeypatch):
    """Spotify / YouTube / Netflix are reconciled into the synchronous cache the
    rest of Era reads; an unrecognised player is simply not mapped."""
    from windows.media_exclusivity import run_media_exclusivity_loop

    sessions = [
        _Session("s1", "Spotify.exe", _Status.PLAYING),
        _Session("s2", "Brave.YouTube.Player", _Status.PAUSED),
        _Session("s3", "Brave.Netflix", _Status.PAUSED),
        _Session("s4", "SomeRandomPlayer.exe", _Status.PAUSED),
    ]
    _install_gsmtc(monkeypatch, sessions)
    seen = []
    monkeypatch.setattr(media_state, "reconcile_from_os_sessions", lambda items: seen.append(items))

    await _run_briefly(run_media_exclusivity_loop, StateStore(), seconds=0.3)
    assert seen, "the reconcile hook was called"
    mapped = {(i["source"], i["status"]) for i in seen[-1]}
    assert ("spotify", "playing") in mapped
    assert ("youtube", "paused") in mapped
    assert ("netflix", "paused") in mapped
    assert not any(i["source"] not in {"spotify", "youtube", "netflix"} for i in seen[-1])


async def test_a_session_that_raises_is_skipped(monkeypatch):
    """One misbehaving player must not take down the whole loop."""
    from windows.media_exclusivity import run_media_exclusivity_loop

    class _Bad(_Session):
        def get_playback_info(self):
            raise RuntimeError("player is wedged")

    good = _Session("s1", "Spotify.exe", _Status.PLAYING)
    _install_gsmtc(monkeypatch, [_Bad("bad", "X", _Status.PLAYING), good])
    monkeypatch.setattr(media_state, "reconcile_from_os_sessions", lambda items: None)

    store = StateStore()
    await _run_briefly(run_media_exclusivity_loop, store, seconds=0.3)
    assert set((await store.snapshot()).media_sessions) == {"s1"}


async def test_media_loop_idles_when_gsmtc_is_unavailable(monkeypatch):
    """No winrt (non-Windows, or a stripped install): park, never crash."""
    from windows.media_exclusivity import run_media_exclusivity_loop

    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", None)
    store = StateStore()
    await _run_briefly(run_media_exclusivity_loop, store, seconds=0.15)
    assert (await store.snapshot()).media_sessions == {}


async def test_media_loop_survives_a_session_manager_that_fails(monkeypatch):
    import types

    from windows.media_exclusivity import run_media_exclusivity_loop

    class _Manager:
        @classmethod
        async def request_async(cls):
            raise RuntimeError("GSMTC unavailable")

    control = types.ModuleType("winrt.windows.media.control")
    control.GlobalSystemMediaTransportControlsSessionManager = _Manager
    control.GlobalSystemMediaTransportControlsSessionPlaybackStatus = _Status
    for name in ("winrt", "winrt.windows", "winrt.windows.media"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", control)

    store = StateStore()
    await _run_briefly(run_media_exclusivity_loop, store, seconds=0.25)
    assert (await store.snapshot()).media_sessions == {}
