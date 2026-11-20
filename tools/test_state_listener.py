"""Tests for the global state listener."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.state_listener import StateListener, StateSnapshot


def _proc(name: str) -> SimpleNamespace:
    # Mock(name=...) names the mock itself, it does NOT set a `.name` attribute,
    # which is what the listener reads. A namespace is the honest stand-in.
    return SimpleNamespace(name=name)


@pytest.fixture
def listener():
    """Create a listener with a short poll interval for testing."""
    return StateListener(poll_interval=0.1)


@pytest.fixture
def mock_processes():
    """Mock process_awareness.list_processes."""
    with patch("core.state_listener.process_awareness.list_processes") as mock:
        yield mock


@pytest.fixture
def mock_win32():
    """Mock win32gui and win32process."""
    with (
        patch("core.state_listener.win32gui") as mock_gui,
        patch("core.state_listener.win32process") as mock_proc,
        patch("core.state_listener.psutil") as mock_psutil,
    ):
        yield mock_gui, mock_proc, mock_psutil


@pytest.fixture
def mock_media_routing():
    """Mock the media_sessions probe."""
    with patch("core.state_listener.media_sessions") as mock:
        yield mock


@pytest.fixture
def mock_audio_endpoints():
    """Mock the COM-apartment pin."""
    with patch("core.state_listener._ensure_com"):
        yield


@pytest.fixture
def mock_browser_control():
    """Mock the browser-session registry."""
    with patch("core.state_listener._registry") as mock:
        yield mock


def _run(loop, coro):
    return loop.run_until_complete(coro)


class TestStateListener:
    def test_start_stop(self, listener):
        """Test that listener can start and stop."""
        loop = asyncio.new_event_loop()
        listener.start(loop)
        assert listener._running
        assert listener._task is not None
        listener.stop()
        assert not listener._running
        assert listener._task is None
        # Let the cancelled task settle so the loop closes without a
        # "coroutine was never awaited" warning.
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()

    def test_register_unregister_callback(self, listener):
        """Test callback registration."""
        callback = Mock()
        listener.register_callback(callback)
        assert callback in listener._callbacks
        listener.unregister_callback(callback)
        assert callback not in listener._callbacks

    def test_detect_process_changes(
        self,
        listener,
        mock_processes,
        mock_win32,
        mock_media_routing,
        mock_audio_endpoints,
        mock_browser_control,
    ):
        """Test process start/stop detection (names compare case-insensitively)."""
        mock_processes.return_value = [_proc("Spotify.exe"), _proc("chrome.exe")]
        mock_gui, mock_proc, mock_psutil = mock_win32
        mock_gui.EnumWindows = Mock(side_effect=lambda fn, ctx: None)
        mock_media_routing.return_value = []

        loop = asyncio.new_event_loop()
        try:
            snap1 = _run(loop, listener._capture_snapshot())
            assert "spotify.exe" in snap1.processes
            assert "chrome.exe" in snap1.processes

            mock_processes.return_value = [_proc("chrome.exe"), _proc("notepad.exe")]
            snap2 = _run(loop, listener._capture_snapshot())

            changes = listener._detect_changes(snap1, snap2)
            assert "notepad.exe" in changes["processes_started"]
            assert "spotify.exe" in changes["processes_stopped"]
        finally:
            loop.close()

    def test_detect_window_changes(
        self,
        listener,
        mock_processes,
        mock_win32,
        mock_media_routing,
        mock_audio_endpoints,
        mock_browser_control,
    ):
        """Test window open/close detection."""
        mock_processes.return_value = [_proc("notepad.exe")]
        mock_gui, mock_proc, mock_psutil = mock_win32
        mock_media_routing.return_value = []

        mock_gui.EnumWindows = Mock(side_effect=lambda fn, ctx: fn(123, None))
        mock_proc.GetWindowThreadProcessId.return_value = (0, 456)
        mock_proc_obj = Mock()
        mock_proc_obj.name.return_value = "notepad.exe"
        mock_psutil.Process.return_value = mock_proc_obj
        mock_gui.IsWindowVisible.return_value = True
        mock_gui.IsIconic.return_value = False

        loop = asyncio.new_event_loop()
        try:
            snap1 = _run(loop, listener._capture_snapshot())
            assert ("notepad.exe", "visible") in snap1.windows

            mock_gui.IsWindowVisible.return_value = False
            snap2 = _run(loop, listener._capture_snapshot())

            changes = listener._detect_changes(snap1, snap2)
            assert {"app": "notepad.exe", "state": "visible", "type": "closed"} in changes[
                "windows_changed"
            ]
        finally:
            loop.close()

    def test_detect_audio_changes(
        self,
        listener,
        mock_processes,
        mock_win32,
        mock_media_routing,
        mock_audio_endpoints,
        mock_browser_control,
    ):
        """Test audio session state changes."""
        mock_processes.return_value = [_proc("spotify.exe")]
        mock_gui, mock_proc, mock_psutil = mock_win32
        mock_gui.EnumWindows = Mock(side_effect=lambda fn, ctx: None)

        mock_session = Mock()
        mock_session.stem = "spotify"
        mock_session.active = True
        mock_media_routing.return_value = [mock_session]

        loop = asyncio.new_event_loop()
        try:
            snap1 = _run(loop, listener._capture_snapshot())
            assert snap1.audio_sessions.get("spotify") == "active"

            mock_session.active = False
            snap2 = _run(loop, listener._capture_snapshot())

            changes = listener._detect_changes(snap1, snap2)
            assert changes["audio_changed"].get("spotify") == "inactive"
        finally:
            loop.close()

    def test_callback_notification(
        self,
        listener,
        mock_processes,
        mock_win32,
        mock_media_routing,
        mock_audio_endpoints,
        mock_browser_control,
    ):
        """Test that callbacks are notified of changes."""
        mock_processes.return_value = [_proc("spotify.exe")]
        mock_gui, mock_proc, mock_psutil = mock_win32
        mock_gui.EnumWindows = Mock(side_effect=lambda fn, ctx: None)
        mock_media_routing.return_value = []

        loop = asyncio.new_event_loop()
        try:
            snap1 = _run(loop, listener._capture_snapshot())
            listener._last_snapshot = snap1

            callback = AsyncMock()
            listener.register_callback(callback)

            mock_processes.return_value = [_proc("chrome.exe")]
            snap2 = _run(loop, listener._capture_snapshot())

            changes = listener._detect_changes(snap1, snap2)
            _run(loop, listener._notify_callbacks(snap1, snap2, changes))

            callback.assert_called_once()
            args = callback.call_args[0]
            assert args[0] == snap1
            assert args[1] == snap2
            assert "chrome.exe" in args[2]["processes_started"]
        finally:
            loop.close()


class TestStateSnapshot:
    def test_snapshot_creation(self):
        """Test StateSnapshot dataclass."""
        snap = StateSnapshot(
            processes={"spotify.exe", "chrome.exe"},
            windows={("spotify.exe", "visible")},
            audio_sessions={"spotify": "active"},
            media_playback={"spotify": "playing"},
        )
        assert "spotify.exe" in snap.processes
        assert "chrome.exe" in snap.processes
        assert ("spotify.exe", "visible") in snap.windows
        assert snap.audio_sessions["spotify"] == "active"
        assert snap.media_playback["spotify"] == "playing"
        assert snap.timestamp > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
