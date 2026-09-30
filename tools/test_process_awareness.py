"""core.process_awareness: "is Spotify actually running?"

This is the telemetry the app-state gate refuses commands on, so a wrong answer
here is either a command sent to a dead app or a working command wrongly
skipped. psutil is faked in every test - nothing enumerates the real machine.

Two behaviours carry most of the risk and are pinned deliberately:
  * the 2 s process-list cache, which is what stops every gate check from
    walking the full process table, and
  * the substring match in `is_running`, which is loose on purpose (names vary
    across installs) and therefore can report a near-miss as a hit.
"""

import pytest

import core.process_awareness as pa


# ── fake psutil ──────────────────────────────────────────────────────────────
class _FakeProc:
    def __init__(self, pid, name, exe="", boom=False):
        self._boom = boom
        self.info = {"pid": pid, "name": name, "exe": exe}

    def __getattr__(self, item):
        raise AttributeError(item)


class _FakePsutil:
    def __init__(self, procs):
        self.procs = procs
        self.calls = 0

    def process_iter(self, _attrs=None):
        self.calls += 1
        for p in self.procs:
            if getattr(p, "_boom", False):
                raise_now = _Raiser()
                yield raise_now
            else:
                yield p


class _Raiser:
    """A process that dies between enumeration and attribute read - the normal
    race on a live machine, and it must be skipped, not propagated."""

    @property
    def info(self):
        raise RuntimeError("process vanished")


@pytest.fixture
def fake_ps(monkeypatch):
    """Install a fake psutil and clear the module-level cache."""

    def _install(procs):
        fake = _FakePsutil(procs)
        monkeypatch.setattr(pa, "psutil", fake)
        monkeypatch.setattr(pa, "_PSUTIL", True)
        monkeypatch.setattr(pa, "_CACHE", (0.0, []))
        return fake

    yield _install
    pa._CACHE = (0.0, [])


DEFAULT = [
    _FakeProc(101, "Spotify.exe", r"C:\Users\x\AppData\Spotify\Spotify.exe"),
    _FakeProc(202, "brave.exe", r"C:\Program Files\BraveSoftware\brave.exe"),
    _FakeProc(303, "python.exe", r"C:\Python314\python.exe"),
    _FakeProc(404, "explorer.exe", r"C:\Windows\explorer.exe"),
]


# ── enumeration ──────────────────────────────────────────────────────────────
def test_list_processes_maps_psutil_rows(fake_ps):
    fake_ps(DEFAULT)
    procs = pa.list_processes(refresh=True)
    assert [p.pid for p in procs] == [101, 202, 303, 404]
    assert procs[0].name == "Spotify.exe"
    assert procs[0].exe.endswith("Spotify.exe")


def test_processes_without_a_name_are_dropped(fake_ps):
    fake_ps([_FakeProc(1, ""), _FakeProc(2, None), _FakeProc(3, "ok.exe")])
    assert [p.name for p in pa.list_processes(refresh=True)] == ["ok.exe"]


def test_a_process_that_vanishes_mid_scan_is_skipped(fake_ps):
    """psutil raises for a PID that exits between enumeration and attribute
    read - the normal race on a live machine. That row is skipped and the scan
    carries on, so one dying process cannot blank out the whole table (which
    would read to the app-state gate as "nothing is running")."""
    fake_ps([_FakeProc(1, "a.exe"), _FakeProc(2, "boom", boom=True), _FakeProc(3, "c.exe")])
    names = [p.name for p in pa.list_processes(refresh=True)]
    assert names == ["a.exe", "c.exe"]


def test_missing_psutil_returns_empty_not_raise(monkeypatch):
    monkeypatch.setattr(pa, "_PSUTIL", False)
    monkeypatch.setattr(pa, "_CACHE", (0.0, []))
    assert pa.list_processes(refresh=True) == []
    assert pa.is_running("spotify") is False
    assert pa.snapshot()["psutil_available"] is False


# ── caching ──────────────────────────────────────────────────────────────────
def test_second_call_within_ttl_is_served_from_cache(fake_ps):
    """Every gate check calls this; without the cache each one walks the table."""
    fake = fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    assert fake.calls == 1
    pa.list_processes()
    pa.list_processes()
    assert fake.calls == 1, "TTL not expired: no re-enumeration"


def test_refresh_forces_a_rescan(fake_ps):
    fake = fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    pa.list_processes(refresh=True)
    assert fake.calls == 2


def test_expired_cache_triggers_a_rescan(fake_ps, monkeypatch):
    fake = fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    # Age the cache past the TTL.
    stamp, items = pa._CACHE
    monkeypatch.setattr(pa, "_CACHE", (stamp - pa._CACHE_TTL - 1, items))
    pa.list_processes()
    assert fake.calls == 2


def test_caller_cannot_corrupt_the_cache_through_the_returned_list(fake_ps):
    fake_ps(DEFAULT)
    first = pa.list_processes(refresh=True)
    first.clear()
    assert len(pa.list_processes()) == 4, "the cache hands out a copy"


# ── is_running / aliases ─────────────────────────────────────────────────────
@pytest.mark.parametrize("app", ["spotify", "brave", "python"])
def test_known_aliases_match_case_insensitively(fake_ps, app):
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    assert pa.is_running(app) is True


def test_absent_app_reports_false(fake_ps):
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    for app in ("discord", "steam", "firefox", "telegram"):
        assert pa.is_running(app) is False, app


def test_unknown_app_falls_back_to_its_own_name(fake_ps):
    """Anything not in APP_ALIASES is matched on the bare string."""
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    assert pa.is_running("explorer") is True
    assert pa.is_running("nothing-like-this") is False


def test_match_can_come_from_the_exe_path_alone(fake_ps):
    """Installers rename the binary; the full path still identifies the app."""
    fake_ps([_FakeProc(1, "svc_host_x64.exe", r"C:\BraveSoftware\brave-browser\app.exe")])
    pa.list_processes(refresh=True)
    assert pa.is_running("brave") is True


def test_substring_matching_is_loose_by_design(fake_ps):
    """Documented sharp edge: aliases match as substrings, so 'vscode.exe'
    satisfies a 'code' probe. Anything relying on an exact identity must not
    use is_running."""
    fake_ps([_FakeProc(1, "vscode.exe", "")])
    pa.list_processes(refresh=True)
    assert pa.is_running("code") is True


def test_is_running_tolerates_odd_input(fake_ps):
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    assert pa.is_running("  SPOTIFY  ") is True, "input is normalised"
    assert pa.is_running("") is True, "empty probe matches everything - caller must guard"


# ── find_app ─────────────────────────────────────────────────────────────────
def test_find_app_returns_every_matching_process(fake_ps):
    fake_ps(DEFAULT + [_FakeProc(105, "Spotify.exe", "")])
    pa.list_processes(refresh=True)
    found = pa.find_app("spotify")
    assert [p.pid for p in found] == [101, 105]


def test_find_app_returns_empty_when_absent(fake_ps):
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    assert pa.find_app("discord") == []


# ── name listing ─────────────────────────────────────────────────────────────
def test_running_app_names_dedupes_case_insensitively(fake_ps):
    fake_ps([_FakeProc(1, "chrome.exe"), _FakeProc(2, "Chrome.exe"), _FakeProc(3, "brave.exe")])
    pa.list_processes(refresh=True)
    assert pa.running_app_names() == ["chrome.exe", "brave.exe"]


def test_running_app_names_honours_the_limit(fake_ps):
    fake_ps([_FakeProc(i, f"p{i}.exe") for i in range(50)])
    pa.list_processes(refresh=True)
    assert len(pa.running_app_names(limit=5)) == 5


# ── aggregate views ──────────────────────────────────────────────────────────
def test_media_state_reports_each_player(fake_ps):
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    state = pa.media_state()
    assert state["spotify_running"] is True
    assert state["brave_running"] is True
    assert state["chrome_running"] is False
    assert set(state) == {
        "spotify_running",
        "brave_running",
        "chrome_running",
        "edge_running",
        "firefox_running",
    }


def test_snapshot_merges_availability_apps_and_media(fake_ps):
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    snap = pa.snapshot()
    assert snap["psutil_available"] is True
    assert "Spotify.exe" in snap["running_apps"]
    assert snap["spotify_running"] is True


def test_format_running_apps_renders_a_bullet_list(fake_ps):
    fake_ps(DEFAULT)
    pa.list_processes(refresh=True)
    text = pa.format_running_apps()
    assert "- Spotify.exe" in text and "- brave.exe" in text


def test_format_running_apps_says_so_when_there_is_no_data(monkeypatch):
    monkeypatch.setattr(pa, "_PSUTIL", False)
    monkeypatch.setattr(pa, "_CACHE", (0.0, []))
    assert pa.format_running_apps() == "No process data available."
