from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "token",
        "api_key",
        "key",
        "secret",
        "authorization",
        "auth",
        "credential",
        "cookie",
    }
)

_LOG_LEVEL_MAP: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_MAX_PARAM_STRING_LENGTH = 120

_ERA_CONFIGURED_ATTR = "_era_configured"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(sk in k for sk in _SENSITIVE_KEYS)


def _truncate(value: str, max_len: int = _MAX_PARAM_STRING_LENGTH) -> str:
    s = str(value).strip()
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _sanitize_field(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive(k):
                sanitized[k] = "***"
            elif k in ("message_text", "message", "content", "body"):
                if isinstance(v, str):
                    sanitized[k] = _truncate(v, 120)
                    sanitized[f"{k}_length"] = len(v)
                else:
                    sanitized[k] = v
            elif k == "attachments":
                sanitized[k] = f"<{len(v) if isinstance(v, (list, tuple)) else 0} attachment(s)>"
            else:
                sanitized[k] = _truncate(str(v)) if isinstance(v, str) else v
        return sanitized
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, (list, tuple)):
        screened = []
        for item in value:
            if isinstance(item, dict):
                screened.append(_sanitize_field(key, item))
            elif isinstance(item, str):
                screened.append(_truncate(item))
            else:
                screened.append(item)
        return screened
    return value


EXTERNAL_PATH_PREFIX = "<external>/"


def safe_log_path(path: Path | str, base_dir: Path | None = None) -> str:
    """Convert an absolute path to a forward-slash relative path if under project root.

    Paths outside the project root are reduced to `<external>/<filename>` — never
    the full user home path.

    The `<external>/` tag matters: the test-suite runs against temp-dir copies
    that share the REAL filenames (e.g. a throwaway `long_term.json`), so a bare
    basename made a test fixture indistinguishable from the live memory file.
    That ambiguity is exactly what made a self-diagnosis report "frequent
    corruption of long_term.json" when the live file had never once been corrupt.
    The real file logs as `memory/long_term.json`; a temp copy logs as
    `<external>/long_term.json`.
    """
    p = Path(path).resolve()
    base = (base_dir or _REPO_ROOT).resolve()
    try:
        rel = p.relative_to(base)
        return rel.as_posix()
    except ValueError:
        return f"{EXTERNAL_PATH_PREFIX}{p.name}"


def _now_iso() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _get_log_dir() -> Path:
    env_dir = os.environ.get("ERA_LOG_DIR")
    if env_dir:
        d = Path(env_dir).resolve()
    else:
        d = Path(__file__).resolve().parent.parent / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _JsonlFormatter(logging.Formatter):
    """Outputs the message as-is (must already be a JSON line).

    Newlines are escaped: json.dumps never emits a raw one, so this only bites a
    plain-text message — and one of those carrying "\\n{...}" could otherwise
    forge a whole record that core/self_heal would read as a real traceback."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        return msg.replace("\r", "\\r").replace("\n", "\\n") if "\n" in msg or "\r" in msg else msg


_SECRET_VALUE = re.compile(
    r"AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{16,}|ya29\.[A-Za-z0-9._\-]{10,}|"
    r"\bbot\d{6,}:[A-Za-z0-9_\-]{20,}|"  # Telegram bot token as it appears in API URLs
    r"(?i:bearer|basic)\s+[A-Za-z0-9._\-~+/=]{8,}|"
    r"(?i:(?:api_?key|key|token|secret|password|passwd|auth)=)[^&\s\"']+|"
    r"(?i:[\"'](?:access_token|refresh_token|id_token|token|api_?key|client_secret|secret|"
    r"password)[\"']\s*:\s*)[\"'][^\"']+[\"']"
)


def redact_secrets(text: str) -> str:
    """Mask credential-shaped values. Exception text routinely echoes URLs and
    headers, and crash records are persisted and fed to the self-heal model."""
    return _SECRET_VALUE.sub("<redacted>", text or "")


class _ConsoleFormatter(logging.Formatter):
    """Human-readable format for WARNING+ console output."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return f"[{ts}] [{record.levelname}] [{record.name}] {record.getMessage()}"


# ONE RotatingFileHandler per log FILE, shared by every logger that writes to it.
#
# Each setup_logger() call used to build its OWN handler on the SAME file. With
# four loggers (main, executor, memory, doctor) that meant four open handles on
# era.log — so once it passed maxBytes, whichever handler tried to roll over hit
# the other three still holding the file:
#
#   PermissionError: [WinError 32] ... used by another process:
#     'logs\era.log' -> 'logs\era.log.1'
#
# Windows cannot rename a file anyone else has open, so rotation then failed on
# EVERY record, spraying a "--- Logging error ---" traceback after each log line.
# A single handle rotates fine.
#
# Keyed by path rather than a bare singleton, so ERA_LOG_DIR stays honest: point
# it elsewhere and you get that file's own handler, instead of being stuck with
# whichever directory happened to be set when this module was first imported.
_file_handlers: dict[str, RotatingFileHandler] = {}
_file_handler_lock = threading.Lock()


def _get_file_handler() -> RotatingFileHandler:
    path = str((_get_log_dir() / "era.log").resolve())
    with _file_handler_lock:
        handler = _file_handlers.get(path)
        if handler is None:
            handler = RotatingFileHandler(
                path,
                mode="w",  # start each launch with a clean era.log, not last run's tail
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(_JsonlFormatter())
            _file_handlers[path] = handler
        return handler


def setup_logger(name: str = "era_one") -> logging.Logger:
    logger = logging.getLogger(name)

    if getattr(logger, _ERA_CONFIGURED_ATTR, False):
        return logger

    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(_ConsoleFormatter())

    logger.handlers.clear()
    logger.addHandler(_get_file_handler())
    logger.addHandler(console_handler)

    setattr(logger, _ERA_CONFIGURED_ATTR, True)
    return logger


main_logger: logging.Logger = setup_logger("era_one.main")
executor_logger: logging.Logger = setup_logger("era_one.executor")
memory_logger: logging.Logger = setup_logger("era_one.memory")
doctor_logger: logging.Logger = setup_logger("era_one.doctor")


def _under_pytest() -> bool:
    """True while the test-suite is driving this process.

    The suite deliberately exercises failure paths — asserting that
    ``confirm_pending_confirmation`` raises when nothing is pending, that a
    corrupt store is detected, and so on. Those expected failures land in the
    same era.log as production ones, on the same production loggers, so
    self-diagnosis counted them as live defects and reported "Degraded" with
    no real fault behind it.

    Tagging at the point of writing is the same trick ``safe_log_path`` uses
    for ``<external>/`` paths: mark the record at the source, then match the
    tag — never guess afterwards from a logger name or a suspicious timestamp,
    because both are shared with genuine production errors.
    """
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def log_event(
    logger: logging.Logger,
    event: str,
    level: str = "INFO",
    **fields: Any,
) -> None:
    level_num = _LOG_LEVEL_MAP.get(level.upper(), logging.INFO)

    record: dict[str, Any] = {
        "ts": _now_iso(),
        "level": level.upper(),
        "logger": logger.name,
        "event": event,
    }
    if _under_pytest():
        record["test_run"] = True
    for key, value in fields.items():
        sanitized = _sanitize_field(key, value)
        if sanitized is not None:
            record[key] = sanitized

    json_line = json.dumps(record, default=str)

    logger.log(level_num, json_line)


_MAX_TRACEBACK_LENGTH = 20000


def log_crash(
    logger: logging.Logger,
    source: str,
    exc_type: type[BaseException] | None = None,
    exc_value: BaseException | None = None,
    exc_tb: Any = None,
    *,
    event: str = "engine.crash",
    level: str = "CRITICAL",
    **extra: Any,
) -> None:
    """Persist a full exception traceback to the log.

    Unlike ``log_event``, the ``traceback`` field is NOT run through the
    120-char field truncator — the whole stack is kept (bounded only by
    ``_MAX_TRACEBACK_LENGTH``) so a crash can be diagnosed post-mortem from
    the log alone instead of relying on catching it live in the console.
    Never raises: logging a crash must not itself crash the crash handler.
    """
    try:
        import traceback as _tb

        if exc_value is not None and exc_type is None:
            exc_type = type(exc_value)
        if exc_value is not None and exc_tb is None:
            exc_tb = exc_value.__traceback__

        tb_text = redact_secrets("".join(_tb.format_exception(exc_type, exc_value, exc_tb)))
        if len(tb_text) > _MAX_TRACEBACK_LENGTH:
            tb_text = tb_text[:_MAX_TRACEBACK_LENGTH] + "...<truncated>"
        # Frames straight from the traceback OBJECT. The text above ends with
        # str(exc), which can quote a web page — including lines shaped like
        # 'File "...", line N, in f'. core/self_heal locates faults from THIS
        # field only, never by parsing tb_text.
        frames = [[f.filename, f.lineno, f.name] for f in _tb.extract_tb(exc_tb)][-40:]

        record: dict[str, Any] = {
            "ts": _now_iso(),
            "level": level.upper(),
            "logger": logger.name,
            "event": event,
            "source": source,
            "error_type": exc_type.__name__ if exc_type else "Unknown",
            "error": _truncate(redact_secrets(str(exc_value)), 500) if exc_value else "",
            "traceback": tb_text,
            "frames": frames,
        }
        if _under_pytest():
            record["test_run"] = True
        for key, value in extra.items():
            sanitized = _sanitize_field(key, value)
            if sanitized is not None:
                record[key] = sanitized

        level_num = _LOG_LEVEL_MAP.get(level.upper(), logging.CRITICAL)
        logger.log(level_num, json.dumps(record, default=str))
    except Exception:
        pass


def log_frames(
    logger: logging.Logger,
    source: str,
    error_type: str,
    error: str,
    frames: list,
    *,
    event: str,
    level: str = "ERROR",
    **extra: Any,
) -> None:
    """log_crash for an exception that was CAUGHT and is gone: its frames were
    captured as plain data by core/handled_exceptions. Same record shape
    (`frames` is what core/self_heal reads); `traceback` is rebuilt from the
    frames, so it holds no exception text. Never raises."""
    try:
        clean = [[str(f[0]), int(f[1]), str(f[2])] for f in (frames or [])][-40:]
        tb_text = "Traceback (caught by a handler; frames only):\n" + "".join(
            f'  File "{fn}", line {ln}, in {name}\n' for fn, ln, name in clean
        )
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "level": level.upper(),
            "logger": logger.name,
            "event": event,
            "source": source,
            "error_type": str(error_type or "Unknown")[:80],
            "error": _truncate(redact_secrets(str(error or "")), 500),
            "traceback": tb_text[:_MAX_TRACEBACK_LENGTH],
            "frames": clean,
        }
        if _under_pytest():
            record["test_run"] = True
        for key, value in extra.items():
            sanitized = _sanitize_field(key, value)
            if sanitized is not None:
                record[key] = sanitized
        logger.log(
            _LOG_LEVEL_MAP.get(level.upper(), logging.ERROR), json.dumps(record, default=str)
        )
    except Exception:
        pass
