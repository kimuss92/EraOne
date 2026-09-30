r"""
music_generate.py — generate an original background track via Google Lyria
RealTime (Gemini API), for the CM's automated Reel-assembly pipeline.

WHY THIS EXISTS
  The CM's automated Reel pipeline (see the vault's
  Automated_Reel_Assembly_Pipeline.md) needs a music source that isn't
  Magnific — Karim rejected Magnific's audio_music_generate directly on
  2026-07-20 ("use anything else"). This calls Google's Lyria RealTime model
  through the same google-genai SDK Era already depends on, using a
  DEDICATED API key (core.utils.get_music_api_key()) so a content-production
  run never competes for the same Gemini quota Era's own voice/vision Live
  API needs — that quota has a documented history of exhaustion.

WHAT THIS IS NOT
  Lyria RealTime is an *interactive, steerable* generation API (send prompts,
  get a continuous stream, adjust live) — not a "give me one finished N-second
  clip" endpoint. This script wraps it into that shape anyway: connect, send
  one prompt + config, play, buffer raw PCM until the requested duration is
  covered, stop, write a WAV. That's a deliberate simplification for a
  one-shot background-music use case; it does not use Lyria's live-steering
  capability at all.

USAGE
  python music_generate.py --prompt "warm acoustic guitar, upbeat, 100bpm" \
      --duration 30 --out track.wav
  python music_generate.py --prompt "..." --duration 45 --out track.mp3 --json

  Requires ffmpeg on PATH only when --out ends in something other than .wav
  (the raw stream is always PCM; non-wav output is transcoded after capture).

OPTIONAL GENERATION CONTROLS
  --bpm INT             (Lyria accepts a limited discrete set; invalid values
                          are rejected by the API, not validated here)
  --scale STR           e.g. "C_MAJOR_A_MINOR" — see LiveMusicGenerationConfig
  --temperature FLOAT
  --density FLOAT
  --brightness FLOAT
  --mute-bass / --mute-drums / --only-bass-and-drums

Exit code 0 on success (path on stdout, or JSON with --json);
non-zero on failure (reason on stderr, or JSON {"ok": false, ...} with --json).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.utils import get_music_api_key  # noqa: E402

# Lyria RealTime's public model id on the Gemini Developer API (NOT Vertex AI —
# the SDK's live.music.connect() explicitly raises NotImplementedError under
# Vertex AI credentials, so this only ever runs against an AI Studio key).
DEFAULT_MODEL = "models/lyria-realtime-exp"

# Fallback PCM assumptions if the server's mime_type can't be parsed — Lyria
# RealTime's documented default output. Actual values are parsed from the
# real response when present, so this is a safety net, not the source of truth.
_FALLBACK_SAMPLE_RATE = 48000
_FALLBACK_CHANNELS = 2
_SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM

_MIME_RATE_RE = re.compile(r"rate=(\d+)")
_MIME_CHANNELS_RE = re.compile(r"channels=(\d+)")

# Confirmed live 2026-07-20: Lyria RealTime needs the v1alpha surface — the
# default client (v1beta) 404s the BidiGenerateMusic websocket outright, even
# though the SDK method itself works fine unmodified. This is the one thing
# that isn't discoverable from the SDK's method signatures; only a live probe
# against a real key surfaced it.
_API_VERSION = "v1alpha"


class MusicGenerationError(RuntimeError):
    """Raised when Lyria generation fails, with a human-readable reason."""


def _parse_pcm_format(mime_type: str | None) -> tuple[int, int]:
    """(sample_rate, channels) from a mime_type like "audio/l16;rate=48000;
    channels=2" (confirmed live 2026-07-20), with fallbacks.

    Never raises: a malformed or missing mime_type degrades to the confirmed
    default rather than aborting a generation that otherwise succeeded.
    """
    if not mime_type:
        return _FALLBACK_SAMPLE_RATE, _FALLBACK_CHANNELS
    rate_match = _MIME_RATE_RE.search(mime_type)
    channels_match = _MIME_CHANNELS_RE.search(mime_type)
    rate = int(rate_match.group(1)) if rate_match else _FALLBACK_SAMPLE_RATE
    channels = int(channels_match.group(1)) if channels_match else _FALLBACK_CHANNELS
    return rate, channels


async def _capture(
    prompt: str,
    duration_seconds: float,
    api_key: str,
    model: str,
    gen_config: dict,
) -> tuple[bytes, int, int]:
    """Connect, request `prompt`, buffer PCM until >= duration_seconds, stop.

    Returns (pcm_bytes, sample_rate, channels). Raises MusicGenerationError
    on any failure the caller should surface, rather than letting a raw SDK
    exception (websocket close codes, auth errors) reach the CM unexplained.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise MusicGenerationError(f"google-genai not installed: {exc}") from exc

    client = genai.Client(api_key=api_key, http_options={"api_version": _API_VERSION})
    pcm_chunks: list[bytes] = []
    sample_rate, channels = _FALLBACK_SAMPLE_RATE, _FALLBACK_CHANNELS
    bytes_needed = int(duration_seconds * sample_rate * channels * _SAMPLE_WIDTH_BYTES)

    try:
        async with client.aio.live.music.connect(model=model) as session:
            await session.set_weighted_prompts(
                prompts=[types.WeightedPrompt(text=prompt, weight=1.0)]
            )
            if gen_config:
                await session.set_music_generation_config(
                    config=types.LiveMusicGenerationConfig(**gen_config)
                )
            await session.play()

            bytes_collected = 0
            async for message in session.receive():
                content = getattr(message, "server_content", None)
                chunks = getattr(content, "audio_chunks", None) if content else None
                if not chunks:
                    continue
                for chunk in chunks:
                    data = getattr(chunk, "data", None)
                    if not data:
                        continue
                    # Format may only be reliably present on the first chunk;
                    # re-parsing every time is cheap and never wrong to redo.
                    sample_rate, channels = _parse_pcm_format(getattr(chunk, "mime_type", None))
                    bytes_needed = int(
                        duration_seconds * sample_rate * channels * _SAMPLE_WIDTH_BYTES
                    )
                    pcm_chunks.append(data)
                    bytes_collected += len(data)
                if bytes_collected >= bytes_needed > 0:
                    break

            await session.stop()
    except Exception as exc:  # noqa: BLE001 - normalize every SDK failure mode
        raise MusicGenerationError(f"Lyria RealTime session failed: {exc}") from exc

    if not pcm_chunks:
        raise MusicGenerationError(
            "No audio received from Lyria RealTime — the session closed "
            "without producing any audio_chunks. Check the API key has "
            "access to the live music model."
        )
    return b"".join(pcm_chunks), sample_rate, channels


def _write_wav(pcm: bytes, sample_rate: int, channels: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)


def _transcode_if_needed(wav_path: Path, final_path: Path) -> Path:
    """ffmpeg-convert the captured WAV to the requested extension, if not .wav."""
    if final_path.suffix.lower() == ".wav":
        return wav_path
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path), str(final_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise MusicGenerationError(
            f"ffmpeg transcode to {final_path.suffix} failed: "
            f"{(result.stderr or '').strip()[-500:]}"
        )
    wav_path.unlink(missing_ok=True)
    return final_path


def generate_music(
    prompt: str,
    duration_seconds: float,
    out_path: Path,
    model: str = DEFAULT_MODEL,
    **gen_config,
) -> dict:
    """Synchronous entry point — what a CM run's Bash step actually calls.

    `gen_config` accepts any LiveMusicGenerationConfig field (bpm, scale,
    temperature, density, brightness, mute_bass, mute_drums,
    only_bass_and_drums, guidance, top_k, seed, music_generation_mode).
    """
    api_key = get_music_api_key()
    pcm, sample_rate, channels = asyncio.run(
        _capture(prompt, duration_seconds, api_key, model, gen_config)
    )
    wav_path = out_path.with_suffix(".wav") if out_path.suffix.lower() != ".wav" else out_path
    _write_wav(pcm, sample_rate, channels, wav_path)
    final_path = _transcode_if_needed(wav_path, out_path)
    return {
        "ok": True,
        "path": str(final_path.resolve()),
        "duration_seconds": round(len(pcm) / (sample_rate * channels * _SAMPLE_WIDTH_BYTES), 2),
        "sample_rate": sample_rate,
        "channels": channels,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--prompt", required=True, help="Music description (genre/mood/instruments/tempo)."
    )
    parser.add_argument(
        "--duration", type=float, default=30.0, help="Target seconds of audio (default 30)."
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output file path (.wav, or another extension to transcode via ffmpeg).",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Override the model id (default {DEFAULT_MODEL})."
    )
    parser.add_argument("--bpm", type=int, default=None)
    parser.add_argument("--scale", default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--density", type=float, default=None)
    parser.add_argument("--brightness", type=float, default=None)
    parser.add_argument("--mute-bass", action="store_true")
    parser.add_argument("--mute-drums", action="store_true")
    parser.add_argument("--only-bass-and-drums", action="store_true")
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of a bare path."
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    gen_config = {}
    if args.bpm is not None:
        gen_config["bpm"] = args.bpm
    if args.scale is not None:
        gen_config["scale"] = args.scale
    if args.temperature is not None:
        gen_config["temperature"] = args.temperature
    if args.density is not None:
        gen_config["density"] = args.density
    if args.brightness is not None:
        gen_config["brightness"] = args.brightness
    if args.mute_bass:
        gen_config["mute_bass"] = True
    if args.mute_drums:
        gen_config["mute_drums"] = True
    if args.only_bass_and_drums:
        gen_config["only_bass_and_drums"] = True

    try:
        result = generate_music(
            args.prompt, args.duration, Path(args.out), model=args.model, **gen_config
        )
    except (MusicGenerationError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Music generation failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
