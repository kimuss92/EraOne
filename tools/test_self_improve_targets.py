"""Which file "modify the HUD" means — and when to admit you don't know.

(Board item 26.)

WHAT HAPPENED
  On 2026-09-29 Era was asked, by voice: "Modify the HUD to dynamically add a
  button whenever a new file or folder is created. Clicking this button should
  open the item's location in File Explorer."

  She rewrote `actions/open_app.py`. No HUD file was touched, so the feature did
  not exist — and she reported it done.

TWO ROOT CAUSES, AND THE BOARD HAD THE SECOND ONE WRONG
  The board said `resolve_request()` "picked actions/open_app.py". Replaying the
  request showed something more specific:

  1. THE HUD WAS INVISIBLE. `_candidate_files()` globbed `*.py` one level deep in
     a hand-kept tuple of directories. `app/` was not in it and the glob did not
     recurse, so all 43 modules under `app/` — the whole HUD, the live-session
     layer, the tool dispatcher — were not candidates at all. The word "HUD"
     matched NOTHING in this repo. Candidates now come from
     `runtime/manifest.json`, which is generated from the code, and a module's
     SUB-package counts as one of its names, so "hud" finds `app/hud/*`.

  2. THERE WAS NO CONFIDENCE FLOOR. Scoring is "how many words of the
     description appear in the module's name". `actions/open_app.py` scored
     exactly 1 — the single word "open" — and a score of 1 with one candidate
     was returned as a confident `ok`, indistinguishable from a strong match.

WHY THE FLOOR IS 2 AND NOT A NUMBER SOMEBODY LIKED
  `test_the_confidence_floor_is_where_the_measurement_put_it` below IS the
  measurement, kept executable. Every request that resolves correctly scores 2
  or more; the one that resolved wrongly scored 1. If a future change makes a
  correct resolution score 1, that test fails and says the floor must be
  re-derived rather than quietly lowered.

WHAT A REFUSAL IS WORTH
  Downgrading to `ambiguous` is only useful because of board item 25: the
  dispatcher now turns a non-ok status into an honest "I did nothing, which file
  do you mean?" with the candidates named. Before that, ambiguous still reached
  the user as "Working on my own code in the background".
"""

from __future__ import annotations

import pytest

from actions.self_improve import _MIN_CONFIDENT_SCORE, resolve_request
from actions.self_improve_targets import (
    _candidate_files,
    _manifest_candidates,
    _name_tokens,
    find_target_files,
)

# The three descriptions Era was actually given on 2026-09-29, verbatim from
# logs/task_trace-20260929.jsonl. Not paraphrases: the wording is the input.
LIVE_HUD_REQUEST = (
    "Modify the HUD to dynamically add a button whenever a new file or folder "
    "is created. Clicking this button should open the item's location in File "
    "Explorer."
)
LIVE_FOLLOWUPS = (
    "Fix the error handling in the new HUD button's click action to ensure it "
    "correctly resolves and opens the containing folder path without reporting "
    "a 'file not found' error.",
    "Update the HUD's generated file buttons to have a sleek, modern visual "
    "style consistent with the existing HUD design. The entire file block "
    "element should be made clickable, not just the text label.",
)


# ── the request that shipped the wrong file ─────────────────────────────────


def test_the_live_hud_request_no_longer_resolves_to_open_app():
    """THE regression. It must not confidently pick a file it shares one word
    with — and if it ever does resolve this, it had better be a HUD file."""
    target, status, _msg = resolve_request("", LIVE_HUD_REQUEST)
    if status == "ok":
        rel = target.as_posix()
        assert "hud" in rel.lower(), f"still resolving the HUD request to {rel}"
    else:
        assert status == "ambiguous", status


def test_the_refusal_names_hud_files_the_user_can_choose_from():
    """A refusal is only useful if the user can answer it. Before the manifest
    fix this listed no HUD file, because none was a candidate."""
    _t, status, msg = resolve_request("", LIVE_HUD_REQUEST)
    assert status != "ok"
    assert "hud" in msg.lower(), f"the HUD is still not among the candidates: {msg}"


@pytest.mark.parametrize("desc", LIVE_FOLLOWUPS)
def test_the_two_live_followups_stay_unresolved(desc):
    """These were already `ambiguous` on the day — the guard classified them
    "will only report, not write" — and must not become confident now that the
    candidate set is 25% larger."""
    _t, status, _msg = resolve_request("", desc)
    assert status != "ok", f"a wider candidate set made this confident: {desc[:50]}"


# ── the HUD is reachable at all ─────────────────────────────────────────────


def test_the_hud_is_a_candidate_now():
    rels = set(_candidates_by_rel())
    hud = {r for r in rels if r.startswith("app/hud/")}
    assert hud, "app/hud/* is still invisible to the resolver"
    assert "app/hud/api.py" in hud


def test_the_word_hud_finds_hud_modules():
    """The exact failure: on 2026-09-29 "HUD" scored against nothing."""
    matches = find_target_files("modify the HUD")
    assert matches, "'HUD' still matches no module in this repo"
    assert all("hud" in rel for _s, rel in matches[:3]), matches[:3]


def test_a_named_hud_module_resolves_cleanly():
    """The floor must not make the HUD permanently unreachable — naming it
    plus one word of its filename has to be enough to actually get work done."""
    target, status, msg = resolve_request("", "improve the HUD api")
    assert status == "ok", f"{status}: {msg}"
    assert target.as_posix().endswith("app/hud/api.py")


def test_a_sub_package_counts_as_one_of_a_modules_names():
    tokens = _name_tokens(_candidates_by_rel()["app/hud/api.py"])
    assert "hud" in tokens and "api" in tokens


def test_the_top_level_directory_is_not_a_name():
    """ "app", "core" and "actions" would each hand a free point to a hundred
    files and drown the specific word the user said."""
    tokens = _name_tokens(_candidates_by_rel()["app/hud/api.py"])
    assert "app" not in tokens


def _candidates_by_rel():
    """Candidates keyed by repo-relative posix path (they come back absolute)."""
    from actions.self_improve_targets import REPO_ROOT

    return {p.relative_to(REPO_ROOT).as_posix(): p for p in _candidate_files()}


# ── the floor, and the measurement behind it ───────────────────────────────

# Requests that SHOULD resolve, with the file each must land on. Each one is a
# real module of this repo, so a rename breaks this test loudly rather than
# silently degrading voice-driven self-editing.
SHOULD_RESOLVE = [
    ("improve your vision policy", "core/vision_policy.py"),
    ("change the voice output device selection", "core/voice_output.py"),
    ("improve the safety guard confirmation", "core/safety_guard.py"),
    ("improve the HUD api", "app/hud/api.py"),
]

# Requests that must NOT resolve: too vague to act on, or spanning many modules.
SHOULD_REFUSE = [
    LIVE_HUD_REQUEST,
    "modify the HUD",
    "fix the netflix search",
    "make it better",
]


@pytest.mark.parametrize("desc, expected", SHOULD_RESOLVE)
def test_requests_that_should_resolve_still_do(desc, expected):
    """The floor's cost. Raising it is only free while this list stays green."""
    target, status, msg = resolve_request("", desc)
    assert status == "ok", f"{status}: {msg}"
    assert target.as_posix().endswith(expected)


@pytest.mark.parametrize("desc", SHOULD_REFUSE)
def test_requests_that_should_be_refused_are(desc):
    _t, status, _msg = resolve_request("", desc)
    assert status != "ok", f"{desc[:50]!r} was resolved confidently"


def test_the_confidence_floor_is_where_the_measurement_put_it():
    """This is the measurement, kept runnable instead of written down.

    The floor is defensible only while the two populations stay separated: every
    correct resolution scoring AT LEAST the floor, and the known-bad one scoring
    BELOW it. If a future change lands a correct resolution on a score of 1,
    this fails — and the right response is to re-derive the floor, not to lower
    it and let `open_app.py` back through.
    """
    good = [find_target_files(d)[0][0] for d, _ in SHOULD_RESOLVE]
    assert min(good) >= _MIN_CONFIDENT_SCORE, (
        f"a correct resolution now scores {min(good)}, below the floor of "
        f"{_MIN_CONFIDENT_SCORE} — re-derive the floor, do not just lower it"
    )

    bad = [s for s, rel in find_target_files(LIVE_HUD_REQUEST) if rel == "actions/open_app.py"]
    assert bad, "the wrong answer no longer even scores — re-check this measurement"
    assert bad[0] < _MIN_CONFIDENT_SCORE, (
        f"actions/open_app.py scores {bad[0]} against the live HUD request, at or "
        f"above the floor of {_MIN_CONFIDENT_SCORE} — the 2026-09-29 defect is back"
    )


# ── the manifest is the source, and it degrades safely ─────────────────────


def test_candidates_come_from_the_generated_manifest():
    """Not a hand-kept list: a new package must become reachable by voice
    without anyone remembering to edit a tuple."""
    assert len(_manifest_candidates()) > 200
    assert _manifest_candidates() == _candidate_files()


def test_the_test_suite_is_never_a_candidate():
    """`is_test` is the manifest's own classification. Era resolving "fix the
    tests" to a test file is a different and riskier feature than this one."""
    assert not [p for p in _candidate_files() if p.name.startswith("test_")]
    assert not [p for p in _candidate_files() if "/tools/" in p.as_posix()]


def test_private_packages_are_not_offered_for_a_description():
    """actions/_netflix is "Internal implementation package for
    actions/netflix_control.py" — listing its 4 files pushed the module the user
    means off the answer to "fix the netflix search"."""
    assert not [p for p in _candidate_files() if "/_" in p.as_posix()]
    matches = find_target_files("fix the netflix search")
    assert "actions/netflix_control.py" in [rel for _s, rel in matches]


def test_a_missing_manifest_falls_back_instead_of_finding_nothing(monkeypatch, tmp_path):
    """A resolver that returns no candidates would refuse every request. The
    directory walk is worse than the manifest and much better than nothing."""
    from actions import self_improve_targets as t

    monkeypatch.setattr(t, "REPO_ROOT", tmp_path)  # no runtime/manifest.json here
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "vision_policy.py").write_text("x = 1\n", encoding="utf-8")

    assert t._manifest_candidates() == []
    assert [p.name for p in t._candidate_files()] == ["vision_policy.py"]


def test_an_unreadable_manifest_does_not_raise(monkeypatch, tmp_path):
    from actions import self_improve_targets as t

    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "manifest.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(t, "REPO_ROOT", tmp_path)
    assert t._manifest_candidates() == []


def test_ranking_never_decides_whether_a_file_may_be_written():
    """This module's stated contract. The manifest widened what it can SEE, and
    that must not widen what may be EDITED — `_resolve_target` still owns every
    refusal (protected paths, size, non-Python, outside the repo)."""
    from pathlib import Path

    import actions.self_improve_targets as t

    source = Path(t.__file__).read_text(encoding="utf-8")
    for forbidden in ("write_text", "SAFETY_CRITICAL", "requires_confirmation", "shutil"):
        assert forbidden not in source, f"{forbidden} does not belong in a ranker"
