"""Bounded JSON/PCM protocol for an owned child process's private pipes."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

MAX_LINE = 8192
KINDS = frozenset(
    {
        "dial",
        "answer",
        "hangup",
        "reply",
        "pcm_out",
        "ready",
        "incoming",
        "connected",
        "pcm_in",
        "submitted",
        "discarded",
        "failed",
        "stopped",
        "disabled",
    }
)
DETAILS = frozenset(
    {
        "",
        "calls_disabled",
        "identity_invalid",
        "auth_required",
        "native_failed",
        "call_privacy",
        "no_answer",
        "protocol_failed",
        "input_backpressure",
        "busy",
        "discarded",
        "leave_requested",
        "discard_observed",
        "cleanup_incomplete",
        "parent_closed",
        "test_window_expired",
    }
)


@dataclass(frozen=True)
class CallWire:
    kind: str
    generation: str
    peer_id: int
    sequence: int = -1
    reply: str = ""
    pcm: bytes = field(default=b"", repr=False)
    detail: str = ""

    def validate(self) -> None:
        if (
            not isinstance(self.kind, str)
            or not isinstance(self.detail, str)
            or self.kind not in KINDS
            or self.detail not in DETAILS
        ):
            raise ValueError("Unknown call protocol code.")
        if not isinstance(self.generation, str) or not re.fullmatch(
            r"[0-9a-f]{32}", self.generation
        ):
            raise ValueError("Invalid call generation.")
        if type(self.peer_id) is not int or self.peer_id <= 0:
            raise ValueError("Invalid call peer.")
        if type(self.sequence) is not int or not -1 <= self.sequence < 2**53:
            raise ValueError("Invalid frame sequence.")
        if not isinstance(self.reply, str) or (
            self.reply and not re.fullmatch(r"[0-9a-f]{32}", self.reply)
        ):
            raise ValueError("Invalid reply identity.")
        if not isinstance(self.pcm, bytes):
            raise ValueError("Invalid PCM value.")
        if self.kind in {"pcm_in", "pcm_out"}:
            if self.sequence < 0 or not self.pcm or len(self.pcm) % 2 or len(self.pcm) > 3200:
                raise ValueError("Invalid PCM packet.")
            if self.kind == "pcm_out" and (len(self.pcm) != 960 or not self.reply):
                raise ValueError("Output must be an owned 20 ms frame.")
            if self.kind == "pcm_in" and self.reply:
                raise ValueError("Remote input cannot claim a reply.")
        elif self.pcm:
            raise ValueError("Control messages cannot contain PCM.")
        if self.kind in {"reply", "submitted"} and not self.reply:
            raise ValueError("Reply identity required.")
        if self.kind == "submitted" and self.sequence < 0:
            raise ValueError("Frame receipt requires a sequence.")

    def encode(self) -> bytes:
        self.validate()
        line = (
            json.dumps(
                {
                    "version": 1,
                    "kind": self.kind,
                    "generation": self.generation,
                    "peer_id": self.peer_id,
                    "sequence": self.sequence,
                    "reply": self.reply,
                    "pcm": base64.b64encode(self.pcm).decode("ascii"),
                    "detail": self.detail,
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            + b"\n"
        )
        if len(line) > MAX_LINE:
            raise ValueError("Call protocol packet exceeds its limit.")
        return line

    @classmethod
    def decode(cls, line: bytes) -> CallWire:
        if not isinstance(line, bytes) or not line.endswith(b"\n") or len(line) > MAX_LINE:
            raise ValueError("Incomplete or oversized call protocol line.")
        try:
            payload = json.loads(line)
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {
                    "version",
                    "kind",
                    "generation",
                    "peer_id",
                    "sequence",
                    "reply",
                    "pcm",
                    "detail",
                }
                or type(payload["version"]) is not int
                or payload["version"] != 1
            ):
                raise ValueError("Incompatible call protocol.")
            if not isinstance(payload["pcm"], str):
                raise ValueError("Invalid encoded PCM.")
            packet = cls(
                payload["kind"],
                payload["generation"],
                payload["peer_id"],
                payload["sequence"],
                payload["reply"],
                base64.b64decode(payload["pcm"], validate=True),
                payload["detail"],
            )
            packet.validate()
            return packet
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError("Invalid call protocol packet.") from exc
