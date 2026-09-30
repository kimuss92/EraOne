"""
text_edit.py — precise, verified edits to plain-text files for file_processor.

Why this exists: file_processor's "fix" action used to send Gemini a fixed
"fix grammar and style" prompt and silently DROP the user's instruction, then
save the rewrite to a new "<stem>_fix.txt" sibling. A request like "change
10-second to 15-second" therefore never reached the model, every retry read an
older copy, and nothing checked the result — so the file kept saying 10-second
while Era reported success.

The rule now: the LLM may only PLAN an edit as literal find/replace pairs.
Code applies them, edits the file in place (original backed up first), then
re-reads the file from disk and verifies every change landed. An edit that
cannot be applied exactly changes nothing and says so.
"""

import json
import re
import shutil
import time
from pathlib import Path

_DEFAULT_BACKUP_DIR = Path(__file__).resolve().parent.parent / "runtime" / "file_backups"

# Characters a user (or model) types as "-" or a quote but a file may store as
# a typographic variant. Used only as a fallback when the exact text is absent.
_DASHES = "-‐‑‒–—−"
_QUOTES = {"'": "'‘’", '"': '"“”'}

_PLAN_PROMPT = """You are planning precise edits to a text file.

USER INSTRUCTION:
{instruction}

Return ONLY a JSON object, no prose, in exactly this shape:
{{"edits": [{{"find": "<text copied verbatim from the file>", "replace": "<new text>"}}]}}

Rules:
- "find" must be copied character-for-character from the file below.
- Change ONLY what the instruction asks for. Do not fix grammar, reword, or restyle anything else.
- Every occurrence of "find" in the file is replaced, so add surrounding context if only one occurrence should change.
- Include one edit per distinct change the instruction implies.
- If the instruction asks for no change to this file, return {{"edits": []}}.

FILE CONTENT:
<<<FILE
{content}
FILE>>>"""


def _fuzzy_pattern(find: str) -> re.Pattern:
    """Regex matching ``find`` while tolerating dash/quote variants and whitespace runs."""
    parts = []
    for ch in find:
        if ch in _DASHES:
            parts.append("[" + re.escape(_DASHES) + "]")
        elif ch in _QUOTES:
            parts.append("[" + re.escape(_QUOTES[ch]) + "]")
        elif ch.isspace():
            parts.append(r"\s+")
        else:
            parts.append(re.escape(ch))
    # Collapse consecutive \s+ produced by multi-space runs.
    return re.compile(re.sub(r"(\\s\+)+", r"\\s+", "".join(parts)))


def apply_literal_edits(text: str, edits: list[dict]) -> tuple[str, list[str], list[str]]:
    """Apply find/replace pairs. Returns (new_text, applied_notes, failures).

    Exact substring match first; falls back to a dash/quote/whitespace-tolerant
    match. No regex semantics are exposed to the caller — ``find`` is literal.
    """
    applied, failures = [], []
    for edit in edits:
        find = str(edit.get("find", ""))
        replace = str(edit.get("replace", ""))
        if not find:
            failures.append("an edit had an empty 'find'")
            continue
        if find == replace:
            continue
        count = text.count(find)
        if count:
            text = text.replace(find, replace)
            applied.append(f"'{find}' → '{replace}' ({count}x)")
            continue
        pattern = _fuzzy_pattern(find)
        # A function, not a string, so "\1" or "\g" in the replacement stays literal.
        text, count = pattern.subn(lambda _m, r=replace: r, text)
        if count:
            applied.append(f"'{find}' → '{replace}' ({count}x, matched typographic variant)")
        elif _already_applied(text, replace):
            # 2026-09-28: a deletion landed, then Era re-sent it three times and each
            # "not found" read as a fresh failure, so it kept retrying. An edit whose
            # result is already in the file is a no-op, not an error.
            continue
        else:
            failures.append(f"'{find}' not found in the file")
    return text, applied, failures


def _already_applied(text: str, replace: str) -> bool:
    """True when the target state of an edit whose ``find`` is absent is already present.

    A deletion (empty ``replace``) with nothing left to delete is done; otherwise
    the replacement text must already appear in the file.
    """
    if not replace.strip():
        return True
    return replace in text or bool(_fuzzy_pattern(replace).search(text))


def _parse_plan(raw: str) -> list[dict]:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return JSON")
    data = json.loads(raw[start : end + 1])
    edits = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(edits, list):
        raise ValueError("model JSON has no 'edits' list")
    return [e for e in edits if isinstance(e, dict)]


def plan_edits(content: str, instruction: str, model) -> list[dict]:
    """Ask the model for literal find/replace pairs implementing ``instruction``."""
    response = model.generate_content(_PLAN_PROMPT.format(instruction=instruction, content=content))
    return _parse_plan(response.text or "")


def _read(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    # newline="" semantics: keep \r\n exactly as stored.
    return raw.decode(encoding), encoding


def edit_text_file(
    path: Path,
    *,
    instruction: str = "",
    find: str = "",
    replace: str | None = None,
    model_factory=None,
    backup_dir: Path | None = None,
) -> str:
    """Edit ``path`` in place and verify on disk. Returns a user-facing message.

    Deterministic when ``find`` is given (no LLM). Otherwise ``instruction`` is
    turned into a find/replace plan by ``model_factory()``.
    """
    try:
        content, encoding = _read(path)
    except UnicodeDecodeError:
        return f"Edit failed: {path} is not UTF-8 text; refusing to rewrite it."
    except OSError as e:
        return f"Edit failed: could not read {path}: {e}"

    if find:
        edits = [{"find": find, "replace": "" if replace is None else replace}]
    elif instruction.strip():
        if model_factory is None:
            return "Edit failed: no model available to interpret the instruction."
        try:
            edits = plan_edits(content, instruction, model_factory())
        except Exception as e:
            return f"Edit failed: could not plan the edit ({e}). File unchanged: {path}"
    else:
        return "Edit failed: give an instruction, or 'find' and 'replace' text."

    if not edits:
        return f"No change needed — the instruction does not apply to {path}."

    new_content, applied, failures = apply_literal_edits(content, edits)
    if failures:
        return f"Edit failed: {'; '.join(failures)}. Nothing was written — file unchanged: {path}"
    if new_content == content:
        if find and not (replace or "").strip():
            return f"No change needed — that text is already gone from {path} (already removed?)."
        return f"No change needed — {path} already matches the requested text."

    # A literal edit can drop a newline and still "verify on disk" — that is how
    # browser_control.py went unimportable on 2026-09-26. Refuse to write Python
    # that no longer parses (only when the original did, so broken files stay fixable).
    if path.suffix.lower() in (".py", ".pyw"):
        try:
            compile(content, str(path), "exec", dont_inherit=True)
        except SyntaxError:
            pass
        else:
            try:
                compile(new_content, str(path), "exec", dont_inherit=True)
            except SyntaxError as e:
                return (
                    f"Edit failed: the change would break Python syntax "
                    f"(line {e.lineno}: {e.msg}). Nothing was written — file unchanged: {path}"
                )

    backup_dir = backup_dir or _DEFAULT_BACKUP_DIR
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{path.name}"
        shutil.copy2(path, backup)
        tmp = path.with_name(path.name + ".era-tmp")
        tmp.write_bytes(new_content.encode(encoding))
        tmp.replace(path)
    except OSError as e:
        return f"Edit failed: could not write {path}: {e}"

    # Verify against what is actually on disk, not what we meant to write.
    on_disk, _ = _read(path)
    if on_disk != new_content:
        return f"Edit failed: {path} did not persist the change (on-disk content differs)."
    leftovers = [e["find"] for e in edits if e["find"] not in e["replace"] and e["find"] in on_disk]
    if leftovers:
        return f"Edit failed: still present after writing: {', '.join(map(repr, leftovers))}."

    return (
        f"Edited {path} in place and verified on disk: "
        + "; ".join(applied)
        + (f". Original backed up to {backup}")
    )
