"""Parse HUD requests into the canonical core.visual_engine.apply contract."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from core.visual_engine import PANELS

FEATURE = {
    "id": "hud_panel_visibility",
    "description": (
        "Parse voice requests to hide or show an Era One HUD panel and emit "
        "a visual_engine command with a top-level panel_id."
    ),
}

Command = dict[str, Any]
Dispatcher = Callable[[Command], Any]
VISIBILITY_COMMAND_TYPE = "panel"

# These semantic IDs are owned by the visual_engine boundary and remain stable
# if a React component name or layout position changes.
_HIDE_PHRASES = (
    "turn off",
    "switch off",
    "get rid of",
    "hide",
    "close",
    "dismiss",
    "remove",
    "collapse",
    "conceal",
    "minimize",
    "minimise",
)
_SHOW_PHRASES = (
    "turn on",
    "switch on",
    "bring back",
    "show",
    "open",
    "display",
    "reveal",
    "restore",
    "expand",
)
_LIST_RE = re.compile(
    r"^(?:(?:list|name|show me|what are) (?:the )?(?:available |known |supported )?(?:hud )?panels|"
    r"(?:available|known|supported) (?:hud )?panels)$"
)


_registered_dispatcher: Dispatcher | None = None


def register_visual_dispatcher(dispatcher: Dispatcher | None) -> None:
    """Register the visual_engine parent's dispatcher, or clear it with None."""
    if dispatcher is not None and not callable(dispatcher):
        raise TypeError("visual dispatcher must be callable or None")
    global _registered_dispatcher
    _registered_dispatcher = dispatcher


def panel_catalog() -> list[dict[str, Any]]:
    """Return a fresh, JSON-safe copy of the supported panel catalog."""
    return [
        {
            "id": panel_id,
            "label": str(spec["label"]),
            "aliases": list(spec["aliases"]),
        }
        for panel_id, spec in PANELS.items()
    ]


def _normalise(text: str) -> str:
    """Normalize STT punctuation without changing meaningful word order."""
    text = text.casefold().replace("’", "'").replace("`", "'")
    text = re.sub(r"[-_/]", " ", text)
    text = re.sub(r"[^\w\s']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _phrase_pattern(phrases: tuple[str, ...]) -> str:
    return "|".join(re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True))


_ACTION_PATTERN = _phrase_pattern(_HIDE_PHRASES + _SHOW_PHRASES)
_NEGATED_ACTION_RE = re.compile(
    rf"\b(?:do not|don't|dont|never|not to)\s+(?:please\s+)?"
    rf"(?:{_ACTION_PATTERN})(?!\w)"
)


def _matched_actions(text: str) -> set[bool]:
    actions: set[bool] = set()
    if any(_contains_phrase(text, phrase) for phrase in _HIDE_PHRASES):
        actions.add(False)
    if any(_contains_phrase(text, phrase) for phrase in _SHOW_PHRASES):
        actions.add(True)
    return actions


def _matched_panels(text: str) -> list[str]:
    matches = []
    remaining = text
    aliases = sorted(
        ((alias, panel_id) for panel_id, spec in PANELS.items() for alias in spec["aliases"]),
        key=lambda row: len(row[0]),
        reverse=True,
    )
    for alias, panel_id in aliases:
        pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
        if re.search(pattern, remaining):
            if panel_id not in matches:
                matches.append(panel_id)
            remaining = re.sub(pattern, " ", remaining)
    return matches


def _visibility_command(panel_id: str, visible: bool) -> Command:
    """Build the exact visual_engine wire object with top-level panel_id."""
    command: Command = {
        "action": VISIBILITY_COMMAND_TYPE,
        "panel_id": panel_id,
        "visible": visible,
    }
    _validate_visibility_command(command)
    return command


def _validate_visibility_command(command: Mapping[str, Any]) -> None:
    """Fail closed if an internal change violates the dispatch protocol."""
    if command.get("action") != VISIBILITY_COMMAND_TYPE:
        raise ValueError("invalid visual_engine visibility command type")
    panel_id = command.get("panel_id")
    if not isinstance(panel_id, str) or panel_id not in PANELS:
        raise ValueError("visual_engine command requires a known top-level panel_id")
    if not isinstance(command.get("visible"), bool):
        raise ValueError("visual_engine command requires a boolean visible value")
    for nested_key in ("payload", "arguments", "params"):
        nested = command.get(nested_key)
        if isinstance(nested, Mapping) and "panel_id" in nested:
            raise ValueError("panel_id must not be nested in the visual_engine command")


def parse_voice_command(text: str) -> dict[str, Any]:
    """Parse exactly one deterministic visibility request.

    Negated, conflicting, unknown, and multi-panel requests are rejected so an
    uncertain transcript can never hide the wrong part of the HUD.
    """
    normalised = _normalise(text)
    if not normalised:
        return {
            "ok": False,
            "error": "empty_command",
            "message": "Say which HUD panel to hide or show.",
        }

    if _LIST_RE.search(normalised):
        return {"ok": True, "kind": "catalog", "panels": panel_catalog()}

    if _NEGATED_ACTION_RE.search(normalised):
        return {
            "ok": False,
            "error": "negated_action",
            "message": "No panel was changed because the action was negated.",
        }

    request = re.sub(
        r"^(?:(?:hey )?era(?: one)? |please |(?:can|could|would) you )+", "", normalised
    )
    if not re.match(rf"^(?:{_ACTION_PATTERN})(?!\w)", request):
        return {
            "ok": False,
            "error": "missing_action",
            "message": "No direct HUD visibility command found.",
        }
    actions = _matched_actions(normalised)
    if not actions:
        return {
            "ok": False,
            "error": "missing_action",
            "message": ("Use 'hide', 'show', 'turn off', or 'turn on' with a HUD panel name."),
        }
    if len(actions) != 1:
        return {
            "ok": False,
            "error": "conflicting_actions",
            "message": "Use one hide or show action per command.",
        }

    panels = _matched_panels(normalised)
    if not panels:
        return {
            "ok": False,
            "error": "unknown_panel",
            "message": "That HUD panel is not known.",
            "panels": panel_catalog(),
        }
    if len(panels) != 1:
        return {
            "ok": False,
            "error": "multiple_panels",
            "message": "Name one HUD panel per command.",
            "matched_panel_ids": panels,
        }

    panel_id = panels[0]
    aliases = _phrase_pattern(tuple(PANELS[panel_id]["aliases"]))
    if not re.fullmatch(
        rf"(?:{_ACTION_PATTERN})\s+(?:me\s+)?(?:the\s+)?(?:hud\s+)?"
        rf"(?:{aliases})(?:\s+(?:hud\s+)?panel)?(?:\s+please)?",
        request,
    ):
        return {
            "ok": False,
            "error": "ambiguous_command",
            "message": "No panel changed; use one direct panel command.",
        }
    visible = actions.pop()
    command = _visibility_command(panel_id, visible)
    verb = "Shown" if visible else "Hidden"
    return {
        "ok": True,
        "kind": "command",
        "command": command,
        "message": f"{verb} {PANELS[panel_id]['label']} panel.",
    }


def _text_from_value(value: Any) -> str:
    """Extract text from a direct or structured STT value without recursion."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "transcript", "utterance", "command"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _extract_text(arguments: Mapping[str, Any]) -> str:
    for key in ("text", "command", "transcript", "utterance", "query"):
        text = _text_from_value(arguments.get(key))
        if text:
            return text
    return ""


def _resolve_dispatcher(arguments: Mapping[str, Any]) -> Dispatcher | None:
    explicit = arguments.get("dispatcher")
    if callable(explicit):
        return explicit

    engine = arguments.get("visual_engine")
    dispatch = getattr(engine, "apply", None) or getattr(engine, "dispatch", None)
    if callable(dispatch):
        return dispatch

    if _registered_dispatcher is not None:
        return _registered_dispatcher
    from core.visual_engine import apply

    return apply


def run(arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Parse and optionally dispatch one visual_engine visibility command."""
    args = arguments if isinstance(arguments, Mapping) else {}
    result = parse_voice_command(_extract_text(args))
    result["feature_id"] = FEATURE["id"]

    if not result.get("ok") or result.get("kind") != "command":
        return result

    command = result["command"]
    try:
        _validate_visibility_command(command)
    except ValueError as exc:
        result.update(
            ok=False,
            status="invalid_command",
            error="visual_engine_protocol_error",
            message=str(exc),
        )
        return result

    dispatcher = _resolve_dispatcher(args)
    if dispatcher is None:
        result.update(
            ok=False,
            status="dispatch_failed",
            message="HUD dispatcher unavailable; nothing changed.",
        )
        result["invocation"] = "Pass command to visual_engine.dispatch(command)."
        return result

    try:
        # The dispatcher receives a copy so visual_engine code cannot mutate the
        # canonical command retained for the feature response and audit trail.
        dispatch_result = dispatcher(dict(command))
    except Exception as exc:
        result.update(
            ok=False,
            status="dispatch_failed",
            error="visual_engine_dispatch_failed",
            message=f"The panel command was valid but could not be applied: {exc}",
        )
        return result

    if not isinstance(dispatch_result, dict) or dispatch_result.get("success") is not True:
        result.update(
            ok=False,
            status="dispatch_failed",
            message=(
                dispatch_result.get("message", "HUD command failed.")
                if isinstance(dispatch_result, dict)
                else "HUD dispatcher returned no success evidence."
            ),
        )
        return result
    result["status"] = "applied"
    if isinstance(dispatch_result, dict) and dispatch_result.get("message"):
        result["message"] = dispatch_result["message"]
    if dispatch_result is not None:
        result["dispatch_result"] = dispatch_result
    return result
