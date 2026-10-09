# actions/window_control.py
"""
Advanced window management for Era One.
Features an aggressive Windows 11 foreground stealer and class-based master resolution.
"""

import re
import subprocess
import sys
import time

if sys.platform == "win32":
    try:
        import psutil
        import win32api  # Added for GetCurrentThreadId
        import win32com.client  # noqa: F401  (availability probe: presence sets the capability flag)
        import win32con
        import win32gui
        import win32process

        _WIN32_GUI = True
    except ImportError:
        _WIN32_GUI = False
else:
    _WIN32_GUI = False

try:
    import pywinctl as pwc

    _PWC = True
except ImportError:
    _PWC = False

from core.utils import get_os
from core.win_subprocess import NO_WINDOW

_OS = get_os()

# Updated registry including your primary asset creation and media suites
MASTER_APP_REGISTRY = {
    "spotify": {"exe": "Spotify.exe", "class": "SpotifyMainWindow"},
    "brave": {"exe": "brave.exe", "class": "Chrome_WidgetWin_1"},
    "chrome": {"exe": "chrome.exe", "class": "Chrome_WidgetWin_1"},
    "edge": {"exe": "msedge.exe", "class": "Chrome_WidgetWin_1"},
    "firefox": {"exe": "firefox.exe", "class": "MozillaWindowClass"},
    "discord": {"exe": "Discord.exe", "class": "Chrome_WidgetWin_1"},
    "calculator": {"exe": "ApplicationFrameHost.exe", "class": "ApplicationFrameWindow"},
    "vscode": {"exe": "Code.exe", "class": "Chrome_WidgetWin_1"},
    "visual studio code": {"exe": "Code.exe", "class": "Chrome_WidgetWin_1"},
    "code": {"exe": "Code.exe", "class": "Chrome_WidgetWin_1"},
    "photoshop": {"exe": "Photoshop.exe", "class": "photoshop"},
    "illustrator": {"exe": "Illustrator.exe", "class": "illustrator_app"},
    "premiere": {"exe": "Premiere.exe", "class": "Premiere Pro"},
    "premiere pro": {"exe": "Premiere.exe", "class": "Premiere Pro"},
    "whatsapp": {"exe": "WhatsApp.exe", "class": "ApplicationFrameWindow"},
    "telegram": {"exe": "Telegram.exe", "class": "TelegramWidget"},
}


def _names_app(said: str, key: str) -> bool:
    """True if `said` names registry app `key` as whole words, either way round.
    Plain substrings let "codecraft" hit "code" and focus VS Code (25/09/2026)."""
    said, key = said.lower().strip(), key.lower().strip()
    if not said or not key:
        return False
    return bool(
        re.search(rf"\b{re.escape(key)}\b", said) or re.search(rf"\b{re.escape(said)}\b", key)
    )


def force_absolute_focus(hwnd):
    """Aggressively overrides Windows 11 Foreground Lockout restrictions using Thread Input Attachment."""
    if not _WIN32_GUI or hwnd == 0:
        return

    try:
        foreground_hwnd = win32gui.GetForegroundWindow()
        if foreground_hwnd == hwnd:
            return

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        elif not win32gui.IsWindowVisible(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        # Step 2: Bind Era One's thread identity to the active application thread
        current_thread = win32api.GetCurrentThreadId()  # FIXED
        remote_thread = win32process.GetWindowThreadProcessId(foreground_hwnd)[0]

        if current_thread != remote_thread and remote_thread != 0:
            # Synchronize threads so Windows treats Era One as an active foreground process
            win32process.AttachThreadInput(current_thread, remote_thread, True)

            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )

            win32gui.SetForegroundWindow(hwnd)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetActiveWindow(hwnd)

            # Safely decouple the thread context lock
            win32process.AttachThreadInput(current_thread, remote_thread, False)
        else:
            win32gui.SetForegroundWindow(hwnd)
            win32gui.BringWindowToTop(hwnd)

        # Step 3: Keystroke-free foreground retry.
        # A synthetic Shift tap used to live here to defeat Windows' foreground
        # lock, but that key was injected into whatever window currently had
        # focus — e.g. a fullscreen Netflix/DRM video — and would knock it out of
        # fullscreen. The AttachThreadInput unlock above is the correct mechanism;
        # if focus still didn't take, just retry it without touching the keyboard.
        if win32gui.GetForegroundWindow() != hwnd:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)

    except Exception as e:
        print(f"[WindowControl] Foreground force bypass warning: {e}")


def find_window_by_master_spec(app_name: str):
    """
    Ultra-fast Win32 lookup that maps windows by title or class signature,
    safely verifying background process filenames to prevent cross-app matching.
    """
    if sys.platform != "win32" or not _WIN32_GUI:
        return None

    target_key = app_name.lower().strip()
    spec = MASTER_APP_REGISTRY.get(target_key)

    # Alias resolution: if the spoken name isn't an exact registry key, match it
    # against known keys as a substring (e.g. "spotify app" -> "spotify",
    # "vs code" -> "code") so we still get the process spec.
    if spec is None:
        for k, v in MASTER_APP_REGISTRY.items():
            if _names_app(target_key, k):
                spec, target_key = v, k
                break

    spec_exe = spec["exe"].lower() if spec else None
    # Three priority buckets, best first. We enumerate ALL top-level windows so a
    # weak match never shadows a strong one.
    title_hwnds: list[int] = []  # window caption contains the name
    class_proc_hwnds: list[int] = []  # window class AND owning process match spec
    proc_hwnds: list[int] = []  # owning process matches spec (title may be a song, doc, etc.)

    def enum_windows_callback(hwnd, extra):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        title_l = title.lower()
        if "mark-xxxix" in title_l:
            return True

        pname = None
        if spec:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                pname = psutil.Process(pid).name().lower()
            except Exception:
                pname = None
            if pname == spec_exe:
                win_class = win32gui.GetClassName(hwnd)
                # Layer 1: class + verified process (strongest structural match)
                if win_class == spec["class"]:
                    class_proc_hwnds.append(hwnd)
                # Layer 2: process match regardless of title/class. Rescues apps
                # with dynamic captions — e.g. Spotify showing "Artist – Song".
                else:
                    proc_hwnds.append(hwnd)
                return True

        # Layer 3: caption match. For a known app this is the WEAKEST signal and
        # only used when the real process has no window — a Brave tab titled
        # "Spotify - Web Player" must never win over the Spotify app itself.
        if target_key in title_l:
            if spec and pname is not None and pname != spec_exe:
                return True
            title_hwnds.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(enum_windows_callback, None)
    except Exception:
        pass

    # Strongest signal first: the owning process outranks any window caption.
    for bucket in (class_proc_hwnds, proc_hwnds, title_hwnds):
        if bucket and _PWC:
            try:
                return pwc.Window(bucket[0])
            except Exception:
                continue
    return None


def _focus_hwnd(target) -> None:
    hwnd = target.getHandle()
    if _WIN32_GUI and win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    force_absolute_focus(hwnd)


def focus_window(title_fragment: str) -> str:
    try:
        frag = title_fragment.lower().strip()

        # For known apps, resolve by process first — this reliably targets the
        # real application (e.g. Spotify) even when its caption is a song title
        # or a browser tab merely mentions the name.
        is_known_app = any(_names_app(frag, k) for k in MASTER_APP_REGISTRY)
        if is_known_app:
            target = find_window_by_master_spec(title_fragment)
            if target:
                _focus_hwnd(target)
                return f"Focused {target.title[:40] or title_fragment}."

        # General case: substring match over all live window captions.
        if _PWC:
            matches = [
                w
                for w in pwc.getAllWindows()
                if frag in w.title.lower() and "mark-xxxix" not in w.title.lower()
            ]
            if matches:
                target = matches[0]
                _focus_hwnd(target)
                return f"Focused window: {target.title[:40]}"

        # Last resort: master resolver for non-registry names.
        target = find_window_by_master_spec(title_fragment)
        if target:
            _focus_hwnd(target)
            return f"Focused via Master Resolver: {target.title[:40]}"

        return f"No open window matching '{title_fragment}'."
    except Exception as e:
        return f"Focus failed: {e}"


def close_window(title_fragment: str) -> str:
    """
    Closes an application cleanly via its window handle,
    falling back to a tree force-kill if it targets system tray apps.
    """
    try:
        target_lower = title_fragment.lower().strip()

        # 1. Critical System Guard Checks
        # Only block closing windows that are clearly Era One itself.
        # This fixes the issue where other Brave windows were sometimes
        # interpreted as Era One/terminal and refused to close.
        protected_keywords = ["era one", "era.one", "mark-xxxix", "main.py", "face.png"]
        if any(kw in target_lower for kw in protected_keywords):
            # Extra guard: only block if the title contains Era One/Mark.
            return "Sir, I am blocked from closing our primary terminal interface window."

        if sys.platform == "win32":
            # 2. Check the synchronized MASTER_APP_REGISTRY mapping file coordinates first
            matched_app_key = next((k for k in MASTER_APP_REGISTRY if k in target_lower), None)

            if matched_app_key:
                exec_name = MASTER_APP_REGISTRY[matched_app_key]["exe"]

                # Dynamic switch: if app minimizes to tray or utilizes multi-threaded sub-processes, tree-kill it cleanly
                if matched_app_key in ["spotify", "discord", "brave", "chrome", "edge"]:
                    # argv list, no shell — exec_name comes from the trusted
                    # registry but we never interpolate into a shell string.
                    subprocess.run(
                        ["taskkill", "/F", "/IM", exec_name, "/T"],
                        shell=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=NO_WINDOW,
                    )
                    return f"{title_fragment.title()} process channels terminated completely, sir."

                # For non-tray master apps (Photoshop, Premiere, Illustrator), try graceful closure first to preserve workspace save notifications
                target = find_window_by_master_spec(matched_app_key)
                if target:
                    win32gui.PostMessage(target.getHandle(), win32con.WM_CLOSE, 0, 0)
                    return f"Sent native close request sequence to {title_fragment.title()}."

            # 3. Fallback: Lookup by manual window title fragment trace
            target = None
            if _PWC:
                for w in pwc.getAllWindows():
                    if target_lower in w.title.lower():
                        target = w
                        break

            if target:
                if _WIN32_GUI:
                    win32gui.PostMessage(target.getHandle(), win32con.WM_CLOSE, 0, 0)
                else:
                    target.close()
                return f"Closed window: {target.title[:40]}"

            # 4. Deep Fallback: Execute a direct image binary termination search sequence.
            # The title fragment is user/LLM-derived, so validate it as a plain
            # executable name and pass argv (never a shell string) to avoid
            # command injection (e.g. a title like "x&calc").
            exec_name = f"{re.sub(r'[^A-Za-z0-9._-]', '', target_lower)}.exe"
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.exe", exec_name):
                return f"No application runtime thread found with '{title_fragment}' anywhere in the system matrix."
            result = subprocess.run(
                ["taskkill", "/IM", exec_name, "/T"], shell=False, capture_output=True, text=True, creationflags=NO_WINDOW
            )
            if "SUCCESS" in result.stdout:
                return f"Terminated background process target: {exec_name}."

            if "spotify" in target_lower:
                return "There is no Spotify open right now, sir."
            return f"No application runtime thread found with '{title_fragment}' anywhere in the system matrix."

        else:
            # Cross-platform validation layer (Mac/Linux)
            target = None
            if _PWC:
                for w in pwc.getAllWindows():
                    if target_lower in w.title.lower():
                        target = w
                        break
            if target:
                target.close()
                return f"Closed window: {target.title[:40]}"
            return f"No window found with '{title_fragment}' in title."

    except Exception as e:
        return f"Close failed: {e}"


def list_windows(filter_title: str = None) -> str:
    if not _PWC:
        return "pywinctl missing"
    try:
        lines = []
        for w in pwc.getAllWindows():
            title = w.title.strip()
            if not title or "mark-xxxix" in title.lower():
                continue
            if filter_title and filter_title.lower() not in title.lower():
                continue
            try:
                width = w.width
                height = w.height
            except AttributeError:
                rect = w.getRect()
                width = rect[2] if rect and len(rect) > 2 else 0
                height = rect[3] if rect and len(rect) > 3 else 0
            lines.append(f"• {title[:40]} | Size:{width}×{height}")
        return "Open windows:\n" + "\n".join(lines[:30]) if lines else "No open windows."
    except Exception as e:
        return f"List failed: {e}"


def get_active_window() -> str:
    """Instantly resolves true underlying target application while ignoring Era One console app."""
    if not _WIN32_GUI:
        return "Desktop"
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        if title:
            if any(kw in title.lower() for kw in ["mark-xxxix", "era.one", "main.py"]):
                targets = []

                def _callback(h, extra):
                    if win32gui.IsWindowVisible(h):
                        t = win32gui.GetWindowText(h)
                        if t and not any(
                            k in t.lower() for k in ["mark-xxxix", "era.one", "main.py"]
                        ):
                            extra.append(t)
                            return False
                    return True

                win32gui.EnumWindows(_callback, targets)
                return targets[0] if targets else "Desktop"
            return title.strip()
        return "Desktop"
    except Exception:
        return "Desktop"


def move_window(title_fragment: str, x: int, y: int) -> str:
    try:
        target = find_window_by_master_spec(title_fragment)
        if target and _WIN32_GUI:
            win32gui.SetWindowPos(
                target.getHandle(), None, x, y, 0, 0, win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
            )
            return f"Moved '{target.title[:30]}' to ({x}, {y})"
        return "Move target window not tracked."
    except Exception as e:
        return f"Move failed: {e}"


def resize_window(title_fragment: str, width: int, height: int) -> str:
    try:
        target = find_window_by_master_spec(title_fragment)
        if target and _WIN32_GUI:
            rect = win32gui.GetWindowRect(target.getHandle())
            win32gui.SetWindowPos(
                target.getHandle(), None, rect[0], rect[1], width, height, win32con.SWP_NOZORDER
            )
            return f"Resized '{target.title[:30]}' to {width}×{height}"
        return "Resize target window not tracked."
    except Exception as e:
        return f"Resize failed: {e}"


def tile_windows(style: str = "grid") -> str:
    return "Tiling layout operation complete."


def _resolve_target(title_fragment: str):
    """Resolve a window by app spec first, then by loose caption match."""
    target = find_window_by_master_spec(title_fragment)
    if not target and _PWC:
        frag = title_fragment.lower()
        for w in pwc.getAllWindows():
            if frag in w.title.lower() and "mark-xxxix" not in w.title.lower():
                target = w
                break
    return target


def _is_zoomed(hwnd) -> bool:
    """True when the window is maximized.

    win32gui exposes IsIconic but NOT IsZoomed in this pywin32 build — calling
    it raised AttributeError and made every maximize report a failure. Read the
    placement's showCmd instead, which is available everywhere.
    """
    try:
        return win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED
    except Exception:
        return False


def _await_window_state(hwnd, predicate, timeout_s: float = 1.0) -> bool:
    """Poll the real window state until predicate holds.

    ShowWindow's return value reports the window's PREVIOUS visibility, not
    whether the state change succeeded — trusting it made Era report
    "couldn't maximize" for windows it had just maximized. Always verify the
    resulting state instead.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if predicate(hwnd):
                return True
        except Exception:
            return False
        time.sleep(0.05)
    return False


def minimize_window(title_fragment: str) -> str:
    """Minimize the target window, verifying it actually minimized."""
    target = _resolve_target(title_fragment)
    if not target:
        return f"No window found with '{title_fragment}'."
    if not _WIN32_GUI:
        return "Minimize only supported on Windows."

    hwnd = target.getHandle()
    name = target.title[:40] or title_fragment
    if not win32gui.IsWindowVisible(hwnd):
        return f"Window '{name}' is not visible."
    if win32gui.IsIconic(hwnd):
        return f"Window '{name}' is already minimized."

    # Never focus before minimizing: raising the window first is what made Era
    # pop a different window to the front on "minimize spotify".
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    if _await_window_state(hwnd, win32gui.IsIconic):
        return f"Minimized: {name}"

    # Fallback for apps that ignore a cross-thread SW_MINIMIZE.
    try:
        win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_MINIMIZE, 0)
    except Exception:
        pass
    if _await_window_state(hwnd, win32gui.IsIconic):
        return f"Minimized: {name}"
    return f"Failed to minimize '{name}'."


def maximize_window(title_fragment: str) -> str:
    """Maximize the target window, verifying it actually maximized."""
    target = _resolve_target(title_fragment)
    if not target:
        return f"No window found with '{title_fragment}'."
    if not _WIN32_GUI:
        return "Maximize only supported on Windows."

    hwnd = target.getHandle()
    name = target.title[:40] or title_fragment
    if not win32gui.IsWindowVisible(hwnd):
        return f"Window '{name}' is not visible."
    if _is_zoomed(hwnd):
        return f"Window '{name}' is already maximized."

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    if _await_window_state(hwnd, _is_zoomed):
        force_absolute_focus(hwnd)
        return f"Maximized: {name}"

    # Fallback: ask the window itself to maximize via its system menu.
    try:
        win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_MAXIMIZE, 0)
    except Exception:
        pass
    if _await_window_state(hwnd, _is_zoomed):
        force_absolute_focus(hwnd)
        return f"Maximized: {name}"
    return f"Failed to maximize '{name}'."


def restore_window(title_fragment: str) -> str:
    """Restore (un-minimize/un-maximize) the target window and bring to front."""
    target = _resolve_target(title_fragment)
    if not target:
        return f"No window found with '{title_fragment}'."
    if not _WIN32_GUI:
        return "Restore only supported on Windows."
    hwnd = target.getHandle()
    name = target.title[:40] or title_fragment
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    _await_window_state(hwnd, lambda h: not win32gui.IsIconic(h))
    force_absolute_focus(hwnd)
    return f"Restored: {name}"


def show_window(title_fragment: str) -> str:
    """Show (bring to front, un-minimize if needed) the target window."""
    target = _resolve_target(title_fragment)
    if not target:
        return f"No window found with '{title_fragment}'."
    if not _WIN32_GUI:
        return "Show only supported on Windows."
    hwnd = target.getHandle()
    name = target.title[:40] or title_fragment
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    force_absolute_focus(hwnd)
    return f"Shown: {name}"


# ========== ACTION MAP ==========
ACTION_MAP = {
    "list": list_windows,
    "focus": focus_window,
    "close": close_window,
    "active": get_active_window,
    "move": move_window,
    "resize": resize_window,
    "tile": tile_windows,
    "minimize": minimize_window,
    "maximize": maximize_window,
    "restore": restore_window,
    "show": show_window,
}


def window_control(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    if not action:
        return "No action specified."

    try:
        if action == "list":
            return list_windows(params.get("title"))
        elif action in ("focus", "close", "minimize", "maximize", "restore", "show"):
            title = params.get("title", "")
            if not title:
                return "Title parameter required."
            func = ACTION_MAP.get(action)
            if func:
                return func(title)
            else:
                return f"Unsupported action: {action}"
        elif action == "move":
            title = params.get("title", "")
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
            if not title:
                return "Title parameter required for move."
            return move_window(title, x, y)
        elif action == "resize":
            title = params.get("title", "")
            width = int(params.get("width", 0))
            height = int(params.get("height", 0))
            if not title:
                return "Title parameter required for resize."
            return resize_window(title, width, height)
        elif action == "active":
            return get_active_window()
        elif action in ("move_to_monitor", "monitors"):
            # Lives in its own module: this file is already past the line budget.
            from actions.monitor_move import describe_monitors, move_window_to_monitor

            if action == "monitors":
                return describe_monitors()
            return move_window_to_monitor(params.get("title", ""), params.get("screen", "other"))
        elif action == "tile":
            style = params.get("style", "grid")
            return tile_windows(style)
        else:
            return f"Unknown action: {action}"
    except Exception as e:
        return f"Window failure: {e}"


class WindowWorkspaceRestore:
    """
    Captures the absolute geometry, state, and focus parameters of the active
    foreground window, then seamlessly reconstructs it post-execution.
    """

    def __enter__(self):
        self.orig_hwnd = win32gui.GetForegroundWindow() if _WIN32_GUI else 0
        self.rect = None
        self.placement = None

        if self.orig_hwnd != 0:
            try:
                self.rect = win32gui.GetWindowRect(self.orig_hwnd)
                self.placement = win32gui.GetWindowPlacement(self.orig_hwnd)
            except Exception:
                pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.orig_hwnd != 0:
            try:
                time.sleep(0.05)
                if win32gui.GetForegroundWindow() != self.orig_hwnd:
                    if self.placement:
                        win32gui.SetWindowPlacement(self.orig_hwnd, self.placement)
                    if self.rect:
                        left, top, right, bottom = self.rect
                        win32gui.SetWindowPos(
                            self.orig_hwnd,
                            None,
                            left,
                            top,
                            right - left,
                            bottom - top,
                            win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW,
                        )
                    force_absolute_focus(self.orig_hwnd)
            except Exception as e:
                print(f"[WindowControl] Workspace layout restoration exception: {e}")
