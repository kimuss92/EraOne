from __future__ import annotations

import os

from core.utils import load_config


def resolve_vision_api_key() -> str:
    cfg = load_config()
    for key in ("gemini_vision_api_key", "GEMINI_VISION_API_KEY"):
        val = cfg.get(key, "")
        if val:
            return val
    env_key = os.environ.get("GEMINI_VISION_API_KEY", "")
    if env_key:
        return env_key
    for key in ("gemini_api_key", "GEMINI_API_KEY"):
        val = cfg.get(key, "")
        if val:
            return val
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key:
        return env_key
    raise RuntimeError(
        "No Gemini API key found for vision "
        "(checked gemini_vision_api_key, GEMINI_VISION_API_KEY, "
        "gemini_api_key, GEMINI_API_KEY)"
    )


def resolve_vision_model() -> str:
    cfg = load_config()
    for key in ("GEMINI_VISION_MODEL", "gemini_vision_model"):
        val = cfg.get(key, "")
        if val:
            return val
    return os.environ.get("GEMINI_VISION_MODEL", "gemini-2.5-flash")


def resolve_ollama_model() -> str:
    cfg = load_config()
    for key in ("ollama_vision_model", "OLLAMA_VISION_MODEL"):
        val = cfg.get(key, "")
        if val:
            return val
    return os.environ.get("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")


def resolve_ollama_base_url() -> str:
    cfg = load_config()
    for key in ("ollama_base_url", "OLLAMA_BASE_URL"):
        val = cfg.get(key, "")
        if val:
            return val
    return os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")


def resolve_ollama_timeout() -> int:
    """Give up on Ollama and fall through to the next provider after this long.

    25s: warm calls land in ~1-2s and a cold load costs ~4s, so anything past this
    means Ollama is wedged, not slow. The old 60s was sized for Ollama-as-last-resort,
    where there was nothing left to fall through to.
    """
    cfg = load_config()
    for key in ("ollama_vision_timeout_s", "OLLAMA_VISION_TIMEOUT_S"):
        val = cfg.get(key, 0)
        if val:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    try:
        return int(os.environ.get("OLLAMA_VISION_TIMEOUT_S", "25"))
    except (ValueError, TypeError):
        return 25


def resolve_ollama_keep_alive() -> str:
    """How long Ollama holds the vision model in memory after a call.

    Cold load is the whole latency story (~13.7s cold vs ~2.2s warm on qwen2.5vl:3b),
    so this is the single most important vision-speed setting.
    """
    cfg = load_config()
    for key in ("ollama_vision_keep_alive", "OLLAMA_VISION_KEEP_ALIVE"):
        val = cfg.get(key, "")
        if val:
            return str(val)
    return os.environ.get("OLLAMA_VISION_KEEP_ALIVE", "30m")


def resolve_openai_api_key() -> str | None:
    cfg = load_config()
    for key in ("OPENAI_API_KEY", "openai_api_key"):
        val = cfg.get(key, "")
        if val:
            return val
    return os.environ.get("OPENAI_API_KEY") or None


def resolve_openai_model() -> str:
    cfg = load_config()
    for key in ("openai_vision_model", "OPENAI_VISION_MODEL"):
        val = cfg.get(key, "")
        if val:
            return val
    return os.environ.get("OPENAI_VISION_MODEL", "gpt-4.1-mini")


def resolve_openai_timeout() -> int:
    cfg = load_config()
    for key in ("openai_vision_timeout_s", "OPENAI_VISION_TIMEOUT_S"):
        val = cfg.get(key, 0)
        if val:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    try:
        return int(os.environ.get("OPENAI_VISION_TIMEOUT_S", "40"))
    except (ValueError, TypeError):
        return 40


def resolve_vision_provider_order() -> list[str]:
    """Vision providers in priority order. First available one takes the job and finishes it.

    Ollama leads because it is local: no quota, no 429, no overload. Gemini/OpenAI
    quota exhaustion is only visible by making the real call, so a remote-first order
    pays a full request timeout to rediscover it on every screen look.
    """
    cfg = load_config()
    order = cfg.get("vision_provider_order", ["ollama", "gemini", "openai"])
    return [p.lower().strip() for p in order]
