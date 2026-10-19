"""App opening must not uninstall, launch the wrong browser, or invent success."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from actions import open_app as launcher
from core.app_launch_safety import safe_executable, shortcut_score, unsafe_launch
from core.cancel_registry import CancelledTask, CancelRegistry, bind
from core.tool_result import ToolResult


@pytest.mark.parametrize(
    "name",
    [
        "Désinstaller Telegram",
        "Uninstall Brave",
        "unins000.exe",
        "setup.exe",
        "msiexec.exe",
        "Telegram.exe /uninstall",
    ],
)
def test_maintenance_names_are_rejected(name):
    assert unsafe_launch(name)


def test_shortcut_ranking_prefers_exact_app_over_other_product():
    assert shortcut_score("Telegram", "telegram") > shortcut_score("Telegram Beta", "telegram")
    assert shortcut_score("Désinstaller Telegram", "telegram") == 0
    assert shortcut_score("Edge WebView", "edge") < shortcut_score("Edge", "edge")
    assert shortcut_score("Knowledge", "edge") == 0


def test_safe_executable_rejects_shell_and_uninstall_arguments(tmp_path):
    executable = tmp_path / "Telegram.exe"
    executable.touch()
    assert safe_executable(str(executable))
    assert not safe_executable(str(executable), "/uninstall /silent")
    shell = tmp_path / "powershell.exe"
    shell.touch()
    assert not safe_executable(str(shell))


def test_shortcut_selection_skips_first_uninstaller(monkeypatch, tmp_path):
    paths = [str(tmp_path / "Désinstaller Telegram.lnk"), str(tmp_path / "Telegram.lnk")]
    monkeypatch.setattr(launcher, "_shortcut_inventory", lambda *a: tuple(paths))
    inspect = Mock(
        side_effect=lambda path: (
            (str(tmp_path / "Telegram.exe"), "")
            if path == paths[1]
            else pytest.fail("uninstall link must not be selected")
        )
    )
    monkeypatch.setattr(launcher, "inspect_shortcut", inspect)
    assert launcher.scan_start_menu_shortcuts("telegram") == paths[1]


def test_exact_name_link_with_unsafe_target_is_skipped(monkeypatch, tmp_path):
    paths = [str(tmp_path / "Telegram.lnk"), str(tmp_path / "Telegram Desktop.lnk")]
    monkeypatch.setattr(launcher, "_shortcut_inventory", lambda *a: tuple(paths))
    monkeypatch.setattr(
        launcher,
        "inspect_shortcut",
        lambda path: None if path == paths[0] else (str(tmp_path / "Telegram.exe"), ""),
    )
    assert launcher.scan_start_menu_shortcuts("telegram") == paths[1]


def test_ambiguous_shortcuts_do_not_choose_arbitrary_binary(monkeypatch, tmp_path):
    paths = [str(tmp_path / "one" / "Telegram.lnk"), str(tmp_path / "two" / "Telegram.lnk")]
    monkeypatch.setattr(launcher, "_shortcut_inventory", lambda *a: tuple(paths))
    monkeypatch.setattr(
        launcher, "inspect_shortcut", lambda path: (path.replace(".lnk", ".exe"), "")
    )
    assert launcher.scan_start_menu_shortcuts("telegram") is None


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(launcher, "remember_action", lambda **kw: None)
    monkeypatch.setattr(launcher, "get_executable_from_registry", lambda name: None)
    monkeypatch.setattr(launcher, "scan_start_menu_shortcuts", lambda name: None)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    monkeypatch.setattr(launcher, "_find_launch_window", lambda *a: 0)
    monkeypatch.setattr(launcher, "_store_app_id", lambda name: None)
    spawn = Mock()
    start = Mock()
    browser = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher.os, "startfile", start)
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    return spawn, start, browser


def test_brave_registry_launch_uses_brave_not_default_browser(monkeypatch, tmp_path, isolated):
    executable = tmp_path / "brave.exe"
    executable.touch()
    monkeypatch.setattr(launcher, "get_executable_from_registry", lambda name: str(executable))
    monkeypatch.setattr(launcher, "_launch_result", lambda name, path: "Opened brave")
    assert launcher.open_app({"app_name": "Brave"}) == "Opened brave"
    isolated[0].assert_called_once_with(
        [str(executable)], shell=False, creationflags=launcher.NO_WINDOW
    )
    isolated[1].assert_not_called()
    isolated[2].assert_not_called()


def test_launch_failure_does_not_press_enter_in_start_menu(isolated, monkeypatch):
    keys = Mock()
    monkeypatch.setattr(launcher, "keyboard", keys)
    assert "Could not find a safe" in launcher.open_app({"app_name": "not installed"})
    keys.send.assert_not_called()
    for action in isolated:
        action.assert_not_called()


def test_cancelled_open_never_spawns(isolated):
    token = CancelRegistry().register("launch-fixture", "open_app")
    token.event.set()
    with bind(token), pytest.raises(CancelledTask):
        launcher.open_app({"app_name": "brave"})
    for action in isolated:
        action.assert_not_called()


def test_close_request_is_handled_before_launch(monkeypatch, isolated):
    handles = iter([123, 0])
    monkeypatch.setattr(launcher, "_find_launch_window", lambda *a: next(handles))
    close = Mock(return_value=True)
    monkeypatch.setattr(launcher, "_try_close_window_handles", close)
    monkeypatch.setattr(launcher, "sleep_cancellable", lambda seconds: True)
    assert "Closed" in launcher.open_app({"app_name": "brave", "mode": "close"})
    close.assert_called_once_with([123])
    for action in isolated:
        action.assert_not_called()


def test_delayed_window_is_not_reported_as_open(monkeypatch):
    monkeypatch.setattr(launcher, "_find_launch_window", lambda *a: 0)
    assert "has not been confirmed" in launcher._launch_result("brave", timeout=0)


def test_unobserved_launch_is_not_verified(monkeypatch):
    from core.action_verifiers import verify_action_result

    monkeypatch.setattr(launcher, "_find_launch_window", lambda *a: 0)
    result = verify_action_result("open_app", ToolResult.ok("Opened brave"), {"app_name": "brave"})
    assert not result.verified and result.verification_status == "NOT_VERIFIED"


def test_owned_window_verifies_launch(monkeypatch):
    from core.action_verifiers import verify_action_result

    monkeypatch.setattr(launcher, "_find_launch_window", lambda *a: 123)
    result = verify_action_result("open_app", ToolResult.ok("Opened brave"), {"app_name": "brave"})
    assert result.verified and result.verification_status == "VERIFIED"


def test_store_catalog_matches_only_registered_package_id(monkeypatch):
    monkeypatch.setattr(
        launcher,
        "_store_catalog",
        lambda epoch: (
            {"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_abc!App"},
            {"Name": "Calculator", "AppID": "cmd.exe /c uninstall"},
        ),
    )
    assert launcher._store_app_id("calculator") == "Microsoft.WindowsCalculator_abc!App"


def test_store_launch_uses_registered_shell_identity(monkeypatch, isolated):
    monkeypatch.setattr(
        launcher, "_store_app_id", lambda name: "Microsoft.WindowsCalculator_abc!App"
    )
    monkeypatch.setattr(launcher, "_launch_result", lambda name: "Launch sent")
    launcher.open_app({"app_name": "calculator"})
    isolated[1].assert_called_once_with("shell:AppsFolder\\Microsoft.WindowsCalculator_abc!App")
    isolated[0].assert_not_called()


def test_already_running_app_avoids_stale_shortcut_and_relaunch(monkeypatch, isolated):
    monkeypatch.setattr(launcher, "_find_launch_window", lambda *a: 123)
    monkeypatch.setattr(launcher, "_focus_launch_window", lambda hwnd: True)
    resolve = Mock(side_effect=AssertionError("must focus before resolving stale installation"))
    monkeypatch.setattr(launcher, "get_executable_from_registry", resolve)
    assert "already open and is in front" in launcher.open_app({"app_name": "Telegram Desktop"})
    for action in isolated:
        action.assert_not_called()


@pytest.mark.parametrize(
    "process,title,expected",
    [
        ("brave.exe", "Telegram Web", 0),
        ("unins000.exe", "Telegram Desktop", 0),
        ("Telegram.exe", "Conversation with Jane", 123),
    ],
)
def test_window_readback_uses_app_process_not_caption(monkeypatch, process, title, expected):
    import psutil
    import win32process

    gui = SimpleNamespace(
        EnumWindows=lambda callback, data: callback(123, data),
        IsWindowVisible=lambda hwnd: True,
        GetWindowText=lambda hwnd: title,
    )
    monkeypatch.setattr(launcher, "win32gui", gui)
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda hwnd: (1, 2))
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(name=lambda: process))
    assert launcher._find_launch_window("telegram") == expected


def test_inspection_rejects_safe_named_shortcut_to_uninstaller(monkeypatch, tmp_path):
    import pythoncom
    import win32com.client

    from core.app_launch_safety import inspect_shortcut

    uninstaller = tmp_path / "unins000.exe"
    uninstaller.touch()
    link = SimpleNamespace(TargetPath=str(uninstaller), Arguments="")
    monkeypatch.setattr(pythoncom, "CoInitialize", lambda: None)
    monkeypatch.setattr(pythoncom, "CoUninitialize", lambda: None)
    monkeypatch.setattr(
        win32com.client, "Dispatch", lambda name: SimpleNamespace(CreateShortcut=lambda path: link)
    )
    assert inspect_shortcut(str(tmp_path / "Telegram.lnk")) is None
