from __future__ import annotations

import asyncio
import os
import time

from core.loop_rate_limit import Debounce
from core.state_store import MediaSessionInfo, StateStore

if os.name != "nt":

    async def run_media_exclusivity_loop(state: StateStore) -> None:
        while True:
            await asyncio.sleep(10)


async def run_media_exclusivity_loop(state: StateStore) -> None:
    """Enforce exclusive media playback - only one source plays at a time."""
    playing_change_debounce = Debounce(interval_s=2.0)

    # Try to import winsdk for GSMTC, fallback to polling mode
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
        )
    except Exception:
        # No GSMTC available - run idle loop
        while True:
            await asyncio.sleep(10)

    last_playing_session: str | None = None
    sm = None
    sessions: dict[str, any] = {}

    async def pause_session(session_id: str) -> None:
        try:
            s = sessions.get(session_id)
            if not s:
                return
            playback = await s.try_get_media_playback()
            if playback:
                await playback.pause_async()
        except Exception:
            pass

    async def refresh_sessions() -> None:
        nonlocal sm, sessions
        try:
            if sm is None:
                sm = await SessionManager.request_async()
            mgr_sessions = sm.get_sessions()
            new_sessions: dict[str, any] = {}
            for s in mgr_sessions:
                try:
                    sid = str(s.id)
                except Exception:
                    sid = str(id(s))
                new_sessions[sid] = s
            sessions = new_sessions
        except Exception:
            pass

    async def handle_sessions() -> None:
        nonlocal last_playing_session

        while True:
            await refresh_sessions()
            playing_ids = []
            try:
                for sid, s in sessions.items():
                    try:
                        info = s.get_playback_info()
                        status = info.playback_status
                        if status == PlaybackStatus.PLAYING:
                            playing_ids.append(sid)
                    except Exception:
                        continue
            except Exception:
                playing_ids = []

            if playing_ids:
                canonical = playing_ids[0]
                if playing_change_debounce.allow():
                    if last_playing_session != canonical:
                        tasks = []
                        for sid in list(sessions.keys()):
                            if sid != canonical:
                                tasks.append(asyncio.create_task(pause_session(sid)))
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
                        last_playing_session = canonical

            # Update state store + reconcile central MEDIA_STATE cache
            reconcile_items: list[dict] = []
            try:
                for sid, s in sessions.items():
                    try:
                        info = s.get_playback_info()
                        status = info.playback_status
                        disp = ""
                        try:
                            disp = s.source_app_user_model_id or ""
                        except Exception:
                            disp = ""
                        mi = MediaSessionInfo(
                            session_id=sid,
                            display_name=disp,
                            player_type=getattr(s, "source_app_display_info", ""),
                            is_playing=(status == PlaybackStatus.PLAYING),
                            is_paused=(status == PlaybackStatus.PAUSED),
                            last_change_ts=time.time(),
                        )
                        await state.update_media_session(mi)

                        # Map OS session display name to our source key
                        disp_lower = disp.lower()
                        source = None
                        if "spotify" in disp_lower:
                            source = "spotify"
                        elif "youtube" in disp_lower:
                            source = "youtube"
                        elif "netflix" in disp_lower:
                            source = "netflix"
                        if source:
                            if status == PlaybackStatus.PLAYING:
                                reconcile_items.append({"source": source, "status": "playing"})
                            elif status == PlaybackStatus.PAUSED:
                                reconcile_items.append({"source": source, "status": "paused"})
                    except Exception:
                        continue
            except Exception:
                pass

            # Push reconciled OS state into the synchronous MEDIA_STATE cache.
            if reconcile_items:
                try:
                    from core.media_state import reconcile_from_os_sessions

                    reconcile_from_os_sessions(reconcile_items)
                except Exception:
                    pass

            await asyncio.sleep(0.18)

    await handle_sessions()
