"""Memory audit log — records every memory mutation to logs/memory_audit.jsonl."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memory.safe_store import safe_append_jsonl

_AUDIT_DIR = Path(__file__).resolve().parent.parent / "logs"
_AUDIT_FILENAME = "memory_audit.jsonl"

# Mutable so tests can override via module assignment
AUDIT_DIR = _AUDIT_DIR
AUDIT_FILENAME = _AUDIT_FILENAME

# This log is append-only forever otherwise — it's genuinely useful (it's
# what let us reconstruct and restore everything memory_manager's old trim
# bug had deleted), so rotate-and-keep rather than truncate: once the active
# file crosses this size, it's renamed to an .N archive instead of growing
# unbounded, and only the oldest archives beyond the keep count are deleted.
_MAX_ACTIVE_BYTES = 5 * 1024 * 1024  # 5 MB
_ROTATED_KEEP = 5


def _get_audit_path() -> Path:
    """Return current audit path (reads module globals at call time for test override)."""
    import memory.memory_audit as _self

    return Path(_self.AUDIT_DIR) / _self.AUDIT_FILENAME


def _ensure_dir() -> None:
    _get_audit_path().parent.mkdir(parents=True, exist_ok=True)


def _rotate_if_needed(path: Path) -> None:
    """Rename path -> path.1, shifting existing .1..N up, dropping anything
    past _ROTATED_KEEP. Best-effort: a rotation failure must never block an
    audit write, since losing one log line matters far less than the log
    growing forever or crashing memory writes."""
    try:
        if not path.exists() or path.stat().st_size < _MAX_ACTIVE_BYTES:
            return
        for i in range(_ROTATED_KEEP, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            if not src.exists():
                continue
            if i == _ROTATED_KEEP:
                src.unlink()
            else:
                dst = path.with_suffix(path.suffix + f".{i + 1}")
                src.replace(dst)
        path.replace(path.with_suffix(path.suffix + ".1"))
    except Exception:
        pass


def _summarize(value: Any, max_len: int = 120) -> str:
    """Return a short, safe summary of a value for the audit log."""
    if value is None:
        return ""
    if isinstance(value, dict):
        val = value.get("value", "")
    else:
        val = str(value)
    val = val.strip()
    if len(val) > max_len:
        val = val[: max_len - 3] + "..."
    return val


def audit_event(
    *,
    operation: str,
    category: str = "",
    key: str = "",
    old_value_summary: str = "",
    new_value_summary: str = "",
    source_tool: str = "",
    reason: str = "",
    allowed: bool = True,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one audit event to the JSONL log.

    Operations: save, update, delete, trim, dedup, block, feedback
    """
    _ensure_dir()
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "op": operation,
        "cat": category,
        "key": key,
        "old": old_value_summary,
        "new": new_value_summary,
        "src": source_tool,
        "reason": reason,
        "allowed": allowed,
    }
    if extra:
        record["extra"] = extra
    path = _get_audit_path()
    _rotate_if_needed(path)
    safe_append_jsonl(path, record)
