"""Tee Era's console output into rotating log files.

Era logs mostly through print(); with no visible console (normal launch) that
output would simply vanish. install() wraps sys.stdout/sys.stderr so every line
still reaches the console when there is one (the Debug launcher) AND is written
to:

  logs/eraone.log        every line
  logs/eraone-error.log  warnings and errors only

Format:  2026-09-26 03:12:01.123 | INFO    | era/TOOL     | message

Secrets are masked before anything touches disk (API-key, bearer and bot-token
shapes). Rotation is size-based and never raises: if the file is held open by a
reader (a tail in the Debug console blocks a rename on Windows), rotation is
simply retried later. No `logging` module here on purpose — a handler error
writes to stderr, which is this tee, which would recurse.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5

_SECRETS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "AIza***"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}"), "sk-***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh*_***"),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}"), "xox*-***"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}"), "<bot-token>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1***"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|token|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"
        ),
        r"\1***",
    ),
]
_TAG_RE = re.compile(r"^\s*\[([^\]\n]{1,24})\](?:\s*\[([^\]\n]{1,24})\])?")
_ERROR_RE = re.compile(r"\b(ERROR|ERR|CRITICAL|FATAL|Traceback|Exception)\b|❌")
_WARN_RE = re.compile(r"\b(WARN|WARNING)\b|⚠")


def mask(text: str) -> str:
    for rx, repl in _SECRETS:
        text = rx.sub(repl, text)
    return text


def classify(line: str, is_err: bool) -> tuple[str, str]:
    """(level, component) for one line of console output."""
    m = _TAG_RE.match(line)
    component = "/".join(g for g in (m.group(1), m.group(2)) if g) if m else ""
    if _ERROR_RE.search(line):
        level = "ERROR"
    elif _WARN_RE.search(line):
        level = "WARNING"
    else:
        level = "ERROR" if is_err and "Traceback" in line else ("WARNING" if is_err else "INFO")
    return level, (component or ("stderr" if is_err else "stdout"))[:24]


class RotatingWriter:
    def __init__(self, path: Path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0
        self._next_try = 0.0

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0

    def _rotate(self) -> None:
        if time.monotonic() < self._next_try:
            return
        try:
            self._fh.close()
            for i in range(self.backups - 1, 0, -1):
                src = self.path.with_name(f"{self.path.name}.{i}")
                if src.exists():
                    os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            self._next_try = time.monotonic() + 60  # held open by a reader; later
        finally:
            self._open()

    def write(self, text: str) -> None:
        with self._lock:
            try:
                if self._fh is None:
                    self._open()
                if self._size > self.max_bytes:
                    self._rotate()
                self._fh.write(text)
                self._fh.flush()
                self._size += len(text.encode("utf-8", "replace"))
            except Exception:
                pass


class TeeStream:
    """File-like wrapper: pass through to the original stream, log whole lines."""

    def __init__(self, original, main: RotatingWriter, errors: RotatingWriter, is_err: bool):
        self._orig = original
        self._main = main
        self._errors = errors
        self._is_err = is_err
        self._buf = threading.local()
        self.encoding = getattr(original, "encoding", None) or "utf-8"

    def write(self, text: str) -> int:
        if self._orig is not None:
            try:
                self._orig.write(text)
            except Exception:
                pass
        try:
            pending = getattr(self._buf, "s", "") + str(text)
            *lines, rest = pending.split("\n")
            self._buf.s = rest
            for line in lines:
                self._log(line)
        except Exception:
            pass
        return len(text)

    def _log(self, line: str) -> None:
        line = line.rstrip("\r")
        if not line.strip():
            return
        level, comp = classify(line, self._is_err)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        out = f"{stamp} | {level:<7} | {comp:<12} | {mask(line)}\n"
        self._main.write(out)
        if level != "INFO":
            self._errors.write(out)

    def flush(self) -> None:
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        try:
            return bool(self._orig and self._orig.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        if self._orig is None:
            raise OSError("no underlying stream")
        return self._orig.fileno()

    def reconfigure(self, **kwargs) -> None:
        fn = getattr(self._orig, "reconfigure", None)
        if callable(fn):
            fn(**kwargs)

    def __getattr__(self, name: str):
        return getattr(self._orig, name)


_installed = False


def log_dir() -> Path:
    return Path(os.environ.get("ERA_LOG_DIR") or (Path(__file__).resolve().parent.parent / "logs"))


def install(base: str = "eraone") -> bool:
    """Idempotent. Returns True if the tee is active after the call."""
    global _installed
    if _installed:
        return True
    d = log_dir()
    main = RotatingWriter(d / f"{base}.log")
    errors = RotatingWriter(d / f"{base}-error.log")
    sys.stdout = TeeStream(sys.stdout, main, errors, is_err=False)
    sys.stderr = TeeStream(sys.stderr, main, errors, is_err=True)
    _installed = True
    return True
