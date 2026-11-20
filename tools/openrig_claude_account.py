"""Private credential helper: follow the owner's connected Windows Claude Code.

`status` never prints credentials. `token` is only for Claude's captured helper
pipe, never a terminal. No credential files, refresh tokens or app settings are
modified, and distinct simultaneously connected accounts are never guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import psutil


class AccountUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectedAccount:
    identity: str
    token: str = field(repr=False)
    source: str = "windows-claude-code"
    pid: int | None = None

    def status(self) -> dict:
        return {
            "connected": True,
            "account_fingerprint": hashlib.sha256(self.identity.encode()).hexdigest()[:12],
            "source": self.source,
            "pid": self.pid,
        }


def _valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sk-ant-oat")
        and 32 <= len(value) <= 8192
        and not any(c.isspace() for c in value)
    )


def _trusted_binary(executable: str, home: Path) -> bool:
    candidate = os.path.normcase(os.path.abspath(executable))
    if Path(candidate).name.lower() != "claude.exe":
        return False
    standalone = os.path.normcase(str(home / ".local/bin/claude.exe"))
    desktop = os.path.normcase(str(home / "AppData/Local/Claude-Data"))
    return candidate == standalone or (
        candidate.startswith(desktop + os.sep)
        and "claude-code" in Path(candidate).parts
    )


def account_from_environment(env: dict, pid: int) -> ConnectedAccount | None:
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not _valid_token(token):
        return None
    endpoint = env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    try:
        url = urlsplit(endpoint)
        if url.scheme != "https" or url.netloc != "api.anthropic.com" or url.path not in ("", "/"):
            return None
    except ValueError:
        return None
    # Desktop supplies this account identity. Without it, separate credentials
    # cannot be assumed to refer to the same account.
    identity = env.get("CLAUDE_CODE_ACCOUNT_UUID")
    if not isinstance(identity, str) or not identity.strip():
        identity = hashlib.sha256(token.encode()).hexdigest()
    return ConnectedAccount(identity, token, pid=pid)


def select_account(accounts: list[ConnectedAccount]) -> ConnectedAccount:
    if not accounts:
        raise AccountUnavailable("Open Windows Claude Code and sign in to the account OpenRig should use.")
    identities = {account.identity for account in accounts}
    if len(identities) != 1:
        raise AccountUnavailable(
            "More than one Windows Claude account is connected. Leave only the intended account connected; no account was selected."
        )
    # Multiple sessions of the SAME account are harmless. Prefer the most recent
    # process for a fresh host-supplied access token; never rotate identities.
    return accounts[-1]


def discover_account(home: Path | None = None) -> ConnectedAccount:
    home = home or Path.home()
    owner = psutil.Process().username().casefold()
    candidates = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            info = process.info
            if (info.get("name") or "").lower() != "claude.exe":
                continue
            if not info.get("exe") or not _trusted_binary(info["exe"], home):
                continue
            if process.username().casefold() != owner:
                continue
            account = account_from_environment(process.environ(), process.pid)
            if account is not None:
                candidates.append((float(info.get("create_time") or 0), account))
        except (psutil.Error, OSError, ValueError, UnicodeError):
            continue
    return select_account([account for _, account in sorted(candidates, key=lambda row: row[0])])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "token"), nargs="?", default="status")
    args = parser.parse_args(argv)
    try:
        if args.action == "token" and sys.stdout.isatty():
            raise AccountUnavailable("Token output is restricted to Claude's private helper pipe. Use status here.")
        account = discover_account()
        if args.action == "token":
            sys.stdout.write(account.token + "\n")
        else:
            print(json.dumps(account.status()))
        return 0
    except AccountUnavailable as exc:
        if args.action == "status":
            print(json.dumps({"connected": False, "message": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Exception messages from process inspection must never echo an env or
        # credential. Claude should fail closed rather than use an API key.
        print("Windows Claude account inspection is unavailable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
