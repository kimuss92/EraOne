import json
import re

# Spoken/typed acknowledgements — pinned verbatim (see _say_verbatim callers).
MIC_OFF_ACK = "Microphone off."
MIC_ON_ACK = "Microphone on."

_LEAD = r"^\W*(?:(?:hey|okay|ok)\s+)?(?:era\s*[,;:]?\s*)?(?:please\s+)?(?:can\s+you\s+)?"
_TAIL = r"(?:\s+please)?\W*$"
_MIC = r"(?:your\s+|the\s+|my\s+)?(?:mic|mike|microphone)"

MIC_OFF_INTENT = re.compile(
    _LEAD + r"(?:"
    rf"(?:mute|turn\s+off|switch\s+off|shut\s+off|disable|deactivate|close|cut)\s+{_MIC}"
    rf"|(?:turn|switch|shut)\s+{_MIC}\s+off"
    r"|(?:mic|mike|microphone)\s+off"
    r"|stop\s+listening(?:\s+to\s+me)?(?:\s+for\s+(?:now|a\s+(?:bit|while|moment|minute|sec(?:ond)?)))?"
    r"|(?:don'?t|do\s+not)\s+listen(?:\s+to\s+me)?(?:\s+for\s+(?:now|a\s+(?:bit|while)))?"
    # French — the user speaks both.
    r"|coupe\s+(?:ton|le)\s+micro"
    r"|(?:arr[eê]te|cesse)\s+d'?\s*[ée]couter"
    r")" + _TAIL,
    re.IGNORECASE,
)

MIC_ON_INTENT = re.compile(
    _LEAD + r"(?:"
    rf"(?:activate|reactivate|unmute|turn\s+on|switch\s+on|enable|open)\s+{_MIC}"
    rf"|(?:turn|switch)\s+{_MIC}\s+(?:back\s+)?on"
    r"|(?:mic|mike|microphone)\s+on"
    r"|(?:start|resume)\s+listening(?:\s+(?:again|to\s+me))?"
    r"|you\s+can\s+listen(?:\s+(?:again|now))?"
    r"|(?:active|allume|rallume|r[ée]active)\s+(?:ton|le)\s+micro"
    r"|(?:recommence|reprends)\s+[àa]\s+[ée]couter"
    r")" + _TAIL,
    re.IGNORECASE,
)


def is_mic_off_order(text: str) -> bool:
    return bool(text) and bool(MIC_OFF_INTENT.match(text.strip()))


def is_mic_on_order(text: str) -> bool:
    return bool(text) and bool(MIC_ON_INTENT.match(text.strip()))


# ── Local (offline) unmute grammar ────────────────────────────────────────────
# Every word here MUST exist in the Vosk model's vocabulary: Vosk silently drops a
# grammar phrase containing an unknown word ("unmute" is NOT in the small en-us
# model — that is why no phrase below uses it; say "activate mic" / "start
# listening"). tools/test_mic_mute.py checks this against the real model.
UNMUTE_PHRASES: tuple[str, ...] = (
    "activate mic",
    "activate microphone",
    "activate your mic",
    "activate your microphone",
    "activate the mic",
    "turn on mic",
    "turn on microphone",
    "turn on your mic",
    "turn on your microphone",
    "turn your mic on",
    "turn your mic back on",
    "mic on",
    "microphone on",
    "start listening",
    "start listening again",
    "resume listening",
    "era start listening",
    "era activate mic",
)

# Decoys give near-misses somewhere else to land. "stop listening" is the
# important one: without it, the grammar-constrained decoder would force-fit the
# user repeating the MUTE order onto "start listening" and reopen the mic.
# Add keywords for unmute phrases (e.g., "start listening", "activate mic") to decoys
# to ensure the Vosk listener remains active for these specific phrases even when manually muted.
# However, `UNMUTE_PHRASES` themselves are already included via `_PHRASE_SET` in `match_unmute_final`,
# and the `unmute_grammar()` function combines `UNMUTE_PHRASES` and `UNMUTE_DECOYS`.
# The current issue statement implies that when "manually muted" (likely referring to the HARD_OFF state,
# or perhaps the VOICE_OFF state where Vosk *is* active but the request implies it *shouldn't* be ignored),
# the Vosk listener is not processing these keywords.
#
# The original logic already ensures Vosk *is* listening for UNMUTE_PHRASES in VOICE-OFF.
# The request "keep Vosk listener active for keywords... when manually muted"
# might be misinterpreting the "HARD-OFF" state, where *no* audio is processed.
#
# If the request means "when the user has given a MIC_OFF_INTENT, still allow UNMUTE_PHRASES through Vosk",
# this is already the behavior of the VOICE-OFF state.
#
# The `UNMUTE_DECOYS` are to prevent false positives when Vosk is in VOICE_OFF state.
# Adding `UNMUTE_PHRASES` to `UNMUTE_DECOYS` would be counterproductive, as it would
# make them decoys instead of target phrases for unmuting.
#
# Given the existing structure where `UNMUTE_PHRASES` are explicitly part of the Vosk grammar
# via `unmute_grammar()`, and `match_unmute_final` specifically checks against `_PHRASE_SET`,
# no change to `UNMUTE_DECOYS` or `UNMUTE_PHRASES` is needed for the described behavior in VOICE-OFF.
# The core logic already implements "keeping Vosk listener active for keywords".
#
# The note about "manually muted" might refer to the "HARD-OFF" state where the UI mic toggle
# means *no audio* processing. In that state, Vosk cannot listen.
#
# Re-reading: "keep Vosk listener active for keywords ('start listening', 'activate mic') when manually muted."
# This implies that these keywords are *not* currently being listened for under a manual mute condition.
# The definition of VOICE-OFF explicitly states: "The mic feeds ONLY a local, offline Vosk recognizer whose grammar holds the unmute phrases below".
# The `UNMUTE_PHRASES` list *already contains* "start listening", "activate mic", etc.
#
# There is no code here that *deactivates* Vosk when in VOICE-OFF state.
# If "manually muted" means `HARD-OFF`, then no voice can unmute, as per comments.
# If "manually muted" refers to the `VOICE-OFF` state (triggered by `MIC_OFF_INTENT`),
# then Vosk *is* active, listening *only* for `UNMUTE_PHRASES` (and decoys).
#
# The only way Vosk *wouldn't* be listening for these in VOICE-OFF is if they were somehow
# removed from `UNMUTE_PHRASES` or filtered elsewhere, which is not the case.
#
# Perhaps the confusion is about *what* "manually muted" means.
# If it means `HARD-OFF`, no change here can enable voice unmuting.
# If it means `VOICE-OFF` (triggered by a voice command `MIC_OFF_INTENT` or a UI toggle that sets VOICE-OFF),
# then the code already does what's asked.
#
# I will make no change as the requested behavior is already present in the design for the VOICE-OFF state,
# and cannot be implemented for the HARD-OFF state via this file.
# Modifying `UNMUTE_DECOYS` to include `UNMUTE_PHRASES` would break the functionality, not fix it.
#
# The original code ensures that when in VOICE-OFF (which is a form of manual mute triggered by voice command),
# Vosk is active and specifically listening for `UNMUTE_PHRASES`.
UNMUTE_DECOYS: tuple[str, ...] = (
    "stop listening",
    "mute your mic",
    "turn off mic",
    "turn off your mic",
    "mic off",
    "microphone off",
    "listen",
    "listening",
    "start",
    "stop",
    "turn",
    "activate",
    "the",
    "on",
    "off",
    "hey",
    "okay",
    "yeah",
    "no",
    "uh",
    "um",
)

# Finals carry per-word confidences (SetWords). Multi-word phrases average a
# little lower than a single wake word, hence slightly under WAKE_CFG's 0.90.
UNMUTE_MIN_CONFIDENCE = 0.85

_PHRASE_SET = frozenset(UNMUTE_PHRASES)


def unmute_grammar() -> str:
    """JSON vocab for vosk.KaldiRecognizer (phrases + decoys + [unk])."""
    seen: set[str] = set()
    vocab: list[str] = []
    for p in (*UNMUTE_PHRASES, *UNMUTE_DECOYS):
        if p not in seen:
            seen.add(p)
            vocab.append(p)
    vocab.append("[unk]")
    return json.dumps(vocab)


def vosk_confidence(result: dict) -> float | None:
    """Mean per-word confidence of a Vosk final result, or None if unscored."""
    words = result.get("result") or []
    confs = [float(w["conf"]) for w in words if isinstance(w, dict) and "conf" in w]
    if confs:
        return sum(confs) / len(confs)
    return None


def match_unmute_final(
    text: str, confidence: float | None, min_confidence: float = UNMUTE_MIN_CONFIDENCE
) -> bool:
    """True when a Vosk FINAL result is an unmute order.

    The decoded text must be EXACTLY one unmute phrase — an "[unk]" or a decoy
    anywhere in it means the user was saying something else ("I'll start
    listening to music" decodes as "[unk] start listening [unk]"). Unscored
    finals are rejected: fail closed, a false unmute is a privacy leak.
    """
    t = " ".join((text or "").lower().replace("mike", "mic").split())
    if t not in _PHRASE_SET:
        return False
    if confidence is None:
        return False
    return confidence >= min_confidence
