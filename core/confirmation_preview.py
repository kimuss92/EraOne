"""core/confirmation_preview.py — resolve what a risky call will actually DO,
before the user is asked to approve it.

The confirmation layer normally stores the tool call verbatim and asks about it.
That works when the parameters already say what will happen ("email bob@x about
Y"). It does not work when the parameters are an *instruction to a model* and
the concrete effects are decided afterwards: `desktop_control action=task`
collected a yes for a task string, and only then let Gemini choose which files
to move. The user approved "tidy my desktop" and got whatever the model picked
— and under the threat model the model is adversarial, so that approval carried
no information. `core/main_tool_bridge.py` states the standard this violates:
a confirmation the user cannot check against what is actually about to happen
is not a confirmation.

A preview builder runs BEFORE the pending confirmation is stored. It resolves
the call into concrete steps, returns a sentence describing them, and returns
the resolved plan to be carried through the confirmation. On approval the
dispatcher executes THAT stored plan — never a freshly generated one, and never
one echoed back by the model, which could differ from what was shown.

Failures are contained: a builder that raises yields no preview and no carried
state, and the caller falls back to the generic question. It must never be
possible for a preview bug to skip a confirmation.
"""

from __future__ import annotations

from collections.abc import Callable

# Keys a preview builder may inject into the stored parameters. They are
# excluded from the confirm-time parameter match (the model never sees them and
# cannot echo them back) and are re-read from the STORE on approval, so a model
# that invents one cannot pre-authorise its own plan.
CARRIED_KEYS = frozenset({"_approved_plan"})

# Set by the dispatcher on the ToolResult data so the question builder can use
# the rendered steps instead of the generic fallback sentence.
PREVIEW_KEY = "confirmation_preview"

# Set by a dispatcher once it has put the preview in front of the user over a
# channel the model does not control. The question builder then tells the
# model NOT to ask again, so the user hears the real steps rather than the
# model's paraphrase of them.
ANNOUNCED_KEY = "confirmation_announced"

# Parameter keys that are transport, not intent: they are excluded from the
# confirm-time match between what was stored and what the caller re-sent.
CONFIRM_META = frozenset({"confirmed", "confirmation_id", "auto_confirm"}) | CARRIED_KEYS


def _desktop_control_preview(parameters: dict) -> tuple[str, dict]:
    from actions.desktop_preview import plan_desktop_preview

    return plan_desktop_preview(parameters)


_BUILDERS: dict[str, Callable[[dict], tuple[str, dict]]] = {
    "desktop_control": _desktop_control_preview,
}


def strip_carried(parameters: dict) -> dict:
    """Drop any carried key supplied by the caller.

    The model can put anything in a tool call, including `_approved_plan`. Only
    a preview builder may set one, so an incoming value is discarded before the
    call is classified, stored or run.
    """
    return {k: v for k, v in parameters.items() if k not in CARRIED_KEYS}


def carried_from(stored: dict) -> dict:
    """The carried state to merge back in at execution time, taken from the
    pending-confirmation store rather than from the caller."""
    return {k: v for k, v in (stored or {}).items() if k in CARRIED_KEYS}


def build_confirmation_preview(tool: str, parameters: dict) -> tuple[str, dict]:
    """Return (sentence, carried params) for a call about to be confirmed.

    ("", {}) means "no preview available" — the caller keeps its generic
    question and its normal behaviour.
    """
    builder = _BUILDERS.get(tool)
    if builder is None:
        return "", {}
    try:
        text, carried = builder(dict(parameters))
    except Exception as e:  # a preview must never break the confirmation path
        print(f"[ConfirmPreview] {tool} preview failed: {e}")
        return "", {}
    if not isinstance(carried, dict):
        return (text or ""), {}
    return (text or ""), {k: v for k, v in carried.items() if k in CARRIED_KEYS}
