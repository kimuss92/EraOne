"""Offline SDK-shaped call tests; no account, provider, or hardware is opened."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from google.genai import types

from core.input_envelope import current_input
from core.telegram_call_audio import CallAudio
from core.telegram_call_gemini import GeminiCall, call_config, connect_call


class Session:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.inputs = []
        self.responses = []
        self.client_contents = []
        self.turns = 0
        self.closed = False
        self.send_error = False
        self.close_error = False

    async def receive(self):
        self.turns += 1
        while True:
            message = await self.messages.get()
            if message is None:
                return
            yield message
            if message.server_content and message.server_content.turn_complete:
                return

    async def send_realtime_input(self, *, audio):
        self.inputs.append(audio)

    async def send_client_content(self, *, turns, turn_complete):
        self.client_contents.append((turns, turn_complete))

    async def send_tool_response(self, *, function_responses):
        if self.send_error:
            raise ConnectionError("synthetic failure")
        self.responses.extend(function_responses)

    async def close(self):
        self.closed = True
        if self.close_error:
            raise ConnectionError("synthetic close failure")


def content(**kwargs):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kwargs))


def speech(pcm, **kwargs):
    return content(
        model_turn=types.Content(
            parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))]
        ),
        **kwargs,
    )


def tools(identifier="t1", name="open_app"):
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[types.FunctionCall(id=identifier, name=name, args={"app": "brave"})]
        )
    )


def fixture(*, dispatch=None, policy=lambda packet: True, timeout=0.15, capacity=25):
    audio = CallAudio(42, capacity)
    generation = audio.begin(42)
    assert audio.connected(generation, 16000, 24000)
    events = []
    adapter = GeminiCall(
        audio,
        generation,
        notify=events.append,
        dispatch=dispatch,
        input_policy=policy,
        io_timeout=timeout,
        tool_timeout=timeout,
    )
    return audio, generation, adapter, events, Session(), asyncio.Event()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def finish(task, stop):
    stop.set()
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_connected_call_requests_one_greeting_without_tool_authority():
    _, _, adapter, _, session, stop = fixture()
    running = asyncio.create_task(adapter.run(session, stop))
    await until(lambda: session.client_contents)
    assert len(session.client_contents) == 1
    turns, complete = session.client_contents[0]
    assert complete and "Greet" in turns[0].parts[0].text
    assert adapter._envelope is None
    await finish(running, stop)


async def drain(audio, generation, collected):
    while True:
        packet = audio.take_output(generation)
        if packet:
            collected.append(packet.data)
            assert audio.submitted(packet)
        await asyncio.sleep(0.002)


def test_config_uses_native_audio_and_only_explicit_tools():
    config = call_config("Era call instructions", "Algenib", [])
    assert config.response_modalities == [types.Modality.AUDIO]
    assert config.tools == []
    assert config.input_audio_transcription is not None
    assert config.output_audio_transcription is not None
    assert not config.realtime_input_config.automatic_activity_detection.disabled
    assert config.realtime_input_config.activity_handling == "START_OF_ACTIVITY_INTERRUPTS"
    config = call_config("test", "Algenib", [{"name": "open_app"}])
    assert config.tools[0].function_declarations[0].name == "open_app"


@pytest.mark.parametrize("timeout", [0, -1, 31])
def test_deadlines_are_bounded(timeout):
    with pytest.raises(ValueError):
        fixture(timeout=timeout)


def test_only_current_connected_generation_can_get_adapter():
    audio = CallAudio(42)
    generation = audio.begin(42)
    with pytest.raises(ValueError):
        GeminiCall(
            audio,
            generation,
            notify=lambda e: None,
            dispatch=None,
            input_policy=lambda p: True,
        )


@pytest.mark.asyncio
async def test_remote_pcm_preserved_and_private_local_state_not_consulted():
    audio, generation, adapter, events, session, stop = fixture()
    pcm = b"\x04\x00" * 320
    assert audio.receive(42, generation, 0, pcm)
    task = asyncio.create_task(adapter.run(session, stop))
    await until(lambda: session.inputs)
    assert session.inputs[0].data == pcm
    assert session.inputs[0].mime_type == "audio/pcm;rate=16000"
    await finish(task, stop)
    assert session.closed and audio.state == "ended"
    assert events[-1].kind == "provider_stopped"


@pytest.mark.asyncio
async def test_explicit_speaker_policy_can_refuse_remote_pcm():
    audio, gen, adapter, _, session, stop = fixture(policy=lambda p: False)
    audio.receive(42, gen, 0, bytes(640))
    task = asyncio.create_task(adapter.run(session, stop))
    await asyncio.sleep(0.03)
    await finish(task, stop)
    assert not session.inputs


@pytest.mark.asyncio
async def test_successive_turns_preserve_full_audio_and_reopen_receive():
    audio, gen, adapter, events, session, stop = fixture(capacity=2)
    output = []
    drainer = asyncio.create_task(drain(audio, gen, output))
    task = asyncio.create_task(adapter.run(session, stop))
    first, second = b"\x01\x00" * 2500, b"\x02\x00" * 500
    await session.messages.put(
        speech(
            first,
            turn_complete=True,
            output_transcription=types.Transcription(text="First."),
        )
    )
    await session.messages.put(speech(second, turn_complete=True))
    await until(lambda: len(output) == 8)
    assert b"".join(output[:6]) == first.ljust(5760, b"\0")
    assert b"".join(output[6:]) == second.ljust(1920, b"\0")
    assert session.turns >= 3
    assert any(e.kind == "output_caption" and e.text == "First." for e in events)
    await finish(task, stop)
    drainer.cancel()
    await asyncio.gather(drainer, return_exceptions=True)


@pytest.mark.asyncio
async def test_interruption_processed_while_output_is_backpressured():
    audio, gen, adapter, events, session, stop = fixture(capacity=1)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(speech(b"\x01\x00" * 2400))
    await until(lambda: audio.reply and audio.output_room(gen, audio.reply) == 0)
    old = audio.take_output(gen)
    await session.messages.put(content(interrupted=True))
    await until(lambda: any(e.kind == "interrupted" for e in events))
    assert not audio.submitted(old)
    assert audio.take_output(gen) is None
    await session.messages.put(speech(b"\x02\x00" * 480, turn_complete=True))
    await until(lambda: audio.reply and audio.output_room(gen, audio.reply) == 0)
    new = audio.take_output(gen)
    assert new.data == b"\x02\x00" * 480
    assert audio.submitted(new)
    await finish(task, stop)


@pytest.mark.asyncio
async def test_tool_has_immutable_owner_context_dedup_and_telegram_source():
    commands = []

    async def dispatch(command):
        assert current_input.get() is command.input
        commands.append(command)
        return {"ok": True}

    audio, gen, adapter, _, session, stop = fixture(dispatch=dispatch)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(content(input_transcription=types.Transcription(text="Open ")))
    await session.messages.put(
        content(input_transcription=types.Transcription(text="Brave", finished=True))
    )
    await session.messages.put(tools())
    await session.messages.put(tools())
    await until(lambda: session.responses)
    assert len(commands) == 1
    assert commands[0].generation == gen and commands[0].peer_id == 42
    assert commands[0].input.text == "Open Brave" and commands[0].input.source == "telegram"
    assert session.responses[0].id == "t1"
    assert current_input.get() is None
    await finish(task, stop)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_dispatch", "error", "timeout"])
async def test_tool_failure_returns_truthful_result(failure):
    async def dispatch(command):
        if failure == "error":
            raise ValueError("synthetic secret must not leak")
        await asyncio.sleep(1)

    _, _, adapter, _, session, stop = fixture(
        dispatch=None if failure == "missing_dispatch" else dispatch, timeout=0.03
    )
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(
        content(input_transcription=types.Transcription(text="test", finished=True))
    )
    await session.messages.put(tools())
    await until(lambda: session.responses)
    expected = {
        "missing_dispatch": "call_tools_unavailable",
        "error": "tool_failed",
        "timeout": "tool_timeout_outcome_unknown",
    }[failure]
    assert session.responses[0].response == {"ok": False, "error": expected}
    await finish(task, stop)


@pytest.mark.asyncio
async def test_cancelled_tool_cannot_deliver_late_result():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def dispatch(command):
        started.set()
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.set()

    _, _, adapter, events, session, stop = fixture(dispatch=dispatch)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(
        content(input_transcription=types.Transcription(text="test", finished=True))
    )
    await session.messages.put(tools())
    await started.wait()
    await session.messages.put(
        types.LiveServerMessage(
            tool_call_cancellation=types.LiveServerToolCallCancellation(ids=["t1", "unknown"])
        )
    )
    await cancelled.wait()
    await finish(task, stop)
    assert not session.responses
    assert any(e.kind == "tool_cancel_requested" for e in events)


@pytest.mark.asyncio
async def test_tool_send_failure_ends_session_even_without_new_messages():
    _, _, adapter, events, session, stop = fixture()
    session.send_error = True
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(
        content(input_transcription=types.Transcription(text="test", finished=True))
    )
    await session.messages.put(tools())
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(task, 1)
    assert any(e.kind == "provider_failed" for e in events)
    assert session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        None,
        types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="1s")),
        tools(identifier=""),
        tools(name=""),
        content(input_transcription=types.Transcription(text="x" * 16001)),
        speech(bytes(96002)),
        speech(b"x"),
        content(
            model_turn=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(data=bytes(960), mime_type="audio/pcm;rate=48000")
                    )
                ]
            )
        ),
    ],
)
async def test_provider_faults_stop_audio_without_claiming_remote_hangup(message):
    audio, _, adapter, events, session, stop = fixture()
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(message)
    with pytest.raises((ValueError, ConnectionError)):
        await asyncio.wait_for(task, 1)
    assert audio.state == "failed" and audio.failure == "gemini_call_failed"
    assert session.closed
    assert all(e.kind != "remote_call_ended" for e in events)


@pytest.mark.asyncio
async def test_fast_provider_burst_waits_for_playback_without_losing_audio():
    audio, gen, adapter, events, session, stop = fixture(timeout=1, capacity=2)
    output = []
    drainer = asyncio.create_task(drain(audio, gen, output))
    task = asyncio.create_task(adapter.run(session, stop))
    chunks = [bytes([i, 0]) * 12000 for i in range(1, 7)]
    for i, chunk in enumerate(chunks):
        await session.messages.put(speech(chunk, turn_complete=i == len(chunks) - 1))
    # This represents six seconds of audio; Windows timer granularity and a
    # busy live Era process can make the synthetic drainer exceed two seconds.
    async with asyncio.timeout(8):
        while len(output) != 150:
            await asyncio.sleep(0.005)
    assert b"".join(output) == b"".join(chunks)
    assert all(event.kind != "provider_failed" for event in events)
    await finish(task, stop)
    drainer.cancel()
    await asyncio.gather(drainer, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_backpressure_has_bounded_failure():
    audio, _, adapter, _, session, stop = fixture(timeout=0.03, capacity=1)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(speech(bytes(1920), turn_complete=True))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, 1)
    assert audio.state == "failed"


@pytest.mark.asyncio
async def test_duplicate_run_refused_and_cancellation_closes_provider():
    audio, _, adapter, events, session, stop = fixture()
    task = asyncio.create_task(adapter.run(session, stop))
    await until(lambda: events)
    with pytest.raises(RuntimeError):
        await adapter.run(Session(), stop)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed and audio.state == "ended"


@pytest.mark.asyncio
async def test_close_failure_reported():
    _, _, adapter, events, session, stop = fixture()
    session.close_error = True
    stop.set()
    await adapter.run(session, stop)
    assert any(e.kind == "provider_close_failed" for e in events)


@pytest.mark.asyncio
async def test_old_adapter_cleanup_does_not_stop_new_generation():
    audio, gen, adapter, events, session, stop = fixture()
    task = asyncio.create_task(adapter.run(session, stop))
    await until(lambda: events)
    audio.end(gen)
    new = audio.begin(42)
    audio.connected(new, 16000, 24000)
    await finish(task, stop)
    assert audio.generation == new and audio.state == "connected"


def test_remote_input_requires_integer_peer_and_failure_is_generation_owned():
    audio, gen, _, _, _, _ = fixture()
    assert not audio.receive(42.0, gen, 0, bytes(640))
    assert not audio.fail("old", "unrelated")
    assert audio.fail(gen, "transport_failed")
    assert not audio.fail(gen, "replacement")
    assert audio.failure == "transport_failed"


@pytest.mark.asyncio
async def test_new_partial_transcript_cannot_reuse_old_command():
    commands = []

    async def dispatch(command):
        commands.append(command)
        return {"success": True}

    _, _, adapter, _, session, stop = fixture(dispatch=dispatch)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(
        content(input_transcription=types.Transcription(text="old", finished=True))
    )
    await session.messages.put(content(input_transcription=types.Transcription(text="new partial")))
    await session.messages.put(tools())
    await until(lambda: session.responses)
    assert commands[0].input.text == "new partial"
    await finish(task, stop)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_message_complete", [False, True])
async def test_music_tool_without_finished_keeps_original_owner_context(
    monkeypatch, same_message_complete
):
    clock = [100.0]
    monkeypatch.setattr("core.telegram_call_gemini.time.time", lambda: clock[0])
    commands = []

    async def dispatch(command):
        commands.append(command)
        return {"success": True, "result": "Playing music."}

    _, _, adapter, events, session, stop = fixture(dispatch=dispatch)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(content(input_transcription=types.Transcription(text="Play music")))
    await until(lambda: any(e.kind == "input_caption" for e in events))
    clock[0] = 200.0
    message = tools(name="spotify_control")
    if same_message_complete:
        message.server_content = types.LiveServerContent(turn_complete=True)
    await session.messages.put(message)
    await until(lambda: session.responses)
    assert len(commands) == 1
    assert commands[0].input.text == "Play music"
    assert commands[0].input.received_at == 100.0
    assert commands[0].input.source == "telegram"
    assert not task.done()
    await finish(task, stop)


@pytest.mark.asyncio
async def test_missing_transcript_blocks_action_but_keeps_call_and_followup_alive():
    commands = []

    async def dispatch(command):
        commands.append(command)
        return {"success": True}

    _, _, adapter, _, session, stop = fixture(dispatch=dispatch)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(tools())
    await until(lambda: session.responses)
    assert session.responses[0].response["error_type"] == "MissingUserTranscript"
    assert not commands and not task.done()
    await session.messages.put(content(input_transcription=types.Transcription(text="Play music")))
    await session.messages.put(tools(identifier="followup", name="spotify_control"))
    await until(lambda: len(commands) == 1)
    assert commands[0].input.text == "Play music"
    await finish(task, stop)


@pytest.mark.asyncio
async def test_tool_budget_bounds_session_memory():
    _, _, adapter, _, session, stop = fixture()
    adapter._seen.update(str(i) for i in range(256))
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(tools())
    with pytest.raises(ValueError, match="budget"):
        await task


@pytest.mark.asyncio
async def test_previous_reply_backpressure_does_not_drop_speech_silently():
    audio, _, adapter, _, session, stop = fixture(timeout=0.03, capacity=1)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(speech(bytes(960), turn_complete=True))
    await session.messages.put(speech(bytes(960), turn_complete=True))
    with pytest.raises(TimeoutError):
        await task
    assert audio.state == "failed"


@pytest.mark.asyncio
async def test_uncancellable_tool_cleanup_reported_without_hanging_call():
    started, release = asyncio.Event(), asyncio.Event()

    async def dispatch(command):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return {"ok": True}

    _, _, adapter, events, session, stop = fixture(dispatch=dispatch, timeout=0.03)
    task = asyncio.create_task(adapter.run(session, stop))
    await session.messages.put(
        content(input_transcription=types.Transcription(text="test", finished=True))
    )
    await session.messages.put(tools())
    await started.wait()
    await finish(task, stop)
    assert any(e.kind == "worker_cleanup_incomplete" for e in events)
    release.set()
    await asyncio.gather(*adapter._tools.values(), *adapter._operations, return_exceptions=True)
    assert not session.responses and session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_explicit_provider_connection_uses_shared_managed_boundary(monkeypatch, fail):
    audio, _, adapter, events, session, stop = fixture()
    client = Mock()
    observed = []

    @asynccontextmanager
    async def connection(passed, **kwargs):
        observed.append((passed, kwargs))
        if fail:
            raise ConnectionError("synthetic setup failure")
        try:
            yield session
        finally:
            client.close()

    monkeypatch.setattr("app.era_live.session_runtime.managed_live_connection", connection)
    config = call_config("test", "Algenib", [])
    task = asyncio.create_task(
        connect_call(adapter, client, model="selected-live-model", config=config, stop=stop)
    )
    if fail:
        with pytest.raises(ConnectionError):
            await task
        assert audio.state == "failed"
    else:
        await until(lambda: events)
        await finish(task, stop)
        assert client.close.called and session.closed
    assert observed[0][0] is client
    assert observed[0][1]["config"] is config
    assert observed[0][1]["model"] == "selected-live-model"


@pytest.mark.asyncio
async def test_stop_before_connect_never_contacts_provider():
    audio, _, adapter, _, _, stop = fixture()
    client = Mock()
    stop.set()
    await connect_call(
        adapter,
        client,
        model="test",
        config=call_config("test", "Algenib", []),
        stop=stop,
    )
    assert client.close.called and audio.state == "ended"
    assert not client.aio.live.connect.called


@pytest.mark.asyncio
async def test_stop_during_provider_setup_cleans_up(monkeypatch):
    audio, _, adapter, _, _, stop = fixture()
    started, closed = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def connection(client, **kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield None
        finally:
            closed.set()

    monkeypatch.setattr("app.era_live.session_runtime.managed_live_connection", connection)
    task = asyncio.create_task(
        connect_call(
            adapter,
            Mock(),
            model="test",
            config=call_config("test", "Algenib", []),
            stop=stop,
        )
    )
    await started.wait()
    await finish(task, stop)
    assert closed.is_set() and audio.state == "ended"
