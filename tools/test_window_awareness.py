"""core.window_awareness: what window is in front, and what else is open.

This feeds the model's situational context, so a wrong answer is not a crash -
it is Era confidently narrating the wrong screen. Every OS call (win32gui,
win32process, psutil, pywinctl) is faked here; nothing reads the real desktop.

The behaviour worth pinning is the fallback chain. `_from_win32() or
_from_pywinctl() or WindowInfo(title="Desktop")` short-circuits on any
WindowInfo instance - and a dataclass instance is truthy even when its title is
empty - so a win32 probe that succeeds with a blank title wins the chain rather
than deferring to pywinctl.
"""

import pytest

import core.window_awareness as wa


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeWin32Gui:
    def __init__(self, hwnd=1234, title="Netflix - Brave", boom=False):
        self._hwnd, self._title, self._boom = hwnd, title, boom

    def GetForegroundWindow(self):
        if self._boom:
            raise OSError("no foreground window")
        return self._hwnd

    def GetWindowText(self, _hwnd):
        return self._title


class _FakeWin32Process:
    def __init__(self, pid=999):
        self._pid = pid

    def GetWindowThreadProcessId(self, _hwnd):
        return (0, self._pid)


class _FakePsutil:
    def __init__(self, name="brave.exe", boom=False):
        self._name, self._boom = name, boom

    def Process(self, _pid):
        if self._boom:
            raise RuntimeError("no such process")
        return type("P", (), {"name": lambda _s: self._name})()


class _FakeWindow:
    def __init__(self, title):
        self.title = title


class _FakePwc:
    def __init__(self, active=None, windows=(), boom=False):
        self._active, self._windows, self._boom = active, list(windows), boom

    def getActiveWindow(self):
        if self._boom:
            raise RuntimeError("display unavailable")
        return self._active

    def getAllWindows(self):
        if self._boom:
            raise RuntimeError("display unavailable")
        return self._windows


@pytest.fixture(autouse=True)
def _no_real_memory_writes(monkeypatch):
    """get_active_window records the window into intent memory; capture the
    calls instead of writing."""
    calls = []
    monkeypatch.setattr(wa, "remember_action", lambda **kw: calls.append(kw))
    return calls


def _use_win32(monkeypatch, gui=None, proc=None, ps=None):
    monkeypatch.setattr(wa, "_WIN32", True)
    monkeypatch.setattr(wa, "win32gui", gui or _FakeWin32Gui(), raising=False)
    monkeypatch.setattr(wa, "win32process", proc or _FakeWin32Process(), raising=False)
    monkeypatch.setattr(wa, "psutil", ps or _FakePsutil(), raising=False)


# ── _from_win32 ──────────────────────────────────────────────────────────────
def test_win32_probe_reports_title_pid_and_app(monkeypatch):
    _use_win32(monkeypatch)
    info = wa._from_win32()
    assert info.title == "Netflix - Brave"
    assert info.pid == 999
    assert info.exe == "brave.exe"
    assert info.app == "brave", "app is the exe with its extension stripped"


def test_win32_probe_survives_a_dead_process(monkeypatch):
    """The window exists but its PID is already gone: keep the title, drop the
    process detail rather than losing the whole probe."""
    _use_win32(monkeypatch, ps=_FakePsutil(boom=True))
    info = wa._from_win32()
    assert info.title == "Netflix - Brave"
    assert info.exe == "" and info.app == ""


def test_win32_probe_returns_none_when_the_api_fails(monkeypatch):
    _use_win32(monkeypatch, gui=_FakeWin32Gui(boom=True))
    assert wa._from_win32() is None


def test_win32_probe_returns_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(wa, "_WIN32", False)
    assert wa._from_win32() is None


def test_win32_probe_normalises_a_null_title(monkeypatch):
    """GetWindowText returns None for a title-less window; it must become ""."""
    _use_win32(monkeypatch, gui=_FakeWin32Gui(title=None))
    assert wa._from_win32().title == ""


# ── _from_pywinctl ───────────────────────────────────────────────────────────
def test_pywinctl_probe_reports_title_only(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(active=_FakeWindow("Spotify Premium")))
    info = wa._from_pywinctl()
    assert info.title == "Spotify Premium"
    assert info.pid is None and info.exe == "", "pywinctl carries no process detail"


def test_pywinctl_probe_returns_none_without_an_active_window(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(active=None))
    assert wa._from_pywinctl() is None


def test_pywinctl_probe_returns_none_when_it_raises(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(boom=True))
    assert wa._from_pywinctl() is None


def test_pywinctl_probe_returns_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", False)
    assert wa._from_pywinctl() is None


# ── fallback chain ───────────────────────────────────────────────────────────
def test_win32_is_preferred_over_pywinctl(monkeypatch):
    monkeypatch.setattr(wa, "_from_win32", lambda: wa.WindowInfo(title="from-win32"))
    monkeypatch.setattr(wa, "_from_pywinctl", lambda: wa.WindowInfo(title="from-pwc"))
    assert wa.get_active_window()["title"] == "from-win32"


def test_pywinctl_is_used_when_win32_is_unavailable(monkeypatch):
    monkeypatch.setattr(wa, "_from_win32", lambda: None)
    monkeypatch.setattr(wa, "_from_pywinctl", lambda: wa.WindowInfo(title="from-pwc"))
    assert wa.get_active_window()["title"] == "from-pwc"


def test_desktop_is_the_last_resort(monkeypatch):
    monkeypatch.setattr(wa, "_from_win32", lambda: None)
    monkeypatch.setattr(wa, "_from_pywinctl", lambda: None)
    assert wa.get_active_window()["title"] == "Desktop"


def test_a_blank_titled_win32_result_still_wins_the_chain(monkeypatch):
    """Documented sharp edge: WindowInfo() is truthy, so an empty-title win32
    result short-circuits `or` and pywinctl is never consulted."""
    monkeypatch.setattr(wa, "_from_win32", lambda: wa.WindowInfo(title=""))
    monkeypatch.setattr(wa, "_from_pywinctl", lambda: wa.WindowInfo(title="from-pwc"))
    assert wa.get_active_window()["title"] == ""


def test_get_active_window_returns_a_plain_dict(monkeypatch):
    _use_win32(monkeypatch)
    out = wa.get_active_window()
    assert isinstance(out, dict)
    assert set(out) == {"title", "app", "pid", "exe"}


# ── intent-memory side effect ────────────────────────────────────────────────
def test_a_real_window_is_recorded_to_intent_memory(monkeypatch, _no_real_memory_writes):
    _use_win32(monkeypatch)
    wa.get_active_window()
    assert _no_real_memory_writes == [{"window": "Netflix - Brave", "app": "brave"}]


def test_a_blank_title_is_not_recorded(monkeypatch, _no_real_memory_writes):
    monkeypatch.setattr(wa, "_from_win32", lambda: wa.WindowInfo(title=""))
    wa.get_active_window()
    assert _no_real_memory_writes == [], "nothing useful to remember"


def test_a_missing_app_is_recorded_as_none_not_empty_string(monkeypatch, _no_real_memory_writes):
    monkeypatch.setattr(wa, "_from_win32", lambda: wa.WindowInfo(title="Untitled", app=""))
    wa.get_active_window()
    assert _no_real_memory_writes == [{"window": "Untitled", "app": None}]


# ── get_active_title ─────────────────────────────────────────────────────────
def test_active_title_returns_the_title(monkeypatch):
    _use_win32(monkeypatch)
    assert wa.get_active_title() == "Netflix - Brave"


def test_active_title_falls_back_to_the_given_default(monkeypatch):
    monkeypatch.setattr(wa, "_from_win32", lambda: wa.WindowInfo(title=""))
    assert wa.get_active_title() == "Desktop"
    assert wa.get_active_title(default="nothing") == "nothing"


# ── list_windows ─────────────────────────────────────────────────────────────
def test_list_windows_returns_titles(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(windows=[_FakeWindow("Brave"), _FakeWindow("Spotify")]))
    assert wa.list_windows() == [{"title": "Brave"}, {"title": "Spotify"}]


def test_list_windows_drops_blank_and_whitespace_titles(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(
        wa,
        "pwc",
        _FakePwc(
            windows=[_FakeWindow(""), _FakeWindow("   "), _FakeWindow(None), _FakeWindow("X")]
        ),
    )
    assert wa.list_windows() == [{"title": "X"}]


def test_list_windows_honours_the_limit(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(windows=[_FakeWindow(f"w{i}") for i in range(40)]))
    assert len(wa.list_windows(limit=5)) == 5


def test_list_windows_is_empty_without_pywinctl(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", False)
    assert wa.list_windows() == []


def test_list_windows_swallows_backend_errors(monkeypatch):
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(boom=True))
    assert wa.list_windows() == []


# ── format_context ───────────────────────────────────────────────────────────
def test_format_context_reports_active_window_and_app(monkeypatch):
    _use_win32(monkeypatch)
    monkeypatch.setattr(wa, "_PWC", False)
    text = wa.format_context()
    assert "Active window: Netflix - Brave" in text
    assert "Active app: brave" in text
    assert "Open windows:" not in text, "omitted when there is no window list"


def test_format_context_lists_open_windows(monkeypatch):
    _use_win32(monkeypatch)
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(windows=[_FakeWindow("Brave"), _FakeWindow("Code")]))
    text = wa.format_context()
    assert "Open windows:" in text
    assert "- Brave" in text and "- Code" in text


def test_format_context_says_unknown_rather_than_blank(monkeypatch):
    monkeypatch.setattr(wa, "_from_win32", lambda: None)
    monkeypatch.setattr(wa, "_from_pywinctl", lambda: None)
    monkeypatch.setattr(wa, "_PWC", False)
    text = wa.format_context()
    assert "Active window: Desktop" in text
    assert "Active app: unknown" in text


def test_format_context_truncates_a_very_long_title(monkeypatch):
    _use_win32(monkeypatch)
    monkeypatch.setattr(wa, "_PWC", True)
    monkeypatch.setattr(wa, "pwc", _FakePwc(windows=[_FakeWindow("T" * 300)]))
    line = [ln for ln in wa.format_context().splitlines() if ln.startswith("- ")][0]
    assert len(line) == 102, "'- ' plus the 100-char cap"
