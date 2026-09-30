"""Voicebox backend lifecycle — keeps the local voice available 24/7.

Era's kokoro_text mode is the escape hatch for a Gemini quota outage, so it is
only worth anything if the backend is *already up* when the outage hits. This
supervisor owns that: probe, spawn if down, keep Kokoro preloaded, restart on
crash with a growing backoff.

Two storage knobs are passed on EVERY spawn, not just the first:

    VOICEBOX_MODELS_DIR (env)  → model downloads; Voicebox assigns it straight
                                 into HF_HUB_CACHE (backend/config.py:16-18)
    --data-dir          (arg)  → db, profiles, generations, cache

Voicebox otherwise defaults to ``Path("data").resolve()`` under its working
directory and the HuggingFace cache under the user profile — both on C:, which
this machine has the least room on. Setting them only at install time would mean
a supervisor-initiated restart silently reverts to C:.

Degrade, never raise: a backend that will not start costs the local voice, never
Era's process.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app.reconnect import grow_cooldown
from core.tts_voicebox import VOICEBOX
from core.voice_mode import VOICE_CFG, get_voicebox_config

# Backoff for respawn attempts. Deliberately much shorter than the Gemini quota
# cooldown: a local process that died should come back in seconds, not minutes.
RESTART_BASE_S = 5.0
RESTART_CAP_S = 300.0
HEALTH_POLL_S = 20.0

# A cold Kokoro load includes a possible first-run model download.
STARTUP_GRACE_S = 180.0


def _log(msg: str) -> None:
    print(f"[TTS-SUP] {msg}")


def discover_backend_cmd() -> list[str] | None:
    """Locate the Voicebox backend, preferring the bundled binary.

    The desktop app ships a PyInstaller-frozen ``voicebox-backend`` carrying its
    own torch/transformers stack. Era must NOT try to run the backend under its
    own interpreter unless that is the only option — Era runs on Python 3.14 and
    the backend's ML dependencies are not installed there.
    """
    cfg = get_voicebox_config()
    explicit = (cfg.get("backend_cmd") or "").strip()
    if explicit:
        return explicit.split() if " " in explicit else [explicit]

    # The shipped binary is voicebox-server(.exe); voicebox-backend is the name
    # used when running the backend from source. Try both.
    names = [
        "voicebox-server.exe",
        "voicebox-server",
        "voicebox-backend.exe",
        "voicebox-backend",
    ]
    roots = [
        Path(r"D:\Program Files\Voicebox"),
        Path(cfg["data_dir"]).parent,
        Path(r"D:\Voicebox"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Voicebox",
        Path(os.environ.get("PROGRAMFILES", "")) / "Voicebox",
        Path(r"C:\Program Files\Voicebox"),
    ]
    for root in roots:
        if not str(root) or not root.exists():
            continue
        for name in names:
            direct = root / name
            if direct.is_file():
                return [str(direct)]
            try:
                for found in root.rglob(name):
                    if found.is_file():
                        return [str(found)]
            except OSError:
                continue

    for candidate in ("voicebox-server", "voicebox-backend"):
        on_path = shutil.which(candidate)
        if on_path:
            return [on_path]

    _log("no voicebox-server binary found; install Voicebox or set voicebox.backend_cmd")
    return None


class VoiceboxSupervisor:
    """Probe / spawn / restart the local Voicebox backend."""

    def __init__(self, ui=None) -> None:
        self.ui = ui
        self._proc: subprocess.Popen | None = None
        self._task: asyncio.Task | None = None
        self._backoff = RESTART_BASE_S
        self._stopping = False
        self._model_ready = False
        # True once a health probe has ever succeeded. Distinguishes "still
        # starting" from "cannot start", which the user needs to tell apart.
        self._ever_healthy = False

    # ── Status ───────────────────────────────────────────────────────────────
    def _status(self, text: str) -> None:
        """Surface state to the UI. Never let a UI failure break the supervisor."""
        _log(text)
        try:
            if self.ui and hasattr(self.ui, "set_status"):
                self.ui.set_status(text)
        except Exception:
            pass

    @property
    def model_ready(self) -> bool:
        """True when Kokoro is loaded and a synth should be fast."""
        return self._model_ready

    # ── Spawn ────────────────────────────────────────────────────────────────
    def _spawn(self) -> bool:
        cfg = get_voicebox_config()
        cmd = discover_backend_cmd()
        if not cmd:
            return False

        models_dir = Path(cfg["models_dir"])
        data_dir = Path(cfg["data_dir"])
        try:
            models_dir.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            _log(f"could not create storage dirs ({e}); refusing to spawn onto C: defaults")
            return False

        env = dict(os.environ)
        # Both are required. VOICEBOX_MODELS_DIR is read at backend import time
        # and assigned into HF_HUB_CACHE, so it must be in the CHILD env — it
        # cannot be applied after the process is up.
        env["VOICEBOX_MODELS_DIR"] = str(models_dir)
        env["HF_HUB_CACHE"] = str(models_dir)
        env["HF_HOME"] = str(models_dir)

        argv = [
            *cmd,
            "--host",
            cfg["host"],
            "--port",
            str(cfg["port"]),
            "--data-dir",
            str(data_dir),
        ]

        creationflags = 0
        if sys.platform == "win32":
            # Detach the console so the backend does not steal focus or die with
            # Era's terminal, and so Ctrl-C in Era's console is not forwarded.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._proc = subprocess.Popen(
                argv,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._status(f"Starting local voice backend (models: {models_dir})")
            return True
        except Exception as e:
            _log(f"spawn failed: {e}")
            self._proc = None
            return False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def ensure_running(self) -> bool:
        """Make the backend healthy, spawning it if needed. True if healthy."""
        if await VOICEBOX.health():
            self._ever_healthy = True
            return True
        # Someone else's instance may own the port; only spawn if we have none.
        if not self._alive() and not self._spawn():
            return False
        return await self._await_health(STARTUP_GRACE_S)

    async def _await_health(self, timeout_s: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if self._stopping:
                return False
            if await VOICEBOX.health():
                self._ever_healthy = True
                return True
            # A process that exited during startup will never become healthy;
            # bail out immediately rather than burning the whole grace period.
            if self._proc is not None and self._proc.poll() is not None:
                _log(f"backend exited during startup (code {self._proc.returncode})")
                return False
            await asyncio.sleep(1.0)
        return False

    async def preload(self) -> bool:
        """Load Kokoro and provision Era's profile so the first line is instant.

        Kept warm regardless of the active voice mode — the point is that it is
        ready the moment Gemini's quota dies, and a first-run model download
        during an outage would defeat the whole design.
        """
        if not await VOICEBOX.health():
            return False
        self._status("Loading local voice model…")
        # The CONFIGURED engine, not a hardcoded one: switching Era's local
        # voice must be a config edit, and preloading kokoro while synthesis
        # asks for luxtts would warm the wrong model on every launch.
        ok = await VOICEBOX.load_model(VOICE_CFG.engine)
        if ok:
            ok = bool(await VOICEBOX.ensure_profile())
        self._model_ready = ok
        self._status("Local voice ready" if ok else "Local voice model failed to load")
        return ok

    async def start(self) -> None:
        """Bring the backend up and begin supervising it."""
        self._stopping = False

        # Voicebox not installed at all: there is nothing to supervise, so do
        # not start a watchdog that re-discovers its absence forever. Era stays
        # on the cloud voice, and set_voice refuses the switch (model_ready is
        # False) rather than leaving her with no voice.
        if not await VOICEBOX.health() and discover_backend_cmd() is None:
            self._status(
                "Local voice unavailable — Voicebox is not installed. "
                "Era will stay on her cloud voice."
            )
            return

        if await self.ensure_running():
            await self.preload()
        elif not self._ever_healthy:
            self._status("Local voice unavailable — Voicebox backend could not be started")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        """Poll health; respawn with a growing backoff on crash.

        A healthy process is not the same as a loaded model: /health only
        checks the HTTP endpoint is answering, so a preload() that failed once
        (e.g. a transient WinError 64 right after boot) left model_ready stuck
        False forever on an otherwise-healthy backend, because a retry used to
        only happen on the crash/restart path below. Retrying here closes that
        gap without waiting for an actual crash.
        """
        while not self._stopping:
            try:
                await asyncio.sleep(HEALTH_POLL_S)
                if self._stopping:
                    break
                if await VOICEBOX.health():
                    self._backoff = RESTART_BASE_S  # recovered
                    if not self._model_ready:
                        await self.preload()
                    continue

                self._model_ready = False
                self._status("Local voice backend down — restarting…")
                if await self.ensure_running():
                    await self.preload()
                    self._backoff = RESTART_BASE_S
                else:
                    await asyncio.sleep(self._backoff)
                    self._backoff = grow_cooldown(self._backoff, RESTART_CAP_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log(f"watchdog error: {e}")
                await asyncio.sleep(self._backoff)
                self._backoff = grow_cooldown(self._backoff, RESTART_CAP_S)

    async def stop(self, *, terminate_backend: bool = False) -> None:
        """Stop supervising.

        ``terminate_backend`` defaults to False so Era shutting down does not
        kill a backend the user is also using from the Voicebox desktop app —
        and because Era only ever spawned it as a convenience.
        """
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        if terminate_backend and self._alive():
            try:
                self._proc.terminate()
            except Exception:
                pass
        await VOICEBOX.close()
