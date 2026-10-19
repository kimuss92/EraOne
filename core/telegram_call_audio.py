"""Owner-bound PCM queues for the isolated native Telegram transport.

No microphone, speaker, network or account access occurs in this module. Native
external sources request 16 kHz input and 24 kHz output, both mono int16 PCM.
Queue acceptance and native submission are distinct from remote audibility.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

CallState = Literal["idle", "negotiating", "connected", "active", "ended", "failed"]


@dataclass(frozen=True)
class CallPCM:
    generation: str
    reply: str
    sequence: int
    sample_rate: int
    data: bytes = field(repr=False)


class CallAudio:
    """One call generation, one peer, bounded queues and explicit reply ownership."""

    def __init__(self, owner_id: int, capacity: int = 25) -> None:
        if (
            not isinstance(owner_id, int)
            or isinstance(owner_id, bool)
            or owner_id <= 0
            or not isinstance(capacity, int)
            or isinstance(capacity, bool)
            or not 1 <= capacity <= 100
        ):
            raise ValueError("A positive owner ID and bounded capacity are required.")
        self.owner_id = owner_id
        self.capacity = capacity
        self.state: CallState = "idle"
        self.generation = ""
        self.reply = ""
        self._sealed_reply = False
        self.failure = ""
        self._lock = threading.RLock()
        self._input: deque[CallPCM] = deque()
        self._output: deque[CallPCM] = deque()
        self._pending: dict[int, CallPCM] = {}
        self._tail = bytearray()
        self._input_sequence = -1
        self._output_sequence = 0
        self.input_bytes = 0
        self.submitted_bytes = 0

    def begin(self, peer_id: int) -> str:
        """Reserve only the configured owner; overlapping calls are refused."""
        with self._lock:
            if (
                not isinstance(peer_id, int)
                or isinstance(peer_id, bool)
                or peer_id != self.owner_id
            ):
                raise PermissionError("Unknown Telegram call peer.")
            if self.state in {"negotiating", "connected", "active"}:
                raise RuntimeError("A call is already in progress.")
            self._clear()
            self.generation = uuid.uuid4().hex
            self.reply = ""
            self.failure = ""
            self._input_sequence = -1
            self._output_sequence = 0
            self.input_bytes = self.submitted_bytes = 0
            self.state = "negotiating"
            return self.generation

    def connected(self, generation: str, input_rate: int, output_rate: int) -> bool:
        """Accept transport readiness only for the configured external PCM formats."""
        with self._lock:
            if generation != self.generation or self.state != "negotiating":
                return False
            if (
                not isinstance(input_rate, int)
                or not isinstance(output_rate, int)
                or (input_rate, output_rate) != (16000, 24000)
            ):
                self._fail("pcm_format_mismatch")
                return False
            self.state = "connected"
            return True

    def _live(self, generation: str) -> bool:
        return generation == self.generation and self.state in {"connected", "active"}

    def receive(self, peer_id: int, generation: str, sequence: int, pcm: bytes) -> bool:
        """Queue only ordered, whole-sample remote input; never loop TTS into it."""
        with self._lock:
            if (
                not isinstance(peer_id, int)
                or isinstance(peer_id, bool)
                or peer_id != self.owner_id
                or not self._live(generation)
            ):
                return False
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                self._fail("invalid_input_sequence")
                return False
            if sequence <= self._input_sequence:
                return False
            if sequence != self._input_sequence + 1:
                self._fail("input_sequence_gap")
                return False
            # Bound individual callbacks as well as queue length (maximum 100 ms).
            if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 3200:
                self._fail("invalid_input_pcm")
                return False
            if (
                len(self._input) >= self.capacity
                or sum(len(packet.data) for packet in self._input) + len(pcm) > self.capacity * 640
            ):
                self._fail("input_backpressure")
                return False
            self._input_sequence = sequence
            self._input.append(CallPCM(generation, "", sequence, 16000, pcm))
            self.input_bytes += len(pcm)
            self._observe_flow()
            return True

    def take_input(self, generation: str) -> CallPCM | None:
        with self._lock:
            if not self._live(generation) or not self._input:
                return None
            return self._input.popleft()

    def begin_reply(self, generation: str) -> str:
        """Invalidate unfinished output before granting a new reply token."""
        with self._lock:
            if not self._live(generation):
                raise RuntimeError("No connected call for this generation.")
            self._output.clear()
            self._pending.clear()
            self._tail.clear()
            self.reply = uuid.uuid4().hex
            self._sealed_reply = False
            return self.reply

    def enqueue_reply(
        self, generation: str, reply: str, pcm: bytes, *, final: bool = False
    ) -> bool:
        """Packetize Gemini PCM into 20 ms native frames, preserving the final tail."""
        with self._lock:
            if not self._live(generation) or not reply or reply != self.reply or self._sealed_reply:
                return False
            if not isinstance(pcm, bytes) or len(pcm) % 2:
                self._fail("invalid_output_pcm")
                return False
            if len(pcm) + len(self._tail) > self.capacity * 960:
                self._fail("output_backpressure")
                return False
            data = bytes(self._tail) + pcm
            count, tail = divmod(len(data), 960)
            needed = count + int(final and bool(tail))
            if len(self._output) + len(self._pending) + needed > self.capacity:
                self._fail("output_backpressure")
                return False
            for offset in range(0, count * 960, 960):
                self._queue_output(data[offset : offset + 960])
            self._tail = bytearray(data[count * 960 :])
            if final and self._tail:
                self._queue_output(bytes(self._tail).ljust(960, b"\0"))
                self._tail.clear()
            if final:
                self._sealed_reply = True
            return True

    def _queue_output(self, pcm: bytes) -> None:
        self._output.append(CallPCM(self.generation, self.reply, self._output_sequence, 24000, pcm))
        self._output_sequence += 1

    def take_output(self, generation: str) -> CallPCM | None:
        with self._lock:
            if not self._live(generation) or not self._output:
                return None
            packet = self._output.popleft()
            self._pending[packet.sequence] = packet
            return packet

    def output_room(self, generation: str, reply: str) -> int:
        """Expose available frame slots for a bounded asynchronous producer."""
        with self._lock:
            if not self._live(generation) or not reply or reply != self.reply:
                return 0
            return self.capacity - len(self._output) - len(self._pending)

    def fail(self, generation: str, reason: str) -> bool:
        """Invalidate one live generation without affecting a replacement call."""
        with self._lock:
            if not self._live(generation):
                return False
            self._fail(reason)
            return True

    def submitted(self, packet: CallPCM) -> bool:
        """Record native acceptance once; this is not a remote playback receipt."""
        with self._lock:
            if not self._live(packet.generation) or packet.reply != self.reply:
                return False
            if self._pending.get(packet.sequence) is not packet:
                return False
            self._pending.pop(packet.sequence)
            self.submitted_bytes += len(packet.data)
            self._observe_flow()
            return True

    def interrupt(self, generation: str) -> bool:
        """Discard queued replies and reject late output from the interrupted turn."""
        with self._lock:
            if not self._live(generation):
                return False
            self.reply = ""
            self._output.clear()
            self._pending.clear()
            self._tail.clear()
            return True

    def end(self, generation: str) -> bool:
        with self._lock:
            if generation != self.generation or self.state in {
                "idle",
                "ended",
                "failed",
            }:
                return False
            self._clear()
            self.reply = ""
            self.state = "ended"
            return True

    def _clear(self) -> None:
        self._input.clear()
        self._output.clear()
        self._pending.clear()
        self._tail.clear()

    def _fail(self, reason: str) -> None:
        self.failure = reason
        self._clear()
        self.reply = ""
        self.state = "failed"

    def _observe_flow(self) -> None:
        if self.input_bytes and self.submitted_bytes:
            self.state = "active"

    def snapshot(self) -> dict[str, object]:
        """Return bounded non-audio diagnostics; remote playback stays unknown."""
        with self._lock:
            return {
                "state": self.state,
                "generation": self.generation,
                "input_bytes": self.input_bytes,
                "submitted_bytes": self.submitted_bytes,
                "remote_playback_verified": False,
                "failure": self.failure,
            }
