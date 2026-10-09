"""Lane router: community-management extension behind the offline Autopilot gate.

The three-lane command/coding/conversation classifier lives in core.autopilot.
This compatibility router keeps the existing cm/assistant dispatch contract.
Default classification uses deterministic rules, with no Ollama or Gemini call.
Model-assisted CM classification remains an explicit ERA_LANE_PROVIDER_ORDER
opt-in. Brand-specific dispatch and missing-brand questions retain their gates.
See AUTOPILOT_ARCHITECTURE.md for the complete execution and voice boundaries.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# Reuse the brand registry + resolver that era_dispatch already trusts, so the lane router
# and the dispatcher never disagree about what counts as a brand.
import era_dispatch as ed

# The one Gemini entry point Era uses everywhere else.
from core.ai_provider import DEFAULT_FAST_MODEL, generate_json

# Routing defaults to offline rules. Model-assisted CM classification is an
# explicit opt-in through ERA_LANE_PROVIDER_ORDER (e.g. "ollama,gemini").
_DEFAULT_PROVIDER_ORDER = "rules"
_DEFAULT_LOCAL_MODEL = "qwen2.5-coder:7b"


def _provider_order() -> list[str]:
    raw = os.environ.get("ERA_LANE_PROVIDER_ORDER", _DEFAULT_PROVIDER_ORDER)
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _resolve_local_model() -> str:
    return os.environ.get("ERA_LANE_LOCAL_MODEL", _DEFAULT_LOCAL_MODEL)


def _resolve_local_timeout() -> int:
    """Bound opt-in classifier latency; default is two seconds."""
    try:
        return max(1, min(10, int(os.environ.get("ERA_LANE_LOCAL_TIMEOUT_S", "2"))))
    except (ValueError, TypeError):
        return 2


# --- The top-level lanes. Keep this list SHORT; add a lane only when a genuinely
#     separate subsystem needs to own a class of requests. -----------------------
CATEGORIES = ("cm", "assistant")
DEFAULT_CATEGORY = "assistant"  # when in doubt, keep it in the live flow (safe: gated CM work is never triggered by accident)

# CM action words — the verbs/nouns that signal community-management work. Used
# only by the deterministic fallback (the model handles the nuanced cases).
_CM_KEYWORDS = (
    "post",
    "posts",
    "publish",
    "publishing",
    "publie",
    "publier",
    "caption",
    "captions",
    "légende",
    "legende",
    "carousel",
    "carrousel",
    "reel",
    "reels",
    "story",
    "stories",
    "campaign",
    "campagne",
    "promo",
    "content plan",
    "weekly plan",
    "week's content",
    "this week's content",
    "schedule the post",
    "instagram",
    "tiktok",
    "facebook",
    "community management",
    "brand week",
    "plan the week",
    # Meta Ads (paid) — added 2026-07-20. Most ads phrasings already match a
    # word above ("boost this post", "run a Meta ad campaign"), but a bare
    # budget question ("what's the ad budget") has none of those and would
    # otherwise fall through this deterministic fallback to "assistant".
    "ads",
    "publicité",
    "publicite",
    "sponsoring",
    "sponsorisé",
    "sponsorise",
    "boost",
    "ad budget",
    "budget publicitaire",
    "campagne sponsorisée",
)

_SYSTEM_INSTRUCTION = (
    "You are the lane router, the top-level request router for a personal AI assistant "
    "named Era. Your ONLY job is to classify one user utterance into exactly one "
    "top-level category. You never answer the request or take any action.\n\n"
    "Categories:\n"
    '  "cm"        = community-management / social-media brand work that should be '
    "handed to a separate senior-CM pipeline: planning a content week, building a "
    "marketing/promo campaign, writing captions, producing or scheduling or "
    "publishing Instagram/TikTok/Facebook posts, reels, carousels, or stories. "
    "This is CM work whether or not a specific brand is named in the request.\n"
    '  "assistant" = everything else Era does itself: controlling the computer or '
    "apps, playing or controlling media (Spotify/Netflix/YouTube), browsing or "
    "searching the web, answering questions, chatting, remembering or recalling "
    "facts, reminders, general help.\n\n"
    "Rules:\n"
    "- Choose exactly one category.\n"
    '- Choose "cm" whenever the request is clearly community-management / '
    "social-content work (a content plan/week, a campaign, captions, or producing/"
    "scheduling/publishing posts/reels/carousels/stories) — EVEN IF no specific "
    'brand is named. A missing brand does NOT make it "assistant"; the pipeline '
    'downstream will ask which brand. Set "brand" to null in that case.\n'
    "- General questions ABOUT marketing/social media, or casual chat, are "
    '"assistant".\n'
    '- Only fall back to "assistant" if you are genuinely unsure it is CM work '
    "at all.\n"
    "- Extract the brand key if one of the KNOWN BRANDS is clearly referenced; "
    "otherwise use null. This does NOT change the category.\n\n"
    "Respond with STRICT JSON only, no prose, of the form:\n"
    '{"category": "cm"|"assistant", "brand": <brand_key or null>, '
    '"confidence": <0.0-1.0>, "reason": <short string>}'
)


@dataclass
class RouteDecision:
    """The whole result of a routing call. `text` is echoed back so a caller can
    pass the decision straight to the chosen lane without re-threading the input."""

    category: str
    text: str
    brand: str | None = None
    confidence: float = 0.0
    reason: str = ""
    source: str = "model"  # "model" | "fallback" — for logging/telemetry
    known_brands: list[str] = field(default_factory=list)

    @property
    def is_cm(self) -> bool:
        return self.category == "cm"

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "brand": self.brand,
            "confidence": self.confidence,
            "reason": self.reason,
            "source": self.source,
            "text": self.text,
        }


def _brand_catalog(cfg: dict | None = None) -> tuple[dict, list[str]]:
    """Return (brands.yaml cfg, list of human-readable brand names+aliases) for the
    prompt and for the fallback heuristic. Never raises — a missing/broken registry
    just means 'no known brands', which biases everything to 'assistant' (safe)."""
    try:
        cfg = cfg or ed.load_brands()
    except Exception:
        return {}, []
    names: list[str] = []
    for key, b in (cfg.get("brands") or {}).items():
        names.append(key.replace("_", " "))
        dn = (b.get("display_name") or "").strip()
        if dn:
            names.append(dn)
        names += [a for a in (b.get("aliases") or []) if a]
    # De-dup preserving order, drop very short/ambiguous tokens (< 2 chars).
    seen, out = set(), []
    for n in names:
        nl = n.lower().strip()
        if len(nl) >= 2 and nl not in seen:
            seen.add(nl)
            out.append(n)
    return cfg, out


def _fallback(text: str, cfg: dict) -> RouteDecision:
    """No-model classification. Conservative: only 'cm' when BOTH a known brand and
    a CM action word are present. Otherwise 'assistant'. This is what runs when the
    Gemini call fails, so a blip can never misroute — it can only under-route to the
    safe lane, where Era can still ask the user."""
    t = (text or "").lower()
    brand = ed.resolve_brand(text, cfg)  # exactly-one-match or None (same rule as dispatch)
    has_cm_word = any(kw in t for kw in _CM_KEYWORDS)
    if brand and has_cm_word:
        return RouteDecision(
            category="cm",
            text=text,
            brand=brand,
            confidence=0.5,
            reason="fallback: known brand + CM keyword",
            source="fallback",
        )
    return RouteDecision(
        category="assistant",
        text=text,
        brand=brand,
        confidence=0.4,
        reason="fallback: no clear brand+CM signal",
        source="fallback",
    )


def _coerce(raw: dict, text: str, known: list[str]) -> RouteDecision | None:
    """Validate the model's JSON into a RouteDecision, or None if it's unusable
    (so the caller falls back). Guards against hallucinated categories/brands."""
    if not isinstance(raw, dict):
        return None
    cat = str(raw.get("category", "")).strip().lower()
    if cat not in CATEGORIES:
        return None
    brand = raw.get("brand")
    brand = str(brand).strip() if brand not in (None, "", "null") else None
    # Only accept a brand key the registry actually knows; else drop it (keeps the
    # 'cm' category but forces era_dispatch to re-resolve / ask).
    try:
        cfg = ed.load_brands()
        if brand and brand not in (cfg.get("brands") or {}):
            brand = None
    except Exception:
        brand = brand  # registry unreadable; trust the model's string as-is
    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reason = str(raw.get("reason", "")).strip()[:200]
    return RouteDecision(
        category=cat,
        text=text,
        brand=brand,
        confidence=conf,
        reason=reason or "model",
        source="model",
        known_brands=known,
    )


def _build_prompt(text: str, known: list[str]) -> str:
    brands_block = ", ".join(known) if known else "(none configured)"
    return (
        f"KNOWN BRANDS (brand key -> also recognized as): {brands_block}\n\n"
        f"User utterance:\n{text}\n\n"
        f"Classify it. Respond with the strict JSON described in your instructions."
    )


def _classify_local(prompt: str, text: str, known: list[str]) -> RouteDecision | None:
    """Classify via the local Ollama model. Returns a validated RouteDecision
    (source='local') or None so route() falls through to the next provider when
    Ollama is down / returns junk. Never raises."""
    try:
        from core.text_ollama import classify_json_with_ollama
        from core.vision_config import resolve_ollama_base_url

        raw = classify_json_with_ollama(
            prompt=prompt,
            system=_SYSTEM_INSTRUCTION,
            model=_resolve_local_model(),
            base_url=resolve_ollama_base_url(),
            timeout_s=_resolve_local_timeout(),
        )
    except Exception as e:
        print(f"[LANE] local (ollama) classify unavailable, trying next provider: {e}")
        return None
    decision = _coerce(raw, text, known)
    if decision is not None:
        decision.source = "local"
    return decision


def _classify_gemini(prompt: str, text: str, known: list[str], model: str) -> RouteDecision | None:
    """Classify via Gemini. Returns a validated RouteDecision (source='model') or
    None (quota/error/junk — generate_json already logs a [QUOTA]/error line)."""
    raw = generate_json(prompt, model=model, system_instruction=_SYSTEM_INSTRUCTION, fallback={})
    return _coerce(raw, text, known)


def route(text: str, *, model: str = DEFAULT_FAST_MODEL) -> RouteDecision:
    """Classify one utterance into a top-level lane. Never raises: it tries each
    configured provider in order (local Ollama first by default, then Gemini) and,
    if all of them fail, returns a conservative deterministic decision. A
    `source="fallback"` result means EVERY model provider was unreachable — not
    that the input was ambiguous."""
    text = (text or "").strip()
    cfg, known = _brand_catalog()
    if not text:
        return _log(
            RouteDecision(
                category=DEFAULT_CATEGORY,
                text=text,
                confidence=1.0,
                reason="empty input",
                source="fallback",
                known_brands=known,
            )
        )

    prompt = _build_prompt(text, known)
    for provider in _provider_order():
        if provider == "rules":
            decision = _fallback(text, cfg)
            decision.category = "assistant"
            decision.reason = "rules: no clear CM production order"
            # Clear brandless production orders keep the existing brand-ask
            # pipeline. General questions ABOUT marketing remain conversation.
            import re

            if re.match(
                r"^(?:prepare|create|produce|publish|schedule|build|plan|write)\b", text, re.I
            ) and re.search(
                r"\b(?:content plan|content week|campaigns?|captions?|carousels?|reels?|posts?|week)\b",
                text,
                re.I,
            ):
                decision.category = "cm"
                decision.reason = "rules: explicit CM production order"
            decision.source = "rules"
        elif provider == "ollama":
            decision = _classify_local(prompt, text, known)
        elif provider == "gemini":
            decision = _classify_gemini(prompt, text, known, model)
        else:
            continue
        if decision is not None:
            return _log(decision)

    # Every model provider failed — conservative deterministic heuristic.
    return _log(_fallback(text, cfg))


def _log(decision: RouteDecision) -> RouteDecision:
    """Emit one line per routing decision so live runs are diagnosable: crucially,
    `source` distinguishes a real model classification from the deterministic
    fallback (which requires BOTH a known brand and a CM keyword). If CM work keeps
    landing as 'assistant' via source=fallback, the model path is unreachable —
    not a prompt problem."""
    print(
        f"[LANE] route: category={decision.category} brand={decision.brand} "
        f"source={decision.source} confidence={decision.confidence:.2f} "
        f"text={decision.text[:60]!r}"
    )
    return decision


def warm_up() -> None:
    """Preload the local classifier model so the user's FIRST real CM request
    doesn't eat the cold-load latency (a 7B model can take 30-60s to load, which
    otherwise times out into the heuristic fallback). Safe to call from a daemon
    thread at Era startup — never raises, and is a no-op when 'ollama' isn't in
    the provider order. Warm calls thereafter are ~1s and keep_alive holds the
    model resident."""
    if "ollama" not in _provider_order():
        return
    try:
        _, known = _brand_catalog()
        d = _classify_local(_build_prompt("warm up", known), "warm up", known)
        print(f"[LANE] warm-up complete (local model ready: {d is not None})")
    except Exception as e:
        print(f"[LANE] warm-up skipped: {e}")


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "prepare this week's content for Delta Cuisine"
    d = route(q)
    import json

    print(json.dumps(d.as_dict(), ensure_ascii=False, indent=2))
