"""Safe call failure breadcrumbs and native stdout isolation regressions."""

import json
import subprocess
import sys

from core.telegram_call_diagnostics import record
from core.telegram_call_protocol import CallWire


def test_diagnostics_never_store_exception_message(monkeypatch, tmp_path):
    monkeypatch.delenv("ERA_LOG_DIR", raising=False)
    monkeypatch.setattr("core.telegram_call_setup.private_directory", lambda: tmp_path)
    record("native_connect_failed", "a" * 32, ValueError("SECRET_SESSION_AND_AUDIO"))
    text = (tmp_path / "telegram-call-diagnostics.jsonl").read_text()
    assert "SECRET_SESSION_AND_AUDIO" not in text
    assert json.loads(text)["exception_type"] == "ValueError"


def test_diagnostics_reject_unbounded_or_invalid_metadata(monkeypatch, tmp_path):
    monkeypatch.delenv("ERA_LOG_DIR", raising=False)
    monkeypatch.setattr("core.telegram_call_setup.private_directory", lambda: tmp_path)
    record("unexpected secret message", "a" * 32)
    record("native_connected", "not a generation")
    assert not (tmp_path / "telegram-call-diagnostics.jsonl").exists()


def test_diagnostics_storage_failure_does_not_break_call(monkeypatch):
    monkeypatch.delenv("ERA_LOG_DIR", raising=False)

    def unavailable():
        raise RuntimeError("private location unavailable")

    monkeypatch.setattr("core.telegram_call_setup.private_directory", unavailable)
    record("native_connected", "a" * 32)


def test_native_stdout_cannot_corrupt_wire():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from core.telegram_call_helper import isolate_wire_output; "
            "from core.telegram_call_protocol import CallWire; "
            "wire=isolate_wire_output(); os.write(1,b'native log\\n'); "
            "wire.write(CallWire('ready','a'*32,42).encode()); wire.close()",
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert CallWire.decode(result.stdout).kind == "ready"
    assert b"native log" not in result.stdout
    assert b"native log" in result.stderr
