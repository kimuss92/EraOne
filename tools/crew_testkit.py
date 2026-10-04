"""Shared helpers for the crew tests (not a test module).

ScriptedLLM stands in for the MODEL only. Everything else under test — the turn loop,
capability enforcement, the vault bridge, the confirmation store, Nabar's report rules,
the real dispatcher — is the production code, so a scripted reply can never make a test
pass by skipping a safety check; it can only say what a model might say.
"""

from __future__ import annotations

import re

from crew.transport import ERROR_TYPES


class ScriptedLLM:
    """Replies per bot, in order. A reply is a dict, or a callable (system, user) -> dict
    so a script can read what the bot was just shown (e.g. an observation id)."""

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {k.lower(): list(v) for k, v in scripts.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str, tier: str) -> dict:
        match = re.match(r"You are (\w+),", system)
        bot = match.group(1).lower() if match else "?"
        self.calls.append((bot, user))
        queue = self.scripts.get(bot, [])
        if not queue:
            return {}  # "model unavailable" — the honest failure, never a lucky pass
        item = queue.pop(0)
        return item(system, user) if callable(item) else item

    def bots_called(self) -> list[str]:
        return [b for b, _ in self.calls]


def call(capability: str, **args) -> dict:
    return {"call": {"capability": capability, "args": args}}


def final(text) -> dict:
    return {"final": text}


def last_obs_id(user: str, capability: str) -> str:
    """The id of the most recent observation a bot was shown for `capability`."""
    ids = re.findall(rf'<untrusted id="(obs\d+)" capability="{re.escape(capability)}"', user)
    return ids[-1] if ids else "obs-none"


def last_obs_text(user: str, capability: str) -> str:
    """The text of the most recent observation a bot was shown for `capability` — what a
    real check must quote from (Nabar's `observed` has to appear in it)."""
    found = re.findall(
        rf'<untrusted id="obs\d+" capability="{re.escape(capability)}">\n(.*?)\n</untrusted>',
        user,
        flags=re.S,
    )
    return found[-1] if found else ""


def excerpt(user: str, capability: str) -> str:
    """A short exact excerpt of that observation: its first line."""
    return (
        last_obs_text(user, capability).splitlines()[0] if last_obs_text(user, capability) else ""
    )


def assert_crew_failure_shape(result) -> None:
    """The contract's error shape: a closed error_type and a string detail, nothing else."""
    assert result.success is False
    assert result.error_type in ERROR_TYPES, result.error_type
    assert isinstance(result.data.get("detail"), str) and result.data["detail"].strip()
