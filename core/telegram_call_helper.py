"""Optional native call child. Never import native bindings in Era's process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import threading
from collections.abc import Callable
from typing import Any, BinaryIO, Protocol

from core.telegram_call_diagnostics import record
from core.telegram_call_protocol import MAX_LINE, CallWire

_WIRE_OUTPUT: BinaryIO | None = None


def isolate_wire_output() -> BinaryIO:
    """Reserve the protocol pipe before redirecting native stdout to stderr."""
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return output


class Endpoint(Protocol):
    async def connect(self) -> None: ...
    async def send(self, pcm: bytes) -> None: ...
    async def leave(self) -> None: ...
    async def disconnect(self) -> None: ...


class NativeCall:
    """One peer and one generation per process; no reuse of native peer handles."""

    def __init__(
        self,
        generation: str,
        peer_id: int,
        emit: Callable[[CallWire], None],
        *,
        timeout: float = 8.0,
        connect_timeout: float = 45.0,
    ) -> None:
        CallWire("ready", generation, peer_id).validate()
        if not 0 < timeout <= 60 or not 0 < connect_timeout <= 60:
            raise ValueError("Bounded native deadline required.")
        self.generation, self.peer_id, self.emit, self.timeout = (
            generation,
            peer_id,
            emit,
            timeout,
        )
        self.connect_timeout = connect_timeout
        self.endpoint: Endpoint | None = None
        self.state = "waiting"
        self.reply = ""
        self._input_sequence = 0
        self._output_sequence = -1
        self._connect: asyncio.Task[None] | None = None
        self._discarded = False
        self._used = False
        self._stopping = False
        self._next_output = 0.0
        self.owner_calls_private = False
        self._direction = ""
        self.done = asyncio.Event()

    def event(self, kind: str, detail: str = "") -> None:
        try:
            self.emit(CallWire(kind, self.generation, self.peer_id, detail=detail))
        except asyncio.QueueFull:
            self.state = "failed"
            self.done.set()

    def incoming(self, peer_id: int) -> bool:
        if type(peer_id) is not int or peer_id != self.peer_id or self._used or self._stopping:
            return False
        if self.state != "waiting":
            return False
        self.state = "incoming"
        self.event("incoming")
        return True

    def discarded(self, peer_id: int, busy: bool = False) -> None:
        if type(peer_id) is not int or peer_id != self.peer_id:
            return
        self._discarded = True
        self.state = "ended"
        self.event("discarded", "busy" if busy else "discarded")
        self.done.set()

    def receive(self, peer_id: int, pcm: bytes) -> None:
        if (
            type(peer_id) is not int
            or peer_id != self.peer_id
            or self.state != "connected"
            or self._stopping
        ):
            return
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 3200:
            raise ValueError("Invalid native input frame.")
        if self._input_sequence == 0:
            record("native_input_received", self.generation)
        self.emit(
            CallWire(
                "pcm_in",
                self.generation,
                self.peer_id,
                sequence=self._input_sequence,
                pcm=pcm,
            )
        )
        self._input_sequence += 1

    async def _establish(self) -> None:
        try:
            record("native_connect_start", self.generation)
            if self._direction == "dial" and self.owner_calls_private:
                # No native call was started; cleanup must not attempt a leave.
                self._used = False
                self.state = "failed"
                self.event("failed", "call_privacy")
                self.done.set()
                return
            if self.endpoint is None:
                raise RuntimeError("Native endpoint unavailable.")
            await asyncio.wait_for(self.endpoint.connect(), self.connect_timeout)
            if self._stopping or self._discarded:
                return
            self.state = "connected"
            record("native_connected", self.generation)
            self.event("connected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record("native_connect_failed", self.generation, exc)
            self.state = "failed"
            # Never expose SDK exception messages, which can contain account data.
            code = {
                "UserPrivacyRestrictedError": "call_privacy",
                "TimedOutAnswer": "no_answer",
            }.get(type(exc).__name__, "native_failed")
            self.event("failed", code)
            self.done.set()

    async def command(self, packet: CallWire) -> None:
        packet.validate()
        if packet.generation != self.generation or packet.peer_id != self.peer_id:
            raise PermissionError("Wrong call identity.")
        if packet.kind == "hangup":
            self.done.set()
            return
        if self._stopping or self.state in {"ended", "failed"}:
            return
        if packet.kind in {"dial", "answer"}:
            required = "waiting" if packet.kind == "dial" else "incoming"
            if self._used or self.state != required:
                raise RuntimeError("Duplicate or invalid native call start.")
            self._used = True
            self._direction = packet.kind
            self.state = "negotiating"
            self._connect = asyncio.create_task(self._establish())
        elif packet.kind == "reply":
            if self.state != "connected":
                raise RuntimeError("No connected call for speech.")
            self.reply = packet.reply
        elif packet.kind == "pcm_out":
            if self.state != "connected" or self.endpoint is None:
                raise RuntimeError("No connected call for PCM.")
            if packet.reply != self.reply or packet.sequence <= self._output_sequence:
                return
            if self._next_output == 0.0:
                self._next_output = asyncio.get_running_loop().time()
            delay = self._next_output - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            await asyncio.wait_for(self.endpoint.send(packet.pcm), self.timeout)
            # Advance the audio clock, rather than adding pipe/send latency to
            # every frame. Accumulating that latency creates audible gaps.
            now = asyncio.get_running_loop().time()
            self._next_output = max(self._next_output + 0.02, now)
            self._output_sequence = packet.sequence
            self.emit(
                CallWire(
                    "submitted",
                    self.generation,
                    self.peer_id,
                    sequence=packet.sequence,
                    reply=packet.reply,
                )
            )
        else:
            raise ValueError("Unknown native control command.")

    async def close(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.reply = ""
        clean = True
        if self._connect and not self._connect.done():
            self._connect.cancel()
            settled, _ = await asyncio.wait([self._connect], timeout=self.timeout)
            clean = bool(settled)
        if self.endpoint:
            if not self._discarded and (self._used or self.state == "incoming"):
                try:
                    await asyncio.wait_for(self.endpoint.leave(), self.timeout)
                except Exception:
                    clean = False
            try:
                await asyncio.wait_for(self.endpoint.disconnect(), self.timeout)
            except Exception as exc:
                record("native_disconnect_failed", self.generation, exc)
                clean = False
        detail = (
            "cleanup_incomplete"
            if not clean
            else ("discard_observed" if self._discarded else "leave_requested")
        )
        self.event("stopped", detail)
        self.done.set()


def external_streams() -> tuple[Any, Any]:
    """SDK descriptions only: explicit external sources, no device/camera defaults."""
    from ntgcalls import MediaSource
    from pytgcalls.types.raw import AudioParameters, AudioStream, Stream

    return (
        Stream(microphone=AudioStream(MediaSource.EXTERNAL, "", AudioParameters(24000, 1))),
        Stream(
            # PLAYBACK microphone denotes the remote participant's audio.
            # This matches RecordStream(audio=True), not the speaker mix.
            microphone=AudioStream(MediaSource.EXTERNAL, "", AudioParameters(16000, 1))
        ),
    )


class SDKEndpoint:
    def __init__(self, client: Any, calls: Any, owner: int, generation: str = "") -> None:
        self.client, self.calls, self.owner = client, calls, owner
        self.generation = generation

    async def connect(self) -> None:
        from pytgcalls.types import CallConfig

        outgoing, incoming = external_streams()
        record("telegram_signaling_start", self.generation)
        await self.calls.play(self.owner, outgoing, config=CallConfig(timeout=30))
        record("telegram_signaling_connected", self.generation)
        await self.calls.record(self.owner, incoming)
        record("native_recording_ready", self.generation)

    async def send(self, pcm: bytes) -> None:
        from pytgcalls.types import Device

        if len(pcm) != 960:
            raise ValueError("A 20 ms mono 24 kHz PCM packet is required.")
        # NTgCalls 3.0.3 consumes exactly 10 ms per external audio frame.
        # A 960-byte submission silently loses its second half at this boundary.
        second_frame_at = asyncio.get_running_loop().time() + 0.01
        await self.calls.send_frame(self.owner, Device.MICROPHONE, pcm[:480])
        await asyncio.sleep(max(0, second_frame_at - asyncio.get_running_loop().time()))
        await self.calls.send_frame(self.owner, Device.MICROPHONE, pcm[480:])

    async def leave(self) -> None:
        await self.calls.leave_call(self.owner)

    async def disconnect(self) -> None:
        await self.client.disconnect()


async def open_endpoint(settings: dict[str, Any], controller: NativeCall) -> SDKEndpoint:
    """Reuse sealed authorization only; never prompt for credentials in a helper."""
    from core.telegram_call_activation import calling_enabled
    from core.telegram_call_setup import validate_identities, validate_setup

    if not calling_enabled(settings) or not all(
        settings.get(key) is True
        for key in (
            "calls_enabled",
            "authenticated",
            "owner_bound",
            "account_authenticated",
        )
    ):
        raise PermissionError("Calling is disabled or unverified.")
    validate_setup(settings)
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.types import InputUser

    client = TelegramClient(
        StringSession(settings["session_secret"]),
        settings["api_id"],
        settings["api_hash"],
    )
    try:
        await asyncio.wait_for(client.connect(), controller.timeout)
        if not await asyncio.wait_for(client.is_user_authorized(), controller.timeout):
            raise PermissionError("Local call session requires authentication.")
        me = await asyncio.wait_for(client.get_me(), controller.timeout)
        owner = await asyncio.wait_for(
            client.get_entity(
                InputUser(settings["owner_user_id"], settings["owner_access_hash_secret"])
            ),
            controller.timeout,
        )
        validate_identities(settings, me, owner)
        if owner.id != controller.peer_id or me.id != settings["era_user_id"]:
            raise PermissionError("Local account binding changed.")
        permissions = await asyncio.wait_for(client(GetFullUserRequest(owner)), controller.timeout)
        controller.owner_calls_private = permissions.full_user.phone_calls_private is True
        from pytgcalls import PyTgCalls
        from pytgcalls.types import ChatUpdate, Device, Direction, StreamFrames

        calls = PyTgCalls(client)
        endpoint = SDKEndpoint(client, calls, controller.peer_id, controller.generation)
        controller.endpoint = endpoint

        async def update(_: Any, event: Any) -> None:
            try:
                if isinstance(event, StreamFrames):
                    if event.direction == Direction.INCOMING and event.device == Device.MICROPHONE:
                        for frame in event.frames:
                            controller.receive(event.chat_id, frame.frame)
                elif isinstance(event, ChatUpdate):
                    if event.status & ChatUpdate.Status.INCOMING_CALL:
                        if not controller.incoming(event.chat_id):
                            # Refuse unknown peers. Never auto-answer through play().
                            if event.chat_id != controller.peer_id:
                                await asyncio.wait_for(
                                    calls.leave_call(event.chat_id), controller.timeout
                                )
                            elif controller.state != "incoming":
                                # Peer-only SDK callbacks cannot distinguish a
                                # second call from an active generation safely.
                                controller.done.set()
                                controller.event("failed", "native_failed")
                    elif event.status & ChatUpdate.Status.LEFT_CALL:
                        controller.discarded(
                            event.chat_id,
                            bool(event.status & ChatUpdate.Status.BUSY_CALL),
                        )
            except Exception as exc:
                record("native_callback_failed", controller.generation, exc)
                controller.done.set()
                controller.event("failed", "native_failed")

        calls.on_update()(update)

        await asyncio.wait_for(calls.start(), controller.timeout)
        return endpoint
    except BaseException:
        await asyncio.wait_for(client.disconnect(), controller.timeout)
        raise


async def run_child(generation: str) -> None:
    """Private inherited pipes; bounded queues; structured diagnostics only."""
    from core.telegram_call_activation import calling_enabled
    from core.telegram_call_setup import load_settings, private_directory

    settings = load_settings(private_directory())
    owner = settings.get("owner_user_id")
    output = _WIRE_OUTPUT or sys.stdout.buffer
    incoming = sys.stdin.buffer
    write_lock = threading.Lock()

    def write(packet: CallWire) -> None:
        with write_lock:
            output.write(packet.encode())
            output.flush()

    queue: asyncio.Queue[CallWire] = asyncio.Queue(50)
    controller = NativeCall(generation, owner, queue.put_nowait)
    if not calling_enabled(settings):
        write(CallWire("disabled", generation, owner, detail="calls_disabled"))
        return

    async def writer() -> None:
        while True:
            packet = await queue.get()
            await asyncio.to_thread(write, packet)

    async def reader() -> None:
        while not controller.done.is_set():
            line = await asyncio.to_thread(incoming.readline, MAX_LINE + 1)
            if not line:
                controller.done.set()
                return
            await controller.command(CallWire.decode(line))

    writer_task = asyncio.create_task(writer())
    expiry_task: asyncio.Task[None] | None = None
    if settings.get("attended_test_mode") is True:
        import time

        async def expire() -> None:
            await asyncio.sleep(max(0, settings["attended_test_until"] - time.time()))
            controller.event("failed", "test_window_expired")
            controller.done.set()

        expiry_task = asyncio.create_task(expire())
    reader_task: asyncio.Task[None] | None = None
    failed = False
    failure_code = "native_failed"
    endpoint_opened = False
    try:
        # Native SDK banners/logs cannot enter the machine-readable pipe.
        with contextlib.redirect_stdout(sys.stderr):
            controller.endpoint = await open_endpoint(settings, controller)
            endpoint_opened = True
            controller.event("ready")
            reader_task = asyncio.create_task(reader())
            stopped = asyncio.create_task(controller.done.wait())
            try:
                done, _ = await asyncio.wait(
                    [reader_task, writer_task, stopped],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.result()
            finally:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
    except Exception as exc:
        record("native_helper_failed", generation, exc)
        failed = True
        if not endpoint_opened:
            if isinstance(exc, PermissionError):
                failure_code = "auth_required"
            elif isinstance(exc, ValueError):
                failure_code = "identity_invalid"
    finally:
        if expiry_task:
            expiry_task.cancel()
            await asyncio.gather(expiry_task, return_exceptions=True)
        if reader_task:
            reader_task.cancel()
        # Emit teardown receipt directly after stopping the stream writer.
        writer_task.cancel()
        await asyncio.gather(
            writer_task, *([reader_task] if reader_task else []), return_exceptions=True
        )
        receipts: list[CallWire] = []
        if failed:
            receipts.append(CallWire("failed", generation, owner, detail=failure_code))
        controller.emit = receipts.append
        with contextlib.redirect_stdout(sys.stderr):
            await controller.close()
        for receipt in receipts:
            await asyncio.to_thread(write, receipt)
    # Known native finalizer hang: supervisor reaps this exact child after receipt.
    await asyncio.Event().wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run_child(args.generation))
        return 0
    except Exception:
        # Avoid leaking SDK exception strings, sessions or account hashes.
        print(
            "Native call helper failed; inspect structured parent status.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    # redirect_stdout cannot catch C/C++ writes to descriptor 1. Keep the
    # protocol on its own duplicate so native logs cannot corrupt JSON frames.
    _WIRE_OUTPUT = isolate_wire_output()
    raise SystemExit(main())
