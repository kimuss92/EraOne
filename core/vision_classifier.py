from __future__ import annotations

import json
import re
from typing import Any

ALLOWED_SCREEN_STATES = frozenset(
    {
        "desktop",
        "browser",
        "media_page",
        "document",
        "form",
        "error",
        "unknown",
    }
)

ALLOWED_ELEMENT_TYPES = frozenset(
    {
        "button",
        "input",
        "link",
        "text",
        "icon",
        "menu",
        "video",
        "image",
        "unknown",
    }
)

ALLOWED_RISKS = frozenset({"low", "medium", "high"})

ALLOWED_ACTIONS = frozenset({"none", "use_dom_first", "ask_user", "safe_click_possible"})

ALLOWED_ACTION_HINTS = ALLOWED_ACTIONS

HIGH_RISK_KEYWORDS: list[str] = [
    "payment",
    "pay",
    "password",
    "delete",
    "buy",
    "purchase",
    "submit",
    "sign in",
    "log in",
    "login",
    "logout",
    "reset",
    "uninstall",
    "remove",
    "format",
    "system settings",
    "sudo",
    "admin",
    "permanent",
    "paiement",
    "payer",
    "mot de passe",
    "supprimer",
    "acheter",
    "valider",
    "envoyer",
    "connexion",
    "déconnexion",
    "réinitialiser",
    "désinstaller",
    "effacer",
    "réglages système",
    "administrateur",
]

MEDIUM_RISK_KEYWORDS: list[str] = [
    "play",
    "pause",
    "download",
    "install",
    "update",
    "share",
    "send",
    "open",
    "close",
    "refresh",
    "mute",
    "unmute",
    "volume",
    "fullscreen",
    "lire",
    "pause",
    "télécharger",
    "installer",
    "mise à jour",
    "partager",
    "envoyer",
    "ouvrir",
    "fermer",
    "actualiser",
    "couper le son",
    "plein écran",
]

DOM_FIRST_STATES = frozenset({"browser", "media_page"})

DOM_FIRST_ELEMENT_TYPES = frozenset({"button", "link", "input"})


def build_structured_vision_prompt(user_prompt: str, mode: str, target: str) -> str:
    return (
        "Return ONLY valid JSON with this exact structure "
        "(no markdown, no code fences, no extra text before or after):\n"
        '{"screen_state":"...","app_name":"...","page_type":"...",'
        '"summary":"...","elements":[...],'
        '"recommended_action":"...","needs_confirmation":false}\n\n'
        "screen_state must be one of: desktop, browser, media_page, document, form, error, unknown\n"
        "app_name: name of the application or website visible\n"
        "page_type: type of page (e.g. home, search, video, settings, checkout)\n"
        "summary: 1 sentence describing what is visible\n"
        'elements: array of {"label":"...","type":"button|input|link|text|icon|menu|video|image|unknown",'
        '"bbox":[x,y,w,h],"confidence":0.0-1.0,"risk":"low|medium|high",'
        '"action_hint":"none|use_dom_first|ask_user|safe_click_possible"}\n'
        "recommended_action: one of none, use_dom_first, ask_user, safe_click_possible\n"
        "needs_confirmation: true if any element is high risk or action needs user confirmation\n\n"
        "List at most 8 elements: only the ones most relevant to the user question.\n"
        "If no interactive elements are visible, return elements as empty array.\n"
        "If the screen shows a browser or media page, set recommended_action to use_dom_first.\n\n"
        f"User question: {user_prompt}"
    )


def _normalize_bbox(bbox: Any) -> list[int]:
    if not isinstance(bbox, (list, tuple)):
        return []
    if len(bbox) != 4:
        return []
    try:
        return [int(v) for v in bbox]
    except (ValueError, TypeError):
        return []


def _normalize_confidence(val: Any) -> float:
    try:
        v = float(val)
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v
    except (ValueError, TypeError):
        return 0.0


def _classify_element_risk(label: str, el_type: str) -> str:
    label_lower = label.lower().strip()
    for kw in HIGH_RISK_KEYWORDS:
        if kw in label_lower:
            return "high"
    for kw in MEDIUM_RISK_KEYWORDS:
        if kw in label_lower:
            return "medium"
    return "low"


def _normalize_element(el_dict: Any) -> dict:
    if not isinstance(el_dict, dict):
        el_dict = {}
    label = (
        str(el_dict.get("label", ""))
        if not isinstance(el_dict.get("label"), str)
        else el_dict["label"]
    )
    el_type = el_dict.get("type", "unknown")
    if el_type not in ALLOWED_ELEMENT_TYPES:
        el_type = "unknown"
    bbox = _normalize_bbox(el_dict.get("bbox", []))
    confidence = _normalize_confidence(el_dict.get("confidence", 0.0))
    risk = el_dict.get("risk", "low")
    if risk not in ALLOWED_RISKS:
        risk = _classify_element_risk(label, el_type)
    action_hint = el_dict.get("action_hint", "none")
    if action_hint not in ALLOWED_ACTION_HINTS:
        action_hint = "none"
    return {
        "label": label,
        "type": el_type,
        "bbox": bbox,
        "confidence": confidence,
        "risk": risk,
        "action_hint": action_hint,
    }


def _apply_dom_first(parsed: dict) -> dict:
    result = dict(parsed)
    screen_state = result.get("screen_state", "unknown")
    if screen_state in DOM_FIRST_STATES:
        current_action = result.get("recommended_action", "none")
        if current_action != "ask_user":
            result["recommended_action"] = "use_dom_first"
        elements = result.get("elements", [])
        for el in elements:
            if isinstance(el.get("action_hint"), str) and el["action_hint"] not in (
                "ask_user",
                "use_dom_first",
            ):
                if el.get("type") in DOM_FIRST_ELEMENT_TYPES:
                    el["action_hint"] = "use_dom_first"
    return result


def _compute_needs_confirmation(parsed: dict) -> bool:
    if parsed.get("recommended_action") == "ask_user":
        return True
    for el in parsed.get("elements", []):
        if el.get("risk") == "high":
            return True
    screen_state = parsed.get("screen_state", "")
    if screen_state == "form":
        for el in parsed.get("elements", []):
            if el.get("type") == "input" and el.get("risk") in ("medium", "high"):
                return True
    return False


def parse_structured_vision_response(raw: str) -> dict:
    fallback = {
        "ok": True,
        "screen_state": "unknown",
        "app_name": "",
        "page_type": "",
        "summary": raw[:800] if raw else "",
        "elements": [],
        "recommended_action": "none",
        "needs_confirmation": False,
    }
    if not raw or not raw.strip():
        result = dict(fallback)
        result["raw_text"] = ""
        return result

    text = raw.strip()

    json_str = None

    code_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_match:
        json_str = code_match.group(1).strip()
    else:
        if "{" in text and "}" in text:
            start = text.index("{")
            end = text.rindex("}") + 1
            json_str = text[start:end]

    if not json_str:
        result = dict(fallback)
        result["raw_text"] = raw[:800]
        return result

    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        result = dict(fallback)
        result["raw_text"] = raw[:800]
        return result

    if not isinstance(data, dict):
        result = dict(fallback)
        result["raw_text"] = raw[:800]
        return result

    try:
        screen_state = data.get("screen_state", "unknown")
        if screen_state not in ALLOWED_SCREEN_STATES:
            screen_state = "unknown"

        summary = str(data.get("summary", ""))[:800] if data.get("summary") else ""
        app_name = str(data.get("app_name", "")) if data.get("app_name") else ""
        page_type = str(data.get("page_type", "")) if data.get("page_type") else ""

        recommended_action = data.get("recommended_action", "none")
        if recommended_action not in ALLOWED_ACTIONS:
            recommended_action = "none"

        elements_raw = data.get("elements", [])
        if not isinstance(elements_raw, list):
            elements_raw = []

        gemini_risk_map: dict[int, str] = {}
        normalized_elements: list[dict] = []
        for i, el in enumerate(elements_raw):
            normalized = _normalize_element(el)
            gemini_risk = el.get("risk", "low") if isinstance(el, dict) else "low"
            if isinstance(gemini_risk, str) and gemini_risk in ALLOWED_RISKS:
                gemini_risk_map[i] = gemini_risk
            if gemini_risk_map.get(i) == "high":
                normalized["risk"] = "high"
            else:
                local_risk = _classify_element_risk(normalized["label"], normalized["type"])
                if gemini_risk_map.get(i) == "medium" and local_risk == "low":
                    pass
                elif gemini_risk_map.get(i) == "medium":
                    pass
                else:
                    if normalized["risk"] != "high":
                        if gemini_risk_map.get(i) == "medium":
                            normalized["risk"] = "medium"
                        elif local_risk == "high":
                            normalized["risk"] = "high"
                        elif local_risk == "medium" and normalized["risk"] == "low":
                            normalized["risk"] = "medium"
            normalized_elements.append(normalized)

        parsed: dict = {
            "ok": True,
            "screen_state": screen_state,
            "app_name": app_name,
            "page_type": page_type,
            "summary": summary,
            "elements": normalized_elements,
            "recommended_action": recommended_action,
            "needs_confirmation": False,
            "raw_text": raw[:800],
        }

        parsed = _apply_dom_first(parsed)
        parsed["needs_confirmation"] = _compute_needs_confirmation(parsed)

        for i, el in enumerate(parsed.get("elements", [])):
            gemini_risk = gemini_risk_map.get(i)
            if gemini_risk == "high":
                el["risk"] = "high"

        return parsed

    except Exception:
        result = dict(fallback)
        result["raw_text"] = raw[:800]
        return result
