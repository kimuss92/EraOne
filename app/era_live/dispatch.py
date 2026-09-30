"""Dispatch — being ANSWERED by a model other than the live voice session.

WHY THIS IS SEPARATE FROM model_route.py
----------------------------------------
That module owns one question: which alias should serve a request. This one owns
a different and much more consequential one: whether to actually go and spend
money asking it. Keeping them apart means the routing decision stays pure and
testable without any of the machinery below — and it kept both files under the
500-line limit when phase 11 arrived.

THE LAYERS, OUTERMOST FIRST
---------------------------
  shadow    observe what automatic routing WOULD do, act on none of it. Safe to
            leave on; costs a regex pass and one appended line.
  autopilot automatic by-task routing, allowlist-gated. The first thing that can
            spend money on a request the user did not explicitly route.
  pin       the user said "use Claude". Their instruction, so it goes.

`claude_dispatch.enabled` is the master switch over all of it — one flag turns
everything off — and it ships OFF.

THE SPEECH PATH IS A SECURITY BOUNDARY
--------------------------------------
Answers come back through _sanitize_spoken + _say_exactly, together. In
gemini_audio mode speak() feeds text INTO the live session as conversational
input, and that session holds Era's entire tool surface, so an unwrapped answer
from another model is a prompt-injection path with real actions on the end of
it. See _sanitize_spoken in app/text_intents.py for the payload it defuses.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from app.cm_intents import is_cm_order
from app.text_intents import _sanitize_spoken, _say_exactly
from core.llm.router import (
    RouteDecision,
    autopilot_policy,
    dispatch_policy,
    shadow_policy,
)
from core.llm.shadow import Observation, ShadowLog, fingerprint
from core.llm.spend import SpendLedger
from core.llm.spend import from_registry as spend_from_registry
from core.llm.taskclass import classify
from core.llm.types import LlmMessage, LlmRequest, PromptBlock
from core.logger import log_event, main_logger


class DispatchMixin:
    """Shadow observation, autopilot, and the actual non-voice call."""

    # ── dispatch (phase 10, default OFF) ──────────────────────────────────

    @property
    def spend_ledger(self) -> SpendLedger:
        """Daily budget guard, built on first use from the registry."""
        ledger = getattr(self, "_spend_ledger", None)
        if ledger is None:
            ledger = spend_from_registry()
            self._spend_ledger = ledger
        return ledger

    # ── shadow + autopilot ────────────────────────────────────────────────

    @property
    def shadow_log(self) -> ShadowLog:
        log = getattr(self, "_shadow_log", None)
        if log is None:
            log = ShadowLog()
            self._shadow_log = log
        return log

    def observe_routing(self, text: str, *, handled_by: str = "era.voice") -> Observation | None:
        """Record what automatic routing WOULD have done. Changes nothing.

        Costs a regex pass and an appended line. Never raises and never returns
        early on failure in a way the caller has to handle — a diagnostic that
        can break the request it observes is not worth having.
        """
        try:
            if not shadow_policy().get("enabled"):
                return None
            body = (text or "").strip()
            if not body:
                return None

            guess = classify(body)
            obs = Observation(
                task=guess.task if guess else "",
                matched=guess.matched if guess else "",
                confident=bool(guess and guess.confident),
                words=len(body.split()),
                handled_by=handled_by,
                fingerprint=fingerprint(body),
            )
            if guess is not None:
                decision = self.current_route(guess.task)
                if decision is not None and not decision.is_lane:
                    obs.would_alias = decision.alias
                    obs.would_provider = decision.provider
                    entry = decision.entry
                    obs.would_cost_usd = entry.call_overhead_usd if entry else 0.0
            self.shadow_log.record(obs)
            return obs
        except Exception as e:
            print(f"[era] [WARN] shadow observation skipped: {e}")
            return None

    def autopilot_decision(self, text: str, guess: Any = None) -> RouteDecision | None:
        """Automatic by-task routing, if autopilot allows this exact case.

        Four gates beyond the feature flag, because this is the first thing that
        would spend money on a request the user did not route:

          * the classifier must have an opinion at all;
          * that opinion must be on the ALLOWLIST (empty list = nothing);
          * it must be confident, unless the policy says otherwise;
          * today's automatic-call count must be under the cap — a misfiring
            classifier is caught by a count long before a dollar ceiling that
            also has to accommodate deliberate use.
        """
        policy = autopilot_policy()
        if not policy.get("enabled"):
            return None
        allowed = set(policy.get("tasks") or ())
        if not allowed:
            return None

        if guess is None:
            guess = classify(text)
        if guess is None or guess.task not in allowed:
            return None
        if policy.get("require_confident", True) and not guess.confident:
            return None

        cap = int(policy.get("max_per_day") or 0)
        if cap and self._autopilot_calls_today() >= cap:
            print(f"[era] [AUTOPILOT] daily cap of {cap} automatic call(s) reached.")
            return None

        decision = self.current_route(guess.task)
        if decision is None or decision.is_lane or decision.provider == "gemini":
            return None
        self._announce_autopilot(decision, guess)
        return decision

    def _announce_autopilot(self, decision: RouteDecision, guess: Any) -> None:
        """Say which model took an autopiloted request — once per session.

        Autopilot is the only path where a DIFFERENT model answers without the
        user asking for it. Silent substitution is precisely what the whole
        indicator exists to prevent, so the first time it happens Era says so
        out loud. Once, not every time: a note on every long question would be
        noise, and the indicator carries it from then on.
        """
        try:
            if not autopilot_policy().get("announce_first", True):
                return
            if getattr(self, "_autopilot_announced", False):
                return
            self._autopilot_announced = True
            note = (
                f"Switching to {decision.display_name} for this one — "
                f"it is better suited to {guess.task.replace('_', ' ')}."
            )
            self.ui.write_log(f"SYS: {note}")
            speak = getattr(self, "speak", None)
            if speak:
                speak(_say_exactly(note))
        except Exception as e:
            print(f"[era] [WARN] autopilot announcement skipped: {e}")

    def _autopilot_calls_today(self) -> int:
        return int(getattr(self, "_autopilot_count", 0))

    def _note_autopilot_call(self) -> None:
        self._autopilot_count = self._autopilot_calls_today() + 1

    def should_dispatch(self, text: str) -> RouteDecision | None:
        """Should this message be ANSWERED by a non-voice model?

        Every condition is a reason to say no, and the defaults all say no.
        There are two ways through, and `claude_dispatch.enabled` is the master
        switch for both — one flag turns everything off.

          PINNED   the user said "use Claude". Their explicit instruction, so
                   any non-CM message goes.
          CANARY   nobody pinned anything; the classifier decides. Off by
                   default, allowlist-gated, and confidence- and count-capped.
                   See autopilot_decision() for why each of those exists.

        Community-management orders are refused on BOTH paths: they belong to
        the era_dispatch lane, and handing "post the reel" to a tool-less oracle
        would produce a description of posting instead of a post. Note this asks
        `is_cm_order`, NOT `_infer_cm_task_type` — the latter answers "which CM
        lane", defaults to "weekly", and is therefore truthy for every string.
        """
        body = (text or "").strip()
        if not body:
            return None
        if not dispatch_policy().get("enabled"):
            return None

        try:
            if is_cm_order(body):
                return None
        except Exception:
            return None  # cannot prove it is NOT a CM order — do not take it

        if not self.model_override:
            # No pin: autopilot is the only remaining route, and it owns every
            # gate of its own. Counted at the DECISION rather than after the
            # call — over-counting a failed call errs toward the cap, which is
            # the safe direction for a spend limiter.
            decision = self.autopilot_decision(body)
            if decision is not None:
                self._note_autopilot_call()
            return decision

        decision = self.current_route()
        if decision is None or decision.is_lane:
            return None
        if decision.provider == "gemini":
            return None  # the live session already serves this; nothing to do
        return decision

    def _dispatch_system_blocks(self) -> tuple[PromptBlock, ...]:
        """What Claude is told about its own limits.

        It has no tools by design (see providers.claude_cli), so a request to DO
        something has to come back as a plain refusal the user can act on, not
        as a confident description of an action that never happened.
        """
        return (
            PromptBlock(
                text=(
                    "You are answering one question for Era, a desktop voice assistant, "
                    "on behalf of its user. Reply in at most four sentences, in plain "
                    "prose meant to be READ ALOUD — no markdown, no lists, no code "
                    "fences.\n"
                    "You cannot run tools, open apps, control the machine or access "
                    "files. If the user is asking for an ACTION rather than an answer, "
                    "say only that you cannot do it from here and that they should say "
                    "'back to automatic' and ask again."
                ),
                label="era.dispatch.system",
            ),
        )

    def run_dispatch(
        self,
        text: str,
        decision: RouteDecision,
        *,
        provider: Any = None,
    ) -> tuple[str, str]:
        """Call the routed model. Returns (spoken_text, error). Never raises.

        Blocking — the CLI is a subprocess. Callers on the event loop must hand
        this to a thread.
        """
        entry = decision.entry
        estimate = entry.call_overhead_usd if entry else 0.0
        affordable, why_not = self.spend_ledger.can_afford(estimate)
        if not affordable:
            return "", why_not

        if provider is None:
            try:
                from core.llm.adapters.claude_cli import ClaudeCliProvider

                provider = ClaudeCliProvider()
            except Exception as e:
                return "", f"the reasoning model is not reachable ({e})"

        request = LlmRequest(
            alias=decision.alias,
            messages=(LlmMessage.text("user", text),),
            system=self._dispatch_system_blocks(),
            timeout_s=float(dispatch_policy().get("timeout_s") or 45.0),
            request_id=f"dispatch-{int(time.time() * 1000)}",
        )
        try:
            response = provider.generate(request)
        except Exception as e:
            return "", f"{type(e).__name__}: {e}"

        # Record the REAL cost, not the estimate, so the ceiling tracks reality.
        try:
            self.spend_ledger.record(response.usage.est_cost_usd, alias=decision.alias)
        except Exception:
            pass

        answer = (response.text or "").strip()
        if not answer:
            return "", "the reasoning model returned nothing"
        return answer, ""

    def start_dispatch(self, text: str, decision: RouteDecision) -> None:
        """Run a dispatch off the caller's thread and speak the result.

        `_on_text_command` runs on the WebSocket handler thread, and the CLI
        call takes ~3s, so doing it inline would stall every other message from
        the UI for the duration. A plain worker thread rather than the event
        loop, because dispatch must keep working when there is no live session
        at all — a quota outage is exactly when a second opinion is useful.
        """

        def _worker() -> None:
            answer, error = self.run_dispatch(text, decision)
            if error:
                self.ui.write_log(f"SYS: {decision.alias} could not answer — {error}")
                try:
                    log_event(
                        main_logger,
                        "llm.dispatch.failed",
                        "WARNING",
                        alias=decision.alias,
                        error=error[:200],
                    )
                except Exception:
                    pass
                return
            self.speak_dispatched(answer)
            try:
                log_event(
                    main_logger,
                    "llm.dispatch.completed",
                    "INFO",
                    alias=decision.alias,
                    provider=decision.provider,
                    chars=len(answer),
                    day_total_usd=round(self.spend_ledger.today_total(), 4),
                )
            except Exception:
                pass

        threading.Thread(target=_worker, name="era-llm-dispatch", daemon=True).start()

    def speak_dispatched(self, answer: str) -> None:
        """Say another model's answer without letting the live session act on it.

        _sanitize_spoken + _say_exactly, together, are the whole reason this is
        a separate method: in gemini_audio mode speak() feeds text INTO the live
        session as conversational input, and that session holds Era's entire
        tool surface. Passing a second model's raw output through it unwrapped
        is a prompt-injection path with real actions on the end of it.
        """
        safe = _sanitize_spoken(answer)
        if not safe:
            return
        self.ui.write_log(f"era: {safe}")
        speak = getattr(self, "speak", None)
        if speak:
            speak(_say_exactly(safe))
