"""Real disposable rename outcomes through isolated confirmation stores."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from actions.file_controller import file_controller, rename_file
from core.main_tool_bridge import _build_suggested_confirmation_question
from core.pending_confirmation import PendingConfirmationStore
from core.tool_result import ToolResult


@pytest.fixture
def workdir():
    # Keep native test files in this allowed project root, never a user's file.
    with tempfile.TemporaryDirectory(
        prefix="rename-approval-", dir=Path.cwd() / "runtime"
    ) as value:
        yield Path(value)


@pytest.fixture
def gate(monkeypatch):
    import agent.executor as executor
    import app.era_live.tool_dispatch as live

    store = PendingConfirmationStore()
    consent = {"approved": False}
    monkeypatch.setattr(executor, "_pending_store", store)
    monkeypatch.setattr(executor, "user_approved", lambda pending: consent["approved"])
    monkeypatch.setattr(executor, "publish_approved_result", lambda *args: None)
    monkeypatch.setattr(executor, "route_tool_call", lambda tool, params: (tool, params))
    monkeypatch.setattr(live, "route_tool_call", lambda tool, params, text: (tool, params))

    async def no_capture(*args):
        return None

    monkeypatch.setattr("core.vision_context.jit_vision_capture", no_capture)
    return executor, store, consent


@pytest.mark.parametrize("lane", ["live", "background"])
def test_destination_in_name_without_new_name_does_not_arm_an_approval(gate, workdir, lane):
    from app.era_live.tool_dispatch import ToolDispatchMixin

    executor, store, _ = gate
    source = workdir / "original.txt"
    source.write_text("original", encoding="utf-8")
    params = {"action": "rename", "path": str(source), "name": "renamed.txt"}
    if lane == "background":
        result = executor._call_tool_result("file_controller", params, None)
    else:
        era = ToolDispatchMixin()
        era._pending_store = store
        era._last_user_text = "rename this file"
        era.ui = SimpleNamespace(write_log=lambda text: None)
        result = asyncio.run(
            era._execute_tool_result_inner(SimpleNamespace(name="file_controller", args=params))
        )
    assert result.error_type == "InvalidArguments"
    assert "new_name" in result.message
    assert store.peek() is None
    assert source.read_text(encoding="utf-8") == "original"
    assert not (workdir / "renamed.txt").exists()


def test_direct_handler_names_missing_destination_before_source_lookup(workdir):
    source = workdir / "original.txt"
    source.write_text("original", encoding="utf-8")
    result = file_controller({"action": "rename", "path": str(source), "name": "renamed.txt"})
    assert "new_name" in result
    assert source.exists() and not (workdir / "renamed.txt").exists()


@pytest.mark.parametrize("bad_name", ["../escaped.txt", r"..\escaped.txt", "nested/new.txt"])
def test_rename_cannot_act_as_an_unreviewed_move(workdir, bad_name):
    folder = workdir / "nested"
    folder.mkdir()
    source = folder / "original.txt"
    source.write_text("original", encoding="utf-8")
    rename_file(str(source), new_name=bad_name)
    assert source.read_text(encoding="utf-8") == "original"
    assert not (workdir / "escaped.txt").exists()


@pytest.mark.parametrize("source_form", ["full_path", "parent_and_name"])
def test_approved_rename_runs_once_and_verifies_actual_files(gate, workdir, source_form):
    executor, store, consent = gate
    source = workdir / "original.txt"
    source.write_text("test contents", encoding="utf-8")
    params = {"action": "rename", "new_name": "renamed.txt", "path": str(source)}
    if source_form == "parent_and_name":
        params.update(path=str(workdir), name=source.name)
    pending = executor._call_tool_result("file_controller", params, None)
    assert pending.error_type == "ConfirmationRequired"
    assert source.exists() and not (workdir / "renamed.txt").exists()
    consent["approved"] = True  # isolated owner event; never the live Era store
    result = executor._call_tool_result("confirm_pending_confirmation", {}, None)
    assert result.success and result.verified
    assert not source.exists()
    assert (workdir / "renamed.txt").read_text(encoding="utf-8") == "test contents"
    assert store.peek() is None
    replay = executor._call_tool_result(
        "file_controller",
        {**params, "confirmed": True, "confirmation_id": pending.confirmation_id},
        None,
    )
    assert not replay.success
    assert (workdir / "renamed.txt").read_text(encoding="utf-8") == "test contents"


def test_cancelled_rename_clears_hud_and_cannot_execute(gate, workdir, monkeypatch):
    from app.hud.events import HudBus

    executor, store, _ = gate
    monkeypatch.setattr("core.pending_confirmation.get_shared_store", lambda: store)
    bus = HudBus()
    source = workdir / "original.txt"
    source.write_text("test contents", encoding="utf-8")
    params = {"action": "rename", "path": str(source), "new_name": "renamed.txt"}
    pending = executor._call_tool_result("file_controller", params, None)
    call_id = bus.tool_started(None, "file_controller", params)
    bus.tool_finished(call_id, "file_controller", params, pending)
    assert bus.snapshot()["approval"]["id"] == pending.confirmation_id
    result = executor._call_tool_result("cancel_pending_confirmation", {}, None)
    assert result.success and store.peek() is None
    assert bus.snapshot()["approval"] is None
    assert bus.state()["state"] != "awaiting_approval"
    retry = executor._call_tool_result("confirm_pending_confirmation", {}, None)
    assert not retry.success
    assert source.read_text(encoding="utf-8") == "test contents"
    assert not (workdir / "renamed.txt").exists()


def test_unrequested_or_changed_rename_cannot_use_an_armed_approval(gate, workdir):
    executor, store, consent = gate
    source = workdir / "original.txt"
    source.write_text("original", encoding="utf-8")
    params = {"action": "rename", "path": str(source), "new_name": "reviewed.txt"}
    pending = executor._call_tool_result("file_controller", params, None)
    refused = executor._call_tool_result("confirm_pending_confirmation", {}, None)
    assert not refused.success and store.peek() is not None
    consent["approved"] = True
    changed = executor._call_tool_result(
        "file_controller",
        {
            **params,
            "new_name": "different.txt",
            "confirmed": True,
            "confirmation_id": pending.confirmation_id,
        },
        None,
    )
    assert not changed.success
    assert source.read_text(encoding="utf-8") == "original"
    assert not (workdir / "reviewed.txt").exists()
    assert not (workdir / "different.txt").exists()


@pytest.mark.parametrize("action", ["rename", " ReNaMe "])
@pytest.mark.parametrize("split_source", [False, True])
def test_confirmation_question_and_hud_show_both_rename_names(action, split_source):
    from app.hud.tool_adapter import describe

    params = {"action": action, "path": r"C:\work\original.txt", "new_name": "renamed.txt"}
    if split_source:
        params.update(path=r"C:\work", name="original.txt")
    question = _build_suggested_confirmation_question(
        ToolResult.fail(
            "Needs approval",
            error_type="ConfirmationRequired",
            tool_name="file_controller",
            data=params,
        )
    )
    activity = describe("file_controller", params)["activity"]
    assert "original.txt" in question and "renamed.txt" in question
    assert "original.txt" in activity and "renamed.txt" in activity


@pytest.mark.parametrize(
    "name",
    [
        None,
        5,
        "",
        " ",
        ".",
        "..",
        "CON",
        "aux.txt",
        "COM1.txt",
        "LPT²",
        "new.",
        "new ",
        "file:stream",
        'quote".txt',
        "line\nbreak.txt",
        r"C:\work\different.txt",
    ],
)
def test_bad_destination_arguments_never_arm_confirmation(gate, workdir, name):
    executor, store, _ = gate
    source = workdir / "original.txt"
    source.write_text("original", encoding="utf-8")
    result = executor._call_tool_result(
        "file_controller", {"action": "rename", "path": str(source), "new_name": name}, None
    )
    assert result.error_type == "InvalidArguments" and result.data["executed"] is False
    assert store.peek() is None and source.read_text(encoding="utf-8") == "original"


def test_invalid_rename_cannot_consume_an_already_reviewed_action(gate, workdir):
    executor, store, consent = gate
    source = workdir / "original.txt"
    source.write_text("original", encoding="utf-8")
    params = {"action": "rename", "path": str(source), "new_name": "reviewed.txt"}
    armed = executor._call_tool_result("file_controller", params, None)
    consent["approved"] = True
    result = executor._call_tool_result(
        "file_controller",
        {**params, "new_name": "", "confirmed": True, "confirmation_id": armed.confirmation_id},
        None,
    )
    assert result.error_type == "InvalidArguments"
    assert store.peek().confirmation_id == armed.confirmation_id
    assert store.peek().parameters == params
    assert source.read_text(encoding="utf-8") == "original"


def test_existing_destination_is_preserved_after_valid_approval(gate, workdir):
    executor, _, consent = gate
    source, destination = workdir / "original.txt", workdir / "renamed.txt"
    source.write_text("original", encoding="utf-8")
    destination.write_text("keep this", encoding="utf-8")
    result = executor._call_tool_result(
        "file_controller",
        {"action": "rename", "path": str(source), "new_name": destination.name},
        None,
    )
    assert result.error_type == "ConfirmationRequired"
    consent["approved"] = True
    result = executor._call_tool_result("confirm_pending_confirmation", {}, None)
    assert not result.success
    assert source.read_text(encoding="utf-8") == "original"
    assert destination.read_text(encoding="utf-8") == "keep this"


def test_unicode_names_are_kept_exactly(gate, workdir):
    executor, _, consent = gate
    source, destination = workdir / "notes.txt", workdir / "ملاحظات été.txt"
    source.write_text("unicode", encoding="utf-8")
    result = executor._call_tool_result(
        "file_controller",
        {"action": "rename", "path": str(source), "new_name": destination.name},
        None,
    )
    assert result.error_type == "ConfirmationRequired"
    consent["approved"] = True
    result = executor._call_tool_result("confirm_pending_confirmation", {}, None)
    assert result.success and destination.read_text(encoding="utf-8") == "unicode"
