"""Crew candidates enter the existing development verifier and user review.

HELD UNTIL THE USER SAYS YES (2026-10-18)
  Verification runs the candidate's model-written tests as the user, before the
  activation approval. So a crew candidate (`hold=True`) does not start verifying on
  submit: it is parked under an unguessable ticket and only the ticket travels
  through the shared confirmation store (as the carried `_approved_plan`, which the
  model cannot supply). The user's own yes re-runs `crew` action=verify_candidate,
  which pops the ticket (single use) and only then starts the verifier.
"""

from __future__ import annotations

import copy
import re
import secrets
import threading
import uuid

from core import development as dev

HELD_STATE = "held_for_verification"
_HELD: dict[str, tuple[dict, dict]] = {}  # ticket -> (job, plan); memory only


def submit(
    request: str,
    plan: dict,
    *,
    owner: str,
    participants: list[str],
    work_id: str,
    authority: dict | None = None,
    hold: bool = False,
) -> dict:
    if not isinstance(request, str) or not 1 <= len(request.strip()) <= 8000:
        raise ValueError("A candidate requires a bounded user task")
    plan = copy.deepcopy(plan)
    files = plan.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 6:
        raise ValueError("A candidate requires 1-6 files")
    bases = {}
    for item in files:
        if not isinstance(item, dict) or "base_sha256" not in item:
            raise ValueError("Each file needs its observed base_sha256, or null for a new file")
        path, base = item.get("path"), item["base_sha256"]
        target = dev._path(path)
        if base is not None and (
            not isinstance(base, str) or not re.fullmatch(r"[a-f0-9]{64}", base)
        ):
            raise ValueError("Invalid base hash")
        if dev._hash(target) != base:
            raise ValueError(f"Source changed; read {path} again before submitting")
        if path in bases or not isinstance(item.get("content"), str):
            raise ValueError("Candidate paths must be unique and contain complete source")
        bases[path] = base
    if sum(len(item["content"].encode("utf-8")) for item in files) > 200000:
        raise ValueError("Candidate exceeds 200KB")
    tests = plan.get("tests")
    if not isinstance(tests, list) or not 1 <= len(tests) <= 6:
        raise ValueError("Give 1-6 focused tests")
    for test in tests:
        dev._path(test)
        if not re.fullmatch(r"tools/test_[a-zA-Z0-9_]+\.py", test):
            raise ValueError("Tests must be tools/test_*.py files")
    if not any(test in bases for test in tests):
        raise ValueError("The candidate must include a new or updated test")
    with dev._lock:
        if dev._active is not None:
            raise ValueError("A development job is already running")
        job = {
            "id": uuid.uuid4().hex[:16],
            "request": request,
            "state": "planning",
            "progress": 0,
            "plan": "",
            "phase": "Crew candidate queued for verification",
            "attempt": 1,
            "attempts": [],
            "files": [],
            "checks": {},
            "reload": True,
            "approval": "",
            "expected_bases": bases,
            "owner": owner,
            "participants": sorted(set(participants)),
            "work_id": work_id,
            "source": "crew",
        }
        if authority is not None:
            if authority.get("source") != "codemode":
                raise ValueError("Unsupported submission authority")
            job.update(
                {
                    key: authority[key]
                    for key in ("source", "session_id", "turn_revision", "input_id", "input_source")
                }
            )
            dev._cancel_events[job["id"]] = threading.Event()
        if hold:
            return _hold(job, plan)
        dev._active = job["id"]
        try:
            dev._save(job)
            threading.Thread(
                target=_verify, args=(job, plan), name="CrewVerification", daemon=True
            ).start()
        except Exception:
            dev._active = None
            dev._cancel_events.pop(job["id"], None)
            raise
        return copy.deepcopy(job)


def _hold(job: dict, plan: dict) -> dict:
    """Park a crew candidate and arm the user's yes. Caller holds dev._lock."""
    from core.pending_confirmation import PendingConfirmationBusy, get_shared_store

    ticket = secrets.token_urlsafe(24)
    job.update(state=HELD_STATE, phase="Waiting for your yes before its code and tests run")
    try:
        confirmation_id = get_shared_store().store(
            tool="crew",
            parameters={
                "action": "verify_candidate",
                "development_id": job["id"],
                "_approved_plan": {"ticket": ticket},
            },
            risk_level="MEDIUM",
            reason=describe_held(job["owner"], plan),
        )
    except PendingConfirmationBusy as exc:
        dev._cancel_events.pop(job["id"], None)
        raise ValueError("Something else is already waiting for the user's yes") from exc
    _HELD[ticket] = (job, plan, confirmation_id)
    dev._held_ids.add(job["id"])
    dev._save(job)
    return {**copy.deepcopy(job), "confirmation_id": confirmation_id}


def describe_held(owner: str, plan: dict) -> str:
    """What the user is asked to approve, said plainly: the candidate's own code runs."""
    files = ", ".join(str(item.get("path")) for item in plan.get("files", []))[:400]
    tests = ", ".join(str(t) for t in plan.get("tests", []))[:200]
    return (
        f"{owner} staged a change to {files}. Verifying it runs that code and its tests "
        f"({tests}) on this PC with your permissions, before you review the result."
    )


def start_held(approved_plan, approved_confirmation: str | None) -> dict:
    """Run a held crew candidate's verification. Only the user's own yes gets here:
    the ticket arrives from the confirmation store, and `approved_confirmation` is the
    id the dispatcher consumed after checking that yes. It is passed explicitly —
    the crew tool runs in an executor thread, where the ContextVar is not visible —
    and must be the very confirmation this ticket was armed with."""
    ticket = approved_plan.get("ticket") if isinstance(approved_plan, dict) else None
    with dev._lock:
        held = _HELD.get(ticket) if isinstance(ticket, str) else None
        if held is None:
            raise ValueError("No crew candidate is waiting for verification")
        job, plan, armed_with = held
        if not approved_confirmation or approved_confirmation != armed_with:
            raise ValueError("Verifying a crew candidate needs the user's own yes to it")
        del _HELD[ticket]
        dev._held_ids.discard(job["id"])
        try:
            on_disk = dev._read(job["id"]).get("state")
        except (OSError, ValueError):
            on_disk = None
        if on_disk != HELD_STATE:
            dev._cancel_events.pop(job["id"], None)
            raise ValueError("That crew candidate is no longer waiting (cancelled or expired)")
        if dev._active is not None:
            job.update(
                state="failed",
                progress=100,
                phase="Another development job was running; nothing was verified",
                error="Another development job was running. Ask the crew to stage it again.",
            )
            dev._cancel_events.pop(job["id"], None)
            dev._save(job)
            raise ValueError(job["error"])
        job.update(state="planning", phase="Crew candidate verifying after your yes")
        dev._active = job["id"]
        try:
            dev._save(job)
            threading.Thread(
                target=_verify, args=(job, plan), name="CrewVerification", daemon=True
            ).start()
        except Exception:
            dev._active = None
            dev._cancel_events.pop(job["id"], None)
            raise
        return copy.deepcopy(job)


def _verify(job: dict, plan: dict) -> None:
    try:
        dev.prepare(job, plan)
    except Exception as exc:
        job.update(
            state="failed",
            phase="Crew candidate failed; live source unchanged",
            error=dev._failure_hint(exc),
            approval="",
        )
        dev._save(job)
    finally:
        with dev._lock:
            if dev._active == job["id"]:
                dev._active = None
            dev._cancel_events.pop(job["id"], None)
        dev.report_finished(job)
