"""Owned-process supervisor connecting native private pipes to call PCM queues."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.telegram_call_audio import CallAudio, CallPCM
from core.telegram_call_diagnostics import record
from core.telegram_call_protocol import MAX_LINE, CallWire


@dataclass(frozen=True)
class CallShutdown:
    process_reaped: bool
    teardown_receipt: str
    remote_discard_observed: bool


class CallProcess:
    """One disposable helper; exact owned process handles, never PID searches."""

    def __init__(
        self,
        audio: CallAudio,
        generation: str,
        *,
        notify: Callable[[CallWire], None],
        deadline: float = 8.0,
        cleanup_deadline: float = 30.0,
    ) -> None:
        snapshot = audio.snapshot()
        if snapshot["generation"] != generation or snapshot["state"] != "negotiating":
            raise ValueError("Reserve a fresh call generation before launching its helper.")
        if not 0 < deadline <= 60 or not 0 < cleanup_deadline <= 60:
            raise ValueError("Bounded process deadlines required.")
        self.audio, self.generation, self.notify = audio, generation, notify
        self.deadline, self.cleanup_deadline = deadline, cleanup_deadline
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._sender: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._stopped = asyncio.Event()
        self._closing = False
        self._used = False
        self._pending: dict[int, tuple[CallPCM, asyncio.Future[bool]]] = {}
        self._receipt = ""
        self._failure = ""
        self._remote_discarded = False

    async def start(self, interpreter: Path | None = None) -> None:
        if self._used:
            raise RuntimeError("A native helper cannot be reused.")
        self._used = True
        if interpreter is None:
            local = os.environ.get("LOCALAPPDATA")
            if not local:
                raise RuntimeError("Optional native environment unavailable.")
            interpreter = Path(local) / "EraOne/telegram-calls/.venv/Scripts/python.exe"
        if not interpreter.is_file():
            raise FileNotFoundError("Optional native interpreter unavailable.")
        environment = dict(os.environ)
        environment["ERA_LOG_DIR"] = str(
            Path(tempfile.gettempdir()) / ("era-call-" + self.generation)
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(interpreter),
                "-m",
                "core.telegram_call_helper",
                "--generation",
                self.generation,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_LINE + 1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._reader = asyncio.create_task(self._read())
            await asyncio.wait_for(self._ready.wait(), self.deadline)
            if self._failure:
                raise RuntimeError("Native call helper unavailable: " + self._failure)
            self._sender = asyncio.create_task(self._send_output())
        except BaseException:
            await self.close()
            raise

    async def command(self, kind: str, *, reply: str = "", packet: CallPCM | None = None) -> None:
        message = CallWire(
            kind,
            self.generation,
            self.audio.owner_id,
            sequence=packet.sequence if packet else -1,
            reply=reply,
            pcm=packet.data if packet else b"",
        )
        data = message.encode()
        if (
            self.process is None
            or self.process.stdin is None
            or self.process.returncode is not None
        ):
            raise ConnectionError("Native helper pipe unavailable.")
        async with self._write_lock:
            self.process.stdin.write(data)
            await asyncio.wait_for(self.process.stdin.drain(), self.deadline)

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    if not (self._closing or self._remote_discarded or self._stopped.is_set()):
                        raise ConnectionError("Native helper exited.")
                    return
                message = CallWire.decode(line)
                if message.generation != self.generation or message.peer_id != self.audio.owner_id:
                    raise PermissionError("Native helper identity mismatch.")
                if message.kind == "ready":
                    self._ready.set()
                elif message.kind == "connected":
                    if not self.audio.connected(self.generation, 16000, 24000):
                        raise RuntimeError("Invalid native connection transition.")
                elif message.kind == "pcm_in":
                    if self.audio.state not in {"connected", "active"}:
                        # A provider failure/normal hangup can precede the last
                        # native input frame. It is not a new protocol failure.
                        continue
                    if not self.audio.receive(
                        message.peer_id, self.generation, message.sequence, message.pcm
                    ):
                        if self.audio.state == "failed":
                            raise RuntimeError("Native input rejected.")
                elif message.kind == "submitted":
                    pending = self._pending.get(message.sequence)
                    if pending and pending[0].reply == message.reply:
                        packet, receipt = self._pending.pop(message.sequence)
                        accepted = self.audio.submitted(packet)
                        if accepted and self.audio.submitted_bytes == len(packet.data):
                            record("native_audio_submitted", self.generation)
                        if not receipt.done():
                            receipt.set_result(accepted)
                elif message.kind == "discarded":
                    self._remote_discarded = True
                    self.audio.end(self.generation)
                    if self._sender:
                        self._sender.cancel()
                elif message.kind in {"failed", "disabled"}:
                    self._failure = message.detail or "native_failed"
                    self.audio.fail(self.generation, self._failure)
                    self._ready.set()
                elif message.kind == "stopped":
                    self._receipt = message.detail
                    self._remote_discarded |= message.detail == "discard_observed"
                    self._stopped.set()
                    if self._sender:
                        self._sender.cancel()
                elif message.kind != "incoming":
                    raise ValueError("Unexpected native helper event.")
                self.notify(message)
                if message.kind == "disabled":
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record("native_protocol_failed", self.generation, exc)
            self._failure = "protocol_failed"
            self.audio.fail(self.generation, self._failure)
            self._ready.set()
            self._stopped.set()
            self.notify(
                CallWire("failed", self.generation, self.audio.owner_id, detail=self._failure)
            )

    async def _send_output(self) -> None:
        reply = ""
        try:
            while not self._closing and not self._stopped.is_set():
                packet = self.audio.take_output(self.generation)
                if packet is None:
                    await asyncio.sleep(0.005)
                    continue
                if packet.reply != reply:
                    reply = packet.reply
                    await self.command("reply", reply=reply)
                if self.audio.reply != packet.reply:
                    continue
                receipt: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                self._pending[packet.sequence] = (packet, receipt)
                await self.command("pcm_out", reply=reply, packet=packet)
                # One frame in flight prevents a large native playback backlog.
                await asyncio.wait_for(receipt, self.deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._closing or self._remote_discarded or self._stopped.is_set():
                return
            self._failure = "native_failed"
            self.audio.fail(self.generation, self._failure)
            self.notify(
                CallWire("failed", self.generation, self.audio.owner_id, detail=self._failure)
            )
            self._stopped.set()

    async def close(self) -> CallShutdown:
        self._closing = True
        self.audio.end(self.generation)
        if self._sender:
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
        for _, receipt in self._pending.values():
            receipt.cancel()
        self._pending.clear()
        process = self.process
        if process is not None:
            if process.returncode is None:
                try:
                    await self.command("hangup")
                    receipt_wait = asyncio.create_task(self._stopped.wait())
                    exit_wait = asyncio.create_task(process.wait())
                    try:
                        await asyncio.wait(
                            [receipt_wait, exit_wait],
                            timeout=self.cleanup_deadline,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        receipt_wait.cancel()
                        exit_wait.cancel()
                        await asyncio.gather(receipt_wait, exit_wait, return_exceptions=True)
                except Exception:
                    pass
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            await asyncio.wait_for(process.wait(), self.deadline)
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.stdin.wait_closed(), self.deadline)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if process is not None and process.returncode is not None:
            # Python 3.14 Windows can retain the stdout pipe after process.wait().
            # Close only this already-reaped child's transport, never other handles.
            owned_transport = getattr(process, "_transport", None)
            if owned_transport is not None:
                owned_transport.close()
                await asyncio.sleep(0)
        return CallShutdown(
            process is None or process.returncode is not None,
            self._receipt or "cleanup_incomplete",
            self._remote_discarded,
        )
