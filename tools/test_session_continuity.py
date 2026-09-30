"""
test_session_continuity.py — Era must finish the sentence a dropped connection
cut off, WITHOUT the user having to ask her to continue.

The resumption handle restores what the MODEL knows. It does nothing about the
reply that was half-spoken when the socket died: before this, the audio stopped
mid-word, the new session came up holding the whole conversation, and then sat
there until the user noticed the silence and said "carry on". Every time.

Four pieces, tested here:

  1. TurnMemory now records WHO cut the turn off, and can hold an unanswered
     question (a drop that lands before Era gets a word out).
  2. build_disconnect_resume_note — the note that makes her carry on. It must
     contain no question (the 2026-07-21 bug: Gemini ANSWERS a question fed
     into the session, and once acted on the topic it named), forbid tools, and
     never mention the connection.
  3. _capture_turn_cut_by_drop / _resume_after_disconnect — stash on the way
     down, hand back on the way up; refuses a barge-in, standby, and a session
     that does not exist.
  4. _is_resumption_rejection — a dead handle must be dropped, not replayed
     forever behind a 15-minute "config rejected" wait.

Run:  pytest tools/test_session_continuity.py -v
"""

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.text_intents import build_disconnect_resume_note  # noqa: E402
from core.live_turn_state import LiveTurnState  # noqa: E402
from core.turn_memory import TurnMemory  # noqa: E402

_SPOKEN = "The quarterly numbers are up eleven percent, and the reason is"
_REMAINDER = "almost entirely the Ceram Quartz launch."
_ASKED = "what did the quarterly report actually say about the launch?"


# ── 1. TurnMemory carries the cause ──────────────────────────────────────────


def test_capture_defaults_to_a_user_barge_in():
    tm = TurnMemory()
    assert tm.capture(spoken=_SPOKEN)
    assert tm.peek().cut_by == "user"


def test_capture_records_a_disconnect_as_such():
    tm = TurnMemory()
    assert tm.capture(spoken=_SPOKEN, cut_by="disconnect")
    assert tm.peek().cut_by == "disconnect"


def test_an_unanswered_question_alone_is_substantive_enough_to_stash():
    """A drop between the question and Era's first word leaves no `spoken`
    text at all. The old substance test looked only at spoken + remainder and
    would have thrown away the one thing worth resuming."""
    tm = TurnMemory()
    assert tm.capture(spoken="", cut_by="disconnect", pending_user=_ASKED)
    assert tm.peek().pending_user == _ASKED


def test_a_thin_fragment_is_still_not_worth_stashing():
    tm = TurnMemory()
    assert not tm.capture(spoken="As I—", cut_by="disconnect")
    assert tm.peek() is None


def test_a_stale_stash_expires_rather_than_resuming_late():
    """A reconnect after a two-hour quota cooldown must come up silent, not
    answer a question the user has long since forgotten."""
    tm = TurnMemory(ttl_s=120.0)
    tm.capture(spoken=_SPOKEN, cut_by="disconnect", now=1000.0)
    assert tm.peek(now=1100.0) is not None
    assert tm.peek(now=1200.0) is None


# ── 2. The note itself ───────────────────────────────────────────────────────


def test_resume_note_carries_what_was_already_said():
    note = build_disconnect_resume_note(_SPOKEN, _REMAINDER)
    assert _SPOKEN in note
    assert _REMAINDER in note


def test_resume_note_asks_nothing():
    """The whole reason the old reconnect apology was deleted: text fed into a
    Live session is conversational input, not narration. A question in it gets
    answered, and an action named in it can get performed."""
    note = build_disconnect_resume_note(_SPOKEN, _REMAINDER)
    assert "?" not in note
    note_no_speech = build_disconnect_resume_note("", "", _ASKED)
    # The user's own question is quoted, so exactly one '?' may appear — and it
    # must be inside the quoted span, never the note's own voice.
    assert note_no_speech.count("?") == _ASKED.count("?")


def test_resume_note_forbids_tools_and_actions():
    for note in (
        build_disconnect_resume_note(_SPOKEN, _REMAINDER),
        build_disconnect_resume_note("", "", _ASKED),
    ):
        assert "Do not use any tool" in note
        assert "do not take any action" in note


def test_resume_note_never_apologises_for_the_drop():
    """ "Sorry, I lost connection" is the robotic tell. From the user's side the
    reply merely paused; the repair should sound like picking a sentence up."""
    for note in (
        build_disconnect_resume_note(_SPOKEN, _REMAINDER),
        build_disconnect_resume_note("", "", _ASKED),
    ):
        assert "do not apologise" in note.lower()
        assert "do not mention the connection" in note.lower()


def test_resume_note_without_spoken_text_answers_the_question_instead():
    note = build_disconnect_resume_note("", "", _ASKED)
    assert _ASKED in note
    assert "Answer it now" in note


def test_resume_note_is_bounded_so_a_long_reply_cannot_blow_up_the_turn():
    note = build_disconnect_resume_note("x" * 5000, "y" * 5000, "z" * 5000)
    assert len(note) < 1600


# ── 3. The engine path ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def main_module():
    import main

    return main


@pytest.fixture()
def era(main_module):
    """An EraLive-shaped stub carrying only what the two methods touch."""

    class _Stub:
        pass

    stub = _Stub()
    stub.logged = []
    stub.sent = []
    stub.turn_memory = TurnMemory(ttl_s=120.0)
    # The REAL turn-state machine, not a fake: _capture_turn_cut_by_drop now
    # snapshots it, so stubbing it out would test nothing. See
    # core/live_turn_state.py.
    stub.live_turn = LiveTurnState()
    stub._disconnect_snapshot = None
    stub._user_spoke_since_drop = False
    stub._conn_generation = 1
    stub._conn_started_at = time.time()
    stub._live_turn_text = ""
    stub._recap_pending_user = ""
    stub._standby_active = False
    stub._cm_handoff = False
    stub._cm_handoff_active = lambda: stub._cm_handoff
    stub.speech_out = types.SimpleNamespace(pending_text=lambda: "")
    stub.ui = types.SimpleNamespace(write_log=lambda line: stub.logged.append(line))

    async def _send(text=None, **_):
        stub.sent.append(text)

    stub.session = types.SimpleNamespace(send_realtime_input=_send)

    cls = main_module.EraLive
    stub._capture_turn_cut_by_drop = cls._capture_turn_cut_by_drop.__get__(stub)
    stub._resume_after_disconnect = cls._resume_after_disconnect.__get__(stub)
    return stub


def test_a_drop_mid_sentence_is_stashed_then_resumed(era):
    era.live_turn.note_text(_SPOKEN)
    era.live_turn.note_audio_received(4000)
    era._live_turn_text = _SPOKEN
    assert era._capture_turn_cut_by_drop()
    assert asyncio.run(era._resume_after_disconnect())
    assert len(era.sent) == 1
    assert _SPOKEN in era.sent[0]


def test_a_drop_before_the_first_word_resumes_the_question(era):
    # Nothing said yet — the drop landed between the question and the answer.
    era._recap_pending_user = _ASKED
    assert era._capture_turn_cut_by_drop()
    assert asyncio.run(era._resume_after_disconnect())
    assert _ASKED in era.sent[0]


def test_nothing_in_flight_means_nothing_to_resume(era):
    assert not era._capture_turn_cut_by_drop()
    assert not asyncio.run(era._resume_after_disconnect())
    assert era.sent == []


def test_a_barge_in_is_never_resumed_automatically(era):
    """The user took the floor on purpose. Talking back over them on the next
    rotation is the opposite of the fix.

    The premise now comes from the server's own `interrupted` signal rather than
    from a local cut_by label — per https://ai.google.dev/api/live an interrupted
    turn reports `interrupted` and then `turn_complete` with no
    `generation_complete`, which is exactly what decide_recovery reads."""
    era.live_turn.note_text(_SPOKEN)
    era.live_turn.note_audio_received(4000)
    era.live_turn.note_interrupted()
    era._capture_turn_cut_by_drop()
    assert not asyncio.run(era._resume_after_disconnect())
    assert era.sent == []
    # ...and the legacy stash survives, so an explicit "go on" still works.
    assert era.turn_memory.has_pending()


def test_the_stash_is_consumed_so_a_rotation_storm_cannot_repeat_it(era):
    era.live_turn.note_text(_SPOKEN)
    era.live_turn.note_audio_received(4000)
    era._live_turn_text = _SPOKEN
    era._capture_turn_cut_by_drop()
    assert asyncio.run(era._resume_after_disconnect())
    assert not asyncio.run(era._resume_after_disconnect())
    assert len(era.sent) == 1


def test_standby_stays_silent(era):
    era.live_turn.note_text(_SPOKEN)
    era.live_turn.note_audio_received(4000)
    era._live_turn_text = _SPOKEN
    era._capture_turn_cut_by_drop()
    era._standby_active = True
    assert not asyncio.run(era._resume_after_disconnect())
    assert era.sent == []


def test_a_dead_session_cannot_be_spoken_into(era):
    era.live_turn.note_text(_SPOKEN)
    era.live_turn.note_audio_received(4000)
    era._live_turn_text = _SPOKEN
    era._capture_turn_cut_by_drop()
    era.session = None
    assert not asyncio.run(era._resume_after_disconnect())


def test_cm_handoff_speech_is_not_the_users_conversation(era):
    """That speech belongs to the headless CM lane, which reports itself via
    _deliver_or_queue_cm_report."""
    era.live_turn.note_text(_SPOKEN)
    era._live_turn_text = _SPOKEN
    era._cm_handoff = True
    assert not era._capture_turn_cut_by_drop()


def test_capture_clears_the_mirror_so_a_second_drop_cannot_restash_it(era):
    era.live_turn.note_text(_SPOKEN)
    era.live_turn.note_audio_received(4000)
    era._live_turn_text = _SPOKEN
    era._capture_turn_cut_by_drop()
    assert era._live_turn_text == ""
    assert not era._capture_turn_cut_by_drop()


# ── 4. A resumption handle the server will not take back ─────────────────────


def _rejection(main_module, message):
    return main_module.EraLive._is_resumption_rejection(Exception(message))


@pytest.mark.parametrize(
    "message",
    [
        "INVALID_ARGUMENT: session resumption handle is invalid",
        "NOT_FOUND: no such handle",
        "Invalid resumption handle",
    ],
)
def test_a_refused_handle_is_recognised(main_module, message):
    assert _rejection(main_module, message)


def test_an_ordinary_network_drop_is_not_mistaken_for_a_bad_handle(main_module):
    assert not _rejection(main_module, "1006 abnormal closure: connection reset")


def test_a_refused_handle_is_found_inside_a_taskgroup_group(main_module):
    group = ExceptionGroup(  # noqa: F821  (builtin on 3.11+)
        "unhandled errors in a TaskGroup",
        [ValueError("INVALID_ARGUMENT: bad session resumption handle")],
    )
    assert main_module.EraLive._is_resumption_rejection(group)


# ── 5. Unlimited sessions ────────────────────────────────────────────────────


def test_the_connect_config_asks_for_sliding_window_compression():
    """Without this the session dies when its context window fills — which, with
    Era's system prompt plus audio in both directions, is every couple of
    minutes. Each of those is a drop the user hears as Era going quiet."""
    source = Path(__file__).resolve().parent.parent / "app" / "era_live" / "tool_dispatch.py"
    text = source.read_text(encoding="utf-8")
    assert "context_window_compression=compression_config" in text
    assert "types.SlidingWindow()" in text


# ── 6. Shutdown must end the connect loop, not feed it ───────────────────────
# Regression, 2026-09-21: the UI closed at 12:02 and the process kept
# reconnecting until 14:31, emitting 177 ERROR engine.reconnect_error entries
# with full tracebacks — the largest signature in that day's DEGRADED verdict,
# and not one of them a real connection fault. The engine thread is a daemon,
# so it is only killed at Py_Finalize; a blocked non-daemon executor worker
# stops the interpreter ever getting there, and meanwhile concurrent.futures'
# atexit hook has already set the flag that makes every DNS lookup — and so
# every connect — raise instantly and permanently.


_SHUTDOWN_MESSAGE = "cannot schedule new futures after interpreter shutdown"


def test_interpreter_shutdown_is_recognised(main_module):
    assert main_module.EraLive._is_interpreter_shutdown(RuntimeError(_SHUTDOWN_MESSAGE))


def test_interpreter_shutdown_is_found_inside_a_taskgroup_group(main_module):
    """The connect runs under asyncio.TaskGroup, so the real one always arrives
    wrapped — an unwrapped-only check would never have fired in production."""
    group = ExceptionGroup(  # noqa: F821  (builtin on 3.11+)
        "unhandled errors in a TaskGroup",
        [RuntimeError(_SHUTDOWN_MESSAGE)],
    )
    assert main_module.EraLive._is_interpreter_shutdown(group)


def test_an_ordinary_drop_is_not_mistaken_for_shutdown(main_module):
    """This guard returns out of run(), so a false positive would take Era
    offline for good on a passing Wi-Fi blip."""
    for message in (
        "1006 abnormal closure: connection reset",
        "1008 policy violation: The operation was aborted",
        "RESOURCE_EXHAUSTED: quota exceeded",
        "INVALID_ARGUMENT: session resumption handle is invalid",
    ):
        assert not main_module.EraLive._is_interpreter_shutdown(Exception(message)), message


def test_a_runtime_error_that_is_not_about_shutdown_still_reconnects(main_module):
    assert not main_module.EraLive._is_interpreter_shutdown(RuntimeError("no running event loop"))


def test_the_connect_loop_stops_on_shutdown_rather_than_retrying(main_module):
    """Both exits must be present: the flag check catches a loop sitting
    between attempts, the exception check catches one inside a failing attempt.
    Either alone leaves a path that spins."""
    source = Path(__file__).resolve().parent.parent / "main.py"
    text = source.read_text(encoding="utf-8-sig")
    body = text.split("    async def run(self):", 1)[1]
    assert "if self._shutdown_requested:" in body
    assert "if self._is_interpreter_shutdown(e):" in body


def test_closing_the_window_forces_the_process_to_exit(main_module):
    """A daemon engine thread does NOT die on its own when a non-daemon worker
    hangs the interpreter's join. os._exit is the only door that cannot be
    blocked, so the guard must be wired to the mainloop's return."""
    assert hasattr(main_module, "_force_exit_after_ui_close")
    source = Path(__file__).resolve().parent.parent / "main.py"
    text = source.read_text(encoding="utf-8-sig")
    after_mainloop = text.split("ui.root.mainloop()", 1)[1]
    assert "_force_exit_after_ui_close(active_era_instance)" in after_mainloop


def test_the_forced_exit_sets_the_stop_flag_and_never_blocks_exit(main_module):
    """The timer must be a daemon and must not be joined: a guard that can
    itself hold the process open is not a guard."""
    import threading

    started = {}
    real_timer = threading.Timer

    class _CapturingTimer(real_timer):
        def start(self):
            started["delay"] = self.interval
            started["daemon"] = self.daemon
            # Deliberately not started — firing it would kill the test run.

    class _Era:
        _shutdown_requested = False

    era = _Era()
    threading.Timer = _CapturingTimer
    try:
        main_module._force_exit_after_ui_close(era, grace_seconds=0.25)
    finally:
        threading.Timer = real_timer

    assert era._shutdown_requested is True
    assert started["daemon"] is True
    assert started["delay"] == 0.25
