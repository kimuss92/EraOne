"""core.app_state_gate: the pre-command system-state awareness gate.

Proves the gate refuses a control command only on positive evidence the target
is absent, never gates a launch or a query, and fails OPEN when telemetry
breaks. All telemetry is faked - these never touch a real process or window.
"""

import core.app_state_gate as g


# ── launches and queries are never gated ─────────────────────────────────────
def test_launch_intent_never_gated():
    for tool, action in (
        ("computer_settings", "spotify_play"),
        ("spotify_control", "open"),
        ("spotify_control", "search"),
        ("youtube_video", "youtube_play"),
        ("netflix_control", "netflix_play"),
    ):
        d = g.evaluate_app_command(tool, action, {})
        assert d.allow, f"{tool}/{action} must not be gated (it launches the app)"


def test_query_intent_never_gated(monkeypatch):
    # Even with the app confirmed absent, a status/current_track query runs:
    # it IS the telemetry.
    monkeypatch.setattr(g, "_spotify_telemetry", lambda: {"present": False})
    for action in ("status", "current_track", "is_playing"):
        assert g.evaluate_app_command("spotify_control", action, {}).allow


def test_non_app_tool_not_gated():
    assert g.evaluate_app_command("web_search", "search", {}).allow
    assert g.evaluate_app_command("file_controller", "delete", {}).allow


# ── spotify: direct process + audio-session + window telemetry ───────────────
def test_spotify_control_skipped_when_absent(monkeypatch):
    monkeypatch.setattr(
        g,
        "_spotify_telemetry",
        lambda: {
            "present": False,
            "process_running": False,
            "window_present": False,
            "audio_session_state": None,
        },
    )
    d = g.evaluate_app_command("computer_settings", "spotify_pause", {})
    assert not d.allow
    assert d.app == "spotify"
    assert "spotify" in d.message.lower()
    # Must read as an honest state report, never as a failure (or it gets logged
    # as one and spammed to the Claude bridge).
    assert "error" not in d.message.lower()
    assert "failed" not in d.message.lower()


def test_spotify_allowed_when_present(monkeypatch):
    monkeypatch.setattr(
        g,
        "_spotify_telemetry",
        lambda: {"present": True, "process_running": True, "window_present": True},
    )
    assert g.evaluate_app_command("computer_settings", "spotify_next", {}).allow


def test_spotify_present_by_audio_session_only(monkeypatch):
    # A running audio session with no top-level window still counts as present.
    monkeypatch.setattr(
        g,
        "_spotify_telemetry",
        lambda: {
            "present": True,
            "process_running": False,
            "window_present": False,
            "audio_session_state": 1,
        },
    )
    assert g.evaluate_app_command("spotify_control", "pause", {}).allow


# ── browser media: only skip when NO browser is running ──────────────────────
def test_browser_media_skipped_only_without_browser(monkeypatch):
    monkeypatch.setattr(g, "_any_browser_running", lambda: False)
    for tool, action in (("youtube_video", "youtube_pause"), ("netflix_control", "netflix_pause")):
        d = g.evaluate_app_command(tool, action, {})
        assert not d.allow
        assert "browser" in d.message.lower()


def test_browser_media_allowed_when_browser_running(monkeypatch):
    # A backgrounded tab has no top-level title, so a running browser means the
    # real tab state is the handler's call, not the gate's.
    monkeypatch.setattr(g, "_any_browser_running", lambda: True)
    assert g.evaluate_app_command("youtube_video", "youtube_pause", {}).allow
    assert g.evaluate_app_command("netflix_control", "netflix_pause", {}).allow


# ── native desktop apps via the window_control registry ──────────────────────
def test_desktop_app_skipped_when_absent(monkeypatch):
    monkeypatch.setattr(
        g,
        "_desktop_app_telemetry",
        lambda k: {"present": False, "process_running": False, "window_present": False},
    )
    d = g.evaluate_app_command("premiere_control", "close", {})
    assert not d.allow
    assert "premiere" in d.message.lower()


def test_window_control_unknown_title_falls_through():
    # An unrecognized window title is left to window_control to resolve honestly.
    assert g.evaluate_app_command(
        "window_control", "close", {"title": "quarterly report.docx"}
    ).allow


def test_window_control_known_app_absent(monkeypatch):
    # "Spotify" resolves via the registry to the spotify branch (audio-session
    # aware), not the generic desktop path.
    monkeypatch.setattr(g, "_spotify_telemetry", lambda: {"present": False})
    d = g.evaluate_app_command("window_control", "minimize", {"title": "Spotify"})
    assert d.app == "spotify"
    assert not d.allow

    monkeypatch.setattr(
        g, "_desktop_app_telemetry", lambda k: {"present": False, "process_running": False}
    )
    d = g.evaluate_app_command("window_control", "close", {"title": "Premiere Pro"})
    assert d.app in ("premiere", "premiere pro")
    assert not d.allow


# ── degrade-never-raise ──────────────────────────────────────────────────────
def test_telemetry_error_fails_open(monkeypatch):
    def boom():
        raise RuntimeError("psutil is dead")

    monkeypatch.setattr(g, "_spotify_telemetry", boom)
    d = g.evaluate_app_command("spotify_control", "pause", {})
    assert d.allow
    assert d.reason == "telemetry_error"


def test_none_action_does_not_raise():
    d = g.evaluate_app_command("spotify_control", None, {})
    assert d.allow
