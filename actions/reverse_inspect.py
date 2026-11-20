"""Bounded, read-only inspection: input files and web pages are never executed."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

_SKIP = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "config",
    "memory",
    "logs",
    "runtime",
}
_SOURCE = {".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".json", ".md", ".toml", ".yaml"}


def _file(path: Path) -> dict:
    raw = path.read_bytes()
    result = {"name": path.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if path.suffix.lower() == ".py":
        tree = ast.parse(raw.decode("utf-8-sig", errors="replace"))
        result["symbols"] = [
            {"name": n.name, "kind": type(n).__name__, "line": n.lineno}
            for n in ast.walk(tree)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ][:300]
        result["imports"] = sorted(
            {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        )
    elif raw[:2] == b"MZ":
        import pefile

        pe = pefile.PE(data=raw, fast_load=True)
        try:
            pe.parse_data_directories(
                directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
            )
            result["pe"] = {
                "machine": pe.FILE_HEADER.Machine,
                "entry_point": pe.OPTIONAL_HEADER.AddressOfEntryPoint,
                "sections": [
                    {
                        "name": s.Name.rstrip(b"\x00").decode("ascii", "replace"),
                        "size": s.SizeOfRawData,
                    }
                    for s in pe.sections
                ],
                "imports": [
                    {
                        "dll": e.dll.decode("ascii", "replace"),
                        "functions": [
                            (i.name or b"ordinal").decode("ascii", "replace") for i in e.imports
                        ][:200],
                    }
                    for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
                ][:100],
            }
        finally:
            pe.close()
    else:
        result["format"] = (
            "wasm"
            if raw[:4] == b"\x00asm"
            else "text"
            if path.suffix.lower() in _SOURCE
            else "binary"
        )
        result["strings"] = [
            s.decode("ascii") for s in re.findall(rb"[ -~]{8,180}", raw[:2_000_000])[:80]
        ]
    return result


def _web(url: str) -> dict:
    import requests
    from bs4 import BeautifulSoup

    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Use an http(s) page URL without credentials")
    with requests.get(url, timeout=15, stream=True, allow_redirects=False) as response:
        if response.is_redirect:
            raise ValueError("Page redirects; inspect its final URL explicitly")
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_content(16384):
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise ValueError("Page exceeds the 2 MB inspection limit")
    soup = BeautifulSoup(bytes(raw), "html.parser")
    references = sorted(
        {
            urljoin(url, tag.get("src") or tag.get("href") or "")
            for tag in soup.find_all(["script", "link", "a"])
        }
    )[:150]
    endpoints = sorted(
        set(
            re.findall(
                r"""["']((?:/api/|/v[1-9]/)[^"'\s<>]{1,200})["']""", raw.decode("utf-8", "replace")
            )
        )
    )[:100]
    return {
        "target": url,
        "title": soup.title.get_text() if soup.title else "",
        "bytes": len(raw),
        "headings": [x.get_text(" ", strip=True)[:300] for x in soup.find_all(["h1", "h2", "h3"])][
            :80
        ],
        "references": references,
        "api_references": endpoints,
        "note": "Static HTML references only; no scripts executed, authenticated traffic captured, or API behavior verified.",
    }


def run(target: str) -> dict:
    if not isinstance(target, str) or not target.strip() or len(target) > 2000:
        raise ValueError("A file, source folder or page URL is required")
    if target.startswith(("https://", "http://")):
        return {"success": True, "analysis": _web(target)}
    path = Path(target).expanduser().resolve()
    if not path.exists():
        raise ValueError("Target does not exist")
    if path.is_dir():
        import os

        files = []
        total = 0
        for folder, dirs, names in os.walk(path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP and not d.startswith("."))
            for name in sorted(names):
                item = Path(folder) / name
                if (
                    item.is_symlink()
                    or item.suffix.lower() not in _SOURCE
                    or name.startswith(".")
                    or re.search(r"secret|credential|api_keys|token", name, re.I)
                ):
                    continue
                if item.stat().st_size > 1_000_000:
                    continue
                total += item.stat().st_size
                if total > 10_000_000 or len(files) >= 150:
                    return {"success": True, "target": str(path), "files": files, "truncated": True}
                try:
                    info = _file(item)
                    info["name"] = item.relative_to(path).as_posix()
                    files.append(info)
                except (OSError, SyntaxError, ValueError) as exc:
                    files.append(
                        {"name": item.relative_to(path).as_posix(), "error": type(exc).__name__}
                    )
        return {"success": True, "target": str(path), "files": files, "truncated": False}
    if path.stat().st_size > 50_000_000:
        raise ValueError("File exceeds the 50 MB inspection limit")
    result = _file(path)
    # Ensure callers always get a provider-serializable report.
    json.dumps(result)
    return {
        "success": True,
        "analysis": result,
        "note": "Static inspection only. No target code was executed or decompiled.",
    }
