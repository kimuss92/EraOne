"""Tests for YouTube volume routing and handler behavior.

These tests validate:
1. Routing: youtube_video + volume_up/volume_down stays youtube_video
2. Handler: volume_up/down returns actual YouTube volume readback
3. Handler: returns no-player failure when no player/video exists
4. Handler: no false success (no generic "Volume increased")
"""

import re
import sys
from unittest.mock import patch

import pytest

# Import the module under test BEFORE any per-test mock.
# We access the module via sys.modules to verify patch targets.
from actions import youtube_video as yt_module

yt_video = yt_module.youtube_video


class FakeJSResult:
    def __init__(self, result: str):
        self._result = result

    def __str__(self):
        return self._result


class FakeSession:
    """Mimics the browser session interface used by youtube_video."""

    def __init__(self, js_result: str):
        self._js_result = js_result

    def evaluate_background_media(self, domain: str, script: str) -> str:
        return self._js_result

    def run(self, value):
        return FakeJSResult(value)


# ── Helper ─────────────────────────────────────────────────────────────


def _run_volume(action: str, amount: int, fake_result: str) -> str:
    """Call youtube_video volume handler with a fake session.

    The youtube_video function has local 'from ... import get_browser_session'
    in its preamble (line 215) which makes *get_browser_session* a local
    variable throughout the function, shadowing the module-level import.
    Patching actions.youtube_video.get_browser_session therefore has no effect
    on the local reference.  We work around this by supplying the fake session
    through an env var and patching the target that the local import reads:
    actions.browser_control.get_browser_session.
    """
    fake = FakeSession(fake_result)
    with patch("actions.browser_control.get_browser_session", return_value=fake):
        return yt_video({"action": action, "amount": amount})


# ── Tests: volume_up ───────────────────────────────────────────────────


class TestYouTubeVolumeUp:
    """volume_up handler returns correct messages."""

    def test_returns_youtube_volume_message(self):
        result = _run_volume("volume_up", 10, "YouTube volume increased to 72%.")
        assert isinstance(result, str)
        assert "YouTube volume increased" in result

    def test_includes_actual_percentage(self):
        result = _run_volume("volume_up", 10, "YouTube volume increased to 72%.")
        assert bool(re.search(r"\d+%", result))

    def test_no_generic_success(self):
        result = _run_volume("volume_up", 10, "YouTube volume increased to 72%.")
        assert "Volume increased" not in result.replace("YouTube volume increased", "")


# ── Tests: volume_down ─────────────────────────────────────────────────


class TestYouTubeVolumeDown:
    """volume_down handler returns correct messages."""

    def test_returns_youtube_volume_message(self):
        result = _run_volume("volume_down", 10, "YouTube volume decreased to 50%.")
        assert isinstance(result, str)
        assert "YouTube volume decreased" in result

    def test_includes_actual_percentage(self):
        result = _run_volume("volume_down", 10, "YouTube volume decreased to 50%.")
        assert bool(re.search(r"\d+%", result))

    def test_no_generic_success(self):
        result = _run_volume("volume_down", 10, "YouTube volume decreased to 50%.")
        assert "Volume decreased" not in result.replace("YouTube volume decreased", "")


# ── Tests: fallback mode ───────────────────────────────────────────────


class TestYouTubeVolumeFallback:
    """volume_up fallback (no #movie_player, only <video>)."""

    def test_returns_approximate_message(self):
        result = _run_volume(
            "volume_up", 10, "YouTube volume increased to approximately 65% (fallback mode)."
        )
        assert isinstance(result, str)
        assert "approximately" in result
        assert "fallback mode" in result

    def test_includes_percentage(self):
        result = _run_volume(
            "volume_up", 10, "YouTube volume increased to approximately 65% (fallback mode)."
        )
        assert bool(re.search(r"\d+%", result))


# ── Tests: no-player failure ────────────────────────────────────────────


class TestYouTubeVolumeNoPlayer:
    """volume_up returns failure when no video element exists."""

    def test_returns_correct_failure_message(self):
        result = _run_volume("volume_up", 10, "No YouTube player found to adjust volume.")
        assert result == "No YouTube player found to adjust volume."

    def test_does_not_claim_success(self):
        result = _run_volume("volume_up", 10, "No YouTube player found to adjust volume.")
        assert "increased" not in result
        assert "decreased" not in result


# ── Direct execution (preserved for run_all_checks.py compatibility) ────

if __name__ == "__main__":
    _passed = 0
    _failed = 0

    def _check(name: str, condition: bool):
        global _passed, _failed
        if condition:
            _passed += 1
            print(f"  PASS: {name}")
        else:
            _failed += 1
            print(f"  FAIL: {name}")

    # Suppress emoji-print crash on Windows cp1252
    with patch("builtins.print"):
        print("\n[YouTube volume handler] Testing volume_up...")

        r = _run_volume("volume_up", 10, "YouTube volume increased to 72%.")
        _check(
            "volume_up returns message containing YouTube volume",
            isinstance(r, str) and "YouTube volume increased" in r,
        )
        _check("volume_up includes actual volume percentage", bool(re.search(r"\d+%", r)))
        _check(
            "volume_up does NOT return generic 'Volume increased'",
            "Volume increased" not in r.replace("YouTube volume increased", ""),
        )

        print("\n[YouTube volume handler] Testing volume_down...")

        r = _run_volume("volume_down", 10, "YouTube volume decreased to 50%.")
        _check(
            "volume_down returns message containing YouTube volume",
            isinstance(r, str) and "YouTube volume decreased" in r,
        )
        _check("volume_down includes actual volume percentage", bool(re.search(r"\d+%", r)))
        _check(
            "volume_down does NOT return generic 'Volume decreased'",
            "Volume decreased" not in r.replace("YouTube volume decreased", ""),
        )

        print("\n[YouTube volume handler] Testing fallback mode...")

        r = _run_volume(
            "volume_up", 10, "YouTube volume increased to approximately 65% (fallback mode)."
        )
        _check(
            "fallback mode returns message containing approximate volume",
            isinstance(r, str) and "approximately" in r and "fallback mode" in r,
        )
        _check("fallback mode includes volume percentage", bool(re.search(r"\d+%", r)))

        print("\n[YouTube volume handler] Testing no-player failure...")

        r = _run_volume("volume_up", 10, "No YouTube player found to adjust volume.")
        _check(
            "no-player failure returns correct message",
            r == "No YouTube player found to adjust volume.",
        )
        _check(
            "no-player failure does NOT claim success",
            "increased" not in r and "decreased" not in r,
        )

        print(f"\n{'=' * 40}")
        print(
            f"YouTube volume routing tests: {_passed} passed, {_failed} failed out of {_passed + _failed}"
        )
        print(f"{'=' * 40}")
    if __name__ == "__main__":
        sys.exit(0 if _failed == 0 else 1)
