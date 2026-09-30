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

Mixed into HudBus (app/hud/events.py); relies on its lock, ``_emit`` and
``_poke``. Every method swallows its own errors (degrade-never-raise).
"""

from __future__ import annotations

import time
from typing import Any

from app.hud import tool_adapter
from app.hud.bus_const import APPROVAL_RECHECK_S


def _live_pending() -> Any:
    """The store's live confirmation, or None (never raises)."""
    try:
        from core.pending_confirmation import get_shared_store

        return get_shared_store().peek()
    except Exception:
        return None


class ApprovalMixin:
    _approval: dict | None = None

    def approval_public(self) -> dict | None:
        a = self._approval
        return None if a is None else {k: v for k, v in a.items() if not k.startswith("_")}

    def _approval_next_deadline(self) -> float | None:
        a = self._approval
        return None if a is None else a["_next"]

    def _approval_label(self) -> str:
        a = self._approval
        return "" if a is None else f"Approval required · {a['title']}"

    def _sync_approval(self) -> None:
        """Make the HUD's approval match the store. Call under the bus lock."""
        try:
            p = _live_pending()
            a = self._approval
            if a is not None and p is not None and p.confirmation_id == a["id"]:
                a["_next"] = time.monotonic() + APPROVAL_RECHECK_S
                return
            if a is not None:
                self._approval = None
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
                    "_next": time.monotonic() + APPROVAL_RECHECK_S,
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
