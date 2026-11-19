"""One deterministic input/speech boundary for CodeMode across Era channels."""

import os
import re

from app.hud.events import hud
from core.codemode import CANCEL, ENTRY, EXIT, get_codemode
from core.codemode_intent import coding_intent, model_order
from core.input_envelope import InputEnvelope


def owns_reply():
    return get_codemode().enabled


async def emit(era, kind, text, owner):
    latest = getattr(era, "_input_envelope", owner)
    if latest is not None and latest.id != owner.id:
        if kind in ("end", "notice", "error"):
            era.ui.write_log("SYS [previous CodeMode turn]: " + text)
        return
    from app.codemode_speech import local_mouth

    speech = getattr(era, "speech_out", None)
    local = local_mouth(era, fresh=kind == "start")
    manager = get_codemode()
    view = manager.snapshot()
    hud._emit(
        "codemode." + kind,
        buffer=kind not in ("text", "text_silent"),
        message=text if kind not in ("text", "text_silent") else "",
        metadata={
            "session_id": view["session_id"],
            "work_id": view["work_id"],
            "revision": view["revision"],
            "sequence": view["sequence"],
            "input_id": owner.id,
            "source": owner.source,
        },
    )
    if kind == "start":
        tracker = getattr(era, "_model_reply_ownership", None)
        if tracker is not None:
            tracker.reset()
        if speech is not None:
            speech.interrupt()
        prepare = getattr(era, "_prepare_owned_speech", None)
        if owner.source == "telegram" and callable(prepare):
            prepare(owner)
    elif kind in ("text", "text_silent"):
        hud.response_delta(text, verbatim=True)
        if local is not None and kind == "text":
            local.feed(text)
        elif speech is not None and kind == "text":
            speech.feed(text, owner=owner, external=True)
    elif kind == "end":
        hud.response_completed(text)
        if local is not None:
            local.feed("", final=True)
        elif speech is not None:
            speech.end_turn()
        era.ui.write_log("ERA ONE: [Claude / CodeMode] " + text)
        # This is a reply to the user's authenticated channel, not a new recipient.
        if owner.source == "telegram":
            # Existing progressive speech mirroring owns delivery when available.
            bridge = getattr(era, "_telegram_speech", None)
            if bridge is None or local is not None:
                era._tg_ack(text)
    elif kind in ("notice", "error"):
        if kind == "error":
            hud.response_completed(text)
        era.ui.write_log("ERA ONE: [CodeMode] " + text)
        if local is not None and not manager._speech_muted:
            local.feed(text, final=True)
        elif speech is not None and not manager._speech_muted:
            speech.say(text, owner=None if owner.source == "telegram" else owner, external=True)
        if owner.source == "telegram":
            era._tg_ack(text)


def codemode_allowed() -> bool:
    """PF-050: CodeMode is OFF unless ERA_CODEMODE=on (owner, 2026-11-19: "disable codemode").

    It runs Claude Code, whose subscription login is gone (PF-043), and its implicit
    activation took coding requests meant for the crew. Code work goes to the crew.
    """
    return os.environ.get("ERA_CODEMODE", "off").strip().lower() in {"on", "1", "true", "yes"}


def route(era, text, owner=None):
    if not codemode_allowed():
        return False  # nothing enters CodeMode: not "start coding", implicit, or the tool
    manager = get_codemode()
    entry, exit_mode = ENTRY.fullmatch(text.strip()), EXIT.fullmatch(text.strip())
    selection = model_order(text)
    implicit = not manager.enabled and not entry and coding_intent(text)
    decision = bool(
        re.fullmatch(r"(?:approve|reject) coding change [a-f0-9]{16}[.! ]*", text, re.I)
    )
    if not (manager.enabled or entry or exit_mode or decision or implicit or selection):
        return False
    if text.startswith("__"):
        return False  # microphone/wake/standby controls retain their established path
    from core.mic_mute import is_mic_off_order
    from core.standby_policy import auto_standby_order

    if (
        is_mic_off_order(text)
        or auto_standby_order(text) is not None
        or text.strip().lower() == "abort mission"
        or getattr(era, "_is_reboot_order", lambda value: False)(text)
    ):
        return False
    owner = owner or getattr(era, "_input_envelope", None) or InputEnvelope.create(text, "voice")
    era._input_envelope = owner
    loop = getattr(era, "_loop", None)
    if loop is None or not loop.is_running():
        era.ui.write_log("SYS: CodeMode needs Era's running event loop.")
        return True
    if entry or exit_mode:
        if owner.id in manager._controls_seen:
            return True
        if len(manager._controls_seen) >= 128:
            manager._controls_seen.clear()
        manager._controls_seen.add(owner.id)

    def interrupt():
        tracker = getattr(era, "_model_reply_ownership", None)
        if tracker is not None:
            tracker.reset()
        mouth = getattr(era, "_codemode_local_mouth", None)
        if mouth is not None:
            mouth.interrupt()
        speech = getattr(era, "speech_out", None)
        if speech is not None:
            speech.interrupt()
        flush = getattr(era, "_flush_audio_in_queue", None)
        if callable(flush):
            flush()

    async def handle():
        try:
            if selection:
                await emit(era, "notice", manager.select_model(selection), owner)
            elif entry:
                interrupt()
                message = manager.enter()
                await emit(era, "notice", message, owner)
            elif exit_mode:
                interrupt()
                manager.cancel(exit_mode=True)
                await emit(
                    era, "notice", "CodeMode closed. Normal conversation is available.", owner
                )
            elif CANCEL.fullmatch(text.strip()) or text.strip().lower() in (
                "cancel that",
                "cancel",
                "stop the task",
            ):
                interrupt()
                manager.cancel()
                await emit(
                    era,
                    "notice",
                    "Coding task cancelled. No unapproved change will be applied.",
                    owner,
                )
            elif text.strip().lower() in ("stop speaking", "be quiet", "pause speech"):
                interrupt()
                manager._speech_muted = True
            elif text.strip().lower() in ("enter standby", "go to sleep"):
                interrupt()
                manager._speech_muted = True
                era.enter_standby()
            elif text.strip().lower() == "wake up":
                era._exit_standby()
            else:

                def callback(kind, value, envelope):
                    return emit(era, kind, value, envelope)

                if not await manager.decide(text.strip(), owner, callback):
                    if manager.start(owner, callback):
                        hud.request_sent("codemode")
                        interrupt()
        except Exception as exc:
            from core.logger import redact_secrets

            await emit(era, "error", str(redact_secrets(str(exc)))[:400], owner)

    # The mode claim must precede the next model audio/tool event. Both typed
    # adapters and transcript callbacks invoke this on Era's event loop.
    if entry or implicit:
        try:
            message = manager.enter()
        except Exception as exc:
            era.ui.write_log("SYS: CodeMode unavailable: " + type(exc).__name__)
            return True
        interrupt()
        if implicit:
            loop.create_task(handle())
        else:
            loop.create_task(emit(era, "notice", message, owner))
    else:
        loop.create_task(handle())
    return True
