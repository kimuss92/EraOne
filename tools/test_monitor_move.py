"""Tests for moving windows / browser tabs between monitors (actions/monitor_move.py)."""

from actions import monitor_move as mm

LEFT = {
    "number": 1,
    "handle": 11,
    "rect": (-1920, 0, 0, 1080),
    "work": (-1920, 0, 0, 1032),
    "primary": False,
}
MAIN = {
    "number": 2,
    "handle": 22,
    "rect": (0, 0, 1920, 1080),
    "work": (0, 0, 1920, 1032),
    "primary": True,
}
RIGHT = {
    "number": 3,
    "handle": 33,
    "rect": (1920, 0, 4480, 1440),
    "work": (1920, 0, 4480, 1400),
    "primary": False,
}


def _resolve(screen, current, monitors):
    mon, err = mm.resolve_target_monitor(screen, current, monitors)
    return (mon["number"] if mon else None), err


def test_other_picks_the_screen_the_window_is_not_on():
    assert _resolve("other", MAIN, [LEFT, MAIN]) == (1, None)
    assert _resolve("other", LEFT, [LEFT, MAIN]) == (2, None)
    assert _resolve("", LEFT, [LEFT, MAIN]) == (2, None)


def test_numbers_and_spoken_forms():
    mons = [LEFT, MAIN, RIGHT]
    assert _resolve("2", LEFT, mons) == (2, None)
    assert _resolve("screen 3", LEFT, mons) == (3, None)
    assert _resolve("the second monitor", LEFT, mons) == (2, None)
    assert _resolve("two", LEFT, mons) == (2, None)


def test_positional_and_primary():
    mons = [MAIN, LEFT, RIGHT]
    assert _resolve("left", MAIN, mons)[0] == 1
    assert _resolve("right", MAIN, mons)[0] == 3
    assert _resolve("primary", LEFT, mons)[0] == 2


def test_errors_are_spoken_not_raised():
    assert _resolve("5", MAIN, [LEFT, MAIN])[1].startswith("There is no screen 5")
    assert _resolve("kitchen", MAIN, [LEFT, MAIN])[1].startswith("I don't know which screen")
    assert _resolve("2", MAIN, [MAIN]) == (None, "Only one screen is connected.")


def test_target_rect_keeps_relative_position_and_fits():
    # Top-left corner stays top-left on the other screen.
    assert mm._target_rect((0, 0, 800, 600), MAIN["work"], LEFT["work"]) == (-1920, 0, 800, 600)
    # A window larger than the target work area is shrunk to fit it.
    x, y, w, h = mm._target_rect((0, 0, 3000, 1300), RIGHT["work"], MAIN["work"])
    assert (w, h) == (1920, 1032) and (x, y) == (0, 0)


def test_window_control_routes_move_to_monitor(monkeypatch):
    from actions import window_control as wc

    calls = []
    monkeypatch.setattr(mm, "move_window_to_monitor", lambda t, s: calls.append((t, s)) or "ok")
    assert wc.window_control({"action": "move_to_monitor", "screen": "2"}) == "ok"
    assert calls == [("", "2")]


def test_new_action_names_do_not_collide_with_registry():
    # action_registry rewrites calls by action name; a collision would hijack them.
    from core.action_registry import canonicalize_tool_call

    assert canonicalize_tool_call("window_control", {"action": "move_to_monitor"})[0] == (
        "window_control"
    )
    assert canonicalize_tool_call("browser_control", {"action": "tab_to_monitor"})[0] == (
        "browser_control"
    )


def test_move_to_monitor_is_ungated():
    from core.safety_guard import classify_action

    risk = classify_action("window_control", {"action": "move_to_monitor"})
    assert risk["requires_confirmation"] is False


# ── PF-031: the screen number the user said wins over the model's guess ──


def test_spoken_screen_number_is_read_from_the_sentence():
    assert mm.spoken_screen("Move Brave to screen one") == "1"
    assert mm.spoken_screen("Okay, move it to screen one.") == "1"
    assert mm.spoken_screen("put it on monitor 2") == "2"
    assert mm.spoken_screen("send it to the second screen") == "2"
    assert mm.spoken_screen("move it from screen two to screen one") == "1"
    assert mm.spoken_screen("move it to the other screen") is None
    assert mm.spoken_screen("move it to the primary monitor") is None


def test_screen_one_is_never_sent_as_primary():
    # Live 2026-11-16: "move Brave to screen one" arrived as screen="primary"
    # (Windows display 2) and Era answered "already on screen 2" four times.
    args = {"action": "move_to_monitor", "title": "Brave", "screen": "primary"}
    assert mm.ground_screen("window_control", args, "Move Brave to screen one")["screen"] == "1"
    tab = {"action": "tab_to_monitor", "screen": "primary"}
    assert mm.ground_screen("browser_control", tab, "send this tab to screen one")["screen"] == "1"


def test_grounding_leaves_other_calls_alone():
    other = {"action": "move_to_monitor", "screen": "other"}
    assert mm.ground_screen("window_control", other, "move it to the other screen") is other
    main = {"action": "move_to_monitor", "screen": "primary"}
    assert mm.ground_screen("window_control", main, "move it to my main screen") is main
    focus = {"action": "focus", "title": "screen one"}
    assert mm.ground_screen("window_control", focus, "focus screen one") is focus


def test_the_router_applies_screen_grounding():
    from core.self_healing_router import route_tool_call

    tool, params = route_tool_call(
        "window_control",
        {"action": "move_to_monitor", "title": "Spotify", "screen": "primary"},
        "Okay, move it to screen one.",
    )
    assert tool == "window_control" and params["screen"] == "1"
