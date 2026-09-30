"""ToolResult.from_any judges plain strings by their FIRST LINE only.

Regression: a successful file_controller read / file_processor summary whose
BODY mentioned "error" or "failed" was logged as tool.execution.failed, told
the model success=false, and fed self_diagnosis phantom file-operation failures.
"""

from core.tool_result import ToolResult


def test_content_with_failure_words_in_body_is_success():
    content = (
        "Act as an expert Systems Engineer.\n"
        "1. Frequent crashes: the watchdog failed.\n"
        "2. File operation error when appending.\n"
        "Nothing could not be found, timed out."
    )
    assert ToolResult.from_any(content).success


def test_leading_failure_lines_still_fail():
    for msg in (
        "File not found: notes.txt",
        "Could not read file: [Errno 13] Permission denied",
        "Unknown action: 'frobnicate'",
        "Error: failed",
        "File controller error (read): boom\nTraceback follows",
        "  Failed to open path: x\n",
    ):
        assert not ToolResult.from_any(msg).success, msg


def test_plain_success_strings():
    assert ToolResult.from_any("Folder created: Reports").success
    assert ToolResult.from_any("").success


# 2026-09-28: a failure that quotes a long find string or path ran past the
# 200-char status-line cap and was read as success.
LONG = "x" * 250


def test_long_status_line_that_opens_with_failure_still_fails():
    for msg in (
        f"Edit failed: '{LONG}' not found in the file. Nothing was written",
        f"Path not found: C:\\Users\\{LONG}\\notes.txt",
        f"Could not read file: C:\\{LONG}.txt",
        f"File controller error (read): {LONG}",
        f"Error: {LONG}",
        f"Unknown action: '{LONG}'",
    ):
        assert not ToolResult.from_any(msg).success, msg[:60]


def test_long_prose_mentioning_failure_stays_content():
    for msg in (
        f"The subprocess module provides input/output/error pipes and more {LONG}",
        f"Error handling in Python: use try/except blocks {LONG}",
        f"Summary: the watchdog failed twice and the upload timed out {LONG}",
        f"No change needed — that text is already gone from C:\\{LONG}.txt (already removed?).",
        f"Edited C:\\{LONG}.txt in place and verified on disk: 'not found' → 'found' (1x)",
    ):
        assert ToolResult.from_any(msg).success, msg[:60]
