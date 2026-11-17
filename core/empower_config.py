"""Validated capability settings; credentials stay in Era's DPAPI config store."""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from urllib.parse import urlsplit

from core import protected_config
from core.utils import CONFIG_PATH

_lock = threading.RLock()
ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "rea_cli_path": (
        "REA analysis CLI path",
        "path",
        str(Path.home() / ".era/tools/rea/node_modules/rea-agents/scripts/rea.mjs"),
    ),
    "gemini_api_key": ("Gemini voice key", "secret", ""),
    "gemini_vision_api_key": ("Gemini vision key", "secret", ""),
    "gemini_vision_model": ("Gemini vision model", "text", "gemini-3.8-flash"),
    "ollama_base_url": ("Ollama address", "url", "http://127.0.0.1:11434"),
    "ollama_vision_model": ("Ollama vision model", "text", "qwen2.5vl:3b"),
    "empower_output_dir": ("Created documents folder", "path", str(ROOT / "runtime/deliverables")),
    "home_assistant_url": ("Home Assistant address", "url", ""),
    "home_assistant_token": ("Home Assistant access token", "secret", ""),
}
DEPENDENCIES = {
    "Word": "docx",
    "Excel": "openpyxl",
    "PowerPoint": "pptx",
    "PDF creation": "reportlab",
    "PDF merge/extract": "pypdf",
    "Binary inspection": "pefile",
    "Floating launcher": "PyQt6",
    "Pairing QR": "qrcode",
    "Desktop inspection": "pywinauto",
}


def _read_with_stale() -> tuple[dict, list[str]]:
    if not CONFIG_PATH.exists():
        return {}, []
    return protected_config.read_config_lenient(CONFIG_PATH)


def read() -> dict:
    """Fail closed on damaged JSON; an undecryptable key just reads as unset."""
    return _read_with_stale()[0]


def snapshot() -> dict:
    cfg, stale = _read_with_stale()
    note = "Keys are encrypted for your Windows account. Voice key changes apply on reconnect. Vision changes apply on the next request."
    if stale:
        note = (
            "Some saved keys cannot be decrypted on this Windows account "
            "(sealed before a password reset or on another PC) and must be re-entered. "
        ) + note
    return {
        "fields": [
            {
                "key": key,
                "label": label,
                "kind": kind,
                "value": "" if kind == "secret" else cfg.get(key, default),
                "configured": bool(cfg.get(key)),
                "stale": key in stale,
            }
            for key, (label, kind, default) in FIELDS.items()
        ],
        "dependencies": {
            label: importlib.util.find_spec(module) is not None
            for label, module in DEPENDENCIES.items()
        },
        "note": note,
    }


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use an http(s) address without credentials, query or fragment")
    _ = parsed.port
    return value.rstrip("/")


def update(patch: dict) -> dict:
    if not isinstance(patch, dict) or not patch or set(patch) - FIELDS.keys():
        raise ValueError("Unknown or empty capability settings")
    clean = {}
    for key, value in patch.items():
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise ValueError(f"Invalid value for {key}")
        value = value.strip()
        kind = FIELDS[key][1]
        if kind == "url" and value:
            value = validate_url(value)
        if kind == "path":
            p = Path(value).expanduser()
            if not p.is_absolute() or value.startswith(("\\\\", "//")):
                raise ValueError("Output folder must be an absolute local path")
            value = str(p)
        clean[key] = value
    with _lock:
        # Patch only these fields: re-entering one key must work even while
        # others are undecryptable, and must not destroy those others.
        protected_config.update_fields(CONFIG_PATH, clean)
    return snapshot()


def output_dir() -> Path:
    return Path(read().get("empower_output_dir") or FIELDS["empower_output_dir"][2]).expanduser()


def test_connection(service: str) -> dict:
    import requests

    cfg = read()
    if service == "ollama":
        url = validate_url(cfg.get("ollama_base_url") or FIELDS["ollama_base_url"][2]) + "/api/tags"
        headers = {}
    elif service == "home_assistant":
        if not cfg.get("home_assistant_url") or not cfg.get("home_assistant_token"):
            return {"ok": False, "message": "Enter the Home Assistant address and token first."}
        url = validate_url(cfg["home_assistant_url"]) + "/api/"
        headers = {"Authorization": "Bearer " + cfg["home_assistant_token"]}
    elif service == "gemini":
        key = cfg.get("gemini_api_key")
        if not key:
            return {"ok": False, "message": "Enter the Gemini key first."}
        url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
        headers = {"x-goog-api-key": key}
    else:
        raise ValueError("Unknown connection test")
    try:
        response = requests.get(url, headers=headers, timeout=8, allow_redirects=False)
        return {
            "ok": response.status_code == 200,
            "message": f"Service returned HTTP {response.status_code}.",
        }
    except requests.RequestException:
        return {
            "ok": False,
            "message": "Connection failed. Check address, network and credentials.",
        }
