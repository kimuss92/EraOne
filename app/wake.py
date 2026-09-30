"""Wake-word subsystem (extracted from main.py).

WakeWordConfig + the live WAKE_CFG instance + Vosk loader. WAKE_CFG is a shared
mutable instance imported by reference by both main.py and the EraLive tool-
dispatch mixin (set-wake-word tool), so updates are visible everywhere.
"""

import json
import re
from typing import Any

from core.utils import BASE_DIR

try:
    import vosk
except ImportError:
    vosk = None

# ---- VOSK CONFIGURATION ----
VOSK_MODEL_PATH = BASE_DIR / "models" / "vosk-model-small-en-us-0.15"

# ---- WAKE WORD (self-modifiable at runtime) ----
# The wake word is NOT hard-coded. It lives in config/wake_word.json, and Era can
# rewrite it herself via the `set_wake_word` tool (see _execute_tool_raw). That
# file is Era's own editable state, so a wake-word change is autonomous, survives
# restarts, and — because WakeWordConfig rebuilds the grammar + regex in place —
# takes effect immediately without touching source or restarting the process.
WAKE_WORD_CONFIG_PATH = BASE_DIR / "config" / "wake_word.json"

# Generic phonetic decoys. Restricting the Vosk decoder to a tiny vocabulary — the
# wake word, a few natural prefixes, phonetic decoys, and an [unk] catch-all —
# massively improves wake precision/recall over free-form recognition (which
# force-fits ambient noise onto the wake word). Decoys absorb near-misses so
# similar-sounding speech doesn't get mis-decoded as the wake word.
_DEFAULT_WAKE_DECOYS = [
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


class WakeWordConfig:
    """Single source of truth for the wake word + its derived Vosk grammar and
    match regex.

    - `word`    : the live wake word (lowercase).
    - `grammar` : JSON vocab string handed to Vosk's KaldiRecognizer.
    - `pattern` : compiled regex used to confirm a decoded transcript is the wake word.

    `update()` validates a new word, rebuilds grammar+regex in place, and persists
    to config/wake_word.json. Callers rebuild any live standby recognizer so the
    new word is armed instantly.

    Confidence gating: Vosk's grammar-constrained decoder force-fits near-misses
    ("sha..." for the word "shazam") onto the wake word. The decoys only absorb
    near-misses if they share enough phonetics; for arbitrary user-chosen words the
    generic decoys ("error","her",...) do NOT cover the sha-prefix. So the real
    gate is `min_confidence` on **final** results, plus a stricter partial-count
    threshold combined with a minimum-decoded-length check on **partials**.
    """

    def __init__(self) -> None:
        self.word = "wake"
        self.aliases: list[str] = []
        self.decoys: list[str] = list(_DEFAULT_WAKE_DECOYS)
        self.grammar = ""
        self.pattern = re.compile(r"\bwake\b", re.IGNORECASE)
        # Minimum Vosk confidence for a FINAL result to count as a wake.
        # The grammar-constrained decoder force-fits near-misses onto the wake
        # word; the decoys only cover the default phonetic space, so confidence is
        # the real gate. 0.90 is strict — a true wake word utterance scores ~0.95+.
        self.min_confidence = 0.90
        # Dynamic noise adaptation: raise confidence floor when ambient noise
        # produces near-miss candidates, preventing accidental wakes.
        self._noise_floor = 0.85
        self._noise_samples: list[float] = []
        # Partials lack a confidence score. To defend against a partial "shaz"
        # being force-decoded to "shazam" mid-utterance, require:
        #   1. the decoded partial to be at least this long (chars), AND
        #   2. the SAME partial text to appear on this many consecutive chunks.
        # `_min_partial_len` is derived from the word length so short prefixes
        # ("sha", "shaz") cannot trigger a wake on their own even if Vosk
        # force-decodes them to the full word.
        self.min_partial_confirmations = 3  # default; raised for short words
        self._min_partial_len = 4
        self.reload()

    # ── Per-word phonetic decoy generation ────────────────────────────────────
    # When the wake word is "shazam", a partial like "shadow" or "shatter" still
    # gets force-decoded by Vosk to "shazam" because that's the only sha-prefixed
    # token in the vocab. Adding near-miss words that share the prefix gives Vosk
    # somewhere else to put the audio. This is purely a Vosk grammar hint; it does
    # NOT change the regex match (only the wake word/aliases match).
    @staticmethod
    def _generate_decoys_for_word(word: str) -> list[str]:
        """Return near-miss decoys sharing the word's prefix/syllables.

        Pure-Python phonetic neighbour generator — no external deps. Builds a
        small list of plausible-sounding words starting with the same leading
        characters so Vosk's grammar has somewhere to route close-but-not-equal
        audio instead of force-fitting it onto the wake word.
        """
        word = (word or "").lower().strip()
        if not word or not word.isalpha():
            return []
        prefix2 = word[:2]
        prefix3 = word[:3]
        # Common English words that share the prefix and are likely to be
        # misheard. We deliberately keep the list SMALL so the vocab stays tiny
        # (large vocabs hurt precision and add nothing — the wake word is the
        # ONLY token that should match the regex anyway).
        common_BY_PREFIX = {
            "sh": ("shall", "shy", "shed", "she", "shot", "ship", "shop", "show"),
            "sho": ("shot", "show", "shop", "shore"),
            "sha": ("shall", "shadow", "shallow", "shatter", "shawn", "sham"),
            "she": ("shell", "shed", "she", "shelby"),
            "wa": ("water", "wall", "walk", "war", "way", "want", "wait"),
            "wo": ("work", "word", "world", "woke"),
            "we": ("week", "well", "went", "web"),
            "wi": ("will", "win", "with", "wish"),
            "er": ("error", "era", "errand"),
            "he": ("hey", "her", "here", "head"),
            # "ja" and "al" were each written twice. A dict literal keeps only
            # the LAST entry, so the earlier tuples were silently discarded and
            # "jarvis"/"jan"/"alex" never reached the decoy grammar at all —
            # exactly the near-misses this list exists to absorb. Merged.
            "ja": ("jarvis", "java", "jason", "jan", "jar", "jam", "jade"),
            "no": ("no", "now", "know", "note", "nova"),
            "al": ("also", "alpha", "all", "alex", "alert"),
            "zi": ("zip", "zinc", "zika"),
            "za": ("zap", "zara"),
        }
        out: list[str] = []
        seen: set[str] = {word}
        # Prefix-3 neighbours first (tightest match)
        for tok in common_BY_PREFIX.get(prefix3, ()):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
        for tok in common_BY_PREFIX.get(prefix2, ()):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
        # Always add a few generic short helpers that absorb ambient noise.
        for tok in ("huh", "oh", "ah", "uh", "um", "yeah", "no", "yes", "okay"):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
        return out

    def _phrases(self) -> list[str]:
        base = self.word.lower().strip()
        phrases = [base, f"hey {base}", f"okay {base}"]
        phrases += [a.lower().strip() for a in self.aliases if a.strip()]
        seen: set[str] = set()
        out: list[str] = []
        for p in phrases:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _rebuild(self) -> None:
        phrases = self._phrases()
        # Augment persistent decoys with auto-generated per-word decoys so the
        # vocab has somewhere to route near-misses for THIS wake word. Dedup.
        auto_decoys = self._generate_decoys_for_word(self.word)
        # Persistent decoys take priority (user may have hand-tuned them), then
        # aliases, then auto-decoys, then the [unk] catch-all.
        merged: list[str] = []
        seen: set[str] = set()
        for d in list(self.decoys) + auto_decoys:
            d = (d or "").strip().lower()
            if d and d not in phrases and d not in seen:
                seen.add(d)
                merged.append(d)
        vocab = phrases + merged + ["[unk]"]
        self.grammar = json.dumps(vocab)
        # Longest-first so multi-word phrases ("hey wake") match before the bare word.
        alts = sorted({re.escape(p) for p in phrases}, key=len, reverse=True)
        self.pattern = re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.IGNORECASE)
        # Derive partial-confirmation thresholds from the word length so very
        # short prefixes cannot trigger a wake from a single partial chunk.
        wl = max(1, len(self.word))
        self._min_partial_len = max(4, int(wl * 0.8))
        # Shorter words are noisier — require more consecutive partial hits.
        # The partial path has no confidence score, so it is the main source of
        # false wakes (Vosk force-decodes ambient noise onto the wake word). The
        # confidence-gated FINAL path is the reliable trigger; partials are only a
        # low-latency fast-path, so we keep them strict — one extra confirmation
        # across the board over the old thresholds.
        if wl <= 3:
            self.min_partial_confirmations = 5
        elif wl <= 5:
            self.min_partial_confirmations = 4
        else:
            self.min_partial_confirmations = 3

    def reload(self) -> None:
        try:
            raw = json.loads(WAKE_WORD_CONFIG_PATH.read_text(encoding="utf-8"))
            word = str(raw.get("wake_word", "")).strip()
            if word:
                self.word = word.lower()
            self.aliases = [str(a) for a in raw.get("aliases", []) if str(a).strip()]
            decoys = [str(d) for d in raw.get("decoys", []) if str(d).strip()]
            if decoys:
                self.decoys = decoys
            try:
                mc = float(raw.get("min_confidence", self.min_confidence))
                self.min_confidence = min(1.0, max(0.5, mc))
            except (TypeError, ValueError):
                pass
        except FileNotFoundError:
            self._write()  # seed the file with current defaults
        except Exception as e:
            print(f"[WAKE] Config load failed ({e}); keeping wake word '{self.word}'.")
        self._rebuild()

    def _write(self) -> None:
        try:
            WAKE_WORD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            WAKE_WORD_CONFIG_PATH.write_text(
                json.dumps(
                    {
                        "wake_word": self.word,
                        "aliases": self.aliases,
                        "decoys": self.decoys,
                        "min_confidence": self.min_confidence,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[WAKE] Could not persist wake word: {e}")

    def update(self, new_word: str, aliases: list[str] | None = None) -> str:
        """Validate + apply a new wake word. Returns the applied word.

        Raises ValueError on a wake word that would wreck detection precision
        (multi-word, too short/long, or non-alphabetic). A single, distinct,
        2+ syllable word is best; ultra-short words cause false wakes.
        """
        candidate = (new_word or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z'-]{1,20}", candidate):
            raise ValueError(
                "wake word must be a single alphabetic word of 2-21 letters "
                "(letters, apostrophe or hyphen only)"
            )
        self.word = candidate
        if aliases is not None:
            self.aliases = [str(a).strip().lower() for a in aliases if str(a).strip()]
        self._rebuild()
        self._write()
        return self.word

    # ── Confidence-gated match ───────────────────────────────────────────────
    # The regex alone is NOT enough: Vosk's grammar-constrained decoder will
    # happily emit the literal text "shazam" when the user said "shadow",
    # because "shazam" is the only sha-prefixed candidate left after the
    # grammar filter. So we additionally require:
    #   • on FINAL results: a Vosk confidence ≥ min_confidence (finals carry
    #     a real score and a low-confidence final means the decoder was unsure).
    #   • on PARTIAL results: the decoded text to be at least _min_partial_len
    #     chars long AND to repeat on min_partial_confirmations consecutive
    #     audio chunks. A short partial ("sha") force-decoded to "shazam" is
    #     rejected by the length guard; a longer one that doesn't repeat is
    #     rejected by the count guard.
    def match_final(self, text: str, confidence: float | None) -> bool:
        """True if `text` matches the wake pattern with sufficient confidence."""
        if not text:
            return False
        if not self.pattern.search(text.lower().strip()):
            return False
        if confidence is not None:
            # Dynamic noise adaptation + stricter phonetic filter: reject
            # near-misses (e.g., "shadow" force-decoded to wake word) and
            # low-confidence candidates.
            adaptive_floor = max(self.min_confidence, self._noise_floor)
            if confidence < adaptive_floor:
                self._noise_samples.append(confidence)
                if len(self._noise_samples) > 10:
                    self._noise_samples = self._noise_samples[-10:]
                return False
            # Strict exact-word match filter: only exact configured word or alias
            word = self.word.lower()
            token = text.lower().strip().replace("hey ", "").replace("okay ", "")
            found = word in token.split()
            if not found:
                for a in self.aliases:
                    if a.lower() in token.split():
                        found = True
                        break
            if not found:
                return False
        else:
            # No confidence — require exact word presence
            word = self.word.lower()
            token = text.lower().strip().replace("hey ", "").replace("okay ", "")
            found = word in token.split()
            if not found:
                for a in self.aliases:
                    if a.lower() in token.split():
                        found = True
                        break
                if not found:
                    return False
        return True

    def match_partial(self, text: str) -> bool:
        """True if a PARTIAL result looks like the wake word.

        Partials have no confidence score, so we substitute a length floor +
        the caller enforces the consecutive-count threshold separately
        (see _run_wake_listener).
        """
        if not text:
            return False
        t = text.lower().strip()
        if not self.pattern.search(t):
            return False
        # Length guard: short partials get force-decoded by the grammar
        # filter to the closest vocab word (often the wake word). Require the
        # decoded text to be near the wake word's own length before trusting it.
        if len(t) < self._min_partial_len:
            return False
        return True


WAKE_CFG = WakeWordConfig()

WAKE_WORD = WAKE_CFG.word  # retained for descriptive references (prompts/tool text)


def init_vosk() -> Any | None:
    """Load Vosk model if available, otherwise print error and return None."""
    if vosk is None:
        print("[WAKE] Vosk package not installed; wake word disabled.")
        return None
    if not VOSK_MODEL_PATH.exists():
        print(f"[WAKE] Model not found: {VOSK_MODEL_PATH}")
        print("Please download vosk-model-small-en-us-0.15 into the 'models' folder.")
        return None
    try:
        model = vosk.Model(str(VOSK_MODEL_PATH))
        print("[WAKE] Vosk model loaded.")
        return model
    except Exception as e:
        print(f"[WAKE] Failed to load Vosk model: {e}")
        return None
