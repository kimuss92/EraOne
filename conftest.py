"""Pytest bootstrap.

Three jobs: settle the COM apartment model, put the repo root on sys.path, and
redirect the persistent memory stores at a throwaway copy so the suite never
writes to the user's real data.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ── COM apartment model ─────────────────────────────────────────────────────
# MUST be set before anything imports comtypes or pywinauto, which is why it is
# the first statement in this file rather than a fixture.
#
# comtypes calls CoInitializeEx() AT IMPORT TIME, reading sys.coinit_flags and
# defaulting to APARTMENTTHREADED when it is unset. pywinauto sets that flag to
# 0 (MULTITHREADED) when IT is imported. So whichever lands first decides, and
# the loser gets
#
#     OSError: [WinError -2147417850] Cannot change thread mode after it is set
#
# at import — which pytest reports as a COLLECTION ERROR that aborts the whole
# run, not as one failing file. Observed 2026-09-29: test_memory_guard.py then
# tools/test_spotify_search.py killed all 2,427 tests, while the reverse order
# was clean. A plain `pytest` run only escaped it because `testpaths = tools .`
# happens to import the pywinauto side first.
#
# Pinning it to 0 here makes both agree, so the outcome no longer depends on
# collection order. Test-process only — Era's own runtime is untouched.
sys.coinit_flags = 0  # COINIT_MULTITHREADED

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))

# ── Memory isolation ────────────────────────────────────────────────────────
# This MUST happen at conftest import time, before any project module is
# imported: the stores bind their paths to module-level constants
# (memory_manager.MEMORY_PATH, intent_memory.STATE_PATH, ...) at import, so a
# fixture would run far too late to move them.
#
# Why it's needed: a number of tests legitimately write state and read it back
# — `save_state({})` in test_browser_media_routing, `update_memory(...)` in the
# memory-guard tests, `remember_action(...)` in test_regression_14b1. Without a
# redirect every one of those writes landed in the real memory/ directory: it
# mutated the user's actual long-term memory, preferences and runtime state,
# spawned real entries in memory/backups/, and let one test clobber state that
# another had just set up.
#
# The temp directory is SEEDED with a copy of the real store rather than left
# empty, because several tests expect realistic content to be present. Writes
# go to the copy; the originals are never opened for writing.
_REAL_MEMORY_DIR = _REPO_ROOT / "memory"
_TEST_MEMORY_DIR = Path(tempfile.mkdtemp(prefix="era-test-memory-"))

if _REAL_MEMORY_DIR.is_dir():
    for _src in _REAL_MEMORY_DIR.iterdir():
        # Data only. memory/ is also a Python package — its modules keep
        # importing from the real location, so .py files must NOT be copied.
        if _src.is_file() and _src.suffix in (".json", ".jsonl"):
            shutil.copy2(_src, _TEST_MEMORY_DIR / _src.name)

os.environ["ERA_MEMORY_DIR"] = str(_TEST_MEMORY_DIR)

# Semantic recall (memory/semantic_recall.py) talks to a local Ollama. Left on,
# every test that ranks a memory would reach a real service on a developer
# machine — embedding, writing caches, and making results depend on whether
# Ollama happens to be running. Off here; tools/test_semantic_recall.py turns it
# back on for the tests whose subject it actually is.
os.environ["ERA_SEMANTIC_RECALL"] = "0"
# Never let importing application startup from tests launch PC Manager's UI
# automation. Unit tests that exercise the watchdog must inject their own fake
# sampler/boost and opt in explicitly.
os.environ["ERA_RAM_WATCHDOG"] = "0"
# The user's live high-risk waivers (e.g. "shut down without asking") must not
# leak into the copy: power-gate tests assert the default confirm-first behaviour.
_prefs_copy = _TEST_MEMORY_DIR / "user_prefs.json"
if _prefs_copy.is_file():
    import json as _json

    try:
        _p = _json.loads(_prefs_copy.read_text(encoding="utf-8"))
        # A live voice mic-mute would likewise start every test engine deaf.
        if isinstance(_p, dict) and any(
            [_p.pop(_k, None) is not None for _k in ("risk_confirmation_waived", "mic_muted")]
        ):
            _prefs_copy.write_text(_json.dumps(_p, indent=2), encoding="utf-8")
    except Exception:
        pass

# The audit log lives under logs/. core.logger already honours ERA_LOG_DIR;
# memory.memory_audit resolves its directory at call time and documents the
# module attribute as test-overridable, so it is redirected in
# pytest_configure below (after project imports are safe).
_TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="era-test-logs-"))
os.environ.setdefault("ERA_LOG_DIR", str(_TEST_LOG_DIR))

# The Obsidian vault, for the same reason and with more at stake.
#
# `run_task` writes an entry to the brand's run log on every finished job, and
# tests that exercise the real dispatch path (test_ads_dispatch, with the
# subprocess mocked) reach that write. cm_vault.VAULT_DIR is resolved from
# ERA_VAULT_DIR at IMPORT time, so with nothing set here it pointed at the live
# vault — and every full-suite run appended a fake `ads` entry to the user's real
# Era_Run_Log_delta_cuisine.md. Three runs today put three of them there before
# this was noticed.
#
# Unlike memory/ this is NOT seeded from the real vault: nothing under test needs
# the user's actual notes, and copying them in would only give a test a way to
# assert against content that is free to change. An empty PARA tree is enough for
# the writes to land somewhere harmless.
_TEST_VAULT_DIR = Path(tempfile.mkdtemp(prefix="era-test-vault-"))
for _folder in (
    "00 - Inbox",
    "01 - Daily Notes",
    "02 - Delta Cuisine",
    "03 - Ceram Quartz",
    "04 - Global",
    "05 - Archive",
    "06 - Resources",
):
    (_TEST_VAULT_DIR / _folder).mkdir(parents=True, exist_ok=True)
os.environ["ERA_VAULT_DIR"] = str(_TEST_VAULT_DIR)


# The Gmail state directory, for the same reason and with the most at stake of
# all three: it holds the OAuth refresh token for the user's real mailbox.
#
# core.gmail_service resolves ERA_GMAIL_DIR at CALL time, so this redirect works
# whenever it is set before the first call — but it is set here, beside the
# others, so no future test can reach the live token by importing early. Never
# seeded: a test must never run against real credentials, and a test that needs
# a token writes its own fake one.
_TEST_GMAIL_DIR = Path(tempfile.mkdtemp(prefix="era-test-gmail-"))
os.environ["ERA_GMAIL_DIR"] = str(_TEST_GMAIL_DIR)

# The Protocol hive (core/hive.py): the live issue map Era briefs the user from.
# A test posting a fake "critical" insight into runtime/hive would be read aloud
# as a real finding. Resolved at call time, like Gmail.
_TEST_HIVE_DIR = Path(tempfile.mkdtemp(prefix="era-test-hive-"))
os.environ["ERA_HIVE_DIR"] = str(_TEST_HIVE_DIR)


# The user's scheduled tasks (core/task_scheduler.py). A test task left in the real
# store would be re-registered at Era's next start and actually run.
_TEST_TASKS_DIR = Path(tempfile.mkdtemp(prefix="era-test-tasks-"))
os.environ["ERA_SCHEDULED_TASKS_DIR"] = str(_TEST_TASKS_DIR)


# The cross-process dispatch mutex (era_dispatch.FileLock, ~/.era/era.lock).
#
# This one is not about writing to the user's data — it is about CONTENDING with
# the user's running Era. Several test_ads_dispatch tests call the real
# `run_task()` with only `subprocess.run` faked, and run_task takes the shared
# job lock before it gets that far. When Era is live (or a previous crash left a
# lockdir behind) the suite waits on that lock for the full `_DEFAULT_TIMEOUT` —
# 1800 s. It does not fail, it does not report: pytest simply produces no output
# for thirty minutes and a plain `python -m pytest` looks hung.
#
# _LOCK_PATH is resolved from the environment at IMPORT time (era_dispatch line
# ~64), exactly like cm_vault.VAULT_DIR, so this must be set here and not in a
# fixture. FileLock appends ".lockdir" to whatever it is given.
_TEST_LOCK_DIR = Path(tempfile.mkdtemp(prefix="era-test-lock-"))
os.environ["ERA_LOCK_FILE"] = str(_TEST_LOCK_DIR / "era.lock")

# The CM job status file (era_dispatch._STATUS_PATH, ~/.era/era_status.json),
# also resolved at IMPORT time. run_task() writes it and unlinks it, so without
# this the suite rewrote and deleted the LIVE Era's "a job is running" marker —
# and failed intermittently with WinError 32 whenever the running Era had the
# file open at the moment of the unlink (seen 2026-09-26).
os.environ["ERA_STATUS_FILE"] = str(_TEST_LOCK_DIR / "era_status.json")


@atexit.register
def _cleanup_test_memory_dirs() -> None:
    for d in (
        _TEST_MEMORY_DIR,
        _TEST_LOG_DIR,
        _TEST_VAULT_DIR,
        _TEST_GMAIL_DIR,
        _TEST_HIVE_DIR,
        _TEST_TASKS_DIR,
        _TEST_LOCK_DIR,
    ):
        shutil.rmtree(d, ignore_errors=True)


def pytest_configure(config):
    for marker in ("slow", "unit", "integration", "smoke"):
        config.addinivalue_line("markers", marker)

    # Requires importing a project module, so it has to run after
    # ERA_MEMORY_DIR is set above rather than at import time.
    try:
        import memory.memory_audit as _audit

        _audit.AUDIT_DIR = _TEST_LOG_DIR
    except Exception:
        pass

    # ── Live browser isolation ──────────────────────────────────────────────
    # Same motivation as the redirects above, for the one piece of user state
    # that is not a directory: the running Brave.
    #
    # The media helpers resolve their session lazily and function-locally —
    # `from actions.browser_intelligence import scan_netflix_cards` *inside* the
    # function, which then calls get_browser_session on its OWN module. A test
    # that patches the calling module's names therefore does not stop them, and
    # unpatched they run against whatever the user happens to have open. That is
    # not hypothetical: a YouTube test went red because Brave had moved to an
    # unrelated video, 27 Netflix tests were opening real CDP links mid-suite,
    # and is_netflix_profile_page's live answer gated a real profile CLICK in the
    # user's browser.
    #
    # _launch() is the single gateway to that attachment (it is what reaches
    # connect_over_cdp on port 9222), so blocking it here covers every route in
    # and cannot be bypassed by a function-local import. It deliberately does NOT
    # touch run()/_ensure_driver()/_restart_driver(): test_browser_driver_recovery
    # drives a REAL Playwright driver on purpose — with no browser attached — and
    # must keep working.
    #
    # Set ERA_ALLOW_LIVE_BROWSER=1 to run something against the real browser.
    if not os.environ.get("ERA_ALLOW_LIVE_BROWSER"):
        try:
            import actions.browser_control as _bc

            async def _blocked_launch(self, *_args, **_kwargs):
                raise RuntimeError(
                    "Live browser attach blocked under pytest. Mock the helper you "
                    "are calling (scan_netflix_cards / scan_youtube_cards / "
                    "is_netflix_profile_page / get_or_open_platform_tab) at its "
                    "OWN module, or set ERA_ALLOW_LIVE_BROWSER=1."
                )

            _bc._BrowserSession._launch = _blocked_launch
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _reset_vision_provider_health():
    """core.vision_resilience benches a provider after it fails; that is process-wide
    state, so one test's simulated Ollama/Gemini failure would silently skip the provider
    in the next test. Only touched if a test already imported it."""
    yield
    module = sys.modules.get("core.vision_resilience")
    if module is not None:
        module.reset_provider_health()


@pytest.fixture(autouse=True)
def _reset_browser_grid_state():
    """core.browser_grid_state (saved grids, scroll context, last query) is process-wide.
    Netflix grid_play falls back to the last saved grid query, so a grid left by one
    test turned another's "no title → list and ask" into "Playing: One Piece".
    Only touched if a test already imported it."""
    module = sys.modules.get("core.browser_grid_state")
    if module is not None:
        module.clear_all_for_tests()
    yield
    module = sys.modules.get("core.browser_grid_state")
    if module is not None:
        module.clear_all_for_tests()


def pytest_report_header(config):
    return f"era: memory store isolated -> {_TEST_MEMORY_DIR}"
