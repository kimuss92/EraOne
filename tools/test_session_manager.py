import pytest

from core.era_state import EraState
from core.session_manager import SessionManager


@pytest.fixture
def mgr():
    return SessionManager()


class FakeResponse:
    def __init__(
        self,
        has_update=False,
        has_go_away=False,
        resumable=False,
        new_handle=None,
        consumed_index=None,
        time_left=None,
    ):
        self.session_resumption_update = None
        self.go_away = None
        if has_update:
            self.session_resumption_update = _FakeSessionUpdate(
                resumable=resumable,
                new_handle=new_handle,
                consumed_index=consumed_index,
            )
        if has_go_away:
            self.go_away = _FakeGoAway(time_left=time_left)


class _FakeSessionUpdate:
    def __init__(self, resumable=False, new_handle=None, consumed_index=None):
        self.resumable = resumable
        self.new_handle = new_handle
        self.last_consumed_client_message_index = consumed_index


class _FakeGoAway:
    def __init__(self, time_left=None):
        self.time_left = time_left


class TestTokenCapture:
    def test_capture_valid_token(self, mgr):
        resp = FakeResponse(has_update=True, resumable=True, new_handle="abc123")
        result = mgr.capture_token(resp)
        assert result is True
        assert mgr.current_resumption_token == "abc123"

    def test_ignore_invalid_token(self, mgr):
        resp = FakeResponse(has_update=True, resumable=False, new_handle=None)
        result = mgr.capture_token(resp)
        assert result is False
        assert mgr.current_resumption_token is None

    def test_get_resumption_token(self, mgr):
        mgr.current_resumption_token = "tok_xyz"
        assert mgr.get_resumption_token() == "tok_xyz"

    def test_clear_token(self, mgr):
        mgr.current_resumption_token = "tok_xyz"
        mgr.clear_token()
        assert mgr.current_resumption_token is None

    def test_extract_consumed_index(self, mgr):
        resp = FakeResponse(has_update=True, resumable=True, new_handle="tok", consumed_index=42)
        idx = mgr.extract_consumed_index(resp)
        assert idx == 42

    def test_extract_consumed_index_none(self, mgr):
        resp = FakeResponse(has_update=True, resumable=True, new_handle="tok")
        idx = mgr.extract_consumed_index(resp)
        assert idx is None


class TestGoAway:
    def test_handle_go_away(self, mgr):
        resp = FakeResponse(has_go_away=True, time_left="30s")
        result = mgr.handle_go_away(resp)
        assert result is True
        assert mgr.last_go_away_timestamp is not None

    def test_handle_go_away_no_signal(self, mgr):
        resp = FakeResponse()
        result = mgr.handle_go_away(resp)
        assert result is False


class TestStateTransitions:
    def test_initial_state(self, mgr):
        assert mgr.current_state == EraState.OFFLINE

    def test_transition_to(self, mgr):
        mgr.transition_to(EraState.LISTENING, reason="mic_ready")
        assert mgr.current_state == EraState.LISTENING
        assert mgr.previous_state == EraState.OFFLINE

    def test_transition_to_reconnecting_saves_last(self, mgr):
        mgr.transition_to(EraState.LISTENING, reason="started")
        mgr.transition_to(EraState.RECONNECTING, reason="drop")
        assert mgr.last_non_reconnecting_state == EraState.LISTENING

    def test_state_history_bounded(self, mgr):
        for i in range(60):
            mgr.transition_to(EraState.LISTENING, reason=f"step_{i}")
        assert len(mgr.get_state_history()) <= 50

    def test_get_state_snapshot(self, mgr):
        mgr.transition_to(EraState.LISTENING)
        snap = mgr.get_state_snapshot()
        assert snap["current_state"] == EraState.LISTENING
        assert "is_muted" in snap
        assert "wake_word_active" in snap
        assert "is_reconnecting" in snap


class TestGreetingPolicy:
    def test_greeting_when_processing(self, mgr):
        mgr.is_resumed_session = True
        mgr.transition_to(EraState.PROCESSING, reason="user_input")
        mgr.transition_to(EraState.RECONNECTING, reason="drop")
        mgr.restore_after_reconnect()
        assert mgr.should_greet_on_reconnect() is True
        assert mgr.get_greeting() == "Back online, sir."

    def test_greeting_when_idle(self, mgr):
        mgr.is_resumed_session = True
        mgr.transition_to(EraState.ONLINE_IDLE, reason="ready")
        mgr.transition_to(EraState.RECONNECTING, reason="drop")
        mgr.restore_after_reconnect()
        assert mgr.should_greet_on_reconnect() is False
        assert mgr.get_greeting() is None

    def test_greeting_when_standby(self, mgr):
        mgr.is_resumed_session = True
        mgr.transition_to(EraState.STANDBY, reason="user_request")
        mgr.transition_to(EraState.RECONNECTING, reason="drop")
        mgr.restore_after_reconnect()
        assert mgr.should_greet_on_reconnect() is False
        assert mgr.get_greeting() is None

    def test_no_greeting_on_fresh_session(self, mgr):
        mgr.is_resumed_session = False
        assert mgr.get_greeting() is None

    def test_speaking_not_restored_blindly(self, mgr):
        mgr.transition_to(EraState.SPEAKING, reason="responding")
        mgr.transition_to(EraState.RECONNECTING, reason="drop")
        mgr.restore_after_reconnect()
        assert mgr.current_state != EraState.SPEAKING
        assert mgr.current_state == EraState.LISTENING

    def test_last_non_reconnecting_state_saved(self, mgr):
        mgr.transition_to(EraState.LISTENING, reason="started")
        mgr.transition_to(EraState.RECONNECTING, reason="drop")
        assert mgr.last_non_reconnecting_state == EraState.LISTENING


class TestMessageBuffer:
    def test_add_message(self, mgr):
        idx = mgr.register_outgoing_message(b"test", "audio/pcm")
        assert idx == 0

    def test_message_index_increments(self, mgr):
        idx1 = mgr.register_outgoing_message(b"a", "audio/pcm")
        idx2 = mgr.register_outgoing_message(b"b", "audio/pcm")
        assert idx1 == 0
        assert idx2 == 1

    def test_prune_buffer(self, mgr):
        mgr.register_outgoing_message(b"a", "audio/pcm")
        mgr.register_outgoing_message(b"b", "audio/pcm")
        mgr._last_consumed_index = 0
        mgr._prune_buffer()
        msgs = mgr.get_unacknowledged_messages()
        assert len(msgs) == 1
        assert msgs[0].index == 1

    def test_get_unacknowledged(self, mgr):
        mgr.register_outgoing_message(b"a", "audio/pcm")
        mgr.register_outgoing_message(b"b", "audio/pcm")
        msgs = mgr.get_unacknowledged_messages()
        assert len(msgs) == 2

    def test_clear_message_buffer(self, mgr):
        mgr.register_outgoing_message(b"a", "audio/pcm")
        mgr.clear_message_buffer()
        assert len(mgr.get_unacknowledged_messages()) == 0

    def test_stale_audio_not_replayed_after_reconnect(self, mgr):
        mgr.register_outgoing_message(b"stale", "audio/pcm")
        mgr.clear_message_buffer()
        mgr.register_outgoing_message(b"fresh", "audio/pcm")
        msgs = mgr.get_unacknowledged_messages()
        assert len(msgs) == 1
        assert msgs[0].data == b"fresh"


class TestReconnect:
    def test_prepare_reconnect(self, mgr):
        mgr.transition_to(EraState.LISTENING, reason="started")
        mgr.prepare_reconnect()
        assert mgr.current_state == EraState.RECONNECTING

    def test_restore_after_reconnect(self, mgr):
        mgr.transition_to(EraState.LISTENING, reason="started")
        mgr.prepare_reconnect()
        mgr.restore_after_reconnect()
        assert mgr.current_state == EraState.LISTENING
        assert mgr.is_resumed_session is True
        assert mgr.last_reconnect_timestamp is not None


class TestDebugInfo:
    def test_get_debug_info(self, mgr):
        info = mgr.get_debug_info()
        assert "has_token" in info
        assert "is_resumed" in info
        assert "current_state" in info
        assert "buffer_size" in info
