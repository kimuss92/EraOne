"""Turn a real tool call into what the HUD may show about it.

Three jobs, all pure functions so they are trivially testable:

  * describe()  — category, display label and a one-line activity string
                  ("SPOTIFY · pause") for a tool call;
  * safe_args() — a display-safe summary of the arguments. Secrets are masked,
                  message bodies are reduced to their length, long strings are
                  clipped. The HUD shows WHAT Era is doing, never the payload of
                  an email or a credential that happened to be an argument;
  * summarize() — the result reduced to a short summary plus any context cards
                  and sources that can be read straight out of the real result.

Nothing is inferred beyond what the result text actually contains: a source is
only produced for a URL that appears in the tool's output, a memory card only
for a line the memory store returned. A tool with no specific handler still
gets a generic card — adding a backend tool never requires a HUD change.
"""

from __future__ import annotations

import re
from typing import Any

# category, display label, verb phrase used when there is no `action` argument.
_TOOLS: dict[str, tuple[str, str, str]] = {
    "web_search": ("web", "WEB", "Searching the web"),
    "weather_report": ("weather", "WEATHER", "Checking the weather"),
    "spotify_control": ("media", "SPOTIFY", "Controlling Spotify"),
    "media_coordinator": ("media", "MEDIA", "Controlling playback"),
    "youtube_video": ("media", "YOUTUBE", "Working with YouTube"),
    "netflix_control": ("media", "NETFLIX", "Controlling Netflix"),
    "save_memory": ("memory", "MEMORY", "Saving to memory"),
    "retrieve_memory_context": ("memory", "MEMORY", "Searching memory"),
    "list_memory_keys": ("memory", "MEMORY", "Listing memory"),
    "forget_memory": ("memory", "MEMORY", "Removing a memory"),
    "record_feedback": ("memory", "LEARNING", "Recording feedback"),
    "vault_note": ("files", "VAULT", "Writing to the vault"),
    "file_controller": ("files", "FILES", "Working with files"),
    "file_processor": ("files", "FILES", "Processing a file"),
    "code_helper": ("files", "CODE", "Working on code"),
    "gmail_control": ("email", "GMAIL", "Working with Gmail"),
    "send_message": ("message", "MESSAGE", "Sending a message"),
    "open_app": ("system", "APPS", "Launching an application"),
    "window_control": ("system", "WINDOWS", "Managing windows"),
    "computer_settings": ("system", "SYSTEM", "Changing a setting"),
    "computer_control": ("system", "SYSTEM", "Controlling the computer"),
    "desktop_control": ("system", "DESKTOP", "Working on the desktop"),
    "system_performance": ("system", "SYSTEM", "Checking performance"),
    "audio_control": ("system", "AUDIO", "Adjusting audio"),
    "set_microphone": ("voice", "MIC", "Changing the microphone"),
    "set_voice": ("voice", "VOICE", "Changing voice"),
    "voice_lock": ("voice", "VOICE LOCK", "Changing voice lock"),
    "set_wake_word": ("voice", "WAKE WORD", "Changing the wake word"),
    "screen_process": ("vision", "VISION", "Looking at the screen"),
    "browser_control": ("browser", "BROWSER", "Driving the browser"),
    "agent_browser": ("browser", "BROWSER", "Running a browser agent"),
    "scheduled_task": ("task", "SCHEDULE", "Managing scheduled tasks"),
    "reminder": ("task", "REMINDER", "Setting a reminder"),
    "manage_schedule": ("task", "SCHEDULE", "Managing CM schedules"),
    "agent_task": ("task", "AGENT", "Running a background task"),
    "dev_agent": ("task", "DEV AGENT", "Running the dev agent"),
    "community_manager": ("cm", "COMMUNITY", "Running a community-management job"),
    "manage_offer": ("cm", "OFFERS", "Updating a brand offer"),
    "manage_preference": ("memory", "PREFERENCES", "Updating a preference"),
    "flight_finder": ("web", "FLIGHTS", "Searching flights"),
    "game_updater": ("system", "GAMES", "Updating games"),
    "premiere_control": ("apps", "PREMIERE", "Controlling Premiere Pro"),
    "claude_bridge": ("apps", "CLAUDE", "Talking to Claude Code"),
    "self_diagnosis": ("system", "DIAGNOSIS", "Running self-diagnosis"),
    "self_improve": ("system", "SELF-IMPROVE", "Running self-improvement"),
    "protocol_one": ("system", "PROTOCOL ONE", "Running Protocol One"),
    "protocol_zero": ("system", "PROTOCOL ZERO", "Running Protocol Zero"),
    "enter_standby": ("voice", "STANDBY", "Entering standby"),
    "shutdown_era": ("system", "ERA ONE", "Shutting down"),
    "confirm_pending_confirmation": ("safety", "CONFIRM", "Confirming an action"),
    "cancel_pending_confirmation": ("safety", "CANCEL", "Cancelling an action"),
}

# Tools whose `action` argument is worth showing (they are all "verb" tools).
_ACTION_KEYS = ("action", "operation", "mode", "command")

# Argument keys that are never shown, not even truncated.
_SECRET_RE = re.compile(
    r"pass(word)?|token|secret|api[_-]?key|\bkey\b|auth|cookie|credential|\botp\b|\bpin\b",
    re.IGNORECASE,
)
# Argument keys that carry a message/document body: shown as a length only.
_BODY_KEYS = frozenset(
    {"message", "body", "content", "text", "html", "draft", "reply", "note", "value", "prompt"}
)

_MAX_ARGS = 6
_MAX_STR = 80
_SUMMARY_LEN = 160

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\n\r\"<>|?*]{2,240}")

# Error types that mean "the gate held it", not "it broke".
_BLOCKED_TYPES = frozenset({"BlockedAction"})
_AWAITING_TYPES = frozenset({"ConfirmationRequired"})
_CANCELLED_TYPES = frozenset({"ConfirmationFailed", "Cancelled"})


def meta(tool: str) -> tuple[str, str, str]:
    """(category, label, verb) — a generic row for tools this table has never seen."""
    if tool in _TOOLS:
        return _TOOLS[tool]
    pretty = (tool or "tool").replace("_", " ").upper()
    return ("tool", pretty, f"Running {pretty.lower()}")


def _clip(value: str, limit: int = _MAX_STR) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def safe_args(args: Any) -> dict[str, Any]:
    """Display-safe argument summary. Never raises; never returns a secret."""
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in args.items():
        if len(out) >= _MAX_ARGS:
            out["…"] = f"+{len(args) - _MAX_ARGS} more"
            break
        key = str(k)
        if key.startswith("_"):
            continue  # internal carry keys (e.g. _source) are plumbing, not intent
        if _SECRET_RE.search(key):
            out[key] = "•••"
        elif key.lower() in _BODY_KEYS and isinstance(v, str):
            out[key] = f"<{len(v)} chars>"
        elif isinstance(v, str):
            out[key] = _clip(v)
        elif isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            out[key] = v
        elif isinstance(v, (list, tuple)):
            out[key] = f"[{len(v)} item{'s' if len(v) != 1 else ''}]"
        elif isinstance(v, dict):
            out[key] = f"{{{len(v)} field{'s' if len(v) != 1 else ''}}}"
        else:
            out[key] = _clip(repr(v), 40)
    return out


def describe(tool: str, args: Any) -> dict[str, str]:
    """Category, label and the activity line for a tool call."""
    category, label, verb = meta(tool)
    activity = verb
    if isinstance(args, dict):
        for k in _ACTION_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                target = ""
                for tk in ("query", "city", "app_name", "name", "target", "title", "path"):
                    tv = args.get(tk)
                    if isinstance(tv, str) and tv.strip() and not _SECRET_RE.search(tk):
                        target = _clip(tv, 48)
                        break
                activity = f"{v.strip().replace('_', ' ')}" + (f" · {target}" if target else "")
                break
        else:
            for tk in ("query", "city", "app_name", "path", "name"):
                tv = args.get(tk)
                if isinstance(tv, str) and tv.strip():
                    activity = f"{verb} · {_clip(tv, 48)}"
                    break
    return {"category": category, "label": label, "activity": activity}


def result_status(result: Any) -> str:
    """success | failed | blocked | awaiting | cancelled — from the real result."""
    success = bool(getattr(result, "success", True)) if not isinstance(result, str) else True
    if success:
        return "success"
    etype = getattr(result, "error_type", None) or ""
    if etype in _AWAITING_TYPES:
        return "awaiting"
    if etype in _BLOCKED_TYPES:
        return "blocked"
    if etype in _CANCELLED_TYPES:
        return "cancelled"
    return "failed"


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    msg = getattr(result, "message", "") or ""
    err = getattr(result, "error", "") or ""
    return str(msg or err or "")


def short_summary(result: Any) -> str:
    fn = getattr(result, "short_message", None)
    if callable(fn):
        try:
            return _clip(fn(_SUMMARY_LEN), _SUMMARY_LEN)
        except Exception:
            pass
    return _clip(_result_text(result), _SUMMARY_LEN)


def _web_sources(text: str, query: str) -> list[dict[str, Any]]:
    """Sources for URLs that literally appear in the result. The DuckDuckGo
    formatter prints `N. Title` then the snippet then the URL, so the nearest
    numbered line above a URL is its title — when there is one."""
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for url in _URL_RE.findall(line):
            url = url.rstrip(".,;")
            if url in seen:
                continue
            seen.add(url)
            title = ""
            for j in range(i - 1, max(-1, i - 4), -1):
                m = re.match(r"\s*\d+\.\s+(.+)", lines[j])
                if m:
                    title = m.group(1).strip()
                    break
            host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            sources.append(
                {
                    "type": "web",
                    "title": _clip(title or host, 90),
                    "subtitle": host,
                    "url": url,
                    "query": _clip(query, 80) if query else "",
                }
            )
            if len(sources) >= 8:
                return sources
    return sources


def _memory_entries(text: str) -> list[dict[str, str]]:
    """Lines shaped `category/key: value`, exactly as retrieve_context_reinforced
    formats them. Anything else (e.g. "No relevant memories found.") yields none."""
    entries = []
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_ -]{1,40})/([^:]{1,80}):\s*(.*)", line)
        if m:
            entries.append(
                {"category": m.group(1).strip(), "key": m.group(2).strip(), "value": m.group(3)}
            )
    return entries[:12]


def summarize(tool: str, args: Any, result: Any, *, call_id: str, ts: float) -> dict[str, Any]:
    """Summary, context cards, sources and memory entries for one finished call."""
    args = args if isinstance(args, dict) else {}
    status = result_status(result)
    text = _result_text(result)
    summary = short_summary(result)
    category, label, _verb = meta(tool)
    cards: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    memory: list[dict[str, str]] = []

    def card(ctype: str, title: str, subtitle: str = "", **payload: Any) -> dict[str, Any]:
        return {
            "id": f"{call_id}:{len(cards)}",
            "type": ctype,
            "title": _clip(title, 90),
            "subtitle": _clip(subtitle, 120),
            "icon": category,
            "payload": payload,
            "source": f"tool:{tool}",
            "timestamp": ts,
            "actions": [],
        }

    if status != "success":
        return {
            "status": status,
            "summary": summary,
            "cards": [],
            "sources": [],
            "memory": [],
        }

    if tool in ("web_search", "flight_finder"):
        query = str(args.get("query") or ", ".join(map(str, args.get("items") or [])))
        sources = _web_sources(text, query)
        if not sources and query:
            # Grounded answers carry no URLs in the text; say what was searched,
            # and nothing more. No citation is invented.
            sources = [
                {"type": "web", "title": f"Web search · {_clip(query, 60)}", "subtitle": label}
            ]
        cards.append(card("web", f"Search · {query or label}", summary, results=len(sources)))
    elif tool == "retrieve_memory_context":
        memory = _memory_entries(text)
        for e in memory[:6]:
            cards.append(card("memory", e["key"], e["category"], value=_clip(e["value"], 160)))
        if memory:
            sources.append(
                {
                    "type": "memory",
                    "title": f"{len(memory)} related memor{'y' if len(memory) == 1 else 'ies'}",
                    "subtitle": "Long-term memory",
                }
            )
    elif tool == "save_memory":
        key = str(args.get("key") or "")
        cards.append(card("memory", key or "Memory saved", str(args.get("category") or "memory")))
    elif tool == "weather_report":
        cards.append(card("weather", str(args.get("city") or "Weather"), summary))
        sources.append({"type": "external", "title": "Open-Meteo", "subtitle": "Weather data"})
    elif category == "media":
        cards.append(card("media", label, summary, action=str(args.get("action") or "")))
    elif category == "files":
        path = str(args.get("path") or args.get("file_path") or "")
        if not path:
            m = _PATH_RE.search(text)
            path = m.group(0).strip() if m else ""
        c = card("file", path.rsplit("\\", 1)[-1] if path else label, summary)
        if path:
            c["payload"]["path"] = path
            c["actions"] = [{"id": "reveal", "label": "Reveal"}]
            sources.append({"type": "file", "title": path.rsplit("\\", 1)[-1], "subtitle": path})
        cards.append(c)
    elif category == "email":
        cards.append(card("email", "Gmail", summary, action=str(args.get("action") or "")))
        sources.append({"type": "email", "title": "Gmail", "subtitle": summary})
    elif category in ("system", "apps", "browser", "voice"):
        target = str(args.get("app_name") or args.get("name") or args.get("target") or label)
        cards.append(card("system", target, summary, action=str(args.get("action") or "")))
    elif category == "task":
        cards.append(card("task", label, summary))
    else:
        cards.append(card("generic", label, summary))

    return {
        "status": status,
        "summary": summary,
        "cards": cards,
        "sources": sources,
        "memory": memory,
    }


def grounding_sources(grounding: Any) -> list[dict[str, Any]]:
    """Web sources from a Gemini grounding_metadata block — the model's own
    citations, so these are exactly as real as the answer they back."""
    out: list[dict[str, Any]] = []
    try:
        chunks = getattr(grounding, "grounding_chunks", None) or []
        for ch in chunks:
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", None) if web is not None else None
            if not uri:
                continue
            title = getattr(web, "title", None) or ""
            host = re.sub(r"^https?://(www\.)?", "", str(uri)).split("/")[0]
            out.append(
                {
                    "type": "web",
                    "title": _clip(title or host, 90),
                    "subtitle": _clip(title or host, 60),
                    "url": str(uri),
                }
            )
            if len(out) >= 8:
                break
    except Exception:
        return out
    return out
