"""File context cards (app/hud/tool_adapter.py): a real name for any path
shape, a path pulled out of a result sentence without the words around it, and
a Reveal action only for something that exists."""

from app.hud import tool_adapter as ta
from core.tool_result import ToolResult


def file_card(args=None, message="done"):
    out = ta.summarize(
        "file_controller", args or {}, ToolResult(success=True, message=message), call_id="c", ts=0
    )
    return out["cards"][0], out["sources"]


def test_a_folder_path_ending_in_a_separator_still_has_a_name(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir()
    card, sources = file_card({"path": str(folder) + "\\"})
    assert card["title"] == "config" and sources[0]["title"] == "config"


def test_forward_slash_paths_are_named_by_their_last_part():
    card, _ = file_card({"path": "C:/Users/someone/Documents/report.pdf"})
    assert card["title"] == "report.pdf"


def test_a_path_in_the_result_text_loses_trailing_words_and_punctuation(tmp_path):
    folder = tmp_path / "my notes"  # a real path with a space in it
    folder.mkdir()
    card, _ = file_card(message=f"Found it: {folder} and opened it.")
    assert card["payload"]["path"] == str(folder)
    assert card["title"] == "my notes"


def test_reveal_is_offered_only_for_what_exists(tmp_path):
    there = tmp_path / "a.txt"
    there.write_text("x")
    gone = tmp_path / "deleted.txt"
    assert file_card({"path": str(there)})[0]["actions"] == [{"id": "reveal", "label": "Reveal"}]
    card, sources = file_card({"path": str(gone)})
    assert card["actions"] == [] and card["title"] == "deleted.txt"
    assert sources[0]["subtitle"] == str(gone)  # still reported, just not revealable


def test_unfound_path_in_text_keeps_the_punctuation_trimmed_match():
    card, _ = file_card(message=r"Saved to Z:\nowhere\notes.txt.")
    assert card["payload"]["path"] == r"Z:\nowhere\notes.txt"


def test_a_failure_with_only_an_error_still_says_why():
    # short_message() is empty for such results; the HUD showed no reason at all
    r = ToolResult(success=False, error="Could not read the title cards", error_type="ActionError")
    assert ta.short_summary(r) == "Could not read the title cards"
    out = ta.summarize("netflix_control", {"action": "play"}, r, call_id="f", ts=0)
    assert out["status"] == "failed" and out["summary"] == "Could not read the title cards"
