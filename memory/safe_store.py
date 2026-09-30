from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from core.logger import log_event, memory_logger, safe_log_path

_BACKUP_SUFFIX = ".jsonbak"
_BACKUP_DIR_NAME = "backups"
_BACKUP_KEEP = 3  # newest N versioned backups retained per memory file
_backup_seq_counter: int = 0


def _get_backup_dir(path: Path) -> Path:
    return path.parent / _BACKUP_DIR_NAME


def _backup_path(path: Path, backup_dir: Path | None = None) -> Path:
    bd = backup_dir or _get_backup_dir(path)
    bd.mkdir(parents=True, exist_ok=True)
    return bd / f"{path.stem}{_BACKUP_SUFFIX}"


def _versioned_backup(path: Path, backup_dir: Path | None = None) -> Path:
    global _backup_seq_counter
    _backup_seq_counter += 1
    bd = backup_dir or _get_backup_dir(path)
    bd.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    return bd / f"{path.stem}.{ts}.{os.getpid()}.{_backup_seq_counter:04d}{_BACKUP_SUFFIX}"


def _log_backup_created(path: Path, bk: Path) -> None:
    if os.environ.get("ERA_LOG_BACKUPS") == "1":
        log_event(
            memory_logger,
            "memory.backup.created",
            "DEBUG",
            path=safe_log_path(path),
            backup=safe_log_path(bk),
        )


def _backup_sort_key(p: Path) -> tuple[int, int]:
    """Newest-first ordering key. Uses the creation order embedded in the
    versioned filename ({stem}.{ts_ms}.{pid}.{seq}.jsonbak) so retention and
    recovery stay correct even when writes land in the same mtime tick; falls
    back to mtime for names that don't match the versioned scheme."""
    name = p.name[: -len(_BACKUP_SUFFIX)] if p.name.endswith(_BACKUP_SUFFIX) else p.name
    parts = name.split(".")
    try:
        return (int(parts[-3]), int(parts[-1]))  # (timestamp_ms, sequence)
    except (ValueError, IndexError):
        try:
            return (int(p.stat().st_mtime * 1000), 0)
        except OSError:
            return (0, 0)


def _list_backups(path: Path, backup_dir: Path | None = None) -> list[Path]:
    bd = backup_dir or _get_backup_dir(path)
    if not bd.is_dir():
        return []
    stem = path.stem
    return sorted(bd.glob(f"{stem}.*{_BACKUP_SUFFIX}"), key=_backup_sort_key, reverse=True)


def _latest_backup(path: Path, backup_dir: Path | None = None) -> Path | None:
    # Explicit backup_dir — only search there
    if backup_dir is not None:
        backups = _list_backups(path, backup_dir)
        return backups[0] if backups else None
    # Default: search new location (path.parent/backups/) first
    backups = _list_backups(path)
    if backups:
        return backups[0]
    # Legacy fallback: search path.parent directly
    legacy = _list_backups(path, path.parent)
    return legacy[0] if legacy else None


def _prune_backups(path: Path, backup_dir: Path | None = None) -> None:
    """Best-effort retention: keep only the newest _BACKUP_KEEP versioned backups.
    A pruning failure must never break an otherwise-successful memory write."""
    try:
        cleanup_backups(path, backup_dir, keep=_BACKUP_KEEP)
    except Exception:
        pass


def _replace(src: str, dst: Path, attempts: int = 6) -> None:
    """os.replace, retried while Windows reports the target busy.

    WinError 5/32 on the rename means another process (Defender, the search
    indexer, a reader) has the file open for a moment — it clears in tens of
    milliseconds. Seen failing a test on 2026-09-26; the same call writes the
    live memory stores. Anything else, or a lock that outlasts ~0.8 s, raises.
    """
    for i in range(attempts):
        try:
            os.replace(src, str(dst))
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.025 * (2**i))


def safe_write_json(path: Path, data: Any, backup_dir: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bk = _versioned_backup(path, backup_dir)
        shutil.copy2(path, bk)
        _log_backup_created(path, bk)
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        _replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise
    _prune_backups(path, backup_dir)


def safe_write_jsonl(path: Path, lines: list[dict], backup_dir: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bk = _versioned_backup(path, backup_dir)
        shutil.copy2(path, bk)
        _log_backup_created(path, bk)
    fd, tmp_path = tempfile.mkstemp(suffix=".jsonl", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        _replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise
    _prune_backups(path, backup_dir)


def safe_append_jsonl(path: Path, record: dict, backup_dir: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            lines = content.strip().splitlines()
            valid: list[str] = []
            had_invalid = False
            for line in lines:
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                    valid.append(line)
                except json.JSONDecodeError:
                    had_invalid = True
            if had_invalid:
                bk = _versioned_backup(path, backup_dir)
                shutil.copy2(path, bk)
                log_event(
                    memory_logger,
                    "memory.jsonl.repaired",
                    "WARNING",
                    path=safe_log_path(path),
                    backup=safe_log_path(bk),
                    invalid_lines=len(content.strip().splitlines()) - len(valid),
                )
                text = "\n".join(valid) + "\n" if valid else ""
                path.write_text(text, encoding="utf-8")
                _prune_backups(path, backup_dir)
        except Exception:
            pass
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def recover_from_backup(path: Path, backup_dir: Path | None = None) -> bool:
    latest = _latest_backup(path, backup_dir)
    if latest is None:
        return False
    try:
        shutil.copy2(latest, path)
        log_event(
            memory_logger,
            "memory.backup.recovered",
            "WARNING",
            path=safe_log_path(path),
            backup=safe_log_path(latest),
        )
        return True
    except Exception:
        return False


def cleanup_backups(path: Path, backup_dir: Path | None = None, keep: int = 3) -> int:
    backups = _list_backups(path, backup_dir)
    removed = 0
    for old in backups[keep:]:
        try:
            old.unlink()
            removed += 1
        except Exception:
            pass
    return removed


def validate_json_file(path: Path, backup_dir: Path | None = None) -> dict:
    result: dict[str, Any] = {"status": "OK", "message": "", "file_size": 0}
    if not path.exists():
        result["status"] = "MISSING"
        result["message"] = "File not found"
        return result
    try:
        result["file_size"] = path.stat().st_size
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, (dict, list)):
            result["status"] = "CORRUPTED_NO_BACKUP"
            result["message"] = "Root value is not a JSON object or array"
            return result
        return result
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        latest = _latest_backup(path, backup_dir)
        if latest is not None:
            try:
                json.loads(latest.read_text(encoding="utf-8"))
                shutil.copy2(latest, path)
                result["status"] = "CORRUPTED_RECOVERED"
                result["message"] = f"Recovered from backup: {latest.name}"
                log_event(
                    memory_logger,
                    "memory.corruption.detected",
                    "WARNING",
                    path=safe_log_path(path),
                    status="CORRUPTED_RECOVERED",
                    backup=safe_log_path(latest),
                )
                return result
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        result["status"] = "CORRUPTED_NO_BACKUP"
        result["message"] = f"Invalid JSON and no valid backup: {e}"
        log_event(
            memory_logger,
            "memory.corruption.detected",
            "ERROR",
            path=safe_log_path(path),
            status="CORRUPTED_NO_BACKUP",
        )
        return result
