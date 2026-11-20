"""Completion reporting: render "Era owes you" reports from event bus events.

Era Fabric F4. Subscribes to the event bus, renders completion reports from
events (not prose), and integrates with report_outbox, self_heal, and lessons.

Design: docs/era-enhancement/ERA_FABRIC_DESIGN.md sections 6, 9.4, 9.5
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from core.fabric import events as ev
from core.report_outbox import enqueue, mark_delivered, mark_undelivered, pending
from core.self_healing_router import record_tool_result
from core.task_lessons import observe_async

# ── Topic filter for completion events ──────────────────────────────────────
# We care about task completions, verifications, and delivery events that signal
# "Era owes the owner a report".
COMPLETION_TOPICS = frozenset(
    {
        "task.done",
        "dev.verified",
        "crew.result",
        "report.owed",
        "report.delivered",
    }
)

# ── Report templates: rendered from events, never hand-crafted prose ──────────
# Each template extracts fields from an Event and formats them into a concise
# spoken report. The template is the only place prose appears — everything else
# is data-driven from the event.

_terminal_faults = frozenset({"Denied", "NeedsConfirmation", "Cancelled"})


@dataclass
class ReportTemplate:
    """A template that renders a spoken report from an Event."""

    # A short natural-language fragment describing what the event was about.
    # This is derived from the event data, not hand-written.
    summary_template: str

    # Whether this report should include a "LIVE HEALTH" paragraph when something
    # is down (for capability_check integration).
    include_health: bool = False

    # How to determine if a "LIVE HEALTH" paragraph is needed.
    health_check: str | None = None  # e.g. "crew.hermes" → check health of crew.hermes


def _render_health_paragraph(component: str | None) -> str | None:
    """Return a LIVE HEALTH paragraph if a component is open, or None."""
    if not component:
        return None
    try:
        from core.fabric import health as hl

        state = hl.state(component)
        if state == hl.OPEN:
            # Return a brief health line; the full paragraph is assembled by the
            # caller from ci.check() logic.
            return f"the {component} is down"
    except Exception:
        pass
    return None


def _extract_summary(event: ev.Event) -> str:
    """Extract a short summary from an event, preferring the event summary then
    falling back to the first line of the component name or topic."""
    if event.summary:
        return event.summary
    # Fallback: derive from topic or component
    topic = event.topic or ""
    # Strip the prefix to get the action
    m = re.match(r"\w+\.", topic)
    if m:
        return topic[m.end() :].replace("_", " ")[:120]
    return str(event.component or "")[:120]


def _extract_fault(event: ev.Event) -> str | None:
    """Extract fault kind from event if it's a terminal fault."""
    fault = event.fault
    if not fault:
        return None
    kind = fault.get("kind")
    if kind in _terminal_faults:
        return kind
    return None


def _extract_provenance(event: ev.Event) -> dict:
    """Extract provenance dict from event for the report."""
    prov = event.provenance or {}
    # Ensure we have the essentials
    if "capability" not in prov and event.topic:
        prov["capability"] = event.topic
    return prov


def _build_report(event: ev.Event) -> str:
    """Build a concise spoken report from an event.

    Rules (in order):
    1. Start with the event summary (derived from the event data).
    2. If the event is a terminal fault, prepend the fault kind.
    3. If LIVE HEALTH is warranted, append a health line.
    4. Append provenance info (provider, fallback trail).
    5. Cap at ~140 spoken chars.
    """
    lines: list[str] = []

    # Step 1: summary
    summary = _extract_summary(event)
    if summary:
        lines.append(summary)

    # Step 2: fault kind
    fault_kind = _extract_fault(event)
    if fault_kind:
        lines.insert(0, fault_kind)

    # Step 3: health paragraph
    component = event.component or ""
    health_line = _render_health_paragraph(component)
    if health_line:
        lines.append(health_line)

    # Step 4: provenance
    prov = _extract_provenance(event)
    prov_parts: list[str] = []
    if prov.get("provider"):
        prov_parts.append(f"via {prov['provider']}")
    if prov.get("fallback_from"):
        prov_parts.append(f"after {prov['fallback_from']}")
    if prov_parts:
        lines.append("(" + ", ".join(prov_parts) + ")")

    # Step 5: cap and join
    text = " ".join(lines)
    # Cap at ~140 spoken characters (roughly 20-25 words)
    if len(text) > 140:
        text = text[:137] + "..."
    return text


# ── Subscriber: render reports from completion events ────────────────────────

_report_templates: dict[str, ReportTemplate] = {}


def _register_template(topic: str, template: ReportTemplate) -> None:
    """Register a report template for a given event topic."""
    _report_templates[topic] = template


def _unsubscribe_from_bus() -> None:
    """Remove all our bus subscribers. Called on unload."""
    for topic in COMPLETION_TOPICS:
        try:
            ev.unsubscribe(topic)
        except Exception:
            pass


def _subscribe_to_bus() -> None:
    """Subscribe to completion events on the fabric event bus."""

    def _make_handler(template: ReportTemplate) -> Callable[[ev.Event], None]:
        def _handler(event: ev.Event) -> None:
            # Only handle topics we care about
            if event.topic not in COMPLETION_TOPICS:
                return

            # Build the report text
            report_text = _build_report(event)

            # Only enqueue if we have meaningful text
            if not report_text or not report_text.strip():
                return

            # Enqueue in the report outbox (idempotent by key)
            # Use a key derived from the event for dedup
            key = f"comp:{event.topic}:{event.ctx_id or 'noctx'}"
            enqueue(key=key, text=report_text, source="fabric.f4.completion")

            # Also record as a tool result for self_heal / lessons
            try:
                record_tool_result(
                    tool=event.topic,
                    parameters={},
                    result=report_text,
                )
            except Exception:
                pass

            # Also enqueue as a lesson candidate
            try:
                observe_async(
                    user_text="",
                    tool=event.topic,
                    args={},
                    ok=bool(report_text.strip()),
                    error_type=None,
                    result=report_text,
                    verification=None,
                    safety=None,
                )
            except Exception:
                pass

        return _handler

    for topic in COMPLETION_TOPICS:
        template = ReportTemplate(
            summary_template="",
            include_health=False,
        )
        _register_template(topic, template)
        # Subscribe with a glob pattern that matches exactly
        ev.subscribe(topic, _make_handler(template))


# ── Auto-subscribe on import ───────────────────────────────────────────────

# We subscribe when this module is imported. The subscriber is registered once.
# It will be un subscribed when the module is reloaded or the process exits.
_subscribe_to_bus()


# ── Public API ─────────────────────────────────────────────────────────────


def render_from_event(event: ev.Event) -> str | None:
    """Render a completion report from a single event, bypassing the bus subscriber.

    Used for testing or one-off inspection.
    """
    if event.topic not in COMPLETION_TOPICS:
        return None
    return _build_report(event)


def get_pending_reports() -> list[dict]:
    """Return undelivered reports from the outbox, oldest first."""
    return pending()


def mark_delivered_report(key: str) -> None:
    """Mark a pending report as delivered."""
    mark_delivered(key)


def mark_undelivered_report(key: str) -> None:
    """Mark a delivered report as undelivered (e.g. after a session drop)."""
    mark_undelivered(key)


# ── Export for bus subscriber wiring ────────────────────────────────────────
#
# The subscriber handlers are registered above. The _subscribe_to_bus() call above
# sets up the bus subscriptions. Each handler enqueues a report into the outbox,
# records it as a tool result, and enqueues it as a lesson candidate.
#
# --- End of completion.py ---
