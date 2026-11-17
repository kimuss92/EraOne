"""Sprint 1, slice 1 — evidence-gated completion (2026-11-11).

Three behaviours the audit found missing, each pinned here:

1. the CONFIRMED-action path must re-enter verification (before this change it
   returned `_execute_tool_raw` directly, so a delete/send/overwrite approved by
   the user was the one action never checked);
2. "the tool returned without raising" is NOT evidence: a side-effecting result
   with no evidence-based verifier is `unverified`, the model is told so, and a
   background report may not speak it as success;
3. an action outside a tool's declared vocabulary fails CLOSED instead of
   falling through to LOW (`file_controller(action="organize_desktop")` used to
   bulk-move the Desktop with no confirmation).

The tests are deliberately behavioural where the code allows it, and structural
(AST) only for the confirm branch, whose real dependencies make a live call
impractical here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.era_live.speak_once import spoken_outcome
from app.era_live.tool_dispatch import finalize_tool_result
from core.completion_contract import (
    annotate,
    is_unconfirmed,
    model_note,
    verdict,
)
from core.main_tool_bridge import tool_result_to_response_payload
from core.safety_guard import classify_action
from core.tool_result import ToolResult

REPO = Path(__file__).resolve().parent.parent
DISPATCH = REPO / "app" / "era_live" / "tool_dispatch.py"


# ── 1. The confirmed path verifies ──────────────────────────────────────────


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(path.read_text(encoding="utf-8"), node)
            assert segment, f"could not extract source for {name}"
            return segment
    raise AssertionError(f"{name} not found in {path}")


def test_the_confirmed_branch_hands_the_real_tool_to_verification():
    """The confirm handler must verify the action it RAN, not its own name.

    `verify_action_result("confirm_pending_confirmation")` finds no verifier and
    would report every approved delete/send as an unverified success — or, worse,
    overwrite an evidence-based verdict. The handler records what ran, and the
    outer dispatcher finalizes under that name.
    """
    handler = _function_source(DISPATCH, "_execute_tool_raw")
    branch = handler[handler.index("confirm_pending_confirmation") :]
    assert "_confirmed_action" in branch, (
        "the confirmed branch must record the tool and parameters it executed"
    )

    outer = _function_source(DISPATCH, "_execute_tool_result_inner")
    assert 'tool_name == "confirm_pending_confirmation"' in outer
    assert "_confirmed_action" in outer
    assert "verify_tool" in outer, "the recorded tool must reach _finalize_tool_result"


def test_finalize_runs_the_verifier_not_just_from_any():
    """`from_any` reads the string as success; the web_search verifier does not."""
    raw = "No results found for that query."
    assert ToolResult.from_any(raw).success is True, "precondition: from_any alone accepts it"

    result = finalize_tool_result(object(), "web_search", {"query": "x"}, raw)

    assert result.success is False
    assert result.verification_status == "FAILED"
    assert result.data["completion"]["verdict"] == "failed"


def test_finalize_attaches_a_completion_verdict_to_every_result():
    result = finalize_tool_result(object(), "code_helper", {}, None)

    assert result.success is True
    assert result.data["completion"]["verdict"] == "unverified", (
        "the generic verifier must not count as evidence"
    )


# ── 2. Evidence, not the absence of an exception ────────────────────────────


def test_generic_verifier_is_not_evidence():
    result = ToolResult.ok("Done.", tool_name="code_helper")
    result.verified = True
    result.verifier_name = "generic"
    result.verification_status = "VERIFIED"

    assert verdict(result) == "unverified"


def test_evidence_based_verifier_is_proven():
    result = ToolResult.ok("Folder created.", tool_name="file_controller")
    result.verified = True
    result.verifier_name = "file_controller"
    result.verification_status = "VERIFIED"

    assert verdict(result) == "proven"
    assert model_note(annotate(result, "file_controller", "create_folder")) is None


def test_informational_tool_is_not_called_unconfirmed():
    result = ToolResult.ok("Three results: ...", tool_name="web_search")
    annotate(result, "web_search", "search")

    assert verdict(result) == "informational"
    assert not is_unconfirmed(result)


def test_unproven_side_effect_reaches_the_model_as_not_confirmed():
    result = annotate(
        ToolResult.ok("Message sent.", tool_name="send_message"), "send_message", "send"
    )

    payload = tool_result_to_response_payload(result)

    assert payload["completion"]["verdict"] == "unverified"
    assert "NOT CONFIRMED" in payload["completion_instruction"]
    assert payload["success"] is True, "the tag must not rewrite the tool's own outcome"


def test_proven_result_carries_no_instruction():
    result = ToolResult.ok("Chrome is open.", tool_name="open_app")
    result.verified = True
    result.verifier_name = "open_app"
    result.verification_status = "VERIFIED"
    annotate(result, "open_app")

    payload = tool_result_to_response_payload(result)

    assert payload["completion"]["verdict"] == "proven"
    assert "completion_instruction" not in payload


def test_failure_is_never_upgraded_by_the_tag():
    result = annotate(
        ToolResult.fail("Access denied", error_type="ActionError"),
        "file_controller",
        "delete",
    )

    assert verdict(result) == "failed"
    assert not is_unconfirmed(result)


def test_background_speech_qualifies_an_unconfirmed_success():
    result = annotate(
        ToolResult.ok("Playing 'X' on Spotify, sir.", tool_name="spotify_control"),
        "spotify_control",
        "play",
    )

    said = spoken_outcome("spotify_control", result)

    assert "Playing 'X' on Spotify" in said
    assert "couldn't confirm" in said


def test_background_speech_keeps_a_proven_success_verbatim():
    result = ToolResult.ok("Playing 'X' on Spotify, sir.", tool_name="spotify_control")
    result.verified = True
    result.verifier_name = "spotify_native"
    result.verification_status = "VERIFIED"
    annotate(result, "spotify_control", "play")

    assert spoken_outcome("spotify_control", result) == "Playing 'X' on Spotify, sir."


# ── 3. Unknown actions fail closed ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "action"),
    [
        ("file_controller", "organize_desktop"),
        ("file_controller", "bulk_move"),
    ],
)
def test_an_action_outside_the_declared_vocabulary_requires_confirmation(tool, action):
    verdict_ = classify_action(tool, {"action": action})

    assert verdict_["requires_confirmation"] is True
    assert verdict_["risk_level"] == "MEDIUM"
    assert "failing closed" in verdict_["reason"].lower()


def test_desktop_control_unrecognised_action_stays_on_the_high_planner_rule():
    """Pinned so the vocabulary branch is not duplicated for this tool."""
    verdict_ = classify_action("desktop_control", {"action": "sweep"})

    assert verdict_["requires_confirmation"] is True
    assert verdict_["risk_level"] == "HIGH"


def test_known_file_actions_keep_their_existing_weight():
    read = classify_action("file_controller", {"action": "read", "path": "C:/tmp/a.txt"})
    delete = classify_action("file_controller", {"action": "delete", "path": "C:/tmp/a.txt"})

    assert read["requires_confirmation"] is False
    assert read["risk_level"] == "LOW"
    assert delete["risk_level"] == "HIGH"


def test_empty_action_is_not_gated_by_the_vocabulary_rule():
    """Status-style calls with no action keep their previous classification."""
    assert classify_action("gmail_control", {})["risk_level"] == "LOW"
    assert classify_action("nonexistent_tool_xyz")["risk_level"] == "LOW"
