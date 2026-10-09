"""Which model serves this request, and why.

WHAT THIS IS
------------
A pure decision layer. It reads the registry and the routing policy and returns
a `RouteDecision`. It does not call a model, spend anything, or touch Era's
state — which is what makes every rule below testable without a network.

WHY THE DECISION IS AN OBJECT AND NOT A STRING
----------------------------------------------
Returning just an alias would make a fallback indistinguishable from a first
choice. The requirement is explicit: a fallback must never be silent, and the
model that actually handled a request has to be visible in the UI and the logs.
So the decision carries where it came from (`source`), what it was displaced
from (`fell_back_from`), and a sentence that can be logged or spoken.

PRECEDENCE
----------
    explicit override  >  routing.by_task  >  default

with an availability check applied AFTER selection, so an unavailable choice
becomes a recorded fallback rather than a failure. If the fallback is itself
unusable the router RAISES: a router that returns something it knows will not
work has converted a clear error into a mystery halfway through a sentence.

LANES ARE NOT MODELS
--------------------
`routing.by_task` may name `LANE:cm` — community management is a whole separate
pipeline (era_dispatch), not a model choice. Those come back as a decision with
`lane` set and no alias, so a caller cannot accidentally treat the CM lane as
something to prompt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from core.llm.registry import (
    LANE_PREFIX,
    ModelEntry,
    RegistryError,
    all_aliases,
    default_alias,
    resolve,
    routing_policy,
)
from core.llm.registry import display_name as _display_name

# Where a decision came from. Ordered by precedence.
SOURCE_OVERRIDE = "override"
SOURCE_TASK_POLICY = "task_policy"
SOURCE_DEFAULT = "default"


class RoutingError(RuntimeError):
    """No usable model could be selected. Always says what was tried."""


@dataclass(frozen=True)
class RouteDecision:
    """The answer, with its reasoning attached."""

    alias: str
    source: str
    reason: str
    task: str | None = None
    requested: str | None = None
    fell_back_from: str | None = None
    fallback_reason: str = ""
    lane: str | None = None
    entry: ModelEntry | None = None

    @property
    def is_fallback(self) -> bool:
        return self.fell_back_from is not None

    @property
    def is_lane(self) -> bool:
        return self.lane is not None

    @property
    def provider(self) -> str:
        return self.entry.provider if self.entry else ""

    @property
    def model(self) -> str:
        return self.entry.model if self.entry else ""

    @property
    def display_name(self) -> str:
        """The model's human name ("Claude Opus 5"), for the UI and for speech.

        The alias is Era's word for a ROLE; this answers "which model is this?",
        which is what a person looking at the indicator is actually asking.
        """
        if self.is_lane:
            return f"{self.lane} lane"
        return _display_name(self.alias) if self.alias else "unknown"

    def describe(self) -> str:
        """One line, suitable for a log or for saying out loud.

        A fallback always says so. Hiding it is how a user ends up believing
        they got the reasoning model when they got the fast one.
        """
        if self.is_lane:
            return f"{self.lane} lane (not a model)"
        base = f"{self.alias} ({self.provider}/{self.model})"
        if self.is_fallback:
            return f"{base} — fell back from {self.fell_back_from}: {self.fallback_reason}"
        return f"{base} — {self.reason}"

    def to_log_fields(self) -> dict[str, Any]:
        """Flat fields for log_event, so routing is queryable after the fact."""
        return {
            "alias": self.alias,
            "display_name": self.display_name,
            "provider": self.provider,
            "model": self.model,
            "source": self.source,
            "task": self.task or "",
            "requested": self.requested or "",
            "fell_back_from": self.fell_back_from or "",
            "fallback_reason": self.fallback_reason,
            "lane": self.lane or "",
        }


def _usable(alias: str, needs: dict[str, bool]) -> tuple[bool, str, ModelEntry | None]:
    """Can this alias serve the request right now? Never raises."""
    try:
        entry = resolve(alias)
    except RegistryError as e:
        return False, str(e), None
    ok, reason = entry.capabilities.satisfies(**needs)
    return ok, reason, entry


def _fallback_alias() -> str:
    policy = routing_policy()
    return str(policy.get("availability_fallback") or "").strip() or default_alias()


def route(
    task: str | None = None,
    *,
    override: str | None = None,
    **needs: bool,
) -> RouteDecision:
    """Pick the alias for one request.

    `override` is an explicit user instruction ("use Claude") and outranks
    policy — but it does not outrank reality: an unavailable override still
    falls back, and the decision records that it did, so the user can be told
    they did not get what they asked for.
    """
    policy = routing_policy()
    by_task = policy.get("by_task") or {}

    requested: str | None = None
    source = SOURCE_DEFAULT
    reason = "no task class matched; using the default alias"

    if override:
        requested = override.strip()
        source = SOURCE_OVERRIDE
        reason = "explicitly requested"
    elif task and task in by_task:
        target = str(by_task[task] or "").strip()
        if target.startswith(LANE_PREFIX):
            lane = target[len(LANE_PREFIX) :] or "unknown"
            return RouteDecision(
                alias="",
                source=SOURCE_TASK_POLICY,
                reason=f"task {task!r} is handled by the {lane} lane, not a model",
                task=task,
                lane=lane,
            )
        requested = target
        source = SOURCE_TASK_POLICY
        reason = f"routing policy for task {task!r}"
    else:
        requested = default_alias()

    if not requested:
        raise RoutingError(
            "no alias could be selected: the registry declares no default "
            f"and task {task!r} is not in the routing policy"
        )

    ok, why_not, entry = _usable(requested, needs)
    if ok:
        return RouteDecision(
            alias=requested,
            source=source,
            reason=reason,
            task=task,
            requested=requested,
            entry=entry,
        )

    # ── Fallback. Recorded, never silent. ──
    fallback = _fallback_alias()
    if not fallback:
        raise RoutingError(f"{requested} cannot serve this request ({why_not}) and no fallback")
    if fallback == requested:
        raise RoutingError(
            f"{requested} cannot serve this request ({why_not}) and it is itself the fallback"
        )

    ok, fb_why_not, fb_entry = _usable(fallback, needs)
    if not ok:
        raise RoutingError(
            f"{requested} cannot serve this request ({why_not}), and the fallback "
            f"{fallback} cannot either ({fb_why_not})"
        )

    return RouteDecision(
        alias=fallback,
        source=source,
        reason=reason,
        task=task,
        requested=requested,
        fell_back_from=requested,
        fallback_reason=why_not,
        entry=fb_entry,
    )


def resolve_override(word: str | None) -> str | None:
    """Turn a spoken/typed override token into a real alias, or None.

    Accepts an alias outright ("era.reason"), a bare provider-ish word
    ("claude", "opus"), or None/"auto" to mean "no override". Returns None for
    anything unrecognised rather than guessing — a wrong guess here routes the
    user's request to a model they did not ask for.
    """
    if not word:
        return None
    token = word.strip().lower()
    if not token or token in {"auto", "automatic", "default", "normal"}:
        return None

    known = set(all_aliases())
    if token in known:
        return token

    # Match on the alias's own model id and provider, so the registry stays the
    # single source of truth and this never needs a hand-maintained word list.
    for alias in sorted(known):
        try:
            entry = resolve(alias)
        except RegistryError:
            continue
        if token == entry.model.lower() or token == alias.split(".")[-1].lower():
            return alias
    return None


def dispatch_policy() -> dict[str, Any]:
    """The `routing.claude_dispatch` block, with its env override applied.

    Returns `enabled: False` on ANY problem reading the config. A feature flag
    that fails open is not a feature flag — an unreadable registry must not be
    a second route to spending money.
    """
    out: dict[str, Any] = {"enabled": False, "timeout_s": 45.0}
    try:
        block = dict(routing_policy().get("claude_dispatch") or {})
    except Exception:
        return out

    out["enabled"] = bool(block.get("enabled", False))
    try:
        out["timeout_s"] = float(block.get("timeout_s") or 45.0)
    except (TypeError, ValueError):
        pass

    env_name = str(block.get("env_override") or "").strip()
    if env_name:
        raw = os.environ.get(env_name, "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            out["enabled"] = True
        elif raw in ("0", "false", "no", "off"):
            out["enabled"] = False  # the env can also force it OFF
    return out


def _flag_block(name: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """One `routing.<name>` block with its env override applied.

    Fails CLOSED on any config problem, for the same reason dispatch_policy
    does: an unreadable registry must not become a route to acting.
    """
    out = dict(defaults)
    try:
        block = dict(routing_policy().get(name) or {})
    except Exception:
        return out

    for key, default in defaults.items():
        if key in block:
            value = block[key]
            if isinstance(default, bool):
                out[key] = bool(value)
            elif isinstance(default, int) and not isinstance(value, bool):
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    pass
            elif isinstance(default, list):
                out[key] = [str(v).strip() for v in (value or []) if str(v).strip()]
            else:
                out[key] = value

    env_name = str(block.get("env_override") or "").strip()
    if env_name:
        raw = os.environ.get(env_name, "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            out["enabled"] = True
        elif raw in ("0", "false", "no", "off"):
            out["enabled"] = False
    return out


def shadow_policy() -> dict[str, Any]:
    """`routing.shadow` — observe-only, so it defaults ON."""
    return _flag_block("shadow", {"enabled": False})


def autopilot_policy() -> dict[str, Any]:
    """`routing.autopilot` — Era picking the model herself, per task.

    Every default here is the refusing one. `tasks` defaulting to [] is the
    load-bearing part: it is an ALLOWLIST, so a config that fails to parse
    grants nothing rather than everything.
    """
    return _flag_block(
        "autopilot",
        {
            "enabled": False,
            "tasks": [],
            "require_confident": True,
            "max_per_day": 0,
            "announce_first": True,
        },
    )


def quota_failover_policy() -> dict[str, Any]:
    """`routing.quota_failover` — Gemini quota -> spare key -> Claude.

    Defaults refuse (enabled False, no calls) so an unreadable registry cannot
    become a route to spending money. See core/llm/quota_failover.py.
    """
    return _flag_block(
        "quota_failover",
        {
            "enabled": False,
            "alias": "era.cheap",
            "max_per_day": 0,
            "timeout_s": 45.0,
            "latch_seconds": 300,
            "rotate_keys": True,
            # CodeCraft (subscription) before Claude. Needs CODECRAFT_* set.
            "codecraft": False,
            # Final, free step after Claude: an Ollama alias. "" disables it.
            "local_alias": "",
            # Also fail over when Gemini is unreachable (no network), not only
            # on quota. Claude needs the network too, so this goes local only.
            "offline_failover": False,
        },
    )


def local_fallback_policy() -> dict[str, Any]:
    """`routing.local_fallback` — the offline voice loop that stands in for the
    Gemini Live session while it is down (quota / auth / config / network).

    Defaults refuse: with the block missing, Era keeps the old behaviour (wait
    silently for Gemini). See app/era_live/local_fallback.py.
    """
    return _flag_block(
        "local_fallback",
        {
            "enabled": False,
            "alias": "era.local",
            "tools": True,
            "max_tool_rounds": 4,
            "history_turns": 8,
            # How often Gemini is re-probed while the local loop serves.
            "probe_gemini_every_s": 300,
            # Consecutive Gemini-side failures before serving on the backup
            # brain ("upstream"). 0 = only after every retry, as before.
            "upstream_after_failures": 0,
            # Languages Whisper may return; others are re-decoded. () = any.
            "languages": (),
        },
    )


def cascade_policy() -> dict[str, Any]:
    """`routing.cascade` — the cascade voice mode that REPLACES the Gemini Live
    session (mic -> local STT -> CodeCraft -> Fish/Piper). Only taken when
    CodeCraft is actually configured. See app/era_live/cascade.py.

    Defaults refuse, so a missing block keeps Era on Gemini Live.
    """
    return _flag_block(
        "cascade",
        {
            "enabled": False,
            "history_turns": 5,
            "max_tool_rounds": 4,
            "timeout_s": 45,
            # "" = no second brain. "era.local" = Ollama when CodeCraft fails
            # before answering — note that model runs on the GPU.
            "fallback_alias": "",
            "low_priority": True,
            "stt_threads": 4,
            # The loop re-checks config and re-enters after this long.
            "cycle_s": 3600,
        },
    )


def claude_voice_policy() -> dict[str, Any]:
    """`routing.claude_voice` — Claude (subscription CLI) as the spoken brain on the
    local voice loop while Gemini Live is connected but not answering (deaf / mute /
    flaky), or when the user says "use Claude". See app/era_live/claude_voice.py.

    Defaults refuse: with the block missing, Era keeps waiting on Gemini exactly as
    before. The `*_stay_s` keys are read by ClaudeVoiceMixin.enter_claude_voice; the
    values below are the ones it falls back to when a key is absent.
    """
    return _flag_block(
        "claude_voice",
        {
            "enabled": False,
            "alias": "era.cheap",
            "stt_threads": 2,
            "history_turns": 8,
            "max_tool_rounds": 4,
            "timeout_s": 90.0,
            "probe_gemini_every_s": 60.0,
            "max_turns_per_day": 200,
            "min_verdict_hold_s": 5.0,
            "stay_s": 180,
            "manual_stay_s": 3600,
            "healthy_after_s": 600,
            "max_stay_s": 1800,
        },
    )


def available_for(**needs: bool) -> list[str]:
    """Aliases that could serve this request right now — what to offer when the
    requested one is out."""
    out = []
    for alias in all_aliases():
        ok, _why, _entry = _usable(alias, needs)
        if ok:
            out.append(alias)
    return out
