# Design Spec: Shared Candidate Resolver for File/Folder Finding

**Date:** 2026-06-27
**Status:** Final — Awaiting Build Approval
**Scope:** File/folder finding and opening only. No changes to creation, editing, backup, Spotify, browser, weather, wake word, or session logic.

---

## 1. Files to Modify

| File | Change |
|------|--------|
| `actions/file_controller.py` | Add `find_file_folder_candidates()` helper. Update `find_files()` and `open_file_or_folder()` to use it. Add OneDrive/project root detection. Add skip directories. |
| `main.py:449-467` | Add `item_type`, `recursive`, `max_results` to `file_controller` schema |
| `core/prompt.txt:168` | Strengthen routing rules for find vs open, ambiguity handling |
| `tools/test_file_candidates.py` | New test file for candidate resolver |
| `tools/test_file_controller.py` | Update existing tests for new behavior |

No changes to: `core/utils.py`, `actions/code_helper.py`, `core/safety_guard.py`, `conftest.py`, Spotify/browser/weather/session files.

---

## 2. New Helper Function

### `find_file_folder_candidates()`

**Signature:**

```python
def find_file_folder_candidates(
    query: str,
    roots: list[Path] | None = None,
    item_type: str = "both",         # "file" | "folder" | "both"
    extension: str | None = None,
    max_results: int = 10,
    timeout_sec: float = 3.0,
) -> dict:
```

**Returns:**

```python
{
    "status": "complete" | "partial",
    "candidates": list[dict],
    "query": str,
    "message": str | None
}
```

---

## 3. Skip Directories

```python
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    "dist", "build", ".tox", ".eggs",
}
```

Applied in `os.walk()` by modifying `dirnames` in-place.

---

## 4. Search Root Strategy

Default roots when `roots=None`:
- Desktop (Priority 1)
- Downloads (Priority 2)
- Documents (Priority 3)
- OneDrive (Priority 4, optional, non-failing)
- Project folder / BASE_DIR (Priority 5)

Home directory: shallow depth only (max 2 levels).

---

## 5. Scoring Strategy

### Match Types (in order)

| Match Type | Score Range | Description |
|------------|-------------|-------------|
| `exact` | 1.0 | Exact name match |
| `stem` | 0.95 | Filename without extension matches |
| `contains` | 0.70-0.85 | Query contained in name |
| `extension` | 0.60 | Extension matches query |
| `fuzzy` | 0.0-0.50 | SequenceMatcher ratio * 0.5 |

### Final Score Factors

- Base: raw_score * 100
- Type match bonus: +10 (exact) / +5 (both) / -5 (wrong)
- Root priority: +10 (Desktop) to +0 (deep roots)
- Recency: +8 (<1 day) / +5 (<7 days) / +2 (<30 days)
- Path depth: +10 (shallow) to +0 (deep)

Normalized to 0.0-1.0.

---

## 6. Auto-Open Rules (Critical)

### Allowed Auto-Open

1. Exact path exists (fast path, no scanning)
2. `match_type == "exact"` and score >= 0.95
3. `match_type == "stem"` and score >= 0.90
4. `match_type == "contains"` AND score >= 0.85 AND second candidate score < 0.60

### Never Auto-Open

- Fuzzy matches (even with high score)
- Multiple close candidates (within 15% of each other)
- Score < 0.70

### Decision Logic

```python
def _should_auto_open(candidates: list[dict]) -> tuple[bool, str | None]:
    if not candidates:
        return False, "No matches found"
    
    top = candidates[0]
    
    if top["match_type"] == "fuzzy":
        return False, "Fuzzy match — please verify"
    
    if len(candidates) == 1 and top["score"] >= 0.85:
        return True, None
    
    if len(candidates) >= 2:
        if top["score"] >= 0.85 and candidates[1]["score"] < 0.60:
            return True, None
        if (top["score"] - candidates[1]["score"]) < 0.15:
            return False, "Multiple close matches"
    
    if top["score"] < 0.70:
        return False, "Low confidence match"
    
    if len(candidates) == 1 and top["score"] >= 0.70:
        return True, None
    
    return False, "Ambiguous results"
```

---

## 7. Timeout Behavior

- Return best candidates found so far
- Set `status = "partial"`
- Include message: "Search timed out — showing partial results"
- Do NOT auto-open partial results
- Do NOT crash

---

## 8. Structured Result Format

### Multiple Matches

```json
{
    "status": "multiple_matches",
    "query": "report",
    "candidates": [
        {
            "name": "report.pdf",
            "path": "C:/Users/.../Documents/report.pdf",
            "type": "file",
            "score": 0.94,
            "match_type": "contains",
            "modified": "2026-06-27 10:30",
            "source": "Documents"
        }
    ],
    "message": "I found 2 matches. Which one should I open?"
}
```

### Backward Compatibility

- `find_files()` keeps plain text return
- `open_file_or_folder()` returns plain text for single matches
- Structured JSON only for ambiguity resolution

---

## 9. Tool Schema Changes

Added parameters:
- `item_type`: "file" | "folder" | "both" (renamed from `type` to avoid JSON schema confusion)
- `recursive`: BOOLEAN (default true)
- `max_results`: INTEGER (default 10, max 50)

---

## 10. Prompt Changes

Add after line 168:

```
File/Folder Finding Rule: Use file_controller(action="find", name="X") to search for files or folders by name. Use file_controller(action="open", name="X") to search and open in one step.

Ambiguity Rule: If multiple matches are found, the tool will return a list of candidates. Ask the user which one to open. Do NOT guess or auto-open when multiple candidates exist.

Search Roots: Jarvis searches Desktop, Downloads, Documents, OneDrive (if available), and the current project folder. It does NOT search the full C drive by default.
```

---

## 11. Tests

All tests use temporary directories. No dependence on real user folders.

| Test | Description |
|------|-------------|
| `test_exact_file_found` | Exact filename → single candidate, score >= 0.95 |
| `test_exact_folder_found` | Exact folder → single candidate, type="folder" |
| `test_stem_match` | "report" matches "report.pdf" → match_type="stem" |
| `test_contains_match` | "report" matches "annual_report.pdf" → match_type="contains" |
| `test_extension_filter` | extension=".pdf" → only PDFs |
| `test_type_filter_file` | item_type="file" → no folders |
| `test_type_filter_folder` | item_type="folder" → only folders |
| `test_multiple_matches` | 3 matches → all returned, sorted by score |
| `test_fuzzy_never_auto_opens` | Fuzzy match → not auto-opened |
| `test_low_fuzzy_no_open` | Fuzzy < 0.6 → no candidates |
| `test_onedrive_detection_optional` | No OneDrive → no failure |
| `test_project_folder_in_roots` | BASE_DIR in roots |
| `test_protected_path_blocked` | C:\Windows paths blocked |
| `test_shallow_depth_home` | Home limited to 2 levels |
| `test_scoring_ranking` | Exact > contains > fuzzy |
| `test_recency_bonus` | Recent file scores higher |
| `test_timeout_returns_partial` | Timeout → partial results |
| `test_skip_directories` | .git, node_modules skipped |
| `test_exact_path_opens_directly` | Full path → no scanning |
| `test_multiple_close_candidates` | Within 15% → multiple_matches |
| `test_windows_indexer_failure_fallback` | Indexer fails → Python resolver works |
| `test_resolver_shared_by_find_and_open` | Both use same resolver |

---

## 12. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| OneDrive detection fails | Low | Graceful fallback |
| Windows Indexer unavailable | Low | Python resolver is primary |
| Performance on large folders | Medium | Timeout + skip dirs |
| Gemini chooses `open_app` | Medium | Strengthened prompt |
| Backward compatibility | Low | Plain text preserved |

---

## 13. Rollback Notes

Revert to `.checkpoints/milestone-file-folder-safety-overwrite-backup-schema/` if issues arise. No impact on creation/editing/backup, Spotify, browser, weather, session, or safety gate.

---

## 14. Implementation Order

1. Add `SKIP_DIRS` constant
2. Add `_compute_match()` helper
3. Add `_compute_final_score()` helper
4. Add `_detect_onedrive()` and `_get_project_folder()` helpers
5. Add `_get_search_roots()` helper
6. Add `find_file_folder_candidates()` main helper
7. Add `_should_auto_open()` decision logic
8. Update `open_file_or_folder()` to use resolver
9. Update `find_files()` to use resolver
10. Update `file_controller()` dispatcher
11. Update `main.py` schema
12. Update `core/prompt.txt`
13. Create `tools/test_file_candidates.py`
14. Run full test suite
15. Live verification
