"""Tests for actions/claude_self_improve.py — Claude CLI generator + fallback."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from actions import claude_self_improve as cs
from actions import self_improve as si

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _not_nested(monkeypatch):
    monkeypatch.delenv(cs._GUARD_ENV, raising=False) if hasattr(cs, "_GUARD_ENV") else None


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A repo-relative module self_improve may rewrite; backups stay in tmp_path."""
    target = REPO_ROOT / "tools" / "_verifier_selftest.py"
    original = target.read_bytes() if target.exists() else None
    target.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(si, "BACKUP_ROOT", tmp_path / "backups")
    try:
        yield target
    finally:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(original)
            assert target.read_bytes() == original, "sandbox did not restore the tracked source"
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_strips_fences():
    assert cs._strip_fences("```python\nprint('hi')\n```") == "print('hi')\n"
    assert cs._strip_fences("```\nprint('hi')\n```") == "print('hi')\n"


def test_claude_generate_factory_callable():
    f = cs.claude_generate_factory()
    assert callable(f)


def test_apply_routes_through_claude_generator(sandbox, monkeypatch):
    """generator='claude' must reach the factory and pass its output to _apply,
    which then verifies + rolls back on failure. We stub the CLI so no real
    model call happens."""
    calls = []

    def fake_generate(target, original, instruction):
        calls.append((str(target), instruction))
        # syntactically invalid Python -> verification fails -> rollback
        return "def broken(:\n"

    monkeypatch.setattr(cs, "_generate_via_claude_cli", fake_generate)
    # The guided repair pass is a SECOND model call. Unstubbed, a live Gemini call
    # repaired the broken output, so the rollback this test asserts never happened.
    monkeypatch.setattr(si, "_generate_repair", lambda *a, **k: "def still_broken(:\n")

    result = si.self_improve(
        {
            "action": "apply",
            "file_path": "tools/_verifier_selftest.py",
            "description": "break it",
            "generator": "claude",
        }
    )
    assert calls, "the claude generator was never called"
    assert "rolled" in result.lower() or "verification failed" in result.lower()
    # file must be restored to its original content
    assert sandbox.read_text(encoding="utf-8") == "X = 1\n"


def test_default_generator_unchanged(sandbox, monkeypatch):
    """Without generator='claude', the default Gemini path is used."""
    seen = []

    def fake_default(target, original, instruction):
        seen.append(instruction)
        return "Y = 2\n"

    monkeypatch.setattr(si, "_generate_updated_code", fake_default)

    result = si.self_improve(
        {
            "action": "apply",
            "file_path": "tools/_verifier_selftest.py",
            "description": "set Y",
        }
    )
    assert seen, "the default generator was never called"
    assert "Done" in result
    assert sandbox.read_text(encoding="utf-8") == "Y = 2\n"


def test_claude_generator_unavailable_falls_back(sandbox, monkeypatch):
    """If the claude generator import fails, self_improve must not crash —
    it falls back to the default generator."""

    def boom():
        raise RuntimeError("claude cli not installed")

    monkeypatch.setattr(cs, "claude_generate_factory", boom)

    def fake_default(target, original, instruction):
        return "Z = 3\n"

    monkeypatch.setattr(si, "_generate_updated_code", fake_default)

    result = si.self_improve(
        {
            "action": "apply",
            "file_path": "tools/_verifier_selftest.py",
            "description": "set Z",
            "generator": "claude",
        }
    )
    assert "Done" in result
    assert sandbox.read_text(encoding="utf-8") == "Z = 3\n"
