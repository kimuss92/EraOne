"""Widget ownership, persistence, concurrent edits and unsafe schema rejection."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.hud.layout import LayoutConflict, WidgetLayout


def test_layout_recovers_from_temporary_windows_lock(tmp_path, monkeypatch):
    from app.hud import layout

    events, attempts = [], []
    service = WidgetLayout(tmp_path / "layout.json", notify=events.append)
    replace = layout.os.replace

    def temporarily_locked(source, destination):
        attempts.append(True)
        if len(attempts) < 3:
            raise PermissionError("transient Windows lock")
        replace(source, destination)

    monkeypatch.setattr(layout.os, "replace", temporarily_locked)
    monkeypatch.setattr(layout.time, "sleep", lambda _: None)
    changed = service.apply(0, widget="calls", visible=False)
    assert len(attempts) == 3 and events == [changed]
    assert WidgetLayout(service.path).snapshot() == changed


def test_layout_persistent_lock_preserves_revision_and_saved_bytes(tmp_path, monkeypatch):
    from app.hud import layout

    events, attempts = [], []
    service = WidgetLayout(tmp_path / "layout.json", notify=events.append)
    service.apply(0, widget="calls", visible=False)
    before, saved = service.snapshot(), service.path.read_bytes()
    events.clear()

    def locked(*_):
        attempts.append(True)
        raise PermissionError("persistent lock")

    monkeypatch.setattr(layout.os, "replace", locked)
    monkeypatch.setattr(layout.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="persistent"):
        service.apply(1, widget="calls", visible=True)
    assert len(attempts) == 6 and events == []
    assert service.snapshot() == before and service.path.read_bytes() == saved
    assert not list(tmp_path.glob(".hud-layout-*.tmp"))


def test_widget_layout_persists_and_rejects_stale_edit(tmp_path):
    events = []
    path = tmp_path / "layout.json"
    service = WidgetLayout(path, notify=events.append)
    original = service.snapshot()
    changed = service.apply(0, widget="calls", visible=False, before="activity")
    assert changed["revision"] == 1 and changed["widgets"][1]["id"] == "calls"
    assert changed["widgets"][1]["visible"] is False
    assert WidgetLayout(path).snapshot() == changed
    with pytest.raises(LayoutConflict):
        service.apply(0, widget="calls", visible=True)
    assert service.snapshot() == changed and len(events) == 1
    assert original["widgets"][0]["id"] == "core"


def test_widget_concurrent_update_one_writer_wins(tmp_path):
    service = WidgetLayout(tmp_path / "layout.json")

    def update(widget):
        try:
            return service.apply(0, widget=widget, visible=False)["revision"]
        except LayoutConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(update, ["calls", "memory"]))
    assert sorted(map(str, results)) == ["1", "conflict"]


@pytest.mark.parametrize(
    "change",
    [
        {"widget": "javascript", "visible": True},
        {"widget": {}, "visible": True},
        {"widget": "calls", "visible": "yes"},
        {"view": "execute_code"},
        {"view": {}},
        {"visible": True},
        {"widget": "calls", "before": "calls"},
    ],
)
def test_widget_bad_fields_do_not_mutate_saved_state(tmp_path, change):
    service = WidgetLayout(tmp_path / "layout.json")
    with pytest.raises(ValueError):
        service.apply(0, **change)
    assert service.snapshot()["revision"] == 0 and not service.path.exists()


def test_widget_snapshot_is_detached_and_focus_mode_persists(tmp_path):
    service = WidgetLayout(tmp_path / "layout.json")
    snap = service.apply(0, view="conversation")
    snap["widgets"].clear()
    assert len(service.snapshot()["widgets"]) == 10
    assert WidgetLayout(service.path).snapshot()["view"] == "conversation"


def test_widget_updates_feed_reconnect_snapshot_without_shared_mutation(tmp_path):
    from app.hud.events import HudBus

    bus = HudBus()
    service = WidgetLayout(tmp_path / "layout.json", notify=bus.widget_layout_observed)
    result = service.apply(0, widget="calls", visible=False)
    snap = bus.snapshot()
    assert snap["widgetLayout"]["revision"] == 1
    result["widgets"].clear()
    assert len(bus.snapshot()["widgetLayout"]["widgets"]) == 10


def test_legacy_layout_migration_preserves_preferences_without_eager_write(tmp_path):
    import json

    from app.hud.layout import CATALOG

    path = tmp_path / "layout.json"
    ids = list(reversed([key for key in CATALOG if key != "core"]))
    legacy = {
        "schemaVersion": 1,
        "revision": 8,
        "view": "conversation",
        "widgets": [{"id": key, "visible": key != "calls"} for key in ids],
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    service = WidgetLayout(path)
    snap = service.snapshot()
    assert snap["schemaVersion"] == 3 and snap["revision"] == 8
    assert snap["view"] == "conversation"
    assert [w["id"] for w in snap["widgets"][1:]] == ids
    assert not next(w for w in snap["widgets"] if w["id"] == "calls")["visible"]
    assert json.loads(path.read_text()) == legacy
    saved = service.apply(8, view="command-center")
    assert WidgetLayout(path).snapshot() == saved


def test_move_resize_displaces_collisions_and_survives_restart(tmp_path):
    from app.hud.layout import overlaps

    service = WidgetLayout(tmp_path / "layout.json")
    placed = {"x": 3, "y": 0, "w": 6, "h": 12}
    snap = service.apply(0, widget="activity", placement=placed)
    activity = next(w for w in snap["widgets"] if w["id"] == "activity")
    assert all(activity[k] == v for k, v in placed.items())
    core = next(w for w in snap["widgets"] if w["id"] == "core")
    assert core["y"] == 12
    visible = [w for w in snap["widgets"] if w["visible"]]
    assert not any(overlaps(a, b) for i, a in enumerate(visible) for b in visible[i + 1 :])
    assert WidgetLayout(service.path).snapshot() == snap
    hidden = service.apply(1, widget="calls", visible=False)
    restored = service.apply(hidden["revision"], widget="calls", visible=True)
    assert next(w for w in restored["widgets"] if w["id"] == "calls")["visible"]


@pytest.mark.parametrize(
    "placement",
    [
        {"x": -1, "y": 0, "w": 3, "h": 4},
        {"x": 10, "y": 0, "w": 3, "h": 4},
        {"x": 0, "y": 201, "w": 3, "h": 4},
        {"x": 0, "y": 0, "w": True, "h": 4},
        {"x": 0, "y": 0, "w": 3, "h": float("nan")},
        {"x": 0, "y": 0, "w": 3, "h": 4, "code": "execute"},
        [],
    ],
)
def test_bad_geometry_never_changes_layout(tmp_path, placement):
    service = WidgetLayout(tmp_path / "layout.json")
    before = service.snapshot()
    with pytest.raises(ValueError):
        service.apply(0, widget="core", placement=placement)
    assert service.snapshot() == before and not service.path.exists()


def test_collision_overflow_is_rejected_without_partial_write(tmp_path):
    service = WidgetLayout(tmp_path / "layout.json")
    service.apply(0, widget="calls", placement={"x": 0, "y": 200, "w": 3, "h": 4})
    saved = service.path.read_bytes()
    with pytest.raises(ValueError):
        service.apply(1, widget="activity", placement={"x": 0, "y": 200, "w": 3, "h": 4})
    assert service.path.read_bytes() == saved
    assert service.snapshot()["revision"] == 1


@pytest.mark.parametrize("data", [[], {"schemaVersion": 99}])
def test_malformed_or_future_saved_layout_is_not_overwritten(tmp_path, data):
    import json

    path = tmp_path / "layout.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    saved = path.read_bytes()
    with pytest.raises(ValueError):
        WidgetLayout(path)
    assert path.read_bytes() == saved


def test_repeated_dock_reorder_preserves_saved_geometry(tmp_path):
    service = WidgetLayout(tmp_path / "layout.json")
    original = {
        w["id"]: {k: w[k] for k in ("x", "y", "w", "h")} for w in service.snapshot()["widgets"]
    }
    for i in range(120):
        service.apply(
            i,
            widget="memory" if i % 2 else "activity",
            before="activity" if i % 2 else "memory",
            reorder_only=True,
        )
    assert service.snapshot()["revision"] == 120
    assert {
        w["id"]: {k: w[k] for k in ("x", "y", "w", "h")} for w in service.snapshot()["widgets"]
    } == original


@pytest.mark.parametrize(
    "change",
    [
        {"widget": "memory", "reorder_only": True},
        {"widget": "memory", "before": "activity", "reorder_only": "yes"},
        {
            "widget": "memory",
            "before": "activity",
            "reorder_only": True,
            "placement": {"x": 0, "y": 0, "w": 3, "h": 4},
        },
    ],
)
def test_bad_order_only_edit_cannot_mutate(tmp_path, change):
    service = WidgetLayout(tmp_path / "layout.json")
    with pytest.raises(ValueError):
        service.apply(0, **change)
    assert service.snapshot()["revision"] == 0 and not service.path.exists()


@pytest.mark.parametrize("widget", ["core", "weather", "system", "awareness"])
def test_previously_fixed_widgets_can_move_and_resize(tmp_path, widget):
    service = WidgetLayout(tmp_path / "layout.json")
    placement = {"x": 4, "y": 35, "w": 5, "h": 8}
    service.apply(0, widget=widget, placement=placement)
    restored = WidgetLayout(service.path).snapshot()
    target = next(w for w in restored["widgets"] if w["id"] == widget)
    assert {k: target[k] for k in placement} == placement


def test_v2_migration_uses_corner_panels_and_preserves_visibility(tmp_path):
    import json

    from app.hud.layout import CATALOG, overlaps

    path = tmp_path / "layout.json"
    # A valid v2 scene, laid out vertically; v2 UI ignored these coordinates.
    old = {
        "schemaVersion": 2,
        "revision": 9,
        "view": "command-center",
        "widgets": [
            {"id": key, "visible": key != "calls", "x": 0, "y": i * 4, "w": 3, "h": 4}
            for i, key in enumerate(CATALOG)
        ],
    }
    path.write_text(json.dumps(old), encoding="utf-8")
    service = WidgetLayout(path)
    widgets = {w["id"]: w for w in service.snapshot()["widgets"]}
    assert widgets["system"]["x"] == 0 and widgets["system"]["y"] == 21
    assert widgets["awareness"]["x"] == 9 and widgets["awareness"]["y"] == 21
    assert widgets["calls"]["visible"] is False
    assert all(w["y"] + w["h"] <= 28 for w in widgets.values())
    assert not any(
        overlaps(a, b)
        for i, a in enumerate(widgets.values())
        for b in list(widgets.values())[i + 1 :]
    )
    assert json.loads(path.read_text()) == old
    service.apply(9, widget="system", placement={"x": 9, "y": 30, "w": 3, "h": 7})
    assert WidgetLayout(path).snapshot()["schemaVersion"] == 3
