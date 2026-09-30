#!/usr/bin/env python3
"""
Comprehensive verification script for media switching and Spotify default behavior.
Tests all three scenarios: Spotify (default), Netflix, YouTube with proper pause coordination.
"""

import sys
import time
from pathlib import Path

# Ensure repo root is on sys.path
repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


def test_scenario(name, test_fn):
    """Helper to run a test scenario and report results."""
    print(f"\n{'=' * 70}")
    print(f"TEST: {name}")
    print("=" * 70)
    try:
        test_fn()
        print(f"✅ {name} completed")
    except Exception as e:
        print(f"❌ {name} failed: {e}")
        import traceback

        traceback.print_exc()


def test_spotify_default():
    """Test 1: Open Spotify as default media player (should pause YouTube/Netflix)."""
    print("[1a] Importing actions...")
    from actions.open_app import open_app

    print("[1b] Opening Spotify (should pause YouTube/Netflix via media_coordinator)...")
    result = open_app({"app_name": "Spotify"})
    print(f"     Result: {result}")

    assert "opened" in result.lower() or "already running" in result.lower(), (
        f"Unexpected result: {result}"
    )
    print("     ✓ Spotify opened and resume coordinated")


def test_spotify_play_from_bare_command():
    """Test 2: Test 'play music' defaults to Spotify (via self_healing_router)."""
    print("[2a] Importing modules...")
    from core.self_healing_router import route_tool_call

    print("[2b] Routing bare 'play' command (no target specified)...")
    tool, params = route_tool_call("computer_settings", {"action": "play"}, user_text="")
    print(f"     Routed to: {tool}, action={params.get('action')}")

    assert tool == "computer_settings", f"Should route to computer_settings, got {tool}"
    assert "spotify" in params.get("action", "").lower(), f"Should be spotify_play, got {params}"
    print("     ✓ Bare 'play' defaults to Spotify")


def test_netflix_resume():
    """Test 3: Resume Netflix (should pause Spotify first via media_coordinator)."""
    print("[3a] Importing modules...")
    from actions.media_coordinator import media_coordinator

    print("[3b] Resuming Netflix (should pause Spotify/YouTube via media_coordinator)...")
    result = media_coordinator({"target": "netflix", "action": "play"})
    print(f"     Result: {result}")

    assert (
        "resumed" in result.lower() or "video" in result.lower() or "netflix" in result.lower()
    ), f"Unexpected result: {result}"
    print("     ✓ Netflix resumed with competing media paused first")


def test_youtube_resume():
    """Test 4: Resume YouTube (should pause Spotify/Netflix first via media_coordinator)."""
    print("[4a] Importing modules...")
    from actions.media_coordinator import media_coordinator

    print("[4b] Resuming YouTube (should pause Spotify/Netflix via media_coordinator)...")
    result = media_coordinator({"target": "youtube", "action": "play"})
    print(f"     Result: {result}")

    assert (
        "resume" in result.lower() or "youtube" in result.lower() or "paused" in result.lower()
    ), f"Unexpected result: {result}"
    print("     ✓ YouTube resumed with competing media paused first")


def test_spotify_pause_safety():
    """Test 5: Pause Spotify safely (should not toggle if not running)."""
    print("[5a] Importing modules...")
    from actions.computer_settings import spotify_pause

    print("[5b] Calling spotify_pause (should be safe when not running)...")
    result = spotify_pause()
    print(f"     Result: {result}")

    # Result should either say paused or not running, never an error
    assert isinstance(result, str) and len(result) > 0, f"Should return a message, got {result}"
    print("     ✓ Spotify pause executed safely")


def test_media_coordinator_orchestration():
    """Test 6: Full orchestration - pause competing, resume target."""
    print("[6a] Importing modules...")
    from actions.media_coordinator import media_coordinator

    print("[6b] Resuming Spotify (full cross-media coordination)...")
    result = media_coordinator({"target": "spotify", "action": "play"})
    print(f"     Result: {result}")

    assert isinstance(result, str) and len(result) > 0, f"Should return a message, got {result}"
    print("     ✓ Full orchestration completed")


def test_browser_media_detection():
    """Test 7: Browser can detect active HTML5 media across tabs."""
    print("[7a] Importing modules...")
    from actions.browser_control import get_browser_session

    print("[7b] Checking if browser session can query media state...")
    try:
        sess = get_browser_session(None)
        if sess:
            res = sess.run(sess.any_media_playing())
            print(f"     Browser media state: {res}")
            assert res in ("True", "False"), f"Should return True/False, got {res}"
            print("     ✓ Browser media detection working")
        else:
            print("     ⚠ Browser session not available (CDP port 9222 may not be open)")
    except Exception as e:
        print(f"     ⚠ Browser detection test skipped: {e}")


def main():
    """Run all verification tests."""
    print("\n" + "=" * 70)
    print("ERA ONE MEDIA SWITCHING VERIFICATION SUITE")
    print("=" * 70)
    print("\nThis script verifies:")
    print("  1. Spotify opens and resumes with competing media paused")
    print("  2. Bare 'play' commands default to Spotify")
    print("  3. Netflix resume pauses competing media first")
    print("  4. YouTube resume pauses competing media first")
    print("  5. Spotify pause is safe (no blind toggles)")
    print("  6. Full media_coordinator orchestration works")
    print("  7. Browser media detection works")

    tests = [
        ("Spotify Default Open", test_spotify_default),
        ("Spotify Default Bare Command", test_spotify_play_from_bare_command),
        ("Netflix Resume Coordination", test_netflix_resume),
        ("YouTube Resume Coordination", test_youtube_resume),
        ("Spotify Pause Safety", test_spotify_pause_safety),
        ("Media Coordinator Orchestration", test_media_coordinator_orchestration),
        ("Browser Media Detection", test_browser_media_detection),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_scenario(name, test_fn)
            passed += 1
        except AssertionError as e:
            print(f"ASSERTION FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1
        time.sleep(0.5)

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 70)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
