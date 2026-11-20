"""Behavior and protocol tests for voice-controlled HUD visibility."""

from __future__ import annotations

import importlib

import pytest

import features.hud_panel_visibility as feature_module


@pytest.fixture
def feature(tmp_path, monkeypatch):
    from app.hud import layout
    from core import visual_engine

    isolated = layout.WidgetLayout(tmp_path / "layout.json")
    monkeypatch.setattr(layout, "get_widget_layout", lambda: isolated)
    monkeypatch.setattr(visual_engine, "PATH", tmp_path / "scene.json")
    module = importlib.reload(feature_module)
    module.register_visual_dispatcher(None)
    yield module
    module.register_visual_dispatcher(None)


def test_feature_contract_and_panel_alias_ownership(feature):
    assert feature.FEATURE["id"] == "hud_panel_visibility"
    assert callable(feature.run)
    catalog = feature.panel_catalog()
    assert {panel["id"] for panel in catalog} == set(feature.PANELS)
    assert all(panel["aliases"] for panel in catalog)
    catalog[0]["aliases"].clear()
    assert feature.panel_catalog()[0]["aliases"]


def test_hide_command_has_required_top_level_panel_id(feature):
    result = feature.run({"text": "Please hide the live activity panel"})
    assert result["ok"] is True
    assert result["status"] == "applied"
    assert result["command"] == {
        "action": "panel",
        "panel_id": "hud.live_activity",
        "visible": False,
    }
    assert "payload" not in result["command"]
    assert "arguments" not in result["command"]


def test_show_command_has_required_top_level_panel_id(feature):
    result = feature.run({"text": "Bring back the context panel"})
    assert result["command"]["panel_id"] == "hud.context"
    assert result["command"]["visible"] is True
    assert result["command"]["action"] == feature.VISIBILITY_COMMAND_TYPE


@pytest.mark.parametrize(
    ("text", "panel_id", "visible"),
    [
        ("turn off the left sidebar", "hud.left", False),
        ("switch on the right panel", "hud.right", True),
        ("close conversation history", "hud.conversation", False),
        ("restore the tools panel", "hud.capabilities", True),
        ("dismiss developer panel", "hud.development", False),
        ("open preferences panel", "hud.settings", True),
    ],
)
def test_common_voice_phrases_map_to_stable_commands(feature, text, panel_id, visible):
    result = feature.parse_voice_command(text)
    assert result["ok"] is True
    assert result["command"]["panel_id"] == panel_id
    assert result["command"]["visible"] is visible


def test_structured_transcript_payload_is_accepted(feature):
    result = feature.run({"transcript": {"text": "minimise the activity panel"}})
    assert result["ok"] is True
    assert result["command"]["panel_id"] == "hud.live_activity"
    assert result["command"]["visible"] is False


def test_explicit_dispatcher_receives_top_level_panel_id_once(feature):
    received = []

    def dispatch(command):
        received.append(command)
        return {"success": True, "revision": 12}

    result = feature.run({"text": "hide the context panel", "dispatcher": dispatch})
    assert result["status"] == "applied"
    assert result["dispatch_result"] == {"success": True, "revision": 12}
    assert received == [result["command"]]
    assert received[0] is not result["command"]
    assert received[0]["panel_id"] == "hud.context"
    assert sum(key == "panel_id" for key in received[0]) == 1


def test_dispatcher_mutation_cannot_change_returned_command(feature):
    def dispatch(command):
        command["panel_id"] = "hud.settings"
        command["payload"] = {"panel_id": "hud.settings"}
        return {"success": True}

    result = feature.run({"text": "hide live activity", "dispatcher": dispatch})
    assert result["status"] == "applied"
    assert result["command"]["panel_id"] == "hud.live_activity"
    assert "payload" not in result["command"]


def test_visual_engine_object_dispatch_path_preserves_protocol(feature):
    class VisualEngine:
        def __init__(self):
            self.commands = []

        def dispatch(self, command):
            self.commands.append(command)
            return {"success": True, "message": "persisted"}

    engine = VisualEngine()
    result = feature.run({"text": "turn off conversation history", "visual_engine": engine})
    assert result["status"] == "applied"
    assert result["dispatch_result"] == {"success": True, "message": "persisted"}
    assert engine.commands[0]["panel_id"] == "hud.conversation"
    assert engine.commands[0]["visible"] is False


def test_registered_parent_dispatch_path_preserves_protocol(feature):
    received = []
    feature.register_visual_dispatcher(
        lambda command: received.append(command) or {"success": True}
    )

    result = feature.run({"text": "show the capabilities panel"})

    assert result["status"] == "applied"
    assert received == [
        {
            "action": feature.VISIBILITY_COMMAND_TYPE,
            "panel_id": "hud.capabilities",
            "visible": True,
        }
    ]


def test_explicit_dispatcher_takes_precedence_over_engine_and_registration(feature):
    calls = []

    class VisualEngine:
        def dispatch(self, command):
            calls.append(("engine", command))

    feature.register_visual_dispatcher(lambda command: calls.append(("registered", command)))
    result = feature.run(
        {
            "text": "hide settings panel",
            "visual_engine": VisualEngine(),
            "dispatcher": lambda command: calls.append(("explicit", command)) or {"success": True},
        }
    )
    assert result["status"] == "applied"
    assert [owner for owner, _ in calls] == ["explicit"]


def test_invalid_registration_is_rejected(feature):
    with pytest.raises(TypeError, match="callable"):
        feature.register_visual_dispatcher(object())


def test_dispatch_failure_does_not_claim_success(feature):
    def failing_dispatch(_command):
        raise RuntimeError("layout revision conflict")

    result = feature.run({"text": "hide context", "dispatcher": failing_dispatch})
    assert result["ok"] is False
    assert result["status"] == "dispatch_failed"
    assert result["error"] == "visual_engine_dispatch_failed"
    assert "layout revision conflict" in result["message"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "empty_command"),
        ("context panel", "missing_action"),
        ("hide the mystery panel", "unknown_panel"),
        ("hide context and activity panels", "multiple_panels"),
        ("hide and then show context", "conflicting_actions"),
        ("do not hide the context panel", "negated_action"),
        ("don't turn off the left panel", "negated_action"),
    ],
)
def test_uncertain_commands_are_rejected_without_dispatch(feature, text, error):
    received = []
    result = feature.run({"text": text, "dispatcher": received.append})
    assert result["ok"] is False
    assert result["error"] == error
    assert received == []


def test_catalog_request_lists_supported_panels_without_dispatch(feature):
    received = []
    result = feature.run(
        {"text": "What are the supported HUD panels?", "dispatcher": received.append}
    )
    assert result["ok"] is True
    assert result["kind"] == "catalog"
    assert {panel["id"] for panel in result["panels"]} == set(feature.PANELS)
    assert received == []


def test_validator_rejects_nested_panel_id_shape(feature):
    with pytest.raises(ValueError, match="must not be nested"):
        feature._validate_visibility_command(
            {
                "action": feature.VISIBILITY_COMMAND_TYPE,
                "panel_id": "hud.context",
                "visible": False,
                "payload": {"panel_id": "hud.context"},
            }
        )
