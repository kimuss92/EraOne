"""actions/protocol_one.py — Protocol One, Era's project health guardian.

Runs ONLY on the user's explicit command "Launch Protocol One" (the dispatcher
checks the user's own words, not the model's decision — see tool_dispatch).

One maintenance cycle:
  1. MAP       read what the spies already found (core/hive.py), refresh them.
  2. DIAGNOSE  syntax, doctor, 24h error log with evidence, correctness lint,
               security, secrets, full test suite. Evidence comes from tools,
               never from a model (actions/self_diagnosis, core/protocol_checks).
  3. PRIORITIZE critical -> high -> medium -> low.
  4. FIX, in three tiers — the line between them is the whole safety design:
       A. autofix  whitespace / import order via ruff. Backup, compile, verify,
                   roll back on any failure.
       B. model    real bugs with a precise location, through self_improve's
                   backup -> generate -> py_compile+ruff+tests -> rollback
                   pipeline, using the strongest coding model the daily budget
                   allows (Claude Opus via era.deep, else Gemini).
       C. review   everything else is REPORTED, never touched: safety-layer
                   files, files over 200 KB, security design decisions, leaked
                   credentials (rotating a key is yours to do), test failures.
     A fix counts as done only after a re-check shows the finding is gone.
  5. REPORT    markdown report, hive status + message to Protocol Zero, a
               session block in today's vault day-log, and a short spoken summary.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from core import hive
from core import protocol_checks as checks
from core.protocol_checks import Finding
from core.utils import log

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "runtime" / "protocol_one" / "reports"
BACKUP_ROOT = REPO_ROOT / "runtime" / "protocol_one" / "backups"
# The fixes a cycle is confident it can apply but did not auto-apply this run are
# staged here, so the user can authorise them all with a single "yes" instead of
# re-running the whole ~12-minute diagnosis. Cleared at the start of every launch
# and after a confirm, and ignored once older than the TTL (a stale "yes" much
# later must not apply fixes whose lines have since drifted — see the stale-lines
# rule; every staged fix is also re-verified fresh before it is applied).
PENDING_FIXES_PATH = REPO_ROOT / "runtime" / "protocol_one" / "pending_fixes.json"
# What the previous cycle found and measured, so the next report can say what is
# NEW, what is RESOLVED, and whether the test count moved — instead of leaving
# the reader to diff two long files by eye.
LAST_CYCLE_PATH = REPO_ROOT / "runtime" / "protocol_one" / "last_cycle.json"
_PENDING_TTL_S = 3600  # one hour
_REPORT_NAME = re.compile(r"^protocol_one_(\d{8})_(\d{6})\.md$")
_MAX_LISTED = 60  # per report section; anything beyond is COUNTED, never silently dropped
_MAX_MODEL_FIXES = 3
_MAX_STAGED_FIXES = 12  # cap what one "yes" can apply in a single confirm pass
_DEEP_MODEL_MAX_BYTES = 80_000  # larger files exceed a safe CLI output length
_DEEP_ESTIMATE_USD = 0.75

_RUN_LOCK = threading.Lock()
_last_model_used = {"name": ""}

_PERSONA = (
    "You are a principal software engineer doing production maintenance on Era One, "
    "a Windows Python voice assistant (asyncio + Gemini Live + PyQt6). You fix exactly "
    "the defect described, with the smallest correct change, preserving every comment, "
    "public signature, safety check and confirmation gate. You output only the complete "
    "updated file as raw Python."
)


# ── 1-2. map + diagnose ──────────────────────────────────────────────────────
def diagnose(run_tests: bool = True, player=None) -> tuple[list[Finding], dict]:
    from actions import self_diagnosis as sd
    from core import protocol_spies

    meta: dict = {}
    net = protocol_spies.network()
    if net is not None:
        net.tick()  # make the spies' map current before reading it
    # The hive map: what the spies saw plus Protocol Zero's prompt-drift finding.
    shared = hive.open_insights(source="spies") + hive.open_insights(source="protocol_zero")
    meta["spy_insights"] = len(shared)
    syncs = [m for m in hive.inbox("protocol_one") if m.get("kind") == "prompt_sync"]
    if syncs and isinstance(syncs[-1].get("payload"), dict):
        pl = syncs[-1]["payload"]
        meta["zero"] = (
            f"{syncs[-1]['at']} via {pl.get('trigger')}: +{len(pl.get('added') or [])} "
            f"~{len(pl.get('modified') or [])} -{len(pl.get('removed') or [])} prompt entries"
        )

    findings: list[Finding] = []
    for e in shared:
        if e["signature"].startswith("log:"):
            continue  # the 24h log phase below covers these with fuller counts
        findings.append(
            Finding(
                e["signature"],
                e["severity"],
                e["category"],
                e["title"],
                e["evidence"],
                e["location"],
            )
        )

    log("PROTOCOL1", "diagnosis: syntax + doctor + logs", player)
    for p in sd.phase_syntax():
        findings.append(
            Finding(
                f"syntax:{p.split(':')[0]}", "critical", "fatal", "Syntax error", p, p.split(":")[0]
            )
        )
    meta["doctor"], doctor = sd.phase_doctor()
    for line in doctor:
        sev = "high" if "[FAIL]" in line or "[ERROR]" in line else "low"
        findings.append(Finding(f"doctor:{line[:80]}", sev, "health", line[:200]))
    meta["logs_as_of"] = datetime.now().isoformat(timespec="seconds")
    logs, meta["suppressed"] = sd.phase_logs()
    for item in logs:
        engine = item["sig"].startswith(("engine.", "memory."))
        seen = f" (last seen {item['last_seen']})" if item.get("last_seen") else ""
        findings.append(
            Finding(
                f"log:{item['sig']}",
                "high" if engine and item["count"] >= 3 else "medium",
                "runtime_error",
                f"{item['count']}x {item['sig']}",
                f"{item['evidence']}{seen}",
                item["location"],
                "review",
            )
        )

    log("PROTOCOL1", "diagnosis: lint + security", player)
    findings += checks.check_lint()
    findings += checks.check_security()
    meta["tests"] = "skipped"
    if run_tests:
        log("PROTOCOL1", "diagnosis: full test suite (low priority)", player)
        failures, meta["tests"] = checks.check_tests()
        meta["test_stats"] = checks.last_test_stats()
        findings += failures

    # Dedupe by signature; worst severity first.
    unique = {f.signature: f for f in findings}
    ordered = sorted(
        unique.values(), key=lambda f: (bool(f.accepted), hive.severity_rank(f.severity))
    )
    return ordered, meta


# ── 4. fix ───────────────────────────────────────────────────────────────────
def _file_of(f: Finding) -> str:
    return f.location.split(":")[0] if f.location else ""


def _eligible(rel: str) -> tuple[Path | None, str]:
    """self_improve's own gate: protected paths, size, repo containment —
    plus the safety-critical set, which Protocol One never edits unattended."""
    from actions.self_improve import SAFETY_CRITICAL, _resolve_target

    if not rel:
        return None, "no file location"
    if rel in SAFETY_CRITICAL:
        return None, "safety-critical file — needs your confirmation via self_improve"
    target, err = _resolve_target(rel)
    return target, err


def _backup(target: Path, stamp: str) -> Path:
    dest = BACKUP_ROOT / stamp / target.relative_to(REPO_ROOT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dest)
    return dest


def _annotate(findings: list[Finding], rel: str, note: str) -> None:
    """Attach a reason to every autofix finding of one file, shown in the report."""
    for f in findings:
        if f.fix == "autofix" and _file_of(f) == rel and note not in f.evidence:
            f.evidence = f"{f.evidence} ({note})".strip()


def apply_autofixes(findings: list[Finding], stamp: str) -> list[str]:
    from actions.self_improve import _verify

    cmd = checks.ruff_command()
    if cmd is None:
        return []
    files = sorted({_file_of(f) for f in findings if f.fix == "autofix"})
    done = []
    for rel in files:
        target, _why = _eligible(rel)
        if target is None:
            _annotate(findings, rel, f"autofix skipped: {_why or 'not eligible'}")
            continue
        backup = _backup(target, stamp)
        subprocess.run(
            [*cmd, "check", "--fix", "--select", ",".join(checks.SAFE_AUTOFIX_RULES), str(target)],
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        failures, _ = _verify(target)
        # _verify runs the FULL default rule set; pre-existing lint elsewhere in the
        # file is not our regression, so only a syntax or test failure rolls back.
        blocking = [x for x in failures if x.startswith(("syntax", "tests"))]
        if blocking:
            shutil.copy2(backup, target)
            # Say so. A rollback used to be invisible: the report read "None
            # applied" and the finding stayed open with no hint that a fix had
            # been tried and what stopped it (here: the file's own tests were red).
            _annotate(findings, rel, f"autofix tried and rolled back: {blocking[0][:200]}")
            continue
        # Claim only what a re-lint proves. Ruff refuses some "safe" rules as
        # unsafe in context (trailing whitespace inside a JS string literal), and
        # the first version reported those files as fixed on every cycle.
        remaining, err = checks._ruff_json(list(checks.SAFE_AUTOFIX_RULES), [str(target)])
        left = {(d.get("code"), d.get("message")) for d in remaining}
        mine = [f for f in findings if f.fix == "autofix" and _file_of(f) == rel]
        fixed_any = False
        for f in mine:
            if err or (f.title.split(" ", 1)[0], f.evidence) in left:
                f.fix = "review"
                f.evidence = f"{f.evidence} (ruff will not fix this safely here)"
            else:
                fixed_any = True
        if fixed_any:
            done.append(rel)
    return done


def _deep_generate(target: Path, original: str, instruction: str) -> str:
    """Strongest affordable model. Falls back to Gemini, and says which ran."""
    from actions.self_improve import _generate_updated_code, _rel, _strip_fences

    if len(original.encode("utf-8")) <= _DEEP_MODEL_MAX_BYTES:
        try:
            from core.llm.adapters.claude_cli import ClaudeCliProvider
            from core.llm.spend import from_registry
            from core.llm.types import LlmMessage, LlmRequest, PromptBlock

            ledger = from_registry()
            ok, _why = ledger.can_afford(_DEEP_ESTIMATE_USD)
            if ok:
                resp = ClaudeCliProvider().generate(
                    LlmRequest(
                        alias="era.deep",
                        system=(PromptBlock(text=_PERSONA),),
                        messages=(
                            LlmMessage.text(
                                "user",
                                f"File: {_rel(target)}\n\nDefect to fix: {instruction}\n\n"
                                f"Current code:\n{original}\n\nReturn the COMPLETE updated file.",
                            ),
                        ),
                        max_output_tokens=32000,
                        timeout_s=600,
                        request_id=f"protocol1-{int(time.time())}",
                    )
                )
                ledger.record(resp.usage.est_cost_usd, alias="era.deep")
                code = _strip_fences(resp.text)
                if code.strip():
                    _last_model_used["name"] = resp.model_id or "claude (era.deep)"
                    return code
        except Exception as e:
            log("PROTOCOL1", f"deep model unavailable, falling back: {e}")
    _last_model_used["name"] = "gemini (era.smart)"
    return _generate_updated_code(target, original, instruction)


def _still_present(f: Finding) -> bool:
    """Re-check ONE finding after a fix. Unknown = still present (never claim a fix)."""
    rel = _file_of(f)
    if f.category == "lint":
        code = f.title.split(" ", 1)[0]
        items, err = checks._ruff_json([code], [rel])
        return bool(err) or any(d.get("message") == f.evidence for d in items)
    if f.category == "security" and f.signature.startswith("security:"):
        code = f.signature.split(":")[2]
        items, err = checks._ruff_json([code], [rel])
        return bool(err) or bool(items)
    if f.category == "fatal":
        try:
            compile((REPO_ROOT / rel).read_text(encoding="utf-8-sig"), rel, "exec")
            return False
        except (SyntaxError, OSError):
            return True
    return True


def apply_model_fixes(findings: list[Finding], limit: int, player=None) -> list[dict]:
    from actions.self_improve import _apply

    results = []
    for f in findings:
        if len(results) >= limit:
            break
        if f.fix != "model" or f.accepted:
            continue
        target, why = _eligible(_file_of(f))
        if target is None:
            f.fix, f.accepted = "review", ""
            f.evidence = f"{f.evidence} (not auto-fixed: {why})".strip()
            continue
        instruction = (
            f"Fix this {f.category} finding at {f.location}: {f.title}. "
            f"Evidence: {f.evidence or 'see title'}. Change nothing unrelated."
        )
        log("PROTOCOL1", f"model fix: {f.location} — {f.title}", player)
        outcome = _apply(target, instruction, player, generate=_deep_generate)
        verified = outcome.startswith("Done") and not _still_present(f)
        if verified:
            hive.resolve_insight(f.signature, by="protocol_one", note=f.title)
        results.append(
            {
                "finding": f,
                "verified": verified,
                "model": _last_model_used["name"],
                "outcome": outcome[:400],
            }
        )
    return results


# ── 4b. stage the rest for a one-word "yes" ──────────────────────────────────
def _stageable(findings: list[Finding], applied_sigs: set[str]) -> list[Finding]:
    """Model-fixable findings a cycle did NOT auto-apply (it stops at
    _MAX_MODEL_FIXES) and that are eligible to edit — the ones a single "yes"
    can safely finish. Safety-critical, accepted, and review-only findings are
    never staged: those stay the user's call, exactly as before."""
    out: list[Finding] = []
    for f in findings:
        if f.fix != "model" or f.accepted or f.signature in applied_sigs:
            continue
        target, _why = _eligible(_file_of(f))
        if target is None:
            continue
        out.append(f)
        if len(out) >= _MAX_STAGED_FIXES:
            break
    return out


def _save_pending(staged: list[Finding], stamp: str) -> None:
    """Persist the staged batch. Best-effort — a failure here only means the
    user is offered no one-word confirm, never a broken cycle."""
    try:
        PENDING_FIXES_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not staged:
            PENDING_FIXES_PATH.unlink(missing_ok=True)
            return
        PENDING_FIXES_PATH.write_text(
            json.dumps(
                {
                    "stamp": stamp,
                    "created": time.time(),
                    "findings": [f.to_dict() for f in staged],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        log("PROTOCOL1", f"could not stage pending fixes: {e}")


def _load_pending() -> list[Finding]:
    """The staged batch, or [] if none, unreadable, or older than the TTL."""
    try:
        data = json.loads(PENDING_FIXES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if time.time() - float(data.get("created") or 0) > _PENDING_TTL_S:
        return []
    out: list[Finding] = []
    for d in data.get("findings") or []:
        try:
            out.append(Finding(**d))
        except (TypeError, ValueError):
            continue
    return out


def clear_pending() -> None:
    try:
        PENDING_FIXES_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def has_pending_fixes() -> int:
    """How many fixes are staged and still fresh (0 if none)."""
    return len(_load_pending())


def confirm_pending_fixes(player=None, speak=None) -> str:
    """Apply the staged batch on the user's single "yes" — no re-diagnosis.

    Every staged fix is re-verified against the CURRENT source first (its line
    may have drifted, or it may already be fixed), so a confirm can only act on
    a finding that is still really present. Fixes run through the same
    backup -> generate -> compile+lint+test -> rollback pipeline as a launch,
    and the same eligibility gate, so nothing safety-critical is ever touched.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        return "Protocol One is busy with a cycle right now — try 'yes' again once it reports."
    started = datetime.now()
    try:
        pending = _load_pending()
        if not pending:
            return (
                "There are no fixes waiting for confirmation. Say 'Launch Protocol One' "
                "to run a fresh cycle first."
            )
        # Re-verify against current source; a finding already gone (fixed by hand,
        # or by a previous confirm) is dropped, never re-applied on a stale line.
        present = [f for f in pending if _still_present(f)]
        gone = len(pending) - len(present)
        if not present:
            clear_pending()
            return (
                f"Those {len(pending)} fix(es) are no longer needed — a re-check shows they "
                "are already resolved. Nothing to apply."
            )
        log("PROTOCOL1", f"confirm: applying {len(present)} staged fix(es)", player)
        results = apply_model_fixes(present, limit=len(present), player=player)
        clear_pending()
        verified = sum(1 for r in results if r["verified"])
        rolled = len(results) - verified

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = f"{started:%Y%m%d_%H%M%S}"
        report_path = REPORT_DIR / f"protocol_one_confirm_{stamp}.md"
        report_path.write_text(_render_confirm(results, gone, started), encoding="utf-8")

        summary = (
            f"Applied your confirmed fixes: {verified} verified"
            + (f", {rolled} rolled back" if rolled else "")
            + (f", {gone} already resolved" if gone else "")
            + f". Report: {report_path.name}."
        )
        if verified:
            summary += " Code changes take effect after my next restart."
        hive.set_status("protocol_one", "idle", summary, last_report=str(report_path))
        return summary
    except Exception as e:
        return f"Confirming the staged fixes hit an error: {type(e).__name__}: {e}"
    finally:
        _RUN_LOCK.release()


# ── 5. report ────────────────────────────────────────────────────────────────
def _render_confirm(model_results, already_gone: int, started) -> str:
    lines = [
        f"# Protocol One — confirmed fixes {started:%Y-%m-%d %H:%M}",
        "",
        "## Applied on your confirmation",
    ]
    for r in model_results:
        f = r["finding"]
        mark = "✅ verified" if r["verified"] else "↩ not applied / rolled back"
        lines.append(f"- {mark} — `{f.location}` {f.title} — model: {r['model']}")
        lines.append(f"  - {r['outcome']}")
    if not model_results:
        lines.append("- None.")
    if already_gone:
        lines += ["", f"_{already_gone} staged fix(es) were already resolved on re-check._"]
    return "\n".join(lines) + "\n"


def _is_error_finding(f: Finding) -> bool:
    """A concrete detected error vs an advisory potential risk. Errors are
    outright failures (syntax, tests, runtime) or actionable lint/security
    findings; review-only advisories (security design calls, unused imports,
    health warnings) are risks to weigh, not errors."""
    if f.category in ("fatal", "tests", "runtime_error", "toolchain"):
        return True
    if f.category in ("lint", "security"):
        return f.fix != "review"
    return hive.severity_rank(f.severity) <= hive.severity_rank("high")


def _test_scope_line(meta: dict) -> str:
    s = meta.get("test_stats") or {}
    if not s:
        return "- Test scope: not measured this cycle (tests skipped or pytest produced no XML)."
    return (
        f"- Test scope: pytest's own testpaths (`tools/` + repo root): {s.get('tests', 0)} run — "
        f"{s.get('passed', 0)} passed, {s.get('failures', 0)} failed, {s.get('errors', 0)} "
        f"errors, {s.get('skipped', 0)} skipped."
    )


def _state_line(
    applied_n: int, staged: list[Finding], errors: list[Finding], risks: list[Finding]
) -> str:
    if any(f.category == "fatal" for f in errors):
        state = "BROKEN — a syntax/fatal error is present"
    elif any(hive.severity_rank(f.severity) <= hive.severity_rank("high") for f in errors):
        state = "NEEDS ATTENTION — unresolved high-severity errors"
    elif errors:
        state = "STABLE WITH ISSUES — only lower-severity errors remain"
    else:
        state = "STABLE — no unresolved errors"
    return (
        f"**State: {state}.** {applied_n} fix(es) applied, {len(staged)} staged for your "
        f"'yes', {len(errors)} error(s) open, {len(risks)} potential risk(s) to review."
    )


def _finding_line(f: Finding) -> str:
    where = f" `{f.location}`" if f.location else ""
    ev = f" — {f.evidence}" if f.evidence and f.evidence not in f.title else ""
    many = f" (x{f.count}: lines {f.location.split(':')[-1]}, {f.also_at})" if f.count > 1 else ""
    return f"- **{f.severity}** [{f.category}]{where} {f.title}{many}{ev}"


def _listed(findings: list[Finding]) -> list[str]:
    """The lines of one report section. Capped, but the cap is stated: a report
    that silently drops its 61st finding is not a report you can trust."""
    lines = [_finding_line(f) for f in findings[:_MAX_LISTED]]
    if len(findings) > _MAX_LISTED:
        lines.append(f"- … and {len(findings) - _MAX_LISTED} more not listed here.")
    return lines


def _load_last_cycle() -> dict:
    try:
        data = json.loads(LAST_CYCLE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_last_cycle(stamp: str, findings: list[Finding], meta: dict) -> None:
    """Best-effort: losing this only costs the next report its comparison."""
    try:
        LAST_CYCLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_CYCLE_PATH.write_text(
            json.dumps(
                {
                    "stamp": stamp,
                    "open": {f.signature: f.title for f in findings if not f.accepted},
                    "tests": meta.get("test_stats") or {},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        log("PROTOCOL1", f"could not save last-cycle snapshot: {e}")


def _changes_section(open_items: list[Finding], meta: dict) -> list[str]:
    """New vs resolved since the previous cycle, and whether the test count moved."""
    prev = meta.get("previous") or {}
    if not prev.get("stamp"):
        return ["- First recorded cycle: nothing earlier to compare against."]
    before = prev.get("open") or {}
    now = {f.signature: f for f in open_items}
    new = [f for sig, f in now.items() if sig not in before]
    gone = [title for sig, title in before.items() if sig not in now]
    lines = [f"- Compared with the cycle of {prev['stamp']}:"]
    lines.append(f"  - NEW since then ({len(new)}):" if new else "  - New since then: none.")
    lines += [f"    - {f.title} ({f.location})" for f in new[:15]]
    lines.append(f"  - RESOLVED since then ({len(gone)}):" if gone else "  - Resolved: none.")
    lines += [f"    - {t}" for t in gone[:15]]
    old_t, new_t = (
        (prev.get("tests") or {}).get("tests"),
        (meta.get("test_stats") or {}).get("tests"),
    )
    if old_t and new_t:
        delta = new_t - old_t
        flag = "  ⚠ FEWER tests than last cycle — confirm that was intended." if delta < 0 else ""
        lines.append(f"  - Tests run: {new_t} (previous cycle {old_t}, {delta:+d}).{flag}")
    return lines


def _render(findings, meta, autofixed, model_results, started, staged=()) -> str:
    """Deterministic report: applied fixes, staged fixes, detected errors and
    potential risks are each in their own section so nothing reads as both a
    problem and a fix. Every section is grounded in verifiable telemetry."""
    staged_sigs = {f.signature for f in staged}
    fixed = {r["finding"].signature for r in model_results if r["verified"]}
    open_items = [
        f
        for f in findings
        if not f.accepted
        and f.signature not in fixed
        and f.signature not in staged_sigs
        and not (f.fix == "autofix" and _file_of(f) in autofixed)
    ]
    errors = [f for f in open_items if _is_error_finding(f)]
    risks = [f for f in open_items if not _is_error_finding(f)]

    lines = [
        f"# Protocol One — maintenance cycle {started:%Y-%m-%d %H:%M}",
        "",
        _state_line(len(autofixed) + len(fixed), list(staged), errors, risks),
        "",
        f"- Doctor: {meta.get('doctor')}  |  Tests: {meta.get('tests')}",
        _test_scope_line(meta),
        f"- Cycle started {started:%Y-%m-%d %H:%M:%S}; the 24 h runtime-log scan ran at "
        f"{(meta.get('logs_as_of') or 'n/a').replace('T', ' ')}. Events after that moment "
        "belong to the NEXT cycle, and a repair made after it is not reflected here.",
        f"- Hive insights on the map at launch (spies + Protocol Zero): {meta.get('spy_insights')}",
        f"- Protocol Zero last prompt sync: {meta.get('zero') or 'none recorded'}",
        f"- Duration: {int((datetime.now() - started).total_seconds())}s",
        "",
        "## Applied fixes (verified this cycle)",
    ]
    for rel in autofixed:
        lines.append(f"- ✅ Formatting/import-order autofix in `{rel}` (verified, backup kept)")
    for r in model_results:
        f = r["finding"]
        mark = "✅ verified" if r["verified"] else "↩ not applied / rolled back"
        lines.append(f"- {mark} — `{f.location}` {f.title} — model: {r['model']}")
        lines.append(f"  - {r['outcome']}")
    if not (autofixed or model_results):
        lines.append("- None applied this cycle.")

    lines += ["", '## Staged fixes — say "yes" to apply them all']
    if staged:
        lines.append(
            "_Verified-location, non-safety-critical fixes I can apply on one word. "
            'A single "yes" (or "confirm") applies them now — no need to relaunch._'
        )
        for f in staged:
            lines.append(_finding_line(f))
    else:
        lines.append("- None staged.")

    lines += ["", "## Detected errors — still open"]
    lines += _listed(errors) or ["- None."]

    lines += ["", "## Potential risks — review only (not auto-fixed)"]
    lines += _listed(risks) or ["- None."]

    lines += ["", "## Since the previous cycle"]
    lines += _changes_section(open_items, meta)

    lines += ["", "## Reviewed and accepted risks"]
    lines += [f"- `{f.location}` {f.title} — {f.accepted}" for f in findings if f.accepted] or [
        "- None."
    ]
    lines += ["", "## Log noise suppressed (with reason)"]
    lines += [f"- {s}" for s in meta.get("suppressed") or ["Nothing suppressed."]]
    return "\n".join(lines) + "\n"


def launch_protocol_one(parameters: dict | None = None, player=None, speak=None) -> str:
    """Entry point (sync; the dispatcher runs it in a background thread)."""
    params = parameters or {}
    if not _RUN_LOCK.acquire(blocking=False):
        return "Protocol One is already running a cycle."
    started = datetime.now()
    stamp = f"{started:%Y%m%d_%H%M%S}"
    try:
        # A fresh diagnosis supersedes any batch staged by the last cycle.
        clear_pending()
        previous = _load_last_cycle()
        hive.set_status("protocol_one", "diagnosing", "deep diagnosis in progress")
        findings, meta = diagnose(run_tests=params.get("tests", True) is not False, player=player)
        meta["previous"] = previous

        hive.set_status("protocol_one", "fixing", f"{len(findings)} finding(s) mapped")
        autofixed = apply_autofixes(findings, stamp)
        limit = int(params.get("max_model_fixes", _MAX_MODEL_FIXES))
        model_results = apply_model_fixes(findings, limit, player)
        # Everything model-fixable that the auto pass did not reach is staged so
        # the user can finish it with a single "yes" — no second ~12-min cycle.
        applied_sigs = {r["finding"].signature for r in model_results}
        staged = _stageable(findings, applied_sigs)
        _save_pending(staged, stamp)

        for f in findings:  # publish the map for Zero, the spies and Era
            if not f.accepted and hive.severity_rank(f.severity) <= hive.severity_rank("medium"):
                hive.post_insight(
                    "protocol_one",
                    f.signature,
                    severity=f.severity,
                    category=f.category,
                    title=f.title,
                    evidence=f.evidence,
                    location=f.location,
                )
        for r in model_results:
            if r["verified"]:
                hive.resolve_insight(r["finding"].signature, by="protocol_one")
        _resolve_stale(findings, tests_ran=meta.get("tests") != "skipped")

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORT_DIR / f"protocol_one_{stamp}.md"
        report_path.write_text(
            _render(findings, meta, autofixed, model_results, started, staged), encoding="utf-8"
        )
        fixed_sigs = {r["finding"].signature for r in model_results if r["verified"]}
        if meta.get("tests") != "skipped":  # a test-less cycle must not erase the test baseline
            _save_last_cycle(
                stamp,
                [
                    f
                    for f in findings
                    if f.signature not in fixed_sigs
                    and not (f.fix == "autofix" and _file_of(f) in autofixed)
                ],
                meta,
            )

        counts = {s: 0 for s in hive.SEVERITIES}
        for f in findings:
            if not f.accepted:
                counts[f.severity] += 1
        verified = sum(1 for r in model_results if r["verified"])
        summary = (
            f"Protocol One complete. Found {counts['critical']} critical, {counts['high']} high, "
            f"{counts['medium']} medium and {counts['low']} low issue(s). "
            f"Applied {len(autofixed)} formatting fix(es) and {verified} verified code fix(es)"
            f"{f', {len(model_results) - verified} rolled back' if len(model_results) > verified else ''}. "
            f"Tests: {meta.get('tests')}. Full report: {report_path.name}."
        )
        if verified:
            summary += " Code changes take effect after my next restart."
        if staged:
            summary += (
                f" {len(staged)} more fix(es) are staged and ready — say 'yes' and I'll "
                "apply them all now, no need to relaunch."
            )

        hive.set_status("protocol_one", "idle", summary, last_report=str(report_path))
        hive.send_message(
            "protocol_one",
            "*",
            "cycle_report",
            {"report": str(report_path), "counts": counts, "verified_fixes": verified},
        )
        _log_to_vault(summary, autofixed, model_results, counts)
        return summary
    except Exception as e:
        hive.set_status("protocol_one", "failed", f"{type(e).__name__}: {e}")
        return f"Protocol One stopped with an error before finishing: {type(e).__name__}: {e}"
    finally:
        _RUN_LOCK.release()


def _resolve_stale(findings: list[Finding], tests_ran: bool) -> None:
    """A full re-diagnosis IS the re-check: what Protocol One posted last cycle and
    did not find now is gone. Without this, a fixed issue (or a false positive
    that was corrected, like the BOM "syntax error") stayed "open" forever and
    was briefed to the user as current. Test findings are only cleared by a cycle
    that actually ran the tests."""
    current = {f.signature for f in findings}
    for e in hive.open_insights(source="protocol_one"):
        sig = e["signature"]
        if sig in current or (sig.startswith("tests:") and not tests_ran):
            continue
        hive.resolve_insight(sig, by="protocol_one", note="not found on re-diagnosis")


def _log_to_vault(summary, autofixed, model_results, counts) -> None:
    """Era's brain: the day-log in the vault. Best-effort, never raises."""
    try:
        from core import daily_note

        daily_note.append_session(
            "Protocol One maintenance cycle",
            done=[summary]
            + [f"Autofix: {r}" for r in autofixed]
            + [
                f"Code fix {'verified' if r['verified'] else 'rolled back'}: "
                f"{r['finding'].location} {r['finding'].title}"
                for r in model_results
            ],
            in_progress=[f"{n} {s} issue(s) open" for s, n in counts.items() if n],
        )
    except Exception as e:
        log("PROTOCOL1", f"vault day-log skipped: {e}")


# ── 6. reading the report back ───────────────────────────────────────────────
# The report is the ground truth of a cycle. Era used to see only hive.briefing(),
# which drops any detail over 120 characters — so "what did Protocol One find?"
# could only be answered from memory or a guess. These two functions let her
# (and anyone) read the real file, any time, with an honest age.
_SECTIONS = {
    "applied": "Applied fixes",
    "staged": "Staged fixes",
    "errors": "Detected errors",
    "risks": "Potential risks",
    "changes": "Since the previous cycle",
    "accepted": "Reviewed and accepted risks",
    "noise": "Log noise suppressed",
}


def latest_report_path() -> Path | None:
    """Newest full-cycle report (a confirm report is a different, shorter file)."""
    try:
        names = [p for p in REPORT_DIR.iterdir() if _REPORT_NAME.match(p.name)]
    except OSError:
        return None
    return max(names, key=lambda p: p.name, default=None)


def _written_at(path: Path) -> datetime | None:
    m = _REPORT_NAME.match(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _age_text(then: datetime) -> str:
    secs = max(0, int((datetime.now() - then).total_seconds()))
    if secs < 90:
        return "moments"
    if secs < 5400:
        return f"{secs // 60} min"
    if secs < 172800:
        return f"{secs // 3600} h"
    return f"{secs // 86400} days"


def _changed_since(then: datetime) -> int:
    """How many source files were edited after the report was written. Every
    finding stores a file:line; any edit above it moves the line (see the
    stale-lines rule), so this is the honest 'is this still current?' signal."""
    cutoff = then.timestamp()
    count = 0
    roots = [REPO_ROOT, *[REPO_ROOT / d for d in checks._CHECK_DIRS]]
    for root in roots:
        try:
            files = root.glob("*.py") if root == REPO_ROOT else root.rglob("*.py")
            for p in files:
                if "__pycache__" in p.parts:
                    continue
                try:
                    if p.stat().st_mtime > cutoff:
                        count += 1
                except OSError:
                    continue
        except OSError:
            continue
    return count


def _section(text: str, title: str) -> str:
    """One '## <title…>' block of a report, heading included."""
    out: list[str] = []
    grabbing = False
    for line in text.splitlines():
        if line.startswith("## "):
            if grabbing:
                break
            grabbing = line[3:].startswith(title)
        if grabbing:
            out.append(line)
    return "\n".join(out)


def read_report(section: str | None = None, max_chars: int = 7000) -> str:
    """The latest cycle report (or one section of it), prefixed with its age and
    whether the code has changed since it was written."""
    path = latest_report_path()
    if path is None:
        return "No Protocol One report exists yet. The user must say 'Launch Protocol One' first."
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"Protocol One's latest report ({path.name}) could not be read: {e}"
    then = _written_at(path)
    head = f"Protocol One report {path.name}"
    if then:
        changed = _changed_since(then)
        head += f", written {then:%Y-%m-%d %H:%M} ({_age_text(then)} ago)."
        head += (
            f" {changed} source file(s) have been edited since, so file:line locations and some "
            "findings may already be fixed — re-check before acting on one."
            if changed
            else " No source file has changed since, so it is still current."
        )
    key = (section or "").strip().lower()
    if key:
        title = _SECTIONS.get(key)
        if title is None:
            return f"{head}\nUnknown section '{section}'. Available: {', '.join(_SECTIONS)}."
        body = _section(text, title) or f"(this report has no '{title}' section)"
    else:
        body = text
    if len(body) > max_chars:
        body = (
            body[:max_chars]
            + f"\n… [cut at {max_chars} characters; ask for one section: {', '.join(_SECTIONS)}]"
        )
    return f"{head}\n\n{body}"


def report_digest() -> str:
    """One spoken-friendly line about the latest report, for the status briefing."""
    path = latest_report_path()
    if path is None:
        return "Protocol One has not produced a report yet."
    try:
        state = next(
            (
                line.strip("* ").strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("**State:")
            ),
            "",
        )
    except OSError:
        state = ""
    then = _written_at(path)
    when = f", written {_age_text(then)} ago" if then else ""
    return (
        f"Latest Protocol One report: {path.name}{when}. {state} "
        "To read it, call protocol_one with action=report (optionally section: "
        f"{', '.join(_SECTIONS)})."
    ).replace("  ", " ")


def read_recent(limit: int | str | None = None) -> str:
    """What Era's last tool calls REALLY did: asked vs. ran, and the proof.

    The truth source for "did you actually do it?". Her own recollection is the
    model's reading of a tool reply; this is the runtime's record (task trace),
    including calls that were detached and finished after the turn ended.
    """
    from core.task_trace import recent_tool_calls, render_recent_calls

    try:
        count = max(1, min(25, int(limit))) if limit not in (None, "") else 8
    except (TypeError, ValueError):
        count = 8
    rows = recent_tool_calls(count)
    return (
        "Recent tool calls (oldest first). Only CONFIRMED means the effect was observed; "
        "NOT CONFIRMED and FAILED mean the request did NOT demonstrably happen - never "
        "tell the user it did. 'Era actually ran' marks a call the router swapped.\n"
        + render_recent_calls(rows)
    )


def protocol_status() -> str:
    return f"{hive.briefing()} {report_digest()}"
