from __future__ import annotations

from features.gemini_latch_audit import FEATURE, run


def test_feature_contract_is_reloadable_and_registered_by_metadata():
    assert FEATURE == {
        "id": "gemini_latch_audit",
        "description": FEATURE["description"],
    }
    assert "Gemini" in FEATURE["description"]
    assert callable(run)


def test_confirmed_bindings_stage_only_verified_model_and_redact_credentials():
    result = run(
        {
            "configuration": {
                "gemini_api_key": "primary-secret-value",
                "gemini_api_key_fallback": "fallback-secret-value",
                "gemini": {
                    "model": "gemini-3.8-flash",
                    "key_models": {
                        "primary": "gemini-3.8-flash",
                        "spare": "gemini-3.8-flash",
                    },
                },
                "web_search": {"providers": {"duckduckgo": {"enabled": False}}},
            },
            "replacement_model": "gemini-stable",
            "verified_gemini_models": ["gemini-stable"],
        }
    )

    assert result["status"] == "review"
    assert result["gemini"]["confirmed_latch"] is True
    assert result["gemini"]["configured_key_count"] == 2
    assert result["proposed_patch"]["set"]["gemini.model"] == "gemini-stable"
    assert result["proposed_patch"]["unset"] == ["gemini.key_models"]
    assert result["proposed_patch"]["apply"] is False
    assert result["proposed_patch"]["requires_review"] is True
    rendered = repr(result)
    assert "primary-secret-value" not in rendered
    assert "fallback-secret-value" not in rendered


def test_failure_history_confirms_latch_but_unverified_replacement_is_blocked():
    result = run(
        {
            "configuration": {
                "gemini_api_key": "one-secret",
                "gemini_api_key_fallback": "two-secret",
                "gemini": {"model": "gemini-3.8-flash"},
            },
            "failure_history": [
                {
                    "component": "tasks",
                    "error": "All Gemini keys are latched for gemini-3.8-flash",
                }
            ],
            "replacement_model": "guessed-model",
            "verified_gemini_models": [],
            "duckduckgo_default_enabled": False,
        }
    )

    assert result["status"] == "blocked"
    assert result["gemini"]["confirmed_latch"] is True
    assert result["gemini"]["replacement_model"] is None
    assert "gemini.model" not in result["proposed_patch"]["set"]
    assert "one-secret" not in repr(result)
    assert "two-secret" not in repr(result)


def test_nested_provider_policy_uses_web_level_conditions_and_review_marker():
    result = run(
        {
            "configuration": {
                "gemini": {"model": "gemini-stable"},
                "web_search": {
                    "providers": {"duckduckgo": {"enabled": True}},
                    "fallback_on": ["provider_unavailable"],
                    "duckduckgo_reviewed": True,
                },
            },
            "failure_history": [],
            "verified_gemini_models": ["gemini-next"],
        }
    )

    assert result["status"] == "review"
    assert result["duckduckgo"]["configured"] is True
    assert result["duckduckgo"]["implicit_or_default"] is False
    assert result["duckduckgo"]["explicit_conditions"] is True
    assert result["duckduckgo"]["reviewed"] is True
    assert result["duckduckgo"]["audit_required"] is False
    assert "web_search.providers.duckduckgo.enabled" not in result["proposed_patch"]["set"]


def test_provider_level_conditions_are_also_recognized():
    result = run(
        {
            "configuration": {
                "routing": {"providers": {"gemini": {"model": "gemini-stable"}}},
                "tools": {
                    "web_search": {
                        "providers": {
                            "duckduckgo": {
                                "enabled": True,
                                "fallback_on": ["quota_exhausted", "provider_unavailable"],
                                "reviewed": True,
                            }
                        }
                    }
                },
            }
        }
    )

    assert result["gemini"]["configuration_location"] == "routing.providers.gemini"
    assert result["duckduckgo"]["configuration_location"] == "tools.web_search"
    assert result["duckduckgo"]["explicit_conditions"] is True
    assert result["duckduckgo"]["audit_required"] is False


def test_implicit_default_is_flagged_and_staged_disabled_pending_audit():
    result = run(
        {
            "configuration": {"gemini": {"model": "gemini-stable"}},
            "duckduckgo_default_enabled": True,
        }
    )

    duck = result["duckduckgo"]
    assert result["status"] == "review"
    assert duck["enabled"] is True
    assert duck["configured"] is False
    assert duck["implicit_or_default"] is True
    assert duck["audit_required"] is True
    assert result["proposed_patch"]["set"]["web_search.providers.duckduckgo.enabled"] is False
    assert result["proposed_patch"]["apply"] is False


def test_explicitly_disabled_fallback_needs_no_privacy_patch():
    result = run(
        {
            "configuration": {
                "gemini": {"model": "gemini-stable"},
                "actions": {"web_search": {"providers": {"duckduckgo": {"enabled": False}}}},
            }
        }
    )

    assert result["duckduckgo"]["configured"] is True
    assert result["duckduckgo"]["enabled"] is False
    assert result["duckduckgo"]["audit_required"] is False
    assert "web_search.providers.duckduckgo.enabled" not in result["proposed_patch"]["set"]


def test_invalid_arguments_fail_without_applying_anything():
    result = run(None)  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert result["proposed_patch"]["apply"] is False
