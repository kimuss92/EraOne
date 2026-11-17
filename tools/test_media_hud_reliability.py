"""Regression cases from 2026-11-11, through real routing/engine/provider contracts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import personal_media, spotify_api, visual_engine
from core.tool_result import ToolResult

URI = "spotify:track:1234567890123456789012"
OTHER = "spotify:track:abcdefghijklmnopqrstuv"
VIDEO = "abcdefghijk"


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ERA_SPOTIFY_ACCESS_TOKEN", "test-only-token")
    monkeypatch.setattr(spotify_api, "_cooldown_until", 0)
    monkeypatch.setattr(spotify_api, "_cached", None)
    monkeypatch.setattr(spotify_api.time, "sleep", lambda _: None)
    return Mock()


def response(data=None, status=200):
    return SimpleNamespace(status_code=status, headers={}, json=lambda: data)


def track(name="Sabr El Rijal", uri=URI):
    return {"name": name, "uri": uri, "artists": [{"name": "Singer"}]}


def test_api_exact_title_ignores_wrong_first_hit_and_verifies_identity(api, monkeypatch):
    api.side_effect = [
        response({"tracks": {"items": [track("Savad Al Ghaim", OTHER), track()]}}),
        response(status=204),
        response({"is_playing": True, "item": track()}),
    ]
    monkeypatch.setattr(spotify_api.requests, "request", api)
    result = spotify_api.execute("search", {"value": "Sabr El Rijal"})
    assert result.success and result.verified and result.data["observed_uri"] == URI
    assert [c.args[0] for c in api.call_args_list] == ["GET", "PUT", "GET"]
    assert api.call_args_list[1].kwargs["json"] == {"uris": [URI]}


@pytest.mark.parametrize("items", [[track("Savad Al Ghaim")], [track(), track(uri=OTHER)], []])
def test_no_exact_or_ambiguous_title_never_starts_a_track(api, monkeypatch, items):
    api.return_value = response({"tracks": {"items": items}})
    monkeypatch.setattr(spotify_api.requests, "request", api)
    result = ToolResult.from_any(spotify_api.execute("search", {"value": "Sabr El Rijal"}))
    assert not result.success and result.error_type == "TrackSelectionRequired"
    assert api.call_count == 1


@pytest.mark.parametrize(
    "state",
    [{"is_playing": False, "item": track()}, {"is_playing": True, "item": track(uri=OTHER)}, {}],
)
def test_accepted_play_does_not_mean_requested_track_is_playing(api, monkeypatch, state):
    api.side_effect = [response(status=204)] + [response(state)] * 4
    monkeypatch.setattr(spotify_api.requests, "request", api)
    result = ToolResult.from_any(spotify_api.execute("play_uri", {"uri": URI}))
    assert not result.success and result.error_type == "PlaybackNotVerified"
    assert sum(c.args[0] == "PUT" for c in api.call_args_list) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_search_api_failure_never_falls_back_to_a_different_transport(api, monkeypatch, status):
    api.return_value = response(status=status)
    monkeypatch.setattr(spotify_api.requests, "request", api)
    result = ToolResult.from_any(spotify_api.execute("search", {"value": "Sabr El Rijal"}))
    assert not result.success
    assert api.call_count == 1


def test_timeout_after_play_is_not_replayed(api, monkeypatch):
    api.side_effect = [response(status=204), spotify_api.requests.Timeout()]
    monkeypatch.setattr(spotify_api.requests, "request", api)
    assert not ToolResult.from_any(spotify_api.execute("play_uri", {"uri": URI})).success
    assert [c.args[0] for c in api.call_args_list] == ["PUT", "GET"]


@pytest.mark.parametrize(
    "utterance,title",
    [
        ("Play Sabr El Rijal on Spotify", "Sabr El Rijal"),
        ("Play my song called Sabr El Rijal", "Sabr El Rijal"),
        ("I didn't ask for sawad al ghaima, I ask for sabr al rijal.", "sabr al rijal"),
    ],
)
def test_explicit_title_overrides_stale_song_identity(utterance, title):
    tool, args = personal_media.resolve_song(
        "spotify_control", {"action": "play_uri", "uri": OTHER}, utterance
    )
    assert (tool, args) == ("spotify_control", {"action": "search", "value": title})


@pytest.mark.parametrize(
    "utterance",
    [
        "play my favorite song",
        "don't play Sabr El Rijal",
        "what did I play",
        "play music",
        "play it",
        # Live 2026-11-14: grounded as the literal title "me some chill music. chill rap
        # music", overriding the model's own query ("chill rap mix").
        "Play me some chill music. chill rap music",
        "play some chill rap music",
        "play me a chill rap mix",
        "put on something relaxing",
    ],
)
def test_generic_or_negated_speech_is_not_invented_as_a_title(utterance):
    assert personal_media.explicit_track(utterance) is None


def test_play_me_a_real_title_drops_the_indirect_object():
    assert personal_media.explicit_track("Play me Sabr El Rijal") == "Sabr El Rijal"
    assert personal_media.explicit_track("play for me Sabr El Rijal on Spotify") == "Sabr El Rijal"


def test_a_mood_request_keeps_the_models_own_query():
    args = {"action": "search", "value": "chill rap mix"}
    assert personal_media.ground_explicit_track(
        "spotify_control", args, "Play me some chill music. chill rap music"
    ) == ("spotify_control", args)


@pytest.fixture
def hud_env(tmp_path, monkeypatch):
    from app.hud import events, layout

    bus = events.HudBus()
    sent = []
    bus.attach(sent.append)
    monkeypatch.setattr(events, "hud", bus)
    storage = layout.WidgetLayout(tmp_path / "layout.json")
    monkeypatch.setattr(layout, "get_widget_layout", lambda: storage)
    monkeypatch.setattr(visual_engine, "PATH", tmp_path / "scene.json")
    return storage, sent


@pytest.mark.parametrize(
    "phrase,panel",
    [
        ("hide diagnostics", "hud.diagnostics"),
        ("hide live activity", "hud.live_activity"),
        ("hide system awareness", "hud.awareness"),
    ],
)
def test_transcript_and_wrong_model_payload_reach_real_engine(hud_env, phrase, panel):
    from tools.test_voice_vault_roundtrip import VoiceEra

    era = VoiceEra()
    era.user_says(phrase)
    result = era.call("visual_engine", command=json.dumps({"action": "remove", "panel_id": panel}))
    assert result.success, result.message
    storage, sent = hud_env
    if panel == "hud.diagnostics":
        assert sent[-1]["event"]["type"] == "hud.panel.visibility"
        assert sent[-1]["event"]["metadata"] == {
            "drawer": "diagnostics",
            "panel_id": panel,
            "visible": False,
        }
    else:
        widget = visual_engine.PANELS[panel]["widget"]
        assert (
            next(w for w in storage.snapshot()["widgets"] if w["id"] == widget)["visible"] is False
        )
        from app.hud.layout import WidgetLayout

        assert (
            next(w for w in WidgetLayout(storage.path).snapshot()["widgets"] if w["id"] == widget)[
                "visible"
            ]
            is False
        )


def test_feature_calls_real_apply_without_injected_fake_dispatcher(hud_env):
    from features.hud_panel_visibility import run

    result = run({"transcript": {"text": "hide live activity"}})
    assert result["ok"] and result["status"] == "applied"
    assert (
        next(w for w in hud_env[0].snapshot()["widgets"] if w["id"] == "activity")["visible"]
        is False
    )


@pytest.mark.parametrize("command", [[], None, 42, "bad"])
def test_non_object_visual_payload_is_expected_failure_not_engine_crash(command):
    from tools.test_voice_vault_roundtrip import VoiceEra

    result = VoiceEra().call("visual_engine", command=json.dumps(command))
    assert not result.success and result.error_type == "VisualCommandRejected"


def test_accumulated_transcript_reaches_provider_with_correction(monkeypatch):
    from actions import spotify_control
    from core import live_app_state
    from tools.test_voice_vault_roundtrip import VoiceEra

    seen = []

    def provider(parameters, player=None):
        seen.append(parameters)
        return ToolResult(
            success=True,
            message="Playing exact track",
            verified=True,
            verification_status="VERIFIED",
            verifier_name="spotify_api",
            data={"observed_uri": URI},
        )

    monkeypatch.setattr(spotify_control, "spotify_control", provider)
    monkeypatch.setattr(
        live_app_state,
        "collect_live_state",
        Mock(side_effect=AssertionError("OS state cannot replace exact API identity")),
    )
    era = VoiceEra()
    era._last_user_text = "rijal."
    era._inflight_user_text = "I didn't ask for sawad al ghaima, I ask for sabr al rijal."
    result = era.call("spotify_control", action="search", value="Savad Al Ghaim")
    assert result.success and result.verified, result.message
    assert seen == [{"action": "search", "value": "sabr al rijal"}]


def test_failure_records_regression_seed_immediately_and_excludes_gates(tmp_path, monkeypatch):
    from core import task_lessons as lessons

    monkeypatch.setattr(lessons, "_path", lambda: tmp_path / "lessons.json")
    lessons.observe(
        "play requested song",
        "youtube_video",
        {"action": "play", "query": "private title"},
        False,
        "PlaybackNotVerified",
        "private output",
        "FAILED",
        "LOW",
        now=100,
    )
    data = json.loads(lessons._path().read_text())
    assert len(data["diagnostic_cases"]) == 1
    assert data["diagnostic_cases"][0]["status"] == "needs_root_cause_and_regression"
    assert "private" not in json.dumps(data["diagnostic_cases"])
    assert lessons.diagnostic_for_failure(
        "youtube_video", {"action": "play"}, "PlaybackNotVerified"
    )
    assert (
        lessons.diagnostic_for_failure("youtube_video", {"action": "play"}, "ConfirmationRequired")
        is None
    )


def test_show_me_named_panel_is_not_mistaken_for_catalog():
    from features.hud_panel_visibility import parse_voice_command

    result = parse_voice_command("show me the weather panel")
    assert result["kind"] == "command"
    assert result["command"] == {"action": "panel", "panel_id": "hud.weather", "visible": True}


def test_hud_aliases_have_one_owner():
    owners = {}
    for panel_id, spec in visual_engine.PANELS.items():
        for alias in spec["aliases"]:
            assert alias not in owners, (alias, owners.get(alias), panel_id)
            owners[alias] = panel_id


def test_native_specific_title_never_selects_partial_wrong_song():
    from actions.spotify_ui import _best_search_match

    class Button:
        def window_text(self):
            return "Play Sabr Something Else by Someone"

        def rectangle(self):
            return SimpleNamespace(top=10)

    assert _best_search_match([Button()], "Sabr El Rijal") == (None, 0)


def test_reported_spelling_variant_matches_but_other_song_does_not():
    from actions.spotify_control import _track_matches
    from utils.media_match import score_media_candidate, title_spelling_variant

    title = "Saber Elrjel | صبر الرجال"
    assert title_spelling_variant("Sabr El Rijal", title)
    assert _track_matches("Sabr El Rijal", (title, "Mikasa Music"))
    assert score_media_candidate("Sabr El Rijal", "Hassen Thabet - " + title) >= 0.7
    assert not title_spelling_variant("Sabr El Rijal", "Savad Al Ghaim")
    assert not title_spelling_variant("Sabr Al Rijal", "Saber Elrkel")
    assert not _track_matches("Sabr El Rijal", ("Savad Al Ghaim", "Mikasa Music"))


def test_youtube_transliteration_survives_artist_and_music_terms():
    from utils.media_match import score_media_candidate

    title = "Hassen Thabet - Saber Elrjel | صبر الرجال (Mikasa Music)"
    assert score_media_candidate("Sabr Al Rijal Mikasa Music", title, platform="youtube") >= 0.86
    latin_only = "Hassen Thabet - Saber Elrjel | Mikasa Music"
    assert (
        score_media_candidate("Sabr Al Rijal Mikasa Music", latin_only, platform="youtube") >= 0.86
    )
    assert (
        score_media_candidate(
            "Sabr Al Rijal Mikasa Music",
            "Saber Elrjel (Full Version) | Mikasa Music",
            platform="youtube",
        )
        >= 0.86
    )
    assert (
        score_media_candidate(
            "Sabr Al Rijal Mikasa Music", "Mikasa Music - Savad Al Ghaim", platform="youtube"
        )
        < 0.7
    )
