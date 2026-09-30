"""
Centralized scroll engine for Era One.

Provides:
  - resolve_amount     — preset/px → pixel amount (small=300, normal=700, large=1200, page=90% vh)
  - resolve_direction  — "up"/"down"/"left"/"right" → (x_sign, y_sign)
  - clamp_repeat       — clamp repeat count to [1, 10]
  - build_scroll_script— return JS scrollBy string
  - scroll             — execute scroll via browser page (JS)

Usage:
    from utils.scroll import scroll, resolve_amount, resolve_direction
"""

from __future__ import annotations

from typing import Any

from core.logger import log_event, main_logger

# ── Presets ───────────────────────────────────────────────────────────────────
_PRESET_MAP: dict[str, int] = {
    "small": 300,
    "sm": 300,
    "normal": 700,
    "md": 700,
    "med": 700,
    "large": 1200,
    "lg": 1200,
}

_BEHAVIOR_MAP: dict[str, str] = {
    "smooth": "smooth",
    "auto": "auto",
    "instant": "auto",
}

_PAGE_FALLBACK_PX = 900


def resolve_amount(amount: str | int, viewport_height: int | None = None) -> int:
    """Convert preset name or pixel number to pixel amount.

    Presets: small(300), normal(700), large(1200), page(90% viewport)

    Aliases: sm, md/med, lg

    If ``amount`` is a string it is looked up case-insensitively.
    Integers are returned as-is.
    Unknown strings fall back to the **normal** preset (700 px).
    """
    if isinstance(amount, int):
        return max(0, amount)

    key = amount.strip().lower()

    if key == "page":
        if viewport_height is not None and viewport_height > 0:
            return max(1, int(viewport_height * 0.9))
        return _PAGE_FALLBACK_PX

    resolved = _PRESET_MAP.get(key)
    if resolved is not None:
        return resolved

    try:
        return max(0, int(key))
    except (ValueError, TypeError):
        log_event(main_logger, "scroll.unknown_amount", "WARNING", amount=amount, fallback="normal")
        return _PRESET_MAP["normal"]


def resolve_direction(direction: str) -> tuple[int, int]:
    """Return (x_sign, y_sign) for the given direction string."""
    d = direction.strip().lower()
    if d == "up":
        return (0, -1)
    if d == "down":
        return (0, 1)
    if d == "left":
        return (-1, 0)
    if d == "right":
        return (1, 0)
    log_event(
        main_logger, "scroll.invalid_direction", "WARNING", direction=direction, fallback="down"
    )
    return (0, 1)


def clamp_repeat(repeat: int) -> int:
    """Clamp repeat count to the inclusive range [1, 10]."""
    return max(1, min(repeat, 10))


def resolve_behavior(behavior: str) -> str:
    """Map user-facing behavior name to JS-valid behavior string."""
    mapped = _BEHAVIOR_MAP.get(behavior.strip().lower())
    if mapped is not None:
        return mapped
    log_event(
        main_logger, "scroll.unknown_behavior", "WARNING", behavior=behavior, fallback="smooth"
    )
    return "smooth"


def build_scroll_script(
    x_sign: int,
    y_sign: int,
    amount_px: int,
    behavior: str = "smooth",
) -> str:
    """Build a JavaScript ``window.scrollBy`` expression string.

    The returned script returns a JSON string with post-scroll metrics.
    """
    top = y_sign * amount_px
    left = x_sign * amount_px
    js_behavior = resolve_behavior(behavior)
    return f"""
    window.scrollBy({{top: {top}, left: {left}, behavior: '{js_behavior}'}});
    JSON.stringify({{scrollY: window.scrollY, innerHeight: window.innerHeight, bodyHeight: document.body.scrollHeight}});
    """


async def scroll(
    page,
    direction: str = "down",
    amount: str | int = "normal",
    repeat: int = 1,
    behavior: str = "smooth",
    source: str = "browser_control",
) -> dict[str, Any]:
    """Execute scroll on a Playwright page using the centralized engine.

    Parameters
    ----------
    page : Page
        Playwright async page instance.
    direction : str, optional
        One of "up", "down", "left", "right" (default "down").
    amount : str | int, optional
        Preset name ("small"/"normal"/"large"/"page") or pixel integer.
        (default "normal" → 700 px).
    repeat : int, optional
        How many times to repeat the scroll (clamped 1–10, default 1).
    behavior : str, optional
        CSS scroll-behavior value: "smooth", "auto", "instant" (default "smooth").
    source : str, optional
        Identifier for logging provenance (default "browser_control").

    Returns
    -------
    dict
        Keys: direction, amount_px, repeat, behavior, total_px, source, method
    """
    repeat = clamp_repeat(repeat)
    x_sign, y_sign = resolve_direction(direction)

    # Determine viewport height for "page" preset
    viewport_height: int | None = None
    try:
        vh_str = await page.evaluate("window.innerHeight")
        viewport_height = int(vh_str) if vh_str else None
    except Exception:
        pass

    amount_px = resolve_amount(amount, viewport_height=viewport_height)
    total_px = amount_px * repeat
    js_behavior = resolve_behavior(behavior)

    for i in range(repeat):
        try:
            script = f"""
            window.scrollBy({{top: {y_sign * amount_px}, left: {x_sign * amount_px}, behavior: '{js_behavior}'}});
            """
            await page.evaluate(script)
            if i < repeat - 1:
                await page.wait_for_timeout(80)
        except Exception as exc:
            log_event(
                main_logger,
                "scroll.execution_error",
                "ERROR",
                source=source,
                iteration=i + 1,
                error=str(exc),
            )
            raise

    log_event(
        main_logger,
        "scroll.performed",
        "INFO",
        source=source,
        direction=direction,
        amount_px=amount_px,
        repeat=repeat,
        total_px=total_px,
        behavior=js_behavior,
        method="js",
    )

    return {
        "direction": direction,
        "amount_px": amount_px,
        "repeat": repeat,
        "behavior": js_behavior,
        "total_px": total_px,
        "source": source,
        "method": "js",
    }
