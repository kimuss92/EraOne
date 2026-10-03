"""Load only user-approved feature modules; replacement does not restart Era."""

import hashlib
import importlib.util
import inspect
import re
import threading
import uuid
from pathlib import Path

SESSION_ID = uuid.uuid4().hex
ROOT = Path(__file__).resolve().parent.parent
_features = {}
_lock = threading.RLock()


def load(path: Path):
    path = path.resolve()
    if path.parent != (ROOT / "features").resolve() or path.suffix != ".py":
        raise ValueError("Reloadable features must be direct features/*.py modules")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(f"era_feature_{path.stem}_{digest}", path)
    module = importlib.util.module_from_spec(spec)
    # Avoid stale timestamp-based pyc caches on rapid live replacement.
    try:
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    except (SystemExit, KeyboardInterrupt) as exc:
        raise ValueError("Feature import tried to terminate the host") from exc
    info = getattr(module, "FEATURE", None)
    if not isinstance(info, dict) or not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,47}", str(info.get("id", ""))
    ):
        raise ValueError("Feature requires an id and description")
    if not isinstance(info.get("description"), str) or not 1 <= len(info["description"]) <= 500:
        raise ValueError("Feature description must be 1–500 characters")
    if not callable(getattr(module, "run", None)):
        raise ValueError("Feature must define run(arguments)")
    inspect.signature(module.run).bind({})
    return {
        "id": info["id"],
        "description": info["description"],
        "module": module,
        "path": path.relative_to(ROOT).as_posix(),
    }


def install(paths: list[Path]) -> None:
    # Validate all modules first, then publish the collection atomically.
    loaded = [load(path) for path in paths]
    if len({f["id"] for f in loaded}) != len(loaded):
        raise ValueError("Duplicate feature ids")
    with _lock:
        updated = dict(_features)
        for feature in loaded:
            updated = {k: v for k, v in updated.items() if v["path"] != feature["path"]}
            if feature["id"] in updated:
                raise ValueError("Feature id already belongs to another module")
            updated[feature["id"]] = feature
        _features.clear()
        _features.update(updated)
        for feature in loaded:
            _restored[feature["path"]] = hashlib.sha256(
                (ROOT / feature["path"]).read_bytes()
            ).hexdigest()


def catalog() -> list[dict]:
    with _lock:
        return [{k: f[k] for k in ("id", "description", "path")} for f in _features.values()]


async def run(feature_id: str, arguments: dict):
    import asyncio

    with _lock:
        feature = _features.get(feature_id)
    if feature is None:
        raise ValueError("Feature is not activated")
    function = feature["module"].run

    async def invoke():
        try:
            if inspect.iscoroutinefunction(function):
                return await function(arguments)
            return await asyncio.to_thread(function, arguments)
        except (SystemExit, KeyboardInterrupt) as exc:
            raise ValueError("Feature execution tried to terminate the host") from exc

    return await asyncio.wait_for(invoke(), timeout=30)


_restored = {}


def restore() -> None:
    """Restore only recorded approvals whose deployed bytes still match."""
    import json

    with _lock:
        # Latest approved deployment for a path wins. Older revisions cannot
        # reappear after an approved replacement or after external edits.
        records = []
        for file in (ROOT / "runtime/development").glob("*/job.json"):
            try:
                job = json.loads(file.read_text(encoding="utf-8"))
                if job.get("state") == "activated" and not (
                    job.get("requires_restart") and job.get("engine_session") == SESSION_ID
                ):
                    records.append((file.stat().st_mtime, job))
            except (OSError, ValueError, AttributeError):
                continue
        seen = set()
        for _, job in sorted(records, key=lambda item: item[0], reverse=True):
            for item in job.get("files", []):
                rel = item.get("path", "")
                if not re.fullmatch(r"features/[a-zA-Z0-9_]+\.py", rel) or rel in seen:
                    continue
                seen.add(rel)
                path = ROOT / rel
                try:
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                    if actual != item.get("candidate"):
                        for key in [
                            key for key, feature in _features.items() if feature["path"] == rel
                        ]:
                            del _features[key]
                        continue
                    if _restored.get(rel) != actual or not any(
                        feature["path"] == rel for feature in _features.values()
                    ):
                        install([path])
                        _restored[rel] = actual
                except Exception:
                    continue
