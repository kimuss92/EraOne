"""actions/self_improve.py — Era cautiously fixes and improves her OWN code.

Voice/text: "fix yourself", "improve your netflix search", "add a feature to
yourself". This is the ONLY sanctioned path for modifying Era's source —
actions/code_helper.py refuses repo files and points here.

Caution model (every rule is load-bearing):
  1. PROTECTED paths (secrets, memory state, backups) are never written. Ever.
  2. SAFETY-CRITICAL files (safety layer, main.py, this file) are HIGH-risk in
     core/safety_guard.py and require the user's spoken confirmation first.
  3. Every apply takes a timestamped backup under runtime/self_improve/backups/.
  4. The new code must pass py_compile + ruff check; a matching test_*.py is run
     too. ANY failure → automatic rollback from the backup. Era never leaves
     herself broken. Failures first meet a DETERMINISTIC repair (ruff fixes what
     ruff can fix), then one model pass carrying the exact diagnostics
     (core/lint_feedback.py) — never the blind retry that burned I001/F401.
     A landed change then meets actions/self_improve_verifier.py (import check,
     every importing test, diff safety) which rolls back on its own failure.
  5. Nothing here ever EXECUTES a repo file — no main.py relaunch, no second
     Era instance, no Telegram 409. Verification is static + pytest only.
  6. Changes touch the running process only after a restart; the supervisor
     handles that. We report "restart needed", we do not restart ourselves.
"""


from __future__ import annotations

import difflib
import py_compile
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from actions.self_improve_targets import find_target_files
from core import lint_feedback
from core.lint_feedback import Finding
from core.utils import log
from core.win_subprocess import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_ROOT = REPO_ROOT / "runtime" / "self_improve" / "backups"
_VERIFY_TIMEOUT_S = 120
_MAX_FILE_BYTES = 200_000  # refuse to LLM-rewrite giant files whole
# Covering tests are found by import graph, which can select several. Verifying
# a self-edit must stay a fast verdict on THIS change, not a suite run.
_MAX_VERIFY_TEST_FILES = 4
# A repair budget, not a retry budget — an identical second round won't land it.
_MAX_MODEL_REPAIRS = 1
# Description tokens that must appear in a module's own name before a match
# counts as "I know which file you mean". See resolve_request — measured at 2,
# because every correct resolution scores 2+ and the one wrong one scored 1.
_MIN_CONFIDENT_SCORE = 2

# Never written by this tool, no matter what the model or the user asks.
_PROTECTED_GLOBS = (
    "config/*",
    ".env*",
    "memory/*.json",
    "memory/backups/*",
    "runtime/self_improve/*",
    ".claude/*",
)

# Editable only after the user confirms (enforced in core/safety_guard.py —
# keep both lists in sync). Breaking any of these bricks Era or her safety net.
SAFETY_CRITICAL = frozenset(
    {
        "main.py",
        "app/websocket_server.py",
        "core/task_scheduler.py",
        "core/task_store.py",
        "core/task_chain.py",
        "tools/test_stability_regressions.py",
        "tools/test_autopilot.py",
        "tools/test_task_scheduler_authority.py",
        "tools/test_visual_engine.py",
        "tools/test_development.py",
        "core/development.py",
        "core/source_discovery.py",
        "tools/test_autonomy_recovery.py",
        "core/visual_engine.py",
        "core/feature_engine.py",
        "tools/era_development_worker.py",
        "app/hud/api.py",
        "app/hud/schedule_control.py",
        "app/tool_declarations.py",
        "app/era_live/tool_dispatch.py",
        "core/autopilot.py",
        "core/autopilot_coding.py",
        "app/era_live/autopilot.py",
        "lane_router.py",
        "actions/claude_self_improve.py",
        "core/llm/adapters/claude_cli.py",
        "era_supervisor.py",
        "core/safety_guard.py",
        "core/input_safety_gate.py",
        "core/pending_confirmation.py",
        "core/user_prefs.py",
        "core/confirmation.py",
        "actions/self_improve.py",
        "actions/self_improve_verifier.py",
        # The autonomous healer's policy, scope guard and probation: an
        # unattended loop must never be able to widen its own permissions.
        "core/self_heal.py",
        "core/self_heal_patch.py",
        "core/self_heal_incidents.py",
        "actions/self_heal.py",
        # safety_guard imports DIRECT_ACTIONS/WINDOW_ACTIONS from desktop_ops to
        # decide which desktop_control calls need confirmation, and desktop_ops
        # also owns the path containment for every generated plan. Adding "task"
        # to DIRECT_ACTIONS, or widening _safe_roots, silently removes that gate
        # — so the file is as safety-critical as the guard that reads it.
        "actions/desktop_ops.py",
        "actions/desktop.py",
    }
)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _resolve_target(file_path: str) -> tuple[Path | None, str]:
    """Resolve a target inside the repo. Returns (path, error)."""
    if not file_path:
        return None, "Please tell me which of my files to change, sir."
    p = Path(file_path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    try:
        p = p.resolve()
        p.relative_to(REPO_ROOT)
    except (ValueError, OSError):
        return None, (
            f"{file_path} is not part of my own code — for outside files use code_helper."
        )
    if p.suffix != ".py":
        return None, "I only self-modify Python files for now — they are the ones I can verify."
    if not p.exists():
        return None, f"I don't have a file at {file_path}."
    rel = _rel(p)
    # Prefix check first: Path.match's "*" spans ONE segment, so
    # "runtime/self_improve/*" never matched a backup at <ts>/actions/x.py —
    # a patched backup would later be "restored" into live code.
    if rel.startswith(("runtime/", "memory/", "config/")) or any(
        part.startswith(".") for part in Path(rel).parts
    ):
        return None, f"{rel} is protected — I never modify backups, secrets or state."
    for pattern in _PROTECTED_GLOBS:
        if p.match(str(REPO_ROOT / pattern)) or Path(rel).match(pattern):
            return None, f"{rel} is protected — I never modify secrets or memory state."
    if p.stat().st_size > _MAX_FILE_BYTES:
        # main.py and netflix_control.py exceed this. A whole-file LLM rewrite
        # of a 200KB+ file risks silent output truncation — a file that still
        # compiles but lost half its functions. That failure mode survives
        # verification, so size is a hard refusal, not a warning.
        return None, (
            f"{rel} is too large for me to rewrite safely in one piece — "
            "point me at a smaller module, or use Claude Code for this one."
        )
    return p, ""


def is_safety_critical(file_path: str) -> bool:
    p, err = _resolve_target(file_path)
    if p is None:
        return bool(err)  # unresolvable targets are treated as critical
    return _rel(p) in SAFETY_CRITICAL


def resolve_request(file_path: str, description: str) -> tuple[Path | None, str, str]:
    """Resolve an explicit file_path OR find the file from the description.

    Returns (path, status, message). status: 'ok' | 'ambiguous' | 'none' |
    'refused'. The caller MUST NOT write unless status == 'ok' — safety_guard
    relies on that invariant to skip confirmation for non-ok outcomes.
    """
    if file_path:
        p, err = _resolve_target(file_path)
        return (p, "ok", "") if p is not None else (None, "refused", err)

    matches = find_target_files(description)
    if not matches:
        return (
            None,
            "none",
            "I couldn't work out which of my files that refers to. Name the "
            "feature more precisely (e.g. 'netflix search', 'vision policy'), "
            "or run a self-diagnosis first.",
        )
    top_score = matches[0][0]
    top = [rel for score, rel in matches if score == top_score]
    if top_score < _MIN_CONFIDENT_SCORE:
        # ONE weak word in common is not a resolution (board item 26). On
        # 2026-09-29 "Modify the HUD to dynamically add a button … open the
        # item's location in File Explorer" scored exactly 1 against
        # actions/open_app.py — the single word "open" — and that came back as a
        # confident `ok`. Era rewrote the wrong file and told the user the HUD
        # feature was done.
        #
        # The floor is measured, not chosen: across the realistic requests in
        # tools/test_self_improve_targets.py every CORRECT resolution scores 2
        # or more (two words of the description in the module's own name), and
        # the one wrong resolution scored 1. So a score of 1 is downgraded to
        # ambiguous, which reaches the user as the honest refusal from item 25
        # — naming the candidates so they can pick one — instead of a silent
        # rewrite of whatever happened to share a common word.
        listing = ", ".join(top[:6])
        return (
            None,
            "ambiguous",
            f"I'm not confident which file that means — the closest are {listing}, "
            "but only on one word in common. Tell me which one, or name the file.",
        )
    if len(top) > 1:
        listing = ", ".join(top[:6])
        return (
            None,
            "ambiguous",
            f"That feature spans several of my modules: {listing}. "
            "Tell me which one, or describe the specific behavior to change.",
        )
    p, err = _resolve_target(top[0])
    if p is None:
        return None, "refused", err
    return p, "ok", ""


def _backup(target: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_ROOT / ts / _rel(target)
    n = 0
    while dest.exists():  # two applies in one second must not share a backup
        n += 1
        dest = BACKUP_ROOT / f"{ts}-{n}" / _rel(target)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(target), str(dest))
    return dest


def _backup_stamp(backup: Path) -> str:
    """When a backup was taken. NOT backup.parent.name — backups live at
    <ts>/<rel>, so for core/x.py that yields "core", which misnamed them in
    reports AND sorted every _latest_backup candidate under one key, restoring
    an arbitrary backup instead of the newest."""
    parts = backup.relative_to(BACKUP_ROOT).parts if BACKUP_ROOT in backup.parents else ()
    return parts[0] if parts else backup.parent.name


def _latest_backup(target: Path) -> Path | None:
    rel = _rel(target)
    if not BACKUP_ROOT.is_dir():
        return None
    candidates = sorted(BACKUP_ROOT.glob(f"*/{rel}"), key=_backup_stamp)
    return candidates[-1] if candidates else None


def _strip_fences(code: str) -> str:
    """Models wrap code in markdown fences no matter how firmly you ask."""
    code = (code or "").strip()
    for fence in ("```python", "```"):
        if code.startswith(fence):
            code = code[len(fence) :]
    if code.endswith("```"):
        code = code[:-3]
    return code.strip() + "\n"


def _generate_updated_code(target: Path, original: str, instruction: str) -> str:
    from core.ai_provider import DEFAULT_SMART_MODEL, generate_text

    code = generate_text(
        f"File: {_rel(target)} (part of Era One, a Python voice assistant — "
        "asyncio + Gemini Live + PyQt6 WebEngine UI).\n\n"
        f"Requested change: {instruction}\n\n"
        f"Current code:\n{original}\n\n"
        "Return the COMPLETE updated file. Output ONLY raw Python code — no "
        "explanation, no markdown fences. Preserve all existing behavior that "
        "the change does not touch: comments, public function signatures, "
        "imports used elsewhere. Make the smallest change that fulfils the "
        "request. Never remove safety checks, confirmation gates, or backups.",
        model=DEFAULT_SMART_MODEL,
        system_instruction=(
            "You are a careful senior Python engineer editing a production "
            "codebase you must not break. You output only code."
        ),
    )
    return _strip_fences(code)


def _generate_repair(
    rel: str, current: str, instruction: str, failures: list[str], findings: list[Finding]
) -> str:
    """Re-ask the model to fix VERIFIED failures, naming each one precisely —
    prompt in core/lint_feedback.py, beside the rules it quotes."""
    from core.ai_provider import DEFAULT_SMART_MODEL, generate_text

    return _strip_fences(
        generate_text(
            lint_feedback.repair_prompt(rel, current, instruction, failures, findings),
            model=DEFAULT_SMART_MODEL,
            system_instruction=lint_feedback.REPAIR_SYSTEM_INSTRUCTION,
        )
    )


def _verify(target: Path) -> tuple[list[str], list[Finding]]:
    """Static + test verification; no failures = pass. Findings ride along so
    the repair path acts on exact rule codes, not a truncated message."""
    failures: list[str] = []
    try:
        py_compile.compile(str(target), doraise=True)
    except py_compile.PyCompileError as e:
        return [f"syntax: {e.msg}".strip()], []  # no point running more

    findings, lint_error = lint_feedback.run_ruff(target, REPO_ROOT)
    if lint_error == lint_feedback.UNAVAILABLE:
        pass  # ruff not installed — syntax check above still stands
    elif lint_error:
        failures.append(f"ruff: {lint_error}")
    elif findings:
        failures.append(f"ruff: {lint_feedback.summarize(findings)}")

    test_files = _find_test_files(target)
    if test_files:
        # Capped: this runs inside a self-edit, not a CI job, and the point is
        # a fast verdict on THIS change. The full suite is era_gate's job.
        test_files = test_files[:_MAX_VERIFY_TEST_FILES]
        label = ", ".join(t.name for t in test_files)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", *[str(t) for t in test_files], "-q", "-x"],
                capture_output=True,
                text=True,
                timeout=_VERIFY_TIMEOUT_S,
                cwd=str(REPO_ROOT),
                encoding="utf-8",
                errors="replace",
            creationflags=NO_WINDOW,
        )
            if proc.returncode != 0:
                tail = (proc.stdout or "").strip().splitlines()[-15:]
                failures.append(f"tests ({label}): " + " | ".join(tail)[:500])
        except subprocess.TimeoutExpired:
            failures.append(f"tests ({label}): timed out")
        except Exception as e:
            failures.append(f"tests: could not run ({e})")
    else:
        # Say it. A self-edit that ran no tests passed a weaker check than the
        # caller will assume, and staying quiet is how that assumption sticks.
        log("SELFIMPROVE", f"no test covers {target.name} — verified by compile + ruff only")
    return failures, findings


def _verify_and_heal(target: Path) -> tuple[list[str], list[Finding], list[str]]:
    """Verify, then let ruff mechanically repair what ruff can — the
    deterministic half of self-healing. I001 and F401 have exactly one correct
    resolution and ruff knows it, so they are settled before any model is asked.
    Empty notes means nothing was repaired: the caller's signal to stop."""
    failures, findings = _verify(target)
    if not failures or not any(f.fixable for f in findings):
        return failures, findings, []
    notes = lint_feedback.autofix(target, REPO_ROOT)
    if notes:
        failures, findings = _verify(target)
    return failures, findings, notes


def _find_test_files(target: Path) -> list[Path]:
    """Every test that actually covers `target`, best effort.

    WHY NOT JUST test_<name>.py
      Name matching only finds a test somebody happened to name after the
      file. `core/llm/claude_accounts.py` is covered by
      tools/test_claude_account_failover.py, which no naming rule would ever
      find — so the old lookup returned None and Era verified a self-edit of
      that module against NO tests at all, while still reporting the change
      verified. Silent zero-coverage is the worst outcome for a pipeline whose
      entire safety argument is "it tests itself and rolls back".

      So the import graph in runtime/manifest.json is asked first: which test
      modules can reach this one. The name convention stays as a fallback for
      when the manifest is missing or stale — losing the map must degrade
      coverage, never remove it.

    ORDER MATTERS, because the caller caps the list.
      A widely-imported module is reached by dozens of tests
      (core/panic_stop.py: 16). Selected alphabetically and then capped at
      four, the list excluded tools/test_panic_stop.py — the one test written
      for the file being edited. So results are ordered by specificity:
      name-convention match, then tests importing the target DIRECTLY, then
      the rest. Truncation then drops the least relevant, never the obvious.
    """
    found: list[Path] = []

    def _add(path: Path) -> None:
        if path.exists() and path not in found:
            found.append(path)

    # 1. The test named after the file, if someone wrote one.
    name = target.stem
    for candidate in (
        REPO_ROOT / f"test_{name}.py",
        REPO_ROOT / "tests" / f"test_{name}.py",
        REPO_ROOT / "tools" / f"test_{name}.py",
    ):
        _add(candidate)

    # 2. and 3. Direct importers, then one hop out.
    try:
        from tools import era_gate

        manifest = era_gate.load_manifest()
        rel = target.resolve().relative_to(REPO_ROOT).as_posix()
        for depth in (1, 2):
            selected, _uncovered = era_gate.select_tests([rel], manifest, max_depth=depth)
            for path in selected:
                _add(REPO_ROOT / path)
    except Exception as e:  # noqa: BLE001 - the name match above still stands
        log("SELFIMPROVE", f"import-graph test lookup unavailable ({e}); using name match")

    return found


def _find_test_file(target: Path) -> Path | None:
    """Back-compat shim: the first covering test, or None.

    Kept because callers and tests outside this module reference it.
    """
    files = _find_test_files(target)
    return files[0] if files else None


def _diff_summary(before: str, after: str, rel: str) -> str:
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
            n=1,
        )
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return f"+{added}/-{removed} lines"


def _apply(
    target: Path,
    instruction: str,
    player=None,
    generate=None,
    model_repairs: int | None = None,
    info: dict | None = None,
) -> str:
    """`generate(target, original, instruction) -> code` lets a caller choose the
    model (Protocol One uses the strongest coding model it can afford). Only the
    generation is swappable — backup, verification and rollback are not.
    `model_repairs=0` disables the whole-file repair pass: core/self_heal.py
    passes it because its patch is scope-checked and a repair would not be.
    `info`, when given, receives {"backup": Path, "verdict": status} so an
    unattended caller can refuse anything short of a clean "pass" — the text
    starts with "Done" for review/environment/skipped verdicts too."""
    info = info if info is not None else {}
    rel = _rel(target)
    original = target.read_text(encoding="utf-8")

    log("SelfImprove", f"apply start: {rel}", player)
    try:
        updated = (generate or _generate_updated_code)(target, original, instruction)
    except Exception as e:
        return f"I couldn't generate the change for {rel}: {e}"
    if not updated.strip() or updated.strip() == original.strip():
        return f"The model produced no effective change for {rel} — nothing was modified."

    backup = _backup(target)
    info["backup"] = backup
    log("SelfImprove", f"backup: {backup}", player)

    target.write_text(updated, encoding="utf-8")
    failures, findings, notes = _verify_and_heal(target)
    repairs = list(notes)

    # Still failing → one model pass carrying the exact diagnostics and the
    # remediation rule for each code — never the original prompt again.
    for _ in range(_MAX_MODEL_REPAIRS if model_repairs is None else model_repairs):
        if not failures:
            break
        current = target.read_text(encoding="utf-8")
        codes = ",".join(sorted({f.code for f in findings})) or "non-lint"
        log("SelfImprove", f"repair pass on {rel} for {codes}", player)
        try:
            repaired = _generate_repair(rel, current, instruction, failures, findings)
        except Exception as e:
            log("SelfImprove", f"repair pass failed to generate: {e}", player)
            break
        if not repaired.strip() or repaired.strip() == current.strip():
            break  # nothing new to verify — stop rather than spin
        target.write_text(repaired, encoding="utf-8")
        failures, findings, notes = _verify_and_heal(target)
        repairs += [f"one guided repair pass ({codes})", *notes]

    if failures:
        info["verdict"] = "failed"
        shutil.copy2(str(backup), str(target))  # automatic rollback
        log("SelfImprove", f"verification FAILED, rolled back {rel}", player)
        detail = "; ".join(failures)[:600]
        tried = f" I tried {'; '.join(repairs)} first." if repairs else ""
        return (
            f"I attempted the change to {rel} but verification failed, so I rolled "
            f"everything back — my code is untouched.{tried} Remaining failures: {detail}"
        )

    updated = target.read_text(encoding="utf-8")  # repairs may have moved it on
    healed = ", ".join(repairs)
    summary = _diff_summary(original, updated, rel) + (f" — {healed}" if healed else "")
    log("SelfImprove", f"apply OK: {rel} ({summary})", player)

    # Independent second pass (import, importing tests, diff safety). It rolls
    # back itself; a rolled-back result must not start with "Done" — Protocol
    # One reads that prefix as success.
    from actions import self_improve_verifier

    verdict = self_improve_verifier.verify(
        target, backup, rel, REPO_ROOT, rollback=lambda: _rollback(target, player), player=player
    )
    info["verdict"] = verdict.status
    if verdict.status in ("rolled_back", "rollback_failed"):
        return f"I applied the change to {rel}, but {verdict.summary}"

    # Only a clean "pass" may authorise an automatic reload. review /
    # environment / skipped all mean the independent second check could NOT
    # vouch for the change, and restarting into code nothing vouched for is
    # precisely the outcome this pipeline exists to prevent. The caller still
    # has to ask for a reload; this only says one would be safe.
    info["reload_safe"] = verdict.status == "pass"
    info["rel"] = rel

    # `reload_intent` is set by the caller BEFORE this runs; `reload_safe` is
    # decided just above. The message must agree with what will actually
    # happen, so it needs both.
    tail = (
        "I'll restart myself now so it takes effect."
        if info.get("reload_intent") and info["reload_safe"]
        else "The change takes effect after my next restart — say 'restart yourself' when you're ready."
    )
    return (
        f"Done, sir. I updated {rel} ({summary}); syntax, lint and tests all pass. "
        f"{verdict.summary + ' ' if verdict.summary else ''}"
        f"A backup is at {_backup_stamp(backup)}/{rel}. {tail}"
    )


def _propose(target: Path, instruction: str) -> str:
    """Read-only: describe what WOULD change, without writing anything."""
    from core.ai_provider import DEFAULT_SMART_MODEL, generate_text

    original = target.read_text(encoding="utf-8")
    try:
        return generate_text(
            f"File {_rel(target)} of Era One (Python voice assistant).\n"
            f"Requested change: {instruction}\n\nCurrent code:\n{original[:12000]}\n\n"
            "Briefly (5 sentences max) describe HOW you would implement this — "
            "which functions to touch and the risk involved. Do not output code.",
            model=DEFAULT_SMART_MODEL,
        )
    except Exception as e:
        return f"Could not draft a proposal: {e}"


def _rollback(target: Path, player=None) -> str:
    rel = _rel(target)
    backup = _latest_backup(target)
    if backup is None:
        return f"I have no self-improve backup for {rel}."
    shutil.copy2(str(backup), str(target))
    log("SelfImprove", f"rollback: {rel} <- {_backup_stamp(backup)}", player)
    return (
        f"Rolled {rel} back to the {_backup_stamp(backup)} backup. "
        "Restart me to run the restored code."
    )


def _list_backups() -> str:
    if not BACKUP_ROOT.is_dir():
        return "No self-improve backups yet."
    lines = []
    for ts_dir in sorted(BACKUP_ROOT.iterdir(), reverse=True)[:10]:
        if ts_dir.is_dir():
            files = [str(p.relative_to(ts_dir)) for p in ts_dir.rglob("*.py")]
            lines.append(f"{ts_dir.name}: {', '.join(files)}")
    return "Recent self-improve backups:\n" + "\n".join(lines) if lines else "No backups yet."


def self_improve(
    parameters: dict, response=None, player=None, session_memory=None, speak=None, out=None
) -> str:
    """Called from main.py.

    parameters:
        action      : propose | apply | rollback | find | list_backups  (default: propose)
        file_path   : Era's own file to change (optional — resolved from
                      description when omitted)
        description : What to fix/improve/add — also used to FIND the file
        generator   : optional. "claude" routes the generated code through the
                      tool-less Claude Code CLI (actions/claude_self_improve),
                      with a Gemini fallback. Omit (or "default") to use the
                      Era LLM API as before. Only affects WHERE the text comes
                      from — backup, verification and rollback are unchanged.
        reload      : optional, DEFAULT FALSE. Ask to restart so the change
                      goes live. See below — this module never restarts.

    `out`, when given, receives the structured outcome:
        {"verdict": str, "reload_safe": bool, "reload_requested": bool, "rel": str}

    WHY reload IS A SIGNAL AND NOT AN ACTION
      Rule 5 of this module: nothing here ever executes a repo file, starts a
      second Era, or exits. That is what keeps a self-edit from producing two
      live instances (and a Telegram 409). So `reload=true` sets
      `out["reload_requested"]`, and the LIVE-SESSION layer — which already
      owns the reboot sequence for "reboot Era One" — decides and acts.

    WHY IT DEFAULTS TO FALSE
      `_apply()` is also driven by automation: Protocol One calls it from
      inside a ~12-minute maintenance cycle, and core/self_heal.py calls it
      from the self-healing loop. If a reload were the default, either could
      restart Era mid-cycle. The voice path opts in; automation does not.
    """
    p = parameters or {}
    out = out if out is not None else {}
    action = (p.get("action") or "propose").lower().strip()
    file_path = (p.get("file_path") or "").strip()
    description = (p.get("description") or "").strip()
    generator = str(p.get("generator") or "").lower().strip()
    wants_reload = bool(p.get("reload", False))

    if action == "create":
        # New source is created only inside an isolated development workspace.
        # The normal engine cannot turn a tool argument into a live file write.
        import os

        if os.environ.get("ERA_DEVELOPMENT_STAGE") != str(REPO_ROOT):
            return "New own-source files must be staged through autonomous_development first."
        target = (REPO_ROOT / file_path).resolve()
        if not target.is_relative_to(REPO_ROOT) or target.suffix != ".py" or target.exists():
            return "Creation refused: require a new repository Python path."
        relative = target.relative_to(REPO_ROOT)
        if relative.parts[0] not in {"features", "app", "core", "actions", "tools"} or any(
            part.startswith(".") for part in relative.parts
        ):
            return "Creation refused: protected path."
        content = p.get("content")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content.encode()) > _MAX_FILE_BYTES
        ):
            return "Creation refused: missing or oversized source."
        compile(content, str(target), "exec")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        out.update(verdict="staged", rel=relative.as_posix(), reload_requested=False)
        return f"Created staged source {relative.as_posix()}; final verification and approval are still required."

    if action == "list_backups":
        return _list_backups()

    if action == "locate":
        from actions.self_improve_locator import locate
        return locate(description or file_path)

    if action == "find":
        matches = find_target_files(description or file_path)
        if not matches:
            return "No module of mine matches that — try different feature words."
        return "Files that match:\n" + "\n".join(f"- {rel} (score {s})" for s, rel in matches)

    target, status, message = resolve_request(file_path, description)
    if status != "ok":
        return message  # never writes on refused/ambiguous/none — see resolve_request

    resolved_note = "" if file_path else f"(I resolved that to {_rel(target)}.) "

    if action == "rollback":
        return _rollback(target, player)

    if not description:
        return "Please describe what to fix or improve, sir."

    if action == "apply":
        start = time.time()
        # Only the generation is swappable — backup, verification and rollback
        # are owned by _apply and never change with the model. "claude" routes
        # through the tool-less Claude Code CLI (actions/claude_self_improve);
        # anything else (or nothing) uses the Era LLM API as before.
        gen = None
        if generator == "claude":
            try:
                from actions.claude_self_improve import claude_generate_factory

                gen = (
                    claude_generate_factory(allow_fallback=False)
                    if p.get("allow_generator_fallback") is False
                    else claude_generate_factory()
                )
            except Exception as e:
                log("SelfImprove", f"claude generator unavailable: {e}")
                if p.get("allow_generator_fallback") is False:
                    return "Claude coding is unavailable. No file was changed and no alternate generator was used."
        out["reload_intent"] = wants_reload
        result = _apply(target, description, player, generate=gen, info=out)
        # Requested AND independently vouched for. Both halves matter: the
        # user asking is not evidence the code is sound, and the code being
        # sound is not permission to restart.
        out["reload_requested"] = wants_reload and bool(out.get("reload_safe"))
        if wants_reload and not out["reload_requested"]:
            result += (
                " I'm not restarting on my own, though — the independent check "
                f"came back '{out.get('verdict', 'unknown')}', not a clean pass."
            )
        log("SelfImprove", f"apply finished in {time.time() - start:.1f}s")
        return resolved_note + result

    # default: propose (read-only)
    return resolved_note + _propose(target, description)
