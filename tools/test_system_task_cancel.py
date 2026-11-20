"""Cancellation must stop vendor automation before its next external action."""

from unittest.mock import Mock

import pytest

from actions import system_performance as sp
from core.cancel_registry import CancelledTask, CancelRegistry, bind


@pytest.fixture
def token():
    return CancelRegistry().register("system-fixture", "system_performance")


@pytest.mark.parametrize("action", ["pc_boost", "msi_scenario", "performance_status"])
def test_cancelled_system_entry_never_calls_vendor_action(monkeypatch, token, action):
    token.event.set()
    actions = [Mock(), Mock(), Mock()]
    for name, mock in zip(
        ("pc_boost", "msi_set_scenario", "msi_scenario_status"), actions, strict=True
    ):
        monkeypatch.setattr(sp, name, mock)
    with bind(token), pytest.raises(CancelledTask):
        sp.system_performance({"action": action, "value": "balanced"})
    for mock in actions:
        mock.assert_not_called()


def test_cancel_during_focus_prevents_physical_click_and_restores_cursor(monkeypatch, token):
    target = Mock()
    target.rectangle.return_value.width.return_value = 10
    target.rectangle.return_value.height.return_value = 10
    target.set_focus.side_effect = token.event.set
    monkeypatch.setattr(sp, "_cursor_pos", lambda: (7, 9))
    restore = Mock()
    monkeypatch.setattr(sp, "_restore_cursor", restore)
    with bind(token), pytest.raises(CancelledTask):
        sp._click_for_real(target)
    target.click_input.assert_not_called()
    restore.assert_called_once_with((7, 9))


def test_cancel_verification_prevents_another_registry_read(monkeypatch, token):
    reads = Mock(return_value=(2, 0))
    monkeypatch.setattr(sp, "_scenario_state", reads)
    monkeypatch.setattr(sp, "sleep_cancellable", lambda seconds: token.event.set())
    with bind(token), pytest.raises(CancelledTask):
        sp._await_scenario("super_battery", (2, 0))
    reads.assert_called_once()


def test_cancel_reacquisition_is_not_treated_as_missing_control(monkeypatch, token):
    window = Mock()
    monkeypatch.setattr(
        sp, "_wake_window", lambda win: token.event.set() or sp.raise_if_cancelled()
    )
    with bind(token), pytest.raises(CancelledTask):
        sp._reacquire_control(window, "balanced")
    window.descendants.assert_not_called()


@pytest.mark.parametrize("cancel_on_scan", [True, False])
def test_pc_boost_discovery_cancellation_or_success(monkeypatch, token, cancel_on_scan):
    window, button = Mock(), Mock()
    button.window_text.return_value = "Boost"

    def discover(**kwargs):
        if cancel_on_scan:
            token.event.set()
        return [button]

    window.descendants.side_effect = discover
    monkeypatch.setattr(sp, "is_windows", lambda: True)
    monkeypatch.setattr(sp, "_co_init", lambda: None)
    monkeypatch.setattr(sp, "_find_windows", lambda *args, **kwargs: [window])
    monkeypatch.setattr(sp, "_wake_webview_a11y", lambda hwnd: None)
    with bind(token):
        if cancel_on_scan:
            with pytest.raises(CancelledTask):
                sp.pc_boost()
            button.invoke.assert_not_called()
            button.click_input.assert_not_called()
        else:
            assert "PC Boost triggered" in sp.pc_boost()
            button.invoke.assert_called_once()
            button.click_input.assert_not_called()


def test_cancel_after_failed_invoke_prevents_fallback_click(monkeypatch, token):
    window, button = Mock(), Mock()
    button.window_text.return_value = "Boost"
    window.descendants.return_value = [button]

    def failed_invoke():
        token.event.set()
        raise RuntimeError("UIA failed during cancellation")

    button.invoke.side_effect = failed_invoke
    monkeypatch.setattr(sp, "is_windows", lambda: True)
    monkeypatch.setattr(sp, "_co_init", lambda: None)
    monkeypatch.setattr(sp, "_find_windows", lambda *args, **kwargs: [window])
    monkeypatch.setattr(sp, "_wake_webview_a11y", lambda hwnd: None)
    with bind(token), pytest.raises(CancelledTask):
        sp.pc_boost()
    button.click_input.assert_not_called()
