# core/process_awareness.py
"""Running-process awareness for Era One."""

from __future__ import annotations

import time
from dataclasses import dataclass

try:
    import psutil

    _PSUTIL = True
except Exception:
    psutil = None
    _PSUTIL = False


APP_ALIASES: dict[str, tuple[str, ...]] = {
    "spotify": ("spotify.exe", "spotify"),
    "brave": ("brave.exe", "brave", "brave-browser"),
    "chrome": ("chrome.exe", "chrome", "google-chrome"),
    "edge": ("msedge.exe", "microsoft-edge", "msedge"),
    "firefox": ("firefox.exe", "firefox"),
    "opera": ("opera.exe", "opera"),
    "discord": ("discord.exe", "discord"),
    "steam": ("steam.exe", "steam"),
    "epic": ("epicgameslauncher.exe", "epicgameslauncher"),
    "vscode": ("code.exe", "code"),
    "telegram": ("telegram.exe", "telegram"),
    "whatsapp": ("whatsapp.exe", "whatsapp"),
    "python": ("python.exe", "python", "python3"),
}


@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe: str = ""
    title: str = ""


_CACHE: tuple[float, list[ProcessInfo]] = (0.0, [])
_CACHE_TTL = 2.0


def _norm(s: str | None) -> str:
    return (s or "").lower().strip()


def list_processes(refresh: bool = False) -> list[ProcessInfo]:
    global _CACHE
    now = time.time()
    if not refresh and now - _CACHE[0] < _CACHE_TTL:
        return list(_CACHE[1])

    items: list[ProcessInfo] = []
    if not _PSUTIL:
        return items

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            if not name:
                continue
            items.append(
                ProcessInfo(
                    pid=int(info.get("pid") or 0),
                    name=name,
                    exe=info.get("exe") or "",
                )
            )
        except Exception:
            continue

    _CACHE = (now, items)
    return list(items)


def is_running(app_name: str) -> bool:
    target = _norm(app_name)
    aliases = APP_ALIASES.get(target, (target,))
    aliases = tuple(_norm(a) for a in aliases)

    for p in list_processes():
        n = _norm(p.name)
        e = _norm(p.exe)
        if any(a == n or a in n or a in e for a in aliases):
            return True
    return False


def find_app(app_name: str) -> list[ProcessInfo]:
    target = _norm(app_name)
    aliases = APP_ALIASES.get(target, (target,))
    aliases = tuple(_norm(a) for a in aliases)
    found = []
    for p in list_processes():
        n = _norm(p.name)
        e = _norm(p.exe)
        if any(a == n or a in n or a in e for a in aliases):
            found.append(p)
    return found


def running_app_names(limit: int = 80) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in list_processes():
        key = p.name.lower()
        if key not in seen:
            seen.add(key)
            out.append(p.name)
        if len(out) >= limit:
            break
    return out


def media_state() -> dict:
    return {
        "spotify_running": is_running("spotify"),
        "brave_running": is_running("brave"),
        "chrome_running": is_running("chrome"),
        "edge_running": is_running("edge"),
        "firefox_running": is_running("firefox"),
    }


def snapshot() -> dict:
    return {
        "psutil_available": _PSUTIL,
        "running_apps": running_app_names(),
        **media_state(),
    }


def format_running_apps(limit: int = 40) -> str:
    names = running_app_names(limit=limit)
    if not names:
        return "No process data available."
    return "\n".join(f"- {name}" for name in names)
