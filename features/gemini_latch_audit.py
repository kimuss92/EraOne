"""Read-only audit for Gemini model latching and DuckDuckGo fallback policy.

This reloadable feature analyzes caller-supplied evidence and returns a staged,
redacted plan. It never reads or writes live configuration, contacts providers,
or clears runtime state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

FEATURE = {
    "id": "gemini_latch_audit",
    "description": (
        "Audit Gemini key/model latching and DuckDuckGo fallback policy, "
        "then produce a redacted, review-gated configuration plan."
    ),
}

_DEFAULT_FAILED_MODEL = "gemini-3.8-flash"
_KEY_FIELDS = (
    "gemini_api_key",
    "gemini_api_key_fallback",
    "gemini_api_key_fallback2",
    "gemini_api_key_backup",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonblank_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    upper = stripped.upper()
    return (
        bool(stripped)
        and upper not in {"PLACEHOLDER", "CHANGE_ME"}
        and not (upper.startswith("YOUR_") or stripped.startswith("<"))
    )


def _gemini_section(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    direct = config.get("gemini")
    if isinstance(direct, Mapping):
        return direct, "gemini"

    providers = config.get("providers")
    if isinstance(providers, Mapping) and isinstance(providers.get("gemini"), Mapping):
        return providers["gemini"], "providers.gemini"

    for parent_name in ("llm", "routing", "models"):
        parent = config.get(parent_name)
        if not isinstance(parent, Mapping):
            continue
        nested = parent.get("gemini")
        if isinstance(nested, Mapping):
            return nested, f"{parent_name}.gemini"
        nested_providers = parent.get("providers")
        if isinstance(nested_providers, Mapping) and isinstance(
            nested_providers.get("gemini"), Mapping
        ):
            return nested_providers["gemini"], f"{parent_name}.providers.gemini"

    return {}, "gemini"


def _configured_key_count(config: Mapping[str, Any], gemini: Mapping[str, Any]) -> int:
    """Count usable credential slots without retaining their names or values."""
    count = sum(1 for field in _KEY_FIELDS if _nonblank_secret(config.get(field)))

    extra = config.get("gemini_api_keys")
    if isinstance(extra, list):
        count += sum(1 for value in extra if _nonblank_secret(value))

    nested_keys = gemini.get("keys")
    if isinstance(nested_keys, list):
        count = max(count, sum(1 for value in nested_keys if value))

    bindings = gemini.get("key_models")
    if isinstance(bindings, Mapping):
        count = max(count, len(bindings))
    return count


def _model_name(gemini: Mapping[str, Any]) -> str:
    for field in ("model", "model_id", "default_model"):
        value = gemini.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _binding_models(gemini: Mapping[str, Any]) -> list[str]:
    bindings = gemini.get("key_models")
    if not isinstance(bindings, Mapping):
        return []
    return [
        value.strip() for value in bindings.values() if isinstance(value, str) and value.strip()
    ]


def _evidence_text(value: Any, *, depth: int = 0) -> str:
    """Flatten bounded diagnostic evidence for matching, never for output."""
    if depth > 5:
        return ""
    if isinstance(value, str):
        return value[:20000]
    if isinstance(value, Mapping):
        return "\n".join(
            _evidence_text(item, depth=depth + 1) for item in list(value.values())[:100]
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "\n".join(_evidence_text(item, depth=depth + 1) for item in list(value)[:100])
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _failure_evidence(arguments: Mapping[str, Any]) -> str:
    parts = (
        _evidence_text(arguments.get("failure_logs")),
        _evidence_text(arguments.get("failure_history")),
    )
    return "\n".join(parts).lower()[:40000]


def _find_web_search(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    direct = config.get("web_search")
    if isinstance(direct, Mapping):
        return direct, "web_search"

    for parent_name in ("search", "tools", "actions"):
        parent = config.get(parent_name)
        if isinstance(parent, Mapping) and isinstance(parent.get("web_search"), Mapping):
            return parent["web_search"], f"{parent_name}.web_search"

    return {}, "web_search"


def _condition_is_explicit(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(isinstance(item, str) and bool(item.strip()) for item in value)
    return False


def _first_condition(*values: Any) -> Any:
    for value in values:
        if _condition_is_explicit(value):
            return value
    return None


def _duckduckgo_audit(config: Mapping[str, Any], *, default_enabled: bool) -> dict[str, Any]:
    web, location = _find_web_search(config)
    providers = _mapping(web.get("providers"))

    raw_duck = providers.get("duckduckgo")
    if raw_duck is None:
        raw_duck = web.get("duckduckgo")

    fallback_value = web.get("fallback_provider", web.get("default_fallback"))
    named_as_fallback = isinstance(fallback_value, str) and fallback_value.strip().lower() in {
        "duckduckgo",
        "ddg",
    }
    configured = raw_duck is not None or named_as_fallback
    duck = _mapping(raw_duck)

    if isinstance(raw_duck, bool):
        enabled = raw_duck
    elif configured:
        enabled = bool(duck.get("enabled", True))
    else:
        enabled = default_enabled

    fallback_section = _mapping(web.get("fallback"))
    conditions = _first_condition(
        duck.get("fallback_on"),
        duck.get("conditions"),
        web.get("fallback_on"),
        web.get("fallback_conditions"),
        fallback_section.get("on"),
        fallback_section.get("conditions"),
    )
    explicit_conditions = conditions is not None
    reviewed = bool(
        duck.get(
            "reviewed",
            web.get("duckduckgo_reviewed", fallback_section.get("duckduckgo_reviewed", False)),
        )
    )
    implicit_or_default = enabled and not configured

    concerns: list[str] = []
    if enabled and implicit_or_default:
        concerns.append("DuckDuckGo is enabled by default rather than explicit configuration.")
    if enabled and not explicit_conditions:
        concerns.append("Fallback activation conditions are not explicitly bounded.")
    if enabled and not reviewed:
        concerns.append(
            "Privacy, result quality, and empty-result behavior are not marked reviewed."
        )

    return {
        "enabled": enabled,
        "configured": configured,
        "configuration_location": location if configured else None,
        "implicit_or_default": implicit_or_default,
        "explicit_conditions": explicit_conditions,
        "reviewed": reviewed,
        "audit_required": bool(concerns),
        "concerns": concerns,
    }


def _replacement_model(arguments: Mapping[str, Any], failed_model: str) -> str:
    verified = arguments.get("verified_gemini_models")
    if not isinstance(verified, list):
        return ""

    allowed = {
        value.strip()
        for value in verified
        if isinstance(value, str) and value.strip() and value.strip() != failed_model
    }
    requested = arguments.get("replacement_model")
    if isinstance(requested, str) and requested.strip() in allowed:
        return requested.strip()
    return sorted(allowed)[0] if allowed else ""


def run(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted audit and non-applying staged patch proposal."""
    if not isinstance(arguments, dict):
        return {
            "status": "error",
            "error": "arguments must be an object",
            "proposed_patch": {"apply": False},
        }

    config = _mapping(arguments.get("configuration"))
    gemini, gemini_location = _gemini_section(config)
    failed_model_value = arguments.get("failed_model") or _DEFAULT_FAILED_MODEL
    failed_model = str(failed_model_value).strip() or _DEFAULT_FAILED_MODEL
    configured_model = _model_name(gemini)
    key_count = _configured_key_count(config, gemini)
    binding_models = _binding_models(gemini)
    evidence = _failure_evidence(arguments)

    all_bindings_failed = bool(binding_models) and all(
        model == failed_model for model in binding_models
    )
    model_in_evidence = failed_model.lower() in evidence
    latch_language = any(
        phrase in evidence
        for phrase in (
            "latched",
            "all keys",
            "all gemini keys",
            "every gemini key",
            "key_models",
        )
    )
    log_confirms_latch = model_in_evidence and latch_language
    confirmed_latch = all_bindings_failed or log_confirms_latch
    shared_model_risk = configured_model == failed_model and key_count > 1
    affected = confirmed_latch or shared_model_risk or configured_model == failed_model
    replacement = _replacement_model(arguments, failed_model)

    findings: list[str] = []
    if confirmed_latch:
        findings.append(f"Failure evidence indicates Gemini keys are latched to {failed_model}.")
    elif shared_model_risk:
        findings.append(
            f"All {key_count} configured key slots share the failing default model "
            f"{failed_model}; key rotation cannot provide model failover."
        )
    elif configured_model == failed_model:
        findings.append(
            f"The configured Gemini model is the reported failing model {failed_model}."
        )
    else:
        findings.append("The supplied configuration does not independently confirm the latch.")

    if replacement:
        findings.append(f"Verified replacement candidate: {replacement}.")
    elif affected:
        findings.append(
            "No verified replacement model was supplied; model substitution remains blocked."
        )

    duck = _duckduckgo_audit(
        config,
        default_enabled=bool(arguments.get("duckduckgo_default_enabled", True)),
    )

    set_values: dict[str, Any] = {}
    unset_values: list[str] = []
    if affected and replacement:
        set_values[f"{gemini_location}.model"] = replacement
        if binding_models:
            unset_values.append(f"{gemini_location}.key_models")

    if duck["enabled"] and duck["audit_required"]:
        set_values.update(
            {
                "web_search.providers.duckduckgo.enabled": False,
                "web_search.providers.duckduckgo.reviewed": False,
            }
        )

    blocked = affected and not replacement
    status = "blocked" if blocked else "review"

    plan = [
        "Capture a redacted backup and before-state of the configuration in staging.",
        (
            f"Stage the Gemini model change from {failed_model} to {replacement}; remove "
            "per-key model bindings so rotation cannot preserve the failed model."
            if affected and replacement
            else "Verify an available Gemini replacement model before changing configuration; do not guess a model ID."
            if affected
            else "Retain the Gemini model until failure evidence or binding configuration confirms the reported latch."
        ),
        "Restart only the staged worker or session so model selection and latch state are rebuilt.",
        "Run offline configuration and failover tests before any separately approved provider smoke test.",
        (
            "Keep DuckDuckGo explicitly disabled until privacy, result quality, empty-result behavior, and bounded activation conditions are reviewed."
            if duck["enabled"] and duck["audit_required"]
            else "Retain the explicit DuckDuckGo policy and record monitoring for fallback reasons and empty results."
        ),
        "Promote only after review confirms that key rotation no longer repeats the failed model and fallback behavior is explicit.",
    ]

    return {
        "status": status,
        "gemini": {
            "failed_model": failed_model,
            "configured_model": configured_model or None,
            "configuration_location": gemini_location if gemini else None,
            "configured_key_count": key_count,
            "confirmed_latch": confirmed_latch,
            "shared_model_risk": shared_model_risk,
            "replacement_model": replacement or None,
            "findings": findings,
        },
        "duckduckgo": duck,
        "proposed_patch": {
            "apply": False,
            "set": set_values,
            "unset": unset_values,
            "requires_backup": True,
            "requires_review": True,
            "requires_staged_restart": bool(affected and replacement),
        },
        "plan": plan,
    }
