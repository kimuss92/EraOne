"""Inspect launch targets; opening an app must never select its maintenance link."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def normalized_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def unsafe_launch(value: str) -> bool:
    text = normalized_name(value)
    return bool(
        re.search(
            r"\b(?:unins\w*|uninstall\w*|desinstall\w*|deinstall\w*|remove|"
            r"setup|installer|install|repair|maintenance|modify|msiexec)\b",
            text,
        )
    ) or bool(re.search(r"(?:^|\s)[/-](?:uninstall|remove|repair|modify)(?:\s|$)", value, re.I))


def safe_executable(target: str, arguments: str = "") -> bool:
    """Accept application binaries, excluding maintenance and shell interpreters."""
    path = Path(target)
    return (
        path.suffix.casefold() == ".exe"
        and path.is_file()
        and not unsafe_launch(path.name)
        and not unsafe_launch(arguments)
        and path.stem.casefold()
        not in {"cmd", "powershell", "pwsh", "wscript", "cscript", "rundll32", "regsvr32"}
    )


def shortcut_score(name: str, query: str) -> int:
    """Prefer exact app names; never treat substring collisions as a match."""
    if unsafe_launch(name):
        return 0
    candidate, wanted = normalized_name(name), normalized_name(query)
    if candidate == wanted:
        return 100
    if candidate == wanted + " desktop" or candidate == wanted + " browser":
        return 90
    if wanted and set(wanted.split()).issubset(candidate.split()):
        return 50
    return 0


def inspect_shortcut(path: str) -> tuple[str, str] | None:
    """Read an existing .lnk using Windows COM, without executing it."""
    import pythoncom
    from win32com.client import Dispatch

    initialized = False
    shortcut = None
    try:
        pythoncom.CoInitialize()
        initialized = True
        shortcut = Dispatch("WScript.Shell").CreateShortcut(str(path))
        target, arguments = str(shortcut.TargetPath), str(shortcut.Arguments)
        if not unsafe_launch(Path(path).stem) and safe_executable(target, arguments):
            return target, arguments
        return None
    except Exception:
        return None
    finally:
        # Release COM proxies while their apartment is still initialized.
        shortcut = None
        if initialized:
            pythoncom.CoUninitialize()
