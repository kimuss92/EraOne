"""Alias registry — the one place that maps Era's internal model aliases to a
provider and a concrete model id.

WHY
  Four modules currently hardcode a provider model id (main.py's LIVE_MODEL,
  core/ai_provider.py's two defaults, core/vision_service.py). Provider ids get
  renamed and retired far more often than Era's intent does, so every one of
  those is a future edit in code. Here they are config.

CONTRACT
  * Reads config/llm.yaml. Never writes it — so yaml.safe_load is correct and
    ruamel's round-trip loader (which era_offers/era_scheduler need because they
    WRITE yaml and must keep comments) is not needed.
  * Caches on file mtime, exactly like era_dispatch.load_brands(), so editing
    the file takes effect immediately without re-parsing on every lookup.
  * Degrades loudly, not silently: a missing or malformed file raises on the
    first resolve() rather than quietly returning an empty registry that makes
    every alias "unknown". A registry that reads 0 aliases and reports health is
    the failure this project has already been bitten by once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core.llm.types import Capability
from core.utils import BASE_DIR

CONFIG_PATH = BASE_DIR / "config" / "llm.yaml"


class RegistryError(RuntimeError):
    """The registry could not answer. Always names the alias or the file."""


@dataclass(frozen=True)
class ModelEntry:
    """One resolved alias."""

    alias: str
    provider: str
    model: str
    role: str
    capabilities: Capability
    measured: dict[str, Any]
    price: dict[str, float]
    provider_config: dict[str, Any]

    @property
    def call_overhead_usd(self) -> float:
        """Measured cost of making a call at all, before any real work.

        Non-zero only for claude_cli, where every `claude -p` invocation
        reloads the Claude Code harness (the prompt cache does not survive
        across processes). It is the dominant term for short prompts and the
        reason routing to Claude must be selective.
        """
        try:
            return float(self.measured.get("call_overhead_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def floor_latency_ms(self) -> int:
        try:
            return int(self.measured.get("total_ms", 0) or 0)
        except (TypeError, ValueError):
            return 0


# How a provider's model id becomes something worth showing a human. Ordered:
# the first pattern that matches wins, so more specific entries come first.
# Derived from the model id rather than hand-listed per alias, so adding an
# alias or renaming a model does not require a second edit here.
_DISPLAY_RULES: tuple[tuple[str, str], ...] = (
    ("gemini-3.8-live", "Gemini 3.8 Live"),
    ("gemini-3.1-flash-live", "Gemini 3.1 Flash Live"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite"),
    ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ("claude-opus-5", "Claude Opus 5"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-haiku", "Claude Haiku"),
    ("opus", "Claude Opus"),
    ("sonnet", "Claude Sonnet"),
    ("haiku", "Claude Haiku"),
    ("qwen", "Qwen (local)"),
    ("gemma", "Gemma (local)"),
)


def display_name(alias: str) -> str:
    """The model's name as a person would say it — "Claude Opus 5", not "opus".

    The UI indicator used to show the internal alias (era.voice), which is
    Era's word for a ROLE, not an answer to "which model is this?". The alias
    stays the routing key everywhere in code; this is purely what gets rendered.

    Prefers the MEASURED resolved id over the requested one: `--model opus`
    resolves to claude-opus-5, and the resolved value is the truthful answer to
    what actually ran. Falls back to the raw id, then the alias, so this always
    returns something printable.
    """
    try:
        entry = resolve(alias)
    except RegistryError:
        return alias or "unknown"

    resolved = str(entry.measured.get("resolved_model") or "").strip()
    candidate = (resolved or entry.model or "").strip()
    if not candidate:
        return alias
    haystack = candidate.lower()
    for needle, label in _DISPLAY_RULES:
        if needle in haystack:
            return label
    # Unknown id: show it as-is, minus any provider path prefix.
    return candidate.rsplit("/", 1)[-1]


def _config_path() -> Path:
    """Honours ERA_LLM_CONFIG, the same escape hatch core.utils gives the
    memory dir and media_host gives its config. Tests point it at a fixture so
    they never read — or depend on — the user's real routing policy."""
    override = os.environ.get("ERA_LLM_CONFIG", "").strip()
    return Path(override) if override else CONFIG_PATH


# (mtime, path, parsed) — path is part of the key so a test that repoints
# ERA_LLM_CONFIG cannot be served the previous file's cache.
_CACHE: tuple[float, str, dict] | None = None


def load_config(*, force: bool = False) -> dict:
    global _CACHE
    path = _config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        raise RegistryError(f"cannot read the model registry at {path}: {e}") from e

    if not force and _CACHE is not None and _CACHE[0] == mtime and _CACHE[1] == str(path):
        return _CACHE[2]

    try:
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as e:
        raise RegistryError(f"the model registry at {path} is not valid YAML: {e}") from e

    if not isinstance(cfg, dict) or not cfg.get("aliases"):
        raise RegistryError(
            f"the model registry at {path} declares no aliases — refusing to run "
            "with an empty registry, which would make every alias unknown"
        )

    _CACHE = (mtime, str(path), cfg)
    return cfg


def _capability_from(spec: dict[str, Any]) -> Capability:
    caps = spec.get("capabilities") or {}
    return Capability(
        realtime_audio=bool(caps.get("realtime_audio", False)),
        tools=bool(caps.get("tools", False)),
        json_schema=bool(caps.get("json_schema", False)),
        vision=bool(caps.get("vision", False)),
        streaming=bool(caps.get("streaming", False)),
        long_context=bool(caps.get("long_context", False)),
        cost_reporting=bool(caps.get("cost_reporting", False)),
        max_input_tokens=caps.get("max_input_tokens"),
        max_output_tokens=caps.get("max_output_tokens"),
        # available lives at the alias level, not inside capabilities: it is a
        # fact about this environment, not about what the model can do.
        available=bool(spec.get("available", True)),
        unavailable_reason=str(spec.get("unavailable_reason", "") or "").strip(),
    )


def resolve(alias: str) -> ModelEntry:
    """Look up one alias. Raises RegistryError naming the alias and listing the
    known ones — an unknown alias is usually a typo, and the list is the fix."""
    cfg = load_config()
    aliases = cfg.get("aliases") or {}
    spec = aliases.get(alias)
    if not isinstance(spec, dict):
        known = ", ".join(sorted(aliases)) or "(none)"
        raise RegistryError(f"unknown model alias {alias!r}. Known aliases: {known}")

    provider = str(spec.get("provider", "") or "").strip()
    model = str(spec.get("model", "") or "").strip()
    if not provider or not model:
        raise RegistryError(f"alias {alias!r} is missing a provider or model id")

    providers = cfg.get("providers") or {}
    return ModelEntry(
        alias=alias,
        provider=provider,
        model=model,
        role=str(spec.get("role", "") or "").strip(),
        capabilities=_capability_from(spec),
        measured=dict(spec.get("measured") or {}),
        price=dict(spec.get("price") or {}),
        provider_config=dict(providers.get(provider) or {}),
    )


def all_aliases() -> list[str]:
    return sorted((load_config().get("aliases") or {}).keys())


def default_alias() -> str:
    return str(load_config().get("default") or "").strip()


def voice_alias() -> str:
    """The alias for anything needing realtime audio. Separate from the plain
    default so changing the everyday default can never point the voice loop at
    a model that cannot hear."""
    cfg = load_config()
    return str(cfg.get("voice_default") or cfg.get("default") or "").strip()


def routing_policy() -> dict[str, Any]:
    return dict(load_config().get("routing") or {})


def validate(alias: str, **needs: bool) -> ModelEntry:
    """Resolve an alias and assert it can do what is being asked.

    This is the gate the router calls BEFORE spending anything, so an
    unsupported request costs an explanation rather than a provider error
    halfway through a sentence.
    """
    entry = resolve(alias)
    ok, reason = entry.capabilities.satisfies(**needs)
    if not ok:
        raise RegistryError(f"{alias} cannot serve this request — {reason}")
    return entry


def available_aliases(**needs: bool) -> list[str]:
    """Every alias usable right now that satisfies `needs`. What the router
    offers as an alternative when the requested one is out."""
    out = []
    for alias in all_aliases():
        try:
            validate(alias, **needs)
        except RegistryError:
            continue
        out.append(alias)
    return out


def describe(alias: str) -> str:
    """One human-readable line, for `era_doctor` and for saying out loud."""
    try:
        e = resolve(alias)
    except RegistryError as err:
        return f"{alias}: {err}"
    caps = e.capabilities
    if not caps.available:
        return f"{alias}: UNAVAILABLE — {caps.unavailable_reason or 'no reason given'}"
    bits = [f"{e.provider}/{e.model}"]
    if e.call_overhead_usd:
        bits.append(f"~${e.call_overhead_usd:.3f}/call")
    if e.floor_latency_ms:
        bits.append(f"~{e.floor_latency_ms}ms floor")
    return f"{alias}: " + ", ".join(bits)


# Routing targets that are deliberately NOT aliases: a whole separate lane.
LANE_PREFIX = "LANE:"


def doctor() -> dict[str, Any]:
    """Structural health of the registry, for tools/era_doctor.py.

    Never raises — a health tool that dies on the thing it is inspecting tells
    you nothing. A registry that cannot load reports `loaded: False` and why.

    The checks are the ones that fail SILENTLY at runtime. A typo in
    `routing.by_task` does not crash: it resolves to nothing and the request
    quietly takes the fallback, so the only place it can be noticed is here.
    """
    out: dict[str, Any] = {
        "path": str(_config_path()),
        "loaded": False,
        "error": "",
        "aliases": 0,
        "available": [],
        "unavailable": [],
        "default": "",
        "voice": "",
        "problems": [],
        "providers": {},
        "daily_budget_usd": None,
    }

    try:
        cfg = load_config()
    except RegistryError as e:
        out["error"] = str(e)
        return out
    except Exception as e:  # pragma: no cover - defensive
        out["error"] = f"unexpected error reading the registry: {e}"
        return out

    out["loaded"] = True
    known = set(all_aliases())
    out["aliases"] = len(known)
    problems: list[str] = out["problems"]

    for alias in sorted(known):
        try:
            entry = resolve(alias)
        except RegistryError as e:
            problems.append(str(e))
            continue
        if entry.capabilities.available:
            out["available"].append(alias)
        else:
            out["unavailable"].append(
                {
                    "alias": alias,
                    "reason": entry.capabilities.unavailable_reason or "no reason given",
                }
            )
        provider_stats = out["providers"].setdefault(
            entry.provider, {"aliases": 0, "configured": bool(entry.provider_config)}
        )
        provider_stats["aliases"] += 1
        if not entry.provider_config:
            problems.append(
                f"alias {alias!r} names provider {entry.provider!r}, "
                "which has no `providers:` block"
            )

    # ── default and voice ──
    out["default"] = default_alias()
    out["voice"] = voice_alias()
    if not out["default"]:
        problems.append("no `default` alias is declared")
    elif out["default"] not in known:
        problems.append(f"`default` names unknown alias {out['default']!r}")

    if not out["voice"]:
        problems.append("no `voice_default` alias is declared")
    elif out["voice"] not in known:
        problems.append(f"`voice_default` names unknown alias {out['voice']!r}")
    else:
        # The one hard invariant. Era feeds raw PCM straight into this session
        # with no STT stage, so an alias without realtime audio does not
        # degrade the voice loop — it removes it.
        try:
            if not resolve(out["voice"]).capabilities.realtime_audio:
                problems.append(
                    f"voice alias {out['voice']!r} has no realtime_audio — "
                    "the voice loop cannot run on it"
                )
        except RegistryError:
            pass  # already reported above

    # ── routing policy ──
    routing = cfg.get("routing") or {}
    for task, target in (routing.get("by_task") or {}).items():
        target = str(target or "").strip()
        if target.startswith(LANE_PREFIX) or not target:
            continue
        if target not in known:
            problems.append(f"routing.by_task[{task}] names unknown alias {target!r}")

    fallback = str(routing.get("availability_fallback") or "").strip()
    if fallback:
        if fallback not in known:
            problems.append(f"routing.availability_fallback names unknown alias {fallback!r}")
        elif any(u["alias"] == fallback for u in out["unavailable"]):
            # The fallback is what every other alias falls back TO. If it is
            # itself out, a provider outage has nowhere to land.
            problems.append(f"routing.availability_fallback {fallback!r} is itself unavailable")

    out["daily_budget_usd"] = routing.get("daily_budget_usd")
    return out


def _probe_claude_cli(config: dict[str, Any]) -> tuple[str, str]:
    """Is the `claude` binary present, and does its config dir exist?

    PRESENCE, NOT AUTHORISATION. This proves the binary can be found; it does
    not prove a headless `claude -p` would succeed. Phase 4 established that an
    already-"connected" interactive connector does NOT carry into a headless
    run, so treat a pass here as "worth trying", never as "verified".
    """
    import shutil

    binary = os.getenv("CLAUDE_BIN", "claude")
    found = shutil.which(binary)
    if not found:
        return "error", f"{binary!r} is not on PATH — no Claude alias can run"

    config_dir = str(config.get("config_dir") or "").strip()
    if config_dir and not Path(config_dir).is_dir():
        return "warn", f"binary at {found}, but config_dir {config_dir} does not exist"
    where = f"config_dir {config_dir}" if config_dir else "default config_dir"
    return "ok", f"{found} ({where}; presence only, not an auth check)"


def _probe_ollama(config: dict[str, Any]) -> tuple[str, str]:
    env_name = str(config.get("base_url_env") or "OLLAMA_BASE_URL").strip()
    base = os.getenv(env_name, "").strip()
    if base:
        return "ok", f"{env_name}={base} (not contacted)"
    return "ok", f"{env_name} unset — will use the local default (not contacted)"


def _probe_gemini(_config: dict[str, Any]) -> tuple[str, str]:
    # Deliberately not re-checked here: era_doctor's "API key config" check
    # already owns Gemini credentials, and two checks disagreeing about the
    # same fact is worse than one.
    return "ok", "credentials covered by the API key config check"


_PROBES = {
    "claude_cli": _probe_claude_cli,
    "ollama": _probe_ollama,
    "gemini": _probe_gemini,
}


def provider_health() -> list[dict[str, Any]]:
    """Can each provider the registry names be reached from this machine?

    Local probes only — nothing here spends money or makes a network call, so
    it is safe to run on every `era_doctor` invocation.
    """
    try:
        cfg = load_config()
    except RegistryError as e:
        return [{"provider": "(registry)", "status": "error", "detail": str(e)}]

    providers = cfg.get("providers") or {}
    used = set()
    for alias in all_aliases():
        try:
            used.add(resolve(alias).provider)
        except RegistryError:
            continue

    out: list[dict[str, Any]] = []
    for name in sorted(used):
        config = providers.get(name) or {}
        probe = _PROBES.get(name)
        if probe is None:
            out.append({"provider": name, "status": "warn", "detail": "no local probe available"})
            continue
        try:
            status, detail = probe(config)
        except Exception as e:  # pragma: no cover - defensive
            status, detail = "warn", f"probe failed: {e}"
        out.append({"provider": name, "status": status, "detail": detail})
    return out


def main(argv: list[str] | None = None) -> int:
    """`python -m core.llm.registry` — list what Era can route to."""
    import argparse

    ap = argparse.ArgumentParser(description="Inspect Era's model alias registry.")
    ap.add_argument("alias", nargs="?", help="describe one alias (default: all)")
    args = ap.parse_args(argv)

    if args.alias:
        print(describe(args.alias))
        return 0

    cfg = load_config()
    print(f"registry: {_config_path()}")
    print(f"default: {default_alias()}   voice: {voice_alias()}\n")
    for alias in all_aliases():
        print("  " + describe(alias))
    unavailable = [a for a in all_aliases() if not resolve(a).capabilities.available]
    print(f"\n{len(all_aliases()) - len(unavailable)}/{len(all_aliases())} aliases available")
    budget = (cfg.get("routing") or {}).get("daily_budget_usd")
    if budget is not None:
        print(f"daily budget: ${budget}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
