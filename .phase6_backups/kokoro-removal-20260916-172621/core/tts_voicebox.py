"""Voicebox TTS client — local speech synthesis for Era's kokoro_text mode.

Voicebox (https://voicebox.sh, MIT) is a LOCAL voice studio: a FastAPI backend
on 127.0.0.1:8000. No API key, no per-character cost, no network dependency.

Era uses exactly one synthesis endpoint, ``POST /generate/stream``, which returns
the WAV inline without touching disk. Deliberately NOT ``POST /speak``: that
plays audio through *Voicebox's own* output device, which would bypass Era's
_play_audio and so kill both the UI sphere amplitude and the _is_speaking gating
that mic suppression and every ``while self._is_speaking`` wait depend on. Era
must own the PCM.

Contract with the rest of Era: this module hands back raw **int16 mono PCM at
RECEIVE_SAMPLE_RATE (24 kHz)**, framed, ready to put on ``audio_in_queue``.

Degrade, never raise — same discipline as core/cm_vault.py. A dead or slow
backend costs the *voice*, never the process: every public coroutine returns
None/False/[] and logs rather than propagating.
"""

from __future__ import annotations

import asyncio
import io
import struct
import wave
from pathlib import Path

import numpy as np

from app.audio_config import CHUNK_SIZE, RECEIVE_SAMPLE_RATE
from core.voice_mode import VOICE_CFG, get_voicebox_config

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp is in requirements.txt
    aiohttp = None  # type: ignore[assignment]

# Bytes per frame handed to the player. CHUNK_SIZE samples of int16 mono is
# ~43 ms at 24 kHz — comfortably finer than _play_audio's ~33 Hz amplitude
# throttle, so the sphere still gets a fresh spectrum on every emit.
FRAME_BYTES = CHUNK_SIZE * 2

# A single sentence should never take this long locally; Kokoro is realtime on
# CPU. Hitting this means the backend is wedged, and waiting longer just makes
# Era look frozen.
SYNTH_TIMEOUT_S = 60.0
PROBE_TIMEOUT_S = 5.0
# Cold-load ceiling. Long enough for a real model load, short enough that a
# busy backend fails visibly instead of stalling Era's startup for minutes.
LOAD_TIMEOUT_S = 90


def _log(msg: str) -> None:
    print(f"[TTS] {msg}")


# ──────────────────────────────────────────────────────────────────────────────
# WAV → Era PCM
# ──────────────────────────────────────────────────────────────────────────────
def _parse_wav(wav_bytes: bytes) -> tuple[np.ndarray, int, int] | None:
    """Return (samples, channels, sample_rate) with samples as float32 in [-1, 1].

    Tries the stdlib `wave` module first, which handles the integer-PCM WAVs
    soundfile writes by default. Falls back to a minimal RIFF walk for
    WAVE_FORMAT_IEEE_FLOAT (format 3), which `wave` rejects outright with
    "unknown format: 3". Handling both means an upstream encoder change cannot
    silently mute Era.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
        if width == 1:
            # 8-bit WAV is unsigned, centred on 128.
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 2:
            arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 4:
            arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            _log(f"unsupported WAV sample width {width}")
            return None
        return arr, channels, rate
    except wave.Error:
        pass  # fall through to the float-WAV path
    except Exception as e:
        _log(f"WAV parse failed: {e}")
        return None

    return _parse_wav_float(wav_bytes)


def _parse_wav_float(wav_bytes: bytes) -> tuple[np.ndarray, int, int] | None:
    """Minimal RIFF walk for IEEE-float WAV, which stdlib `wave` cannot read."""
    try:
        if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
            _log("not a RIFF/WAVE payload")
            return None
        pos = 12
        fmt: tuple[int, int, int, int] | None = None
        end = len(wav_bytes)
        while pos + 8 <= end:
            cid = wav_bytes[pos : pos + 4]
            (csize,) = struct.unpack("<I", wav_bytes[pos + 4 : pos + 8])
            body = pos + 8
            if cid == b"fmt " and csize >= 16:
                audio_fmt, channels, rate, _br, _ba, bits = struct.unpack(
                    "<HHIIHH", wav_bytes[body : body + 16]
                )
                fmt = (audio_fmt, channels, rate, bits)
            elif cid == b"data" and fmt is not None:
                audio_fmt, channels, rate, bits = fmt
                chunk = wav_bytes[body : body + csize]
                if audio_fmt == 3 and bits == 32:
                    arr = np.frombuffer(chunk, dtype="<f4").astype(np.float32)
                elif audio_fmt == 3 and bits == 64:
                    arr = np.frombuffer(chunk, dtype="<f8").astype(np.float32)
                elif audio_fmt == 1 and bits == 16:
                    arr = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
                else:
                    _log(f"unsupported WAV format={audio_fmt} bits={bits}")
                    return None
                return arr, channels, rate
            # Chunks are word-aligned: an odd size carries a pad byte.
            pos = body + csize + (csize & 1)
        _log("no data chunk found in WAV")
        return None
    except Exception as e:
        _log(f"float-WAV parse failed: {e}")
        return None


def wav_to_pcm24k(wav_bytes: bytes) -> bytes:
    """Convert a WAV payload to raw int16 mono PCM at RECEIVE_SAMPLE_RATE.

    Kokoro is natively 24 kHz mono, so in the normal case the resample is a
    no-op — but the conversion is done from the header rather than assumed,
    because a rate mismatch would not error, it would just play Era's voice at
    the wrong pitch and speed.

    Returns b"" on any failure (degrade, never raise).
    """
    if not wav_bytes:
        return b""
    parsed = _parse_wav(wav_bytes)
    if parsed is None:
        return b""
    samples, channels, rate = parsed
    if samples.size == 0:
        return b""

    # Downmix to mono by averaging the interleaved frames.
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)

    # Linear-interpolation resample. Adequate for speech and dependency-free —
    # scipy/librosa are deliberately not added, and audioop.ratecv is gone
    # (removed in Python 3.13; this machine runs 3.14).
    if rate != RECEIVE_SAMPLE_RATE and samples.size > 1:
        n_out = int(round(samples.size * RECEIVE_SAMPLE_RATE / float(rate)))
        if n_out <= 0:
            return b""
        samples = np.interp(
            np.linspace(0.0, samples.size - 1, n_out, dtype=np.float64),
            np.arange(samples.size, dtype=np.float64),
            samples.astype(np.float64),
        ).astype(np.float32)

    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def iter_pcm_frames(pcm: bytes, frame_bytes: int = FRAME_BYTES):
    """Yield frame-sized slices of PCM, keeping any short tail intact."""
    if frame_bytes <= 0:
        yield pcm
        return
    # Never split a sample across two frames — an odd offset would swap the
    # bytes of an int16 and turn the tail into noise.
    frame_bytes -= frame_bytes % 2
    for i in range(0, len(pcm), frame_bytes):
        chunk = pcm[i : i + frame_bytes]
        if chunk:
            yield chunk


# ──────────────────────────────────────────────────────────────────────────────
# Port discovery
# ──────────────────────────────────────────────────────────────────────────────
# The Voicebox DESKTOP APP picks a random high port per launch and passes it to
# its server as --port (observed: 17493). So the port cannot be hard-coded, and
# a fixed 8000 only applies to a backend Era spawned herself. Era therefore
# prefers to ATTACH to an already-running server — it is free and its model is
# already warm — and only falls back to her own configured port.
_SERVER_PROCESS_NAMES = ("voicebox-server", "voicebox-backend")


def discover_running_ports() -> list[int]:
    """Ports of Voicebox backends already running, read from their command lines.

    Returns [] rather than raising if psutil is missing or a process vanishes
    mid-scan — this is an optimisation over the configured port, never a
    requirement.
    """
    ports: set[int] = set()
    try:
        import psutil
    except ImportError:
        return []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not any(name.startswith(n) for n in _SERVER_PROCESS_NAMES):
                continue
            cmd = proc.info.get("cmdline") or []
            for i, arg in enumerate(cmd):
                if arg == "--port" and i + 1 < len(cmd):
                    ports.add(int(cmd[i + 1]))
                elif arg.startswith("--port="):
                    ports.add(int(arg.split("=", 1)[1]))
        except Exception:
            continue
    return sorted(ports)


# ──────────────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────────────
class VoiceboxClient:
    """Async HTTP client for the local Voicebox backend.

    One long-lived aiohttp session, created lazily on the running loop. Every
    method degrades: a dead backend yields None/False/[] plus a log line, never
    an exception into Era's audio path.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._profile_id: str | None = None
        self._lock = asyncio.Lock()
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        c = get_voicebox_config()
        return f"http://{c['host']}:{self._port or c['port']}"

    def forget_port(self) -> None:
        """Drop the cached port so the next probe rediscovers.

        The desktop app re-rolls its port on every launch, so a port that
        stopped answering is far more likely to be stale than to be a backend
        that will come back.
        """
        self._port = None
        self._profile_id = None

    async def _probe(self, port: int) -> bool:
        return await self._probe_health(port) is not None

    async def _probe_health(self, port: int) -> dict | None:
        """The backend's /health payload, or None if it is not answering.

        Returns the BODY rather than a bool because the body is what says
        whether this backend has a GPU — see resolve_port for why that decides
        which one Era attaches to.
        """
        s = await self._get_session()
        if s is None:
            return None
        c = get_voicebox_config()
        try:
            async with s.get(
                f"http://{c['host']}:{port}/health",
                timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S),
            ) as r:
                if r.status != 200:
                    return None
                body = await r.json(content_type=None)
                return body if isinstance(body, dict) else {}
        except Exception:
            return None

    async def resolve_port(self) -> int | None:
        """Find the BEST live backend, not merely the first one that answers.

        WHY THIS RANKS INSTEAD OF TAKING THE FIRST HIT
          Voicebox can have more than one backend up at once: the desktop app
          spawns its own (``voicebox-server-cuda`` on a dynamic port) and a
          plain CPU ``voicebox-server`` may also be running. Both answer
          /health identically, so the old order — cached, then the CONFIGURED
          port, then discovered — attached to whichever was listed first.

          In practice that was the configured 8000, a CPU-only instance, while
          an RTX 3070 backend sat idle on 17493. Era's local voice ran on the
          CPU and nothing reported anything wrong, because nothing WAS wrong by
          the old test: the CPU backend is perfectly healthy, just slower by an
          order of magnitude.

          Hardcoding the GPU port would not fix it — the app re-rolls that port
          on every launch (see forget_port). So the choice is made from what
          /health reports, which stays correct whatever ports move.
        """
        c = get_voicebox_config()
        candidates: list[int] = []
        if self._port:
            candidates.append(self._port)
        if c["port"] not in candidates:
            candidates.append(c["port"])
        for p in discover_running_ports():
            if p not in candidates:
                candidates.append(p)

        # Probe every candidate, then rank. The cost is one extra round trip per
        # live backend, once per attachment — not per utterance.
        live: list[tuple[int, dict]] = []
        for port in candidates:
            health = await self._probe_health(port)
            if health is not None:
                live.append((port, health))
        if not live:
            self._port = None
            return None

        def rank(item: tuple[int, dict]) -> tuple[int, int]:
            port, health = item
            gpu = 1 if health.get("gpu_available") else 0
            # Tie-break on the caller's own ordering so a cached or configured
            # port still wins among equals.
            return (-gpu, candidates.index(port))

        live.sort(key=rank)
        port, health = live[0]
        if port != self._port:
            gpu = health.get("gpu_type") or ("GPU" if health.get("gpu_available") else "CPU")
            extra = ""
            if len(live) > 1:
                extra = f" (preferred over {len(live) - 1} other live backend(s))"
            _log(f"attached to Voicebox on port {port} [{gpu}]{extra}")
            # A different server means different profiles.
            self._profile_id = None
        self._port = port
        return port

    async def _get_session(self) -> aiohttp.ClientSession | None:
        if aiohttp is None:
            _log("aiohttp not installed; local voice unavailable")
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

    # ── Probes ───────────────────────────────────────────────────────────────
    async def health(self) -> bool:
        """True if a backend is reachable, resolving/rediscovering the port."""
        return await self.resolve_port() is not None

    async def model_loaded(self, engine: str) -> bool:
        """Is this engine already resident? Cheap GET, no side effects."""
        s = await self._get_session()
        if s is None:
            return False
        try:
            async with s.get(
                f"{self.base_url}/models/status",
                timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S),
            ) as r:
                if r.status != 200:
                    return False
                body = await r.json(content_type=None)
        except Exception:
            return False
        want = engine.lower()
        for m in (body or {}).get("models", []):
            name = str(m.get("model_name", "")).lower()
            # "luxtts" matches "luxtts"; "kokoro" matches "kokoro"; the qwen
            # entries carry a size suffix, so match on prefix.
            if (name == want or name.startswith(want)) and m.get("loaded"):
                return True
        return False

    async def load_model(self, engine: str = "kokoro", model_size: str = "1.7B") -> bool:
        """Preload an engine so the first utterance is not a cold model load.

        Era keeps the local engine warm at all times regardless of the active
        mode — the whole point is that it is ready the instant Gemini's quota
        dies.

        SKIPS THE LOAD IF THE ENGINE IS ALREADY RESIDENT. That is not just an
        optimisation: /models/load blocks behind whatever else the backend is
        doing, and a model DOWNLOAD can hold it for minutes. Measured: with a
        3.8 GB qwen download in flight, this call hung for the full 300s timeout
        while the engine Era wanted was already loaded and ready to speak. A
        cheap status check first turns that startup stall into one GET.
        """
        if await self.model_loaded(engine):
            return True
        s = await self._get_session()
        if s is None:
            return False
        try:
            async with s.post(
                f"{self.base_url}/models/load",
                json={"engine": engine, "model_size": model_size},
                timeout=aiohttp.ClientTimeout(total=LOAD_TIMEOUT_S),
            ) as r:
                ok = r.status == 200
                if not ok:
                    _log(f"model load returned HTTP {r.status}")
                return ok
        except TimeoutError:
            _log(
                f"model load timed out after {LOAD_TIMEOUT_S}s "
                "(the backend may be busy downloading another model)"
            )
            return False
        except Exception as e:
            _log(f"model load failed: {e}")
            return False

    async def list_profiles(self) -> list[dict]:
        """Every voice profile the backend knows about.

        Returns [] on any failure — ensure_profile treats "no profiles" and
        "could not ask" identically, because both mean it has to provision one.
        """
        s = await self._get_session()
        if s is None:
            return []
        try:
            async with s.get(
                f"{self.base_url}/profiles",
                timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S),
            ) as r:
                if r.status != 200:
                    return []
                body = await r.json(content_type=None)
        except Exception:
            return []
        if isinstance(body, list):
            return [p for p in body if isinstance(p, dict)]
        if isinstance(body, dict):
            rows = body.get("profiles") or body.get("items") or []
            return [p for p in rows if isinstance(p, dict)]
        return []

    async def _upload_clone_sample(self, profile_id: str) -> bool:
        """Attach the reference recording that a cloning engine learns from.

        LuxTTS ships NO preset voices — /profiles/presets/luxtts returns an
        empty list — so a profile without a sample can never speak. That makes
        this step part of provisioning, not an optional extra.
        """
        sample = Path(VOICE_CFG.clone_sample_path or "")
        if not sample.is_file():
            _log(f"clone sample not found: {sample}")
            return False
        s = await self._get_session()
        if s is None:
            return False
        try:
            form = aiohttp.FormData()
            form.add_field("reference_text", VOICE_CFG.clone_reference_text or "")
            form.add_field(
                "file",
                sample.read_bytes(),
                filename=sample.name,
                content_type="audio/wav",
            )
            async with s.post(
                f"{self.base_url}/profiles/{profile_id}/samples",
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                if r.status not in (200, 201):
                    _log(f"clone sample upload returned HTTP {r.status}: {await r.text()}")
                    return False
                _log(f"cloned voice from {sample.name}")
                return True
        except Exception as e:
            _log(f"clone sample upload failed: {e}")
            return False

    async def ensure_profile(self) -> str | None:
        """Return the id of Era's voice profile, creating it if absent.

        Auto-provisioning means a fresh machine needs no manual clicking in the
        Voicebox UI — which matters, because this profile is what Era falls back
        to when the cloud voice dies.

        Two shapes, decided by VOICE_CFG.engine:
          kokoro  a PRESET profile carrying a stock voice id.
          luxtts  a CLONED profile, provisioned with a reference sample. LuxTTS
                  has no presets, so the sample is mandatory — a cloned profile
                  without one exists but cannot speak, which would look like a
                  silent failure at the first utterance rather than at setup.
        """
        async with self._lock:
            if self._profile_id:
                return self._profile_id

            name = VOICE_CFG.profile_name
            cloning = VOICE_CFG.engine == "luxtts"
            voice_id = VOICE_CFG.kokoro_voice

            for p in await self.list_profiles():
                if str(p.get("name", "")).strip().lower() != name.lower():
                    continue
                if cloning:
                    # A cloned profile is identified by its name alone: the
                    # sample lives in a sub-resource, and re-uploading it on
                    # every launch would pile up duplicates.
                    self._profile_id = str(p.get("id"))
                    return self._profile_id
                # A voice change rebuilds the profile rather than mutating
                # it, so the stored preset always matches config/voice.json.
                if str(p.get("preset_voice_id") or "") == voice_id:
                    self._profile_id = str(p.get("id"))
                    return self._profile_id
                break

            s = await self._get_session()
            if s is None:
                return None
            if cloning:
                body = {
                    "name": name,
                    "description": "Era's local voice (cloned, auto-provisioned)",
                    "language": "en",
                    "voice_type": "cloned",
                    "default_engine": "luxtts",
                }
            else:
                body = {
                    "name": name,
                    "description": "Era's local voice (auto-provisioned)",
                    "language": "en",
                    "voice_type": "preset",
                    "preset_engine": "kokoro",
                    "preset_voice_id": voice_id,
                    "default_engine": "kokoro",
                }
            try:
                async with s.post(
                    f"{self.base_url}/profiles",
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status not in (200, 201):
                        _log(f"profile create returned HTTP {r.status}: {await r.text()}")
                        return None
                    data = await r.json()
                    profile_id = str(data.get("id"))
            except Exception as e:
                _log(f"profile create failed: {e}")
                return None

            if cloning and not await self._upload_clone_sample(profile_id):
                # Leave _profile_id unset: a cloned profile with no sample
                # cannot speak, and pretending it is ready would surface as
                # silence at the first utterance instead of here.
                _log("cloned profile has no sample; local voice is not ready")
                return None

            self._profile_id = profile_id
            label = "cloned" if cloning else voice_id
            _log(f"provisioned profile '{name}' ({label}) -> {profile_id}")
            return self._profile_id

    def invalidate_profile(self) -> None:
        """Drop the cached profile id, forcing a re-resolve on the next synth.

        Called when the Kokoro voice changes — the profile carries the preset
        voice, so a new voice means a different profile.
        """
        self._profile_id = None

    # ── Synthesis ────────────────────────────────────────────────────────────
    async def synthesize(self, text: str) -> bytes:
        """Synthesize `text` and return int16 mono PCM at 24 kHz. b"" on failure."""
        text = (text or "").strip()
        if not text:
            return b""
        profile_id = await self.ensure_profile()
        if not profile_id:
            return b""
        s = await self._get_session()
        if s is None:
            return b""

        body = {
            "profile_id": profile_id,
            "text": text,
            "language": "en",
            # The configured engine. Hardcoding kokoro here would silently
            # override a cloned profile's own engine and speak in a stock voice.
            "engine": VOICE_CFG.engine,
            "normalize": True,
        }
        try:
            async with s.post(
                f"{self.base_url}/generate/stream",
                json=body,
                timeout=aiohttp.ClientTimeout(total=SYNTH_TIMEOUT_S),
            ) as r:
                if r.status != 200:
                    _log(f"synthesis returned HTTP {r.status}: {(await r.text())[:200]}")
                    return b""
                wav = await r.read()
        except TimeoutError:
            _log(f"synthesis timed out after {SYNTH_TIMEOUT_S}s")
            return b""
        except Exception as e:
            _log(f"synthesis failed: {e}")
            return b""

        return wav_to_pcm24k(wav)


# Module-level singleton, mirroring how WAKE_CFG / VOICE_CFG are shared.
VOICEBOX = VoiceboxClient()
