from __future__ import annotations

import ast
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core.vision_context import (
    clear_latest_vision_result,
    get_latest_vision_result,
    store_latest_vision_result,
)
from core.vision_types import VisionElement, VisionResult

# ── Helper: route_tool_call with standard patches ─────────────────────────


def _route(
    tool: str,
    params: dict,
    user_text: str = "",
    extra_mocks=None,
) -> tuple[str, dict]:
    """Call route_tool_call with standard patches on self_healing_router."""
    from core.self_healing_router import route_tool_call

    base_patches = [
        patch(
            "core.self_healing_router.canonicalize_tool_call",
            side_effect=lambda t, p: (t, dict(p)),
        ),
        patch("core.self_healing_router.load_state", return_value={}),
        patch("core.self_healing_router.remember_action"),
        # `is_running` and `get_recent_grid` used to be patched here too. The
        # router imported both and never called either, so the patches replaced
        # names nothing reached — they isolated nothing. Once the dead imports
        # were dropped the patches failed outright with AttributeError, which is
        # what surfaced them. Removed rather than resurrected: re-adding an
        # unused import purely to feed an inert patch would restore the illusion
        # of coverage, not the coverage.
        patch(
            "core.self_healing_router.get_active_title",
            return_value="",
        ),
        patch(
            "core.self_healing_router.get_scroll_context",
            return_value=None,
        ),
        patch("core.self_healing_router.set_last_user_query"),
        patch(
            "core.self_healing_router.load_recent_grids",
            return_value={},
        ),
        patch(
            "core.self_healing_router.get_last_user_query",
            return_value={},
        ),
        patch("core.self_healing_router.log"),
    ]
    all_patches = base_patches + (extra_mocks or [])
    with ExitStack() as stack:
        for p in all_patches:
            stack.enter_context(p)
        return route_tool_call(tool, params, user_text)


def _make_low_risk_result(**overrides) -> VisionResult:
    params = dict(
        ok=True,
        screen_state="desktop",
        summary="A clean desktop.",
        recommended_action="none",
        needs_confirmation=False,
        elements=[
            VisionElement(label="File", type="icon", risk="low", confidence=0.95),
        ],
    )
    params.update(overrides)
    return VisionResult(**params)


class TestVisionContext(unittest.TestCase):
    """Direct tests of core/vision_context.py ."""

    def setUp(self):
        clear_latest_vision_result()

    def tearDown(self):
        clear_latest_vision_result()

    # ── 1. store/retrieve latest VisionResult ────────────────────────────

    def test_store_and_retrieve_latest(self):
        result = _make_low_risk_result()
        store_latest_vision_result("vr-test", result)
        ctx = get_latest_vision_result()
        self.assertIsNotNone(ctx)
        ctx_id, ctx_result = ctx
        self.assertEqual(ctx_id, "vr-test")
        self.assertEqual(ctx_result.screen_state, "desktop")

    # ── 2. expired latest VisionResult returns None ──────────────────────

    def test_expired_latest_returns_none(self):
        result = _make_low_risk_result()
        store_latest_vision_result("vr-test", result)
        ctx = get_latest_vision_result(max_age_seconds=0.0)
        self.assertIsNone(ctx)

    # ── 3. clear latest VisionResult works ───────────────────────────────

    def test_clear_latest_works(self):
        result = _make_low_risk_result()
        store_latest_vision_result("vr-test", result)
        self.assertIsNotNone(get_latest_vision_result())
        clear_latest_vision_result()
        self.assertIsNone(get_latest_vision_result())

    # ── 4. vision_context has no computer_control ────────────────────────

    def test_vision_context_no_computer_control(self):
        path = Path(__file__).resolve().parent.parent / "core" / "vision_context.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        "computer_control",
                        alias.name,
                        "vision_context.py must not import computer_control",
                    )
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    "computer_control",
                    (node.module or ""),
                    "vision_context.py must not import from computer_control",
                )
                for alias in node.names:
                    self.assertNotIn(
                        "computer_control",
                        alias.name,
                        "vision_context.py must not import computer_control",
                    )

    # ── 5. vision_context has no pyautogui ───────────────────────────────

    def test_vision_context_no_pyautogui(self):
        path = Path(__file__).resolve().parent.parent / "core" / "vision_context.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("pyautogui", source)


class TestVisionRouterGuard(unittest.TestCase):
    """Tests for the VisionPolicy guard in self_healing_router.route_tool_call()."""

    def setUp(self):
        clear_latest_vision_result()

    def tearDown(self):
        clear_latest_vision_result()

    # ── 6. high-risk VisionResult blocks computer_control click ──────────

    def test_high_risk_blocks_computer_control_click(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="browser",
                recommended_action="none",
                elements=[
                    VisionElement(
                        label="Delete",
                        type="button",
                        risk="high",
                        confidence=0.9,
                    ),
                ],
            ),
        )
        tool, params = _route("computer_control", {"action": "click", "x": 100, "y": 200})
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "result")
        self.assertEqual(params.get("request_id"), "latest")

    # ── 7. needs_confirmation blocks silent mutation ─────────────────────

    def test_needs_confirmation_blocks_silent_mutation(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="form",
                recommended_action="ask_user",
                needs_confirmation=True,
                elements=[
                    VisionElement(
                        label="Submit",
                        type="button",
                        risk="medium",
                        confidence=0.9,
                    ),
                ],
            ),
        )
        tool, params = _route("computer_control", {"action": "click", "x": 100, "y": 200})
        self.assertEqual(tool, "screen_process")

    # ── 8. browser state blocks computer_control click ───────────────────

    def test_browser_state_blocks_computer_control_click(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="browser",
                recommended_action="none",
            ),
        )
        tool, params = _route("computer_control", {"action": "click", "x": 300, "y": 400})
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("request_id"), "latest")

    # ── 9. media_page blocks computer_control click ──────────────────────

    def test_media_page_blocks_computer_control_click(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="media_page",
                recommended_action="use_dom_first",
            ),
        )
        tool, params = _route("computer_control", {"action": "click", "x": 200, "y": 300})
        self.assertEqual(tool, "screen_process")

    # ── 10. use_dom_first blocks coordinate click ────────────────────────

    def test_use_dom_first_blocks_computer_control_click(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="browser",
                recommended_action="use_dom_first",
            ),
        )
        tool, params = _route("computer_control", {"action": "click", "x": 100, "y": 200})
        self.assertEqual(tool, "screen_process")

    # ── 11. browser_control DOM action NOT blocked by use_dom_first alone ─

    def test_browser_control_not_blocked_by_use_dom_first_alone(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="browser",
                recommended_action="use_dom_first",
            ),
        )
        tool, params = _route(
            "browser_control", {"action": "fill_form", "value": "hello", "selector": "#input"}
        )
        self.assertNotEqual(
            (tool, params), ("screen_process", {"action": "result", "request_id": "latest"})
        )
        self.assertEqual(tool, "browser_control")

    # ── 12. browser_control blocked when high risk exists ────────────────

    def test_browser_control_blocked_when_high_risk(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="browser",
                recommended_action="none",
                elements=[
                    VisionElement(
                        label="Delete",
                        type="button",
                        risk="high",
                        confidence=0.95,
                    ),
                ],
            ),
        )
        tool, params = _route("browser_control", {"action": "fill_form", "value": "test"})
        self.assertEqual(tool, "screen_process")

    # ── 13. desktop low-risk without policy block leaves routing unchanged ─

    def test_desktop_low_risk_leaves_routing_unchanged(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="desktop",
                recommended_action="none",
            ),
        )
        tool, params = _route("computer_control", {"action": "click", "x": 500, "y": 600})
        self.assertNotEqual(tool, "screen_process")

    # ── 14. no recent VisionResult leaves routing unchanged ──────────────

    def test_no_recent_vision_leaves_routing_unchanged(self):
        clear_latest_vision_result()
        tool, params = _route("computer_control", {"action": "click", "x": 100, "y": 200})
        self.assertNotEqual(tool, "screen_process")

    # ── 15. non-mutation action ignored ──────────────────────────────────

    def test_non_mutation_action_ignored(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(
                screen_state="browser",
                recommended_action="use_dom_first",
            ),
        )
        tool, params = _route("computer_control", {"action": "running_apps"})
        self.assertNotEqual(tool, "screen_process")
        self.assertEqual(tool, "computer_control")
        self.assertEqual(params.get("action"), "running_apps")

    # ── 16. VisionPolicy failure with recent VisionResult fails closed ────

    def test_vision_policy_failure_fails_closed(self):
        store_latest_vision_result(
            "vr-test",
            _make_low_risk_result(screen_state="desktop"),
        )
        with patch(
            "core.vision_context.get_latest_vision_result",
            side_effect=RuntimeError("simulated crash"),
        ):
            tool, params = _route("computer_control", {"action": "click", "x": 100, "y": 200})
            self.assertEqual(tool, "screen_process")
            self.assertEqual(params.get("action"), "result")
            self.assertEqual(params.get("request_id"), "latest")


class TestScreenProcessorRequestIdLatest(unittest.TestCase):
    """Tests for screen_process request_id='latest' support."""

    def setUp(self):
        clear_latest_vision_result()

    def tearDown(self):
        clear_latest_vision_result()

    # ── 17. request_id='latest' returns formatted result ────────────────

    def test_request_id_latest_returns_formatted_result(self):
        result = VisionResult(
            ok=True,
            screen_state="desktop",
            summary="Test summary.",
            recommended_action="none",
            needs_confirmation=False,
            elements=[
                VisionElement(label="Icon", type="icon", risk="low", confidence=0.9),
            ],
        )
        store_latest_vision_result("my-vr", result)

        from actions.screen_processor import _handle_vision_result

        output = _handle_vision_result({"action": "result", "request_id": "latest"})
        self.assertIn("Vision result my-vr:", output)
        self.assertIn("Screen: desktop", output)
        self.assertIn("Test summary.", output)

    # ── 18. request_id='latest' with empty context returns message ───────

    def test_request_id_latest_no_result(self):
        clear_latest_vision_result()
        from actions.screen_processor import _handle_vision_result

        output = _handle_vision_result({"action": "result", "request_id": "latest"})
        self.assertEqual(output, "No latest vision result is available. Start an analysis first.")

    # ── 19. blocking path stores vision result ──────────────────────────

    def test_blocking_path_stores_vision_result(self):
        stored = []

        def fake_store(rid, result):
            stored.append((rid, result))

        with (
            patch("core.vision_context.store_latest_vision_result", side_effect=fake_store),
            patch("core.vision_service.analyze_screen") as mock_analyze,
            patch("actions.screen_processor.analyze_image_once"),
            patch("actions.screen_processor.capture_screen_raw"),
        ):
            vr = _make_low_risk_result(
                screen_state="desktop",
                summary="Blocking path test.",
            )
            mock_analyze.return_value = vr

            from actions.screen_processor import _get_cached, _set_cached, screen_process

            _set_cached("test:cache:key", None)

            output = screen_process({"text": "what do you see", "angle": "screen"})

        self.assertTrue(
            any(rid == "latest" for rid, _ in stored),
            "Expected store_latest_vision_result('latest', ...) to be called",
        )
        self.assertIn("Blocking path test.", output)


class TestScreenProcessRouterGuard(unittest.TestCase):
    """Router must not hijack screen_process vision actions."""

    # ── Test 20: Router preserves screen_process {"action": "list"} ─────

    def test_router_preserves_screen_process_list(self):
        tool, params = _route("screen_process", {"action": "list"})
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "list")

    # ── Test 21: Router preserves screen_process {"action": "result"} ───

    def test_router_preserves_screen_process_result(self):
        tool, params = _route("screen_process", {"action": "result"})
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "result")

    # ── Test 22: Router preserves screen_process with action="screen" ───

    def test_router_preserves_screen_process_action_screen(self):
        tool, params = _route("screen_process", {"action": "screen", "text": "What do you see?"})
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "screen")

    # ── Test 23: Router preserves screen_process action="status" ────────

    def test_router_preserves_screen_process_status(self):
        tool, params = _route("screen_process", {"action": "status"})
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "status")

    # ── Test 24: Router preserves screen_process action="clear" ─────────

    def test_router_preserves_screen_process_clear(self):
        tool, params = _route("screen_process", {"action": "clear"})
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "clear")

    # ── Test 25: Router preserves screen_process with text (no action) ──

    def test_router_preserves_screen_process_with_text(self):
        tool, params = _route("screen_process", {"text": "What do you see?"})
        self.assertEqual(tool, "screen_process")

    # ── Test 26: Router preserves screen_process async ──

    def test_router_preserves_screen_process_async(self):
        tool, params = _route("screen_process", {"async": True, "text": "Analyze in background"})
        self.assertEqual(tool, "screen_process")
        self.assertTrue(params.get("async"))

    # ── Test 27: Router preserves screen_process even with Netflix active ──

    def test_router_preserves_screen_process_with_netflix_active(self):
        tool, params = _route(
            "screen_process",
            {"action": "list"},
            extra_mocks=[
                patch("core.self_healing_router.get_scroll_context", return_value="netflix"),
            ],
        )
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "list")

    # ── Test 28: Router preserves screen_process with empty action + text ──

    def test_router_preserves_screen_process_empty_action_with_text(self):
        tool, params = _route("screen_process", {"text": "Describe what you see"})
        self.assertEqual(tool, "screen_process")

    # ── Test 29: Router preserves screen_process with action=screen + text ──

    def test_router_preserves_screen_process_action_screen_with_text(self):
        tool, params = _route(
            "screen_process",
            {"action": "screen", "text": "Analyze my screen"},
        )
        self.assertEqual(tool, "screen_process")
        self.assertEqual(params.get("action"), "screen")


class TestRecordToolResultNetflixPollution(unittest.TestCase):
    """record_tool_result must not set Netflix failure/cooldown for screen_process."""

    def setUp(self):
        from core.browser_grid_state import clear_scroll_context_for_tests
        from core.intent_memory import save_state

        clear_scroll_context_for_tests()
        save_state({})

    # ── Test 30: screen_process error does NOT set Netflix failure ──────

    def test_screen_process_error_no_netflix_failure(self):
        from core.self_healing_router import record_tool_result

        nf_failure_called = []

        def fake_set_failure(**kwargs):
            nf_failure_called.append(kwargs)

        with patch(
            "actions.netflix_control._set_netflix_failure",
            side_effect=fake_set_failure,
        ):
            record_tool_result(
                "screen_process",
                {"action": "screen"},
                result=None,
                error="I could not analyze the screen quickly enough.",
            )

        self.assertEqual(
            nf_failure_called,
            [],
            "screen_process failure must NOT trigger Netflix failure context",
        )

    # ── Test 31: screen_process 503 error does NOT set Netflix cooldown ─

    def test_screen_process_503_no_netflix_cooldown(self):
        from core.self_healing_router import record_tool_result

        nf_cooldown_called = []

        def fake_cooldown(duration=60.0):
            nf_cooldown_called.append(duration)

        with patch(
            "actions.netflix_control._set_netflix_vision_cooldown",
            side_effect=fake_cooldown,
        ):
            record_tool_result(
                "screen_process",
                {"action": "screen"},
                result=None,
                error="503 UNAVAILABLE: This model is currently experiencing high demand",
            )

        self.assertEqual(
            nf_cooldown_called,
            [],
            "screen_process 503 must NOT trigger Netflix vision cooldown",
        )

    # ── Test 32: Netflix control failure DOES still set Netflix failure ──

    def test_netflix_control_failure_sets_netflix_failure(self):
        from core.self_healing_router import record_tool_result

        nf_failure_called = []

        def fake_set_failure(**kwargs):
            nf_failure_called.append(kwargs)

        with patch(
            "actions.netflix_control._set_netflix_failure",
            side_effect=fake_set_failure,
        ):
            record_tool_result(
                "netflix_control",
                {"action": "search"},
                result=None,
                error="could not resolve Netflix page",
            )

        self.assertEqual(len(nf_failure_called), 1)

    # ── Test 33: browser_control Netflix collision DOES set failure ─────

    def test_browser_control_netflix_collision_sets_failure(self):
        from core.self_healing_router import record_tool_result

        nf_failure_called = []

        def fake_set_failure(**kwargs):
            nf_failure_called.append(kwargs)

        with patch(
            "actions.netflix_control._set_netflix_failure",
            side_effect=fake_set_failure,
        ):
            record_tool_result(
                "browser_control",
                {"action": "go_to", "url": "https://netflix.com"},
                result="netflix.com connection refused",
                error=None,
            )

        self.assertEqual(len(nf_failure_called), 1)
