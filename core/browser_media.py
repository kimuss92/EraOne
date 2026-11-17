"""Narrow, read-only playback evidence from already-connected media tabs."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from copy import deepcopy
from urllib.parse import urlparse

FRESH_S = 35.0


def media_source(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        return None
    for source, domains in (
        ("youtube", ("youtube.com", "youtu.be")),
        ("netflix", ("netflix.com",)),
    ):
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return source
    return None


READ_MEDIA = """() => {
  const media = Array.from(document.querySelectorAll('video,audio'));
  let status = 'unknown';
  if (!media.length) status = 'no_player';
  else if (media.some(m => !m.error && !m.paused && !m.ended && m.readyState >= 2)) status = 'playing';
  else if (media.every(m => !m.error && m.ended)) status = 'ended';
  else if (media.every(m => !m.error && m.paused && m.readyState >= 1)) status = 'paused';
  const current = media.find(m => !m.paused && !m.ended) || media[0];
  return { url: location.href, status, title: document.title,
           position_ms: current ? Math.round(current.currentTime * 1000) : null };
}"""


def listener_script(binding):
    return """(() => {
      if (window.top !== window) return;
      const host = location.hostname.toLowerCase();
      if (!['youtube.com','youtu.be','netflix.com'].some(d => host === d || host.endsWith('.' + d))) return;
      const key = BINDING;
      if (window[key + '_installed']) return;
      window[key + '_installed'] = true;
      let timer = null;
      let interval = null;
      let mutation = null;
      const events = ['play','pause','ended','loadedmetadata','canplay','waiting','emptied','error'];
      const dispose = () => {
        for (const event of events) document.removeEventListener(event, notify, true);
        if (mutation) mutation.disconnect();
        clearInterval(interval); clearTimeout(timer);
        delete window[key + '_installed'];
      };
      const notify = () => {
        if (timer !== null) return;
        timer = setTimeout(() => {
          timer = null;
          if (typeof window[key] === 'function') Promise.resolve(window[key]()).catch(dispose);
          else dispose();
        }, 100);
      };
      for (const event of events)
        document.addEventListener(event, notify, true);
      mutation = new MutationObserver(notify);
      mutation.observe(document, { childList: true, subtree: true });
      interval = setInterval(notify, 2000);
      notify();
    })()""".replace("BINDING", json.dumps(binding))


class BrowserMediaCache:
    def __init__(self, *, publisher=None, clock=time.time):
        self.clock, self.publisher = clock, publisher
        self.lock = threading.RLock()
        self.rows = {}
        self.seen = set()
        self.revision = 0

    def update(self, ident, browser, source, status):
        if source not in ("youtube", "netflix") or status not in (
            "playing",
            "paused",
            "ended",
            "unknown",
            "no_player",
        ):
            raise ValueError("invalid browser media evidence")
        with self.lock:
            previous = self.rows.get(ident)
            row = {
                "id": ident,
                "browser": browser,
                "source": source,
                "status": status,
                "observedAt": self.clock(),
                "health": "ready",
            }
            changed = previous is None or any(
                previous[k] != row[k] for k in ("status", "source", "health")
            )
            self.rows[ident] = row
            self.seen.add(source)
            self.revision += 1
        self._publish(changed)

    def remove(self, ident):
        with self.lock:
            if self.rows.pop(ident, None) is None:
                return
            self.revision += 1
        self._publish(True)

    def snapshot(self):
        with self.lock:
            rows = deepcopy(list(self.rows.values()))
            revision = self.revision
        now = self.clock()
        for row in rows:
            if not 0 <= now - row["observedAt"] <= FRESH_S:
                row["health"], row["status"] = "stale", "unknown"
        return {"revision": revision, "sessions": rows}

    def expire(self):
        now, changed = self.clock(), False
        with self.lock:
            for row in self.rows.values():
                if row["health"] == "ready" and not 0 <= now - row["observedAt"] <= FRESH_S:
                    row["health"], row["status"] = "stale", "unknown"
                    changed = True
            if changed:
                self.revision += 1
        if changed:
            self._publish(True)

    def status(self, source):
        rows = [r for r in self.snapshot()["sessions"] if r["source"] == source]
        if any(r["health"] == "ready" and r["status"] == "playing" for r in rows):
            return "playing"
        if rows and all(
            r["health"] == "ready" and r["status"] in ("paused", "ended") for r in rows
        ):
            return "paused"
        return None

    def has_observed(self, source):
        with self.lock:
            return source in self.seen

    def _publish(self, changed):
        if self.publisher:
            try:
                self.publisher(self.snapshot(), changed=changed)
            except Exception as exc:
                _log("publish_failed", exc)


def _log(event, exc):
    from core.logger import log_event, main_logger

    log_event(main_logger, f"awareness.browser.{event}", "WARNING", error_type=type(exc).__name__)


_cache = None
_cache_lock = threading.Lock()


def get_browser_media_cache():
    global _cache
    with _cache_lock:
        if _cache is None:
            from app.hud.events import hud

            _cache = BrowserMediaCache(publisher=hud.browser_media_observed)
        return _cache


class BrowserMediaObserver:
    def __init__(self, browser, *, cache=None):
        self.browser, self.cache = browser, cache or get_browser_media_cache()
        self.binding = "__era_media_" + uuid.uuid4().hex
        self.pages, self.tasks = {}, {}
        self.bound, self.registered = set(), set()
        self.closed = False
        self.sampling = set()

    def track(self, page):
        if self.closed or page.is_closed():
            return
        if not media_source(page.url):
            self.remove(page)
            return
        if page in self.tasks:
            return
        task = asyncio.create_task(self.attach(page))
        self.tasks[page] = task
        task.add_done_callback(lambda _: self.tasks.pop(page, None))

    async def attach(self, page):
        if page not in self.pages:
            self.pages[page] = uuid.uuid4().hex
        if page not in self.registered:
            try:
                if page not in self.bound:
                    await asyncio.wait_for(
                        page.expose_binding(self.binding, self._event), timeout=2
                    )
                    self.bound.add(page)
                await asyncio.wait_for(
                    page.add_init_script(script=listener_script(self.binding)), timeout=2
                )
                page.on("close", lambda *_: self.remove(page))
                page.on("framenavigated", lambda *_: self.track(page))
                self.registered.add(page)
            except Exception as exc:
                self.remove(page)
                _log("subscribe_failed", exc)
                return
        try:
            await asyncio.wait_for(page.evaluate(listener_script(self.binding)), timeout=2)
            await self.sample(page)
        except Exception as exc:
            self.remove(page)
            _log("read_failed", exc)

    async def _event(self, source, *_ignored_payload):
        if self.closed:
            raise RuntimeError(
                "media observer disconnected"
            )  # lets our page listener dispose itself
        if not isinstance(source, dict):
            return
        page = source.get("page")
        if self.closed or page not in self.pages or source.get("frame") != page.main_frame:
            return
        await self.sample(page)

    async def sample(self, page):
        ident = self.pages.get(page)
        if ident is None or self.closed or page.is_closed() or page in self.sampling:
            return
        expected = page.url
        source = media_source(expected)
        if source is None:
            self.remove(page)
            return
        self.sampling.add(page)
        sample_started_at = time.time()
        try:
            data = await asyncio.wait_for(page.evaluate(READ_MEDIA), timeout=2)
            if (
                self.closed
                or self.pages.get(page) != ident
                or page.is_closed()
                or page.url != expected
                or not isinstance(data, dict)
                or data.get("url") != expected
            ):
                if self.pages.get(page) == ident:
                    self.cache.remove(ident)
                return
            self.cache.update(ident, self.browser, source, data.get("status"))
            from core.media_observation import publish

            publish(source, {"source": "browser_dom", "sample_started_at": sample_started_at,
                             "observed_at": time.time(), "url": expected, "title": data.get("title", ""),
                             "playback": data.get("status", "unknown"), "position_ms": data.get("position_ms")})
        except Exception as exc:
            self.cache.remove(ident)
            _log("read_failed", exc)
        finally:
            self.sampling.discard(page)

    def remove(self, page):
        ident = self.pages.pop(page, None)
        if ident is not None:
            self.cache.remove(ident)
        if page.is_closed():
            self.bound.discard(page)
            self.registered.discard(page)

    def close(self):
        self.closed = True
        for page in list(self.pages):
            self.remove(page)
        for task in list(self.tasks.values()):
            task.cancel()
        self.bound.clear()
        self.registered.clear()
