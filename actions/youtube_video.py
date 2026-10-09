# actions/youtube_video.py
"""
Era One YouTube Controller — tab-aware, browser-integrated.

All playback uses the active Playwright browser tab (Brave by default).
No subprocess.Popen / OS URL opens — videos always play in the same tab
Era One already has open, just like a real user would navigate it.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

try:
    import requests

    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi

    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False

from core.intent_memory import remember_action
from core.utils import get_base_dir, is_linux, is_mac, is_windows
from core.win_subprocess import NO_WINDOW

# ── Import browser session bridge ────────────────────────────────────────────
try:
    from actions.browser_control import detect_default_browser_name, get_browser_session

    _BROWSER_BRIDGE = True
except ImportError:
    _BROWSER_BRIDGE = False

BASE_DIR = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# YouTube scraping constants
_YT_VIDEO_FILTER = "EgIQAQ%3D%3D"  # filter: videos only (no Shorts, no playlists)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_DEFAULT_BROWSER = None


def _default_youtube_browser() -> str | None:
    if not _BROWSER_BRIDGE:
        return None
    try:
        return detect_default_browser_name()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL UTILITIES
# ─────────────────────────────────────────────────────────────────────────────


def _scrape_first_video_url(query: str) -> str | None:
    if not _REQUESTS_OK:
        return None
    search_url = (
        f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={_YT_VIDEO_FILTER}"
    )
    try:
        r = requests.get(search_url, headers=_HEADERS, timeout=10)
        html = r.text
        seen: set[str] = set()
        for vid in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
            if vid in seen:
                continue
            seen.add(vid)
            if f"/shorts/{vid}" in html:
                continue
            return f"https://www.youtube.com/watch?v={vid}"
    except Exception as e:
        print(f"[YouTube] ⚠️ Scrape failed: {e}")
    return None


def _extract_video_id(url: str) -> str | None:
    match = re.search(r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else None


def _is_valid_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or ""))


def _ask_for_url(prompt_text: str = "YouTube video URL:") -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk._default_root or tk.Tk()
        root.withdraw()
        url = simpledialog.askstring("Era One", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] ⚠️ UI Dialog failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER NAVIGATION  (in-tab, no OS window)
# ─────────────────────────────────────────────────────────────────────────────


def _save_youtube_grid(cards: list, query: str) -> None:
    """Save YouTube grid snapshot for ordinal selection."""
    try:
        import time

        from core.browser_grid_state import GridCandidate, GridSnapshot, save_grid

        candidates = []
        for i, card in enumerate(cards):
            candidates.append(
                GridCandidate(
                    visible_order=i + 1,
                    title=getattr(card, "title", "") or "",
                    url=getattr(card, "href", "") or "",
                    kind="video",
                    safe=True,
                    source="youtube",
                )
            )
        snap = GridSnapshot(
            platform="youtube",
            query=query or "",
            timestamp=time.time(),
            candidates=candidates,
        )
        save_grid(snap)
    except Exception:
        pass


def _navigate_in_browser(url: str, browser_name: str | None = _DEFAULT_BROWSER) -> str:
    """Navigate the owned YouTube tab to *url*. Uses platform_tab ownership."""
    browser_name = browser_name or _default_youtube_browser()
    if _BROWSER_BRIDGE:
        try:
            from actions.platform_tab import get_or_open_platform_tab

            tab_result = get_or_open_platform_tab(
                platform="youtube", browser=browser_name or "brave"
            )
            page = tab_result.page
            if page is None:
                raise RuntimeError("get_or_open_platform_tab returned None page")
            # Navigate the owned page to the specific URL
            import asyncio

            try:
                loop = asyncio.get_event_loop()
            except Exception:
                loop = None
            if loop and loop.is_running():
                from actions.browser_control import get_browser_session

                sess = get_browser_session(browser_name)
                sess.run(page.goto(url, wait_until="domcontentloaded", timeout=30_000))
            else:
                from actions.browser_control import get_browser_session

                sess = get_browser_session(browser_name)
                sess.run(page.goto(url, wait_until="domcontentloaded", timeout=30_000))
            print(f"[YouTube] 🎬 Owned-tab navigation → {url}")
            return f"Navigated owned YouTube tab to: {url}"
        except Exception as e:
            print(f"[YouTube] ⚠️ Platform tab navigation failed ({e}), fallback")

    _os_open_url(url)
    return f"Opened (OS): {url}"


def _os_open_url(url: str) -> None:
    try:
        if is_mac():
            subprocess.Popen(["open", url])
        elif is_linux():
            subprocess.Popen(["xdg-open", url])
        else:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False, creationflags=NO_WINDOW)
    except Exception as e:
        print(f"[YouTube] ⚠️ OS open_url failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# PLAYBACK CONTROL  (Background execution)
# ─────────────────────────────────────────────────────────────────────────────


def _handle_playback_control(action: str, browser_name: str | None = _DEFAULT_BROWSER) -> str:
    browser_name = browser_name or _default_youtube_browser()
    try:
        remember_action(
            tool="youtube_video",
            action=f"youtube_{action}",
            app="browser",
            media_app="youtube",
            browser=browser_name,
        )
    except Exception:
        pass

    print(f"[YouTube] Intercepting play/pause instruction for background execution: {action}")

    # Activate the browser window so the YouTube tab has a focused renderer.
    # This ensures CDP commands reach an active player, not a throttled background tab.
    try:
        from actions.window_control import focus_window

        focus_window(browser_name or "brave")
        import time

        time.sleep(0.15)
    except Exception:
        pass

    # Forcefully maximize the browser window to ensure the video is displayed fullscreen.
    try:
        from actions.browser_control import get_browser_session

        sess = get_browser_session(browser_name)
        sess.run(sess._maximize_browser_window_windows())
        time.sleep(0.1)
    except Exception:
        pass

    js_scripts = {
        "pause": (
            "const v = document.querySelector('video'); "
            "if(v) { v.pause(); return 'YouTube video paused cleanly'; } "
            "return 'No HTML5 video element found on YouTube page';"
        ),
        "resume": (
            "const v = document.querySelector('video'); "
            "if(v) { v.play(); return 'YouTube video resumed cleanly'; } "
            "return 'No HTML5 video element found on YouTube page';"
        ),
        "play_pause": (
            "const v = document.querySelector('video'); "
            "if(v) { if(v.paused) { v.play(); return 'YouTube video resumed'; } else { v.pause(); return 'YouTube video paused'; } } "
            "return 'No HTML5 video target discovered';"
        ),
    }

    script = js_scripts.get(action if action in js_scripts else "play_pause")

    cdp_error: str | None = None
    # Primary path: CDP-based JS injection into the YouTube tab.
    try:
        sess = get_browser_session(browser_name)
        result = sess.run(sess.evaluate_background_media("youtube.com", script))
        print(f"[YouTube] Native CDP Pipeline Response: {result}")
        # If CDP succeeded, return the result directly.
        if "No HTML5" not in result and "exception" not in result.lower():
            return result
    except Exception as e:
        cdp_error = str(e)
        print(f"[YouTube] CDP injection failed ({cdp_error}), falling back to keyboard simulation")

    # Fallback: keyboard shortcut simulation on the focused browser window.
    # YouTube uses Spacebar (or 'k') to toggle play/pause.
    try:
        import pyautogui

        pyautogui.press("space")
        print(f"[YouTube] Keyboard fallback sent spacebar for action: {action}")
        return f"YouTube {action} triggered via keyboard fallback."
    except Exception as kb_e:
        cdp_detail = f" CDP: {cdp_error}" if cdp_error else ""
        return f"YouTube control failed: keyboard ({kb_e}){cdp_detail}"


# ─────────────────────────────────────────────────────────────────────────────
# TRANSCRIPT + SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

_LANG_PRIORITY = ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]


def _get_transcript(video_id: str) -> str | None:
    if not _TRANSCRIPT_OK:
        return None
    try:
        tlist = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        try:
            transcript = tlist.find_manually_created_transcript(_LANG_PRIORITY)
        except Exception:
            pass
        if transcript is None:
            try:
                transcript = tlist.find_generated_transcript(_LANG_PRIORITY)
            except Exception:
                for t in tlist:
                    transcript = t
                    break
        if transcript is None:
            return None
        return " ".join(entry["text"] for entry in transcript.fetch())
    except Exception as e:
        print(f"[YouTube] ⚠️ Transcript retrieval failed: {e}")
        return None


def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from core.ai_provider import GeminiModel  # genai.configure() is gone in google-genai 2.x

    model = GeminiModel(
        model_name="gemini-3.8-flash",
        system_instruction=(
            "You are Era One, an AI assistant. "
            "Summarize YouTube video transcripts clearly and concisely. "
            "Structure: 1-sentence overview, then 3-5 key bullet points. "
            "Be direct. Address the user as 'sir'. "
            "Match the language of the transcript."
        ),
    )
    max_chars = 80_000
    truncated = transcript[:max_chars] + ("…" if len(transcript) > max_chars else "")
    response = model.generate_content(f"Summarize this YouTube video transcript:\n\n{truncated}")
    return response.text.strip()


def _save_summary(content: str, video_url: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / f"youtube_summary_{ts}.txt"
    header = (
        f"Era One — YouTube Summary\n"
        f"{'─' * 50}\n"
        f"URL    : {video_url}\n"
        f"Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 50}\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")

    if is_windows():
        subprocess.Popen(["notepad.exe", str(filepath)], creationflags=NO_WINDOW)
    elif is_mac():
        subprocess.Popen(["open", "-t", str(filepath)])
    else:
        subprocess.Popen(["xdg-open", str(filepath)])

    return str(filepath)


# ─────────────────────────────────────────────────────────────────────────────
# VIDEO INFO + TRENDING SCRAPERS
# ─────────────────────────────────────────────────────────────────────────────


def _scrape_video_info(video_id: str) -> dict:
    if not _REQUESTS_OK:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=12)
        html = r.text
        info: dict = {}
        for key, pattern in [
            ("title", r'"title":\{"runs":\[\{"text":"([^"]+)"'),
            ("channel", r'"ownerChannelName":"([^"]+)"'),
            ("views", r'"viewCount":"(\d+)"'),
            ("duration", r'"lengthSeconds":"(\d+)"'),
            ("likes", r'"label":"([0-9,]+ likes)"'),
        ]:
            m = re.search(pattern, html)
            if not m:
                continue
            raw = m.group(1)
            if key == "views":
                info[key] = f"{int(raw):,}"
            elif key == "duration":
                s = int(raw)
                info[key] = f"{s // 60}:{s % 60:02d}"
            else:
                info[key] = raw
        return info
    except Exception as e:
        print(f"[YouTube] ⚠️ Info scrape failed: {e}")
        return {}


def _scrape_trending(region: str = "US", max_results: int = 8) -> list[dict]:
    if not _REQUESTS_OK:
        return []
    url = f"https://www.youtube.com/feed/trending?gl={region.upper()}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=12)
        html = r.text
        titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]', html)
        channels = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html)
        results: list[dict] = []
        seen: set[str] = set()
        for i, title in enumerate(titles):
            if title in seen or len(title) < 5:
                continue
            seen.add(title)
            channel = channels[i] if i < len(channels) else "Unknown"
            results.append({"rank": len(results) + 1, "title": title, "channel": channel})
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        print(f"[YouTube] ⚠️ Trending scrape failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ACTION HANDLERS
# ─────────────────────────────────────────────────────────────────────────────


def _get_page_url(browser_name: str | None) -> str:
    """Get current page URL from the browser session."""
    try:
        sess = get_browser_session(browser_name)
        return sess.run(sess.get_url()) or ""
    except Exception:
        return ""


def _verify_youtube_navigation(browser_name: str | None, expected_url: str) -> tuple[bool, str]:
    """Check that the owned YouTube page navigated to a watch page.

    Uses owned page URL instead of active tab URL to avoid
    reading the wrong tab (e.g. WhatsApp).
    Returns (success, final_url).
    """
    try:
        from actions.platform_tab import get_or_open_platform_tab

        tab_result = get_or_open_platform_tab(platform="youtube", browser=browser_name or "brave")
        page = tab_result.page
        if page is None:
            sess = get_browser_session(browser_name)
            current_url = sess.run(sess.get_url()) or ""
        else:
            current_url = str(page.url) or ""
        print(f"[YouTube] Post-navigation URL: {current_url[:100]}")
        if "/watch" in current_url or "youtu.be/" in current_url:
            return True, current_url
        print(f"[YouTube] ⚠️ URL is not a watch page: {current_url[:120]}")
        return False, current_url
    except Exception as e:
        print(f"[YouTube] ⚠️ URL verification failed: {e}")
        return False, ""


def _handle_play(params: dict, player, browser_name: str) -> str:
    query = params.get("query", "").strip()
    if not query:
        return "Please tell me what you'd like to watch, sir."

    # Running-only Spotify pause: prevents unwanted Spotify startup during YouTube navigation.
    try:
        from actions.spotify_control import spotify_control

        spotify_control({"action": "pause_if_running_only"})
    except Exception:
        pass

    if player:
        player.write_log(f"[YouTube] Searching: {query}")
    print(f"[YouTube] 🔍 Searching for: {query}")

    search_url = (
        f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={_YT_VIDEO_FILTER}"
    )

    # ── Check for ordinal command before doing search ──
    try:
        from utils.media_match import clean_youtube_watch_url, parse_ordinal_media_command

        ordinal_info = parse_ordinal_media_command(query)
        if ordinal_info:
            ordinal = ordinal_info["ordinal"]
            print(f"[YouTube] Ordinal command detected: #{ordinal}")
            from actions.browser_intelligence import scan_youtube_cards

            cards = scan_youtube_cards(browser_name)
            _save_youtube_grid(cards, query)
            if not cards:
                return "I need YouTube search results first. What should I search?"
            from utils.media_match import filter_youtube_candidates

            candidates = [
                {"title": c.title, "url": c.href, "href": c.href, "kind": "video"} for c in cards
            ]
            filtered = filter_youtube_candidates(candidates)
            if ordinal > len(filtered):
                return f"I only found {len(filtered)} safe YouTube videos. Which one should I open?"
            target = filtered[ordinal - 1]
            target_url = clean_youtube_watch_url(target["url"]) or target["url"]
            print(f"[YouTube] Ordinal #{ordinal}: {target['title'][:60]} → {target_url[:80]}")
            before_url = _get_page_url(browser_name)
            _navigate_in_browser(target_url, browser_name)
            time.sleep(1.0)
            ok, final_url = _verify_youtube_navigation(browser_name, target_url)
            print(f"[YouTube] selected_url={target_url}")
            print(f"[YouTube] target_page_before_url={before_url[:100] if before_url else ''}")
            print(f"[YouTube] final_url={final_url[:100] if final_url else ''}")
            print(f"[YouTube] verification_success={ok}")
            if ok:
                try:
                    from core.browser_grid_state import set_scroll_context

                    set_scroll_context("youtube")
                except Exception:
                    pass
                return f"Playing: {target['title']}"
            return "I selected a YouTube result but could not verify that it opened."
    except ImportError:
        pass
    except Exception as e:
        print(f"[YouTube] Ordinal selection error: {e}")

    # ── Generic query fast path: try HTTP scrape for direct URL ──
    try:
        from utils.media_match import is_generic_media_query as _is_generic

        if _is_generic(query):
            print(f"[YouTube] Generic query detected: '{query}' — trying fast direct path")
            video_url = _scrape_first_video_url(query)
            if video_url:
                print(f"[YouTube] Fast path: got URL {video_url}")
                before_url = _get_page_url(browser_name)
                _navigate_in_browser(video_url, browser_name)
                time.sleep(1.0)
                ok, final_url = _verify_youtube_navigation(browser_name, video_url)
                if ok:
                    try:
                        from core.browser_grid_state import set_scroll_context

                        set_scroll_context("youtube")
                    except Exception:
                        pass
                    print(f"[YouTube] Fast path succeeded: {video_url}")
                    return f"Playing: {query}"
                print(
                    "[YouTube] Fast path navigation verification failed, falling back to browser search"
                )
            else:
                print("[YouTube] Fast path scrape failed, falling back to browser search")
    except Exception as e:
        print(f"[YouTube] Fast path error: {e}")

    # ── Safe selection via browser scanning + title scoring ──────────────
    try:
        from actions.browser_intelligence import scan_youtube_cards
        from utils.media_match import (
            filter_youtube_candidates,
            is_generic_media_query,
            is_youtube_watch_url,
            select_best_media_candidate,
        )

        _navigate_in_browser(search_url, browser_name)
        time.sleep(2.0)

        cards = scan_youtube_cards(browser_name)
        _save_youtube_grid(cards, query)
        print(f"[YouTube] Scanned {len(cards) if cards else 0} cards")
        if cards:
            candidates = [
                {"title": c.title, "url": c.href, "href": c.href, "kind": "video"} for c in cards
            ]
            for i, c in enumerate(candidates):
                print(f"[YouTube]  Candidate {i + 1}: title={c['title'][:60]}, url={c['url'][:80]}")

            filtered = filter_youtube_candidates(candidates)
            print(f"[YouTube] After filter: {len(filtered)}/{len(candidates)} candidates remain")

            # Determine if query is short (1-2 words, specific) for fallback
            query_words = [w for w in query.lower().split() if w not in ("play", "the", "a", "an")]
            is_short_query = len(query_words) <= 2

            if filtered:
                is_generic = is_generic_media_query(query)
                print(f"[YouTube] is_generic={is_generic}, query_words={len(query_words)}")

                if is_generic:
                    selected_url = clean_youtube_watch_url(filtered[0]["url"]) or filtered[0]["url"]
                    before_url = _get_page_url(browser_name)
                    _navigate_in_browser(selected_url, browser_name)
                    time.sleep(1.0)
                    ok, final_url = _verify_youtube_navigation(browser_name, selected_url)
                    print(f"[YouTube] selected_url={selected_url}")
                    print(
                        f"[YouTube] target_page_before_url={before_url[:100] if before_url else ''}"
                    )
                    print(f"[YouTube] final_url={final_url[:100] if final_url else ''}")
                    print(f"[YouTube] verification_success={ok}")
                    if ok:
                        return f"Playing: {query}"
                    return "I selected a YouTube result but could not verify that it opened."

                best, score, reason = select_best_media_candidate(query, filtered, "youtube")
                print(
                    f"[YouTube] Selection: best={best['title'][:60] if best else 'None'}, score={score:.2f}, reason={reason}"
                )

                if best and reason == "selected":
                    selected_url = clean_youtube_watch_url(best["url"]) or best["url"]
                    before_url = _get_page_url(browser_name)
                    _navigate_in_browser(selected_url, browser_name)
                    time.sleep(1.0)
                    ok, final_url = _verify_youtube_navigation(browser_name, selected_url)
                    print(f"[YouTube] selected_url={selected_url}")
                    print(
                        f"[YouTube] target_page_before_url={before_url[:100] if before_url else ''}"
                    )
                    print(f"[YouTube] final_url={final_url[:100] if final_url else ''}")
                    print(f"[YouTube] verification_success={ok}")
                    if ok:
                        return f"Playing: {query}"
                    return "I selected a YouTube result but could not verify that it opened."

                if reason == "ambiguous":
                    lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(filtered[:5])]
                    msg = "I found multiple possible YouTube videos. Please specify the title more clearly."
                    if lines:
                        msg += "\n" + "\n".join(lines)
                    return msg

                # low_confidence: fall back to first safe organic for short queries
                if is_short_query:
                    fallback = filtered[0]
                    fallback_url = clean_youtube_watch_url(fallback["url"]) or fallback["url"]
                    print(
                        f"[YouTube] Low confidence but short query — using first safe organic: {fallback['title'][:60]}"
                    )
                    before_url = _get_page_url(browser_name)
                    _navigate_in_browser(fallback_url, browser_name)
                    time.sleep(1.0)
                    ok, final_url = _verify_youtube_navigation(browser_name, fallback_url)
                    print(f"[YouTube] selected_url={fallback_url}")
                    print(
                        f"[YouTube] target_page_before_url={before_url[:100] if before_url else ''}"
                    )
                    print(f"[YouTube] final_url={final_url[:100] if final_url else ''}")
                    print(f"[YouTube] verification_success={ok}")
                    if ok:
                        return f"Playing: {query}"
                    return "I selected a YouTube result but could not verify that it opened."

                return "I found multiple possible YouTube videos. Please specify the title more clearly."

            # filtered empty — try first card that has a watch URL
            print(
                f"[YouTube] All {len(candidates)} candidates were filtered out — trying first valid URL"
            )
            for c in candidates:
                url = c.get("url", "") or c.get("href", "") or ""
                if is_youtube_watch_url(url):
                    clean_url = clean_youtube_watch_url(url) or url
                    print(
                        f"[YouTube] Fallback to unfiltered card: {c['title'][:60]} → {clean_url[:80]}"
                    )
                    before_url = _get_page_url(browser_name)
                    _navigate_in_browser(clean_url, browser_name)
                    time.sleep(1.0)
                    ok, final_url = _verify_youtube_navigation(browser_name, clean_url)
                    print(f"[YouTube] selected_url={clean_url}")
                    print(
                        f"[YouTube] target_page_before_url={before_url[:100] if before_url else ''}"
                    )
                    print(f"[YouTube] final_url={final_url[:100] if final_url else ''}")
                    print(f"[YouTube] verification_success={ok}")
                    if ok:
                        return f"Playing: {query}"
                    return "I selected a YouTube result but could not verify that it opened."
            return "No suitable YouTube video found for that search, sir."
        else:
            print("[YouTube] No cards found on search page, using fallback")
    except ImportError as e:
        print(f"[YouTube] media_match/browser_intelligence not available: {e}")
    except Exception as e:
        print(f"[YouTube] Safe selection error: {e}")

    # ── Fallback: request scrape if browser bridge unavailable ──────────
    video_url = _scrape_first_video_url(query)
    target = video_url or search_url
    _navigate_in_browser(target, browser_name)

    # Post-navigation double-playback guard.
    try:
        from actions.spotify_control import spotify_control

        spotify_control({"action": "pause_if_running_only"})
    except Exception:
        pass

    action_desc = f"Playing: {query}" if video_url else f"Opened YouTube search for: {query}"
    print(f"[YouTube] {action_desc}")
    return action_desc


def _handle_summarize(params: dict, player, speak, browser_name: str) -> str:
    if not _TRANSCRIPT_OK:
        return "youtube-transcript-api not installed. Run: pip install youtube-transcript-api"
    url = params.get("url", "").strip()
    if not url and _BROWSER_BRIDGE:
        try:
            sess = get_browser_session(browser_name)
            live_url = sess.run(sess.get_url())
            if _is_valid_youtube_url(live_url):
                url = live_url
                print(f"[YouTube] 📋 Using active tab URL: {url}")
        except Exception:
            pass
    if not url:
        url = _ask_for_url("Paste the YouTube video URL:")
    if not url or not _is_valid_youtube_url(url):
        return "A valid YouTube URL is required, sir."
    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID from that URL, sir."
    if player:
        player.write_log(f"[YouTube] Summarizing: {url}")
    if speak:
        speak("Extracting transcript now, sir. One moment.")
    transcript = _get_transcript(video_id)
    if not transcript:
        return "No transcript was available for this video, sir."
    if speak:
        speak("Transcript captured. Running summarization.")
    try:
        summary = _summarize_with_gemini(transcript, url)
    except Exception as e:
        return f"Summarization failed, sir: {e}"
    if speak:
        speak(summary)
    if params.get("save", False):
        saved_path = _save_summary(summary, url)
        return f"Summary saved to Desktop: {saved_path}"
    return summary


def _handle_get_info(params: dict, player, speak, browser_name: str) -> str:
    url = params.get("url", "").strip()
    if not url and _BROWSER_BRIDGE:
        try:
            sess = get_browser_session(browser_name)
            live_url = sess.run(sess.get_url())
            if _is_valid_youtube_url(live_url):
                url = live_url
        except Exception:
            pass
    if not url:
        url = _ask_for_url("Paste the YouTube video URL:")
    if not url or not _is_valid_youtube_url(url):
        return "Please provide a valid YouTube URL, sir."
    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID from that URL, sir."
    if player:
        player.write_log(f"[YouTube] Fetching info: {url}")
    info = _scrape_video_info(video_id)
    if not info:
        return "Could not retrieve video info, sir."
    lines = [
        f"{k.capitalize()}: {info[k]}"
        for k in ("title", "channel", "views", "duration", "likes")
        if k in info
    ]
    result = "\n".join(lines)
    if speak:
        speak(f"Video info, sir. {result.replace(chr(10), '. ')}")
    return result


def _handle_trending(params: dict, player, speak) -> str:
    region = params.get("region", "US").upper()
    if player:
        player.write_log(f"[YouTube] Trending: {region}")
    trending = _scrape_trending(region=region, max_results=8)
    if not trending:
        return f"Could not retrieve trending videos for {region}, sir."
    lines = [f"Top trending in {region}:"] + [
        f"  {v['rank']}. {v['title']} — {v['channel']}" for v in trending
    ]
    result = "\n".join(lines)
    if speak:
        top3 = trending[:3]
        spoken = f"Here are the top trending videos in {region}, sir. " + ". ".join(
            f"Rank {v['rank']}: {v['title']} by {v['channel']}" for v in top3
        )
        speak(spoken)
    return result


def _handle_home_play(params: dict, browser_name: str) -> str:
    position = params.get("position", "").strip().lower().replace("_", " ")
    title = params.get("title", "").strip() or params.get("query", "").strip()

    print(f"[YouTube] Grid Selection - Position: '{position}', Title: '{title}'")

    try:
        from actions.browser_intelligence import normalize_query

        norm_title, norm_pos = normalize_query(title)
        if norm_pos:
            position = norm_pos
            title = norm_title or ""
            print(
                f"[BrowserIntel] youtube normalized: query -> position='{position}', title='{title}'"
            )
    except Exception:
        pass

    try:
        from actions.browser_intelligence import (
            choose_card,
            focus_or_open_domain,
            scan_youtube_cards,
            validate_youtube_href,
        )

        if not focus_or_open_domain(browser_name, "youtube.com", "https://www.youtube.com/"):
            return "Could not reach YouTube. Please open YouTube in your browser."

        # Track last user query for cross-turn disambiguation
        try:
            from core.browser_grid_state import set_last_user_query

            set_last_user_query(title, "youtube")
        except Exception:
            pass

        cards = scan_youtube_cards(browser_name)
        _save_youtube_grid(cards, title or "")

        if not cards:
            return "No YouTube cards found on the current page. Please wait for the page to load or open YouTube Home."

        sel = choose_card(cards, query=title, position=position, platform="youtube")

        if sel.status == "selected" and sel.card:
            href = sel.card.href
            if not validate_youtube_href(href):
                return f"Invalid YouTube URL for '{sel.card.title}'."
            nav_script = f"window.location.href = {json.dumps(href)};"
            sess = get_browser_session(browser_name)
            sess.run(sess.evaluate_background_media("youtube.com", nav_script))
            card_num = next((i + 1 for i, c in enumerate(cards) if c is sel.card), 0)
            return f"Clicked YouTube card #{card_num}: {sel.card.title}"

        if sel.status == "candidates":
            lines = [
                f"{i + 1}. {c.title}" + (f" \u2014 {c.subtitle}" if c.subtitle else "")
                for i, c in enumerate(sel.candidates)
            ]
            return (
                "I found multiple YouTube matches:\n"
                + "\n".join(lines)
                + "\nWhich one should I open?"
            )

        msg = "No confident YouTube match found."
        if title:
            msg += f" (query: '{title}')"
        if cards:
            msg += f" I found {len(cards)} visible cards."
        return msg

    except ImportError:
        print("[YouTube] browser_intelligence not available, using fallback")
    except Exception as e:
        print(f"[YouTube] Scanner crashed ({e}), using fallback")

    if title:
        script = f"""
        const links = document.querySelectorAll('a#video-title-link, a#video-title, #video-title');
        for (const link of links) {{
            if (link.textContent.toLowerCase().includes(`{title.lower()}`)) {{
                const thumb = link.closest('ytd-rich-item-renderer, ytd-video-renderer, ytd-compact-video-renderer')?.querySelector('a#thumbnail, a[href^="/watch"]');
                const target = thumb || link;
                const r = target.getBoundingClientRect();
                target.click();
                return JSON.stringify({{ok: true, msg: 'Playing requested video: {title}',
                    x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}});
            }}
        }}
        return JSON.stringify({{ok: false, msg: 'Could not find a video titled "{title}" on the grid.'}});
        """
    else:
        script = f"""
        let pos = "{position}";
        let items = Array.from(document.querySelectorAll('ytd-rich-item-renderer, ytd-video-renderer, ytd-compact-video-renderer'))
            .filter(el => el.getBoundingClientRect().width > 0 && el.querySelector('a[href^="/watch"]'));

        if (items.length === 0) return JSON.stringify({{ok: false, msg: 'No videos found on the YouTube grid.'}});

        items.sort((a, b) => {{
            let rA = a.getBoundingClientRect(); let rB = b.getBoundingClientRect();
            if (Math.abs(rA.top - rB.top) < 50) return rA.left - rB.left;
            return rA.top - rB.top;
        }});

        let row1 = items.filter(i => Math.abs(i.getBoundingClientRect().top - items[0].getBoundingClientRect().top) < 50);
        let targetIndex = 0;

        if (pos.includes("second") || (pos.includes("top") && pos.includes("middle"))) targetIndex = 1;
        else if (pos.includes("third")) targetIndex = 2;
        else if (pos.includes("top right") || pos.includes("right")) targetIndex = row1.length > 0 ? row1.length - 1 : 0;
        else if (pos.includes("bottom left")) targetIndex = row1.length;
        else if (pos.includes("bottom right")) targetIndex = (row1.length > 0 ? (row1.length * 2) - 1 : 0);

        if (targetIndex >= items.length) targetIndex = items.length - 1;

        let thumb = items[targetIndex].querySelector('a#thumbnail, a[href^="/watch"]');
        if (thumb) {{
            const r = thumb.getBoundingClientRect();
            thumb.click();
            return JSON.stringify({{ok: true, msg: 'Playing video at calculated position: ' + pos,
                x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}});
        }}
        return JSON.stringify({{ok: false, msg: 'Failed to click thumbnail at position.'}});
        """

    try:
        sess = get_browser_session(browser_name)
        result = sess.run(sess.evaluate_background_media("youtube.com", script))
        return _finalize_grid_click(result, browser_name)
    except Exception as e:
        return f"Failed to click grid item: {e}"


def _finalize_grid_click(result, browser_name: str) -> str:
    """Parse the grid-click JS result; if the synthetic click didn't actually
    start a video, retry it as a trusted human-like mouse click.

    YouTube sometimes ignores untrusted a#thumbnail .click() events, leaving
    the grid on screen while Era reports success. The JS now returns the
    card's viewport center, so we can verify /watch was reached and, if not,
    re-click through the browser's real input pipeline (utils/human_click).
    """
    raw = str(result or "")
    try:
        data = json.loads(raw)
    except Exception:
        return raw  # legacy plain-text result (e.g. "Target browser tab ... not found")
    msg = str(data.get("msg", "")) or raw
    if not data.get("ok"):
        return msg
    x, y = data.get("x"), data.get("y")
    try:
        time.sleep(1.8)  # give the synthetic click a chance to navigate
        sess = get_browser_session(browser_name)
        current = str(
            sess.run(sess.evaluate_background_media("youtube.com", "return window.location.href;"))
            or ""
        )
        if "/watch" in current or not x or not y:
            return msg
        from actions.platform_tab import get_or_open_platform_tab
        from utils.human_click import human_like_click

        tab = get_or_open_platform_tab(platform="youtube", browser=browser_name)
        if tab.page is not None and human_like_click(
            sess, tab.page, float(x), float(y), label="youtube grid"
        ):
            return msg
        print("[YouTube] Human-like click fallback could not confirm playback")
    except Exception as exc:
        print(f"[YouTube] Human-click fallback failed: {exc}")
    return msg


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────


def youtube_video(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Era One YouTube controller."""
    params = parameters or {}
    raw_action = params.get("action", "play").lower().strip()

    # Backward-compatible normalization: some older prompt/tooling uses these tokens
    if raw_action in {"youtube_play", "youtube_pause", "youtube_resume"}:
        raw_action = raw_action.replace("youtube_", "")

    # Ensure spatial actions use expected naming if older callers still pass home_play
    # (keeps existing behavior)

    browser_value = params.get("browser", _DEFAULT_BROWSER)
    browser_name = (
        str(browser_value).lower().strip() if browser_value else _default_youtube_browser()
    )

    # Normalize input strings
    action = raw_action.replace("youtube_", "")

    if player:
        player.write_log(f"[YouTube] Action: {action}")
    print(f"[YouTube] ▶️  Action: {action} | Browser: {browser_name} | Params: {params}")

    def _pause_spotify_running_only():
        """
        Pause Spotify only if it's already running. Prevents YouTube actions
        from accidentally launching Spotify, but still removes double playback.
        """
        try:
            from actions.spotify_control import spotify_control

            return spotify_control({"action": "pause_if_running_only"})
        except Exception:
            return None

    # ── NEW: SINGLE PLAYBACK ENFORCER ────────────────────────────────────────
    # Treat all YouTube play-like navigation/actions as "exclusive media play" events.
    # IMPORTANT: include grid_play; this path previously didn't silence Spotify.
    if action in ("resume", "play_pause", "play", "grid_play", "youtube_home_play", "home_play"):
        try:
            from core.media_state import enforce_single_playback

            enforce_single_playback("youtube")
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────

    # ── Playback state controls ───────────────────────────────────────────────
    if action in ("pause", "resume", "play_pause"):
        result = _handle_playback_control(action, browser_name)

        # Update central state cache based on the actual action taken.
        try:
            from core.media_state import set_status

            if action == "pause":
                set_status("youtube", "paused")
            elif action in ("resume", "play_pause", "play"):
                set_status("youtube", "playing")
        except Exception:
            pass

        return result

    # ── Routed actions ────────────────────────────────────────────────────────
    try:
        if action == "play":
            result = _handle_play(params, player, browser_name) or "Done."
            if "Playing" in result:
                try:
                    from core.browser_grid_state import set_scroll_context

                    set_scroll_context("youtube")
                except Exception:
                    pass
            return result

        # Unified routing track to guarantee advanced visual mapping
        if action in ("scroll", "scroll_down", "scroll_up"):
            try:
                from actions.browser_intelligence import focus_or_open_domain, scroll_page

                direction = "down" if action in ("scroll", "scroll_down") else "up"
                if not focus_or_open_domain(
                    browser_name, "youtube.com", "https://www.youtube.com/"
                ):
                    return "Could not reach YouTube for scrolling."
                result_dict = scroll_page(
                    platform="youtube", browser_name=browser_name, direction=direction
                )
                if result_dict.get("scrolled"):
                    count = result_dict.get("visible_cards", 0)
                    return f"Scrolled YouTube {direction}. Visible cards: {count}."
                return f"Failed to scroll YouTube: {result_dict.get('error', 'unknown error')}"
            except ImportError:
                sess = get_browser_session(browser_name)
                sess.run(sess.scroll(direction))
                return f"Scrolled YouTube {direction} (basic)."

        if action == "home":
            try:
                from actions.platform_tab import get_or_open_platform_tab

                tab_result = get_or_open_platform_tab(
                    platform="youtube", browser=browser_name or "brave"
                )
                page = tab_result.page
                if page is None:
                    return "Could not reach YouTube."
                from actions.browser_control import get_browser_session

                sess = get_browser_session(browser_name)
                sess.run(
                    page.goto(
                        "https://www.youtube.com/", wait_until="domcontentloaded", timeout=30_000
                    )
                )
                import time as _t

                _t.sleep(1.5)
                try:
                    from core.browser_grid_state import set_scroll_context

                    set_scroll_context("youtube")
                except Exception:
                    pass
                return "Opened YouTube Home."
            except ImportError:
                pass
            sess = get_browser_session(browser_name)
            sess.run(sess.go_to("https://www.youtube.com/"))
            return "Opened YouTube Home."

        if action == "grid_match":
            params["action"] = "grid_play"

        if action in ("home_play", "grid_play"):
            ordinal = params.get("ordinal")
            title = params.get("title", "").strip() or params.get("query", "").strip()
            target = params.get("target", None) or params.get("position", None)

            try:
                from actions.spotify_control import spotify_control

                spotify_control({"action": "pause_if_running_only"})
            except Exception:
                pass

            # ── Track last user query for cross-turn disambiguation ──
            try:
                from core.browser_grid_state import set_last_user_query

                # FIX: 'query' was undefined here; use the resolved title.
                set_last_user_query(title, "youtube")
            except Exception:
                pass

            # ── Read candidates from recent grid snapshot ──
            candidates = []
            try:
                from core.browser_grid_state import get_recent_grid

                snap = get_recent_grid("youtube")
                if snap and snap.candidates:
                    candidates = snap.candidates
            except Exception:
                pass

            # ── If no grid snapshot exists, try scanning the owned page ──
            if not candidates:
                try:
                    from actions.platform_tab import get_or_open_platform_tab

                    tab_result = get_or_open_platform_tab(
                        platform="youtube", browser=browser_name or "brave"
                    )
                    page = tab_result.page
                    if page is not None:
                        try:
                            from actions.browser_control import get_browser_session

                            sess = get_browser_session(browser_name)
                            scan_js = """
                            (() => {
                                const links = Array.from(document.querySelectorAll('a#video-title-link, a#video-title, a[href*="/watch?v="]'));
                                const seen = new Set();
                                return links
                                    .filter(l => { const v = new URL(l.href, location.href).searchParams.get('v'); return v && !seen.has(v) && seen.add(v); })
                                    .map(l => ({ title: (l.textContent || l.getAttribute('title') || '').trim(), href: l.href }));
                            })();
                            """
                            raw = sess.run(page.evaluate(scan_js))
                            import json as _json

                            if isinstance(raw, str):
                                raw = _json.loads(raw)
                            if raw:
                                for item in raw:
                                    from core.browser_grid_state import GridCandidate

                                    candidates.append(
                                        GridCandidate(
                                            visible_order=len(candidates) + 1,
                                            title=item.get("title", ""),
                                            url=item.get("href", ""),
                                            kind="video",
                                            safe=True,
                                            source="youtube",
                                        )
                                    )
                        except Exception:
                            pass
                except Exception:
                    pass

            # ── Safety-net ordinal: if title is a number word or "top", treat as ordinal ──
            if title and not target and not ordinal:
                try:
                    from utils.ordinal_selection import ordinal_to_index

                    inferred = ordinal_to_index(title)
                    if inferred is not None and 1 <= inferred <= 10:
                        ordinal = inferred
                        title = ""
                        print(f"[YouTube] Safety-net ordinal → ordinal #{ordinal}")
                except Exception:
                    pass

            # ── Ordinal selection ──
            if ordinal is not None:
                try:
                    ordinal_val = int(ordinal)
                except (ValueError, TypeError):
                    ordinal_val = 1
                if not candidates:
                    return "I need YouTube search results first. What should I search?"
                if ordinal_val < 1 or ordinal_val > len(candidates):
                    return f"I only found {len(candidates)} safe YouTube videos. Which one should I open?"
                selected = candidates[ordinal_val - 1]
                target_url = selected.url
                print(
                    f"[YouTube] Ordinal #{ordinal_val}: {selected.title[:60]} → {target_url[:80]}"
                )
                try:
                    from actions.platform_tab import get_or_open_platform_tab

                    tab_result = get_or_open_platform_tab(
                        platform="youtube", browser=browser_name or "brave"
                    )
                    page = tab_result.page
                    if page is None:
                        return "Could not access YouTube tab."
                    from actions.browser_control import get_browser_session

                    sess = get_browser_session(browser_name)
                    sess.run(page.goto(target_url, wait_until="domcontentloaded", timeout=30_000))
                    import time as _t

                    _t.sleep(1.0)
                    final_url = str(page.url) or ""
                    ok = "/watch" in final_url or "youtu.be/" in final_url
                    if ok:
                        try:
                            from core.browser_grid_state import set_scroll_context

                            set_scroll_context("youtube")
                        except Exception:
                            pass
                        return f"Playing: {selected.title}"
                    return "I selected a YouTube result but could not verify that it opened."
                except Exception as e:
                    return f"Failed to open YouTube video: {e}"

            # ── Title-based matching (grid_match path) ──
            if title and not target:
                if candidates:
                    from utils.media_match import score_media_candidate

                    best_score = 0.0
                    best_candidate = None
                    for c in candidates:
                        score = score_media_candidate(title, c.title, c.url, "youtube")
                        if score > best_score:
                            best_score = score
                            best_candidate = c
                    if best_candidate and best_score >= 0.6:
                        target_url = best_candidate.url
                        print(
                            f"[YouTube] Title match '{title}' → '{best_candidate.title}' (score={best_score:.2f})"
                        )
                        try:
                            from actions.platform_tab import get_or_open_platform_tab

                            tab_result = get_or_open_platform_tab(
                                platform="youtube", browser=browser_name or "brave"
                            )
                            page = tab_result.page
                            if page is None:
                                return "Could not access YouTube tab."
                            from actions.browser_control import get_browser_session

                            sess = get_browser_session(browser_name)
                            sess.run(
                                page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
                            )
                            import time as _t

                            _t.sleep(1.0)
                            final_url = str(page.url) or ""
                            ok = "/watch" in final_url or "youtu.be/" in final_url
                            if ok:
                                try:
                                    from core.browser_grid_state import set_scroll_context

                                    set_scroll_context("youtube")
                                except Exception:
                                    pass
                                return f"Playing: {best_candidate.title}"
                            return (
                                "I selected a YouTube result but could not verify that it opened."
                            )
                        except Exception as e:
                            return f"Failed to open YouTube video: {e}"
                    if best_candidate and best_score > 0:
                        lines = [f"{i + 1}. {c.title}" for i, c in enumerate(candidates[:5])]
                        msg = "I found several possible videos. Please say the video number."
                        if lines:
                            msg += "\n" + "\n".join(lines)
                        return msg
                    return f"I can see the YouTube grid, but I could not match '{title}' to a visible video. Please say the video number."
                return "I need YouTube search results first. What should I search?"

            # ── Positional selection (legacy spatial mapping) ──
            if candidates and target:
                spatial = {
                    "top left": 1,
                    "first": 1,
                    "top center": 2,
                    "top middle": 2,
                    "center": 2,
                    "middle": 2,
                    "top right": 3,
                    "third": 3,
                    "bottom left": 4,
                    "middle left": 4,
                    "lower left": 4,
                    "bottom center": 5,
                    "bottom middle": 5,
                    "lower center": 5,
                    "bottom right": 6,
                    "middle right": 6,
                    "lower right": 6,
                }

                def parse_target_to_index(t):
                    if t is None:
                        return 1
                    if isinstance(t, (int, float)):
                        try:
                            return max(1, int(t))
                        except Exception:
                            return 1
                    s = str(t).strip().lower()
                    if s in spatial:
                        return spatial[s]
                    m = re.search(r"(\d+)", s)
                    if m:
                        return max(1, int(m.group(1)))
                    return 1

                idx = parse_target_to_index(target)
                if 1 <= idx <= len(candidates):
                    selected = candidates[idx - 1]
                    target_url = selected.url
                    try:
                        from actions.platform_tab import get_or_open_platform_tab

                        tab_result = get_or_open_platform_tab(
                            platform="youtube", browser=browser_name or "brave"
                        )
                        page = tab_result.page
                        if page is None:
                            return "Could not access YouTube tab."
                        from actions.browser_control import get_browser_session

                        sess = get_browser_session(browser_name)
                        sess.run(
                            page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
                        )
                        import time as _t

                        _t.sleep(1.0)
                        final_url = str(page.url) or ""
                        ok = "/watch" in final_url or "youtu.be/" in final_url
                        if ok:
                            try:
                                from core.browser_grid_state import set_scroll_context

                                set_scroll_context("youtube")
                            except Exception:
                                pass
                            return f"Playing: {selected.title}"
                        return "I selected a YouTube result but could not verify that it opened."
                    except Exception as e:
                        return f"Failed to open YouTube video: {e}"
                return (
                    f"I only found {len(candidates)} safe YouTube videos. Which one should I open?"
                )

            # ── Fallback: no ordinal, no title, no target, no candidates ──
            if not candidates:
                return "I need YouTube search results first. What should I search?"
            return f"I found {len(candidates)} YouTube videos. Which one should I open?"

        if action == "volume_up":
            amount = params.get("amount", None)
            try:
                vol_change = float(amount) if amount is not None else 10.0
            except Exception:
                vol_change = 10.0

            from actions.browser_control import get_browser_session

            sess = get_browser_session(browser_name)
            script = f"""
                const player = document.querySelector('#movie_player');
                if (player && typeof player.setVolume === 'function') {{
                    let current = player.getVolume();
                    if (player.isMuted()) {{ player.unMute(); current = 0; }}
                    let newVol = Math.min(100, current + {vol_change});
                    player.setVolume(newVol);
                    let actual = player.getVolume();
                    return `YouTube volume increased to ${{Math.round(actual)}}%.`;
                }}
                const v = document.querySelector('video');
                if (v) {{
                    v.volume = Math.min(1.0, v.volume + ({vol_change} / 100.0));
                    let actualPct = Math.round(v.volume * 100);
                    return `YouTube volume increased to approximately ${{actualPct}}% (fallback mode).`;
                }}
                return 'No YouTube player found to adjust volume.';
            """
            return str(sess.run(sess.evaluate_background_media("youtube.com", script)))

        if action == "volume_down":
            amount = params.get("amount", None)
            try:
                vol_change = float(amount) if amount is not None else 10.0
            except Exception:
                vol_change = 10.0

            from actions.browser_control import get_browser_session

            sess = get_browser_session(browser_name)
            script = f"""
                const player = document.querySelector('#movie_player');
                if (player && typeof player.setVolume === 'function') {{
                    let current = player.getVolume();
                    let newVol = Math.max(0, current - {vol_change});
                    player.setVolume(newVol);
                    let actual = player.getVolume();
                    return `YouTube volume decreased to ${{Math.round(actual)}}%.`;
                }}
                const v = document.querySelector('video');
                if (v) {{
                    v.volume = Math.max(0.0, v.volume - ({vol_change} / 100.0));
                    let actualPct = Math.round(v.volume * 100);
                    return `YouTube volume decreased to approximately ${{actualPct}}% (fallback mode).`;
                }}
                return 'No YouTube player found to adjust volume.';
            """
            return str(sess.run(sess.evaluate_background_media("youtube.com", script)))

        if action == "next_video":
            try:
                from actions.browser_control import get_browser_session
                from actions.platform_tab import get_or_open_platform_tab

                tab_result = get_or_open_platform_tab(
                    platform="youtube", browser=browser_name or "brave"
                )
                page = tab_result.page
                if page is not None:
                    try:
                        sess = get_browser_session(browser_name)
                        sess.run(page.bring_to_front())
                        import time as _t

                        _t.sleep(0.2)
                        sess.run(page.keyboard.press("Shift+N"))
                        _t.sleep(0.5)
                        try:
                            from core.browser_grid_state import set_scroll_context

                            set_scroll_context("youtube")
                        except Exception:
                            pass
                        return "Playing next YouTube video."
                    except Exception as kb_err:
                        print(
                            f"[YouTube] Shift+N keyboard failed ({kb_err}), trying .ytp-next-button"
                        )
                        sess = get_browser_session(browser_name)
                        script = """
                            const nextBtn = document.querySelector('.ytp-next-button');
                            if (nextBtn) { nextBtn.click(); return 'Next video button clicked successfully'; }
                            return 'Next video controller button link node was not found on this frame layout';
                        """
                        return str(sess.run(sess.evaluate_background_media("youtube.com", script)))
                else:
                    sess = get_browser_session(browser_name)
                    script = """
                        const nextBtn = document.querySelector('.ytp-next-button');
                        if (nextBtn) { nextBtn.click(); return 'Next video button clicked successfully'; }
                        return 'Next video controller button link node was not found on this frame layout';
                    """
                    return str(sess.run(sess.evaluate_background_media("youtube.com", script)))
            except Exception as e:
                print(f"[YouTube] next_video error: {e}")
                sess = get_browser_session(browser_name)
                script = """
                    const nextBtn = document.querySelector('.ytp-next-button');
                    if (nextBtn) { nextBtn.click(); return 'Next video button clicked successfully'; }
                    return 'Next video controller button link node was not found on this frame layout';
                """
                return str(sess.run(sess.evaluate_background_media("youtube.com", script)))

        if action == "fullscreen":
            sess = get_browser_session(browser_name)
            script = """
                const fsBtn = document.querySelector('.ytp-fullscreen-button');
                if (fsBtn) { fsBtn.click(); return 'Fullscreen mode toggled via video canvas buttons'; }
                const v = document.querySelector('video');
                if (v) {
                    if (document.fullscreenElement) { document.exitFullscreen(); }
                    else { v.requestFullscreen(); }
                    return 'Native layout canvas fullscreen adjusted via fallback API';
                }
                return 'No clickable fullscreen nodes found inside DOM layer';
            """
            return str(sess.run(sess.evaluate_background_media("youtube.com", script)))

        if action == "summarize":
            return _handle_summarize(params, player, speak, browser_name) or "Done."

        if action == "get_info":
            return _handle_get_info(params, player, speak, browser_name) or "Done."

        if action == "trending":
            return _handle_trending(params, player, speak) or "Done."

        return f"Unknown YouTube action: '{action}'."

    except Exception as e:
        print(f"[YouTube] ❌ Error in action '{action}': {e}")
        return f"YouTube action failed, sir: {e}"
