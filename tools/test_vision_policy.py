from __future__ import annotations

import ast
import unittest
from pathlib import Path

from core.safety_guard import classify_vision_risk
from core.vision_policy import evaluate_vision_recommendation
from core.vision_types import VisionElement, VisionResult


class TestVisionPolicy(unittest.TestCase):
    def setUp(self):
        self.default_result = VisionResult(
            ok=True,
            screen_state="desktop",
            summary="A clean desktop with a few icons.",
            recommended_action="none",
            needs_confirmation=False,
            elements=[
                VisionElement(
                    label="Folder",
                    type="icon",
                    risk="low",
                    action_hint="none",
                    confidence=0.95,
                ),
            ],
        )

    # ── Rule 1: result.ok is False ────────────────────────────────────────

    def test_result_not_ok_blocks(self):
        result = VisionResult(ok=False, screen_state="error", summary="Analysis failed.")
        policy = evaluate_vision_recommendation(result)
        self.assertFalse(policy["allow_action"])
        self.assertEqual(policy["recommended_path"], "no_action")

    # ── Rule 2: needs_confirmation → ask_user ─────────────────────────────

    def test_needs_confirmation_sets_ask_user(self):
        result = VisionResult(
            ok=True,
            screen_state="form",
            summary="Payment form detected.",
            needs_confirmation=True,
            recommended_action="ask_user",
            elements=[
                VisionElement(label="Pay", type="button", risk="medium", confidence=0.9),
            ],
        )
        policy = evaluate_vision_recommendation(result)
        self.assertTrue(policy["requires_confirmation"])
        self.assertEqual(policy["recommended_path"], "ask_user")

    # ── Rule 3: high-risk element ─────────────────────────────────────────

    def test_high_risk_element(self):
        result = VisionResult(
            ok=True,
            screen_state="desktop",
            summary="Delete confirmation dialog.",
            elements=[
                VisionElement(
                    label="Delete",
                    type="button",
                    risk="high",
                    action_hint="ask_user",
                    confidence=0.95,
                ),
            ],
        )
        policy = evaluate_vision_recommendation(result)
        self.assertEqual(policy["risk_level"], "high")
        self.assertTrue(policy["requires_confirmation"])

    # ── Rule 3b: medium-risk element → medium risk level ──────────────────

    def test_medium_risk_element(self):
        result = VisionResult(
            ok=True,
            screen_state="desktop",
            summary="Media player visible.",
            elements=[
                VisionElement(
                    label="Play",
                    type="button",
                    risk="medium",
                    action_hint="none",
                    confidence=0.88,
                ),
            ],
        )
        policy = evaluate_vision_recommendation(result)
        self.assertEqual(policy["risk_level"], "medium")

    # ── Rule 4: use_dom_first + mutation → block ─────────────────────────

    def test_use_dom_first_blocks_computer_control_click(self):
        result = VisionResult(
            ok=True,
            screen_state="browser",
            summary="Search results page.",
            recommended_action="use_dom_first",
            needs_confirmation=False,
        )
        intended = {"tool": "computer_control", "action": "click", "x": 100, "y": 200}
        policy = evaluate_vision_recommendation(result, intended)
        self.assertFalse(policy["allow_action"])
        self.assertEqual(policy["recommended_path"], "use_dom_first")

    # ── Rule 5: browser state + computer_control mutation → block ─────────

    def test_browser_state_blocks_computer_control(self):
        result = VisionResult(
            ok=True,
            screen_state="browser",
            summary="YouTube page.",
            recommended_action="none",
        )
        intended = {"tool": "computer_control", "action": "click", "x": 300, "y": 400}
        policy = evaluate_vision_recommendation(result, intended)
        self.assertFalse(policy["allow_action"])
        self.assertEqual(policy["recommended_path"], "use_dom_first")

    # ── Rule 5b: media_page state blocks computer_control ─────────────────

    def test_media_page_blocks_computer_control(self):
        result = VisionResult(
            ok=True,
            screen_state="media_page",
            summary="Netflix title page.",
            recommended_action="use_dom_first",
        )
        intended = {"tool": "computer_control", "action": "click", "x": 200, "y": 300}
        policy = evaluate_vision_recommendation(result, intended)
        self.assertEqual(policy["recommended_path"], "use_dom_first")

    # ── Rule 7: desktop + all low + no mutation → safe_to_describe ────────

    def test_desktop_low_risk_safe_to_describe(self):
        policy = evaluate_vision_recommendation(self.default_result)
        self.assertEqual(policy["recommended_path"], "safe_to_describe")
        self.assertTrue(policy["allow_action"])

    # ── Rule 6: error state → ask_user ────────────────────────────────────

    def test_error_state_ask_user(self):
        result = VisionResult(
            ok=True,
            screen_state="error",
            summary="Something went wrong — error page shown.",
            recommended_action="none",
        )
        policy = evaluate_vision_recommendation(result)
        self.assertEqual(policy["recommended_path"], "ask_user")
        self.assertFalse(policy["allow_action"])
        self.assertEqual(policy["risk_level"], "high")

    # ── Rule 8: no elements → safe_to_describe ────────────────────────────

    def test_empty_elements_safe_to_describe(self):
        result = VisionResult(
            ok=True,
            screen_state="desktop",
            summary="Nothing visible.",
            elements=[],
        )
        policy = evaluate_vision_recommendation(result)
        self.assertEqual(policy["recommended_path"], "safe_to_describe")

    # ── Browser_control mutation also blocked ──────────────────────────────

    def test_browser_control_mutation_blocked(self):
        result = VisionResult(
            ok=True,
            screen_state="browser",
            summary="Form visible.",
            recommended_action="use_dom_first",
        )
        intended = {"tool": "browser_control", "action": "fill_form", "value": "test"}
        policy = evaluate_vision_recommendation(result, intended)
        self.assertFalse(policy["allow_action"])
        self.assertEqual(policy["recommended_path"], "use_dom_first")

    # ── Desktop + non-mutation action allowed ─────────────────────────────

    def test_desktop_non_mutation_allowed(self):
        intended = {"tool": "computer_control", "action": "running_apps"}
        policy = evaluate_vision_recommendation(self.default_result, intended)
        self.assertTrue(policy["allow_action"])

    # ── High risk overrides medium ────────────────────────────────────────

    def test_high_risk_overrides_medium(self):
        result = VisionResult(
            ok=True,
            screen_state="desktop",
            summary="Mixed risk elements.",
            elements=[
                VisionElement(label="Play", type="button", risk="medium", confidence=0.8),
                VisionElement(label="Delete", type="button", risk="high", confidence=0.9),
            ],
        )
        policy = evaluate_vision_recommendation(result)
        self.assertEqual(policy["risk_level"], "high")
        self.assertTrue(policy["requires_confirmation"])

    # ── No computer_control import in policy module ───────────────────────

    def test_no_computer_control_import(self):
        path = Path(__file__).resolve().parent.parent / "core" / "vision_policy.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        "computer_control",
                        alias.name,
                        "vision_policy.py must not import computer_control",
                    )
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    "computer_control",
                    (node.module or ""),
                    "vision_policy.py must not import from computer_control",
                )
                for alias in node.names:
                    self.assertNotIn(
                        "computer_control",
                        alias.name,
                        "vision_policy.py must not import computer_control",
                    )

    # ── No pyautogui import in policy module ──────────────────────────────

    def test_no_pyautogui_import(self):
        path = Path(__file__).resolve().parent.parent / "core" / "vision_policy.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("pyautogui", source)

    # ── safety_guard.classify_vision_risk delegates to VisionPolicy ───────

    def test_safety_guard_classify_vision_risk(self):
        policy = classify_vision_risk(self.default_result)
        self.assertIn("risk_level", policy)
        self.assertIn("requires_confirmation", policy)
        self.assertIn("allow_action", policy)
        self.assertIn("recommended_path", policy)
        self.assertIn("reason", policy)

    # ── safety_guard fallback blocks mutation when VisionPolicy unavailable ─

    def test_safety_guard_fallback_blocks_mutation(self):
        intended = {"tool": "computer_control", "action": "click"}
        policy = classify_vision_risk(None, intended)
        self.assertFalse(policy["allow_action"])
        self.assertTrue(policy["requires_confirmation"])
        self.assertEqual(policy["recommended_path"], "ask_user")

    # ── safety_guard fallback requires confirmation when no vision result ───

    def test_safety_guard_fallback_requires_confirmation(self):
        policy = classify_vision_risk(None, None)
        self.assertFalse(policy["allow_action"])
        self.assertTrue(policy["requires_confirmation"])
        self.assertEqual(policy["recommended_path"], "ask_user")
        self.assertIn("No vision result", policy["reason"])
