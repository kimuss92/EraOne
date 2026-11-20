"""Isolated, resumable HUD setup. Only controlled status leaves this process."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import uuid

from core.protected_config import ProtectedConfigError
from core.telegram_call_setup import (
    SetupFault,
    authenticate,
    load_settings,
    normalize_phone,
    private_directory,
    save_settings,
    validate_setup,
)


def draft_directory():
    return private_directory() / "setup-draft"


def status():
    try:
        current = load_settings(private_directory())
        configured = current.get("owner_bound") is True
        locked = False
    except ProtectedConfigError:
        configured, locked = False, True
    draft = load_settings(draft_directory())
    return {
        "step": draft.get("setup_step", "details"),
        "configured": configured,
        "locked": locked,
        "hasDraft": bool(draft),
    }


async def run(data):
    action = data.get("action", "status")
    if action == "status":
        return status()
    if action == "cancel":
        (draft_directory() / "account.json").unlink(missing_ok=True)
        return status()
    if action == "start":
        # A new draft never overwrites the working calling account.
        s = {
            "era_phone": normalize_phone(data.get("era_phone", "")),
            "owner_phone": normalize_phone(data.get("owner_phone", "")),
            "api_id": int(data.get("api_id", 0)),
            "api_hash": data.get("api_hash", ""),
        }
        validate_setup(s)
        owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if owner.isdigit() and int(owner) > 0:
            s["expected_owner_user_id"] = int(owner)
        else:
            try:
                previous = load_settings(private_directory())
            except ProtectedConfigError:
                previous = {}
            previous_owner = previous.get("expected_owner_user_id")
            if type(previous_owner) is int and previous_owner > 0:
                s["expected_owner_user_id"] = previous_owner
        save_settings(draft_directory(), s)
    else:
        s = load_settings(draft_directory())
        if not s:
            raise SetupFault("NO_DRAFT", "Start account setup first.")
    if action == "enable":
        from core.telegram_call_activation import daily_settings

        enabled = daily_settings(s, True)
        enabled.pop("setup_step", None)
        enabled.pop("phone_code_hash", None)
        account = private_directory() / "account.json"
        if account.exists():
            # Copy ciphertext within the existing private directory before replacement.
            backup = account.with_name(f"account.before-setup-{uuid.uuid4().hex}.json")
            backup.write_bytes(account.read_bytes())
        save_settings(private_directory(), enabled)
        (draft_directory() / "account.json").unlink(missing_ok=True)
        return {"step": "done", "configured": True, "restartRequired": True}
    if action == "owner":
        username = str(data.get("username", "")).strip().lstrip("@")
        import re

        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
            raise SetupFault("USERNAME", "Enter your personal Telegram @username.")
        if not s.get("account_authenticated"):
            raise SetupFault("LOGIN", "Finish Era's account login first.")
        s["owner_username"] = username
        # authenticate reuses the saved session and existing identity guards.
        with contextlib.redirect_stdout(io.StringIO()):
            verified = await authenticate(
                s, lambda value: save_settings(draft_directory(), {**value, "setup_step": "owner"})
            )
        if not verified.get("expected_owner_user_id"):
            # validate_identities already verified the configured phone in this case.
            verified["expected_owner_user_id"] = verified["owner_user_id"]
        save_settings(draft_directory(), {**verified, "setup_step": "enable"})
        return {"step": "enable"}
    if action not in {"start", "code", "password"}:
        raise SetupFault("ACTION", "Unknown setup action.")
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(s.get("session_secret", "")), s["api_id"], s["api_hash"])
    try:
        await client.connect()
        if action == "start":
            result = await client.send_code_request(s["era_phone"])
            s["phone_code_hash"] = result.phone_code_hash
            s["setup_step"] = "code"
        else:
            if s.get("setup_step") != action:
                raise SetupFault("STEP", "Refresh setup to continue from the current step.")
            try:
                if action == "code":
                    await client.sign_in(
                        s["era_phone"],
                        code=str(data.get("code", "")),
                        phone_code_hash=s["phone_code_hash"],
                    )
                else:
                    await client.sign_in(password=str(data.get("password", "")))
            except SessionPasswordNeededError:
                s["setup_step"] = "password"
            else:
                me = await client.get_me()
                if getattr(me, "bot", False) or str(me.phone) != s["era_phone"].lstrip("+"):
                    raise SetupFault(
                        "ACCOUNT", "Login was for a different account. Use Era's number."
                    )
                s.update(account_authenticated=True, era_user_id=me.id, setup_step="owner")
                s.pop("phone_code_hash", None)
        s["session_secret"] = client.session.save()
        save_settings(draft_directory(), s)
        return {"step": s["setup_step"]}
    finally:
        await client.disconnect()


def main():
    try:
        data = json.loads(sys.stdin.read(16385))
        result = asyncio.run(run(data))
    except SetupFault as exc:
        result = {"error": exc.explanation}
    except ProtectedConfigError:
        result = {
            "error": "Windows cannot unlock setup progress. Cancel setup and start again; the saved account is preserved."
        }
    except Exception as exc:
        messages = {
            "PhoneCodeInvalidError": "That login code is incorrect. Try again.",
            "PhoneCodeExpiredError": "The code expired. Cancel setup and start again.",
            "PasswordHashInvalidError": "The two-step password is incorrect. Try again.",
            "FloodWaitError": "Telegram requires a wait before another attempt. Try later.",
        }
        result = {
            "error": messages.get(
                type(exc).__name__,
                "Setup could not complete. Check the account details and connection, then retry.",
            )
        }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
