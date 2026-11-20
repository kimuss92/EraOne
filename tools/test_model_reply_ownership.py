"""Model reply attribution: mocked SDK/notifications, never external traffic."""

import asyncio
from types import SimpleNamespace

import pytest

from core.input_envelope import InputEnvelope, current_input
from core.model_reply import ModelReplyOwnership, completed_model_owner, send_model_text


def request(source="telegram"):
    return InputEnvelope.create("request", source)


def test_single_request_and_empty_tool_turn_keep_the_owner():
    owner, tracker = request(), ModelReplyOwnership()
    tracker.register(owner)
    tracker.note_tools()
    assert tracker.complete(has_text=False) is None
    assert tracker.owner is owner and tracker.active
    assert tracker.complete(has_text=True) is owner
    assert not tracker.active


def test_empty_non_tool_turn_releases_owner():
    tracker = ModelReplyOwnership()
    tracker.register(request())
    assert tracker.complete(has_text=False) is None
    assert not tracker.active


@pytest.mark.parametrize("source", ["telegram", "typed", "voice"])
def test_overlapping_requests_never_assign_old_output_to_new_owner(source):
    tracker = ModelReplyOwnership()
    tracker.register(request())
    tracker.register(request(source))
    assert tracker.ambiguous and tracker.complete(has_text=True) is None
    fresh = request()
    tracker.register(fresh)
    assert tracker.complete(has_text=True) is fresh


def test_internal_send_invalidates_active_remote_request():
    tracker = ModelReplyOwnership()
    tracker.register(request())
    tracker.register(None)
    assert tracker.complete(has_text=True) is None


def test_same_owner_continuation_remains_attributed():
    tracker, owner = ModelReplyOwnership(), request()
    tracker.register(owner)
    tracker.register(owner)
    assert tracker.complete(has_text=True) is owner


@pytest.mark.parametrize("which", [0, 1])
def test_failed_overlapping_send_does_not_enable_old_tail_attribution(which):
    tracker = ModelReplyOwnership()
    tickets = [tracker.register(request()), tracker.register(request())]
    tracker.failed(tickets[which])
    assert tracker.active and tracker.ambiguous
    assert tracker.complete(has_text=True) is None


def test_only_failed_send_clears_its_registration():
    tracker = ModelReplyOwnership()
    ticket = tracker.register(request())
    tracker.failed(ticket)
    assert not tracker.active


@pytest.mark.parametrize("reason", ["new_input", "audio_flush", "session_disconnect"])
def test_invalidation_cannot_reassign_buffered_tail_to_another_request(reason):
    tracker = ModelReplyOwnership()
    tracker.register(request())
    tracker.invalidate(reason)
    tracker.register(request())
    assert tracker.complete(has_text=True) is None


def test_sdk_send_captures_context_at_actual_send_and_preserves_text():
    async def scenario():
        calls = []
        owner = request()

        async def sdk(**kwargs):
            calls.append(kwargs)

        era = SimpleNamespace(
            session=SimpleNamespace(send_realtime_input=sdk), _input_envelope=owner
        )
        token = current_input.set(owner)
        try:
            await send_model_text(era, "original text")
        finally:
            current_input.reset(token)
        assert calls == [{"text": "original text"}]
        assert completed_model_owner(era, "answer") is owner

    asyncio.run(scenario())


def test_delayed_lane_result_does_not_inherit_latest_input():
    async def scenario():
        old, fresh = request(), request("typed")

        async def sdk(**kwargs):
            pass

        era = SimpleNamespace(
            session=SimpleNamespace(send_realtime_input=sdk), _input_envelope=fresh
        )
        token = current_input.set(old)
        try:
            await send_model_text(era, "delayed")
        finally:
            current_input.reset(token)
        assert completed_model_owner(era, "answer") is None

    asyncio.run(scenario())


def test_latest_input_match_is_required_at_response_completion():
    old, fresh = request(), request()
    tracker = ModelReplyOwnership()
    tracker.register(old)
    reports = []
    era = SimpleNamespace(
        _model_reply_ownership=tracker,
        _input_envelope=fresh,
        _report_unattributed_model_reply=reports.append,
    )
    assert completed_model_owner(era, "old answer") is None
    assert reports == ["superseded_input"]


def test_sdk_exception_propagates_and_clears_single_request():
    async def scenario():
        owner = request()

        async def sdk(**kwargs):
            raise RuntimeError("provider failed")

        era = SimpleNamespace(
            session=SimpleNamespace(send_realtime_input=sdk), _input_envelope=owner
        )
        token = current_input.set(owner)
        try:
            with pytest.raises(RuntimeError, match="provider failed"):
                await send_model_text(era, "original")
        finally:
            current_input.reset(token)
        assert not era._model_reply_ownership.active

    asyncio.run(scenario())


def test_internal_note_cannot_inherit_remote_context():
    async def scenario():
        owner = request()

        async def sdk(**kwargs):
            pass

        era = SimpleNamespace(
            session=SimpleNamespace(send_realtime_input=sdk), _input_envelope=owner
        )
        token = current_input.set(owner)
        try:
            await send_model_text(era, "system note", internal=True)
        finally:
            current_input.reset(token)
        assert era._model_reply_ownership.owner is None

    asyncio.run(scenario())


def test_reporting_failure_does_not_break_response_processing():
    tracker = ModelReplyOwnership()
    old, fresh = request(), request()
    tracker.register(old)

    def report(reason):
        raise RuntimeError("HUD offline")

    era = SimpleNamespace(
        _model_reply_ownership=tracker,
        _input_envelope=fresh,
        _report_unattributed_model_reply=report,
    )
    assert completed_model_owner(era, "old answer") is None


def test_real_receive_disconnect_reports_pending_remote_and_resets_owner():
    async def run():
        from app.era_live.audio import AudioIOMixin

        tracker = ModelReplyOwnership()
        owner = request()
        tracker.register(owner)
        notices = []

        async def empty():
            if False:
                yield

        era = AudioIOMixin()
        era.session = SimpleNamespace(receive=empty)
        era._input_envelope = owner
        era._model_reply_ownership = tracker
        era._go_away_event = asyncio.Event()
        era._is_expected_rotation = lambda _: False
        era._is_network_blip = lambda _: True
        era._report_unattributed_model_reply = notices.append
        with pytest.raises(ConnectionError, match="ended without messages"):
            await era._receive_audio()
        assert notices == ["session_closed"]
        assert not tracker.active and not tracker.remote_pending

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["owned", "local", "overlap", "missing", "tool_then_reply"])
def test_real_receive_loop_routes_only_proven_remote_owner(monkeypatch, scenario):
    async def run():
        import app.era_live.audio as audio
        from core.live_turn_state import LiveTurnState

        notifications = []
        monkeypatch.setattr(audio, "_tg_notify", notifications.append)
        monkeypatch.setattr(audio.VOICE_CFG, "uses_speech_out", lambda: False)
        monkeypatch.setattr(audio.VOICE_CFG, "synthesizes_from_transcript", lambda: False)
        owner = request("typed" if scenario == "local" else "telegram")
        tracker = ModelReplyOwnership()
        if scenario != "missing":
            tracker.register(owner)
        if scenario == "overlap":
            tracker.register(request())
        if scenario == "tool_then_reply":
            tracker.note_tools()

        def response(text):
            content = SimpleNamespace(
                output_transcription=SimpleNamespace(text=text) if text else None,
                input_transcription=None,
                turn_complete=True,
                generation_complete=True,
            )
            return SimpleNamespace(data=None, server_content=content, tool_call=None)

        era = audio.AudioIOMixin()
        era._input_envelope = owner
        era._model_reply_ownership = tracker
        era._go_away_event = asyncio.Event()

        async def receive():
            if scenario == "tool_then_reply":
                yield response("")
            yield response("A grounded answer.")
            era._go_away_event.set()

        era.session = SimpleNamespace(receive=receive)
        era.session_manager = SimpleNamespace(
            handle_go_away=lambda _: False, capture_token=lambda _: False
        )
        era._renewal_deadline = None
        era._barge_in_requested = False
        era._turn_done_event = asyncio.Event()
        era.live_turn = LiveTurnState()
        era.ui = SimpleNamespace(write_log=lambda _: None)
        era._note_era_question = lambda _: None
        era._tg_reply_pending = True  # deliberately misleading for local/missing
        era._resume_offer_enabled = False
        era._log_turn_latency = lambda: None
        era._clear_cloud_audio_buffer = lambda: None
        era._maybe_restore_primary_tts = lambda: None
        era._is_expected_rotation = lambda _: False
        era._is_network_blip = lambda _: True
        await asyncio.wait_for(era._receive_audio(), 1)
        expected = ["A grounded answer."] if scenario in ("owned", "tool_then_reply") else []
        assert notifications == expected
        assert not tracker.active  # teardown cannot retain session ownership

    asyncio.run(run())
