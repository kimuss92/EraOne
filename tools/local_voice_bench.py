"""Latency benchmark for the local fallback voice loop (migration Phase 11).

    python -m tools.local_voice_bench            # all stages, 6 utterances
    python -m tools.local_voice_bench --threads 8

Per utterance it measures the stages the user actually waits on:

    STT   speech end -> text          (Silero VAD + Whisper-small)   target <500 ms
    LLM   text -> first sentence      (era.local via Ollama)          target <1 s
    TTS   sentence -> first audio     (Kokoro George, GPU)            target <500 ms
    TOTAL user stops speaking -> Era starts speaking                  target <2 s

Input audio is generated with Kokoro (English only; Piper, which also spoke
French, was removed 2026-10-10) and fed through the same VAD + Whisper
path the mic uses, so STT is measured on real speech. Tool calls go to a fake
executor: the benchmark never touches the machine. Needs Ollama running and
the models in models/local_voice/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time

import numpy as np

UTTERANCES = [
    ("en", "What time is it in Tokyo?"),
    ("en", "Pause the music."),
    ("en", "Open notepad please."),
    ("en", "Tell me a short fact about Tunisia."),
]
TARGETS_MS = {"stt": 500, "llm": 1000, "tts": 500, "total": 2000}


async def _fake_tool(name, args):
    return {"success": True, "message": "Done."}


def _speech(lang: str, text: str) -> np.ndarray:
    """Kokoro speech at 16 kHz float32, padded with silence so VAD sees an end."""
    from core.tts_kokoro import KOKORO, SAMPLE_RATE

    samples = np.frombuffer(KOKORO.synthesize(text), dtype=np.int16).astype(np.float32) / 32768
    n = int(len(samples) * 16000 / SAMPLE_RATE)
    resampled = np.interp(
        np.linspace(0, 1, n, endpoint=False),
        np.linspace(0, 1, len(samples), endpoint=False),
        samples,
    ).astype(np.float32)
    pad = np.zeros(16000, dtype=np.float32)
    return np.concatenate([pad[:8000], resampled, pad])


async def bench(threads: int) -> int:
    os.environ["ERA_LOCAL_STT_THREADS"] = str(threads)
    from core.local_brain import LocalBrain
    from core.local_stt import LocalListener
    from core.tts_kokoro import KOKORO

    t = time.perf_counter()
    listener = LocalListener()
    listener.load()
    KOKORO.warm()
    print(f"models loaded in {int((time.perf_counter() - t) * 1000)} ms (stt threads={threads})")

    brain = LocalBrain()
    t = time.perf_counter()
    async for _ in brain.respond("ping", _fake_tool):
        pass
    print(f"brain cold start (model + tool prefix) {int((time.perf_counter() - t) * 1000)} ms\n")
    brain.clear()

    rows = []
    for lang, text in UTTERANCES:
        pcm16 = (_speech(lang, text) * 32767).astype(np.int16).tobytes()
        listener.reset()
        segments = []
        for i in range(0, len(pcm16), 2048):
            segments += listener.feed(pcm16[i : i + 2048])
        if not segments:
            print(f"  VAD found no speech in {text!r}")
            continue
        t0 = time.perf_counter()
        utt = listener.transcribe(segments[0])
        stt = (time.perf_counter() - t0) * 1000
        heard = utt.text if utt else ""

        t1 = time.perf_counter()
        first_sentence, llm = "", None
        async for sentence in brain.respond(heard or text, _fake_tool):
            if llm is None:
                llm = (time.perf_counter() - t1) * 1000
                first_sentence = sentence
        llm = llm or (time.perf_counter() - t1) * 1000

        t2 = time.perf_counter()
        KOKORO.synthesize(first_sentence or "Done, sir.")
        tts = (time.perf_counter() - t2) * 1000

        total = stt + llm + tts
        rows.append({"stt": stt, "llm": llm, "tts": tts, "total": total})
        print(f"[{lang}] {text}")
        print(f"     heard: {heard!r}  ({utt.lang if utt else '?'})")
        print(f"     reply: {first_sentence!r}")
        print(f"     stt {stt:6.0f}  llm {llm:6.0f}  tts {tts:5.0f}  TOTAL {total:6.0f} ms")

    if not rows:
        return 1
    print("\nstage   p50 ms  worst ms  target  ")
    for stage, target in TARGETS_MS.items():
        vals = [r[stage] for r in rows]
        p50, worst = statistics.median(vals), max(vals)
        verdict = "ok" if p50 <= target else "OVER"
        print(f"{stage:6} {p50:7.0f} {worst:9.0f} {target:7}  {verdict}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--threads", type=int, default=4, help="Whisper CPU threads")
    return asyncio.run(bench(ap.parse_args().threads))


if __name__ == "__main__":
    raise SystemExit(main())
