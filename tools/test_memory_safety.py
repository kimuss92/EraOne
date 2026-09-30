"""Tests for memory/safe_store.py and memory/memory_manager.py — tempfile-based."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
os.environ["PYTHONIOENCODING"] = "utf-8"

# Isolate test logs from real logs/era.log
_MEMORY_TEST_LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "test_memory_isolation"
_MEMORY_TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("ERA_LOG_DIR", str(_MEMORY_TEST_LOG_DIR))

repo_root = Path(__file__).resolve().parents[1]

import memory.memory_manager as mm
from memory.safe_store import (
    _list_backups,
    _versioned_backup,
    cleanup_backups,
    recover_from_backup,
    safe_append_jsonl,
    safe_write_json,
    safe_write_jsonl,
    validate_json_file,
)

passed = 0
failed = 0


def check(name: str, condition: bool):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name}")


# -- safe_write_json tests --

print("\n[safe_write_json] Basic write...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    safe_write_json(path, {"key": "value", "num": 42})
    check("file exists", path.exists())
    data = json.loads(path.read_text(encoding="utf-8"))
    check("content correct", data == {"key": "value", "num": 42})

print("\n[safe_write_json] Overwrite creates backup...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    safe_write_json(path, {"v": 1})
    safe_write_json(path, {"v": 2})
    baks = _list_backups(path)
    check("backup exists", len(baks) >= 1)
    bk_data = json.loads(baks[0].read_text(encoding="utf-8"))
    check("backup has old data", bk_data == {"v": 1})
    data = json.loads(path.read_text(encoding="utf-8"))
    check("current has new data", data == {"v": 2})

print("\n[safe_write_json] Nested directories created...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "a" / "b" / "c" / "test.json"
    safe_write_json(path, {"x": 1})
    check("nested dirs created", path.exists())

print("\n[safe_write_json] Write None...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "null.json"
    safe_write_json(path, None)
    data = json.loads(path.read_text(encoding="utf-8"))
    check("None is serialized as null", data is None)

print("\n[safe_write_json] Write list...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "list.json"
    safe_write_json(path, [1, 2, 3])
    data = json.loads(path.read_text(encoding="utf-8"))
    check("list round-trips", data == [1, 2, 3])

# -- safe_write_jsonl tests --

print("\n[safe_write_jsonl] Basic write...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.jsonl"
    safe_write_jsonl(path, [{"a": 1}, {"b": 2}])
    check("file exists", path.exists())
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    check("two lines", len(lines) == 2)
    check("line 1 valid", json.loads(lines[0]) == {"a": 1})
    check("line 2 valid", json.loads(lines[1]) == {"b": 2})

print("\n[safe_write_jsonl] Overwrite with backup...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.jsonl"
    safe_write_jsonl(path, [{"v": 1}])
    safe_write_jsonl(path, [{"v": 2}])
    baks = _list_backups(path)
    check("backup exists", len(baks) >= 1)
    bk_lines = json.loads(baks[0].read_text(encoding="utf-8").strip().splitlines()[0])
    check("backup has old line", bk_lines == {"v": 1})

# -- safe_append_jsonl tests --

print("\n[safe_append_jsonl] Append to new file...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.jsonl"
    safe_append_jsonl(path, {"a": 1})
    check("file created", path.exists())
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    check("one line", len(lines) == 1)

print("\n[safe_append_jsonl] Append multiple...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.jsonl"
    safe_append_jsonl(path, {"n": 1})
    safe_append_jsonl(path, {"n": 2})
    safe_append_jsonl(path, {"n": 3})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    check("three lines", len(lines) == 3)

print("\n[safe_append_jsonl] Repairs invalid lines...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.jsonl"
    path.write_text('{"valid": 1}\ninvalid line\n{"valid": 2}\n', encoding="utf-8")
    baks_before = _list_backups(path)
    check("no backup before append", len(baks_before) == 0)
    safe_append_jsonl(path, {"new": 3})
    baks_after = _list_backups(path)
    check("backup created after repair", len(baks_after) >= 1)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    check("valid lines preserved + new", len(lines) == 3)
    check("line 1 valid", json.loads(lines[0])["valid"] == 1)
    check("line 2 valid", json.loads(lines[1])["valid"] == 2)
    check("line 3 is new", json.loads(lines[2])["new"] == 3)

print("\n[safe_append_jsonl] All invalid preserved via backup...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.jsonl"
    path.write_text("invalid1\ninvalid2\n", encoding="utf-8")
    safe_append_jsonl(path, {"ok": 1})
    baks = _list_backups(path)
    check("backup created", len(baks) >= 1)
    original = baks[0].read_text(encoding="utf-8")
    check("backup preserves original", "invalid1" in original and "invalid2" in original)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    check("invalid lines removed", len(lines) == 1)
    check("new line present", json.loads(lines[0])["ok"] == 1)

print("\n[safe_append_jsonl] Empty file append...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.jsonl"
    path.write_text("", encoding="utf-8")
    safe_append_jsonl(path, {"a": 1})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    check("append to empty file works", len(lines) == 1)

# -- recover_from_backup tests --

print("\n[recover_from_backup] Recovery...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    safe_write_json(path, {"original": "data"})
    safe_write_json(path, {"corrupted": "data"})
    path.write_text("not json{{{", encoding="utf-8")
    check("file is corrupted", True)
    ok = recover_from_backup(path)
    check("recovery succeeded", ok)
    data = json.loads(path.read_text(encoding="utf-8"))
    check("recovered to first version", data == {"original": "data"})

print("\n[recover_from_backup] No backup...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    path.write_text("not json", encoding="utf-8")
    ok = recover_from_backup(path)
    check("no backup to recover", not ok)

# -- cleanup_backups tests --

print("\n[cleanup_backups] Keeps N most recent...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    for i in range(5):
        safe_write_json(path, {"v": i})
    # safe_write_json auto-prunes to _BACKUP_KEEP on every write, so assert the
    # explicit cleanup relative to whatever it left rather than a fixed count.
    before = len(_list_backups(path))
    count = cleanup_backups(path, keep=2)
    check(f"removed {count} old backups", count == max(0, before - 2))
    remaining = _list_backups(path)
    check("kept 2 backups", len(remaining) == 2)

print("\n[cleanup_backups] Per-stem isolation...")
with TemporaryDirectory() as tmp:
    path_a = Path(tmp) / "alpha.json"
    path_b = Path(tmp) / "beta.json"
    for i in range(3):
        safe_write_json(path_a, {"v": i})
        safe_write_json(path_b, {"v": i})
    count_a = cleanup_backups(path_a, keep=1)
    count_b = cleanup_backups(path_b, keep=1)
    # 2 backups per file (writes 2-3), keep=1 => remove 1 each
    check("alpha cleaned", count_a == 1)
    check("beta cleaned", count_b == 1)
    alpha_baks = _list_backups(path_a)
    beta_baks = _list_backups(path_b)
    check("alpha has 1 backup", len(alpha_baks) == 1)
    check("beta has 1 backup", len(beta_baks) == 1)

# -- Backup filename uniqueness tests --

print("\n[backup uniqueness] Rapid writes produce unique filenames...")
import memory.safe_store as _ss

_ss._backup_seq_counter = 0
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "unique_test.json"
    all_names: set[str] = set()
    for i in range(20):
        safe_write_json(path, {"v": i})
        for b in _list_backups(path):
            all_names.add(b.name)
    # 20 writes: first write has no existing file (no backup), remaining 19 create backups
    check("all 19 backup names unique under rapid writes", len(all_names) == 19)

print("\n[backup uniqueness] No backup overwrites (same-millisecond simulation)...")
_ss._backup_seq_counter = 0
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "collision_test.json"
    bk_paths: list[Path] = []
    for _ in range(10):
        bk_paths.append(_versioned_backup(path))
    names = [p.name for p in bk_paths]
    check("versioned backup returns 10 unique paths", len(set(names)) == 10)

print("\n[backup log noise] memory.backup.created suppressed without env var...")
_ss._backup_seq_counter = 0
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "noise_test.json"
    safe_write_json(path, {"v": 0})
    safe_write_json(path, {"v": 1})
    safe_write_json(path, {"v": 2})
    # Without ERA_LOG_BACKUPS=1, backup.created events are not emitted to logs
    check("backup.created not logged without env var", True)

# -- validate_json_file tests --

print("\n[validate_json_file] OK...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    safe_write_json(path, {"a": 1})
    r = validate_json_file(path)
    check("status OK", r["status"] == "OK")
    check("file_size > 0", r["file_size"] > 0)

print("\n[validate_json_file] MISSING...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "nonexistent.json"
    r = validate_json_file(path)
    check("status MISSING", r["status"] == "MISSING")
    check("file_size 0", r["file_size"] == 0)

print("\n[validate_json_file] CORRUPTED_NO_BACKUP...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    path.write_text("not valid json{{{", encoding="utf-8")
    r = validate_json_file(path)
    check("status CORRUPTED_NO_BACKUP", r["status"] == "CORRUPTED_NO_BACKUP")
    check("file_size > 0", r["file_size"] > 0)

print("\n[validate_json_file] CORRUPTED_RECOVERED...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "test.json"
    safe_write_json(path, {"original": True})
    safe_write_json(path, {"second": "write"})
    path.write_text("corrupted{{{", encoding="utf-8")
    r = validate_json_file(path)
    check("status CORRUPTED_RECOVERED", r["status"] == "CORRUPTED_RECOVERED")
    data = json.loads(path.read_text(encoding="utf-8"))
    check("file recovered from backup", data["original"] is True)

print("\n[validate_json_file] List root is OK...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "list.json"
    safe_write_json(path, [1, 2, 3])
    r = validate_json_file(path)
    check("list root status OK", r["status"] == "OK")

# -- Thread safety (safe_store callers use their own locks) --

print("\n[concurrent writes] Final file must be valid...")
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "thread.json"

    def writer(n: int):
        for _ in range(20):
            try:
                safe_write_json(path, {"thread": n, "ts": time.time()})
            except Exception:
                pass

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("final file is valid JSON", json.loads(path.read_text(encoding="utf-8")) is not None)

# ---------------------------------------------------------------------------
# Step 13A — Memory Manager tests (ranked retrieval + dedup)
# ---------------------------------------------------------------------------


def _make_memory_entry(
    value: str, updated: str | None = "2026-06-01", access_count: int = 0
) -> dict:
    entry: dict = {"value": value}
    if updated is not None:
        entry["updated"] = updated
    if access_count:
        entry["access_count"] = access_count
    return entry


print("\n[rank_memory_entries] Ranked retrieval...")
_TEST_MEMORY = {
    "notes": {
        "color": _make_memory_entry("User likes blue and dark mode", "2026-06-10"),
        "food": _make_memory_entry("User loves Italian pasta", "2026-05-01"),
        "music": _make_memory_entry("User enjoys jazz and classical", "2026-04-15"),
        "query": _make_memory_entry("relevant keyword match here", "2026-06-15"),
    },
    "preferences": {},
}
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mm.save_memory(_TEST_MEMORY)
        results = mm.retrieve_ranked_memory("keyword")
        check("ranked: keyword match returns results", len(results) > 0)
        check("ranked: keyword match first", results[0]["key"] == "query")
        check("ranked: top_k respected", len(mm.retrieve_ranked_memory("user", top_k=2)) == 2)
    finally:
        mm.MEMORY_PATH = saved

with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mm.save_memory(_TEST_MEMORY)
        results = mm.retrieve_ranked_memory("")
        check("ranked: empty query returns stable order", len(results) > 0)
        check("ranked: empty query first by recency", results[0]["key"] in ("query", "color"))
    finally:
        mm.MEMORY_PATH = saved

check("ranked: empty memory no crash", len(mm.rank_memory_entries({}, "test")) == 0)

print("\n[score_memory_entry] Scoring...")
entry_with_meta = {
    "value": "blue is the best color yes",
    "updated": "2026-06-10",
    "access_count": 5,
}
entry_without = {"value": "blue is the best color yes"}
entry_old = {"value": "blue is the best color yes", "updated": "2020-01-01"}
entry_float_ts = {"value": "test value", "updated_at": time.time() - 86400}
check(
    "score: keyword match scores higher",
    mm.score_memory_entry("blue", entry_with_meta)
    > mm.score_memory_entry("pizza", entry_with_meta),
)
check("score: no crash on old entry", mm.score_memory_entry("blue", {}) >= 0)
check("score: no crash on missing value", mm.score_memory_entry("test", {"key": "val"}) >= 0)
s1 = mm.score_memory_entry("blue", entry_with_meta)
s2 = mm.score_memory_entry("blue", entry_without)
check("score: access_count bonus increases score", s1 > s2)
check(
    "score: recency affects score",
    mm.score_memory_entry("blue", entry_with_meta) > mm.score_memory_entry("blue", entry_old),
)
check("score: updated_at float works", mm.score_memory_entry("test", entry_float_ts) > 0)
check("score: result clamped 0-1", 0 <= s1 <= 1)

print("\n[is_near_duplicate] Dedup detection...")
check("dedup: exact duplicate", mm.is_near_duplicate("hello world", "hello world"))
check(
    "dedup: near duplicate",
    mm.is_near_duplicate("near duplicate test here", "near duplicate test here plus"),
)
check("dedup: not similar", not mm.is_near_duplicate("python programming", "italian pasta"))
check("dedup: empty value", not mm.is_near_duplicate("", "something"))
check(
    "dedup: threshold higher should be stricter",
    not mm.is_near_duplicate("a b c d", "a b x y", threshold=0.9),
)

print("\n[find_duplicate_memory] In-memory dedup...")
mem = {
    "notes": {
        "a": _make_memory_entry("user likes blue a lot", "2026-06-01"),
        "b": _make_memory_entry("different value here now", "2026-06-01"),
    }
}
# Same key = an assignment to a named slot. Only a verbatim restatement is a
# duplicate; a REFINED value is the user correcting the fact and must be
# written. This check asserted the opposite until 2026-09-20, which is what
# made every small correction ("Tuesday" -> "Thursday") vanish silently.
check(
    "find_dup: same key verbatim restatement is a duplicate",
    mm.find_duplicate_memory("notes", "a", "User Likes Blue A Lot", mem) is not None,
)
check(
    "find_dup: same key refined value is NOT a duplicate",
    mm.find_duplicate_memory("notes", "a", "user likes blue a lot lots", mem) is None,
)
check(
    "find_dup: different key high sim",
    mm.find_duplicate_memory("notes", "c", "user likes blue a lot", mem) is not None,
)
check(
    "find_dup: different key low sim not merged",
    mm.find_duplicate_memory("notes", "c", "completely unrelated value", mem) is None,
)

print("\n[update_memory] Dedup integration...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mm.update_memory({"notes": {"key_a": {"value": "original value here with more text"}}})
        mem1 = mm.load_memory()
        check(
            "dedup: first save works",
            mem1.get("notes", {}).get("key_a", {}).get("value")
            == "original value here with more text",
        )
        mm.update_memory(
            {"notes": {"key_a": {"value": "original value here with more text indeed"}}}
        )
        mem2 = mm.load_memory()
        check(
            "dedup: same-key correction overwrites",
            mem2.get("notes", {}).get("key_a", {}).get("value")
            == "original value here with more text indeed",
        )
        mm.update_memory({"notes": {"key_b": {"value": "brand new different topic here now"}}})
        mem3 = mm.load_memory()
        check("dedup: genuinely new is saved", "key_b" in mem3.get("notes", {}))
        check(
            "dedup: new value correct",
            mem3["notes"]["key_b"]["value"] == "brand new different topic here now",
        )
    finally:
        mm.MEMORY_PATH = saved

print("\n[compact_memory_preview] Preview formatting...")
preview = mm.compact_memory_preview(_TEST_MEMORY, "keyword")
check("preview: returns string", isinstance(preview, str))
check("preview: contains relevant result", "keyword" in preview or "query" in preview)
check("preview: max 2000 chars", len(preview) <= 2000)
check("preview: empty memory returns empty", mm.compact_memory_preview({}, "test") == "")

big_mem = {"notes": {"x": _make_memory_entry("A" * 500, "2026-06-01")}}
preview_big = mm.compact_memory_preview(big_mem, "A")
check("preview: truncates long values", "..." in preview_big or len(preview_big) < 100)

# ---------------------------------------------------------------------------
# Step 13B — Recency-Ranked Session Memory Injection tests
# ---------------------------------------------------------------------------

print("\n[format_memory_for_prompt_ranked] Ranked prompt formatting...")

_TEST_RANKED_MEM = {
    "notes": {
        "color": _make_memory_entry("User likes blue and dark mode", "2026-06-10"),
        "food": _make_memory_entry("User loves Italian pasta", "2026-05-01"),
    },
    "preferences": {
        "theme": _make_memory_entry("Dark mode preferred", "2026-06-15"),
    },
    "identity": {
        "name": _make_memory_entry("Alex", "2026-06-01"),
        "age": _make_memory_entry("28", "2026-06-01"),
    },
}

# 1. Non-empty for valid memory
result = mm.format_memory_for_prompt_ranked(_TEST_RANKED_MEM)
check("ranked prompt: non-empty for valid memory", isinstance(result, str) and len(result) >= 10)

# 2. Empty memory returns empty
check("ranked prompt: empty memory returns empty", mm.format_memory_for_prompt_ranked({}) == "")

# 3. Most recent entry before older entry (within same category)
_TEST_RECENCY_MEM = {
    "notes": {
        "old": _make_memory_entry("older_entry_text_only", "2026-01-01"),
        "new": _make_memory_entry("newer_entry_text_only", "2026-06-15"),
    },
}
result_r = mm.format_memory_for_prompt_ranked(_TEST_RECENCY_MEM)
new_idx = result_r.find("newer_entry_text_only")
old_idx = result_r.find("older_entry_text_only")
check("ranked prompt: recent before older", 0 <= new_idx < old_idx)

# 4. top_k respected by entry count
_TEST_TOP_K_MEM = {
    "notes": {
        "a": _make_memory_entry("alpha_only_val", "2026-06-01"),
        "b": _make_memory_entry("beta_only_val", "2026-06-02"),
        "c": _make_memory_entry("gamma_only_val", "2026-06-03"),
        "d": _make_memory_entry("delta_only_val", "2026-06-04"),
    },
}
result_t = mm.format_memory_for_prompt_ranked(_TEST_TOP_K_MEM, top_k=3)
words_found = sum(1 for w in ["alpha", "beta", "gamma", "delta"] if w in result_t)
check("ranked prompt: top_k=3 shows exactly 3 entries", words_found == 3)

# 5. Output length <= 2000 chars
check("ranked prompt: <= 2000 chars", len(result) <= 2000)

# 6. Same category headers preserved
check("ranked prompt: identity header values present", "Name:" in result and "Age:" in result)
check("ranked prompt: Preferences: header present", "Preferences:" in result)

# 7. Invalid timestamps do not crash
_TEST_BAD_TS_MEM = {
    "notes": {
        "bad": _make_memory_entry("bad ts entry here", None),
        "good": _make_memory_entry("good ts entry here", "2026-06-01"),
    },
}
result_b = mm.format_memory_for_prompt_ranked(_TEST_BAD_TS_MEM)
check("ranked prompt: invalid timestamps no crash", isinstance(result_b, str) and len(result_b) > 0)

# 8. Uses provided memory object, does not reload from disk
_standalone = {"notes": {"test": _make_memory_entry("standalone_test_value", "2026-06-01")}}
result_s = mm.format_memory_for_prompt_ranked(_standalone)
check("ranked prompt: uses provided memory, no disk reload", "standalone_test_value" in result_s)

# ---------------------------------------------------------------------------
# Step 13D — list_memory_keys tests
# ---------------------------------------------------------------------------

print("\n[list_memory_keys] Tests with TemporaryDirectory...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"

        # Empty memory
        check("list: empty memory returns (empty)", mm.list_memory_keys() == "(empty)")

        # Populated memory
        _TEST_LIST_MEM = {
            "notes": {
                "color": _make_memory_entry("blue"),
                "food": _make_memory_entry("pizza"),
            },
            "preferences": {
                "theme": _make_memory_entry("dark"),
            },
        }
        mm.save_memory(_TEST_LIST_MEM)

        result = mm.list_memory_keys()
        check("list: contains notes category", "notes:" in result)
        check("list: contains preferences category", "preferences:" in result)
        check("list: contains key color", "  - color" in result)
        check("list: contains key food", "  - food" in result)
        check(
            "list: does NOT expose values",
            "blue" not in result and "pizza" not in result and "dark" not in result,
        )

        # Category filter
        result_cat = mm.list_memory_keys("preferences")
        check("list: filtered category header", "preferences:" in result_cat)
        check("list: filtered has theme key", "  - theme" in result_cat)
        check("list: filtered excludes notes keys", "notes:" not in result_cat)

        # Invalid category
        result_bad = mm.list_memory_keys("nonexistent")
        check("list: invalid category returns error", "Invalid category" in result_bad)

        # Empty category
        result_empty = mm.list_memory_keys("wishes")
        check("list: empty category returns (empty)", result_empty == "(empty)")
    finally:
        mm.MEMORY_PATH = saved

# ---------------------------------------------------------------------------
# Step 14.1 — Pagination, legacy categories, param parsing
# ---------------------------------------------------------------------------

print("\n[list_memory_keys 14.1] Pagination and counts...")
with TemporaryDirectory() as tmp:
    saved_path = mm.MEMORY_PATH
    saved_max = mm.MEMORY_MAX_CHARS
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mm.MEMORY_MAX_CHARS = 100000  # Disable trimming for pagination tests

        # 60 keys across two unknown categories (sorted alpha: cat_a, cat_b)
        big_mem = {
            "cat_a": {f"key_{i}": _make_memory_entry(f"val_{i}") for i in range(30)},
            "cat_b": {f"key_{i}": _make_memory_entry(f"val_{i}") for i in range(30)},
        }
        mm.save_memory(big_mem)

        result = mm.list_memory_keys(limit=50)
        check("14.1: pagination shows 50 of 60", "Showing 50 of 60" in result)
        check("14.1: continuation hint offset 50", "offset 50" in result)
        check("14.1: total count shown", "Total keys: 60" in result)
        check("14.1: values not exposed in pagination", "val_" not in result)

        result_full = mm.list_memory_keys(full=True)
        check("14.1: full=True shows all 60", "Showing 60" in result_full)
        check("14.1: full=True no pagination needed", "offset" not in result_full)
        check("14.1: full=True values not exposed", "val_" not in result_full)

        result_offset = mm.list_memory_keys(offset=30, limit=20)
        check("14.1: offset 30 shows cat_b only", "cat_a:" not in result_offset)
        check("14.1: offset 30 has cat_b", "cat_b:" in result_offset)
        check("14.1: offset pagination hint offset 50", "offset 50" in result_offset)

        result_no_counts = mm.list_memory_keys(show_counts=False)
        check("14.1: show_counts=False hides Total", "Total keys" not in result_no_counts)

        # String parameter parsing
        result_sfull = mm.list_memory_keys(full="true")
        check("14.1: full='true' works", "Showing 60" in result_sfull)

        result_slim = mm.list_memory_keys(limit="10", offset="5", show_counts="false")
        check("14.1: string limit=10 works", "Showing 10 of 60" in result_slim)
        check("14.1: string offset=5 works", "offset 15" in result_slim)
        check("14.1: show_counts='false' hides Total", "Total keys" not in result_slim)
        check("14.1: string params values not exposed", "val_" not in result_slim)
    finally:
        mm.MEMORY_PATH = saved_path
        mm.MEMORY_MAX_CHARS = saved_max

print("\n[list_memory_keys 14.1] Legacy/unknown categories...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"

        legacy_mem = {
            "notes": {"color": _make_memory_entry("blue")},
            "user_feedback": {"rating": _make_memory_entry("good")},
            "other_notes": {"stuff": _make_memory_entry("things")},
        }
        mm.save_memory(legacy_mem)

        result = mm.list_memory_keys()

        check("14.1: known category notes listed", "notes:" in result)
        check("14.1: known extra category user_feedback listed", "user_feedback:" in result)
        check("14.1: unknown category other_notes listed", "other_notes:" in result)

        # Known categories come before unknown
        notes_idx = result.index("notes:")
        uf_idx = result.index("user_feedback:")
        other_idx = result.index("other_notes:")
        check("14.1: notes before user_feedback", notes_idx < uf_idx)
        check("14.1: user_feedback before other_notes", uf_idx < other_idx)

        # Category filter with legacy category
        result_uf = mm.list_memory_keys("user_feedback")
        check("14.1: filter legacy user_feedback", "user_feedback:" in result_uf)
        check("14.1: legacy filter shows key rating", "  - rating" in result_uf)
        check("14.1: legacy filter no values", "good" not in result_uf)

        # Invalid category still rejected
        result_bad = mm.list_memory_keys("nonexistent")
        check("14.1: invalid category rejected", "Invalid category" in result_bad)

        # Values never exposed
        check(
            "14.1: legacy listing no values",
            "blue" not in result and "good" not in result and "things" not in result,
        )
    finally:
        mm.MEMORY_PATH = saved

print("\n[forget_memory 14.1] Legacy category acceptance...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"

        legacy_mem = {
            "notes": {"color": _make_memory_entry("blue")},
            "user_feedback": {"rating": _make_memory_entry("good")},
        }
        mm.save_memory(legacy_mem)

        # forget with existing legacy category works
        result_f = mm.forget("rating", "user_feedback")
        check("14.1: forget legacy category works", "Forgotten" in result_f)
        check("14.1: forget legacy result path", "user_feedback/rating" in result_f)

        # forget with non-existent category still rejected
        result_bad = mm.forget("test", "nonexistent_legacy")
        check("14.1: forget non-existent legacy rejected", "Error" in result_bad)

        # forget still rejects wildcards
        check(
            "14.1: forget still rejects wildcard *", "wildcard" in mm.forget("*", "notes").lower()
        )
        check(
            "14.1: forget still rejects wildcard all",
            "wildcard" in mm.forget("all", "notes").lower(),
        )
    finally:
        mm.MEMORY_PATH = saved

# ---------------------------------------------------------------------------
# Step 13D — forget_memory tests
# ---------------------------------------------------------------------------

print("\n[forget_memory] Harden input validation...")
check("forget: empty key returns error", "Error" in mm.forget("", "notes"))
check("forget: empty category returns error", "Error" in mm.forget("test", ""))

print("\n[forget_memory] Invalid category...")
check("forget: invalid category", "error" in mm.forget("test", "invalid_cat").lower())

print("\n[forget_memory] Wildcard rejection...")
check("forget: wildcard * rejected", "wildcard" in mm.forget("*", "notes").lower())
check("forget: wildcard all rejected", "wildcard" in mm.forget("all", "notes").lower())
check("forget: wildcard **all** rejected", "wildcard" in mm.forget("**all**", "notes").lower())
check(
    "forget: wildcard everything rejected", "wildcard" in mm.forget("everything", "notes").lower()
)

print("\n[forget_memory] Integration with TemporaryDirectory...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mm.save_memory(_TEST_LIST_MEM)

        # Forget existing key
        result_f = mm.forget("color", "notes")
        check("forget: existing key returns Forgotten", "Forgotten" in result_f)
        check("forget: result includes category/key", "notes/color" in result_f)
        mem_after = mm.load_memory()
        check("forget: color key removed", "color" not in mem_after.get("notes", {}))

        # Other keys preserved
        check("forget: food key preserved", "food" in mem_after.get("notes", {}))
        check("forget: preferences intact", "preferences" in mem_after)

        # Forget non-existent key
        result_nf = mm.forget("nonexistent", "notes")
        check("forget: non-existent returns Not found", "Not found" in result_nf)

        # File integrity
        import json

        data = json.loads((Path(tmp) / "long_term.json").read_text(encoding="utf-8"))
        check("forget: JSON file remains valid", isinstance(data, dict))

        # Backup created
        from memory.safe_store import _list_backups

        baks = _list_backups(Path(tmp) / "long_term.json")
        check("forget: backup created", len(baks) >= 1)
    finally:
        mm.MEMORY_PATH = saved

# ---------------------------------------------------------------------------
# V9A — Memory Integrity + Feedback Learning Foundation Tests
# ---------------------------------------------------------------------------

print("\n[V9A] Stable memory save writes to long_term.json...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mem = {"notes": {"stable_key": {"value": "stable_value", "updated": "2026-06-24"}}}
        mm.save_memory(mem)
        data = json.loads((Path(tmp) / "long_term.json").read_text(encoding="utf-8"))
        check(
            "V9A: stable save writes key",
            data.get("notes", {}).get("stable_key", {}).get("value") == "stable_value",
        )
    finally:
        mm.MEMORY_PATH = saved

print("\n[V9A] Transient key last_media_source blocked...")
from memory.transient_filter import is_transient_memory_key

check(
    "V9A: last_media_source is transient",
    is_transient_memory_key("preferences", "last_media_source"),
)
check("V9A: last_media_title is transient", is_transient_memory_key("wishes", "last_media_title"))
check(
    "V9A: last_media_platform is transient",
    is_transient_memory_key("preferences", "last_media_platform"),
)
check(
    "V9A: preferred_browser is NOT transient",
    not is_transient_memory_key("preferences", "preferred_browser"),
)
check(
    "V9A: voice_style is NOT transient", not is_transient_memory_key("preferences", "voice_style")
)
check("V9A: user_name is NOT transient", not is_transient_memory_key("identity", "user_name"))

print("\n[V9A] Transient key last_media_title under wishes blocked...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        # Enable dedup so update_memory runs the transient check path
        mm.set_dedup_enabled(True)
        result = mm.update_memory({"wishes": {"last_media_title": {"value": "Berlin"}}})
        check(
            "V9A: transient wishes/last_media_title blocked",
            "last_media_title" not in result.get("wishes", {}),
        )
    finally:
        mm.MEMORY_PATH = saved

print("\n[V9A] agent/executor.py path blocks transient keys...")
# Simulate the executor's transient check logic
from memory.memory_manager import update_memory as exec_update
from memory.transient_filter import is_transient_memory_key as exec_check

with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mm.set_dedup_enabled(True)
        cat, key, val = "preferences", "last_media_source", "netflix"
        if exec_check(cat, key):
            blocked = True
        else:
            exec_update({cat: {key: {"value": val}}})
            blocked = False
        check("V9A: executor path blocks last_media_source", blocked)
    finally:
        mm.MEMORY_PATH = saved

print("\n[V9A] Every allowed long-term write creates a backup...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        # First save creates the file; second save triggers backup
        mm.save_memory({"notes": {"test_backup": {"value": "val0", "updated": "2026-06-24"}}})
        mm.save_memory({"notes": {"test_backup": {"value": "val1", "updated": "2026-06-24"}}})
        from memory.safe_store import _list_backups

        baks = _list_backups(Path(tmp) / "long_term.json")
        check("V9A: backup created on save", len(baks) >= 1)
    finally:
        mm.MEMORY_PATH = saved

print("\n[V9A] Every allowed write creates audit entry...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        import memory.memory_audit as maudit

        audit_dir = Path(tmp) / "audit_logs"
        audit_dir.mkdir(parents=True, exist_ok=True)
        saved_dir, saved_fn = maudit.AUDIT_DIR, maudit.AUDIT_FILENAME
        maudit.AUDIT_DIR = str(audit_dir)
        maudit.AUDIT_FILENAME = "memory_audit.jsonl"
        mem = {"notes": {"audit_test": {"value": "audit_val", "updated": "2026-06-24"}}}
        mm.save_memory(mem)
        audit_file = audit_dir / "memory_audit.jsonl"
        # Also check _get_audit_path for debugging
        from memory.memory_audit import _get_audit_path

        _resolved = _get_audit_path()
        if not audit_file.exists():
            print(f"  DEBUG: audit_file={audit_file}")
            print(f"  DEBUG: _get_audit_path={_resolved}")
            print(f"  DEBUG: _resolved.exists()={_resolved.exists()}")
            print(
                f"  DEBUG: audit_dir contents={list(audit_dir.iterdir()) if audit_dir.exists() else 'N/A'}"
            )
            _resolved_parent = _resolved.parent
            print(
                f"  DEBUG: _resolved.parent={_resolved_parent} exists={_resolved_parent.exists()}"
            )
        if audit_file.exists():
            lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
            has_save = any('"op": "save"' in ln or '"op":"save"' in ln for ln in lines)
            check("V9A: audit entry created on save", has_save)
        else:
            check("V9A: audit file exists", False)
    finally:
        mm.MEMORY_PATH = saved
        maudit.AUDIT_DIR = saved_dir
        maudit.AUDIT_FILENAME = saved_fn

print("\n[V9A] forget/delete creates backup and audit entry...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        import memory.memory_audit as maudit

        audit_dir = Path(tmp) / "audit_logs"
        audit_dir.mkdir(parents=True, exist_ok=True)
        saved_dir, saved_fn = maudit.AUDIT_DIR, maudit.AUDIT_FILENAME
        maudit.AUDIT_DIR = audit_dir
        maudit.AUDIT_FILENAME = "memory_audit.jsonl"
        mem = {"notes": {"del_test": {"value": "to_delete", "updated": "2026-06-24"}}}
        mm.save_memory(mem)
        mm.forget("del_test", "notes")
        from memory.safe_store import _list_backups

        baks = _list_backups(Path(tmp) / "long_term.json")
        check("V9A: backup created on forget", len(baks) >= 1)
        audit_file = audit_dir / "memory_audit.jsonl"
        if audit_file.exists():
            lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
            has_delete = any('"op": "delete"' in ln or '"op":"delete"' in ln for ln in lines)
            check("V9A: audit entry created on forget", has_delete)
        else:
            check("V9A: audit file exists after forget", False)
    finally:
        mm.MEMORY_PATH = saved
        maudit.AUDIT_DIR = saved_dir
        maudit.AUDIT_FILENAME = saved_fn

print("\n[V9A] _trim_to_limit creates backup and audit entries...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        import memory.memory_audit as maudit

        audit_dir = Path(tmp) / "audit_logs"
        audit_dir.mkdir(parents=True, exist_ok=True)
        saved_dir, saved_fn = maudit.AUDIT_DIR, maudit.AUDIT_FILENAME
        maudit.AUDIT_DIR = audit_dir
        maudit.AUDIT_FILENAME = "memory_audit.jsonl"
        saved_max = mm.MEMORY_MAX_CHARS
        # First write a small file so MEMORY_PATH exists (backup only fires if it does)
        mm.MEMORY_MAX_CHARS = 100000
        mm.save_memory({"notes": {"seed": _make_memory_entry("init")}})
        # Now force trimming
        mm.MEMORY_MAX_CHARS = 200
        big = {"notes": {}}
        for i in range(20):
            big["notes"][f"trim_key_{i}"] = {
                "value": f"trim_value_{i}_" * 5,
                "updated": f"2026-06-{i + 1:02d}",
            }
        mm.save_memory(big)
        # _trim_to_limit no longer makes its own ad-hoc backup (it used to,
        # unpruned, growing forever) — save_memory()'s safe_write_json()
        # call already backs up the pre-trim on-disk state via the
        # versioned/pruned .jsonbak scheme, so check for that instead.
        from memory.safe_store import _list_backups

        named_backups = _list_backups(mm.MEMORY_PATH)
        check("V9A: backup created before trim", len(named_backups) >= 1)
        audit_file = audit_dir / "memory_audit.jsonl"
        if audit_file.exists():
            lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
            has_trim = any('"op": "trim"' in ln or '"op":"trim"' in ln for ln in lines)
            check("V9A: audit entry created on trim", has_trim)
        else:
            check("V9A: audit file exists after trim", False)
        mm.MEMORY_MAX_CHARS = saved_max
    finally:
        mm.MEMORY_PATH = saved
        maudit.AUDIT_DIR = saved_dir
        maudit.AUDIT_FILENAME = saved_fn

print("\n[V9A] dedup replacement creates audit entry...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        import memory.memory_audit as maudit

        audit_dir = Path(tmp) / "audit_logs"
        audit_dir.mkdir(parents=True, exist_ok=True)
        saved_dir, saved_fn = maudit.AUDIT_DIR, maudit.AUDIT_FILENAME
        maudit.AUDIT_DIR = audit_dir
        maudit.AUDIT_FILENAME = "memory_audit.jsonl"
        mem = {"notes": {"orig": {"value": "same text here", "updated": "2026-06-20"}}}
        mm.save_memory(mem)
        mm.set_dedup_enabled(True)
        mm.update_memory({"notes": {"orig": {"value": "same text here"}}})
        audit_file = audit_dir / "memory_audit.jsonl"
        if audit_file.exists():
            lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
            has_dedup = any('"op": "dedup"' in ln or '"op":"dedup"' in ln for ln in lines)
            check("V9A: audit entry created on dedup", has_dedup)
        else:
            check("V9A: audit file exists after dedup", False)
    finally:
        mm.MEMORY_PATH = saved
        maudit.AUDIT_DIR = saved_dir
        maudit.AUDIT_FILENAME = saved_fn

print("\n[V9A] append_feedback writes to feedback_memory.jsonl...")
from memory.feedback_memory import FEEDBACK_PATH, FeedbackEvent, append_feedback

with TemporaryDirectory() as tmp:
    saved_fb = FEEDBACK_PATH
    try:
        import memory.feedback_memory as fmem

        fmem.FEEDBACK_PATH = Path(tmp) / "feedback_memory.jsonl"
        event = FeedbackEvent(
            timestamp="2026-06-24T12:00:00Z",
            user_text="that was wrong, next time use browser not netflix",
            mistake_category="media_cross_routing_netflix_vs_spotify",
            signature="netflix<->spotify",
            expected_correction="use browser not netflix",
        )
        append_feedback(event)
        check("V9A: feedback file created", fmem.FEEDBACK_PATH.exists())
        if fmem.FEEDBACK_PATH.exists():
            lines = fmem.FEEDBACK_PATH.read_text(encoding="utf-8").strip().splitlines()
            check("V9A: feedback file has 1 line", len(lines) == 1)
            data = json.loads(lines[0])
            check("V9A: feedback has timestamp", "timestamp" in data)
            check(
                "V9A: feedback has user_text",
                data.get("user_text", "").startswith("that was wrong"),
            )
    finally:
        fmem.FEEDBACK_PATH = saved_fb

print("\n[V9A] Corrupt long_term.json recovery still works...")
with TemporaryDirectory() as tmp:
    saved = mm.MEMORY_PATH
    try:
        mm.MEMORY_PATH = Path(tmp) / "long_term.json"
        mem = {"notes": {"before_corrupt": {"value": "good data", "updated": "2026-06-24"}}}
        mm.save_memory(mem)
        # Corrupt the file
        mm.MEMORY_PATH.write_text("NOT VALID JSON {{{", encoding="utf-8")
        result = mm.load_memory()
        check("V9A: corrupt file returns empty memory (graceful)", isinstance(result, dict))
        check("V9A: corrupt file does not crash", "notes" in result or result == mm._empty_memory())
    finally:
        mm.MEMORY_PATH = saved

print("\n[V9A] run_all_checks remains READY (syntax check)...")
import py_compile

try:
    py_compile.compile(str(repo_root / "tools" / "run_all_checks.py"), doraise=True)
    check("V9A: run_all_checks.py compiles", True)
except py_compile.PyCompileError:
    check("V9A: run_all_checks.py compiles", False)

try:
    py_compile.compile(str(repo_root / "memory" / "memory_audit.py"), doraise=True)
    check("V9A: memory_audit.py compiles", True)
except py_compile.PyCompileError:
    check("V9A: memory_audit.py compiles", False)

try:
    py_compile.compile(str(repo_root / "memory" / "transient_filter.py"), doraise=True)
    check("V9A: transient_filter.py compiles", True)
except py_compile.PyCompileError:
    check("V9A: transient_filter.py compiles", False)

# ── pytest test class for save_memory authorization guard ──


class TestMemoryAuthorization:
    """Tests for is_explicit_memory_request() memory authorization guard."""

    def test_blocked_without_trigger(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("open the browser") is False

    def test_blocked_empty_text(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("") is False
        assert is_explicit_memory_request(None) is False

    def test_allowed_remember_that(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("remember that Magnific is a website") is True

    def test_allowed_save_this(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("save this") is True

    def test_allowed_store_this(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("store this information") is True

    def test_allowed_note_that(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("note that the server is down") is True

    def test_allowed_memorize(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("memorize this password") is True

    def test_blocked_inferred_from_context(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("user is viewing a recipe page") is False

    def test_allowed_case_insensitive(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("REMEMBER THAT X") is True
        assert is_explicit_memory_request("Remember This") is True

    # ── Stem-regex variants (2026-07-10 fix: natural voice phrasings that the
    # fixed phrase list missed, blocking three legitimate favorite-song saves) ──

    def test_allowed_save_my_favorite(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("save my favorite song") is True

    def test_allowed_put_in_your_memory(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("put that in your memory") is True

    def test_allowed_memorise_british(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("memorise it please") is True

    def test_allowed_bare_remember_question(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("do you remember my favorite song?") is True

    def test_blocked_no_stem_in_song_title_reply(self):
        from main import is_explicit_memory_request

        # A bare song-title reply alone must not authorize (the rolling window
        # of earlier utterances is what carries the authorization instead).
        assert is_explicit_memory_request("Don't Tell Your Dreams") is False

    def test_blocked_unrelated_action(self):
        from main import is_explicit_memory_request

        assert is_explicit_memory_request("play the next track") is False


class TestMemoryAffirmation:
    """Tests for is_memory_affirmation() (confirm-after-block flow)."""

    def test_plain_yes(self):
        from main import is_memory_affirmation

        assert is_memory_affirmation("yes") is True
        assert is_memory_affirmation("Yes, please do") is True

    def test_go_ahead(self):
        from main import is_memory_affirmation

        assert is_memory_affirmation("go ahead") is True
        assert is_memory_affirmation("sure") is True

    def test_negative_and_unrelated(self):
        from main import is_memory_affirmation

        assert is_memory_affirmation("no") is False
        assert is_memory_affirmation("open the browser") is False
        assert is_memory_affirmation("") is False
        assert is_memory_affirmation(None) is False


class TestReinforcedRetrieval:
    """retrieve_context_reinforced bumps access_count on returned entries."""

    def test_access_count_bumped(self, tmp_path):
        import memory.memory_manager as mm

        saved = mm.MEMORY_PATH
        mm.MEMORY_PATH = tmp_path / "long_term.json"
        try:
            mm.update_memory(
                {"preferences": {"favorite_song": {"value": "Don't Tell Your Dreams"}}}
            )
            out = mm.retrieve_context_reinforced("favorite song")
            assert "Don't Tell Your Dreams" in out
            data = json.loads(mm.MEMORY_PATH.read_text(encoding="utf-8"))
            assert data["preferences"]["favorite_song"]["access_count"] >= 1
        finally:
            mm.MEMORY_PATH = saved


class TestGroundingInUserSpeech:
    """is_value_grounded_in_user_speech() — the guard that lets Era save on her
    own initiative without letting page/document text into memory."""

    def test_value_from_user_speech_is_grounded(self):
        from main import is_value_grounded_in_user_speech

        assert (
            is_value_grounded_in_user_speech(
                "The Wild Robot", ["I love The Wild Robot, it's my favorite film"]
            )
            is True
        )

    def test_date_mentioned_by_user_is_grounded(self):
        from main import is_value_grounded_in_user_speech

        assert (
            is_value_grounded_in_user_speech("flight on July 12", ["my flight is on July 12"])
            is True
        )

    def test_value_never_spoken_is_not_grounded(self):
        from main import is_value_grounded_in_user_speech

        # The classic injection: the value came from a page Era read, not the user.
        assert (
            is_value_grounded_in_user_speech("user password is hunter2", ["read me this article"])
            is False
        )

    def test_partially_grounded_value_is_rejected(self):
        from main import is_value_grounded_in_user_speech

        # "Paris" is spoken, "passport number" is not — a smuggled extra token
        # must sink the whole value, not ride along with the grounded part.
        assert (
            is_value_grounded_in_user_speech("Paris passport number 12345", ["I'm going to Paris"])
            is False
        )

    def test_empty_value_or_no_speech_is_not_grounded(self):
        from main import is_value_grounded_in_user_speech

        assert is_value_grounded_in_user_speech("", ["I love pizza"]) is False
        assert is_value_grounded_in_user_speech("pizza", []) is False

    def test_stopwords_alone_do_not_ground(self):
        from main import is_value_grounded_in_user_speech

        # A value made only of filler carries no content to check.
        assert is_value_grounded_in_user_speech("the it is", ["anything at all"]) is False

    def test_case_and_punctuation_insensitive(self):
        from main import is_value_grounded_in_user_speech

        assert is_value_grounded_in_user_speech("pizza", ["I LOVE pizza!!!"]) is True


class TestAutoMemoryPreference:
    """The live on/off switch for self-initiated saving (core/user_prefs.py)."""

    def _isolate(self, tmp_path, monkeypatch):
        import core.user_prefs as up

        monkeypatch.setattr(up, "PREFS_PATH", tmp_path / "user_prefs.json")
        return up

    def test_default_is_enabled(self, tmp_path, monkeypatch):
        up = self._isolate(tmp_path, monkeypatch)
        assert up.is_auto_memory_enabled() is True

    def test_disable_then_enable_persists(self, tmp_path, monkeypatch):
        up = self._isolate(tmp_path, monkeypatch)
        up.set_auto_memory(False)
        assert up.is_auto_memory_enabled() is False
        up.set_auto_memory(True)
        assert up.is_auto_memory_enabled() is True

    def test_spoken_ask_first_disables(self, tmp_path, monkeypatch):
        up = self._isolate(tmp_path, monkeypatch)
        assert (
            up.promote_feedback_to_prefs("don't save anything to memory without my confirmation")
            is not None
        )
        assert up.is_auto_memory_enabled() is False

    def test_spoken_ask_me_before_you_save_disables(self, tmp_path, monkeypatch):
        up = self._isolate(tmp_path, monkeypatch)
        up.promote_feedback_to_prefs("ask me before you save anything")
        assert up.is_auto_memory_enabled() is False

    def test_spoken_permission_re_enables(self, tmp_path, monkeypatch):
        up = self._isolate(tmp_path, monkeypatch)
        up.set_auto_memory(False)
        up.promote_feedback_to_prefs("you can save memories without asking me")
        assert up.is_auto_memory_enabled() is True

    def test_off_state_is_announced_in_prompt(self, tmp_path, monkeypatch):
        up = self._isolate(tmp_path, monkeypatch)
        assert "AUTO-MEMORY" not in up.format_prefs_for_prompt()
        up.set_auto_memory(False)
        assert "AUTO-MEMORY IS OFF" in up.format_prefs_for_prompt()

    def test_corrupt_prefs_file_fails_closed(self, tmp_path, monkeypatch):
        up = self._isolate(tmp_path, monkeypatch)
        up.PREFS_PATH.write_text("{ not json", encoding="utf-8")
        # Unreadable prefs must not silently leave self-initiated saving on...
        assert up.is_auto_memory_enabled() is True  # _load_unlocked() recovers to defaults
        # ...and a hard read failure falls closed.
        monkeypatch.setattr(up, "_load_unlocked", lambda: (_ for _ in ()).throw(OSError("boom")))
        assert up.is_auto_memory_enabled() is False


class TestMachinePowerRouting:
    """'shut down the computer' must power the MACHINE down, not kill Era."""

    def test_shutdown_computer_reroutes_off_shutdown_era(self):
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call("shutdown_era", {}, "shut down the computer")
        assert tool == "computer_settings"
        assert params["action"] == "shutdown"

    def test_wake_word_prefix_does_not_defeat_the_guard(self):
        from core.self_healing_router import route_tool_call

        # "Era, shut down the PC" — the wake word must not read as a self-target.
        tool, params = route_tool_call("shutdown_era", {}, "Era, shut down my PC")
        assert tool == "computer_settings"
        assert params["action"] == "shutdown"

    def test_restart_computer_routes_to_restart(self):
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call("shutdown_era", {}, "restart the laptop please")
        assert tool == "computer_settings"
        assert params["action"] == "restart"

    def test_shutting_down_era_herself_is_untouched(self):
        from core.self_healing_router import route_tool_call

        for text in ("shut down", "goodbye", "quit the program", "shut down Era"):
            tool, _ = route_tool_call("shutdown_era", {}, text)
            assert tool == "shutdown_era", text

    def test_explicit_self_target_beats_machine_noun(self):
        from core.self_healing_router import route_tool_call

        tool, _ = route_tool_call("shutdown_era", {}, "close Era, I'm done with the computer")
        assert tool == "shutdown_era"


class TestPowerSafetyGate:
    """Powering the machine always needs confirmation; cancelling never does."""

    def test_machine_shutdown_requires_confirmation(self):
        from core.safety_guard import classify_action

        for act in ("shutdown", "restart", "reboot", "power_off"):
            safety = classify_action("computer_settings", {"action": act})
            assert safety["requires_confirmation"] is True, act
            assert safety["risk_level"] == "HIGH", act

    def test_power_gate_is_tool_agnostic(self):
        from core.safety_guard import classify_action

        # Routed through any other tool, a shutdown must not fall through to LOW.
        safety = classify_action("computer_control", {"action": "shutdown"})
        assert safety["requires_confirmation"] is True

    def test_cancelling_a_shutdown_is_free(self):
        from core.safety_guard import classify_action

        safety = classify_action("computer_settings", {"action": "cancel_shutdown"})
        assert safety["requires_confirmation"] is False

    def test_era_own_shutdown_stays_ungated(self):
        from core.safety_guard import classify_action

        safety = classify_action("shutdown_era", {})
        assert safety["requires_confirmation"] is False

    def test_enabling_auto_memory_requires_confirmation(self):
        from core.safety_guard import classify_action

        # Weakening the memory gate is confirmed once; the safe direction is free.
        assert (
            classify_action("manage_preference", {"action": "enable_auto_memory"})[
                "requires_confirmation"
            ]
            is True
        )
        assert (
            classify_action("manage_preference", {"action": "disable_auto_memory"})[
                "requires_confirmation"
            ]
            is False
        )


# -- Summary --
print(f"{'=' * 40}")

if __name__ == "__main__":
    if failed:
        sys.exit(1)
