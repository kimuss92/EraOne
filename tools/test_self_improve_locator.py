"""`self_improve action=locate` — "where does this live, and where would it go?"

WHY IT EXISTS
  Asked to "improve the HUD interface", Era listed several app/hud/ files
  without confidence and asked the user for a filename they could not know.
  `locate` adds the two signals the repo already has — the AREA a request
  belongs to (runtime/manifest.json "subsystems") and each module's own one-line
  description — so she can answer "which file" herself.

WHAT THESE TESTS REFUSE TO LET IT BECOME
  A second, looser resolver. The first draft of `decisive()` let a description
  saying "Open ... the HUD" win the live 2026-09-29 button request for
  app/hud/browser.py: the same wrong-file mistake item 26 fixed in
  `resolve_request`. The tests below pin that request as NOT decisive.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from actions.self_improve import SAFETY_CRITICAL, resolve_request, self_improve
from actions.self_improve_locator import (
    NEW_TOOL_CHECKLIST,
    decisive,
    locate,
    pick_area,
    rank_in_area,
)

REPO = Path(__file__).resolve().parent.parent

LIVE_HUD_REQUEST = (
    "Modify the HUD to dynamically add a button whenever a new file or folder "
    "is created. Clicking this button should open the item's location in File "
    "Explorer."
)


def _manifest() -> dict:
    return json.loads((REPO / "runtime" / "manifest.json").read_text(encoding="utf-8"))


# ── the request that started this ───────────────────────────────────────────


def test_improve_the_hud_interface_names_the_api_file():
    out = locate("improve the HUD interface")
    assert "Edit app/hud/api.py" in out
    assert "file_path='app/hud/api.py'" in out, "it must say what to do next"


@pytest.mark.parametrize(
    "desc, expected",
    [
        ("improve the HUD preferences", "app/hud/settings.py"),
        ("improve the HUD health checks", "app/hud/status.py"),
        ("fix the HUD event stream", "app/hud/events.py"),
        ("improve your vision policy", "core/vision_policy.py"),
    ],
)
def test_a_feature_plus_a_part_resolves_to_that_part(desc, expected):
    assert f"Edit {expected}" in locate(desc)


# ── the floor: confident only when it has earned it ─────────────────────────


@pytest.mark.parametrize("desc", ["modify the HUD", "improve the HUD", "make it better"])
def test_a_bare_feature_name_is_never_decisive(desc):
    assert "Edit " not in locate(desc), f"{desc!r} was resolved on one word"


def test_the_live_button_request_does_not_pick_browser_py():
    """THE regression. browser.py's description says "Open ... the HUD", which
    matches the request's "open" and "HUD" — and it is the wrong file."""
    out = locate(LIVE_HUD_REQUEST)
    assert "Edit app/hud/browser.py" not in out
    assert "Edit " not in out, "this request names no part of the HUD; it must not be decisive"
    assert "app/hud/" in out, "a refusal must still list HUD files the model can choose from"


def test_descriptions_break_ties_but_cannot_make_a_pick_alone():
    ranked = [
        {"path": "a.py", "name_score": 1, "doc_score": 2, "total": 3},
        {"path": "b.py", "name_score": 1, "doc_score": 0, "total": 1},
    ]
    assert decisive(ranked) is None, "one filename word plus a doc line is not enough"
    ranked[0]["name_score"], ranked[0]["total"] = 2, 4
    assert decisive(ranked)["path"] == "a.py"


def test_a_tie_is_not_decisive():
    ranked = [
        {"path": "a.py", "name_score": 2, "doc_score": 0, "total": 2},
        {"path": "b.py", "name_score": 2, "doc_score": 0, "total": 2},
    ]
    assert decisive(ranked) is None


@pytest.mark.parametrize(
    "desc",
    [
        "improve your vision policy",
        "change the voice output device selection",
        "improve the safety guard confirmation",
        "improve the HUD api",
    ],
)
def test_locate_never_disagrees_with_the_resolver(desc):
    """If resolve_request would already say ok, locate must name the same file —
    two answers to one question is how she promises what apply refuses."""
    target, status, _ = resolve_request("", desc)
    assert status == "ok", desc
    rel = target.relative_to(REPO).as_posix()
    assert f"Edit {rel}" in locate(desc)


# ── area ────────────────────────────────────────────────────────────────────


def test_the_hud_is_an_area():
    area = pick_area("improve the HUD interface", _manifest()["subsystems"])
    assert area and area["name"].startswith("HUD")


def test_an_ambiguous_word_picks_no_area_rather_than_the_first():
    """ "browser" is in both the HUD heading and the actions description. A tie
    must fall back to the whole tree, not to whichever was listed first."""
    area = pick_area("fix the browser", _manifest()["subsystems"])
    assert area is None or not area["name"].startswith("HUD")


def test_the_area_narrows_the_candidates():
    data = _manifest()
    area = pick_area("improve the HUD interface", data["subsystems"])
    ranked = rank_in_area("improve the HUD interface", data["modules"], area)
    assert ranked and all(r["path"].startswith(("app/hud/", "ui")) for r in ranked)


# ── new abilities ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "desc",
    [
        "add a new tool that checks the weather in my city",
        "give her a new ability to read PDFs",
        "create a command to restart the router",
    ],
)
def test_a_new_ability_gets_the_checklist_not_a_guessed_file(desc):
    out = locate(desc)
    assert "NEW ability" in out
    assert "app/tool_declarations.py" in out
    assert "Edit " not in out, "no existing file is the answer for something that does not exist"
    assert "autonomous_development propose" in out
    assert "final approval" in out
    assert "features/<name>.py" in out


def test_an_edit_that_mentions_an_action_is_not_a_new_ability():
    assert "NEW ability" not in locate("add error handling to the click action in the HUD")


def test_every_checklist_path_is_real():
    """A recipe pointing at files that moved is worse than none — it reads like
    knowledge. `<name>` templates are checked by their directory."""
    for path, _why in NEW_TOOL_CHECKLIST:
        probe = REPO / (Path(path).parent if "<" in path else Path(path))
        assert probe.exists(), f"checklist names {path}, which is not in the repo"


# ── honesty about what cannot be edited ─────────────────────────────────────


def test_a_file_too_large_to_edit_is_flagged_in_the_list():
    out = locate("fix the netflix search")
    assert "netflix_control.py" in out
    assert "CANNOT edit" in out


# ── it is wired in, and it is read-only ─────────────────────────────────────


def test_the_tool_action_returns_the_locator_answer():
    desc = "improve the HUD interface"
    assert self_improve({"action": "locate", "description": desc}) == locate(desc)


def test_locate_writes_nothing(tmp_path, monkeypatch):
    import actions.self_improve as si

    monkeypatch.setattr(si, "BACKUP_ROOT", tmp_path / "backups")
    self_improve({"action": "locate", "description": "improve the HUD interface"})
    assert not (tmp_path / "backups").exists(), "a read-only action took a backup"


def test_the_guard_treats_locate_as_read_only():
    from core.safety_guard import classify_action

    verdict = classify_action(
        "self_improve", {"action": "locate", "description": "improve your vision policy"}
    )
    assert verdict["requires_confirmation"] is False
    assert verdict["risk_level"] == "LOW"


def test_applying_to_a_safety_critical_file_is_still_confirmed():
    """locate is advice. Whatever it recommends, apply to the safety layer must
    still need the user's spoken confirmation."""
    from core.safety_guard import classify_action

    rel = sorted(SAFETY_CRITICAL - {"main.py"})[0]
    verdict = classify_action("self_improve", {"action": "apply", "file_path": rel})
    assert verdict["requires_confirmation"] is True


def _dispatcher_inline_actions() -> set[str]:
    """The tuple of self_improve actions the dispatcher answers inline, read from
    the AST — the branch is inside a 3,000-line method no unit test can drive."""
    tree = ast.parse((REPO / "app" / "era_live" / "tool_dispatch.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "improve_action"
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.comparators[0], ast.Tuple)
        ):
            return {e.value for e in node.comparators[0].elts if isinstance(e, ast.Constant)}
    raise AssertionError("could not find the inline-actions check in tool_dispatch.py")


def test_the_dispatcher_answers_find_and_locate_inline():
    """`find` was classified read-only by the guard but NOT by the dispatcher, so
    it was announced as 'working on my own code in the background'. Read-only
    must be read-only in both layers."""
    assert {"find", "locate"} <= _dispatcher_inline_actions()


def test_the_refusal_points_at_locate_before_asking_the_user(tmp_path, monkeypatch):
    """The user cannot know a filename, so asking for one must be the LAST step."""
    import asyncio

    import actions.self_improve as si
    from app.era_live import tool_dispatch

    class _UI:
        def write_log(self, text=""):
            pass

    class _Era(tool_dispatch.ToolDispatchMixin):
        ui = _UI()

    monkeypatch.setattr(si, "resolve_request", lambda fp, d: (None, "ambiguous", "Which one?"))
    out = asyncio.run(
        asyncio.to_thread(_Era()._self_improve_refusal, {"action": "apply", "description": "x"})
    )
    assert 'action="locate"' in out
    assert out.index('action="locate"') < out.index("ask which file they mean")


def test_the_prompt_tells_her_to_locate_first():
    text = (REPO / "core" / "prompt.txt").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    assert 'action="locate"' in text
    assert "Never ask the user to find repository files" in text
