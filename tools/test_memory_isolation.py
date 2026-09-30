"""Guard: the test suite must never write to the real memory store.

Several tests legitimately write state and read it back (`save_state({})`,
`update_memory(...)`, `remember_action(...)`). conftest.py redirects the stores
to a throwaway copy via ERA_MEMORY_DIR before any project module is imported.

That redirect is easy to break silently — a new store that hardcodes
``BASE_DIR / "memory"``, or a module whose constant is bound before conftest
runs, would quietly resume writing to the user's real data and nothing else in
the suite would notice. These tests fail loudly instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_MEMORY_DIR = REPO_ROOT / "memory"


def _test_memory_dir() -> Path:
    raw = os.environ.get("ERA_MEMORY_DIR")
    assert raw, "ERA_MEMORY_DIR is not set — conftest.py memory isolation did not run"
    return Path(raw).resolve()


def test_memory_dir_is_redirected():
    d = _test_memory_dir()
    assert d != REAL_MEMORY_DIR.resolve(), "ERA_MEMORY_DIR points at the REAL memory directory"
    assert d.is_dir()


def test_get_memory_dir_honours_the_redirect():
    from core.utils import get_memory_dir

    assert get_memory_dir().resolve() == _test_memory_dir()


@pytest.mark.parametrize(
    ("module_path", "attr"),
    [
        ("memory.memory_manager", "MEMORY_PATH"),
        ("core.intent_memory", "STATE_PATH"),
        ("core.user_prefs", "PREFS_PATH"),
        ("core.self_learning", "FEEDBACK_PATH"),
        ("core.self_learning", "PATTERNS_PATH"),
        ("memory.feedback_memory", "FEEDBACK_PATH"),
        ("memory.feedback_memory", "LEARNED_MISTAKES_PATH"),
        ("actions.computer_control", "_MEMORY_PATH"),
    ],
)
def test_every_store_constant_is_outside_the_real_memory_dir(module_path: str, attr: str):
    """Each store's module-level path must resolve into the throwaway dir.

    Parametrised per store so a newly-added one that misses the redirect names
    itself in the failure rather than hiding behind an aggregate assertion.
    """
    import importlib

    mod = importlib.import_module(module_path)
    path = Path(getattr(mod, attr)).resolve()
    assert path.parent == _test_memory_dir(), (
        f"{module_path}.{attr} -> {path}\n"
        f"expected it under {_test_memory_dir()}. If this is a new store, resolve "
        f"its path with core.utils.get_memory_dir() instead of BASE_DIR / 'memory'."
    )
    assert REAL_MEMORY_DIR.resolve() not in path.parents


def test_a_real_write_lands_in_the_copy_not_the_original():
    """Positive control: prove writes actually happen, and land in the copy."""
    import hashlib

    from core.intent_memory import STATE_PATH, load_state, remember_action

    real_state = REAL_MEMORY_DIR / "runtime_intent.json"
    before = hashlib.sha256(real_state.read_bytes()).hexdigest() if real_state.exists() else None

    remember_action(tool="netflix_control", action="play", media_app="__isolation_probe__")

    assert load_state().get("last_media_app") == "__isolation_probe__", (
        "the write did not take effect — this test would not prove anything"
    )
    assert STATE_PATH.exists(), "state file was not created in the redirected directory"

    after = hashlib.sha256(real_state.read_bytes()).hexdigest() if real_state.exists() else None
    assert before == after, "the REAL runtime_intent.json was modified by the test suite"
