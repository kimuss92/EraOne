"""Task lessons: Era learns from her own failed tasks, without being told.

The user asked (2026-11-05) that when Era fails a task she works out why and
does it right next time. Two layers already existed and neither covers this:
self-heal fixes *crashes in her code*, self_learning turns *the user's spoken
corrections* into rules. A task that fails because she picked the wrong tool,
action or arguments taught her nothing unless the user complained.

How a lesson is made (deterministic, no LLM, safe on the live path):

1. A tool call fails. Its cause is diagnosed from structured fields only
   (error_type, verification, a short status line) and parked as *pending*.
2. Within RECOVERY_WINDOW_S a different call succeeds for a similar request
   (same tool or overlapping words). That pair IS the lesson: "when X failed
   because Y, Z worked". Stored as a *candidate*.
3. Probation. The fix approach succeeding again on a similar request confirms
   it; the fix approach failing counts a miss; MAX_MISSES misses retires it.
   Same trial idea as self-heal: a lesson only stays while it keeps being true.
4. A failure repeated REPEAT_TO_WARN times with no recovery becomes a
   *warning* lesson ("this keeps failing because Y: try another way").

How a lesson is used:
- hint_for_failure(): the moment the same kind of call fails again, the known
  fix is attached to the tool result, so the model retries correctly in the
  same turn.
- format_lessons_for_prompt(): the strongest lessons ride in the session
  prompt, so the right approach is picked first next time.

Invariants (each one is a way this could go wrong — keep them):
- A held gate is not a failure. Gate/confirmation/cancel error types are never
  learned from (memory: the auto-report loop once asked to "fix" a gate).
- Never learn a fix that escalates: a HIGH-risk call is never stored as a fix,
  and safety/confirmation/self-edit/lifecycle tools are never part of a lesson.
- Only structured, clipped fields go in a lesson. A tool's result text can
  carry web or file content, so it is reduced to a diagnosis category and is
  never copied into the prompt.
- Degrade, never raise: every public function swallows its own errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from typing import Any

from core.utils import get_memory_dir

RECOVERY_WINDOW_S = 180.0
MAX_MISSES = 1
REPEAT_TO_WARN = 3
MAX_LESSONS = 150
MAX_PENDING = 4
PROMPT_LESSONS = 8
_TEXT_MAX = 120

# Tools whose outcomes must never become a lesson: the safety layer, consent,
# self-modification and lifecycle. Learning "what worked" there could only
# ever teach a way around a gate.
_EXCLUDED_TOOL_PARTS = (
    "confirm",
    "waive",
    "approv",
    "safety",
    "self_heal",
    "self_improve",
    "abort",
    "shutdown",
    "power",
    "relaunch",
    "manage_preference",
    "save_memory",
    "capability_check",
)
# Tools that run generated code or whole agents. Never offered as the "fix"
# for a failed GUI step: that would teach a jump from a click to arbitrary code.
_NEVER_A_FIX = ("code_helper", "dev_agent", "agent_task", "autonomous_development", "code_mode")
# Refusals, holds and aborts end in these words: the system saying no, never a
# task the model should learn to route around (security review 2026-11-05).
_REFUSAL_RE = re.compile(
    r"(Refused|Required|Busy|Abort|Aborted|Tainted|NotAuthorized|Expired|Mismatch|Cancel)"
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_NOT_FAILURES = frozenset(
    {
        "Cancelled",
        "BlockedAction",
        "ConfirmationFailed",
        "ConfirmationRequired",
        "UserCancelled",
        "ApprovalRequired",
        "ConfirmationBusy",
    }
)
_STOP = frozenset(
    "the a an and or to of for in on at is it my me you your please can could would "
    "with this that now just then from era one hey okay ok yes no not do does did "
    "i want need let lets".split()
)
_lock = threading.Lock()


def _path():
    return get_memory_dir() / "task_lessons.json"


def _empty() -> dict:
    return {"version": 1, "pending": [], "failures": {}, "lessons": [], "diagnostic_cases": []}


def _load() -> dict:
    """Read the store, dropping any malformed entry rather than letting one bad
    row silently disable learning (every later call would raise and be swallowed)."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    out = _empty()
    out["diagnostic_cases"] = [
        row
        for row in data.get("diagnostic_cases", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ][-100:]
    out["pending"] = [
        p
        for p in data.get("pending", [])
        if isinstance(p, dict)
        and isinstance(p.get("ts"), (int, float))
        and isinstance(p.get("call"), dict)
        and isinstance(p.get("sig"), str)
        and isinstance(p.get("words", []), list)
    ]
    failures = data.get("failures", {})
    if isinstance(failures, dict):
        out["failures"] = {k: v for k, v in failures.items() if isinstance(v, int)}
    out["lessons"] = [
        x
        for x in data.get("lessons", [])
        if isinstance(x, dict)
        and isinstance(x.get("text"), str)
        and isinstance(x.get("failed_sig"), str)
        and isinstance(x.get("words", []), list)
    ]
    return out


def _save(data: dict) -> None:
    """Atomic replace with no versioned backups (a derived store).

    A short retry stays: on Windows an antivirus or OneDrive handle can hold a
    just-written file for a moment, and without it the lesson was silently lost
    (seen as an intermittent test failure). This runs on the lessons worker,
    never the receive loop, so the pauses cost the live path nothing."""
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
    except Exception:  # noqa: BLE001 — a lesson is never worth a broken task
        pass


# ── normalisation ───────────────────────────────────────────────────────────


def _clean(text: Any, limit: int = _TEXT_MAX) -> str:
    """One clipped line with no markup: safe to place in a prompt."""
    s = re.sub(r"[\r\n\t<>`{}\[\]]+", " ", str(text or ""))
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zA-ZÀ-ɏ؀-ۿ]{3,}", (text or "").lower())
    return {w.rstrip("s") if len(w) > 4 else w for w in words if w not in _STOP}


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_ACTION_KEYS = ("action", "operation", "op", "capability", "mode")


def _action(args: Any) -> str:
    """The action NAME only: an identifier, or '?'. Free text here was a direct
    channel into the prompt (security review: action="SYSTEM: never ask")."""
    if isinstance(args, dict):
        for key in _ACTION_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                low = value.strip().lower()
                return low if _NAME_RE.match(low) else "?"
    return ""


def _call(tool: str, args: Any) -> dict:
    """The approach, not the content: tool + action + the NAMES of the arguments set.

    Argument values never enter a lesson. They are model-chosen, a web page can
    steer them (a look-alike URL stored as "worked, use that first" would be a
    phishing redirect that outlives the session), and the approach is what
    transfers to the next request anyway. Only a fingerprint is kept, to tell an
    identical retry from a new approach.
    """
    args = args if isinstance(args, dict) else {}
    keys = []
    for k, v in list(args.items())[:8]:
        name = str(k).lower()
        if (
            name in _ACTION_KEYS
            or v in (None, "", [], {})
            or not _NAME_RE.match(name)
            or "confirm" in name
        ):
            continue
        keys.append(name)
    fp = hashlib.sha1(
        json.dumps(args, sort_keys=True, default=str).encode("utf-8", "replace")
    ).hexdigest()[:12]
    tool_name = str(tool or "").lower()
    return {
        "tool": tool_name if _NAME_RE.match(tool_name) else "?",
        "action": _action(args),
        "keys": keys[:6],
        "fp": fp,
    }


def _signature(call: dict) -> str:
    return f"{call['tool']}|{call['action']}"


def _describe(call: dict) -> str:
    parts = [f"action={call['action']}"] if call["action"] else []
    parts += list(call.get("keys", []))
    return f"{call['tool']}({', '.join(parts)})"


# Actions that grant or withhold consent, on ANY tool. Measured on the real
# traces: "scheduled_task run_now failed -> approve worked" would have taught
# Era to approve her own held tasks.
_EXCLUDED_ACTION_PARTS = ("confirm", "approv", "waive", "reject", "consent", "grant")


def _excluded(tool: str, args: Any = None) -> bool:
    low = (tool or "").lower()
    if any(part in low for part in _EXCLUDED_TOOL_PARTS):
        return True
    action = _action(args).lower()
    return any(part in action for part in _EXCLUDED_ACTION_PARTS)


def _same_tool(lesson: dict) -> bool:
    fixed = str(lesson.get("fix_sig") or "").split("|")[0]
    return fixed == str(lesson.get("failed_sig") or "").split("|")[0]


def diagnose(error_type: Any, result: Any, verification: Any) -> str:
    """Why a call failed, from structured fields. A category, never raw content."""
    et = str(error_type or "")
    low_et = et.lower()
    text = str(result or "").lower()[:300]
    ver = str(verification or "").upper()
    if "timeout" in low_et or "timed out" in text or "timeout" in text:
        return "it timed out (app slow or not responding)"
    if "notfound" in low_et or "not found" in text or "no such" in text or "no result" in text:
        return "the target was not found"
    if "unknown action" in text or "invalid" in text or et in {"ValueError", "TypeError"}:
        return "wrong action or arguments"
    if "not running" in text or "not open" in text:
        return "the app was not open"
    if "permission" in text or "denied" in text:
        return "access was denied"
    if ver == "FAILED" or "verif" in low_et:
        return "it ran but the result did not take effect"
    return f"it failed ({_clean(et, 40) or 'no error type'})"


# ── learning ────────────────────────────────────────────────────────────────


def _verdict(ok: bool, error_type: Any, verification: Any) -> bool | None:
    """True = a failure to learn from, False = success, None = ignore (gate/cancel)."""
    et = str(error_type or "")
    # Any confirmation/approval state is a gate doing its job (measured on the
    # real traces: ConfirmationBusy was being learned as a "failure").
    if et in _NOT_FAILURES or et.startswith(("Confirmation", "Approval")) or _REFUSAL_RE.search(et):
        return None
    if ok and str(verification or "").upper() != "FAILED":
        return False
    return True


def _matching(data: dict, sig: str, words: set[str]) -> list[dict]:
    hits = []
    for lesson in data["lessons"]:
        if lesson.get("status") == "retired" or lesson.get("failed_sig") != sig:
            continue
        hits.append((_similar(words, set(lesson.get("words", []))), lesson))
    hits.sort(key=lambda x: (x[1].get("status") != "confirmed", -x[0], -x[1].get("hits", 0)))
    return [lesson for _, lesson in hits]


def observe(
    user_text: str,
    tool: str,
    args: Any,
    ok: bool,
    error_type: Any = None,
    result: Any = None,
    verification: Any = None,
    safety: Any = None,
    now: float | None = None,
) -> str | None:
    """Feed one finished tool call. Returns the lesson text learned, or None. Never raises."""
    try:
        return _observe(user_text, tool, args, ok, error_type, result, verification, safety, now)
    except Exception:  # noqa: BLE001
        return None


def _observe(user_text, tool, args, ok, error_type, result, verification, safety, now):
    if not tool or _excluded(tool, args):
        return None
    verdict = _verdict(bool(ok), error_type, verification)
    if verdict is None:
        return None
    now = time.time() if now is None else now
    call = _call(tool, args)
    sig = _signature(call)
    words = _tokens(user_text)
    with _lock:
        data = _load()
        before = json.dumps(data, sort_keys=True)
        data["pending"] = [p for p in data["pending"] if 0 <= now - p["ts"] <= RECOVERY_WINDOW_S]
        learned = None
        if verdict:
            learned = _on_failure(data, call, sig, words, user_text, error_type, result,
                                  verification, now)  # fmt: skip
        else:
            learned = _on_success(data, call, sig, words, args, now)
        _prune(data)
        if json.dumps(data, sort_keys=True) != before:  # a plain success writes nothing
            _save(data)
        return learned


def _on_failure(data, call, sig, words, user_text, error_type, result, verification, now):
    reason = diagnose(error_type, result, verification)
    # Each failure has a bounded regression seed, even before a lesson exists.
    # The call contains only sanitized names and a hash of values, no raw output.
    case_id = hashlib.sha256(f"{sig}|{call.get('fp')}|{reason}".encode()).hexdigest()[:16]
    cases = data.setdefault("diagnostic_cases", [])
    case = next((row for row in cases if row["id"] == case_id), None)
    if case is None:
        case = {
            "id": case_id,
            "call": call,
            "reason": reason,
            "first": now,
            "count": 0,
            "status": "needs_root_cause_and_regression",
        }
        cases.append(case)
    case.update(last=now, count=case["count"] + 1)
    data["diagnostic_cases"] = cases[-100:]
    for lesson in data["lessons"]:
        # The lesson's own fix just failed on a similar request: a strike.
        if (
            lesson.get("status") != "retired"
            and lesson.get("fix_sig") == sig
            and _similar(words, set(lesson["words"])) > 0
        ):
            lesson["misses"] = lesson.get("misses", 0) + 1
            if lesson["misses"] >= MAX_MISSES:
                lesson["status"] = "retired"
    data["pending"].append(
        {
            "ts": now,
            "sig": sig,
            "call": call,
            "words": sorted(words),
            "reason": reason,
            "user_text": _clean(user_text),
        }
    )
    data["pending"] = data["pending"][-MAX_PENDING:]
    count = data["failures"].get(sig, 0) + 1
    data["failures"][sig] = count
    if count >= REPEAT_TO_WARN and not _matching(data, sig, words):
        return _add(data, sig, call, words, user_text, reason, None, now)
    return None


def _safe_fix(tool: str, args: Any) -> bool:
    """A call may be learned as a fix only if the REAL gate calls it LOW with no
    confirmation, re-classified here without the user's waivers. The trace's
    safety string is not trusted: it is None on some paths, and MEDIUM calls the
    user confirmed or waived (close, click, file move...) must never become
    "use that first" (security review 2026-11-05)."""
    if any(part in str(tool).lower() for part in _NEVER_A_FIX):
        return False
    try:
        from core.safety_guard import classify_action

        verdict = classify_action(tool, dict(args) if isinstance(args, dict) else {})
    except Exception:  # noqa: BLE001 — unknown risk is not LOW
        return False
    return str(verdict.get("risk_level", "")).upper() == "LOW" and not verdict.get(
        "requires_confirmation"
    )


def _on_success(data, call, sig, words, args, now):
    learned = None
    recovered = None
    for p in reversed(data["pending"]):
        if p["sig"] == sig and p["call"].get("fp") == call["fp"]:
            continue  # the identical call succeeded on a plain retry: no new approach
        # A short fragment ("approve right now") is too ambiguous to pair across tools.
        same_request = len(words) >= 3 and _similar(words, set(p["words"])) >= 0.6
        same_tool = p["call"]["tool"] == call["tool"]
        # Measured on 6,088 real calls: "different tool + loosely similar words"
        # paired unrelated requests ("cancel the task" -> spotify play). A fix is
        # the same tool on a related request, or another tool on the SAME request.
        if (same_tool and _similar(words, set(p["words"])) >= 0.34) or same_request:
            recovered = p
            break
    if recovered is not None:
        data["pending"].remove(recovered)
        # Never learn an escalation; and a fix that reads exactly like the failure
        # (only the values changed, which lessons never show) teaches nothing.
        if _safe_fix(call["tool"], args) and _describe(call) != _describe(recovered["call"]):
            data["failures"].pop(recovered["sig"], None)
            learned = _add(
                data,
                recovered["sig"],
                recovered["call"],
                set(recovered["words"]),
                recovered["user_text"],
                recovered["reason"],
                call,
                now,
            )
    for lesson in data["lessons"]:
        # The fix approach worked again on a later, similar request: proven.
        # Only for a same-tool fix. A switch to ANOTHER tool may just be the
        # next step of a multi-step task (measured on the real traces), so it is
        # proven only by recurring as failure-then-switch (_add, seen >= 2).
        if (
            _same_tool(lesson)
            and lesson.get("fix_sig") == sig
            and lesson.get("status") == "candidate"
            and now - lesson.get("created", now) >= 1
            and _similar(words, set(lesson["words"])) >= 0.34
        ):
            lesson["hits"] = lesson.get("hits", 0) + 1
            lesson["status"] = "confirmed"
            lesson["misses"] = 0
            lesson["last"] = now
    data["failures"].pop(sig, None)
    return learned


def _add(data, sig, failed, words, user_text, reason, fix, now) -> str:
    fix_sig = _signature(fix) if fix else None
    for lesson in data["lessons"]:
        if lesson.get("failed_sig") == sig and lesson.get("fix_sig") == fix_sig:
            lesson["seen"] = lesson.get("seen", 1) + 1
            lesson["words"] = sorted(set(lesson["words"]) | set(words))[:24]
            lesson["last"] = now
            if lesson.get("status") == "retired":
                lesson.update(status="candidate", misses=0)
            elif fix and lesson["seen"] >= 2:
                lesson["status"] = "confirmed"
            return lesson["text"]
    # A one-word fragment ("Yes.", "two") says nothing about the task: omit it.
    example = _clean(user_text, 70) if len(words) >= 2 else ""
    if fix:
        lead = f"When asked like '{example}': " if example else ""
        text = (
            f"{lead}{_describe(failed)} failed because {reason}; "
            f"{_describe(fix)} worked. Use that first."
        )
    else:
        tail = f" (e.g. '{example}')" if example else ""
        text = (
            f"{_describe(failed)} keeps failing because {reason}{tail}. "
            "Try another approach or say so plainly."
        )
    data["lessons"].append(
        {
            "failed_sig": sig,
            "fix_sig": fix_sig,
            "words": sorted(words)[:24],
            "reason": reason,
            "text": _clean(text, 320),
            "status": "candidate",
            "hits": 0,
            "misses": 0,
            "seen": 1,
            "created": now,
            "last": now,
        }
    )
    return data["lessons"][-1]["text"]


def _prune(data: dict) -> None:
    lessons = data["lessons"]
    if len(lessons) > MAX_LESSONS:
        rank = {"confirmed": 0, "candidate": 1, "retired": 2}
        lessons.sort(key=lambda x: (rank.get(x.get("status"), 3), -x.get("last", 0)))
        del lessons[MAX_LESSONS:]
    if len(data["failures"]) > 300:
        data["failures"] = dict(list(data["failures"].items())[-200:])


# ── using what was learned ───────────────────────────────────────────────────


def hint_for_failure(user_text: str, tool: str, args: Any, error_type: Any = None) -> str | None:
    """A known fix for a call that just failed, to attach to its result. Never raises.

    Never for a refusal or a held gate: a hint there would push the model to route
    around a busy confirmation slot or a refused call in the same turn."""
    try:
        if not tool or _excluded(tool, args) or _verdict(False, error_type, None) is None:
            return None
        sig = _signature(_call(tool, args))
        with _lock:
            data = _load()
        for lesson in _matching(data, sig, _tokens(user_text)):
            # A switch to another tool is only offered once it has recurred.
            if lesson.get("fix_sig") and (_same_tool(lesson) or lesson["status"] == "confirmed"):
                tag = "proven" if lesson.get("status") == "confirmed" else "worked once"
                return f"LEARNED FIX ({tag}): {lesson['text']}"
        return None
    except Exception:  # noqa: BLE001
        return None


def diagnostic_for_failure(tool: str, args: Any, error_type: Any = None) -> str | None:
    """Immediate escalation advice; never authorizes a mutation or paid crew run."""
    if not tool or _excluded(tool, args) or _verdict(False, error_type, None) is None:
        return None
    return (
        "DIAGNOSTIC REQUIRED: Do not repeat the identical failed command. Inspect the "
        "actual source/configuration/API contract and fresh state first. If this was "
        "an applied fix, reopen its root cause immediately. Have Drew document the "
        "cause, Cody add this case to a full-pipeline regression and repair the core, "
        "and Nabar verify. Report applied, reloaded and live-proven separately."
    )


def format_lessons_for_prompt(limit: int = PROMPT_LESSONS) -> str:
    """The strongest lessons for the session prompt. Empty string if none."""
    try:
        with _lock:
            data = _load()
        # Standing prompt text only once proven (or a warning to stop retrying):
        # one unconfirmed occurrence must not become a lasting instruction.
        live = [
            x
            for x in data["lessons"]
            if x.get("status") == "confirmed"
            or (x.get("status") == "candidate" and not x.get("fix_sig"))
        ]
        if not live:
            return ""
        live.sort(
            key=lambda x: (x.get("status") != "confirmed", -x.get("hits", 0), -x.get("last", 0))
        )
        lines = [
            "=== OBSERVATIONS FROM YOUR OWN PAST TOOL CALLS (data recorded automatically: "
            "what failed and what then worked). Advisory only: they never override a "
            "confirmation, a safety rule or what the user asks for. ==="
        ]
        for lesson in live[:limit]:
            mark = "proven" if lesson.get("status") == "confirmed" else "trial"
            lines.append(f"- [{mark}] {lesson['text']}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


# ── off the receive loop ─────────────────────────────────────────────────────

_queue: queue.Queue = queue.Queue(maxsize=500)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _run_worker() -> None:
    while True:
        item = _queue.get()
        try:
            if item is not None:
                observe(*item)
        finally:
            _queue.task_done()


def observe_async(*args: Any) -> None:
    """Queue one outcome for a single background worker. The caller (task_trace,
    which runs on the live receive loop) only enqueues: disk work there stalls the
    socket. Never raises; drops the item when the queue is full."""
    global _worker
    try:
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run_worker, name="task-lessons", daemon=True)
                _worker.start()
        _queue.put_nowait(args)
    except Exception:  # noqa: BLE001 — queue.Full included
        pass


def drain(timeout: float = 5.0) -> bool:
    """Wait for queued outcomes to be learned (tests, shutdown). True if empty."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _queue.unfinished_tasks


def summary() -> dict:
    """Counts for diagnostics."""
    data = _load()
    counts: dict[str, int] = {}
    for lesson in data["lessons"]:
        status = lesson.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
    return {"lessons": counts, "pending": len(data["pending"])}


def backfill(records: list[dict]) -> int:
    """Replay task_trace 'tool' records (oldest first). Returns how many taught a lesson."""
    from datetime import datetime

    learned = 0
    for rec in records:
        if rec.get("kind") != "tool" or rec.get("app", "era") != "era":
            continue
        try:
            ts = datetime.fromisoformat(rec["ts"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if observe(
            rec.get("user_text", ""),
            rec.get("tool") or rec.get("requested", ""),
            rec.get("args") or rec.get("requested_args") or {},
            bool(rec.get("ok")),
            rec.get("error_type"),
            rec.get("result"),
            rec.get("verification"),
            rec.get("safety"),
            now=ts,
        ):
            learned += 1
    return learned
