"""Recall by meaning, and the two things it must never cost. (Board item 15.)

ACCEPTANCE POINT 4 is "tell her something on Monday, ask on Friday after
restarts — she recalls it". Saving and persistence already worked; what failed
was recall, because the scorer matched WORDS. Measured against 12 paraphrases of
real entries, top-1 recall was 6/12, and "where do I live" returned
`live_ui_feed_plan` — a lexical hit on the token "live".

THE TWO COSTS THIS FILE GUARDS, because both are regressions waiting to happen:

  1. NOISE. An earlier fix in memory_manager.py stopped unanswerable queries
     ("showroom") from returning the five least-irrelevant entries scored on
     recency alone — the model then answered confidently from unrelated notes.
     Cosine similarity CANNOT tell an answerable query from an unanswerable one
     (measured: answerable 0.290-0.743, unanswerable 0.148-0.404 — they
     overlap, and z-score and gap-to-second overlap too). So the threshold was
     chosen as the lowest one at which noise stays at ZERO, and
     `test_an_unanswerable_query_still_returns_nothing` is the test that keeps
     it there.

  2. THE EXISTING LEXICAL HITS. Scores combine with `max`, never a sum, so an
     entry the words already found cannot be pushed down or dropped by adding
     this. The worst case of the whole feature is the behaviour before it.

WHY MOST TESTS HERE USE A FAKE EMBEDDER
  The real one is a local Ollama. A unit test that depends on whether a service
  is running is a test that fails for reasons unrelated to what it asserts (the
  suite already learned this from live browsers and the real vault). So the
  logic is tested against a stub, and the ONE test that measures real quality
  skips when the model is absent — and is the only place the measured numbers
  live.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

# This file is BOTH a pytest module and the one measurement script (see the
# __main__ block at the end). Under pytest the repo root is already importable;
# run directly as `python tools/test_semantic_recall.py` it is not, and the
# imports below would fail before reaching that block. The repo's other
# standalone tools ask for `PYTHONPATH=.` instead — this one is meant to be a
# single copy-paste command, and on Windows setting an env var first is a
# second step people forget.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import semantic_recall as sr  # noqa: E402
from memory.memory_manager import (  # noqa: E402
    _RETRIEVAL_MIN_SCORE,
    rank_memory_entries,
)

# The 12 paraphrases, and the entry each must find. The keys match the FROZEN
# FIXTURE (tools/golden/semantic_recall_store.json); test_the_fixture_stays_clean
# fails loudly if one is renamed or dropped.
PARAPHRASES = [
    ("what team do I support", "favorite_football_team"),
    ("which football club am I a fan of", "favorite_football_team"),
    ("what do I do for a living", "job"),
    ("where do I live", "current_city"),
    ("when is my birthday", "birthday"),
    ("what should you call me", "preferred_name"),
    ("which browser should you use for magnific", "magnific_browser_preference"),
    ("what song does my kid like", "son_favorite_song"),
    ("how should you greet me when you boot", "startup_greeting_preference"),
    ("what happens after you crash", "restart_rule"),
    ("what is the main design goal for the project", "era_architecture_goal"),
    ("what voice engine did we agree on", "era_voice_upgrade_vibe_voice"),
]

# Queries with no answer in the store. These must return NOTHING, not the
# least-irrelevant five.
UNANSWERABLE = [
    "showroom",
    "HUD display preferences",
    "what is my bank balance",
    "who won the 1998 world cup",
    "my dentist appointment",
    "the capital of Peru",
    "my favourite wine",
    "the office wifi password",
]

# ── The corpus these numbers are measured against ───────────────────────────
#
# WHY A FROZEN FIXTURE AND NOT THE REAL STORE (board item 36)
#   Until 2026-09-30 this measured against `load_memory()` — which conftest.py
#   seeds from the REAL `memory/*.json`. That store is gitignored, personal, and
#   changes every time Era learns something, so the one test carrying acceptance
#   point 4's numbers could not be reproduced on another machine, could not be
#   re-measured a year later, and went red on 2026-09-30 without a line of
#   relevant code changing: entries added since the calibration had pushed one
#   target out of the top 3. A measurement whose corpus drifts is not a
#   measurement.
#
#   The fixture is SYNTHETIC on purpose. Committing the real store would make
#   the test stable by putting Karim's birthday, city and family details in the
#   repository, which is a worse trade than the flakiness it fixes.
#
# SYNTHETIC MEANS "NOT HIS", NOT "NOT REAL" — learned the hard way, 2026-09-30
#   The first version used invented proper nouns: current_city "Rivermouth",
#   favorite_football_team "Northgate Rovers". An embedding model has never seen
#   those, so they embed as noise and semantic recall could NEVER reach them —
#   "where do I live" returned nothing but the lexical trap. The real store has
#   "Tunis", which the model knows IS a city, and that is precisely why recall
#   worked there. Values must therefore be real-world terms the model has seen,
#   chosen so they are not Karim's: Bristol, not Rivermouth; Ajax, not a club he
#   follows.
#
#   The same run showed the second half of the rule. `job` was "runs a
#   fitted-kitchen and wardrobe studio" — a business PREMISES, which sits almost
#   on top of "showroom", one of the 8 queries that must return NOTHING. It
#   leaked noise 1/8 while still failing "what do I do for a living". The
#   banned-topic scan in test_the_fixture_stays_clean only catches the literal
#   WORD; nothing here can catch semantic proximity without a model, so a value
#   near an UNANSWERABLE topic is caught only by the measurement run itself.
#   That is why the runner prints "NOISE (must be empty)" rather than hiding it.
#
#   Live recall against his ACTUAL memory is still covered — by board item 30b,
#   the acceptance test a unit test cannot make: tell her on Monday, ask on
#   Friday, after restarts.
FIXTURE_PATH = Path(__file__).resolve().parent / "golden" / "semantic_recall_store.json"

# The old live-store numbers, kept for the record. They are NOT carried over:
# they were measured against a different corpus, so reusing them here would be
# fiction in the opposite direction from lowering them.
LIVE_STORE_TOP1_2026_09_29 = 9  # of 12, against the real store, at COSINE_CUT 0.36
LIVE_STORE_TOP3_2026_09_29 = 10

# Floors for the FIXTURE. MEASURED 2026-10-01 on Karim's Windows machine with
# Ollama serving all-minilm at COSINE_CUT 0.36, against the 50-entry corpus:
#
#     top-1 8/12    top-3 11/12    noise 0/8
#
# Run 1 that day measured 7/12 and 10/12 with **noise 1/8** ('showroom' -> job)
# and was NOT banked: a floor taken from a run that also reported noise is a
# number measured under a condition the noise test forbids. Two fixture values
# were fixed first (see the header above on invented proper nouns), the corpus's
# words-only difficulty was re-checked as unchanged, and only then were these
# taken from a clean run.
#
# `>=`, not `==`: a better embedding model may push these up and that must not
# be a failure. Falling BELOW them, on a corpus that cannot drift, is a real
# regression rather than the store having moved underneath the test.
FIXTURE_TOP1: int | None = 8
FIXTURE_TOP3: int | None = 11

LEXICAL_TOP1 = 6  # the REAL store's words-only baseline, kept for the record

# The fixture's OWN words-only baseline. Measured 2026-09-30 in the cloud
# container with ERA_SEMANTIC_RECALL=0 — no model, no Ollama, so unlike the two
# floors above it this number is reproducible ANYWHERE and is pinned by
# test_the_fixture_is_hard_enough_without_a_model below.
#
# It is the bar semantic recall has to beat, and it is the evidence the corpus
# is hard in the right way: 5/12 by words alone, with every trap firing —
# "where do I live" returns ONLY live_ui_feed_plan, "when is my birthday" ranks
# birthday_card_supplier above birthday, and "what do I do for a living" returns
# nothing at all. That last one is the case only meaning can rescue.
FIXTURE_LEXICAL_TOP1 = 5
FIXTURE_LEXICAL_TOP3 = 10

# What semantic recall is worth on this corpus, measured rather than asserted:
#     words only   top-1 5/12   top-3 10/12
#     + semantic   top-1 8/12   top-3 11/12
# +3 on top-1, which is the same gain the REAL store showed (6 -> 9) before the
# measurement was moved off it. That agreement is the evidence the synthetic
# corpus is a fair stand-in and not merely an easier one.


def load_fixture() -> dict:
    """The frozen synthetic store. Read fresh each call: a test that mutated a
    shared dict would silently change every later measurement."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def measure(memory: dict) -> dict:
    """Run the 12 paraphrases and the 8 unanswerable queries over `memory`.

    One function so the pytest assertions and the __main__ report can never
    measure different things — the numbers Karim pastes back are the numbers the
    test will then assert.
    """
    top1 = top3 = 0
    missed_top1: list[str] = []
    missed_top3: list[str] = []
    for query, want in PARAPHRASES:
        keys = [
            e["key"]
            for e in rank_memory_entries(memory, query, top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
        ]
        top1 += keys[:1] == [want]
        top3 += want in keys[:3]
        if keys[:1] != [want]:
            missed_top1.append(f"{query!r} -> {keys[:5]} (wanted {want})")
        if want not in keys[:3]:
            missed_top3.append(f"{query!r} -> {keys[:5]} (wanted {want})")

    noise = {}
    for query in UNANSWERABLE:
        ranked = rank_memory_entries(memory, query, top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
        if ranked:
            noise[query] = [e["key"] for e in ranked]

    return {
        "entries": sum(
            1
            for items in memory.values()
            if isinstance(items, dict)
            for entry in items.values()
            if isinstance(entry, dict) and "value" in entry
        ),
        "top1": top1,
        "top3": top3,
        "missed_top1": missed_top1,
        "missed_top3": missed_top3,
        "noise": noise,
    }


@pytest.fixture(autouse=True)
def _fresh_probe():
    """The availability answer is cached for a minute; no test may inherit it."""
    sr.reset_probe()
    yield
    sr.reset_probe()


@pytest.fixture
def enabled(monkeypatch):
    """Turn the feature back on (conftest.py disables it suite-wide)."""
    monkeypatch.delenv("ERA_SEMANTIC_RECALL", raising=False)


# ── off by default, and silent when it cannot work ──────────────────────────


def test_it_is_off_for_the_test_suite_by_default():
    """Otherwise thousands of unrelated tests reach a real local service."""
    assert os.environ.get("ERA_SEMANTIC_RECALL") == "0"
    assert sr.available() is False


def test_the_off_switch_beats_a_reachable_server(enabled, monkeypatch):
    monkeypatch.setenv("ERA_SEMANTIC_RECALL", "0")
    monkeypatch.setattr(sr, "embed", lambda *a, **k: np.ones((1, 4), dtype=np.float32))
    assert sr.available() is False


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda *a, **k: None, id="server_down_or_timeout"),
        pytest.param(lambda *a, **k: (_ for _ in ()).throw(OSError("refused")), id="raises"),
    ],
)
def test_recall_falls_back_to_words_when_embedding_fails(enabled, monkeypatch, failure):
    """Memory must not become the thing that breaks when a local service is
    down. This is the single most important test in the file: its failure mode
    is Era losing her memory, not merely recalling it less well."""
    monkeypatch.setattr(sr, "embed", failure)
    assert sr.semantic_scores("anything", [("c", "k", "text")]) == {}


def test_a_broken_semantic_layer_cannot_break_the_ranker(enabled, monkeypatch):
    """The ranker wraps this in try/except on purpose; assert that, don't trust it."""
    from memory import memory_manager

    def _explode(*_a, **_k):
        raise RuntimeError("semantic layer is broken")

    monkeypatch.setattr(sr, "semantic_scores", _explode)
    ranked = rank_memory_entries(
        {"identity": {"name": {"value": "Karim"}}}, "what is my name", top_k=5
    )
    assert [e["key"] for e in ranked] == ["name"], "a broken optional layer took recall with it"


def test_prompt_injection_never_embeds(enabled, monkeypatch):
    """A PERFORMANCE invariant, and the one most likely to regress silently.

    `format_memory_for_prompt_ranked` runs inside every `_build_config()` — so
    on every reconnect, which is roughly every 2.5 minutes — and it ranks once
    PER ephemeral category. If it ever passed a query, one prompt build would
    embed the same string ~7 times, and a hanging Ollama would stall prompt
    construction behind seven 2.5 s timeouts. It passes no query today, so
    rank_memory_entries takes its pure-recency branch; this test is what notices
    if that changes.
    """
    from memory import memory_manager

    calls: list[str] = []
    monkeypatch.setattr(sr, "semantic_scores", lambda q, items: calls.append(q) or {})
    memory = {
        "identity": {"name": {"value": "Karim"}},
        "notes": {"a_note": {"value": "something"}},
        "projects": {"a_project": {"value": "something else"}},
    }
    assert memory_manager.format_memory_for_prompt_ranked(memory, top_k=15).strip()
    assert calls == [], f"prompt injection embedded {len(calls)} time(s) per build"


# ── the combination rule ───────────────────────────────────────────────────


def _stub_scores(monkeypatch, mapping):
    monkeypatch.setattr(
        sr, "semantic_scores", lambda q, items: {(c, k): mapping.get(k, 0.0) for c, k, _t in items}
    )


def test_a_lexical_hit_is_never_lost_to_a_semantic_zero(enabled, monkeypatch):
    """`max`, not a sum, and not a replacement. The whole no-regression claim."""
    _stub_scores(monkeypatch, {})  # semantic contributes nothing at all
    memory = {"identity": {"name": {"value": "Karim"}, "city": {"value": "Tunis"}}}
    ranked = rank_memory_entries(memory, "what is my name", top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
    assert [e["key"] for e in ranked][:1] == ["name"]


def test_a_semantic_score_can_rescue_an_entry_the_words_missed(enabled, monkeypatch):
    """The point of the feature: "living" shares no token with "job"."""
    memory = {"identity": {"job": {"value": "video editor"}}}
    lexical = rank_memory_entries(
        memory, "what do I do for a living", top_k=5, min_score=_RETRIEVAL_MIN_SCORE
    )
    assert lexical == [], "the premise is wrong — words already found this"

    _stub_scores(monkeypatch, {"job": 0.9})
    rescued = rank_memory_entries(
        memory, "what do I do for a living", top_k=5, min_score=_RETRIEVAL_MIN_SCORE
    )
    assert [e["key"] for e in rescued] == ["job"]
    assert rescued[0]["matched_by"] == "meaning", "how it was found must be visible"


def test_a_weak_semantic_score_cannot_drag_a_strong_lexical_hit_down(enabled, monkeypatch):
    _stub_scores(monkeypatch, {"name": 0.2})
    memory = {"identity": {"name": {"value": "Karim"}}}
    with_semantic = rank_memory_entries(memory, "what is my name", top_k=5)[0]["score"]
    _stub_scores(monkeypatch, {})
    without = rank_memory_entries(memory, "what is my name", top_k=5)[0]["score"]
    assert with_semantic >= without


# ── the threshold, and what it is protecting ───────────────────────────────


def test_a_cosine_below_the_cut_scores_exactly_zero():
    """0.404 was reached by a query with NO answer in the store, so anything
    under the cut is noise by measurement, not a small amount of signal."""
    assert sr.score(sr.COSINE_CUT) == 0.0
    assert sr.score(sr.COSINE_CUT - 0.01) == 0.0
    assert sr.score(0.30) == 0.0
    assert sr.score(sr.COSINE_CUT + 0.01) > 0.0
    assert sr.score(0.99) == 1.0


def test_the_cut_stays_above_the_measured_noise_ceiling():
    """The number is a noise budget. An unanswerable query reached cosine 0.404
    against this store; the cut must not drop under that without the sweep in
    memory/semantic_recall.py being re-run and this file's numbers updated."""
    assert sr.COSINE_CUT >= 0.36, (
        "lowering the cut readmits the 'confident-looking noise' an earlier fix "
        "removed — re-run the recall/noise sweep before changing it"
    )


def test_the_key_is_embedded_with_the_value():
    """ "APT" alone is findable by nothing; "son favorite song: APT" is findable
    by "what song does my kid like"."""
    assert sr.entry_text("son_favorite_song", "APT") == "son favorite song: APT"


# ── the cache ──────────────────────────────────────────────────────────────


def test_the_cache_lands_in_the_test_memory_dir_not_the_real_one():
    """ERA_MEMORY_DIR is what keeps tests out of the user's live store, and this
    cache has to obey it too — the suite has been burned by that before."""
    assert str(sr._cache_path()).startswith(os.environ["ERA_MEMORY_DIR"])
    assert "era-test-memory-" in str(sr._cache_path())


def test_text_is_embedded_once_and_then_reused(enabled, monkeypatch):
    calls: list[list[str]] = []

    def _embed(texts, **_k):
        calls.append(list(texts))
        return np.eye(len(texts), 4, dtype=np.float32)

    monkeypatch.setattr(sr, "embed", _embed)
    sr._vectors_for(["alpha", "beta"])
    assert calls == [["alpha", "beta"]]
    sr._vectors_for(["alpha", "beta"])
    assert len(calls) == 1, "the cache was not used"
    sr._vectors_for(["alpha", "beta", "gamma"])
    assert calls[-1] == ["gamma"], "only the new text should be embedded"


def test_changing_the_model_invalidates_the_cache(enabled, monkeypatch):
    """Vectors from two different models live in different spaces. Mixing them
    would look like a quality regression rather than a stale cache."""
    monkeypatch.setattr(sr, "embed", lambda texts, **_k: np.eye(len(texts), 4, dtype=np.float32))
    sr._vectors_for(["shared text"])
    first = set(sr._load_cache())
    monkeypatch.setattr(sr, "MODEL", "some-other-model")
    sr._vectors_for(["shared text"])
    assert set(sr._load_cache()) - first, "the same text under a new model reused the old vector"


def test_a_corrupt_cache_is_ignored_rather_than_fatal(enabled):
    sr._cache_path().write_text("{not json", encoding="utf-8")
    assert sr._load_cache() == {}


def test_the_cache_keeps_no_versioned_backups(enabled, monkeypatch):
    """safe_write_json backs up every write, which is right for long_term.json
    and wrong for a cache: one backup per new memory entry, forever, of data
    that can always be recomputed."""
    monkeypatch.setattr(sr, "embed", lambda texts, **_k: np.eye(len(texts), 4, dtype=np.float32))
    for i in range(3):
        sr._vectors_for([f"text {i}"])
    backups = list(sr._cache_path().parent.glob("embeddings_cache*.bak*"))
    assert not backups, f"the cache is accumulating backups: {backups}"


def test_the_cache_file_is_valid_json_with_its_model_recorded(enabled, monkeypatch):
    monkeypatch.setattr(sr, "embed", lambda texts, **_k: np.eye(len(texts), 4, dtype=np.float32))
    sr._vectors_for(["something"])
    raw = json.loads(sr._cache_path().read_text(encoding="utf-8"))
    assert raw["model"] == sr.MODEL and raw["vectors"]


# ── the real thing: quality, measured, skipped when the model is absent ────


def _model_present() -> bool:
    os.environ.pop("ERA_SEMANTIC_RECALL", None)
    try:
        sr.reset_probe()
        return sr.available()
    finally:
        os.environ["ERA_SEMANTIC_RECALL"] = "0"
        sr.reset_probe()


requires_model = pytest.mark.skipif(
    not _model_present(),
    reason=f"local embedding model {sr.MODEL!r} unavailable (ollama pull {sr.MODEL})",
)


@requires_model
def test_measured_recall_beats_the_lexical_baseline(enabled):
    """THE acceptance-point-4 measurement, now against the frozen fixture.

    Asserted as a floor, not an equality: a better model may push it up and that
    must not be a failure. Falling back to the lexical baseline, however, means
    the feature has silently stopped working.

    SKIPS while the fixture floors are unmeasured. Asserting a number nobody has
    observed would be worse than measuring nothing — it is how the previous
    floors ended up describing a corpus that no longer existed.
    """
    m = measure(load_fixture())
    where = f"{m['entries']} entries in the frozen fixture ({FIXTURE_PATH.name})"

    assert m["top1"] > FIXTURE_LEXICAL_TOP1, (
        f"top-1 recall is {m['top1']}/12, no better than this fixture's words-only "
        f"baseline of {FIXTURE_LEXICAL_TOP1} — semantic recall has stopped "
        f"contributing. {where}. Misses: {m['missed_top1']}"
    )

    if FIXTURE_TOP1 is None or FIXTURE_TOP3 is None:
        pytest.skip(
            f"fixture floors not measured yet — top-1 {m['top1']}/12, top-3 {m['top3']}/12 "
            f"here. Run `python tools/test_semantic_recall.py` on a machine with Ollama "
            f"and bake the numbers (board item 36)."
        )

    assert m["top1"] >= FIXTURE_TOP1, (
        f"top-1 recall fell to {m['top1']}/12 from {FIXTURE_TOP1}. {where}. "
        f"The corpus is frozen, so unlike the old live-store measurement this IS a "
        f"regression, not drift. Misses: {m['missed_top1']}"
    )
    assert m["top3"] >= FIXTURE_TOP3, (
        f"top-3 recall fell to {m['top3']}/12 from {FIXTURE_TOP3}. {where}. "
        f"Each line shows the top 5 actually returned: {m['missed_top3']}"
    )


@requires_model
def test_an_unanswerable_query_still_returns_nothing(enabled):
    """The cost side of the same coin, and the reason the cut is 0.36 and not
    0.25 (which scored 10/12 but returned noise for 3 of these 8).

    Against the fixture this is also a check ON THE FIXTURE: adding an entry that
    happens to answer one of these eight would weaken the noise guard silently,
    which is why test_the_fixture_stays_clean pins the topics out of the file.
    """
    noise = measure(load_fixture())["noise"]
    assert not noise, (
        "unanswerable queries returned confident-looking noise, which is exactly "
        f"what the relevance floor was added to stop: {noise}"
    )


@requires_model
def test_recall_is_fast_enough_to_sit_in_a_conversation(enabled):
    """It runs while the user is waiting for an answer. The entry index is
    cached, so the per-query cost is one embed (~46 ms measured) plus the
    lexical pass."""
    import time

    memory = load_fixture()
    rank_memory_entries(memory, "warm the index", top_k=5)  # pay the one-time build
    start = time.monotonic()
    for query, _ in PARAPHRASES:
        rank_memory_entries(memory, query, top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
    per_query = (time.monotonic() - start) / len(PARAPHRASES)
    assert per_query < 1.0, f"{per_query * 1000:.0f} ms per recall is too slow to speak over"


# ── the fixture itself is under test (board item 36) ────────────────────────


def test_the_fixture_stays_clean():
    """The fixture is only worth freezing if it stays synthetic and complete.

    Every rule here is one that, if broken, would quietly weaken a measurement
    rather than fail loudly: real data creeping in makes the corpus personal
    again, a missing target key makes a paraphrase unanswerable by construction,
    and an entry that happens to answer one of the 8 UNANSWERABLE queries turns
    the noise guard into a test that cannot fail.
    """
    store = load_fixture()
    entries = {
        (cat, key): entry
        for cat, items in store.items()
        for key, entry in items.items()
        if isinstance(entry, dict)
    }
    assert len(entries) >= 40, "too small a corpus for top-3 to mean anything"

    wanted_keys = {want for _q, want in PARAPHRASES}
    present = {key for _cat, key in entries}
    assert wanted_keys <= present, f"fixture is missing targets: {sorted(wanted_keys - present)}"

    # The trap that made "where do I live" return a UI note before item 30.
    assert "live_ui_feed_plan" in present, "the lexical trap is gone; the corpus got easier"

    blob = json.dumps(store).lower()
    for topic in ("showroom", "hud", "bank", "world cup", "dentist", "peru", "wine", "wifi"):
        assert topic not in blob, (
            f"fixture answers the UNANSWERABLE query about {topic!r}, which silently "
            "weakens test_an_unanswerable_query_still_returns_nothing"
        )

    stamps = {e.get("updated") for e in entries.values()}
    counts = {e.get("access_count") for e in entries.values()}
    assert len(stamps) == 1 and counts == {0}, (
        "recency and access_count are TIEBREAKERS in score_memory_entry; holding "
        "them constant is what keeps this a measurement of relevance alone"
    )


def test_injection_shaped_memory_is_data_not_instruction(enabled, monkeypatch):
    """A memory entry holding injection text must travel as DATA.

    Distinct from test_prompt_injection_never_embeds above, which despite its
    name is a PERFORMANCE invariant about the memory -> prompt path. This one is
    about content: Era embeds whatever the user told her to remember, and some
    of that could be hostile text copied from a web page or an email.

    What is actually assertable without a model: the text reaches the embedder
    verbatim as a string argument (so it is being embedded, never interpreted),
    and it does not hijack an unrelated query.
    """
    store = load_fixture()
    injection = store["notes"]["injection_probe"]["value"]
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in injection, "the probe lost its teeth"

    # It is embedded as "<key>: <value>" and nothing else — no interpretation.
    text = sr.entry_text("injection_probe", injection)
    assert text == f"injection probe: {injection}"

    seen: list[str] = []

    def _capture(texts, **_kw):
        seen.extend(texts)
        return np.zeros((len(texts), 4), dtype=np.float32)

    monkeypatch.setattr(sr, "embed", _capture)
    sr.semantic_scores("anything at all", [("notes", "injection_probe", text)])
    assert any(injection in s for s in seen), "the entry never reached the embedder as text"
    assert all(isinstance(s, str) for s in seen), "something other than a string was embedded"

    # And it does not hijack a query about something else entirely.
    _stub_scores(monkeypatch, {})
    ranked = rank_memory_entries(
        store, "what should you call me", top_k=5, min_score=_RETRIEVAL_MIN_SCORE
    )
    assert "injection_probe" not in [e["key"] for e in ranked]


if __name__ == "__main__":
    # `python tools/test_semantic_recall.py` — the ONE measurement run.
    #
    # Prints what the pytest floors will assert, using the same measure()
    # the test uses, so the numbers pasted back cannot disagree with the ones
    # that get baked in. Needs Ollama running with the embedding model.
    import tempfile

    # Keep the fixture's embedding cache out of the real memory/ directory:
    # get_memory_dir() reads this on every call (core/utils.py).
    os.environ["ERA_MEMORY_DIR"] = tempfile.mkdtemp(prefix="era-fixture-measure-")
    os.environ.pop("ERA_SEMANTIC_RECALL", None)
    sr.reset_probe()

    # ── Absorb the cold start, and diagnose honestly if it still fails ──────
    #
    # `ollama list` does NOT load a model into memory, so the first embed after
    # a restart (or after Ollama's idle unload) pays a cold load. Era's own
    # measured figure for that is ~4s — see the docstring of
    # core/vision_config.resolve_ollama_timeout — against available()'s
    # _PROBE_TIMEOUT_S of 1.5s. The probe therefore reports "unavailable" for a
    # model that is installed and a server that is answering, which is exactly
    # what happened on Karim's machine on 2026-09-30.
    #
    # This runner is a one-off measurement, so it can simply wait. Production
    # cannot, and that is a separate defect (board item 41) — NOT patched here,
    # because changing a live timeout to unblock a measurement is how a
    # measurement starts changing the thing it measures.
    print(f"warming {sr.MODEL!r} at {sr._base_url()} (a cold load takes a few seconds)...")
    if sr.embed(["warm up the model"], timeout_s=90.0) is None:
        # _post() collapses refused / 404 / timeout / bad-JSON into one None, so
        # ask the server directly and report which it actually was.
        import urllib.error
        import urllib.request

        url = f"{sr._base_url()}/api/embed"
        body = json.dumps({"model": sr.MODEL, "input": ["probe"]}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90.0) as resp:
                detail = f"HTTP {resp.status} but the reply had no usable 'embeddings' array"
        except urllib.error.HTTPError as e:
            detail = (
                f"HTTP {e.code} from {url}\n  {e.read()[:300].decode('utf-8', 'replace')}\n"
                f"  (a 404 here usually means an Ollama too old for /api/embed — "
                f"check `ollama --version`)"
            )
        except urllib.error.URLError as e:
            detail = (
                f"cannot reach {url}: {e.reason}\n"
                f"  Era resolves the base URL from config `ollama_base_url` / env "
                f"OLLAMA_BASE_URL, NOT from OLLAMA_HOST that the CLI may be using."
            )
        except Exception as e:  # noqa: BLE001 — a diagnostic must never hide the cause
            detail = f"{type(e).__name__}: {e}"
        raise SystemExit(f"\ncannot embed with {sr.MODEL!r}:\n  {detail}\n")

    sr.reset_probe()  # the warm model should now pass the 1.5s probe

    report = measure(load_fixture())
    print(f"\nfixture      : {FIXTURE_PATH}")
    print(f"entries      : {report['entries']}")
    print(f"model        : {sr.MODEL}   cosine cut: {sr.COSINE_CUT}")
    print(f"\ntop-1 recall : {report['top1']}/12")
    print(f"top-3 recall : {report['top3']}/12")
    print(f"noise        : {len(report['noise'])}/8 unanswerable queries returned something")
    if report["missed_top1"]:
        print("\ntop-1 misses:")
        for line in report["missed_top1"]:
            print(f"  {line}")
    if report["noise"]:
        print("\nNOISE (must be empty):")
        for query, keys in report["noise"].items():
            print(f"  {query!r} -> {keys}")
    print(
        f"\n--> paste these back: FIXTURE_TOP1 = {report['top1']}, "
        f"FIXTURE_TOP3 = {report['top3']}, noise = {len(report['noise'])}/8\n"
    )


def test_the_fixture_is_hard_enough_without_a_model():
    """The half of the measurement that needs NO Ollama, and therefore runs in
    every gate on every machine.

    conftest.py sets ERA_SEMANTIC_RECALL=0 suite-wide and this test deliberately
    does NOT take the `enabled` fixture, so it measures words only. That makes it
    deterministic everywhere — which is the whole point of freezing the corpus.

    It pins the DIFFICULTY. If someone later adds an entry that hands a target
    away lexically, the semantic floors would rise for a reason that has nothing
    to do with semantic recall, and the measurement would quietly stop measuring
    what it claims to.
    """
    m = measure(load_fixture())
    assert (m["top1"], m["top3"]) == (FIXTURE_LEXICAL_TOP1, FIXTURE_LEXICAL_TOP3), (
        f"the fixture's words-only difficulty moved: top-1 {m['top1']}/12, "
        f"top-3 {m['top3']}/12. Misses: {m['missed_top1']}"
    )
    assert not m["noise"], f"the corpus answers an UNANSWERABLE query lexically: {m['noise']}"


def test_the_traps_actually_trap():
    """Named individually, because 5/12 could also be reached by a corpus that is
    hard by accident. These are the specific failures item 30 was built to fix."""
    store = load_fixture()

    def keys(query):
        return [
            e["key"]
            for e in rank_memory_entries(store, query, top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
        ]

    # The original bug verbatim: a lexical hit on "live" beats the real answer.
    assert keys("where do I live") == ["live_ui_feed_plan"]
    # The decoy outranks the target on words alone.
    assert keys("when is my birthday")[0] == "birthday_card_supplier"
    assert keys("what song does my kid like")[0] == "song_licensing_rules"
    # And the case words cannot reach at all.
    assert keys("what do I do for a living") == []


# ── board item 41: the cold model must heal itself, not stall the turn ───────
#
# The bug: Ollama unloads an idle model after five minutes, a cold load costs
# ~4s, and the probe allowed 1.5s. So the first recall after any pause timed
# out, dropped to word-matching, cached that for a minute, and did it again --
# forever, and silently. These pin all three halves of the fix: the model stays
# loaded, a cold one is warmed OFF the conversational path, and the drop is
# no longer invisible.


@pytest.fixture
def no_threads(monkeypatch):
    """Run the warm-up inline so the test is deterministic.

    A real thread would make every assertion below a race. Captures the thread
    objects too, because "is it a daemon" is itself a thing worth asserting --
    a non-daemon warm-up waiting on a dead Ollama would hold Era's shutdown open.
    """
    made = []

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None, **kw):
            self.target, self.name, self.daemon = target, name, daemon
            made.append(self)

        def start(self):
            self.target()

    monkeypatch.setattr(sr.threading, "Thread", FakeThread)
    return made


def _posts(monkeypatch, answer):
    """Record every call into Ollama. `answer(path, payload, timeout)` decides
    what comes back, so a test can make the model cold, warm, or absent."""
    seen = []

    def fake_post(path, payload, timeout_s):
        seen.append({"path": path, "payload": payload, "timeout": timeout_s})
        return answer(path, payload, timeout_s)

    monkeypatch.setattr(sr, "_post", fake_post)
    return seen


_ONE_VECTOR = {"embeddings": [[1.0, 0.0, 0.0, 0.0]]}


def test_every_embed_asks_ollama_to_keep_the_model_loaded(monkeypatch):
    """THE root-cause fix. Without keep_alive the model is gone five minutes
    after the last question, which is the whole of board item 41."""
    seen = _posts(monkeypatch, lambda *_a: _ONE_VECTOR)
    sr.embed(["anything"])
    assert seen[0]["payload"]["keep_alive"] == sr.KEEP_ALIVE
    assert sr.KEEP_ALIVE, "an empty keep_alive silently restores the 5-minute unload"


def test_the_keep_alive_window_is_configurable(monkeypatch):
    """Karim runs several local models on one 8 GB card. If holding even a 46 MB
    model is ever the wrong trade, it must be changeable without a code edit."""
    monkeypatch.setattr(sr, "KEEP_ALIVE", "-1")
    seen = _posts(monkeypatch, lambda *_a: _ONE_VECTOR)
    sr.embed(["anything"])
    assert seen[0]["payload"]["keep_alive"] == "-1"


def test_a_cold_probe_warms_the_model_off_the_conversational_path(monkeypatch, enabled, no_threads):
    """The heart of it. The probe keeps its short budget -- a turn must never
    wait 4s for memory -- and the real cold-load budget is spent in the
    background instead."""
    calls = []

    def answer(_path, _payload, timeout_s):
        calls.append(timeout_s)
        # Cold: too slow for the probe's budget, fine for the warm-up's.
        return _ONE_VECTOR if timeout_s >= sr._WARM_TIMEOUT_S else None

    _posts(monkeypatch, answer)
    assert sr.available() is False, "a cold model must not stall the turn"
    assert calls == [sr._PROBE_TIMEOUT_S, sr._WARM_TIMEOUT_S]
    assert sr._WARM_TIMEOUT_S > sr._PROBE_TIMEOUT_S


def test_a_successful_warm_up_clears_the_cached_unavailable(monkeypatch, enabled, no_threads):
    """Otherwise the fix does nothing for a minute: the model would be loaded
    and ready while recall kept reading a stale 'unavailable' off the cache."""
    _posts(
        monkeypatch,
        lambda _p, _pl, t: _ONE_VECTOR if t >= sr._WARM_TIMEOUT_S else None,
    )
    sr.available()
    assert sr._probe is None, "the warm-up left the stale answer in place"


def test_the_warm_up_runs_as_a_daemon(monkeypatch, enabled, no_threads):
    """A warm-up still waiting on a dead Ollama must not hold Era's exit open."""
    _posts(monkeypatch, lambda *_a: None)
    sr.available()
    assert no_threads and no_threads[0].daemon is True


def test_only_one_warm_up_runs_at_a_time(monkeypatch, enabled):
    """Without the guard, every probe during an outage starts another thread and
    a long outage quietly piles them all onto the same unreachable socket."""
    started = []

    class NeverFinishes:
        def __init__(self, target=None, name=None, daemon=None, **kw):
            started.append(name)

        def start(self):
            pass  # deliberately never runs, so _warming stays set

    monkeypatch.setattr(sr.threading, "Thread", NeverFinishes)
    _posts(monkeypatch, lambda *_a: None)
    for _ in range(5):
        sr.reset_probe()
        sr.available()
    assert len(started) == 1


def test_a_warm_up_that_throws_never_escapes(monkeypatch, enabled, no_threads):
    """This runs beside a live conversation. An exception out of it would land
    somewhere nobody is catching."""

    def boom(*_a, **_k):
        raise RuntimeError("ollama exploded")

    monkeypatch.setattr(sr, "embed", lambda texts, **k: None if k else boom())
    monkeypatch.setattr(sr, "_post", lambda *_a: None)
    sr.available()  # must not raise


def test_the_off_switch_never_starts_a_warm_up(monkeypatch, no_threads):
    """conftest.py disables this suite-wide. If the off switch still spawned
    warm-up threads, every one of the thousands of memory tests would reach for
    a real local service."""
    monkeypatch.setenv("ERA_SEMANTIC_RECALL", "0")
    _posts(monkeypatch, lambda *_a: None)
    assert sr.available() is False
    assert no_threads == []


def test_the_drop_to_word_matching_is_announced_once_not_every_minute(
    monkeypatch, enabled, no_threads, capsys
):
    """Item 41 went unnoticed BECAUSE this was silent -- memory quietly got
    worse and the symptom pointed at the memory store instead of a timeout. But
    an Ollama that is simply not installed must not write a line every minute
    forever, so the log marks the CHANGE, not the state."""
    _posts(monkeypatch, lambda *_a: None)
    sr.available()
    first = capsys.readouterr().out
    assert "WORD-MATCHING ONLY" in first

    sr._probe = None  # re-probe without clearing what was last reported
    sr.available()
    assert "WORD-MATCHING ONLY" not in capsys.readouterr().out, "it repeated itself"


def test_recovery_is_announced_too(monkeypatch, enabled, no_threads, capsys):
    """Knowing memory came back matters as much as knowing it went: without it,
    a reader of the log cannot tell a blip from a permanent degradation."""
    _posts(monkeypatch, lambda *_a: None)
    sr.available()
    capsys.readouterr()

    sr._probe = None
    _posts(monkeypatch, lambda *_a: _ONE_VECTOR)
    assert sr.available() is True
    assert "meaning-based" in capsys.readouterr().out
