"""Exact-ID dispatch, native fallback and hostile-link regression checks."""

from unittest.mock import Mock

import pytest

from core.spotify_api import identity

ID = "4lOgmIWfsaTu8nj4JWW2qw"
URI = "spotify:track:" + ID
QUERY = "سواد الغيم by Mikasa Music"
TRACK = ("سواد الغيم", "Mikasa Music")


@pytest.mark.parametrize(
    "value",
    [
        ID,
        URI,
        "https://open.spotify.com/track/" + ID,
        "https://open.spotify.com/intl-fr/track/" + ID + "?si=tracking#context",
    ],
)
def test_share_links_and_existing_id_forms_have_one_canonical_identity(value):
    assert identity(value) == URI


@pytest.mark.parametrize(
    "value",
    [
        "http://open.spotify.com/track/" + ID,
        "https://open.spotify.com.evil.example/track/" + ID,
        "https://open.spotify.com@evil.example/track/" + ID,
        "https://user@open.spotify.com/track/" + ID,
        "https://open.spotify.com:8443/track/" + ID,
        "https://open.spotify.com/track/" + ID + "/extra",
        "https://open.spotify.com/track/%2f" + ID,
        "https://open.spotify.com/episode/" + ID,
        "spotify:track:" + ID + '" & calc',
        "spotify:track:" + ID + "\n",
        None,
    ],
)
def test_invalid_links_never_become_a_protocol_or_shell_target(value):
    with pytest.raises(ValueError):
        identity(value)


@pytest.fixture
def native(monkeypatch):
    from actions import spotify_control as control

    monkeypatch.setattr("memory.memory_manager.load_memory", lambda: {"preferences": {}})

    clock = [0.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        control, "_cancel_wait", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(control, "_spotify_window_exists", lambda: True)
    opened, play, mark = Mock(return_value=(True, "requested")), Mock(return_value=True), Mock()
    monkeypatch.setattr("actions.spotify_search_worker.open_track_uri", opened)
    monkeypatch.setattr(
        "actions.spotify_search_worker.play_track_page", Mock(return_value=(False, "not ready"))
    )
    monkeypatch.setattr(control, "_send_spotify_command", play)
    monkeypatch.setattr(control, "_mark_spotify_playing_state", mark)
    monkeypatch.setattr(
        control, "_spotify_search_and_play", Mock(side_effect=AssertionError("no name search"))
    )
    monkeypatch.setattr(
        control, "_spotify_play", Mock(side_effect=AssertionError("no generic resume"))
    )
    return control, opened, play, mark


def test_native_track_uri_waits_for_matching_metadata_before_explicit_play(monkeypatch, native):
    control, opened, play, mark = native
    monkeypatch.setattr(
        control,
        "_now_playing",
        Mock(side_effect=[None, ("Other song", "Other artist"), TRACK, TRACK, TRACK]),
    )
    monkeypatch.setattr(control, "_spotify_is_playing", Mock(side_effect=[False, False, True]))

    result = control._spotify_play_uri({"uri": URI, "value": QUERY})

    assert "Playing" in result and "Mikasa Music" in result
    assert opened.call_count == 1 and opened.call_args.args == (URI,)
    assert opened.call_args.kwargs["timeout_s"] <= 1.5
    play.assert_called_once_with(control.APPCOMMAND_MEDIA_PLAY, timeout_ms=250)
    mark.assert_called_once()


def test_unrelated_playing_audio_never_proves_requested_track_or_triggers_resume(
    monkeypatch, native
):
    control, opened, play, mark = native
    metadata = Mock(return_value=("Unrelated", "Someone else"))
    monkeypatch.setattr(control, "_now_playing", metadata)
    monkeypatch.setattr(control, "_spotify_is_playing", Mock(return_value=True))

    assert "could not verify" in control._spotify_play_uri({"uri": URI, "value": QUERY})
    assert metadata.call_count <= 32
    assert opened.call_count == 1 and opened.call_args.args == (URI,)
    play.assert_not_called()
    mark.assert_not_called()


def test_launch_failure_is_not_replayed_or_reported_playing(monkeypatch, native):
    control, opened, play, mark = native
    opened.return_value = (False, "failed")
    metadata = Mock(side_effect=AssertionError("no polling after launch failure"))
    monkeypatch.setattr(control, "_now_playing", metadata)
    assert "failed" in control._spotify_play_uri({"uri": URI, "value": QUERY}).lower()
    opened.assert_called_once()
    play.assert_not_called()
    mark.assert_not_called()


@pytest.mark.parametrize(
    "params", [{"uri": URI}, {"uri": "https://evil.example/track/" + ID, "value": QUERY}]
)
def test_unverifiable_or_invalid_native_target_does_not_launch(params, native):
    control, opened, play, mark = native
    assert "failed" in control._spotify_play_uri(params).lower()
    opened.assert_not_called()
    play.assert_not_called()
    mark.assert_not_called()


def test_cancellation_cannot_launch_or_resume_late(monkeypatch, native):
    control, opened, play, mark = native

    def cancelled():
        raise InterruptedError("cancelled")

    monkeypatch.setattr(control, "raise_if_cancelled", cancelled)
    with pytest.raises(InterruptedError):
        control._spotify_play_uri({"uri": URI, "value": QUERY})
    opened.assert_not_called()
    play.assert_not_called()
    mark.assert_not_called()


def test_only_unconfigured_api_and_disabled_bridge_reach_native(monkeypatch):
    from actions import spotify_control as control
    from core import spotify_bridge

    monkeypatch.setattr(spotify_bridge, "is_enabled", lambda: False)
    desktop = Mock(return_value="verified")
    monkeypatch.setattr(control, "_spotify_play_uri", desktop, raising=False)
    assert control._dispatch("play_uri", {"uri": URI, "value": QUERY}) == "verified"
    desktop.assert_called_once()


@pytest.mark.parametrize(
    "api_result", ["Spotify accepted play uri.", "Spotify API connection failed; outcome unknown."]
)
def test_api_result_never_replays_through_native_transport(monkeypatch, api_result):
    from actions import spotify_control as control

    monkeypatch.setattr("core.spotify_api.execute", Mock(return_value=api_result))
    monkeypatch.setattr(control, "remember_action", Mock())
    monkeypatch.setattr(control, "_dispatch", Mock(side_effect=AssertionError("must not replay")))
    monkeypatch.setattr("core.user_prefs.is_post_action_minimize", lambda app: False)
    monkeypatch.setattr("core.media_routing.enforce_media_target_async", Mock())
    assert control.spotify_control({"action": "play_uri", "uri": URI, "value": QUERY}) == api_result


def test_favorite_identity_keeps_saved_metadata_and_current_media_route(monkeypatch):
    from actions import spotify_control as control
    from core.personal_media import resolve_song

    monkeypatch.setattr(
        "memory.memory_manager.load_memory",
        lambda: {
            "preferences": {"favorite_song": {"value": QUERY}, "favorite_song_uri": {"value": URI}}
        },
    )
    _, args = resolve_song("spotify_control", {"action": "play"}, "play my favorite song")
    assert args == {"action": "play_uri", "uri": URI, "value": QUERY}
    monkeypatch.setattr("core.spotify_api.execute", lambda *a: None)
    monkeypatch.setattr(control, "remember_action", Mock())
    monkeypatch.setattr(control, "_dispatch", Mock(return_value="Playing the requested track."))
    monkeypatch.setattr("core.user_prefs.is_post_action_minimize", lambda app: True)
    minimize, route = Mock(), Mock()
    monkeypatch.setattr(control, "_minimize_after_use", minimize)
    monkeypatch.setattr("core.media_routing.enforce_media_target_async", route)
    control.spotify_control(args)
    minimize.assert_called_once()
    route.assert_called_once_with("spotify")


def test_direct_saved_uri_recovers_omitted_title_without_api(monkeypatch, native):
    control, opened, play, mark = native
    monkeypatch.setattr(
        "memory.memory_manager.load_memory",
        lambda: {
            "preferences": {
                "favorite_song": {"value": QUERY},
                "favorite_song_uri": {"value": URI},
            }
        },
    )
    monkeypatch.setattr(control, "_now_playing", Mock(return_value=TRACK))
    monkeypatch.setattr(control, "_spotify_is_playing", lambda: True)
    assert QUERY.split(" by ")[0] in control._spotify_play_uri({"uri": URI})
    opened.assert_called_once()
    play.assert_not_called()
    mark.assert_called_once()


def test_uri_navigation_activates_only_named_track_once(monkeypatch, native):
    control, opened, play, mark = native
    page_play = Mock(return_value=(True, "clicked"))
    monkeypatch.setattr("actions.spotify_search_worker.play_track_page", page_play)
    reads = iter([("Old", "Artist"), TRACK])
    monkeypatch.setattr(control, "_now_playing", lambda: next(reads, TRACK))
    monkeypatch.setattr(control, "_spotify_is_playing", lambda: True)
    assert "Playing" in control._spotify_play_uri({"uri": URI, "value": QUERY})
    opened.assert_called_once()
    page_play.assert_called_once()
    assert page_play.call_args.args == (QUERY,)
    play.assert_not_called()
    mark.assert_called_once()


def test_my_track_overrides_model_uri_with_saved_identity(monkeypatch):
    from core.personal_media import resolve_song

    monkeypatch.setattr(
        "memory.memory_manager.load_memory",
        lambda: {
            "preferences": {
                "favorite_song": {"value": QUERY},
                "favorite_song_uri": {"value": URI},
            }
        },
    )
    assert resolve_song(
        "spotify_control",
        {"action": "play_uri", "uri": "spotify:track:abcdefghijklmnopqrstuv"},
        "play my track",
    ) == ("spotify_control", {"action": "play_uri", "uri": URI, "value": QUERY})


def media_row(title, playing=True):
    return {
        "available": True,
        "title": title,
        "artist": "Artist",
        "playback": "playing" if playing else "paused",
    }


def test_next_waits_for_metadata_change_then_survives_completion_pipeline(monkeypatch, native):
    import asyncio

    from core import media_observation
    from core.completion_contract import verdict
    from core.live_app_state import refresh_after_command
    from core.verification import verify_tool_result

    control, _, play, mark = native
    monkeypatch.setattr(control, "_enforce_spotify_priority", Mock())
    read = Mock(side_effect=[media_row("Old"), media_row("Old"), media_row("New")])
    monkeypatch.setattr(media_observation, "spotify", read)
    result = control._spotify_next()
    result = asyncio.run(refresh_after_command("spotify_control", {"action": "next"}, result))
    result = verify_tool_result(result, tool="spotify_control", parameters={"action": "next"})
    assert result.success and result.verified and verdict(result, "spotify_control") == "proven"
    assert "New" in result.message and "Old" not in result.message
    assert result.data["previous_media_observation"]["title"] == "Old"
    play.assert_called_once_with(control.APPCOMMAND_MEDIA_NEXTTRACK)
    mark.assert_called_once()
    assert read.call_count == 3


@pytest.mark.parametrize("title,playing", [("Old", True), ("New", False)])
def test_skip_receipt_or_paused_new_metadata_cannot_prove_next(monkeypatch, native, title, playing):
    import asyncio

    from core import media_observation
    from core.live_app_state import refresh_after_command

    control, _, play, mark = native
    monkeypatch.setattr(control, "_enforce_spotify_priority", Mock())
    rows = iter([media_row("Old")])
    read = Mock(side_effect=lambda: next(rows, media_row(title, playing)))
    monkeypatch.setattr(media_observation, "spotify", read)
    result = control._spotify_next()
    assert (
        asyncio.run(refresh_after_command("spotify_control", {"action": "next"}, result)) is result
    )
    assert not result.success and not result.verified
    assert result.error_type == "PlaybackNotVerified"
    play.assert_called_once_with(control.APPCOMMAND_MEDIA_NEXTTRACK)
    mark.assert_not_called()


def test_registered_spotify_verifier_returns_result_even_inside_async_dispatch(monkeypatch):
    import asyncio

    from core import media_observation
    from core.action_verifiers import verify_action_result
    from core.tool_result import ToolResult

    monkeypatch.setattr(media_observation, "spotify", lambda: media_row("Current"))

    async def dispatch():
        return verify_action_result(
            "spotify_control", ToolResult.ok("Spotify is playing"), {"action": "play"}
        )

    result = asyncio.run(dispatch())
    assert isinstance(result, ToolResult) and result.verified
    assert result.verification_status == "VERIFIED" and "Current" in result.message


def test_playing_alone_cannot_verify_a_skip_receipt(monkeypatch):
    from core import media_observation
    from core.action_verifiers import verify_action_result
    from core.tool_result import ToolResult

    monkeypatch.setattr(media_observation, "spotify", lambda: media_row("Old"))
    result = verify_action_result(
        "spotify_control", ToolResult.ok("Skipping track."), {"action": "next"}
    )
    assert not result.verified and result.verification_status == "NOT_VERIFIED"


def test_protocol_child_never_imports_the_gui_stack(monkeypatch, capsys):
    import builtins
    import io
    import json

    from actions import spotify_search_worker as worker

    native_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in ("actions", "actions.spotify_control", "actions.spotify_ui", "pywinauto"):
            raise AssertionError("protocol mode must not import GUI controls")
        return native_import(name, *args, **kwargs)

    opened = Mock()
    monkeypatch.setattr(worker.os, "startfile", opened, raising=False)
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps({"mode": "uri", "uri": URI})))
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    worker.main()
    opened.assert_called_once_with(URI)
    assert json.loads(capsys.readouterr().out.split("ERA_SPOTIFY_RESULT:")[1])["ok"]


def test_protocol_mode_rejects_a_hostile_child_payload(monkeypatch, capsys):
    import io
    import json

    from actions import spotify_search_worker as worker

    opened = Mock()
    monkeypatch.setattr(worker.os, "startfile", opened, raising=False)
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        io.StringIO(json.dumps({"mode": "uri", "uri": "file:///C:/anything.exe"})),
    )
    worker.main()
    opened.assert_not_called()
    assert not json.loads(capsys.readouterr().out.split("ERA_SPOTIFY_RESULT:")[1])["ok"]


def test_track_page_worker_activates_named_control_without_search_or_uri_replay(
    monkeypatch, capsys
):
    import io
    import json

    from actions import spotify_control as control
    from actions import spotify_search_worker as worker

    monkeypatch.setattr(
        worker.sys,
        "stdin",
        io.StringIO(json.dumps({"mode": "track_page", "query": QUERY, "cold": False})),
    )
    monkeypatch.setattr(control, "_get_spotify_hwnd_and_title", lambda: (42, "Spotify"))
    monkeypatch.setattr(control, "_focus_spotify_window", Mock(return_value=True))
    forbidden = Mock(side_effect=AssertionError("page activation must not navigate"))
    monkeypatch.setattr(worker, "navigate_search", forbidden)
    monkeypatch.setattr(worker.os, "startfile", forbidden)
    activate = Mock(return_value=(True, "clicked"))
    monkeypatch.setattr("actions.spotify_ui.invoke_search_result", activate)
    worker.main()
    activate.assert_called_once_with(42, QUERY, timeout_s=2.0, prefer_click=True)
    forbidden.assert_not_called()
    assert json.loads(capsys.readouterr().out.split("ERA_SPOTIFY_RESULT:")[1])["ok"]


def test_native_explicit_play_uses_bounded_targeted_windows_message(monkeypatch):
    from actions import spotify_control as control

    monkeypatch.setattr(control, "_get_spotify_hwnd_and_title", lambda: (42, "Spotify"))
    bounded = Mock()
    monkeypatch.setattr(control.win32gui, "SendMessageTimeout", bounded)
    monkeypatch.setattr(
        control.win32gui, "SendMessage", Mock(side_effect=AssertionError("unbounded send"))
    )
    assert control._send_spotify_command(control.APPCOMMAND_MEDIA_PLAY, timeout_ms=250)
    assert bounded.call_args.args[-1] == 250
    assert bounded.call_args.args[0] == 42
    bounded.side_effect = OSError("Spotify is not responding")
    assert not control._send_spotify_command(control.APPCOMMAND_MEDIA_PLAY, timeout_ms=250)


def test_desktop_setup_does_not_make_any_api_request(monkeypatch):
    from actions import spotify_control as control

    monkeypatch.delenv("ERA_SPOTIFY_ACCESS_TOKEN", raising=False)
    http = Mock(side_effect=AssertionError("desktop setup must not call Spotify Web API"))
    monkeypatch.setattr("core.spotify_api.requests.request", http)
    monkeypatch.setattr("core.spotify_bridge.is_enabled", lambda: False)
    monkeypatch.setattr(control, "remember_action", Mock())
    native_play = Mock(return_value="Playing the requested track.")
    monkeypatch.setattr(control, "_spotify_play_uri", native_play)
    monkeypatch.setattr("core.user_prefs.is_post_action_minimize", lambda app: False)
    monkeypatch.setattr("core.media_routing.enforce_media_target_async", Mock())
    assert (
        control.spotify_control({"action": "play_uri", "uri": URI, "value": QUERY})
        == "Playing the requested track."
    )
    native_play.assert_called_once()
    http.assert_not_called()


def test_playback_state_finds_spotify_on_any_output_not_the_default(monkeypatch):
    from types import SimpleNamespace

    from actions import spotify_control as control

    monkeypatch.setattr(
        "core.media_routing.media_sessions",
        lambda: [
            SimpleNamespace(stem="spotify", active=False, endpoint_name="glasses"),
            SimpleNamespace(stem="spotify", active=True, endpoint_name="PC speakers"),
            SimpleNamespace(stem="msedge", active=True, endpoint_name="glasses"),
        ],
    )
    assert control._get_spotify_audio_session_state() == 1


def test_other_app_audio_does_not_prove_spotify_playback(monkeypatch):
    from types import SimpleNamespace

    from actions import spotify_control as control

    monkeypatch.setattr(
        "core.media_routing.media_sessions",
        lambda: [
            SimpleNamespace(stem="spotify", active=False),
            SimpleNamespace(stem="msedge", active=True),
        ],
    )
    assert control._get_spotify_audio_session_state() == 0
