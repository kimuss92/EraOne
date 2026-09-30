# utils/human_click.py
"""Trusted, human-like click fallback for YouTube/Netflix grid cards.

Synthetic DOM clicks (``el.click()`` / ``dispatchEvent``) produce
``isTrusted: false`` events, which streaming sites sometimes ignore — the
card visibly "clicks" but nothing plays. This module clicks through the
browser's REAL input pipeline instead:

  Tier 1 — Playwright mouse (CDP ``Input.dispatchMouseEvent``): fully trusted
           events. The cursor glides to the target in eased steps with a
           short hover dwell and a natural press-release delay, like a hand.
  Tier 2 — OS-level pyautogui click at best-effort screen coordinates
           (only meaningful when the browser window is foreground).

Coordinates are CSS viewport pixels — exactly what
``getBoundingClientRect()`` returns, so callers can pass through the values
their scan JS already produces.
"""

from __future__ import annotations

import random
import time

__all__ = ["human_like_click", "page_url", "wait_for_url_change"]


def page_url(sess, page) -> str:
    """Current page URL, or '' when the page/context is unreachable."""
    try:
        return str(sess.run(page.evaluate("window.location.href")) or "")
    except Exception:
        return ""


def wait_for_url_change(sess, page, before_url: str, timeout_s: float = 2.5) -> bool:
    """True if the page URL changes from before_url within timeout_s.

    A page.evaluate failure while waiting is treated as a change: navigation
    tears down the JS context, which is exactly the success signal.
    """
    deadline = time.time() + max(0.2, timeout_s)
    while time.time() < deadline:
        try:
            current = str(sess.run(page.evaluate("window.location.href")) or "")
        except Exception:
            return True
        if current and current != before_url:
            return True
        time.sleep(0.25)
    return False


def _eased_waypoints(
    x0: float, y0: float, x1: float, y1: float, n: int
) -> list[tuple[float, float]]:
    """Waypoints along a slightly bowed path with ease-in-out pacing."""
    # Perpendicular bow so the path is an arc, not a robotic straight line.
    dx, dy = x1 - x0, y1 - y0
    dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
    bow = min(40.0, dist * 0.12) * random.choice((-1.0, 1.0))
    pts = []
    for i in range(1, n + 1):
        t = i / n
        te = t * t * (3 - 2 * t)  # smoothstep ease-in-out
        arc = bow * 4 * te * (1 - te)
        pts.append((x0 + dx * te - dy / dist * arc, y0 + dy * te + dx / dist * arc))
    return pts


def human_like_click(
    sess,
    page,
    x: float,
    y: float,
    *,
    expect_navigation: bool = True,
    timeout_s: float = 3.0,
    label: str = "",
) -> bool:
    """Move the trusted mouse to CSS viewport point (x, y) and click it.

    Returns True when the click was delivered and (if ``expect_navigation``)
    the page URL changed within ``timeout_s``.
    """
    tag = f" [{label}]" if label else ""
    if page is None or x is None or y is None or x <= 0 or y <= 0:
        return False

    before_url = page_url(sess, page)

    # ── Tier 1: trusted CDP mouse with human-like motion ─────────────────
    try:
        start_x = x + random.uniform(-160, 160)
        start_y = max(90.0, y - random.uniform(120, 220))
        sess.run(page.mouse.move(start_x, start_y))
        for wx, wy in _eased_waypoints(start_x, start_y, x, y, random.randint(10, 16)):
            sess.run(page.mouse.move(wx, wy))
            time.sleep(random.uniform(0.008, 0.022))
        time.sleep(random.uniform(0.09, 0.18))  # hover dwell (lets previews arm)
        sess.run(page.mouse.down())
        time.sleep(random.uniform(0.05, 0.11))
        sess.run(page.mouse.up())
        print(f"[HumanClick]{tag} trusted CDP click at ({int(x)}, {int(y)})")
        if not expect_navigation:
            return True
        if wait_for_url_change(sess, page, before_url, timeout_s):
            print(f"[HumanClick]{tag} navigation confirmed")
            return True
        print(f"[HumanClick]{tag} CDP click delivered but URL unchanged — trying OS tier")
    except Exception as exc:
        print(f"[HumanClick]{tag} CDP mouse failed: {exc}")

    # ── Tier 2: OS-level click (browser must be foreground) ──────────────
    try:
        import pyautogui

        geo = sess.run(
            page.evaluate(
                "() => ({sx: window.screenX, sy: window.screenY,"
                " ow: window.outerWidth, iw: window.innerWidth,"
                " oh: window.outerHeight, ih: window.innerHeight,"
                " dpr: window.devicePixelRatio || 1})"
            )
        )
        if not isinstance(geo, dict):
            return False
        border = max(0.0, (float(geo["ow"]) - float(geo["iw"])) / 2)
        chrome_h = max(0.0, float(geo["oh"]) - float(geo["ih"]) - border)
        dpr = float(geo.get("dpr") or 1)
        screen_x = (float(geo["sx"]) + border + x) * dpr
        screen_y = (float(geo["sy"]) + chrome_h + y) * dpr
        pyautogui.moveTo(screen_x, screen_y, duration=random.uniform(0.25, 0.45))
        time.sleep(0.1)
        pyautogui.click()
        print(f"[HumanClick]{tag} OS-level click at screen ({int(screen_x)}, {int(screen_y)})")
        if not expect_navigation:
            return True
        return wait_for_url_change(sess, page, before_url, timeout_s)
    except Exception as exc:
        print(f"[HumanClick]{tag} OS click failed: {exc}")
        return False
