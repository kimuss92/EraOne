"""Fish Audio TTS client — Era's PRIMARY speech synthesis backend.

Contract is byte-for-byte the same as core/tts_voicebox.py so the two are
interchangeable behind core/tts_router.py: hand it text, get back raw **int16
mono PCM at RECEIVE_SAMPLE_RATE (24 kHz)**, ready for ``audio_in_queue``.

API (verified against docs.fish.audio): ``POST https://api.fish.audio/v1/tts``
with ``Authorization: Bearer <key>``, an optional ``model`` header, and a JSON
body carrying ``text``. We ask for ``format="wav"`` at ``sample_rate=24000`` so
the payload already matches Era's playback rate and wav_to_pcm24k's resample is
a no-op.

SECURITY: the API key is NEVER hardcoded and never logged. It is resolved at
call time from config/api_keys.json (``fish_audio.api_key`` or
``fish_audio_api_key``) or the ``FISH_AUDIO_API_KEY`` environment variable —
the same layering core/voice_mode.py::get_voicebox_config uses.

Degrade, never raise: every failure returns b"" and records WHY, so the router
can fall back to Gemini instead of Era going silent. A 401/402 (bad key /
out of credit) sets a long cooldown — retrying those per sentence is pointless
and just burns latency on every utterance.
"""

from __future__ import annotations

import os
import time

from app.audio_config import RECEIVE_SAMPLE_RATE
from core.tts_voicebox import wav_to_pcm24k

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp is in requirements.txt
    aiohttp = None  # type: ignore[assignment]

DEFAULT_ENDPOINT = "https://api.fish.audio/v1/tts"
# Free-tier model by default — the whole reason Fish is primary here. Any of
# s1 / s2-pro / s2.1-pro / s2.1-pro-free / drama-3-preview is accepted.
DEFAULT_MODEL = "s2.1-pro-free"
SYNTH_TIMEOUT_S = 45.0

# Cooldowns after a failure, so a dead key does not cost a network round trip on
# every single sentence. Auth/billing problems will not fix themselves in 30s.
COOLDOWN_AUTH_S = 900.0  # 401 / 402
COOLDOWN_TRANSIENT_S = 30.0  # 503, timeout, network


def _log(msg: str) -> None:
    print(f"[FISH] {msg}")


def get_fish_config() -> dict:
    """Resolve Fish Audio settings: config file -> env var -> default.

    The api_key is returned for immediate use and must never be logged or
    persisted anywhere else.
    """
    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    fa = cfg.get("fish_audio", {}) if isinstance(cfg.get("fish_audio"), dict) else {}

    def _resolve(key: str, env: str, default):
        val = fa.get(key)
        if val in (None, ""):
            val = cfg.get(f"fish_audio_{key}")
        if val in (None, ""):
            val = os.environ.get(env) or None
        return default if val in (None, "") else val

    return {
        "api_key": str(_resolve("api_key", "FISH_AUDIO_API_KEY", "") or ""),
        "model": str(_resolve("model", "FISH_AUDIO_MODEL", DEFAULT_MODEL)),
        # Voice model id from fish.audio. Empty = the model's default voice.
        "reference_id": str(_resolve("reference_id", "FISH_AUDIO_REFERENCE_ID", "") or ""),
        "endpoint": str(_resolve("endpoint", "FISH_AUDIO_ENDPOINT", DEFAULT_ENDPOINT)),
        "latency": str(_resolve("latency", "FISH_AUDIO_LATENCY", "normal")),
    }


def is_configured() -> bool:
    """True when an API key is available. No key = Fish is simply not in play."""
    try:
        return bool(get_fish_config()["api_key"])
    except Exception:
        return False


class FishAudioClient:
    """Async Fish Audio TTS client. One long-lived aiohttp session."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cooldown_until = 0.0
        self.last_error = ""

    # ── Session ──────────────────────────────────────────────────────────────
    async def _get_session(self):
        if aiohttp is None:
            _log("aiohttp not installed; Fish Audio unavailable")
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    # ── Cooldown ─────────────────────────────────────────────────────────────
    def in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def _cool(self, seconds: float, reason: str) -> None:
        self._cooldown_until = time.time() + seconds
        self.last_error = reason
        _log(f"{reason} — pausing Fish for {int(seconds)}s; Gemini covers the gap.")

    def reset_cooldown(self) -> None:
        self._cooldown_until = 0.0

    # ── Synthesis ────────────────────────────────────────────────────────────
    async def synthesize(self, text: str) -> bytes:
        """Synthesize `text` -> int16 mono PCM @ 24 kHz. b"" on ANY failure."""
        text = (text or "").strip()
        if not text:
            return b""
        if self.in_cooldown():
            return b""

        cfg = get_fish_config()
        api_key = cfg["api_key"]
        if not api_key:
            self._cool(COOLDOWN_AUTH_S, "no API key configured")
            return b""

        session = await self._get_session()
        if session is None:
            self._cool(COOLDOWN_TRANSIENT_S, "aiohttp unavailable")
            return b""

        body = {
            "text": text,
            "format": "wav",
            "sample_rate": RECEIVE_SAMPLE_RATE,
            "latency": cfg["latency"],
        }
        if cfg["reference_id"]:
            body["reference_id"] = cfg["reference_id"]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "model": cfg["model"],
        }

        try:
            async with session.post(
                cfg["endpoint"],
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=SYNTH_TIMEOUT_S),
            ) as r:
                if r.status != 200:
                    detail = (await r.text())[:200]
                    if r.status in (401, 403):
                        self._cool(COOLDOWN_AUTH_S, f"auth rejected (HTTP {r.status})")
                    elif r.status == 402:
                        self._cool(COOLDOWN_AUTH_S, "out of Fish Audio credit (HTTP 402)")
                    else:
                        self._cool(COOLDOWN_TRANSIENT_S, f"HTTP {r.status}: {detail}")
                    return b""
                audio = await r.read()
        except TimeoutError:
            self._cool(COOLDOWN_TRANSIENT_S, f"timed out after {SYNTH_TIMEOUT_S}s")
            return b""
        except Exception as e:
            self._cool(COOLDOWN_TRANSIENT_S, f"request failed: {type(e).__name__}")
            return b""

        pcm = wav_to_pcm24k(audio)
        if not pcm:
            self._cool(COOLDOWN_TRANSIENT_S, "returned audio could not be decoded")
            return b""
        self.last_error = ""
        return pcm


# Module-level singleton, mirroring VOICEBOX / VOICE_CFG.
FISH = FishAudioClient()


# ──────────────────────────────────────────────────────────────────────────────
# Voice cloning (model creation)
# ──────────────────────────────────────────────────────────────────────────────
# POST https://api.fish.audio/model, multipart/form-data. Verified against the
# live OpenAPI schema (api.fish.audio/openapi.json):
#   required: type="tts", title, train_mode="fast", voices=<file(s)>
#   optional: description, texts (skip it and Fish runs ASR itself), visibility
#             (default "private"), enhance_audio_quality (default true)
#   success:  HTTP 201 with the model in the body; "_id" is the reference_id
#             that /v1/tts then speaks with.
CLONE_ENDPOINT = "https://api.fish.audio/model"


async def create_voice_model(
    audio_path,
    title: str,
    description: str | None = None,
    texts: str | None = None,
    visibility: str = "private",
    enhance_audio_quality: bool = True,
) -> dict:
    """Clone a voice from a reference clip. Returns {"id": ...} or {"error": ...}.

    Degrades like the rest of this module: never raises, so a failed clone is a
    reported error rather than a traceback. ``visibility`` defaults to private —
    a cloned voice is personal data and must not be published by accident.
    """
    from pathlib import Path

    path = Path(audio_path)
    if not path.is_file():
        return {"error": f"file not found: {path}"}

    cfg = get_fish_config()
    if not cfg["api_key"]:
        return {"error": "no API key configured"}
    if aiohttp is None:
        return {"error": "aiohttp not installed"}

    vis = visibility if visibility in ("private", "unlist", "public") else "private"

    try:
        data = aiohttp.FormData()
        data.add_field("type", "tts")
        data.add_field("title", title)
        data.add_field("train_mode", "fast")
        data.add_field("visibility", vis)
        data.add_field("enhance_audio_quality", "true" if enhance_audio_quality else "false")
        if description:
            data.add_field("description", description)
        if texts:
            data.add_field("texts", texts)
        data.add_field(
            "voices",
            path.read_bytes(),
            filename=path.name,
            content_type="audio/wav",
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                CLONE_ENDPOINT,
                data=data,
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                body = await r.text()
                if r.status not in (200, 201):
                    return {"error": f"HTTP {r.status}: {body[:300]}"}
                try:
                    import json as _json

                    payload = _json.loads(body)
                except Exception:
                    return {"error": f"unparseable response: {body[:200]}"}
    except TimeoutError:
        return {"error": "cloning request timed out"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    model_id = payload.get("_id") or payload.get("id") or ""
    if not model_id:
        return {"error": f"no model id in response: {str(payload)[:200]}"}
    return {"id": str(model_id), "title": payload.get("title", title), "visibility": vis}
