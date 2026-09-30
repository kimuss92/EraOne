"""Era answers "what can you do?" from her real tools, and refuses what is absent.

(Board item 16, acceptance point 3: a truthful self-model.)

WHY THIS IS A TOOL AND NOT A PROMPT RULE
  Protocol Zero already keeps section 16 of core/prompt.txt in step with the real
  declarations. That fixed the PROMPT. It did not make Era refuse, because a
  prompt is still text a model reads and paraphrases — and this session watched
  her assert three untrue things about herself: a tool the model is never offered
  (`spotify_bridge`), work she was already barred from doing (item 25), and a
  HUD feature that was never built (item 26). So the answer is COMPUTED, from the
  same list the live model was handed.

THE SOURCE HAS TO BE THE GATED ONE
  `actions.protocol_zero.load_declarations()` reads app/tool_declarations.py from
  disk AND applies RUNTIME_GATED_TOOLS. Reading the literal alone once reported
  53 tools where the model was offered 52, and the 53rd went into Era's prompt as
  a capability she had. `test_the_source_is_the_gated_declaration_list` is what
  keeps this module on the honest side of that.

THE TWO FAILURE DIRECTIONS, both of which are tested
  over-claiming   saying yes to something she cannot do. The expensive one: the
                  user acts on it, as they did on 2026-09-29 when they went to
                  check a HUD feature that did not exist.
  under-claiming  refusing something she CAN do. Cheaper but not free — a
                  capability she denies is a capability the user stops asking
                  for, and it is how a working tool quietly becomes dead code.
"""

from __future__ import annotations

import pytest

from core import capability_index as ci

# Real capabilities, phrased as a user would. Each must reach its tool.
CAN_DO = [
    "play some music",
    "send an email",
    "read my emails",
    "book me a flight",
    "control netflix",
    "remember something for me",
    "check the weather",
    "search the web",
    "take a screenshot",
    "set a reminder",
    "schedule a task for tomorrow",
    "change your voice",
    "open spotify",
]

# Things Era genuinely cannot do. None may come back as a confirmation.
CANNOT_DO = [
    "order a pizza",
    "mine bitcoin",
    "drive my car",
    "call my mother on the phone",
    "buy shares in Apple",
    "water my plants",
    "book a dentist appointment",
    "unlock my front door",
    "print a document",
    "translate speech in real time",
]


def _verdict(claim: str) -> str:
    head = ci.check(claim).splitlines()[0]
    if head.startswith("RELATED TOOLS FOUND"):
        return "related"
    if head.startswith("NOT CONFIRMED"):
        return "not_confirmed"
    if head.startswith("NOT SUPPORTED"):
        return "not_supported"
    return f"unexpected: {head[:60]}"


# ── the source of truth ────────────────────────────────────────────────────


def test_the_source_is_the_gated_declaration_list():
    """Not the raw literal: a withheld tool is declared in the file and never
    handed to the model, and documenting one as real is the exact bug this
    module must not reproduce."""
    from actions.protocol_zero import load_declarations

    assert [d["name"] for d in ci.declarations()] == [d["name"] for d in load_declarations()]


def test_every_offered_tool_appears_in_the_summary():
    """The summary is the answer to "what can you do?". A tool missing from it is
    a capability the user will never learn she has."""
    from app.tool_declarations import TOOL_DECLARATIONS

    text = ci.summary()
    missing = [t["name"] for t in TOOL_DECLARATIONS if t["name"] not in text]
    assert not missing, f"tools absent from her own answer: {missing}"


def test_the_summary_tells_her_not_to_go_beyond_it():
    text = ci.summary()
    assert "Answer ONLY from this list" in text
    assert "if something is not here, you cannot do it" in text


def test_the_summary_is_not_a_hand_maintained_category_table():
    """A grouping kept by hand is one more thing that drifts from the code, which
    is the problem this file exists to solve. Names and declared summaries only."""
    source = (ci.__file__ and open(ci.__file__, encoding="utf-8").read()) or ""
    for invented in ('"Media"', '"Productivity"', '"System control"'):
        assert invented not in source, f"a hand-written category table crept in: {invented}"


# ── over-claiming: the expensive direction ─────────────────────────────────


@pytest.mark.parametrize("claim", CANNOT_DO)
def test_something_she_cannot_do_is_never_confirmed(claim):
    """The headline must never read as a yes.

    Note `print a document` and `translate speech in real time` land on
    "related": `file_processor` really does declare `document` and `translate`
    actions, so a tool in the right AREA exists while the capability does not.
    That is exactly why "related" is worded as "NOT confirmation" — calling it
    "supported, probably", as the first version of this module did, would be the
    over-claiming the whole item exists to stop.
    """
    assert _verdict(claim) in ("related", "not_confirmed", "not_supported")


def test_a_wholly_unknown_request_is_refused_outright():
    answer = ci.check("mine bitcoin")
    assert answer.startswith("NOT SUPPORTED")
    assert "Tell the user plainly that you cannot do this" in answer
    assert "do not describe how you would" in answer


def test_a_related_match_is_explicitly_not_a_yes():
    answer = ci.check("print a document")
    assert "NOT confirmation that you can do it" in answer
    assert "a tool in the same area is not the same capability" in answer


def test_a_single_shared_word_is_not_evidence():
    """The item-26 lesson, applied here before it can bite: one common word is
    shared by a dozen tools and is indistinguishable from a real match."""
    assert ci._MIN_EVIDENCE >= 2
    weight, _words = ci._evidence({"name": "x", "description": "handles a document"}, {"document"})
    assert weight < ci._MIN_EVIDENCE


def test_one_word_cannot_be_counted_twice_by_pluralisation():
    """A real bug found while building this: tokens used to expand "shares" into
    {shares, share}, so ONE word in the request matched twice and pushed "buy
    shares in Apple" over the evidence floor."""
    assert ci._tokens("shares") == ci._tokens("share")
    assert len(ci._tokens("shares")) == 1
    weight, _w = ci._evidence(
        {"name": "x", "description": "share and shares"}, ci._tokens("shares")
    )
    assert weight < ci._MIN_EVIDENCE, "one concept still counts as two"


def test_stemming_does_not_maul_double_s_words():
    assert "access" in ci._tokens("access")
    assert "email" in ci._tokens("emails")


# ── under-claiming: the quieter direction ──────────────────────────────────


@pytest.mark.parametrize("claim", CAN_DO)
def test_something_she_can_do_is_not_denied(claim):
    """A capability she denies is one the user stops asking for.

    `book me a flight` is the case that caught the first version: `flight_finder`
    exists, and weighting a tool's NAME the same as its prose answered NOT
    CONFIRMED for a tool named after the thing being asked for.
    """
    assert _verdict(claim) == "related", f"{claim!r} was denied: {ci.check(claim).splitlines()[0]}"


def test_a_name_match_outweighs_a_prose_match():
    """Why: a noun shared with a tool's NAME is near-decisive; the same word
    buried in a paragraph of description means almost nothing."""
    named = ci._evidence({"name": "flight_finder", "description": "unrelated text"}, {"flight"})[0]
    prosed = ci._evidence({"name": "unrelated", "description": "mentions flight once"}, {"flight"})[
        0
    ]
    assert named >= ci._MIN_EVIDENCE > prosed


# ── it must never become a new way to fail ─────────────────────────────────


def test_an_unreadable_declaration_list_is_admitted_not_guessed(monkeypatch):
    """ "I don't know what I can do" is a legitimate thing to say out loud. Making
    something up, or crashing the turn, is not."""
    monkeypatch.setattr(
        ci, "declarations", lambda: (_ for _ in ()).throw(OSError("declarations unreadable"))
    )
    for answer in (ci.summary(), ci.check("anything at all")):
        assert "could not read my own capability list" in answer
        assert "do not" in answer.lower()


@pytest.mark.parametrize("claim", ["", "   ", "the a an my", "?!"])
def test_an_empty_or_contentless_claim_falls_back_to_the_full_list(claim):
    """ "what can you do?" arrives as no claim at all, and a question made only of
    stopwords must not read as "nothing matched, so you cannot"."""
    assert "tools you actually have right now" in ci.check(claim)


# ── the wiring: an unreachable capability is no capability ─────────────────


def test_the_tool_is_declared_to_the_model():
    from app.tool_declarations import TOOL_DECLARATIONS

    decl = next(t for t in TOOL_DECLARATIONS if t["name"] == "capability_check")
    assert "claim" in decl["parameters"]["properties"]
    assert not decl["parameters"].get("required"), "asking what she can do needs no argument"
    assert "never claim a capability it did not confirm" in decl["description"]


def test_the_dispatcher_handles_it():
    from pathlib import Path

    from app.era_live import tool_dispatch

    source = Path(tool_dispatch.__file__).read_text(encoding="utf-8")
    assert 'tool_name == "capability_check"' in source
    assert "from core.capability_index import check" in source


def test_the_prompt_sends_capability_questions_to_the_tool():
    """Without this rule the model answers from its own impression, which is the
    behaviour being replaced."""
    from pathlib import Path

    prompt = (Path(ci.__file__).parent / "prompt.txt").read_text(encoding="utf-8-sig")
    assert "capability_check" in prompt
    assert "ASK, DO NOT REMEMBER" in prompt
    assert "is NOT a yes" in prompt


def test_the_tool_is_never_gated_off():
    """A capability question must be answerable in every configuration — a
    withheld honesty tool is worse than none, because the prompt still points at
    it."""
    import ast
    from pathlib import Path

    from app.tool_declarations import __file__ as decl_file

    tree = ast.parse(Path(decl_file).read_text(encoding="utf-8-sig"))
    gates = next(
        (
            ast.literal_eval(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RUNTIME_GATED_TOOLS" for t in node.targets)
        ),
        {},
    )
    assert "capability_check" not in gates
