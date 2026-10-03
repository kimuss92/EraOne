"""Real persisted scene validation and concurrent revision checks."""

import concurrent.futures
import json

import pytest

from core import autopilot
from core import visual_engine as visual


@pytest.fixture(autouse=True)
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "PATH", tmp_path / "scene.json")


def test_scene_round_trip_and_compare_swap():
    state = visual.apply(
        {
            "action": "upsert",
            "widget": {"id": "task", "kind": "progress", "binding": "development", "label": "Work"},
        }
    )
    assert state == visual.snapshot()
    with pytest.raises(ValueError, match="Scene changed"):
        visual.apply({"action": "reset", "revision": 0})
    assert visual.snapshot() == state
    assert visual.apply({"action": "reset", "revision": 1})["widgets"] == []


@pytest.mark.parametrize(
    "widget",
    [
        {"id": "../x", "kind": "text"},
        {"id": "x", "kind": "html"},
        {"id": "x", "kind": "progress", "value": float("nan")},
        {"id": "x", "kind": "progress", "value": 101},
        {"id": "x", "kind": "progress", "value": True},
        {"id": "x", "kind": "text", "label": "x" * 121},
        {"id": "x", "kind": "button", "action": {"command": "delete files"}},
        {"id": "x", "kind": "chart", "series": [0] * 129},
        {"id": "x", "kind": "metric", "binding": "shell"},
    ],
)
def test_rejects_unsupported_payload_without_state_change(widget):
    with pytest.raises(ValueError):
        visual.apply({"action": "upsert", "widget": widget})
    assert visual.snapshot()["revision"] == 0


@pytest.mark.parametrize(
    "layout",
    [{"accent": "url(x)"}, {"columns": True}, {"columns": 4}, {"css": "x"}, {"left": 1}, []],
)
def test_layout_is_constrained(layout):
    with pytest.raises(ValueError):
        visual.apply({"action": "layout", "layout": layout})


def test_concurrent_edit_has_one_revision_winner():
    def attempt(i):
        try:
            return visual.apply(
                {"action": "upsert", "revision": 0, "widget": {"id": f"item{i}", "kind": "text"}}
            )
        except ValueError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(result is not None for result in pool.map(attempt, range(8))) == 1
    assert visual.snapshot()["revision"] == 1


def test_malformed_persistence_recovers():
    visual.PATH.write_text('{"revision":true,"widgets":[]}', encoding="utf-8")
    assert visual.snapshot()["revision"] == 0
    visual.apply({"action": "layout", "layout": {"accent": "#112233"}})
    assert json.loads(visual.PATH.read_text())["revision"] == 1


@pytest.mark.parametrize(
    "text",
    ["hide the right panel", "set HUD accent #12aabb", "add liquid progress for background tasks"],
)
def test_visual_commands_do_not_use_a_model(text):
    decision = autopilot.classify(text)
    assert decision.lane == autopilot.LOCAL and decision.tool == "visual_engine"
    assert visual.apply(json.loads(decision.args["command"]))["revision"] == 1


def test_explicit_feature_request_stages_in_specialist():
    result = autopilot.classify("add a workload balancing feature to Era One")
    assert result.lane == autopilot.SPECIALIST
    assert result.tool == "autonomous_development" and result.args["action"] == "propose"
