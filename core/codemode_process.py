"""Owned verification subprocesses with bounded cancellation and tree cleanup."""

import os
import signal
import subprocess
import time

from core.win_subprocess import NO_WINDOW


def kill_tree(proc):
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=NO_WINDOW,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.poll() is None:
            proc.kill()
    proc.communicate(timeout=5)


def run_verifier(command, stage, env, cancelled):
    if cancelled.is_set():
        raise ValueError("Development verification cancelled")
    proc = subprocess.Popen(
        command,
        cwd=stage,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
        **({"start_new_session": True} if os.name != "nt" else {}),
    )
    deadline = time.monotonic() + 600
    try:
        while True:
            if cancelled.is_set():
                raise ValueError("Development verification cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Development verification timed out")
            try:
                out, err = proc.communicate(timeout=0.1)
                return subprocess.CompletedProcess(command, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                continue
    finally:
        kill_tree(proc)
