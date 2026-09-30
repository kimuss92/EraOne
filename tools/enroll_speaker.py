"""Enroll the owner's voice for Era's speaker gate (core/voice_gate.py).

    python tools/enroll_speaker.py --check     # model / profile status
    python tools/enroll_speaker.py             # record + build the voice profile
    python tools/enroll_speaker.py --test      # live: say something, see the verdict

Records free sentences plus repetitions of the current wake word. Thresholds are
NOT guessed: an impostor cohort is synthesised offline with the Windows speech
voices installed on this PC, its mean is subtracted from every embedding (this
model's raw scores barely separate speakers), and each threshold is set above the
highest impostor score. The tool reports how often YOUR voice clears that bar, and
refuses to save a profile Era could not use. Record in the room and with the mic
you normally use. The previous profile is kept as a timestamped .bak. All local.

Synthetic impostors are not people: after enrolling, have someone else try --test.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.speaker_recognition import (  # noqa: E402
    PROFILE_PATH,
    SAMPLE_RATE,
    THRESHOLD_FLOOR,
    SherpaEmbedder,
    SpeakerProfile,
    SpeakerVerifier,
    load_settings,
    voiced_segment,
)

SENTENCES = [
    "Era, open my calendar and tell me what is on for tomorrow morning.",
    "Play something relaxing on Spotify and turn the volume down a little.",
    "Remind me to call the supplier about the quartz samples at three.",
    "What is the weather going to be like this weekend in Montreal?",
    "Draft a short caption for the new kitchen photos and show it to me.",
    "Stop the music, pause the video, and put the computer to sleep.",
]
COMMAND_CLIP_S, WAKE_CLIP_S, MARGIN = 1.2, 0.7, 0.05
# Impostors are also calibrated mixed with YOUR room's background sound at these
# SNRs, so a stranger talking in the same room/mic is what the threshold beats.
ROOM_SNRS_DB = (15.0, 5.0)

_SAPI_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
  [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 0
foreach ($v in $s.GetInstalledVoices()) {
  if (-not $v.Enabled) { continue }
  $s.SelectVoice($v.VoiceInfo.Name)
  $s.SetOutputToWaveFile((Join-Path $args[0] "v$i.wav"), $fmt)
  $s.Speak($args[1]); $s.SetOutputToNull(); $i++
}
"""


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def record(seconds: float) -> np.ndarray:
    import sounddevice as sd

    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return audio.reshape(-1).astype(np.float32) / 32768.0


def clips(x: np.ndarray, seconds: float) -> list[np.ndarray]:
    n = int(seconds * SAMPLE_RATE)
    return [x[i : i + n] for i in range(0, len(x) - n + 1, n // 2)]


def synth_cohort() -> list[np.ndarray]:
    """One waveform per installed Windows voice (offline). Empty on failure."""
    text = " ".join(SENTENCES)
    with tempfile.TemporaryDirectory(prefix="era-cohort-") as tmp:
        script = Path(tmp) / "cohort.ps1"
        script.write_text(_SAPI_SCRIPT, encoding="utf-8")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
                + [tmp, text],
                check=True,
                capture_output=True,
                timeout=300,
            )
        except Exception as e:
            print(f"Could not synthesise the impostor cohort: {e}")
            return []
        out = []
        for wav in sorted(Path(tmp).glob("v*.wav")):
            with wave.open(str(wav)) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
            out.append(pcm.astype(np.float32) / 32768.0)
        return out


def mix_room(x: np.ndarray, room: np.ndarray, snr_db: float) -> np.ndarray:
    """Overlay looped room tone on `x` at `snr_db` (relative to x's voiced power)."""
    if room.size == 0 or not np.any(room):
        return x
    reps = int(np.ceil(len(x) / len(room)))
    noise = np.tile(room, reps)[: len(x)]
    loud = x[np.abs(x) > 0.01]
    p_sig = float(np.mean(loud * loud)) if loud.size else float(np.mean(x * x))
    p_noise = float(np.mean(noise * noise)) or 1e-12
    gain = np.sqrt(p_sig / (p_noise * 10 ** (snr_db / 10)))
    return np.clip(x + gain * noise, -1.0, 1.0).astype(np.float32)


def embed_clips(embedder, x: np.ndarray, seconds: float) -> list[np.ndarray]:
    embs = []
    for c in clips(x, seconds):
        v = voiced_segment(c)
        if len(v) >= int(seconds * 0.5 * SAMPLE_RATE):
            embs.append(embedder.embed(v))
    return embs


def calibrate(owner_full, owner_cmd, owner_wake, cohort_full, cohort_cmd, cohort_wake):
    """Impostor-driven thresholds. Each impostor voice is scored against a cohort
    mean that EXCLUDES it, so the calibration is not flattered by having seen it.
    Owner command clips are scored leave-one-sentence-out for the same reason."""
    imp_cmd, imp_wake = [], []
    for k in range(len(cohort_full)):
        mu = np.mean([e for j, es in enumerate(cohort_full) if j != k for e in es], axis=0)
        cen = _unit(np.mean([_unit(e - mu) for e in owner_full], axis=0))
        imp_cmd += [float(_unit(e - mu) @ cen) for e in cohort_cmd[k]]
        imp_wake += [float(_unit(e - mu) @ cen) for e in cohort_wake[k]]
    mu = np.mean([e for es in cohort_full for e in es], axis=0)
    own_cmd = []
    for i, clip_embs in enumerate(owner_cmd):
        cen = _unit(np.mean([_unit(e - mu) for j, e in enumerate(owner_full) if j != i], axis=0))
        own_cmd += [float(_unit(e - mu) @ cen) for e in clip_embs]
    centroid = _unit(np.mean([_unit(e - mu) for e in owner_full], axis=0))
    own_wake = [float(_unit(e - mu) @ centroid) for e in owner_wake]

    def pick(imp, own):
        t = min(0.95, max(THRESHOLD_FLOOR, max(imp) + MARGIN))
        return t, float(np.mean(np.array(own) >= t)) if own else 0.0

    cmd_t, cmd_rate = pick(imp_cmd, own_cmd)
    wake_t, wake_rate = pick(imp_wake, own_wake)
    return mu, centroid, (cmd_t, cmd_rate, max(imp_cmd)), (wake_t, wake_rate, max(imp_wake))


def cmd_check(settings: dict) -> int:
    v = SpeakerVerifier(settings)
    ready, why = v.readiness()
    print(f"enabled   : {settings['enabled']}")
    print(f"model     : {settings['model_path']} (exists={Path(settings['model_path']).is_file()})")
    print(f"profile   : {PROFILE_PATH} (exists={PROFILE_PATH.is_file()})")
    print(f"ready     : {ready} — {why}")
    if v.is_enrolled():
        print(f"thresholds: command={v.threshold('command'):.2f} wake={v.threshold('wake'):.2f}")
    return 0 if ready else 1


def cmd_test(settings: dict) -> int:
    v = SpeakerVerifier(settings)
    print("Ctrl+C to stop. Speak for ~3 s after each prompt.")
    try:
        while True:
            input("\n[Enter] then speak... ")
            pcm = (record(3.0) * 32768.0).astype("<i2").tobytes()
            for purpose in ("command", "wake"):
                r = v.verify(pcm, purpose)
                print(f"  {purpose:7s}: {'ACCEPT' if r.accepted else 'REJECT'} — {r.reason}")
    except KeyboardInterrupt:
        return 0


def cmd_enroll(settings: dict, wake_reps: int) -> int:
    try:
        from app.wake import WAKE_CFG

        wake_word = WAKE_CFG.word
    except Exception:
        wake_word = "era"
    embedder = SherpaEmbedder(settings["model_path"])
    try:
        embedder._load()
    except Exception as e:
        print(f"Cannot load speaker model: {e}")
        return 2

    input("\n[Enter] then stay SILENT for 4 s (recording your room's background sound)...")
    room = record(4.0)

    print("Building the impostor cohort from this PC's speech voices (offline)...")
    cohort = synth_cohort()
    if len(cohort) < 3:
        print("Need at least 3 Windows speech voices for a trustworthy calibration. Not saved.")
        return 4
    cohort_full = [embed_clips(embedder, x, 2.0) for x in cohort]
    cohort_cmd = [embed_clips(embedder, x, COMMAND_CLIP_S) for x in cohort]
    cohort_wake = [embed_clips(embedder, x, WAKE_CLIP_S) for x in cohort]
    for k, x in enumerate(cohort):
        for snr in ROOM_SNRS_DB:
            noisy = mix_room(x, room, snr)
            cohort_cmd[k] += embed_clips(embedder, noisy, COMMAND_CLIP_S)
            cohort_wake[k] += embed_clips(embedder, noisy, WAKE_CLIP_S)

    def take(prompt: str, seconds: float, min_voiced_s: float) -> np.ndarray:
        while True:
            input(f"\n[Enter] then say: {prompt}")
            voiced = voiced_segment(record(seconds))
            if len(voiced) >= int(min_voiced_s * SAMPLE_RATE):
                return voiced
            print("  Too quiet / too short — again, a bit closer to the mic.")

    print(f"\nRecording {len(SENTENCES)} sentences, then '{wake_word}' x{wake_reps}.")
    sentences = [take(f'"{s}"', 5.0, 1.5) for s in SENTENCES]
    wakes = [take(f'"{wake_word}"', 2.0, 0.3) for _ in range(wake_reps)]

    owner_full = [embedder.embed(x) for x in sentences]
    owner_cmd = [embed_clips(embedder, x, COMMAND_CLIP_S) for x in sentences]
    owner_wake = [embedder.embed(x) for x in wakes]
    mu, centroid, cmd, wake = calibrate(
        owner_full, owner_cmd, owner_wake, cohort_full, cohort_cmd, cohort_wake
    )
    print(f"\ncommand: threshold {cmd[0]:.2f} (highest impostor {cmd[2]:.2f}) — ", end="")
    print(f"your voice passes {cmd[1]:.0%} of 1.2 s clips")
    print(f"wake   : threshold {wake[0]:.2f} (highest impostor {wake[2]:.2f}) — ", end="")
    print(f"your wake word passes {wake[1]:.0%} of takes")
    if cmd[1] < 0.5 or wake[1] < 0.4:
        print(
            "\nYour voice does not clear the impostor bar often enough; Era would ignore you"
            " most of the time. Not saved. Re-run in a quieter spot, closer to the mic."
        )
        return 3

    if PROFILE_PATH.exists():
        bak = PROFILE_PATH.with_name(f"{PROFILE_PATH.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(PROFILE_PATH, bak)
        print(f"previous profile kept: {bak.name}")
    SpeakerProfile(
        model_name=embedder.model_name,
        centroid=centroid,
        command_threshold=cmd[0],
        wake_threshold=wake[0],
        samples=len(sentences) + len(wakes),
        extra={
            "wake_word": wake_word,
            "cohort_voices": len(cohort),
            "owner_command_pass_rate": round(cmd[1], 3),
            "owner_wake_pass_rate": round(wake[1], 3),
        },
        cohort_mean=mu,
    ).save(PROFILE_PATH)
    print(f"\nSaved {PROFILE_PATH}. Restart Era to load it.")
    print("Then run --test, and have someone else try it too.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--wake-reps", type=int, default=5)
    args = ap.parse_args()
    settings = load_settings()
    if args.check:
        return cmd_check(settings)
    if args.test:
        return cmd_test(settings)
    return cmd_enroll(settings, max(3, min(10, args.wake_reps)))


if __name__ == "__main__":
    raise SystemExit(main())
