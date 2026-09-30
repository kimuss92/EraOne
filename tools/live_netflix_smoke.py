#!/usr/bin/env python3
"""Optional live Netflix smoke test.

Verifies real end-to-end behavior against a logged-in Netflix session:
  - Attaches to existing Brave CDP session
  - Navigates to Netflix search
  - Scans real DOM for candidate cards
  - Selects a safe match
  - Opens the title page
  - Verifies playback (video element or playback verifier)

Usage:
    python tools/live_netflix_smoke.py
    python tools/live_netflix_smoke.py --query "The Wild Robot"
    python tools/live_netflix_smoke.py --query "The Wild Robot" --timeout 45
    python tools/live_netflix_smoke.py --query "Berlin" --dry-run

Exit codes / output strings:
    LIVE_NETFLIX_OK              — playback verified
    LIVE_NETFLIX_SKIPPED_*       — pre-condition not met (not a failure)
    LIVE_NETFLIX_FAILED          — ran but could not complete playback
"""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# 0. Repo root on sys.path
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Netflix titles can contain characters outside the Windows console's default
# codepage (e.g. cp1252); avoid crashing on print() for those.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 1. Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Optional live Netflix smoke test against a real browser session.",
    )
    p.add_argument(
        "--query", default="The Wild Robot", help="Search query (default: 'The Wild Robot')"
    )
    p.add_argument(
        "--timeout", type=int, default=45, help="Per-step timeout in seconds (default 45)"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Only verify search + scan + match; do not play"
    )
    p.add_argument(
        "--scanner",
        action="store_true",
        help="Use legacy smart DOM scanner instead of simple text-matching mode",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# 2. Diagnostics collector
# ---------------------------------------------------------------------------


class Diagnostics:
    """Collects step-by-step diagnostics for reporting."""

    def __init__(self):
        self.steps: list[str] = []
        self.candidates: list[dict] = []
        self.rejected_chrome: list[str] = []
        self.rejected_unrelated: list[str] = []
        self.current_url: str = ""
        self.scan_scope: str = ""
        self.final_title: str = ""
        self.playback_verified: bool = False

    def step(self, msg: str):
        self.steps.append(msg)
        print(f"  [step] {msg}")

    def warn(self, msg: str):
        self.steps.append(f"WARN: {msg}")
        print(f"  [warn] {msg}")

    def fail(self, msg: str):
        self.steps.append(f"FAIL: {msg}")
        print(f"  [fail] {msg}")

    def report(self) -> str:
        lines = []
        lines.append(f"current_url: {self.current_url}")
        lines.append(f"scan_scope: {self.scan_scope}")
        if self.candidates:
            titles = [c.get("title", "?") for c in self.candidates[:10]]
            lines.append(f"candidates ({len(self.candidates)}): {titles}")
        else:
            lines.append("candidates: []")
        if self.rejected_chrome:
            lines.append(f"rejected_chrome: {self.rejected_chrome[:5]}")
        if self.rejected_unrelated:
            lines.append(f"rejected_unrelated: {self.rejected_unrelated[:5]}")
        if self.playback_verified:
            lines.append("playback_verified: true")
        lines.append(f"steps: {len(self.steps)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. CDP connectivity check
# ---------------------------------------------------------------------------


def _check_cdp_available() -> tuple[bool, str]:
    """Check if a Brave CDP session can be established.

    Returns (available: bool, reason: str).
    """
    try:
        from actions.browser_control import get_browser_session

        sess = get_browser_session("brave")
        # Try a trivial evaluate to confirm connection
        result = sess.run(
            sess.evaluate_background_media("netflix.com", "() => 'ok'"),
            timeout=10,
        )
        return True, str(result)
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 4. Netflix login check
# ---------------------------------------------------------------------------


def _check_netflix_login(sess, page) -> tuple[bool, str]:
    """Check whether Netflix is logged in by examining the current page.

    Returns (logged_in: bool, reason: str).
    """
    js = """
    () => {
        const url = window.location.href || '';
        const hasProfile = !!document.querySelector(
            '[data-uia="action-select-profile"], .profile-link, .profiles-gate'
        );
        const hasLogin = url.includes('/login') || url.includes('/Login');
        const hasBrowse = url.includes('/browse');
        const title = document.title || '';
        return JSON.stringify({
            url: url,
            hasProfile: hasProfile,
            hasLogin: hasLogin,
            hasBrowse: hasBrowse,
            title: title
        });
    }
    """
    try:
        if page is not None:
            raw = sess.run(page.evaluate(js))
        else:
            raw = sess.run(sess.evaluate_background_media("netflix.com", js))
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        url = parsed.get("url", "")
        has_login = parsed.get("hasLogin", False)
        has_browse = parsed.get("hasBrowse", False)
        has_profile = parsed.get("hasProfile", False)

        if has_login:
            return False, "login_page_detected"
        if has_profile:
            return False, "profile_chooser_detected"
        if has_browse or "netflix.com" in url:
            return True, f"logged_in_on_{url}"
        return False, f"not_on_netflix_{url}"
    except Exception as e:
        return False, f"login_check_error: {e}"


# ---------------------------------------------------------------------------
# 5. Navigate to Netflix search and scan
# ---------------------------------------------------------------------------


def _navigate_and_scan(
    sess,
    page,
    query: str,
    timeout: int,
    diag: Diagnostics,
) -> tuple[list, str, str]:
    """Navigate to Netflix search URL, wait for cards, scan.

    Returns (candidates: list[dict], scan_scope: str, current_url: str).
    """
    search_url = f"https://www.netflix.com/search?q={quote_plus(query)}"
    diag.step(f"Navigating to {search_url}")

    # Dismiss any overlays
    try:
        sess.run(page.keyboard.press("Escape"))
    except Exception:
        pass

    # Navigate
    try:
        sess.run(page.goto(search_url, wait_until="domcontentloaded", timeout=timeout * 1000))
    except Exception as e:
        diag.warn(f"goto raised: {e}")

    # Phase 1: URL readiness
    diag.step("Waiting for URL to stabilize...")
    for _ in range(6):
        time.sleep(0.5)
        try:
            url = sess.run(page.evaluate("window.location.href"))
            if "/search" in str(url).lower():
                break
        except Exception:
            pass

    # Phase 2: Card wait
    diag.step("Waiting for cards to appear...")
    card_js = """
    () => {
        const cards = document.querySelectorAll('a[href*="/title/"], a[href*="/watch/"]');
        let count = 0;
        for (const a of cards) {
            const r = a.getBoundingClientRect();
            if (r.width > 50 && r.height > 50) count++;
        }
        return count;
    }
    """
    for _ in range(8):
        time.sleep(1.5)
        try:
            count = sess.run(page.evaluate(card_js))
            if int(count) > 0:
                diag.step(f"Found {count} visible card links")
                break
        except Exception:
            pass

    # Final settle
    time.sleep(2.0)

    # Get current URL
    try:
        current_url = sess.run(page.evaluate("window.location.href"))
        diag.current_url = str(current_url)
    except Exception:
        diag.current_url = "(unknown)"

    # Scan
    diag.step("Scanning Netflix cards...")
    from actions.browser_intelligence import scan_netflix_cards

    cards = scan_netflix_cards("brave", page=page)
    diag.step(f"Scanner returned {len(cards)} cards")

    # Convert to candidates
    from actions.netflix_control import _convert_cards_to_candidates

    candidates = _convert_cards_to_candidates(cards)
    diag.candidates = [{"title": c["title"], "url": c["url"]} for c in candidates]
    diag.step(f"Converted to {len(candidates)} candidates")

    # Determine scan scope from card metadata
    scope = "unknown"
    if cards:
        first_card = cards[0]
        scope = getattr(first_card, "platform", "netflix")
    diag.scan_scope = scope

    return candidates, scope, diag.current_url


# ---------------------------------------------------------------------------
# 5b. Navigate to Netflix search and scan (simple text-matching mode)
# ---------------------------------------------------------------------------


def _navigate_and_scan_simple(
    sess,
    page,
    query: str,
    timeout: int,
    diag: Diagnostics,
) -> tuple[list, str, str]:
    """Navigate to Netflix search URL, wait for links, score by text match.

    Uses the simple mode helpers from netflix_control instead of the smart
    DOM scanner.  Returns (candidates: list[dict], scope: str, current_url: str).
    """
    search_url = f"https://www.netflix.com/search?q={quote_plus(query)}"
    diag.step(f"Navigating to {search_url} (simple mode)")

    # Dismiss any overlays
    try:
        sess.run(page.keyboard.press("Escape"))
        time.sleep(0.3)
        sess.run(page.keyboard.press("Escape"))
        time.sleep(0.3)
        sess.run(page.keyboard.press("Escape"))
    except Exception:
        pass

    # Navigate
    try:
        sess.run(page.goto(search_url, wait_until="domcontentloaded", timeout=timeout * 1000))
    except Exception as e:
        diag.warn(f"goto raised: {e}")

    # Phase 1: URL readiness
    diag.step("Waiting for URL to stabilize...")
    for _ in range(6):
        time.sleep(0.5)
        try:
            url = str(sess.run(page.evaluate("window.location.href")) or "").lower()
            if "/search" in url and "?q=" in url:
                break
        except Exception:
            pass

    # Phase 2: Wait for visible title links
    diag.step("Waiting for title links to appear...")
    link_js = """
    () => {
        const links = document.querySelectorAll('a[href*="/title/"], a[href*="/watch/"]');
        let count = 0;
        for (const a of links) {
            const r = a.getBoundingClientRect();
            if (r.width > 50 && r.height > 50) count++;
        }
        return count;
    }
    """
    for _ in range(8):
        time.sleep(1.5)
        try:
            count = sess.run(page.evaluate(link_js))
            if int(count) > 0:
                diag.step(f"Found {count} visible title links")
                break
        except Exception:
            pass

    time.sleep(1.0)

    # Get current URL
    try:
        diag.current_url = str(sess.run(page.evaluate("window.location.href")))
    except Exception:
        diag.current_url = "(unknown)"

    # Use simple mode text matching
    diag.step("Scoring candidates by text match (simple mode)...")
    from actions.netflix_control import _find_netflix_title_by_text

    scored = _find_netflix_title_by_text(page, sess, query)
    diag.step(f"Simple mode returned {len(scored)} scored candidates")

    # Convert to standard candidate format
    candidates = []
    for s in scored:
        candidates.append(
            {
                "title": s.get("text", ""),
                "url": s.get("href", ""),
                "score": s.get("score", 0),
                "reason": s.get("reason", ""),
            }
        )
    diag.candidates = [{"title": c["title"], "url": c["url"]} for c in candidates]
    diag.scan_scope = "simple_text_match"

    return candidates, "simple_text_match", diag.current_url


# ---------------------------------------------------------------------------
# 6. Match and verify
# ---------------------------------------------------------------------------


def _match_candidate(
    query: str, candidates: list[dict], diag: Diagnostics
) -> tuple[dict | None, float, str]:
    """Select the best candidate using existing scoring.

    Returns (best, score, reason).
    """
    from utils.media_match import select_best_media_candidate

    best, score, reason = select_best_media_candidate(query, candidates, "netflix")
    if best:
        diag.step(f"Best match: {best.get('title', '?')} (score={score:.2f}, reason={reason})")
    else:
        diag.step(f"No confident match (best_score={score:.2f}, reason={reason})")
    return best, score, reason


# ---------------------------------------------------------------------------
# 7. Playback verification
# ---------------------------------------------------------------------------


def _verify_and_play(
    sess,
    page,
    best: dict,
    diag: Diagnostics,
    timeout: int,
) -> bool:
    """Navigate to the matched title and verify playback.

    Returns True if playback is verified.
    """
    nav_url = best.get("url", "")
    if not nav_url:
        diag.fail("No URL in matched candidate")
        return False

    diag.step(f"Navigating to title: {nav_url}")
    try:
        sess.run(page.goto(nav_url, wait_until="domcontentloaded", timeout=timeout * 1000))
    except Exception as e:
        diag.warn(f"goto title raised: {e}")

    time.sleep(2.0)

    # Update current URL
    try:
        diag.current_url = sess.run(page.evaluate("window.location.href"))
    except Exception:
        pass

    # Check if we landed on a /watch/ or /title/ page
    if "/watch/" not in diag.current_url and "/title/" not in diag.current_url:
        diag.warn(f"Landed on unexpected URL: {diag.current_url}")

    # Verify playback
    from actions.netflix_control import _verify_playback_started

    diag.step("Verifying playback...")
    is_playing = _verify_playback_started(sess, page)

    if is_playing:
        diag.playback_verified = True
        diag.step("Playback verified successfully")
        return True

    # Try clicking play button
    diag.step("Playback not yet active, attempting to click Play button...")
    from actions.netflix_control import (
        _extract_netflix_title_id,
        click_netflix_play_button_for_current_title,
    )

    expected_title_id = _extract_netflix_title_id(diag.current_url) or _extract_netflix_title_id(
        nav_url
    )
    err = click_netflix_play_button_for_current_title(
        sess, page, expected_title=best.get("title"), expected_title_id=expected_title_id
    )
    if err:
        diag.warn(f"Play button click: {err}")

    time.sleep(2.0)

    # Re-verify
    is_playing = _verify_playback_started(sess, page)
    if is_playing:
        diag.playback_verified = True
        diag.step("Playback verified after Play button click")
        return True

    diag.fail("Playback verification failed after all attempts")
    return False


# ---------------------------------------------------------------------------
# 8. Dry-run mode
# ---------------------------------------------------------------------------


def _dry_run(
    sess,
    page,
    query: str,
    timeout: int,
    diag: Diagnostics,
    use_scanner: bool = False,
) -> str:
    """Dry-run: verify search + scan + match without playing.

    Returns result string.
    """
    diag.step("DRY RUN — will not start playback")

    if use_scanner:
        candidates, scope, current_url = _navigate_and_scan(sess, page, query, timeout, diag)
    else:
        candidates, scope, current_url = _navigate_and_scan_simple(sess, page, query, timeout, diag)

    if not candidates:
        diag.fail("No candidates found")
        return f"LIVE_NETFLIX_FAILED\n{diag.report()}\nreason: no candidates in dry-run"

    best, score, reason = _match_candidate(query, candidates, diag)

    if not best or reason != "selected":
        diag.fail(f"No confident match (reason={reason})")
        return f"LIVE_NETFLIX_FAILED\n{diag.report()}\nreason: {reason} in dry-run"

    diag.final_title = best.get("title", "")
    diag.step(f"DRY RUN matched: {diag.final_title} (score={score:.2f})")
    return (
        f"LIVE_NETFLIX_OK_DRY_RUN\nMatched: {diag.final_title}\nScore: {score:.2f}\n{diag.report()}"
    )


# ---------------------------------------------------------------------------
# 9. Full live run
# ---------------------------------------------------------------------------


def _live_run(
    sess,
    page,
    query: str,
    timeout: int,
    diag: Diagnostics,
    use_scanner: bool = False,
) -> str:
    """Full live run: search → scan → match → play → verify.

    Returns result string.
    """
    if use_scanner:
        candidates, scope, current_url = _navigate_and_scan(sess, page, query, timeout, diag)
    else:
        candidates, scope, current_url = _navigate_and_scan_simple(sess, page, query, timeout, diag)

    if not candidates:
        diag.fail("Zero candidates found after scan")
        return f"LIVE_NETFLIX_FAILED\n{diag.report()}\nreason: zero_cards"

    best, score, reason = _match_candidate(query, candidates, diag)

    if not best or reason != "selected":
        diag.fail(f"No confident match (reason={reason})")
        return f"LIVE_NETFLIX_FAILED\n{diag.report()}\nreason: {reason}"

    diag.final_title = best.get("title", "")

    ok = _verify_and_play(sess, page, best, diag, timeout)
    if not ok:
        return f"LIVE_NETFLIX_FAILED\n{diag.report()}\nreason: playback_verification_failed"

    return f"LIVE_NETFLIX_OK\nPlaying: {diag.final_title}\n{diag.report()}"


# ---------------------------------------------------------------------------
# 10. Main
# ---------------------------------------------------------------------------


def main():
    args = _parse_args()
    query = args.query.strip()
    timeout = max(10, min(args.timeout, 120))
    dry_run = args.dry_run
    use_scanner = args.scanner

    if not query:
        print("LIVE_NETFLIX_FAILED\nreason: empty query")
        sys.exit(1)

    mode_label = "scanner (legacy)" if use_scanner else "simple (text-match)"
    print(
        f"Live Netflix smoke test: query={query!r}, timeout={timeout}s, dry_run={dry_run}, mode={mode_label}"
    )
    print()

    # Step 1: Check CDP
    print("[1/4] Checking CDP connection...")
    cdp_ok, cdp_reason = _check_cdp_available()
    if not cdp_ok:
        print(f"  CDP unavailable: {cdp_reason}")
        print()
        print("LIVE_NETFLIX_SKIPPED_CDP_UNAVAILABLE")
        sys.exit(0)
    print("  CDP connected")

    # Step 2: Acquire session + page
    print("[2/4] Acquiring Netflix page...")
    try:
        from actions.browser_control import get_browser_session
        from actions.platform_tab import get_or_open_platform_tab

        sess = get_browser_session("brave")
        tab_result = get_or_open_platform_tab(platform="netflix", browser="brave")
        page = tab_result.page
        print(f"  Page acquired: {page is not None}")
    except Exception as e:
        print(f"  Failed to acquire page: {e}")
        print()
        print("LIVE_NETFLIX_SKIPPED_CDP_UNAVAILABLE")
        sys.exit(0)

    # Step 3: Check login
    print("[3/4] Checking Netflix login status...")
    logged_in, login_reason = _check_netflix_login(sess, page)
    if not logged_in:
        print(f"  Not logged in: {login_reason}")
        print()
        print("LIVE_NETFLIX_SKIPPED_LOGIN_REQUIRED")
        sys.exit(0)
    print(f"  Logged in: {login_reason}")

    # Step 4: Run test
    print(f"[4/4] Running {'dry-run' if dry_run else 'live'} test...")
    diag = Diagnostics()
    try:
        if dry_run:
            result = _dry_run(sess, page, query, timeout, diag, use_scanner=use_scanner)
        else:
            result = _live_run(sess, page, query, timeout, diag, use_scanner=use_scanner)
    except Exception as e:
        diag.fail(f"Unhandled exception: {e}")
        traceback.print_exc()
        result = f"LIVE_NETFLIX_FAILED\n{diag.report()}\nreason: exception_{type(e).__name__}"

    print()
    print(result)

    # Exit code: 0 for OK/skip, 1 for failure
    if result.startswith("LIVE_NETFLIX_OK") or result.startswith("LIVE_NETFLIX_SKIPPED"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
