from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# One context size for EVERY text call to Ollama. Ollama reloads a resident model
# whenever a request asks for a different num_ctx, so the lane router (4k default) and the
# local fallback brain (12k, see core/llm/adapters/ollama_chat.py) alternating on
# the same qwen model cost a ~10s reload each switch. Measured 2026-09-23.
SHARED_NUM_CTX = 12288


def _keep_alive(value: str | None) -> str:
    return value or os.environ.get("OLLAMA_KEEP_ALIVE", "5m")


def _admit(model: str, base_url: str) -> None:
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        from core.llm.resource_guard import admit_cold_load

        admission = admit_cold_load(model, base_url)
    except Exception:  # noqa: BLE001 - Ollama remains the hard memory boundary
        return
    if not admission.allowed:
        raise RuntimeError(admission.reason)


def classify_json_with_ollama(
    prompt: str,
    system: str,
    model: str,
    base_url: str,
    timeout_s: int,
    keep_alive: str | None = None,
) -> dict:
    """One-shot JSON classification via a local Ollama model.

    Mirrors core.vision_ollama's stdlib-only (urllib, no extra deps) style, but
    for text: uses `/api/generate` with `format: "json"` so Ollama constrains the
    output to a JSON object, and a `system` instruction. Returns the parsed dict.
    Raises RuntimeError on transport/parse failure so the caller can fall through
    to another provider — it NEVER silently returns an empty/партial result.
    """
    _admit(model, base_url)
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "system": system,
            "format": "json",  # Ollama constrains generation to valid JSON
            "stream": False,
            # Keep the model resident after use so only the FIRST call per idle window
            # pays the cold-load cost — warm calls are ~1s. See lane_router.warm_up().
            "keep_alive": _keep_alive(keep_alive),
            # temperature 0: deterministic classification.
            "options": {"temperature": 0, "num_ctx": SHARED_NUM_CTX},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
        data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama request failed (is it running at {base_url}?): {e}") from e
    except Exception as e:
        raise RuntimeError(f"Ollama request failed: {e}") from e

    text = (data.get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ollama returned non-JSON: {text[:160]!r}") from e


def generate_text_with_ollama(
    prompt: str,
    model: str,
    base_url: str,
    timeout_s: int = 180,
    system: str | None = None,
    keep_alive: str | None = None,
) -> str:
    """One-shot free-form text generation via a local Ollama model.

    Unlike classify_json_with_ollama this does not constrain output to JSON.
    Raises RuntimeError on transport failure or empty output so callers can
    surface the original error instead of a blank answer.
    """
    _admit(model, base_url)
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": _keep_alive(keep_alive),
        "options": {"num_ctx": SHARED_NUM_CTX},
    }
    if system:
        body["system"] = system
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Ollama request failed (is it running at {base_url}?): {e}") from e
    text = (data.get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response")
    return text
