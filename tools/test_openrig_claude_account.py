"""Account switches are live, unambiguous, subscription-only and secret-safe."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "openrig_claude_account", Path(__file__).with_name("openrig_claude_account.py")
)
helper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)
TOKEN = "sk-ant-oat01-" + "test-only-not-real-credential-" * 2


def test_connected_account_changes_without_cached_identity(monkeypatch):
    current = [helper.ConnectedAccount("account-a", TOKEN)]
    monkeypatch.setattr(helper, "discover_account", lambda: helper.select_account(current))
    assert helper.discover_account().identity == "account-a"
    current[:] = [helper.ConnectedAccount("account-b", TOKEN + "b")]
    assert helper.discover_account().identity == "account-b"


def test_simultaneous_distinct_accounts_are_not_guessed():
    with pytest.raises(helper.AccountUnavailable, match="More than one"):
        helper.select_account([
            helper.ConnectedAccount("a", TOKEN), helper.ConnectedAccount("b", TOKEN + "b")
        ])


def test_multiple_sessions_of_one_account_use_latest_token():
    result = helper.select_account([
        helper.ConnectedAccount("a", TOKEN), helper.ConnectedAccount("a", TOKEN + "new")
    ])
    assert result.token == TOKEN + "new"


@pytest.mark.parametrize("token", ["", None, "sk-ant-api03-" + "a" * 50, TOKEN + "\n"])
def test_api_keys_empty_values_and_header_injection_are_rejected(token):
    assert helper.account_from_environment({"CLAUDE_CODE_OAUTH_TOKEN": token}, 1) is None


def test_custom_gateway_credentials_are_not_reused():
    assert helper.account_from_environment({
        "CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "ANTHROPIC_BASE_URL": "https://example.com"
    }, 1) is None


def test_account_status_and_repr_never_contain_the_token():
    account = helper.ConnectedAccount("account-a", TOKEN, pid=123)
    assert TOKEN not in json.dumps(account.status()) + repr(account)
    assert account.status()["connected"] is True


def test_token_mode_refuses_interactive_terminal(monkeypatch, capsys):
    monkeypatch.setattr(helper.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(helper, "discover_account", lambda: pytest.fail("must refuse before discovery"))
    assert helper.main(["token"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "private helper pipe" in output.err


def test_process_inspection_error_does_not_echo_secret(monkeypatch, capsys):
    def fail():
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(helper, "discover_account", fail)
    assert helper.main(["status"]) == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_owner_and_binary_checks_exclude_other_processes(monkeypatch, tmp_path):
    def process(pid, username, exe, identity):
        return SimpleNamespace(
            pid=pid, info={"name": "claude.exe", "exe": exe, "create_time": pid},
            username=lambda: username,
            environ=lambda: {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "CLAUDE_CODE_ACCOUNT_UUID": identity},
        )
    monkeypatch.setattr(helper.psutil, "Process", lambda: SimpleNamespace(username=lambda: "owner"))
    binary = str(tmp_path / ".local/bin/claude.exe")
    rows = [process(1, "other", binary, "wrong"),
            process(2, "owner", str(tmp_path / "untrusted/claude.exe"), "wrong"),
            process(3, "owner", binary, "correct")]
    monkeypatch.setattr(helper.psutil, "process_iter", lambda _: iter(rows))
    assert helper.discover_account(tmp_path).identity == "correct"


def test_no_connected_account_does_not_fall_back_to_paid_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    with pytest.raises(helper.AccountUnavailable, match="sign in"):
        helper.select_account([])
