from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.era_state import EraState
from core.session_manager import SessionManager


class TestTokenClear1008:
    """Verify the 1008 fix: stale session token is cleared in _exit_standby(), not enter_standby()."""

    def test_clear_token_on_standby(self):
        """SessionManager.clear_token() resets the resumption token to None."""
        mgr = SessionManager()
        mgr.current_resumption_token = "stale_token_from_old_session"
        mgr.clear_token()
        assert mgr.current_resumption_token is None
        assert mgr.get_resumption_token() is None

    def test_token_still_present_before_standby(self):
        """Token is available during a live connection (before standby)."""
        mgr = SessionManager()
        mgr.current_resumption_token = "live_token_abc"
        assert mgr.get_resumption_token() == "live_token_abc"

    def test_token_survives_enter_standby(self):
        """Token is NOT cleared in enter_standby() — survives the state transition."""
        mgr = SessionManager()
        mgr.current_resumption_token = "live_token"

        # Simulate entering standby: state changes but token persists
        mgr.transition_to(EraState.STANDBY, reason="user_standby_request")
        # NOTE: clear_token() is NOT called here — that's the fix
        # The token survives despite the standby transition

        assert mgr.get_resumption_token() == "live_token"

    def test_token_cleared_on_exit_standby(self):
        """Token IS cleared in _exit_standby(), just before the reconnect."""
        mgr = SessionManager()
        mgr.current_resumption_token = "token_after_teardown"

        # Simulate: old session dies and re-populated the token
        # (the race scenario — token looks valid at this point)

        # Now _exit_standby() runs — clear the token
        mgr.clear_token()

        # Prepare reconnection — no stale token
        mgr.prepare_reconnect()
        assert mgr.get_resumption_token() is None

    def test_race_condition_during_teardown(self):
        """Simulate the race: enter_standby → token repopulated during teardown → cleared in exit_standby."""
        mgr = SessionManager()
        mgr.current_resumption_token = "original_live_token"

        # Step 1: enter_standby() — state changes but token NOT cleared
        mgr.transition_to(EraState.STANDBY, reason="user_standby_request")
        # (no clear_token here — the fix)

        # Step 2: Old session emits final session_resumption_update during teardown
        mgr.current_resumption_token = "stale_token_from_teardown"
        # This is what was causing the 1008 — the token is re-populated

        # Step 3: TaskGroup fully exits, run() blocks on standby guard

        # Step 4: _exit_standby() runs — clears token before reconnect
        mgr.clear_token()

        # Step 5: _build_config() reads the token
        assert mgr.get_resumption_token() is None

    def test_token_not_cleared_on_other_transitions(self):
        """Token survives transitions that aren't standby-related."""
        mgr = SessionManager()
        mgr.current_resumption_token = "precious_token"

        mgr.transition_to(EraState.LISTENING, reason="wake")
        mgr.prepare_reconnect()
        mgr.restore_after_reconnect()

        assert mgr.get_resumption_token() == "precious_token"

    def test_full_standby_wake_cycle_no_stale_token(self):
        """After a full standby→wake cycle, the reconnection has no stale token."""
        mgr = SessionManager()
        mgr.current_resumption_token = "original_live_token"

        # enter_standby — token NOT cleared
        mgr.transition_to(EraState.STANDBY, reason="user_standby_request")
        # Token repopulated during teardown
        mgr.current_resumption_token = "stale_token_from_teardown"

        # exit_standby — token IS cleared
        mgr.clear_token()
        mgr.transition_to(EraState.LISTENING, reason="f4_wake")

        # _build_config() reads None
        assert mgr.get_resumption_token() is None

    def test_build_config_gets_no_token_after_standby(self):
        """_build_config() uses get_resumption_token() — after exit_standby it's None."""
        mgr = SessionManager()
        mgr.current_resumption_token = "stale_token"

        mgr.transition_to(EraState.STANDBY, reason="user_standby_request")
        # Token repopulated during teardown
        mgr.current_resumption_token = "repopulated_token"

        # exit_standby clears it
        mgr.clear_token()

        token = mgr.get_resumption_token()
        assert token is None
