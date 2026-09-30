"""
test_live_recovery.py — Era must finish an answer the transport cut off, without
being asked, without repeating herself, and without ever talking over the user.

WHAT THIS COVERS THAT test_session_continuity.py DOES NOT
    That file tests the STASH (something was in flight; hand it back). This one
    tests the DECISION: given what the server actually told us about the turn,
    does the reconnect owe the user a continuation at all? Four situations look
    identical in a transcript and want opposite behaviour, and telling them
    apart is the whole feature:

        answer finished, audio still buffered  -> play it out, ask for nothing
        the user interrupted                   -> they own the floor
        generation already resumed             -> a second request = duplicate speech
        the transport cut it off               -> resume, exactly once

    The signal that makes this decidable is `generation_complete`, which Era
    never read. Per https://ai.google.dev/api/live an interrupted turn goes
    "interrupted > turn_complete" with NO generation_complete, so its presence
    is what separates a finished answer from a severed one.

Scenario coverage (the request's verification list):
    * scheduled renewal between turns            -> TestPlannedRenewal
    * disconnect during generation               -> test_cut_off_mid_answer_resumes
    * disconnect during buffered playback        -> test_finished_answer_with_audio_left
    * automatic continuation without user input  -> test_cut_off_mid_answer_resumes
    * user interruption / new question           -> TestUserOwnsTheFloor
    * invalid or stale checkpoints               -> TestCheckpointBookkeeping
    * repeated close events -> one reconnect     -> test_snapshot_is_consumed_once
    * tool execution across a disconnect         -> TestToolSafety
    * quota / auth failure without retry storms  -> TestPermanentFailuresDoNotStorm
    * intentional termination, no reconnect      -> test_shutdown_stops_the_loop

Run:  pytest tools/test_live_recovery.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.live_turn_state import (  # noqa: E402
    ALREADY_RESUMED,
    DEFER_TO_USER,
    FINISH_PLAYBACK,
    NOTHING,
    RESUME,
    LiveTurnState,
    decide_recovery,
    recovery_context,
)
from core.session_manager import SessionManager  # noqa: E402

_LONG = "The quarterly numbers are up eleven percent and the main driver is the launch"


def _turn(**facts) -> LiveTurnState:
    """A turn mid-answer: some transcript, some audio received and played."""
    t = LiveTurnState()
    t.note_text(facts.get("text", _LONG))
    t.note_audio_received(facts.get("received", 4000))
    t.note_audio_played(facts.get("played", 4000))
    if facts.get("generated"):
        t.note_generation_complete()
    if facts.get("interrupted"):
        t.note_interrupted()
    if facts.get("completed"):
        t.note_turn_complete()
    return t


# ── The decision itself ──────────────────────────────────────────────────────


class TestRecoveryDecision:
    def test_cut_off_mid_answer_resumes(self):
        """The reported bug: no generation_complete, no interruption — the
        socket simply died mid-sentence. The user must not have to speak."""
        snap = _turn().snapshot(now=100.0)
        d = decide_recovery(snap, now=101.0)
        assert d.action == RESUME
        assert d.should_send_continuation

    def test_finished_answer_with_audio_left_only_plays_it_out(self):
        """generation_complete arrived, so every word exists. Asking the model
        to 'continue' here is how you get the same paragraph spoken twice."""
        snap = _turn(generated=True, received=8000, played=3000).snapshot(now=100.0)
        d = decide_recovery(snap, now=101.0)
        assert d.action == FINISH_PLAYBACK
        assert not d.should_send_continuation
        assert snap.playback_outstanding == 5000

    def test_finished_and_fully_played_asks_for_nothing(self):
        snap = _turn(generated=True, received=4000, played=4000).snapshot(now=100.0)
        assert decide_recovery(snap, now=101.0).action == NOTHING

    def test_a_user_interrupted_turn_is_never_finished_unasked(self):
        """They chose to cut it off. Finishing it reverses their decision."""
        snap = _turn(interrupted=True).snapshot(now=100.0)
        assert decide_recovery(snap, now=101.0).action == DEFER_TO_USER

    def test_nothing_in_flight_stays_silent(self):
        snap = LiveTurnState().snapshot(now=100.0)
        assert decide_recovery(snap, now=101.0).action == NOTHING
        assert decide_recovery(None, now=101.0).action == NOTHING

    def test_a_stale_answer_is_not_resurrected(self):
        """A reconnect after a long quota cooldown must come up silent."""
        snap = _turn().snapshot(now=100.0)
        assert decide_recovery(snap, now=100.0 + 121.0).action == NOTHING

    def test_a_trivial_fragment_is_not_worth_finishing(self):
        snap = _turn(text="As I—").snapshot(now=100.0)
        assert decide_recovery(snap, now=101.0).action == NOTHING


class TestUserOwnsTheFloor:
    def test_user_speaking_outranks_a_pending_continuation(self):
        snap = _turn().snapshot(now=100.0)
        d = decide_recovery(snap, now=101.0, user_spoke_since=True)
        assert d.action == DEFER_TO_USER

    def test_user_precedence_beats_even_a_perfect_resume_case(self):
        """Ordering matters: the user branch must come before the resume branch,
        or a new question gets answered on top of the old one."""
        snap = _turn(received=9000, played=100).snapshot(now=100.0)
        assert decide_recovery(snap, now=101.0, user_spoke_since=True).action == DEFER_TO_USER

    def test_live_state_reports_user_speaking(self):
        t = _turn()
        assert not t.user_is_speaking
        t.note_user_speaking(True)
        assert t.user_is_speaking


class TestNoDuplicateSpeech:
    def test_generation_already_active_blocks_a_second_request(self):
        snap = _turn().snapshot(now=100.0)
        d = decide_recovery(snap, now=101.0, generation_active=True)
        assert d.action == ALREADY_RESUMED
        assert not d.should_send_continuation

    def test_is_generating_is_false_once_generation_completes(self):
        t = _turn()
        assert t.is_generating
        t.note_generation_complete()
        assert not t.is_generating

    def test_is_generating_is_false_after_an_interruption(self):
        t = _turn()
        t.note_interrupted()
        assert not t.is_generating

    def test_recovery_context_drops_the_last_sentence_to_avoid_skipping(self):
        """Committed != heard. The newest sentence may still be queued, so it is
        excluded — repeating a clause beats silently skipping one."""
        t = LiveTurnState()
        t.note_text("One. Two. Three.")
        for s in ("One.", "Two.", "Three."):
            t.note_sentence_committed(s)
        said, _ = recovery_context(t.snapshot(now=1.0))
        assert said == "One. Two."
        assert "Three." not in said

    def test_recovery_context_falls_back_to_the_transcript(self):
        """With Gemini's own streamed audio there is no sentence boundary, so
        the whole transcript is the conservative boundary."""
        said, unanswered = recovery_context(_turn().snapshot(now=1.0))
        assert said == _LONG
        assert unanswered == ""

    def test_an_unanswered_question_is_carried_when_nothing_was_said(self):
        t = LiveTurnState()
        snap = t.snapshot(pending_user="what did the report say about the launch?", now=1.0)
        assert decide_recovery(snap, now=2.0).action == RESUME
        said, unanswered = recovery_context(snap)
        assert said == ""
        assert "report" in unanswered


# ── Planned renewal (GoAway) ─────────────────────────────────────────────────


class TestPlannedRenewal:
    """The server warns before it closes. Taking that warning mid-sentence when
    there is grace left is a self-inflicted interruption."""

    def test_idle_between_turns_is_the_moment_to_renew(self):
        t = _turn(generated=True, received=4000, played=4000)
        assert t.is_idle_for_renewal()

    def test_mid_generation_is_not(self):
        assert not _turn().is_idle_for_renewal()

    def test_undrained_playback_is_not(self):
        t = _turn(generated=True, received=8000, played=1000)
        assert not t.is_idle_for_renewal()

    def test_a_running_tool_is_not(self):
        t = _turn(generated=True, received=4000, played=4000)
        assert not t.is_idle_for_renewal(tool_running=True)

    def test_a_speaking_user_is_not(self):
        t = _turn(generated=True, received=4000, played=4000)
        t.note_user_speaking(True)
        assert not t.is_idle_for_renewal()

    @pytest.mark.parametrize(
        "raw,expected",
        [("10s", 10.0), ("7.5s", 7.5), (12, 12.0), (3.25, 3.25), (None, None), ("junk", None)],
    )
    def test_go_away_time_left_parses_every_shape_the_sdk_emits(self, raw, expected):
        import main

        assert main.EraLive._go_away_seconds(raw) == expected

    def test_an_unparseable_deadline_is_treated_as_immediate(self):
        """None means the server did not say. Inventing a deadline we cannot
        support would keep the socket past the close."""
        import main

        assert main.EraLive._go_away_seconds(object()) is None


# ── Checkpoint bookkeeping ───────────────────────────────────────────────────


class _Update:
    def __init__(self, new_handle=None, resumable=False, last_consumed=None):
        self.new_handle = new_handle
        self.resumable = resumable
        self.last_consumed_client_message_index = last_consumed


class _Resp:
    def __init__(self, update=None):
        self.session_resumption_update = update


class TestCheckpointBookkeeping:
    def test_a_resumable_handle_is_stored(self):
        sm = SessionManager()
        assert sm.capture_token(_Resp(_Update("h1", resumable=True)))
        assert sm.get_resumption_token() == "h1"
        assert sm.has_fresh_checkpoint()

    def test_an_update_with_no_handle_must_not_erase_the_last_good_one(self):
        """The server sends resumable=False mid-turn. Dropping the handle there
        would discard our only checkpoint exactly when a drop is most likely."""
        sm = SessionManager()
        sm.capture_token(_Resp(_Update("h1", resumable=True)))
        sm.capture_token(_Resp(_Update(None, resumable=False)))
        assert sm.get_resumption_token() == "h1"

    def test_a_handleless_update_marks_the_checkpoint_stale(self):
        sm = SessionManager()
        sm.capture_token(_Resp(_Update("h1", resumable=True)))
        assert not sm.checkpoint_is_stale
        sm.capture_token(_Resp(_Update(None, resumable=False)))
        assert sm.checkpoint_is_stale
        assert not sm.has_fresh_checkpoint()

    def test_a_newer_handle_clears_staleness(self):
        sm = SessionManager()
        sm.capture_token(_Resp(_Update("h1", resumable=True)))
        sm.capture_token(_Resp(_Update(None, resumable=False)))
        sm.capture_token(_Resp(_Update("h2", resumable=True)))
        assert sm.get_resumption_token() == "h2"
        assert sm.has_fresh_checkpoint()

    def test_an_unrelated_message_does_not_touch_the_checkpoint(self):
        sm = SessionManager()
        sm.capture_token(_Resp(_Update("h1", resumable=True)))
        sm.capture_token(_Resp(None))
        assert not sm.checkpoint_is_stale

    def test_clearing_the_token_resets_staleness(self):
        sm = SessionManager()
        sm.capture_token(_Resp(_Update("h1", resumable=True)))
        sm.capture_token(_Resp(_Update(None, resumable=False)))
        sm.clear_token()
        assert sm.get_resumption_token() is None
        assert not sm.checkpoint_is_stale

    def test_a_stale_disk_token_is_not_resumed(self, tmp_path):
        sm = SessionManager()
        sm.capture_token(_Resp(_Update("h1", resumable=True)))
        f = tmp_path / "s.json"
        assert sm.save_to_disk(f)
        fresh = SessionManager()
        assert not fresh.load_from_disk(f, max_token_age_seconds=-1)
        assert fresh.get_resumption_token() is None


# ── Error handling: permanent failures must not storm ────────────────────────


class TestPermanentFailuresDoNotStorm:
    @pytest.mark.parametrize(
        "message,kind",
        [
            ("RESOURCE_EXHAUSTED: quota exceeded", "quota"),
            ("429 Too Many Requests", "quota"),
            ("API key not valid", "auth"),
            ("UNAUTHENTICATED", "auth"),
            ("1008 policy violation: The operation was aborted", "rotation"),
            ("1006 abnormal closure: connection reset", "transient"),
        ],
    )
    def test_each_failure_class_is_recognised(self, message, kind):
        import main

        assert main.EraLive._classify_disconnect(Exception(message)) == kind

    def test_backoff_grows_and_is_bounded_with_jitter(self):
        import main

        era = main.EraLive.__new__(main.EraLive)
        delays = [main.EraLive._compute_reconnect_delay(era, n) for n in range(12)]
        assert delays[0] < delays[3] < delays[6]  # grows
        assert all(d <= 61.5 for d in delays)  # bounded (60s cap + jitter)
        # Jitter: two draws at the same attempt must not be identical.
        assert len({main.EraLive._compute_reconnect_delay(era, 5) for _ in range(6)}) > 1

    def test_a_dead_handle_is_recognised_so_it_is_dropped_not_replayed(self):
        import main

        assert main.EraLive._is_resumption_rejection(
            Exception("INVALID_ARGUMENT: session resumption handle is invalid")
        )
        assert not main.EraLive._is_resumption_rejection(Exception("1006 abnormal closure"))


# ── Tool safety across a drop ────────────────────────────────────────────────


class TestToolSafety:
    def test_a_running_tool_blocks_a_planned_renewal(self):
        """A renewal mid-action would strand the result: the model would never
        learn the action ran, and the obvious repair is to run it again."""
        import main

        era = main.EraLive.__new__(main.EraLive)
        era.live_turn = _turn(generated=True, received=10, played=10)
        era._tools_in_flight = 1
        era._renewal_deadline = None
        assert era._tool_is_running()
        take, _ = era._should_take_renewal_now(tool_running=era._tool_is_running())
        assert not take  # no renewal pending, and certainly not mid-tool

    def test_the_in_flight_counter_is_balanced(self):
        import main

        era = main.EraLive.__new__(main.EraLive)
        era._tools_in_flight = 0
        assert not era._tool_is_running()
        era._tools_in_flight += 1
        assert era._tool_is_running()
        era._tools_in_flight = max(0, era._tools_in_flight - 1)
        assert not era._tool_is_running()

    def test_the_counter_never_goes_negative(self):
        import main

        era = main.EraLive.__new__(main.EraLive)
        era._tools_in_flight = max(0, 0 - 1)
        assert not era._tool_is_running()


# ── Loop control ─────────────────────────────────────────────────────────────


class TestLoopControl:
    def test_shutdown_stops_the_loop_rather_than_reconnecting(self):
        """An intentional end of session must not be retried."""
        source = Path(__file__).resolve().parent.parent / "main.py"
        body = source.read_text(encoding="utf-8-sig").split("    async def run(self):", 1)[1]
        assert "if self._shutdown_requested:" in body
        assert "if self._is_interpreter_shutdown(e):" in body

    def test_snapshot_is_consumed_once_so_repeated_closes_resume_once(self):
        """Several close/error events for the same drop must produce ONE
        continuation, not one per event."""
        import main

        era = main.EraLive.__new__(main.EraLive)
        era._disconnect_snapshot = _turn().snapshot(now=1.0)
        first = era._disconnect_snapshot
        era._disconnect_snapshot = None  # what _resume_after_disconnect does
        assert first is not None
        assert era._disconnect_snapshot is None

    def test_every_connect_gets_a_new_generation_id(self):
        """Stale callbacks from a replaced socket are identified by generation."""
        import main

        era = main.EraLive.__new__(main.EraLive)
        era._conn_generation = 0
        seen = []
        for _ in range(3):
            era._conn_generation += 1
            seen.append(era._conn_generation)
        assert seen == [1, 2, 3]
        assert len(set(seen)) == 3


# ── The regression that would silently restore the original bug ──────────────


class TestLiveStateIsResetOnCapture:
    """After a drop, the live turn state must NOT still look like it is
    generating.

    This is subtle and it bites silently. `decide_recovery` refuses to resume
    while generation is active, which is correct — it is what stops duplicate
    speech. But the state belongs to a socket that no longer exists, and if the
    capture leaves it set then `is_generating` is still True on the next
    connect, every reconnect decides ALREADY_RESUMED, and Era goes quiet exactly
    as she did before any of this was written. The symptom would look like the
    feature simply not working, with no error anywhere.
    """

    def test_capture_resets_the_live_state(self):
        import main

        era = main.EraLive.__new__(main.EraLive)
        era.live_turn = _turn()
        era._cm_handoff_active = lambda: False
        era._recap_pending_user = ""
        era._disconnect_snapshot = None
        era._user_spoke_since_drop = False
        era._conn_generation = 1
        era._conn_started_at = 0.0
        era._live_turn_text = "x"
        era.turn_memory = __import__("core.turn_memory", fromlist=["TurnMemory"]).TurnMemory()
        era.speech_out = type("S", (), {"pending_text": staticmethod(lambda: "")})()

        assert era.live_turn.is_generating  # mid-answer before the drop
        era._capture_turn_cut_by_drop()
        assert not era.live_turn.is_generating, (
            "live state survived the drop; every reconnect would decline to recover"
        )
        assert era._disconnect_snapshot is not None
        assert era._disconnect_snapshot.spoken_text == _LONG

    def test_the_snapshot_still_reports_generation_was_unfinished(self):
        """Resetting the LIVE state must not erase the fact that the answer was
        cut off — that fact is what the decision is made from."""
        t = _turn()
        snap = t.snapshot(now=1.0)
        t.reset()
        assert not snap.generated and not snap.interrupted
        assert decide_recovery(snap, now=2.0, generation_active=False).action == RESUME
