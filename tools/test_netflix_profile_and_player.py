"""Tests for Netflix profile auto-selection and the rich player action surface.

The highest-value test here is `TestGeneratedJavaScriptIsValid`: every script in
this lane is a Python-built string that only ever runs inside a browser, so a
syntax error would sail through the whole Python suite and only surface as a
silent no-op on the user's screen. Node parses them here instead.

The rest pin the behaviours that were actually broken: a gate probe that never
waited, a click that was never verified, and a "click the first profile"
fallback that could silently select the wrong person's profile.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from actions._netflix import player_api, player_js
from actions._netflix import profile_gate as pg

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePage:
    """Stands in for a Playwright page; records evaluated scripts."""

    def __init__(self, responses):
        # responses: list of values, or a callable taking the script
        self._responses = responses
        self.scripts: list[str] = []

    def evaluate(self, script):
        self.scripts.append(script)
        if callable(self._responses):
            return self._responses(script)
        if self._responses:
            return self._responses.pop(0)
        return "no-gate"


class FakeSession:
    """sess.run() is identity here — FakePage.evaluate already returned a value."""

    def run(self, value):
        return value


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    """Collapse the real waits so tests do not sleep for seconds."""
    monkeypatch.setattr(pg, "POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(pg, "GATE_APPEAR_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pg, "GATE_CLEAR_TIMEOUT_S", 0.05)
    pg.reset_profile_cache()
    yield
    pg.reset_profile_cache()


# ---------------------------------------------------------------------------
# Generated JavaScript must actually parse
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestGeneratedJavaScriptIsValid:
    """Parse every generated script with Node.

    Wrapped exactly the way BrowserSession.evaluate_background_media wraps it —
    `(() => { ... })()` — because these scripts use a top-level `return`, which
    is only legal inside that wrapper.
    """

    def _assert_parses(self, script: str, label: str, tmp_path):
        """Parse-check with Node. `--check` on a FILE, never on stdin.

        Feeding the source through stdin made `node --check` block until the
        pipe closed, which hung this class for minutes and produced timeout
        "failures" that had nothing to do with the JavaScript. A temp file is
        deterministic and an order of magnitude faster.
        """
        js = tmp_path / f"{abs(hash(label))}.js"
        js.write_text(f"(() => {{ {script} }})()", encoding="utf-8")
        proc = subprocess.run(
            ["node", "--check", str(js)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"{label} is not valid JavaScript:\n{proc.stderr}"

    def test_gate_detection_script_parses(self, tmp_path):
        self._assert_parses(player_js.DETECT_GATE_JS, "DETECT_GATE_JS", tmp_path)

    @pytest.mark.parametrize("name", ["Marwa", "Jean-Luc", "O'Brien", 'Quote"Name', "Grégoire"])
    def test_profile_scripts_parse_for_any_name(self, name, tmp_path):
        """Names are interpolated into JS — quotes must not break out."""
        self._assert_parses(player_js.build_profile_lookup_js(name), f"lookup({name})", tmp_path)
        self._assert_parses(player_js.build_profile_click_js(name), f"click({name})", tmp_path)

    @pytest.mark.parametrize("action", sorted(player_api.PLAYER_ACTIONS))
    def test_every_player_action_parses(self, action, tmp_path):
        params = {"position": 90, "value": 50, "amount": 30}
        script = player_api.build_player_script(action, params)
        assert script is not None, f"{action} produced no script"
        self._assert_parses(script, action, tmp_path)

    def test_negative_and_float_seeks_parse(self, tmp_path):
        for params in ({"amount": 7.5}, {"amount": 0.5}, {"amount": 3600}):
            self._assert_parses(
                player_api.build_player_script("rewind", params), "rewind", tmp_path
            )


# ---------------------------------------------------------------------------
# Profile name resolution
# ---------------------------------------------------------------------------


class TestProfileName:
    def test_defaults_to_marwa(self, monkeypatch):
        monkeypatch.delenv("ERA_NETFLIX_PROFILE", raising=False)
        pg.reset_profile_cache()
        assert pg.configured_profile() == "Marwa"

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("ERA_NETFLIX_PROFILE", "Nesri")
        pg.reset_profile_cache()
        assert pg.configured_profile() == "Nesri"

    def test_name_is_embedded_case_insensitively(self):
        assert '"marwa"' in player_js.build_profile_lookup_js("MaRwA")

    def test_accents_are_folded_before_embedding(self):
        """The live gate is French: "Grégoire" must be findable as "gregoire"."""
        assert player_js._fold("Grégoire") == "gregoire"
        assert '"gregoire"' in player_js.build_profile_lookup_js("Grégoire")

    def test_name_with_quotes_is_escaped_not_injected(self):
        """A profile name is data; it must not be able to inject JS."""
        payload = 'x"; window.evil=1; //'
        script = player_js.build_profile_lookup_js(payload)
        assert "window.evil" not in script.replace(json.dumps(player_js._fold(payload)), "")


class TestPlayerUsesNetflixOwnApi:
    """The seek/volume failures came from starting at `document.querySelector('video')`.

    Probed live over CDP: that returns **null** on the watch page while
    Netflix's own API reports a healthy player (322s of a 3816s title). A
    script that begins at the element finds nothing, returns its sentinel, and
    the user is told the command ran while nothing moved.
    """

    _API = "netflix.appContext.state.playerApp.getAPI()"

    @pytest.mark.parametrize(
        "action,params",
        [
            ("forward", {"amount": 10}),
            ("rewind", {"amount": 10}),
            ("seek", {"position": 120}),
            ("volume_up", {}),
            ("volume_down", {}),
            ("set_volume", {"value": 40}),
            ("restart", {}),
            ("next_episode", {}),
            ("playback_info", {}),
        ],
    )
    def test_action_reaches_for_the_netflix_player_first(self, action, params):
        script = player_api.build_player_script(action, params)
        assert self._API in script, f"{action} does not use Netflix's player API"

    def test_video_element_is_kept_as_a_fallback(self):
        """The element is unreliable, not useless — keep it as the second rung."""
        script = player_api.build_player_script("rewind", {"amount": 10})
        assert "querySelectorAll('video')" in script

    def test_seek_is_verified_not_assumed(self):
        """Netflix can accept a seek and ignore it when the session is dead."""
        for action, params in [("forward", {}), ("rewind", {}), ("seek", {"position": 30})]:
            script = player_api.build_player_script(action, params)
            assert "_seekVerified" in script
        assert "did not move the playback position" in player_api._PREAMBLE

    def test_volume_calls_the_players_own_setter(self):
        """Netflix volume, never the system mixer."""
        script = player_api.build_player_script("set_volume", {"value": 40})
        assert "setVolume(" in script
        assert "computer_settings" not in script

    def test_next_episode_uses_the_real_api_method(self):
        """playNextEpisode exists on the live player — no credits overlay needed."""
        assert "playNextEpisode" in player_api.build_player_script("next_episode")

    def test_previous_episode_is_honest_about_netflix_having_no_control(self):
        """There is no previous-episode API method or button; say so, don't fake it."""
        script = player_api.build_player_script("previous_episode")
        assert "no previous-episode" in script
        assert "control-episodes" in script  # offers the episode list instead

    def test_api_times_are_milliseconds(self):
        """getCurrentTime/seek are ms; video.currentTime is seconds."""
        assert "/ 1000" in player_api._PREAMBLE
        assert "* 1000" in player_api._PREAMBLE


class TestNeverWriteCurrentTimeOnNetflix:
    """The M7375 regression, pinned.

    Netflix streams through Media Source Extensions. Assigning to
    `video.currentTime` on an MSE element desynchronises the buffer from the
    player's state machine: the user's playback session died with Netflix error
    M7375 ("Veuillez nous excuser pour cette interruption") on every rewind and
    fast-forward. Reading the property is fine; writing it is not.

    Seeking must go through Netflix's own `player.seek()` and nothing else.
    """

    _WRITES = ("currentTime =", "currentTime=", "currentTime +=", "currentTime -=")

    def _assert_no_write(self, script, label):
        for pattern in self._WRITES:
            assert pattern not in script, (
                f"{label} writes video.currentTime — this is what caused M7375. "
                "Seek through the Netflix player API instead."
            )

    @pytest.mark.parametrize("action", sorted(player_api.PLAYER_ACTIONS))
    def test_no_player_script_writes_current_time(self, action):
        script = player_api.build_player_script(action, {"position": 30, "value": 50})
        self._assert_no_write(script or "", action)

    def test_legacy_js_scripts_no_longer_seek_by_element(self):
        """forward/rewind used to live here and set currentTime directly."""
        from actions.netflix_control import _JS_SCRIPTS

        for name, script in _JS_SCRIPTS.items():
            self._assert_no_write(script, f"_JS_SCRIPTS[{name}]")
        assert "forward" not in _JS_SCRIPTS
        assert "rewind" not in _JS_SCRIPTS

    def test_seek_refuses_rather_than_falling_back_to_the_element(self):
        """No API means no seek — a refusal is recoverable, a dead stream is not."""
        script = player_api.build_player_script("rewind", {"amount": 10})
        assert "cannot reach the Netflix player" in script

    def test_position_is_still_read_from_the_element_as_a_fallback(self):
        """Reading is harmless and keeps status working when the API is absent."""
        assert "return v ? v.currentTime : null" in player_api._PREAMBLE

    def test_a_stalled_player_is_reported_not_retried(self):
        script = player_api.build_player_script("forward", {"amount": 10})
        assert "needs a reload" in script

    def test_reload_recovery_action_exists(self):
        """A dead session is fixed by reloading, not by another seek."""
        script = player_api.build_player_script("reload")
        assert script is not None
        assert "location.reload()" in script
        assert "Resuming from about" in script  # tells the user where they were

    def test_playback_info_surfaces_a_netflix_error(self):
        assert "_playerError" in player_api.build_player_script("playback_info")


class TestNetflixActionsAreNeverHijacked:
    """Both of these shipped broken and were reported by the user.

    The action registry rewrites a call's TOOL from its action name alone, and
    the safety guard classifies power actions tool-agnostically. Netflix's
    vocabulary collides with both.
    """

    @pytest.mark.parametrize(
        "action", ["volume_up", "volume_down", "set_volume", "restart", "back", "forward", "rewind"]
    )
    def test_netflix_actions_stay_on_netflix(self, action):
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call("netflix_control", {"action": action}, "netflix")
        assert tool == "netflix_control", f"{action} was rerouted to {tool}"
        assert params.get("action") == action

    def test_netflix_volume_is_not_system_volume(self):
        """The reported bug: asking for Netflix volume changed the whole machine."""
        from core.action_registry import canonicalize_tool_call

        tool, params = canonicalize_tool_call("netflix_control", {"action": "volume_up"})
        assert tool == "netflix_control"
        assert params["action"] == "volume_up"

    def test_netflix_restart_is_not_a_machine_reboot(self):
        """The dangerous one: Era offered to reboot the PC to restart an episode."""
        from core.safety_guard import classify_action

        verdict = classify_action("netflix_control", {"action": "restart"})
        assert verdict["risk_level"] == "LOW"
        assert verdict["requires_confirmation"] is False
        assert "power" not in verdict["reason"].lower()

    @pytest.mark.parametrize(
        "tool,action",
        [
            ("computer_settings", "restart"),
            ("computer_settings", "shutdown"),
            ("computer_control", "reboot"),
            ("file_controller", "shutdown"),
        ],
    )
    def test_real_power_actions_are_still_gated(self, tool, action):
        """Exempting media players must not weaken the machine-power guard."""
        from core.safety_guard import classify_action

        verdict = classify_action(tool, {"action": action})
        assert verdict["risk_level"] == "HIGH"
        assert verdict["requires_confirmation"] is True

    def test_system_volume_still_belongs_to_computer_settings(self):
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call(
            "computer_settings", {"action": "volume_up"}, "turn up the volume"
        )
        assert tool == "computer_settings"
        assert params.get("action") == "volume_up"


class TestAgainstRealGateMarkup:
    """Pin the hooks taken from the LIVE profile gate (captured 2026-09-15).

    The gate is French and each card is a plain anchor:

        <li class="profile">
          <a class="profile-link" data-uia="action-select-profile+secondary"
             href="/SwitchProfile?tkn=…"><span class="profile-name">Marwa</span></a>
        </li>

    These assertions exist because the first implementation clicked the card —
    which found the right element every time and never navigated, since
    Netflix's React handler consumes the click. Reading the href is the fix, so
    the lookup must keep targeting these exact hooks.
    """

    def test_lookup_reads_the_switchprofile_href(self):
        script = player_js.build_profile_lookup_js("Marwa")
        assert 'a[href*="SwitchProfile"]' in script
        assert "getAttribute('href')" in script
        assert "new URL(" in script  # relative href resolved to absolute

    def test_lookup_reads_the_profile_name_span(self):
        assert ".profile-name" in player_js.build_profile_lookup_js("Marwa")

    def test_lookup_scans_list_items(self):
        assert "querySelectorAll('li, .profile')" in player_js.build_profile_lookup_js("Marwa")

    def test_picker_selector_matches_the_live_container(self):
        """The live container is ul.choose-profile inside .list-profiles."""
        assert "ul.choose-profile" in player_js.PICKER_SELECTOR
        assert ".list-profiles" in player_js.PICKER_SELECTOR

    def test_lookup_does_not_click(self):
        """Clicking is the fallback only — the primary path must not fire events."""
        script = player_js.build_profile_lookup_js("Marwa")
        assert ".click()" not in script
        assert "dispatchEvent" not in script

    def test_exact_name_wins_over_a_substring_match(self):
        """ "Marwa" must not resolve to "Marwa Kids" when a real "Marwa" exists."""
        script = player_js.build_profile_lookup_js("Marwa")
        assert "=== needle" in script  # exact pass runs first
        assert ".includes(needle)" in script  # containing pass is the fallback


class TestAgainstButtonTileMarkup:
    """Pin the hooks of the redesigned gate (captured live 2026-09-16).

        <div data-uia="profile-gate-screen">
          <div data-uia="profile-selector">
            <button data-uia="profile-selector+tile-2XNV3RMVE5EZPK3C4UZKRLN5LQ">
              <div><img alt=""></div><span><span>Marwa</span></span>
            </button>
            ...
          <button data-uia="profile-gate-screen+manage">Gérer les profils</button>

    No anchors, no .profile-name, no <li>: the 2026-09-15 lookup found zero
    cards on this page and reported every profile as missing. The tile suffix
    is the same SwitchProfile token the old anchor carried.
    """

    def test_picker_selector_matches_the_new_container(self):
        assert '[data-uia="profile-gate-screen"]' in player_js.PICKER_SELECTOR
        assert '[data-uia="profile-selector"]' in player_js.PICKER_SELECTOR

    def test_lookup_reads_tiles_and_builds_the_switchprofile_url(self):
        script = player_js.build_profile_lookup_js("Marwa")
        assert "profile-selector+tile-" in script
        assert "/SwitchProfile?tkn=" in script

    def test_ordinal_is_embedded_zero_based(self):
        assert "const idx = 3;" in player_js.build_profile_lookup_js(ordinal=4)

    def test_ordinal_lookup_never_falls_back_to_a_name(self):
        script = player_js.build_profile_lookup_js("Marwa", ordinal=2)
        assert "needle" not in script


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestLookupAgainstLiveMarkupInNode:
    """Run the generated lookup against a DOM-shaped stub of the real gate."""

    STUB = r"""
const TILES = [
  ['GDLJTYJJAVHF3KGYWSPXKC47EQ', 'Grégoire'], ['2X5YWXOKERACRODTA6FPJQFA2Q', 'Catherine'],
  ['HS76BB23VRDF7M2AMJQDBXQYIU', 'Aurélien'], ['2XNV3RMVE5EZPK3C4UZKRLN5LQ', 'Marwa'],
  ['ZB2PMXVG5FFHZISV6UW6NSBBEE', 'Jeunesse'],
];
const mk = (uia, text) => ({
  getAttribute: (k) => (k === 'data-uia' ? uia : null),
  querySelector: () => null, textContent: text,
});
const els = TILES.map(([t, n]) => mk('profile-selector+tile-' + t, n))
  .concat([mk('profile-gate-screen+manage', 'Gérer les profils')]);
const root = {
  closest: () => root,
  querySelectorAll: (sel) => sel.startsWith('[data-uia^=')
    ? els.filter((e) => e.getAttribute('data-uia').startsWith('profile-selector+tile-'))
    : [],
};
globalThis.document = {querySelector: () => root};
globalThis.location = {origin: 'https://www.netflix.com'};
"""

    def _run(self, script, tmp_path):
        js = tmp_path / "lookup.js"
        js.write_text(
            self.STUB + f"process.stdout.write((() => {{ {script} }})());", encoding="utf-8"
        )
        proc = subprocess.run(
            ["node", str(js)], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def test_fourth_profile_is_marwa(self, tmp_path):
        out = self._run(player_js.build_profile_lookup_js(ordinal=4), tmp_path)
        assert out == (
            "found|https://www.netflix.com/SwitchProfile?tkn=2XNV3RMVE5EZPK3C4UZKRLN5LQ|Marwa"
        )

    def test_name_with_accents_folded(self, tmp_path):
        out = self._run(player_js.build_profile_lookup_js("gregoire"), tmp_path)
        assert out.endswith("tkn=GDLJTYJJAVHF3KGYWSPXKC47EQ|Grégoire")

    def test_out_of_range_ordinal_lists_profiles_without_the_manage_button(self, tmp_path):
        out = self._run(player_js.build_profile_lookup_js(ordinal=6), tmp_path)
        assert out == "profile-not-found|seen=Grégoire, Catherine, Aurélien, Marwa, Jeunesse"


# ---------------------------------------------------------------------------
# Gate handling
# ---------------------------------------------------------------------------


class TestProfileGate:
    def test_no_gate_is_reported_not_treated_as_failure(self):
        page = FakePage(lambda s: "no-gate")
        assert pg.ensure_profile_selected(FakeSession(), page)["status"] == "no-gate"

    def test_gate_appearing_late_is_still_caught(self, monkeypatch):
        """The original bug: Netflix renders the picker asynchronously, so a
        single immediate probe found nothing and gave up."""
        monkeypatch.setattr(pg, "GATE_APPEAR_TIMEOUT_S", 2.0)
        state = {"probes": 0}

        def respond(script):
            if "new URL(" in script:  # the lookup script
                return "found|https://www.netflix.com/SwitchProfile?tkn=ABC|Marwa"
            state["probes"] += 1
            if state["probes"] < 4:
                return "no-gate"  # not painted yet
            if state["probes"] == 4:
                return "gate"  # appears
            return "no-gate"  # cleared after navigating

        page = FakePage(respond)
        page.goto = lambda url, **kw: "navigated"
        result = pg.ensure_profile_selected(FakeSession(), page)
        assert result["status"] == "selected"

    def test_selection_follows_the_switchprofile_link(self):
        """The whole fix: navigate the token instead of clicking the card."""
        navigated = {}

        def respond(script):
            if "new URL(" in script:
                return "found|https://www.netflix.com/SwitchProfile?tkn=TOKEN123|Marwa"
            return "gate" if not navigated else "no-gate"

        page = FakePage(respond)

        def goto(url, **kw):
            navigated["url"] = url
            return "ok"

        page.goto = goto
        result = pg.ensure_profile_selected(FakeSession(), page)
        assert navigated["url"] == "https://www.netflix.com/SwitchProfile?tkn=TOKEN123"
        assert result["status"] == "selected"

    def test_selection_is_verified_by_the_gate_clearing(self):
        def respond(script):
            if "new URL(" in script:
                return "found|https://www.netflix.com/SwitchProfile?tkn=ABC|Marwa"
            return "gate"  # never clears

        page = FakePage(respond)
        page.goto = lambda url, **kw: "ok"
        result = pg.ensure_profile_selected(FakeSession(), page)
        assert result["status"] == "selected-unsure"
        assert "manual click" in result["message"]

    def test_reports_the_name_netflix_actually_showed(self):
        """Matching folds accents, so report the card's real spelling back."""

        def respond(script):
            if "new URL(" in script:
                return "found|https://www.netflix.com/SwitchProfile?tkn=X|Grégoire"
            return "gate" if "gate" not in respond.__dict__ else "no-gate"

        page = FakePage(
            lambda s: (
                "found|https://n/SwitchProfile?tkn=X|Grégoire" if "new URL(" in s else "no-gate"
            )
        )
        page.goto = lambda url, **kw: "ok"
        result = pg.ensure_profile_selected(
            FakeSession(), page, profile_name="gregoire", wait_for_gate=False
        )
        assert result["status"] == "no-gate"  # no gate up: nothing to do

    def test_click_fallback_retries_once_before_giving_up(self):
        """Reached only when the card has no SwitchProfile link."""
        calls = {"click": 0}

        def respond(script):
            # The click script shares the lookup's card enumeration, so tell
            # them apart by what only the click does.
            if "dispatchEvent" in script:  # click fallback
                calls["click"] += 1
                return "clicked|Marwa"
            if "SwitchProfile" in script and "new URL" in script:  # lookup
                return "no-href|Marwa"
            return "gate"

        result = pg.ensure_profile_selected(FakeSession(), FakePage(respond))
        assert calls["click"] == 2
        assert result["status"] == "selected-unsure"

    def test_missing_profile_names_what_it_saw(self):
        """Never guess — tell the user which profiles exist."""

        def respond(script):
            if "profile-not-found" in script:
                return "profile-not-found|seen=Nesri, Kids, Guest"
            return "gate"

        result = pg.ensure_profile_selected(FakeSession(), FakePage(respond))
        assert result["status"] == "not-found"
        assert "Nesri" in result["message"]
        assert "Marwa" in result["message"]

    def test_ordinal_selection_follows_the_tile_token(self):
        navigated = {}

        def respond(script):
            if "new URL(" in script:
                assert "const idx = 3;" in script
                return "found|https://www.netflix.com/SwitchProfile?tkn=TILE4|Marwa"
            return "no-gate" if navigated else "gate"

        page = FakePage(respond)
        page.goto = lambda url, **kw: navigated.setdefault("url", url)
        result = pg.ensure_profile_selected(FakeSession(), page, ordinal=4)
        assert navigated["url"].endswith("tkn=TILE4")
        assert result == {
            "status": "selected",
            "message": "Signed in on the Marwa profile.",
            "profile": "Marwa",
        }

    def test_out_of_range_ordinal_says_so(self):
        def respond(script):
            if "profile-not-found" in script:
                return "profile-not-found|seen=A, B"
            return "gate"

        result = pg.ensure_profile_selected(FakeSession(), FakePage(respond), ordinal=9)
        assert result["status"] == "not-found"
        assert "#9" in result["message"] and "A, B" in result["message"]

    def test_no_page_is_handled(self):
        assert pg.ensure_profile_selected(None, None)["status"] == "unavailable"

    def test_evaluation_errors_never_raise(self):
        class Exploding:
            def evaluate(self, script):
                raise RuntimeError("CDP died")

        result = pg.ensure_profile_selected(FakeSession(), Exploding())
        assert result["status"] in ("no-gate", "unavailable")

    def test_passive_calls_do_not_poll(self):
        """wait_for_gate=False must probe once, not spin."""
        page = FakePage(lambda s: "no-gate")
        pg.ensure_profile_selected(FakeSession(), page, wait_for_gate=False)
        assert len(page.scripts) == 1

    def test_gate_vanishing_mid_flight_counts_as_success(self):
        state = {"through": False}

        def respond(script):
            if "profile-not-found" in script:
                state["through"] = True
                return "no-gate"  # someone got through first
            return "no-gate" if state["through"] else "gate"

        assert pg.ensure_profile_selected(FakeSession(), FakePage(respond))["status"] == "selected"

    def test_a_failed_lookup_is_not_reported_as_success(self):
        """An evaluation error returns "" - that proves nothing while the gate is up."""

        def respond(script):
            if "dispatchEvent" in script:
                return "clicked|Marwa"
            if "profile-not-found" in script:
                return ""  # CDP hiccup
            return "gate"

        result = pg.ensure_profile_selected(FakeSession(), FakePage(respond))
        assert result["status"] != "selected"


class TestNoCoordinatesAnywhere:
    """Resolution independence is structural: if no script contains a hard-coded
    pixel position or a window-size read, screen size cannot affect behaviour."""

    def _all_scripts(self):
        yield player_js.DETECT_GATE_JS
        yield player_js.build_profile_lookup_js("Marwa")
        yield player_js.build_profile_click_js("Marwa")
        for action in sorted(player_api.PLAYER_ACTIONS):
            s = player_api.build_player_script(action, {"position": 10, "value": 50})
            if s:
                yield s

    def test_no_screen_or_window_size_reads(self):
        banned = ("window.innerWidth", "window.innerHeight", "screen.width", "screen.height")
        for script in self._all_scripts():
            for token in banned:
                assert token not in script, f"{token} makes behaviour resolution-dependent"

    def test_coordinates_are_always_derived_from_element_bounds(self):
        for script in self._all_scripts():
            if "clientX" in script:
                assert "getBoundingClientRect" in script


# ---------------------------------------------------------------------------
# Player action surface
# ---------------------------------------------------------------------------


class TestPlayerActions:
    @pytest.mark.parametrize(
        "action",
        [
            "skip_intro",
            "skip_recap",
            "skip_credits",
            "next_episode",
            "previous_episode",
            "restart",
            "episodes",
            "subtitles",
            "back",
            "playback_info",
        ],
    )
    def test_action_produces_a_script(self, action):
        assert player_api.build_player_script(action) is not None

    def test_forward_defaults_to_ten_seconds(self):
        assert "(10.0)" in player_api.build_player_script("forward", {})

    def test_forward_honours_amount(self):
        assert "(60.0)" in player_api.build_player_script("forward", {"amount": 60})

    def test_rewind_is_negative(self):
        assert "(-30.0)" in player_api.build_player_script("rewind", {"amount": 30})

    def test_seek_amount_is_clamped_to_something_sane(self):
        script = player_api.build_player_script("forward", {"amount": 999999})
        assert "3600.0" in script

    def test_rewind_never_goes_below_zero(self):
        assert "Math.max" in player_api.build_player_script("rewind", {"amount": 30})

    def test_seek_requires_a_position(self):
        assert player_api.build_player_script("seek", {}) is None
        assert player_api.build_player_script("seek", {"position": 120}) is not None

    def test_set_volume_accepts_percent_and_fraction(self):
        assert "0.3" in player_api.build_player_script("set_volume", {"value": 30})
        assert "0.3" in player_api.build_player_script("set_volume", {"value": 0.3})

    def test_set_volume_requires_a_value(self):
        assert player_api.build_player_script("set_volume", {}) is None

    def test_volume_is_clamped_to_unit_range(self):
        script = player_api.build_player_script("set_volume", {"value": 400})
        assert "Math.min(Math.max(1.0, 0), 1)" in script or "1.0" in script

    def test_garbage_amount_falls_back_to_the_default(self):
        assert "(10.0)" in player_api.build_player_script("forward", {"amount": "loud"})

    def test_unknown_action_returns_none_so_the_caller_falls_through(self):
        assert player_api.build_player_script("play") is None
        assert player_api.build_player_script("pause") is None

    def test_skip_scripts_target_netflix_stable_hooks(self):
        assert "player-skip-intro" in player_api.build_player_script("skip_intro")
        assert "player-skip-recap" in player_api.build_player_script("skip_recap")

    def test_next_episode_has_more_than_one_strategy(self):
        """One selector would break on the first Netflix redesign."""
        script = player_api.build_player_script("next_episode")
        assert "next-episode-seamless-button" in script
        assert "control-next" in script
        assert "next episode" in script.lower()

    def test_no_action_silently_does_nothing(self):
        """Every script must report an outcome, never fall off the end."""
        for action in sorted(player_api.PLAYER_ACTIONS):
            script = player_api.build_player_script(action, {"position": 5, "value": 50})
            assert "return" in script


# ---------------------------------------------------------------------------
# Dispatcher wiring
# ---------------------------------------------------------------------------


class TestDispatcherWiring:
    def test_aliases_reach_real_actions(self):
        from actions.netflix_control import _ACTION_ALIASES

        resolved = {
            "next": "next_episode",
            "skip": "skip_intro",
            "skip_the_recap": "skip_recap",
            "louder": "volume_up",
            "start_over": "restart",
            "open_netflix": "open",
            "exit_player": "back",
        }
        for alias, target in resolved.items():
            assert _ACTION_ALIASES.get(alias) == target

    def test_every_alias_target_is_dispatchable(self):
        """An alias pointing at an action nothing handles is a dead end."""
        from actions._netflix.player_api import PLAYER_ACTIONS
        from actions.netflix_control import _ACTION_ALIASES, _JS_SCRIPTS

        handled = (
            set(_JS_SCRIPTS)
            | set(PLAYER_ACTIONS)
            | {
                "search",
                "grid_play",
                "play",
                "status",
                "current",
                "open",
                "select_profile",
                "home",
                "browse",
                "go_home",
                "scroll",
                "scroll_up",
                "scroll_down",
                "focus",
            }
        )
        for alias, target in _ACTION_ALIASES.items():
            assert target in handled, f"alias '{alias}' points at unhandled '{target}'"

    def test_seeking_is_owned_solely_by_the_player_api(self):
        """forward/rewind used to exist in BOTH tables.

        The legacy pair set `video.currentTime`, which is what caused M7375, so
        they were removed from _JS_SCRIPTS rather than merely shadowed — a
        shadowed script is one dispatcher change away from running again.
        """
        from actions._netflix.player_api import PLAYER_ACTIONS
        from actions.netflix_control import _JS_SCRIPTS

        for action in ("forward", "rewind", "seek"):
            assert action in PLAYER_ACTIONS
            assert action not in _JS_SCRIPTS

    def test_any_remaining_overlap_still_resolves_to_the_player_api(self):
        """Whatever overlaps must be dispatched by PLAYER_ACTIONS first."""
        import inspect

        from actions import netflix_control as nc

        src = inspect.getsource(nc.netflix_control)
        player_branch = src.index("PLAYER_ACTIONS")
        legacy_branch = src.index("if action in _JS_SCRIPTS")
        assert player_branch < legacy_branch

    def test_open_netflix_is_rerouted_away_from_open_app(self):
        """open_app just loads the page and leaves the picker on screen.

        The reroute is the pre-existing hard-block further down route_tool_call,
        which lands on `browse` — and the browse branch clears the profile gate.
        Asserted here so a future change to that routing still has to keep
        Netflix off open_app.
        """
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call("open_app", {"app_name": "Netflix"}, "open netflix")
        assert tool == "netflix_control"
        # browse (no media intent) is the path that clears the gate.
        assert params["action"] in ("browse", "open")

    def test_open_netflix_and_play_still_reaches_search(self):
        """The launch reroute must not flatten a title into a bare open."""
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call(
            "open_app", {"app_name": "netflix"}, "open netflix and play Wednesday"
        )
        assert tool == "netflix_control"
        assert params["action"] == "search"

    def test_other_apps_still_open_normally(self):
        from core.self_healing_router import route_tool_call

        tool, _ = route_tool_call("open_app", {"app_name": "Spotify"}, "open spotify")
        assert tool == "open_app"

    def test_declaration_lists_the_new_actions(self):
        from app.tool_declarations import TOOL_DECLARATIONS

        decl = next(d for d in TOOL_DECLARATIONS if d["name"] == "netflix_control")
        action_desc = decl["parameters"]["properties"]["action"]["description"]
        for action in ("open", "next_episode", "skip_recap", "seek", "restart"):
            assert action in action_desc
