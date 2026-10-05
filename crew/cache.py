"""crew/cache.py — the two Era-side caches of a crew turn.

  1. TOOL RESULT      `vault.read` results, 60 s. In memory. Dropped the moment any verified
                      vault write happens (core.vault_bridge.write_generation), so a write —
                      including a user-approved `replace`, applied OUTSIDE the crew — is never
                      hidden behind a stale read. Keyed on the caller's brand scope too, so a
                      bot confined to one brand can never be served what a wider bot read.
  2. CREW RESPONSE    (bot, task, context) -> final answer, persisted to memory/crew_cache/.
                      ONLY an answer that did nothing is stored: a turn that wrote, handed off,
                      proposed code, or was reviewed is never replayed (replaying "I created the
                      note" without creating it would be a lie). Bounded by a TTL and by the
                      vault write generation within one process.

Both degrade: a corrupt cache file or a failed write is a miss, never an error for the turn.
There is no provider-request cache: Era no longer talks to a provider, and Hermes caches the
prompt prefix itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.tool_result import ToolResult
from core.utils import BASE_DIR

TOOL_TTL_S = 60.0
DEFAULT_RESPONSE_TTL_S = 600.0
_MAX_RESPONSE_FILES = 200
_PURE_READ = frozenset({"vault.read", "vault.search"})
BOOT_ID = secrets.token_hex(4)  # generation counters only compare within one process


def sha(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p if isinstance(p, str) else json.dumps(p, sort_keys=True, default=str)).encode())
        h.update(b"\x00")
    return h.hexdigest()


def _vault_generation() -> int:
    try:
        from core import vault_bridge

        return vault_bridge.write_generation()
    except Exception:  # noqa: BLE001 — no bridge means nothing to invalidate on
        return 0


class TTLCache:
    """Small thread-safe LRU with a TTL. `gen` (optional) must still match on read."""

    def __init__(
        self,
        ttl_s: float,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self._max = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, Any, Any]] = OrderedDict()

    def get(self, key: str, gen: Any = None) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            stamp, value, entry_gen = hit
            if self._clock() - stamp > self.ttl_s or entry_gen != gen:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: str, value: Any, gen: Any = None) -> None:
        if self.ttl_s <= 0:
            return
        with self._lock:
            self._data[key] = (self._clock(), value, gen)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class ToolResultCache:
    """Layer 1: `vault.read`, 60 s, invalidated by any verified vault write."""

    def __init__(self, ttl_s: float = TOOL_TTL_S, clock: Callable[[], float] = time.monotonic):
        self._cache = TTLCache(ttl_s, clock=clock)

    @staticmethod
    def key(caller: Any, args: dict) -> str:
        brands = None if caller.brands is None else sorted(caller.brands)
        return sha("vault.read", brands, args)

    def get(self, key: str) -> ToolResult | None:
        hit = self._cache.get(key, _vault_generation())
        # A copy: a caller that mutates `result.data` must not corrupt the cached entry.
        return None if hit is None else copy.deepcopy(hit)

    def put(self, key: str, result: ToolResult) -> None:
        # Only a success is worth replaying; a failure may be transient (a held lock).
        if result.success:
            self._cache.put(key, result, _vault_generation())

    def clear(self) -> None:
        self._cache.clear()


class ResponseCache:
    """Layer 2: a crew bot's final answer, persisted so a restart keeps it."""

    def __init__(
        self,
        directory: Path | None = None,
        ttl_s: float = DEFAULT_RESPONSE_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.dir = Path(directory) if directory else BASE_DIR / "memory" / "crew_cache"
        self.ttl_s = float(ttl_s)
        self._clock = clock

    # ── what may be stored ──
    @staticmethod
    def cacheable(spec: Any, turn: Any, result: ToolResult) -> bool:
        """True only for an answer that DID nothing: it succeeded, wrote nothing, handed
        nothing off, proposed no code, and was not itself a review."""
        if not result.success or spec.id == "nabar" or turn.context.get("review"):
            return False
        data = result.data or {}
        if data.get("effects") or data.get("review"):
            return False
        return set(data.get("capabilities_used") or []) <= _PURE_READ

    @staticmethod
    def key(spec: Any, turn: Any) -> str:
        # The bot's own definition is part of the key, so editing its yaml, prompt or
        # brand scope can never be answered from what the old definition produced.
        fingerprint = sha(
            spec.brands, spec.capabilities, spec.write_zones, spec.prompt, spec.tier_preference
        )[:12]
        return f"{spec.id}_{sha(turn.task)[:16]}_{sha(turn.context)[:16]}_{fingerprint}"

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    # ── read / write ──
    def get(self, spec: Any, turn: Any) -> ToolResult | None:
        if self.ttl_s <= 0:
            return None
        path = self._path(self.key(spec, turn))
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if self._clock() - float(entry["created"]) > self.ttl_s:
                return None
            if entry.get("boot") == BOOT_ID and entry.get("gen") != _vault_generation():
                return None  # the vault was written since this was said
            data = dict(entry["data"])
            data["response_cache"] = True
            return ToolResult.ok(message=str(entry["message"]), data=data)
        except (OSError, ValueError, KeyError, TypeError):
            return None  # absent, expired-unreadable or corrupt: a miss, never an error

    def put(self, spec: Any, turn: Any, result: ToolResult) -> None:
        if self.ttl_s <= 0 or not self.cacheable(spec, turn, result):
            return
        keep = ("bot", "steps", "capabilities_used", "effects", "final_json")
        data = {k: (result.data or {}).get(k) for k in keep if k in (result.data or {})}
        entry = {
            "v": 1,
            "created": self._clock(),
            "boot": BOOT_ID,
            "gen": _vault_generation(),
            "bot": spec.id,
            "message": result.message,
            "data": data,
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._path(self.key(spec, turn))
            tmp = path.with_name(f".{path.name}.{secrets.token_hex(3)}.tmp")
            tmp.write_text(json.dumps(entry, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, path)
            self._prune()
        except (OSError, TypeError, ValueError):
            return  # a failed cache write must never fail the turn

    def _prune(self) -> None:
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - _MAX_RESPONSE_FILES)]:
            try:
                old.unlink()
            except OSError:
                pass

    def clear(self) -> None:
        for p in self.dir.glob("*.json"):
            try:
                p.unlink()
            except OSError:
                pass


class CrewCaches:
    """The two layers, built from `routing.cache` (every key optional)."""

    def __init__(
        self,
        tool: ToolResultCache | None = None,
        response: ResponseCache | None = None,
    ) -> None:
        self.tool = tool or ToolResultCache()
        self.response = response or ResponseCache()

    @classmethod
    def from_config(cls, cfg: dict | None) -> CrewCaches:
        cfg = dict(cfg or {})
        if cfg.get("enabled") is False:
            # Zero TTLs: every get misses and every put is dropped.
            return cls(ToolResultCache(0.0), ResponseCache(ttl_s=0.0))
        return cls(
            ToolResultCache(float(cfg.get("tool_ttl_s", TOOL_TTL_S))),
            ResponseCache(
                Path(cfg["response_dir"]) if cfg.get("response_dir") else None,
                float(cfg.get("response_ttl_s", DEFAULT_RESPONSE_TTL_S)),
            ),
        )
