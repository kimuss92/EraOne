"""Recovery from a dead Playwright driver ("Connection closed while reading from the driver").

Uses a REAL Playwright driver process (no browser needed) and really kills it,
because the bug was that Playwright's own state keeps reporting a healthy link
after the driver is gone — a mock would just encode our assumptions.
"""

import asyncio

import pytest

from actions.browser_control import _BrowserSession
from actions.browser_driver_health import driver_alive, is_driver_death, rebuild_call


class _Session(_BrowserSession):
    """Session with a few probe methods; no browser is ever attached."""

    def __init__(self):
        super().__init__("brave")
        self.calls = 0

    async def echo(self, value, suffix: str = "") -> str:
        self.calls += 1
        return f"{value}{suffix}"

    async def _talk_to_driver(self):
        # Any RPC through the driver; port 9 has no browser, so a LIVE driver
        # answers with a normal connect error, a DEAD one with driver death.
        await self._pw.chromium.connect_over_cdp("http://127.0.0.1:9", timeout=1000)

    async def die_then_raise(self) -> str:
        self.calls += 1
        if self.calls == 1:
            _kill(self)
            await asyncio.sleep(0.5)
            await self._talk_to_driver()
        return "ok after retry"

    async def die_then_swallow(self) -> str:
        self.calls += 1
        if self.calls == 1:
            _kill(self)
            await asyncio.sleep(0.5)
            try:
                await self._talk_to_driver()
            except Exception as e:
                return f"DOM Media Injection Failure: {e}"
        return "ok after retry"


def _kill(sess) -> None:
    sess._pw._impl_obj._connection._transport._proc.kill()


def _wait_dead(sess, seconds: float = 5.0) -> None:
    async def _poll():
        for _ in range(int(seconds / 0.05)):
            if not driver_alive(sess._pw):
                return
            await asyncio.sleep(0.05)

    asyncio.run_coroutine_threadsafe(_poll(), sess._loop).result(seconds + 1)


@pytest.fixture
def sess():
    s = _Session()
    s.start()
    assert s._pw is not None, "Playwright driver did not start"
    yield s
    try:
        s.close()
    except Exception:
        pass


def test_driver_death_is_detected(sess):
    assert driver_alive(sess._pw)
    _kill(sess)
    _wait_dead(sess)
    assert not driver_alive(sess._pw)


def test_dead_driver_fails_connection_alive_even_if_browser_says_connected(sess):
    class _FakeBrowser:
        contexts = [object()]

        def is_connected(self):
            return True  # exactly what Playwright reports after driver death

    sess._browser_obj = _FakeBrowser()
    sess._context = object()
    assert sess._connection_alive()
    _kill(sess)
    _wait_dead(sess)
    assert not sess._connection_alive()


def test_preflight_replaces_driver_that_died_while_idle(sess):
    old = sess._pw
    _kill(sess)
    _wait_dead(sess)
    assert sess.run(sess.echo("netflix", suffix="!")) == "netflix!"
    assert sess._pw is not old
    assert driver_alive(sess._pw)


def test_mid_call_death_is_retried_once(sess):
    old = sess._pw
    assert sess.run(sess.die_then_raise()) == "ok after retry"
    assert sess.calls == 2
    assert sess._pw is not old and driver_alive(sess._pw)


def test_mid_call_death_swallowed_into_error_string_is_retried(sess):
    assert sess.run(sess.die_then_swallow()) == "ok after retry"
    assert sess.calls == 2


def test_live_driver_errors_are_not_retried(sess):
    old = sess._pw
    with pytest.raises(Exception) as err:
        sess.run(sess._talk_to_driver())
    assert not is_driver_death(err.value)
    assert sess._pw is old


def test_rebuild_call_only_for_unstarted_own_methods(sess):
    coro = sess.echo("a", suffix="b")
    factory = rebuild_call(sess, coro)
    assert factory is not None
    coro.close()
    fresh = factory()
    assert asyncio.run(fresh) == "ab"

    other = _Session()  # never started: only used as a foreign owner
    foreign = other.echo("x")
    assert rebuild_call(sess, foreign) is None
    foreign.close()

    async def free_fn():
        return 1

    free = free_fn()
    assert rebuild_call(sess, free) is None
    free.close()


def test_death_marker_matching():
    assert is_driver_death(Exception("Page.close: Connection closed while reading from the driver"))
    assert is_driver_death(
        "DOM Media Injection Failure: Connection closed while reading from the driver"
    )
    assert not is_driver_death("Target page, context or browser has been closed")
