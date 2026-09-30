"""The last step of voice-to-code: restart so a verified self-edit goes live.

WHAT IS BEING PROTECTED
  Era edits her own source, verifies it, and then — only then — restarts so
  the change is actually visible. The restart is the useful half and the
  dangerous half at once, so almost every test here is about REFUSING it:

    unverified code          `review` / `environment` / `skipped` all mean the
                             independent second check could not vouch for the
                             change. Restarting into it is exactly what the
                             backup/verify/rollback pipeline exists to prevent.
    unattended callers       Protocol One drives the same _apply() inside a
                             ~12-minute maintenance cycle, and self_heal.py
                             drives it from the healing loop. A default-on
                             reload would restart Era mid-cycle.
    a running CM job         minutes of real work against live brand accounts.
                             A self-edit is never urgent enough to interrupt
                             one.
    nothing supervising      if no watchdog will bring Era back, exiting is an
                             outage wearing a reload's clothes.

  The happy path is one test. The refusals are seven, and that ratio is the
  point: a reload that fires when it should not is far more expensive than one
  that fails to fire.

WHY THE SIGNAL IS SPLIT ACROSS TWO LAYERS
  actions/self_improve.py never restarts anything (its rule 5: no executing a
  repo file, no second Era, no Telegram 409). It only reports that a reload
  was asked for AND would be safe. The live-session layer decides and acts.
  These tests hold both halves of that contract.
"""

from __future__ import annotations

import pytest

from actions import self_improve as si


class FakeVerdict:
    def __init__(self, status: str, summary: str = ""):
        self.status = status
        self.summary = summary
        self.lines: list[str] = []


@pytest.fixture
def target(tmp_path, monkeypatch):
    """A writable file inside a fake repo, so nothing real is touched."""
    repo = tmp_path / "repo"
    (repo / "actions").mkdir(parents=True)
    f = repo / "actions" / "demo.py"
    f.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(si, "REPO_ROOT", repo)
    monkeypatch.setattr(si, "BACKUP_ROOT", repo / "runtime" / "backups")
    return f


def _stub_pipeline(monkeypatch, verdict_status: str):
    """Everything between 'write the file' and 'the verdict' is stubbed.

    Generation, ruff and pytest are exercised by tools/test_self_improve*.py
    already; re-running them here would test those, not the reload decision.

    The generator MUST be stubbed, not just the verifier: left real, these
    tests call Gemini for every case (observed once as a 429 that made six of
    them fail for a reason unrelated to what they assert). A unit test of a
    decision should never depend on a network quota.
    """
    monkeypatch.setattr(
        si, "_generate_updated_code", lambda target, original, instruction: "VALUE = 2\n"
    )
    monkeypatch.setattr(si, "_verify_and_heal", lambda t: ([], [], []))

    from actions import self_improve_verifier

    monkeypatch.setattr(
        self_improve_verifier,
        "verify",
        lambda *a, **k: FakeVerdict(verdict_status, "checked."),
    )


# ── the action layer reports; it never acts ─────────────────────────────────


def test_a_clean_pass_with_reload_requested_sets_the_signal(target, monkeypatch):
    _stub_pipeline(monkeypatch, "pass")
    out: dict = {}
    si.self_improve(
        {
            "action": "apply",
            "file_path": "actions/demo.py",
            "description": "bump it",
            "reload": True,
        },
        out=out,
    )
    assert out["verdict"] == "pass"
    assert out["reload_safe"] is True
    assert out["reload_requested"] is True


@pytest.mark.parametrize("status", ["review", "environment", "skipped"])
def test_an_unvouched_verdict_never_authorises_a_reload(target, monkeypatch, status):
    """The expensive half. None of these mean "the change is good"."""
    _stub_pipeline(monkeypatch, status)
    out: dict = {}
    text = si.self_improve(
        {
            "action": "apply",
            "file_path": "actions/demo.py",
            "description": "bump it",
            "reload": True,
        },
        out=out,
    )
    assert out["reload_safe"] is False
    assert out["reload_requested"] is False
    assert "not restarting on my own" in text, "the refusal must be stated, not silent"
    assert status in text, "the user should learn WHY it refused"


def test_reload_defaults_to_false_so_automation_never_restarts_era(target, monkeypatch):
    """Protocol One and self_heal drive the same path unattended."""
    _stub_pipeline(monkeypatch, "pass")
    out: dict = {}
    si.self_improve(
        {"action": "apply", "file_path": "actions/demo.py", "description": "bump it"},
        out=out,
    )
    assert out["reload_safe"] is True, "the change itself was fine"
    assert out["reload_requested"] is False, "but nobody asked for a restart"


def test_the_spoken_result_matches_what_will_actually_happen(target, monkeypatch):
    _stub_pipeline(monkeypatch, "pass")
    live = si.self_improve(
        {
            "action": "apply",
            "file_path": "actions/demo.py",
            "description": "bump it",
            "reload": True,
        },
        out={},
    )
    assert "restart myself now" in live

    target.write_text("VALUE = 1\n", encoding="utf-8")
    later = si.self_improve(
        {"action": "apply", "file_path": "actions/demo.py", "description": "bump it"},
        out={},
    )
    assert "after my next restart" in later


def test_a_rolled_back_change_reports_no_reload_signal(target, monkeypatch):
    _stub_pipeline(monkeypatch, "rolled_back")
    out: dict = {}
    si.self_improve(
        {
            "action": "apply",
            "file_path": "actions/demo.py",
            "description": "bump it",
            "reload": True,
        },
        out=out,
    )
    assert not out.get("reload_safe")
    assert not out.get("reload_requested")


def test_self_improve_never_imports_the_relaunch_machinery():
    """Rule 5, asserted rather than trusted.

    If this module ever reaches for era_relaunch, the restart has moved into
    the action layer and the "no second Era" guarantee is gone.
    """
    source = (si.REPO_ROOT / "actions" / "self_improve.py").read_text(encoding="utf-8")
    assert "era_relaunch" not in source
    assert "spawn_supervisor" not in source
    assert "sys.exit" not in source


# ── the declaration the model actually sees ─────────────────────────────────


def test_the_reload_parameter_is_declared_to_the_model():
    """Unreachable capability: without this, no voice command can set it."""
    from app.tool_declarations import TOOL_DECLARATIONS

    decl = next(t for t in TOOL_DECLARATIONS if t["name"] == "self_improve")
    spec = decl["parameters"]["properties"]["reload"]
    assert spec["type"] == "BOOLEAN"
    assert "default false" in spec["description"].lower()
    assert "reload" not in decl["parameters"].get("required", [])


# ── the live-session layer: where the restart actually happens ──────────────


@pytest.fixture
def era(tmp_path, monkeypatch):
    """A minimal Era carrying the real mixin, as tools/test_era_relaunch.py does."""
    from app.era_live import tool_dispatch

    monkeypatch.setenv("ERA_HOME", str(tmp_path))
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ERA_SUPERVISED", "1")

    started: list = []

    class _NoThread:  # the real one would os._exit the test process
        def __init__(self, *a, target=None, **k):
            self._target = target

        def start(self):
            started.append(self._target)

    monkeypatch.setattr(tool_dispatch.threading, "Thread", _NoThread)

    class _UI:
        def __init__(self):
            self.lines = []

        def write_log(self, text=""):
            self.lines.append(text)

    class _Era(tool_dispatch.ToolDispatchMixin):
        def __init__(self):
            self.ui = _UI()
            self.spoken: list[str] = []
            self._shutdown_requested = False
            self._shutdown_awaiting_confirm = False
            self._is_speaking = False

        def speak(self, text):
            self.spoken.append(text)

    import era_dispatch

    monkeypatch.setattr(era_dispatch, "current_job", lambda: None)
    e = _Era()
    e._home = tmp_path
    e.exit_threads = started  # what the process would actually leave by
    return e


OUTCOME = {"rel": "actions/demo.py", "reload_safe": True, "reload_requested": True}


def test_a_verified_edit_restarts_era_so_the_change_goes_live(era, tmp_path):
    """The happy path — the reason the whole feature exists."""
    era._reload_after_self_edit(dict(OUTCOME))
    assert (tmp_path / "era.relaunch").exists(), "the watchdog was never told to bring her back"
    assert not (tmp_path / "era.stop").exists(), "a reload must never look like a shutdown"
    assert era._shutdown_requested is True
    assert "Restarting now" in " ".join(era.spoken)


def test_a_running_community_job_defers_the_restart(era, tmp_path, monkeypatch):
    """A CM job is minutes of real work on live brand accounts."""
    import era_dispatch

    monkeypatch.setattr(
        era_dispatch, "current_job", lambda: {"brand": "delta_cuisine", "task_type": "weekly"}
    )
    era._reload_after_self_edit(dict(OUTCOME))

    assert not (tmp_path / "era.relaunch").exists(), "it restarted through a live brand job"
    assert era._shutdown_requested is False
    said = " ".join(era.spoken)
    assert "delta_cuisine" in said and "reboot Era One" in said, (
        "the user must learn why, and how to finish the job themselves"
    )


def test_a_restart_already_under_way_is_not_doubled(era, tmp_path):
    era._shutdown_requested = True
    era._reload_after_self_edit(dict(OUTCOME))
    assert not (tmp_path / "era.relaunch").exists()
    assert "already under way" in " ".join(era.spoken)


def test_era_stays_up_when_nothing_could_bring_her_back(era, tmp_path, monkeypatch):
    """Exiting unsupervised is an outage, not a reload.

    The single most important refusal here: every other one costs a delayed
    change, this one would cost the user their assistant.
    """
    from core import era_relaunch

    monkeypatch.delenv("ERA_SUPERVISED", raising=False)
    monkeypatch.setattr(era_relaunch, "is_supervised", lambda: False)
    monkeypatch.setattr(era_relaunch, "spawn_supervisor", lambda _pid: False)

    era._reload_after_self_edit(dict(OUTCOME))

    assert era._shutdown_requested is False, "she armed an exit with no way back"
    said = " ".join(era.spoken)
    assert "would not come back" in said
    assert "Restart me" in said


def test_an_unsupervised_era_that_can_spawn_a_watchdog_still_reloads(era, tmp_path, monkeypatch):
    from core import era_relaunch

    monkeypatch.delenv("ERA_SUPERVISED", raising=False)
    monkeypatch.setattr(era_relaunch, "is_supervised", lambda: False)
    spawned: list[int] = []
    monkeypatch.setattr(era_relaunch, "spawn_supervisor", lambda pid: spawned.append(pid) or True)

    era._reload_after_self_edit(dict(OUTCOME))

    assert spawned, "a watchdog must be started before exiting unsupervised"
    assert era._shutdown_requested is True
    assert "Restarting now" in " ".join(era.spoken)


def test_reload_cannot_buy_a_way_past_the_safety_critical_confirmation():
    """A new parameter must not become a new bypass.

    Editing the safety layer, main.py or the supervisor requires the user's
    spoken confirmation — those are exactly the files the backup/verify/
    rollback pipeline cannot protect Era from breaking. Asking for a reload in
    the same call must not soften that, and the guard must keep classifying on
    the TARGET, not on how eager the caller is.
    """
    from core.safety_guard import classify_action

    for params in (
        {"action": "apply", "file_path": "core/safety_guard.py", "description": "x"},
        {
            "action": "apply",
            "file_path": "core/safety_guard.py",
            "description": "x",
            "reload": True,
        },
    ):
        verdict = classify_action("self_improve", params)
        assert verdict["requires_confirmation"] is True, f"reload weakened the gate: {params}"
        assert verdict["risk_level"] == "HIGH"


# ── the exit itself: arming a flag is not leaving ───────────────────────────
#
# Item 24. Live on 2026-09-29 15:45 the reload DID arm the relaunch and then
# sat there: `_shutdown_requested` is read by the connect loop only between
# session attempts (main.py:1556), so a live Gemini session outlasted the
# supervisor's 25s relaunch grace and the process was force-killed —
# `exit_code 1, forced: true`. Every test above passed while that was true,
# because all of them stopped at the flag.


class _Exited(Exception):
    """os._exit, made observable instead of taking the test runner with it."""


def _run_exit(era, monkeypatch, *, speaking=(), clock_start=1000.0):
    """Run the exit thread's body under a fake clock.

    `speaking` is read one entry per 0.1s sleep, so a test can script
    "talking, pause, talking again, done". Returns the simulated seconds the
    exit took, so a test can hold it against the supervisor's budget.
    """
    import os

    from app.era_live import tool_dispatch

    assert era.exit_threads, "no exit was ever started"
    body = era.exit_threads[-1]
    clock = {"t": clock_start, "n": 0}

    def _sleep(seconds):
        clock["t"] += seconds
        clock["n"] += 1
        i = clock["n"]
        era._is_speaking = speaking[i] if i < len(speaking) else False

    monkeypatch.setattr(tool_dispatch.time, "time", lambda: clock["t"])
    monkeypatch.setattr(tool_dispatch.time, "sleep", _sleep)
    monkeypatch.setattr(os, "_exit", lambda _code: (_ for _ in ()).throw(_Exited()))
    with pytest.raises(_Exited):
        body()
    return clock["t"] - clock_start


def test_a_verified_edit_actually_leaves_the_process(era, monkeypatch):
    """THE regression for item 24: the reload must exit, not wait to be killed."""
    era._reload_after_self_edit(dict(OUTCOME))
    assert era.exit_threads, "she armed the relaunch flag and never exited"
    _run_exit(era, monkeypatch)  # raising _Exited is the assertion


def test_a_reload_leaves_the_hud_window_open(era, monkeypatch):
    """A reboot keeps the HUD — it reconnects to the new backend by itself."""
    from app.hud import browser

    closed: list[int] = []
    monkeypatch.setattr(browser, "close_hud_windows", lambda: closed.append(1) or 1)
    era._reload_after_self_edit(dict(OUTCOME))
    _run_exit(era, monkeypatch)
    assert not closed, "a reload that closes the HUD looks like a shutdown to the user"


def _pretend_a_cm_job_is_running(era, mp):
    import era_dispatch

    mp.setattr(era_dispatch, "current_job", lambda: {"brand": "delta", "task_type": "weekly"})


@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(
            lambda era, mp: setattr(era, "_shutdown_requested", True), id="already_exiting"
        ),
        pytest.param(_pretend_a_cm_job_is_running, id="cm_job_running"),
    ],
)
def test_a_refused_reload_never_starts_an_exit(era, monkeypatch, arrange):
    """A refusal that still exits is worse than no reload at all."""
    arrange(era, monkeypatch)
    era._reload_after_self_edit(dict(OUTCOME))
    assert not era.exit_threads


def test_an_unsupervised_refusal_never_starts_an_exit(era, monkeypatch):
    from core import era_relaunch

    monkeypatch.delenv("ERA_SUPERVISED", raising=False)
    monkeypatch.setattr(era_relaunch, "is_supervised", lambda: False)
    monkeypatch.setattr(era_relaunch, "spawn_supervisor", lambda _pid: False)
    era._reload_after_self_edit(dict(OUTCOME))
    assert not era.exit_threads, "exiting with nothing watching is an outage, not a reload"


def test_the_gap_between_two_utterances_is_not_the_end_of_speech(era, monkeypatch):
    """The reload speaks twice: the self-edit summary, then "restarting now".

    Reading the pause between them as silence truncates the sentence that
    explains why the assistant just vanished.
    """
    era._is_speaking = True
    era._reload_after_self_edit(dict(OUTCOME))
    # 0.5s talking, 0.4s pause, 0.5s talking, then genuinely done.
    script = [True] * 5 + [False] * 4 + [True] * 5 + [False] * 40
    took = _run_exit(era, monkeypatch, speaking=script)
    assert took > 1.4, f"exited after {took:.1f}s — inside the gap, mid-sentence"


def test_a_lagging_era_still_exits_inside_the_supervisors_grace(era, monkeypatch):
    """Past ERA_RELAUNCH_GRACE the supervisor kills her, and the reload is a
    force-kill again. Every wait in the exit path must fit inside that budget,
    so this pins the cap against the watchdog's own constant rather than a
    number copied into the test."""
    import era_supervisor

    era._is_speaking = True  # never stops talking
    era._reload_after_self_edit(dict(OUTCOME))
    took = _run_exit(era, monkeypatch, speaking=[True] * 10_000)
    assert took < era_supervisor.RELAUNCH_GRACE_S, (
        f"the exit can take {took:.0f}s against a {era_supervisor.RELAUNCH_GRACE_S:.0f}s grace"
    )


def test_the_process_has_exactly_one_door_out(era):
    """Two copies of an exit sequence drift until one forgets a wait — which is
    precisely how the reload path came to have no exit at all."""
    from pathlib import Path

    from app.era_live import tool_dispatch

    source = Path(tool_dispatch.__file__).read_text(encoding="utf-8")
    assert source.count("os._exit(") == 1, "the exit sequence has been copied"
    assert source.count("def _exit_after_speaking") == 1


def test_reboot_and_self_edit_share_one_relaunch_sequence(era, tmp_path):
    """Two copies of a restart sequence drift until one forgets a check.

    Both paths must end in the same place: era.relaunch written, era.stop
    absent, and the exit armed.
    """
    era._shutdown_era({"relaunch": True})
    assert (tmp_path / "era.relaunch").exists()
    (tmp_path / "era.relaunch").unlink()

    era._shutdown_requested = False
    era._reload_after_self_edit(dict(OUTCOME))
    assert (tmp_path / "era.relaunch").exists()
    assert not (tmp_path / "era.stop").exists()
