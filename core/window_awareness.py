# core/window_awareness.py
"""Active-window awareness for Era One."""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass

from core.intent_memory import remember_action

try:
    import pywinctl as pwc

    _PWC = True
except Exception:
    pwc = None
    _PWC = False

if sys.platform == "win32":
    try:
        import psutil
        import win32gui
        import win32process

        _WIN32 = True
    except Exception:
        _WIN32 = False
else:
    _WIN32 = False


@dataclass
class WindowInfo:
    title: str = ""
    app: str = ""
    pid: int | None = None
    exe: str = ""


def _from_win32() -> WindowInfo | None:
    if not _WIN32:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = ""
        app = ""
        try:
            proc = psutil.Process(pid)
            exe = proc.name()
            app = exe.rsplit(".", 1)[0]
        except Exception:
            pass
        return WindowInfo(title=title, app=app, pid=pid, exe=exe)
    except Exception:
        return None


def _from_pywinctl() -> WindowInfo | None:
    if not _PWC:
        return None
    try:
        w = pwc.getActiveWindow()
        if not w:
            return None
        return WindowInfo(title=w.title or "", app="", pid=None, exe="")
    except Exception:
        return None


def get_active_window() -> dict:
    info = _from_win32() or _from_pywinctl() or WindowInfo(title="Desktop")
    if info.title:
        remember_action(window=info.title, app=info.app or None)
    return asdict(info)


def get_active_title(default: str = "Desktop") -> str:
    return get_active_window().get("title") or default


def list_windows(limit: int = 50) -> list[dict]:
    out = []
    if _PWC:
        try:
            for w in pwc.getAllWindows()[:limit]:
                title = (w.title or "").strip()
                if title:
                    out.append({"title": title})
        except Exception:
            pass
    return out


def format_context() -> str:
    active = get_active_window()
    lines = [
        f"Active window: {active.get('title') or 'Desktop'}",
        f"Active app: {active.get('app') or 'unknown'}",
    ]
    wins = list_windows(limit=15)
    if wins:
        lines.append("Open windows:")
        lines.extend(f"- {w['title'][:100]}" for w in wins)
    return "\n".join(lines)
