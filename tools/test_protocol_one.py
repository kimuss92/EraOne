"""Protocol One, its spies, the hive, and the de-faked self_diagnosis.

conftest.py points ERA_HIVE_DIR at a temp dir; every test here additionally
gets its own fresh hive so counts never leak between tests.
"""

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import protocol_one as p1
from actions import self_diagnosis as sd
from core import hive, protocol_spies
from core.protocol_checks import Finding


@pytest.fixture(autouse=True)
def fresh_hive(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_HIVE_DIR", str(tmp_path / "hive"))
    return tmp_path / "hive"


# ── hive ─────────────────────────────────────────────────────────────────────
class TestHive:
    def test_same_signature_is_deduplicated_not_duplicated(self):
        assert hive.post_insight("spies", "sig:a", severity="high", category="x", title="A")
        assert not hive.post_insight("spies", "sig:a", severity="high", category="x", title="A")
        items = hive.open_insights()
        assert len(items) == 1 and items[0]["count"] == 2

    def test_resolved_then_recurring_counts_as_fresh(self):
        hive.post_insight("spies", "sig:a", severity="high", category="x", title="A")
        assert hive.resolve_insight("sig:a", by="test")
        assert hive.open_insights() == []
        assert hive.post_insight("spies", "sig:a", severity="high", category="x", title="A")

    def test_open_insights_sorted_worst_first_and_filtered(self):
        hive.post_insight("spies", "l", severity="low", category="x", title="L")
        hive.post_insight("spies", "c", severity="critical", category="x", title="C")
        hive.post_insight("protocol_one", "m", severity="medium", category="x", title="M")
        assert [e["signature"] for e in hive.open_insights()] == ["c", "m", "l"]
        assert [e["signature"] for e in hive.open_insights(min_severity="medium")] == ["c", "m"]
        assert [e["signature"] for e in hive.open_insights(source="spies")] == ["c", "l"]

    def test_prune_never_drops_open_findings_first(self, monkeypatch):
        monkeypatch.setattr(hive, "_MAX_INSIGHTS", 3)
        hive.post_insight("s", "old", severity="low", category="x", title="old")
        hive.resolve_insight("old", by="t")
        for i in range(3):
            hive.post_insight("s", f"open{i}", severity="low", category="x", title="o")
        sigs = {e["signature"] for e in hive.open_insights()}
        assert sigs == {"open0", "open1", "open2"}

    def test_mailbox_direct_and_broadcast_never_echo_to_sender(self):
        hive.send_message("protocol_one", "protocol_zero", "fix_applied", {"f": 1})
        hive.send_message("protocol_one", "*", "cycle_report", "done")
        assert len(hive.inbox("protocol_zero")) == 2
        assert len(hive.inbox("spies")) == 1
        assert hive.inbox("protocol_one") == []

    def test_status_defaults_every_agent_and_briefing_mentions_issues(self):
        assert hive.get_status()["protocol_zero"]["state"] == "not_running"
        hive.post_insight(
            "s", "x", severity="critical", category="fatal", title="Boom", location="core/a.py:3"
        )
        text = hive.briefing()
        assert "1 critical" in text and "Boom" in text and "core/a.py:3" in text

    def test_corrupt_store_reads_as_empty_not_crash(self, fresh_hive):
        fresh_hive.mkdir(parents=True, exist_ok=True)
        (fresh_hive / "insights.json").write_text("{torn", encoding="utf-8")
        assert hive.open_insights() == []


# ── spies ────────────────────────────────────────────────────────────────────
class TestSpies:
    def test_source_spy_flags_syntax_error_then_resolves_it(self, tmp_path):
        pkg = tmp_path / "core"
        pkg.mkdir()
        bad = pkg / "m.py"
        bad.write_text("def f(:\n", encoding="utf-8")
        spy = protocol_spies.SourceSpy(dirs=("core",), root=tmp_path)
        spy.tick()
        crit = hive.open_insights(min_severity="critical")
        assert len(crit) == 1 and crit[0]["category"] == "fatal"

        bad.write_text("def f():\n    return 1\n", encoding="utf-8")
        spy.seen.clear()  # mtime resolution on some filesystems is coarse
        spy.tick()
        assert hive.open_insights() == []

    def test_unchanged_files_are_not_reread(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        spy = protocol_spies.SourceSpy(dirs=(), root=tmp_path)
        assert spy.tick() == 1
        assert spy.tick() == 0

    def test_secret_is_reported_but_never_copied_into_the_hive(self, tmp_path):
        key = "AIza" + "B" * 35
        (tmp_path / "leak.py").write_text(f'KEY = "{key}"\n', encoding="utf-8")
        protocol_spies.SourceSpy(dirs=(), root=tmp_path).tick()
        items = hive.open_insights(min_severity="critical")
        assert items and items[0]["category"] == "security"
        assert key not in json.dumps(items)

    def test_state_spy_flags_invalid_memory_json(self, tmp_path):
        (tmp_path / "good.json").write_text("{}", encoding="utf-8")
        (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
        protocol_spies.StateSpy(memory_dir=tmp_path).tick()
        items = hive.open_insights()
        assert [e["signature"] for e in items] == ["state:bad.json"]

    def test_log_spy_baselines_then_reads_only_new_real_errors(self, tmp_path):
        log = tmp_path / "era.log"
        old = {"level": "ERROR", "event": "old.error"}
        log.write_text(json.dumps(old) + "\n", encoding="utf-8")
        spy = protocol_spies.LogSpy(log_file=log)
        assert spy.tick() == 0  # history is self_diagnosis's job, not the spy's

        blip = {
            "level": "ERROR",
            "event": "engine.reconnect_error",
            "error_type": "OSError",
            "error": "[WinError 1231] The network location cannot be reached",
        }
        real = {
            "level": "ERROR",
            "event": "tool.execution.failed",
            "tool": "web_search",
            "error_type": "ActionError",
            "error": "ddgs returned 0 results",
        }
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(blip) + "\n" + json.dumps(real) + "\n")
        assert spy.tick() == 1
        (item,) = hive.open_insights()
        assert "web_search" in item["title"] and item["evidence"] == "ddgs returned 0 results"

    def test_network_survives_a_raising_spy_and_announces_critical_once(self):
        class Boom:
            name = "boom"

            def tick(self):
                raise RuntimeError("spy bug")

        class Crit:
            name = "crit"

            def tick(self):
                hive.post_insight("spies", "c", severity="critical", category="x", title="C")
                return 1

        seen = []
        net = protocol_spies.SpyNetwork(spies=[Boom(), Crit()], on_critical=seen.append)
        report = net.tick()
        net.tick()
        assert report["boom"].startswith("error:") and report["crit"] == 1
        assert len(seen) == 1
        assert hive.get_status()["spies"]["state"] == "watching"

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("ERA_SPIES", "0")
        assert protocol_spies.start_network() is None


# ── de-faked self_diagnosis ──────────────────────────────────────────────────
class TestDiagnosisAccuracy:
    def test_winerror_1231_is_a_network_blip_not_a_fault(self):
        rec = {
            "event": "engine.reconnect_error",
            "error": "Multiple exceptions: [WinError 1231] The network location cannot be reached",
        }
        assert sd._classify(rec["event"], "ERROR", rec, json.dumps(rec))[0] == "network_blip"

    def test_group_holding_a_real_audio_fault_stays_visible(self):
        rec = {
            "event": "engine.reconnect_error",
            "leaf_errors": [
                "ConnectionResetError: connection reset",
                "PortAudioError: There is no driver installed",
            ],
        }
        assert sd._classify(rec["event"], "ERROR", rec, json.dumps(rec)) is None

    def test_interpreter_shutdown_race_is_not_a_memory_fault(self):
        # The exact Protocol One false positive: a shutdown-time RuntimeError
        # reported as a live engine/memory fault. It must classify as benign.
        rec = {
            "event": "engine.reconnect_error",
            "error_type": "RuntimeError",
            "error": "cannot schedule new futures after interpreter shutdown",
        }
        klass, why = sd._classify(rec["event"], "ERROR", rec, json.dumps(rec))
        assert klass == "shutdown_race"
        assert "not a memory" in why or "lifecycle" in why

    def test_a_real_reconnect_error_without_the_marker_still_reports(self):
        rec = {
            "event": "engine.reconnect_error",
            "error_type": "RuntimeError",
            "error": "unexpected keyframe decode failure",
        }
        assert sd._classify(rec["event"], "ERROR", rec, json.dumps(rec)) is None

    def test_evidence_and_repo_frame_come_from_the_traceback(self):
        root = str(sd._REPO_ROOT)
        tb = (
            f'  File "{root}\\app\\era_live\\audio.py", line 491, in _play_audio\n'
            '  File "C:\\x\\site-packages\\sounddevice.py", line 2837, in _check\n'
            "sounddevice.PortAudioError: no driver [MME error 6]"
        )
        rec = {"traceback": tb, "error": "unhandled errors in a TaskGroup"}
        assert sd.repo_frame(tb) == "app/era_live/audio.py:491 in _play_audio"
        assert sd._evidence(rec).startswith("sounddevice.PortAudioError")

    def test_model_output_is_validated_against_the_evidence(self, monkeypatch):
        logs = [
            {
                "sig": "engine.loop.uncaught [ERROR]",
                "count": 2,
                "evidence": "boom",
                "location": "app/era_live/audio.py:491 in _play_audio",
                "last_seen": "",
            }
        ]
        fake = {
            "items": [
                {"sig": "invented.event [X]", "fix": "rewrite everything"},
                {
                    "sig": "engine.loop.uncaught [ERROR]",
                    "file": "tools/netflix_control.py",
                    "fix": "edit tools/netflix_control.py",
                    "confidence": "high",
                },
            ]
        }
        import core.ai_provider as ap

        monkeypatch.setattr(ap, "generate_json", lambda *a, **k: fake)
        items = sd.phase_analyze(logs, [], [])
        assert [i["sig"] for i in items] == ["engine.loop.uncaught [ERROR]"]
        assert items[0]["file"] == "app/era_live/audio.py:491 in _play_audio"
        assert "non-existent" in items[0]["validation"]

    def test_crash_and_its_reconnect_echo_count_once(self, tmp_path, monkeypatch):
        # One 1011 drop logs engine.crash AND engine.reconnect_error 0.1s later;
        # counting both turned 4 upstream drops into a DEGRADED verdict.
        now = datetime.now(UTC)
        lines = []
        for i in range(4):
            t = now - timedelta(minutes=10 - i)
            err = "1011 None. Internal error encountered."
            lines.append(
                {"ts": t.isoformat(), "level": "CRITICAL", "event": "engine.crash", "error": err}
            )
            lines.append(
                {
                    "ts": (t + timedelta(milliseconds=100)).isoformat(),
                    "level": "ERROR",
                    "event": "engine.reconnect_error",
                    "error": "unhandled errors in a TaskGroup",
                    "leaf_errors": [f"APIError: {err}"],
                }
            )
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "era.log").write_text(
            "\n".join(json.dumps(x) for x in lines), encoding="utf-8"
        )
        monkeypatch.setattr(sd, "_REPO_ROOT", tmp_path)
        real, benign = sd.phase_logs(24)
        assert [(r["sig"].split(" ")[0], r["count"]) for r in real] == [("engine.crash", 4)]
        assert any("crash_echo" in b for b in benign)
        assert sd.verdict_for([], "READY", real).startswith("HEALTHY WITH ISSUES")

    def test_verdict_is_proportionate(self):
        two = [{"sig": "engine.loop.uncaught [ERROR]", "count": 2}]
        assert sd.verdict_for([], "READY", two).startswith("HEALTHY WITH ISSUES")
        assert sd.verdict_for([], "READY", []).startswith("HEALTHY")
        assert sd.verdict_for(["a.py:1: bad"], "READY", []).startswith("BROKEN")

    def test_doctor_parser_sees_bracketed_warnings(self, monkeypatch):
        out = "  [PASS] fine\n  [WARN] Root cleanliness\n  [FAIL] Imports\n"
        monkeypatch.setattr(
            sd.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout=out, stderr="", returncode=1),
        )
        verdict, findings = sd.phase_doctor()
        assert verdict == "NOT READY" and len(findings) == 2


# ── Protocol One ─────────────────────────────────────────────────────────────
class TestProtocolOne:
    def test_safety_critical_files_are_never_auto_fixed(self):
        target, why = p1._eligible("core/safety_guard.py")
        assert target is None and "safety-critical" in why

    def test_protected_and_missing_locations_are_refused(self):
        assert p1._eligible("")[0] is None
        assert p1._eligible("config/api_keys.json")[0] is None

    def test_ineligible_model_fix_is_demoted_to_review(self, monkeypatch):
        import actions.self_improve as si

        monkeypatch.setattr(si, "_apply", lambda *a, **k: pytest.fail("must not edit"))
        f = Finding("lint:x", "high", "lint", "F821 undefined", "", "main.py:10", "model")
        accepted = Finding(
            "s",
            "high",
            "security",
            "S102",
            "",
            "actions/desktop.py:1",
            "model",
            accepted="reviewed",
        )
        assert p1.apply_model_fixes([f, accepted], limit=3) == []
        assert f.fix == "review" and "not auto-fixed" in f.evidence

    def test_a_fix_is_only_verified_when_the_recheck_passes(self, monkeypatch):
        import actions.self_improve as si

        monkeypatch.setattr(p1, "_eligible", lambda rel: (Path(rel), ""))
        monkeypatch.setattr(si, "_apply", lambda *a, **k: "Done, sir. I updated it.")
        monkeypatch.setattr(p1, "_still_present", lambda f: True)
        f = Finding("lint:y", "medium", "lint", "F811 x", "x", "actions/a.py:1", "model")
        (result,) = p1.apply_model_fixes([f], limit=3)
        assert result["verified"] is False  # "Done" from the pipeline is not proof

    def test_unknown_categories_are_never_claimed_fixed(self):
        assert p1._still_present(Finding("tests:x", "high", "tests", "Failed"))

    def test_autofix_is_only_claimed_when_the_relint_proves_it(self, monkeypatch, tmp_path):
        import actions.self_improve as si

        target = tmp_path / "js.py"
        target.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(p1, "_eligible", lambda rel: (target, ""))
        monkeypatch.setattr(p1, "_backup", lambda t, s: target)
        monkeypatch.setattr(p1.checks, "ruff_command", lambda: ["ruff"])
        monkeypatch.setattr(p1.subprocess, "run", lambda *a, **k: None)
        monkeypatch.setattr(si, "_verify", lambda t: ([], []))
        still = [{"code": "W291", "message": "Trailing whitespace"}]
        monkeypatch.setattr(p1.checks, "_ruff_json", lambda rules, paths: (still, ""))
        f = Finding(
            "lint:a",
            "low",
            "lint",
            "W291 Trailing whitespace",
            "Trailing whitespace",
            "a.py:3",
            "autofix",
        )

        assert p1.apply_autofixes([f], "stamp") == []
        assert f.fix == "review" and "will not fix" in f.evidence

    def test_status_briefing_full_of_failure_words_is_still_a_success(self):
        from core.tool_result import ToolResult

        briefing = "Open issues: 5x tool.execution.failed [ActionError]; error rate up."
        assert not ToolResult.from_any(briefing).success  # why the dict form exists
        assert ToolResult.from_any({"success": True, "message": briefing}).success

    def test_findings_absent_from_a_new_cycle_are_resolved(self):
        for sig in ("syntax:main.py", "lint:still", "tests:tools/test_x.py::t"):
            hive.post_insight("protocol_one", sig, severity="high", category="x", title=sig)
        hive.post_insight("spies", "spy:other", severity="high", category="x", title="spy")
        still = Finding("lint:still", "high", "lint", "still here")

        p1._resolve_stale([still], tests_ran=False)
        open_sigs = {e["signature"] for e in hive.open_insights()}
        # fixed one cleared; test finding kept (tests skipped); other agents untouched
        assert open_sigs == {"lint:still", "tests:tools/test_x.py::t", "spy:other"}

        p1._resolve_stale([still], tests_ran=True)
        assert {e["signature"] for e in hive.open_insights()} == {"lint:still", "spy:other"}

    def test_second_launch_while_running_is_refused(self):
        assert p1._RUN_LOCK.acquire(blocking=False)
        try:
            assert "already running" in p1.launch_protocol_one({})
        finally:
            p1._RUN_LOCK.release()

    def test_status_reads_the_hive(self):
        hive.set_status("protocol_one", "idle", "done")
        assert "Protocol One: idle" in p1.protocol_status()

    # ── staging + single-"yes" confirm ───────────────────────────────────────
    def test_stageable_skips_safety_critical_accepted_and_already_applied(self, monkeypatch):
        monkeypatch.setattr(p1, "_eligible", lambda rel: (Path(rel), ""))
        ok = Finding("lint:a", "high", "lint", "F821 x", "x", "actions/a.py:1", "model")
        applied = Finding("lint:b", "high", "lint", "F811 y", "y", "actions/b.py:1", "model")
        accepted = Finding(
            "lint:c", "high", "lint", "F821 z", "z", "actions/c.py:1", "model", accepted="reviewed"
        )
        review = Finding("lint:d", "low", "lint", "F401 w", "w", "actions/d.py:1", "review")
        staged = p1._stageable([ok, applied, accepted, review], applied_sigs={"lint:b"})
        assert [f.signature for f in staged] == ["lint:a"]

    def test_pending_fixes_roundtrip_and_ttl(self, monkeypatch, tmp_path):
        monkeypatch.setattr(p1, "PENDING_FIXES_PATH", tmp_path / "pending.json")
        f = Finding("lint:a", "high", "lint", "F821 x", "x", "actions/a.py:1", "model")
        p1._save_pending([f], "stamp")
        assert p1.has_pending_fixes() == 1
        assert [g.signature for g in p1._load_pending()] == ["lint:a"]

        # An expired batch reads as empty — a stale "yes" must not apply it.
        future = time.time() + p1._PENDING_TTL_S + 10
        monkeypatch.setattr(p1.time, "time", lambda: future)
        assert p1.has_pending_fixes() == 0

    def test_confirm_with_no_batch_is_a_safe_no_op(self, monkeypatch, tmp_path):
        monkeypatch.setattr(p1, "PENDING_FIXES_PATH", tmp_path / "pending.json")
        monkeypatch.setattr(
            p1, "apply_model_fixes", lambda *a, **k: pytest.fail("must not edit code")
        )
        msg = p1.confirm_pending_fixes()
        assert "no fixes waiting" in msg.lower()

    def test_confirm_applies_only_still_present_fixes_then_clears(self, monkeypatch, tmp_path):
        monkeypatch.setattr(p1, "PENDING_FIXES_PATH", tmp_path / "pending.json")
        monkeypatch.setattr(p1, "REPORT_DIR", tmp_path / "reports")
        here = Finding("lint:a", "high", "lint", "F821 x", "x", "actions/a.py:1", "model")
        gone = Finding("lint:b", "high", "lint", "F811 y", "y", "actions/b.py:1", "model")
        p1._save_pending([here, gone], "stamp")

        # Re-verify: only `here` is still present; `gone` was fixed by hand.
        monkeypatch.setattr(p1, "_still_present", lambda f: f.signature == "lint:a")
        seen = {}

        def fake_apply(findings, limit, player=None):
            seen["sigs"] = [f.signature for f in findings]
            return [{"finding": findings[0], "verified": True, "model": "m", "outcome": "Done"}]

        monkeypatch.setattr(p1, "apply_model_fixes", fake_apply)
        msg = p1.confirm_pending_fixes()
        assert seen["sigs"] == ["lint:a"]  # the resolved one is never re-applied
        assert "1 verified" in msg and "1 already resolved" in msg
        assert p1.has_pending_fixes() == 0  # batch cleared after confirm

    def test_report_isolates_applied_staged_errors_and_risks(self):
        error = Finding("lint:e", "high", "lint", "F821 undefined", "", "actions/e.py:1", "model")
        risk = Finding("lint:r", "low", "lint", "F401 unused", "", "actions/r.py:1", "review")
        staged = [Finding("lint:s", "medium", "lint", "F811 dup", "", "actions/s.py:1", "model")]
        applied = [
            {
                "finding": Finding("lint:v", "high", "lint", "fixed"),
                "verified": True,
                "model": "m",
                "outcome": "Done",
            }
        ]
        meta = {"doctor": "READY", "tests": "ok", "spy_insights": 0, "suppressed": []}
        report = p1._render(
            [error, risk], meta, [], applied, __import__("datetime").datetime.now(), staged
        )
        assert "## Applied fixes (verified this cycle)" in report
        assert '## Staged fixes — say "yes" to apply them all' in report
        assert "## Detected errors — still open" in report
        assert "## Potential risks — review only (not auto-fixed)" in report
        # An error is not printed as a risk, and a staged fix is in neither.
        errors_block = report.split("## Detected errors")[1].split("## Potential risks")[0]
        risks_block = report.split("## Potential risks")[1].split("## Reviewed")[0]
        assert "F821" in errors_block and "F821" not in risks_block
        assert "F401" in risks_block
        assert "lint:s" not in errors_block  # staged, not open


# ── wiring: activation gate, safety tier, registry ───────────────────────────
class TestWiring:
    def _gate(self, last, recent=()):
        from app.era_live.tool_dispatch import ToolDispatchMixin

        fake = SimpleNamespace(_last_user_text=last, _recent_user_texts=list(recent))
        fake._user_said = lambda pattern, window_s=120: ToolDispatchMixin._user_said(
            fake, pattern, window_s
        )
        return ToolDispatchMixin._protocol_one_commanded(fake)

    def test_only_the_users_own_command_launches(self):
        assert self._gate("Era, launch Protocol One.")
        assert self._gate("okay", [(time.time() - 30, "launch protocol 1")])
        assert not self._gate("check your own code")
        assert not self._gate("okay", [(time.time() - 600, "launch protocol one")])

    def test_safety_tiers(self):
        from core.safety_guard import classify_action

        launch = classify_action("protocol_one", {"action": "launch"})
        assert launch["risk_level"] == "MEDIUM" and not launch["requires_confirmation"]
        # confirm edits code too, so it is MEDIUM like launch, not a LOW status read.
        confirm = classify_action("protocol_one", {"action": "confirm"})
        assert confirm["risk_level"] == "MEDIUM" and not confirm["requires_confirmation"]
        assert classify_action("protocol_one", {"action": "status"})["risk_level"] == "LOW"

    def _confirm_gate(self, last, pending, recent=()):
        from app.era_live.tool_dispatch import ToolDispatchMixin

        fake = SimpleNamespace(_last_user_text=last, _recent_user_texts=list(recent))
        fake._user_said = lambda pattern, window_s=120: ToolDispatchMixin._user_said(
            fake, pattern, window_s
        )
        orig = p1.has_pending_fixes
        p1.has_pending_fixes = lambda: pending
        try:
            return ToolDispatchMixin._protocol_one_confirm_commanded(fake)
        finally:
            p1.has_pending_fixes = orig

    def test_confirm_needs_both_a_yes_and_a_waiting_batch(self):
        # A spoken yes AND a staged batch -> apply.
        assert self._confirm_gate("yes, go ahead", pending=2)
        # A yes but nothing staged -> refuse (stray affirmation edits nothing).
        assert not self._confirm_gate("yes, go ahead", pending=0)
        # A batch is staged but the user said nothing affirming -> refuse.
        assert not self._confirm_gate("what's the weather", pending=2)

    def test_generic_action_words_are_not_rerouted(self):
        from core.action_registry import canonicalize_tool_call

        for action in ("status", "launch", "list"):
            tool, params = canonicalize_tool_call("protocol_one", {"action": action})
            assert tool == "protocol_one" and params["action"] == action


# ── report accuracy: what the 2026-11-14 report got wrong or left unsaid ─────
class TestReportAccuracy:
    def test_identical_lint_hits_in_one_file_are_counted_not_merged_away(self, monkeypatch):
        """Two I001 blocks in one file shared a signature and the report showed ONE."""
        root = p1.checks.REPO_ROOT
        diag = [
            {
                "code": "I001",
                "message": "Import block is un-sorted",
                "location": {"row": r},
                "filename": str(root / "core" / "x.py"),
            }
            for r in (166, 145)  # deliberately out of order
        ]
        monkeypatch.setattr(p1.checks, "_ruff_json", lambda rules, paths: (diag, ""))
        found = p1.checks.check_lint()
        assert len(found) == 1
        f = found[0]
        assert f.count == 2 and f.location == "core/x.py:145" and f.also_at == "166"
        line = p1._finding_line(f)
        assert "x2" in line and "145" in line and "166" in line

    def test_a_rolled_back_autofix_says_why(self, monkeypatch, tmp_path):
        import actions.self_improve as si

        target = tmp_path / "m.py"
        target.write_text("x = 1\n", encoding="utf-8")
        backup = tmp_path / "m.bak"
        backup.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(p1, "_eligible", lambda rel: (target, ""))
        monkeypatch.setattr(p1, "_backup", lambda t, s: backup)
        monkeypatch.setattr(p1.checks, "ruff_command", lambda: ["ruff"])
        monkeypatch.setattr(p1.subprocess, "run", lambda *a, **k: None)
        monkeypatch.setattr(si, "_verify", lambda t: (["tests (test_m.py): 4 errors"], []))
        f = Finding("lint:m", "low", "lint", "I001 sort", "sort", "m.py:3", "autofix")

        assert p1.apply_autofixes([f], "stamp") == []
        assert "rolled back" in f.evidence and "4 errors" in f.evidence
        assert "rolled back" in p1._finding_line(f)

    def test_an_ineligible_autofix_says_why(self, monkeypatch):
        monkeypatch.setattr(p1, "_eligible", lambda rel: (None, "safety-critical file"))
        monkeypatch.setattr(p1.checks, "ruff_command", lambda: ["ruff"])
        f = Finding("lint:m", "low", "lint", "I001 sort", "sort", "m.py:3", "autofix")
        p1.apply_autofixes([f], "stamp")
        assert "autofix skipped: safety-critical file" in f.evidence

    def test_junit_carries_the_real_cause_not_a_truncated_one(self, tmp_path):
        xml = tmp_path / "r.xml"
        xml.write_text(
            '<testsuites><testsuite tests="3" failures="1" errors="1" skipped="0">'
            '<testcase classname="tools.test_x.TestC" name="test_a" file="tools/test_x.py">'
            '<failure message="AssertionError: listener must stay active">'
            "E   assert None is not None</failure></testcase>"
            '<testcase classname="tools.test_x" name="test_b" file="tools/test_x.py">'
            '<error message="failed on setup with AttributeError: no media_sessions">'
            "</error></testcase>"
            '<testcase classname="tools.test_x" name="test_ok" file="tools/test_x.py"/>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        found, stats = p1.checks._failures_from_junit(xml)
        assert stats == {"tests": 3, "failures": 1, "errors": 1, "skipped": 0, "passed": 1}
        by_sig = {f.signature: f for f in found}
        failed = by_sig["tests:tools/test_x.py::TestC::test_a"]
        assert failed.title == "Failed: tools/test_x.py::TestC::test_a"
        assert "listener must stay active" in failed.evidence
        errored = by_sig["tests:tools/test_x.py::test_b"]
        assert errored.title.startswith("Error:") and "media_sessions" in errored.evidence

    def test_the_suite_run_covers_the_root_tests_too(self, monkeypatch):
        """`pytest tools/` skipped the 91 root-level tests entirely."""
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(stdout="1 passed in 0.1s", returncode=0)

        monkeypatch.setattr(p1.checks.subprocess, "run", fake_run)
        p1.checks.check_tests()
        assert "tools/" not in seen["cmd"] and "tools" not in seen["cmd"]
        assert any(str(a).startswith("--junitxml=") for a in seen["cmd"])

    def test_a_section_cap_is_stated_not_silent(self):
        many = [Finding(f"l:{i}", "low", "lint", f"F401 x{i}", "", f"a.py:{i}") for i in range(65)]
        lines = p1._listed(many)
        assert len(lines) == p1._MAX_LISTED + 1
        assert "5 more not listed" in lines[-1]

    def test_changes_section_reports_new_resolved_and_a_shrinking_suite(self):
        now = [Finding("lint:new", "low", "lint", "F841 fresh", "", "a.py:1")]
        meta = {
            "previous": {
                "stamp": "20261101_120000",
                "open": {"lint:old": "F401 gone", "lint:new_not": "x"},
                "tests": {"tests": 9000},
            },
            "test_stats": {"tests": 8911},
        }
        text = "\n".join(p1._changes_section(now, meta))
        assert "NEW since then (1)" in text and "F841 fresh" in text
        assert "RESOLVED since then (2)" in text and "F401 gone" in text
        assert "-89" in text and "FEWER tests" in text

    def test_first_cycle_has_nothing_to_compare(self):
        assert "First recorded cycle" in "\n".join(p1._changes_section([], {}))

    def test_the_full_report_states_scope_timing_and_cap(self):
        meta = {
            "doctor": "READY",
            "tests": "1 failed, 5 passed",
            "test_stats": {"tests": 6, "passed": 5, "failures": 1, "errors": 0, "skipped": 0},
            "logs_as_of": "2026-11-14T17:39:10",
            "spy_insights": 0,
            "suppressed": [],
        }
        report = p1._render([], meta, [], [], datetime(2026, 11, 14, 17, 39, 6))
        assert "`tools/` + repo root" in report and "6 run — 5 passed, 1 failed" in report
        assert "24 h runtime-log scan ran at 2026-11-14 17:39:10" in report
        assert "## Since the previous cycle" in report

    # ── Era can find and read the report ─────────────────────────────────────
    def _reports(self, tmp_path, monkeypatch):
        monkeypatch.setattr(p1, "REPORT_DIR", tmp_path)
        (tmp_path / "protocol_one_20261101_090000.md").write_text(
            "# old\n\n**State: STABLE.**\n", encoding="utf-8"
        )
        (tmp_path / "protocol_one_20261114_173906.md").write_text(
            "# Protocol One — cycle\n\n**State: NEEDS ATTENTION — unresolved errors.** x\n\n"
            "## Detected errors — still open\n- **high** the real one\n\n"
            "## Potential risks — review only (not auto-fixed)\n- **low** a risk\n",
            encoding="utf-8",
        )
        # A confirm report and the extracted dump must never be mistaken for a cycle.
        (tmp_path / "protocol_one_confirm_20261115_000000.md").write_text("c", encoding="utf-8")
        (tmp_path / "protocol_one_20261115_000000_extracted.txt").write_text("e", encoding="utf-8")

    def test_the_latest_cycle_report_is_found(self, tmp_path, monkeypatch):
        self._reports(tmp_path, monkeypatch)
        assert p1.latest_report_path().name == "protocol_one_20261114_173906.md"

    def test_no_report_is_said_plainly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(p1, "REPORT_DIR", tmp_path)
        assert "No Protocol One report exists yet" in p1.read_report()

    def test_read_report_returns_one_section_with_age_and_currency(self, tmp_path, monkeypatch):
        self._reports(tmp_path, monkeypatch)
        monkeypatch.setattr(p1, "_changed_since", lambda then: 7)
        text = p1.read_report("errors")
        assert "protocol_one_20261114_173906.md" in text and "written 2026-11-14 17:39" in text
        assert "7 source file(s) have been edited since" in text
        assert "the real one" in text and "a risk" not in text

    def test_read_report_says_when_nothing_changed(self, tmp_path, monkeypatch):
        self._reports(tmp_path, monkeypatch)
        monkeypatch.setattr(p1, "_changed_since", lambda then: 0)
        assert "still current" in p1.read_report()

    def test_unknown_section_lists_the_valid_ones(self, tmp_path, monkeypatch):
        self._reports(tmp_path, monkeypatch)
        assert "Available:" in p1.read_report("nonsense")

    def test_long_reports_are_cut_with_a_pointer_to_sections(self, tmp_path, monkeypatch):
        self._reports(tmp_path, monkeypatch)
        monkeypatch.setattr(p1, "_changed_since", lambda then: 0)
        assert "ask for one section" in p1.read_report(max_chars=60)

    def test_status_now_points_at_the_report(self, tmp_path, monkeypatch):
        self._reports(tmp_path, monkeypatch)
        out = p1.protocol_status()
        assert "protocol_one_20261114_173906.md" in out and "action=report" in out
        assert "NEEDS ATTENTION" in out

    def test_report_is_a_declared_read_only_action(self):
        from app.tool_declarations import TOOL_DECLARATIONS
        from core.action_registry import canonicalize_tool_call
        from core.safety_guard import classify_action

        decl = next(d for d in TOOL_DECLARATIONS if d["name"] == "protocol_one")
        assert "action=report" in decl["description"]
        assert "section" in decl["parameters"]["properties"]
        assert canonicalize_tool_call("protocol_one", {"action": "report"})[0] == "protocol_one"
        verdict = classify_action("protocol_one", {"action": "report"})
        assert verdict["risk_level"] == "LOW" and not verdict["requires_confirmation"]
