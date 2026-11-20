"""Guards for the 2026-11-17 live tool sweep (PF-041).

1. An unnamed reminder takes its message as its name ("remind me in 3 minutes to
   stretch" failed with "Give the task a name").
2. A typed new question while a confirmation is pending is not a cancel: it reaches
   the model and gets answered. A typed refusal word still cancels (PF-011).
3. system_snapshot carries live CPU/RAM numbers ("how is my PC doing?" got none).
"""

import types

import pytest

from app.era_live.tool_dispatch import _typed_cancel_reply
from core import task_scheduler as ts


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_SCHEDULED_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("ERA_HIVE_DIR", str(tmp_path / "hive"))
    monkeypatch.setattr(ts, "_scheduler", None)
    monkeypatch.setattr(ts, "_executor", lambda *a: {"success": True, "message": "ok"})
    monkeypatch.setattr(ts, "_reporter", lambda text, urgent: None)
    return types.SimpleNamespace()


def test_unnamed_reminder_is_named_by_its_message(isolated):
    task = ts.create_task("", "in 3 minutes", kind="reminder", message="time to stretch your legs")
    assert task["name"] == "time to stretch your legs"


def test_two_identical_unnamed_reminders_both_get_created(isolated):
    a = ts.create_task("", "in 3 minutes", kind="reminder", message="drink water")
    b = ts.create_task("", "in 5 minutes", kind="reminder", message="drink water")
    assert a["name"] != b["name"]


def test_unnamed_tool_task_still_needs_a_name(isolated):
    with pytest.raises(ts.TaskError):
        ts.create_task("", "in 3 minutes", tool="weather_report", args={})


def test_explicit_duplicate_name_is_still_refused(isolated):
    ts.create_task("stretch", "in 3 minutes", kind="reminder", message="x")
    with pytest.raises(ts.TaskError):
        ts.create_task("stretch", "in 5 minutes", kind="reminder", message="y")


@pytest.mark.parametrize(
    "said",
    ["What scheduled tasks do I have running?", "what's the weather?", "how is my PC doing?"],
)
def test_a_new_question_is_not_a_typed_cancel(said):
    assert not _typed_cancel_reply(said.lower())


@pytest.mark.parametrize("said", ["no", "cancel", "no, don't", "stop", "annule", "not now"])
def test_a_refusal_word_still_cancels(said):
    assert _typed_cancel_reply(said)


def test_system_snapshot_reports_cpu_and_ram():
    pytest.importorskip("psutil")
    from actions.computer_control import _system_load_line

    line = _system_load_line()
    assert line.startswith("Load: CPU ")
    assert "RAM " in line and "GB" in line


@pytest.mark.parametrize(
    "tool,action",
    [
        ("screen_process", "screen"),
        ("screen_process", "camera"),
        ("computer_control", "system_snapshot"),
        ("computer_control", "running_apps"),
        ("computer_settings", "get_volume"),
    ],
)
def test_reads_are_not_spoken_as_unconfirmed(tool, action):
    from core.completion_contract import is_read_only

    assert is_read_only(tool, action)


def test_side_effects_still_need_evidence():
    from core.completion_contract import is_read_only

    assert not is_read_only("computer_control", "type")
    assert not is_read_only("computer_settings", "set_volume")


@pytest.mark.parametrize(
    "tool",
    [
        "file_controller",
        "file_processor",
        "desktop_control",
        "manage_schedule",
        "manage_offer",
        "manage_preference",
        "game_updater",
        "self_heal",
        "self_improve",
    ],
)
def test_list_on_a_named_tool_is_never_a_window_listing(tool):
    from core.action_registry import canonicalize_tool_call

    assert canonicalize_tool_call(tool, {"action": "list", "path": "desktop"})[0] == tool


def test_window_list_still_routes_for_window_tools():
    from core.action_registry import canonicalize_tool_call

    assert canonicalize_tool_call("computer_control", {"action": "list"})[0] == "window_control"
    assert canonicalize_tool_call("window_control", {"action": "list"})[0] == "window_control"


def test_a_listed_result_is_spoken_as_a_list_not_cut_at_item_one():
    from app.era_live.speak_once import spoken_outcome

    result = types.SimpleNamespace(
        success=True,
        message=(
            "5 message(s) in the inbox matching 'is:unread':\n"
            "1. GitLab: Tomorrow: rebuilding the Git backend\n   id: 1a1e3a\n"
            "2. Team: Black Friday came early\n   id: 1a1e36\n"
            "3. Julian: final hour\n   id: 1a1e32\n4. A\n5. B"
        ),
        data={},
    )
    said = spoken_outcome("gmail_control", result)
    assert "GitLab" in said and "Black Friday" in said
    assert "id:" not in said and not said.rstrip(" .,sir—").endswith("1")


def test_flight_url_carries_the_asked_route_not_a_stale_blob():
    import inspect

    from actions.flight_finder import _build_google_flights_url

    sig = inspect.signature(_build_google_flights_url)
    kw = {"origin": "Tunis", "destination": "Paris", "date": "2026-11-20"}
    kw = {k: v for k, v in kw.items() if k in sig.parameters}
    url = _build_google_flights_url(**kw)
    assert "Tunis" in url and "Paris" in url and "2026-11-20" in url
    assert "tfs=" not in url


def test_mic_status_without_a_device_reads_the_default_microphone(monkeypatch):
    from actions import audio_control as ac

    asked = {}

    def fake_status(device, flow=None):
        asked["device"], asked["flow"] = device, flow
        return types.SimpleNamespace(message="Casque (input) is at 100%, sir.")

    monkeypatch.setattr(ac.ae, "default_ids", lambda flow: {"console": "{mic-id}"})
    monkeypatch.setattr(ac.ae, "device_status", fake_status)
    out = ac.audio_control({"action": "device_status", "flow": "input"})
    assert "input" in out and asked == {"device": "{mic-id}", "flow": "input"}
