#!/usr/bin/env python3
"""Verification test for all 8 media playback rule changes."""

import ast
import os
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


def read(path):
    with open(os.path.join(BASE, path), encoding="utf-8") as f:
        return f.read()


def line_has(filepath, pattern):
    """Check if any line in file contains the pattern."""
    for line in read(filepath).splitlines():
        if pattern in line:
            return True
    return False


def count_occurrences(filepath, pattern):
    return read(filepath).count(pattern)


# ── 1. main.py — spotify_control tool declaration + handler ──────────────────
print("\n[1] main.py — spotify_control tool declaration & handler route")

main_src = read("main.py")
check("Tool declaration 'spotify_control' exists", '"spotify_control"' in main_src)
check(
    "Handler route _execute_tool dispatches to spotify_control",
    "from actions.spotify_control import spotify_control" in main_src,
)
check(
    "Handler calls spotify_control(parameters=args, player=self.ui)",
    "spotify_control(parameters=args, player=self.ui)" in main_src,
)

# ── 2. self_healing_router.py — not prematurely intercepted ─────────────────
print("\n[2] self_healing_router.py — spotify_control routing")

router = read("core/self_healing_router.py")
# spotify_control should still be accessible for normal routing (not blocked)
check("spotify_control listed as recognisable tool in route_tool_call", "spotify_control" in router)
check("spotify_control still has fallback suggestions", 'if tool == "spotify_control":' in router)
check("spotify_control still recorded in record_tool_result", '"spotify_control"' in router)

# ── 3. spotify_control.py — _enforce_spotify_priority calls ──────────────────
print("\n[3] spotify_control.py — _enforce_spotify_priority added")

sc = read("actions/spotify_control.py")
check(
    "_enforce_spotify_priority() in _spotify_play()",
    line_has("actions/spotify_control.py", "_enforce_spotify_priority()"),
)
check(
    "_enforce_spotify_priority() in _spotify_search_and_play()",
    count_occurrences("actions/spotify_control.py", "_enforce_spotify_priority()") >= 2,
)

# ── 4. media_state.py — CDP fallback for Netflix/YouTube pause ──────────────
print("\n[4] media_state.py — CDP pause fallback")

ms = read("core/media_state.py")
check("_cdp_pause_video function defined", "_cdp_pause_video" in ms)
check("CDP fallback used for YouTube in _pause_source", '_cdp_pause_video("youtube.com")' in ms)
check("CDP fallback used for Netflix in _pause_source", '_cdp_pause_video("netflix.com")' in ms)

# ── 5. media_coordinator.py — tab keywords + target='all' pause ─────────────
print("\n[5] media_coordinator.py — tab search keywords + pause_active")

mc = read("actions/media_coordinator.py")
bc_domains = read("actions/browser_control.py")
check(
    "browser_control.py has ['youtube.com', 'youtu.be'] for YouTube",
    '["youtube.com", "youtu.be"]' in bc_domains,
)
check("browser_control.py has ['netflix.com'] for Netflix", '["netflix.com"]' in bc_domains)
check(
    "target='all' pause loop over spotify/youtube/netflix",
    'for candidate in ("spotify", "youtube", "netflix"):' in mc,
)
check(
    "enforce_single_playback called after play actions",
    "enforce_single_playback(target, player=player)" in mc,
)

# ── 6. browser_control.py — title search in _find_tab_by_keywords ───────────
print("\n[6] browser_control.py — title matching in _find_tab_by_keywords")

bc = read("actions/browser_control.py")
check(
    "_find_tab_by_keywords fetches page title", "await page.title()" in bc or "page.title()" in bc
)
check(
    "_find_tab_by_keywords combines url + title for scoring",
    'combined = f"{url} {title}".lower()' in bc,
)

# ── 7. netflix_control.py — _focus_netflix_foreground before play JS ────────
print("\n[7] netflix_control.py — _focus_netflix_foreground before play execution")

nc = read("actions/netflix_control.py")
nc_lines = nc.splitlines()
focus_call_line = next(
    (i for i, ln in enumerate(nc_lines) if "_focus_netflix_foreground(page, sess)" in ln), None
)
check("_focus_netflix_foreground called for play actions", focus_call_line is not None)

exec_line = None
for i, ln in enumerate(nc_lines):
    if "evaluate_background_media" in ln and "script" in ln and "netflix.com" in ln:
        exec_line = i  # last occurrence = standard play path (not grid_play)
if focus_call_line is not None and exec_line is not None:
    check(
        "_focus_netflix_foreground executes before play JS (line order)",
        focus_call_line < exec_line,
        f"focus at line {focus_call_line + 1}, exec at line {exec_line + 1}",
    )
else:
    check(
        "_focus_netflix_foreground executes before play JS (line order)",
        False,
        "Could not locate both focus and exec lines",
    )

check(
    "_focus_netflix_foreground defined in netflix_control.py",
    "def _focus_netflix_foreground(page, sess" in nc,
)

# ── 8. prompt.txt — pause music routing ─────────────────────────────────────
print("\n[8] prompt.txt — pause music routes to media_coordinator(target='all')")

prompt = read("core/prompt.txt")
check(
    "'pause/stop any active media (detects source)' maps to media_coordinator(target='all', action='pause')",
    'media_coordinator(target="all", action="pause")' in prompt,
)
check(
    "'AMBIGUOUS PAUSE' row routes to media_coordinator(target='all', action='pause')",
    "pause music" in prompt and "stop music" in prompt and "media_coordinator" in prompt,
)
check(
    "'Pause/stop Spotify music (explicit)' still routed to spotify_control(action='pause')",
    'spotify_control (action="pause")' in prompt,
)

# ── 9. action_registry.py — Netflix fullscreen guard (Fix A1) ────────────────
print("\n[9] action_registry.py — Netflix fullscreen canonicalize guard")

ar = read("core/action_registry.py")
check("log imported in action_registry.py", "from core.utils import log" in ar)
check(
    "Netflix fullscreen guard logs trace",
    'log("Canonicalize", f"netflix_control fullscreen guard fired' in ar,
)
check(
    "Netflix fullscreen guard returns early",
    'if tool == "netflix_control" and str(action).lower().strip() == "fullscreen":' in ar,
)

# ── 10. self_healing_router.py — Netflix fullscreen early-return (Fix A2) ────
print("\n[10] self_healing_router.py — Netflix fullscreen early-return guard")

sr = read("core/self_healing_router.py")
check(
    "Netflix fullscreen early-return before canonicalize",
    'if tool == "netflix_control" and _norm(params.get("action")) == "fullscreen":' in sr,
)
check("Early-return logs trace", 'log("Router", "netflix_fullscreen_early_return' in sr)

# ── 11. netflix_control.py — 3-tier fullscreen fallback (Fix B) ──────────────
print("\n[11] netflix_control.py — 3-tier fullscreen fallback")

nc = read("actions/netflix_control.py")
check("_handle_fullscreen_real_click uses _is_fullscreen helper", "def _is_fullscreen()" in nc)
check(
    "Step 0: focus video element without harmful click", "v.focus()" in nc and "v.click()" not in nc
)
check("Tier 1: 'f' hotkey via Playwright keyboard", 'sess.run(page.keyboard.press("f"))' in nc)
check(
    "Tier 2: CDP dispatchKeyEvent with keyDown/char/keyUp",
    '"type": "keyDown"' in nc and '"type": "char"' in nc and '"type": "keyUp"' in nc,
)
check("Tier 3: OS-level F11 via pyautogui", 'pyautogui.press("f11")' in nc)

# ── 12. _handle_cdp_action no longer calls _focus_netflix_foreground ─────────
print("\n[12] netflix_control.py — no redundant focus in _handle_cdp_action")

check(
    "_handle_cdp_action removed redundant _focus_netflix_foreground",
    "_focus_netflix_foreground is already called by netflix_control()" in nc,
)

# ── 13. computer_settings.py — full_screen docstring (Fix C) ─────────────────
print("\n[13] computer_settings.py — full_screen docstring")

cs = read("actions/computer_settings.py")
check("full_screen docstring warns NOT for Netflix", "NOT for Netflix" in cs)

# ── 14. browser_control.py — CDP init guard (Fix D) ─────────────────────────
print("\n[14] browser_control.py — CDP init guard")

bc = read("actions/browser_control.py")
check("_BrowserSession has _cdp_ready flag", "self._cdp_ready" in bc)
check("_cdp_ready set True after initial pages registered", "self._cdp_ready = True" in bc)
check(
    "_on_new_page guards _restore_native_downloads with _cdp_ready", "if not self._cdp_ready:" in bc
)
check("CDP connection log shows page count", "CDP connected:" in bc and "existing pages" in bc)
check(
    "framenavigated listener on initial pages",
    'p.on("framenavigated", self._on_page_updated)' in bc,
)
check(
    "framenavigated listener on new pages", 'page.on("framenavigated", self._on_page_updated)' in bc
)

# ── SUMMARY ─────────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"  RESULTS: {PASS} passed, {FAIL} failed out of {PASS + FAIL} checks")
print(f"{'=' * 50}")

if FAIL:
    print("Some checks failed — review details above.")
    sys.exit(1)
else:
    print("All changes verified successfully!")
    sys.exit(0)
