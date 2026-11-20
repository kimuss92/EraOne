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


_UNREADABLE = object()


def _decode_lenient(value, path: str, stale: list[str]):
    """decode(), but a blob this Windows account cannot open is dropped and named.

    DPAPI blobs die together when the account's master key changes (a password
    RESET, a profile rebuild, a file synced in from another machine). Failing the
    whole file then turned one stale key into "no keys at all", and the HUD
    could not even load the form needed to re-enter them.
    """
    if isinstance(value, dict):
        if MARKER in value:
            try:
                return decode(value)
            except ProtectedConfigError:
                stale.append(path)
                return _UNREADABLE
        out = {}
        for key, item in value.items():
            decoded = _decode_lenient(item, f"{path}.{key}" if path else key, stale)
            if decoded is not _UNREADABLE:
                out[key] = decoded
        return out
    if isinstance(value, list):
        items = (_decode_lenient(item, f"{path}[{i}]", stale) for i, item in enumerate(value))
        return [item for item in items if item is not _UNREADABLE]
    return value


def read_config_lenient(path: Path) -> tuple[dict, list[str]]:
    """Readable settings plus the dotted paths of credentials that would not decrypt.

    Malformed JSON still raises: only undecryptable credentials are tolerated.
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ProtectedConfigError("Configuration root must be an object")
    stale: list[str] = []
    return _decode_lenient(data, "", stale), stale


def _secret_field(key: str) -> bool:
    name = key.lower()
    return any(part in name for part in ("api_key", "apikey", "token", "password", "secret"))


def seal(value, secret=False, *, strict=True):
    if isinstance(value, dict):
        if MARKER in value:
            if strict:
                decode(value)  # validate readability, never double-encrypt
            return value
        return {
            key: seal(item, secret or _secret_field(key), strict=strict)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [seal(item, secret, strict=strict) for item in value]
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


def _replace_atomically(path: Path, secured: dict, verify, expected_bytes: bytes | None) -> None:
    payload = json.dumps(secured, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".api_keys-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if not verify(temporary_path):
            raise ProtectedConfigError("Credential file read-back failed")
        current = path.read_bytes() if path.exists() else None
        if expected_bytes is not None and current != expected_bytes:
            raise ProtectedConfigError(
                "Configuration changed during migration; retry after closing editors"
            )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_config(path: Path, data: dict, *, expected_bytes: bytes | None = None) -> None:
    """Seal, verify and replace atomically. Never create a plaintext backup."""
    secured = seal(data)
    if decode(secured) != data:
        raise ProtectedConfigError("Credential round-trip verification failed")
    _replace_atomically(path, secured, lambda tmp: read_config(tmp) == data, expected_bytes)


def update_fields(path: Path, patch: dict) -> None:
    """Set top-level fields without needing to decrypt the rest of the file.

    Credentials this account cannot open are kept byte-for-byte rather than
    dropped, so re-entering one key never destroys the others — they may still
    open under the account or machine that sealed them.
    """
    original = path.read_bytes() if path.exists() else None
    raw = json.loads(original.decode("utf-8-sig")) if original else {}
    if not isinstance(raw, dict):
        raise ProtectedConfigError("Configuration root must be an object")
    expected, stale = read_config_lenient(path) if original else ({}, [])
    expected.update(patch)
    still_stale = sorted(p for p in stale if p.split(".", 1)[0].split("[", 1)[0] not in patch)
    raw.update(patch)
    secured = seal(raw, strict=False)

    def verify(tmp: Path) -> bool:
        data, now_stale = read_config_lenient(tmp)
        return data == expected and sorted(now_stale) == still_stale

    _replace_atomically(path, secured, verify, original)


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
