"""Pinned local REA CLI integration for deeper static reverse engineering."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from core.cancel_registry import raise_if_cancelled
from core.win_subprocess import NO_WINDOW

MODES = {
    "javascript": "analyze-javascript-application",
    "managed": "inspect-managed-artifact",
    "wasm": "inspect-wasm-artifact",
    "artifact": "inspect-artifact",
    "decompile": "decompile",
}


def provider_environment() -> dict[str, str]:
    path = Path.home() / ".era/tools/native/providers.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"GHIDRA_INSTALL_DIR", "JAVA_HOME", "REA_WABT_BIN_DIRECTORY"}
    if (
        not isinstance(data, dict)
        or set(data) - allowed
        or any(not isinstance(v, str) or not Path(v).is_dir() for v in data.values())
    ):
        raise ValueError("Invalid portable analysis provider configuration")
    return data


def cli_path() -> Path:
    from core.empower_config import read

    configured = read().get("rea_cli_path")
    return (
        Path(configured)
        if configured
        else Path.home() / ".era/tools/rea/node_modules/rea-agents/scripts/rea.mjs"
    )


def run(mode: str, arguments: dict) -> dict:
    """Never launch inspected programs; only fixed REA analysis commands run."""
    if mode not in MODES:
        raise ValueError("Unsupported REA analysis mode")
    raw = arguments.get("target")
    if not isinstance(raw, str) or not raw or len(raw) > 2000:
        raise ValueError("Provide an explicit local artifact path")
    target = Path(raw).expanduser().resolve()
    if not target.exists():
        raise ValueError("Target does not exist")
    node, script = shutil.which("node"), cli_path()
    if not node or not script.is_file():
        raise ValueError(
            "REA is not installed. Run tools/install_era_capabilities.ps1 or set its CLI path in Settings."
        )
    command = [node, str(script), MODES[mode], str(target)]
    if mode == "javascript":
        command += ["--analysis-timeout-ms", "30000", "--max-heap-mb", "512"]
    if mode == "decompile":
        procedure = arguments.get("procedure")
        if not isinstance(procedure, str) or not procedure or len(procedure) > 200:
            raise ValueError(
                "Decompile needs an exact procedure name/address and an installed analysis provider"
            )
        command += ["--procedure=" + procedure, "--provider", "ghidra"]
    command += ["--format", "json"]
    timeout_seconds = 180 if mode == "decompile" else 90
    with tempfile.TemporaryDirectory(prefix="era-rea-") as directory:
        stdout, stderr = Path(directory) / "stdout", Path(directory) / "stderr"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=NO_WINDOW,
                env={
                    **{
                        k: v
                        for k, v in os.environ.items()
                        if not any(
                            s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
                        )
                    },
                    **provider_environment(),
                },
            )
            started = time.monotonic()
            try:
                while process.poll() is None:
                    raise_if_cancelled()
                    if (
                        time.monotonic() - started > timeout_seconds
                        or stdout.stat().st_size + stderr.stat().st_size > 20_000_000
                    ):
                        raise TimeoutError(
                            "REA exceeded its time/output budget; no completed analysis is claimed"
                        )
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW,
                            timeout=10,
                        )
                    else:
                        process.kill()
                    process.wait(timeout=10)
        if process.returncode != 0:
            detail = (
                stderr.read_text(encoding="utf-8", errors="replace")[-2000:]
                or stdout.read_text(encoding="utf-8", errors="replace")[-2000:]
            )
            raise ValueError("REA analysis failed: " + detail)
        if stdout.stat().st_size > 20_000_000:
            raise ValueError("REA result exceeded the output budget")
        raw_result = stdout.read_text(encoding="utf-8", errors="replace")
        result = json.loads(raw_result)
        if isinstance(result, dict) and (result.get("error") or result.get("success") is False):
            raise ValueError("REA reported an incomplete/failed analysis: " + str(result)[:1200])
        from core.empower_config import output_dir

        folder = output_dir()
        raise_if_cancelled()
        folder.mkdir(parents=True, exist_ok=True)
        artifact = folder / ("rea-evidence-" + uuid.uuid4().hex[:12] + ".json")
        with artifact.open("x", encoding="utf-8") as stream:
            stream.write(raw_result)
        return {
            "success": True,
            "evidence_path": str(artifact),
            "mode": mode,
            "evidence": result if len(raw_result) <= 16000 else raw_result[:16000],
            "truncated": len(raw_result) > 16000,
            "note": "REA evidence retained locally. Read its limitations and unknowns; analysis does not prove runtime behavior or a complete reconstruction.",
        }
