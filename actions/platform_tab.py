# actions/platform_tab.py
"""
Strict platform tab ownership for YouTube and Netflix.

Every YouTube or Netflix action MUST go through get_or_open_platform_tab().
This guarantees:
  * The returned Playwright Page is the actual platform tab (not the active
    WhatsApp / ChatGPT tab).
  * Tab reuse when a matching tab already exists.
  * Explicit new-tab creation only when force_new=True.
  * No Playwright Page objects are ever stored in params, ToolResult, or
    runtime_intent.json — callers receive the Page directly from this function
    for use within their own private execution scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.utils import log

PLATFORM_HOMES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "netflix": "https://www.netflix.com",
}


@dataclass
class PlatformTabResult:
    """Return value of get_or_open_platform_tab().

    `page` is a live Playwright Page object. Do NOT serialize this object —
    use it only inside the caller's private execution scope.
    """

    page: Any
    platform: str
    reuse: bool
    selected_tab_url: str
    target_page_before_url: str
    final_url: str
    force_new: bool


def _platform_canonical_domain(platform: str) -> str:
    p = (platform or "").lower().strip()
    if p in ("youtube", "yt"):
        return "youtube.com"
    if p in ("netflix", "nf"):
        return "netflix.com"
    return ""


def _platform_home(platform: str) -> str:
    p = (platform or "").lower().strip()
    return PLATFORM_HOMES.get(p, "")


def _is_platform_url(url: str, platform: str) -> bool:
    """True if url belongs to the platform's domain family (YouTube or Netflix)."""
    if not url:
        return False
    canonical = _platform_canonical_domain(platform)
    if not canonical:
        return False
    try:
        from actions.browser_control import _domain_family_matches

        return bool(_domain_family_matches(url, canonical))
    except Exception:
        u = url.lower()
        return canonical in u


def _safe_url(url: str | None) -> str:
    if not url:
        return ""
    return str(url)[:500]


def get_or_open_platform_tab(
    platform: str,
    browser: str = "brave",
    force_new: bool = False,
) -> PlatformTabResult:
    """Return the owned platform Page object (do not serialize).

    The function:
      1. Attaches to the browser session (CDP) and rescans the tab list.
      2. If a matching tab is found and force_new=False:
         - Focuses it.
         - Brings the browser to foreground.
         - Returns it.
      3. Otherwise:
         - Opens a new tab.
         - Navigates to the platform home URL.
         - Verifies the final URL belongs to the platform family.
         - Returns the new page.

    The returned Page is intended for the immediate caller's private use. It
    must not be embedded in any routing, logging, ToolResult, or
    serialization payload.
    """
    p = (platform or "").lower().strip()
    canonical = _platform_canonical_domain(p)
    home = _platform_home(p)
    if not canonical or not home:
        raise ValueError(f"Unsupported platform: {platform!r}")

    selected_tab_url = ""
    target_page_before_url = ""
    final_url = ""
    reuse = False
    page = None

    try:
        from actions.browser_control import get_browser_session

        sess = get_browser_session(browser)
    except Exception as exc:
        log("PlatformTab", f"get_or_open_platform_tab: cannot acquire session: {exc}")
        return PlatformTabResult(
            page=None,
            platform=p,
            reuse=False,
            selected_tab_url="",
            target_page_before_url="",
            final_url="",
            force_new=force_new,
        )

    try:
        page = sess.run(sess.get_or_open_platform_page(p, force_new=force_new, home_url=home))
    except Exception as exc:
        log("PlatformTab", f"get_or_open_platform_tab: session method failed: {exc}")
        page = None

    if page is not None:
        try:
            selected_tab_url = _safe_url(page.url or "")
        except Exception:
            selected_tab_url = ""
        target_page_before_url = selected_tab_url
        final_url = selected_tab_url
        reuse = not force_new and _is_platform_url(selected_tab_url, p)

    log(
        "PlatformTab",
        (
            f"platform_tab.reuse={reuse} "
            f"platform={p} "
            f"selected_tab_url={_safe_url(selected_tab_url)} "
            f"target_page_before_url={_safe_url(target_page_before_url)} "
            f"final_url={_safe_url(final_url)} "
            f"force_new={force_new}"
        ),
    )

    try:
        from core.browser_grid_state import set_scroll_context

        set_scroll_context(p)
    except Exception:
        pass

    return PlatformTabResult(
        page=page,
        platform=p,
        reuse=reuse,
        selected_tab_url=selected_tab_url,
        target_page_before_url=target_page_before_url,
        final_url=final_url,
        force_new=force_new,
    )
