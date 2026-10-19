"""Explicit CodeMode ownership, revisions and existing Development integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path

from core import codemode_models
from core import development as dev
from core.cancel_registry import REGISTRY
from core.codemode_cli import ClaudeSession
from core.input_envelope import InputEnvelope

ROOT = Path(__file__).resolve().parents[1] / "runtime/codemode"
ENTRY = re.compile(r"^(?:era[, ]+)?(?:enter|start|enable|open)\s+code\s*mode[.! ]*$", re.I)
EXIT = re.compile(r"^(?:era[, ]+)?(?:exit|leave|disable|close)\s+code\s*mode[.! ]*$", re.I)
CANCEL = re.compile(
    r"^(?:cancel|stop) (?:this |the )?(?:coding (?:task|turn)|code\s*mode task)[.! ]*$", re.I
)


def configured_profile() -> tuple[str, str]:
    from core.llm.registry import resolve

    entry = resolve("era.reason")
    path = entry.provider_config.get("config_dir") or str(Path.home() / ".claude")
    return str(Path(path).expanduser().resolve()), entry.model or "sonnet"


class CodeMode:
    def __init__(self, *, cli_factory=ClaudeSession, context_builder=None, clock=time.time):
        self.enabled = False
        self.session_id = ""
        self.revision = self.sequence = 0
        self.phase, self.error, self.work_id, self.job_id = "off", "", "", ""
        self.profile, self.context = "", {}
        self._task = self._cli = self._token = None
        self._factory, self._builder, self._clock = cli_factory, context_builder, clock
        self._lock = threading.RLock()
        self._approval_revision = None
        self._speech_muted = False
        self._seen = set()
        self._controls_seen = set()
        self._request_fingerprint = ""
        self.model = ""

    def snapshot(self):
        with self._lock:
            phase = self.phase
            if self.job_id and phase == "review":
                try:
                    job = dev._read(self.job_id)
                    if job["state"] != "awaiting_approval":
                        phase = "applied" if job["state"] == "activated" else job["state"]
                    elif job.get("approval_expires_at", 0) <= self._clock():
                        phase = "expired"
                except (ValueError, OSError):
                    phase = "unavailable"
            return {
                "enabled": self.enabled,
                "session_id": self.session_id,
                "revision": self.revision,
                "sequence": self.sequence,
                "phase": phase,
                "error": self.error,
                "work_id": self.work_id,
                "job_id": self.job_id,
                "profile": Path(self.profile).name if self.profile else "",
                "model": self.model or codemode_models.load(),
                "models": list(codemode_models.MODELS),
                "tools": [
                    "source.read",
                    "source.search",
                    "candidate.stage",
                    "tests.verify",
                    "diff.review",
                ],
                "context": [
                    {k: row[k] for k in ("path", "sha256", "start_line", "truncated")}
                    for row in self.context.get("files", [])
                ],
            }

    def _event(self, phase=None, error=""):
        with self._lock:
            if phase:
                self.phase = phase
            self.error = error
            self.sequence += 1
            view = self.snapshot()
            if self.session_id:
                folder = ROOT / self.session_id
                folder.mkdir(parents=True, exist_ok=True)
                temp = folder / "state.tmp"
                temp.write_text(json.dumps(view), encoding="utf-8")
                from memory.safe_store import _replace

                _replace(temp, folder / "state.json")
        return view

    def enter(self):
        if self.enabled:
            return "CodeMode is already active."
        if self._task and not self._task.done():
            return "CodeMode is stopping the previous turn. Try entry again in a moment."
        profile, model = configured_profile()
        model = codemode_models.load(model)
        session_id = str(uuid.uuid4())
        cli = self._factory(profile, session_id, ROOT / session_id / "cli", model=model)
        self.profile, self.session_id, self._cli = profile, session_id, cli
        self.model = model
        self.enabled = True
        self.revision += 1
        self._approval_revision = None
        self.job_id, self.work_id, self.context = "", "", {}
        self._event("ready")
        return "CodeMode is ready. Tell me what to investigate or change."

    def select_model(self, requested: str) -> str:
        model = codemode_models.normalize(requested)
        if self._task and not self._task.done():
            raise ValueError(
                "Wait for the coding turn to finish or cancel it before changing models"
            )
        with self._lock:
            codemode_models.save(model)
            self.model = model
            if self._cli is not None:
                self._cli.model = model
            self._event()
        suffix = (
            "" if requested.strip().lower() == model else " using its current subscription alias"
        )
        return f"CodeMode will use Claude {model.title()}{suffix}."

    def current(self, revision):
        return revision == self.revision and self._token is not None and not self._token.cancelled()

    def cancel(self, *, exit_mode=False):
        with dev._lock:
            self._cancel_locked(exit_mode=exit_mode)

    def _cancel_locked(self, *, exit_mode=False):
        # Preserve an already verified review on exit, as the design specifies.
        preserve = exit_mode and self.phase == "review"
        if not preserve:
            self.revision += 1
            self._approval_revision = None
            if self.job_id:
                try:
                    dev.cancel(self.job_id)
                except (ValueError, OSError):
                    pass
            if self._token:
                REGISTRY.cancel(self._token.cid, source="codemode")
        if self._task and not self._task.done():
            self._task.cancel()
        if exit_mode:
            self.enabled = False
        self._event("review" if preserve else ("off" if exit_mode else "cancelled"))

    def start(self, owner: InputEnvelope, emit):
        if not self.enabled:
            raise ValueError("Enter CodeMode explicitly first")
        if owner.id in self._seen:
            return False
        fingerprint = hashlib.sha256((owner.source + owner.text.strip()).encode()).hexdigest()
        if self._task and not self._task.done() and fingerprint == self._request_fingerprint:
            return False
        if len(owner.text.encode()) > 8000:
            raise ValueError("Use a coding request of at most 8000 bytes")
        self._seen.add(owner.id)
        self._request_fingerprint = fingerprint
        if len(self._seen) > 128:
            self._seen = {owner.id}
        previous = self._task
        self.cancel()
        self.job_id = ""
        revision = self.revision
        self._speech_muted = False
        self.work_id = "codemode-" + uuid.uuid4().hex
        self._token = REGISTRY.register(self.work_id, "codemode", label="CodeMode")
        token = self._token
        self._event("context")

        async def run():
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            try:
                await self._run(owner, revision, emit)
            except asyncio.CancelledError:
                if revision == self.revision:
                    if self.job_id:
                        await asyncio.to_thread(dev.cancel, self.job_id)
                    self._approval_revision = None
                    self._event("cancelled")
                raise
            except Exception as exc:
                if self.current(revision):
                    from core.logger import redact_secrets

                    message = str(redact_secrets(str(exc)))[:400]
                    self._event("failed", message)
                    await emit("error", "CodeMode could not complete this turn: " + message, owner)
            finally:
                REGISTRY.finish(token.cid)

        self._task = asyncio.create_task(run(), name=self.work_id)
        REGISTRY.attach_task(self.work_id, self._task)
        return True

    async def _run(self, owner, revision, emit):
        from core.codemode_context import SYSTEM, build
        from core.development_submission import submit

        authority = {
            "source": "codemode",
            "session_id": self.session_id,
            "turn_revision": revision,
            "input_id": owner.id,
            "input_source": owner.source,
        }
        from core.codemode_team import ORDER, consult

        contribution = None
        if ORDER.fullmatch(owner.text.strip()):
            pending = asyncio.create_task(
                asyncio.to_thread(consult, owner, self.work_id, self._token, authority)
            )
            try:
                contribution = await asyncio.shield(pending)
            except asyncio.CancelledError:

                def clean_abandoned(future):
                    try:
                        result = future.result()
                        job_id = result.get("work", {}).get("development_id") if result else None
                        if job_id:
                            dev.cancel(job_id)
                    except Exception:
                        pass  # cancelled crew failures cannot revive a dead revision

                pending.add_done_callback(clean_abandoned)
                raise
            if not self.current(revision):
                return
            contributed_job = contribution.get("work", {}).get("development_id")
            if contributed_job:
                self.job_id = contributed_job
                await self._review_job(contributed_job, owner, revision, emit)
                return

        context = await asyncio.to_thread(self._builder or build, owner.text)
        if not self.current(revision):
            return
        self.context = context
        try:
            from memory.memory_manager import retrieve_ranked_memory

            facts = await asyncio.to_thread(
                retrieve_ranked_memory, owner.text, top_k=3, category="preferences"
            )
            context["user_preferences"] = [
                {"key": row.get("key", ""), "value": str(row.get("value", ""))[:350]}
                for row in facts
            ]
        except Exception:
            context["memory_status"] = "unavailable"
        if contribution:
            context["crew_contribution"] = contribution
        self._event("responding")
        await emit("start", "", owner)

        async def text(delta):
            if self.current(revision):
                self.sequence += 1
                await emit("text" if not self._speech_muted else "text_silent", delta, owner)

        from core.codemode_context import expand

        delivered = False

        async def tracked_text(delta):
            nonlocal delivered
            delivered = delivered or bool(delta)
            await text(delta)

        async with asyncio.timeout(180):
            for round_number in range(3):
                prompt = SYSTEM
                prompt = (
                    prompt
                    + "\nUSER REQUEST:\n"
                    + owner.text
                    + "\nUNTRUSTED CURRENT CONTEXT:\n"
                    + json.dumps(context, ensure_ascii=False)
                )
                attempted = {self.model}
                switched = False
                while True:
                    try:
                        answer = await self._cli.generate(prompt, tracked_text)
                        if switched:
                            codemode_models.save(self.model)
                        break
                    except Exception as exc:
                        # Only explicit model rejection before any content may
                        # retry. Auth/quota/transport and partial replies retain
                        # their original failure, without duplicate work.
                        rejected = re.search(
                            r"(?:model.{0,80}(?:not available|unavailable|not found|not supported|does not exist)|(?:unknown|invalid|unsupported) model)",
                            str(exc),
                            re.I,
                        )
                        choices = [
                            model for model in codemode_models.MODELS if model not in attempted
                        ]
                        if (
                            delivered
                            or not rejected
                            or not choices
                            or not self.current(revision)
                            or getattr(exc, "kind", "") in {"auth", "quota"}
                        ):
                            raise
                        previous, replacement = self.model, choices[0]
                        attempted.add(replacement)
                        switched = True
                        self.model = self._cli.model = replacement
                        self._event("responding")
                        await emit(
                            "notice",
                            f"Claude {previous.title()} is unavailable; trying {replacement.title()} on the same subscription.",
                            owner,
                        )
                if not self.current(revision):
                    return
                requests = answer.get("read_requests")
                if not requests:
                    break
                if round_number == 2:
                    raise ValueError(
                        "Source retrieval budget reached; narrow the requested code change"
                    )
                context = await asyncio.to_thread(expand, context, requests)
                if not self.current(revision):
                    return
                self.context = context
                self._event("responding")
        if not self.current(revision):
            return
        await emit("end", answer["reply"], owner)
        plan = answer.get("candidate")
        if plan is None:
            self._event("ready")
            return
        bases = {row["path"]: row["sha256"] for row in context["files"]}
        for item in plan.get("files", []):
            path = dev._path(item.get("path"))
            if path.exists() and (
                item["path"] not in bases or bases[item["path"]] != item.get("base_sha256")
            ):
                raise ValueError("Candidate requires fresh context for " + item["path"])
        submission = asyncio.create_task(
            asyncio.to_thread(
                submit,
                owner.text,
                plan,
                owner="codemode",
                participants=["claude", "era"],
                work_id=self.work_id,
                authority=authority,
            )
        )
        try:
            job = await asyncio.shield(submission)
        except asyncio.CancelledError:
            try:
                abandoned = await submission
                await asyncio.to_thread(dev.cancel, abandoned["id"])
            finally:
                raise
        self.job_id = job["id"]
        if not self.current(revision):
            dev.cancel(job["id"])
            return
        await self._review_job(job["id"], owner, revision, emit)

    async def _review_job(self, job_id, owner, revision, emit):
        self._event("verifying")
        await emit(
            "notice", "Candidate submitted for verification. Live source is unchanged.", owner
        )
        while self.current(revision):
            record = await asyncio.to_thread(dev._read, job_id)
            if record["state"] == "awaiting_approval":
                with dev._lock:
                    record = dev._read(job_id)
                    if record["state"] != "awaiting_approval":
                        continue
                    record["approval_expires_at"] = self._clock() + 900
                    dev._save(record)
                self._approval_revision = revision
                self._event("review")
                await emit(
                    "notice",
                    "Verification passed. Review the exact diff in Development. "
                    f"Approve coding change {job_id} to apply it, or reject it. Approval expires in 15 minutes.",
                    owner,
                )
                return
            if record["state"] in {"failed", "cancelled", "rejected"}:
                raise ValueError(record.get("error") or "Candidate verification did not pass")
            await asyncio.sleep(0.25)

    def authorize(self, job):
        with self._lock:
            if (
                job.get("session_id") != self.session_id
                or job["id"] != self.job_id
                or job.get("turn_revision") != self.revision
                or self._approval_revision != self.revision
                or not job.get("approval_expires_at")
                or self._clock() >= job["approval_expires_at"]
                or self._token is None
                or self._token.cancelled()
            ):
                raise ValueError(
                    "CodeMode approval is expired, cancelled or belongs to another revision"
                )

    async def decide(self, text: str, owner: InputEnvelope, emit):
        if self.phase == "review" and text.lower().strip(".! ") in (
            "yes",
            "approve",
            "apply it",
            "no",
            "reject",
        ):
            presented = await asyncio.to_thread(dev._read, self.job_id)
            if owner.source != presented.get("input_source"):
                raise ValueError(
                    "Answer the presented approval in its original channel or use its exact job ID"
                )
            action = "reject" if text.lower().strip(".! ") in ("no", "reject") else "approve"
            text = f"{action} coding change {self.job_id}"
        match = re.fullmatch(r"(approve|reject) coding change ([a-f0-9]{16})[.! ]*", text, re.I)
        if not match:
            return False
        job = await asyncio.to_thread(dev._read, match[2])
        if match[1].lower() == "reject":
            self.authorize(job)
            await asyncio.to_thread(dev.reject, job["id"])
            self._approval_revision = None
            self._event("ready" if self.enabled else "off")
            await emit("notice", "Coding change rejected. Live source is unchanged.", owner)
        else:
            # activate re-checks this authority under the same promotion lock.
            result = await asyncio.to_thread(
                dev.activate, job["id"], job["approval"], confirmed=True
            )
            self._event("applied")
            await emit(
                "notice",
                "Approved coding change applied."
                + (" Restart Era to load it." if result.get("requires_restart") else ""),
                owner,
            )
        return True


_instance = None
_instance_lock = threading.Lock()


def get_codemode():
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = CodeMode()
        return _instance
