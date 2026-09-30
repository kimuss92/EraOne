"""core/capability_index.py — answer "what can you actually do?" from generated data.

WHY (board item 16, acceptance point 3: a truthful self-model)
  Protocol Zero keeps Era's system PROMPT in step with her real tools. That
  fixed the prompt; it did not make her refuse. A prompt is still text a model
  reads and paraphrases, and this session has already watched her state three
  untrue things about herself:

    * her prompt claimed `spotify_bridge`, a tool the model is never offered;
    * she said she was working on her own code while the safety layer had
      already decided the action would only report (item 25);
    * she reported a HUD feature done after editing an unrelated file (item 26).

  None was a crash. Each was Era being confidently wrong about herself, which is
  the failure Phase 0 exists to remove.

THE ONE IDEA HERE
  A capability answer must be COMPUTED at the moment it is given, from the same
  list the live model is actually offered — never recalled from the prompt and
  never composed from the model's impression of itself. So this module answers
  from `actions.protocol_zero.load_declarations()`, which reads
  `app/tool_declarations.py` from disk AND applies `RUNTIME_GATED_TOOLS`.

  That gate is the whole reason this file does not simply import the module or
  read the literal: a withheld tool is declared in the file and never handed to
  the model. Reading the literal alone once reported 53 tools where 52 were
  offered, and Protocol Zero wrote the 53rd into Era's prompt as a capability
  she had. One source of truth, and it is the one that already knows about
  gates.

WHAT `check()` RETURNS, AND WHY IT READS LIKE AN INSTRUCTION
  Evidence, plus a verdict the model is told not to soften. The pattern is the
  one that worked for item 25: state plainly that nothing supports the claim and
  say what to tell the user. An unqualified "no match" gets smoothed into a
  maybe; a sentence that says "tell the user you cannot do this" does not.

  It deliberately does NOT try to be clever about near-misses. Listing the tools
  that share words with the claim, with their real one-line summaries, lets the
  model see for itself whether any of them is the thing being asked about —
  which is a far safer division of labour than this module deciding.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]+")

# Words that carry no evidence about a capability. Without these, "can you" and
# "for me" match tools by accident and every question looks supported.
_STOPWORDS = frozenset(
    """the a an my your you i me it is are can could would do does did able to of in on for and
    or with when what which how any some please sir era one thing stuff use using used able
    ability capable capability tool tools feature features function functions know tell show
    help want need make made get got have has had will shall should must be been being that
    this these those there here now then very really just also actually able""".split()
)

# A claim needs more than one shared word before its top match counts as
# evidence, for the same reason self_improve's target resolution needs it: a
# single common word ("open", "control", "file") is shared by a dozen tools and
# is indistinguishable from a real match. See tools/test_capability_index.py,
# which measures both halves.
_MIN_EVIDENCE = 2


def _stem(word: str) -> str:
    """ "emails" -> "email", without a stemmer and without touching "access"."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    """Distinct CONCEPTS, canonicalised to one form each.

    The first version returned both "shares" and "share" so either would match.
    That let a single word in the request count as two pieces of evidence: "buy
    shares in Apple" matched `community_manager` twice on one word and was
    reported SUPPORTED. Canonicalising both sides instead of expanding one keeps
    the match and makes the count mean what it says.
    """
    return {_stem(w) for w in _TOKEN_RE.findall((text or "").lower()) if w not in _STOPWORDS}


def declarations() -> list[dict]:
    """The tools the live model is actually offered, gates applied. May raise —
    the caller decides what to say when Era cannot read her own capability list,
    and "I don't know what I can do" is a legitimate answer to give out loud."""
    from actions.protocol_zero import load_declarations

    return load_declarations()


def _condense(text: str, limit: int = 140) -> str:
    one_line = " ".join(str(text or "").split())
    if len(one_line) <= limit:
        return one_line
    cut = one_line[:limit].rsplit(" ", 1)[0]
    return cut + "..."


def _actions_of(decl: dict) -> list[str]:
    from actions.protocol_zero import action_tokens

    try:
        return action_tokens(decl)
    except Exception:
        return []


def _name_words(decl: dict) -> set[str]:
    """The tool's own name and action names."""
    parts = [str(decl.get("name") or ""), *_actions_of(decl)]
    return _tokens(" ".join(parts).replace("_", " "))


def _prose_words(decl: dict) -> set[str]:
    """Words from the declared summary only.

    Kept separate from the name because they are much weaker evidence. The
    summary still has to be searched — "play some music" must reach
    `spotify_control`, whose name says nothing about music — but a single word
    shared with a paragraph of prose means almost nothing, while a noun shared
    with a tool's NAME is close to decisive: "book me a flight" -> flight_finder.
    Scoring both the same was the first version of this file, and it answered
    NOT CONFIRMED for two capabilities Era demonstrably has.
    """
    return _tokens(str(decl.get("description") or "").replace("_", " "))


def _evidence(decl: dict, wanted: set[str]) -> tuple[int, list[str]]:
    """(weight, the words that matched). A name hit is worth two, so one noun in
    the tool's name clears _MIN_EVIDENCE on its own and one word buried in its
    description does not."""
    in_name = wanted & _name_words(decl)
    in_prose = (wanted & _prose_words(decl)) - in_name
    return len(in_name) * 2 + len(in_prose), sorted(in_name) + sorted(in_prose)


def summary(limit: int = 0) -> str:
    """Every tool Era actually has, one line each, for her to phrase herself.

    Names and summaries only, and no grouping invented here: a category table
    maintained by hand is one more thing that drifts from the code, which is the
    problem this whole file exists to solve. The model can group them when it
    speaks; what matters is that the list is the real one.
    """
    try:
        decls = declarations()
    except Exception as e:
        return (
            f"I could not read my own capability list ({e}). Tell the user that plainly "
            "and do not guess at what you can do."
        )
    rows = sorted(decls, key=lambda d: str(d.get("name") or ""))
    if limit > 0:
        rows = rows[:limit]
    lines = []
    for decl in rows:
        actions = _actions_of(decl)
        suffix = f"  [actions: {', '.join(actions[:8])}]" if actions else ""
        lines.append(f"- {decl.get('name')}: {_condense(decl.get('description'))}{suffix}")
    return (
        f"These are the {len(rows)} tools you actually have right now, read from your own "
        "declarations. Answer ONLY from this list. Group them naturally for the user and "
        "keep it short; if something is not here, you cannot do it.\n" + "\n".join(lines)
    )


def check(claim: str) -> str:
    """Can Era do `claim`? An answer grounded in the real declarations.

    The verdict is stated for the model rather than left implied, because a bare
    "no matches" is exactly the kind of result a model turns into "I should be
    able to do that". Item 25 established that the fix is to say what to tell
    the user.
    """
    claim = (claim or "").strip()
    if not claim:
        return summary()
    try:
        decls = declarations()
    except Exception as e:
        return (
            f"I could not read my own capability list ({e}). Tell the user you cannot "
            "confirm whether you can do this, and do not claim that you can."
        )
    wanted = _tokens(claim)
    if not wanted:
        return summary()

    scored = []
    for decl in decls:
        weight, shared = _evidence(decl, wanted)
        if weight:
            scored.append((weight, str(decl.get("name")), decl, shared))
    scored.sort(key=lambda row: (-row[0], row[1]))

    best = scored[0][0] if scored else 0
    evidence = "\n".join(
        f"- {name}: {_condense(decl.get('description'))}  (matched: {', '.join(words)})"
        for _n, name, decl, words in scored[:5]
    )

    if best >= _MIN_EVIDENCE:
        # Deliberately NOT "supported". A name-level match means a tool is in
        # the right AREA, not that it does the thing: `file_processor` declares
        # `document` and `translate` actions, so "print a document" and
        # "translate speech in real time" both land here — and Era can do
        # neither. Announcing those as supported would be the over-claiming this
        # whole module exists to stop, so the verdict says what is actually
        # known and hands the decision to the reader with the evidence.
        return (
            f"RELATED TOOLS FOUND for {claim!r} — this is NOT confirmation that you can do "
            f"it:\n{evidence}\n"
            "Read the summaries. Only say you can do this if one of them plainly does the "
            "thing that was asked; a tool in the same area is not the same capability. "
            "Otherwise tell the user you cannot, and offer the nearest thing you genuinely can."
        )
    if scored:
        return (
            f"NOT CONFIRMED for {claim!r}. Nothing you have clearly does this. The closest "
            f"are below, and each shares only one word with the request, which is not "
            f"evidence:\n{evidence}\n"
            "Tell the user you do not have a way to do this, and offer the nearest thing "
            "you genuinely can do. Do NOT say you can do it."
        )
    return (
        f"NOT SUPPORTED. You have no tool for {claim!r} — nothing in your {len(decls)} "
        "declarations relates to it at all. Tell the user plainly that you cannot do this. "
        "Do not promise it, do not say you will try, and do not describe how you would."
    )
