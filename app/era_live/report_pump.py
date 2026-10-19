"""Speak the reports Era owes the owner (core/report_outbox) when she can.

A finished or failed background job is written to the outbox on disk the moment
it ends. This pump delivers it as soon as there is a live voice and a quiet
moment -- immediately when the job ends, or on the first connection after a
drop, a standby or a relaunch. A report counts as delivered only after it was
spoken, and goes back in the queue if the session dropped right after.
"""

from __future__ import annotations

import asyncio
import time

from core import report_outbox

POLL_S = 4.0  # how often the queue is checked while nothing wakes the pump
SETTLE_S = 10.0  # gap between two reports, so each is heard on its own
CONFIRM_S = 20.0  # a drop inside this window means the report was probably cut off


def _ready(era) -> bool:
    """A live voice, nothing being said, no tool mid-flight, session not retiring."""
    from core.voice_mode import VOICE_CFG

    loop = getattr(era, "_loop", None)
    if loop is None or not loop.is_running():
        return False
    go_away = getattr(era, "_go_away_event", None)
    if go_away is not None and go_away.is_set():
        return False
    if getattr(era, "session", None) is None and not VOICE_CFG.uses_speech_out():
        return False
    if getattr(era, "_is_speaking", False):
        return False
    turn = getattr(era, "live_turn", None)
    if turn is not None:
        try:
            return bool(turn.is_idle_for_renewal(tool_running=era._tool_is_running()))
        except Exception:  # noqa: BLE001 - idle test failing must not block a report forever
            return True
    return True


def _confirm_or_requeue(era) -> None:
    inflight = getattr(era, "_report_inflight", None)
    if not inflight:
        return
    item_id, generation, at = inflight
    if getattr(era, "_conn_generation", 0) != generation:
        report_outbox.mark_undelivered(item_id)  # the connection dropped on it
        era._report_inflight = None
    elif time.monotonic() - at > CONFIRM_S:
        era._report_inflight = None


def _deliver_one(era) -> None:
    from app.text_intents import _say_exactly

    _confirm_or_requeue(era)
    if getattr(era, "_report_inflight", None):
        return
    items = report_outbox.pending()
    if not items or getattr(era, "_shutdown_requested", False):
        return
    if getattr(era, "_standby_active", False):
        # Standby keeps the session open but mute; wake Era so the report is heard.
        wake = getattr(era, "_request_completion_wake", None)
        if wake is not None:
            wake()
        return
    if not _ready(era):
        return
    if time.monotonic() - getattr(era, "_report_last_at", 0.0) < SETTLE_S:
        return
    item = items[0]
    text = str(item["text"]).replace('"', "'")
    if text.startswith("Sir, "):  # the lead-in below already says "sir"
        text = text[5:6].upper() + text[6:]
    late = time.time() - float(item.get("created") or 0) > 120
    spoken = ("While you were away, sir. " if late else "Report for you, sir. ") + text
    try:
        era.ui.write_log(f"SYS: Report: {text}")  # stays visible even if the audio is missed
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.away_calls import automatic_report

        automatic_report(era, text, "background")
    except Exception:  # noqa: BLE001 - the away call is a bonus channel
        pass
    era.speak(_say_exactly(spoken))
    report_outbox.mark_delivered(item["id"])
    era._report_last_at = time.monotonic()
    era._report_inflight = (item["id"], getattr(era, "_conn_generation", 0), time.monotonic())


async def _pump(era, wake: asyncio.Event) -> None:
    from core import development

    # Jobs that ended while Era was down (or before reports existed) are queued
    # once here; the keys make a second run a no-op.
    await asyncio.to_thread(development.reconcile_reports)
    while not getattr(era, "_shutdown_requested", False):
        try:
            await asyncio.wait_for(wake.wait(), POLL_S)
        except TimeoutError:
            pass
        wake.clear()
        try:
            _deliver_one(era)
        except Exception as exc:  # noqa: BLE001 - the pump must outlive any one failure
            print(f"[REPORT] delivery skipped: {type(exc).__name__}: {exc}")


def start_report_pump(era) -> None:
    """Start the one pump per process. Safe to call on every (re)connect."""
    task = getattr(era, "_report_pump_task", None)
    if task is not None and not task.done():
        return
    from core import development

    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    era._report_wake = wake
    development.set_notifier(lambda: loop.call_soon_threadsafe(wake.set))
    era._report_pump_task = era._spawn_background_task(_pump(era, wake), "report-pump")
