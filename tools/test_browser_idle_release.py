"""PF-067 guard: Era lets go of the user's browser when idle.

Live 2026-11-20: Cloudflare's human check on Brave never finished while Era ran
(it held a CDP attachment to the user's own browser) and finished when Era closed.
"""

from __future__ import annotations

import asyncio
import time

from actions import browser_control as bc


def _session():
    sess = bc._BrowserSession.__new__(bc._BrowserSession)
    sess._inflight = 0
    sess._last_used = time.monotonic()
    return sess


def test_a_session_is_released_only_after_the_idle_period(monkeypatch):
    monkeypatch.setattr(bc, "IDLE_RELEASE_S", 45.0)
    sess = _session()
    assert not sess.idle_for_release()
    sess._last_used -= 60
    assert sess.idle_for_release()


def test_a_running_call_blocks_the_release(monkeypatch):
    monkeypatch.setattr(bc, "IDLE_RELEASE_S", 45.0)
    sess = _session()
    sess._last_used -= 600
    sess._inflight = 1
    assert not sess.idle_for_release()


def test_the_idle_loop_drops_the_attachment_once_and_only_when_attached(monkeypatch):
    monkeypatch.setattr(bc, "IDLE_RELEASE_S", 1.0)
    monkeypatch.setattr(bc, "_IDLE_CHECK_S", 0.01)
    sess = _session()
    sess._last_used -= 10
    sess._cdp_ready = True
    calls = []

    async def fake_restart(reason):
        calls.append(reason)
        sess._cdp_ready = False

    sess._restart_driver = fake_restart

    async def go():
        task = asyncio.ensure_future(sess._idle_release_loop())
        await asyncio.sleep(0.15)
        task.cancel()

    asyncio.run(go())
    assert calls == ["idle release"]


def test_run_marks_the_session_used_and_counts_calls():
    import inspect

    src = inspect.getsource(bc._BrowserSession.run)
    assert "_inflight += 1" in src and "_last_used = time.monotonic()" in src
