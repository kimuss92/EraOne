from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request


def check_ollama_availability(base_url: str, timeout_s: float = 1.0) -> bool:
    """Return True if the Ollama daemon answers. Kept short: this gates the fast path."""
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/api/tags",
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except Exception:
        return False


def warm_up_ollama_vision(model: str, base_url: str, keep_alive: str = "30m") -> bool:
    """Preload the vision model so the user's FIRST screen look doesn't eat the
    cold-load cost. Measured on qwen2.5vl:3b: cold ~13.7s vs warm ~2.2s — the entire
    gap is model load, not inference. An empty prompt makes Ollama load the model and
    return immediately. Safe to call from a daemon thread; never raises.
    """
    payload = json.dumps(
        {
            "model": model,
            "prompt": "",
            "keep_alive": keep_alive,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status == 200
    except Exception:
        return False


def analyze_image_with_ollama(
    image_bytes: bytes,
    prompt: str,
    model: str,
    base_url: str,
    timeout_s: int,
    keep_alive: str = "30m",
) -> str:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "images": [b64_image],
            "stream": False,
            # Hold the model resident between screen looks, so only the first call per
            # idle window pays cold-load. Mirrors core.text_ollama. Without this, other
            # local models (coder, gemma) evict it and every look pays ~11s extra.
            "keep_alive": keep_alive,
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
        return data.get("response", "")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama vision request failed: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Ollama vision failed: {e}") from e
