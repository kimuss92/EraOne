"""Two-way bridge between Era and the Claude Code desktop app (Code tab).

  SEND  — GUI automation: focus the Claude window, optionally open a new chat,
          paste the message (clipboard, never per-key typing), press Enter.
          Every keystroke is preceded by a foreground check: if the Claude
          window is not in front, nothing is typed. Pasting into the wrong
          window is the one failure this module must never have.
  READ  — the session transcript (core/claude_transcript.py), not the screen.
          The window exposes no text to UI Automation.
  MATCH — a nonce appended to each message identifies the session and the
          exact prompt, so it does not matter which chat the GUI delivered to.

The GUI has no API to confirm delivery; the transcript is the confirmation. A
send that never shows up in any transcript within LOCATE_TIMEOUT_S is reported
as undelivered — never assumed.
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.claude_transcript import (
    TurnState,
    TurnWatcher,
    awaiting_tool,
    find_session_by_nonce,
    latest_transcript,
    nonce_tag,
)

STATE_DIR = Path(__file__).resolve().parent.parent / "runtime" / "claude_bridge"
LOCATE_TIMEOUT_S = 30.0
REPLY_TIMEOUT_S = float(os.getenv("ERA_CLAUDE_REPLY_TIMEOUT_S", "1800"))
POLL_S = 1.5
# Keys that open a new Code chat. Configurable because it is app UI, not API.
NEW_CHAT_HOTKEY = tuple(
    k.strip() for k in os.getenv("ERA_CLAUDE_NEW_CHAT_KEYS", "ctrl,n").split(",") if k.strip()
)
MAX_MESSAGE_CHARS = 30_000
HISTORY_MAX_BYTES = 512_000


class BridgeError(RuntimeError):
    pass


# ── Windows plumbing ─────────────────────────────────────────────────────────


def find_claude_window() -> int | None:
    """hwnd of the visible Claude desktop main window, or None."""
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return None
    found: list[int] = []

    def _enum(hwnd, _):
        if (
            not win32gui.IsWindowVisible(hwnd)
            or win32gui.GetClassName(hwnd) != "Chrome_WidgetWin_1"
        ):
            return True
        if win32gui.GetWindowText(hwnd) != "Claude":
            return True
        try:
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if psutil.Process(pid).name().lower() == "claude.exe":
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_enum, None)
    return found[0] if found else None


def seconds_since_user_input() -> float:
    """Time since the last real keyboard/mouse input (GetLastInputInfo)."""

    class _LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    try:
        lii = _LII()
        lii.cbSize = ctypes.sizeof(_LII)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


def _foreground() -> int:
    try:
        import win32gui

        return win32gui.GetForegroundWindow()
    except Exception:
        return 0


def _clipboard_text_or_none() -> str | None:
    """The clipboard's text, or None when it holds no text (image, files, empty)."""
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return None
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def _clipboard_set_private(text: str) -> None:
    """Put `text` on the clipboard marked so Windows clipboard history and cloud
    sync skip it — error reports can carry paths and personal data."""
    import win32clipboard
    import win32con

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        for fmt in ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory"):
            win32clipboard.SetClipboardData(
                win32clipboard.RegisterClipboardFormat(fmt), b"\x00\x00\x00\x00"
            )
    finally:
        win32clipboard.CloseClipboard()


class GuiSender:
    """Delivers text to the Claude window. Injectable so tests never type."""

    def send(self, text: str, new_chat: bool, restore_focus: bool) -> None:
        import pyautogui
        import pyperclip

        from actions.window_control import force_absolute_focus

        hwnd = find_claude_window()
        if not hwnd:
            raise BridgeError("The Claude desktop app is not open.")
        previous = _foreground()

        def _front() -> None:
            force_absolute_focus(hwnd)
            deadline = time.time() + 2.0
            while _foreground() != hwnd and time.time() < deadline:
                time.sleep(0.1)
            if _foreground() != hwnd:
                raise BridgeError("Could not bring the Claude window to the front; nothing typed.")

        saved_clip = _clipboard_text_or_none()
        try:
            _front()
            if new_chat and NEW_CHAT_HOTKEY:
                pyautogui.hotkey(*NEW_CHAT_HOTKEY)
                time.sleep(1.5)
                _front()
            _clipboard_set_private(text)
            time.sleep(0.15)
            _front()
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.4)
            _front()
            pyautogui.press("enter")
            time.sleep(0.3)
        finally:
            # Only a TEXT clipboard can be put back; an image or file list is
            # left alone rather than replaced with an empty string.
            if saved_clip is not None:
                try:
                    pyperclip.copy(saved_clip)
                except Exception:
                    pass
            if restore_focus and previous and previous != hwnd:
                try:
                    force_absolute_focus(previous)
                except Exception:
                    pass


# ── the bridge ───────────────────────────────────────────────────────────────


class ClaudeBridge:
    """One tracked conversation with Claude Code, persisted across relaunches."""

    def __init__(self, sender: Any = None, state_dir: Path | None = None) -> None:
        self._sender = sender or GuiSender()
        self._dir = Path(state_dir or STATE_DIR)
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = self._load()

    # state

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads((self._dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._dir / "state.json.tmp"
            tmp.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._dir / "state.json")
        except OSError:
            pass

    def _update(self, **fields: Any) -> None:
        with self._state_lock:
            self._state.update(fields)
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    # send / locate / wait

    def send(self, message: str, new_chat: bool, restore_focus: bool = False) -> str:
        """Deliver `message`; returns the nonce. Raises BridgeError."""
        message = (message or "").strip()
        if not message:
            raise BridgeError("Nothing to send.")
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + "\n[...truncated by Era]"
        nonce = secrets.token_hex(4)
        text = f"{message}\n\n{nonce_tag(nonce)}"
        # Reserve BEFORE typing, atomically: busy() must already be true for any
        # second sender (user ask vs. automatic report), or both type and the
        # second overwrites the first's nonce.
        with self._state_lock:
            if self._busy_locked():
                raise BridgeError("Claude Code is still answering the previous message.")
            previous = dict(self._state)
            self._state.update(
                nonce=nonce,
                sent_at=time.time(),
                last_message=message[:2000],
                status="sent",
                reply=None,
                last_error=None,
                session_path=None if new_chat else self._state.get("session_path"),
            )
            self._save()
        try:
            self._refuse_if_awaiting_permission(previous, new_chat)
            with self._send_lock:
                self._sender.send(text, new_chat=new_chat, restore_focus=restore_focus)
        except BaseException:
            with self._state_lock:
                self._state = previous
                self._save()
            raise
        return nonce

    @staticmethod
    def _refuse_if_awaiting_permission(previous: dict[str, Any], new_chat: bool) -> None:
        """Enter lands on whatever has focus INSIDE the window. On a chat showing
        "Allow this command?", that Enter would approve a tool call nobody saw.
        Checked on the tracked chat and on the most recently active one (where a
        new-chat hotkey that silently did nothing would leave the paste)."""
        paths = [previous.get("session_path")] if not new_chat else []
        paths.append(str(latest_transcript() or ""))
        for p in {p for p in paths if p}:
            if awaiting_tool(Path(p)):
                raise BridgeError(
                    "A Claude Code chat is waiting on a permission prompt or a running "
                    "command. Nothing was typed; answer it in the app first."
                )

    def locate(self, nonce: str, sent_at: float, timeout_s: float | None = None) -> Path:
        deadline = time.time() + (LOCATE_TIMEOUT_S if timeout_s is None else timeout_s)
        while time.time() < deadline:
            path = find_session_by_nonce(nonce, sent_at)
            if path:
                self._update(session_path=str(path), session_id=path.stem)
                return path
            time.sleep(POLL_S)
        self._update(status="undelivered")
        raise BridgeError(
            "The message never appeared in any Claude Code session — it was probably not "
            "delivered (another window may have taken focus, or the chat box was not focused)."
        )

    def wait(
        self,
        path: Path,
        nonce: str,
        timeout_s: float | None = None,
        on_stall: Callable[[TurnState], None] | None = None,
    ) -> TurnState:
        watcher = TurnWatcher(path, nonce)
        deadline = time.time() + (REPLY_TIMEOUT_S if timeout_s is None else timeout_s)
        stalled_reported = False
        state = watcher.poll()
        while time.time() < deadline:
            state = watcher.poll()
            if state.done:
                break
            if state.status == "stalled_on_tool" and not stalled_reported and on_stall:
                stalled_reported = True
                on_stall(state)
            if state.status != "stalled_on_tool":
                stalled_reported = False
            time.sleep(POLL_S)
        self._record(state)
        return state

    def _record(self, state: TurnState) -> None:
        status = state.status if state.done else "timeout"
        self._update(
            status=status,
            reply=state.final_text[:20_000],
            turn_text=state.turn_text[:40_000],
            stop_reason=state.stop_reason,
            replied_at=time.time(),
        )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            history = self._dir / "history.jsonl"
            if history.exists() and history.stat().st_size > HISTORY_MAX_BYTES:
                # Keep the newer half; the history is a convenience, not a record.
                lines = history.read_text(encoding="utf-8").splitlines(keepends=True)
                history.write_text("".join(lines[len(lines) // 2 :]), encoding="utf-8")
            with open(history, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "session": self._state.get("session_id"),
                            "sent": self._state.get("last_message", "")[:1000],
                            "status": status,
                            "reply": state.final_text[:4000],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass

    def ask(
        self,
        message: str,
        new_chat: bool,
        restore_focus: bool = False,
        on_stall: Callable[[TurnState], None] | None = None,
    ) -> TurnState:
        """Blocking send → locate → wait. Run it off the event loop."""
        nonce = self.send(message, new_chat=new_chat, restore_focus=restore_focus)
        sent_at = self.snapshot().get("sent_at") or time.time()
        path = self.locate(nonce, sent_at)
        self._update(status="waiting")
        return self.wait(path, nonce, on_stall=on_stall)

    def ask_async(
        self,
        message: str,
        new_chat: bool,
        on_done: Callable[[TurnState | None, str | None], None],
        restore_focus: bool = False,
        on_stall: Callable[[TurnState], None] | None = None,
    ) -> threading.Thread:
        """ask() on a daemon thread; on_done(state, error) fires exactly once."""

        def _run() -> None:
            try:
                state = self.ask(message, new_chat, restore_focus, on_stall)
            except BridgeError as e:
                on_done(None, str(e))
                return
            except Exception as e:  # never let a thread die silently
                on_done(None, f"{type(e).__name__}: {e}")
                return
            on_done(state, None)

        t = threading.Thread(target=_run, name="era-claude-bridge", daemon=True)
        t.start()
        return t

    def note_error(self, error: str) -> None:
        """Keep a failed send's reason for `status` (spoken notices omit it)."""
        with self._state_lock:
            if self._state.get("status") not in ("sent", "waiting"):
                self._state["status"] = "undelivered"
            self._state["last_error"] = str(error)[:300]
            self._save()

    def busy(self) -> bool:
        with self._state_lock:
            return self._busy_locked()

    def _busy_locked(self) -> bool:
        # Age-bounded: a relaunch mid-wait leaves "waiting" on disk forever.
        fresh = time.time() - float(self._state.get("sent_at") or 0) < REPLY_TIMEOUT_S
        return fresh and self._state.get("status") in ("sent", "waiting")


_BRIDGE: ClaudeBridge | None = None
_BRIDGE_LOCK = threading.Lock()


def get_bridge() -> ClaudeBridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = ClaudeBridge()
        return _BRIDGE
