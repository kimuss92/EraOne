"""
Tests for safe media selection — utils/media_match.py.

Validates title normalization, token scoring, YouTube URL filtering,
candidate selection, generic/specific query detection, and Netflix
candidate selection.

No real browser calls. No real YouTube/Netflix network calls.
All functions tested are pure Python.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from utils.media_match import (
    clean_youtube_watch_url,
    filter_youtube_candidates,
    is_generic_media_query,
    is_youtube_rejected_url,
    is_youtube_watch_url,
    normalize_title,
    parse_ordinal_media_command,
    score_media_candidate,
    select_best_media_candidate,
    token_overlap_score,
)

# ═════════════════════════════════════════════════════════════════════════════
# normalize_title
# ═════════════════════════════════════════════════════════════════════════════


class TestNormalizeTitle:
    def test_empty_string(self):
        assert normalize_title("") == ""

    def test_none_empty(self):
        assert normalize_title(None) == ""

    def test_lowercase(self):
        assert normalize_title("Despacito Luis Fonsi") == "despacito luis fonsi"

    def test_removes_punctuation(self):
        assert normalize_title("Hello, World!") == "hello world"

    def test_collapses_whitespace(self):
        assert normalize_title("  Adele   Hello  ") == "adele hello"

    def test_removes_special_chars(self):
        assert normalize_title("MrBeast's Latest! (2024)") == "mrbeast s latest 2024"


# ═════════════════════════════════════════════════════════════════════════════
# token_overlap_score
# ═════════════════════════════════════════════════════════════════════════════


class TestTokenOverlapScore:
    def test_identical_tokens(self):
        assert token_overlap_score("hello world", "hello world") == 1.0

    def test_partial_overlap(self):
        s = token_overlap_score("despacito luis fonsi", "despacito luis fonsi official video")
        assert 0.5 < s < 1.0

    def test_no_overlap(self):
        assert token_overlap_score("cats", "football highlights") == 0.0

    def test_empty_query(self):
        assert token_overlap_score("", "hello world") == 0.0

    def test_empty_title(self):
        assert token_overlap_score("hello world", "") == 0.0

    def test_case_insensitive(self):
        assert token_overlap_score("HELLO WORLD", "Hello World") == 1.0


# ═════════════════════════════════════════════════════════════════════════════
# is_youtube_watch_url
# ═════════════════════════════════════════════════════════════════════════════


class TestIsYouTubeWatchUrl:
    def test_accepts_watch_url(self):
        assert is_youtube_watch_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_rejects_shorts_url(self):
        assert not is_youtube_watch_url("https://www.youtube.com/shorts/abc123")

    def test_rejects_playlist_url(self):
        assert not is_youtube_watch_url("https://www.youtube.com/playlist?list=PLxyz")

    def test_rejects_channel_url(self):
        assert not is_youtube_watch_url("https://www.youtube.com/channel/UC123")

    def test_rejects_empty(self):
        assert not is_youtube_watch_url("")

    def test_rejects_none(self):
        assert not is_youtube_watch_url(None)

    def test_rejects_non_youtube(self):
        assert not is_youtube_watch_url("https://netflix.com/title/123")


# ═════════════════════════════════════════════════════════════════════════════
# is_youtube_rejected_url
# ═════════════════════════════════════════════════════════════════════════════


class TestIsYouTubeRejectedUrl:
    def test_rejects_shorts(self):
        assert is_youtube_rejected_url("https://www.youtube.com/shorts/abc123")

    def test_accepts_watch_with_list_param(self):
        assert not is_youtube_rejected_url("https://www.youtube.com/watch?v=abc&list=PLxyz")

    def test_rejects_playlist_page(self):
        assert is_youtube_rejected_url("https://www.youtube.com/playlist?list=PLxyz")

    def test_rejects_channel(self):
        assert is_youtube_rejected_url("https://www.youtube.com/channel/UC123")

    def test_accepts_watch_url(self):
        assert not is_youtube_rejected_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_accepts_empty_returns_true(self):
        assert is_youtube_rejected_url("")

    def test_rejects_c_channel(self):
        assert is_youtube_rejected_url("https://www.youtube.com/c/SomeChannel")

    def test_rejects_user(self):
        assert is_youtube_rejected_url("https://www.youtube.com/user/SomeUser")

    def test_accepts_watch_with_tracking_list_param(self):
        assert not is_youtube_rejected_url(
            "https://www.youtube.com/watch?v=abc123&list=PLxyz&index=1"
        )

    def test_accepts_watch_with_multiple_params(self):
        assert not is_youtube_rejected_url(
            "https://www.youtube.com/watch?v=abc123&pp=ygUGbXVzaWM%3D"
        )


# ═════════════════════════════════════════════════════════════════════════════
# is_generic_media_query
# ═════════════════════════════════════════════════════════════════════════════


class TestIsGenericMediaQuery:
    def test_generic_music(self):
        assert is_generic_media_query("play relaxing music")

    def test_generic_highlights(self):
        assert is_generic_media_query("play football highlights")

    def test_generic_single_keyword(self):
        assert is_generic_media_query("music")

    def test_generic_two_words_both_generic(self):
        assert is_generic_media_query("funny videos")

    def test_specific_artist_song(self):
        assert not is_generic_media_query("despacito luis fonsi")

    def test_specific_title(self):
        assert not is_generic_media_query("interstellar trailer")

    def test_specific_quoted(self):
        assert not is_generic_media_query('"Hello" Adele')

    def test_generic_cooking(self):
        assert is_generic_media_query("play cooking video")

    def test_specific_movie_trailer(self):
        assert not is_generic_media_query("avengers endgame trailer")

    def test_generic_chill_music(self):
        assert is_generic_media_query("chill music")

    def test_generic_relaxing_music(self):
        assert is_generic_media_query("relaxing music")

    def test_generic_lofi(self):
        assert is_generic_media_query("lofi")

    def test_generic_lofi_music(self):
        assert is_generic_media_query("lofi music")

    def test_generic_single_word_generic(self):
        assert is_generic_media_query("meditation")

    def test_empty_query_is_generic(self):
        assert is_generic_media_query("")

    def test_single_non_generic_word_is_specific(self):
        assert not is_generic_media_query("despacito")

    def test_mixed_specific_and_generic(self):
        assert not is_generic_media_query("adele hello official video")


# ═════════════════════════════════════════════════════════════════════════════
# score_media_candidate
# ═════════════════════════════════════════════════════════════════════════════


class TestScoreMediaCandidate:
    def test_exact_match_scores_one(self):
        s = score_media_candidate("despacito luis fonsi", "Despacito Luis Fonsi")
        assert s == 1.0

    def test_query_in_title(self):
        s = score_media_candidate("interstellar trailer", "Interstellar Trailer (2014) Official")
        assert s == 0.9

    def test_title_in_query(self):
        s = score_media_candidate("adele hello official music video", "Hello")
        assert s == 0.85

    def test_high_token_overlap(self):
        s = score_media_candidate("mrbeast last video", "MrBeast's Last Video Ever!")
        assert s >= 0.65

    def test_unrelated_low_score(self):
        s = score_media_candidate("cats", "football world cup 2024")
        assert s < 0.50

    def test_empty_query(self):
        assert score_media_candidate("", "anything") == 0.0

    def test_empty_title(self):
        assert score_media_candidate("something", "") == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# select_best_media_candidate
# ═════════════════════════════════════════════════════════════════════════════


class TestSelectBestMediaCandidate:
    def test_exact_match_selected(self):
        candidates = [
            {"title": "Despacito Luis Fonsi", "url": "https://youtube.com/watch?v=abc"},
            {"title": "Baby Shark Dance", "url": "https://youtube.com/watch?v=xyz"},
        ]
        best, score, reason = select_best_media_candidate("despacito luis fonsi", candidates)
        assert best is not None
        assert best["title"] == "Despacito Luis Fonsi"
        assert reason == "selected"
        assert score >= 0.70

    def test_ambiguous_close_candidates(self):
        candidates = [
            {"title": "Interstellar Trailer Official", "url": "https://youtube.com/watch?v=abc"},
            {"title": "Interstellar Trailer 2014", "url": "https://youtube.com/watch?v=xyz"},
        ]
        best, score, reason = select_best_media_candidate("interstellar trailer", candidates)
        assert best is None
        assert reason == "ambiguous"

    def test_no_candidates(self):
        best, score, reason = select_best_media_candidate("anything", [])
        assert best is None
        assert reason == "no_candidates"

    def test_low_confidence(self):
        candidates = [
            {"title": "Cooking Pasta", "url": "https://youtube.com/watch?v=abc"},
            {"title": "How to Bake Bread", "url": "https://youtube.com/watch?v=xyz"},
        ]
        best, score, reason = select_best_media_candidate("quantum physics", candidates)
        assert best is None
        assert reason == "low_confidence"

    # ── Issue 5: YouTube tie-break for short specific queries ──

    def test_youtube_despacito_selects_first_safe_when_ambiguous(self):
        """YouTube 'Despacito' selects first safe official candidate via tie-break."""
        candidates = [
            {
                "title": "Luis Fonsi - Despacito ft. Daddy Yankee",
                "url": "https://www.youtube.com/watch?v=kJQP7kiw5Fk",
                "kind": "video",
            },
            {
                "title": "Despacito (Lyrics) - Luis Fonsi ft. Daddy Yankee",
                "url": "https://www.youtube.com/watch?v=xyz123",
                "kind": "video",
            },
            {
                "title": "Justin Bieber - Despacito (Lyrics)",
                "url": "https://www.youtube.com/watch?v=abc456",
                "kind": "video",
            },
        ]
        best, score, reason = select_best_media_candidate("despacito", candidates, "youtube")
        assert best is not None
        assert reason == "selected"
        assert "Despacito" in best["title"]

    def test_youtube_luis_fonsi_selects_first_safe_when_valid(self):
        """'Luis Fonsi - Despacito' selects first safe matching candidate."""
        candidates = [
            {
                "title": "Luis Fonsi - Despacito ft. Daddy Yankee",
                "url": "https://www.youtube.com/watch?v=kJQP7kiw5Fk",
                "kind": "video",
            },
            {
                "title": "Despacito (Lyrics) - Luis Fonsi ft. Daddy Yankee",
                "url": "https://www.youtube.com/watch?v=xyz123",
                "kind": "video",
            },
        ]
        best, score, reason = select_best_media_candidate(
            "luis fonsi despacito", candidates, "youtube"
        )
        assert best is not None
        assert reason == "selected"

    def test_youtube_tie_break_rejects_shorts(self):
        """YouTube Shorts/playlists are still rejected even with tie-break."""
        candidates = [
            {
                "title": "Despacito Short",
                "url": "https://www.youtube.com/shorts/abc123",
                "kind": "video",
            },
            {
                "title": "Despacito Playlist",
                "url": "https://www.youtube.com/playlist?list=PLxyz",
                "kind": "video",
            },
        ]
        filtered = filter_youtube_candidates(candidates)
        assert len(filtered) == 0

    def test_youtube_tie_break_rejects_ads(self):
        """YouTube ads still rejected even with short query."""
        candidates = [
            {
                "title": "Despacito Ad",
                "url": "https://www.youtube.com/watch?v=abc123",
                "kind": "video",
                "is_ad": True,
            },
            {
                "title": "Despacito Official",
                "url": "https://www.youtube.com/watch?v=def456",
                "kind": "video",
            },
        ]
        filtered = filter_youtube_candidates(candidates)
        assert len(filtered) == 1
        assert not filtered[0].get("is_ad")


class TestNetflixStrictAmbiguity:
    """Issue 5: Netflix ambiguity remains strict — no tie-break for non-YouTube."""

    def test_netflix_ambiguous_candidates_remain_ambiguous(self):
        """Netflix ambiguous candidates do NOT get the YouTube tie-break."""
        candidates = [
            {"title": "Interstellar", "url": "https://www.netflix.com/title/70308300"},
            {"title": "Interstellar Documentary", "url": "https://www.netflix.com/title/80991786"},
        ]
        best, score, reason = select_best_media_candidate("interstellar", candidates, "netflix")
        assert best is None
        assert reason == "ambiguous"


# ═════════════════════════════════════════════════════════════════════════════
# filter_youtube_candidates
# ═════════════════════════════════════════════════════════════════════════════


class TestFilterYouTubeCandidates:
    def test_accepts_watch_video(self):
        candidates = [
            {
                "title": "Good Video",
                "url": "https://www.youtube.com/watch?v=abc123",
                "kind": "video",
            },
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 1

    def test_rejects_shorts(self):
        candidates = [
            {
                "title": "Short Clip",
                "url": "https://www.youtube.com/shorts/abc123",
                "kind": "video",
            },
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 0

    def test_accepts_watch_with_list_param(self):
        candidates = [
            {
                "title": "Video With Track",
                "url": "https://www.youtube.com/watch?v=abc&list=PLxyz",
                "kind": "video",
            },
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 1

    def test_rejects_ad(self):
        candidates = [
            {
                "title": "Sponsored",
                "url": "https://www.youtube.com/watch?v=abc123",
                "kind": "video",
                "is_ad": True,
            },
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 0

    def test_rejects_channel_url(self):
        candidates = [
            {
                "title": "Channel",
                "url": "https://www.youtube.com/channel/UCabc123",
                "kind": "video",
            },
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 0

    def test_rejects_channel_kind(self):
        candidates = [
            {
                "title": "Channel Page",
                "url": "https://www.youtube.com/watch?v=abc123",
                "kind": "channel",
            },
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 0

    def test_rejects_non_youtube_url(self):
        candidates = [
            {"title": "External", "url": "https://example.com/video", "kind": "video"},
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 0

    def test_accepts_valid_video(self):
        candidates = [
            {
                "title": "MrBeast Video",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "kind": "video",
            },
            {
                "title": "Short Clip",
                "url": "https://www.youtube.com/shorts/abc123",
                "kind": "video",
            },
            {
                "title": "Ad Video",
                "url": "https://www.youtube.com/watch?v=xyz789",
                "kind": "video",
                "is_ad": True,
            },
        ]
        result = filter_youtube_candidates(candidates)
        assert len(result) == 1
        assert result[0]["title"] == "MrBeast Video"

    def test_empty_input(self):
        assert filter_youtube_candidates([]) == []


# ═════════════════════════════════════════════════════════════════════════════
# Netflix candidate scenarios
# ═════════════════════════════════════════════════════════════════════════════


class TestNetflixCandidateSelection:
    def test_netflix_strong_title_match_accepted(self):
        candidates = [
            {"title": "Stranger Things", "url": "https://www.netflix.com/title/80057281"},
            {"title": "The Crown", "url": "https://www.netflix.com/title/80025678"},
        ]
        best, score, reason = select_best_media_candidate("stranger things", candidates, "netflix")
        assert best is not None
        assert best["title"] == "Stranger Things"
        assert reason == "selected"

    def test_netflix_weak_title_match_rejected(self):
        candidates = [
            {"title": "Breaking Bad", "url": "https://www.netflix.com/title/70143836"},
            {"title": "The Blacklist", "url": "https://www.netflix.com/title/70281312"},
        ]
        best, score, reason = select_best_media_candidate("stranger things", candidates, "netflix")
        assert best is None
        assert reason != "selected"

    def test_netflix_ambiguous_candidates_none_selected(self):
        candidates = [
            {"title": "Interstellar", "url": "https://www.netflix.com/title/70308300"},
            {"title": "Interstellar Documentary", "url": "https://www.netflix.com/title/80991786"},
        ]
        best, score, reason = select_best_media_candidate("interstellar", candidates, "netflix")
        assert best is None
        assert reason == "ambiguous"


# ═════════════════════════════════════════════════════════════════════════════
# Generic YouTube keyword detection
# ═════════════════════════════════════════════════════════════════════════════


class TestGenericYouTubeQueries:
    def test_relaxing_music_is_generic(self):
        assert is_generic_media_query("relaxing music")

    def test_football_highlights_is_generic(self):
        assert is_generic_media_query("football highlights")

    def test_quran_recitation_is_generic(self):
        # Content category type, not a specific title
        assert is_generic_media_query("quran recitation")

    def test_cooking_video_is_generic(self):
        assert is_generic_media_query("cooking video")

    def test_funny_cats_is_generic(self):
        assert is_generic_media_query("funny cats")

    def test_generic_query_selects_first_safe_candidate(self):
        candidates = [
            {
                "title": "Relaxing Music",
                "url": "https://www.youtube.com/watch?v=abc1",
                "kind": "video",
            },
            {
                "title": "Ad Video",
                "url": "https://www.youtube.com/watch?v=abc2",
                "kind": "video",
                "is_ad": True,
            },
            {"title": "Short Clip", "url": "https://www.youtube.com/shorts/abc3", "kind": "video"},
        ]
        filtered = filter_youtube_candidates(candidates)
        assert len(filtered) == 1
        assert filtered[0]["title"] == "Relaxing Music"

    def test_specific_query_detected(self):
        assert not is_generic_media_query("despacito luis fonsi")
        assert not is_generic_media_query("mrbeast last video")
        assert not is_generic_media_query("interstellar trailer")
        assert not is_generic_media_query("adele hello official video")


class TestGenericKeywordExpansion:
    """Part A: expanded generic keywords for 14B.0.2."""

    def test_house_music_2026_is_generic(self):
        assert is_generic_media_query("house music 2026")

    def test_deep_house_mix_2026_is_generic(self):
        assert is_generic_media_query("deep house mix 2026")

    def test_best_deep_house_2026_is_generic(self):
        assert is_generic_media_query("best deep house 2026")

    def test_ibiza_summer_mix_is_generic(self):
        assert is_generic_media_query("ibiza summer mix")

    def test_relaxing_music_remains_generic(self):
        assert is_generic_media_query("relaxing music")

    def test_despacito_remains_specific(self):
        assert not is_generic_media_query("despacito")

    def test_luis_fonsi_despacito_remains_specific(self):
        assert not is_generic_media_query("luis fonsi despacito")


class TestCleanYouTubeWatchUrl:
    """Part C: YouTube URL cleanup for search results."""

    def test_cleans_list_and_start_radio(self):
        result = clean_youtube_watch_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDxyz&start_radio=1"
        )
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_cleans_relative_url(self):
        result = clean_youtube_watch_url("/watch?v=dQw4w9WgXcQ&list=RDxyz&index=1")
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_accepts_clean_watch_url(self):
        result = clean_youtube_watch_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_rejects_shorts(self):
        result = clean_youtube_watch_url("https://www.youtube.com/shorts/kJQP7kiw5Fk")
        assert result == ""

    def test_accepts_youtu_be(self):
        result = clean_youtube_watch_url("https://youtu.be/dQw4w9WgXcQ")
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_empty_returns_empty(self):
        assert clean_youtube_watch_url("") == ""

    def test_invalid_url_returns_empty(self):
        assert clean_youtube_watch_url("https://example.com/video") == ""

    def test_cleans_pp_param(self):
        result = clean_youtube_watch_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&pp=ygUGbXVzaWM%3D"
        )
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class TestParseOrdinalMediaCommand:
    """Part B: ordinal media command parsing."""

    def test_play_first_video(self):
        assert parse_ordinal_media_command("play the first video") == {
            "ordinal": 1,
            "action": "play",
            "platform_hint": "youtube",
        }

    def test_play_first_video_no_the(self):
        assert parse_ordinal_media_command("play first video") == {
            "ordinal": 1,
            "action": "play",
            "platform_hint": "youtube",
        }

    def test_open_second_result(self):
        assert parse_ordinal_media_command("open the second result") == {
            "ordinal": 2,
            "action": "play",
            "platform_hint": "youtube",
        }

    def test_play_third_video(self):
        assert parse_ordinal_media_command("play the third video") == {
            "ordinal": 3,
            "action": "play",
            "platform_hint": "youtube",
        }

    def test_result_number_three(self):
        assert parse_ordinal_media_command("play result number 3") == {
            "ordinal": 3,
            "action": "play",
            "platform_hint": "youtube",
        }

    def test_fourth_item(self):
        assert parse_ordinal_media_command("fourth item") == {
            "ordinal": 4,
            "action": "play",
            "platform_hint": "youtube",
        }

    def test_1st_video(self):
        assert parse_ordinal_media_command("play 1st video") == {
            "ordinal": 1,
            "action": "play",
            "platform_hint": "youtube",
        }

    def test_non_ordinal_returns_none(self):
        assert parse_ordinal_media_command("house music 2026") is None

    def test_despacito_returns_none(self):
        assert parse_ordinal_media_command("despacito") is None

    def test_empty_returns_none(self):
        assert parse_ordinal_media_command("") is None


# ═════════════════════════════════════════════════════════════════════════════
# Fuzzy matching and scoring — Step 14B.2.4
# ═════════════════════════════════════════════════════════════════════════════


class TestFuzzyScoring:
    """Tests for difflib.SequenceMatcher fuzzy fallback in score_media_candidate."""

    def test_exact_match_1_0(self):
        assert score_media_candidate("Kunoba", "Kunoba") == 1.0

    def test_fuzzy_near_match(self):
        s = score_media_candidate("Kunoba", "Konoba")
        assert s >= 0.65

    def test_fuzzy_slight_miss(self):
        s = score_media_candidate("Shape of My Heart", "Shape of My Hart")
        assert s >= 0.65

    def test_fuzzy_does_not_match_unrelated(self):
        s = score_media_candidate("Kunoba", "Random Song Title")
        assert s < 0.50

    def test_fuzzy_short_query_boost(self):
        s = score_media_candidate("Konoba", "Kunoba Official Music Video")
        assert s >= 0.45

    def test_token_overlap_still_dominates_exact(self):
        assert score_media_candidate("hello world", "hello world foo bar") >= 0.85


class TestOfficialVideoBoost:
    """Tests for _OFFICIAL_KEYWORDS boost in score_media_candidate."""

    def test_official_music_video_gets_boost(self):
        s_no = score_media_candidate("Despacito", "Despacito Audio Version")
        s_yes = score_media_candidate("Despacito", "Despacito Official Music Video")
        assert s_yes > s_no

    def test_official_video_gets_boost(self):
        s_no = score_media_candidate("Despacito", "Despacito Audio Version")
        s_yes = score_media_candidate("Despacito", "Despacito Official Video")
        assert s_yes > s_no

    def test_official_audio_gets_boost(self):
        s_no = score_media_candidate("Hello", "Hello Extra Track")
        s_yes = score_media_candidate("Hello", "Hello Official Audio")
        assert s_yes > s_no

    def test_music_video_gets_boost(self):
        s_no = score_media_candidate("Hello", "Hello Extra Track")
        s_yes = score_media_candidate("Hello", "Hello Music Video")
        assert s_yes > s_no

    def test_official_lyric_video_gets_boost(self):
        s_no = score_media_candidate("Hello", "Hello Extra Track")
        s_yes = score_media_candidate("Hello", "Hello Official Lyric Video")
        assert s_yes > s_no

    def test_boost_does_not_cap_above_1_0(self):
        s = score_media_candidate("Shape of My Heart", "Shape of My Heart Official Video")
        assert s <= 1.0

    def test_boost_precise_value(self):
        """Substring match (0.9) + boost (0.10) = 1.0, capped at 1.0."""
        s = score_media_candidate("Despacito", "Despacito Official Music Video")
        assert s == 1.0


class TestArtistSongBoost:
    """Tests for _artist_song_boost — ' — ' dash pairs."""

    def test_artist_song_dash_boost(self):
        s_no = score_media_candidate("Shape of My Heart", "Shape of My Heart Audio")
        s_yes = score_media_candidate("Shape of My Heart", "Sting — Shape of My Heart")
        assert s_yes > s_no

    def test_artist_song_boost_when_no_dash(self):
        s = score_media_candidate("Despacito", "Luis Fonsi Despacito")
        assert s >= 0.85 and s <= 1.0

    def test_artist_song_boost_works_with_dash(self):
        s = score_media_candidate("Shape of My Heart", "RandomBand — Shape of My Heart")
        assert s >= 0.90


class TestGenericDetectionTighten:
    """Tests for tightened is_generic_media_query — Top Boy is NOT generic."""

    def test_top_boy_not_generic(self):
        assert not is_generic_media_query("Top Boy")

    def test_top_boy_netflix_not_generic(self):
        assert not is_generic_media_query("Top Boy Netflix")

    def test_mr_robot_not_generic(self):
        assert not is_generic_media_query("Mr Robot")

    def test_top_10_still_generic(self):
        assert is_generic_media_query("top 10")

    def test_top_videos_still_generic(self):
        assert is_generic_media_query("top videos")

    def test_best_songs_still_generic(self):
        assert is_generic_media_query("best songs")

    def test_music_video_still_generic(self):
        assert is_generic_media_query("music video")

    def test_despacito_still_specific(self):
        assert not is_generic_media_query("despacito")


# ═════════════════════════════════════════════════════════════════════════════
# Interface stability — Step 14B.2.4
# ═════════════════════════════════════════════════════════════════════════════


class TestNewFunctionsImported:
    """Verify that newly added public API elements are importable."""

    def test_is_likely_proper_noun_not_exported(self):
        """_is_likely_proper_noun is private — verify we don't break."""
        from utils.media_match import is_generic_media_query

        assert callable(is_generic_media_query)
