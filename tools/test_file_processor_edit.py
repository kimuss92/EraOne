"""Regression: file_processor could not change "10-second" to "15-second".

action="fix" sent Gemini a canned grammar-fix prompt and dropped the
instruction, then saved to a new "<stem>_fix.txt" — so seven increasingly
emphatic retries (2026-09-18) each produced another copy still saying
10-second while the tool reported success. Edits are now planned as literal
find/replace pairs, applied in place by code, and verified on disk.
"""

import json
from types import SimpleNamespace

import actions.file_processor as fp
from actions.text_edit import apply_literal_edits, edit_text_file
from core.tool_result import ToolResult

PROMPT = (
    "Starting from the reference kitchen image, create a smooth 10-second vertical "
    "architectural animation in this exact sequence:\r\n[0:00-0:02] The kitchen begins.\r\n"
)


def _read_exact(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


class FakeModel:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def generate_content(self, contents):
        self.prompts.append(contents)
        return SimpleNamespace(text=self.reply)


def _use_model(monkeypatch, reply: str) -> FakeModel:
    model = FakeModel(reply)
    monkeypatch.setattr(fp, "_gemini_client", lambda: model)
    return model


def _backups(monkeypatch, tmp_path):
    import actions.text_edit as te

    monkeypatch.setattr(te, "_DEFAULT_BACKUP_DIR", tmp_path / "backups")
    return tmp_path / "backups"


def test_replace_is_deterministic_in_place_and_verified(monkeypatch, tmp_path):
    backups = _backups(monkeypatch, tmp_path)
    model = _use_model(monkeypatch, "should never be called")
    f = tmp_path / "video prompt.txt"
    f.write_bytes(PROMPT.encode("utf-8"))

    result = fp.file_processor(
        {"file_path": str(f), "action": "replace", "find": "10-second", "replace": "15-second"}
    )

    on_disk = f.read_bytes().decode("utf-8")
    assert "15-second" in on_disk and "10-second" not in on_disk
    assert "\r\n" in on_disk  # line endings preserved
    assert ToolResult.from_any(result).success, result
    assert str(f) in result  # full path, not just the name
    assert model.prompts == []  # no LLM involved
    assert not list(tmp_path.glob("*_fix*"))  # no sibling copies
    assert [p.read_bytes() for p in backups.iterdir()] == [PROMPT.encode("utf-8")]


def test_fix_with_instruction_reaches_the_model_and_edits_in_place(monkeypatch, tmp_path):
    _backups(monkeypatch, tmp_path)
    plan = json.dumps({"edits": [{"find": "10-second", "replace": "15-second"}]})
    model = _use_model(monkeypatch, f"```json\n{plan}\n```")
    f = tmp_path / "p.txt"
    f.write_text(PROMPT, encoding="utf-8", newline="")

    instruction = "Change the duration in the prompt from 10 seconds to 15 seconds."
    result = fp.file_processor({"file_path": str(f), "action": "fix", "instruction": instruction})

    assert instruction in model.prompts[0]  # the bug: this used to be dropped
    assert "Fix grammar, spelling and style" not in model.prompts[0]
    assert "15-second" in f.read_text(encoding="utf-8")
    assert ToolResult.from_any(result).success, result


def test_unmatchable_plan_writes_nothing_and_reports_failure(monkeypatch, tmp_path):
    backups = _backups(monkeypatch, tmp_path)
    plan = json.dumps({"edits": [{"find": "20-second", "replace": "15-second"}]})
    _use_model(monkeypatch, plan)
    f = tmp_path / "p.txt"
    f.write_text(PROMPT, encoding="utf-8", newline="")

    result = fp.file_processor({"file_path": str(f), "action": "edit", "instruction": "x"})

    assert _read_exact(f) == PROMPT
    assert not ToolResult.from_any(result).success, result
    assert "20-second" in result
    assert not backups.exists()


def test_unparseable_plan_is_a_failure_not_a_silent_success(monkeypatch, tmp_path):
    _use_model(monkeypatch, "Here's the revised text with improved grammar: ...")
    f = tmp_path / "p.txt"
    f.write_text(PROMPT, encoding="utf-8", newline="")

    result = fp.file_processor({"file_path": str(f), "action": "fix", "instruction": "15s"})

    assert not ToolResult.from_any(result).success, result
    assert _read_exact(f) == PROMPT


def test_typographic_dash_in_file_still_matches(tmp_path):
    new, applied, failures = apply_literal_edits(
        "a smooth 10‑second clip", [{"find": "10-second", "replace": "15-second"}]
    )
    assert new == "a smooth 15-second clip" and applied and not failures


def test_utf8_bom_is_preserved(tmp_path):
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbf10-second")
    result = edit_text_file(f, find="10-second", replace="15-second", backup_dir=tmp_path / "b")
    assert f.read_bytes() == b"\xef\xbb\xbf15-second", result


def test_code_file_fix_with_instruction_is_precise(monkeypatch, tmp_path):
    _backups(monkeypatch, tmp_path)
    _use_model(
        monkeypatch, json.dumps({"edits": [{"find": "TIMEOUT = 10", "replace": "TIMEOUT = 15"}]})
    )
    f = tmp_path / "cfg.py"
    f.write_text("TIMEOUT = 10\nRETRIES = 3\n", encoding="utf-8")

    fp.file_processor({"file_path": str(f), "action": "fix", "instruction": "timeout to 15"})

    assert f.read_text(encoding="utf-8") == "TIMEOUT = 15\nRETRIES = 3\n"
    assert not (tmp_path / "cfg_fix.py").exists()


def test_plain_grammar_fix_strips_chatty_preamble(monkeypatch, tmp_path):
    body = "Corrected text. " * 60
    _use_model(
        monkeypatch,
        f"Here's the revised text with improved grammar, spelling, and style:\n\n{body}",
    )
    f = tmp_path / "essay.txt"
    f.write_text("teh text", encoding="utf-8")

    result = fp.file_processor({"file_path": str(f), "action": "fix"})

    out = tmp_path / "essay_fix.txt"
    assert out.read_text(encoding="utf-8").startswith("Corrected text.")
    assert str(out) in result


def test_python_edit_that_breaks_syntax_is_refused(tmp_path):
    """2026-09-26: a "fix" joined `try:` and the next statement onto one line,
    "verified on disk", and left browser_control.py unimportable."""
    src = "def f():\n    try:\n        x = 1\n    except Exception:\n        pass\n"
    f = tmp_path / "mod.py"
    f.write_text(src, encoding="utf-8")

    result = edit_text_file(
        f,
        find="    try:\n        x = 1",
        replace="    try:        x = [1]\n        if x:\n            pass",
        backup_dir=tmp_path / "backups",
    )

    assert "break Python syntax" in result, result
    assert f.read_text(encoding="utf-8") == src
    assert not (tmp_path / "backups").exists()


# 2026-09-28: a deletion succeeded, then Era re-sent the same edit three times;
# each "not found" was reported as a failure and fed the auto-report bridge.
REPORT = "Intro.\r\n\r\n1.  **File System Errors:** Desktop writes fail.\r\n\r\n2.  **Spotify:** slow.\r\n"
ITEM_1 = "\n1.  **File System Errors:** Desktop writes fail.\n"


def test_repeated_deletion_is_no_change_not_failure(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text(REPORT, encoding="utf-8", newline="")

    first = fp.file_processor(
        {"file_path": str(f), "action": "replace", "find": ITEM_1, "replace": ""}
    )
    after = _read_exact(f)
    assert ToolResult.from_any(first).success, first
    assert "File System Errors" not in after

    again = fp.file_processor(
        {"file_path": str(f), "action": "replace", "find": ITEM_1, "replace": ""}
    )
    assert ToolResult.from_any(again).success, again
    assert again.startswith("No change needed") and "already gone" in again
    assert _read_exact(f) == after


def test_repeated_replacement_is_no_change_not_failure(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text("item 4. Instant Cancellation\n", encoding="utf-8")
    params = {"file_path": str(f), "action": "replace", "find": "4.", "replace": "3."}

    assert ToolResult.from_any(fp.file_processor(params)).success
    again = fp.file_processor(params)

    assert ToolResult.from_any(again).success, again
    assert again.startswith("No change needed")
    assert f.read_text(encoding="utf-8") == "item 3. Instant Cancellation\n"


def test_find_absent_and_replacement_absent_still_fails(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text("the connection handshake failed\n", encoding="utf-8")

    result = fp.file_processor(
        {
            "file_path": str(f),
            "action": "replace",
            "find": "API quota limits,",
            "replace": "rate limits,",
        }
    )

    # pytest's long tmp path pushes this status line past 200 chars; it must still fail.
    assert not ToolResult.from_any(result).success, result
    assert "not found in the file" in result
    assert f.read_text(encoding="utf-8") == "the connection handshake failed\n"


def test_already_applied_edit_does_not_block_the_rest_of_a_plan():
    new, applied, failures = apply_literal_edits(
        "alpha 3. beta", [{"find": "4.", "replace": "3."}, {"find": "beta", "replace": "gamma"}]
    )
    assert new == "alpha 3. gamma" and len(applied) == 1 and not failures
