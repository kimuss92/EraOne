"""OmniRoute era-gateway lane (core/llm/adapters/omniroute.py) — no real network.

Pins the four things that matter: the key never leaves for a non-loopback host,
the request is tool-free text to the `era-gateway` combo, which combo member
answered is recorded, and ClaudeCliProvider falls back to the subscription CLI
whenever the gateway fails.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from core.llm import registry
from core.llm.adapters import omniroute
from core.llm.adapters.claude_cli import ClaudeCliProvider
from core.llm.types import LlmError, LlmMessage, LlmRequest, PromptBlock

REGISTRY = """
version: 1
default: era.reason
providers:
  claude_cli:
    use_minimal_cwd: true
aliases:
  era.reason:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
routing:
  omniroute:
    enabled: true
    combo: era-gateway
    timeout_s: 30
"""

CLI_OK = {
    "is_error": False,
    "subtype": "success",
    "result": "FROM_CLI",
    "usage": {},
    "modelUsage": {"claude-sonnet-5": {}},
    "type": "result",
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    p = tmp_path / "llm.yaml"
    p.write_text(textwrap.dedent(REGISTRY), encoding="utf-8")
    monkeypatch.setenv("ERA_LLM_CONFIG", str(p))
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "omni-test-key")
    monkeypatch.setattr(omniroute, "_windows_user_env", lambda name: "")


class FakeHttp:
    def __init__(self, status=200, body=None, headers=None, raises=None):
        self.status, self.raises = status, raises
        self.body = body if body is not None else {"content": [{"type": "text", "text": "pong"}]}
        self.headers = headers or {
            "x-omniroute-model": "claude-sonnet-5",
            "x-omniroute-provider": "cc",
        }
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        if self.raises:
            raise self.raises
        return self.status, self.headers, self.body


def req(text="ping", **kw):
    return LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", text),), **kw)


# ── the key never leaves for a non-loopback host ──────────────────────────────
def test_a_non_loopback_process_env_is_ignored_key_included(monkeypatch):
    # The Claude desktop app sets this in its process env (2026-10-09 leak).
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-anthropic-key")
    users = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:20128", "ANTHROPIC_API_KEY": "omni-key"}
    monkeypatch.setattr(omniroute, "_windows_user_env", lambda name: users.get(name, ""))
    assert omniroute.endpoint() == ("http://127.0.0.1:20128", "omni-key")


def test_no_loopback_gateway_anywhere_means_no_endpoint(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(
        omniroute, "_windows_user_env", lambda n: "https://evil.example" if "URL" in n else "k"
    )
    assert omniroute.endpoint() == ("", "")
    http = FakeHttp()
    with pytest.raises(LlmError):
        omniroute.generate(req(), http=http)
    assert http.calls == []


# ── the request ───────────────────────────────────────────────────────────────
def test_the_request_is_tool_free_text_to_the_combo():
    http = FakeHttp()
    out = omniroute.generate(
        LlmRequest(
            alias="era.reason",
            system=(PromptBlock(text="be brief"),),
            messages=(LlmMessage.text("user", "ping"),),
        ),
        http=http,
    )
    sent = http.calls[0]
    assert sent["url"] == "http://127.0.0.1:20128/v1/messages"
    assert sent["headers"]["x-api-key"] == "omni-test-key"
    assert sent["body"]["model"] == "era-gateway"
    assert sent["body"]["system"] == "be brief"
    assert sent["body"]["messages"] == [{"role": "user", "content": "ping"}]
    assert "tools" not in sent["body"]
    assert out.text == "pong" and out.provider == "omniroute"


def test_the_answering_combo_member_is_recorded():
    out = omniroute.generate(req(), http=FakeHttp())
    assert out.model_id == "claude-sonnet-5"
    assert out.raw["member_provider"] == "cc" and out.raw["combo"] == "era-gateway"


def test_json_requests_are_validated_and_unfenced():
    http = FakeHttp(body={"content": [{"type": "text", "text": '```json\n{"a": 1}\n```'}]})
    out = omniroute.generate(req(json_schema={"type": "object"}), http=http)
    assert json.loads(out.text) == {"a": 1}
    bad = FakeHttp(body={"content": [{"type": "text", "text": "not json"}]})
    with pytest.raises(LlmError) as exc:
        omniroute.generate(req(json_schema={"type": "object"}), http=bad)
    assert exc.value.kind == "invalid_response"


@pytest.mark.parametrize(
    "http, kind",
    [
        (FakeHttp(status=401, body={"error": {"message": "invalid x-api-key"}}), "auth"),
        (FakeHttp(status=429, body={}), "rate_limit"),
        (FakeHttp(status=502, body=None), "transient"),
        (FakeHttp(raises=ConnectionRefusedError()), "transient"),
        (FakeHttp(raises=TimeoutError()), "timeout"),
    ],
)
def test_failures_are_typed(http, kind):
    with pytest.raises(LlmError) as exc:
        omniroute.generate(req(), http=http)
    assert exc.value.kind == kind


def test_never_enabled_under_pytest():
    assert omniroute.enabled() is False


# ── ClaudeCliProvider: gateway first, subscription CLI as the fallback ────────
class Runner:
    def __init__(self):
        self.calls = 0

    def __call__(self, cmd, prompt, env, cwd, timeout, rid):
        self.calls += 1
        return 0, json.dumps(CLI_OK), ""


def test_the_gateway_answers_first_and_the_cli_is_not_spawned(monkeypatch):
    monkeypatch.setattr(omniroute, "enabled", lambda: True)
    monkeypatch.setattr(omniroute, "_urllib_http", FakeHttp())
    runner = Runner()
    out = ClaudeCliProvider(runner=runner).generate(req())
    assert out.text == "pong" and out.provider == "omniroute"
    assert runner.calls == 0


def test_a_gateway_failure_falls_back_to_the_subscription_cli(monkeypatch):
    monkeypatch.setattr(omniroute, "enabled", lambda: True)
    monkeypatch.setattr(omniroute, "_urllib_http", FakeHttp(raises=ConnectionRefusedError()))
    runner = Runner()
    out = ClaudeCliProvider(runner=runner).generate(req())
    assert out.text == "FROM_CLI" and runner.calls == 1


def test_tools_are_still_refused_before_any_gateway_call(monkeypatch):
    monkeypatch.setattr(omniroute, "enabled", lambda: True)
    http = FakeHttp()
    monkeypatch.setattr(omniroute, "_urllib_http", http)
    with pytest.raises(LlmError):
        ClaudeCliProvider(runner=Runner()).generate(req(tools=({"name": "file_controller"},)))
    assert http.calls == []
