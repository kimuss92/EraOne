"""Community-management flow mixin for EraLive (extracted from main.py).

Pending brand-ask handling, the lane router hand-off, CM dispatch, feedback capture,
and CM report delivery. Operates on self.* state.
"""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.cm_intents import _infer_cm_task_type
from app.era_constants import _CM_HANDOFF_WINDOW_S
from app.hud.events import hud
from app.notify import _tg_notify
from app.text_intents import _build_paste_clarify_instruction, _say_exactly
from core.cancel_registry import CURRENT_TOKEN, REGISTRY, CancelledTask, offload
from core.tool_result import ToolResult

# PF-072: the outcome of the LAST community-management job is kept where Era can read it.
# Before, a result was only spoken (or queued for a reconnect); asked "what did the Meta
# check say?" she had nothing but a "handed off" line and offered to "check the log file".
_CM_LAST_PATH = Path(
    os.getenv("ERA_CM_LAST_RESULT", str(Path.home() / ".era" / "cm_last_result.json"))
)


def _record_cm_outcome(payload: dict[str, Any], ok: bool, text: Any, result: Any = None) -> None:
    """Persist how a CM job ended. Never raises: bookkeeping must not break delivery."""
    try:
        rec = {
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "brand": payload.get("brand_key"),
            "task_type": payload.get("task_type"),
            "order": str(payload.get("text", ""))[:300],
            "ok": bool(ok),
            "report": str(text or "")[:4000],
            "wrote_files": (result or {}).get("wrote_files") if isinstance(result, dict) else None,
            "log_path": (result or {}).get("log_path") if isinstance(result, dict) else None,
        }
        _CM_LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CM_LAST_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, _CM_LAST_PATH)
    except Exception as e:  # noqa: BLE001
        print(f"[CM-FLOW] could not record outcome: {e}")


def read_last_cm_outcome(max_age_s: float = 6 * 3600) -> dict[str, Any] | None:
    """The last finished CM job (newer than `max_age_s`), or None."""
    try:
        rec = json.loads(_CM_LAST_PATH.read_text(encoding="utf-8"))
        done = datetime.fromisoformat(rec["finished_at"])
        if (datetime.now(UTC) - done).total_seconds() > max_age_s:
            return None
        return rec
    except Exception:  # noqa: BLE001
        return None


def render_last_cm_outcome(rec: dict[str, Any]) -> str:
    """One block Era can answer from: what ran, whether it worked, what it said."""
    status = "FINISHED OK" if rec.get("ok") else "FAILED"
    return (
        f"Last community-management job ({rec.get('brand')} {rec.get('task_type')}, "
        f"{status} at {rec.get('finished_at')}).\nOrder: {rec.get('order')}\n"
        f"Report: {rec.get('report')}"
    )


class _CMOutcome:
    """A failed CM job as the HUD tool adapter reads results."""

    success = False
    error_type = "Exception"

    def __init__(self, error: str) -> None:
        self.error = error
        self.message = ""


def _check_cm_cancel() -> None:
    token = CURRENT_TOKEN.get()
    if token is not None and token.cancelled():
        raise CancelledTask("The user cancelled this CM request.")


def _speakable_ads_result(text: Any, limit: int = 650) -> str:
    """A finished ads run's result, made safe to read aloud.

    The result is the model's final message. Asked for a report it used to be a
    multi-paragraph markdown block — ids, config paths, tables, backticks — which
    is fed to a voice model as if the user had said it. The ads briefs now ask
    for six plain sentences; this is the backstop for when a run ignores that,
    so a long or formatted result degrades to something speakable, never to a
    wall of symbols. Only used for the ads lane: other lanes' results are already
    written to be spoken.
    """
    t = str(text or "").strip()
    if not t:
        return "Done."
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)  # [label](url) -> label
    t = re.sub(r"`[^`]*[\\/][^`]*`", "", t)  # `paths` are noise when spoken
    t = re.sub(r"^\s*[-•*]\s+", "", t, flags=re.MULTILINE)  # bullets
    t = re.sub(r"[*_`#>|]+", "", t)  # remaining markdown marks
    t = re.sub(r"\s*\n+\s*", " ", t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.5:
        cut = cut[: end + 1]
    return cut.rstrip()


class CMFlowMixin:
    def _consume_pending_cm_ask(self, text: str) -> dict[str, Any] | None:
        """One-shot check of an outstanding "which brand?" ask. This incoming
        utterance IS its single reply attempt (per the agreed guardrails), so the
        pending ask is cleared here regardless of outcome. Returns a dispatch
        payload if `text` looks like nothing more than a bare brand-name reply
        (short, resolves to exactly one known brand); otherwise returns None so a
        genuinely new sentence is treated as a topic change, not an answer.
        """
        pending = self._pending_cm_ask
        if not pending:
            return None
        self._pending_cm_ask = None

        if time.time() > pending["expires_at"]:
            return None

        brand_key = None
        if len(text.split()) <= 6:
            try:
                import era_dispatch as ed

                brand_key = ed.resolve_brand(text)
            except Exception:
                brand_key = None

        if not brand_key:
            return None
        return {"text": pending["text"], "task_type": pending["task_type"], "brand_key": brand_key}

    def _pending_cm_ask_active(self) -> bool:
        """True ONLY while an unanswered brand-ask is still inside its 45s window.
        Tool-suppression is gated on this, so it can never get stuck: a valid
        reply or a non-match both clear _pending_cm_ask in _consume_pending_cm_ask,
        and an ask that is simply never answered self-expires HERE (and is lazily
        cleared) with no timer needed — the next tool call after 45s sees it
        inactive and tools resume."""
        pending = self._pending_cm_ask
        if not pending:
            return False
        if time.time() > pending["expires_at"]:
            self._pending_cm_ask = None  # lazy cleanup of an abandoned ask
            return False
        return True

    def _arm_cm_handoff(self) -> None:
        """Mute the live Gemini model for the next few seconds: the CM lane now
        owns this utterance (see _CM_HANDOFF_WINDOW_S). Synchronous and called
        BEFORE the dispatch task is scheduled, so the guard is already up by the
        time any Gemini tool call or audio frame for the same utterance arrives."""
        self._cm_handoff_until = time.time() + _CM_HANDOFF_WINDOW_S
        print(f"[CM-HANDOFF] armed for {_CM_HANDOFF_WINDOW_S}s — Gemini muted, CM lane owns this")

    def _cm_handoff_active(self) -> bool:
        """True only while a CM hand-off window is still open. Self-expiring: no
        timer, the next check after the window simply reads it as inactive."""
        return time.time() < self._cm_handoff_until

    async def _route_text_through_lanes(self, text: str) -> None:
        """Stage 3 wiring, typed text only: classify `text` with the lane router BEFORE
        Era's normal Gemini flow ever sees it, so CM work never reaches Gemini at
        all (no double-reply risk here — unlike voice, Era has the full text
        before responding). Voice is untouched; see [[era-lane-routing]] for why.
        """
        payload = self._consume_pending_cm_ask(text)
        if payload is None:
            hud.routing_started()
            _route_t0 = time.monotonic()
            try:
                import lane_dispatch

                decision = await asyncio.to_thread(lane_dispatch.handle, text, dispatch=False)
            except Exception as e:
                print(f"[era] [LANE] classify failed, falling through to normal flow: {e}")
                decision = {"handled": False}
            hud.routing_finished(
                "community manager" if decision.get("handled") else "live model",
                (time.monotonic() - _route_t0) * 1000,
            )

            if not decision.get("handled"):
                # The lane router didn't claim it (not a CM order), so now it's safe to
                # check whether it's a pasted blob with no command intent. If so,
                # frame it as data and ask what to do, instead of forwarding raw
                # text the model tends to ignore. A real command/question is sent
                # through untouched.
                #
                # The snapshot is NOT optional here. Without it this second
                # classification loses the "Era has a question open" context the
                # first one had in _on_text_command, so a short answer that just
                # passed the gate got re-branded as data one step later and came
                # back as "what would you like me to do with it?" — the reply
                # never reaching the model that asked.
                kind = self.safety_gate.classify_typed_input(text, self._text_state_snapshot())
                if kind == "TEXT_BLOB":
                    print(
                        f"[PASTE] lane-unclaimed input classified as {kind}; asking user what to do."
                    )
                    from core.model_reply import send_model_text

                    await send_model_text(self, _build_paste_clarify_instruction(kind, text))
                else:
                    from core.model_reply import send_model_text

                    await send_model_text(self, text)
                hud.request_sent("typed")
                return
            payload = {"text": text, "task_type": _infer_cm_task_type(text)}
            # CM claimed this. The headless dispatch owns it now, but the open mic
            # means Gemini heard the same words and will otherwise act on them in
            # parallel (rogue tool calls + a hallucinated "I need a public URL"
            # reply). Mute Gemini for this turn — arm BEFORE scheduling the job.
            self._arm_cm_handoff()

        task = asyncio.create_task(self._run_cm_flow(payload))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _handle_manage_schedule(self, args: dict[str, Any]) -> str:
        """List or change a brand's CM schedules via era_scheduler. Runs in the
        tool thread (era_scheduler is sync + does its own file lock)."""
        try:
            import era_dispatch as ed
            import era_scheduler
        except Exception as e:
            return f"The scheduler isn't available: {e}"

        action = (args.get("action") or "list").strip().lower()

        if action == "list":
            try:
                rows = era_scheduler.list_schedules()
            except Exception as e:
                return f"Couldn't read the schedules: {e}"
            if not rows:
                return "No community-management schedules are configured."
            parts = []
            for r in rows:
                state = "ON" if r["enabled"] else "off"
                parts.append(
                    f"{r['brand'].replace('_', ' ')} {r['task'].replace('_', ' ')}: "
                    f"{r['cron']} ({state})"
                )
            return "Current schedules — " + "; ".join(parts) + "."

        if action == "set":
            brand_in = (args.get("brand") or "").strip()
            task = (args.get("task") or "").strip().lower()
            cron = args.get("cron")
            enabled = args.get("enabled")
            if not brand_in or not task:
                return "To change a schedule I need the brand and which task (weekly_plan or auto_publish)."
            # Accept a display name ("Delta Cuisine") or an exact key.
            brand_key = brand_in if "_" in brand_in else (ed.resolve_brand(brand_in) or brand_in)
            if cron is None and enabled is None:
                return "Tell me the new time (as a cron) and/or whether to enable or disable it."
            try:
                updated = era_scheduler.set_schedule(
                    brand_key,
                    task,
                    cron=cron if cron else None,
                    enabled=enabled if enabled is not None else None,
                )
            except KeyError as e:
                return f"I couldn't find that: {e}. Brand must be a known brand and task must be weekly_plan or auto_publish."
            except Exception as e:
                return f"Changing the schedule failed: {e}"
            state = "enabled" if updated.get("enabled") else "disabled"
            return (
                f"Updated {brand_key.replace('_', ' ')} {task.replace('_', ' ')}: "
                f"cron '{updated.get('cron')}', {state}. It's live now."
            )

        return "Use action 'list' to see schedules, or 'set' to change one."

    def _handle_manage_offer(self, args: dict[str, Any]) -> str:
        """Turn a brand's standing promotion on/off in brands.yaml. This is how
        "Era, the -10% is no longer valid" reaches the CM: an inactive offer is
        never injected into a produce brief again. Runs in the tool thread
        (era_offers is sync and takes its own lock)."""
        try:
            import era_dispatch as ed
            import era_offers
        except Exception as e:
            return f"The offers registry isn't available: {e}"

        action = (args.get("action") or "list").strip().lower()

        if action == "list":
            brand_in = (args.get("brand") or "").strip()
            brand_key = None
            if brand_in:
                brand_key = (
                    brand_in if "_" in brand_in else (ed.resolve_brand(brand_in) or brand_in)
                )
            try:
                rows = era_offers.list_offers(brand_key)
            except Exception as e:
                return f"Couldn't read the offers: {e}"
            if not rows:
                return "No promotions are configured for that brand."
            parts = [
                f"{r['brand'].replace('_', ' ')}: {r['label']} "
                f"({'active' if r['active'] else 'ended'})"
                for r in rows
            ]
            return "Current offers — " + "; ".join(parts) + "."

        if action == "set":
            brand_in = (args.get("brand") or "").strip()
            offer_ref = (args.get("offer") or "").strip()
            active = args.get("active")
            if not brand_in or not offer_ref:
                return "To change an offer I need the brand and which offer (e.g. '-10%')."
            if active is None:
                return "Tell me whether that offer is now active or has ended."
            brand_key = brand_in if "_" in brand_in else (ed.resolve_brand(brand_in) or brand_in)
            try:
                updated = era_offers.set_offer(brand_key, offer_ref, active=bool(active))
            except KeyError as e:
                return f"I couldn't apply that: {e}"
            except Exception as e:
                return f"Changing the offer failed: {e}"
            nice_brand = brand_key.replace("_", " ")
            if updated["active"]:
                return (
                    f"{updated['label']} is active again for {nice_brand} — I'll use it "
                    f"in promo content from now on."
                )
            return (
                f"Noted: {updated['label']} has ended for {nice_brand}. I won't use it "
                f"in any new content. Already-published posts are untouched."
            )

        return "Use action 'list' to see offers, or 'set' to start or end one."

    async def _run_cm_flow(self, payload: dict[str, Any], *, announce: bool = True) -> None:
        """HUD-tracked wrapper: the job shows as a BACKGROUND activity (it can run
        for half an hour, so it never holds the agent state). "Ended" is all the
        wrapper can truthfully say — the job reports its own outcome aloud."""
        args = {"task_type": payload.get("task_type", "weekly"), "brand": payload.get("brand_key")}
        token, existing = REGISTRY.claim_unique(
            "cm-" + uuid.uuid4().hex,
            "cm_request",
            {**args, "order": str(payload.get("text", "")).strip()},
        )
        if existing is not None:
            return
        try:
            cid = hud.tool_started(
                token.cid, "community_manager", args, background=True, source="cm lane"
            )
            REGISTRY.rekey_claim(token, cid)
        except BaseException:
            REGISTRY.finish(token.cid)
            raise
        outcome: Any = None
        context_token = CURRENT_TOKEN.set(token)
        try:
            # Through the class, so an object that only borrowed _run_cm_flow
            # (tests bind mixin methods one by one) still reaches the job body.
            _check_cm_cancel()
            await CMFlowMixin._run_cm_flow_job(self, payload, announce=announce)
            outcome = "Job ended — Era reports the outcome"
        except CancelledTask:
            outcome = ToolResult.fail(
                "CM request cancelled; already dispatched effects cannot be undone.",
                error_type="Cancelled",
            )
            hud.tool_cancelled(cid, "Cancellation acknowledged.")
        except BaseException as e:
            outcome = _CMOutcome(f"CM job raised: {type(e).__name__}")
            raise
        finally:
            CURRENT_TOKEN.reset(context_token)
            REGISTRY.finish(cid)
            hud.tool_finished(cid, "community_manager", args, outcome)

    async def _run_cm_flow_job(self, payload: dict[str, Any], *, announce: bool = True) -> None:
        """Runs the actual (potentially 30-min) era_dispatch job as a bare
        create_task, deliberately NOT adopted into the TaskGroup — era_dispatch's
        cross-process lock can hold far longer than a Live session lives, and
        must never block _receive_audio from reaching its _go_away_event check.
        See [[era-goaway-reconnect]].

        announce: speak the "Got it — running X for <brand>" pre-dispatch echo.
        Left True for the text/lane-router path (nothing else acknowledges the order),
        but the community_manager tool sets it False — Gemini already voices its
        own acknowledgement from the tool result, so a second echo would double up.
        """
        _check_cm_cancel()
        text = payload["text"]
        task_type = payload.get("task_type", "weekly")
        brand_key = payload.get("brand_key")

        try:
            import era_dispatch as ed
        except Exception as e:
            _record_cm_outcome(payload, False, f"failed to start: {e}")
            self._deliver_or_queue_cm_report(f"That community-management task failed to start: {e}")
            return

        busy = ed.current_job()
        if busy:
            self.speak(
                f"{busy.get('brand', 'a brand')}'s {busy.get('task_type', 'job')} is running "
                "right now, sir; yours will start right after."
            )

        # Echo back what's about to run BEFORE dispatch fires, so a wrong
        # pending-ask guess is caught immediately instead of discovered after.
        if brand_key and announce:
            self.speak(f'Got it — running "{text}" for {brand_key.replace("_", " ")}.')

        try:
            if brand_key:
                result = await offload(
                    lambda: ed.run_task(text, task_type=task_type, brand_key=brand_key)
                )
            else:
                import lane_dispatch

                result = await offload(
                    lambda: lane_dispatch.handle(text, task_type=task_type, dispatch=True)
                )
        except CancelledTask:
            raise
        except Exception as e:
            _record_cm_outcome(payload, False, f"failed: {e}")
            self._deliver_or_queue_cm_report(f"That community-management task failed: {e}")
            return

        _check_cm_cancel()
        print(
            f"[CM-FLOW] dispatch result: need_brand={result.get('need_brand')} "
            f"ok={result.get('ok')} brand={result.get('brand')}"
        )
        if result.get("need_brand"):
            print(f"[CM-ASK] ARMED pending-ask (guess-tools now suppressed) for {text[:50]!r}")
            # Set the pending flag BEFORE speaking — this assignment is synchronous
            # and completes before speak() even schedules the send to Gemini, so
            # tool-suppression (_pending_cm_ask_active) is already armed by the time
            # any Gemini tool call could arrive. See [[era-lane-routing]].
            self._pending_cm_ask = {
                "text": text,
                "task_type": task_type,
                "expires_at": time.time() + 45,
            }
            self.speak(
                "[System note: Ask the user aloud, exactly this and nothing "
                'more: "Which brand is this for?" Do not guess the brand or '
                "use any tool — just ask and wait for their reply.]"
            )
            return

        if result.get("ok"):
            spoken = result.get("result") or "Done."
            if task_type == "ads":
                spoken = _speakable_ads_result(spoken)
            # ok=True only means the headless subprocess exited 0. A produce/publish
            # run that changed NOTHING on disk is almost always a silent no-op — a
            # missing generation MCP (e.g. Magnific not wired into the brand account)
            # or a stop at an approval gate that produced nothing. Surface it instead
            # of reading it aloud as success. wrote_files is absent on older/faked
            # results, so `== 0` (not falsy) is deliberate — None must NOT trip this.
            #
            # NOT for ads: its work happens on Meta, not on disk. A budget change or
            # a pause legitimately writes nothing, and this caveat told Karim "it
            # may not have actually produced anything" about runs that had.
            if result.get("wrote_files") == 0 and task_type != "ads":
                print(
                    f"[CM-FLOW] WARNING: {result.get('brand') or brand_key} {task_type} "
                    f"wrote 0 files — likely a no-op. log={result.get('log_path')}"
                )
                spoken += (
                    " One caveat, sir: that run finished without creating or "
                    "changing any files, so it may not have actually produced "
                    "anything — worth a check."
                )
            # The two _tg_notify calls below already report this run to the chat, and
            # they carry the brand tag. Disarm the turn mirror so the reply Era speaks
            # doesn't arrive a second time, untagged.
            self._tg_reply_pending = False
            _record_cm_outcome(payload, True, spoken, result)
            self._deliver_or_queue_cm_report(spoken)
            # A CM run can take 30 minutes. If you ordered it from Telegram — or just
            # walked away — the spoken result reaches an empty room. Mirror it to the
            # chat so the plan and the Gate-2 draft list actually reach you.
            _tg_notify(f"[{(result.get('brand') or brand_key or 'CM')}] {spoken}")
        else:
            err = str(result.get("error", "unknown error"))
            _record_cm_outcome(payload, False, err, result)
            self._tg_reply_pending = False
            # Verbatim: live 2026-11-20 21:12 Gemini turned "couldn't find a reel named
            # 'new 2'" into an invented folder path and an offer to list it (PF-072).
            self._deliver_or_queue_cm_report(
                "That community-management task failed: " + err, verbatim=True
            )
            _tg_notify(f"[{brand_key or 'CM'}] task failed: {err}")

    def _maybe_record_feedback(self, user_text: str) -> None:
        """Detect explicit user corrections and record to feedback_memory.jsonl."""
        from memory.feedback_memory import FeedbackEvent, append_feedback, extract_feedback
        from memory.memory_audit import audit_event as _audit_fb

        t = (user_text or "").strip().lower()
        if not t:
            return

        # Long pasted/typed text is context (a draft, a doc, a concept to save),
        # not a live correction — scanning it for marker phrases below would flag
        # "always"/"never"/"i need you to" the moment they appear ANYWHERE in the
        # blob, even quoting someone else or describing a concept, and wrongly
        # log it as behavioral feedback (Karim, reported 2026-07-2x). Genuine
        # corrections and standing rules are short and directly addressed to Era
        # in practice — is_standing_instruction() below already refuses text
        # >=300 chars for the same reason, so nothing promotable is lost here.
        _LONG_PASTE_WORD_THRESHOLD = 50
        if len(t.split()) > _LONG_PASTE_WORD_THRESHOLD:
            return

        correction_markers = [
            # --- Direct correction phrases ---
            "wrong",
            "mistake",
            "that was wrong",
            "next time",
            "don't do that",
            "stop doing",
            "correction:",
            "you should",
            "it should be",
            "not like that",
            "didn't get it",
            "should not have",
            # --- Expanded: behavioral & preference feedback ---
            "never do",
            "always do",
            "from now on",
            "i prefer",
            "i like",
            "i don't like",
            "i hate",
            "don't use",
            "use instead",
            "don't say",
            "don't mention",
            "remember this",
            "keep in mind",
            "note that",
            "change to",
            "switch to",
            "use this instead",
            "that's not how",
            "not what i meant",
            "not what i asked",
            "don't open",
            "don't play",
            "don't search",
            "i want you to",
            "i need you to",
            "please do",
            "make sure to",
            "always remember",
            "never forget",
            "that worked",
            "that was right",
            "good job",
            "perfect",
        ]
        # A standing instruction ("minimize Spotify after use") carries none of
        # the markers above — it is not a correction, it is a rule. Accept it on
        # the same test the prefs layer uses to PERSIST it, so nothing that could
        # be promoted is dropped before promotion runs.
        from core.user_prefs import is_standing_instruction

        if not any(m in t for m in correction_markers) and not is_standing_instruction(user_text):
            return
        mistake_cat, signature, expected = extract_feedback(user_text)
        event = FeedbackEvent(
            timestamp=datetime.now(UTC).isoformat(),
            user_text=user_text[:500],
            assistant_last_tool=getattr(self, "_last_tool_name", None),
            mistake_category=mistake_cat,
            signature=signature,
            expected_correction=expected,
        )
        append_feedback(event)
        _audit_fb(
            operation="feedback",
            category=mistake_cat or "generic",
            key=signature or "unknown",
            new_value_summary=user_text[:120],
            source_tool="main",
            reason="user_correction_detected",
            allowed=True,
        )
        print(
            f"[Memory] [FEEDBACK] Feedback recorded: {mistake_cat or 'generic'}/{signature or 'unknown'}"
        )
        # Promote durable instructions (confirmation waivers, standing
        # behavior rules) so they survive relaunch and change behavior.
        try:
            from core.user_prefs import promote_feedback_to_prefs

            promoted = promote_feedback_to_prefs(user_text)
            if promoted:
                print(f"[Memory] [PREFS] {promoted}")
        except Exception as _pref_err:
            print(f"[Memory] [PREFS] promotion skipped: {_pref_err}")

    def _deliver_or_queue_cm_report(self, text: str, *, verbatim: bool = False) -> None:
        """Speak a finished CM status report now, or hold it for the next
        reconnect if there's no live session to speak it into right now.

        speak() silently no-ops with no session (by design — see its own
        comment), which is fine for routine chatter but wrong for the one
        thing a 30-minute community-management run produces: its result. A
        report that finishes into a dead connection must not just vanish —
        it gets delivered the moment the next session comes up (see
        _build_reconnect_acknowledgment), framed as what was missed rather
        than resent as if it just happened.

        verbatim=True wraps the text in _say_exactly() right before it's
        spoken (the queued/pending copy stays plain either way, since that's
        what gets logged and unit-tested). Use it for anything delivered
        specifically BECAUSE a reconnect just happened — that is exactly the
        situation that produced the 2026-07-21 bug where an unwrapped
        reconnect message got answered by Gemini instead of just spoken. The
        normal in-session completion path (a CM job finishing while already
        connected) does not need it — Gemini reacting naturally to a result
        it wasn't asked a rhetorical question in is the existing, wanted
        behavior.
        """
        from app.away_calls import automatic_report

        automatic_report(self, text, "background")
        # Standby keeps the live session OPEN but gates its audio, so `self.session`
        # is truthy while Era is effectively deaf and mute. Speaking then feeds the
        # report to a session whose reply is discarded: the 2026-09-19 ads report
        # finished 90 seconds after Era went to standby and vanished without a
        # trace. Held instead, it is delivered right after the resume ack (see
        # _resume_from_standby).
        if self._loop and self.session and not getattr(self, "_standby_active", False):
            # The hand-off window mutes Gemini so she does not answer an order the CM
            # lane owns. The lane's OWN report must be heard: live 2026-11-20 21:20 an
            # instant "couldn't find 'new 2'" refusal was spoken inside the window and
            # muted, so Era said nothing at all.
            self._cm_handoff_until = 0.0
            self.speak(_say_exactly(text) if verbatim else text)
            return
        # Accumulate, never overwrite: five reports finishing while Era was away
        # used to leave only the last one.
        held = self._pending_cm_announcement
        self._pending_cm_announcement = f"{held}\n{text}" if held else text
        self.ui.write_log(f"SYS: Task report queued: {text[:200]}")
        wake = getattr(self, "_request_completion_wake", None)
        if wake is not None:
            wake()
