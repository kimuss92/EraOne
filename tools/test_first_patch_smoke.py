"""Smoke tests for the first Netflix patch in core/self_healing_router.py."""

from core.self_healing_router import (
    _WORD_TO_NUM,
    _active_netflix_page_context,
    _clean_netflix_media_query,
    _netflix_user_wants_autoplay,
)


class TestFixFGridOrdinals:
    def test_eleventh_to_twentieth(self):
        assert _WORD_TO_NUM["eleventh"] == 11
        assert _WORD_TO_NUM["twelfth"] == 12
        assert _WORD_TO_NUM["thirteenth"] == 13
        assert _WORD_TO_NUM["fourteenth"] == 14
        assert _WORD_TO_NUM["fifteenth"] == 15
        assert _WORD_TO_NUM["sixteenth"] == 16
        assert _WORD_TO_NUM["seventeenth"] == 17
        assert _WORD_TO_NUM["eighteenth"] == 18
        assert _WORD_TO_NUM["nineteenth"] == 19
        assert _WORD_TO_NUM["twentieth"] == 20

    def test_french_ordinals_11_to_15(self):
        assert _WORD_TO_NUM["onzieme"] == 11
        assert _WORD_TO_NUM["onzième"] == 11
        assert _WORD_TO_NUM["douzieme"] == 12
        assert _WORD_TO_NUM["douzième"] == 12
        assert _WORD_TO_NUM["treizieme"] == 13
        assert _WORD_TO_NUM["treizième"] == 13
        assert _WORD_TO_NUM["quatorzieme"] == 14
        assert _WORD_TO_NUM["quatorzième"] == 14
        assert _WORD_TO_NUM["quinzieme"] == 15
        assert _WORD_TO_NUM["quinzième"] == 15


class TestFixBNetflixUrlContext:
    def test_bare_netflix_url_detected(self):
        assert (
            _active_netflix_page_context(
                "open something", {"url": "https://www.netflix.com/tvshow"}
            )
            is True
        )

    def test_netflix_search_still_detected(self):
        assert (
            _active_netflix_page_context(
                "open something", {"url": "https://www.netflix.com/search"}
            )
            is True
        )

    def test_non_netflix_url_rejected(self):
        assert (
            _active_netflix_page_context("open something", {"url": "https://www.google.com"})
            is False
        )

    def test_empty_url_rejected(self):
        assert _active_netflix_page_context("open something", {}) is False


class TestFixDAutoplayIntent:
    def test_play_wants_autoplay(self):
        assert _netflix_user_wants_autoplay("play One Piece on Netflix") is True

    def test_watch_wants_autoplay(self):
        assert _netflix_user_wants_autoplay("watch Stranger Things") is True

    def test_start_wants_autoplay(self):
        assert _netflix_user_wants_autoplay("start The Witcher") is True

    def test_french_lance_wants_autoplay(self):
        assert _netflix_user_wants_autoplay("lance One Piece") is True

    def test_french_joue_wants_autoplay(self):
        assert _netflix_user_wants_autoplay("joue The Office") is True

    def test_french_regarde_wants_autoplay(self):
        assert _netflix_user_wants_autoplay("regarde Breaking Bad") is True

    def test_search_no_autoplay(self):
        assert _netflix_user_wants_autoplay("search Breaking Bad") is False

    def test_find_no_autoplay(self):
        assert _netflix_user_wants_autoplay("find The Crown") is False

    def test_look_for_no_autoplay(self):
        assert _netflix_user_wants_autoplay("look for The Witcher") is False

    def test_french_cherche_no_autoplay(self):
        assert _netflix_user_wants_autoplay("cherche One Piece") is False

    def test_french_recherche_no_autoplay(self):
        assert _netflix_user_wants_autoplay("recherche The Office") is False

    def test_empty_returns_false(self):
        assert _netflix_user_wants_autoplay("") is False

    def test_none_returns_false(self):
        assert _netflix_user_wants_autoplay(None) is False

    def test_ambiguous_play_in_search_returns_false(self):
        assert _netflix_user_wants_autoplay("search for a movie to play") is False


class TestFixAPreventOverStripping:
    def test_playing_with_fire_preserved(self):
        r = _clean_netflix_media_query("Playing with Fire on Netflix")
        assert r is not None
        assert "Playing" in r, f"Expected 'Playing' in result, got: {r}"
        assert "with Fire" in r, f"Expected 'with Fire' in result, got: {r}"

    def test_do_you_have_playing_with_fire(self):
        # Conservative guard: only fires when text starts with "playing".
        # "do you have playing..." → "playing" is treated as a filler verb → "With Fire"
        r = _clean_netflix_media_query("do you have playing with fire")
        assert r is not None
        assert "With Fire" in r, f"Expected 'With Fire' in result, got: {r}"

    def test_play_breaking_bad_still_works(self):
        r = _clean_netflix_media_query("play breaking bad on netflix")
        assert r is not None
        assert "Breaking" in r, f"Expected 'Breaking' in result, got: {r}"

    def test_play_one_piece_is_a_search_not_a_grid_ordinal(self):
        # "one" alone used to trip the grid-ordinal guard and swallow the title.
        # With an action verb present ("play"), the title must survive as a search
        # query — same contract as test_browser_media_routing.py.
        r = _clean_netflix_media_query("play One Piece on Netflix")
        assert r is not None
        assert "One Piece" in r, f"Expected 'One Piece' in result, got: {r}"

    def test_playing_breaking_bad_still_works(self):
        r = _clean_netflix_media_query("playing breaking bad on netflix")
        assert r is not None
        assert "Breaking" in r, f"Expected 'Breaking' in result, got: {r}"

    def test_search_stranger_things(self):
        r = _clean_netflix_media_query("search Stranger Things on Netflix")
        assert r is not None
        assert "Stranger Things" in r, f"Expected 'Stranger Things' in result, got: {r}"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "--tb=short"])
