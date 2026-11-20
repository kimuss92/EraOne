"""Approval half of the HUD bus: mirrors Era's single pending confirmation.

The ``awaiting_approval`` state is never inferred from wording. It opens only
when a tool result is classified ``awaiting`` AND the process-wide
PendingConfirmationStore really holds a live confirmation, and it closes as soon
as the store no longer holds that one (confirmed, cancelled, expired, replaced).
While open, the bus re-reads the store every APPROVAL_RECHECK_S so a resolution
that happens on another path can never leave the HUD asking.

Only what the user needs to decide is published: the tool, its safe one-line
description (tool_adapter.describe — the same clipped/masked text the Activity
panel shows), the gate's reason, the risk level and the expiry. Raw parameters
never leave the backend.

The on-screen card clears itself after APPROVAL_VISIBLE_S. That is display only:
the confirmation stays armed in the store, so the user's own spoken or typed
answer still resolves it, and the bus keeps tracking it until the store drops it.

Mixed into HudBus (app/hud/events.py); relies on its lock, ``_emit`` and
``_poke``. Every method swallows its own errors (degrade-never-raise).
"""

from __future__ import annotations

import time
from typing import Any

from app.hud import tool_adapter
from app.hud.bus_const import APPROVAL_RECHECK_S

# How long the Action Required card stays on screen before it clears itself.
APPROVAL_VISIBLE_S = 15.0


def _live_pending() -> Any:
    """The store's live confirmation, or None (never raises)."""
    try:
        from core.pending_confirmation import get_shared_store

        return get_shared_store().peek()
    except Exception:
        return None


# How a resolved confirmation settles the Live Tasks row that asked for it. Without
# this the row kept its NEEDS APPROVAL badge forever after a yes, a no or an expiry
# (2026-10-16), so the panel contradicted both the header and Era.
_SETTLE = {
    "consumed": ("success", "Approved."),
    "cancelled": ("cancelled", "Declined — nothing was done."),
    "expired": ("cancelled", "Approval expired — nothing was done."),
    "replaced": ("cancelled", "Superseded by a newer request."),
}
_SETTLE_UNKNOWN = ("cancelled", "No longer waiting for approval.")


class ApprovalMixin:
    _approval: dict | None = None
    _confirmation_outcomes: dict[str, str] | None = None
    _confirmation_unsubscribe: Any = None

    def _watch_confirmations(self) -> None:
        """Subscribe once to the store, so a resolution settles the HUD at once
        instead of at the next APPROVAL_RECHECK_S re-read (never raises)."""
        if self._confirmation_unsubscribe is not None:
            return
        try:
            from core.pending_confirmation import get_shared_store

            self._confirmation_unsubscribe = get_shared_store().subscribe(
                self._on_confirmation_event
            )
        except Exception:
            pass

    def _on_confirmation_event(self, event: dict) -> None:
        """Store lifecycle callback; runs outside the store's lock."""
        try:
            state = event.get("state")
            if state == "stored":
                return
            with self._lock:
                outcomes = self._confirmation_outcomes
                if outcomes is None:
                    outcomes = self._confirmation_outcomes = {}
                outcomes[str(event.get("confirmation_id") or "")] = str(state)
                while len(outcomes) > 32:
                    outcomes.pop(next(iter(outcomes)))
                self._sync_approval()
                self._poke()
        except Exception:
            pass

    def _settle_awaiting_runs(self, live_id: str | None) -> None:
        """Close every NEEDS APPROVAL row whose confirmation is no longer live.

        Isolated: a fault here must never stop _sync_approval mirroring the store."""
        try:
            self._settle_awaiting_runs_unsafe(live_id)
        except Exception:
            pass

    def _settle_awaiting_runs_unsafe(self, live_id: str | None) -> None:
        outcomes = self._confirmation_outcomes or {}
        for cid, run in getattr(self, "_tools", {}).items():
            conf = run.get("_confirmation_id")
            if run.get("status") != "awaiting" or not conf or conf == live_id:
                continue
            status, summary = _SETTLE.get(outcomes.get(conf, ""), _SETTLE_UNKNOWN)
            run.update(status=status, summary=summary, finishedAt=time.time())
            self._emit(
                "tool.completed" if status == "success" else "tool.cancelled",
                toolName=run["tool"],
                toolCallId=cid,
                label=run["label"],
                message=summary,
                metadata=self._public_run(run),
            )

    def approval_public(self) -> dict | None:
        a = self._approval
        if a is None or a.get("_hidden"):
            return None
        return {k: v for k, v in a.items() if not k.startswith("_")}

    def _approval_next_deadline(self) -> float | None:
        a = self._approval
        if a is None:
            return None
        if a.get("_hidden"):
            return a["_next"]
        return min(a["_next"], a["_hide_at"])

    def _approval_label(self) -> str:
        a = self._approval
        if a is None or a.get("_hidden"):
            return ""
        return f"Approval required · {a['title']}"

    def _sync_approval(self) -> None:
        """Make the HUD's approval match the store. Call under the bus lock."""
        try:
            p = _live_pending()
            self._settle_awaiting_runs(p.confirmation_id if p is not None else None)
            a = self._approval
            now = time.monotonic()
            if a is not None and p is not None and p.confirmation_id == a["id"]:
                a["_next"] = now + APPROVAL_RECHECK_S
                if not a.get("_hidden") and now >= a["_hide_at"]:
                    # Display timeout only: the store still holds the confirmation.
                    a["_hidden"] = True
                    self._emit(
                        "approval.resolved",
                        toolName=a["tool"],
                        metadata={"id": a["id"], "reason": "display_timeout"},
                    )
                return
            if a is not None:
                self._approval = None
                if not a.get("_hidden"):
                    self._emit("approval.resolved", toolName=a["tool"], metadata={"id": a["id"]})
            if p is not None:
                d = tool_adapter.describe(p.tool, p.parameters)
                self._approval = {
                    "id": p.confirmation_id,
                    "tool": p.tool,
                    "label": d["label"],
                    "title": d["activity"],
                    "category": d["category"],
                    "reason": str(p.reason or "")[:240],
                    "risk": str(p.risk_level or "").upper(),
                    "createdAt": p.created_at,
                    "expiresAt": p.expires_at,
                    "_next": now + APPROVAL_RECHECK_S,
                    "_hide_at": now + APPROVAL_VISIBLE_S,
                    "_hidden": False,
                }
                self._emit(
                    "approval.requested",
                    toolName=p.tool,
                    label=self._approval["title"],
                    message=self._approval["reason"],
                    metadata=self.approval_public(),
                )
        except Exception:
            pass
