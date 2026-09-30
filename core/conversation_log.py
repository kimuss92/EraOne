r"""
conversation_log.py — what was said earlier in THIS conversation, kept across
a reconnect.

WHY THIS EXISTS
  Era's durable facts live in `memory/long_term.json` and reach the model
  through `format_memory_for_prompt_ranked()` in every `_build_config()`. That
  covers what she KNOWS. It does not cover what was just SAID.

  The live conversation itself lived only inside the Gemini Live session. When
  that session ended — a quota 1011, a network blip, a voice-mode switch, a
  relaunch — the next one was built from the system prompt plus long-term
  memory and nothing else. Gemini's own session-resumption handle covers the
  common case, but it is not always there: it expires (15 min, see
  `SessionManager.DEFAULT_MAX_TOKEN_AGE_SECONDS`), it is absent on a cold
  start, and it is rejected outright after a config change or a long outage.
  In every one of those cases the fifteen minutes of context the user had just
  built up were gone, silently, mid-conversation.

  The user's report — "Era loses track of information shared earlier" — is
  mostly this. Not a memory-store bug: a session boundary with nothing
  carrying across it. So this module keeps a small rolling transcript of
  completed exchanges and renders it back into the system instruction when,
  and only when, the model has no resumption handle of its own.

WHAT IT IS NOT
  Not a memory store and not a substitute for one. Nothing here is a durable
  fact — it is short, bounded, recency-ordered and expires. Anything that
  should outlive the conversation still has to go through `save_memory` or
  `vault_note`. Keeping the two separate is deliberate: a recap that quietly
  became long-term memory would persist every passing remark, which is the
  failure mode `save_memory`'s grounding gate exists to prevent.

SAFETY
  Degrade, never raise. A conversation is a normal thing to have; losing the
  recap costs continuity, never the session. Every entry point is wrapped, the
  store is capped in both turns and characters, and a corrupt file is
  discarded rather than repaired — the worst case is the behaviour that
  existed before this module.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from core.utils import get_memory_dir

# How many completed exchanges to keep. Eight covers the "we were just talking
# about X" window without turning the system instruction into a transcript —
# the recap competes for the same attention budget as the standing rules, and
# those must not be diluted.
MAX_TURNS = 8

# Per-line and whole-block ceilings. A single rambling turn must not be able to
# spend the entire recap budget.
MAX_LINE_CHARS = 320
MAX_BLOCK_CHARS = 2400

# Older than this, a recap is a non-sequitur rather than continuity: the user
# has moved on, or this is a new conversation that merely follows an old one.
DEFAULT_MAX_AGE_S = 45 * 60.0

_lock = RLock()


def _store_path() -> Path:
    """Resolved per call, not at import: the test suite sets ERA_MEMORY_DIR."""
    return get_memory_dir() / "conversation_log.json"


@dataclass
class Exchange:
    user: str
    era: str
    ts: float

    def to_dict(self) -> dict:
        return {"user": self.user, "era": self.era, "ts": self.ts}

    @staticmethod
    def from_dict(d: dict) -> Exchange | None:
        try:
            user = str(d.get("user") or "").strip()
            era = str(d.get("era") or "").strip()
            ts = float(d.get("ts") or 0.0)
        except (TypeError, ValueError):
            return None
        if not user and not era:
            return None
        return Exchange(user=user, era=era, ts=ts)


def _clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text[: limit - 1].rstrip() + "…" if len(text) > limit else text


class ConversationLog:
    """Bounded rolling transcript of completed exchanges, persisted to disk.

    Persisted because the boundary worth surviving includes a full process
    restart (`era.relaunch`, a supervisor restart after a hang), not just a
    dropped socket.
    """

    def __init__(self, max_turns: int = MAX_TURNS, max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        self._max_turns = int(max_turns)
        self._max_age_s = float(max_age_s)
        self._turns: list[Exchange] = []

    # ── recording ────────────────────────────────────────────────────

    def record(self, user: str, era: str, *, now: float | None = None) -> bool:
        """Note one completed exchange. Returns False when there was nothing
        substantive to keep (a bare tool-call turn, an empty transcript)."""
        user = _clip(user)
        era = _clip(era)
        if not user and not era:
            return False
        self._turns.append(Exchange(user=user, era=era, ts=now if now is not None else time.time()))
        if len(self._turns) > self._max_turns:
            del self._turns[: -self._max_turns]
        return True

    # ── reading ──────────────────────────────────────────────────────

    def recent(self, *, now: float | None = None) -> list[Exchange]:
        """Unexpired exchanges, oldest first. Expiry is lazy, as in TurnMemory:
        a stale turn is dropped on the read that discovers it."""
        clock = now if now is not None else time.time()
        self._turns = [t for t in self._turns if clock - t.ts <= self._max_age_s]
        return list(self._turns)

    def recap_block(self, *, now: float | None = None) -> str:
        """The block injected into a fresh session's system instruction.

        Empty string when there is nothing worth carrying — the caller appends
        it unconditionally, so "no recap" has to render as nothing at all
        rather than as an empty heading the model would try to explain.
        """
        turns = self.recent(now=now)
        if not turns:
            return ""
        lines = [
            "[EARLIER IN THIS CONVERSATION — you were already talking to the user; "
            "this is what was said. Treat it as your own recollection: continue "
            "naturally, do not announce that you were reconnected and do not "
            "re-ask what was already answered.]"
        ]
        for t in turns:
            if t.user:
                lines.append(f"User: {t.user}")
            if t.era:
                lines.append(f"You: {t.era}")
        block = "\n".join(lines)
        if len(block) > MAX_BLOCK_CHARS:
            # Trim from the FRONT: the most recent exchange is the one the next
            # reply has to follow on from.
            head = lines[0]
            body: list[str] = []
            used = len(head)
            for line in reversed(lines[1:]):
                cost = len(line) + 1
                if used + cost > MAX_BLOCK_CHARS:
                    break
                body.append(line)
                used += cost
            block = "\n".join([head, *reversed(body)])
        return block + "\n"

    def clear(self) -> None:
        self._turns = []

    # ── persistence ──────────────────────────────────────────────────

    def save(self) -> None:
        """Best effort. A conversation must never fail because its recap did."""
        try:
            path = _store_path()
            with _lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"turns": [t.to_dict() for t in self._turns]}
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            print(f"[era] [CONTEXT] recap not persisted: {type(exc).__name__}: {exc}")

    def load(self) -> None:
        """Restore from disk. A missing or unreadable file is simply an empty
        recap — the pre-existing behaviour, not an error."""
        try:
            path = _store_path()
            if not path.exists():
                return
            with _lock:
                data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("turns") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                return
            restored = [Exchange.from_dict(d) for d in raw if isinstance(d, dict)]
            self._turns = [t for t in restored if t is not None][-self._max_turns :]
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            print(f"[era] [CONTEXT] recap not restored: {type(exc).__name__}: {exc}")
