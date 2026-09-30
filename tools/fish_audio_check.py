#!/usr/bin/env python3
"""Verify the Fish Audio TTS integration, then optionally make it Era's voice.

    python tools/fish_audio_check.py              # check only
    python tools/fish_audio_check.py --activate   # check, then switch Era to it

Does a REAL synthesis round trip, so it proves the key, the model and the audio
decode all work before you rely on Fish as the primary voice. The API key is
never printed, logged, or written anywhere by this script -- it is only read
from the secure locations core/tts_fish.py resolves.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audio_config import RECEIVE_SAMPLE_RATE  # noqa: E402
from core.tts_fish import FISH, get_fish_config, is_configured  # noqa: E402

SAMPLE_TEXT = "Primary voice online, sir. Fish Audio is handling speech."


def _mask(key: str) -> str:
    """Never reveal a key. Only prove one is present and roughly well formed."""
    if not key:
        return "(absent)"
    return f"present, {len(key)} chars, ends ...{key[-4:]}" if len(key) > 8 else "present (short)"


async def _run(args) -> int:
    import os

    if args.reference_id:
        # Test an arbitrary voice model without touching stored config.
        os.environ["FISH_AUDIO_REFERENCE_ID"] = args.reference_id
    cfg = get_fish_config()
    print()
    print("=" * 60)
    print("  Fish Audio -- primary TTS check")
    print("=" * 60)
    print(f"  API key   : {_mask(cfg['api_key'])}")
    print(f"  model     : {cfg['model']}")
    print(f"  endpoint  : {cfg['endpoint']}")
    print(f"  voice id  : {cfg['reference_id'] or '(model default)'}")
    print()

    if not is_configured():
        print("  [FAIL] No API key found.")
        print("  Set it in ONE of these, then re-run:")
        print('    setx FISH_AUDIO_API_KEY "<your key>"      (then open a new terminal)')
        print('    config/api_keys.json ->  "fish_audio": { "api_key": "<your key>" }')
        return 1

    print(f"  Synthesizing: {SAMPLE_TEXT!r}")
    FISH.reset_cooldown()
    pcm = await FISH.synthesize(SAMPLE_TEXT)
    await FISH.close()

    if not pcm:
        print(f"  [FAIL] Synthesis returned nothing -- {FISH.last_error or 'unknown error'}")
        print("  Era would fall back to her Gemini voice in this state.")
        return 1

    seconds = len(pcm) / 2.0 / RECEIVE_SAMPLE_RATE
    print(f"  [OK] {len(pcm):,} bytes of PCM (~{seconds:.1f}s at {RECEIVE_SAMPLE_RATE} Hz)")

    if args.save_sample:
        import contextlib
        import wave

        out = Path(args.save_sample)
        with contextlib.closing(wave.open(str(out), "wb")) as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RECEIVE_SAMPLE_RATE)
            w.writeframes(pcm)
        print(f"  [OK] Sample written to {out} -- play it to hear this voice.")

    if not args.activate:
        print()
        if args.reference_id:
            print("  This voice works. To keep it, put the reference_id in")
            print('  config/api_keys.json under "fish_audio", then re-run with --activate.')
        else:
            print("  Re-run with --activate to make Fish Audio Era's voice.")
        print("=" * 60)
        return 0

    from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG

    applied = VOICE_CFG.set_mode(MODE_FISH_AUDIO)
    print(f"  [OK] Era's voice mode is now '{applied}' (persisted to config/voice.json).")
    print("  Gemini remains the automatic fallback if Fish ever fails.")
    print("=" * 60)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Check (and optionally activate) Fish Audio TTS.")
    ap.add_argument("--activate", action="store_true", help="switch Era's voice mode on success")
    ap.add_argument(
        "--reference-id",
        default=None,
        help="test a specific Fish voice model id (overrides config for this run only)",
    )
    ap.add_argument("--save-sample", default=None, help="write the spoken audio to this wav")
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
