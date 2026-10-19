"""Bound Spotify UI Automation in an owned process, so a hung COM call can stop.

Both UIA lanes run here: `search_ui` (type a query, click the best result) and
`primary_ui` (press a page's own "Play <context>" control — Liked Songs, New
Releases, Made For You). 2026-10-16: the primary lane still ran in-process, and a
hung Invoke() kept a Liked Songs call "running" for 96 s and 170 s until the user
cancelled it; nothing could interrupt the COM call. Out here the parent kills it.

Both lanes restore the window FIRST. Era minimizes Spotify after every action (a
standing user rule), and Chromium does not build an accessibility tree for a
minimized window — so a search that read the window before restoring it spent its
whole budget looking at nothing and timed out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from core.cancel_registry import raise_if_cancelled
from core.win_subprocess import NO_WINDOW


def _run_worker(payload: dict, timeout_s: float) -> tuple[bool, str]:
    data = json.dumps(payload)
    process = subprocess.Popen(
        [sys.executable, "-m", "actions.spotify_search_worker"],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=NO_WINDOW,
    )
    deadline = time.monotonic() + timeout_s
    first = True
    try:
        while True:
            raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "Spotify's window timed out; its UI worker was stopped"
            try:
                output, _ = process.communicate(
                    data if first else None, timeout=min(0.1, remaining)
                )
                for line in reversed(output.splitlines()):
                    if line.startswith("ERA_SPOTIFY_RESULT:"):
                        result = json.loads(line.split(":", 1)[1])
                        return bool(result["ok"]), str(result["detail"])
                return False, "Spotify UI worker failed"
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def search_ui(
    query: str, *, cold: bool, timeout_s: float = 10.0, retry: bool = False
) -> tuple[bool, str]:
    return _run_worker({"query": query, "cold": cold, "retry": retry}, timeout_s)


def open_track_uri(uri: str, *, timeout_s: float = 1.5) -> tuple[bool, str]:
    """Bound the Windows protocol launch without importing the UIA stack."""
    from core.spotify_api import identity

    canonical = identity(uri)
    if not canonical.startswith("spotify:track:"):
        return False, "Desktop protocol playback requires an exact track"
    return _run_worker({"mode": "uri", "uri": canonical}, timeout_s)


def primary_ui(
    label: str, *, cold: bool, timeout_s: float = 12.0, open_row: str | None = None
) -> tuple[bool, str]:
    """Press the page's own "Play <label>" control (only that one).

    `open_row` first opens a sidebar library entry by name (Release Radar)."""
    tree_s = max(2.0, timeout_s - 4.0)  # leave room for the worker's own start-up
    payload = {"mode": "primary", "label": label, "cold": cold, "tree_timeout": tree_s}
    if open_row:
        payload["open_row"] = open_row
    return _run_worker(payload, timeout_s)


def navigate_search(hwnd: int, query: str) -> bool:
    from actions.spotify_control import _open_spotify_uri
    from actions.spotify_ui import set_search_query

    if set_search_query(hwnd, query, timeout_s=1.0):
        return True
    return _open_spotify_uri("spotify:search:" + urllib.parse.quote(query))


def _page_fingerprint(hwnd: int):
    """The current page's Play-control names, or None (never raises)."""
    try:
        from pywinauto import Desktop

        from actions.spotify_ui import _buttons, play_names

        return play_names(_buttons(Desktop(backend="uia").window(handle=hwnd))) or None
    except Exception:
        return None


def main() -> None:
    args = json.loads(sys.stdin.read())
    if args.get("mode") == "uri":
        from core.spotify_api import identity

        try:
            uri = identity(args.get("uri"))
            if not uri.startswith("spotify:track:"):
                raise ValueError("Desktop protocol playback requires a track")
            os.startfile(uri)
            result = (True, "Requested Spotify track")
        except (OSError, ValueError) as exc:
            result = (False, f"Spotify protocol launch failed ({type(exc).__name__})")
        print(
            "ERA_SPOTIFY_RESULT:" + json.dumps({"ok": result[0], "detail": result[1]}), flush=True
        )
        return

    from actions import spotify_control as control
    from actions.spotify_ui import invoke_primary_play, invoke_search_result, open_library_row

    hwnd, _ = (
        control._wait_for_spotify_ui_ready(True)
        if args["cold"]
        else control._get_spotify_hwnd_and_title()
    )
    if not hwnd:
        result = (False, "Spotify window verification failed")
    else:
        control._focus_spotify_window(hwnd)  # restore before reading: see module doc
        if args.get("mode") == "primary":
            if args.get("open_row") and not open_library_row(hwnd, args["open_row"]):
                result = (False, f"'{args['open_row']}' is not in the Spotify library sidebar")
            else:
                result = invoke_primary_play(
                    hwnd,
                    timeout_s=float(args.get("tree_timeout") or 8.0),
                    prefer=args.get("label"),
                )
        else:
            stale = None
            if not args["retry"]:
                stale = _page_fingerprint(hwnd)  # so leftovers are never matched
            if not args["retry"] and not navigate_search(hwnd, args["query"]):
                result = (False, "Spotify search navigation failed")
            else:
                # Real clicks avoid Chromium's successful-but-no-op Invoke pattern.
                control._focus_spotify_window(hwnd)
                result = invoke_search_result(
                    hwnd, args["query"], timeout_s=7.0, prefer_click=True, stale_names=stale
                )
    print("ERA_SPOTIFY_RESULT:" + json.dumps({"ok": result[0], "detail": result[1]}), flush=True)


if __name__ == "__main__":
    main()
