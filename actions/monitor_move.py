# actions/monitor_move.py
"""
Move a window to another connected monitor ("move this window to screen 2").

Lives outside window_control.py (already past the 500-line budget). The
browser tab variant (browser_control action="tab_to_monitor") reuses
move_hwnd_to_monitor so both paths share one geometry + verification routine.

Screen numbers follow Windows' own display numbering (the N in \\\\.\\DISPLAYN),
which is what Settings > Display shows, so "screen 2" means what the user sees.
"""

import ctypes
import re
import sys
import time

if sys.platform == "win32":
    try:
        import win32api
        import win32con
        import win32gui

        _WIN32 = True
    except ImportError:
        _WIN32 = False
else:
    _WIN32 = False

# Titles that identify Era's own windows — never the target of "this window".
_ERA_TITLE_KEYWORDS = ("mark-xxxix", "era.one", "main.py")

# Words the model may pass as the title when the user says "this window".
_THIS_WINDOW_WORDS = {"", "this", "current", "active", "this window", "foreground", "it"}

_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "first": 1, "second": 2, "third": 3}

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2. Without it, a DPI-unaware process
# gets virtualized coordinates and a window lands off-target on mixed-scale setups.
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


class _PerMonitorDpi:
    """Thread-scoped per-monitor DPI awareness for the duration of a move."""

    def __enter__(self):
        self._prev = None
        try:
            fn = ctypes.windll.user32.SetThreadDpiAwarenessContext
            fn.restype = ctypes.c_void_p
            fn.argtypes = [ctypes.c_void_p]
            self._prev = fn(_PER_MONITOR_AWARE_V2)
        except Exception:
            self._prev = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._prev:
            try:
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(self._prev))
            except Exception:
                pass


def list_monitors() -> list[dict]:
    """All connected monitors, ordered by Windows display number.

    Each entry: number (1-based, what the user calls "screen N"), handle,
    rect/work (left, top, right, bottom), primary, device.
    """
    if not _WIN32:
        return []
    monitors = []
    with _PerMonitorDpi():
        for hmon, _hdc, _rect in win32api.EnumDisplayMonitors(None, None):
            info = win32api.GetMonitorInfo(hmon)
            device = info.get("Device", "")
            m = re.search(r"DISPLAY(\d+)", device, re.IGNORECASE)
            monitors.append(
                {
                    "handle": int(hmon),
                    "rect": tuple(info["Monitor"]),
                    "work": tuple(info["Work"]),
                    "primary": bool(info.get("Flags", 0) & 1),
                    "device": device,
                    "_devnum": int(m.group(1)) if m else 10_000,
                }
            )
    monitors.sort(key=lambda d: (d["_devnum"], d["rect"][0], d["rect"][1]))
    for i, mon in enumerate(monitors, start=1):
        mon["number"] = i
        mon.pop("_devnum", None)
    return monitors


def describe_monitors() -> str:
    mons = list_monitors()
    if not mons:
        return "No monitors detected."
    parts = []
    for m in mons:
        left, top, right, bottom = m["rect"]
        tag = " (primary)" if m["primary"] else ""
        parts.append(f"Screen {m['number']}{tag}: {right - left}x{bottom - top} at ({left}, {top})")
    return f"{len(mons)} screen(s) — " + "; ".join(parts)


def _monitor_of_hwnd(hwnd, monitors: list[dict]) -> dict | None:
    try:
        hmon = int(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST))
    except Exception:
        return None
    return next((m for m in monitors if m["handle"] == hmon), None)


def resolve_target_monitor(screen, current: dict | None, monitors: list[dict]):
    """Map a spoken screen reference to a monitor. Returns (monitor, error)."""
    if not monitors:
        return None, "No monitors detected."
    if len(monitors) == 1:
        return None, "Only one screen is connected."

    raw = str(screen if screen is not None else "").strip().lower()
    raw = re.sub(r"^(to\s+)?(the\s+)?", "", raw)
    raw = re.sub(r"^(screen|monitor|display)\s*", "", raw).strip()
    raw = re.sub(r"\s*(screen|monitor|display)$", "", raw).strip()
    cur_idx = monitors.index(current) if current in monitors else 0

    if raw in ("", "other", "next", "another", "the other", "other one"):
        # "other" with two screens is unambiguous; with more it cycles forward.
        return monitors[(cur_idx + 1) % len(monitors)], None
    if raw in ("previous", "prev", "last"):
        return monitors[(cur_idx - 1) % len(monitors)], None
    if raw in ("primary", "main"):
        return next((m for m in monitors if m["primary"]), monitors[0]), None
    if raw in ("left", "leftmost", "right", "rightmost"):
        ordered = sorted(monitors, key=lambda m: m["rect"][0])
        return (ordered[0] if raw.startswith("left") else ordered[-1]), None

    num = _WORD_NUMBERS.get(raw)
    if num is None:
        m = re.search(r"\d+", raw)
        num = int(m.group(0)) if m else None
    if num is None:
        return None, f"I don't know which screen '{screen}' is."
    if not 1 <= num <= len(monitors):
        return None, f"There is no screen {num} — {len(monitors)} screen(s) connected."
    return monitors[num - 1], None


def _is_era_window(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in _ERA_TITLE_KEYWORDS)


def foreground_target_hwnd() -> int | None:
    """The window the user means by "this window": the foreground window,
    skipping Era's own UI (topmost non-Era visible window in z-order)."""
    if not _WIN32:
        return None
    hwnd = win32gui.GetForegroundWindow()
    if hwnd and win32gui.GetWindowText(hwnd) and not _is_era_window(win32gui.GetWindowText(hwnd)):
        return hwnd

    found = []

    def _cb(h, acc):
        try:
            if (
                win32gui.IsWindowVisible(h)
                and not win32gui.IsIconic(h)
                and win32gui.GetParent(h) == 0
                and win32gui.GetWindowText(h)
                and not _is_era_window(win32gui.GetWindowText(h))
                and win32gui.GetWindowText(h) not in ("Program Manager",)
            ):
                acc.append(h)
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, found)
    except Exception:
        pass  # EnumWindows raises when the callback stops enumeration early
    return found[0] if found else None


def _is_maximized(hwnd) -> bool:
    try:
        return win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED
    except Exception:
        return False


def _target_rect(win_rect, src_work, dst_work):
    """Place the window at the same relative spot on the target work area,
    shrinking it if it wouldn't fit."""
    left, top, right, bottom = win_rect
    w, h = right - left, bottom - top
    sl, st, sr, sb = src_work
    dl, dt, dr, db = dst_work
    dw, dh = dr - dl, db - dt
    w, h = min(w, dw), min(h, dh)

    def _frac(pos, start, span, size):
        free = span - size
        return 0.0 if free <= 0 else min(max((pos - start) / free, 0.0), 1.0)

    fx = _frac(left, sl, sr - sl, right - left)
    fy = _frac(top, st, sb - st, bottom - top)
    x = dl + int(round(fx * (dw - w)))
    y = dt + int(round(fy * (dh - h)))
    return x, y, w, h


def _await(predicate, timeout_s: float = 1.5) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            return False
        time.sleep(0.05)
    return False


def move_hwnd_to_monitor(hwnd, screen) -> str:
    """Move a window to the requested monitor, keeping its maximized state.

    Verifies the result by asking Windows which monitor the window is on
    afterwards — SetWindowPos succeeding does not mean the window moved
    (some apps snap back or ignore cross-process moves)."""
    if not _WIN32:
        return "Moving windows between screens is only supported on Windows."
    if not hwnd or not win32gui.IsWindow(hwnd):
        return "No window to move."

    name = (win32gui.GetWindowText(hwnd) or "window")[:40]
    monitors = list_monitors()
    current = _monitor_of_hwnd(hwnd, monitors)
    target, err = resolve_target_monitor(screen, current, monitors)
    if err:
        return err
    if current and target["handle"] == current["handle"]:
        return f"'{name}' is already on screen {target['number']}."

    with _PerMonitorDpi():
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            _await(lambda: not win32gui.IsIconic(hwnd))

        was_max = _is_maximized(hwnd)
        if was_max:
            # SetWindowPos on a maximized window is unreliable; restore first,
            # move, then re-maximize so it fills the new screen.
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            _await(lambda: not _is_maximized(hwnd))

        src_work = current["work"] if current else monitors[0]["work"]
        x, y, w, h = _target_rect(win32gui.GetWindowRect(hwnd), src_work, target["work"])
        win32gui.SetWindowPos(
            hwnd, None, x, y, w, h, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        )
        # A move across a DPI boundary triggers WM_DPICHANGED, and the app may
        # resize itself; one repeat lands it where we asked.
        if (_monitor_of_hwnd(hwnd, monitors) or {}).get("handle") != target["handle"]:
            win32gui.SetWindowPos(
                hwnd, None, x, y, w, h, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
            )

        if was_max:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            _await(lambda: _is_maximized(hwnd))

    moved = _await(
        lambda: (_monitor_of_hwnd(hwnd, monitors) or {}).get("handle") == target["handle"]
    )
    if moved:
        return f"Moved '{name}' to screen {target['number']}."
    return f"Tried to move '{name}' to screen {target['number']}, but it stayed where it was."


def move_window_to_monitor(title_fragment: str = "", screen="other") -> str:
    """window_control action="move_to_monitor" entry point."""
    if not _WIN32:
        return "Moving windows between screens is only supported on Windows."
    frag = (title_fragment or "").strip()
    if frag.lower() in _THIS_WINDOW_WORDS:
        hwnd = foreground_target_hwnd()
        if not hwnd:
            return "I couldn't tell which window is active."
    else:
        from actions.window_control import _resolve_target

        target = _resolve_target(frag)
        if not target:
            return f"No window found with '{frag}'."
        hwnd = target.getHandle()
    return move_hwnd_to_monitor(hwnd, screen)


# ── Browser tab → other monitor (browser_control action="tab_to_monitor") ──


def _browser_hwnds(exe_names) -> list[int]:
    """Visible top-level windows owned by the browser's processes."""
    import psutil
    import win32process

    names = {n.lower() for n in exe_names}
    out = []

    def _cb(h, acc):
        try:
            if (
                win32gui.IsWindowVisible(h)
                and win32gui.GetParent(h) == 0
                and win32gui.GetWindowText(h)
            ):
                _, pid = win32process.GetWindowThreadProcessId(h)
                if psutil.Process(pid).name().lower() in names:
                    acc.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, out)
    return out


async def _window_id(page) -> int | None:
    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)
        tid = (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        return (await cdp.send("Browser.getWindowForTarget", {"targetId": tid}))["windowId"]
    except Exception:
        return None
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass


async def move_tab_to_monitor(sess, screen="other") -> str:
    """Move the active browser tab to another monitor.

    A tab that is alone in its window just moves with its window (no reload).
    A tab that shares its window is reopened in a new window via CDP
    Target.createTarget(newWindow) and the original tab closed — Chromium
    exposes no "detach tab" over CDP, so the page reloads. The original is
    closed with run_before_unload so a page holding unsaved input can refuse,
    in which case it stays open and the user is told.
    """
    import asyncio

    if not _WIN32:
        return "Moving tabs between screens is only supported on Windows."

    page = await sess._current_page()
    url = page.url or ""
    try:
        title = (await page.title()) or url
    except Exception:
        title = url
    if "netflix.com" in url.lower():
        # netflix_control owns that tab; a reload would drop playback position.
        return "Netflix can't be split into its own window — move the whole browser window instead."

    exe_names = sess._browser_exe_names()
    wid = await _window_id(page)
    siblings = 0
    if wid is not None:
        for other in list(sess._pages):
            if other is page or other.is_closed():
                continue
            if await _window_id(other) == wid:
                siblings += 1
                break

    if wid is not None and siblings == 0:
        # Alone in its window: move the window itself, nothing reloads.
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await asyncio.sleep(0.2)
        wanted = title[:40].lower()
        hwnd = next(
            (
                h
                for h in _browser_hwnds(exe_names)
                if win32gui.GetWindowText(h).lower().startswith(wanted)
            ),
            None,
        )
        if not hwnd:
            return f"I couldn't find the window holding '{title[:40]}'."
        return move_hwnd_to_monitor(hwnd, screen)

    if not url or url.startswith(("chrome://", "brave://", "edge://", "about:")):
        return "This tab can't be reopened in a new window."

    before = set(_browser_hwnds(exe_names))
    cdp = await page.context.new_cdp_session(page)
    try:
        await cdp.send("Target.createTarget", {"url": url, "newWindow": True})
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass

    new_hwnd = None
    for _ in range(50):  # up to ~5 s for the window to appear
        await asyncio.sleep(0.1)
        fresh = [h for h in _browser_hwnds(exe_names) if h not in before]
        if fresh:
            new_hwnd = fresh[0]
            break
    if not new_hwnd:
        return "The new browser window didn't appear, so the tab was left where it was."

    result = move_hwnd_to_monitor(new_hwnd, screen)

    try:
        await page.close(run_before_unload=True)
        await asyncio.sleep(0.5)
    except Exception:
        pass
    if not page.is_closed():
        return result + " The original tab stayed open because it may have unsaved changes."
    return result + " (The tab reloaded in its new window.)"
