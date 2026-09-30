#!/usr/bin/env python3
"""Clone a voice with Fish Audio and (optionally) prove it speaks.

    python tools/fish_audio_clone.py --file "C:\\path\\clip.wav" --title "My voice"
    python tools/fish_audio_clone.py --file ... --title ... --verify

Uploads a reference clip to Fish Audio, which returns a voice-model id. That id
is the ``reference_id`` Era passes to /v1/tts, so once it is in config Era speaks
in the cloned voice — with the Gemini fallback still behind it.

The clip is sent to Fish Audio's servers to build the model. The model is created
PRIVATE by default. The API key is read from the same secure locations
core/tts_fish.py uses and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.tts_fish import FISH, create_voice_model, is_configured  # noqa: E402

VERIFY_TEXT = "Voice clone online, sir. This is how I will sound from now on."

# Fish trains best on roughly 10-90s of clean single-speaker audio.
MIN_GOOD_SECONDS = 8.0
MAX_GOOD_SECONDS = 120.0


def describe_clip(path: Path) -> str:
    """Report the clip's shape, and warn when it is outside the useful range."""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as w:
            rate, frames, ch, width = (
                w.getframerate(),
                w.getnframes(),
                w.getnchannels(),
                w.getsampwidth(),
            )
        seconds = frames / float(rate or 1)
        note = ""
        if seconds < MIN_GOOD_SECONDS:
            note = "  <- short; clone quality may suffer"
        elif seconds > MAX_GOOD_SECONDS:
            note = "  <- long; trim to the cleanest ~60s for a better clone"
        return f"{rate} Hz, {ch} ch, {width * 8}-bit, {seconds:.1f}s{note}"
    except Exception as e:
        return f"(not a readable RIFF wav: {e})"


async def _run(args: argparse.Namespace) -> int:
    path = Path(args.file)
    print()
    print("=" * 62)
    print("  Fish Audio -- voice cloning")
    print("=" * 62)
    print(f"  clip      : {path}")
    if not path.is_file():
        print("  [FAIL] file not found.")
        return 1
    print(f"  format    : {describe_clip(path)}")
    print(f"  size      : {path.stat().st_size / 1024:.1f} KB")
    print(f"  title     : {args.title}")
    print(f"  visibility: {args.visibility}")
    print(f"  API key   : {'present' if is_configured() else 'ABSENT'}")
    print()

    if not is_configured():
        print("  [FAIL] No Fish Audio API key configured.")
        print('  Add  "fish_audio": { "api_key": "<key>" }  to config/api_keys.json')
        return 1

    print("  Uploading reference clip and training (fast mode)...")
    result = await create_voice_model(
        path,
        title=args.title,
        description=args.description,
        texts=args.texts,
        visibility=args.visibility,
    )

    if "error" in result:
        print(f"  [FAIL] {result['error']}")
        return 1

    model_id = result["id"]
    print(f"  [OK] Voice model created: {model_id}")
    print(f"       title={result['title']!r} visibility={result['visibility']}")
    print()

    if args.verify:
        print("  Verifying the clone actually speaks...")
        import os

        os.environ["FISH_AUDIO_REFERENCE_ID"] = model_id
        FISH.reset_cooldown()
        pcm = await FISH.synthesize(VERIFY_TEXT)
        await FISH.close()
        if pcm:
            from app.audio_config import RECEIVE_SAMPLE_RATE

            secs = len(pcm) / 2.0 / RECEIVE_SAMPLE_RATE
            print(f"  [OK] Spoke {len(pcm):,} bytes (~{secs:.1f}s) in the cloned voice.")
            if args.save_sample:
                out = Path(args.save_sample)
                with contextlib.closing(wave.open(str(out), "wb")) as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RECEIVE_SAMPLE_RATE)
                    w.writeframes(pcm)
                print(f"  [OK] Sample written to {out} -- play it to hear the clone.")
        else:
            print(f"  [WARN] Clone created but synthesis failed: {FISH.last_error}")
            print("         Era would fall back to her Gemini voice.")

    print()
    print("  To make this Era's voice, add the reference_id to config/api_keys.json:")
    print('      "fish_audio": {')
    print('          "api_key": "<your existing key>",')
    print(f'          "reference_id": "{model_id}"')
    print("      }")
    print("  then:  python tools/fish_audio_check.py --activate")
    print("=" * 62)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Clone a voice with Fish Audio.")
    ap.add_argument("--file", required=True, help="reference audio clip (wav)")
    ap.add_argument("--title", default="Era One voice", help="name for the voice model")
    ap.add_argument("--description", default=None)
    ap.add_argument(
        "--texts",
        default=None,
        help="transcript of the clip; omit and Fish runs ASR itself (usually better)",
    )
    ap.add_argument(
        "--visibility",
        default="private",
        choices=("private", "unlist", "public"),
        help="default private -- a cloned voice is personal data",
    )
    ap.add_argument("--verify", action="store_true", help="synthesize a line with the new voice")
    ap.add_argument("--save-sample", default=None, help="write the verification audio to this wav")
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
