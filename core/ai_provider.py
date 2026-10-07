# core/ai_provider.py
"""
Unified Gemini provider for Era One.

Use this module instead of repeating SDK wrappers in every action file.
Requires:
    pip install -U google-genai
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Any

from google import genai
from google.genai import types as gtypes

from core.utils import get_api_key

# Quota failover (spare Gemini key -> Claude) for EVERY generate_content call in
# the process, not only this module's. Idempotent; never breaks the import.
try:
    from core.llm.quota_failover import install as _install_quota_failover

    _install_quota_failover()
except Exception as _e:  # noqa: BLE001
    print(f"[ai_provider] quota failover not installed: {type(_e).__name__}: {_e}")

DEFAULT_FAST_MODEL = os.getenv("ERA_GEMINI_FAST_MODEL", "gemini-3.1-flash-lite")
DEFAULT_SMART_MODEL = os.getenv("ERA_GEMINI_SMART_MODEL", "gemini-3.8-flash")

# One client per API key for the whole process. Building a genai.Client sets up
# a fresh HTTP connection pool — doing that on every generate call added latency
# to every router/vision/prompt call Era makes.
_CLIENT_CACHE: dict[str, genai.Client] = {}


def get_client(api_key: str | None = None) -> genai.Client:
    key = api_key or get_api_key()
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = genai.Client(api_key=key)
        _CLIENT_CACHE[key] = client
    return client


# A 5xx from Gemini is the service being busy, not a bad request: the same call
# usually succeeds moments later. Without a retry it surfaced to the user as a
# flat "Could not generate code: 503 UNAVAILABLE. This model is currently
# experiencing high load", and the whole action was lost.
#
# Deliberately NOT retried here: 429/RESOURCE_EXHAUSTED (quota is per-key and
# belongs to core.llm.quota_failover, which rotates the key or moves to Claude —
# retrying the same exhausted key only burns time), and every 4xx, which means
# the request itself is wrong and will fail identically forever.
_TRANSIENT_MARKERS = (
    "503",
    "500",
    "502",
    "504",
    "unavailable",
    "overloaded",
    "internal error",
    "try again later",
    "deadline exceeded",
)
_QUOTA_MARKERS = ("429", "resource_exhausted", "quota")
_RETRY_ATTEMPTS = 3
_RETRY_BASE_S = 0.8


def _is_transient_server_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in _QUOTA_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class GeminiModel:
    """Small compatibility wrapper around google-genai."""

    def __init__(
        self,
        model_name: str | None = None,
        system_instruction: str | None = None,
        **kwargs: Any,
    ):
        self.model_name = (
            model_name or kwargs.get("model") or kwargs.get("model_name") or DEFAULT_FAST_MODEL
        )
        self.system_instruction = system_instruction or kwargs.get("system_instruction")

    def generate_content(self, contents: Any, *, response_mime_type: str | None = None):
        config = None
        if self.system_instruction or response_mime_type:
            config = gtypes.GenerateContentConfig(
                system_instruction=self.system_instruction,
                response_mime_type=response_mime_type,
            )
        client = get_client()
        last: BaseException | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                return client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
            except Exception as e:  # noqa: BLE001 - re-raised below
                last = e
                if attempt == _RETRY_ATTEMPTS - 1 or not _is_transient_server_error(e):
                    raise
                # Jitter so several callers retrying the same outage do not
                # line up and hammer the service in lockstep.
                delay = _RETRY_BASE_S * (2**attempt) + random.uniform(0, 0.3)
                print(
                    f"[ai_provider] {self.model_name}: transient "
                    f"{type(e).__name__} — retrying in {delay:.1f}s "
                    f"({attempt + 1}/{_RETRY_ATTEMPTS - 1})"
                )
                time.sleep(delay)
        raise last  # unreachable: the loop always returns or raises


def generate_text(
    prompt: Any,
    *,
    model: str = DEFAULT_FAST_MODEL,
    system_instruction: str | None = None,
) -> str:
    response = GeminiModel(model, system_instruction).generate_content(prompt)
    return (getattr(response, "text", "") or "").strip()


def generate_json(
    prompt: Any,
    *,
    model: str = DEFAULT_FAST_MODEL,
    system_instruction: str | None = None,
    fallback: dict | None = None,
) -> dict:
    try:
        response = GeminiModel(model, system_instruction).generate_content(
            prompt,
            response_mime_type="application/json",
        )
        text = (response.text or "").strip()
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
        return json.loads(text)
    except Exception as e:
        # NEVER swallow silently — a permanently-failing model call (e.g. an
        # exhausted free-tier quota) otherwise masquerades as "the model chose
        # the fallback", which is exactly how a 429 hid here for a whole session.
        # 429/RESOURCE_EXHAUSTED gets a distinct, un-missable line because it
        # means the model path is DOWN, not that the input was ambiguous.
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            print(
                f"[ai_provider] [QUOTA] generate_json BLOCKED by quota on {model} "
                f"— returning fallback. Callers relying on the model are degraded. ({msg[:160]})"
            )
        else:
            print(
                f"[ai_provider] generate_json failed on {model}: {type(e).__name__}: {msg[:200]} — returning fallback"
            )
        return fallback or {}


# Lightweight, dependency-free quota detection. The same marker tuple appears in
# core/vision_resilience and core/llm/adapters/gemini_generate; it is duplicated
# on purpose so any action can call this from an `except` block without dragging
# in numpy or the router.
_QUOTA_MARKERS = ("429", "RESOURCE_EXHAUSTED", "quota")


def is_quota_error(exc: BaseException) -> bool:
    """True when `exc` is an AI-service quota / rate exhaustion, not a real bug."""
    return any(m in str(exc) for m in _QUOTA_MARKERS)


def quota_message(exc: BaseException, *, what: str) -> str | None:
    """A clear, user-facing line when `exc` is a quota/429 outage, else None.

    Returning None lets the caller keep its own error text for genuine failures.
    A quota error means the model path is temporarily out of capacity — not that
    the input was bad — so surfacing the raw provider JSON blob only confuses the
    user. The spare-key -> Claude failover (core/llm/quota_failover) recovers
    this transparently when enabled; this is the last-resort message shown when
    every model path is exhausted (or failover is off, which is the default).
    """
    if is_quota_error(exc):
        return (
            f"{what} is temporarily unavailable — the AI service quota is exhausted "
            "right now. It resets on its own, so please try again in a few minutes."
        )
    return None
