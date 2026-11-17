"""Install checksum-pinned portable Ghidra/JDK/WABT without global PATH changes."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PACKAGES = [
    (
        "ghidra",
        "https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip",
        "ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db",
    ),
    (
        "jdk",
        "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip",
        "f9d6e191ab098c0d416e7d588a24420a8621cd2f4720dab2459b8b7b2d2d8b4e",
    ),
    (
        "wabt",
        "https://github.com/WebAssembly/wabt/releases/download/1.0.42/wabt-1.0.42-windows-x64.tar.gz",
        "62f1eb2b51aa57cf0f0b8ba333bf2c72a049930aa9c5de01dcad271c4fe48c88",
    ),
]


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install(package: tuple[str, str, str], root: Path) -> None:
    name, url, digest = package
    archive = root / (name + (".tar.gz" if url.endswith(".tar.gz") else ".zip"))
    if not archive.exists() or checksum(archive) != digest:
        part = archive.with_suffix(archive.suffix + ".part")
        print(f"Downloading {name} from its official release…", flush=True)
        with urllib.request.urlopen(url, timeout=60) as response, part.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        if checksum(part) != digest:
            raise ValueError(f"Checksum mismatch for {name}; archive not extracted")
        os.replace(part, archive)
    destination = root / name
    marker = destination / ".verified-sha256"
    if marker.exists() and marker.read_text() == digest:
        print(f"{name} already installed", flush=True)
        return
    destination.mkdir(exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()) or ":" in member.filename:
                    raise ValueError("Archive contains an unsafe path")
            z.extractall(destination)
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(destination, filter="data")
    marker.write_text(digest)
    print(f"Installed and checksum-verified {name}", flush=True)


def main() -> None:
    root = Path.home() / ".era/tools/native"
    root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(install, package, root) for package in PACKAGES]
        for future in futures:
            future.result()
    ghidra = next((root / "ghidra").glob("*/support/analyzeHeadless.bat")).parents[1]
    java = next((root / "jdk").glob("*/bin/java.exe")).parents[1]
    wabt = next((root / "wabt").rglob("wasm2wat.exe")).parent
    manifest = {
        "GHIDRA_INSTALL_DIR": str(ghidra),
        "JAVA_HOME": str(java),
        "REA_WABT_BIN_DIRECTORY": str(wabt),
    }
    (root / "providers.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Portable analysis providers ready; no system environment changed.", flush=True)


if __name__ == "__main__":
    main()
