"""What Era was in the middle of when the transport died — and what to do about it.

WHY THIS EXISTS
    A Gemini Live reconnect restores what the MODEL knows. It says nothing about
    what the USER actually heard. Those are different facts and Era used to
    conflate them: the only per-turn state was `_live_turn_text`, a mirror of the
    output transcript. A transcript proves the model SAID something. It does not
    prove the speaker played it, and on a drop mid-sentence the difference is the
    whole bug — the reply stopped mid-word and nothing ever finished it.

    So this module tracks five things separately, because each can be true while
    the others are false:

        received   bytes of audio the server sent us
        queued     bytes handed to the player but not yet through the speaker
        played     bytes the speaker actually consumed
        generated  the model said it was done   (generation_complete)
        completed  the turn is closed           (turn_complete)

THE SIGNAL SEMANTICS ARE THE MODEL'S, NOT OURS
    Verified against https://ai.google.dev/api/live (2026-09-21):

      * `generationComplete` — "the model is done generating". This is the one
        that answers "was the answer finished?", and it is the signal Era never
        read. It only ever looked at `turn_complete`.
      * `turnComplete` — the turn is over and generation resumes only on a new
        client message. It fires in BOTH the finished and the interrupted case,
        so on its own it cannot tell them apart.
      * `interrupted` — a client message cut the model off. Crucially the docs
        state: "When model is interrupted while generating there will be no
        'generation_complete' message in interrupted turn, it will go through
        'interrupted > turn_complete'."

    That last sentence is what makes the decision below decidable at all:
    generation_complete present means finished, absent-with-interrupted means
    the user took the floor, absent-with-neither means we were cut off by the
    transport and owe the user the rest of the answer.

DELIBERATELY PURE
    No asyncio, no audio, no SDK types — it takes plain facts and returns a
    decision, so every branch in `decide_recovery` is unit-testable without a
    socket or a speaker. The engine wires it up in app/era_live/.
"""

from __future__ import annotations

from dataclasses import dataclass

# How much of a reply must exist before finishing it is worth doing at all.
# Below this, "continue" would resume almost nothing and just sound odd.
_MIN_RESUMABLE_CHARS = 40

# A recovered answer goes stale. Past this, the moment has passed and picking
# the thread back up unprompted reads as a non-sequitur rather than as
# attentiveness — a reconnect after a long quota cooldown must come up silent.
DEFAULT_RECOVERY_TTL_S = 120.0

# Decisions. Strings rather than an enum so they survive a log round-trip and
# read plainly in a test failure.
FINISH_PLAYBACK = "finish_playback"  # answer was done; just drain what we hold
ALREADY_RESUMED = "already_resumed"  # the new session is generating on its own
DEFER_TO_USER = "defer_to_user"  # the user spoke/interrupted; they own the floor
RESUME = "resume"  # send exactly one continuation
NOTHING = "nothing"  # nothing was in flight


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str

    @property
    def should_send_continuation(self) -> bool:
        return self.action == RESUME


@dataclass
class TurnSnapshot:
    """What was true about the assistant's turn at the instant the socket died."""

    spoken_text: str = ""  # transcript of what the model said this turn
    played_text: str = ""  # the conservative "definitely heard" prefix
    pending_user: str = ""  # a question still awaiting any answer at all
    generated: bool = False  # generation_complete arrived
    interrupted: bool = False  # the user cut in
    completed: bool = False  # turn_complete arrived
    audio_received: int = 0
    audio_played: int = 0
    ts: float = 0.0

    @property
    def playback_outstanding(self) -> int:
        """Bytes received but not yet played. > 0 means audio we still owe."""
        return max(0, self.audio_received - self.audio_played)

    @property
    def had_content(self) -> bool:
        return bool(self.spoken_text.strip() or self.pending_user.strip())


class LiveTurnState:
    """Mutable per-turn bookkeeping. One instance lives for the whole app."""

    def __init__(self) -> None:
        self.reset()

    # ── lifecycle ────────────────────────────────────────────────────────
    def reset(self) -> None:
        """Start a fresh turn. Called on turn_complete and on a new user turn."""
        self._spoken: list[str] = []
        self._committed: list[str] = []
        self._generated = False
        self._interrupted = False
        self._completed = False
        self._received = 0
        self._played = 0
        self._user_speaking = False

    # ── facts in ─────────────────────────────────────────────────────────
    def note_text(self, delta: str) -> None:
        if delta:
            self._spoken.append(delta)

    def note_audio_received(self, nbytes: int) -> None:
        if nbytes > 0:
            self._received += nbytes

    def note_audio_played(self, nbytes: int) -> None:
        """Bytes the speaker consumed. This is the ONLY evidence the user heard
        anything; a transcript is not."""
        if nbytes > 0:
            self._played += nbytes

    def note_sentence_committed(self, sentence: str) -> None:
        """One sentence has been fully handed to the speaker.

        This is the ONLY place transcript and audio are aligned at a boundary we
        can name: the local voice synthesizes sentence by sentence, so when the
        last frame of a sentence is enqueued we know that sentence is committed.
        Gemini's own streamed audio has no such boundary — a byte offset into it
        cannot be mapped back to a word — so in that mode this stays empty and
        `recovery_context` falls back to the full transcript.

        Committed is not the same as HEARD: the last committed sentence may still
        be sitting in the queue when the socket dies. `played_text` therefore
        drops it (see below), which is the conservative direction — the recovered
        answer may repeat a clause the user already heard, which is a far smaller
        fault than silently skipping one they never did.
        """
        sentence = (sentence or "").strip()
        if sentence:
            self._committed.append(sentence)

    @property
    def _played_text(self) -> str:
        """Everything committed EXCEPT the most recent sentence."""
        return " ".join(self._committed[:-1]).strip()

    def note_generation_complete(self) -> None:
        self._generated = True

    def note_interrupted(self) -> None:
        self._interrupted = True

    def note_turn_complete(self) -> None:
        self._completed = True

    def note_user_speaking(self, speaking: bool = True) -> None:
        self._user_speaking = speaking

    # ── facts out ────────────────────────────────────────────────────────
    @property
    def spoken_text(self) -> str:
        return " ".join(self._spoken).strip()

    @property
    def is_generating(self) -> bool:
        """Content has started arriving and nothing has closed the turn."""
        return bool(self._spoken or self._received) and not (
            self._generated or self._completed or self._interrupted
        )

    @property
    def playback_drained(self) -> bool:
        return self._played >= self._received

    @property
    def user_is_speaking(self) -> bool:
        return self._user_speaking

    def is_idle_for_renewal(self, tool_running: bool = False) -> bool:
        """Safe to reconnect right now with nothing audible being lost.

        Used for a GoAway: the server has told us it is going to close, and the
        kindest moment to take that is between turns — generation finished, the
        speaker drained, no tool mid-flight. See app/era_live's GoAway handler,
        which waits for this until the deadline forces its hand.
        """
        if tool_running or self._user_speaking:
            return False
        if self.is_generating:
            return False
        return self.playback_drained

    def snapshot(self, *, pending_user: str = "", now: float = 0.0) -> TurnSnapshot:
        return TurnSnapshot(
            spoken_text=self.spoken_text,
            played_text=self._played_text,
            pending_user=pending_user.strip(),
            generated=self._generated,
            interrupted=self._interrupted,
            completed=self._completed,
            audio_received=self._received,
            audio_played=self._played,
            ts=now,
        )


def decide_recovery(
    snap: TurnSnapshot | None,
    *,
    now: float,
    user_spoke_since: bool = False,
    generation_active: bool = False,
    ttl_s: float = DEFAULT_RECOVERY_TTL_S,
) -> RecoveryDecision:
    """What the reconnect owes the user, given what was in flight when it died.

    The order of these branches is the contract, and each one exists because
    getting it wrong produces a specific bad behaviour:

      1. nothing in flight            -> say nothing (the common case; a drop
                                         while idle must stay silent)
      2. the user is talking now      -> they own the floor; resuming would talk
                                         over a live human
      3. the model is already talking -> a second request here is the duplicate
                                         speech this whole feature must not cause
      4. the user interrupted         -> they chose to cut it off; finishing it
                                         unasked reverses their decision
      5. generation finished          -> the words all exist; if any audio is
                                         still buffered just play it out, and
                                         never re-ask the model for an answer it
                                         already gave (that is how you get the
                                         same paragraph twice)
      6. stale                        -> the moment has passed
      7. otherwise                    -> genuinely cut off mid-answer: resume,
                                         exactly once
    """
    if snap is None or not snap.had_content:
        return RecoveryDecision(NOTHING, "nothing was in flight")

    if user_spoke_since:
        return RecoveryDecision(DEFER_TO_USER, "the user has taken the floor")

    if generation_active:
        return RecoveryDecision(ALREADY_RESUMED, "the session is already generating")

    if snap.interrupted:
        return RecoveryDecision(DEFER_TO_USER, "the user interrupted this turn")

    if snap.generated:
        if snap.playback_outstanding > 0:
            return RecoveryDecision(
                FINISH_PLAYBACK,
                f"answer complete; {snap.playback_outstanding} bytes still to play",
            )
        return RecoveryDecision(NOTHING, "the answer finished and was fully played")

    if snap.ts and (now - snap.ts) > ttl_s:
        return RecoveryDecision(NOTHING, f"stale by {now - snap.ts:.0f}s")

    if len(snap.spoken_text) + len(snap.pending_user) < _MIN_RESUMABLE_CHARS:
        return RecoveryDecision(NOTHING, "too little was said to be worth finishing")

    return RecoveryDecision(RESUME, "generation was cut off mid-answer")


def recovery_context(snap: TurnSnapshot) -> tuple[str, str]:
    """(already_said, still_unanswered) to put in the continuation note.

    Prefers the text we can show was actually PLAYED. Where that is unavailable
    the whole transcript is used instead — deliberately conservative: repeating
    a clause the user already heard is a far smaller fault than skipping one
    they did not, and no byte offset into a stream of synthesized audio can tell
    you which word it landed on.
    """
    said = (snap.played_text or snap.spoken_text).strip()
    return said, snap.pending_user.strip()
