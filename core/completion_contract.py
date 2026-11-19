# core/completion_contract.py
"""Evidence-gated completion: a success claim is only as good as its evidence.

WHY THIS EXISTS
  Era's tools report `success=True` for three different things: a state that was
  observed to change, a call that merely returned, and a message that contained
  no failure words. `core.tool_result` deliberately stays permissive because it
  is the shared wire format of 400+ call sites; tightening it there would change
  every existing tool at once. So the judgement lives here, in one place, and is
  attached to the result the model and the speech layer read:

    {"verdict": "proven" | "informational" | "unverified" | "failed",
     "side_effecting": bool, "verifier": str | None, "status": str}

  Rules (each pinned by tools/test_completion_contract.py):
  * a failure is a failure, whatever else the payload says;
  * `proven` needs an evidence-based verifier. `verified=True` from the
    "generic" verifier only means "the tool returned without raising" — that is
    not evidence, and it is exactly what made 8 tools look verified when
    nothing had been checked;
  * a read-only/informational tool or a READ action on any tool (status, list,
    report...; `is_read_only`) reports `informational` — its own text IS the
    answer, and there is no state to confirm. So does a tool that only switches
    Era's own in-process state (`ERA_STATE_TOOLS`);
  * a side-effecting tool with no verifier is `unverified`, and that is a state
    the runtime must be able to speak and the model must not upgrade to success.

  `annotate` never raises and never changes `success` — a tool's outcome is not
  this module's to alter. It only records what the runtime knows.
"""

from __future__ import annotations

PROVEN = "proven"
INFORMATIONAL = "informational"
UNVERIFIED = "unverified"
FAILED = "failed"

# Tools whose result IS the answer: a search, a status, a read, a report. There
# is no postcondition to observe, so "unverified" would be a false alarm.
INFORMATIONAL_TOOLS = frozenset(
    {
        "web_search",
        "weather_report",
        "flight_finder",
        "game_updater",
        "crew",
        "code_mode",
        "file_processor",
        "self_diagnosis",
        "deep_think",
        "capability_check",
        "retrieve_memory_context",
        "save_memory",
        "forget_memory",
        "enter_standby",
        "manage_preference",
        # Live 2026-11-17: a screen description was spoken as "…, though I
        # couldn't confirm it took effect". Looking at the screen changes nothing.
        "screen_process",
    }
)

# Tools that only switch Era's OWN state in-process (her mic, voice, standby,
# session). The tool sets that state itself before it returns, so its result IS
# the state; there is nothing outside Era to observe.
ERA_STATE_TOOLS = frozenset(
    {
        "shutdown_era",
        "set_microphone",
        "set_voice",
        "voice_lock",
        "record_feedback",
        "list_memory_keys",
        "capability_check",
        "cancel_pending_confirmation",
    }
)

# Actions that only READ, on any tool: their answer is the evidence. "search" is
# deliberately absent: spotify/netflix "search" starts playback.
READ_ACTIONS = frozenset(
    {
        "status",
        "list",
        "read",
        "get",
        "report",
        "recent",
        "info",
        "current_track",
        "current_track_data",
        "playback_state",
        "progress",
        "get_volume_exact",
        "is_playing",
        "check",
        "verify",
        "pending",
        "list_devices",
        "device_status",
        "battery",
        "active",
        "monitors",
        "scan",
    }
)

# Reads that only make sense for one tool (a generic word elsewhere).
TOOL_READ_ACTIONS = {
    "gmail_control": frozenset({"search", "important", "unread"}),
    "vault_bridge": frozenset({"search"}),
    "claude_bridge": frozenset({"read_reply"}),
    "system_performance": frozenset({"msi_scenario_status"}),
    "computer_control": frozenset({"running_apps", "active_window", "system_snapshot", "clock"}),
    "computer_settings": frozenset({"get_volume"}),
}


# Actions that only change Era's OWN settings on a tool that also does real work.
TOOL_ERA_STATE_ACTIONS = {
    "audio_control": frozenset({"set_voice_output", "clear_voice_output"}),
}


def is_read_only(tool: str | None, action: str | None) -> bool:
    """True when this call only answered a question: there is no effect to confirm."""
    tool = str(tool or "").strip()
    action = str(action or "").strip().lower()
    if tool in INFORMATIONAL_TOOLS or tool in ERA_STATE_TOOLS:
        return True
    return bool(action) and (
        action in READ_ACTIONS
        or action in TOOL_READ_ACTIONS.get(tool, frozenset())
        or action in TOOL_ERA_STATE_ACTIONS.get(tool, frozenset())
    )


# Verifiers that only prove the plumbing worked, not the effect. Kept as a named
# set so a future verifier can be added as evidence without touching call sites.
_NON_EVIDENCE_VERIFIERS = frozenset({"generic"})

_COMPLETION_KEY = "completion"

_MODEL_INSTRUCTION = (
    "NOT CONFIRMED: this action returned without independent evidence that it "
    "took effect. Do not say it worked, do not confirm the requested state and "
    "do not repeat a success phrase. Report it as unconfirmed in one short "
    "sentence."
)


def is_side_effecting(tool: str | None, action: str | None = None) -> bool:
    """True when a successful call changes something outside Era's own answer."""
    return not is_read_only(tool, action)


def verdict(result, tool: str | None = None, action: str | None = None) -> str:
    """Classify what the runtime can actually prove about this result."""
    tool_name = str(tool or getattr(result, "tool_name", "") or "").strip()
    action_name = action if action is not None else getattr(result, "action_name", None)
    if not getattr(result, "success", False):
        return FAILED
    if getattr(result, "verification_status", "") == "FAILED":
        return FAILED

    verified = bool(getattr(result, "verified", False))
    verifier = str(getattr(result, "verifier_name", "") or "")
    if verified and verifier and verifier not in _NON_EVIDENCE_VERIFIERS:
        return PROVEN
    if not is_side_effecting(tool_name, action_name):
        # A read's own answer is its evidence, whichever verifier ran (a read
        # checked only by "generic" is still a read, not an unproven action).
        return INFORMATIONAL
    if verified and verifier in _NON_EVIDENCE_VERIFIERS:
        # "Returned without raising" is not a postcondition.
        return UNVERIFIED
    return UNVERIFIED


def annotate(result, tool: str | None = None, action: str | None = None):
    """Attach the verdict to `result.data["completion"]`. Never raises.

    The stored tag is what `core.main_tool_bridge` sends to the model and what
    `app.era_live.speak_once.spoken_outcome` reads before speaking; neither
    re-derives it, so both agree about what happened.
    """
    try:
        action = action or getattr(result, "action_name", None)
        tag = {
            "verdict": verdict(result, tool, action),
            "side_effecting": is_side_effecting(tool or getattr(result, "tool_name", ""), action),
            "verifier": getattr(result, "verifier_name", None),
            "status": getattr(result, "verification_status", ""),
            "action": action or getattr(result, "action_name", None),
        }
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            data[_COMPLETION_KEY] = tag
        elif data is None:
            result.data = {_COMPLETION_KEY: tag}
        else:
            # A tool that returned a non-dict payload keeps it under "result".
            result.data = {"result": data, _COMPLETION_KEY: tag}
    except Exception:  # evidence tagging must never change an outcome
        pass
    return result


def completion_tag(result) -> dict | None:
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        tag = data.get(_COMPLETION_KEY)
        if isinstance(tag, dict):
            return tag
    return None


def is_unconfirmed(result) -> bool:
    """True when a TAGGED success result carries no evidence of its effect.

    Deliberately tag-only: a `ToolResult` built outside the live dispatch path
    (a unit test, a background helper, an older call site) keeps exactly the
    behaviour it had. The contract is attached where the runtime knows the
    facts — in `_finalize_tool_result` — and nowhere else.
    """
    tag = completion_tag(result)
    return tag is not None and tag.get("verdict") == UNVERIFIED


def model_note(result) -> str | None:
    """The sentence the model must obey, or None when the result is proven."""
    tag = completion_tag(result)
    if tag is not None and tag.get("verdict") == UNVERIFIED:
        return _MODEL_INSTRUCTION
    return None


def speech_note(result) -> str | None:
    """One short clause for a spoken background outcome, or None."""
    return "though I couldn't confirm it took effect" if is_unconfirmed(result) else None
