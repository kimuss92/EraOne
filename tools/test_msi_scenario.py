"""MSI Center scenario switching — the "it never actually applied" regression.

The bug: UIA's SelectionItem.Select() flips the radio's checkmark without
running MSI Center's apply handler, so `is_selected()` returned True for a
switch that never happened and Era reported success while the machine stayed
on the old scenario. Only a synthesized physical click applies it, and only
MSI's own registry state proves it did.

These tests pin the two things that made the bug possible: that we never fall
back to select(), and that success is claimed only against MSI's state.
"""

import pytest

import actions.system_performance as sp


class _FakeRadio:
    """A radio that lies exactly the way MSI Center's does: select() flips the
    checkmark and changes nothing; only a click reaches the app."""

    def __init__(self, label="Balanced", applies=True, on_apply=None):
        self.label = label
        self._selected = False
        self.applies = applies
        self.on_apply = on_apply
        self.selects = 0
        self.clicks = 0
        self.focus_calls = 0

    # -- pywinauto surface ------------------------------------------------
    def window_text(self):
        return self.label

    def is_selected(self):
        return self._selected

    def select(self):
        self.selects += 1
        self._selected = True  # the lie

    def set_focus(self):
        self.focus_calls += 1

    def click_input(self):
        self.clicks += 1
        self._selected = True
        if self.applies and self.on_apply:
            self.on_apply()

    def rectangle(self):
        class _R:
            def width(self):
                return 380

            def height(self):
                return 190

        return _R()


class _FakeWindow:
    def __init__(self):
        self.minimized = 0

    def minimize(self):
        self.minimized += 1

    def is_minimized(self):
        return False

    def descendants(self, **kwargs):
        return []


@pytest.fixture
def wired(monkeypatch):
    """Drive msi_set_scenario against a fake MSI Center + fake registry."""
    state = {"value": (2, 0)}  # Balanced, AI Engine off
    radios: dict = {}
    window = _FakeWindow()

    monkeypatch.setattr(sp, "is_windows", lambda: True)
    monkeypatch.setattr(sp, "_co_init", lambda: None)
    # These tests are about the UI path. Pin the direct path closed so they
    # never write the real HKLM key or stop MSI's service on a dev machine
    # that happens to be elevated.
    monkeypatch.setattr(sp, "_scenario_key_writable", lambda: False)
    monkeypatch.setattr(sp, "_find_windows", lambda *a, **k: [window])
    monkeypatch.setattr(sp, "_wake_window", lambda win: None)
    monkeypatch.setattr(sp, "_cursor_pos", lambda: (7, 9))
    monkeypatch.setattr(sp, "_restore_cursor", lambda pos: None)
    monkeypatch.setattr(sp, "_scenario_state", lambda: state["value"])
    monkeypatch.setattr(sp, "_scenario_inventory", lambda buttons: dict(radios))
    monkeypatch.setattr(sp, "_SCENARIO_SCAN_TIMEOUT", 0.2)
    monkeypatch.setattr(sp, "_SCENARIO_VERIFY_TIMEOUT", 0.2)
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)

    def add(canonical, label, applies=True, becomes=None):
        radio = _FakeRadio(
            label,
            applies=applies,
            on_apply=(lambda: state.update(value=becomes)) if becomes else None,
        )
        radios[canonical] = radio
        return radio

    return type("Wired", (), {"add": staticmethod(add), "state": state, "window": window})


# -- the actual regression ---------------------------------------------------


def test_switch_clicks_for_real_and_never_uses_select(wired):
    radio = wired.add("super_battery", "Super Battery", becomes=(4, 0))
    result = sp.msi_set_scenario("super battery")

    assert radio.clicks == 1
    assert radio.selects == 0, "select() flips the checkmark without applying — never use it"
    assert "switched to 'Super Battery'" in result
    assert wired.state["value"] == (4, 0)


def test_checkmark_alone_is_never_reported_as_success(wired):
    # The exact bug: the app takes the click visually but never applies it.
    radio = wired.add("super_battery", "Super Battery", applies=False)
    result = sp.msi_set_scenario("super_battery")

    assert radio.is_selected() is True, "the UI claims it switched"
    assert "hasn't applied it" in result, f"reported a phantom success: {result!r}"
    assert radio.clicks == 3, "a click that doesn't apply is retried until the attempts run out"


def test_desynced_checkmark_is_repaired_not_trusted(wired):
    # Residue of an earlier select(): checkmark says Super Battery, machine is
    # still Balanced. Asking for Super Battery must click, not shrug.
    radio = wired.add("super_battery", "Super Battery", becomes=(4, 0))
    radio._selected = True

    result = sp.msi_set_scenario("super_battery")

    assert radio.clicks == 1, "a lying checkmark must not short-circuit the switch"
    assert "switched to" in result
    assert wired.state["value"] == (4, 0)


def test_already_active_is_decided_by_msi_state_not_the_radio(wired):
    radio = wired.add("balanced", "Balanced", becomes=(2, 0))
    radio._selected = False  # checkmark disagrees; MSI state (2, 0) wins

    result = sp.msi_set_scenario("balanced")

    assert radio.clicks == 0
    assert "already 'Balanced'" in result


def test_ai_engine_is_verified_by_its_own_flag(wired):
    # AI Engine leaves Mode at whatever it resolved to; Intelligent is the tell.
    wired.state["value"] = (1, 0)
    radio = wired.add("ai_engine", "AI Engine", becomes=(1, 1))

    assert "switched to 'AI Engine'" in sp.msi_set_scenario("ai")
    assert radio.clicks == 1

    # ...and with AI Engine driving, no fixed scenario counts as active.
    assert sp._state_matches("extreme_performance", (1, 1)) is False


def test_unreadable_msi_state_downgrades_the_claim(wired, monkeypatch):
    monkeypatch.setattr(sp, "_scenario_state", lambda: None)
    radio = wired.add("extreme_performance", "Extreme Performance")

    result = sp.msi_set_scenario("turbo")

    assert radio.clicks == 1, "one attempt only — there is nothing to wait for"
    assert "couldn't read MSI Center's own state" in result


def test_unclickable_control_reports_failure(wired, monkeypatch):
    wired.add("extreme_performance", "Extreme Performance")
    monkeypatch.setattr(sp, "_click_for_real", lambda target: "the control is collapsed")

    result = sp.msi_set_scenario("extreme")

    assert result.startswith("Failed to select MSI scenario")
    assert "collapsed" in result


def test_unavailable_scenario_lists_what_is_offered(wired, monkeypatch):
    monkeypatch.setattr(sp, "_log_radio_inventory", lambda wins: None)
    wired.add("balanced", "Balanced")
    wired.add("super_battery", "Super Battery")

    result = sp.msi_set_scenario("silent")

    assert "isn't available right now" in result
    assert "balanced" in result and "super_battery" in result
    assert "isn't offered on this model" in result


# -- the tile-type and re-render regressions -------------------------------
# Both are ways a switch can fail while the window is open and readable:
# MSI re-emitting the scenario tiles as a non-RadioButton control type, and
# the live control object going stale between the scan and the click.


class _FakeAnyTypeRadio(_FakeRadio):
    """A scenario tile that is NOT a RadioButton — e.g. an XAML update re-emits
    them as a custom tile. It must still be found and clicked."""

    def element_info(self):
        class _Info:
            automation_id = "Balanced_Mode_Copy1"
            control_type = "Custom"

        return _Info()


def test_tiles_found_even_when_not_radios(wired, monkeypatch):
    """Scanning only control_type='RadioButton' returns nothing and every
    switch reads as 'controls not readable'. The full-tree fallback must find
    the tile by its id stem regardless of control type."""
    # Start on Super Battery: from Balanced, msi_set_scenario correctly answers
    # "already Balanced" before it ever looks for a tile.
    wired.state["value"] = (4, 0)
    tile = _FakeAnyTypeRadio(on_apply=lambda: wired.state.update(value=(2, 0)))
    monkeypatch.setattr(sp, "_log_radio_inventory", lambda wins: None)
    monkeypatch.setattr(sp, "_scenario_inventory", lambda buttons: {})
    monkeypatch.setattr(sp, "_scenario_inventory_any", lambda win: {"balanced": tile})
    monkeypatch.setattr(sp, "_reacquire_control", lambda win, canonical: tile)
    monkeypatch.setattr(sp, "_control_label", lambda c: "Balanced")

    result = sp.msi_set_scenario("balanced")

    assert "switched to 'Balanced'" in result


def test_inventory_any_ignores_controls_whose_metadata_raises(monkeypatch):
    """The full-tree fallback meets every control type; a half-rendered one
    raises on element_info. One bad control must not cost the whole inventory."""
    good = _FakeAnyTypeRadio()

    class _BadInfo:
        @property
        def automation_id(self):
            raise RuntimeError("half-rendered")

        control_type = "Button"

    bad = type(
        "Bad", (), {"element_info": lambda self: _BadInfo(), "window_text": lambda self: "x"}
    )()

    win = type("W", (), {"descendants": lambda self: [good, bad]})()
    found = sp._scenario_inventory_any(win)

    assert set(found) == {"balanced"}


def test_reacquire_uses_the_full_tree_when_radios_are_gone(monkeypatch):
    """After a re-render that drops the tiles' control type, re-acquisition
    must still find the scenario — a stale control object is what makes the
    first click land on nothing."""
    seen = {"radio_scan": 0, "full_scan": 0}

    def fake_descendants(self, **kwargs):
        if kwargs.get("control_type") == "RadioButton":
            seen["radio_scan"] += 1
            return []
        seen["full_scan"] += 1
        return [_FakeAnyTypeRadio()]

    win = type("W", (), {"descendants": fake_descendants, "is_minimized": lambda self: False})()
    monkeypatch.setattr(sp, "_wake_window", lambda win: None)
    # Nothing else is patched: the radio scan comes back empty on its own, and
    # the REAL _scenario_inventory_any must then find the tile. Replacing it
    # (or _scenario_inventory, which it reuses) meant the full-tree scan this
    # test counts could never happen.

    assert sp._reacquire_control(win, "balanced") is not None
    assert seen["radio_scan"] == 1 and seen["full_scan"] == 1


def test_a_switch_re_acquires_the_control_after_a_failed_attempt(wired, monkeypatch):
    """The reported symptom: the first click doesn't apply. Instead of giving
    up, the next attempt re-scans the live tree — which is where a re-rendered
    tile would now be found."""
    monkeypatch.setattr(sp, "_log_radio_inventory", lambda wins: None)
    wired.state["value"] = (4, 0)  # on Super Battery, so "balanced" is a real switch
    first = wired.add("balanced", "Balanced", becomes=(2, 0))
    first.applies = False  # the first click does not apply

    # The re-rendered tile: what the live tree holds after the first click.
    reacquired = _FakeRadio(
        "Balanced", applies=True, on_apply=lambda: wired.state.update(value=(2, 0))
    )
    scans = iter([first, reacquired, reacquired])
    monkeypatch.setattr(sp, "_reacquire_control", lambda win, canonical: next(scans))
    monkeypatch.setattr(sp, "_control_label", lambda c: "Balanced")

    result = sp.msi_set_scenario("balanced")

    assert "switched to 'Balanced'" in result
    assert first.clicks == 1 and reacquired.clicks == 1


def test_reacquire_returns_none_when_the_control_is_gone(monkeypatch):
    monkeypatch.setattr(sp, "_wake_window", lambda win: None)
    monkeypatch.setattr(sp, "_scenario_inventory", lambda buttons: {})
    monkeypatch.setattr(sp, "_scenario_inventory_any", lambda win: {})

    win = _FakeWindow()
    assert sp._reacquire_control(win, "balanced") is None


def test_click_attempts_give_up_after_the_control_vanishes(wired, monkeypatch):
    monkeypatch.setattr(sp, "_log_radio_inventory", lambda wins: None)
    wired.state["value"] = (4, 0)  # on Super Battery, so "balanced" is a real switch
    wired.add("balanced", "Balanced")
    monkeypatch.setattr(sp, "_reacquire_control", lambda win, canonical: None)

    result = sp.msi_set_scenario("balanced")

    assert result.startswith("Failed to select MSI scenario")
    assert "vanished" in result


# -- pure logic --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("turbo", "extreme_performance"),
        ("gaming", "extreme_performance"),
        ("Extreme Performance", "extreme_performance"),
        ("normal", "balanced"),
        ("quiet", "silent"),
        ("battery saver", "super_battery"),
        ("eco", "super_battery"),
        ("AI Engine", "ai_engine"),
        ("auto", "ai_engine"),
        ("", None),
        (None, None),
        ("banana", None),
    ],
)
def test_scenario_normalization(raw, expected):
    assert sp._normalize_scenario(raw) == expected


@pytest.mark.parametrize(
    "canonical,state,expected",
    [
        ("balanced", (2, 0), True),
        ("balanced", (2, 1), False),  # AI Engine is driving
        ("balanced", (4, 0), False),
        ("extreme_performance", (1, 0), True),
        ("super_battery", (4, 0), True),
        ("ai_engine", (2, 1), True),
        ("ai_engine", (2, 0), False),
        ("silent", (3, 0), False),  # code unverified — never claim "already"
        ("balanced", None, False),
    ],
)
def test_state_predicate(canonical, state, expected):
    assert sp._state_matches(canonical, state) is expected


def test_await_accepts_any_change_for_an_unmapped_scenario(monkeypatch):
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setattr(sp, "_scenario_state", lambda: (3, 0))
    assert sp._await_scenario("silent", (2, 0), timeout=0.2) is True
    assert sp._await_scenario("silent", (3, 0), timeout=0.2) is False


def test_dispatch_routes_actions(monkeypatch):
    monkeypatch.setattr(sp, "msi_set_scenario", lambda v: f"<scenario:{v}>")
    monkeypatch.setattr(sp, "pc_boost", lambda: "<boost>")

    assert sp.system_performance({"action": "pc_boost"}) == "<boost>"
    assert sp.system_performance({"action": "smart_boost"}) == "<boost>"
    assert sp.system_performance({"action": "msi_scenario", "value": "turbo"}) == "<scenario:turbo>"
    assert sp.system_performance({"action": "set_scenario", "mode": "eco"}) == "<scenario:eco>"
    assert sp.system_performance({"description": "boost the pc"}) == "<boost>"
    assert sp.system_performance({"description": "go quiet mode"}) == "<scenario:silent>"
    assert sp.system_performance({}).startswith("Unknown system_performance")
    assert sp.system_performance(None).startswith("Unknown system_performance")


# -- the direct path ---------------------------------------------------------
# Writing MSI's scenario key and bouncing its service skips the UI entirely.
# The first attempt at it reported success it had not earned: it stopped a
# service that does not exist under that name ("MSIAPService" is the process;
# the service is "MSI Foundation Service"), and it treated reading back its
# own SetValueEx as proof the machine had changed. These pin both shut.


@pytest.fixture
def direct(monkeypatch):
    """A writable fake of MSI's scenario key, with no real registry or service
    anywhere near it."""
    sp._DIRECT_CACHE.clear()
    state = {"value": (2, 0)}  # Balanced
    calls = {"writes": [], "restarts": 0, "clicks_possible": False}

    monkeypatch.setattr(sp, "is_windows", lambda: True)
    monkeypatch.setattr(sp, "_co_init", lambda: None)
    monkeypatch.setattr(sp, "_DIRECT_PATH_ENABLED", True)
    monkeypatch.setattr(sp, "_scenario_key_writable", lambda: True)
    monkeypatch.setattr(sp, "_scenario_state", lambda: state["value"])
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)

    def write(mode, intelligent):
        calls["writes"].append((mode, intelligent))
        state["value"] = (mode, intelligent)
        return None

    monkeypatch.setattr(sp, "_write_scenario_values", write)
    monkeypatch.setattr(
        sp, "_restart_msi_service", lambda: calls.update(restarts=calls["restarts"] + 1)
    )

    def no_ui(*a, **k):
        raise AssertionError("the UI must not be touched when the direct path works")

    monkeypatch.setattr(sp, "_find_windows", no_ui)
    return type("Direct", (), {"state": state, "calls": calls})


def _dead_end_ui(monkeypatch):
    """Let the fallback run without it reaching the real machine: a window with
    no scenario controls, and no launch if there were none to find."""
    monkeypatch.setattr(sp, "_find_windows", lambda *a, **k: [_FakeWindow()])
    monkeypatch.setattr(sp, "_wake_window", lambda win: None)
    monkeypatch.setattr(sp, "_scenario_inventory", lambda buttons: {})
    monkeypatch.setattr(sp, "_log_radio_inventory", lambda wins: None)
    monkeypatch.setattr(sp, "_SCENARIO_SCAN_TIMEOUT", 0.0)
    monkeypatch.setattr(
        sp.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch MSI Center")
    )


def test_direct_switch_skips_the_ui_entirely(direct):
    result = sp.msi_set_scenario("eco")

    assert direct.calls["writes"] == [(4, 0)]
    assert direct.calls["restarts"] == 0, "don't bounce the service when it took the key on its own"
    assert direct.state["value"] == (4, 0)
    assert "switched to 'Battery'" in result


def test_direct_bounces_the_service_only_when_the_write_did_not_settle(direct, monkeypatch):
    seen = {"confirms": 0}
    real_confirm = sp._confirm_direct

    def flaky(canonical, before, settle):
        seen["confirms"] += 1
        if seen["confirms"] == 1:
            return False  # the service hasn't noticed yet
        return real_confirm(canonical, before, settle)

    monkeypatch.setattr(sp, "_confirm_direct", flaky)

    assert "switched to" in sp.msi_set_scenario("turbo")
    assert direct.calls["restarts"] == 1


def test_a_write_the_service_reverts_is_not_a_switch(direct, monkeypatch):
    # The exact false positive to avoid: SetValueEx succeeds, so an immediate
    # read agrees with us — and then the service puts its own value back.
    monkeypatch.setattr(sp, "_write_scenario_values", lambda mode, intelligent: None)
    _dead_end_ui(monkeypatch)

    result = sp.msi_set_scenario("eco")

    assert "switched to" not in result, f"claimed a switch the service reverted: {result!r}"


def test_direct_never_guesses_an_unverified_scenario_code(direct, monkeypatch):
    # Silent has no known Mode code on this chassis; writing one would be a
    # guess we could not check, so the UI has to take it.
    _dead_end_ui(monkeypatch)

    sp.msi_set_scenario("quiet")

    assert direct.calls["writes"] == []


def test_ai_engine_sets_the_flag_and_leaves_mode_alone(direct):
    direct.state["value"] = (1, 0)  # Extreme Performance, AI Engine off

    assert "switched to 'Ai Engine'" in sp.msi_set_scenario("ai")
    assert direct.calls["writes"] == [(1, 1)], "Mode is AI Engine's to resolve, not ours to pick"


def test_confirm_requires_the_state_to_have_actually_moved(monkeypatch):
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setattr(sp, "_scenario_state", lambda: (4, 0))

    assert sp._confirm_direct("super_battery", (2, 0), 0.0) is True
    assert sp._confirm_direct("super_battery", (4, 0), 0.0) is False, "a no-op is not a switch"


def test_unwritable_key_costs_nothing_and_falls_through(monkeypatch):
    sp._DIRECT_CACHE.clear()
    monkeypatch.setattr(sp, "_scenario_key_writable", lambda: False)
    monkeypatch.setattr(sp, "_write_scenario_values", lambda *a: pytest.fail("must not write"))
    monkeypatch.setattr(
        sp, "_restart_msi_service", lambda: pytest.fail("must not touch the service")
    )

    assert sp._set_msi_scenario_direct("balanced", (4, 0)) is False


def test_service_name_is_probed_not_assumed(monkeypatch):
    sp._DIRECT_CACHE.clear()
    assert "MSIAPService" not in sp._MSI_SERVICE_CANDIDATES, (
        "MSIAPService is the process name; `net stop MSIAPService` always fails"
    )

    asked = []

    class _Done:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = self.stderr = ""

    def fake_run(cmd, **kwargs):
        asked.append(cmd[-1])
        # 1060 == the service does not exist
        return _Done(0 if cmd[-1] == "MSI_Center_Service" else 1060)

    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    assert sp._msi_service_name() == "MSI_Center_Service"
    assert asked[0] == "MSI Foundation Service", "the installed name is tried first"
    assert sp._msi_service_name() == "MSI_Center_Service"
    assert len(asked) == 2, "the answer is memoized, not re-probed on every switch"


def test_writability_is_re_probed_not_remembered(monkeypatch):
    """The grant can land while Era is running, and an MSI Center update could
    take it away again. Caching the answer would need a relaunch to notice."""
    sp._DIRECT_CACHE.clear()
    monkeypatch.setattr(sp, "_scenario_key_view", lambda: 512)
    answers = iter([OSError(5, "denied"), None, OSError(5, "denied")])

    def probe(*a, **k):
        outcome = next(answers)
        if outcome:
            raise outcome
        return type("K", (), {"Close": lambda self: None})()

    monkeypatch.setattr(sp.winreg, "OpenKey", probe)

    assert sp._scenario_key_writable() is False  # before the grant
    assert sp._scenario_key_writable() is True  # granted mid-session
    assert sp._scenario_key_writable() is False  # an update took it away


def test_direct_path_is_off_because_the_key_is_not_an_input(monkeypatch):
    """2026-09-21: with SetValue granted, writing the key left MSI Center still
    showing the old scenario — the service writes this key, it never reads it.
    An unverifiable switch must not be attempted, let alone reported."""
    assert sp._DIRECT_PATH_ENABLED is False
    monkeypatch.setattr(
        sp, "_scenario_key_writable", lambda: pytest.fail("must not even probe while disabled")
    )

    assert sp._set_msi_scenario_direct("balanced", (2, 1)) is False


# -- reading state (the one thing the key IS good for) -----------------------


def test_status_reads_state_without_touching_the_ui(monkeypatch):
    monkeypatch.setattr(sp, "is_windows", lambda: True)
    monkeypatch.setattr(sp, "_scenario_state", lambda: (1, 0))
    monkeypatch.setattr(sp, "_on_ac_power", lambda: True)
    monkeypatch.setattr(
        sp, "_find_windows", lambda *a, **k: pytest.fail("status must never open the UI")
    )

    result = sp.msi_scenario_status()

    assert "Extreme Performance" in result
    assert "every scenario is available" in result


def test_status_names_ai_engine_and_what_it_resolved_to(monkeypatch):
    monkeypatch.setattr(sp, "is_windows", lambda: True)
    monkeypatch.setattr(sp, "_scenario_state", lambda: (2, 1))
    monkeypatch.setattr(sp, "_on_ac_power", lambda: False)

    result = sp.msi_scenario_status()

    assert "AI Engine is driving" in result
    assert "Balanced" in result, "the resolved mode is worth reporting, not just the flag"
    assert "on battery" in result


def test_status_says_so_when_the_state_is_unreadable(monkeypatch):
    monkeypatch.setattr(sp, "is_windows", lambda: True)
    monkeypatch.setattr(sp, "_scenario_state", lambda: None)

    assert "can't read" in sp.msi_scenario_status()


def test_status_actions_route_before_the_scenario_inference(monkeypatch):
    # "performance_status" contains "performance"; the loose alias match would
    # otherwise read it as "switch to Extreme Performance".
    monkeypatch.setattr(sp, "msi_scenario_status", lambda: "<status>")
    monkeypatch.setattr(
        sp, "msi_set_scenario", lambda v: pytest.fail(f"routed to a switch instead: {v!r}")
    )

    for action in (
        "msi_scenario_status",
        "scenario_status",
        "current_scenario",
        "performance_status",
        "which_scenario",
    ):
        assert sp.system_performance({"action": action}) == "<status>", action
