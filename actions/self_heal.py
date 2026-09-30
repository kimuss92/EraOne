"""actions/self_heal.py — the `self_heal` tool: Era's view of her own healing loop.

The loop itself runs unattended (core/self_heal.py, started by main.py). This
tool lets the user see it, trigger it now, undo a fix, or change its mode.

    status    what has been fixed, what is in probation, what was given up on
    scan      code-located faults in era.log and whether each is eligible
    heal      fix the top eligible fault now (or the one matching `fingerprint`)
    rollback  undo the newest applied fix (or the one matching `fingerprint`)
    mode      off | propose | auto — turning it ON is gated in core/safety_guard.py
"""

from __future__ import annotations

from core import self_heal
from core import self_heal_incidents as incidents


def self_heal_tool(parameters: dict | None = None, player=None, speak=None) -> dict:
    p = parameters or {}
    action = str(p.get("action") or "status").lower().strip()
    fingerprint = str(p.get("fingerprint") or "").strip()

    if action == "status":
        return {"success": True, "message": self_heal.status()}
    if action == "scan":
        # Status replies say "failed"/"error" by nature — an explicit success
        # flag keeps ToolResult from classifying the report as a failed call.
        return {"success": True, "message": self_heal.scan_report()}
    if action == "mode":
        msg = self_heal.set_mode(str(p.get("mode") or ""))
        return {"success": msg.startswith("Self-healing is now"), "message": msg}
    if action == "rollback":
        return {"success": True, "message": self_heal.manual_rollback(fingerprint)}
    if action == "heal":
        found = incidents.scan()
        if fingerprint:
            found = [i for i in found if fingerprint in i.fingerprint]
        if not found:
            return {"success": True, "message": "No code-located fault to heal right now."}
        st = self_heal.load_state()
        eligible = [i for i in found if not self_heal.eligibility(i, st)]
        if not eligible:
            why = self_heal.eligibility(found[0], st)
            return {"success": True, "message": f"Nothing I may fix on my own yet: {why}."}
        return {"success": True, "message": self_heal.heal(eligible[0])}
    return {
        "success": False,
        "error_type": "UnknownAction",
        "message": f"Unknown self_heal action '{action}'. Use status, scan, heal, rollback or mode.",
    }
