"""Say each thing ONCE — for every tool, not just media.

2026-10-16, "play my track": Era said "Playing 'X' on Spotify, sir." while the tool
was still running, then read the tool's own line aloud when it returned — "Playing
'X' by Mikasa Music (search: 'X Mikasa Music'), sir." Two bubbles, one action, and
the second leaked the search query. The same shape repeats on any tool: "Checking
the weather, sir." … "I checked the weather: …", "Opening Chrome." … "Chrome is
open, sir." core/prompt.txt already says routine actions are silent (SILENT
ACTION), but a prompt rule is a request; this is the code half.

`speak_once` rewrites only the text the MODEL receives for a successful call. The
HUD row and the intent memory have already recorded the real result by the time it
runs (tool_dispatch._execute_tool), so nothing true is lost:

- Era already spoke this turn -> quote it back and forbid repeating or rephrasing
  it; she adds only what the result says that is new (or nothing).
- Era has not spoken yet, routine action -> one short line at most.
- Always: the "(search: '…')" query echo is removed — useless when spoken.

Never touched: a failure (it must always be reported, even if it contradicts what
was said), and a result that already carries its own speaking order ("Say
nothing", "say EXACTLY") — those tools own their wording.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

# Tool -> actions that are "routine utility" in the SILENT ACTION sense: done, not
# answered. An action not listed (a status, a listing, a search result) is an ANSWER.
_ROUTINE: dict[str, frozenset[str]] = {
    "spotify_control": frozenset(
        {
            "play",
            "pause",
            "next",
            "previous",
            "skip",
            "forward",
            "back",
            "search",
            "liked_songs",
            "new_releases",
            "made_for_you",
            "volume_up",
            "volume_down",
            "set_volume_exact",
            "volume_delta",
            "mute_exact",
            "unmute_exact",
            "toggle_mute_exact",
            "like_track",
            "minimize",
        }
    ),
    "youtube_video": frozenset(
        {"youtube_play", "youtube_pause", "youtube_resume", "play", "volume_up", "volume_down"}
    ),
}
# Tools whose every successful call is a routine action, whatever the action.
_ROUTINE_TOOLS = frozenset({"open_app", "window_control", "send_message"})

# Tools that manage their own silence or wording end to end.
_SKIP_TOOLS = frozenset({"save_memory", "enter_standby", "shutdown_era"})

# "(search: 'سواد الغيم Mikasa Music')" — the query echo.
_SEARCH_ECHO = re.compile(r"\s*\(search:\s*'[^']*'\)", re.IGNORECASE)
# A result that already tells the model how (or whether) to speak.
_OWN_ORDER = re.compile(r"\bsay (nothing|exactly)\b|\bdo not (speak|say)\b", re.IGNORECASE)


# ── Background outcomes, spoken VERBATIM by the code (no model in between) ───
# 2026-10-16: "That didn't finish, sir. Spotify search failed for 'X': Spotify's
# window timed out; its UI worker was stopped." — tool internals read aloud.
# A person says "I couldn't get X playing — Spotify didn't respond in time."

# Internal clauses that mean nothing to the listener: dropped outright.
_JARGON = re.compile(
    r";?\s*its UI worker was stopped|\s*\((?:UI|ui) worker[^)]*\)|"
    r"\s*in the accessibility tree|\s*from the controls",
    re.IGNORECASE,
)
# Technical reasons -> what a person would say. First match wins.
_PLAIN_REASONS: tuple[tuple[re.Pattern, str], ...] = (
    (
        re.compile(r"timed? ?out|timeout|did not respond|not respond", re.I),
        "it didn't respond in time",
    ),
    (re.compile(r"no matching result|not found|no '.*' play control", re.I), "I couldn't find it"),
    (re.compile(r"cancel", re.I), "it was cancelled"),
    (
        re.compile(r"not (?:running|connected)|no spotify window|could not open", re.I),
        "it isn't open",
    ),
    (re.compile(r"quota|rate.?limit|429", re.I), "the service is busy right now"),
)
# "Spotify search failed for 'X': <reason>" -> ("Spotify", "X", reason)
_FAILED_FOR = re.compile(
    r"^(?P<app>[\w ]+?) search failed for '(?P<what>[^']+)':\s*(?P<why>.*)$", re.I
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(text: str) -> str:
    return _SENTENCE.split(text.strip(), maxsplit=1)[0].strip()


_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s+(.+?)\s*$")


def _spoken_list(text: str, limit: int = 3) -> str:
    """'Header:' + numbered/bulleted lines -> 'Header: a, b and c.' ('' if not a list).

    Live 2026-11-17 "do I have unread emails?" was spoken as "5 message(s) in the
    inbox matching 'is:unread': 1." — the first-sentence cut landed on "1.".
    """
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2 or not lines[0].rstrip().endswith(":"):
        return ""
    items = [m.group(1) for ln in lines[1:] if (m := _LIST_ITEM.match(ln))]
    if not items:
        return ""
    head = lines[0].rstrip().rstrip(":").replace("(s)", "s")
    shown = [i.rstrip(".") for i in items[:limit]]
    joined = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    more = f", and {len(items) - limit} more" if len(items) > limit else ""
    return f"{head}: {joined}{more}."


_URL = re.compile(r"\(?\s*(?:https?://|www\.)\S+\s*\)?", re.IGNORECASE)


def _speakable(text: str) -> str:
    """Never read a URL or a long technical aside aloud. Live 2026-11-17 an agent-
    browser failure was spoken with a 150-character Yahoo URL and an API-key note."""
    out = _URL.sub("the site ", text or "")
    out = re.sub(r"\s*>\s*", ": ", out)
    cut = out.find(" (")
    if cut > 0 and len(out) - cut > 40:  # a long aside, closed or truncated
        out = out[:cut]
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out if out.endswith((".", "!", "?")) else out + "."


def spoken_outcome(tool: str, result: Any) -> str:
    """One natural sentence for a background tool's real outcome (never raises)."""
    try:
        return _speakable(_spoken_outcome(tool, result))
    except Exception:
        return "I couldn't finish that, sir."


def _spoken_outcome(tool: str, result: Any) -> str:
    try:
        raw = str(getattr(result, "message", "") or getattr(result, "error", "") or "").strip()
        # A detached website answer: this path speaks verbatim, so never read out a
        # page's text. Only the plain line before the fence may be spoken.
        if "<<<UNTRUSTED_WEB_DATA" in raw:
            head = raw.split("<<<UNTRUSTED_WEB_DATA", 1)[0].strip()
            return head or "The website took too long for me to read that out, sir. Ask me again."
        text = _JARGON.sub("", _SEARCH_ECHO.sub("", raw)).strip()
        # A verifier's own verdict ("Spotify is open on screen.", "I sent play to
        # Spotify, but it is paused, not playing.") already says what was asked
        # and whether it happened; speak it as-is, never the raw telemetry.
        data = getattr(result, "data", None)
        verdict = data.get("outcome_sentence") if isinstance(data, dict) else None
        if getattr(result, "success", False) and isinstance(verdict, str) and verdict.strip():
            return verdict.strip().rstrip(".") + ", sir."
        if getattr(result, "success", False):
            spoken = _spoken_list(text) or _first_sentence(text) or "Done, sir."
            # Evidence-gated completion (core/completion_contract.py): a tool
            # that returned is not a state that changed. When dispatch could not
            # prove the effect, the background report says so in the same
            # sentence instead of asserting the success it cannot support.
            try:
                from core.completion_contract import is_unconfirmed

                if is_unconfirmed(result):
                    cleaned = re.sub(r",?\s*sir\.?$", "", spoken, flags=re.IGNORECASE)
                    cleaned = cleaned.rstrip(" .,")
                    return f"{cleaned}, sir — though I couldn't confirm it took effect."
            except Exception:
                pass
            return spoken
        m = _FAILED_FOR.match(text)
        app, what, why = (m["app"], m["what"], m["why"]) if m else ("", "", text)
        plain = next((say for rx, say in _PLAIN_REASONS if rx.search(why)), "")
        if m:
            reason = (f"{app} {plain[3:]}" if plain.startswith("it ") else plain) or why
            return f"I couldn't get '{what}' playing, sir — {reason.rstrip('.')}."
        if plain:
            return f"I couldn't finish that, sir — {plain}."
        return _first_sentence(text) or "I couldn't finish that, sir."
    except Exception:
        return "I couldn't finish that, sir."


def _is_routine(tool: str, args: dict | None) -> bool:
    if tool in _ROUTINE_TOOLS:
        return True
    actions = _ROUTINE.get(tool)
    if not actions:
        return False
    action = str((args or {}).get("action") or "").lower().strip()
    return action in actions


def speak_once(result: Any, tool: str, args: dict | None, spoken_this_turn: str) -> Any:
    """Return the ToolResult the model should see (never raises)."""
    try:
        if not getattr(result, "success", False) or tool in _SKIP_TOOLS:
            return result
        original = str(getattr(result, "message", "") or "")
        if _OWN_ORDER.search(original):
            return result
        message = _SEARCH_ECHO.sub("", original).strip()
        said = " ".join(str(spoken_this_turn or "").split())
        if said:
            note = (
                f"{message}\nYou ALREADY told the user: {said[-200:]!r}. Do not repeat or "
                "rephrase it. Say only what this result adds that you have not said yet — "
                "or nothing at all if it adds nothing."
            )
        elif _is_routine(tool, args):
            note = f"{message}\nConfirm in one short sentence at most, then stop."
        elif message == original:
            return result
        else:
            note = message
        return replace(result, message=note)
    except Exception:
        return result
