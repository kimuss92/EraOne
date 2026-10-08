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

import numpy as np
import pytest

from memory import semantic_recall as sr
from memory.memory_manager import _RETRIEVAL_MIN_SCORE, rank_memory_entries

# The 12 paraphrases, and the entry each must find. Keys are real entries of the
# store conftest.py copies, so a renamed key breaks this loudly.
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

MEASURED_TOP1 = 9  # of 12, at COSINE_CUT 0.36 — see the sweep in the module docstring
MEASURED_TOP3 = 10
LEXICAL_TOP1 = 6  # what it was before, and the floor this must never fall back to


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


def test_a_near_floor_semantic_match_is_not_returned_as_answer(enabled, monkeypatch):
    """Borderline semantic noise must stay below the answerability floor."""
    _stub_scores(monkeypatch, {"unrelated_fact": 0.15})
    memory = {"notes": {"unrelated_fact": {"value": "a synthetic fact"}}}

    ranked = rank_memory_entries(
        memory, "what is the current weather", top_k=5, min_score=_RETRIEVAL_MIN_SCORE
    )

    assert ranked == []


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


def _measurement_memory():
    """A fixed, synthetic corpus: quality tests must not depend on user memory."""
    entries = {
        "favorite_football_team": "The football club I support is Example FC.",
        "job": "My occupation is video editing.",
        "current_city": "I live in Example City.",
        "birthday": "My birthday is April 1, 2000.",
        "preferred_name": "Please call me Alex.",
        "magnific_browser_preference": "I use Firefox for Magnific.",
        "son_favorite_song": "My child likes the song Example Song.",
        "startup_greeting_preference": "When I start the app, greet me with Hello again.",
        "restart_rule": "After a crash, restart the app automatically.",
        "era_architecture_goal": "The main project design goal is reliability.",
        "era_voice_upgrade_vibe_voice": "We chose Example Voice as the speech synthesis engine.",
    }
    return {"test_facts": {key: {"value": value} for key, value in entries.items()}}


@requires_model
def test_measured_recall_beats_the_lexical_baseline(enabled):
    """THE acceptance-point-4 measurement. Numbers, not adjectives.

    Asserted as a floor rather than an equality: a better model or a richer
    store may push it up, and that must not be a failure. Falling back to the
    lexical baseline, however, means this feature has silently stopped working.
    """
    memory = _measurement_memory()
    top1 = top3 = 0
    misses = []
    for query, want in PARAPHRASES:
        keys = [
            e["key"]
            for e in rank_memory_entries(memory, query, top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
        ]
        top1 += keys[:1] == [want]
        top3 += want in keys[:3]
        if keys[:1] != [want]:
            misses.append(f"{query!r} -> {keys[:3]} (wanted {want})")

    assert top1 > LEXICAL_TOP1, (
        f"top-1 recall is {top1}/12, no better than the lexical baseline of "
        f"{LEXICAL_TOP1} — semantic recall has stopped contributing. Misses: {misses}"
    )
    assert top1 >= MEASURED_TOP1, f"top-1 recall fell to {top1}/12 from {MEASURED_TOP1}: {misses}"
    assert top3 >= MEASURED_TOP3, (
        f"top-3 recall fell to {top3}/12 from {MEASURED_TOP3}: {misses}"
    )


@requires_model
def test_an_unanswerable_query_still_returns_nothing(enabled):
    """The cost side of the same coin, and the reason the cut is 0.36 and not
    0.25 (which scored 10/12 but returned noise for 3 of these 8)."""
    memory = _measurement_memory()
    noisy = {}
    for query in UNANSWERABLE:
        ranked = rank_memory_entries(memory, query, top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
        if ranked:
            noisy[query] = [e["key"] for e in ranked]
    assert not noisy, (
        "unanswerable queries returned confident-looking noise, which is exactly "
        f"what the pre-existing relevance floor was added to stop: {noisy}"
    )


@requires_model
def test_recall_is_fast_enough_to_sit_in_a_conversation(enabled):
    """It runs while the user is waiting for an answer. The entry index is
    cached, so the per-query cost is one embed (~46 ms measured) plus the
    lexical pass."""
    import time

    from memory.memory_manager import load_memory

    memory = load_memory()
    rank_memory_entries(memory, "warm the index", top_k=5)  # pay the one-time build
    start = time.monotonic()
    for query, _ in PARAPHRASES:
        rank_memory_entries(memory, query, top_k=5, min_score=_RETRIEVAL_MIN_SCORE)
    per_query = (time.monotonic() - start) / len(PARAPHRASES)
    assert per_query < 1.0, f"{per_query * 1000:.0f} ms per recall is too slow to speak over"
