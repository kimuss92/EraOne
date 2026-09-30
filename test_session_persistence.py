"""Phase-1 reliability: session save/restore + automatic expiry of stale tokens.

Proves that Era can resume its Gemini conversation after a restart, and that an
old (expired) resumption handle is dropped instead of wedging the reconnect loop.
No network — pure disk round-trip.
"""

import time

from core.session_manager import SessionManager


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "session_state.json"
    sm = SessionManager()
    sm.current_resumption_token = "handle-abc123"
    sm._token_saved_at = time.time()
    assert sm.save_to_disk(path) is True

    fresh = SessionManager()
    assert fresh.get_resumption_token() is None
    assert fresh.load_from_disk(path) is True
    assert fresh.get_resumption_token() == "handle-abc123"


def test_expired_token_is_dropped(tmp_path):
    path = tmp_path / "session_state.json"
    sm = SessionManager()
    sm.current_resumption_token = "handle-old"
    sm._token_saved_at = time.time() - 3600  # 1 hour old
    sm.save_to_disk(path)

    fresh = SessionManager()
    # Default max age is 15 min, so a 1-hour-old handle must NOT be restored.
    assert fresh.load_from_disk(path) is False
    assert fresh.get_resumption_token() is None


def test_fresh_token_within_window_is_restored(tmp_path):
    path = tmp_path / "session_state.json"
    sm = SessionManager()
    sm.current_resumption_token = "handle-recent"
    sm._token_saved_at = time.time() - 60  # 1 minute old
    sm.save_to_disk(path)

    fresh = SessionManager()
    assert fresh.load_from_disk(path, max_token_age_seconds=900) is True
    assert fresh.get_resumption_token() == "handle-recent"


def test_missing_file_is_safe(tmp_path):
    fresh = SessionManager()
    assert fresh.load_from_disk(tmp_path / "does_not_exist.json") is False
    assert fresh.get_resumption_token() is None


def test_corrupt_file_is_safe(tmp_path):
    path = tmp_path / "session_state.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    fresh = SessionManager()
    assert fresh.load_from_disk(path) is False  # never raises


def test_clear_token_resets_timestamp(tmp_path):
    sm = SessionManager()
    sm.current_resumption_token = "handle"
    sm._token_saved_at = time.time()
    sm.clear_token()
    assert sm.get_resumption_token() is None
    assert sm._token_saved_at is None
