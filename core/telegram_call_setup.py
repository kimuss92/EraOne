"""Local-only authentication for Era's separate Telegram calling account.

This module does not start calls or enable an unfinished audio transport.
Telethon uses an in-memory session; only a DPAPI-sealed session is persisted.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.protected_config import read_config, write_config

CONFIG_FIELD = "telegram_call_credentials_secret"


class SetupFault(ValueError):
    """A controlled, non-secret diagnostic that is safe to show in the terminal."""

    def __init__(self, code: str, explanation: str) -> None:
        super().__init__(explanation)
        self.code = code
        self.explanation = explanation


def unbound_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep account credentials without persisting an unverified owner candidate."""
    clean = {
        key: value
        for key, value in settings.items()
        if key
        not in {
            "owner_username",
            "owner_user_id",
            "owner_access_hash_secret",
        }
    }
    return {
        **clean,
        "authenticated": False,
        "owner_bound": False,
        "calls_enabled": False,
    }


def normalize_phone(value: str) -> str:
    """Validate an explicit international account number without guessing a country."""
    cleaned = re.sub(r"[\s()-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", cleaned):
        raise ValueError("Use an international phone number beginning with + and country code.")
    return cleaned


def private_directory() -> Path:
    """Return a user-local location, independently of the project working directory."""
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise RuntimeError("Windows local app-data directory is unavailable.")
    return Path(root) / "EraOne" / "telegram-calls" / "private"


def secure_directory(directory: Path) -> None:
    """Restrict the credential folder to this Windows user and SYSTEM."""
    from win32api import GetCurrentProcess
    from win32security import (
        ConvertSidToStringSid,
        GetTokenInformation,
        OpenProcessToken,
        TokenUser,
    )

    directory.mkdir(parents=True, exist_ok=True)
    token = OpenProcessToken(GetCurrentProcess(), 8)  # TOKEN_QUERY
    try:
        sid = ConvertSidToStringSid(GetTokenInformation(token, TokenUser)[0])
    finally:
        token.Close()
    result = subprocess.run(
        [
            "icacls.exe",
            str(directory),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
        ],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Could not restrict the local call credential directory.")


def save_settings(directory: Path, settings: dict[str, Any]) -> None:
    """Atomically persist the entire payload as a Windows-user protected secret."""
    secure_directory(directory)
    write_config(directory / "account.json", {CONFIG_FIELD: json.dumps(settings)})


def load_settings(directory: Path) -> dict[str, Any]:
    """Read a protected setup payload; a missing file is an unconfigured account."""
    path = directory / "account.json"
    if not path.exists():
        return {}
    settings = json.loads(read_config(path)[CONFIG_FIELD])
    if not isinstance(settings, dict):
        raise ValueError("Invalid call-account settings.")
    return settings


def validate_setup(settings: dict[str, Any]) -> None:
    """Check local inputs before transmitting an authentication request."""
    era = normalize_phone(str(settings.get("era_phone", "")))
    owner = normalize_phone(str(settings.get("owner_phone", "")))
    if era == owner:
        raise ValueError("Era must use a separate Telegram account.")
    api_id = settings.get("api_id")
    if not isinstance(api_id, int) or isinstance(api_id, bool) or api_id <= 0:
        raise ValueError("A Telegram application API ID is required.")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", str(settings.get("api_hash", ""))):
        raise ValueError("A valid Telegram application API hash is required.")


def validate_identities(settings: dict[str, Any], me: Any, owner: Any) -> None:
    """Refuse personal-account login, bots, wrong owner binding or self calls."""
    if getattr(me, "bot", False) or getattr(owner, "bot", False):
        raise SetupFault("BOT_ACCOUNT", "Native calls require user accounts.")
    if str(getattr(me, "phone", "")) != normalize_phone(settings["era_phone"]).lstrip("+"):
        raise SetupFault(
            "ERA_ACCOUNT_MISMATCH",
            "Authenticated account is not the configured Era account.",
        )
    if me.id == owner.id:
        raise SetupFault("SELF_CALL", "Era cannot be bound to herself as the owner.")
    expected = settings.get("expected_owner_user_id")
    if expected and owner.id != int(expected):
        raise SetupFault(
            "OWNER_ID_MISMATCH",
            "Resolved owner differs from the existing Era bot owner. No binding was saved.",
        )
    phone = getattr(owner, "phone", None)
    if phone and str(phone) != normalize_phone(settings["owner_phone"]).lstrip("+"):
        raise SetupFault(
            "OWNER_PHONE_MISMATCH",
            "Resolved peer does not match the configured owner phone.",
        )
    if settings.get("owner_username") and not phone and not expected:
        raise SetupFault(
            "OWNER_UNVERIFIED",
            "A hidden-phone username needs the existing bot owner ID for verification.",
        )


async def authenticate(
    settings: dict[str, Any],
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Authenticate locally and resolve only the configured owner contact."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.contacts import ImportContactsRequest
    from telethon.tl.types import InputPhoneContact

    validate_setup(settings)
    client = TelegramClient(
        StringSession(settings.get("session_secret", "")),
        settings["api_id"],
        settings["api_hash"],
    )
    try:
        await client.start(
            phone=settings["era_phone"],
            code_callback=lambda: getpass.getpass("Telegram login code (hidden): "),
            password=lambda: getpass.getpass("Telegram two-step password (hidden): "),
        )
        me = await client.get_me()
        # Verify Era before any owner contact write.
        if getattr(me, "bot", False) or str(getattr(me, "phone", "")) != settings[
            "era_phone"
        ].lstrip("+"):
            raise SetupFault(
                "ERA_ACCOUNT_MISMATCH",
                "Login succeeded for a different account. Era session was not saved.",
            )
        # Preserve a verified Era login even if owner lookup fails later. This
        # prevents repeat OTP requests without enabling calls or trusting a peer.
        if checkpoint:
            checkpoint(
                {
                    **unbound_settings(settings),
                    "era_user_id": me.id,
                    "session_secret": client.session.save(),
                    "account_authenticated": True,
                    "authenticated": False,
                    "owner_bound": False,
                    "calls_enabled": False,
                }
            )
        try:
            owner = await client.get_entity(
                settings.get("owner_username") or settings["owner_phone"]
            )
        except ValueError:
            if settings.get("owner_username"):
                raise SetupFault(
                    "OWNER_USERNAME_UNRESOLVED",
                    "Telegram could not resolve that owner username.",
                ) from None
            # Import only the explicitly provided owner; never enumerate chats
            # or contacts, or import the user's personal Desktop session.
            imported = await client(
                ImportContactsRequest(
                    [
                        InputPhoneContact(
                            client_id=1,
                            phone=settings["owner_phone"],
                            first_name="Era owner",
                            last_name="",
                        ),
                    ]
                )
            )
            matches = [
                user
                for user in imported.users
                if any(
                    contact.client_id == 1 and contact.user_id == user.id
                    for contact in imported.imported
                )
            ]
            if len(matches) != 1:
                raise SetupFault(
                    "OWNER_PHONE_UNRESOLVED",
                    "Era login succeeded, but Telegram could not resolve your owner phone. Calls remain disabled.",
                ) from None
            owner = matches[0]
            print("Added only the configured owner contact to Era's account.")
        validate_identities(settings, me, owner)
        return {
            **settings,
            "era_user_id": me.id,
            "owner_user_id": owner.id,
            "owner_access_hash_secret": owner.access_hash,
            "session_secret": client.session.save(),
            "authenticated": True,
            "account_authenticated": True,
            "owner_bound": True,
            "calls_enabled": False,
        }
    finally:
        await client.disconnect()


def main() -> int:
    """Run an interactive local setup; never echo codes, hashes or sessions."""
    if not os.isatty(0):
        print("Run this setup in an interactive local terminal; credentials must stay local.")
        return 2
    directory = private_directory()
    try:
        settings = load_settings(directory)
        print("Era Telegram call account setup")
        print("Application API ID/hash: https://my.telegram.org/apps")
        print("Use Era's separate account. No native call will be placed during setup.")
        if not settings.get("era_phone"):
            settings["era_phone"] = normalize_phone(input("Era account phone (+country code): "))
        if not settings.get("owner_phone"):
            settings["owner_phone"] = normalize_phone(input("Owner phone (+country code): "))
        if not settings.get("api_id"):
            settings["api_id"] = int(input("Application API ID (not a phone number): ").strip())
        if not settings.get("api_hash"):
            settings["api_hash"] = getpass.getpass("Application API hash (hidden): ").strip()
        validate_setup(settings)
        # Preserve provided API credentials if login must be completed later.
        save_settings(directory, unbound_settings(settings))

        def checkpoint(value: dict[str, Any]) -> None:
            save_settings(directory, value)

        try:
            verified = asyncio.run(authenticate(settings, checkpoint=checkpoint))
        except SetupFault as exc:
            if exc.code != "OWNER_PHONE_UNRESOLVED":
                raise
            print(exc.explanation)
            print("Your verified Era session is saved. No need to repeat its login code.")
            username = (
                input("Your personal Telegram @username (or Enter to stop): ").strip().lstrip("@")
            )
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
                raise exc
            settings = load_settings(directory)
            settings["owner_username"] = username
            verified = asyncio.run(authenticate(settings, checkpoint=checkpoint))
        save_settings(directory, verified)
        print("Era account authenticated; owner bound. Session stored with Windows protection.")
        print("Calls remain disabled until the duplex audio bridge and live checks are complete.")
        return 0
    except SetupFault as exc:
        print(f"Setup stopped [{exc.code}]: {exc.explanation}")
        print("Calls remain disabled.")
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        # Telegram exception strings can contain account/authentication details.
        print(f"Setup did not complete ({type(exc).__name__}). Calls remain disabled.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
