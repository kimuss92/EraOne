"""PF-039: "stay silent" / "remain silent" -> inactivity mode at once.

Owner 2026-11-16: saying "remain silent" or "stay silent" must immediately put
Era into inactivity mode, so she answers only when named again. It must be the
inactivity window (mic + Live session warm), never the real STANDBY (wake word
only), and Era must not answer the order out loud.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

from core.attention import ListeningState, is_silence_order


@pytest.mark.parametrize(
    "text",
    [
        "stay silent",
        "Remain silent.",
        "Era, stay silent",
        "era stay quiet please",
        "Okay Era, remain silent now",
        "be quiet",
        "Keep quiet, sir.",
        "Era One, stay silent for now",
        "reste silencieuse",
    ],
)
def test_silence_orders_are_recognised(text):
    assert is_silence_order(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "stay silent when I'm on a call",
        "why did you stay silent",
        "don't stay silent",
        "the movie stays silent at the end",
        "silent night",
        "can you tell me who said stay silent",
        "stay",
    ],
)
def test_other_sentences_are_not_silence_orders(text):
    assert not is_silence_order(text)


class _UI:
    def __init__(self):
        self.logs, self.states = [], []

    def write_log(self, text):
        self.logs.append(text)

    def set_state(self, state):
        self.states.append(state)


def _era():
    import main

    state = ListeningState(window_s=120)
    era = SimpleNamespace(
        ui=_UI(),
        _barge_in_requested=False,
        _last_interaction_timestamp=0.0,
        session_manager=SimpleNamespace(transition_to=lambda *a, **k: None),
        _listening_state=lambda: state,
        barge_ins=[],
        standbys=[],
    )
    era.trigger_barge_in = lambda source="voice": era.barge_ins.append(source)
    era.enter_standby = lambda *a, **k: era.standbys.append(1)
    era._enter_inactivity = lambda source, drop_reply=True: main.EraLive._enter_inactivity(
        era, source, drop_reply
    )
    era._release_silence_hold = lambda: main.EraLive._release_silence_hold(era)
    era._discard_silence_hold = lambda: main.EraLive._discard_silence_hold(era)
    era._emit_or_hold = lambda emit: main.EraLive._emit_or_hold(era, emit)
    era.spoken = []
    return main, era, state


def _reply(era, chunk):
    """What _receive_audio does with each reply chunk (audio, text, HUD)."""
    from app.era_live import audio

    audio._emit_or_hold(era, lambda c=chunk: era.spoken.append(c))


def _partial(main, era, text):
    return main.EraLive._silence_partial(era, text)


def _finished(main, era, text, at_turn_end=False):
    return main.EraLive._silence_finished(era, text, at_turn_end=at_turn_end)


def _turn_boundary(era):
    era._barge_in_requested = False  # what turn_complete does in _receive_audio


def test_a_longer_sentence_starting_with_the_order_leaves_the_window_open():
    # dev-review 2026-11-16: "stay silent" then "when I'm on a call" as two chunks.
    main, era, state = _era()
    assert _partial(main, era, "stay silent") is True  # only HOLDS her reply
    assert era._barge_in_requested is False  # nothing is dropped
    assert state.window_open() is True  # nothing irreversible yet
    assert _partial(main, era, "stay silent when I'm on a call") is False
    assert _finished(main, era, "stay silent when I'm on a call") is False
    assert state.window_open() is True
    assert era.barge_ins == [] and era.ui.states == []


def test_interleaved_reply_output_is_delivered_complete_for_a_longer_sentence():
    # dev-review 2026-11-16 (second review): input chunk -> OUTPUT chunk -> input
    # chunk must deliver her whole ordinary reply, in order.
    main, era, _ = _era()
    _partial(main, era, "stay silent")
    _reply(era, "Of course, ")
    _reply(era, "I'll keep ")
    assert era.spoken == []  # held, not dropped
    _partial(main, era, "stay silent when I'm on a call")
    assert era.spoken == ["Of course, ", "I'll keep "]
    _reply(era, "quiet during calls.")
    _finished(main, era, "stay silent when I'm on a call")
    assert era.spoken == ["Of course, ", "I'll keep ", "quiet during calls."]


def test_a_standalone_order_delivers_no_reply_at_all():
    main, era, state = _era()
    _partial(main, era, "stay silent")
    _reply(era, "Understood, ")
    _reply(era, "I'll be quiet.")
    _finished(main, era, "stay silent")  # end-of-speech mark
    assert era.spoken == []  # the held reply is discarded
    assert era._barge_in_requested is True  # later chunks of it are dropped too
    assert state.window_open() is False


def test_an_era_prefix_of_a_longer_sentence_does_not_silence_her():
    main, era, state = _era()
    _partial(main, era, "Era, stay silent")
    _reply(era, "Sure.")
    _partial(main, era, "Era, stay silent about this at the meeting")
    _finished(main, era, "Era, stay silent about this at the meeting")
    assert state.window_open() is True and era._barge_in_requested is False
    assert era.spoken == ["Sure."]


def test_a_standalone_order_closes_the_window_without_speech():
    main, era, state = _era()
    assert _partial(main, era, "Era, stay silent") is True
    assert _finished(main, era, "Era, stay silent") is True  # end-of-speech mark
    assert state.window_open() is False
    assert era._barge_in_requested is True  # her reply to the order is dropped
    assert era.barge_ins == ["silence_order"] and era.standbys == []
    _turn_boundary(era)
    assert _finished(main, era, "Era, stay silent", at_turn_end=True) is True  # backstop
    assert era._barge_in_requested is False  # her NEXT reply is not swallowed
    assert era.barge_ins == ["silence_order"]


def test_without_an_end_of_speech_mark_the_backstop_commits_but_drops_nothing_later():
    main, era, state = _era()
    _partial(main, era, "remain silent")
    _turn_boundary(era)
    assert _finished(main, era, "remain silent", at_turn_end=True) is True
    assert state.window_open() is False
    assert era._barge_in_requested is False


def test_the_same_order_works_again_on_a_later_turn():
    main, era, state = _era()
    _partial(main, era, "Era, stay silent")
    _finished(main, era, "Era, stay silent")
    _turn_boundary(era)
    _finished(main, era, "Era, stay silent", at_turn_end=True)
    state.note_transcript("Era, what time is it?")  # she is named: window reopens
    assert state.window_open() is True
    _partial(main, era, "Era, stay silent")
    _finished(main, era, "Era, stay silent")
    assert state.window_open() is False


def test_the_order_closes_the_window_and_drops_her_reply():
    main, era, state = _era()
    assert state.window_open() is True
    main.EraLive._enter_inactivity(era, "voice")
    assert state.window_open() is False  # only her name reopens it
    assert era._barge_in_requested is True  # her reply to this turn is dropped
    assert era.barge_ins == ["silence_order"]
    assert era.standbys == []  # never the real STANDBY
    assert era.ui.states == ["IDLE"]


def test_her_name_reopens_it_afterwards():
    main, era, state = _era()
    main.EraLive._enter_inactivity(era, "voice")
    state.note_transcript("Era, what time is it?")
    assert state.window_open() is True


def test_every_input_path_uses_inactivity_not_standby():
    import main
    from app.era_live import audio, tool_dispatch

    typed = inspect.getsource(main.EraLive._handle_text_command)
    assert typed.index("is_silence_order(text)") < typed.index(
        "_STANDBY_INTENT.match(text.strip())"
    )
    voice = inspect.getsource(audio)
    assert "silence = self._silence_partial(self._inflight_user_text)" in voice
    assert "if callable(listening) and not silence:" in voice
    assert "self._silence_finished(code_text)" in voice  # end-of-speech mark
    assert "_silence_turn_end(self, " in voice  # turn_complete backstop, before end markers
    assert "_enter_inactivity" not in voice  # never acted on a partial
    # audio, checkpoint, HUD text, spoken text (owner/plain/space): all held, none dropped
    assert len(re.findall(r"_emit_or_hold\(\s*self,", voice)) >= 6
    tool = inspect.getsource(tool_dispatch)
    i = tool.index('if tool_name == "enter_standby":')
    branch = tool[i : i + 600]
    assert branch.index("is_silence_order") < branch.index("self.enter_standby()")


# ── PF-039 through the REAL _receive_audio loop (dev-review, third review) ──


class _Nothing:
    """Any engine part the scripted loop does not care about: callable,
    falsy, and every attribute is another _Nothing."""

    def __call__(self, *a, **k):
        return None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Nothing()

    def __bool__(self):
        return False

    def __iter__(self):
        return iter(())


def _drive_receive(monkeypatch, events):
    """Run AudioIOMixin._receive_audio over scripted server messages in
    transcript-speech mode; return (ordered speech_out log, listening state)."""
    import asyncio

    import app.era_live.audio as audio
    import main
    from core.live_turn_state import LiveTurnState

    monkeypatch.setattr(audio.VOICE_CFG, "uses_speech_out", lambda: True)
    monkeypatch.setattr(audio.VOICE_CFG, "synthesizes_from_transcript", lambda: True)
    state = ListeningState(window_s=120)
    log = []

    class _SpeechOut:
        def feed(self, text, owner=None):
            log.append(("feed", text))

        def end_turn(self, held_audio=None):
            log.append(("end_turn", None))

        def __getattr__(self, name):
            return lambda *a, **k: None

    class _Era(audio.AudioIOMixin):
        _silence_partial = main.EraLive._silence_partial
        _silence_finished = main.EraLive._silence_finished
        _emit_or_hold = main.EraLive._emit_or_hold
        _release_silence_hold = main.EraLive._release_silence_hold
        _discard_silence_hold = main.EraLive._discard_silence_hold
        _enter_inactivity = main.EraLive._enter_inactivity

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return _Nothing()

    def msg(inp=None, out=None, done=False):
        content = SimpleNamespace(
            input_transcription=SimpleNamespace(text=inp, finished=False) if inp else None,
            output_transcription=SimpleNamespace(text=out) if out else None,
            turn_complete=done,
            generation_complete=done,
            interrupted=False,
            grounding_metadata=None,
            model_turn=None,
        )
        return SimpleNamespace(
            data=None, server_content=content, tool_call=None, session_resumption_update=None
        )

    era = _Era()
    era.speech_out = _SpeechOut()
    era._listening_state = lambda: state
    era._barge_in_requested = False
    era._go_away_event = asyncio.Event()
    era._turn_done_event = asyncio.Event()
    era.live_turn = LiveTurnState()
    era.ui = _UI()
    era.session_manager = SimpleNamespace(
        handle_go_away=lambda _: False,
        capture_token=lambda _: False,
        transition_to=lambda *a, **k: None,
    )
    era._renewal_deadline = None
    era._last_interaction_timestamp = 0.0
    era._inflight_user_text = ""
    era._shutdown_requested = False
    era._cm_handoff_active = lambda: False
    era._is_abort_order = lambda _: False
    era._is_reboot_order = lambda _: False
    era._take_cloud_audio_buffer = lambda: None

    async def receive():
        for kwargs in events:
            yield msg(**kwargs)
        era._go_away_event.set()

    era.session = SimpleNamespace(receive=receive)

    async def run():
        try:
            await asyncio.wait_for(era._receive_audio(), 2)
        except Exception:
            pass  # the scripted stream ends; only the emitted order matters

    asyncio.run(run())
    return log, state


def test_real_loop_standalone_order_without_finished_mark_speaks_nothing(monkeypatch):
    log, state = _drive_receive(
        monkeypatch,
        [
            {"inp": "stay silent"},
            {"out": "Understood, I'll be quiet."},
            {"done": True},
        ],
    )
    assert [e for e in log if e[0] == "feed"] == []
    assert state.window_open() is False


def test_real_loop_longer_sentence_delivers_the_whole_reply_before_end_turn(monkeypatch):
    log, state = _drive_receive(
        monkeypatch,
        [
            {"inp": "stay silent"},
            {"out": "Of course, "},
            {"inp": "when I'm on a call"},
            {"out": "I'll stay quiet during calls."},
            {"done": True},
        ],
    )
    spoken = "".join(t for kind, t in log if kind == "feed")
    assert "Of course," in spoken and "during calls." in spoken
    assert spoken.index("Of course,") < spoken.index("during calls.")
    assert log and log[-1] == ("end_turn", None)  # no reply text after its end marker
    assert state.window_open() is True


def test_the_hold_is_resolved_before_any_end_of_reply_marker():
    import inspect as _inspect

    from app.era_live import audio

    src = _inspect.getsource(audio.AudioIOMixin._receive_audio)
    tc = src.index("if sc.turn_complete:")
    assert src.index("_silence_turn_end(self", tc) < src.index("self.speech_out.end_turn(", tc)
    assert src.index("_silence_turn_end(self", tc) < src.index("NativeSpeechEnd(", tc)
