"""The lane router's offline front gate: parse a command, select coding, or abstain.

Only complete allowlisted command grammars produce executable arguments.
This module uses the standard library and never calls a model or a tool.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "autopilot.json"
LOCAL = "local_command"
SPECIALIST = "local_specialist"
MAIN = "main_llm"


def enabled() -> bool:
    override = os.getenv("ERA_AUTOPILOT")
    if override is not None:
        return override.strip().lower() not in {"0", "false", "off"}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        return isinstance(data, dict) and data.get("enabled", True) is True
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False  # an invalid policy never enables actions


@dataclass(frozen=True)
class Decision:
    lane: str
    reason: str
    confidence: float = 1.0
    tool: str = ""
    args: dict = field(default_factory=dict)
    rule: str = "abstain"

    def as_dict(self) -> dict:
        return asdict(self)


def _local(tool: str, args: dict, rule: str) -> Decision:
    return Decision(LOCAL, "Complete deterministic command", tool=tool, args=args, rule=rule)


def _absolute_path(raw: str) -> bool:
    # No relative names, wildcard searches, URL paths, or command fragments.
    return (
        bool(raw)
        and not any(c in raw for c in "\n\r\x00*?<>|")
        and (Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute())
    )


def classify(text: str) -> Decision:
    if not enabled():
        return Decision(MAIN, "Autopilot disabled")
    raw = (text or "").strip()
    if not raw or len(raw) > 12000 or "\n" in raw or "\r" in raw:
        return Decision(MAIN, "Empty, pasted, or long-context input")
    body = re.sub(r"^(?:era(?: one)?[, :]\s*|please\s+|s'il te plaît\s+)", "", raw, flags=re.I)
    body = re.sub(r"^(?:please\s+|(?:can|could|would) you\s+)", "", body, flags=re.I)
    body = re.sub(r"^please\s+", "", body, flags=re.I).strip().rstrip(".!? ")
    low = body.lower()
    # Scope is explicit: bare volume/mute means SYSTEM. Glasses never leak there.
    match = re.fullmatch(
        r"(?:(?:set|mets|règle|regle)\s+)?(?:(system|glasses|lunettes)\s+)?"
        r"(?:volume|le volume)(?:\s+(?:to|à|a))?\s+(\d{1,3})(?:\s*%)?"
        r"(?:\s+(?:on|des|du)\s+(system|glasses|lunettes))?",
        low,
    )
    if match:
        before, value, after = match.groups()
        if int(value) <= 100 and not (before and after and before != after):
            scope = after or before or "system"
            return _local(
                "audio_control",
                {
                    "action": "set",
                    "value": int(value),
                    "device": "glasses" if scope in {"glasses", "lunettes"} else "system",
                },
                "volume_set",
            )
        return Decision(MAIN, "Invalid or conflicting volume parameters")
    match = re.fullmatch(
        r"(?:(system|glasses|lunettes)\s+)?(?:volume\s+(up|down)|"
        r"turn (?:the )?volume (up|down)|monte le volume|baisse le volume)"
        r"(?:\s+(?:by|de)\s+(\d{1,2})(?:\s*%)?)?"
        r"(?:\s+(?:on|des|du)\s+(system|glasses|lunettes))?",
        low,
    )
    if match:
        before, direction, other_direction, step, after = match.groups()
        if step and int(step) == 0:
            return Decision(MAIN, "Volume step must be positive")
        if before and after and before != after:
            return Decision(MAIN, "Conflicting devices")
        scope = before or after or "system"
        action = direction or other_direction or ("up" if "monte le volume" in low else "down")
        return _local(
            "audio_control",
            {
                "action": action,
                "step": int(step or 10),
                "device": "glasses" if scope in {"glasses", "lunettes"} else "system",
            },
            "volume_step",
        )
    match = re.fullmatch(r"(mute|unmute)(?: (?:the )?(system|glasses|lunettes))?(?: volume)?", low)
    if match:
        action, scope = match.groups()
        return _local(
            "audio_control",
            {
                "action": action,
                "device": "glasses" if scope in {"glasses", "lunettes"} else "system",
            },
            "volume_mute",
        )
    if low in {"list windows", "show windows", "liste les fenêtres", "liste les fenetres"}:
        return _local("window_control", {"action": "list"}, "window_list")
    match = re.fullmatch(
        r'(focus|minimize|maximize|restore|close) (?:window )?(?:"([^"\r\n]+)"|([\w -]{1,60}))',
        body,
        re.I,
    )
    if match:
        action, quoted, plain = match.groups()
        title = quoted or plain
        # Titles with conjunctions require quotes; do not absorb a second task.
        if quoted or not re.search(r"\b(and|then|or|if|because|et|puis)\b", title, re.I):
            return _local(
                "window_control", {"action": action.lower(), "title": title}, "window_action"
            )
    match = re.fullmatch(
        r'(list files in|read file|file info|open file|delete file) "([^"\r\n]+)"', body, re.I
    )
    if match and _absolute_path(match[2]):
        action = {
            "list files in": "list",
            "read file": "read",
            "file info": "info",
            "open file": "open",
            "delete file": "delete",
        }[match[1].lower()]
        return _local("file_controller", {"action": action, "path": match[2]}, "file_exact")
    match = re.fullmatch(r'(copy|move) file "([^"\r\n]+)" to "([^"\r\n]+)"', body, re.I)
    if match and _absolute_path(match[2]) and _absolute_path(match[3]):
        return _local(
            "file_controller",
            {
                "action": match[1].lower(),
                "path": match[2],
                "destination": match[3],
                "overwrite": False,
            },
            "file_transfer",
        )

    visual = re.fullmatch(r"(show|hide) (?:the )?(left|right) (?:hud )?panel", low)
    if visual:
        return _local(
            "visual_engine",
            {
                "command": json.dumps(
                    {"action": "layout", "layout": {visual[2]: visual[1] == "show"}}
                )
            },
            "visual_panel",
        )
    visual = re.fullmatch(r"set (?:the )?hud accent (#[0-9a-f]{6})", low)
    if visual:
        return _local(
            "visual_engine",
            {"command": json.dumps({"action": "layout", "layout": {"accent": visual[1]}})},
            "visual_accent",
        )
    if low in {"add liquid progress for background tasks", "show development progress"}:
        return _local(
            "visual_engine",
            {
                "command": json.dumps(
                    {
                        "action": "upsert",
                        "widget": {
                            "id": "development",
                            "kind": "progress",
                            "label": "Development",
                            "binding": "development",
                        },
                    }
                )
            },
            "visual_progress",
        )

    # A project scope plus an engineering imperative is required. Merely
    # mentioning code, an error, or Claude never starts a coding job.
    coding = re.match(
        r"^(audit|debug|fix|refactor|improve|implement|add|repair|optimi[sz]e|corrige|améliore|ameliore)\b",
        low,
    )
    scoped = re.search(r"\b(?:era[ -]?one|era's (?:code|repo)|era codebase)\b", low)
    files = re.findall(
        r"(?<![\w./\\])((?:app|core|actions|tools)/[\w./-]+\.py|main\.py)(?![\w./])", body
    )
    if coding and (scoped or files):
        if re.search(
            r"\b(?:don't|do not|never|ne pas)\s+(?:edit|change|modify|apply)\b", low
        ) or re.search(r"\b(?:and|then|et|puis)\s+(?:send|publish|delete|open|run|execute)\b", low):
            files = []  # analysis-only wording must never become an apply
        target = files[0] if len(set(files)) == 1 and ".." not in files[0].split("/") else ""
        if target and coding[1] not in {"audit", "debug"}:
            return Decision(
                SPECIALIST,
                "Explicit single-file Era coding change",
                tool="autonomous_development",
                args={
                    "action": "propose",
                    "file_paths": json.dumps([target]),
                    "request": raw,
                    "reload": True,
                },
                rule="coding_apply",
            )
        if (
            coding[1] in {"add", "implement"}
            and scoped
            and not re.search(r"\b(?:don't|do not|never|ne pas)\b", low)
        ):
            return Decision(
                SPECIALIST,
                "Explicit Era feature request",
                tool="autonomous_development",
                args={"action": "propose", "request": raw, "reload": True},
                rule="coding_feature",
            )
        return Decision(SPECIALIST, "Repository-scoped coding investigation", rule="coding_plan")
    return Decision(MAIN, "Conversation, research, ambiguity, or multiple actions")
