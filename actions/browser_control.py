# actions/browser_control.py
"""
Era One Browser Controller — Playwright-based, tab-aware, Brave-first.

Key capabilities:
  • Connects directly to your manual live browser via Remote Debugging CDP
  • Smart Tab Reuse: Recycles open media tabs to prevent duplicate window tabs
  • Direct Thumbnail Player: Clicks show thumbnails directly to bypass player modals
  • TV-Exclusive App Casting: Streams directly to 'Salle TV' while auto-playing backends
  • Single registry singleton across the whole Era One process
"""


from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)
from rapidfuzz import fuzz

from actions.browser_driver_health import (
    consume_driver_error,
    driver_alive,
    is_driver_death,
    kill_driver,
    rebuild_call,
)
from actions.page_refs import hold_if_sensitive
from core.utils import get_os, log
from core.win_subprocess import NO_WINDOW

_OS = get_os()  # "windows" | "darwin" | "linux"

# How long a deliberate tab selection wins over "whatever tab looks active"
# (see _BrowserSession._current_page). Long enough for a multi-step task on one
# tab; short enough that a tab Karim clicks himself takes over again.
PIN_TTL_S = 180.0


def _trace_page(why: str, page) -> None:
    """One log line per tab choice, so a wrong-tab read is visible in the logs."""
    try:
        url = (page.url or "")[:90]
    except Exception:
        url = "?"
    log("Browser", f"current_page={why} url={url}")


def _normalize_url(url: str) -> str:
    """'youtube' → 'https://youtube.com', 'google.com' → 'https://google.com'"""
    url = url.strip()
    if not url:
        return "about:blank"
    if "://" in url:
        return url
    if "." not in url:
        url = url + ".com"
    return "https://" + url


_DOMAIN_FAMILIES: dict[str, tuple[str, ...]] = {
    "youtube.com": ("youtube.com", "youtu.be"),
    "netflix.com": ("netflix.com",),
}


def _domain_matches(url: str, domain: str) -> bool:
    """True if *url*'s hostname is *domain* or a subdomain of *domain*.

    _domain_matches("https://www.youtube.com/watch?v=x", "youtube.com")  → True
    _domain_matches("https://youtube.com.fake.com/page",   "youtube.com")  → False
    """
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def _domain_family_matches(url: str, canonical_domain: str) -> bool:
    """True if *url* belongs to the domain family of *canonical_domain*.

    Uses _DOMAIN_FAMILIES so that, e.g., youtu.be is recognised as YouTube.
    Falls back to _domain_matches when the canonical is not in the families map.
    """
    family = _DOMAIN_FAMILIES.get(canonical_domain.lower())
    if family:
        return any(_domain_matches(url, member) for member in family)
    return _domain_matches(url, canonical_domain)


def _expected_domain_family_from_url(url: str) -> str | None:
    """Return the canonical domain key whose family *url* belongs to, or None."""
    for canonical, family in _DOMAIN_FAMILIES.items():
        if any(_domain_matches(url, member) for member in family):
            return canonical
    return None


# Web apps where a second tab is never what the user meant: go_to reuses the
# open one. Exact hosts only — "google.com" here would hijack search tabs.
_REUSABLE_APP_HOSTS: tuple[str, ...] = ("mail.google.com",)


def _reusable_app_host(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    return host if host in _REUSABLE_APP_HOSTS else None


def _is_app_root(url: str) -> bool:
    """True for the app's landing URL (e.g. https://mail.google.com/ or /mail/u/0/)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = (parsed.path or "/").rstrip("/")
    return not parsed.query and not parsed.fragment and path in ("", "/mail", "/mail/u/0")


def _find_opera_windows() -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    prog = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")
    for p in [
        Path(local) / "Programs" / "Opera" / "opera.exe",
        Path(local) / "Programs" / "Opera GX" / "opera.exe",
        Path(prog) / "Opera" / "opera.exe",
        Path(prog86) / "Opera" / "opera.exe",
    ]:
        if p.exists():
            return str(p)
    try:
        import winreg

        for key in [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
        ]:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k = winreg.OpenKey(hive, key)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    pass
    except Exception:
        pass
    return shutil.which("opera")


def _find_exe_windows(prog_name: str) -> str | None:
    try:
        import winreg

        for key in [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{prog_name}.exe",
            rf"SOFTWARE\Clients\StartMenuInternet\{prog_name}\shell\open\command",
        ]:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k = winreg.OpenKey(hive, key)
                    val = (
                        winreg.QueryValueEx(k, "")[0]
                        if winreg.QueryValue(k, None) is None
                        else winreg.QueryValue(k, None)
                    )
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    pass
    except Exception:
        pass
    return shutil.which(prog_name)


_BROWSER_SPECS: dict[str, dict] = {
    "windows": {
        "chrome": {"engine": "chromium", "channel": "chrome", "bins": []},
        "edge": {"engine": "chromium", "channel": "msedge", "bins": []},
        "firefox": {"engine": "firefox", "channel": None, "bins": ["firefox.exe"]},
        "opera": {
            "engine": "chromium",
            "channel": None,
            "bins": ["opera.exe"],
            "special": "opera_windows",
        },
        "operagx": {
            "engine": "chromium",
            "channel": None,
            "bins": [],
            "special": "opera_windows",
        },
        "brave": {"engine": "chromium", "channel": None, "bins": ["brave.exe"]},
        "vivaldi": {"engine": "chromium", "channel": None, "bins": ["vivaldi.exe"]},
    },
    "darwin": {
        "chrome": {"engine": "chromium", "channel": "chrome", "bins": []},
        "edge": {"engine": "chromium", "channel": "msedge", "bins": ["microsoft-edge"]},
        "firefox": {"engine": "firefox", "channel": None, "bins": ["firefox"]},
        "brave": {
            "engine": "chromium",
            "channel": None,
            "bins": ["brave browser", "brave"],
        },
        "vivaldi": {"engine": "chromium", "channel": None, "bins": ["vivaldi"]},
        "safari": {"engine": "webkit", "channel": None, "bins": []},
    },
    "linux": {
        "chrome": {
            "engine": "chromium",
            "channel": None,
            "bins": [
                "google-chrome",
                "google-chrome-stable",
                "chromium-browser",
                "chromium",
            ],
        },
        "edge": {
            "engine": "chromium",
            "channel": None,
            "bins": ["microsoft-edge", "microsoft-edge-stable"],
        },
        "firefox": {"engine": "firefox", "channel": None, "bins": ["firefox"]},
        "brave": {
            "engine": "chromium",
            "channel": None,
            "bins": ["brave-browser", "brave"],
        },
        "vivaldi": {
            "engine": "chromium",
            "channel": None,
            "bins": ["vivaldi-stable", "vivaldi"],
        },
    },
}

_ALIASES: dict[str, str] = {
    "google chrome": "chrome",
    "google-chrome": "chrome",
    "microsoft edge": "edge",
    "ms edge": "edge",
    "msedge": "edge",
    "mozilla firefox": "firefox",
    "opera gx": "operagx",
    "opera_gx": "operagx",
}


def _resolve_browser(name: str) -> dict | None:
    name = _ALIASES.get(name.lower().strip(), name.lower().strip())
    os_map = _BROWSER_SPECS.get(_OS, {})
    spec = os_map.get(name)
    if spec is None:
        return None

    engine, channel = spec["engine"], spec.get("channel")
    bins = spec.get("bins", [])
    exe = None

    if spec.get("special") == "opera_windows":
        return {"engine": engine, "exe": _find_opera_windows(), "channel": channel}

    for b in bins:
        found = shutil.which(b)
        if found:
            exe = found
            break

    if not exe and _OS == "darwin":
        app_names = {
            "chrome": ["Google Chrome.app"],
            "edge": ["Microsoft Edge.app"],
            "firefox": ["Firefox.app"],
            "brave": ["Brave Browser.app"],
            "vivaldi": ["Vivaldi.app"],
        }
        for app in app_names.get(name, []):
            app_dir = Path("/Applications") / app / "Contents" / "MacOS"
            if app_dir.exists():
                found_bins = list(app_dir.iterdir())
                if found_bins:
                    exe = str(found_bins[0])
                    break

    if not exe and _OS == "windows" and not channel:
        exe = _find_exe_windows(name)

    return {"engine": engine, "exe": exe, "channel": channel}


def _detect_default_browser() -> str:
    try:
        if _OS == "windows":
            import winreg

            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(k, "ProgId")[0].lower()
            winreg.CloseKey(k)
            for kw in ("edge", "firefox", "opera", "brave", "vivaldi", "chrome"):
                if kw in prog_id:
                    return kw
        elif _OS == "darwin":
            out = subprocess.run(
                [
                    "defaults",
                    "read",
                    "com.apple.LaunchServices/com.apple.launchservices.secure",
                    "LSHandlers",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.lower()
            for kw in (
                "firefox",
                "opera",
                "brave",
                "vivaldi",
                "safari",
                "chrome",
                "edge",
            ):
                if kw in out:
                    return kw
        elif _OS == "linux":
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "chrome", "edge"):
                if kw in out:
                    return kw
    except Exception:
        pass
    return "chrome"


def _try_save_grid(platform: str, page: Page) -> None:
    """Minimal grid context marker. Actual scanning happens in media controllers."""
    try:
        import time

        from core.browser_grid_state import GridSnapshot, save_grid

        save_grid(GridSnapshot(platform=platform, query="", timestamp=time.time()))
    except Exception:
        pass


class _BrowserSession:
    def __init__(self, browser_name: str):
        self._cdp_ready = False
        self.browser_name = browser_name
        self._spec = _resolve_browser(browser_name)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

        self._pw: Playwright | None = None
        self._browser_obj: any = None
        self._context: BrowserContext | None = None

        self._pages: list[Page] = []
        self._active: int = 0
        # The tab the assistant deliberately selected last (switch/focus/open/
        # go_to) and when. _current_page() honours it for PIN_TTL_S. Before
        # 25/09 it returned the FIRST page whose visibilityState was 'visible'
        # -- with two Brave windows that is each window's active tab, so after
        # "focus the Netflix tab" get_text kept reading a CodeCraft tab.
        self._pinned: Page | None = None
        self._pinned_at: float = 0.0
        # Pages already carrying close/navigation listeners. Rescans run on
        # every tab query, so without this the same page accumulated a new
        # pair of handlers on each pass.
        self._listened_pages: set = set()
        # Serialises driver restarts: several tool threads can notice a dead
        # driver at once, and only one of them should replace it.
        self._driver_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"BrowserThread-{self.browser_name}",
        )
        self._thread.start()
        self._ready.wait(timeout=20)

    async def _fresh_cdp_rescan(self) -> None:
        await self._launch()
        self._pages = [p for p in self._pages if not p.is_closed()]

        # Gather pages across ALL CDP contexts, not just contexts[0]. Separate
        # browser windows, profiles and incognito sessions surface as distinct
        # contexts, so scanning only one context misses tabs the user has open.
        if self._browser_obj:
            try:
                all_cdp_pages = []
                for ctx in self._browser_obj.contexts:
                    try:
                        all_cdp_pages.extend(ctx.pages)
                    except Exception:
                        continue
                for p in all_cdp_pages:
                    if p not in self._pages and not p.is_closed():
                        self._pages.append(p)
                        # Close + URL/title listeners for real-time tab tracking
                        self._attach_page_listeners(p)
            except Exception:
                pass

        # Re-sort pages by their visual left-to-right tab position
        self._pages = await self._sort_tabs_by_visual_position(self._pages)

        if not self._pages:
            self._active = 0
            return

        if self._active < 0 or self._active >= len(self._pages):
            self._active = min(max(self._active, 0), len(self._pages) - 1)

    def _maybe_brave_focus_retry(self, attempts: int = 3, delay_s: float = 0.35) -> None:
        if _OS != "windows":
            return

        try:
            import psutil
            import win32gui
            import win32process
        except Exception:
            try:
                from actions.window_control import focus_window

                focus_window("brave")
            except Exception:
                pass
            return

        target_proc_name = "brave.exe"

        def _get_process_name(pid: int) -> str | None:
            try:
                p = psutil.Process(pid)
                return (p.name() or "").lower()
            except Exception:
                return None

        for _ in range(max(1, int(attempts))):
            try:
                found_hwnd = None

                def enum_handler(hwnd, _):
                    nonlocal found_hwnd
                    try:
                        if not win32gui.IsWindowVisible(hwnd):
                            return
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        pname = _get_process_name(pid)
                        if pname == target_proc_name:
                            found_hwnd = hwnd
                            return
                    except Exception:
                        return

                win32gui.EnumWindows(enum_handler, None)

                if found_hwnd:
                    try:
                        win32gui.ShowWindow(found_hwnd, 5)  # SW_SHOW
                    except Exception:
                        pass
                    try:
                        win32gui.SetForegroundWindow(found_hwnd)
                    except Exception:
                        pass
                    return
            except Exception:
                pass

            time.sleep(max(0.05, float(delay_s)))

        try:
            from actions.window_control import focus_window

            focus_window("brave")
        except Exception:
            pass

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())
        self._ready.set()
        self._loop.run_forever()

    async def _async_init(self):
        self._pw = await async_playwright().start()

    def run(self, coro, timeout: int = 60) -> str:
        """Run `coro` on the session loop, surviving a dead or hung driver.

        - Preflight: a driver that died while Era was idle is replaced before
          the call, so the call runs on a fresh one instead of failing.
        - Mid-call death (raised, or swallowed into an error string by the
          method): restart the driver and retry once, when the call is a
          session method we can re-invoke (see rebuild_call).
        - Timeout: the call is cancelled and the driver probed; a driver that
          doesn't answer is killed and replaced for the next call.
        """
        if not self._loop:
            raise RuntimeError(f"Session '{self.browser_name}' not started.")
        self._ensure_driver()
        retry = rebuild_call(self, coro)
        try:
            result = self._submit(coro, timeout)
        except concurrent.futures.TimeoutError:
            self._recover_from_hang()
            raise
        except Exception as e:
            if not self._driver_died(e):
                raise
            self._ensure_driver(force=True, reason=f"died mid-call: {e}")
            if retry is None:
                raise
            return self._submit(retry(), timeout)
        if retry is not None and isinstance(result, str) and self._driver_died(result):
            self._ensure_driver(force=True, reason=f"died mid-call: {result}")
            return self._submit(retry(), timeout)
        return result

    def _submit(self, coro, timeout: int):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # Don't leave a stuck call running behind the next one.
            future.cancel()
            raise

    def _driver_died(self, err) -> bool:
        return is_driver_death(err) or not driver_alive(self._pw)

    def _ensure_driver(self, force: bool = False, reason: str = "") -> bool:
        """Replace the Playwright driver if it is dead. True if it was replaced."""
        if threading.current_thread() is self._thread:
            return False  # blocking on our own loop would deadlock
        if not force and driver_alive(self._pw):
            return False
        with self._driver_lock:
            if not force and driver_alive(self._pw):
                return False
            future = asyncio.run_coroutine_threadsafe(
                self._restart_driver(reason or "driver process exited"), self._loop
            )
            future.result(timeout=30)
            return True

    async def _restart_driver(self, reason: str) -> None:
        """Stop the old driver (killing it if it won't stop) and start a new one.

        Every Page/Browser object belongs to the old driver, so the CDP
        attachment is dropped too; the next _launch() reattaches to the same
        running Brave — tabs and playback are untouched.
        """
        log(
            "Browser",
            f"Playwright driver lost ({reason}) – restarting it and reattaching.",
        )
        old = self._pw
        self._pw = None
        self._drop_stale_connection()
        if old is not None:
            try:
                await asyncio.wait_for(old.stop(), timeout=5)
            except Exception:
                kill_driver(old)
            consume_driver_error(old)
        self._pw = await async_playwright().start()

    def _recover_from_hang(self) -> None:
        if threading.current_thread() is self._thread:
            return
        try:
            ok = asyncio.run_coroutine_threadsafe(self._probe_driver(), self._loop).result(
                timeout=10
            )
        except Exception:
            ok = False
        if not ok:
            kill_driver(self._pw)
            try:
                self._ensure_driver(force=True, reason="driver stopped responding")
            except Exception as e:
                log("Browser", f"Driver restart after hang failed: {e}")

    async def _probe_driver(self) -> bool:
        """One real round trip driver→Brave→driver, bounded at 5 s."""
        if not driver_alive(self._pw):
            return False
        browser = self._browser_obj
        if browser is None:
            return True  # nothing attached; a live process is all we can check

        async def _ping():
            cdp = await browser.new_browser_cdp_session()
            try:
                await cdp.send("Browser.getVersion")
            finally:
                await cdp.detach()

        try:
            await asyncio.wait_for(_ping(), timeout=5)
            return True
        except Exception:
            return False

    def close(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(10)

    async def _async_close(self):
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser_obj:
            try:
                await self._browser_obj.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = None
        self._browser_obj = None
        self._pages = []
        self._active = 0
        self._cdp_ready = False
        self._listened_pages.clear()

    def _attach_page_listeners(self, page: Page) -> None:
        """Register close/navigation listeners on a page exactly once."""
        if page in self._listened_pages:
            return
        try:
            page.on("close", self._on_page_close)
            page.on("framenavigated", self._on_page_updated)
            self._listened_pages.add(page)
        except Exception:
            pass

    def _connection_alive(self) -> bool:
        """True only if the cached CDP attachment still has a live browser behind it.

        `self._context` being non-None is NOT proof of life. When the user quits
        or restarts the browser, Playwright keeps the stale objects around but
        `is_connected()` flips to False and `contexts` empties. Because
        `_launch()` used to short-circuit on `self._context is not None` alone,
        a dead attachment was never rebuilt: every later tab query walked an
        empty `contexts` list and answered "No open tabs." — instantly, and
        logged as a *successful* call — for a browser that was plainly open.
        That is the "Era can't see my tabs" bug, and it never self-healed until
        Era was restarted.

        A dead Playwright *driver* is the same trap one level down: is_connected()
        and contexts both keep reporting a healthy browser, so driver_alive()
        is checked first.
        """
        browser = self._browser_obj
        if browser is None or self._context is None:
            return False
        if not driver_alive(self._pw):
            return False
        try:
            if not browser.is_connected():
                return False
            return bool(browser.contexts)
        except Exception:
            return False

    def _drop_stale_connection(self) -> None:
        """Forget a dead CDP attachment so the next _launch() reattaches cleanly."""
        self._context = None
        self._browser_obj = None
        self._pages = []
        self._active = 0
        self._cdp_ready = False
        self._listened_pages.clear()

    async def _launch(self):
        if self._connection_alive():
            return

        # Reattaching through a dead driver fails with the very error we are
        # recovering from ("Connection closed while reading from the driver").
        if not driver_alive(self._pw):
            await self._restart_driver("driver process exited")

        # Reaching here with state still set means the previous attachment died.
        if self._context is not None or self._browser_obj is not None:
            log(
                "Browser",
                "CDP link is dead (browser closed or restarted) – reattaching.",
            )
            self._drop_stale_connection()

        if self._spec is None:
            raise RuntimeError(f"'{self.browser_name}' is not supported on {_OS}.")

        engine_name = self._spec["engine"]
        if engine_name != "chromium":
            raise RuntimeError(
                f"CDP attach-only mode is only implemented for Chromium browsers; got engine '{engine_name}'."
            )

        try:
            try:
                self._browser_obj = await self._attach_cdp()
            except Exception as attach_err:
                # ── CDP self-recovery ────────────────────────────────────
                # If the browser is already running, it was started WITHOUT
                # the debug flag: relaunching with the flag now would be
                # silently ignored by the existing process, so killing and
                # restarting is the user's call — explain exactly that.
                if self._browser_process_running():
                    raise RuntimeError(
                        f"{self.browser_name.title()} is running WITHOUT remote debugging, "
                        "so Era cannot attach (the debug flag is ignored while an unflagged "
                        f"instance is alive). Fully quit {self.browser_name.title()} — all "
                        "windows and the tray icon — then relaunch it with "
                        "--remote-debugging-port=9222."
                    ) from attach_err
                # Browser not running at all: start it ourselves with the
                # flag, then poll the attach until the process is ready.
                if not self._start_browser_with_debug_port():
                    raise RuntimeError(
                        f"CDP attach-only mode failed and '{self.browser_name}' could not "
                        "be auto-started. Launch it manually with "
                        "--remote-debugging-port=9222."
                    ) from attach_err
                last_err: Exception | None = None
                for _ in range(20):  # up to ~10 s for the browser to boot
                    await asyncio.sleep(0.5)
                    try:
                        self._browser_obj = await self._attach_cdp()
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                if last_err is not None:
                    raise RuntimeError(
                        f"Auto-started {self.browser_name} with remote debugging but could "
                        "not attach to CDP on http://127.0.0.1:9222. "
                        f"Attach error: {last_err}"
                    ) from last_err

            if not self._browser_obj.contexts:
                raise RuntimeError(
                    f"Attached to {self.browser_name.title()} on port 9222 but it reported "
                    "no browser context (no open window). Open a window and retry."
                )
            self._context = self._browser_obj.contexts[0]
            # Collect tabs from every context (all windows / profiles / incognito),
            # not just the first, so Era sees the user's full set of open tabs.
            self._pages = []
            for ctx in self._browser_obj.contexts:
                try:
                    self._pages.extend(ctx.pages)
                    ctx.on("page", self._on_new_page)
                except Exception:
                    continue
            self._active = 0
            self._pages = await self._sort_tabs_by_visual_position(self._pages)

            for p in self._pages:
                self._attach_page_listeners(p)

            # Undo Playwright's default light-scheme emulation on every adopted
            # tab so existing dark-mode sites don't flip to light on connect.
            for p in self._pages:
                await self._reset_media_emulation(p)

            # Undo Playwright's download interception on every adopted tab too.
            # connect_over_cdp() puts Chromium's download behavior under
            # Playwright's control browser-wide; without this, downloads
            # started in tabs that were already open before Era attached get
            # saved to a Playwright-managed temp path with a GUID filename
            # instead of the real suggested name — the corrupted-filename bug.
            # _on_new_page() only covers tabs opened *after* connect, so the
            # initial batch needs the same reset applied here explicitly.
            for p in self._pages:
                await self._restore_native_downloads(p)

            self._cdp_ready = True
            log("Browser", f"CDP connected: {len(self._pages)} existing pages")
            log("Browser", "🔗 CDP Link active on port 9222.")
            return
        except RuntimeError:
            # Specific recovery diagnostics from above — pass through untouched.
            raise
        except Exception as e:
            raise RuntimeError(
                "CDP attach-only mode failed. Ensure Brave/Chromium is already "
                "running with Remote Debugging enabled on http://127.0.0.1:9222 "
                f"Attach error: {e}"
            ) from e

    def _live_context(self) -> BrowserContext | None:
        """A context that can open tabs right now, or None.

        `self._context` is pinned to contexts[0] at attach time; if the user
        closed that window/profile since, it is dead while the browser itself
        is fine — fall through to any other live context before giving up.
        """
        browser = self._browser_obj
        if browser is None:
            return None
        try:
            if not browser.is_connected():
                return None
            contexts = list(browser.contexts)
        except Exception:
            return None
        if self._context is not None and self._context in contexts:
            return self._context
        if contexts:
            self._context = contexts[0]
            return self._context
        return None

    async def _open_new_page(self) -> Page:
        """Open a new tab on the attached browser — the ONLY way to create a tab.

        Every call site used to do `self._context.new_page()` directly, and
        several (go_to among them) never called `_launch()` first, so the first
        such call after an Era relaunch died with
        "'NoneType' object has no attribute 'new_page'". This attaches if
        needed, and if the attachment turns out to be dead mid-call (browser
        restarted between the liveness check and the CDP command) it drops it,
        reattaches once and retries.
        """
        last_err: Exception | None = None
        for attempt in range(2):
            await self._launch()
            ctx = self._live_context()
            if ctx is None:
                self._drop_stale_connection()
                last_err = RuntimeError(
                    f"{self.browser_name.title()} is attached but exposes no browser "
                    "context to open a tab in."
                )
                continue
            try:
                page = await ctx.new_page()
            except Exception as e:
                last_err = e
                log(
                    "Browser",
                    f"new_page failed (attempt {attempt + 1}): {e} – reattaching.",
                )
                self._drop_stale_connection()
                continue
            if page not in self._pages:
                self._pages.append(page)
            self._attach_page_listeners(page)
            self._pages = await self._sort_tabs_by_visual_position(self._pages)
            if page not in self._pages:  # sort drops closed pages
                self._pages.append(page)
            self._active = self._pages.index(page)
            self._pin(page)
            return page
        raise RuntimeError(
            f"Could not open a new {self.browser_name.title()} tab: {last_err}"
        ) from last_err

    async def _attach_cdp(self):
        """Attach to an already-running Chromium's debug port (attach-only mode)."""
        try:
            return await self._pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        except Exception:
            return await self._pw.chromium.connect_over_cdp("http://localhost:9222")

    def _browser_exe_names(self) -> tuple[str, ...]:
        known = {
            "brave": ("brave.exe", "brave", "brave-browser"),
            "chrome": ("chrome.exe", "chrome", "google-chrome", "google-chrome-stable"),
            "edge": ("msedge.exe", "msedge", "microsoft-edge"),
            "vivaldi": ("vivaldi.exe", "vivaldi", "vivaldi-stable"),
            "opera": ("opera.exe", "opera"),
            "operagx": ("opera.exe", "opera"),
        }
        return known.get(self.browser_name, (f"{self.browser_name}.exe", self.browser_name))

    def _browser_process_running(self) -> bool:
        try:
            import psutil

            names = self._browser_exe_names()
            for proc in psutil.process_iter(["name"]):
                if (proc.info.get("name") or "").lower() in names:
                    return True
        except Exception:
            pass
        return False

    def _start_browser_with_debug_port(self) -> bool:
        """Launch the browser with --remote-debugging-port=9222. Returns True on spawn."""
        exe = (self._spec or {}).get("exe")
        if not exe and _OS == "windows" and self.browser_name == "brave":
            for candidate in (
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "BraveSoftware"
                / "Brave-Browser"
                / "Application"
                / "brave.exe",
                Path(os.environ.get("PROGRAMFILES", ""))
                / "BraveSoftware"
                / "Brave-Browser"
                / "Application"
                / "brave.exe",
            ):
                if candidate.exists():
                    exe = str(candidate)
                    break
        if not exe:
            return False
        try:
            subprocess.Popen(
                [
                    exe,
                    "--remote-debugging-port=9222",
                    # Keep painting when covered/unfocused so screenshots don't stall.
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW)
            log(
                "Browser",
                f"Auto-started {self.browser_name} with remote debugging on 9222.",
            )
            return True
        except Exception:
            return False

    async def _restore_native_downloads(self, page: Page):
        """Forces Playwright to release its grip on the download manager so Brave works natively."""
        try:
            cdp = await page.context.new_cdp_session(page)
            # Revert both Browser and Page level download interceptions back to Brave's default
            await cdp.send("Browser.setDownloadBehavior", {"behavior": "default"})
            await cdp.send("Page.setDownloadBehavior", {"behavior": "default"})
        except Exception:
            pass

    async def _reset_media_emulation(self, page: Page):
        """Undo Playwright's default `colorScheme: light` emulation on an adopted page.

        `connect_over_cdp` makes Playwright adopt every existing tab under the
        browser context's default options, whose `colorScheme` is 'light'. That
        silently forces `prefers-color-scheme` to light on tabs that were showing
        the OS dark theme — the "sites flip dark→light on CDP connect" bug.
        `emulate_media(color_scheme="null")` clears the per-page override so the
        query falls back to the real OS value, and it persists across navigations.
        """
        try:
            await page.emulate_media(
                color_scheme="null", reduced_motion="null", forced_colors="null"
            )
        except Exception:
            pass

    def _on_new_page(self, page: Page):
        if page not in self._pages:
            self._pages.append(page)
            page.on("close", self._on_page_close)
            page.on("framenavigated", self._on_page_updated)

            # Reset Playwright's default light-scheme emulation on any newly
            # adopted tab too (runs for both init-time and later tabs; the call
            # is idempotent, so double-resetting an init tab is harmless).
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._reset_media_emulation(page), self._loop)

            # ── CDP init guard ──────────────────────────────────────────────
            # During initial connect_over_cdp, Playwright may fire the "page"
            # event for existing tabs.  Skip _restore_native_downloads for
            # those — only apply it to tabs genuinely created after init.
            if not self._cdp_ready:
                return

            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._restore_native_downloads(page), self._loop)

    def _on_page_close(self, page: Page):
        if page in self._pages:
            self._pages.remove(page)
            if self._active >= len(self._pages) and self._pages:
                self._active = len(self._pages) - 1
            elif not self._pages:
                self._active = 0

    def _on_page_updated(self, frame=None):
        """Event handler for page URL or title updates."""
        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._sort_tabs_by_visual_position(self._pages), self._loop
                )
        except Exception:
            pass

    async def _current_page(self) -> Page:
        await self._launch()
        await self._fresh_cdp_rescan()

        # CRITICAL PROTECTION: If tabs appear empty, poll CDP for up to 300ms
        # to allow the initial connection handshake to discover existing tabs.
        if not self._pages and self._context:
            for _ in range(3):
                if list(self._context.pages):
                    await self._fresh_cdp_rescan()
                    break
                await asyncio.sleep(0.1)

        # Only create a brand new tab if Brave literally has 0 windows/tabs open
        if not self._pages:
            return await self._open_new_page()

        # 1. The tab the assistant just selected, while the selection is fresh.
        pinned = self._pinned
        if (
            pinned is not None
            and not pinned.is_closed()
            and time.monotonic() - self._pinned_at < PIN_TTL_S
        ):
            if pinned in self._pages:
                self._active = self._pages.index(pinned)
            _trace_page("pinned", pinned)
            return pinned

        # 2. The focused window's active tab: document.hasFocus() is true for
        #    ONE tab, while visibilityState is 'visible' in every window.
        # 3. Else the first visible tab (browser in the background).
        for probe, why in (("document.hasFocus()", "focused"),
                           ("document.visibilityState === 'visible'", "visible")):  # fmt: skip
            for page in self._pages:
                try:
                    if await page.evaluate(probe):
                        self._active = self._pages.index(page)
                        _trace_page(why, page)
                        return page
                except Exception:
                    continue

        # Fallback: If browser is minimized or hidden, return last known active tab safely
        if self._active < 0 or self._active >= len(self._pages):
            self._active = len(self._pages) - 1
        _trace_page("last-known", self._pages[self._active])
        return self._pages[self._active]

    def _pin(self, page: Page | None) -> None:
        """Remember a deliberate tab selection (see _current_page)."""
        if page is not None:
            self._pinned = page
            self._pinned_at = time.monotonic()

    async def _find_tab_by_keywords(self, keywords: list[str]) -> Page | None:
        await self._fresh_cdp_rescan()
        best_score = 0
        best_page: Page | None = None
        for page in self._pages:
            if page.is_closed():
                continue

            url = ""
            try:
                url = page.url or ""
            except Exception:
                pass

            title = ""
            try:
                title = (await page.title()) or ""
            except Exception:
                pass

            if not url and not title:
                continue

            score = 0
            for kw in keywords:
                kw_lc = kw.lower()
                if "." in kw_lc:
                    if _domain_matches(url, kw_lc):
                        score = max(score, 100)
                else:
                    combined = f"{url} {title}".lower()
                    if kw_lc in combined:
                        score = max(score, 60)

            if score > best_score:
                best_score = score
                best_page = page

        return best_page

    async def get_or_open_platform_page(
        self, platform: str, force_new: bool = False, home_url: str = ""
    ) -> Page:
        """Find existing platform tab or create+go_to home. Called by platform_tab module."""
        await self._launch()
        await self._fresh_cdp_rescan()

        platform = (platform or "").lower().strip()
        domain_keywords = []
        if platform in ("youtube", "yt"):
            domain_keywords = ["youtube.com", "youtu.be"]
        elif platform in ("netflix", "nf"):
            domain_keywords = ["netflix.com"]
        if not domain_keywords:
            raise ValueError(f"Unsupported platform: {platform}")

        if not force_new:
            existing = await self._find_tab_by_keywords(domain_keywords)
            if existing is not None and not existing.is_closed():
                self._active = self._pages.index(existing)
                self._pin(existing)
                try:
                    await existing.bring_to_front()
                except Exception:
                    pass
                log(
                    "Browser",
                    f"get_or_open_platform_page reused {platform} tab [{self._active + 1}]",
                )
                _try_save_grid(platform, existing)
                return existing

        page = await self._open_new_page()
        try:
            await page.bring_to_front()
            await page.evaluate("window.focus()")
        except Exception:
            pass

        target_url = home_url or _normalize_url(domain_keywords[0])
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(0.3)
        except PlaywrightTimeout:
            pass
        except Exception as e:
            log("Browser", f"get_or_open_platform_page goto error: {e}")

        log(
            "Browser",
            f"get_or_open_platform_page created new {platform} tab [{self._active + 1}]",
        )
        _try_save_grid(platform, page)
        return page

    async def netflix_search_and_play(self, title: str) -> str:
        page = await self._current_page()
        try:
            search_url = f"https://www.netflix.com/search?q={title.replace(' ', '%20')}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            thumbnail_clicked = False
            selectors = [
                f'a[aria-label*="{title}" i]',
                ".title-card a",
                "a.slider-refocus",
            ]

            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() > 0:
                        await locator.click(timeout=4000)
                        thumbnail_clicked = True
                        break
                except Exception:
                    continue

            if not thumbnail_clicked:
                return f"Could not resolve a visible thumbnail link locator for '{title}'."

            await asyncio.sleep(1.5)
            if "watch" in page.url.lower():
                return f"Direct player stream initiated for '{title}'."

            modal_play_selectors = [
                '[data-uia="play-button"]',
                "a.playLink",
                "button.playLink",
                'a:has-text("Ép. suivant")',
                'button:has-text("Ép. suivant")',
                'a:has-text("Reprendre")',
                'button:has-text("Reprendre")',
                '[aria-label="Play"]',
                '[aria-label="Lecture"]',
            ]

            for modal_selector in modal_play_selectors:
                try:
                    modal_locator = page.locator(modal_selector).first
                    if await modal_locator.count() > 0 and await modal_locator.is_visible():
                        await modal_locator.click(timeout=3000)
                        return f"Modal anchor bypassed. Streaming initialized for '{title}'."
                except Exception:
                    continue

            return "Thumbnail clicked, but secondary modal play button could not be resolved programmatically."
        except Exception as e:
            return f"Netflix dynamic pipeline error: {e}"

    async def cast_screen(self, fragment: str = "") -> str:
        if fragment:
            target_index = await self._find_tab_index(fragment)
            if target_index is not None:
                await self.switch_tab(target_index)
                await asyncio.sleep(0.5)

        page = await self._current_page()
        try:
            log("Browser", "Initializing core Chromium DevTools Protocol Cast layer...")
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Cast.enable")
            await cdp.send("Cast.setSinkToUse", {"sinkName": "Salle TV"})
            await cdp.send("Cast.startTabMirroring", {"sinkName": "Salle TV"})

            netflix_cast_button = page.locator(
                'button[data-uia="control-cast"], button[aria-label*="ast" i]'
            ).first
            if await netflix_cast_button.count() > 0:
                log(
                    "Browser",
                    "Activating inline video player cast node inside Netflix container DOM...",
                )
                await netflix_cast_button.click(timeout=2000)

            title = await page.title() or fragment or "active"
            return f"Casting '{title}' natively to 'Salle TV', sir."
        except Exception as e:
            log(
                "Browser",
                f"CDP Cast domain exception: {e}. Executing fallback stream matrix...",
            )
            try:
                await cdp.send("Cast.startTabMirroring", {"sinkName": "Salle TV"})
                return "Tab mirrored directly to 'Salle TV' via backend fallback sequence, sir."
            except Exception as ex:
                return f"Native casting domain pipeline failure: {ex}"

    async def stop_casting(self) -> str:
        page = await self._current_page()
        try:
            log("Browser", "Sending teardown signal to active Chromium Cast session...")
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Cast.enable")
            await cdp.send("Cast.stopCasting", {"sinkName": "Salle TV"})
            return "Casting session terminated safely, sir."
        except Exception as e:
            return f"Failed to gracefully close the casting pipeline: {e}"

    def _strip_dynamic_tab_suffixes(self, s: str) -> str:
        if not s:
            return ""
        s = (s or "").strip()
        s = re.sub(r"\s*[-|]\s*YouTube\s*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*[-|]\s*Netflix\s*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*[-|]\s*Hulu\s*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*[-|]\s*Prime\s*Video\s*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*[-|]\s*Disney\s*\+\s*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _build_fuzzy_regex(self, fragment: str) -> re.Pattern:
        frag = (fragment or "").strip()
        frag = self._strip_dynamic_tab_suffixes(frag)
        frag = frag.lower()

        if not frag:
            return re.compile(r"a^")

        tokens = [t for t in re.split(r"[\s\-\|\_:/\.]+", frag) if t]
        tokens = tokens[:8]

        pattern = r""
        for idx, tok in enumerate(tokens):
            tok_esc = re.escape(tok)
            if idx == 0:
                pattern += tok_esc
            else:
                pattern += r".*?" + tok_esc
        return re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)

    def _browser_parent_hwnd_windows(self) -> int | None:
        try:
            import psutil
            import win32gui
            import win32process

            target_exe = None
            if self.browser_name in {
                "brave",
                "chrome",
                "edge",
                "vivaldi",
                "opera",
                "operagx",
            }:
                if self.browser_name == "brave":
                    target_exe = "brave.exe"
                elif self.browser_name in ("opera", "operagx"):
                    target_exe = "opera.exe"
                elif self.browser_name == "vivaldi":
                    target_exe = "vivaldi.exe"
                elif self.browser_name == "edge":
                    target_exe = "msedge.exe"
                else:
                    target_exe = "chrome.exe"
            else:
                target_exe = "chrome.exe"

            found_hwnd = None

            def enum_handler(hwnd, _):
                nonlocal found_hwnd
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    pname = psutil.Process(pid).name().lower()
                    if pname == target_exe:
                        found_hwnd = hwnd
                        return
                    if pname.endswith(
                        (
                            "chrome.exe",
                            "msedge.exe",
                            "brave.exe",
                            "vivaldi.exe",
                            "opera.exe",
                        )
                    ):
                        if found_hwnd is None:
                            found_hwnd = hwnd
                except Exception:
                    return

            win32gui.EnumWindows(enum_handler, None)
            return found_hwnd
        except Exception:
            return None

    async def _force_activate_browser_window_windows(self) -> None:
        if _OS != "windows":
            return
        try:
            import win32gui

            hwnd = self._browser_parent_hwnd_windows()
            if hwnd:
                try:
                    win32gui.ShowWindow(hwnd, 5)  # SW_SHOW
                except Exception:
                    pass
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
        except Exception:
            pass

    async def _maximize_browser_window_windows(self) -> None:
        if _OS != "windows":
            return
        try:
            import win32con
            import win32gui

            hwnd = self._browser_parent_hwnd_windows()
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXIMIZED)
                win32gui.SetForegroundWindow(hwnd)
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
                time.sleep(0.05)
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
        except Exception:
            pass

    async def _rescan_pages_real_time(self) -> None:
        # _fresh_cdp_rescan() already calls _launch() and gathers every context.
        await self._fresh_cdp_rescan()
        await asyncio.sleep(0)
        if not self._browser_obj:
            return
        try:
            # Re-read from ALL contexts, not just contexts[0]. Reading only
            # self._context here discarded the full set _fresh_cdp_rescan had
            # just built, so focus_tab (the sole caller) could not see tabs in
            # a second window, another profile, or an incognito session — even
            # though list_tabs listed them.
            live_pages: list[Page] = []
            for ctx in self._browser_obj.contexts:
                try:
                    live_pages.extend(p for p in ctx.pages if not p.is_closed())
                except Exception:
                    continue
            self._pages = live_pages
            for p in self._pages:
                self._attach_page_listeners(p)
            self._pages = await self._sort_tabs_by_visual_position(self._pages)
        except Exception:
            pass

    async def _find_tab_multistage(self, fragment: str) -> tuple[int, Page] | None:
        frag_lc = (fragment or "").lower().strip()
        if not frag_lc:
            return None

        cleaned_fragment = self._strip_dynamic_tab_suffixes(fragment)
        cleaned_frag_lc = cleaned_fragment.lower().strip()
        search_tokens = [tok for tok in cleaned_frag_lc.split(" ") if tok]
        regex = self._build_fuzzy_regex(cleaned_fragment)

        best_score = 0
        best_match = None

        for i, page in enumerate(self._pages):
            if page.is_closed():
                continue

            url = ""
            try:
                url = (page.url or "").lower()
            except Exception:
                pass

            title = ""
            try:
                # Safely attempt to get title; gracefully ignore if page is currently navigating
                title = (await page.title() or "").lower()
            except Exception:
                pass

            if not url and not title:
                continue

            score = 0
            cand_title = self._strip_dynamic_tab_suffixes(title)
            cand_url = self._strip_dynamic_tab_suffixes(url)

            # 1. Exact Literal Substring Match (Highest priority)
            if frag_lc in url or frag_lc in title:
                score += 100

            # 2. Fuzzy Pattern Match (High priority)
            if regex.search(cand_title) or regex.search(cand_url):
                score += 80

            # 3. Tokenized Match (Fixes the "Greedy" bug)
            if search_tokens:
                matched_tokens = sum(
                    1 for tok in search_tokens if tok in cand_title or tok in cand_url
                )
                if matched_tokens == len(search_tokens):
                    score += 60
                elif matched_tokens > 0:
                    score += matched_tokens * 5

            # 4. App vs Search Priority
            if frag_lc in url and "google.com/search" not in url:
                score += 40

            if score > best_score:
                best_score = score
                best_match = (i, page)

        return best_match

    async def list_tabs(self) -> str:
        await self._fresh_cdp_rescan()
        if not self._pages:
            return "No open tabs."
        lines = []
        for i, page in enumerate(self._pages):
            url = page.url or "about:blank"
            title = ""
            try:
                title = await page.title()
            except Exception:
                pass
            active_marker = " ◀ active" if i == self._active else ""
            display = title if title else url[:70]
            lines.append(f"  [{i + 1}] {display}{active_marker}")
        return "Browser tabs detected (CDP order):\n" + "\n".join(lines)

    async def switch_tab(self, index: int) -> str:
        await self._fresh_cdp_rescan()
        self._pages = await self._sort_tabs_by_visual_position(self._pages)

        if not self._pages:
            return "No open tabs."
        if index < 0 or index >= len(self._pages):
            return f"Index {index + 1} is out of range (1-{len(self._pages)})."

        window_restored = False
        if _OS == "windows":
            try:
                import win32con
                import win32gui

                hwnd = self._browser_parent_hwnd_windows()
                if hwnd and win32gui.IsIconic(hwnd):
                    log("Browser", "Window minimized – restoring before tab switch.")
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    window_restored = True
            except Exception:
                pass

        self._active = index
        page = self._pages[self._active]
        self._pin(page)
        try:
            await page.bring_to_front()
        except Exception:
            pass

        is_visible = False
        try:
            is_visible = await page.evaluate("document.visibilityState === 'visible'")
        except Exception:
            is_visible = True

        if not is_visible and window_restored:
            log("Browser", "Tab not visible after restore – retrying switch.")
            try:
                await page.bring_to_front()
                is_visible = await page.evaluate("document.visibilityState === 'visible'")
            except Exception:
                pass

        title = ""
        try:
            title = await page.title()
        except Exception:
            pass

        return f"Switched to tab [{self._active + 1}]: {title or page.url}"

    async def focus_tab_by_url(self, fragment: str) -> str:
        await self._rescan_pages_real_time()
        self._pages = await self._sort_tabs_by_visual_position(self._pages)
        found = await self._find_tab_multistage(fragment)
        if not found:
            return f"No tab found containing '{fragment}'."

        page_index, page = found

        current_url = page.url.lower()
        is_video = any(
            domain in current_url for domain in ("youtube.com", "youtu.be", "netflix.com")
        )
        if is_video:
            log(
                "Browser",
                f"Video tab detected - forcing foreground/maximize for {fragment}",
            )
            self._force_window_foreground_maximized(timeout=2.0)

        try:
            self._active = page_index
            self._pin(page)
            await page.bring_to_front()
            await page.evaluate("window.focus();")
        except Exception as e:
            log("Browser", f"CDP bring_to_front error: {e}")

        try:
            title = await page.title()
        except Exception:
            title = ""

        return f"Focused tab [{page_index + 1}]: {(title or page.url or fragment)[:120]}"

    def _force_window_foreground_maximized(self, timeout: float = 2.0) -> bool:
        if _OS != "windows":
            return False
        try:
            import ctypes
            import time as _time

            import win32con
            import win32gui
            import win32process
        except Exception:
            try:
                from actions.window_control import focus_window

                focus_window(self.browser_name)
            except Exception:
                pass
            return False

        proc_map = {
            "chrome": "chrome.exe",
            "brave": "brave.exe",
            "edge": "msedge.exe",
            "opera": "opera.exe",
            "operagx": "opera.exe",
            "vivaldi": "vivaldi.exe",
        }
        target_proc = proc_map.get(self.browser_name, self.browser_name + ".exe").lower()

        try:
            ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
        except Exception:
            pass

        def _get_process_name(pid: int) -> str:
            try:
                import psutil

                return (psutil.Process(pid).name() or "").lower()
            except Exception:
                return ""

        start = _time.time()
        while _time.time() - start < timeout:
            try:
                candidates = []

                # noqa B023 below: `candidates` is rebound each pass of the
                # while-loop, but EnumWindows runs this callback SYNCHRONOUSLY a
                # few lines down, before the next rebind. The callback never
                # outlives its iteration, so there is no late-binding hazard.
                def _enum_cb(hwnd, _):
                    try:
                        if not win32gui.IsWindow(hwnd):
                            return
                        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                        if not (style & win32con.WS_CAPTION):
                            return
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        pname = _get_process_name(pid)
                        if target_proc in (pname, pname.split(".exe")[0] + ".exe"):
                            candidates.append(hwnd)  # noqa: B023
                        elif self.browser_name in (
                            "chrome",
                            "brave",
                            "edge",
                            "vivaldi",
                            "opera",
                            "operagx",
                        ):
                            if pname in (
                                "chrome.exe",
                                "brave.exe",
                                "msedge.exe",
                                "vivaldi.exe",
                                "opera.exe",
                            ):
                                candidates.append(hwnd)  # noqa: B023
                    except Exception:
                        return

                win32gui.EnumWindows(_enum_cb, None)

                if candidates:
                    try:
                        best = next(
                            (h for h in candidates if win32gui.IsWindowVisible(h)),
                            candidates[0],
                        )
                    except StopIteration:
                        continue

                    try:
                        if win32gui.IsIconic(best):
                            win32gui.ShowWindow(best, win32con.SW_RESTORE)
                        win32gui.ShowWindow(best, win32con.SW_SHOWMAXIMIZED)
                    except Exception:
                        pass
                    try:
                        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                        ctypes.windll.user32.keybd_event(0x12, 0, win32con.KEYEVENTF_KEYUP, 0)
                    except Exception:
                        pass
                    try:
                        win32gui.SetForegroundWindow(best)
                    except Exception:
                        pass
                    try:
                        for _ in range(3):
                            if win32gui.GetForegroundWindow() == best:
                                break
                            _time.sleep(0.03)
                        win32gui.SetWindowPos(
                            best,
                            win32con.HWND_TOPMOST,
                            0,
                            0,
                            0,
                            0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                        )
                        _time.sleep(0.05)
                        win32gui.SetWindowPos(
                            best,
                            win32con.HWND_NOTOPMOST,
                            0,
                            0,
                            0,
                            0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                        )
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            _time.sleep(0.05)

        try:
            from actions.window_control import focus_window

            focus_window(self.browser_name)
        except Exception:
            pass
        return False

    async def _sort_tabs_by_visual_position(self, pages: list[Page]) -> list[Page]:
        try:
            cdp_pages = (
                [p for p in self._context.pages if not p.is_closed()] if self._context else []
            )
            order_index = {p: idx for idx, p in enumerate(cdp_pages)}

            alive = [p for p in pages if not p.is_closed()]

            def _key(page):
                if page in order_index:
                    return (0, order_index[page])
                return (1, 0)

            return sorted(alive, key=_key)
        except Exception:
            return [p for p in pages if not p.is_closed()]

    async def _find_tab_index(self, fragment: str) -> int | None:
        frag_lc = (fragment or "").lower().strip()
        if not frag_lc:
            return None

        await self._fresh_cdp_rescan()
        self._pages = [p for p in self._pages if not p.is_closed()]
        if not self._pages:
            return None

        candidates = []
        for i, page in enumerate(self._pages):
            if page.is_closed():
                continue
            url = ""
            try:
                url = (page.url or "").lower()
            except Exception:
                pass
            title = ""
            try:
                title = ((await page.title()) or "").lower()
            except Exception:
                pass
            combined = (title + " " + url) if title else url
            score = fuzz.partial_ratio(frag_lc, combined)
            if score > 60:
                candidates.append((score, i, title or url[:60]))

        if not candidates:
            return None
        candidates.sort(key=lambda x: -x[0])
        best_score, best_i, best_text = candidates[0]
        # Multiple close matches -> the caller asks for clarification. The top
        # three are parked on _last_tab_candidates, which is where that caller
        # reads them; the runner-up score itself was computed and never used.
        self._last_tab_candidates = candidates[:3]
        return best_i

    async def get_active_tab_info(self) -> str:
        page = await self._current_page()
        url = page.url
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        return f"Active tab [{self._active + 1}] — {title or url}"

    async def new_tab(self, url: str = "") -> str:
        page = await self._open_new_page()
        try:
            await page.bring_to_front()
            await page.evaluate("window.focus()")
        except Exception:
            pass

        if url:
            target = _normalize_url(url)
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(0.2)
            except PlaywrightTimeout:
                pass
            except Exception as e:
                return f"New tab navigation error: {e}"
            return f"New tab opened at [{self._active + 1}]: {page.url}"
        return f"New tab opened at [{self._active + 1}]."

    async def close_tab(self, fragment: str = "") -> str:
        # Step 3: refresh list before matching for most accurate result
        await self._fresh_cdp_rescan()
        if not self._pages:
            return "No open tabs."

        if fragment:
            # Enhanced fuzzy match with refresh
            target_index = await self._find_tab_index(fragment)
            if target_index is None:
                return f"No tab found containing '{fragment}'."
            # Confirm if ambiguity (multiple high-score candidates)
            candidates = getattr(self, "_last_tab_candidates", [])
            if len(candidates) > 1 and (candidates[0][0] - candidates[1][0]) < 15:
                opts = "\n".join(f"  [{i + 1}] {t}" for _, i, t in candidates[:3])
                return (
                    f"Multiple matches for '{fragment}':\n{opts}\n"
                    "Please clarify which tab to close."
                )
        else:
            target_index = self._active

        # Re-refresh after selecting to guard against index drift
        await self._fresh_cdp_rescan()
        if not self._pages or (fragment and target_index >= len(self._pages)):
            return "No open tabs (tab indices changed during operation)."

        # If fragment given but index drifted, retry match once more
        if fragment and target_index >= len(self._pages):
            target_index = await self._find_tab_index(fragment)
            if target_index is None or target_index >= len(self._pages):
                return "Tab removed before close could complete."

        page = self._pages[target_index]
        try:
            title = await page.title()
        except Exception:
            title = page.url
        await page.close()

        await self._fresh_cdp_rescan()
        if self._pages:
            self._active = min(target_index, len(self._pages) - 1)
            try:
                await self._pages[self._active].bring_to_front()
            except Exception:
                pass
            return f"Closed tab: {title or fragment}. Active tab is now [{self._active + 1}]."
        return f"Closed tab: {title or fragment}."

    async def next_tab(self) -> str:
        await self._fresh_cdp_rescan()
        if len(self._pages) <= 1:
            return "Only one tab open."
        self._active = (self._active + 1) % len(self._pages)
        return await self.switch_tab(self._active)

    async def prev_tab(self) -> str:
        await self._fresh_cdp_rescan()
        if len(self._pages) <= 1:
            return "Only one tab open."
        self._active = (self._active - 1) % len(self._pages)
        return await self.switch_tab(self._active)

    async def go_to(self, url: str, force_new: bool = False) -> str:
        # Netflix hard-lock: go_to with netflix.com/search URLs must be handled
        # by netflix_control, not the legacy netflix_search_and_play macro.
        # The router's netflix_url_lock already reroutes before execution,
        # but if a Netflix URL reaches here, navigate normally (no macros).
        if "netflix.com/search" in url.lower():
            log(
                "Browser",
                "Netflix search URL blocked at go_to — reroute to netflix_control expected.",
            )
            return "Netflix search URLs are handled by netflix_control only. Please use 'search' action."

        try:
            if _OS == "windows":
                from actions.window_control import focus_window

                focus_window("brave")
        except Exception:
            pass

        url = _normalize_url(url)
        # Attach (or reattach after an Era relaunch / browser restart) before
        # touching any tab state — go_to used to skip this entirely.
        await self._launch()

        # Check if the requested destination is a media streaming layout
        canonical = _expected_domain_family_from_url(url)
        is_media = canonical is not None
        page = None

        app_host = _reusable_app_host(url)
        if app_host and not force_new:
            # Web apps like Gmail: switch to the tab that's already open rather
            # than stacking a second copy. Only navigate it if a specific
            # view was asked for — reloading the inbox the user is looking at
            # to show them the inbox is just a flicker.
            existing = await self._find_tab_by_keywords([app_host])
            if existing is not None and not existing.is_closed():
                self._active = self._pages.index(existing)
                self._pin(existing)
                try:
                    await existing.bring_to_front()
                except Exception:
                    pass
                if _is_app_root(url):
                    return f"Switched to the open tab: {existing.url}"
                page = existing

        if is_media and not force_new:
            # Media Rule: Try to locate and recycle an existing open media tab
            family = _DOMAIN_FAMILIES.get(canonical, ())
            page = await self._find_tab_by_keywords(list(family))

            if page is not None:
                self._active = self._pages.index(page)
                self._pin(page)
                try:
                    await page.bring_to_front()
                except Exception:
                    pass

        if page is None:
            # No reusable tab found (or force_new=True): create a new tab
            page = await self._open_new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(0.3)
        except PlaywrightTimeout:
            pass
        except Exception as e:
            return f"Navigation error: {e}"

        # Post-navigation domain verification for media URLs
        if is_media and canonical:
            actual_url = page.url or ""
            if not _domain_family_matches(actual_url, canonical):
                log(
                    "Browser",
                    f"go_to domain mismatch: wanted {canonical} family, got {actual_url}",
                )
                # Open a fresh correct tab, leave the mismatched tab untouched
                page = await self._open_new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(0.3)
                except PlaywrightTimeout:
                    pass
                except Exception as e:
                    return f"Navigation error (retry): {e}"

        self._pin(page)
        return f"Navigated to: {page.url}"

    async def search(self, query: str, engine: str = "google") -> str:
        _engines = {
            "google": "https://www.google.com/search?q=",
            "bing": "https://www.bing.com/search?q=",
            "duckduckgo": "https://duckduckgo.com/?q=",
            "yandex": "https://yandex.com/search/?text=",
        }
        base = _engines.get(engine.lower(), _engines["google"])
        return await self.go_to(base + query.replace(" ", "+"))

    async def reload(self) -> str:
        page = await self._current_page()
        try:
            await page.reload(timeout=15_000)
            return f"Reloaded: {page.url}"
        except Exception as e:
            return f"Reload error: {e}"

    async def back(self) -> str:
        page = await self._current_page()
        try:
            await page.go_back(timeout=10_000)
            return f"Back → {page.url}"
        except Exception as e:
            return f"Back error: {e}"

    async def forward(self) -> str:
        page = await self._current_page()
        try:
            await page.go_forward(timeout=10_000)
            return f"Forward → {page.url}"
        except Exception as e:
            return f"Forward error: {e}"

    async def click(self, selector: str = None, text: str = None) -> str:
        page = await self._current_page()
        try:
            if text:
                await page.get_by_text(text, exact=False).first.click(timeout=8_000)
                return f"Clicked text: '{text}'"
            if selector:
                await page.click(selector, timeout=8_000)
                return f"Clicked: {selector}"
            return "No selector or text provided."
        except PlaywrightTimeout:
            return "Element not found (timeout)."
        except Exception as e:
            return f"Click error: {e}"

    async def smart_click(self, description: str) -> str:
        # Gated like click_ref: safety_guard checks the description, and
        # hold_if_sensitive checks the label of the element it actually matched.
        page = await self._current_page()
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab"):
            try:
                loc = page.get_by_role(role, name=description)
                if await loc.count() > 0:
                    held = await hold_if_sensitive(loc.first, description)
                    if held:
                        return held
                    await loc.first.click(timeout=5_000)
                    return f"Clicked ({role}): '{description}'"
            except Exception:
                pass
        return f"Could not find element: '{description}'"

    async def type_text(
        self, selector: str = None, text: str = "", clear_first: bool = True
    ) -> str:
        page = await self._current_page()
        try:
            el = page.locator(selector).first if selector else page.locator(":focus")
            if clear_first:
                await el.clear()
            await el.type(text, delay=50)
            return f"Typed into {selector or 'focused element'}."
        except Exception as e:
            return f"Type error: {e}"

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._current_page()
        candidates = [
            ("placeholder", page.get_by_placeholder(description, exact=False)),
            ("label", page.get_by_label(description, exact=False)),
            ("role", page.get_by_role("textbox", name=description)),
        ]
        for method, loc in candidates:
            try:
                el = loc.first
                if await el.count() == 0:
                    continue
                await el.clear()
                await el.type(text, delay=50)
                return f"Typed into ({method}): '{description}'"
            except Exception:
                continue
        return f"Could not find input: '{description}'"

    async def fill_form(self, fields: dict) -> str:
        page = await self._current_page()
        results = []
        for selector, value in fields.items():
            try:
                el = page.locator(selector).first
                await el.clear()
                await el.type(str(value), delay=40)
                results.append(f"✓ {selector}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Form: " + ", ".join(results)

    async def scroll(
        self,
        direction: str = "down",
        amount: str | int = "normal",
        repeat: int = 1,
        behavior: str = "smooth",
    ) -> str:
        from utils.scroll import scroll as engine_scroll

        try:
            result = await engine_scroll(
                page=await self._current_page(),
                direction=direction,
                amount=amount,
                repeat=repeat,
                behavior=behavior,
                source="browser_control",
            )
            return f"Scrolled {result['direction']} \u00d7{result['repeat']} ({result['total_px']}px, {result['behavior']})."
        except Exception as e:
            return f"Scroll error: {e}"

    async def press(self, key: str) -> str:
        page = await self._current_page()

        key_map = {
            "escape": "Escape",
            "enter": "Enter",
            "space": "Space",
            "tab": "Tab",
            "backspace": "Backspace",
            "delete": "Delete",
            "arrowup": "ArrowUp",
            "arrowdown": "ArrowDown",
            "arrowleft": "ArrowLeft",
            "arrowright": "ArrowRight",
            "f11": "F11",
            "f5": "F5",
        }

        norm_key = key_map.get(str(key).lower().strip(), key)

        try:
            await page.keyboard.press(norm_key)
            return f"Pressed: {norm_key}"
        except Exception as e:
            return f"Key error: {e}"

    async def get_text(self) -> str:
        page = await self._current_page()
        try:
            return (await page.inner_text("body"))[:4_000]
        except Exception as e:
            return f"Could not get text: {e}"

    async def get_url(self) -> str:
        page = await self._current_page()
        return page.url

    async def page_snapshot(self) -> str:
        from actions.page_refs import snapshot

        return await snapshot(await self._current_page())

    async def click_ref(self, ref) -> str:
        from actions.page_refs import click_ref

        return await click_ref(await self._current_page(), ref)

    async def _make_paintable(self, page: Page) -> None:
        """Chromium paints no frames for a background tab or a minimized window, so
        page.screenshot() used to sit on its 30 s timeout. Surface the tab first."""
        try:
            if await page.evaluate("document.visibilityState === 'visible'"):
                return
        except Exception:
            pass
        try:
            await page.bring_to_front()
        except Exception:
            pass
        if _OS == "windows":
            try:
                import win32gui

                hwnd = self._browser_parent_hwnd_windows()
                if hwnd and win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
            except Exception:
                pass
        await asyncio.sleep(0.15)

    async def screenshot(self, path: str = None) -> str:
        page = await self._current_page()
        save_path = path or str(Path.home() / "Desktop" / "era_screenshot.png")
        await self._make_paintable(page)
        try:
            await page.screenshot(
                path=save_path,
                full_page=False,
                timeout=10_000,
                animations="disabled",
                caret="hide",
            )
            return f"Screenshot saved: {save_path}"
        except PlaywrightTimeout:
            return (
                "Screenshot error: the browser did not paint the tab within 10 s "
                "(window hidden or covered). page_snapshot reads the page without a picture."
            )
        except Exception as e:
            return f"Screenshot error: {e}"

    async def evaluate_background_media(self, domain_keyword: str, js_script: str) -> str:
        await self._launch()
        self._pages = [p for p in self._pages if not p.is_closed()]

        if self._context:
            for p in self._context.pages:
                if p not in self._pages:
                    self._pages.append(p)
                    p.on("close", self._on_page_close)
        self._pages = await self._sort_tabs_by_visual_position(self._pages)

        target_pages = [
            page for page in self._pages if _domain_matches(page.url, domain_keyword.lower())
        ]

        if not target_pages:
            return f"Target browser tab matching '{domain_keyword}' was not found."

        media_score_script = (
            "(() => {"
            "try {"
            "  const els = Array.from(document.querySelectorAll('video, audio'));"
            "  if (!els.length) return 0;"
            "  if (els.some(e => !e.paused && !e.ended && e.currentTime > 0 && e.readyState > 2)) return 2;"
            "  if (els.some(e => e.readyState > 0 || e.currentTime > 0)) return 1;"
            "  return 0;"
            "} catch (e) { return 0; }"
            "})()"
        )

        scored_pages = []
        for page in target_pages:
            try:
                score = await page.evaluate(media_score_script)
                scored_pages.append((int(score or 0), page))
            except Exception:
                scored_pages.append((0, page))

        scored_pages.sort(key=lambda item: item[0], reverse=True)

        try:
            iife_script = f"(() => {{ {js_script} }})()"
            fallback_result = ""
            for _, page in scored_pages:
                execution_result = await page.evaluate(iife_script)
                result_text = str(execution_result)
                lowered = result_text.lower()
                fallback_result = result_text
                if not any(
                    token in lowered
                    for token in (
                        "no html5",
                        "no active video",
                        "no active media",
                        "element not found",
                        "node missing",
                    )
                ):
                    return result_text
            return fallback_result or f"No controllable media found on '{domain_keyword}'."
        except Exception as e:
            return f"DOM Media Injection Failure: {e}"

    async def any_media_playing(self) -> str:
        await self._launch()
        self._pages = [p for p in self._pages if not p.is_closed()]
        if self._context:
            for p in self._context.pages:
                if p not in self._pages:
                    self._pages.append(p)
                    p.on("close", self._on_page_close)
        self._pages = await self._sort_tabs_by_visual_position(self._pages)

        check_script = (
            "(() => {"
            "try {"
            "  const els = Array.from(document.querySelectorAll('video, audio'));"
            "  if (!els.length) return false;"
            "  return els.some(e => !e.paused && !e.ended && e.currentTime > 0 && e.readyState > 2);"
            "} catch (e) { return false; }"
            "})()"
        )

        for page in self._pages:
            try:
                res = await page.evaluate(check_script)
                if isinstance(res, bool) and res:
                    return "True"
            except Exception:
                continue
        return "False"


class _SessionRegistry:
    def __init__(self):
        self._sessions: dict[str, _BrowserSession] = {}
        self._active_browser: str = ""
        self._lock = threading.Lock()

    def _get_or_create(self, browser_name: str) -> _BrowserSession:
        with self._lock:
            if browser_name not in self._sessions:
                sess = _BrowserSession(browser_name)
                sess.start()
                self._sessions[browser_name] = sess
            return self._sessions[browser_name]

    def get(self, browser_name: str | None = None) -> _BrowserSession:
        if not browser_name:
            browser_name = self._active_browser or _detect_default_browser()
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        sess = self._get_or_create(browser_name)
        self._active_browser = browser_name
        return sess

    def switch(self, browser_name: str) -> str:
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        self._get_or_create(browser_name)
        self._active_browser = browser_name
        return f"Active browser → {browser_name}"

    def close_one(self, browser_name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(browser_name, None)
        if sess:
            sess.close()
            if self._active_browser == browser_name:
                self._active_browser = ""
            return f"{browser_name} closed."
        return f"No active session for: {browser_name}"

    def close_all(self) -> str:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = ""
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
        return "All browsers closed."

    def list_sessions(self) -> str:
        with self._lock:
            if not self._sessions:
                return "No active browser sessions."
            lines = []
            for name in self._sessions:
                mark = " ◀ active" if name == self._active_browser else ""
                lines.append(f"  • {name}{mark}")
        return "Open browsers:\n" + "\n".join(lines)


_registry = _SessionRegistry()


def get_browser_session(
    browser_name: str | None = None, connect_only: bool = False
) -> _BrowserSession:
    sess = _registry.get(browser_name)
    try:
        sess._connect_only = bool(connect_only)
    except Exception:
        pass
    return sess


def detect_default_browser_name() -> str:
    return _detect_default_browser()


def browser_control(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    browser = params.get("browser", "").lower().strip() or None
    result = "Unknown action."

    # ── Netflix hard-block safety belt ──────────────────────────────────
    _bc_url = (params.get("url") or params.get("value") or "").lower()
    _bc_desc = (
        params.get("description") or params.get("text") or params.get("query") or ""
    ).lower()
    _bc_netflix_context = "netflix.com" in _bc_url or "netflix" in _bc_desc
    if action == "netflix_search_and_play" or (
        _bc_netflix_context
        and action in ("go_to", "new_tab", "search", "smart_click", "click", "type")
    ):
        result = "Netflix browsing/search/card actions are handled by netflix_control only."
        log("Browser", result, player)
        return result

    if action == "switch_browser":
        target = browser or params.get("target", "").lower().strip()
        result = _registry.switch(target) if target else "Specify a browser name."
        log("Browser", result, player)
        return result

    if action == "list_browsers":
        result = _registry.list_sessions()
        log("Browser", result, player)
        return result

    if action == "close_all":
        result = _registry.close_all()
        log("Browser", result, player)
        return result

    try:
        sess = _registry.get(browser)
    except Exception as e:
        result = f"Could not start browser session: {e}"
        log("Browser", result, player)
        return result

    try:
        match action:
            case "list_tabs":
                result = sess.run(sess.list_tabs())
            case "active_tab":
                result = sess.run(sess.get_active_tab_info())
            case "switch_tab":
                input_idx = int(params.get("index", 1))
                target_idx = input_idx - 1 if input_idx > 0 else 0
                result = sess.run(sess.switch_tab(target_idx))
            case "focus_tab":
                fragment = params.get("url") or params.get("fragment", "")
                result = sess.run(sess.focus_tab_by_url(fragment))
            case "next_tab":
                result = sess.run(sess.next_tab())
            case "prev_tab":
                result = sess.run(sess.prev_tab())
            case "new_tab":
                result = sess.run(sess.new_tab(params.get("url", "")))
            case "close_tab":
                fragment = (
                    params.get("target")
                    or params.get("title")
                    or params.get("url")
                    or params.get("fragment")
                    or params.get("name")
                    or ""
                )
                result = sess.run(sess.close_tab(fragment))
            case "go_to":
                result = sess.run(
                    sess.go_to(params.get("url", ""), force_new=params.get("force_new", False))
                )
            case "search":
                result = sess.run(
                    sess.search(params.get("query", ""), params.get("engine", "google"))
                )
            case "reload":
                result = sess.run(sess.reload())
            case "back":
                result = sess.run(sess.back())
            case "forward":
                result = sess.run(sess.forward())
            case "click":
                result = sess.run(sess.click(params.get("selector"), params.get("text")))
            case "smart_click":
                result = sess.run(sess.smart_click(params.get("description", "")))
            case "type":
                result = sess.run(
                    sess.type_text(
                        params.get("selector"),
                        params.get("text", ""),
                        params.get("clear_first", True),
                    )
                )
            case "smart_type":
                result = sess.run(
                    sess.smart_type(params.get("description", ""), params.get("text", ""))
                )
            case "fill_form":
                result = sess.run(sess.fill_form(params.get("fields", {})))
            case "scroll":
                result = sess.run(
                    sess.scroll(
                        direction=params.get("direction", "down"),
                        amount=params.get("amount", "normal"),
                        repeat=int(params.get("repeat", 1)),
                        behavior=params.get("behavior", "smooth"),
                    )
                )
            case "bg_media_eval":
                result = sess.run(
                    sess.evaluate_background_media(
                        params.get("domain", ""), params.get("script", "")
                    )
                )
            case "is_media_playing":
                result = sess.run(sess.any_media_playing())
            case "press":
                result = sess.run(sess.press(params.get("key", "Enter")))
            case "get_text":
                result = sess.run(sess.get_text())
            case "get_url":
                result = sess.run(sess.get_url())
            case "screenshot":
                result = sess.run(sess.screenshot(params.get("path")))
            case "page_snapshot":
                result = sess.run(sess.page_snapshot())
            case "click_ref":
                result = sess.run(sess.click_ref(params.get("ref", params.get("index"))))
            case "tab_to_monitor":
                # Not "move_to_screen": that name already means cast-to-TV below.
                from actions.monitor_move import move_tab_to_monitor

                result = sess.run(move_tab_to_monitor(sess, params.get("screen", "other")))
            case "cast_screen" | "move_to_screen" | "cast_video":
                result = sess.run(sess.cast_screen())
            case "stop_casting" | "disconnect_tv":
                result = sess.run(sess.stop_casting())
            case "close" | "close_browser":
                target = browser or _registry._active_browser
                result = _registry.close_one(target) if target else "No browser specified."
            case _:
                result = f"Unknown browser action: '{action}'"
    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out (60 s)."
    except Exception as e:
        result = f"Browser error ({action}): {e}"

    if action == "page_snapshot":
        from actions.page_refs import summarize

        log("Browser", summarize(result), player)
    else:
        log("Browser", result, player)
    return result
