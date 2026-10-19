"""Literal Spotify queries and direct retrieval of saved personal facts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memory import memory_manager as memory


@pytest.fixture
def saved():
    return {
        "preferences": {
            "favorite_song": {"value": "سواد الغيم by Mikasa Music", "updated": "2020-01-01"},
            "son_favorite_song": {"value": "A different song", "updated": "2026-10-15"},
        },
        "operational_instruction": {"memory_rule": {"value": "Always remember my favourite song"}},
    }


@pytest.mark.parametrize(
    "query", ["my song", "my favourite song", "favorite song", "Play my saved track"]
)
def test_saved_favourite_beats_instructions_and_child_without_embedding(saved, monkeypatch, query):
    semantic = Mock(
        side_effect=AssertionError("Exact personal recall must not wait for embeddings")
    )
    monkeypatch.setattr(memory, "_apply_semantic_scores", semantic)
    found = memory.rank_memory_entries(saved, query, min_score=memory._RETRIEVAL_MIN_SCORE)
    assert [(e["category"], e["key"], e["value"]) for e in found] == [
        ("preferences", "favorite_song", "سواد الغيم by Mikasa Music")
    ]
    semantic.assert_not_called()


def test_child_reference_does_not_use_owners_song(saved, monkeypatch):
    monkeypatch.setattr(memory, "_apply_semantic_scores", Mock())
    found = memory.rank_memory_entries(saved, "my son's favorite song")
    assert found[0]["key"] == "son_favorite_song"


def test_retrieval_tool_returns_verified_fact_not_instruction(saved, monkeypatch):
    monkeypatch.setattr(memory, "load_memory", lambda: saved)
    monkeypatch.setattr(memory, "safe_write_json", Mock())
    result = memory.retrieve_context_reinforced("my favourite song")
    assert result == "preferences/favorite_song: سواد الغيم by Mikasa Music"
    assert saved["preferences"]["favorite_song"]["access_count"] == 1
    assert "access_count" not in saved["preferences"]["son_favorite_song"]


def test_absent_fact_is_not_invented(monkeypatch):
    monkeypatch.setattr(memory, "load_memory", lambda: {})
    monkeypatch.setattr(memory, "_apply_semantic_scores", Mock())
    assert memory.retrieve_context_reinforced("my favourite song") == ""


def test_search_navigation_prefers_verified_literal_field(monkeypatch):
    from actions import spotify_control, spotify_ui
    from actions.spotify_search_worker import navigate_search

    field = Mock(return_value=True)
    uri = Mock()
    monkeypatch.setattr(spotify_ui, "set_search_query", field)
    monkeypatch.setattr(spotify_control, "_open_spotify_uri", uri)
    assert navigate_search(42, "سواد الغيم by Mikasa Music")
    field.assert_called_once_with(42, "سواد الغيم by Mikasa Music", timeout_s=1.0)
    uri.assert_not_called()


def test_ignored_setvalue_uses_focused_unicode_keys_and_verifies(monkeypatch):
    from pywinauto import keyboard

    from actions import spotify_control, spotify_ui

    value = SimpleNamespace(CurrentValue="old+query", SetValue=Mock())
    box = SimpleNamespace(
        iface_value=value,
        set_focus=Mock(),
        element_info=SimpleNamespace(element=SimpleNamespace(CurrentHasKeyboardFocus=True)),
    )
    monkeypatch.setattr(spotify_ui, "_UIA", True)
    monkeypatch.setattr(
        spotify_ui, "Desktop", lambda **kw: SimpleNamespace(window=lambda **kw: object())
    )
    monkeypatch.setattr(spotify_ui, "_buttons", lambda win: [])
    monkeypatch.setattr(spotify_ui, "_find_search_box", lambda win: box)
    monkeypatch.setattr(spotify_control, "_focus_spotify_window", lambda hwnd: True)
    query = "سواد الغيم + Music"

    def keys(text, **kwargs):
        if kwargs.get("vk_packet"):
            assert text == "سواد الغيم {+} Music"
            assert kwargs["with_spaces"]
            value.CurrentValue = query

    monkeypatch.setattr(keyboard, "send_keys", keys)
    assert spotify_ui.set_search_query(42, query, timeout_s=1.0)
    assert value.CurrentValue == query


def test_keyboard_fallback_never_types_without_verified_focus(monkeypatch):
    from pywinauto import keyboard

    from actions import spotify_control, spotify_ui

    box = SimpleNamespace(
        iface_value=SimpleNamespace(CurrentValue="old", SetValue=Mock()),
        set_focus=Mock(),
        element_info=SimpleNamespace(element=SimpleNamespace(CurrentHasKeyboardFocus=False)),
    )
    monkeypatch.setattr(spotify_ui, "_UIA", True)
    monkeypatch.setattr(
        spotify_ui, "Desktop", lambda **kw: SimpleNamespace(window=lambda **kw: object())
    )
    monkeypatch.setattr(spotify_ui, "_buttons", lambda win: [])
    monkeypatch.setattr(spotify_ui, "_find_search_box", lambda win: box)
    monkeypatch.setattr(spotify_control, "_focus_spotify_window", lambda hwnd: True)
    send = Mock()
    monkeypatch.setattr(keyboard, "send_keys", send)
    assert not spotify_ui.set_search_query(42, "private query", timeout_s=1.0)
    send.assert_not_called()
