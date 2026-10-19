"""Tests for the reloadable capability audit feature; no live tools are used."""

from __future__ import annotations

from unittest.mock import patch

from features import capability_audit

DECLARATIONS = [
    {
        "name": "music_control",
        "description": "Play, pause, and search for music.",
        "parameters": {"properties": {}},
    },
    {
        "name": "email_reader",
        "description": "Read and summarize email messages.",
        "parameters": {"properties": {}},
    },
]


def test_feature_contract_is_reloadable():
    assert capability_audit.FEATURE == {
        "id": "capability_audit",
        "description": (
            "Assess one or more requested goals against Era's currently offered tools, "
            "returning truthful related, unsupported, or unavailable findings."
        ),
    }
    assert callable(capability_audit.run)


def test_assesses_multiple_goals_without_claiming_related_means_supported():
    with patch("core.capability_index.declarations", return_value=DECLARATIONS):
        result = capability_audit.run(
            {"goals": ["play music", "teleport to the moon", "read email"]}
        )

    assert result["ok"] is True
    assert result["mode"] == "assess"
    assert [item["verdict"] for item in result["assessments"]] == [
        "related_not_confirmed",
        "unsupported",
        "related_not_confirmed",
    ]
    assert result["counts"] == {
        "related_not_confirmed": 2,
        "unsupported": 1,
        "unavailable": 0,
    }
    assert "not confirmation" in result["guidance"].lower()
    assert "NOT SUPPORTED" in result["assessments"][1]["evidence"]


def test_normalizes_deduplicates_and_caps_large_batches():
    goals = ["  Read   email  ", "read email", "READ EMAIL"]
    goals.extend(f"goal number {number}" for number in range(25))

    with patch("core.capability_index.check", return_value="NOT SUPPORTED. No tool.") as check:
        result = capability_audit.run({"goals": goals})

    assert result["ok"] is True
    assert len(result["assessments"]) == 20
    assert result["assessments"][0]["goal"] == "Read email"
    assert result["omitted"] == 6
    assert check.call_count == 20


def test_inventory_delegates_to_truthful_capability_summary():
    with patch("core.capability_index.summary", return_value="generated inventory") as summary:
        result = capability_audit.run({"mode": "inventory", "limit": 7})

    assert result == {
        "ok": True,
        "feature": "capability_audit",
        "mode": "inventory",
        "inventory": "generated inventory",
    }
    summary.assert_called_once_with(limit=7)


def test_invalid_arguments_return_structured_errors():
    cases = [
        None,
        [],
        {},
        {"mode": "unknown"},
        {"goals": ["valid", 4]},
        {"mode": "inventory", "limit": True},
        {"mode": "inventory", "limit": 101},
    ]
    for arguments in cases:
        result = capability_audit.run(arguments)
        assert result["ok"] is False
        assert result["feature"] == "capability_audit"
        assert result["error"]


def test_unreadable_declarations_are_reported_as_unavailable_not_supported():
    with patch("core.capability_index.declarations", side_effect=OSError("fixture unavailable")):
        result = capability_audit.run({"goal": "play music"})

    assessment = result["assessments"][0]
    assert assessment["verdict"] == "unavailable"
    assert result["counts"]["unavailable"] == 1
    assert "do not claim" in assessment["evidence"].lower()
