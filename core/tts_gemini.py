"""Bounded sentence-only Gemini TTS recovery; independent of the Live session."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
import wave

import aiohttp

from core.utils import get_api_key
from core.voice_mode import gemini_voice_name

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
_MODEL = "gemini-3.8-flash-lite-tts"
_MAX_RESPONSE = 16 * 1024 * 1024
_TIMEOUT = 12.0
_retry_at = 0.0


async def _post(payload: dict, key: str) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as session:
        async with session.post(
            _ENDPOINT,
            json=payload,
            headers={"x-goog-api-key": key},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE:
                    raise ValueError("speech response exceeds size limit")
            return json.loads(body)


def decode_audio(response: dict) -> bytes:
    if response.get("status") != "completed":
        raise ValueError("speech interaction did not complete")
    blocks = [
        block
        for step in response.get("steps", [])
        if step.get("type") == "model_output"
        for block in step.get("content", [])
        if block.get("type") == "audio"
    ]
    if len(blocks) != 1 or blocks[0].get("mime_type") != "audio/wav":
        raise ValueError("expected one complete WAV speech block")
    data = blocks[0].get("data", "")
    if not isinstance(data, str) or len(data) > _MAX_RESPONSE:
        raise ValueError("invalid audio payload")
    decoded = base64.b64decode(data, validate=True)
    with wave.open(io.BytesIO(decoded), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (
            1,
            2,
            24000,
            "NONE",
        ):
            raise ValueError("incompatible speech format")
        count = wav.getnframes()
        pcm = wav.readframes(count)
        if count < 1 or len(pcm) != count * 2:
            raise ValueError("empty or truncated speech")
        return pcm


async def synthesize(text: str) -> bytes:
    """Recite one unsaid sentence. Failure returns no playable phrase prefix."""
    global _retry_at
    text = text.strip()
    if not text or len(text) > 4096 or time.monotonic() < _retry_at:
        return b""
    try:
        key = get_api_key()
        payload = {
            "model": _MODEL,
            "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
            "response_format": {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000},
            "generation_config": {"speech_config": [{"voice": gemini_voice_name()}]},
            "stream": False,
            "store": False,
        }
        async with asyncio.timeout(_TIMEOUT):
            return decode_audio(await _post(payload, key))
    except Exception as exc:
        _retry_at = time.monotonic() + 30.0
        detail = exc.args[0] if exc.args else type(exc).__name__
        print(f"[TTS] Gemini sentence recovery unavailable: {detail} ({type(exc).__name__}).")
        return b""
