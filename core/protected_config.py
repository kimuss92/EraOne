"""User-scoped Windows credential protection; ordinary settings remain editable."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path

MARKER = "$era_dpapi_v1"
_ENTROPY = b"EraOne/config/credentials/v1"
_LITERAL = re.compile(r"(?:sk-|AIza|AQ\.)[A-Za-z0-9_-]{20,}")


class ProtectedConfigError(RuntimeError):
    pass


def _protect(value: str) -> dict:
    import win32crypt

    blob = win32crypt.CryptProtectData(
        value.encode("utf-8"), "EraOne credential", _ENTROPY, None, None, 1
    )
    return {MARKER: base64.b64encode(blob).decode("ascii")}


def _unprotect(value: dict) -> str:
    import win32crypt

    try:
        blob = base64.b64decode(value[MARKER], validate=True)
        _, raw = win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, 1)
        return raw.decode("utf-8")
    except Exception:
        raise ProtectedConfigError(
            "Credential decryption failed for this Windows account"
        ) from None


def decode(value):
    if isinstance(value, dict):
        if MARKER in value:
            if set(value) != {MARKER} or not isinstance(value[MARKER], str):
                raise ProtectedConfigError("Invalid protected credential structure")
            return _unprotect(value)
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def _secret_field(key: str) -> bool:
    name = key.lower()
    return any(part in name for part in ("api_key", "apikey", "token", "password", "secret"))


def seal(value, secret=False):
    if isinstance(value, dict):
        if MARKER in value:
            decode(value)  # validate readability, never double-encrypt
            return value
        return {key: seal(item, secret or _secret_field(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [seal(item, secret) for item in value]
    if isinstance(value, str) and value.strip() and (secret or _LITERAL.search(value)):
        upper = value.strip().upper()
        if not (
            upper.startswith("YOUR_")
            or upper in ("CHANGE_ME", "PLACEHOLDER")
            or value.startswith("<")
        ):
            return _protect(value)
    return value


def read_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ProtectedConfigError("Configuration root must be an object")
    return decode(data)


def write_config(path: Path, data: dict, *, expected_bytes: bytes | None = None) -> None:
    """Seal, verify and replace atomically. Never create a plaintext backup."""
    secured = seal(data)
    if decode(secured) != data:
        raise ProtectedConfigError("Credential round-trip verification failed")
    payload = json.dumps(secured, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".api_keys-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if read_config(temporary_path) != data:
            raise ProtectedConfigError("Credential file read-back failed")
        if expected_bytes is not None and path.read_bytes() != expected_bytes:
            raise ProtectedConfigError(
                "Configuration changed during migration; retry after closing editors"
            )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def migrate(path: Path) -> bool:
    original = path.read_bytes()
    data = read_config(path)
    raw = json.loads(original.decode("utf-8-sig"))
    # Avoid rewriting a fully protected file just to generate new random blobs.
    if decode(raw) == data and not has_plaintext_credentials(raw):
        return False
    write_config(path, data, expected_bytes=original)
    return True


def has_plaintext_credentials(value, secret=False) -> bool:
    if isinstance(value, dict):
        if MARKER in value:
            return False
        return any(
            has_plaintext_credentials(item, secret or _secret_field(key))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(has_plaintext_credentials(item, secret) for item in value)
    if isinstance(value, str) and value.strip() and (secret or _LITERAL.search(value)):
        upper = value.strip().upper()
        return not (
            upper.startswith("YOUR_")
            or upper in ("CHANGE_ME", "PLACEHOLDER")
            or value.startswith("<")
        )
    return False
