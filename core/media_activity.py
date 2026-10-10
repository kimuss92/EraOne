"""Is another app making sound right now (music, a video, a game)?

Karim, 2026-10-10: with Spotify on, Era answered song lyrics ("es", "¿Qué")
with "Sorry, I did not catch that." The attention window (core/attention.py)
let everything through for 45 s after her last reply, and each reply to a lyric
re-opened it, so music kept her answering. While media plays she now needs her
name; this module is how she knows media is playing.

Source: Windows' per-app audio-session peak meters (pycaw), the same live signal
the volume mixer shows. Generic (Spotify, YouTube, Netflix, games), fresh (no
stale "playing" flag), and Era's own voice is excluded by process id. A daemon
thread samples every 0.5 s and holds "playing" for 2 s, so a quiet bar between
songs does not flicker the answer.

FAIL-OPEN (reports False) on any error and under pytest: this only tightens the
attention rule, so not knowing must cost nothing.
"""

from __future__ import annotations

import os
import sys
import threading
import time

PEAK_THRESHOLD = 0.02  # Spotify measured 0.56-0.63 playing, 0.000 when silent
HOLD_S = 2.0
_POLL_S = 0.5

_state = {"last_heard": 0.0, "started": False}
_lock = threading.Lock()


def _other_app_peak() -> float:
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

    me = os.getpid()
    loudest = 0.0
    for session in AudioUtilities.GetAllSessions():
        proc = session.Process
        if proc is None or proc.pid == me:
            continue  # system sounds, and Era's own voice
        try:
            meter = session._ctl.QueryInterface(IAudioMeterInformation)
            loudest = max(loudest, float(meter.GetPeakValue()))
        except Exception:
            continue
    return loudest


def _poll() -> None:
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass
    while True:
        try:
            if _other_app_peak() >= PEAK_THRESHOLD:
                _state["last_heard"] = time.monotonic()
        except Exception:
            pass
        time.sleep(_POLL_S)


def media_playing() -> bool:
    """True while another app has made sound within the last HOLD_S seconds."""
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if not _state["started"]:
        with _lock:
            if not _state["started"]:
                _state["started"] = True
                threading.Thread(target=_poll, name="media-activity", daemon=True).start()
    return time.monotonic() - _state["last_heard"] <= HOLD_S
