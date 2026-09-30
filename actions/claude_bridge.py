"""claude_bridge tool: Era talks to the Claude Code desktop app and reads its answers.

  ask         new chat, send `message`; Claude's reply arrives in the background
  reply       continue the SAME chat with `message` (multi-turn)
  read_reply  Claude's latest reply (optionally wait up to `wait_s` for it)
  status      where the conversation stands

Sending returns at once: a Claude Code turn can run for many minutes, and a
tool call that blocked that long would freeze the live session. When the reply
lands, `notify` tells Era, which then calls read_reply — the reply enters Era's
reasoning as a TOOL RESULT, the same trust level as a web page, and every action
Era takes afterwards still goes through its own safety gates.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from core.claude_bridge import BridgeError, get_bridge

MAX_READ_CHARS = 8000
MAX_WAIT_S = 45.0

Notifier = Callable[[str], None]


def _describe(snap: dict) -> str:
    status = snap.get("status") or "idle"
    if status in ("sent", "waiting"):
        age = int(time.time() - float(snap.get("sent_at") or time.time()))
        return f"Claude Code is still working on the last message ({age}s so far)."
    if status == "undelivered":
        why = snap.get("last_error") or "it never appeared in any Claude Code session"
        return f"The last message was not delivered to Claude Code: {why}"
    if status == "timeout":
        return "Claude Code did not finish replying within the time limit."
    if status in ("complete", "superseded"):
        return "Claude Code has replied."
    return "No conversation with Claude Code yet."


def _read(snap: dict, full: bool) -> str:
    text = (snap.get("turn_text") if full else snap.get("reply")) or ""
    if not text:
        return _describe(snap)
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n[...reply truncated]"
    note = ""
    if snap.get("status") == "superseded":
        note = " (a newer message was sent in that chat before it finished)"
    return f"Claude Code replied{note}:\n\n{text}"


def _ask_headless(message: str) -> str | None:
    """Claude's answer, or None to mean "use the GUI path instead".

    None rather than an error string is the whole point: every reason this can
    fail — no CLI on PATH, a timeout, an API error, an unreadable reply — is a
    reason to fall back to the window, not a reason for Era to stop. Karim's
    standing requirement is that she never hits a dead end.
    """
    try:
        from core import claude_headless
    except Exception as e:  # the optional transport must not break the tool
        print(f"[era] [CLAUDE-BRIDGE] headless transport unavailable ({e}); using the window.")
        return None
    if not claude_headless.available():
        return None
    reply = claude_headless.ask(message)
    if not reply.ok:
        print(f"[era] [CLAUDE-BRIDGE] headless ask failed ({reply.reason}); using the window.")
        return None
    # Claude's own words, so they are returned as the tool result for Era to
    # relay — never spoken into the live session as a notice (the GUI path's
    # notices are fixed phrases for exactly that reason).
    return f"Claude Code answered:\n\n{reply.text}"


def claude_bridge(
    parameters: dict,
    notify: Notifier | None = None,
    bridge=None,
) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").lower().strip()
    bridge = bridge or get_bridge()

    if action in ("ask", "reply"):
        message = str(params.get("message") or "").strip()
        if not message:
            return "Tell me what to send to Claude Code."
        if bridge.busy():
            return (
                "Claude Code is still answering the previous message. Wait for it, or read "
                "the reply first."
            )
        snap = bridge.snapshot()
        new_chat = action == "ask" or not snap.get("session_path")

        # Notices enter the live session, so they are fixed phrases: no exception
        # text, and never Claude's words (those arrive only via read_reply).
        def _done(state, error) -> None:
            if error:
                print(f"[era] [CLAUDE-BRIDGE] send failed: {error}")
                bridge.note_error(error)
            if not notify:
                return
            if error:
                notify("My message to Claude Code was not delivered. Ask me for its status.")
            elif state and state.done:
                notify("Claude Code has replied.")
            else:
                notify("Claude Code did not finish replying in time.")

        def _stall(_state) -> None:
            if notify:
                notify(
                    "Claude Code is waiting on something in its window - probably a "
                    "permission prompt only you can approve."
                )

        # A QUESTION goes to the headless CLI first: no window to focus, no
        # clipboard, no transcript to poll, and it comes back read-only
        # (plan mode + a tool denylist), so an injected question cannot make an
        # invisible agent act. A REPLY continues the conversation in Karim's
        # window, which headless has no way to join — and `new_chat` means the
        # user asked for a fresh chat in the app, which is a visible thing they
        # want. Both of those stay on the GUI path.
        #
        # It is not the default for everything because headless reloads the whole
        # project context each time: ~$0.64 per message, measured, and --resume
        # cost more rather than less (see core/claude_headless.py).
        if action == "ask" and not params.get("new_chat"):
            headless = _ask_headless(message)
            if headless is not None:
                return headless
            # Fell through on purpose: the CLI is missing or failed, so the GUI
            # path answers instead. A slower answer, never a dead end.

        try:
            bridge.ask_async(message, new_chat=new_chat, on_done=_done, on_stall=_stall)
        except BridgeError as e:
            return f"Could not send to Claude Code: {e}"
        where = "a new Claude Code chat" if new_chat else "the current Claude Code chat"
        return f"Sent to {where}. I'll be told when Claude replies; don't guess its answer."

    if action in ("read_reply", "read", "latest"):
        try:
            wait_s = min(MAX_WAIT_S, max(0.0, float(params.get("wait_s") or 0)))
        except (TypeError, ValueError):
            wait_s = 0.0
        deadline = time.time() + wait_s
        while bridge.busy() and time.time() < deadline:
            time.sleep(1.0)
        return _read(bridge.snapshot(), full=bool(params.get("full")))

    if action == "status":
        snap = bridge.snapshot()
        session = snap.get("session_id") or "none"
        return f"{_describe(snap)} Session: {session}."

    return f"Unknown claude_bridge action: {action}. Use ask, reply, read_reply or status."
