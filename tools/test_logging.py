from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from core.logger import (
    _get_log_dir,
    _is_sensitive,
    _sanitize_field,
    _truncate,
    executor_logger,
    log_event,
    main_logger,
    memory_logger,
    safe_log_path,
    setup_logger,
)

_failures = 0


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status:4s}] {label}")
    if not condition:
        global _failures
        _failures += 1


# ── Sanitization Tests ──


def test_sensitive_keys_masked():
    d = {"password": "mysecret123", "token": "abc", "api_key": "xyz"}
    result = _sanitize_field("params", d)
    check("password masked", result["password"] == "***")
    check("token masked", result["token"] == "***")
    check("api_key masked", result["api_key"] == "***")


def test_normal_keys_pass_through():
    d = {"receiver": "John", "app_name": "Chrome"}
    result = _sanitize_field("params", d)
    check("receiver preserved", result["receiver"] == "John")
    check("app_name preserved", result["app_name"] == "Chrome")


def test_long_values_truncated():
    long_str = "A" * 500
    result = _sanitize_field("params", {"note": long_str})
    check("long value truncated", len(result["note"]) < 200)
    check("ends with ellipsis", result["note"].endswith("..."))


def test_message_text_gets_length():
    d = {"message_text": "Hello world"}
    result = _sanitize_field("params", d)
    check("message_text preserved", result["message_text"] == "Hello world")
    check("message_text_length added", result.get("message_text_length") == 11)


def test_long_message_text_truncated():
    long_msg = "Hello " * 100
    result = _sanitize_field("params", {"message_text": long_msg})
    check("long message_text truncated", len(result["message_text"]) <= 120)
    check("message_length tracks original", result["message_text_length"] == len(long_msg))


def test_attachments_summarized():
    result = _sanitize_field("params", {"attachments": ["file1.pdf", "file2.jpg"]})
    check("attachments summarized", result["attachments"] == "<2 attachment(s)>")


def test_empty_attachments():
    result = _sanitize_field("params", {"attachments": []})
    check("empty attachments", result["attachments"] == "<0 attachment(s)>")


def test_original_dict_not_mutated():
    original = {"password": "secret", "name": "John"}
    copy_before = dict(original)
    _sanitize_field("params", original)
    check("original dict unchanged", original == copy_before)


def test_sanitize_list():
    items = [
        {"password": "p1", "name": "a"},
        {"token": "t1", "name": "b"},
    ]
    result = _sanitize_field("items", items)
    check("list item password masked", result[0]["password"] == "***")
    check("list item token masked", result[1]["token"] == "***")
    check("list item name preserved", result[0]["name"] == "a")


def test_sensitive_key_check():
    check("password is sensitive", _is_sensitive("password"))
    check("PASSWORD is sensitive", _is_sensitive("PASSWORD"))
    check("api_key is sensitive", _is_sensitive("api_key"))
    check("auth_token is sensitive", _is_sensitive("auth_token"))
    check("name is not sensitive", not _is_sensitive("name"))
    check("receiver is not sensitive", not _is_sensitive("receiver"))


def test_truncate_short():
    check("short string unchanged", _truncate("hello") == "hello")


def test_truncate_long():
    long_str = "X" * 500
    result = _truncate(long_str)
    check("long truncated", len(result) < 200)
    check("long ends with ...", result.endswith("..."))


def test_truncate_exact():
    exactly = "X" * 120
    check("exact max_len preserved", _truncate(exactly, 120) == exactly)


# ── Logger Initialization Tests ──


def test_logger_propagate_false():
    lg = setup_logger("test_prop")
    check("propagate is False", not lg.propagate)


def test_logger_does_not_pollute_root():
    import logging

    root_handlers_before = len(logging.root.handlers)
    setup_logger("test_no_pollute")
    root_handlers_after = len(logging.root.handlers)
    check("root handlers unchanged", root_handlers_before == root_handlers_after)


# ── ERA_LOG_DIR Tests ──


def test_log_dir_env_var():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERA_LOG_DIR"] = tmp
        try:
            log_dir = _get_log_dir()
            check("log_dir uses ERA_LOG_DIR", log_dir.resolve() == Path(tmp).resolve())
            check("log_dir exists", log_dir.is_dir())
        finally:
            del os.environ["ERA_LOG_DIR"]


# ── Log File Written Tests ──


def _close_handlers(logger):
    for h in logger.handlers[:]:
        h.close()
        logger.removeHandler(h)


def test_log_file_written():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERA_LOG_DIR"] = tmp
        try:
            lg = setup_logger("test_write")
            log_event(lg, "test.event", "INFO", tool="test_tool")
            _close_handlers(lg)
            log_path = Path(tmp) / "era.log"
            check("log file exists", log_path.is_file())
            content = log_path.read_text(encoding="utf-8").strip()
            check("log file not empty", len(content) > 0)
            line = json.loads(content)
            check("line has ts", "ts" in line)
            check("line has level", line["level"] == "INFO")
            check("line has event", line["event"] == "test.event")
            check("line has tool", line.get("tool") == "test_tool")
        finally:
            del os.environ["ERA_LOG_DIR"]


def test_every_line_valid_json():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERA_LOG_DIR"] = tmp
        try:
            lg = setup_logger("test_json_valid")
            for i in range(5):
                log_event(lg, f"event.{i}", "INFO", index=i)
            _close_handlers(lg)
            log_path = Path(tmp) / "era.log"
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            all_valid = True
            for line in lines:
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    all_valid = False
                    break
            check("every line is valid JSON", all_valid)
            check("expected line count", len(lines) == 5)
        finally:
            del os.environ["ERA_LOG_DIR"]


# ── Rotation Tests ──


def test_rotation():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERA_LOG_DIR"] = tmp
        try:
            lg = setup_logger("test_rotate")
            log_path = Path(tmp) / "era.log"
            max_bytes = 1024
            from logging.handlers import RotatingFileHandler

            for handler in lg.handlers:
                if isinstance(handler, RotatingFileHandler):
                    handler.maxBytes = max_bytes
                    handler.backupCount = 5
            big_str = "X" * 500
            for _ in range(50):
                log_event(lg, "rotate.event", "INFO", data=big_str)
            _close_handlers(lg)
            backups = sorted(Path(tmp).glob("era.log*"))
            check("active log exists", log_path.is_file())
            check("backup count <= 6", len(backups) <= 6)
        finally:
            del os.environ["ERA_LOG_DIR"]


# ── send_message params do not store full text ──


def test_send_message_truncation():
    long_msg = "Hello, this is a very long message. " * 50
    d = {"receiver": "John", "message_text": long_msg, "platform": "WhatsApp"}
    result = _sanitize_field("params", d)
    check("receiver preserved", result["receiver"] == "John")
    check("message_text truncated", len(result["message_text"]) <= 120)
    check("message_text_length tracks original", "message_text_length" in result)
    check("platform preserved", result["platform"] == "WhatsApp")


# ── Logger does not configure root ──


def test_logger_no_root_handlers():
    import logging

    root_before = len(logging.root.handlers)
    setup_logger("test_no_root")
    root_after = len(logging.root.handlers)
    check("root handlers not polluted", root_before == root_after)


# ── Module-level loggers exist ──


def test_module_loggers_exist():
    check("main_logger exists", main_logger is not None)
    check("executor_logger exists", executor_logger is not None)
    check("memory_logger exists", memory_logger is not None)


# ── safe_log_path Tests ──


def test_safe_log_path_relative():
    import tempfile

    base = Path.cwd()
    p = base / "memory" / "long_term.json"
    result = safe_log_path(p)
    check("path under cwd is relative", result == "memory/long_term.json")


def test_safe_log_path_uses_forward_slash():
    import tempfile

    base = Path.cwd()
    p = base / "memory" / "backups" / "test.123.jsonbak"
    result = safe_log_path(p)
    check("uses forward slash", "\\" not in result and result == "memory/backups/test.123.jsonbak")


def test_safe_log_path_outside_repo():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "somefile.log"
        result = safe_log_path(p)
        check("outside repo shows tagged filename", result == "<external>/somefile.log")


def test_safe_log_path_rejects_user_home():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        deep = Path(tmp) / "deep" / "nested" / "file.txt"
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.write_text("")
        result = safe_log_path(deep)
        check("nested outside leaks no directory", result == "<external>/file.txt")
        check("nested outside leaks no home path", tmp not in result)


def test_safe_log_path_external_is_distinguishable():
    """A temp-dir file sharing a REAL memory filename must not look like the real one."""
    import tempfile

    repo_root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "long_term.json"
        real = repo_root / "memory" / "long_term.json"
        check("test fixture is tagged external", safe_log_path(fake) == "<external>/long_term.json")
        check(
            "real memory file keeps its directory", safe_log_path(real) == "memory/long_term.json"
        )
        check("the two are distinguishable", safe_log_path(fake) != safe_log_path(real))


# ── Main ──


def main():
    print("=" * 60)
    print("  Logging System Tests")
    print("=" * 60)
    print()

    tests = [
        ("Sensitive keys masked", test_sensitive_keys_masked),
        ("Normal keys pass through", test_normal_keys_pass_through),
        ("Long values truncated", test_long_values_truncated),
        ("Message text gets length", test_message_text_gets_length),
        ("Long message text truncated", test_long_message_text_truncated),
        ("Attachments summarized", test_attachments_summarized),
        ("Empty attachments", test_empty_attachments),
        ("Original dict not mutated", test_original_dict_not_mutated),
        ("Sanitize list of dicts", test_sanitize_list),
        ("Sensitive key detection", test_sensitive_key_check),
        ("Truncate short string", test_truncate_short),
        ("Truncate long string", test_truncate_long),
        ("Truncate exact length", test_truncate_exact),
        ("Logger propagate=False", test_logger_propagate_false),
        ("Logger does not pollute root", test_logger_does_not_pollute_root),
        ("ERA_LOG_DIR env var", test_log_dir_env_var),
        ("Log file is written", test_log_file_written),
        ("Every line is valid JSON", test_every_line_valid_json),
        ("Log rotation", test_rotation),
        ("send_message params truncated", test_send_message_truncation),
        ("Logger no root handlers", test_logger_no_root_handlers),
        ("Module loggers exist", test_module_loggers_exist),
        ("safe_log_path relative", test_safe_log_path_relative),
        ("safe_log_path forward slash", test_safe_log_path_uses_forward_slash),
        ("safe_log_path outside repo", test_safe_log_path_outside_repo),
        ("safe_log_path rejects user home", test_safe_log_path_rejects_user_home),
        (
            "safe_log_path external is distinguishable",
            test_safe_log_path_external_is_distinguishable,
        ),
    ]

    total = len(tests)
    failed_cases = 0
    for name, fn in tests:
        print(f"[{name}]")
        global _failures
        before = _failures
        try:
            fn()
        except Exception as e:
            print(f"  [FAIL] Exception: {e}")
            _failures += 1
        if _failures > before:
            failed_cases += 1
        print()

    passed = total - failed_cases
    print("=" * 60)
    print(f"Logging tests: {passed} passed, {failed_cases} failed out of {total}")
    print("=" * 60)
    return 1 if failed_cases > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
