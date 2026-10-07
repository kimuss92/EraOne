"""Tests for core/llm/adapters/claude_cli.py (phase 4).

No real `claude -p` runs: a real invocation costs ~$0.058 and ~3s, so the CLI
is faked and only the JSON contract is exercised. The payloads below are copied
from actual probe output on 2026-07-30, not invented — a fixture that does not
match the real wire format would test nothing.

The properties that matter:

  * TOOLS STAY OFF. --allowedTools "" is the reason Claude cannot touch the
    machine on this path. If it ever stops being passed, tool execution moves
    inside Claude Code and outside Era's confirmation gates.
  * THE SCRATCH CWD IS USED. Measured: running from Era's repo costs ~11K extra
    tokens and 2-5s per call because Claude Code loads this project's CLAUDE.md
    and skills.
  * FAILURE IS NOT MISTAKEN FOR SUCCESS. The CLI reported subtype "success" on
    a run whose terminal_reason was "api_error" and whose result text was the
    404 message. Trusting subtype alone would hand that error string back as
    Claude's answer.
  * TELEMETRY IS READ. total_cost_usd / ttft_ms / the token breakdown are real
    numbers Era has never had for Gemini.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from core.llm import registry
from core.llm.adapters.claude_cli import ClaudeCliProvider, classify_cli_result
from core.llm.types import LlmError, LlmMessage, LlmRequest, PromptBlock

REGISTRY = """
version: 1
default: era.reason
providers:
  claude_cli:
    config_dir: C:\\Users\\nesri\\.claude-freelance
    use_minimal_cwd: true
  gemini: {}
aliases:
  era.reason:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
      long_context: true
      cost_reporting: true
    measured:
      call_overhead_usd: 0.058
      total_ms: 2756
  era.inrepo:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
  era.cheap:
    provider: claude_cli
    model: haiku
    available: false
    unavailable_reason: the proxy it points at is unreachable
    capabilities:
      json_schema: true
  era.fast:
    provider: gemini
    model: gemini-2.5-flash-lite
    capabilities:
      json_schema: true
"""

# Copied from a real probe (2026-07-30), trimmed to the fields the adapter reads.
SUCCESS = {
    "is_error": False,
    "duration_api_ms": 2285,
    "num_turns": 1,
    "stop_reason": "end_turn",
    "session_id": "757de527-7f25-4bdc-8a6d-1f3f64b4af91",
    "total_cost_usd": 0.0580775,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 8578,
        "cache_read_input_tokens": 21335,
        "output_tokens": 9,
    },
    "modelUsage": {"claude-sonnet-5": {"costUSD": 0.058}},
    "permission_denials": [],
    "terminal_reason": "completed",
    "subtype": "success",
    "api_error_status": None,
    "result": "PROBE_OK",
    "ttft_ms": 2690,
    "type": "result",
    "duration_ms": 2756,
}

# Also from a real probe: --model haiku resolving to an unreachable model. Note
# subtype is "success" while the run plainly failed.
API_ERROR_404 = {
    "is_error": True,
    "total_cost_usd": 0,
    "usage": {},
    "modelUsage": {},
    "terminal_reason": "api_error",
    "subtype": "success",
    "api_error_status": 404,
    "result": "There's an issue with the selected model (moonshotai/kimi-k2.6).",
    "type": "result",
}


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, tmp_path):
    p = tmp_path / "llm.yaml"
    p.write_text(textwrap.dedent(REGISTRY), encoding="utf-8")
    monkeypatch.setenv("ERA_LLM_CONFIG", str(p))
    monkeypatch.setattr(registry, "_CACHE", None)


class FakeRunner:
    """Stands in for the subprocess, recording exactly how it was invoked."""

    def __init__(self, payload=None, *, stdout=None, code=0, stderr="", raises=None):
        self.payload = payload
        self.stdout = stdout
        self.code = code
        self.stderr = stderr
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, cmd, prompt, env, cwd, timeout, rid):
        self.calls.append(
            {
                "cmd": cmd,
                "prompt": prompt,
                "env": env,
                "cwd": cwd,
                "timeout": timeout,
                "rid": rid,
            }
        )
        if self.raises is not None:
            raise self.raises
        out = self.stdout if self.stdout is not None else json.dumps(self.payload or SUCCESS)
        return self.code, out, self.stderr


def provider(payload=None, **kw):
    runner = FakeRunner(payload, **kw)
    return ClaudeCliProvider(runner=runner), runner


# ---------------------------------------------------------------------------
# The safety property: tools are off
# ---------------------------------------------------------------------------


def test_allowed_tools_is_passed_empty():
    """The single most important assertion in this file. Omitting the flag lets
    Claude Code use its default tool set — file writes, bash, the lot — from
    inside a subprocess Era's confirmation gates never see."""
    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "think"),)))
    cmd = runner.calls[0]["cmd"]
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == ""


def test_tool_requests_are_refused_with_the_reason():
    p, _ = provider()
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.reason", tools=({"name": "file_controller"},)))
    assert exc.value.kind == "unsupported"
    assert "gated layer" in exc.value.message


def test_print_mode_and_json_output_are_requested():
    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    cmd = runner.calls[0]["cmd"]
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_model_comes_from_the_registry():
    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    cmd = runner.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


# ---------------------------------------------------------------------------
# The measured optimisations
# ---------------------------------------------------------------------------


def test_runs_in_a_scratch_cwd_not_the_repo():
    """Measured: Era's repo adds ~11K tokens and 2-5s per call, because Claude
    Code loads this project's CLAUDE.md and skills."""
    import os

    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    cwd = runner.calls[0]["cwd"]
    assert cwd != os.getcwd()
    assert "era_llm_" in cwd


def test_scratch_cwd_is_cleaned_up():
    import os

    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert not os.path.exists(runner.calls[0]["cwd"])


def test_scratch_cwd_is_cleaned_up_even_on_failure():
    import os

    p, runner = provider(API_ERROR_404)
    with pytest.raises(LlmError):
        p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert not os.path.exists(runner.calls[0]["cwd"])


def test_config_dir_selects_the_subscription_account():
    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert runner.calls[0]["env"]["CLAUDE_CONFIG_DIR"].endswith(".claude-freelance")


def test_the_base_provider_strips_the_gateway_env(monkeypatch):
    """Regression (2026-10-06): a user environment that points Claude Code at
    OmniRoute (ANTHROPIC_MODEL=era-gateway + ANTHROPIC_BASE_URL) made the
    subscription `claude -p` fail with a model/provider error. The strip used to
    live ONLY on the voice subclass, so the autonomous-dev path -- which uses this
    BASE provider -- inherited the gateway and broke. Every reasoning path must
    strip it now."""
    from core.llm.adapters.claude_cli import GATEWAY_ENV_VARS

    for name in GATEWAY_ENV_VARS:
        monkeypatch.setenv(name, "era-gateway")
    monkeypatch.delenv("ERA_CLAUDE_KEEP_API_ENV", raising=False)
    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    env = runner.calls[0]["env"]
    assert not [n for n in GATEWAY_ENV_VARS if n in env]


def test_the_escape_hatch_keeps_the_gateway_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "era-gateway")
    monkeypatch.setenv("ERA_CLAUDE_KEEP_API_ENV", "1")
    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert runner.calls[0]["env"].get("ANTHROPIC_MODEL") == "era-gateway"


def test_prompt_goes_via_stdin_not_argv():
    """A positional prompt is rejected by the CLI outright."""
    p, runner = provider()
    p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "SECRET_MARKER"),)))
    call = runner.calls[0]
    assert call["prompt"] == "SECRET_MARKER"
    assert not any("SECRET_MARKER" in part for part in call["cmd"])


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def test_system_blocks_are_prepended_in_order():
    """`claude -p` has no separate system channel, so system text leads."""
    p, runner = provider()
    p.generate(
        LlmRequest(
            alias="era.reason",
            system=(PromptBlock("FROZEN", cache_hint=True), PromptBlock("VOLATILE")),
            messages=(LlmMessage.text("user", "the question"),),
        )
    )
    assert runner.calls[0]["prompt"] == "FROZEN\nVOLATILE\n\nthe question"


def test_multi_turn_is_role_labelled():
    p, runner = provider()
    p.generate(
        LlmRequest(
            alias="era.reason",
            messages=(
                LlmMessage.text("user", "first"),
                LlmMessage.text("assistant", "reply"),
            ),
        )
    )
    assert runner.calls[0]["prompt"] == "user: first\n\nassistant: reply"


def test_json_schema_is_appended_as_an_instruction():
    p, runner = provider(dict(SUCCESS, result='{"ok": true}'))
    p.generate(
        LlmRequest(
            alias="era.reason",
            messages=(LlmMessage.text("user", "classify"),),
            json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        )
    )
    prompt = runner.calls[0]["prompt"]
    assert "single JSON object" in prompt
    assert '"properties"' in prompt


def test_an_empty_request_is_refused_before_spending():
    p, runner = provider()
    with pytest.raises(LlmError, match="nothing to ask"):
        p.generate(LlmRequest(alias="era.reason"))
    assert runner.calls == [], "must not invoke the CLI with an empty prompt"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_real_cost_and_tokens_are_read():
    p, _ = provider()
    resp = p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert resp.usage.est_cost_usd == pytest.approx(0.0580775)
    assert resp.usage.reported_cost is True, "the CLI reports a real cost, not a derived one"
    assert resp.usage.input_tokens == 2
    assert resp.usage.output_tokens == 9
    assert resp.usage.cache_read_tokens == 21335
    assert resp.usage.cache_write_tokens == 8578


def test_first_token_latency_is_read():
    p, _ = provider()
    resp = p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert resp.usage.first_token_ms == 2690
    assert resp.usage.latency_ms == 2756


def test_missing_telemetry_costs_a_metric_not_the_answer():
    p, _ = provider({"result": "still an answer", "stop_reason": "end_turn"})
    resp = p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert resp.text == "still an answer"
    assert resp.usage.est_cost_usd == 0.0
    assert resp.usage.latency_ms >= 0


def test_resolved_model_is_reported_over_the_requested_alias():
    """`--model sonnet` is resolved by the CLI itself, and the resolution can
    surprise — on this machine `--model haiku` resolves to a third-party model.
    Logs must say which model actually answered."""
    p, _ = provider()
    resp = p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert resp.model_id == "claude-sonnet-5"


def test_model_falls_back_to_the_registry_when_unreported():
    p, _ = provider({"result": "x", "stop_reason": "end_turn"})
    resp = p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert resp.model_id == "sonnet"


# ---------------------------------------------------------------------------
# Failure classification — the subtype trap
# ---------------------------------------------------------------------------


def test_api_error_is_not_mistaken_for_success():
    """The real 404 payload carries subtype "success". Trusting that would hand
    the error text back as Claude's answer."""
    assert API_ERROR_404["subtype"] == "success"  # the trap, made explicit
    p, _ = provider(API_ERROR_404)
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert exc.value.kind == "unsupported"
    assert "404" in exc.value.message


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth"),
        (403, "auth"),
        (404, "unsupported"),
        (429, "rate_limit"),
        (500, "transient"),
        (529, "transient"),
        (413, "context_overflow"),
    ],
)
def test_http_status_classification(status, kind):
    result = classify_cli_result({"api_error_status": status, "result": "x"})
    assert result is not None and result[0] == kind


def test_successful_payload_classifies_as_no_failure():
    assert classify_cli_result(SUCCESS) is None


def test_refusal_is_surfaced_as_refusal_not_a_transport_error():
    result = classify_cli_result({"stop_reason": "refusal", "result": "I can't help with that"})
    assert result is not None and result[0] == "refusal"


def test_refusal_is_not_retryable():
    p, _ = provider({"stop_reason": "refusal", "result": "declined"})
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert exc.value.retryable is False, "retrying a policy decline burns time for a second no"


def test_truncated_reply_is_invalid_response():
    result = classify_cli_result({"stop_reason": "max_tokens", "result": "half an ans"})
    assert result is not None and result[0] == "invalid_response"


def test_is_error_flag_alone_is_enough():
    result = classify_cli_result({"is_error": True, "result": "something broke"})
    assert result is not None and result[0] == "transient"


def test_empty_output_is_an_error_not_an_empty_answer():
    p, _ = provider(stdout="", code=1, stderr="not logged in")
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert "no output" in exc.value.message
    assert "not logged in" in exc.value.message


def test_non_json_output_is_reported_with_its_text():
    """Usually means the CLI refused to start and printed a human message."""
    p, _ = provider(stdout="Error: unknown flag --allowedTools")
    with pytest.raises(LlmError, match="non-JSON"):
        p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))


def test_json_that_is_not_an_object_is_invalid():
    p, _ = provider(stdout="[1, 2, 3]")
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert exc.value.kind == "invalid_response"


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------


def test_json_fences_are_stripped():
    p, _ = provider(dict(SUCCESS, result='```json\n{"a": 1}\n```'))
    resp = p.generate(
        LlmRequest(
            alias="era.reason",
            messages=(LlmMessage.text("user", "q"),),
            json_schema={"type": "object"},
        )
    )
    assert resp.text == '{"a": 1}'


def test_unparseable_json_is_retryable():
    p, _ = provider(dict(SUCCESS, result="I think the answer is yes"))
    with pytest.raises(LlmError) as exc:
        p.generate(
            LlmRequest(
                alias="era.reason",
                messages=(LlmMessage.text("user", "q"),),
                json_schema={"type": "object"},
            )
        )
    assert exc.value.kind == "invalid_response"
    assert exc.value.retryable is True


def test_prose_mode_does_not_validate_json():
    p, _ = provider(dict(SUCCESS, result="a thoughtful paragraph"))
    resp = p.generate(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    assert resp.text == "a thoughtful paragraph"


# ---------------------------------------------------------------------------
# Alias guarding
# ---------------------------------------------------------------------------


def test_unavailable_alias_explains_itself_before_spending():
    p, runner = provider()
    with pytest.raises(LlmError, match="proxy it points at is unreachable"):
        p.generate(LlmRequest(alias="era.cheap", messages=(LlmMessage.text("user", "q"),)))
    assert runner.calls == [], "must not invoke the CLI for an unavailable alias"


def test_a_gemini_alias_is_refused():
    p, _ = provider()
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.fast", messages=(LlmMessage.text("user", "q"),)))
    assert exc.value.kind == "unsupported"
    assert "gemini" in exc.value.message


def test_unknown_alias_is_unsupported():
    p, _ = provider()
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.nope"))
    assert exc.value.kind == "unsupported"


def test_capabilities_refuses_another_providers_alias():
    p, _ = provider()
    with pytest.raises(LlmError, match="not 'claude_cli'"):
        p.capabilities("era.fast")


# ---------------------------------------------------------------------------
# Stream, cancel, boundaries
# ---------------------------------------------------------------------------


def test_stream_emits_text_usage_turn_complete():
    p, _ = provider()
    events = list(
        p.stream(LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),)))
    )
    assert [e.kind for e in events] == ["text_delta", "usage", "turn_complete"]
    assert events[0].text == "PROBE_OK"


def test_cancel_is_safe_for_unknown_ids():
    p, _ = provider()
    assert p.cancel("never-started") is None


def test_timeout_is_forwarded_to_the_runner():
    p, runner = provider()
    p.generate(
        LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "q"),), timeout_s=12.5)
    )
    assert runner.calls[0]["timeout"] == 12.5


def test_adapter_does_not_import_actions():
    import sys

    before = set(sys.modules)
    import core.llm.adapters.claude_cli  # noqa: F401

    pulled = {m for m in set(sys.modules) - before if m.startswith(("actions", "app", "main"))}
    assert not pulled, f"adapter pulled in {pulled}"


def test_provider_name_matches_the_registry():
    assert ClaudeCliProvider.name == "claude_cli"
