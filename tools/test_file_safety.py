import re
from pathlib import Path

import pytest


@pytest.fixture
def file_creation_waiver(monkeypatch):
    """Drive the file-creation waiver instead of reading the real user's prefs.

    These tests used to call classify_action() bare, so they read the LIVE
    memory/user_prefs.json. On a machine where the user had actually said
    "create files without asking", the standing waiver correctly downgrades
    create_folder to LOW and the assertions failed — reporting a healthy,
    working preference as a safety-gate defect. The risk level here is a
    FUNCTION of user preference, so a test that pins one value must set the
    preference it is pinning.
    """

    def _set(waived: bool):
        import core.safety_guard as sg

        monkeypatch.setattr(sg, "_user_waived_file_creation_confirmation", lambda: waived)

    return _set


class TestSafetyGuardCreateFolder:
    """create_folder is confirm-by-default, and the user can waive that.

    Both directions are asserted: the default protects a user who never opted
    out, and the waiver path proves the opt-out is actually honoured. Testing
    only the default would let a regression that ignores the waiver pass.
    """

    def test_create_folder_is_medium_risk_by_default(self, file_creation_waiver):
        from core.safety_guard import classify_action

        file_creation_waiver(False)
        result = classify_action("file_controller", {"action": "create_folder"})
        assert result["risk_level"] == "MEDIUM"
        assert result["requires_confirmation"] is True

    def test_create_folder_requires_confirmation_by_default(self, file_creation_waiver):
        from core.safety_guard import classify_action

        file_creation_waiver(False)
        result = classify_action("file_controller", {"action": "create_folder"})
        assert result["requires_confirmation"] is True

    @pytest.mark.parametrize("action", ["write", "create_file", "create_folder"])
    def test_waiver_downgrades_every_creation_action(self, file_creation_waiver, action):
        from core.safety_guard import classify_action

        file_creation_waiver(True)
        result = classify_action("file_controller", {"action": action})
        assert result["risk_level"] == "LOW"
        assert result["requires_confirmation"] is False

    @pytest.mark.parametrize("action", ["delete", "move"])
    def test_waiver_does_not_leak_into_destructive_actions(self, file_creation_waiver, action):
        """The creation waiver covers creation only — delete/move keep their gates."""
        from core.safety_guard import classify_action

        file_creation_waiver(True)
        result = classify_action("file_controller", {"action": action, "path": "C:/tmp/x.txt"})
        assert result["requires_confirmation"] is True


class TestCloseConfirmationWaiver:
    """The same adjustable-confirmation mechanism, per app (e.g. Spotify)."""

    def test_waived_app_skips_confirmation(self, monkeypatch):
        import core.safety_guard as sg
        from core.safety_guard import classify_action

        monkeypatch.setattr(sg, "_user_waived_close_confirmation", lambda params: True)
        result = classify_action("computer_control", {"action": "close_app", "app": "spotify"})
        assert result["requires_confirmation"] is False

    def test_unwaived_app_still_confirms(self, monkeypatch):
        import core.safety_guard as sg
        from core.safety_guard import classify_action

        monkeypatch.setattr(sg, "_user_waived_close_confirmation", lambda params: False)
        result = classify_action("computer_control", {"action": "close_app", "app": "spotify"})
        assert result["requires_confirmation"] is True

    @pytest.mark.parametrize("action", ["kill", "kill_process"])
    def test_force_kill_can_never_be_waived(self, monkeypatch, action):
        """Waivers cover graceful close only — force-kill keeps its gate always.

        Without this, a waiver granted by spoken feedback could widen into
        terminating arbitrary processes without asking.
        """
        import core.safety_guard as sg
        from core.safety_guard import classify_action

        monkeypatch.setattr(sg, "_user_waived_close_confirmation", lambda params: True)
        result = classify_action("computer_control", {"action": action, "app": "spotify"})
        assert result["requires_confirmation"] is True
        assert result["risk_level"] == "HIGH"


class TestOverwriteParamExists:
    def test_overwrite_param_in_file_controller_schema(self):
        import main

        declarations = [d for d in main.TOOL_DECLARATIONS if d["name"] == "file_controller"]
        assert len(declarations) == 1
        props = declarations[0]["parameters"]["properties"]
        assert "overwrite" in props
        assert props["overwrite"]["type"] == "BOOLEAN"


class TestToolSchemaActions:
    def test_last_download_in_declared_actions(self):
        import main

        declarations = [d for d in main.TOOL_DECLARATIONS if d["name"] == "file_controller"]
        assert len(declarations) == 1
        action_desc = declarations[0]["parameters"]["properties"]["action"]["description"]
        assert "last_download" in action_desc

    def test_open_app_description_excludes_files(self):
        import main

        declarations = [d for d in main.TOOL_DECLARATIONS if d["name"] == "open_app"]
        assert len(declarations) == 1
        desc = declarations[0]["description"]
        assert "file_controller" in desc
        assert "application" in desc.lower() or "APPLICATION" in desc

    def test_open_in_declared_actions(self):
        import main

        declarations = [d for d in main.TOOL_DECLARATIONS if d["name"] == "file_controller"]
        assert len(declarations) == 1
        action_desc = declarations[0]["parameters"]["properties"]["action"]["description"]
        assert "open" in action_desc


class TestCodeHelperBackup:
    """Verify _edit_action has backup logic (integration test for backup code presence)."""

    def test_edit_action_contains_backup_code(self):
        import inspect

        from actions.code_helper import _edit_action

        source = inspect.getsource(_edit_action)
        assert "backup" in source.lower()
        assert ".bak-" in source
        assert "Backup failed" in source


class TestCodeHelperVerbatimWrite:
    """2026-09-28: write with `code` but no `description` answered "please
    describe..." (read as success) and never created the file — 7 failed opens."""

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv("ERA_ALLOW_GENERATED_CODE", "1")

    def test_content_is_saved_verbatim_and_verified(self, tmp_path):
        from actions.code_helper import code_helper

        target = tmp_path / "note.txt"
        out = code_helper({"action": "write", "code": "hello\nworld", "output_path": str(target)})
        assert out.startswith("Saved to:")
        assert target.read_text(encoding="utf-8") == "hello\nworld"

    def test_missing_description_and_content_reads_as_failure(self):
        from actions.code_helper import code_helper
        from core.tool_result import looks_like_failure

        assert looks_like_failure(code_helper({"action": "write"}))

    def test_space_typed_for_a_separator_lands_in_the_folder(self, tmp_path):
        from actions.code_helper import _repair_glued_path

        (tmp_path / "Desktop").mkdir()
        glued = tmp_path / "Desktop note.txt"
        assert _repair_glued_path(glued) == tmp_path / "Desktop" / "note.txt"
        # No sibling folder named after the first word: the name really has a space.
        assert _repair_glued_path(tmp_path / "my note.txt") == tmp_path / "my note.txt"
