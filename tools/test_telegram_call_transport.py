"""Offline protocol, native controller and owned process integration tests."""

import asyncio
import io
import json
import sys
from enum import IntFlag
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.telegram_call_audio import CallAudio
from core.telegram_call_helper import (
    NativeCall,
    SDKEndpoint,
    external_streams,
    main,
    open_endpoint,
    run_child,
)
from core.telegram_call_process import CallProcess
from core.telegram_call_protocol import MAX_LINE, CallWire

GEN = "a" * 32
REPLY = "b" * 32


def packet(kind, **kwargs):
    return CallWire(kind, GEN, 42, **kwargs)


@pytest.mark.parametrize(
    "kind",
    [
        "dial",
        "answer",
        "hangup",
        "ready",
        "incoming",
        "connected",
        "discarded",
        "failed",
        "stopped",
        "disabled",
    ],
)
def test_control_roundtrip(kind):
    original = packet(kind)
    assert CallWire.decode(original.encode()) == original


@pytest.mark.parametrize(
    "original",
    [
        packet("reply", reply=REPLY),
        packet("pcm_out", reply=REPLY, sequence=3, pcm=b"\x01\x00" * 480),
        packet("pcm_in", sequence=0, pcm=bytes(3200)),
        packet("submitted", sequence=3, reply=REPLY),
    ],
)
def test_audio_roundtrip_is_exact_and_repr_omits_pcm(original):
    assert CallWire.decode(original.encode()) == original
    assert len(original.encode()) <= MAX_LINE
    assert "pcm=" not in repr(original)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "execute"},
        {"kind": []},
        {"detail": "secret exception"},
        {"generation": "old"},
        {"peer_id": True},
        {"peer_id": 42.0},
        {"sequence": True},
        {"sequence": -2},
        {"sequence": 2**53},
        {"reply": "old"},
        {"pcm": b"x"},
        {"pcm": "pcm"},
    ],
)
def test_invalid_protocol_fields_are_rejected(changes):
    fields = {"kind": "ready", "generation": GEN, "peer_id": 42, **changes}
    with pytest.raises(ValueError):
        CallWire(**fields).encode()


@pytest.mark.parametrize(
    "original",
    [
        packet("pcm_in", pcm=b"x", sequence=0),
        packet("pcm_in", pcm=bytes(3202), sequence=0),
        packet("pcm_in", pcm=bytes(640), sequence=0, reply=REPLY),
        packet("pcm_out", pcm=bytes(640), sequence=0, reply=REPLY),
        packet("pcm_out", pcm=bytes(960), sequence=0),
        packet("submitted", reply=REPLY),
        packet("reply"),
    ],
)
def test_audio_metadata_must_match_direction_and_receipt(original):
    with pytest.raises(ValueError):
        original.encode()


@pytest.mark.parametrize(
    "line", [b"{}\n", b"[]\n", b"broken\n", b"{}", b"x" * (MAX_LINE + 1) + b"\n"]
)
def test_incomplete_or_incompatible_pipe_input_rejected(line):
    with pytest.raises(ValueError):
        CallWire.decode(line)


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"extra": "bad"},
        {"pcm": "%notbase64"},
        {"pcm": 3},
    ],
)
def test_wire_version_schema_and_base64_validation(change):
    fields = json.loads(packet("ready").encode())
    fields.update(change)
    with pytest.raises(ValueError):
        CallWire.decode(json.dumps(fields).encode() + b"\n")


def controller(timeout=0.03):
    events = []
    native = NativeCall(GEN, 42, events.append, timeout=timeout, connect_timeout=timeout)
    native.endpoint = AsyncMock()
    return native, events


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.003)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["dial", "answer"])
async def test_controller_owner_only_explicit_start_and_full_pcm(direction):
    native, events = controller()
    assert not native.incoming(43)
    assert not native.incoming(True)
    if direction == "answer":
        assert native.incoming(42)
        assert not native.incoming(42)
    await native.command(packet(direction))
    await until(lambda: native.state == "connected")
    with pytest.raises(RuntimeError):
        await native.command(packet(direction))
    await native.command(packet("reply", reply=REPLY))
    pcm = bytes(960)
    await native.command(packet("pcm_out", reply=REPLY, sequence=0, pcm=pcm))
    native.receive(43, bytes(640))
    native.receive(42, bytes(640))
    assert events[-1].kind == "pcm_in" and events[-1].sequence == 0
    native.endpoint.send.assert_awaited_once_with(pcm)
    assert any(e.kind == "submitted" for e in events)
    await native.close()
    assert events[-1].detail == "leave_requested"
    native.endpoint.leave.assert_awaited_once()
    native.endpoint.disconnect.assert_awaited_once()
    await native.close()
    assert native.endpoint.disconnect.await_count == 1


@pytest.mark.asyncio
async def test_remote_hangup_does_not_leave_already_ended_native_call():
    native, events = controller()
    await native.command(packet("dial"))
    await until(lambda: native.state == "connected")
    native.endpoint.leave.side_effect = RuntimeError("already ended")
    native.discarded(42)
    await native.close()
    native.endpoint.leave.assert_not_awaited()
    native.endpoint.disconnect.assert_awaited_once()
    assert events[-1].detail == "discard_observed"


@pytest.mark.asyncio
async def test_remote_hangup_with_pending_audio_and_pipe_eof_is_normal(monkeypatch):
    audio, gen, parent, events = supervisor(deadline=0.03)
    child = Child(gen)
    original = child.stdin.write

    def write(data):
        message = CallWire.decode(data)
        if message.kind != "pcm_out":
            original(data)

    child.stdin.write = write
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    child.emit("connected")
    await until(lambda: audio.state == "connected")
    reply = audio.begin_reply(gen)
    audio.enqueue_reply(gen, reply, bytes(960))
    await until(lambda: parent._pending)
    child.emit("discarded", detail="discarded")
    child.emit("stopped", detail="discard_observed")
    child.kill()
    await until(lambda: parent._remote_discarded)
    await asyncio.sleep(0.05)
    assert not parent._failure
    assert not any(event.kind == "failed" for event in events)
    assert (await parent.close()).remote_discard_observed


@pytest.mark.asyncio
async def test_stale_and_duplicate_output_never_reaches_endpoint():
    native, events = controller()
    await native.command(packet("dial"))
    await until(lambda: native.state == "connected")
    await native.command(packet("reply", reply=REPLY))
    await native.command(packet("pcm_out", reply="c" * 32, sequence=0, pcm=bytes(960)))
    await native.command(packet("pcm_out", reply=REPLY, sequence=1, pcm=bytes(960)))
    await native.command(packet("pcm_out", reply=REPLY, sequence=1, pcm=bytes(960)))
    assert native.endpoint.send.await_count == 1
    native.discarded(43)
    assert not native.done.is_set()
    native.discarded(42, busy=True)
    assert events[-1].detail == "busy"
    await native.close()
    assert events[-1].detail == "discard_observed"


@pytest.mark.asyncio
async def test_hangup_during_connect_is_responsive_and_cleans_up():
    native, events = controller(timeout=0.15)
    blocked = asyncio.Event()
    native.endpoint.connect.side_effect = blocked.wait
    await native.command(packet("dial"))
    await asyncio.sleep(0)
    await native.command(packet("hangup"))
    assert native.done.is_set()
    await native.close()
    assert native._connect.cancelled()
    assert events[-1].kind == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["connect", "leave", "disconnect"])
async def test_native_failure_reported_without_false_discard_proof(fail):
    native, events = controller()
    getattr(native.endpoint, fail).side_effect = ConnectionError("secret synthetic exception")
    await native.command(packet("dial"))
    await until(lambda: native.state in {"connected", "failed"})
    await native.close()
    assert not native._discarded
    if fail == "connect":
        assert any(e.kind == "failed" and e.detail == "native_failed" for e in events)
    else:
        assert events[-1].detail == "cleanup_incomplete"
    assert all("secret" not in str(e) for e in events)


@pytest.mark.asyncio
async def test_control_identity_and_state_are_verified():
    native, _ = controller()
    with pytest.raises(PermissionError):
        await native.command(CallWire("dial", "c" * 32, 42))
    for command in [
        packet("answer"),
        packet("reply", reply=REPLY),
        packet("pcm_out", reply=REPLY, sequence=0, pcm=bytes(960)),
        packet("ready"),
    ]:
        with pytest.raises((ValueError, RuntimeError)):
            await native.command(command)
    await native.close()


@pytest.mark.asyncio
async def test_disabled_setup_never_imports_native_or_authenticates(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pytgcalls", None)
    monkeypatch.setitem(sys.modules, "telethon", None)
    native, _ = controller()
    with pytest.raises(PermissionError):
        await open_endpoint({"calls_enabled": False}, native)


def test_callback_overflow_ends_controller_instead_of_running_silently():
    native, _ = controller()

    def full(event):
        raise asyncio.QueueFull

    native.emit = full
    native.event("connected")
    assert native.done.is_set() and native.state == "failed"


class Pipe:
    def __init__(self, process):
        self.process = process
        self.commands = []
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        assert self.closed

    def write(self, data):
        message = CallWire.decode(data)
        self.commands.append(message)
        if message.kind == "dial":
            self.process.emit("connected")
        elif message.kind == "pcm_out":
            self.process.emit("submitted", reply=message.reply, sequence=message.sequence)
        elif message.kind == "hangup":
            self.process.emit("stopped", detail="discard_observed")

    async def drain(self):
        await asyncio.sleep(0)


class Child:
    def __init__(self, generation, *, disabled=False):
        self.generation = generation
        self.returncode = None
        self.exited = asyncio.Event()
        self.stdout = asyncio.StreamReader(limit=MAX_LINE + 1)
        self.stdin = Pipe(self)
        self.emit(
            "disabled" if disabled else "ready",
            detail="calls_disabled" if disabled else "",
        )

    def emit(self, kind, **kwargs):
        self.stdout.feed_data(CallWire(kind, self.generation, 42, **kwargs).encode())

    def kill(self):
        self.returncode = -9
        self.exited.set()
        self.stdout.feed_eof()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


def supervisor(deadline=0.15):
    audio = CallAudio(42)
    gen = audio.begin(42)
    events = []
    parent = CallProcess(
        audio, gen, notify=events.append, deadline=deadline, cleanup_deadline=deadline
    )
    return audio, gen, parent, events


@pytest.mark.asyncio
async def test_supervised_pipe_connect_pcm_receipt_and_exact_child_cleanup(monkeypatch):
    audio, gen, parent, events = supervisor()
    child = Child(gen)
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    await parent.start(Path(__file__))
    await parent.command("dial")
    await until(lambda: audio.state == "connected")
    child.emit("pcm_in", sequence=0, pcm=bytes(640))
    await until(lambda: audio.input_bytes > 0)
    assert audio.take_input(gen).data == bytes(640)
    reply = audio.begin_reply(gen)
    assert audio.enqueue_reply(gen, reply, bytes(1920), final=True)
    await until(lambda: audio.submitted_bytes == 1920)
    result = await parent.close()
    assert child.stdin.closed
    assert result.process_reaped and result.remote_discard_observed
    assert result.teardown_receipt == "discard_observed"
    assert child.returncode == -9 and audio.state == "ended"
    assert "CREATE_NO_WINDOW" not in str(spawn.call_args.args)
    assert spawn.call_args.kwargs["env"]["ERA_LOG_DIR"].endswith("era-call-" + gen)
    with pytest.raises(RuntimeError):
        await parent.start(Path(__file__))


@pytest.mark.asyncio
async def test_disabled_helper_fails_without_enabling_or_dialling(monkeypatch):
    audio, gen, parent, events = supervisor()
    child = Child(gen, disabled=True)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    with pytest.raises(RuntimeError, match="calls_disabled"):
        await parent.start(Path(__file__))
    assert all(m.kind != "dial" for m in child.stdin.commands)
    assert audio.state == "ended" and child.returncode == -9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [b"not JSON\n", CallWire("ready", "c" * 32, 42).encode(), packet("dial").encode()],
)
async def test_bad_child_protocol_fails_closed_and_owned_process_reaped(monkeypatch, message):
    audio, gen, parent, _ = supervisor()
    child = Child(gen)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    child.stdout.feed_data(message)
    await until(lambda: parent._failure)
    result = await parent.close()
    assert result.process_reaped
    assert parent._failure == "protocol_failed"


@pytest.mark.asyncio
async def test_no_teardown_receipt_keeps_remote_hangup_unknown(monkeypatch):
    audio, gen, parent, _ = supervisor(deadline=0.02)
    child = Child(gen)
    child.stdin.write = lambda data: None
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    result = await parent.close()
    assert result.process_reaped and not result.remote_discard_observed
    assert result.teardown_receipt == "cleanup_incomplete"


@pytest.mark.asyncio
async def test_close_does_not_end_replacement_generation(monkeypatch):
    audio, gen, parent, _ = supervisor()
    child = Child(gen)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    audio.end(gen)
    new = audio.begin(42)
    assert audio.connected(new, 16000, 24000)
    await parent.close()
    assert audio.generation == new and audio.state == "connected"


@pytest.mark.asyncio
async def test_missing_optional_environment_and_private_pipe_reported():
    _, _, parent, _ = supervisor()
    with pytest.raises(FileNotFoundError):
        await parent.start(Path("Z:/missing/native/python.exe"))
    with pytest.raises(ConnectionError):
        await parent.command("dial")
    assert (await parent.close()).process_reaped


def sdk_fixture(monkeypatch):
    """Provider/network fakes; module imports cannot open accounts or native calls."""

    class Status(IntFlag):
        INCOMING_CALL = 1
        LEFT_CALL = 2
        BUSY_CALL = 4

    class Chat(SimpleNamespace):
        pass

    Chat.Status = Status

    class Frames(SimpleNamespace):
        pass

    client = AsyncMock()
    client.get_me.return_value = SimpleNamespace(id=41, phone="10000000", bot=False)
    client.get_entity.return_value = SimpleNamespace(id=42, phone="10000001", bot=False)
    client.is_user_authorized.return_value = True
    client.return_value = SimpleNamespace(full_user=SimpleNamespace(phone_calls_private=False))
    calls = SimpleNamespace(
        start=AsyncMock(),
        play=AsyncMock(),
        record=AsyncMock(),
        send_frame=AsyncMock(),
        leave_call=AsyncMock(),
        callback=None,
    )

    def register():
        def decorator(callback):
            calls.callback = callback
            return callback

        return decorator

    calls.on_update = register

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)

    module("telethon", TelegramClient=lambda *args: client)
    module("telethon.sessions", StringSession=lambda text: text)
    module("telethon.tl.functions.users", GetFullUserRequest=lambda user: user)
    module(
        "telethon.tl.types",
        InputUser=lambda identifier, secret: SimpleNamespace(id=identifier),
    )
    module("pytgcalls", PyTgCalls=lambda client: calls)
    module(
        "pytgcalls.types",
        ChatUpdate=Chat,
        StreamFrames=Frames,
        Device=SimpleNamespace(SPEAKER="speaker", MICROPHONE="microphone"),
        Direction=SimpleNamespace(INCOMING="incoming", OUTGOING="outgoing"),
        CallConfig=SimpleNamespace,
    )
    module("ntgcalls", MediaSource=SimpleNamespace(EXTERNAL="external"))

    class Parameters:
        def __init__(self, bitrate, channels):
            self.bitrate, self.channels = bitrate, channels

    class Stream:
        def __init__(self, **kwargs):
            self.microphone = self.speaker = self.camera = self.screen = None
            self.__dict__.update(kwargs)

    class Audio:
        def __init__(self, source, path, parameters):
            self.media_source, self.path, self.parameters = source, path, parameters

    module(
        "pytgcalls.types.raw",
        AudioParameters=Parameters,
        AudioStream=Audio,
        Stream=Stream,
    )
    settings = {
        "calls_enabled": True,
        "duplex_validated": True,
        "authenticated": True,
        "owner_bound": True,
        "account_authenticated": True,
        "era_phone": "+10000000",
        "owner_phone": "+10000001",
        "api_id": 1234,
        "api_hash": "a" * 32,
        "session_secret": "synthetic",
        "owner_user_id": 42,
        "era_user_id": 41,
        "owner_access_hash_secret": 7,
        "expected_owner_user_id": 42,
    }
    return settings, client, calls, Chat, Frames, Status


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["dial", "answer"])
async def test_owner_call_privacy_blocks_dial_but_allows_owner_incoming(monkeypatch, direction):
    settings, client, calls, _, _, _ = sdk_fixture(monkeypatch)
    client.return_value.full_user.phone_calls_private = True
    native, events = controller()
    await open_endpoint(settings, native)
    assert native.owner_calls_private
    if direction == "answer":
        assert native.incoming(42)
    await native.command(packet(direction))
    await until(lambda: native.state in {"connected", "failed"})
    if direction == "dial":
        assert events[-1].detail == "call_privacy"
        calls.play.assert_not_awaited()
    else:
        calls.play.assert_awaited_once()
        assert native.state == "connected"
    await native.close()
    if direction == "dial":
        calls.leave_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_name", "expected"),
    [("UserPrivacyRestrictedError", "call_privacy"), ("TimedOutAnswer", "no_answer")],
)
async def test_native_rejection_is_safe_and_actionable(exception_name, expected):
    native, events = controller()
    failure = type(exception_name, (Exception,), {})
    native.endpoint.connect.side_effect = failure("synthetic secret")
    await native.command(packet("dial"))
    await until(lambda: native.done.is_set())
    assert events[-1].detail == expected
    assert "synthetic secret" not in events[-1].encode().decode()
    await native.close()


@pytest.mark.asyncio
async def test_sdk_boundary_external_streams_and_actual_frame_field(monkeypatch):
    settings, client, calls, Chat, Frames, Status = sdk_fixture(monkeypatch)
    native, events = controller()
    endpoint = await open_endpoint(settings, native)
    assert isinstance(endpoint, SDKEndpoint)
    outgoing, incoming = external_streams()
    assert outgoing.microphone.media_source == incoming.microphone.media_source == "external"
    assert outgoing.microphone.parameters.bitrate == 24000
    assert incoming.microphone.parameters.bitrate == 16000
    assert outgoing.speaker is None and incoming.speaker is None
    assert outgoing.camera is None and outgoing.screen is None
    await calls.callback(calls, Chat(chat_id=43, status=Status.INCOMING_CALL))
    calls.leave_call.assert_awaited_once_with(43)
    await calls.callback(calls, Chat(chat_id=42, status=Status.INCOMING_CALL))
    assert native.state == "incoming" and not calls.play.called
    await native.command(packet("answer"))
    await until(lambda: native.state == "connected")
    await calls.callback(
        calls,
        Frames(
            chat_id=42,
            direction="outgoing",
            device="microphone",
            frames=[SimpleNamespace(frame=bytes(320))],
        ),
    )
    await calls.callback(
        calls,
        Frames(
            chat_id=42,
            direction="incoming",
            device="speaker",
            frames=[SimpleNamespace(frame=bytes(320))],
        ),
    )
    assert not any(event.kind == "pcm_in" for event in events)
    pcm = bytes(640)
    await calls.callback(
        calls,
        Frames(
            chat_id=42,
            direction="incoming",
            device="microphone",
            frames=[SimpleNamespace(frame=pcm, data=b"wrong field")],
        ),
    )
    assert events[-1].kind == "pcm_in" and events[-1].pcm == pcm
    native_pcm = b"\x01\x00" * 240 + b"\x02\x00" * 240
    await endpoint.send(native_pcm)
    assert calls.send_frame.await_count == 2
    submitted = [entry.args[2] for entry in calls.send_frame.await_args_list]
    assert all(len(frame) == 480 for frame in submitted)
    assert b"".join(submitted) == native_pcm
    await calls.callback(calls, Chat(chat_id=42, status=Status.LEFT_CALL | Status.BUSY_CALL))
    assert native.done.is_set() and native._discarded
    await native.close()
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_owner_ring_cannot_reuse_active_native_generation(monkeypatch):
    settings, _, calls, Chat, _, Status = sdk_fixture(monkeypatch)
    native, events = controller()
    await open_endpoint(settings, native)
    await native.command(packet("dial"))
    await until(lambda: native.state == "connected")
    await calls.callback(calls, Chat(chat_id=42, status=Status.INCOMING_CALL))
    assert native.done.is_set() and events[-1].kind == "failed"
    assert calls.play.await_count == 1
    await native.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["unauthorized", "wrong_era_id", "wrong_owner", "start", "callback"]
)
async def test_sdk_auth_identity_and_callback_faults_are_fail_closed(monkeypatch, failure):
    settings, client, calls, _, Frames, _ = sdk_fixture(monkeypatch)
    native, events = controller()
    if failure == "unauthorized":
        client.is_user_authorized.return_value = False
    elif failure == "wrong_era_id":
        settings["era_user_id"] = 100
    elif failure == "wrong_owner":
        client.get_entity.return_value.id = 43
    elif failure == "start":
        calls.start.side_effect = RuntimeError("synthetic")
    if failure != "callback":
        with pytest.raises((PermissionError, ValueError, RuntimeError)):
            await open_endpoint(settings, native)
        client.disconnect.assert_awaited_once()
    else:
        await open_endpoint(settings, native)
        native.state = "connected"
        await calls.callback(
            calls,
            Frames(
                chat_id=42,
                direction="incoming",
                device="microphone",
                frames=[SimpleNamespace(frame=b"x")],
            ),
        )
        assert native.done.is_set() and events[-1].kind == "failed"
        await native.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "normal", "setup_failed", "bad_pipe"])
async def test_child_private_stdio_disabled_guard_and_teardown(monkeypatch, mode):
    from core import telegram_call_helper as helper

    output = io.BytesIO()
    settings = {
        "owner_user_id": 42,
        "calls_enabled": mode != "disabled",
        "duplex_validated": True,
    }
    endpoint = AsyncMock()

    async def open_fake(settings, controller):
        if mode == "setup_failed":
            raise ValueError("synthetic")
        controller.endpoint = endpoint
        return endpoint

    data = (
        b"broken\n" if mode == "bad_pipe" else packet("dial").encode() + packet("hangup").encode()
    )
    monkeypatch.setattr("core.telegram_call_setup.load_settings", lambda path: settings)
    monkeypatch.setattr(helper, "open_endpoint", open_fake)
    # No real stdin, stored session, account or native instance is touched.
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        scoped.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
        task = asyncio.create_task(run_child(GEN))
        if mode == "disabled":
            await task
        else:
            await until(lambda: b'"kind":"stopped"' in output.getvalue())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    messages = [CallWire.decode(line + b"\n") for line in output.getvalue().splitlines()]
    if mode == "disabled":
        assert [m.kind for m in messages] == ["disabled"]
        assert not endpoint.connect.called
    else:
        assert messages[-1].kind == "stopped"
        if mode in {"setup_failed", "bad_pipe"}:
            assert any(m.kind == "failed" for m in messages)


@pytest.mark.parametrize("fail", [False, True])
def test_helper_cli_sanitizes_errors(monkeypatch, capsys, fail):
    monkeypatch.setattr(sys, "argv", ["helper", "--generation", GEN])
    replacement = AsyncMock(side_effect=RuntimeError("secret") if fail else None)
    monkeypatch.setattr("core.telegram_call_helper.run_child", replacement)
    assert main() == (1 if fail else 0)
    assert "secret" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_receipt_with_wrong_reply_cannot_account_native_submission(monkeypatch):
    audio, gen, parent, _ = supervisor(deadline=0.03)
    child = Child(gen)

    def write(data):
        message = CallWire.decode(data)
        if message.kind == "dial":
            child.emit("connected")
        elif message.kind == "pcm_out":
            child.emit("submitted", sequence=message.sequence, reply="c" * 32)
        elif message.kind == "hangup":
            child.emit("stopped", detail="leave_requested")

    child.stdin.write = write
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    await parent.command("dial")
    await until(lambda: audio.state == "connected")
    reply = audio.begin_reply(gen)
    audio.enqueue_reply(gen, reply, bytes(960))
    await until(lambda: parent._failure)
    assert audio.submitted_bytes == 0 and audio.state == "failed"
    await parent.close()


@pytest.mark.asyncio
async def test_child_eof_and_failed_connection_reported(monkeypatch):
    _, gen, parent, events = supervisor()
    child = Child(gen)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    child.emit("incoming")
    child.emit("failed", detail="native_failed")
    await until(lambda: parent._failure)
    child.stdout.feed_eof()
    await until(lambda: parent._stopped.is_set())
    await parent.close()
    assert any(e.kind == "incoming" for e in events)


@pytest.mark.asyncio
async def test_late_input_after_provider_failure_does_not_mask_original_error(
    monkeypatch,
):
    audio, gen, parent, _ = supervisor()
    child = Child(gen)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    child.emit("connected")
    await until(lambda: audio.state == "connected")
    audio.fail(gen, "provider_failed")
    child.emit("pcm_in", sequence=0, pcm=bytes(320))
    await asyncio.sleep(0.03)
    assert not parent._failure and audio.failure == "provider_failed"
    await parent.close()


@pytest.mark.asyncio
async def test_invalid_connected_transition_and_input_geometry_reported(monkeypatch):
    audio, gen, parent, _ = supervisor()
    child = Child(gen)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=child))
    await parent.start(Path(__file__))
    child.emit("connected")
    await until(lambda: audio.state == "connected")
    child.emit("pcm_in", sequence=2, pcm=bytes(640))
    await until(lambda: parent._failure)
    assert audio.state == "failed"
    await parent.close()


@pytest.mark.parametrize("timeout", [0, 61])
def test_controller_and_supervisor_deadlines_are_bounded(timeout):
    with pytest.raises(ValueError):
        controller(timeout)
    with pytest.raises(ValueError):
        supervisor(timeout)


def test_supervisor_requires_fresh_generation():
    audio = CallAudio(42)
    with pytest.raises(ValueError):
        CallProcess(audio, GEN, notify=lambda e: None)
