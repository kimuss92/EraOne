"""Era's session-scoped model preference, and the routing decision it feeds.

WHAT THIS OWNS
--------------
One piece of state — "the user asked for a specific model" — plus the two
things that read it: applying a spoken/typed override, and asking the router
which alias serves a request.

WHY THE OVERRIDE IS SESSION-SCOPED AND NOT PERSISTED
----------------------------------------------------
"Use Claude for this" is about the next few minutes, not forever. Writing it to
user_prefs would make a one-off instruction outlive the conversation it was
about, and the user would have no obvious way to discover why every request had
started going somewhere expensive. A relaunch is a clean slate; "back to
automatic" clears it sooner.

WHY IT DOES NOT SPEAK
---------------------
Applying an override must work with NO Gemini session — the same reason the
voice switch is intercepted before everything else. During a quota outage the
mic is dead and typing is the only route in, so this path acknowledges through
the UI log rather than through a model that may not be there.

WHAT MOVED OUT
--------------
Deciding WHO serves a request lives here; actually going and spending money
asking them lives in app/era_live/dispatch.py. Keeping the decision pure is what
lets every routing rule be tested without a network, a budget or a subprocess.
"""

from __future__ import annotations

from typing import Any

from app.text_intents import (
    MODEL_INTENT_AUTO,
)
from core.llm.registry import RegistryError, all_aliases, display_name, resolve
from core.llm.router import (
    RouteDecision,
    RoutingError,
    resolve_override,
    route,
)
from core.logger import log_event, main_logger

# Everything the routing layer can raise. RegistryError is in here because an
# unreadable or malformed config/llm.yaml surfaces from route() as a REGISTRY
# error, not a routing one — catching only RoutingError left a hole straight
# into the live audio loop, which is the one place that must never see an
# exception from a preference lookup.
_ROUTING_FAULTS = (RoutingError, RegistryError)


class ModelRouteMixin:
    """Holds the active model override and answers 'who should serve this?'."""

    _model_override: str | None = None

    # ── the override ──────────────────────────────────────────────────────

    @property
    def model_override(self) -> str | None:
        return getattr(self, "_model_override", None)

    def apply_model_override(self, token: str | None) -> str:
        """Set or clear the override. Returns the line to show the user.

        Never raises: this runs from a text-command handler, and a bad word
        should cost an explanation, not the handler.
        """
        if token == MODEL_INTENT_AUTO:
            previous = self.model_override
            self._model_override = None
            message = (
                f"Model routing back to automatic (was {previous})."
                if previous
                else "Model routing is already automatic."
            )
            self._log_override(None, previous, message)
            self.publish_route(self.current_route())
            self.publish_catalog()
            return message

        try:
            alias = resolve_override(token)
        except Exception as e:  # pragma: no cover - defensive
            return f"I couldn't work out which model you meant ({e})."

        if alias is None:
            return f"I don't know a model called {token!r}. Routing is unchanged."

        # Resolve it now so an unusable choice is reported HERE, at the moment
        # the user asks for it, rather than silently degrading on the next
        # request when they have forgotten they set it.
        try:
            decision = route(override=alias)
        except _ROUTING_FAULTS as e:
            return f"I can't route to {alias}: {e}"

        previous = self.model_override
        self._model_override = alias
        if decision.is_fallback:
            message = (
                f"{alias} isn't usable right now ({decision.fallback_reason}); "
                f"requests will go to {decision.alias} instead."
            )
        else:
            message = f"Model routing pinned to {decision.describe()}."
        self._log_override(alias, previous, message)
        self.publish_route(decision)
        self.publish_catalog()
        return message

    def select_model(self, alias: str | None) -> str:
        """Apply a choice made by CLICKING the switcher in the UI.

        Deliberately the same code path as the spoken and typed overrides —
        "use Claude" said out loud and a click on Claude in the dropdown are the
        same instruction, and giving the click its own logic is how the two
        would drift into disagreeing about what is pinned.

        An empty alias means automatic, which is what the switcher's first entry
        sends: it is the only way back off a pin without speaking.

        Never raises. This runs from a WebSocket message handler, where an
        exception would take the socket thread down and silently stop every
        other command the UI can send.
        """
        try:
            message = self.apply_model_override((alias or "").strip() or MODEL_INTENT_AUTO)
        except Exception as e:  # pragma: no cover - apply_model_override is itself guarded
            message = f"I couldn't switch models ({e})."

        # Echo into the transcript. The indicator changes on its own, but a
        # switch that leaves no trace in the log is one the user cannot find
        # again when they later wonder why answers started costing money.
        ui = getattr(self, "ui", None)
        writer = getattr(ui, "write_log", None)
        if writer is not None:
            try:
                writer(f"SYS: {message}")
            except Exception:
                pass
        return message

    # ── the catalogue behind the switcher ─────────────────────────────────

    def model_catalog(self) -> list[dict[str, Any]]:
        """Every alias the user may switch to, as plain data for the UI.

        Built from the registry rather than a hand-kept list, for the same
        reason display_name is: adding an alias to config/llm.yaml must not also
        require an edit in the frontend, or the switcher will quietly offer a
        model that no longer exists (or hide one that does).

        Unavailable aliases are INCLUDED, carrying their reason. Hiding them
        would leave the user wondering where era.cheap went; showing it greyed
        out with "the proxy it points at is unreachable" answers the question
        the absence would have raised.
        """
        out: list[dict[str, Any]] = []
        try:
            aliases = all_aliases()
        except Exception:
            return out  # unreadable registry costs the switcher, never the run

        for alias in aliases:
            try:
                entry = resolve(alias)
            except Exception:
                continue  # one malformed alias must not empty the whole menu
            caps = entry.capabilities
            out.append(
                {
                    "alias": alias,
                    "label": display_name(alias),
                    "detail": f"{entry.provider}/{entry.model}",
                    "role": entry.role,
                    "available": bool(caps.available),
                    "unavailable_reason": caps.unavailable_reason,
                    # Shown in the menu because pinning a claude_cli alias makes
                    # the next typed message a paid call. A switcher that hides
                    # that is a switcher that spends the user's money quietly.
                    "cost_per_call_usd": entry.call_overhead_usd,
                    "realtime_audio": bool(caps.realtime_audio),
                }
            )
        return out

    def publish_catalog(self) -> None:
        """Push the switcher's options and which one is pinned.

        Guarded exactly like publish_route: this draws a menu, and failing to
        draw a menu must never cost the request or the switch that triggered it.
        """
        ui = getattr(self, "ui", None)
        setter = getattr(ui, "set_model_catalog", None)
        if setter is None:
            return
        try:
            setter(self.model_catalog(), selected=self.model_override or "")
        except Exception as e:
            print(f"[era] [WARN] model switcher update skipped: {e}")

    def publish_model_state(self) -> None:
        """Publish BOTH the catalogue and the current decision.

        Called when a UI client connects. Without it the indicator sits on
        'resolving…' and the switcher is empty until the next request happens to
        route something — which, on a quiet session, can be a long time. The
        state exists on this side the whole while; a freshly opened window
        simply had no way to ask for it.

        Uses route() directly rather than current_route() on purpose: this is a
        redraw, not a request, and logging it as `llm.route.selected` would put
        decisions in the log that nothing ever asked for.
        """
        self.publish_catalog()
        try:
            decision = route(override=self.model_override)
        except Exception:
            decision = None
        self.publish_route(decision)

    def publish_route(self, decision: RouteDecision | None) -> None:
        """Push a routing decision to the UI indicator.

        Guarded end to end: the indicator is a status display, and failing to
        draw it must never cost the request it was describing. A decision of
        None (nothing could serve it) is published as an explicit 'unrouted'
        rather than left showing the previous model — a stale label claiming
        Claude is answering when nothing is would be worse than no label.
        """
        ui = getattr(self, "ui", None)
        setter = getattr(ui, "set_active_model", None)
        if setter is None:
            return
        try:
            if decision is None:
                setter("unrouted", "no model could serve this request", alias="")
                return
            setter(
                decision.display_name,
                decision.describe(),
                alias=decision.lane or decision.alias,
                fell_back_from=decision.fell_back_from or "",
                fallback_reason=decision.fallback_reason,
                pinned=bool(self.model_override),
            )
        except Exception as e:
            print(f"[era] [WARN] model indicator update skipped: {e}")

    def _log_override(self, alias: str | None, previous: str | None, message: str) -> None:
        try:
            log_event(
                main_logger,
                "llm.route.override",
                "INFO",
                alias=alias or "",
                previous=previous or "",
                message=message,
            )
        except Exception:
            pass  # a health/telemetry failure must never cost the switch

    # ── the decision ──────────────────────────────────────────────────────

    def current_route(self, task: str | None = None, **needs: bool) -> RouteDecision | None:
        """Which alias serves this request, honouring any override.

        Returns None when nothing can serve it, having logged why. Callers on
        the live path must treat None as "carry on with the voice session" —
        degrade, never raise, is the rule everywhere this is reachable from.
        """
        try:
            decision = route(task, override=self.model_override, **needs)
        except _ROUTING_FAULTS as e:
            try:
                log_event(
                    main_logger,
                    "llm.route.failed",
                    "WARNING",
                    task=task or "",
                    override=self.model_override or "",
                    error=str(e),
                )
            except Exception:
                pass
            return None

        # Every routing decision is logged, fallbacks at WARNING. This is what
        # makes "which model actually handled it?" answerable after the fact,
        # rather than something to guess from a bill.
        try:
            log_event(
                main_logger,
                "llm.route.selected",
                "WARNING" if decision.is_fallback else "INFO",
                **decision.to_log_fields(),
            )
        except Exception:
            pass
        self.publish_route(decision)
        return decision

    def active_model_label(self) -> str:
        """Short label for the UI indicator (phase 8).

        Falls back to the alias, then to a plain unknown marker — this feeds a
        status display and must always produce something printable.
        """
        try:
            decision = route(override=self.model_override)
        except _ROUTING_FAULTS:
            return self.model_override or "unrouted"
        except Exception:  # pragma: no cover - defensive
            return "unknown"
        if decision.is_lane:
            return decision.lane or "lane"
        return decision.alias or "unknown"
