"""Conservative response attribution for a Live stream without request IDs."""

from dataclasses import dataclass

from core.input_envelope import InputEnvelope, current_input
from core.logger import log_event, main_logger


@dataclass(frozen=True)
class SendTicket:
    sequence: int
    overlapped: bool


class ModelReplyOwnership:
    def __init__(self):
        self.owner: InputEnvelope | None = None
        self.active = False
        self.ambiguous = False
        self.tools_seen = False
        self.sequence = 0
        self.reason = ""
        self.remote_pending = False

    def register(self, owner: InputEnvelope | None) -> SendTicket:
        overlapped = self.active
        self.sequence += 1
        if owner is not None and owner.source == "telegram":
            self.remote_pending = True
        if self.active:
            if self.owner is None or owner is None or self.owner.id != owner.id:
                self.invalidate("overlapping_model_requests")
        else:
            self.owner, self.active = owner, True
            self.ambiguous, self.tools_seen = False, False
            self.reason = "unowned_model_request" if owner is None else ""
        return SendTicket(self.sequence, overlapped)

    def invalidate(self, reason: str) -> None:
        self.owner = None
        self.ambiguous = self.active
        self.reason = reason

    def note_tools(self) -> None:
        self.tools_seen = True

    def complete(self, *, has_text: bool) -> InputEnvelope | None:
        # Keep attribution across an empty tool-call turn, until it answers.
        if self.active and not self.ambiguous and self.tools_seen and not has_text:
            return None
        owner = self.owner if self.active and not self.ambiguous and has_text else None
        self.reset()
        return owner

    def failed(self, ticket: SendTicket) -> None:
        if ticket.sequence == self.sequence and not ticket.overlapped:
            self.reset()
        else:
            self.invalidate("concurrent_send_failure")

    def reset(self) -> None:
        self.owner, self.active = None, False
        self.ambiguous, self.tools_seen = False, False
        self.reason = ""
        self.remote_pending = False


async def send_model_text(era, text: str, *, internal: bool = False) -> None:
    """Capture at SDK send time; retain the existing text and error behavior."""
    from app.codemode_runtime import owns_reply

    if owns_reply():
        return
    tracker = getattr(era, "_model_reply_ownership", None)
    if tracker is None:
        tracker = era._model_reply_ownership = ModelReplyOwnership()
    owner = None if internal else current_input.get()
    # Delayed background work may speak locally but must not inherit a newer chat.
    latest = getattr(era, "_input_envelope", None)
    if owner is not None and (latest is None or latest.id != owner.id):
        owner = None
    ticket = tracker.register(owner)
    if tracker.ambiguous:
        bridge = getattr(era, "_telegram_speech", None)
        if bridge is not None:
            bridge.select(None, reason="model_attribution_uncertain")
    log_event(
        main_logger,
        "model.request.submitting",
        "INFO",
        sequence=ticket.sequence,
        request_id=owner.id if owner else None,
        source=owner.source if owner else "internal",
        ambiguous=tracker.ambiguous,
    )
    try:
        await era.session.send_realtime_input(text=text)
    except BaseException as error:
        tracker.failed(ticket)
        if tracker.ambiguous:
            bridge = getattr(era, "_telegram_speech", None)
            if bridge is not None:
                bridge.select(None, reason="model_attribution_uncertain")
        log_event(
            main_logger,
            "model.request.failed",
            "WARNING",
            sequence=ticket.sequence,
            error_type=type(error).__name__,
        )
        raise
    log_event(
        main_logger,
        "model.request.sent",
        "INFO",
        sequence=ticket.sequence,
        request_id=owner.id if owner else None,
    )
    health = getattr(era, "live_health", None)
    if owner is not None and owner.source in ("typed", "telegram") and health is not None:
        health.note_text()


def active_model_owner(era) -> InputEnvelope | None:
    tracker = getattr(era, "_model_reply_ownership", None)
    latest = getattr(era, "_input_envelope", None)
    if tracker is None or not tracker.active or tracker.ambiguous or tracker.owner is None:
        return None
    owner = tracker.owner
    return owner if latest is not None and latest.id == owner.id else None


def completed_model_owner(era, text: str) -> InputEnvelope | None:
    tracker = getattr(era, "_model_reply_ownership", None)
    if tracker is None:
        return None
    reason = tracker.reason
    remote_pending = tracker.remote_pending
    owner = tracker.complete(has_text=bool(text))
    latest = getattr(era, "_input_envelope", None)
    if owner is not None and (latest is None or latest.id != owner.id):
        reason, owner = "superseded_input", None
    if text and reason:
        log_event(main_logger, "model.reply.unattributed", "WARNING", reason=reason)
        if remote_pending and latest is not None and latest.source == "telegram":
            report_model_uncertainty(era, reason)
    return owner


def report_model_uncertainty(era, reason: str) -> None:
    report = getattr(era, "_report_unattributed_model_reply", None)
    if callable(report):
        try:
            report(reason)
        except Exception as error:
            # Notification/UI failure must not tear down the audio connection.
            log_event(
                main_logger,
                "model.reply.notice_failed",
                "WARNING",
                reason=reason,
                error_type=type(error).__name__,
            )
