"""Guard against 0-byte junk files appearing in Era's project root.

Every extensionless file that has ever landed directly in the project root has
turned out to be a 0-byte artifact of a stray write — a value mistaken for a
path: a type-hint fragment (``dict[str``, ``tuple[bytes``), a builtin type name
(``bool``, ``str``, ``None``, ``Path``), a repr start (``{``, ``{'``) or a bare
literal (``200``, ``33``, ``run``, ``Claude``). The root holds **no** legitimate
extensionless file (every real one has an extension: ``.py``, ``.md``, ``.yaml``
…), so any create-write of an extensionless name whose parent is the root is,
by construction, one of these accidents.

This module intercepts that write in-process. When it fires it:
  1. records a full stack trace to ``runtime/diagnosis/root_file_guard.log`` so
     the true origin (the exact ``open()``/``write_text`` call site) can be
     found and fixed at the source, and
  2. redirects the write into ``runtime/diagnosis/quarantine/`` so the root
     stays clean and the caller still succeeds (never crashes a live session).

``install()`` is idempotent and best-effort: the guard can never itself break a
legitimate file operation — any error in its own logic falls through to the
original call.
"""

from __future__ import annotations

import builtins
import io
import os
import traceback
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # EraOne/
_LOG = _ROOT / "runtime" / "diagnosis" / "root_file_guard.log"
_QUARANTINE = _ROOT / "runtime" / "diagnosis" / "quarantine"

_installed = False
_orig_builtins_open = None
_orig_io_open = None
_orig_path_open = None
_orig_path_write_text = None
_orig_path_write_bytes = None

# Snapshot of the five names as they stood when this module was first imported,
# used only as a last-resort original if ``_orig_*`` is somehow unset while the
# live name already holds one of our wrappers.
_TRUE_BUILTINS_OPEN = builtins.open
_TRUE_IO_OPEN = io.open
_TRUE_PATH_OPEN = Path.open
_TRUE_PATH_WRITE_TEXT = Path.write_text
_TRUE_PATH_WRITE_BYTES = Path.write_bytes


def _is_junk_target(path: str | os.PathLike) -> bool:
    """True when ``path`` names an extensionless file directly in the project
    root — the exact shape of the junk files and nothing a real writer targets."""
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str):
            raw = os.fsdecode(raw)
        if any(0xD800 <= ord(c) <= 0xDFFF for c in raw):
            return False  # undecodable byte paths are not evidence of junk
        # Fast reject (no syscall): anything with an extension, a dotfile, or a
        # path that dives into a subdir is not our junk. This is the common case
        # for essentially every real open(), so the resolve() below almost never
        # runs.
        name = os.path.basename(raw.rstrip("/\\"))
        if not name or "." in name or name.startswith("."):
            return False
        # Extensionless basename: now confirm its parent really is the root.
        p = Path(raw)
        resolved = (Path.cwd() / p if not p.is_absolute() else p).resolve()
        return resolved.parent == _ROOT and resolved.suffix == ""
    except Exception:
        return False


def _is_write_mode(mode: str) -> bool:
    return any(c in mode for c in ("w", "a", "x", "+"))


def _record(target):
    """Log the stack trace behind this creation and return a quarantine path.

    When the quarantine directory cannot be created the ORIGINAL target comes
    back unchanged: a stray 0-byte file in the root beats turning the caller's
    write into a FileNotFoundError.
    """
    try:
        _QUARANTINE.mkdir(parents=True, exist_ok=True)
    except Exception:
        return target
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        stack = "".join(traceback.format_stack()[:-2])  # drop the guard frames
        with _orig_io_open(_LOG, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(
                f"\n===== {datetime.now().isoformat(timespec='seconds')} "
                f"blocked root junk file: {target!r} =====\n{stack}"
            )
    except Exception:
        pass
    safe_name = Path(os.fspath(target)).name or "unnamed"
    return _QUARANTINE / safe_name


def _guarded_open(file, mode="r", *args, **kwargs):
    try:
        if isinstance(mode, str) and _is_write_mode(mode) and _is_junk_target(file):
            file = _record(file)
    except Exception:
        pass  # never let the guard break a real open
    return _orig_io_open(file, mode, *args, **kwargs)


def _guarded_path_open(self, mode="r", *args, **kwargs):
    try:
        if isinstance(mode, str) and _is_write_mode(mode) and _is_junk_target(self):
            self = Path(_record(self))
    except Exception:
        pass
    return _orig_path_open(self, mode, *args, **kwargs)


def _guarded_write_text(self, *args, **kwargs):
    try:
        if _is_junk_target(self):
            self = Path(_record(self))
    except Exception:
        pass
    return _orig_path_write_text(self, *args, **kwargs)


def _guarded_write_bytes(self, *args, **kwargs):
    try:
        if _is_junk_target(self):
            self = Path(_record(self))
    except Exception:
        pass
    return _orig_path_write_bytes(self, *args, **kwargs)


def _capture(current, guarded, stored, pristine):
    """Return the true original for one patched name.

    ``current`` is whatever the name holds right now. When it is already this
    module's own wrapper - a second ``install()`` after ``_installed`` was forced
    back to ``False`` - capturing it would make the guard call itself forever, so
    the original stored by the first install is kept instead. Anything else is a
    genuine outer implementation (unpatched, or another library's wrapper) and is
    captured so the guard chains onto it.
    """
    if current is not guarded:
        return current
    return stored if stored is not None else pristine


def install() -> bool:
    """Activate the guard. Idempotent; safe to call from every entry point.

    Idempotent per name, not merely per ``_installed`` flag: a caller that resets
    the flag (or a second copy of this module) still cannot make the guard wrap
    its own wrappers.
    """
    global _installed, _orig_builtins_open, _orig_io_open
    global _orig_path_open, _orig_path_write_text, _orig_path_write_bytes
    if _installed:
        return True
    _orig_builtins_open = _capture(
        builtins.open, _guarded_open, _orig_builtins_open, _TRUE_BUILTINS_OPEN
    )
    _orig_io_open = _capture(io.open, _guarded_open, _orig_io_open, _TRUE_IO_OPEN)
    _orig_path_open = _capture(Path.open, _guarded_path_open, _orig_path_open, _TRUE_PATH_OPEN)
    _orig_path_write_text = _capture(
        Path.write_text, _guarded_write_text, _orig_path_write_text, _TRUE_PATH_WRITE_TEXT
    )
    _orig_path_write_bytes = _capture(
        Path.write_bytes,
        _guarded_write_bytes,
        _orig_path_write_bytes,
        _TRUE_PATH_WRITE_BYTES,
    )
    # builtins.open / io.open catch bare `open(...)`. Path.* are patched
    # directly because pathlib does not necessarily route through io.open across
    # Python versions (it does not on 3.14).
    builtins.open = _guarded_open  # type: ignore[assignment]
    io.open = _guarded_open  # type: ignore[assignment]
    Path.open = _guarded_path_open  # type: ignore[assignment]
    Path.write_text = _guarded_write_text  # type: ignore[assignment]
    Path.write_bytes = _guarded_write_bytes  # type: ignore[assignment]
    _installed = True
    return True


def uninstall() -> bool:
    """Put the five patched names back. Returns False if nothing was installed.

    Only names still holding one of our wrappers are restored, so a patcher that
    layered on top of the guard is left in place. Tests use this instead of
    reassigning the names by hand, which is how a wrapper once got captured as an
    "original".
    """
    global _installed
    if not _installed:
        return False
    if builtins.open is _guarded_open and _orig_builtins_open is not None:
        builtins.open = _orig_builtins_open  # type: ignore[assignment]
    if io.open is _guarded_open and _orig_io_open is not None:
        io.open = _orig_io_open  # type: ignore[assignment]
    if Path.open is _guarded_path_open and _orig_path_open is not None:
        Path.open = _orig_path_open  # type: ignore[assignment]
    if Path.write_text is _guarded_write_text and _orig_path_write_text is not None:
        Path.write_text = _orig_path_write_text  # type: ignore[assignment]
    if Path.write_bytes is _guarded_write_bytes and _orig_path_write_bytes is not None:
        Path.write_bytes = _orig_path_write_bytes  # type: ignore[assignment]
    _installed = False
    return True
