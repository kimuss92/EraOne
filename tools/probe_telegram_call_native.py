"""Offline native external-PCM probe with an isolated bounded child process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any


async def native_probe(controlled_exit: bool = False) -> dict[str, Any]:
    """Configure synthetic PCM only; no login, call signaling or device sources."""
    from ntgcalls import (
        AudioDescription,
        FrameData,
        MediaDescription,
        MediaSource,
        NTgCalls,
        StreamDevice,
        StreamMode,
    )

    binding = NTgCalls()
    fixture_id = 987654321
    created = False
    try:
        await binding.create_p2p_call(fixture_id)
        created = True
        await binding.set_stream_sources(
            fixture_id,
            StreamMode.CAPTURE,
            MediaDescription(
                AudioDescription(MediaSource.EXTERNAL, 24000, 1, "", False),
                None,
                None,
                None,
            ),
        )
        await binding.set_stream_sources(
            fixture_id,
            StreamMode.PLAYBACK,
            MediaDescription(
                AudioDescription(MediaSource.EXTERNAL, 16000, 1, "", False),
                None,
                None,
                None,
            ),
        )
        await binding.send_external_frame(
            fixture_id, StreamDevice.MICROPHONE, bytes(480), FrameData()
        )
        result = {
            "sources_configured": True,
            "frame_submitted": True,
            "input_rate": 16000,
            "output_rate": 24000,
            "remote_audio_verified": False,
        }
    finally:
        if created:
            await binding.stop(fixture_id)
    result["connection_stopped"] = True
    result["controlled_process_exit"] = controlled_exit
    result["native_finalization_verified"] = False
    # Emit the result before finalization, allowing the parent to distinguish
    # successful native operations from a hanging native destructor.
    print(json.dumps(result), flush=True)
    if controlled_exit:
        # Windows native DLL teardown also hangs through os._exit. Hold this
        # disposable helper after endpoint teardown; the owning supervisor reaps
        # it at its deadline with TerminateProcess, bypassing DLL finalizers.
        await asyncio.Event().wait()
    return result


def run_probe(
    interpreter: Path, timeout: float = 8.0, *, controlled_exit: bool = True
) -> dict[str, Any]:
    """Reap only the owned child on timeout; never search/kill Era processes."""
    timed_out = False
    try:
        command = [str(interpreter), str(Path(__file__).resolve()), "--child"]
        if controlled_exit:
            command.append("--controlled-exit")
        child = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        raw = child.stdout
        cleanup = child.returncode == 0
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        raw = exc.stdout or b""
        cleanup = False
    try:
        lines = raw.decode("utf-8").splitlines()
        result = next(json.loads(line) for line in reversed(lines) if line.startswith("{"))
        if not isinstance(result, dict):
            raise ValueError("Invalid native probe result")
    except (UnicodeError, StopIteration, ValueError):
        result = {
            "sources_configured": False,
            "frame_submitted": False,
            "connection_stopped": False,
            "remote_audio_verified": False,
        }
    result["clean_process_exit"] = cleanup
    result["supervisor_reaped_child"] = timed_out
    result["shutdown_complete"] = bool(result.get("connection_stopped") and (cleanup or timed_out))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--controlled-exit", action="store_true")
    parser.add_argument("--natural-exit", action="store_true")
    args = parser.parse_args()
    if args.child:
        asyncio.run(native_probe(controlled_exit=args.controlled_exit))
        return 0
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("Windows local app-data directory unavailable.")
    interpreter = Path(local) / "EraOne" / "telegram-calls" / ".venv" / "Scripts" / "python.exe"
    result = run_probe(interpreter, controlled_exit=not args.natural_exit)
    print(json.dumps(result))
    if not all(
        result.get(key) for key in ("sources_configured", "frame_submitted", "connection_stopped")
    ):
        return 1
    return 0 if result["shutdown_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
