"""Say "it failed" only once recovery has had its turn. (Board item 19.)

WHY THIS EXISTS
  Era announced every tool failure the instant it happened, synchronously, from
  the one funnel every tool call passes through:

      if not result.success and result.error:
          self.speak_error(name, result.error)      # app/era_live/tool_dispatch.py

  On the Fish / speech_out voice `speak_error` reaches the speakers LITERALLY, so
  the user heard *"Sir, netflix_control encountered an error. <raw message>"*
  before the model had even been handed the failure — let alone tried anything
  else. Any recovery, by the model or by code, could therefore only ever
  contradict an announcement the user had already heard.

  That ordering is the whole defect. "Try workarounds before asking" was not
  merely unimplemented, it was impossible by construction: the asking came first,
  from code, and nothing downstream could take it back. This module holds the
  notice until the turn is over, so a workaround that succeeds can cancel it.

WHAT COUNTS AS RECOVERED — deliberately narrow
  Only a later success of the SAME tool and action, in the SAME turn, clears a
  notice. A retry that works, or a re-route that lands back on the same tool, is
  exactly that and is forgiven silently.

  A DIFFERENT tool succeeding does not clear it, even though that is often the
  real workaround. The broader rule — "anything succeeding afterwards clears
  everything" — would swallow a genuine failure inside a compound command
  ("close Photoshop and open Premiere"): Photoshop fails, Premiere opens, and the
  user is told nothing at all. Silence about a real failure is the worse of the
  two errors, so the narrow rule stands until the logs say how often the broad
  case actually arises. Measure first, per the board.

THE KNOWN HOLE IN THAT RULE, stated rather than discovered later
  The key is (tool, action) and NOT the parameters, and that choice has a cost.
  It is made because the workaround the prompt actually asks for is "the same tool
  with the parameter the error named as wrong" — a corrected retry. Keying on the
  parameters too would mean that exact workaround never cancels its own notice,
  which is the whole point missed.

  The price is a compound command that calls one tool twice with different
  targets: `file_controller read D:\a.txt` fails, `file_controller read D:\b.txt`
  succeeds, and a.txt's failure is forgiven although nothing recovered it. That is
  a real false negative. It is narrower than the one avoided above, and it is
  visible — `tool.recovery.unrecovered` and `tool.execution.failed` are both
  logged, so the gap between them is countable once there are real logs to count.
  Not pre-emptively fixed, because the fix (per-target keys) breaks the case this
  whole module exists to serve.

STALENESS IS NOT OPTIONAL
  Notices are flushed at a turn boundary and there are TWO of them — the cloud
  turn in `app/era_live/audio.py` and the local-brain turn in
  `app/era_live/local_fallback.py`. A missed flush must never surface a failure
  during a later, unrelated conversation, so anything older than `MAX_AGE_S` is
  dropped instead of spoken. Same discipline as `PENDING_MAX_AGE_S` in
  core/claude_error_router.py, and for the same reason.

  That window leaves one accepted residual: a session that drops mid-turn never
  reaches its boundary, so a notice armed just before the drop is announced at the
  end of the NEXT turn instead — possibly after the user has moved on. Within
  `MAX_AGE_S` that report is late but still true, and the alternative (clearing at
  turn start) would turn every mid-turn disconnect into silence about a real
  failure. Late beats silent, so the age window is the guard rather than a reset.

NOTHING HERE MAY RAISE. It sits in the tool funnel and in the audio receive loop;
  instrumentation that can throw is instrumentation that takes the voice with it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

# A notice older than this is never spoken. Long enough to cover a slow turn with
# several tool calls in it, far short of "the next time the user says something".
MAX_AGE_S = 90.0

# A bound on memory, not a policy: one batch of twenty failing calls must not
# build a twenty-item queue. Extras are counted (`overflow`) and dropped.
MAX_NOTICES = 5


@dataclass
class Notice:
    """One tool failure waiting to see whether the turn recovers from it."""

    tool: str
    action: str
    error_type: str
    message: str
    ts: float = 0.0

    def key(self) -> tuple[str, str]:
        return (self.tool, self.action)


@dataclass
class TurnRecovery:
    """The current turn's unresolved failures.

    Every method is safe to call at any time, in any order, any number of times.
    """

    clock: Callable[[], float] = time.monotonic
    _notices: list[Notice] = field(default_factory=list)
    overflow: int = 0
    dropped_stale: int = 0

    def _now(self) -> float:
        try:
            return float(self.clock())
        except Exception:
            return 0.0

    # ── marks ────────────────────────────────────────────────────────────────

    def note_failure(self, tool: str, action: str, error_type: str, message: str) -> None:
        """Arm a notice instead of speaking one."""
        if len(self._notices) >= MAX_NOTICES:
            self.overflow += 1
            return
        self._notices.append(
            Notice(
                tool=str(tool or "unknown"),
                action=str(action or ""),
                error_type=str(error_type or ""),
                message=str(message or ""),
                ts=self._now(),
            )
        )

    def note_success(self, tool: str, action: str) -> None:
        """The workaround worked: drop every armed notice for this tool+action.

        Every one, not the last: a tool retried twice before succeeding armed two
        notices, and announcing either of them would report a failure the user
        has no reason to hear about.
        """
        key = (str(tool or "unknown"), str(action or ""))
        self._notices = [n for n in self._notices if n.key() != key]

    # ── the reading ──────────────────────────────────────────────────────────

    def take(self) -> list[Notice]:
        """Drain the turn. Returns the notices still worth speaking, oldest
        first; stale ones are counted in `dropped_stale` and never returned."""
        now = self._now()
        fresh: list[Notice] = []
        stale = 0
        for n in self._notices:
            if now - n.ts > MAX_AGE_S:
                stale += 1
            else:
                fresh.append(n)
        self.dropped_stale = stale
        self._notices = []
        return fresh

    def pending(self) -> int:
        return len(self._notices)

    def reset(self) -> None:
        """Clear without reporting. For a turn that was abandoned, not answered."""
        self._notices = []
        self.overflow = 0
        self.dropped_stale = 0
