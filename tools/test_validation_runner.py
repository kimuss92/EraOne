"""Validation must execute importable checks and preserve every failed gate."""

import subprocess
from types import SimpleNamespace

from tools import run_all_checks as runner


def test_registered_suite_commands_import_from_repo_root():
    for name, command in runner.TEST_SUITES:
        assert command[:2] == [runner.PYTHON, "-m"], name
        assert command[2] == "pytest" or command[2].startswith("tools.test_"), name


def test_passing_doctor_does_not_hide_a_failed_suite(monkeypatch, capsys):
    failed_command = runner.TEST_SUITES[0][1]

    def run(command, **kwargs):
        return SimpleNamespace(returncode=1 if command == failed_command else 0, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner.main() == 1
    output = capsys.readouterr().out
    assert "GLOBAL_READY:       NO" in output
    assert "FAILED: Registry validator" in output
    assert "ALL_CHECKS_OK" not in output


def test_all_successful_gates_report_ready(monkeypatch, capsys):
    monkeypatch.setattr(
        runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="")
    )
    assert runner.main() == 0
    output = capsys.readouterr().out
    assert "GLOBAL_READY:       YES" in output and "ALL_CHECKS_OK" in output


def test_parallel_suites_keep_failures_and_write_timing_profile(monkeypatch, capsys, tmp_path):
    import json

    suites = runner.TEST_SUITES[:2]
    monkeypatch.setattr(runner, "TEST_SUITES", suites)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=1 if command == suites[1][1] else 0, stderr=""
        ),
    )
    profile = tmp_path / "timings.json"
    assert runner.main(jobs=2, profile=profile) == 1
    data = json.loads(profile.read_text())
    assert {row["suite"] for row in data} == {
        "Compile check",
        "Doctor",
        *(name for name, _ in suites),
    }
    assert any(not row["passed"] and row["suite"] == suites[1][0] for row in data)
    assert "GLOBAL_READY:       NO" in capsys.readouterr().out


def test_hung_suite_is_reported_as_failed(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 900)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    assert not runner._suite([runner.PYTHON, "-m", "pytest", "tools/test_example.py"])


def test_custom_execution_checks_return_failure_when_assertions_fail():
    # Force incorrect conversion results while keeping the result type valid.
    # The script must report its failed checks through the process exit code.
    code = """
import runpy
from core.tool_result import ToolResult
ToolResult.from_any = staticmethod(lambda value: ToolResult.ok('forced result'))
runpy.run_module('tools.test_execution_layer', run_name='__main__')
"""
    result = subprocess.run(
        [runner.PYTHON, "-c", code],
        cwd=runner.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert "FAIL:" in result.stdout
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
