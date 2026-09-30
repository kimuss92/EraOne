"""Era must not say she is working on her code when she is not. (Board item 25.)

WHAT IS BEING PROTECTED
  Acceptance point 3 — a truthful self-model. Not safety: every case below was
  already SAFE, because `resolve_request` guarantees nothing is written unless
  it returns "ok" and `core/safety_guard.py` relies on exactly that invariant.
  What failed was what Era SAID about herself.

WHAT HAPPENED LIVE (2026-09-29, 15:46 and 15:50)
  Two self_improve calls were classified

      LOW: Self-improve target ambiguous — action will only report, not write

  and both answered the user

      "Working on my own code in the background — I'll back it up, verify it,
       and report the outcome."

  They wrote nothing. The proof is arithmetic, not opinion:
  `runtime/self_improve/backups/` gained exactly ONE directory that day, for the
  15:44 call. Two of three self-edits silently no-opped while announcing
  success, and the user went on to ask for a test file on the Desktop to check
  whether "it worked" — the cost of being told a thing was underway.

WHY THE LIE LIVED IN THE DISPATCHER AND NOWHERE ELSE
  `actions/self_improve.py` was already honest: non-ok status returns the
  refusal and writes nothing. But apply/rollback is BACKGROUNDED, so the
  dispatcher answered the model with a fixed optimistic string before the work
  had been attempted, let alone resolved. A promise made before the check is a
  promise that can be false — so the dispatcher now resolves first and only
  promises what it can keep.

THE SHAPE OF EVERY TEST HERE
  Two halves, and both matter: the refusal must be honest (it must NOT claim
  work, it MUST ask which file), and it must not become a new way to refuse
  real work — a resolvable target still starts, in the background, as before.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def era(tmp_path, monkeypatch):
    """A minimal Era carrying the real mixin, as the sibling reload tests do."""
    from app.era_live import tool_dispatch

    monkeypatch.setenv("ERA_HOME", str(tmp_path))
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))

    class _UI:
        def __init__(self):
            self.lines: list[str] = []

        def write_log(self, text=""):
            self.lines.append(text)

    class _Era(tool_dispatch.ToolDispatchMixin):
        def __init__(self):
            self.ui = _UI()
            self.spoken: list[str] = []

        def speak(self, text):
            self.spoken.append(text)

    return _Era()


def _refuse(era, args):
    return asyncio.run(asyncio.to_thread(era._self_improve_refusal, args))


# ── the refusal itself ──────────────────────────────────────────────────────

AMBIGUOUS = {"action": "apply", "description": "modify the HUD", "file_path": ""}


@pytest.mark.parametrize(
    "status, message",
    [
        ("ambiguous", "That feature spans several of my modules: a.py, b.py."),
        ("none", "I couldn't work out which of my files that refers to."),
        ("refused", "That file is outside my own source tree."),
    ],
)
def test_every_unresolved_status_produces_a_refusal(era, monkeypatch, status, message):
    """'ambiguous' is the one that fired live; 'none' and 'refused' are the same
    invariant and would lie in exactly the same way."""
    import actions.self_improve as si

    monkeypatch.setattr(si, "resolve_request", lambda fp, d: (None, status, message))
    out = _refuse(era, dict(AMBIGUOUS))
    assert out, f"status {status!r} was allowed to promise work"
    assert message in out, "the user is not told what to answer"


def test_the_refusal_states_plainly_that_nothing_is_running(era, monkeypatch):
    """The exact false claim from 15:46 must be impossible to produce."""
    import actions.self_improve as si

    monkeypatch.setattr(si, "resolve_request", lambda fp, d: (None, "ambiguous", "Which one?"))
    out = _refuse(era, dict(AMBIGUOUS))

    assert "NOT started" in out
    assert "nothing is running" in out
    assert "Do NOT say you are working on it" in out, (
        "the model will smooth an unqualified refusal into progress if not told otherwise"
    )
    lowered = out.lower()
    assert "working on my own code" not in lowered
    assert "i'll back it up" not in lowered


def test_the_refusal_asks_which_file_rather_than_just_apologising(era, monkeypatch):
    """A refusal the user cannot act on costs them the same turn twice."""
    import actions.self_improve as si

    monkeypatch.setattr(si, "resolve_request", lambda fp, d: (None, "ambiguous", "Which one?"))
    out = _refuse(era, dict(AMBIGUOUS))
    assert "ask which file they mean" in out


def test_the_refusal_is_logged_so_a_no_op_is_visible_afterwards(era, monkeypatch):
    """The live no-ops were only provable by counting backup directories."""
    import actions.self_improve as si

    monkeypatch.setattr(si, "resolve_request", lambda fp, d: (None, "ambiguous", "Which one?"))
    _refuse(era, dict(AMBIGUOUS))
    assert any("not started" in line.lower() for line in era.ui.lines)


# ── it must not become a new way to refuse real work ────────────────────────


def test_a_resolvable_target_is_never_refused(era, monkeypatch, tmp_path):
    """The whole point of the tool is that it edits Era's code unattended."""
    import actions.self_improve as si

    monkeypatch.setattr(si, "resolve_request", lambda fp, d: (tmp_path / "x.py", "ok", ""))
    assert _refuse(era, {"action": "apply", "file_path": "actions/open_app.py"}) == ""


@pytest.mark.parametrize("action", ["propose", "find", "list_backups", ""])
def test_read_only_actions_are_left_alone(era, monkeypatch, action):
    """They answer inline and already report accurately; pre-resolving them
    would refuse the very calls whose JOB is to resolve an unclear target."""
    import actions.self_improve as si

    def _boom(*_a, **_k):  # must never be consulted for these
        raise AssertionError("a read-only action was pre-resolved")

    monkeypatch.setattr(si, "resolve_request", _boom)
    assert _refuse(era, {"action": action, "description": "something vague"}) == ""


def test_it_fails_open_when_the_resolver_itself_breaks(era, monkeypatch):
    """This method only governs what Era SAYS. The real protection is the
    pipeline's backup/verify/rollback, so a broken resolver must not become a
    reason she can no longer work on her own code."""
    import actions.self_improve as si

    def _boom(*_a, **_k):
        raise RuntimeError("manifest unreadable")

    monkeypatch.setattr(si, "resolve_request", _boom)
    assert _refuse(era, dict(AMBIGUOUS)) == "", "a broken resolver silenced the tool"


# ── the two layers must agree, or the guard's invariant is a fiction ────────


def test_the_guard_and_the_dispatcher_read_the_same_resolver(era, monkeypatch):
    """`core/safety_guard.py` skips confirmation for a non-ok target *because*
    nothing will be written. The dispatcher's honesty rests on the identical
    call. If these two ever consult different resolvers, one of them is wrong
    about what is about to happen.
    """
    import actions.self_improve as si
    from core.safety_guard import classify_action

    seen: list[tuple[str, str]] = []

    def _spy(file_path, description):
        seen.append((file_path, description))
        return (None, "ambiguous", "Which one?")

    monkeypatch.setattr(si, "resolve_request", _spy)

    verdict = classify_action("self_improve", dict(AMBIGUOUS))
    assert verdict["requires_confirmation"] is False
    assert "will only report, not write" in verdict["reason"]

    refusal = _refuse(era, dict(AMBIGUOUS))
    assert refusal, "the guard says it will not write; the dispatcher promised work"
    assert len(seen) == 2, "one of the two layers is not consulting resolve_request"
    assert seen[0] == seen[1], "the two layers resolved DIFFERENT requests"


def test_a_real_ambiguous_request_is_refused_end_to_end(era):
    """No stubs: "modify the HUD" is the live 2026-09-29 request verbatim, and
    it must not resolve to one confident file. This is also the regression for
    item 26 — the day it starts resolving, it had better be to a HUD file."""
    from actions.self_improve import resolve_request

    target, status, _msg = resolve_request("", "modify the HUD")
    if status == "ok":
        rel = str(target).replace("\\", "/")
        assert "hud" in rel.lower(), (
            f"'modify the HUD' confidently resolved to {rel} — the item-26 defect"
        )
    else:
        assert _refuse(era, dict(AMBIGUOUS)), "unresolved, yet she would have claimed to work"
