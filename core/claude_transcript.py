"""Read Claude Code's replies from its session transcripts, never from the screen.

WHY NOT SCRAPE THE WINDOW
  Probed 2026-09-19: the Claude desktop window (claude.exe, Chrome_WidgetWin_1)
  exposes exactly three UI Automation elements — Minimise, Restore, Close. The
  chat is an Electron renderer whose accessibility tree is off unless a screen
  reader forces it on, so there is no Edit, Document or Text node to read. OCR
  would work but loses code blocks, breaks on scrolling and is slow.

WHAT WE READ INSTEAD
  Every Code-tab session is written, record by record, to
  ~/.claude/projects/<cwd-slug>/<session-id>.jsonl (entrypoint "claude-desktop").
  That file is the app's own source of truth, so reading it gives the exact text
  Claude produced, and its stop_reason says when a turn is over.

  Record facts this module relies on (measured on real desktop transcripts):
    * one record per content block; blocks of one API message share message.id
    * message.stop_reason is on every block of that message: "tool_use" while
      Claude is still working, "end_turn" once the reply is finished
    * a user record whose content holds tool_result blocks is a tool answer,
      not a prompt; isSidechain records belong to subagents
    * records are appended after each block completes, so a quiet file does NOT
      mean the turn is over — only a terminal stop_reason does

Correlation: Era appends a nonce ("[era-bridge <hex>]") to each message it
sends, and finds the session — and the prompt's position in it — by that nonce.
That makes the reader independent of which chat the GUI actually delivered to.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TERMINAL_STOPS = frozenset({"end_turn", "stop_sequence", "max_tokens", "refusal"})

# How long the file must stay quiet after a terminal stop before the turn is
# called finished. A Stop hook can re-open a turn a moment after end_turn.
SETTLE_S = 2.5
# A tool_use with no tool_result for this long is a permission prompt (or a
# long-running command) — something only the user can move along.
STALLED_TOOL_S = 25.0
# Only the tail is read when looking for a fresh nonce: it was just written.
_TAIL_BYTES = 1_500_000


def projects_root() -> Path:
    explicit = os.getenv("CLAUDE_PROJECTS_DIR", "").strip()
    if explicit:
        return Path(explicit)
    config = os.getenv("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(config) if config else Path.home() / ".claude") / "projects"


def nonce_tag(nonce: str) -> str:
    return f"[era-bridge {nonce}]"


def prompt_text(rec: dict[str, Any]) -> str | None:
    """The text of a real user prompt, or None for anything else (tool results,
    subagent traffic, meta records injected by the harness)."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(texts) if texts else None


def _read_records(path: Path, start: int) -> list[dict[str, Any]]:
    """Whole records from byte `start` on; a trailing line still being written
    is left out."""
    try:
        with open(path, "rb") as fh:
            fh.seek(_aligned_start(path, start))
            raw = fh.read()
    except OSError:
        return []
    return _parse_lines(raw[: raw.rfind(b"\n") + 1])


def find_session_by_nonce(nonce: str, since_ts: float, root: Path | None = None) -> Path | None:
    """The transcript holding the prompt tagged with `nonce`, among files touched
    since `since_ts`. Cheap: a byte search of each recent tail before parsing."""
    root = root or projects_root()
    needle = nonce.encode()
    try:
        candidates = [p for p in root.glob("*/*.jsonl") if p.stat().st_mtime >= since_ts - 5]
    except OSError:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                fh.seek(max(0, size - _TAIL_BYTES))
                if needle not in fh.read():
                    continue
        except OSError:
            continue
        start = max(0, size - _TAIL_BYTES)
        if any(nonce in (prompt_text(r) or "") for r in _read_records(path, start)):
            return path
    return None


def latest_transcript(root: Path | None = None, within_s: float = 3600) -> Path | None:
    """The most recently written transcript (the chat most likely on screen)."""
    root = root or projects_root()
    try:
        recent = [
            (p.stat().st_mtime, p)
            for p in root.glob("*/*.jsonl")
            if time.time() - p.stat().st_mtime < within_s
        ]
    except OSError:
        return None
    return max(recent)[1] if recent else None


def awaiting_tool(path: Path) -> bool:
    """True when the chat's last main-thread assistant record is a tool_use with
    no tool_result yet: a permission prompt, or a command still running."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    records = [r for r in _read_records(path, max(0, size - 400_000)) if not r.get("isSidechain")]
    answered: set[str] = set()
    for rec in reversed(records):
        content = (rec.get("message") or {}).get("content")
        if rec.get("type") == "user" and isinstance(content, list):
            answered |= {
                str(b.get("tool_use_id"))
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            }
            continue
        if rec.get("type") != "assistant":
            continue
        uses = [
            str(b.get("id"))
            for b in (content or [])
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        return any(u not in answered for u in uses)
    return False


@dataclass
class TurnState:
    """Where Claude is in answering one prompt.

    status: "not_found" (prompt not in the file yet) | "pending" (no reply yet) |
    "working" | "stalled_on_tool" (permission prompt or long command) |
    "complete" | "superseded" (someone sent a newer prompt; what we have is final)
    """

    status: str
    final_text: str = ""
    turn_text: str = ""
    stop_reason: str | None = None
    session_id: str | None = None
    idle_s: float = 0.0
    tools_used: list[str] = field(default_factory=list)
    last_error: str | None = None

    @property
    def done(self) -> bool:
        return self.status in ("complete", "superseded")


def _texts(rec: dict[str, Any]) -> list[str]:
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    ]


def analyze_turn(
    records: list[dict[str, Any]],
    nonce: str,
    idle_s: float,
    settle_s: float = SETTLE_S,
    stalled_tool_s: float = STALLED_TOOL_S,
) -> TurnState:
    """Judge the reply to the prompt tagged `nonce` from the records around it.

    Pure: the caller supplies the records and how long the file has been quiet,
    which is what makes every branch here testable without a live app."""
    anchor = None
    for i, rec in enumerate(records):
        if nonce in (prompt_text(rec) or ""):
            anchor = i
    if anchor is None:
        return TurnState(status="not_found", idle_s=idle_s)

    session_id = records[anchor].get("sessionId")
    after = [r for r in records[anchor + 1 :] if not r.get("isSidechain")]

    superseded = False
    turn: list[dict[str, Any]] = []
    for rec in after:
        if prompt_text(rec) is not None:
            superseded = True
            break
        turn.append(rec)

    assistants = [r for r in turn if r.get("type") == "assistant"]
    last_error = None
    for rec in turn:
        if rec.get("type") == "system" and rec.get("subtype") == "api_error":
            last_error = str(rec.get("error") or rec.get("content") or "api_error")[:300]

    tools = []
    answered: set[str] = set()
    for rec in turn:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tools.append(str(b.get("name") or "?"))
            elif b.get("type") == "tool_result":
                answered.add(str(b.get("tool_use_id")))

    turn_text = "\n\n".join(t for r in assistants for t in _texts(r)).strip()
    state = TurnState(
        status="pending",
        turn_text=turn_text,
        session_id=session_id,
        idle_s=idle_s,
        tools_used=tools,
        last_error=last_error,
    )
    if not assistants:
        if superseded:
            state.status = "superseded"
        return state

    last = assistants[-1]
    msg_id = (last.get("message") or {}).get("id")
    final = [r for r in assistants if (r.get("message") or {}).get("id") == msg_id]
    state.final_text = "\n\n".join(t for r in final for t in _texts(r)).strip()
    state.stop_reason = (last.get("message") or {}).get("stop_reason")

    if superseded:
        state.status = "superseded"
    elif state.stop_reason in TERMINAL_STOPS and idle_s >= settle_s:
        state.status = "complete"
    elif state.stop_reason == "tool_use" and idle_s >= stalled_tool_s:
        pending = [
            b.get("id")
            for b in ((last.get("message") or {}).get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        state.status = (
            "stalled_on_tool" if any(p not in answered for p in pending if p) else "working"
        )
    else:
        state.status = "working"
    return state


class TurnWatcher:
    """Incrementally follows one transcript for the reply to one prompt."""

    def __init__(self, path: Path, nonce: str) -> None:
        self.path = Path(path)
        self.nonce = nonce
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        # The nonce prompt is usually already in the file when the watcher is
        # created, so the start must land on a line boundary: skipping "the
        # first line" blindly dropped the prompt itself in every new chat.
        self._offset = _aligned_start(self.path, max(0, size - _TAIL_BYTES))
        self._records: list[dict[str, Any]] = []

    def poll(self) -> TurnState:
        try:
            st = self.path.stat()
        except OSError:
            return TurnState(status="not_found")
        if st.st_size > self._offset:
            # Only consume up to the last complete line so a record still being
            # written is re-read whole on the next poll.
            with open(self.path, "rb") as fh:
                fh.seek(self._offset)
                raw = fh.read(st.st_size - self._offset)
            cut = raw.rfind(b"\n")
            if cut >= 0:
                chunk_end = self._offset + cut + 1
                self._records.extend(_parse_lines(raw[: cut + 1]))
                self._offset = chunk_end
        idle = max(0.0, time.time() - st.st_mtime)
        return analyze_turn(self._records, self.nonce, idle)


def _aligned_start(path: Path, start: int) -> int:
    """`start` moved forward to the beginning of the next whole line (or kept,
    when it already is one)."""
    if start <= 0:
        return 0
    try:
        with open(path, "rb") as fh:
            fh.seek(start - 1)
            chunk = fh.read(1 << 20)
    except OSError:
        return start
    if chunk[:1] == b"\n":
        return start
    nl = chunk.find(b"\n")
    return start + nl if nl >= 0 else start - 1 + len(chunk)


def _parse_lines(raw: bytes) -> list[dict[str, Any]]:
    out = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
