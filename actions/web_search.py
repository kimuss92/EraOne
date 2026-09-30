# web_search.py
import json
import time

# FEATURE-INSPIRE BRIDGE (2026-09-14) — allows /inspire from agency. Declared
# here rather than mid-file: it was appended at the end when the feature landed,
# but feature_inspire does not import web_search, so there is no circular-import
# reason to defer it.
from actions.feature_inspire import inspire as _inspire
from core.utils import get_base_dir

BASE_DIR = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Gemini grounded search shares Era's main key with the live voice session, so a
# busy moment shows up here as a 429/503/timeout rather than a permanent fault.
# One short retry turns most of those back into an answer instead of a fallback.
_TRANSIENT_MARKERS = (
    "429",
    "resource_exhausted",
    "quota",
    "rate limit",
    "500",
    "502",
    "503",
    "504",
    "unavailable",
    "internal",
    "deadline",
    "timeout",
    "timed out",
    "connection",
)


def get_api_key() -> str:
    with open(API_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _is_transient(err: Exception) -> bool:
    text = f"{type(err).__name__} {err}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _gemini_search_once(query: str) -> str:
    from google import genai

    client = genai.Client(api_key=get_api_key())
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _gemini_search(query: str) -> str:
    try:
        return _gemini_search_once(query)
    except Exception as e:
        if not _is_transient(e):
            raise
        print(f"[WebSearch] ⏳ Transient Gemini error ({e}) — one retry.")
        time.sleep(1.5)
        return _gemini_search_once(query)


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    from importlib import import_module

    # `duckduckgo_search` was renamed to `ddgs`. The legacy package still
    # imports and still exposes DDGS, but its backend is dead: every query
    # returns zero results with no error. So an ImportError fallback onto it is
    # a silent no-op fallback — the reason a Gemini outage read to the user as
    # "search is broken" instead of "here are DuckDuckGo's results". Require the
    # live package and say so plainly when it is missing.
    try:
        ddg_module = import_module("ddgs")
    except ImportError as e:
        raise RuntimeError(
            "the 'ddgs' package is not installed (pip install ddgs), so there is "
            "no search fallback available"
        ) from e

    DDGS = ddg_module.DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url": r.get("href", ""),
                }
            )
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):
            lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _compare(items: list[str], aspect: str) -> str:
    query = f"Compare {', '.join(items)} in terms of {aspect}. Give specific facts and data."
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — falling back to DDG")

    # DDG fallback: fetch results per item and merge
    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
    return "\n".join(lines)


def web_search(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query = params.get("query", "").strip()
    mode = params.get("mode", "search").lower().strip()
    items = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query, sir."

    if items and mode != "compare":
        mode = "compare"

    if player:
        player.write_log(f"[Search] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 Query: {query!r}  Mode: {mode}")

    try:
        if mode == "compare" and items:
            print(f"[WebSearch] 📊 Comparing: {items}")
            result = _compare(items, aspect)
            print("[WebSearch] ✅ Compare done.")
            return result

        print("[WebSearch] 🌐 Trying Gemini...")
        try:
            result = _gemini_search(query)
            print("[WebSearch] ✅ Gemini OK.")
            return result
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini failed ({e}) — trying DDG...")
            results = _ddg_search(query)
            print(f"[WebSearch] ✅ DDG: {len(results)} result(s).")
            if not results:
                # Say WHY it came back empty. A bare "no results" reads as "the
                # web has nothing on this", when the real story is that the
                # primary backend is down and the fallback found nothing either.
                return (
                    f"Search came up empty for: {query}\n"
                    f"(Google grounding was unavailable — {e} — and the DuckDuckGo "
                    f"fallback returned nothing.)"
                )
            return _format_ddg(query, results)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed, sir: {e}"


def inspire(cmd_text=""):
    """Public entry: search web for similar AI-agent projects, return feature ideas."""
    return _inspire(query=cmd_text or "python desktop AI agent voice automation 2026")
