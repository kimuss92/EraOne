"""Routine media results are confirmed ONCE (app/era_live/speak_once.py).

2026-10-16: "Playing 'X' on Spotify, sir." during the call, then the tool's own line
"Playing 'X' by Mikasa Music (search: '...'), sir." after it — two bubbles.
"""

from app.era_live.speak_once import speak_once
from core.tool_result import ToolResult

PLAYED = ToolResult(
    success=True,
    message="Playing 'سواد الغيم' by Mikasa Music (search: 'سواد الغيم Mikasa Music'), sir.",
)
SEARCH = {"action": "search", "value": "سواد الغيم Mikasa Music"}


def test_a_pre_announced_play_gets_no_second_confirmation():
    out = speak_once(PLAYED, "spotify_control", SEARCH, "Playing 'سواد الغيم' on Spotify, sir.")
    assert "ALREADY told the user" in out.message and "Do not repeat" in out.message
    assert "(search:" not in out.message


def test_an_unannounced_play_is_one_short_line_without_the_search_echo():
    out = speak_once(PLAYED, "spotify_control", SEARCH, "")
    assert "(search:" not in out.message
    assert out.message.startswith("Playing 'سواد الغيم' by Mikasa Music, sir.")
    assert "one short sentence" in out.message


def test_the_original_result_is_not_mutated():
    speak_once(PLAYED, "spotify_control", SEARCH, "")
    assert "(search:" in PLAYED.message  # the HUD/intent memory copy stays real


def test_a_failure_is_always_reported_untouched():
    failed = ToolResult(success=False, message="Spotify search failed for 'x'.", error="x")
    assert speak_once(failed, "spotify_control", SEARCH, "Playing x, sir.") is failed


def test_an_unannounced_answer_passes_through_untouched():
    track = ToolResult(success=True, message="Now playing: A by B")
    assert speak_once(track, "spotify_control", {"action": "current_track_data"}, "") is track
    assert speak_once(track, "weather_report", {"city": "Tunis"}, "") is track


def test_any_tool_after_a_pre_announcement_adds_only_what_is_new():
    """Not just media: 'Checking the weather, sir.' then the full answer once."""
    weather = ToolResult(success=True, message="Tunis: 24C, clear.")
    out = speak_once(weather, "weather_report", {"city": "Tunis"}, "Checking the weather, sir.")
    assert out.message.startswith("Tunis: 24C, clear.")
    assert "Checking the weather, sir." in out.message
    assert "Do not repeat or rephrase it" in out.message


def test_routine_non_media_actions_are_one_line():
    opened = ToolResult(success=True, message="Chrome is open and focused, sir.")
    out = speak_once(opened, "open_app", {"app_name": "chrome"}, "")
    assert "one short sentence" in out.message


def test_results_with_their_own_speaking_order_are_left_alone():
    standby = ToolResult(success=True, message="Standby mode activated. Say nothing.")
    assert speak_once(standby, "weather_report", {}, "System offline.") is standby
    saved = ToolResult(success=True, message="Saved.")
    assert speak_once(saved, "save_memory", {}, "Noted, sir.") is saved


# ── Background outcomes are said like a person (2026-10-16) ──
def test_a_timeout_is_said_plainly_without_internals():
    from app.era_live.speak_once import spoken_outcome

    failed = ToolResult(
        success=False,
        message="Spotify search failed for 'سواد الغيم by Mikasa Music': Spotify's window "
        "timed out; its UI worker was stopped.",
    )
    said = spoken_outcome("spotify_control", failed)
    assert (
        said
        == "I couldn't get 'سواد الغيم by Mikasa Music' playing, sir — Spotify didn't respond in time."
    )
    assert "worker" not in said and "That didn't finish" not in said


def test_a_success_is_one_clean_sentence():
    from app.era_live.speak_once import spoken_outcome

    ok = ToolResult(success=True, message=PLAYED.message + " Window minimized.")
    assert spoken_outcome("spotify_control", ok) == "Playing 'سواد الغيم' by Mikasa Music, sir."


def test_an_unknown_failure_keeps_its_own_first_sentence():
    from app.era_live.speak_once import spoken_outcome

    odd = ToolResult(success=False, message="The printer is out of paper. Code 0x12.")
    assert spoken_outcome("printer", odd) == "The printer is out of paper."
