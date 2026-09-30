"""Unit tests for the WakeWordConfig confidence gate + per-word decoys.

These tests do NOT require Vosk or audio. They verify the pure-Python logic in
main.WakeWordConfig that decides whether a decoded transcript counts as a wake.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


def _load_wake_word_config_class():
    """Extract WakeWordConfig + its _DEFAULT_WAKE_DECOYS dependency from
    app/wake.py without importing the whole module (avoids PyQt6/vosk deps)."""
    repo_root = Path(__file__).resolve().parent.parent
    src = (repo_root / "app" / "wake.py").read_text(encoding="utf-8")

    # Pull in _DEFAULT_WAKE_DECOYS so the class body can reference it.
    ns = {"re": re, "json": json, "print": print}
    m = re.search(r"_DEFAULT_WAKE_DECOYS\s*=\s*\[([^\]]*)\]", src)
    if m:
        # Eval the list literal in the ns.
        exec(f"_DEFAULT_WAKE_DECOYS = [{m.group(1)}]", ns)
    else:
        ns["_DEFAULT_WAKE_DECOYS"] = [
            "error",
            "her",
            "area",
            "here",
            "hey",
            "wait",
            "way",
            "week",
            "walk",
            "work",
            "woke",
            "wake up",
            "uh huh",
        ]

    start = src.index("class WakeWordConfig:")
    lines = src[start:].splitlines()
    body_end = 1
    for i, line in enumerate(lines[1:], start=1):
        if line and not line.startswith(" ") and not line.startswith("\t"):
            if not line.startswith("#"):
                body_end = i
                break
    class_src = "\n".join(lines[:body_end])
    exec(class_src, ns)
    return ns["WakeWordConfig"]


WakeWordConfig = _load_wake_word_config_class()


@pytest.fixture
def cfg(tmp_path):
    """Isolated WakeWordConfig pointing at a temp config file."""
    cfg_path = tmp_path / "wake_word.json"
    instance = WakeWordConfig()
    instance._cfg_path = cfg_path

    def reload():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            word = str(raw.get("wake_word", "")).strip()
            if word:
                instance.word = word.lower()
            instance.aliases = [str(a) for a in raw.get("aliases", []) if str(a).strip()]
            decoys = [str(d) for d in raw.get("decoys", []) if str(d).strip()]
            if decoys:
                instance.decoys = decoys
            try:
                mc = float(raw.get("min_confidence", instance.min_confidence))
                instance.min_confidence = min(1.0, max(0.5, mc))
            except (TypeError, ValueError):
                pass
        except FileNotFoundError:
            instance._write()
        except Exception:
            pass
        instance._rebuild()

    def _write():
        cfg_path.write_text(
            json.dumps(
                {
                    "wake_word": instance.word,
                    "aliases": instance.aliases,
                    "decoys": instance.decoys,
                    "min_confidence": instance.min_confidence,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    instance.reload = reload
    instance._write = _write
    instance.reload()
    return instance


# ── Confidence gate — final results ──────────────────────────────────────────


def test_final_high_confidence_wakes(cfg):
    cfg.update("shazam")
    assert cfg.match_final("shazam", confidence=0.97) is True


def test_final_low_confidence_rejected(cfg):
    """A near-miss ('shadow' force-decoded to 'shazam') scores low and
    must be rejected even though the regex matches."""
    cfg.update("shazam")
    assert cfg.match_final("shazam", confidence=0.45) is False


def test_final_confidence_missing_still_matches(cfg):
    """No confidence key in the Vosk result — must still wake (regex-only)."""
    cfg.update("shazam")
    assert cfg.match_final("shazam", confidence=None) is True


def test_final_wrong_word_rejected(cfg):
    cfg.update("shazam")
    assert cfg.match_final("hello", confidence=0.99) is False


# ── Length guard — partial results ────────────────────────────────────────────


def test_short_partial_rejected(cfg):
    """A 3-char partial ('sha') force-decoded to 'shazam' by the grammar
    filter would wake Era falsely. The length guard rejects it."""
    cfg.update("shazam")
    assert cfg.match_partial("sha") is False, "3-char partial must be rejected"


def test_full_word_partial_ok(cfg):
    cfg.update("shazam")
    assert cfg.match_partial("shazam") is True


# ── Per-word decoy generation ─────────────────────────────────────────────────


def test_shazam_generates_sha_prefix_decoys(cfg):
    """For the word 'shazam', auto-decoys must include sha-prefixed near
    misses so Vosk has somewhere to route 'shadow'/'shatter' audio."""
    cfg.update("shazam")
    vocab = json.loads(cfg.grammar)
    assert "shadow" in vocab or "shallow" in vocab or "shatter" in vocab, (
        f"Expected sha-prefix decoy in vocab, got {vocab}"
    )


def test_default_decoys_preserved(cfg):
    cfg.update("shazam")
    vocab = json.loads(cfg.grammar)
    for tok in ("error", "her", "here"):
        assert tok in vocab, f"{tok!r} should remain in vocab"


def test_wake_word_always_in_vocab(cfg):
    cfg.update("jarvis")
    vocab = json.loads(cfg.grammar)
    assert "jarvis" in vocab
    assert "hey jarvis" in vocab


def test_unk_catchall_present(cfg):
    cfg.update("nova")
    vocab = json.loads(cfg.grammar)
    assert "[unk]" in vocab


# ── Confirmation count threshold scales with word length ─────────────────────


def test_short_word_requires_more_confirmations(cfg):
    """A 3-char wake word ('era') is noisier — require ≥4 partial hits."""
    cfg.update("era")
    assert cfg.min_partial_confirmations >= 4


def test_long_word_requires_fewer_confirmations(cfg):
    """A 6-char wake word ('shazam') is more discriminative, but the partial
    path stays strict (confidence-gated finals are the reliable trigger), so a
    longer word still needs 3 consecutive partial hits — fewer than short words."""
    cfg.update("shazam")
    assert cfg.min_partial_confirmations == 3


# ── Regression: 'sha'-prefix wakeup with the 'shazam' word ───────────────────


def test_low_confidence_sha_word_does_not_wake(cfg):
    """The user's bug: saying 'shadow' (a sha-prefix word) in standby woke Era
    even though the wake word is 'shazam'. Vosk force-decodes 'shadow' to
    'shazam' with low confidence → must be rejected."""
    cfg.update("shazam")
    assert cfg.match_final("shazam", confidence=0.55) is False


def test_short_partial_does_not_wake(cfg):
    cfg.update("shazam")
    assert cfg.match_partial("sha") is False


def test_real_shazam_wakes(cfg):
    """A genuine 'shazam' utterance with high confidence should still wake."""
    cfg.update("shazam")
    assert cfg.match_final("shazam", confidence=0.96) is True


# ── update() validation ──────────────────────────────────────────────────────


def test_update_rejects_multiword(cfg):
    with pytest.raises(ValueError):
        cfg.update("hey jarvis")


def test_update_rejects_too_short(cfg):
    with pytest.raises(ValueError):
        cfg.update("a")


def test_update_accepts_valid_word(cfg):
    result = cfg.update("nova")
    assert result == "nova"
    assert cfg.word == "nova"


def test_update_persists_to_config(cfg, tmp_path):
    cfg.update("jarvis")
    cfg_path = cfg._cfg_path
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert raw["wake_word"] == "jarvis"


# ── Aliases ──────────────────────────────────────────────────────────────────


def test_alias_in_grammar(cfg):
    cfg.update("jarvis", aliases=["j"])
    vocab = json.loads(cfg.grammar)
    assert "j" in vocab


def test_alias_matches_pattern(cfg):
    cfg.update("jarvis", aliases=["j"])
    # The pattern should match either the word or the alias.
    assert cfg.pattern.search("jarvis")
    assert cfg.pattern.search("j")


# ── Grammar rebuild after update clears旧 decoy bleed ───────────────────────


def test_update_from_shazam_to_nova_clears_sha_decoys(cfg):
    """Switching wake words must NOT leave the old word's decoys in the vocab
    (otherwise 'shadow' would still be a decoy for 'nova', polluting things)."""
    cfg.update("shazam")
    vocab_after_shazam = json.loads(cfg.grammar)
    cfg.update("nova")
    vocab_after_nova = json.loads(cfg.grammar)
    # sha-prefix decoys are auto-generated per-word; after switching to 'nova'
    # they should NOT survive (only the persistent default decoys + nova's
    # auto-decoys remain).
    for sha_tok in ("shadow", "shallow", "shatter"):
        if sha_tok in vocab_after_shazam:
            assert sha_tok not in vocab_after_nova, f"{sha_tok!r} leaked from shazam → nova"
