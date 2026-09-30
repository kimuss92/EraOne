# Fix: Persistent 1008 "Policy Violation" After Standby

## Problem

Every time Era One exits standby and attempts a new Gemini live session, the first
connection is rejected with WebSocket code 1008 ("policy violation"). A later retry
succeeds, but the first attempt always fails, adding latency and log noise.

## Root Cause

A race condition between token clearing and session teardown.

1. `enter_standby()` calls `self.session_manager.clear_token()` immediately, setting
   `current_resumption_token` to `None`.

2. The old Gemini session is still alive. During teardown (triggered by
   `_go_away_event.set()` inside `wait_and_mute()`), the `_receive_audio()` task
   processes a final `session_resumption_update` message from the server and calls
   `capture_token()`, which writes a **new** token back into
   `current_resumption_token`.

3. This newly-captured token is **stale** — it belongs to a session that is about to
   be closed. It is not valid for resumption.

4. When `_exit_standby()` fires `_wake_event.set()`, the `run()` loop unblocks and
   calls `_build_config()`. This reads `get_resumption_token()`, finds the stale
   token, attaches it to `LiveConnectConfig.session_resumption`, and sends it to
   Gemini.

5. Gemini rejects the connection because the token is invalid for a new session.

6. The exception handler in `run()` retries with exponential backoff. Because the
   stale token is still present, every retry hits the same 1008 error. Only after
   enough failures that `enter_standby()` is invoked again does the cycle reset.

**The `clear_token()` call in `enter_standby()` runs too early.** It clears the token
before the old session has fully torn down, leaving a window for `capture_token()`
to re-populate it.

## Fix

**Move the token-clearing call from `enter_standby()` to `_exit_standby()`, placing
it immediately before `_wake_event.set()`.**

By that point:
- The old TaskGroup has fully exited (the `async with` block in `run()` has ended).
- `_receive_audio()` is no longer running, so no `capture_token()` call can occur.
- The token clearing happens in the same synchronous block that unblocks `run()`,
  with no intervening await points where a stale token could be injected.

The `clear_token()` in `enter_standby()` is removed because it is both unnecessary
(the token will be cleared in `_exit_standby()` before the next connection) and
harmful (it creates the race window).

### Exact changes (file: `main.py`)

1. **Remove** line inside `enter_standby()`:
   ```python
   self.session_manager.clear_token()
   ```

2. **Add** the same call inside `_exit_standby()`, before `self._wake_event.set()`:
   ```python
   async def _exit_standby(self):
       if not self._standby_active:
           return
       print("[STANDBY] Exiting standby via F4 toggle.")
       self._stop_wake_word_listener()
       await asyncio.sleep(1.0)
       self._standby_active = False

       self.session_manager.clear_token()    # ← ADD HERE
       self._wake_event.set()
       self._reconnect_attempts = 0
       self._last_interaction_timestamp = time.time()
   ```

## Files affected

| File | Changes |
|---|---|
| `main.py` | 1 line removed in `enter_standby()`; 1 line added in `_exit_standby()` |
| `tools/test_token_clear_1008.py` | Add one integration-style test that simulates the full standby→wake cycle and verifies `get_resumption_token()` is `None` at the point `_build_config()` would read it |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Normal reconnections (not standby) could clear their valid token | Only `_exit_standby()` calls `clear_token()`. Normal reconnection paths in `run()` never touch the token. |
| `clear_token()` called twice (once in `enter_standby()`, once in `_exit_standby()`) | After the fix, it is called exactly once — in `_exit_standby()`. No double call. |
| Token needed for session resumption in non-standby cases | Token is preserved throughout normal operation. It is only cleared when exiting standby, which always requires a fresh session. |

## Verification

1. Run the existing test suite: `python -m pytest tools/test_token_clear_1008.py -v`
2. Add a test that:
   - Creates a `SessionManager`
   - Sets a token
   - Simulates the standby→wake cycle (clear → potential capture → re-clear)
   - Asserts `get_resumption_token()` returns `None`
3. Run end-to-end: enter standby, wait, exit standby — verify no 1008 error in logs
