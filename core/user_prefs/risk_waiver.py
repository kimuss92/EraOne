"""Standing per-CATEGORY waivers for high-risk confirmation ("stop asking before shutdown").

The user chose per-category over one global switch (2026-09-18): a single "skip every
HIGH confirmation" flag would also cover deleting files, killing processes, running
generated code and sending mail — the actions a prompt-injected email or web page asks
for. So only the categories listed in WAIVABLE can be waived, each on its own, and
everything else keeps its gate whatever the user says here.

Invariants:
  * Fails CLOSED: any read error means "not waived".
  * Granting is confirmation-gated in safety_guard (manage_preference branch);
    revoking is free.
  * Honoured ONLY for the live user's own direct call (safety_guard
    classify_action(honor_risk_waivers=True) from tool_dispatch, and never for a call
    the router rewrote). Scheduled tasks and the autonomous agent keep their own gates:
    a waiver must not turn an injected scheduled task into an unattended shutdown.
  * Granting needs the user's own words naming the category (tool_dispatch), on top
    of the confirmation gate.
  * A waiver removes the CONFIRMATION prompt only. The action itself is unchanged:
    shutdown/restart still use the 20-second countdown that "cancel the shutdown" aborts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import core.user_prefs as _store

# category -> the normalised action names it covers.
#   shutdown / restart: computer_settings ACTION_MAP entries. sleep / log-off / lock were
#     dropped after review — they covered nothing real, only gmail_control's "sign_out" by
#     name collision.
#   close: window_control's "close" (routed from "close_window"). Added 2026-09-19 at the
#     owner's explicit request as a DELIBERATE downgrade: window close is only MEDIUM, but
#     it is not cleanly reversible — closing an app can lose unsaved work — so it is gated
#     by default. This is broader than the per-app close waiver (is_close_confirmation_waived),
#     which limits the blast radius to apps the user named one by one; this category lifts the
#     prompt for EVERY window close. The narrower per-app waiver remains the recommended path.
WAIVABLE: dict[str, frozenset[str]] = {
    "shutdown": frozenset({"shutdown", "shut_down", "shutdown_computer", "power_off", "poweroff"}),
    "restart": frozenset({"restart", "reboot", "restart_computer"}),
    "close": frozenset({"close", "close_window"}),
}

# Named so the model can tell the user WHY, instead of silently failing.
NOT_WAIVABLE = (
    "deleting or overwriting files",
    "force-killing processes",
    "running generated code or self-modification",
    "sending email or messages",
    "sensitive browser forms (payment, login)",
    "deleting memories",
)

_ALIASES = {
    "shut down": "shutdown",
    "power off": "shutdown",
    "power": "shutdown",
    "reboot": "restart",
    "close window": "close",
    "close windows": "close",
    "closing windows": "close",
    "window close": "close",
    "fermer": "close",
    "fermeture": "close",
}


def normalize_category(category: str) -> str:
    key = (category or "").strip().lower().replace("-", " ")
    key = _ALIASES.get(key, key).replace(" ", "_")
    return key if key in WAIVABLE else ""


def category_for_action(action: str) -> str:
    """The waivable category an action name belongs to, or ''."""
    action = (action or "").strip().lower()
    for category, actions in WAIVABLE.items():
        if action in actions:
            return category
    return ""


def set_risk_confirmation_waiver(category: str, waived: bool) -> str:
    cat = normalize_category(category)
    if not cat:
        return (
            f"'{category}' can't have its confirmation waived. Waivable: "
            f"{', '.join(WAIVABLE)}. Always confirmed: {'; '.join(NOT_WAIVABLE)}."
        )
    with _store._LOCK:
        data = _store._load_unlocked()
        waivers = data.setdefault("risk_confirmation_waived", {})
        waivers[cat] = {"active": bool(waived), "changed_at": datetime.now(UTC).isoformat()}
        _store._save(data)
    label = cat.replace("_", " ")
    if waived:
        return (
            f"Understood — I'll {label} without asking for confirmation from now on. "
            "The 20-second countdown still runs — say 'cancel the shutdown' to stop it."
        )
    return f"Understood — {label} will require your confirmation again."


def is_risk_confirmation_waived(category: str) -> bool:
    """Fails CLOSED: a corrupt prefs file keeps the confirm-first behaviour."""
    try:
        cat = normalize_category(category)
        if not cat:
            return False
        with _store._LOCK:
            entry = (_store._load_unlocked().get("risk_confirmation_waived") or {}).get(cat)
        return isinstance(entry, dict) and entry.get("active") is True
    except Exception:
        return False


def list_risk_waivers() -> list[str]:
    return [c for c in WAIVABLE if is_risk_confirmation_waived(c)]
