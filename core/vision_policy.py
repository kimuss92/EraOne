from __future__ import annotations

from core.vision_types import VisionResult

DOM_FIRST_STATES = {"browser", "media_page"}

MUTATION_TOOLS = {"computer_control", "browser_control"}

MUTATION_ACTIONS = {"click", "type", "press", "fill_form", "smart_click", "hotkey"}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _max_risk(a: str, b: str) -> str:
    if RISK_ORDER.get(a, 0) >= RISK_ORDER.get(b, 0):
        return a
    return b


def _is_mutation_action(intended_action: dict | None) -> bool:
    if not intended_action:
        return False
    tool = (intended_action.get("tool") or "").lower()
    action = (intended_action.get("action") or "").lower()
    return tool in MUTATION_TOOLS and action in MUTATION_ACTIONS


def evaluate_vision_recommendation(
    result: VisionResult | None,
    intended_action: dict | None = None,
) -> dict:
    """Evaluate a vision result and return a risk classification dict.

    Args:
        result: The vision analysis result, or None if unavailable.
        intended_action: The action the system plans to take, if any.

    Returns:
        Dict with keys: allow_action, requires_confirmation, recommended_path,
        reason, risk_level.
    """
    if result is None:
        return {
            "allow_action": False,
            "requires_confirmation": True,
            "recommended_path": "ask_user",
            "reason": "No vision result available — cannot assess risk.",
            "risk_level": "medium",
        }

    allow_action = True
    requires_confirmation = False
    recommended_path = "safe_to_describe"
    reason_parts = []
    risk_level = "low"

    if not result.ok:
        allow_action = False
        recommended_path = "no_action"
        reason_parts.append("Vision analysis did not complete successfully.")
        return {
            "allow_action": allow_action,
            "requires_confirmation": requires_confirmation,
            "recommended_path": recommended_path,
            "reason": " ".join(reason_parts),
            "risk_level": risk_level,
        }

    # Determine overall risk from elements
    if result.elements:
        for el in result.elements:
            el_risk = (el.risk or "low").lower()
            risk_level = _max_risk(risk_level, el_risk)

    # Check needs_confirmation
    if result.needs_confirmation:
        requires_confirmation = True
        recommended_path = "ask_user"
        risk_level = _max_risk(risk_level, "medium")
        reason_parts.append("Vision recommends asking user before proceeding.")

    # High-risk elements force confirmation
    if risk_level == "high":
        requires_confirmation = True
        if not reason_parts:
            reason_parts.append("High-risk element detected on screen.")
        else:
            reason_parts.append("High-risk element detected.")

    # Error state
    if result.screen_state == "error":
        allow_action = False
        recommended_path = "ask_user"
        risk_level = _max_risk(risk_level, "high")
        reason_parts.append("Screen shows an error state — recommend asking user.")

    # DOM-first state + mutation → recommend DOM
    is_mutation = _is_mutation_action(intended_action)
    if result.screen_state in DOM_FIRST_STATES:
        if is_mutation and intended_action.get("tool") == "computer_control":
            allow_action = False
            recommended_path = "use_dom_first"
            risk_level = _max_risk(risk_level, "medium")
            reason_parts.append(
                "Browser screen state detected — prefer DOM/CDP tools over coordinate clicks."
            )
        elif result.recommended_action == "use_dom_first" and is_mutation:
            allow_action = False
            recommended_path = "use_dom_first"
            risk_level = _max_risk(risk_level, "medium")
            reason_parts.append(
                "Vision recommends DOM-first approach — avoid coordinate-based interaction."
            )
        elif result.recommended_action == "use_dom_first":
            recommended_path = "use_dom_first"
            reason_parts.append("Recommended action indicates DOM-first approach.")

    # Desktop + all low + no mutation → safe to describe
    if result.screen_state == "desktop" and risk_level == "low" and not is_mutation:
        recommended_path = "safe_to_describe"
        if not reason_parts:
            reason_parts.append("Safe to describe — desktop view with no visible risk.")

    # Default fallback for safe description
    if not reason_parts:
        if not result.elements:
            reason_parts.append("No visible elements detected — safe to describe.")
        else:
            reason_parts.append("No risk concerns detected.")

    return {
        "allow_action": allow_action,
        "requires_confirmation": requires_confirmation,
        "recommended_path": recommended_path,
        "reason": " ".join(reason_parts),
        "risk_level": risk_level,
    }
