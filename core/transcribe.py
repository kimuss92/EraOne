"""Speech-to-text for audio that arrives as a FILE rather than through the mic.

WHY THIS EXISTS SEPARATELY FROM THE LIVE SESSION
Era normally hears you through an open Gemini Live session: raw PCM streams in
and the model reacts. A Telegram voice note has neither of those properties —
it is a finished Ogg/Opus file that shows up on a poller thread, possibly while
Era is in standby, mid-reconnect, or in a quota cooldown with no session at all.

So it takes the other road: transcribe it once, then hand the TEXT to the same
entry point a typed message uses. That is what makes a voice note behave exactly
like every other command instead of becoming a third code path with its own
half-implemented rules — standby and wake-word handling, the safety gate, CM
routing and the confirmation flow all come for free.

WHY NOT VOSK
Era already loads Vosk for wake-word detection, but that is
`vosk-model-small-en-us` behind a grammar constrained to a single word. It is
built to answer "was that the wake word?", not to transcribe free speech, and it
is English-only — the owner gives orders in French as often as English. Gemini
handles both and needs no new model on disk.

QUOTA ISOLATION
Deliberately NOT the Live-session key. Quota is per-key (see get_api_keys), and
a voice note burning the voice key would take Era's own ears down with it —
precisely backwards, since a voice note is what you send when you are away from
the machine and depending on her. Same reasoning as the dedicated music key.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from core.win_subprocess import NO_WINDOW

# Gemini accepts audio up to a generous size, but a long recording is far more
# likely to be a misfire (a pocket-dial, a forwarded podcast) than an order.
_MAX_SECONDS = 300

# Flash is right here: transcription is a mechanical task, and the whole point is
# that a voice note reaches Era as fast as a typed one would.
_MODEL = "gemini-3.8-flash"

_PROMPT = (
    "Transcribe this voice recording verbatim.\n"
    "Rules:\n"
    "- Output ONLY the transcription, with no preamble, quotes, or commentary.\n"
    "- Keep the original language (it may be French, English, or a mix of both). "
    "Do not translate.\n"
    "- Keep proper nouns, brand names and file paths exactly as spoken.\n"
    "- If the recording contains no intelligible speech, output nothing at all."
)


class TranscriptionError(RuntimeError):
    """Transcription could not be produced. Carries a message fit to show the user."""


def ffmpeg_path() -> str | None:
    """Locate ffmpeg, or None. Era already ships with it for the reel pipeline."""
    return shutil.which("ffmpeg")


def to_wav(src: Path) -> bytes:
    """Decode any audio Telegram might send into 16 kHz mono 16-bit WAV.

    Telegram voice notes are Opus-in-Ogg. Gemini's documented audio support
    covers Ogg VORBIS, which is a different codec in the same container — so the
    file can be accepted and then decode to nothing. Normalizing to WAV first
    removes the guesswork, and costs a subprocess call on a file measured in tens
    of kilobytes.
    """
    exe = ffmpeg_path()
    if not exe:
        raise TranscriptionError(
            "ffmpeg isn't on PATH, so I can't decode a voice note. Install it and I'll hear you."
        )
    with tempfile.TemporaryDirectory(prefix="era_tg_voice_") as tmp:
        out = Path(tmp) / "voice.wav"
        proc = subprocess.run(
            [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-t",
                str(_MAX_SECONDS),
                "-i",
                str(src),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(out),
            ],
            capture_output=True,
            timeout=120,
            creationflags=NO_WINDOW,
        )
        if proc.returncode != 0 or not out.is_file():
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            raise TranscriptionError(
                f"I couldn't decode that recording ({detail or 'ffmpeg failed'})."
            )
        return out.read_bytes()


def transcribe_file(path) -> str:
    """Return what was said in `path`, or "" when there was no speech.

    Raises TranscriptionError with a user-readable message on any failure. It
    RAISES rather than returning "" for a failure because the two outcomes need
    opposite handling: silence means "nothing to do", while a failure has to be
    reported back to whoever sent the note — a command that vanishes without a
    word is the failure mode this whole feature exists to avoid.
    """
    src = Path(path)
    if not src.is_file():
        raise TranscriptionError(f"The recording never arrived ({src.name}).")

    wav = to_wav(src)

    try:
        from google.genai import types as gtypes

        from core.ai_provider import get_client
        from core.utils import get_transcription_api_key

        client = get_client(get_transcription_api_key())
        response = client.models.generate_content(
            model=_MODEL,
            contents=[
                gtypes.Part.from_bytes(data=wav, mime_type="audio/wav"),
                _PROMPT,
            ],
        )
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            raise TranscriptionError(
                "I'm out of transcription quota right now — send it as text and I'll act on it."
            ) from e
        raise TranscriptionError(f"Transcription failed ({type(e).__name__}).") from e

    text = (getattr(response, "text", "") or "").strip()
    # The model occasionally wraps its answer in quotes despite the instruction;
    # a stray pair would otherwise turn "close brave" into a string literal that
    # no intent matcher recognizes.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text
