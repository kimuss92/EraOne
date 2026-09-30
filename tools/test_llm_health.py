"""Tests for the LLM health layer (phase 6): registry.doctor(),
registry.provider_health(), and the two era_doctor checks that format them.

WHAT A HEALTH CHECK HAS TO EARN
-------------------------------
A check that passes on a healthy machine has proved nothing — the failure mode
worth fearing is a validator that reads 0 of 0 subjects and reports READY. This
repo has been bitten by exactly that once already (the tool-registry validator
that passed vacuously after a refactor moved the source text it was reading).

So every check below is tested in BOTH directions: it passes on a good config
AND it fails on a specifically broken one. The broken-config tests are the
point; the passing ones only guard against over-strictness.

Isolation follows test_llm_registry.py: ERA_LLM_CONFIG points at a fixture, so
these tests exercise the code rather than the state of the user's machine. The
handful that deliberately read the SHIPPED config/llm.yaml say so in the name.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.llm import registry  # noqa: E402

HEALTHY = """
version: 1
default: era.voice
voice_default: era.voice
providers:
  gemini:
    reports_cost: false
  claude_cli:
    reports_cost: true
    config_dir: {config_dir}
aliases:
  era.voice:
    provider: gemini
    model: models/gemini-live
    capabilities:
      realtime_audio: true
      tools: true
  era.reason:
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
routing:
  by_task:
    short_command: era.voice
    complex_planning: era.reason
    community_management: LANE:cm
  availability_fallback: era.voice
  daily_budget_usd: 2.0
"""


def write_config(tmp_path, text: str) -> Path:
    path = tmp_path / "llm.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Never read the user's real registry; never serve a stale cache."""
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(write_config(tmp_path, healthy_text(tmp_path))))
    return tmp_path


def healthy_text(tmp_path) -> str:
    return HEALTHY.format(config_dir=tmp_path)


def repoint(monkeypatch, tmp_path, text: str) -> None:
    """Swap in a different registry for one test."""
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(write_config(tmp_path, text)))


# ── doctor(): the healthy baseline ────────────────────────────────────────


def test_healthy_registry_reports_no_problems():
    info = registry.doctor()
    assert info["loaded"] is True
    assert info["problems"] == [], info["problems"]


def test_healthy_registry_counts_are_non_vacuous():
    """Guard against the 0-of-0 pass: the check must have inspected something."""
    info = registry.doctor()
    assert info["aliases"] == 3
    assert info["available"] == ["era.reason", "era.voice"]


def test_unavailable_alias_is_reported_with_its_reason():
    info = registry.doctor()
    assert len(info["unavailable"]) == 1
    assert info["unavailable"][0]["alias"] == "era.cheap"
    assert "proxy" in info["unavailable"][0]["reason"]


def test_defaults_and_budget_surface():
    info = registry.doctor()
    assert info["default"] == "era.voice"
    assert info["voice"] == "era.voice"
    assert info["daily_budget_usd"] == 2.0


def test_provider_usage_is_counted():
    info = registry.doctor()
    assert info["providers"]["claude_cli"]["aliases"] == 2
    assert info["providers"]["gemini"]["aliases"] == 1


# ── doctor(): each problem it exists to catch ─────────────────────────────


def test_unreadable_registry_reports_loaded_false(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "absent.yaml"))
    info = registry.doctor()
    assert info["loaded"] is False
    assert info["error"]


def test_doctor_never_raises_on_a_broken_file(monkeypatch, tmp_path):
    """A health tool that dies on the thing it is inspecting tells you nothing."""
    path = tmp_path / "llm.yaml"
    path.write_text("aliases: [this is: not a mapping", encoding="utf-8")
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(path))
    info = registry.doctor()  # must not raise
    assert info["loaded"] is False


def test_unknown_default_is_a_problem(monkeypatch, tmp_path):
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace("default: era.voice\n", "default: era.typo\n", 1),
    )
    problems = registry.doctor()["problems"]
    assert any("era.typo" in p for p in problems), problems


def test_unknown_voice_default_is_a_problem(monkeypatch, tmp_path):
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace("voice_default: era.voice", "voice_default: era.nope"),
    )
    problems = registry.doctor()["problems"]
    assert any("era.nope" in p for p in problems), problems


def test_a_voice_alias_that_cannot_hear_is_a_problem(monkeypatch, tmp_path):
    """The one hard invariant: Era feeds raw PCM in with no STT stage, so an
    alias without realtime audio does not degrade the voice loop, it removes it."""
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace("voice_default: era.voice", "voice_default: era.reason"),
    )
    problems = registry.doctor()["problems"]
    assert any("realtime_audio" in p for p in problems), problems


def test_a_routing_typo_is_a_problem(monkeypatch, tmp_path):
    """This is the silent one: a bad by_task target does not crash, the request
    just quietly takes the fallback."""
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace(
            "complex_planning: era.reason", "complex_planning: era.resaon"
        ),
    )
    problems = registry.doctor()["problems"]
    assert any("era.resaon" in p for p in problems), problems


def test_a_lane_target_is_not_treated_as_an_alias():
    """community_management routes to a whole separate lane, not a model."""
    assert registry.doctor()["problems"] == []


def test_unknown_availability_fallback_is_a_problem(monkeypatch, tmp_path):
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace(
            "availability_fallback: era.voice", "availability_fallback: era.ghost"
        ),
    )
    problems = registry.doctor()["problems"]
    assert any("era.ghost" in p for p in problems), problems


def test_an_unavailable_fallback_is_a_problem(monkeypatch, tmp_path):
    """The fallback is what everything else falls back TO. If it is itself out,
    a provider outage has nowhere to land."""
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace(
            "availability_fallback: era.voice", "availability_fallback: era.cheap"
        ),
    )
    problems = registry.doctor()["problems"]
    assert any("itself unavailable" in p for p in problems), problems


def test_a_provider_with_no_block_is_a_problem(monkeypatch, tmp_path):
    text = healthy_text(tmp_path).replace(
        "    provider: gemini\n", "    provider: ghostprovider\n", 1
    )
    repoint(monkeypatch, tmp_path, text)
    problems = registry.doctor()["problems"]
    assert any("ghostprovider" in p for p in problems), problems


def test_an_alias_missing_a_model_is_a_problem(monkeypatch, tmp_path):
    text = healthy_text(tmp_path).replace("    model: sonnet\n", "", 1)
    repoint(monkeypatch, tmp_path, text)
    problems = registry.doctor()["problems"]
    assert any("era.reason" in p for p in problems), problems


# ── provider_health() ─────────────────────────────────────────────────────


def test_provider_health_covers_every_provider_in_use():
    names = {row["provider"] for row in registry.provider_health()}
    assert names == {"gemini", "claude_cli"}


def test_claude_cli_probe_passes_when_the_binary_is_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: r"C:\bin\claude.exe")
    rows = {r["provider"]: r for r in registry.provider_health()}
    assert rows["claude_cli"]["status"] == "ok"


def test_claude_cli_probe_fails_when_the_binary_is_missing(monkeypatch):
    """Without the binary, every Claude alias is dead — and nothing else in
    era_doctor would notice."""
    monkeypatch.setattr("shutil.which", lambda _n: None)
    rows = {r["provider"]: r for r in registry.provider_health()}
    assert rows["claude_cli"]["status"] == "error"
    assert "PATH" in rows["claude_cli"]["detail"]


def test_claude_cli_probe_warns_on_a_missing_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _n: r"C:\bin\claude.exe")
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace(str(tmp_path), str(tmp_path / "absent")),
    )
    rows = {r["provider"]: r for r in registry.provider_health()}
    assert rows["claude_cli"]["status"] == "warn"


def test_claude_cli_probe_honours_CLAUDE_BIN(monkeypatch):
    seen: list[str] = []

    def fake_which(name):
        seen.append(name)
        return r"C:\bin\custom.exe"

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setenv("CLAUDE_BIN", "custom-claude")
    registry.provider_health()
    assert "custom-claude" in seen


def test_claude_cli_probe_says_it_is_not_an_auth_check(monkeypatch):
    """Phase 4 established that an already-connected interactive connector does
    NOT carry into a headless run. The wording must not overclaim."""
    monkeypatch.setattr("shutil.which", lambda _n: r"C:\bin\claude.exe")
    rows = {r["provider"]: r for r in registry.provider_health()}
    assert "not an auth check" in rows["claude_cli"]["detail"]


def test_provider_health_reports_an_unloadable_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "absent.yaml"))
    rows = registry.provider_health()
    assert rows and rows[0]["status"] == "error"


def test_an_unknown_provider_gets_a_warning_not_a_crash(monkeypatch, tmp_path):
    text = (
        healthy_text(tmp_path)
        .replace("  gemini:\n", "  mystery:\n", 1)
        .replace("    provider: gemini\n", "    provider: mystery\n", 1)
    )
    repoint(monkeypatch, tmp_path, text)
    rows = {r["provider"]: r for r in registry.provider_health()}
    assert rows["mystery"]["status"] == "warn"


# ── the era_doctor checks that format all of the above ────────────────────


@pytest.fixture
def doctor_module():
    import era_doctor

    return era_doctor


def test_doctor_check_passes_on_a_healthy_registry(doctor_module, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: r"C:\bin\claude.exe")
    status, detail = doctor_module.check_llm_registry()
    assert status == "OK"
    assert "3" in detail  # alias count reached the report


def test_doctor_check_fails_on_a_routing_typo(doctor_module, monkeypatch, tmp_path):
    repoint(
        monkeypatch,
        tmp_path,
        healthy_text(tmp_path).replace(
            "complex_planning: era.reason", "complex_planning: era.resaon"
        ),
    )
    status, detail = doctor_module.check_llm_registry()
    assert status == "ERROR"
    assert "era.resaon" in detail


def test_doctor_check_fails_on_an_unloadable_registry(doctor_module, monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "absent.yaml"))
    status, _ = doctor_module.check_llm_registry()
    assert status == "ERROR"


def test_doctor_provider_check_fails_without_the_binary(doctor_module, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: None)
    status, detail = doctor_module.check_llm_providers()
    assert status == "ERROR"
    assert "claude_cli" in detail


def test_doctor_provider_check_passes_with_the_binary(doctor_module, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: r"C:\bin\claude.exe")
    status, _ = doctor_module.check_llm_providers()
    assert status == "OK"


def test_both_checks_are_registered_in_the_doctor_run(doctor_module):
    """A check nobody runs is not a check."""
    names = [name for name, _sev, _fn in doctor_module.CHECKS]
    assert "LLM alias registry" in names
    assert "LLM provider reachability" in names


# ── the SHIPPED registry ──────────────────────────────────────────────────
# Everything above runs against a fixture, so none of it would notice a typo in
# the real config/llm.yaml. These read the actual file.


@pytest.fixture
def real_registry(monkeypatch):
    monkeypatch.delenv("ERA_LLM_CONFIG", raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)
    return registry.CONFIG_PATH


def test_shipped_registry_is_structurally_clean(real_registry):
    """The check that would have caught a typo in the file we actually ship."""
    info = registry.doctor()
    assert info["loaded"] is True, info["error"]
    assert info["problems"] == [], info["problems"]


def test_shipped_registry_inspection_is_non_vacuous(real_registry):
    info = registry.doctor()
    assert info["aliases"] >= 5, f"only {info['aliases']} aliases inspected"
    assert len(info["available"]) >= 4


def test_shipped_fallback_is_available(real_registry):
    info = registry.doctor()
    fallback = registry.routing_policy().get("availability_fallback")
    assert fallback in info["available"]
