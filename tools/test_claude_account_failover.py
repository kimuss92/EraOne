"""Dual Claude account failover — the property is "Era never stops".

WHAT THIS PROTECTS
  One subscription hitting its usage limit used to stop every Claude path in
  Era except the community-management lane, which had its own failover inside
  era_dispatch.py. core/llm/claude_accounts.py lifts that logic to where every
  path reaches it, and these tests hold the two halves of it in place:

    IT SWITCHES   on failures the other account could survive — out of usage,
                  rate limited, signed out, credentials rejected.
    IT DOES NOT   on failures that would repeat identically. A refusal or a
                  malformed reply costs ~3s and ~$0.06 per attempt; retrying
                  one on a second account buys nothing and spends twice.

  The second half is the one worth testing hardest. "Retry everything
  everywhere" looks more robust and is strictly worse, and nothing about the
  code's shape prevents someone from widening the switch condition later.

NO REAL CLI RUNS
  A real `claude -p` costs ~$0.058 and ~3s. The runner is faked, and the
  payloads are the ones tools/test_claude_cli_adapter.py copied from real
  probes on 2026-07-30 — a fixture that did not match the wire format would
  test nothing.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from core.llm import claude_accounts, registry
from core.llm.adapters.claude_cli import ClaudeCliProvider
from core.llm.types import LlmError, LlmMessage, LlmRequest

# From a real probe, trimmed to the fields the adapter reads.
SUCCESS = {
    "is_error": False,
    "stop_reason": "end_turn",
    "total_cost_usd": 0.058,
    "usage": {"input_tokens": 2, "output_tokens": 9},
    "terminal_reason": "completed",
    "subtype": "success",
    "api_error_status": None,
    "result": "PROBE_OK",
    "type": "result",
}
RATE_LIMITED = {
    "is_error": True,
    "usage": {},
    "terminal_reason": "api_error",
    "subtype": "success",
    "api_error_status": 429,
    "result": "You have hit your usage limit. Try again later.",
    "type": "result",
}
SIGNED_OUT = {
    "is_error": True,
    "usage": {},
    "terminal_reason": "api_error",
    "subtype": "success",
    "api_error_status": 401,
    "result": "OAuth session expired. Run /login.",
    "type": "result",
}
REFUSAL = {
    "is_error": False,
    "usage": {},
    "stop_reason": "refusal",
    "terminal_reason": "completed",
    "subtype": "success",
    "api_error_status": None,
    "result": "I can't help with that.",
    "type": "result",
}


@pytest.fixture
def accounts(tmp_path):
    """Two real directories, so Account.exists is true for both."""
    primary = tmp_path / ".claude-primary"
    secondary = tmp_path / ".claude-secondary"
    for d in (primary, secondary):
        d.mkdir()
        (d / ".credentials.json").write_text("{}", encoding="utf-8")
    return primary, secondary


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, accounts):
    primary, secondary = accounts
    config = textwrap.dedent(f"""
        version: 1
        default: era.reason
        providers:
          claude_cli:
            config_dir: {primary.as_posix()}
            fallback_config_dirs:
              - {secondary.as_posix()}
            use_minimal_cwd: true
        aliases:
          era.reason:
            provider: claude_cli
            model: sonnet
            capabilities:
              json_schema: true
              cost_reporting: true
    """)
    path = tmp_path / "llm.yaml"
    path.write_text(config, encoding="utf-8")
    monkeypatch.setenv("ERA_LLM_CONFIG", str(path))
    monkeypatch.delenv(claude_accounts.ENV_ACCOUNTS, raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)
    # Cooldowns are module state: one test's failure must not steer the next.
    claude_accounts.reset()
    yield
    claude_accounts.reset()


def _key(path: str) -> str:
    """Compare paths, not spellings.

    The registry is written with forward slashes and Path renders backslashes,
    so the same directory arrives in two spellings on Windows. Matching on the
    raw string made every test fail on a difference that does not exist.
    """
    return os.path.normcase(os.path.normpath(str(path)))


class ScriptedRunner:
    """Answers differently per account, and records who was asked."""

    def __init__(self, by_dir: dict[str, dict]):
        self.by_dir = {_key(k): v for k, v in by_dir.items()}
        self.calls: list[dict] = []

    def __call__(self, cmd, prompt, env, cwd, timeout, rid):
        config_dir = env.get("CLAUDE_CONFIG_DIR", "")
        self.calls.append({"config_dir": config_dir, "cmd": cmd})
        payload = self.by_dir.get(_key(config_dir))
        if payload is None:
            raise AssertionError(f"unexpected account asked: {config_dir!r}")
        return 0, json.dumps(payload), ""

    @property
    def asked(self) -> list[str]:
        return [Path(c["config_dir"]).name for c in self.calls]


def ask(runner) -> str:
    provider = ClaudeCliProvider(runner=runner)
    return provider.generate(
        LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "think"),))
    ).text


# ── it switches when it should ───────────────────────────────────────────────


def test_primary_answers_and_the_second_account_is_never_touched(accounts):
    primary, secondary = accounts
    runner = ScriptedRunner({str(primary): SUCCESS, str(secondary): SUCCESS})
    assert ask(runner) == "PROBE_OK"
    assert runner.asked == [".claude-primary"], "the fallback was called for no reason"


def test_usage_limit_on_the_primary_falls_through_to_the_second(accounts):
    primary, secondary = accounts
    runner = ScriptedRunner({str(primary): RATE_LIMITED, str(secondary): SUCCESS})
    assert ask(runner) == "PROBE_OK", "the work did not continue on the second account"
    assert runner.asked == [".claude-primary", ".claude-secondary"]


def test_signed_out_primary_falls_through_to_the_second(accounts):
    primary, secondary = accounts
    runner = ScriptedRunner({str(primary): SIGNED_OUT, str(secondary): SUCCESS})
    assert ask(runner) == "PROBE_OK"
    assert runner.asked == [".claude-primary", ".claude-secondary"]


def test_both_accounts_exhausted_names_both_and_keeps_the_real_reason(accounts):
    primary, secondary = accounts
    runner = ScriptedRunner({str(primary): RATE_LIMITED, str(secondary): RATE_LIMITED})
    with pytest.raises(LlmError) as exc:
        ask(runner)
    assert exc.value.kind == "rate_limit", "the kind must survive, so callers can still branch"
    assert ".claude-primary" in exc.value.message
    assert ".claude-secondary" in exc.value.message
    assert "usage limit" in exc.value.message.lower(), "the CLI's own reason was replaced"
    assert runner.asked == [".claude-primary", ".claude-secondary"]


# ── it does NOT switch when it shouldn't ─────────────────────────────────────


def test_a_refusal_is_not_retried_on_the_second_account(accounts):
    """The expensive half of the contract.

    A refusal is a property of the request, not the account. Retrying it spends
    a second ~$0.06 call to be refused again, and would make every declined
    request cost double.
    """
    primary, secondary = accounts
    runner = ScriptedRunner({str(primary): REFUSAL, str(secondary): SUCCESS})
    with pytest.raises(LlmError) as exc:
        ask(runner)
    assert exc.value.kind == "refusal"
    assert runner.asked == [".claude-primary"], "a refusal must not burn the second account"


def test_a_malformed_reply_is_not_retried_on_the_second_account(accounts):
    primary, secondary = accounts

    class BadJson(ScriptedRunner):
        def __call__(self, cmd, prompt, env, cwd, timeout, rid):
            self.calls.append({"config_dir": env.get("CLAUDE_CONFIG_DIR", ""), "cmd": cmd})
            return 0, "not json at all", ""

    runner = BadJson({str(primary): SUCCESS, str(secondary): SUCCESS})
    with pytest.raises(LlmError):
        ask(runner)
    assert len(runner.calls) == 1


# ── cooldown ─────────────────────────────────────────────────────────────────


def test_a_failed_account_is_skipped_on_the_next_call(accounts):
    """Otherwise every later call pays the dead account's latency first."""
    primary, secondary = accounts
    runner = ScriptedRunner({str(primary): RATE_LIMITED, str(secondary): SUCCESS})
    ask(runner)
    assert runner.asked == [".claude-primary", ".claude-secondary"]

    second = ScriptedRunner({str(primary): RATE_LIMITED, str(secondary): SUCCESS})
    assert ask(second) == "PROBE_OK"
    assert second.asked == [".claude-secondary"], "the cooled-down account was asked again"


def test_a_successful_call_clears_that_account_cooldown(accounts):
    primary, _secondary = accounts
    account = claude_accounts.Account(str(primary))
    claude_accounts.note_failure(account, "rate_limit: usage limit")
    assert claude_accounts.cooling(account)
    claude_accounts.note_success(account)
    assert not claude_accounts.cooling(account)


def test_when_every_account_is_cooling_they_are_tried_anyway(accounts):
    """A stale timer must never be the reason Era cannot reach Claude at all."""
    primary, secondary = accounts
    for d in (primary, secondary):
        claude_accounts.note_failure(claude_accounts.Account(str(d)), "rate_limit: usage limit")

    pool = claude_accounts.usable(registry.resolve("era.reason").provider_config)
    assert [a.name for a in pool] == [".claude-primary", ".claude-secondary"]


# ── configuration ────────────────────────────────────────────────────────────


def test_order_is_primary_then_fallbacks(accounts):
    primary, secondary = accounts
    pool = claude_accounts.accounts(registry.resolve("era.reason").provider_config)
    assert [_key(a.config_dir) for a in pool] == [_key(primary), _key(secondary)]


def test_a_missing_account_directory_is_skipped_not_attempted(accounts, tmp_path, monkeypatch):
    primary, _secondary = accounts
    monkeypatch.setenv(
        claude_accounts.ENV_ACCOUNTS,
        f"{tmp_path / 'does-not-exist'};{primary}",
    )
    runner = ScriptedRunner({str(primary): SUCCESS})
    assert ask(runner) == "PROBE_OK"
    assert runner.asked == [".claude-primary"]


def test_duplicate_directories_are_collapsed(accounts):
    primary, _ = accounts
    pool = claude_accounts.accounts(
        {"config_dir": str(primary), "fallback_config_dirs": [str(primary)]}
    )
    assert len(pool) == 1, "the same account must not be tried twice"


def test_no_configured_accounts_behaves_exactly_as_before(accounts):
    """Back-compat: with no list, the single config_dir path is unchanged."""
    primary, _ = accounts
    assert claude_accounts.accounts({}) == []
    runner = ScriptedRunner({str(primary): SUCCESS})
    provider = ClaudeCliProvider(runner=runner)
    # provider_config still carries config_dir, so the env is still set.
    provider.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "hi"),)))
    assert _key(runner.calls[0]["config_dir"]) == _key(primary)


# ── the switch predicate, directly ───────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,message",
    [
        ("rate_limit", "HTTP 429"),
        ("auth", "HTTP 401"),
        ("transient", "Invalid API key"),
        ("transient", "You are not logged in"),
        ("transient", "credit balance is too low"),
        ("transient", "Claude is overloaded"),
    ],
)
def test_switch_on_account_level_failures(kind, message):
    assert claude_accounts.should_switch(kind, message)


@pytest.mark.parametrize(
    "kind,message",
    [
        ("refusal", "the model declined"),
        ("unsupported", "no vision on this path"),
        ("invalid_response", "expected JSON, got prose"),
        ("context_overflow", "the prompt is too long"),
        ("timeout", "the Claude CLI did not finish within 45s"),
    ],
)
def test_do_not_switch_on_request_level_failures(kind, message):
    assert not claude_accounts.should_switch(kind, message)
