"""core/self_heal.py — Era repairs her own code defects, unattended, with rollback.

Loop (daemon thread started from main.py, every TICK_S):
  1. PROBATION  each applied fix is watched after the next restart. If the same
                fault recurs, or a NEW fault appears in the patched file, the
                exact pre-fix backup is restored. Survive PROBATION_S of runtime
                and the fix is marked healed.
  2. SCAN       era.log → incidents Python itself located (core/self_heal_incidents).
  3. HEAL       at most one per tick: scoped find/replace patch from the model
                (core/self_heal_patch) → actions/self_improve._apply, which owns
                backup, py_compile + ruff + tests, the verifier and rollback.

What it refuses, and escalates instead (claude_error_router / the user):
  * SAFETY_CRITICAL, protected or >200 KB files (self_improve's own rules)
  * external causes: network, quota, auth, devices
  * a patch the scope guard rejects — never "repaired" by a looser pass
  * a fingerprint that already failed MAX_ATTEMPTS times
Budgets: MIN_OCCURRENCES before acting, DAILY_CAP applies a day, one attempt
per fingerprint per RETRY_AFTER_S, one fix in probation per file.

Modes (runtime/self_heal/state.json, ERA_SELF_HEAL env overrides):
  off      nothing runs
  propose  patches are written to runtime/self_heal/proposals/ only (DEFAULT)
  auto     patches are applied — only after the user switches it on, which
           core/safety_guard.py rates HIGH + confirm
Fail closed: a missing state file means propose, a corrupt one or an unknown
mode means off — deleting or mangling state.json can never turn auto ON.
Never runs under pytest unless a test drives it explicitly.

Security review 2026-09-28 (don't loosen): frames come only from structured
log data; targets are an allowlist; gate functions are never patched; the
patch guard is AST-based; only a clean verifier "pass" is kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from core import self_heal_incidents as incidents
from core import self_heal_patch
from core.utils import log

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "runtime" / "self_heal"
MODES = ("off", "propose", "auto")

FIRST_TICK_S = 240
TICK_S = 600
MIN_OCCURRENCES = 2
MIN_CAUGHT_CALLS = 3  # caught-exception-only incidents: distinct tool calls
DAILY_CAP = 3
RETRY_AFTER_S = 24 * 3600
MAX_ATTEMPTS = 2
PROBATION_S = 24 * 3600  # of RUNTIME with the fix loaded, not wall-clock

Notifier = Callable[[str], None]
_LOCK = threading.RLock()
_PROCESS_START = datetime.now(UTC)


# ── state ────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def load_state(state_dir: Path | None = None) -> dict:
    p = (state_dir or STATE_DIR) / "state.json"
    if not p.exists():
        data = {"mode": "propose"}
    else:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("fixes", {}), dict):
            data = {"mode": "off"}  # corrupt: fail closed, keep nothing from it
    data.setdefault("mode", "off")
    data.setdefault("fixes", {})  # fingerprint -> record
    data.setdefault("day", "")
    data.setdefault("applied_today", 0)
    return data


def save_state(data: dict, state_dir: Path | None = None) -> None:
    d = state_dir or STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, d / "state.json")


def mode(state: dict | None = None) -> str:
    env = os.getenv("ERA_SELF_HEAL", "").strip().lower()
    if env in ("0", "off", "false"):
        return "off"
    if env == "propose":  # env may narrow the mode, never widen it to auto
        return "propose"
    m = (state or load_state()).get("mode", "off")
    return m if m in MODES else "off"


def set_mode(new: str, state_dir: Path | None = None) -> str:
    new = (new or "").strip().lower()
    if new not in MODES:
        return f"Unknown self-heal mode '{new}' — use off, propose or auto."
    with _LOCK:
        st = load_state(state_dir)
        st["mode"] = new
        save_state(st, state_dir)
    return f"Self-healing is now {new}."


# ── policy ───────────────────────────────────────────────────────────────


# An ALLOWLIST of directories, then refusals inside it. Everything else — the
# entry point, app/ (dispatch, HUD, approvals), era_dispatch.py (the CM gates),
# conftest.py and every root script — is escalated, never patched unattended.
HEALABLE_DIRS = ("actions/", "core/", "utils/")
# Files inside those dirs that decide what counts as a fault, verify fixes,
# route tools or hold a gate. The loop must not be able to edit its own judges.
REFUSED = frozenset(
    {
        "core/logger.py",
        "core/lint_feedback.py",
        "core/ai_provider.py",
        "core/action_registry.py",
        "core/action_verifiers.py",
        "core/tool_result.py",
        "core/voice_gate.py",
        "core/speaker_recognition.py",
        "core/panic_stop.py",
        "core/memory_intent.py",
        "core/app_state_gate.py",
        "core/task_store.py",
        "core/task_scheduler.py",
        "core/claude_bridge.py",
        "core/claude_error_router.py",
        "core/root_file_guard.py",
        "core/protocol_checks.py",
        "core/protocol_spies.py",
        "actions/self_diagnosis.py",
        "actions/self_improve_targets.py",
        "actions/claude_self_improve.py",
        "actions/protocol_one.py",
        "actions/protocol_zero.py",
        "actions/page_refs.py",
        "actions/code_helper.py",
        "actions/dev_agent.py",
    }
)
# A module that imports any of these takes part in a gate somewhere.
_GATE_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+[\w.]*\b(?:safety_guard|pending_confirmation|confirmation|"
    r"input_safety_gate|user_prefs|voice_gate|speaker_recognition|panic_stop|self_improve|"
    r"self_heal)\b",
    re.MULTILINE,
)


def target_refusal(rel: str) -> str:
    """'' when self_heal may edit `rel`, else why not (→ escalate)."""
    from actions.self_improve import SAFETY_CRITICAL, _rel, _resolve_target

    p, err = _resolve_target(rel)
    if p is None:
        return err
    rel = _rel(p)
    if rel in SAFETY_CRITICAL or rel in REFUSED:
        return f"{rel} is safety-critical — only the user or Claude Code may change it"
    if Path(rel).name.startswith(("test_", "conftest")) or rel.startswith(("tools/", "tests/")):
        return f"{rel} is a test — tests are the judge, never the patient"
    if not rel.startswith(HEALABLE_DIRS):
        return f"{rel} is outside the modules I may heal on my own ({', '.join(HEALABLE_DIRS)})"
    try:
        if _GATE_IMPORT.search(p.read_text(encoding="utf-8-sig", errors="replace")):
            return f"{rel} takes part in a safety gate — escalated, not patched"
    except OSError as e:
        return f"{rel} unreadable: {e}"
    return ""


def eligibility(inc: incidents.Incident, st: dict, now: datetime | None = None) -> str:
    """'' when the incident may be healed now, else the reason it is held."""
    now = now or _now()
    if inc.uncaught == 0:
        # Only caught-exception evidence: attributed by message correlation,
        # so it takes more separate calls before a patch is written.
        if len(inc.caught_calls) < MIN_CAUGHT_CALLS:
            return f"caught in {len(inc.caught_calls)} call(s), waiting for {MIN_CAUGHT_CALLS}"
    elif inc.count < MIN_OCCURRENCES:
        return f"seen {inc.count}x, waiting for {MIN_OCCURRENCES}"
    rec = st["fixes"].get(inc.fingerprint, {})
    if rec.get("attempts", 0) >= MAX_ATTEMPTS:
        return "gave up after repeated failed fixes — escalated"
    if rec.get("status") in ("probation", "proposed"):
        return f"a fix is already {rec['status']}"
    last = rec.get("last_attempt")
    if last and (now - _dt(last)).total_seconds() < RETRY_AFTER_S:
        return "attempted in the last 24h"
    for other in st["fixes"].values():
        if other.get("rel") == inc.rel and other.get("status") == "probation":
            return f"another fix to {inc.rel} is still in probation"
    today = now.strftime("%Y-%m-%d")
    if st.get("day") == today and st.get("applied_today", 0) >= DAILY_CAP:
        return f"daily cap of {DAILY_CAP} self-heals reached"
    return target_refusal(inc.rel)


# ── heal ─────────────────────────────────────────────────────────────────


def heal(
    inc: incidents.Incident,
    state_dir: Path | None = None,
    generate_text=None,
) -> str:
    """Try to fix one incident. Every rule in eligibility() applies, however
    this was triggered — a tool call from the model is not the user's word."""
    from actions import self_improve

    with _LOCK:
        st = load_state(state_dir)
        m = mode(st)
        if m == "off":
            return "Self-healing is switched off."
        why = eligibility(inc, st)
        if why:
            return f"Not healing {inc.fingerprint}: {why}."

        target = REPO_ROOT / inc.rel
        now = _now()
        rec = st["fixes"].setdefault(inc.fingerprint, {"attempts": 0})
        rec.update(rel=inc.rel, func=inc.func, error_type=inc.error_type, last_attempt=_iso(now))
        rec["attempts"] = rec.get("attempts", 0) + 1

        patch, patched = self_heal_patch.propose(inc, target, generate_text)
        rec["diagnosis"] = patch.diagnosis
        if not patch.ok:
            rec["status"] = "rejected"
            rec["problems"] = patch.problems[:6]
            save_state(st, state_dir)
            log("SelfHeal", f"rejected {inc.fingerprint}: {'; '.join(patch.problems)}")
            return f"No safe fix for {inc.fingerprint}: {'; '.join(patch.problems[:3])}."

        if m == "propose":
            out = (state_dir or STATE_DIR) / "proposals"
            out.mkdir(parents=True, exist_ok=True)
            name = inc.fingerprint.replace("|", "__").replace("/", "_") + ".json"
            (out / name).write_text(
                json.dumps(
                    {"incident": inc.to_dict(), "diagnosis": patch.diagnosis, "edits": patch.edits},
                    indent=2,
                ),
                encoding="utf-8",
            )
            rec["status"] = "proposed"
            save_state(st, state_dir)
            return f"Proposed a fix for {inc.rel} ({inc.func}): {patch.diagnosis}"

        info: dict = {}
        result = self_improve._apply(
            target,
            f"self-heal {inc.fingerprint}",
            generate=lambda _t, _o, _i: patched,
            model_repairs=0,
            info=info,
        )
        backup, verdict = info.get("backup"), info.get("verdict")
        if verdict != "pass":
            # "review" (diff flagged a weakened gate), "environment" (a checker
            # crashed) and "skipped" all read "Done" — none is proof, and no
            # human is watching, so only a clean pass is kept.
            if (
                backup is not None
                and Path(backup).is_file()
                and verdict in ("review", "environment", "skipped")
            ):
                shutil.copy2(backup, target)
            rec["status"] = "failed_verification"
            rec["problems"] = [f"verifier: {verdict}", result[:300]]
            save_state(st, state_dir)
            return f"The fix for {inc.rel} did not pass verification cleanly and was rolled back."

        today = now.strftime("%Y-%m-%d")
        if st.get("day") != today:
            st["day"], st["applied_today"] = today, 0
        st["applied_today"] += 1
        rec.update(
            status="probation",
            applied_at=_iso(now),
            backup=str(backup),
            sha=_sha(target),
            known=sorted(i.fingerprint for i in incidents.scan() if i.rel == inc.rel),
            runtime_s=0.0,
        )
        rec.pop("problems", None)
        save_state(st, state_dir)
        log("SelfHeal", f"applied {inc.fingerprint} -> probation")
        return (
            f"I fixed {inc.error_type} in {inc.rel} ({inc.func}): {patch.diagnosis} "
            "It is verified and backed up, and takes effect after my next restart; "
            "I'll roll it back myself if the fault comes back."
        )


def _rollback(rec: dict, reason: str, state_dir: Path | None = None) -> str:
    from actions import self_improve

    # state.json is a file on disk, not proof: re-validate both paths before
    # copying anything, so a tampered record cannot become a copy primitive.
    target, err = self_improve._resolve_target(str(rec.get("rel", "")))
    try:
        backup = Path(str(rec.get("backup", ""))).resolve()
        backup.relative_to(self_improve.BACKUP_ROOT.resolve())
    except (ValueError, OSError):
        target, err = None, "backup path is outside my backup store"
    if target is None or backup.name != target.name:
        rec["status"] = "rollback_failed"
        return f"Refused to roll back {rec.get('rel')}: {err or 'backup does not match the file'}."
    if _sha(target) != rec.get("sha"):
        rec["status"] = "superseded"
        return f"{rec['rel']} changed since the fix — not rolling back over someone's edit."
    if not backup.is_file():
        rec["status"] = "rollback_failed"
        return f"The backup for {rec['rel']} is missing — could not roll back."
    rejected = (state_dir or STATE_DIR) / "rejected" / time.strftime("%Y%m%d-%H%M%S") / rec["rel"]
    rejected.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, rejected)
    shutil.copy2(backup, target)
    rec.update(status="rolled_back", rolled_back_reason=reason, rolled_back_at=_iso(_now()))
    log("SelfHeal", f"rolled back {rec['rel']}: {reason}")
    return f"I rolled back my fix to {rec['rel']} ({reason}). Restart me to load the original."


def check_probation(
    state_dir: Path | None = None, now: datetime | None = None, tick_s: float = TICK_S
) -> list[str]:
    """Judge every fix in probation. Only faults logged by a process that had
    the fix LOADED count — i.e. after this process started, and only if the
    fix was applied before it did."""
    now = now or _now()
    notes: list[str] = []
    with _LOCK:
        st = load_state(state_dir)
        for fp, rec in st["fixes"].items():
            if rec.get("status") != "probation":
                continue
            if _sha(REPO_ROOT / rec["rel"]) != rec.get("sha"):
                rec["status"] = "superseded"
                notes.append(
                    f"{rec['rel']} was edited after my self-heal fix, so I stopped "
                    "watching that fix — it is no longer on probation."
                )
                continue
            if _dt(rec["applied_at"]) >= _PROCESS_START:
                continue  # fix not loaded yet — waiting for a restart
            if incidents.occurrences_since(fp, _PROCESS_START):
                notes.append(_rollback(rec, "the same fault came back", state_dir))
                continue
            new = incidents.new_faults_in(rec["rel"], _PROCESS_START, set(rec.get("known", [])))
            if new:
                notes.append(_rollback(rec, f"new fault after the fix: {new[0]}", state_dir))
                continue
            rec["runtime_s"] = float(rec.get("runtime_s", 0)) + tick_s
            if rec["runtime_s"] >= PROBATION_S:
                rec["status"] = "healed"
                notes.append(f"My fix to {rec['rel']} held for a day of runtime — healed.")
        save_state(st, state_dir)
    return notes


# ── loop + reporting ─────────────────────────────────────────────────────


def tick(notify: Notifier | None = None) -> list[str]:
    notes = check_probation()
    st = load_state()
    if mode(st) != "off":
        for inc in incidents.scan():
            if eligibility(inc, st):
                continue
            notes.append(heal(inc))
            break  # one heal per tick: each needs its own probation
    for n in notes:
        log("SelfHeal", n)
        if notify:
            try:
                notify(n)
            except Exception:
                pass
    return notes


def start(notify: Notifier | None = None) -> threading.Thread | None:
    """Runs whatever the saved mode is: probation keeps judging applied fixes
    even when healing is off, and a later mode change needs no restart."""
    if "pytest" in sys.modules or os.getenv("ERA_SELF_HEAL", "").strip().lower() in (
        "0",
        "off",
        "false",
    ):
        return None

    # Without this, a tool that catches its own exception and returns a string
    # leaves no frames in era.log, and most real defects stay invisible here.
    # Its failure must never stop the loop below: probation rollback of fixes
    # already applied depends on the loop, not on this capture.
    try:
        from core import handled_exceptions

        if not handled_exceptions.install():
            print(
                "[era] [SELF-HEAL] no sys.monitoring slot free — caught exceptions stay invisible"
            )
    except Exception as e:
        print(f"[era] [SELF-HEAL] caught-exception capture unavailable: {e}")

    def _loop() -> None:
        time.sleep(FIRST_TICK_S)
        while True:
            try:
                tick(notify)
            except Exception as e:  # the healer must never take Era down
                print(f"[era] [SELF-HEAL] tick failed: {type(e).__name__}: {e}")
            time.sleep(TICK_S)

    t = threading.Thread(target=_loop, name="SelfHeal", daemon=True)
    t.start()
    return t


def status(state_dir: Path | None = None) -> str:
    st = load_state(state_dir)
    lines = [
        f"Self-healing mode: {mode(st)}. Applied today: {st.get('applied_today', 0)}/{DAILY_CAP}."
    ]
    for fp, rec in sorted(st["fixes"].items(), key=lambda kv: kv[1].get("last_attempt", "")):
        lines.append(f"- {fp}: {rec.get('status', '?')} (attempts {rec.get('attempts', 0)})")
    return "\n".join(lines[:15])


def scan_report() -> str:
    st = load_state()
    found = incidents.scan()
    if not found:
        return "No code-located faults in my log for the last 24 hours."
    lines = ["Code-located faults (last 24h):"]
    for inc in found[:8]:
        why = eligibility(inc, st) or "eligible"
        lines.append(
            f"- {inc.error_type} in {inc.rel}:{inc.line} ({inc.func}) x{inc.count} — {why}"
        )
    return "\n".join(lines)


def manual_rollback(fingerprint: str = "", state_dir: Path | None = None) -> str:
    with _LOCK:
        st = load_state(state_dir)
        live = [
            (fp, r)
            for fp, r in st["fixes"].items()
            if r.get("status") in ("probation", "healed") and (not fingerprint or fingerprint in fp)
        ]
        if not live:
            return "I have no applied self-heal fix to roll back."
        fp, rec = max(live, key=lambda kv: kv[1].get("applied_at", ""))
        msg = _rollback(rec, "you asked me to", state_dir)
        rec["attempts"] = MAX_ATTEMPTS  # the user rejected it: don't retry on our own
        save_state(st, state_dir)
        return msg
