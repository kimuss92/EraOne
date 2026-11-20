"""One owner-bound native call and one separately owned Gemini conversation."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Protocol, cast

from core.telegram_call_audio import CallAudio
from core.telegram_call_diagnostics import record
from core.telegram_call_gemini import (
    CallCommand,
    CallEvent,
    Dispatch,
    GeminiCall,
    InputPolicy,
    LiveSession,
)
from core.telegram_call_process import CallProcess, CallShutdown
from core.telegram_call_protocol import CallWire


class Transport(Protocol):
    async def start(self) -> None: ...
    async def command(self, kind: str) -> None: ...
    async def close(self) -> CallShutdown: ...


Provider = Callable[[], AbstractAsyncContextManager[LiveSession]]
TransportFactory = Callable[[CallAudio, str, Callable[[CallWire], None]], Transport]


def is_call_goodbye(text: str) -> bool:
    """Match an entire immediate call-ending utterance, never a quoted substring."""
    normalized = " ".join(re.sub(r"[^a-z\s]", " ", text.lower()).split())
    return (
        re.fullmatch(
            r"(?:(?:era|please|can you|could you|would you|ok|okay|good|great|"
            r"thank you|thanks|that s all|all right) )*"
            r"(?:hang up|hangup|end (?:the |this |our )?call|"
            r"stop (?:the |this )call|disconnect (?:the |this )call|"
            r"goodbye|good bye|bye(?: bye)?|see you(?: later)?)"
            r"(?: (?:now|please|for me|era|thank you|thanks|bye|goodbye|sir))*",
            normalized,
        )
        is not None
    )


def native_transport(
    audio: CallAudio, generation: str, notify: Callable[[CallWire], None]
) -> Transport:
    return cast(Transport, CallProcess(audio, generation, notify=notify))


class CallCoordinator:
    """Loop-owned lifecycle. No implicit opt-in, hardware access or tool bypass."""

    def __init__(
        self,
        owner_id: int | None,
        *,
        enabled: bool,
        provider: Provider | None,
        dispatch: Dispatch | None,
        input_policy: InputPolicy | None,
        notify: Callable[[dict[str, object]], None],
        transport: TransportFactory = native_transport,
        deadline: float = 8.0,
        connect_deadline: float = 45.0,
    ) -> None:
        if owner_id is not None and (type(owner_id) is not int or owner_id <= 0):
            raise ValueError("Verified owner ID required.")
        if not 0 < deadline <= 60 or not 0 < connect_deadline <= 60:
            raise ValueError("Bounded coordinator deadlines required.")
        self.owner_id, self.enabled = owner_id, enabled
        self.instance_id = uuid.uuid4().hex
        self.provider, self.dispatch, self.input_policy = (
            provider,
            dispatch,
            input_policy,
        )
        self.notify, self.transport_factory = notify, transport
        self.deadline, self.connect_deadline = deadline, connect_deadline
        self.audio: CallAudio | None = None
        self.generation = ""
        self.state = "disabled" if not self.ready else "idle"
        self.error = ""
        self.input_caption = self.output_caption = ""
        self._new_caption = True
        self._revision = 0
        self._task: asyncio.Task[None] | None = None
        self._unsettled: set[asyncio.Task[object]] = set()
        self._stop = asyncio.Event()
        self._incoming = asyncio.Event()
        self._connected = asyncio.Event()
        self._shutdown: CallShutdown | None = None
        self._transport: Transport | None = None
        self._cleanup_blocked = False
        self.test_window_lock = asyncio.Lock()
        self.listening_armed = False
        self._mode = ""
        self._queued_dial = False
        self._queued_notification = ""
        self._queued_guard: Callable[[], bool] | None = None
        self._notification = ""
        self._dial_guard: Callable[[], bool] | None = None
        self._retry_timer: asyncio.TimerHandle | None = None
        self._retry_attempt = 0
        self._remote_ended = False
        self._hangup_requested = False
        self._expiry: asyncio.TimerHandle | None = None
        self._publish()

    @property
    def ready(self) -> bool:
        return bool(
            self.owner_id and self.enabled and self.provider and self.dispatch and self.input_policy
        )

    def set_test_expiry(self, delay: float) -> None:
        if not 0 < delay <= 1800:
            raise ValueError("A bounded attended test window is required.")

        def expire() -> None:
            self.enabled = False
            self._stop.set()
            if not self._task or self._task.done():
                self.state = "disabled"
            self._publish()

        if self._expiry:
            self._expiry.cancel()
        self._expiry = asyncio.get_running_loop().call_later(delay, expire)

    def snapshot(self) -> dict[str, object]:
        active = bool(self._task and not self._task.done())
        reason = ""
        if self.owner_id is None:
            reason = (
                "credentials_unreadable"
                if self.error == "credentials_unreadable"
                else "account_not_configured"
            )
        elif not self.enabled:
            reason = "validation_pending"
        elif not self.ready:
            reason = "runtime_policy_pending"
        elif self._unsettled or self._cleanup_blocked:
            reason = "cleanup_incomplete"
        elif self.state == "stopped":
            reason = "remote_hangup_unconfirmed"
        return {
            "revision": self._revision,
            "instanceId": self.instance_id,
            "state": self.state,
            "generation": self.generation,
            "canStart": self.ready
            and not active
            and not self._unsettled
            and not self._cleanup_blocked,
            "canRecover": self._cleanup_blocked and not active and not self._unsettled,
            "canDial": bool(
                self.ready
                and not self._unsettled
                and not self._cleanup_blocked
                and (not active or (self._mode == "listen" and self.state == "waiting"))
            ),
            "receivingRetrying": self._retry_timer is not None,
            "listeningArmed": self.listening_armed,
            "canRenewTest": bool(
                self.owner_id
                and not self.enabled
                and self.provider
                and self.dispatch
                and self.input_policy
                and not active
                and not self._unsettled
                and not self._cleanup_blocked
            ),
            "canHangup": active and not self._cleanup_blocked,
            "reason": reason,
            "error": self.error,
            "inputCaption": self.input_caption,
            "outputCaption": self.output_caption,
            "remoteDiscardObserved": bool(
                self._shutdown and self._shutdown.remote_discard_observed
            ),
            "processReaped": bool(self._shutdown and self._shutdown.process_reaped),
        }

    def _publish(self) -> None:
        self._revision += 1
        try:
            self.notify(self.snapshot())
        except Exception:
            # An observer failure must not interrupt a call or local Era audio.
            pass

    def _native_event(self, event: CallWire) -> None:
        if event.generation != self.generation or event.peer_id != self.owner_id:
            return
        if event.kind not in {"pcm_in", "submitted"}:
            record("native_" + event.kind, self.generation)
        if event.kind == "incoming":
            self.state = "incoming"
            self._incoming.set()
        elif event.kind == "connected":
            self._retry_attempt = 0
            self.state = "connected"
            self._connected.set()
        elif event.kind in {"failed", "disabled"}:
            if (
                event.kind == "failed"
                and not self.error
                and (self._remote_ended or self._hangup_requested)
            ):
                return  # Terminal close races are logged, not a new call failure.
            self.state = "failed"
            self.error = self.error or event.detail or "native_failed"
            self._stop.set()
        elif event.kind in {"discarded", "stopped"}:
            if event.kind == "discarded":
                self._remote_ended = True
            self._stop.set()
        else:
            return  # PCM/receipts never enter HUD state or caption logs.
        self._publish()

    def _provider_event(self, event: CallEvent) -> None:
        if event.generation != self.generation:
            return
        if event.kind == "input_complete":
            from app.manual_presence import handle_presence_order

            if self.state == "connected":
                presence_reply = handle_presence_order(event.text)
                if presence_reply is not None:
                    self.output_caption = presence_reply
                    self._publish()
            if self.state == "connected" and is_call_goodbye(event.text):
                record("voice_hangup_requested", self.generation)
                self.hangup(event.generation)
            return
        if event.kind in {"input_caption", "output_caption"}:
            if self._new_caption:
                self.input_caption = self.output_caption = ""
                self._new_caption = False
            if event.kind == "input_caption":
                self.input_caption = (self.input_caption + event.text)[-2000:]
            else:
                self.output_caption = (self.output_caption + event.text)[-2000:]
        elif event.kind == "turn_complete":
            self._new_caption = True
        elif event.kind in {
            "provider_failed",
            "provider_close_failed",
            "worker_cleanup_incomplete",
        }:
            if event.kind == "provider_failed" and (self._remote_ended or self._hangup_requested):
                return
            self.state, self.error = "failed", event.kind
            self._stop.set()
        elif event.kind == "interrupted":
            self.output_caption = ""
        else:
            return
        self._publish()

    def begin(
        self,
        mode: str,
        *,
        notification: str = "",
        permitted: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Queue an explicit dial or owner-only incoming listener; not 'connected'."""
        if mode not in {"dial", "listen"}:
            raise ValueError("Unknown call action.")
        if not self.ready or self.owner_id is None:
            raise PermissionError("Calling is disabled until integration and live checks pass.")
        if (self._task and not self._task.done()) or self._unsettled or self._cleanup_blocked:
            raise RuntimeError("A call or its cleanup is already running.")
        self.audio = CallAudio(self.owner_id)
        self._cancel_receive_retry()
        self._queued_dial = False
        self._queued_notification, self._queued_guard = "", None
        self._notification = notification[:2000] if mode == "dial" else ""
        self._dial_guard = permitted if mode == "dial" else None
        self._mode = mode
        if mode == "listen":
            self.listening_armed = True
        self.generation = self.audio.begin(self.owner_id)
        self._stop, self._incoming, self._connected = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self._shutdown = None
        self._remote_ended = self._hangup_requested = False
        self.error = self.input_caption = self.output_caption = ""
        self._new_caption = True
        self.state = "starting"
        self._task = asyncio.create_task(self._run(mode))
        self._task.add_done_callback(self._settled)
        self._publish()
        return {"accepted": True, "generation": self.generation}

    def stop_listening(self) -> None:
        """Stop future incoming answers; retain an already connected conversation."""
        self.listening_armed = False
        self._cancel_receive_retry()
        if self._mode == "listen" and self.state in {
            "starting",
            "waiting",
            "incoming",
            "connecting",
        }:
            self.hangup(self.generation)
        self._publish()

    def _rearm_listener(self) -> None:
        if (
            self.listening_armed
            and self.state in {"ended", "stopped", "idle"}
            and self.snapshot()["canStart"]
        ):
            self.begin("listen")

    def request_call_owner(
        self, *, notification: str = "", permitted: Callable[[], bool] | None = None
    ) -> dict[str, object]:
        """Replace an idle listener with an outgoing call, preserving its opt-in."""
        if permitted is not None and not permitted():
            raise PermissionError("Automatic call presence changed.")
        if self.listening_armed and self.state == "waiting" and self.ready:
            self._queued_dial = True
            self._queued_notification, self._queued_guard = (
                notification[:2000],
                permitted,
            )
            self._hangup_requested = True
            self._stop.set()
            self.state = "ending"
            self._publish()
            return {"accepted": True, "generation": self.generation}
        return self.begin("dial", notification=notification, permitted=permitted)

    def _cancel_receive_retry(self) -> None:
        if self._retry_timer:
            self._retry_timer.cancel()
            self._retry_timer = None

    def _schedule_receive_retry(self) -> None:
        if (
            not self.listening_armed
            or not self.ready
            or self._retry_timer
            or self._unsettled
            or (self._task and not self._task.done())
        ):
            return
        if self.state in {"ended", "stopped", "idle"} and not self._cleanup_blocked:
            asyncio.get_running_loop().call_soon(self._rearm_listener)
            return
        if self.error in {
            "auth_required",
            "identity_invalid",
            "call_privacy",
            "calls_disabled",
            "test_window_expired",
        }:
            return
        delay = min(2.0 * 2 ** min(self._retry_attempt, 5), 60.0)
        if self.error == "provider_rate_limited":
            delay = 60.0
        self._retry_attempt += 1
        self._retry_timer = asyncio.get_running_loop().call_later(delay, self._retry_receiving)
        self._publish()

    def _retry_receiving(self) -> None:
        self._retry_timer = None
        if not self.listening_armed or not self.ready or self._unsettled:
            return
        if self._cleanup_blocked:
            self.recover(self.generation)
        elif self.snapshot()["canStart"]:
            self.begin("listen")

    async def _dispatch_call_command(self, command: CallCommand) -> dict[str, object]:
        if command.name != "end_telegram_call":
            assert self.dispatch is not None
            return cast(dict[str, object], await self.dispatch(command))
        if (
            not is_call_goodbye(command.input.text)
            or command.arguments
            or command.generation != self.generation
            or command.peer_id != self.owner_id
            or type(command.peer_id) is not int
            or command.input.source != "telegram"
        ):
            return {
                "success": False,
                "result": "No clear request to end this current call was confirmed; keep talking normally.",
            }
        record("voice_hangup_requested", self.generation)
        accepted = self.hangup(command.generation)
        return {
            "success": accepted,
            "result": "Hangup requested." if accepted else "No active call.",
        }

    def recover(self, generation: str) -> bool:
        """Retry cleanup of the exact owned transport without restarting Era."""
        if (
            generation != self.generation
            or not self._cleanup_blocked
            or self._transport is None
            or self._unsettled
            or (self._task and not self._task.done())
        ):
            return False
        self.state = "recovering"
        self._task = asyncio.create_task(self._retry_cleanup())
        self._task.add_done_callback(self._settled)
        self._publish()
        return True

    def enable_test_window(self, delay: float) -> None:
        """Runtime calls this only after revalidating and saving protected opt-in."""
        if not self.snapshot()["canRenewTest"]:
            raise PermissionError("Call testing cannot be enabled in this state.")
        if not 0 < delay <= 1800:
            raise ValueError("A bounded attended test window is required.")
        self.enabled = True
        self.state, self.error = "idle", ""
        self.set_test_expiry(delay)
        self._publish()
        if self.listening_armed:
            asyncio.get_running_loop().call_soon(self._rearm_listener)

    async def _retry_cleanup(self) -> None:
        assert self._transport is not None
        try:
            shutdown = await asyncio.wait_for(self._transport.close(), 60)
            self._shutdown = shutdown
            self._cleanup_blocked = not shutdown.process_reaped
            if self._cleanup_blocked:
                self.state, self.error = "failed", "cleanup_incomplete"
            else:
                self.error = ""
                self.state = "ended" if shutdown.remote_discard_observed else "stopped"
                record("cleanup_recovered", self.generation)
        except Exception as exc:
            record("cleanup_retry_failed", self.generation, exc)
            self.state, self.error = "failed", "cleanup_incomplete"
        self._publish()

    def hangup(self, generation: str) -> bool:
        if not generation or generation != self.generation or not self._task or self._task.done():
            return False
        self._hangup_requested = True
        self._queued_dial = False
        if self._mode == "listen" and self.state in {
            "starting",
            "waiting",
            "incoming",
            "connecting",
        }:
            self.listening_armed = False
        self._stop.set()
        if self.state != "failed":
            self.state = "ending"
        self._publish()
        return True

    def _settled(self, task: asyncio.Task[object]) -> None:
        self._unsettled.discard(task)
        if not task.cancelled():
            task.exception()
        self._publish()
        if self._queued_dial and task is self._task:
            self._queued_dial = False
            notice, guard = self._queued_notification, self._queued_guard
            self._queued_notification, self._queued_guard = "", None
            if (
                self.snapshot()["canStart"]
                and self.state != "failed"
                and (guard is None or guard())
            ):
                self.begin("dial", notification=notice, permitted=guard)
                return
        self._schedule_receive_retry()

    async def _run(self, mode: str) -> None:
        assert self.audio is not None
        audio, generation = self.audio, self.generation
        try:
            transport = self.transport_factory(audio, generation, self._native_event)
            self._transport = transport
        except Exception:
            self.state, self.error = "failed", "call_failed"
            audio.end(generation)
            self._publish()
            return

        async def conversation() -> None:
            record("helper_start", generation)
            await transport.start()
            record("helper_ready", generation)
            if mode == "listen":
                self.state = "waiting"
                self._publish()
                await self._incoming.wait()
            self.state = "connecting"
            self._publish()
            assert self.provider is not None and self.input_policy is not None
            async with AsyncExitStack() as stack:
                # Prepare Gemini before answering/dialling; no local EraLive instance.
                session = await asyncio.wait_for(
                    stack.enter_async_context(self.provider()), self.deadline
                )
                record("gemini_connected", generation)
                if mode == "dial" and self._dial_guard is not None and not self._dial_guard():
                    record("automatic_call_presence_changed", generation)
                    self._hangup_requested = True
                    return
                await transport.command("answer" if mode == "listen" else "dial")
                await asyncio.wait_for(self._connected.wait(), self.connect_deadline)
                adapter = GeminiCall(
                    audio,
                    generation,
                    notify=self._provider_event,
                    dispatch=self._dispatch_call_command,
                    input_policy=self.input_policy,
                    io_timeout=self.deadline,
                    notification=self._notification,
                )
                await adapter.run(session, self._stop)

        worker = asyncio.create_task(conversation())
        stop = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait([worker, stop], return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                worker.result()
        except asyncio.CancelledError:
            self._stop.set()
            raise
        except Exception as exc:
            record("conversation_failed", generation, exc)
            if not (self._remote_ended or self._hangup_requested):
                self.state = "failed"
                code = getattr(exc, "code", None)
                reason = (
                    "provider_rate_limited"
                    if code == 429
                    else ("provider_unavailable" if code in {500, 502, 503, 504} else "call_failed")
                )
                self.error = self.error or reason
        finally:
            worker.cancel()
            stop.cancel()
            settled, pending = await asyncio.wait([worker, stop], timeout=self.deadline)
            await asyncio.gather(*settled, return_exceptions=True)
            for task in pending:
                self._unsettled.add(task)
                task.add_done_callback(self._settled)
                self.state = "failed"
                self.error = self.error or "cleanup_incomplete"
            try:
                shutdown: CallShutdown = await asyncio.wait_for(transport.close(), 60)
                self._shutdown = shutdown
                self._cleanup_blocked = not shutdown.process_reaped
                if not shutdown.process_reaped or shutdown.teardown_receipt == "cleanup_incomplete":
                    self.state = "failed"
                    self.error = self.error or "cleanup_incomplete"
            except Exception:
                self._cleanup_blocked = True
                self.state = "failed"
                self.error = self.error or "cleanup_incomplete"
            audio.end(generation)
            if self.state != "failed":
                self.state = (
                    "ended"
                    if self._shutdown and self._shutdown.remote_discard_observed
                    else "stopped"
                )
            # The task is finishing: permit another call only after its actual completion.
            self._publish()

    async def close(self) -> None:
        self.listening_armed = False
        self._queued_dial = False
        self._cancel_receive_retry()
        if self._expiry:
            self._expiry.cancel()
        self._stop.set()
        if self._task and not self._task.done():
            await self._task
        self._publish()
