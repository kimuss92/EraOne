"""Which Claude subscription account runs a call, and what to do when it can't.

WHY THIS EXISTS
  Era reaches Claude through the subscription CLI, and which account answers is
  chosen by one environment variable: CLAUDE_CONFIG_DIR. There are two accounts
  on this machine (~/.claude-freelance and ~/.claude-delta), each with its own
  login and its own usage limit.

  era_dispatch.py has failed over between them since 2026-09-25 — but only for
  the community-management lane. Every other Claude path in Era read a single
  hardcoded `config_dir` out of config/llm.yaml, so one account hitting its
  limit stopped all of it: reasoning, planning, quota failover from Gemini, and
  anything built on them.

  This module is that same proven logic, lifted to where every path can reach
  it. The failure markers and the ordering rule are era_dispatch's, deliberately
  — a second, subtly different implementation of "is this account dead?" is how
  the two lanes would start disagreeing.

WHAT COUNTS AS "SWITCH ACCOUNTS"
  Only failures that the OTHER account could plausibly survive: the account is
  out of usage, rate limited, signed out, or its credentials are rejected.

  Deliberately NOT switched on:
    refusal, unsupported, invalid_response, context_overflow
        The second account is the same model with the same limits. Retrying
        costs another ~3s and ~$0.06 to fail in exactly the same way.
    timeout
        Says the work was slow, not that the account is bad. Switching would
        double a long wait that was already too long.

COOLDOWN IS IN MEMORY, ON PURPOSE
  A failed account is skipped for a while rather than retried on every call.
  That state is not persisted: after a restart each account gets one fresh
  chance, which costs at most one failed call (~3s) and cannot leave Era
  permanently convinced a recovered account is still dead. Persisting it would
  trade that cheap re-probe for a stale-state failure mode, which is the worse
  of the two.

STATUS
  python -m core.llm.claude_accounts
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Error kinds (core.llm.types) that mean "this ACCOUNT cannot serve the call".
ACCOUNT_SWITCH_KINDS = frozenset({"auth", "rate_limit"})

# Substrings that mean the same thing when they arrive as prose instead of a
# classified kind. Copied from era_dispatch._ROUTE_FAILURE_MARKERS so the two
# lanes cannot drift apart on what "this account is unavailable" means.
ROUTE_FAILURE_MARKERS = (
    "oauth session expired",
    "failed to authenticate",
    "not logged in",
    "/login",
    "invalid api key",
    "usage limit",
    "limit reached",
    "rate limit",
    "rate_limit",
    "overloaded",
    "credit balance",
)

# How long a failed account is skipped. Long enough that a usage limit is not
# re-probed on every call, short enough that a recovered account comes back
# inside one working session.
DEFAULT_COOLDOWN_S = float(os.getenv("ERA_CLAUDE_ACCOUNT_COOLDOWN_S", "900"))

# Semicolon-separated override (os.pathsep on Windows; Windows paths contain a
# colon, so a colon separator would split "C:\..." in half).
ENV_ACCOUNTS = "ERA_CLAUDE_ACCOUNTS"


@dataclass(frozen=True)
class Account:
    """One Claude subscription profile."""

    config_dir: str

    @property
    def name(self) -> str:
        return Path(self.config_dir).name or self.config_dir

    @property
    def exists(self) -> bool:
        return Path(self.config_dir).is_dir()

    @property
    def has_credentials(self) -> bool:
        """A logged-in profile leaves .credentials.json behind.

        Reported, never enforced: Claude Code can also hold a session outside
        the profile directory, so treating a missing file as "not an account"
        would silently drop a working failover. Presence is evidence, absence
        is not proof.
        """
        return (Path(self.config_dir) / ".credentials.json").exists()


# account name -> (unix time the cooldown ends, why)
_cooldowns: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _split_env(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.replace(";", os.pathsep).split(os.pathsep)]
    return [os.path.expandvars(os.path.expanduser(p)) for p in parts if p.strip()]


def accounts(provider_config: dict | None = None) -> list[Account]:
    """Every account to try, best first, de-duplicated.

    Order: ERA_CLAUDE_ACCOUNTS if set, otherwise the registry's `config_dir`
    (the primary, unchanged from before this module existed) followed by
    `fallback_config_dirs`. Keeping `config_dir` first is what makes this a
    pure addition: the account that served every call yesterday still serves
    the first attempt today.
    """
    raw: list[str] = []
    env = os.getenv(ENV_ACCOUNTS, "").strip()
    if env:
        raw = _split_env(env)
    else:
        cfg = provider_config or {}
        primary = str(cfg.get("config_dir") or "").strip()
        if primary:
            raw.append(os.path.expandvars(primary))
        fallbacks = cfg.get("fallback_config_dirs") or []
        if isinstance(fallbacks, str):
            fallbacks = _split_env(fallbacks)
        for item in fallbacks:
            text = str(item or "").strip()
            if text:
                raw.append(os.path.expandvars(text))

    seen: set[str] = set()
    out: list[Account] = []
    for path in raw:
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(Account(path))
    return out


def should_switch(kind: str | None, message: str = "") -> bool:
    """Would the OTHER account plausibly have survived this failure?"""
    if kind in ACCOUNT_SWITCH_KINDS:
        return True
    text = (message or "").lower()
    return any(marker in text for marker in ROUTE_FAILURE_MARKERS)


def note_failure(account: Account, reason: str, cooldown_s: float | None = None) -> None:
    """Skip this account for a while."""
    until = time.time() + (DEFAULT_COOLDOWN_S if cooldown_s is None else cooldown_s)
    with _lock:
        _cooldowns[account.name] = (until, reason[:200])


def note_success(account: Account) -> None:
    """A working call clears any cooldown — evidence beats the timer."""
    with _lock:
        _cooldowns.pop(account.name, None)


def cooling(account: Account) -> str:
    """Why this account is being skipped, or '' if it is usable."""
    with _lock:
        entry = _cooldowns.get(account.name)
        if not entry:
            return ""
        until, reason = entry
        if time.time() >= until:
            del _cooldowns[account.name]
            return ""
        return f"{reason} (retry in {until - time.time():.0f}s)"


def usable(provider_config: dict | None = None) -> list[Account]:
    """Accounts to actually try, in order, with cooled-down ones removed.

    If every account is cooling, the cooldowns are ignored and the full list is
    returned. Being out of accounts is not a reason to stop trying: a stale
    timer must never be the thing that makes Era unable to reach Claude at all.
    """
    every = [a for a in accounts(provider_config) if a.exists]
    if not every:
        return []
    ready = [a for a in every if not cooling(a)]
    return ready or every


def reset() -> None:
    """Drop all cooldowns. For tests, and for `reconnect`."""
    with _lock:
        _cooldowns.clear()


def status(provider_config: dict | None = None) -> list[dict]:
    rows = []
    for account in accounts(provider_config):
        rows.append(
            {
                "name": account.name,
                "config_dir": account.config_dir,
                "exists": account.exists,
                "has_credentials": account.has_credentials,
                "cooling": cooling(account),
            }
        )
    return rows


def _main() -> int:
    from core.llm import registry

    try:
        config = registry.load_config().get("providers", {}).get("claude_cli", {})
    except Exception as exc:  # noqa: BLE001 - a status tool reports, it does not raise
        print(f"could not read the registry: {exc}")
        config = {}

    rows = status(config)
    if not rows:
        print("No Claude accounts configured.")
        print("Set providers.claude_cli.config_dir (and fallback_config_dirs) in config/llm.yaml,")
        print(f"or {ENV_ACCOUNTS} to a {os.pathsep!r}-separated list of profile directories.")
        return 1

    print(f"Claude accounts, in the order they are tried (cooldown {DEFAULT_COOLDOWN_S:.0f}s):")
    for i, row in enumerate(rows, 1):
        if not row["exists"]:
            state = "MISSING — directory does not exist"
        elif row["cooling"]:
            state = f"cooling down: {row['cooling']}"
        else:
            state = "ready"
        creds = "" if row["has_credentials"] else "  [no .credentials.json]"
        print(f"  {i}. {row['name']:<24} {state}{creds}")
        print(f"     {row['config_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
