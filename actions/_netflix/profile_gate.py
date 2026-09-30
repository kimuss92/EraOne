"""Get past Netflix's profile picker onto the configured profile, reliably.

The old inline version failed for a mundane reason: it probed for the picker
exactly once, immediately after navigation. Netflix renders the gate
asynchronously, so the probe usually ran before the picker existed, found
nothing, and returned — leaving the user to click it themselves. It also never
checked whether its click had worked.

This module fixes both: it waits for the gate to appear, clicks the named
profile, then waits for the gate to go away and retries once if it hasn't.

Resolution independence is structural, not tuned. Nothing here uses a
coordinate, a pixel offset or a window measurement — the profile is found by
name in the DOM and clicked through it — so display scaling, window size, zoom
and multi-monitor layouts cannot affect it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from actions._netflix.player_js import (
    DETECT_GATE_JS,
    build_profile_click_js,
    build_profile_lookup_js,
)
from core.utils import get_config_dir, log

DEFAULT_PROFILE = "Marwa"

# How long to wait for the picker to render after a navigation. Netflix is
# usually under a second; the ceiling only matters on a cold profile load.
GATE_APPEAR_TIMEOUT_S = 6.0
# How long to wait for the picker to disappear after clicking a profile.
GATE_CLEAR_TIMEOUT_S = 8.0
POLL_INTERVAL_S = 0.35

_profile_name_cache: str | None = None


def configured_profile() -> str:
    """Which profile to select. Cached for the process.

    Resolution order: ERA_NETFLIX_PROFILE, then `profile` in
    config/netflix.json if that file exists, then "Marwa". No file is created
    — the default is in code, and the config is purely an override.
    """
    global _profile_name_cache
    if _profile_name_cache is not None:
        return _profile_name_cache

    name = (os.environ.get("ERA_NETFLIX_PROFILE") or "").strip()
    if not name:
        try:
            cfg = get_config_dir() / "netflix.json"
            if cfg.is_file():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                name = str(data.get("profile") or "").strip()
        except Exception as e:
            log("Netflix", f"could not read config/netflix.json: {type(e).__name__}")
    _profile_name_cache = name or DEFAULT_PROFILE
    return _profile_name_cache


def reset_profile_cache() -> None:
    """Drop the cached name so a config change takes effect. Used by tests."""
    global _profile_name_cache
    _profile_name_cache = None


def _eval(sess, page, script: str) -> str:
    """Evaluate a bare-body script on the page. Returns "" on any failure."""
    try:
        return str(sess.run(page.evaluate(f"(() => {{ {script} }})()")) or "")
    except Exception as e:
        log("Netflix", f"profile-gate eval failed: {type(e).__name__}")
        return ""


def _navigate(sess, page, url: str) -> bool:
    """Go to `url`. Prefers a real page navigation, falls back to location.

    Returns False only when both routes raise — the caller then tries clicking.
    """
    if not url:
        return False
    try:
        sess.run(page.goto(url, wait_until="domcontentloaded", timeout=30_000))
        return True
    except Exception as e:
        log("Netflix", f"page.goto failed ({type(e).__name__}) — using location.assign")
    try:
        safe = json.dumps(url)
        sess.run(page.evaluate(f"(() => {{ window.location.assign({safe}); return 'ok'; }})()"))
        return True
    except Exception as e:
        log("Netflix", f"location.assign failed: {type(e).__name__}")
        return False


def gate_visible(sess, page) -> bool:
    return _eval(sess, page, DETECT_GATE_JS) == "gate"


def _wait_for_gate(sess, page, timeout_s: float) -> bool:
    """True as soon as the picker is on screen, False if it never appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if gate_visible(sess, page):
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


def _wait_for_gate_to_clear(sess, page, timeout_s: float) -> bool:
    """True once the picker is gone — the only proof the click actually took."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not gate_visible(sess, page):
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


def ensure_profile_selected(
    sess,
    page,
    profile_name: str | None = None,
    wait_for_gate: bool = True,
    appear_timeout_s: float = GATE_APPEAR_TIMEOUT_S,
    ordinal: int | None = None,
) -> dict:
    """Select the configured Netflix profile if the picker is showing.

    `ordinal` (1-based, on-screen order) selects "the fourth profile" instead
    of matching by name. It is an explicit user choice, never a fallback: a
    name that does not match still reports not-found.

    Returns {"status", "message", "profile"} where status is one of:
      no-gate          the picker never appeared — already past it
      selected         the profile was clicked and the gate cleared
      selected-unsure  clicked, but the gate was still up when time ran out
      not-found        the picker is up but has no profile by that name
      unavailable      no usable page/session

    Never raises: a failure here must degrade to "you may need to pick the
    profile yourself", never break the Netflix command that triggered it.

    `wait_for_gate=False` is the cheap path for commands that are not a
    navigation (pause, skip). It checks once and returns rather than paying
    several seconds of polling on every playback tweak.
    """
    name = (profile_name or configured_profile()).strip()
    try:
        ordinal = int(ordinal) if ordinal is not None else None
    except (TypeError, ValueError):
        ordinal = None
    # What to call the target before the picker tells us its real name.
    label = f"profile #{ordinal}" if ordinal is not None else name
    if page is None or sess is None:
        return {"status": "unavailable", "message": "No Netflix page available.", "profile": name}

    try:
        if wait_for_gate:
            present = _wait_for_gate(sess, page, appear_timeout_s)
        else:
            present = gate_visible(sess, page)
        if not present:
            return {"status": "no-gate", "message": "No profile picker showing.", "profile": name}

        # ── Primary path: read the card's SwitchProfile link and go there ──
        # Clicking the card is what used to happen here, and it reliably found
        # the right element and reliably did nothing: Netflix's React handler
        # consumes the click and the anchor's default navigation never runs, so
        # the picker stayed up through both attempts. Following the href is
        # what the anchor would have done, and it cannot be swallowed.
        lookup = _eval(sess, page, build_profile_lookup_js(name, ordinal=ordinal))
        log("Netflix", f"profile lookup: {lookup[:100]}")

        if lookup.startswith("profile-not-found"):
            seen = lookup.split("seen=", 1)[1] if "seen=" in lookup else ""
            return {
                "status": "not-found",
                "message": (
                    (
                        f"There is no profile #{ordinal} on the Netflix picker."
                        if ordinal is not None
                        else f"No Netflix profile named '{name}'."
                    )
                    + (f" The picker shows: {seen}." if seen else "")
                ),
                "profile": name,
            }

        # "" means the evaluation itself failed — that proves nothing, so only
        # a gate that is actually gone counts as being through it.
        if lookup in ("no-gate", "") and not gate_visible(sess, page):
            return {
                "status": "selected",
                "message": "Netflix is past the profile picker.",
                "profile": name,
            }

        if lookup.startswith("found|"):
            _, url, matched = (lookup.split("|", 2) + ["", ""])[:3]
            if _navigate(sess, page, url) and _wait_for_gate_to_clear(
                sess, page, GATE_CLEAR_TIMEOUT_S
            ):
                return {
                    "status": "selected",
                    "message": f"Signed in on the {matched or name} profile.",
                    "profile": matched or name,
                }
            log("Netflix", "SwitchProfile navigation did not clear the gate — trying a click")

        matched_name = ""
        if lookup.startswith(("found|", "no-href|")):
            matched_name = lookup.rsplit("|", 1)[-1]
        target = matched_name or label

        # ── Fallback: click the card (no URL, or the URL did not take) ──
        click_script = build_profile_click_js(name, ordinal=ordinal)
        for attempt in (1, 2):
            result = _eval(sess, page, click_script)
            log("Netflix", f"profile click attempt {attempt}: {result[:80]}")
            if result.startswith("profile-not-found"):
                break
            if _wait_for_gate_to_clear(sess, page, GATE_CLEAR_TIMEOUT_S):
                return {
                    "status": "selected",
                    "message": f"Signed in on the {target} profile.",
                    "profile": matched_name or name,
                }
            if attempt == 1:
                log("Netflix", "gate still visible after click — retrying once")

        return {
            "status": "selected-unsure",
            "message": (
                f"I selected the {target} profile but the picker is still showing. "
                "It may need one manual click."
            ),
            "profile": matched_name or name,
        }
    except Exception as e:  # defensive: never break the caller
        log("Netflix", f"profile gate handling failed: {type(e).__name__}: {e}")
        return {
            "status": "unavailable",
            "message": "Could not handle the Netflix profile picker.",
            "profile": name,
        }


def profile_config_path() -> Path:
    return get_config_dir() / "netflix.json"
