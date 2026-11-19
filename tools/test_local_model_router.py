"""PF-043: Era runs on a free local Qwen primary without melting the laptop.

Design: docs/superpowers/specs/2026-11-18-era-local-model-router-design.md.

* Gemini Live stays the realtime voice model (era.voice).
* era.primary = local qwen3.5:9b; era.local = gemma4:e4b, emergency fallback only.
* Heavy work routes to era.primary; the logged-out direct Claude aliases are never
  chosen automatically.
* Dispatch fails over primary -> local -> overflow on model failures, but never
  on a valid refusal.
* The crew's local path tries Qwen then Gemma and never a paid provider.
* A hot GPU or low RAM refuses a COLD local load; an already-resident model is
  never blocked (no extra latency on the hot path).
* Every Ollama request honors OLLAMA_KEEP_ALIVE (no hard-coded 30m that would
  keep a model squatting VRAM).
* Every model step leaves one bounded, secret-free continuation checkpoint, and
  the next model receives only the latest one.
"""

from __future__ import annotations

import json
import time

import pytest

from core.fabric import context as fc
from core.llm import registry, router
from core.llm.adapters import ollama_chat
from core.llm.resource_guard import admit_cold_load
from core.llm.types import LlmError, LlmMessage, LlmRequest, LlmResponse, Usage
from core.tool_result import ToolResult
from crew import runtime as crew_runtime
from crew.capabilities import Capability
from crew.transport import BotSpec, Turn
from tools.test_llm_dispatch import FakeEra


@pytest.fixture
def real_registry(monkeypatch):
    monkeypatch.delenv("ERA_LLM_CONFIG", raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)
    yield
    registry._CACHE = None


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ZORO_TASK_LOG_DIR", raising=False)
    monkeypatch.setattr(fc, "_sequences", {})
    return tmp_path / "logs"


# ── aliases and routing ─────────────────────────────────────────────────────


def test_voice_stays_on_gemini_live(real_registry):
    voice = registry.resolve("era.voice")
    assert voice.provider == "gemini"
    assert voice.capabilities.realtime_audio is True


def test_qwen_is_primary_and_gemma_is_the_fallback(real_registry):
    primary = registry.resolve("era.primary")
    local = registry.resolve("era.local")
    assert (primary.provider, primary.model) == ("ollama", "qwen3.5:9b")
    assert (local.provider, local.model) == ("ollama", "gemma4:e4b")
    assert router.local_fallback_policy()["alias"] == "era.primary"
    assert router.quota_failover_policy()["local_alias"] == "era.primary"


def test_heavy_work_routes_to_local_qwen_never_to_logged_out_claude(real_registry):
    for task in ("complex_planning", "research_synthesis", "long_context", "creative_advanced"):
        decision = router.route(task)
        assert decision.alias == "era.primary", task
        assert decision.provider == "ollama", task
    by_task = router.routing_policy()["by_task"]
    assert not {"era.reason", "era.deep", "era.cheap"} & set(by_task.values())
    assert router.routing_policy()["availability_fallback"] == "era.primary"


# ── dispatch failover ───────────────────────────────────────────────────────


class _ScriptedOllama:
    """Stands in for OllamaChatProvider; behaviour is scripted per alias."""

    script: dict = {}
    calls: list = []

    def generate(self, request):
        type(self).calls.append(request.alias)
        outcome = type(self).script.get(request.alias, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        return LlmResponse(
            request_id=request.request_id,
            provider="ollama",
            model_id=request.alias,
            alias=request.alias,
            text=f"answer from {request.alias}",
            usage=Usage(),
        )


@pytest.fixture
def scripted(monkeypatch, real_registry, logs):
    _ScriptedOllama.calls = []
    _ScriptedOllama.script = {}
    monkeypatch.setattr(ollama_chat, "OllamaChatProvider", _ScriptedOllama)
    from core.llm.adapters import omniroute

    def _no_overflow(request, **_kw):
        _ScriptedOllama.calls.append(request.alias)
        raise LlmError("unavailable", "overflow off in tests")

    monkeypatch.setattr(omniroute, "generate", _no_overflow)
    return _ScriptedOllama


def test_dispatch_falls_back_from_qwen_to_gemma_on_timeout(scripted):
    scripted.script = {"era.primary": LlmError("timeout", "qwen timed out")}
    era = FakeEra()
    answer, error = era.run_dispatch("plan my week", router.route("complex_planning"))
    assert error == ""
    assert answer == "answer from era.local"
    assert scripted.calls[:2] == ["era.primary", "era.local"]


def test_a_valid_refusal_does_not_fall_through(scripted):
    scripted.script = {"era.primary": LlmError("refusal", "I will not do that")}
    era = FakeEra()
    answer, error = era.run_dispatch("something", router.route("complex_planning"))
    assert answer == ""
    assert "refus" in error.lower() or "will not" in error
    assert scripted.calls == ["era.primary"]


def test_dispatch_success_leaves_a_checkpoint(scripted, logs):
    era = FakeEra()
    era.run_dispatch("plan my week", router.route("complex_planning"))
    records = [
        json.loads(line)
        for path in (logs / "fabric").glob("ctx-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    checkpoints = [r for r in records if r.get("kind") == "checkpoint"]
    assert checkpoints and checkpoints[-1]["status"] == "complete"
    assert checkpoints[-1]["route"]["alias"] == "era.primary"


# ── crew local chain ────────────────────────────────────────────────────────


def test_crew_local_chain_is_qwen_then_gemma_and_never_paid(monkeypatch):
    tried = []

    def fake_call(alias, system, user, tier):
        tried.append(alias)
        if alias == "era.primary":
            raise TimeoutError("qwen busy")
        return {"final": "ok", "_era_route": {"alias": alias, "provider": "ollama", "model": "x"}}

    monkeypatch.setattr(crew_runtime, "_local_crew_call", fake_call)
    reply = crew_runtime.default_llm("sys", "user", "smart")
    assert tried == ["era.primary", "era.local"]
    assert reply["final"] == "ok"
    assert reply["_era_route"]["fell_back_from"][0]["alias"] == "era.primary"


def test_crew_local_chain_exhausted_is_reported_not_replayed(monkeypatch):
    def always_fail(alias, *_a):
        raise ConnectionError("ollama down")

    monkeypatch.setattr(crew_runtime, "_local_crew_call", always_fail)
    reply = crew_runtime.default_llm("sys", "user", "fast")
    assert "__unavailable__" in reply
    assert crew_runtime.default_backup_llm("sys", "user", "fast").get("__unavailable__")


def test_cold_local_crew_model_gets_ninety_seconds_by_default(monkeypatch):
    captured = []

    class Provider:
        def generate(self, request):
            captured.append((request.timeout_s, request.effort_hint))
            return LlmResponse(
                request_id=request.request_id,
                provider="ollama",
                model_id="qwen3.5:9b",
                alias=request.alias,
                text='{"final": "ok"}',
                usage=Usage(),
            )

    monkeypatch.delenv("ERA_CREW_MODEL_TIMEOUT_S", raising=False)
    monkeypatch.setattr(ollama_chat, "OllamaChatProvider", Provider)
    assert crew_runtime._local_crew_call("era.primary", "sys", "user", "smart")["final"] == "ok"
    assert captured == [(90.0, "low")]


def test_local_chain_has_one_outer_attempt_with_room_for_qwen_and_gemma(monkeypatch):
    from crew import reliability

    captured = {}

    def fake_call(model, args, **kwargs):
        captured.update(kwargs)
        return {"final": "ok"}

    monkeypatch.delenv("ERA_CREW_MODEL_TIMEOUT_S", raising=False)
    monkeypatch.setattr(reliability, "call", fake_call)
    transport = crew_runtime.InProcessTransport()
    spec = BotSpec(id="cody", role="coder", description="d", prompt="p", capabilities=())
    reply = transport._ask(
        spec,
        Turn(task="three bugs", deadline=time.monotonic() + crew_runtime.MAX_CALL_S),
        "sys",
        "user",
    )
    assert reply["final"] == "ok"
    assert captured["retries"] == 0
    assert captured["attempt_timeout_s"] == 185.0


def test_bad_model_output_is_handoff_safe_only_before_a_side_effect_runs(monkeypatch):
    spec = BotSpec(
        id="cody",
        role="coder",
        description="d",
        prompt="p",
        capabilities=("era.read",),
    )
    monkeypatch.setitem(
        crew_runtime.CAPABILITIES,
        "era.read",
        Capability(
            "era.read",
            "read test data",
            "",
            lambda _ctx, _args: ToolResult.ok(message="read"),
        ),
    )

    untouched = iter([{"junk": "one"}, {"junk": "two"}])
    transport = crew_runtime.InProcessTransport(llm=lambda *_args: next(untouched))
    transport.register("cody", spec)
    before = transport.dispatch("cody", Turn(task="t"))
    assert before.error_type == "BadModelOutput"
    assert before.data["safe_local_handoff"] is True

    after_read = iter(
        [
            {"call": {"capability": "era.read", "args": {}}},
            {"junk": "one"},
            {"junk": "two"},
        ]
    )
    transport = crew_runtime.InProcessTransport(llm=lambda *_args: next(after_read))
    transport.register("cody", spec)
    after = transport.dispatch("cody", Turn(task="t"))
    assert after.error_type == "BadModelOutput"
    assert after.data["safe_local_handoff"] is True
    assert after.data["capabilities_used"] == ["era.read"]

    monkeypatch.setitem(
        crew_runtime.CAPABILITIES,
        "vault.append",
        Capability(
            "vault.append",
            "change test data",
            "",
            lambda _ctx, _args: ToolResult.ok(message="changed"),
        ),
    )
    write_spec = BotSpec(
        id="cody",
        role="coder",
        description="d",
        prompt="p",
        capabilities=("vault.append",),
    )
    after_write = iter(
        [
            {"call": {"capability": "vault.append", "args": {}}},
            {"junk": "one"},
            {"junk": "two"},
        ]
    )
    transport = crew_runtime.InProcessTransport(llm=lambda *_args: next(after_write))
    transport.register("cody", write_spec)
    after = transport.dispatch("cody", Turn(task="t"))
    assert after.error_type == "BadModelOutput"
    assert after.data["safe_local_handoff"] is False
    assert after.data["capabilities_used"] == ["vault.append"]


# ── resources and lifecycle ─────────────────────────────────────────────────


def test_hot_gpu_refuses_a_cold_load(monkeypatch):
    monkeypatch.delenv("ERA_LOCAL_MAX_GPU_TEMP_C", raising=False)
    adm = admit_cold_load(
        "qwen3.5:9b",
        "http://x",
        loaded_reader=lambda _u: set(),
        temp_reader=lambda: 85.0,
        ram_reader=lambda: 20.0,
    )
    assert adm.allowed is False and "GPU" in adm.reason


def test_low_ram_refuses_a_cold_load(monkeypatch):
    monkeypatch.delenv("ERA_LOCAL_MIN_AVAILABLE_RAM_GB", raising=False)
    adm = admit_cold_load(
        "qwen3.5:9b",
        "http://x",
        loaded_reader=lambda _u: set(),
        temp_reader=lambda: 50.0,
        ram_reader=lambda: 2.0,
    )
    assert adm.allowed is False and "RAM" in adm.reason


def test_a_resident_model_is_never_sampled_or_blocked():
    def boom():
        raise AssertionError("telemetry sampled on the hot path")

    adm = admit_cold_load(
        "qwen3.5:9b",
        "http://x",
        loaded_reader=lambda _u: {"qwen3.5:9b"},
        temp_reader=boom,
        ram_reader=boom,
    )
    assert adm.allowed is True and adm.cold_load is False


def test_requests_honor_keep_alive_and_effort_based_thinking(monkeypatch, real_registry):
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "5m")
    provider = ollama_chat.OllamaChatProvider()
    fast = LlmRequest(
        alias="era.primary", messages=(LlmMessage.text("user", "hi"),), effort_hint="low"
    )
    heavy = LlmRequest(
        alias="era.primary", messages=(LlmMessage.text("user", "plan"),), effort_hint="high"
    )
    fast_body = provider._body(fast, stream=False)
    assert fast_body["keep_alive"] == "5m"
    assert fast_body["model"] == "qwen3.5:9b"
    assert fast_body["think"] is False
    assert provider._body(heavy, stream=False)["think"] is True
    assert fast_body["options"]["num_ctx"] == ollama_chat.DEFAULT_NUM_CTX


def test_a_bad_keep_alive_value_cannot_pin_a_model_forever(monkeypatch):
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "forever; rm")
    assert ollama_chat.keep_alive() == "5m"


# ── continuation checkpoints ────────────────────────────────────────────────


def test_checkpoint_is_bounded_and_redacts_secrets(logs):
    ctx = fc.new_context(origin="test", utterance="u", task="t", intent="i", discover=False)
    slice_ = fc.add_checkpoint(
        ctx.ctx_id,
        status="model_step",
        note="used api_key=sk-SECRET123 then " + "x" * 2000,
        alias="era.primary",
        provider="ollama",
        model="qwen3.5:9b",
    )
    assert "sk-SECRET123" not in json.dumps(slice_)
    assert len(slice_["note"]) <= 500
    assert slice_["sequence"] == 1


def test_the_next_model_receives_only_the_latest_checkpoint(logs):
    ctx = fc.new_context(origin="test", utterance="u", task="t", intent="i", discover=False)
    fc.add_checkpoint(ctx.ctx_id, status="model_step", note="step one done")
    fc.add_checkpoint(ctx.ctx_id, status="complete", note="step two done", next_action="answer")
    loaded = fc.load(ctx.ctx_id)
    assert loaded["checkpoint"]["sequence"] == 2
    assert loaded["checkpoint"]["note"] == "step two done"
    rebuilt = fc.TaskContext(
        ctx_id=ctx.ctx_id, origin="test", utterance="u", intent="i", checkpoint=loaded["checkpoint"]
    )
    assert rebuilt.for_turn()["checkpoint"]["next_action"] == "answer"


def test_the_checkpoint_reaches_the_successor_models_prompt():
    from crew.transport import Turn

    turn = Turn(
        task="finish the audit",
        context={
            "ctx_id": "c1",
            "checkpoint": {
                "sequence": 3,
                "note": "Laura read config/llm.yaml",
                "next_action": "summarize findings",
            },
        },
    )
    prompt = crew_runtime._user(turn, [])
    assert "Laura read config/llm.yaml" in prompt
    assert "summarize findings" in prompt
