"""Regression tests for the 2026-09-20 memory-recall fixes.

Four separate defects, all of which presented to the user as the same thing —
"Era loses track of what I told her":

1. A correction under an EXISTING key was silently discarded when it was
   textually similar to the value it replaced ("Tuesday" -> "Thursday").
2. A multi-fact update aborted entirely on the first transient or deduped key,
   dropping every remaining fact in the same call.
3. `retrieve_memory_context` never scored the memory's KEY, so the most
   natural way to ask ("what is my name") ranked unrelated notes above the
   entry that answers it.
4. Nothing carried the live conversation across a session boundary, so a
   reconnect without a Gemini resumption handle started cold.

conftest.py redirects ERA_MEMORY_DIR, so everything here writes to a throwaway
store, never the user's live memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import memory.memory_manager as mm
from core.conversation_log import ConversationLog


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the memory store at a per-test file."""
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    mm.set_dedup_enabled(True)
    yield tmp_path


# ─────────────────────────────────────────────────────────────────────────
# 1. Corrections must overwrite, however similar they look
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("original", "correction"),
    [
        ("His sister is named Sara", "His sister is named Sarah"),
        ("Meeting with the supplier on Tuesday", "Meeting with the supplier on Thursday"),
        ("Prefers the showroom lights at 50 percent", "Prefers the showroom lights at 70 percent"),
        ("The project deadline is October 3", "The project deadline is October 8"),
        ("Her number ends in 447", "Her number ends in 477"),
    ],
)
def test_small_correction_overwrites_same_key(store, original, correction):
    """The exact failure the user reported: a one-word/one-digit correction
    scores above the old 0.88 dedup threshold, so the stale value survived and
    kept being injected into the prompt."""
    mm.update_memory({"notes": {"fact": {"value": original}}})
    mm.update_memory({"notes": {"fact": {"value": correction}}})
    assert mm.load_memory()["notes"]["fact"]["value"] == correction


def test_identical_restatement_is_deduped_not_rewritten(store):
    """A verbatim repeat is still a duplicate: it bumps reinforcement counters
    instead of rewriting, which is what dedup is actually for."""
    mm.update_memory({"notes": {"fact": {"value": "same text here"}}})
    before = mm.load_memory()["notes"]["fact"]
    mm.update_memory({"notes": {"fact": {"value": "Same   Text Here"}}})
    after = mm.load_memory()["notes"]["fact"]
    assert after["value"] == before["value"]
    assert after.get("access_count", 0) >= 1


def test_cross_key_near_duplicate_still_deduped(store):
    """Anti-bloat dedup between DIFFERENT keys is unchanged — only the same-key
    case was wrong."""
    mm.update_memory({"notes": {"a": {"value": "the wardrobe doors are matte oak"}}})
    mm.update_memory({"notes": {"b": {"value": "the wardrobe doors are matte oak"}}})
    assert "b" not in mm.load_memory()["notes"]


def test_correction_survives_reload_from_disk(store):
    """Persistence, not just the in-memory dict."""
    mm.update_memory({"identity": {"city": {"value": "Lives in Tunis"}}})
    mm.update_memory({"identity": {"city": {"value": "Lives in Sfax"}}})
    on_disk = json.loads(Path(mm.MEMORY_PATH).read_text(encoding="utf-8"))
    assert on_disk["identity"]["city"]["value"] == "Lives in Sfax"


# ─────────────────────────────────────────────────────────────────────────
# 2. A multi-fact update must not abort on one bad key
# ─────────────────────────────────────────────────────────────────────────


def test_transient_key_drops_only_itself(store):
    """A transient key used to `return` out of the whole update, silently
    discarding every other fact in the same call."""
    mm.update_memory(
        {
            "notes": {
                "keep_one": {"value": "the first real fact"},
                "last_media_title": {"value": "some film"},
                "keep_two": {"value": "the second real fact"},
            }
        }
    )
    notes = mm.load_memory()["notes"]
    assert "keep_one" in notes
    assert "keep_two" in notes
    assert "last_media_title" not in notes


def test_dedup_hit_drops_only_itself(store):
    """Same abort, via the dedup branch."""
    mm.update_memory({"notes": {"existing": {"value": "an established fact"}}})
    mm.update_memory(
        {
            "notes": {
                "existing": {"value": "an established fact"},
                "fresh": {"value": "a completely unrelated new thing"},
            }
        }
    )
    assert "fresh" in mm.load_memory()["notes"]


def test_dedup_bump_is_persisted(store):
    """A call that ONLY dedups still has to write the reinforcement bump."""
    mm.update_memory({"notes": {"x": {"value": "a stable fact"}}})
    mm.update_memory({"notes": {"x": {"value": "a stable fact"}}})
    on_disk = json.loads(Path(mm.MEMORY_PATH).read_text(encoding="utf-8"))
    assert on_disk["notes"]["x"].get("access_count", 0) >= 1


def test_multi_category_update_applies_in_full(store):
    mm.update_memory(
        {
            "identity": {"name": {"value": "Nesrine"}},
            "relationships": {"sister": {"value": "Sarah, studies architecture"}},
            "preferences": {"browser": {"value": "Brave"}},
        }
    )
    mem = mm.load_memory()
    assert mem["identity"]["name"]["value"] == "Nesrine"
    assert mem["relationships"]["sister"]["value"].startswith("Sarah")
    assert mem["preferences"]["browser"]["value"] == "Brave"


# ─────────────────────────────────────────────────────────────────────────
# 3. Retrieval must score the key, and must admit when it has nothing
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def populated():
    mem = {
        "identity": {"name": {"value": "Nesrine", "updated": "2026-01-01"}},
        "relationships": {
            "sister": {"value": "Sarah, 24, studies architecture", "updated": "2026-01-01"}
        },
        "notes": {},
    }
    for i in range(10):
        mem["notes"][f"filler{i}"] = {
            "value": f"unrelated filler note number {i} about nothing",
            "updated": "2026-09-19",
        }
    return mem


def test_key_named_in_query_outranks_recent_noise(populated):
    """`sister` is in the KEY, not the value. Scoring the value alone put ten
    fresher filler notes above the entry that answers the question."""
    ranked = mm.rank_memory_entries(populated, "sister", top_k=3)
    assert ranked[0]["key"] == "sister"


def test_natural_question_finds_the_entry(populated):
    """Stopword-stripped so "what is my sister's name" reduces to the two
    tokens that carry signal."""
    ranked = mm.rank_memory_entries(populated, "what is my sister's name", top_k=3)
    assert any(e["key"] == "sister" for e in ranked)


def test_identity_name_is_retrievable(populated):
    ranked = mm.rank_memory_entries(populated, "what is my name", top_k=3)
    assert any(e["category"] == "identity" and e["key"] == "name" for e in ranked)


def test_unanswerable_query_returns_nothing(populated):
    """Recency alone used to carry unrelated entries over any floor, so the
    model was handed confident-looking noise instead of 'I don't have that'."""
    ranked = mm.rank_memory_entries(
        populated, "showroom lighting rig", top_k=5, min_score=mm._RETRIEVAL_MIN_SCORE
    )
    assert ranked == []


def test_unmatched_entry_scores_zero(populated):
    """Recency/popularity are tiebreakers between matches, never relevance."""
    entry = populated["notes"]["filler0"]
    assert mm.score_memory_entry("helicopter", entry, key="filler0") == 0.0


def test_all_stopword_query_does_not_crash(populated):
    """A degenerate query falls back to raw tokens rather than scoring nothing."""
    assert isinstance(mm.rank_memory_entries(populated, "what is the", top_k=3), list)


def test_prompt_injection_is_not_floored(populated):
    """The memory block injected each session is a recency digest, not an
    answer — flooring it would empty the prompt."""
    assert mm.format_memory_for_prompt_ranked(populated, query="", top_k=15).strip() != ""


def test_reinforced_retrieval_round_trip(store):
    mm.update_memory({"relationships": {"sister": {"value": "Sarah, studies architecture"}}})
    assert "Sarah" in mm.retrieve_context_reinforced("sister", top_k=3)
    assert mm.retrieve_context_reinforced("helicopter maintenance", top_k=3) == ""


# ─────────────────────────────────────────────────────────────────────────
# 4. Conversation continuity across a session boundary
# ─────────────────────────────────────────────────────────────────────────


def test_recap_renders_what_was_said():
    log = ConversationLog()
    log.record("the supplier moved the delivery to Thursday", "Noted, sir — Thursday it is.")
    block = log.recap_block()
    assert "Thursday" in block
    assert "User:" in block and "You:" in block


def test_empty_log_renders_nothing():
    assert ConversationLog().recap_block() == ""


def test_recap_keeps_only_the_most_recent_turns():
    log = ConversationLog(max_turns=3)
    for i in range(6):
        log.record(f"question {i}", f"answer {i}")
    block = log.recap_block()
    assert "question 5" in block
    assert "question 0" not in block


def test_stale_conversation_is_not_resumed():
    """A 45-minute-old exchange is a non-sequitur, not continuity."""
    log = ConversationLog(max_age_s=60.0)
    log.record("something old", "a reply", now=1000.0)
    assert log.recap_block(now=1000.0 + 30) != ""
    assert log.recap_block(now=1000.0 + 600) == ""


def test_recap_is_bounded():
    log = ConversationLog(max_turns=8)
    for _ in range(8):
        log.record("x" * 500, "y" * 500)
    block = log.recap_block()
    assert len(block) <= 3000  # block cap + heading + newline


def test_recap_keeps_the_newest_turn_when_trimming():
    log = ConversationLog(max_turns=8)
    for i in range(8):
        log.record(f"turn {i} " + "padding " * 60, f"reply {i} " + "padding " * 60)
    assert "turn 7" in log.recap_block()


def test_empty_exchange_is_not_recorded():
    log = ConversationLog()
    assert log.record("", "") is False
    assert log.recap_block() == ""


def test_recap_survives_a_restart(monkeypatch):
    """Persisted, because the boundary worth surviving includes a relaunch."""
    with TemporaryDirectory() as tmp:
        monkeypatch.setenv("ERA_MEMORY_DIR", tmp)
        first = ConversationLog()
        first.record("remember the Sfax showroom opens at nine", "Understood, sir.")
        first.save()

        second = ConversationLog()
        second.load()
        assert "Sfax" in second.recap_block()


def test_corrupt_recap_file_degrades_quietly(monkeypatch):
    with TemporaryDirectory() as tmp:
        monkeypatch.setenv("ERA_MEMORY_DIR", tmp)
        (Path(tmp) / "conversation_log.json").write_text("{not json", encoding="utf-8")
        log = ConversationLog()
        log.load()  # must not raise
        assert log.recap_block() == ""


def test_unwritable_recap_does_not_raise(monkeypatch):
    """A blocked write costs the recap, never the conversation."""
    with TemporaryDirectory() as tmp:
        # A FILE where the store's directory should be: mkdir/write both fail.
        blocked = Path(tmp) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("ERA_MEMORY_DIR", str(blocked))
        log = ConversationLog()
        log.record("a", "b")
        log.save()  # must not raise


# ─────────────────────────────────────────────────────────────────────────
# 5. Recap WIRING — the plumbing between the input funnel and the recap
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def td():
    import app.era_live.tool_dispatch as mod

    return mod


def test_input_funnel_arms_the_recap_slot(td):
    """_note_user_utterance is the single funnel every complete user input
    goes through, voice or typed. If it stops arming the slot, TYPED orders
    silently vanish from the recap — typed input has no transcription of its
    own to fall back on."""
    import types as pytypes

    era = pytypes.SimpleNamespace(
        _recent_user_texts=[],
        _last_user_text="",
        _inflight_user_text="",
        _inflight_started_at=0.0,
        _recap_pending_user="",
    )
    td.ToolDispatchMixin._note_user_utterance(era, "the delivery moved to Thursday")
    assert era._recap_pending_user == "the delivery moved to Thursday"


def test_blank_input_does_not_arm_the_slot(td):
    import types as pytypes

    era = pytypes.SimpleNamespace(
        _recent_user_texts=[],
        _last_user_text="",
        _inflight_user_text="",
        _inflight_started_at=0.0,
        _recap_pending_user="earlier line",
    )
    td.ToolDispatchMixin._note_user_utterance(era, "   ")
    assert era._recap_pending_user == "earlier line"


def test_recap_is_injected_only_without_a_resumption_handle():
    """With a handle, Gemini still holds the conversation and replaying it
    would duplicate context. Without one, the recap is the only thing that
    carries. Asserted against the source so the gating cannot be dropped
    silently during a refactor of _build_config."""
    import inspect

    import app.era_live.tool_dispatch as mod

    src = inspect.getsource(mod.ToolDispatchMixin._build_config)
    assert "recap_block()" in src
    idx = src.index("recap_block()")
    guard = src.rindex("if not token:", 0, idx)
    assert guard < idx


def test_pairing_holds_the_user_line_until_era_replies():
    """One request often spans several turns — a tool-call turn, then the
    spoken reply. Recording per-turn would split it into two half-exchanges."""
    log = ConversationLog()
    pending = "book the Sfax showroom for Thursday"

    # Turn 1: a pure tool call. Nothing said, nothing recorded, line held.
    full_out = ""
    if full_out:
        log.record(pending, full_out)
        pending = ""
    assert log.recap_block() == ""

    # Turn 2: the spoken reply. Now the whole exchange lands as one.
    full_out = "Booked for Thursday, sir."
    if full_out:
        log.record(pending, full_out)
        pending = ""
    block = log.recap_block()
    assert "book the Sfax showroom for Thursday" in block
    assert "Booked for Thursday, sir." in block
    assert block.count("User:") == 1
