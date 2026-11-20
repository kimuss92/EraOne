import asyncio
import json
import shutil
import subprocess

import pytest

from core.browser_media import (
    FRESH_S,
    READ_MEDIA,
    BrowserMediaCache,
    BrowserMediaObserver,
    listener_script,
    media_source,
)


class Page:
    def __init__(self, url="https://www.youtube.com/watch?v=private", status="playing"):
        self.url, self.status = url, status
        self.main_frame = object()
        self.closed = False
        self.callbacks = {}
        self.bindings = 0
        self.error = False

    def is_closed(self):
        return self.closed

    async def expose_binding(self, name, callback):
        self.bindings += 1
        self.binding = callback

    async def add_init_script(self, *, script):
        self.init_script = script

    def on(self, event, callback):
        self.callbacks[event] = callback

    async def evaluate(self, script):
        if self.error:
            raise RuntimeError("test read failure")
        if script == READ_MEDIA:
            return {"url": self.url, "status": self.status}


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=x", "youtube"),
        ("https://m.youtube.com/x", "youtube"),
        ("https://youtu.be/x", "youtube"),
        ("https://www.netflix.com/watch/x", "netflix"),
        ("https://youtube.com.attacker.test/x", None),
        ("ftp://youtube.com/x", None),
        ("file://netflix.com/x", None),
        ("https://attacker.test/?url=netflix.com", None),
        ("not-a-url", None),
    ],
)
def test_source_attribution_uses_hostname(url, expected):
    assert media_source(url) == expected


async def test_manual_pause_event_reads_dom_ignoring_claimed_payload():
    cache, page = BrowserMediaCache(), Page()
    observer = BrowserMediaObserver("brave", cache=cache)
    await observer.attach(page)
    assert cache.status("youtube") == "playing"
    page.status = "paused"
    await page.binding(
        {"page": page, "frame": page.main_frame}, {"status": "playing", "source": "netflix"}
    )
    assert cache.status("youtube") == "paused" and cache.status("netflix") is None
    assert "private" not in str(cache.snapshot()) and "https://" not in str(cache.snapshot())
    observer.close()


async def test_iframe_cannot_claim_top_level_playback():
    cache, page = BrowserMediaCache(), Page()
    observer = BrowserMediaObserver("brave", cache=cache)
    await observer.attach(page)
    page.status = "paused"
    await page.binding({"page": page, "frame": object()})
    assert cache.status("youtube") == "playing"
    observer.close()


async def test_navigation_away_clears_evidence_and_return_reuses_binding():
    cache, page = BrowserMediaCache(), Page()
    observer = BrowserMediaObserver("brave", cache=cache)
    await observer.attach(page)
    page.url = "https://example.test"
    observer.track(page)
    assert not cache.snapshot()["sessions"]
    page.url, page.status = "https://www.netflix.com/watch/x", "paused"
    await observer.attach(page)
    assert page.bindings == 1 and cache.status("netflix") == "paused"
    assert cache.status("youtube") is None
    observer.close()


async def test_navigation_during_read_cannot_publish_stale_playing():
    cache, page = BrowserMediaCache(), Page()
    observer = BrowserMediaObserver("brave", cache=cache)
    await observer.attach(page)
    old_url = page.url

    async def changed(script):
        page.url = "https://attacker.test"
        return {"url": old_url, "status": "playing"}

    page.evaluate = changed
    await observer.sample(page)
    assert cache.status("youtube") is None
    observer.close()


async def test_failed_dom_read_and_close_do_not_keep_playing_state():
    cache, page = BrowserMediaCache(), Page()
    observer = BrowserMediaObserver("brave", cache=cache)
    await observer.attach(page)
    page.error = True
    await observer.sample(page)
    assert cache.status("youtube") is None
    page.error = False
    await observer.sample(page)
    page.closed = True
    page.callbacks["close"]()
    assert not cache.snapshot()["sessions"]
    observer.close()


async def test_disconnect_rejects_inflight_read_and_clears_cache():
    cache, page = BrowserMediaCache(), Page()
    observer = BrowserMediaObserver("brave", cache=cache)
    await observer.attach(page)
    entered, released = asyncio.Event(), asyncio.Event()

    async def slow(script):
        entered.set()
        await released.wait()
        return {"url": page.url, "status": "playing"}

    page.evaluate = slow
    task = asyncio.create_task(observer.sample(page))
    await entered.wait()
    observer.close()
    released.set()
    await task
    assert not cache.snapshot()["sessions"]


def test_multiple_tabs_do_not_allow_false_paused_aggregate():
    cache = BrowserMediaCache()
    cache.update("one", "brave", "youtube", "paused")
    cache.update("two", "brave", "youtube", "playing")
    assert cache.status("youtube") == "playing"
    cache.update("two", "brave", "youtube", "unknown")
    assert cache.status("youtube") is None
    cache.update("two", "brave", "youtube", "ended")
    assert cache.status("youtube") == "paused"


def test_expiry_is_published_and_cannot_resurrect_intention(monkeypatch):
    from core import browser_media, media_state

    now, events = [100.0], []
    cache = BrowserMediaCache(clock=lambda: now[0], publisher=lambda v, **kw: events.append(v))
    monkeypatch.setattr(browser_media, "get_browser_media_cache", lambda: cache)
    monkeypatch.setattr(media_state, "MEDIA_STATE", {"youtube": {"status": "playing"}})
    cache.update("one", "brave", "youtube", "playing")
    assert media_state.is_playing("youtube")
    now[0] += FRESH_S + 1
    assert cache.status("youtube") is None
    cache.expire()
    assert events[-1]["sessions"][0]["health"] == "stale"
    assert not media_state.is_playing("youtube")


async def test_hud_mirrors_detached_browser_evidence():
    from app.hud.events import HudBus

    bus, events = HudBus(), []
    bus.attach(events.append)
    cache = BrowserMediaCache(publisher=bus.browser_media_observed)
    cache.update("one", "brave", "netflix", "paused")
    snapshot = bus.snapshot()
    snapshot["browserMediaObservation"]["sessions"].clear()
    assert bus.snapshot()["browserMediaObservation"]["sessions"]
    assert any(e.get("event", {}).get("type") == "system.browser.media.observed" for e in events)


def test_pause_readback_rejects_embedded_true_in_error(monkeypatch):
    from actions import browser_control
    from core import media_state

    class Session:
        def evaluate_background_media(self, *args):
            return args

        def run(self, job):
            return "Error: true was requested but pause failed"

    monkeypatch.setattr(browser_control, "get_browser_session", lambda *a, **kw: Session())
    assert not media_state._cdp_pause_video("youtube.com")


def test_youtube_failed_pause_does_not_change_cached_intention(monkeypatch):
    from actions import youtube_video as yt
    from core import media_state

    monkeypatch.setattr(yt, "_handle_playback_control", lambda *a: "YouTube pause failed read-back")
    monkeypatch.setattr(media_state, "MEDIA_STATE", {"youtube": {"status": "playing"}})
    result = yt.youtube_video({"action": "pause", "browser": "brave"})
    assert "failed" in result and media_state.get_state()["youtube"]["status"] == "playing"


def test_actual_js_listener_and_dom_readback_in_isolated_harness():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable for isolated JS check")
    harness = r"""
const vm = require('vm');
let callbacks = {}, pending = [], notifications = 0, mutations;
let element = {paused:false, ended:false, readyState:4, error:null};
global.window = global; window.top = window;
global.location = {href:'https://www.youtube.com/watch?v=isolated',hostname:'www.youtube.com'};
global.document = {
 querySelectorAll: () => [element],
 addEventListener: (event, cb) => { callbacks[event] = cb; },
 removeEventListener: event => { delete callbacks[event]; }
};
global.MutationObserver = class { constructor(cb) { mutations=cb; } observe() {} disconnect() {} };
global.setTimeout = cb => { pending.push(cb); return 1; };
global.clearTimeout = () => {};
global.setInterval = cb => 2;
global.clearInterval = () => {};
window.testBinding = () => { notifications++; return Promise.resolve(); };
vm.runInThisContext(LISTENER);
pending.splice(0).forEach(cb => cb());
const read = vm.runInThisContext('(' + READ + ')');
const playing = read().status;
element.paused=true; callbacks.pause(); mutations();
pending.splice(0).forEach(cb => cb());
const paused=read().status;
element.readyState=0;
const unknown=read().status;
console.log(JSON.stringify({playing,paused,unknown,notifications}));
""".replace("LISTENER", json.dumps(listener_script("testBinding"))).replace(
        "READ", json.dumps(READ_MEDIA)
    )
    result = subprocess.run(
        [node, "-e", harness], capture_output=True, text=True, timeout=5, check=True
    )
    assert json.loads(result.stdout) == {
        "playing": "playing",
        "paused": "paused",
        "unknown": "unknown",
        "notifications": 2,
    }
