import difflib
import re
import time
from dataclasses import dataclass

from core.era_state import EraState

CLARIFICATION_PHRASE = "Sorry, I didn't catch that. Can you repeat?"

WINDOWS_PATH_RE = re.compile(r'(?i)([a-z]:[\\/][^\r\n"]+)')
QUOTED_WINDOWS_PATH_RE = re.compile(r'(?i)"([a-z]:[\\/][^"]+)"')
UNC_PATH_RE = re.compile(r'(\\\\[^\\/\s]+[\\/][^\r\n"]+)')

SAFE_SHORT_COMMANDS = {
    "next",
    "previous",
    "prev",
    "next track",
    "next song",
    "skip track",
    "previous track",
    "previous song",
    "prev track",
    "pause",
    "play",
    "resume",
    "stop",
    "mute",
    "unmute",
    "volume up",
    "volume down",
    "louder",
    "quieter",
    "increase volume",
    "decrease volume",
}

VAGUE_SHORT_INPUTS = {"volume", "go", "vá", "read", "open", "that", "this"}

# Short replies that answer something Era just said. For SPOKEN input they are
# only trusted while a confirmation is pending (a misheard fragment must not be
# read as consent). For TYPED input they are always a reply — see
# is_conversational_reply() for why the two channels differ here.
CONFIRMATION_REPLIES = {
    # affirmative
    "yes",
    "yes please",
    "yeah",
    "yep",
    "yup",
    "sure",
    "okay",
    "ok",
    "of course",
    "affirmative",
    "proceed",
    "continue",
    "carry on",
    "confirm",
    "confirmed",
    "go ahead",
    "go on",
    "do it",
    "do it now",
    "go for it",
    "try again",
    "retry",
    "again",
    "please do",
    # negative
    "no",
    "nope",
    "not now",
    "no thanks",
    "never mind",
    "nevermind",
    "forget it",
    "cancel",
    "stop",
    "abort",
    "leave it",
    # French — the owner switches languages mid-conversation and a reply typed in
    # French is exactly as much a reply as its English twin.
    "oui",
    "ouais",
    "vas-y",
    "vas y",
    "allez",
    "d'accord",
    "daccord",
    "bien sur",
    "bien sûr",
    "fais-le",
    "fais le",
    "non",
    "annule",
    "annuler",
    "arrete",
    "arrête",
    "laisse tomber",
}

FILLER_WORDS = {"hmm", "uh", "um", "ah", "oh", "er", "mm", "huh"}

# Verbs / openers that mark an input as an actual instruction or question rather
# than a chunk of pasted data. Used by classify_typed_input to tell "do X" apart
# from "here is some text/a link/a path" (which needs a "what should I do with
# this?" clarification instead of being silently forwarded).
COMMAND_VERBS = {
    "open",
    "launch",
    "start",
    "run",
    "find",
    "search",
    "locate",
    "show",
    "reveal",
    "delete",
    "remove",
    "trash",
    "move",
    "copy",
    "rename",
    "save",
    "bookmark",
    "remember",
    "list",
    "read",
    "write",
    "edit",
    "create",
    "make",
    "play",
    "pause",
    "resume",
    "stop",
    "skip",
    "next",
    "previous",
    "send",
    "call",
    "text",
    "message",
    "email",
    "whatsapp",
    "summarize",
    "summarise",
    "improve",
    "rewrite",
    "reword",
    "translate",
    "explain",
    "fix",
    "correct",
    "check",
    "download",
    "upload",
    "close",
    "mute",
    "unmute",
    "set",
    "turn",
    "increase",
    "decrease",
    "lower",
    "raise",
    "volume",
    "weather",
    "remind",
    "schedule",
    "note",
    "add",
    "go",
    "navigate",
    "browse",
    "shutdown",
    "standby",
    "wake",
    "define",
    "calculate",
    "convert",
    "generate",
    "draft",
    "minimize",
    "maximize",
    "switch",
    "take",
    "lock",
    "watch",
    "listen",
    "put",
    # French — the owner types orders in both languages.
    "ouvre",
    "ouvrir",
    "lance",
    "lancer",
    "ferme",
    "fermer",
    "joue",
    "jouer",
    "mets",
    "mettre",
    "cherche",
    "chercher",
    "envoie",
    "envoyer",
    "montre",
    "affiche",
    "baisse",
    "monte",
    "augmente",
    "coupe",
    "active",
    "désactive",
    "desactive",
    "prends",
    "fais",
    "écris",
    "ecris",
    "enregistre",
    "rappelle",
    "supprime",
}

# A pasted block only counts as DATA past this many words. Below it, text is
# read as the user's own words, the same as a spoken sentence of that length.
TEXT_BLOB_MIN_WORDS = 50

QUESTION_WORDS = {
    "what",
    "whats",
    "who",
    "whos",
    "when",
    "where",
    "why",
    "how",
    "which",
    "whose",
    "can",
    "could",
    "should",
    "would",
    "is",
    "are",
    "am",
    "do",
    "does",
    "did",
    "will",
    "tell",
    "give",
    "help",
    "please",
    "peux",
    "pourrais",
    "quel",
    "quelle",
    "quoi",
    "comment",
    "pourquoi",
    "où",
    "quand",
    "est-ce",
}

URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|io|ai|co|gov|edu|app|dev|fr|tn|uk|de)\b\S*)",
    re.IGNORECASE,
)

CLARIFICATION_THRESHOLD = 0.6
RECENT_BLOCK_WINDOW = 5.0
MAX_RECENT_BLOCKS = 3


@dataclass
class SafetyDecision:
    action: str  # ALLOW, BLOCK_SILENTLY, ASK_CLARIFICATION
    reason: str
    reason_code: str
    clarification_needed: bool = False
    confidence_score: float | None = None
    normalized_transcript: str = ""


class InputSafetyGate:
    def __init__(self):
        self._recent_blocked_inputs: list[tuple[str, float]] = []

    # ── Public API ────────────────────────────────────────────────

    def is_conversational_reply(self, text: str) -> bool:
        """True when `text` is nothing but an answer to something Era just said.

        TYPED input is deliberate: nobody types "proceed" by accident, and the
        live model — which has the conversation — is far better placed to read it
        in context than a word-list here is. Intercepting it was the whole bug
        behind the "yes" -> "that looks like a word of agreement, sir; what would
        you like to do with it?" loop: the reply never reached the model that had
        asked the question. Spoken input still goes through the pending-only rule
        in validate_input(), because a misheard fragment must never read as
        consent to an action.
        """
        return self.normalize_transcript(text) in CONFIRMATION_REPLIES

    def validate_input(
        self,
        transcript: str,
        confidence: float | None = None,
        state_snapshot: dict | None = None,
        audio_metadata: dict | None = None,
        typed: bool = False,
    ) -> SafetyDecision:
        """Decide what to do with one user input.

        `typed=True` marks input the user WROTE (command bar, WebSocket, Telegram)
        rather than spoke. That distinction matters because most of the rules below
        exist to defend against speech-recognition artefacts — fillers, noise,
        low-confidence decodes, the same misheard phrase arriving three times — and
        none of those can happen to a keystroke. Applying them to text is what made
        the typed channel behave differently from the spoken one, which is the
        opposite of the design: the two are meant to be interchangeable.
        """
        if self._contains_raw_path(transcript):
            return SafetyDecision(
                action="ALLOW",
                reason="Raw file/folder path detected",
                reason_code="RAW_PATH_DETECTED",
                normalized_transcript=transcript.strip(),
            )

        normalized = self.normalize_transcript(transcript)
        snapshot = state_snapshot or {}

        state_check = self._check_state(snapshot, typed=typed)
        if state_check is not None:
            state_check.normalized_transcript = normalized
            return state_check

        if not normalized:
            return SafetyDecision(
                action="BLOCK_SILENTLY",
                reason="Empty transcript after normalization",
                reason_code="EMPTY_TRANSCRIPT",
                normalized_transcript=normalized,
            )

        # A short affirmation/negation ("proceed", "yes", "cancel") is an ANSWER —
        # it must reach the model that asked, not die at the SHORT_UNCLEAR guard
        # below.
        #
        # Spoken: only while something is actually awaiting a reply, because a
        # misheard fragment must never read as consent to an action.
        # Typed: always. Era asks conversational questions constantly ("should I
        # try again?") and only tool-gated ones register in the confirmation
        # store, so the pending flag was False for most real questions — and the
        # user's "Yes" came back as "that looks like a word of agreement, sir;
        # what would you like to do with it?". Nobody types "proceed" by accident.
        awaiting = snapshot.get("pending_confirmation_active") or snapshot.get("awaiting_reply")
        if (typed or awaiting) and normalized in CONFIRMATION_REPLIES:
            return SafetyDecision(
                action="ALLOW",
                reason=f"Confirmation reply: '{normalized}'",
                reason_code="CONFIRMATION_REPLY",
                normalized_transcript=normalized,
            )

        # "open", "read", "that" are vague on their own — and are precisely the
        # answers to "open it or read it?" / "which one?". With a question of
        # Era's outstanding, asking the user to clarify their clarification is
        # the loop this guard was meant to prevent.
        #
        # Spoken only. A spoken "open" reaches the live model raw and she asks
        # "open what?" herself, with the whole conversation in hand; a TYPED one
        # must get exactly that treatment, not a canned intercept.
        if not typed and normalized in VAGUE_SHORT_INPUTS and not awaiting:
            return SafetyDecision(
                action="ASK_CLARIFICATION",
                reason=f"Vague short input: '{normalized}'",
                reason_code="VAGUE_SHORT_INPUT",
                clarification_needed=True,
                normalized_transcript=normalized,
            )

        if normalized in SAFE_SHORT_COMMANDS:
            return SafetyDecision(
                action="ALLOW",
                reason=f"Safe short command: '{normalized}'",
                reason_code="SAFE_SHORT_COMMAND",
                normalized_transcript=normalized,
            )

        if self._is_weather_command(normalized):
            if self._has_city_conflict(normalized):
                return SafetyDecision(
                    action="ASK_CLARIFICATION",
                    reason="City name conflict in weather command",
                    reason_code="WEATHER_CITY_CONFLICT",
                    clarification_needed=True,
                    normalized_transcript=normalized,
                )
            return SafetyDecision(
                action="ALLOW",
                reason=f"Weather command: '{normalized}'",
                reason_code="WEATHER_COMMAND",
                normalized_transcript=normalized,
            )

        # Fillers and noise are microphone artefacts. A keyboard produces neither,
        # so dropping typed input for "looking like" one is dropping something the
        # user meant — "ok" and "eh" are two-character words a person types on
        # purpose, and BLOCK_SILENTLY means they get no answer at all.
        if not typed:
            if normalized in FILLER_WORDS or self._is_single_filler(normalized):
                return SafetyDecision(
                    action="BLOCK_SILENTLY",
                    reason=f"Filler word: '{normalized}'",
                    reason_code="FILLER_WORD",
                    normalized_transcript=normalized,
                )

            if self._is_noise_like(normalized):
                return SafetyDecision(
                    action="BLOCK_SILENTLY",
                    reason=f"Noise-like input: '{normalized}'",
                    reason_code="NOISE_INPUT",
                    normalized_transcript=normalized,
                )

        if self._looks_like_url_or_path(normalized):
            return SafetyDecision(
                action="ASK_CLARIFICATION",
                reason="URL or path without clear command",
                reason_code="URL_WITHOUT_COMMAND",
                clarification_needed=True,
                normalized_transcript=normalized,
            )

        if confidence is not None and confidence < CLARIFICATION_THRESHOLD:
            return SafetyDecision(
                action="ASK_CLARIFICATION",
                reason=f"Low confidence ({confidence:.2f})",
                reason_code="LOW_CONFIDENCE",
                clarification_needed=True,
                confidence_score=confidence,
                normalized_transcript=normalized,
            )

        words = normalized.split()
        # A short input that opens with a real command verb ("play music",
        # "open browser") is not unclear — classify_typed_input() already
        # recognizes this via COMMAND_VERBS, but this gate didn't, so a typed
        # two-word command like "play music" fell through to SHORT_UNCLEAR and
        # got handed to the model as an ambiguous fragment instead of executing.
        #
        # A short reply while Era has a question open is an ANSWER, whatever its
        # words — "Delta Cuisine" answers "which brand?", "the second one" answers
        # "which file?". Only the model knows what it asked, so with a question
        # outstanding this guard steps aside and lets the answer reach it.
        #
        # Spoken only. A verb list can never cover what the user actually types —
        # "minimize spotify", "ouvre spotify", "bluetooth off", "netflix" — and
        # every miss here was a typed order Era answered with a question instead
        # of executing, while the SAME words spoken went straight to the model
        # and ran. Typed input is deliberate; the model decides, as for voice.
        if not typed and len(words) <= 2 and words[0] not in COMMAND_VERBS and not awaiting:
            return SafetyDecision(
                action="ASK_CLARIFICATION",
                reason=f"Short unclear input: '{normalized}'",
                reason_code="SHORT_UNCLEAR",
                clarification_needed=True,
                normalized_transcript=normalized,
            )

        # Repetition-suppression is an ASR defence: the same misheard phrase
        # arriving three times in five seconds is one noise source, not three
        # requests. Typed repetition means the OPPOSITE — the user is retyping
        # because Era did not act, and silently eating the third attempt is the
        # worst possible answer to that.
        if not typed and self._is_recently_blocked(normalized):
            return SafetyDecision(
                action="BLOCK_SILENTLY",
                reason=f"Similar input recently blocked: '{normalized}'",
                reason_code="RECENTLY_BLOCKED",
                normalized_transcript=normalized,
            )

        return SafetyDecision(
            action="ALLOW",
            reason="Input passed all safety checks",
            reason_code="CLEAR_INPUT",
            normalized_transcript=normalized,
        )

    # ── Typed/pasted input classification ─────────────────────────

    def classify_typed_input(self, text: str, state_snapshot: dict | None = None) -> str:
        """Coarse-classify a TYPED (or pasted) input so Era can ASK what to do
        with data she must not act on blindly, instead of silently forwarding it
        to the model (which then ignores a bare path / link / pasted blob).

        Returns one of:
          COMMAND   - anything the live model should read as the user's own words,
                      exactly as if they had SPOKEN it. The default.
          PATH      - a bare file/folder path with no instruction attached.
          URL       - a bare web link with no instruction attached.
          TEXT_BLOB - a long (> TEXT_BLOB_MIN_WORDS) pasted block with no command
                      intent — a draft, a spec — that a microphone cannot produce.

        Everything short is a COMMAND. There used to be an UNCLEAR class for
        short verb-less input, wrapped as "DATA — do NOT execute it", and a word
        list can't know that "minimize spotify" or "ouvre netflix" is an order.
        That wrapper was the reason a typed or Telegram order was answered with a
        question while the same words spoken were executed.
        """
        snapshot = state_snapshot or {}
        stripped = (text or "").strip()
        if not stripped:
            return "COMMAND"  # let the empty-string guard in validate_input handle it

        # Check URL before path: "https://host/x" contains "s:/", which the raw
        # path regex would otherwise mis-read as a Windows path.
        has_url = bool(URL_RE.search(text))
        if has_url and not self._has_command_verb(text):
            return "URL"

        # A path with a verb ("open D:\x") is a command; a bare path is data.
        # (Skip when the text is really a URL that just tripped the path regex.)
        if not has_url and self._contains_raw_path(text):
            return "COMMAND" if self._has_command_verb(text) else "PATH"

        normalized = self.normalize_transcript(text)

        # Answers that are only meaningful in-context stay commands. Unconditional
        # for CONFIRMATION_REPLIES, and for anything short while a question is
        # open: this runs on TYPED input only, where a bare "proceed" is always
        # aimed at Era and never a chunk of pasted data. Gating it on the
        # confirmation store was why "Proceed" came back wrapped as data — most of
        # Era's questions are conversational and register nothing in that store.
        if normalized in CONFIRMATION_REPLIES:
            return "COMMAND"
        if snapshot.get("awaiting_reply") and len(normalized.split()) <= 4:
            return "COMMAND"
        if normalized in SAFE_SHORT_COMMANDS or self._is_weather_command(normalized):
            return "COMMAND"

        # A URL with a command verb ("open example.com") is a command; fall through.

        words = normalized.split()
        first = words[0] if words else ""
        has_intent = (
            first in COMMAND_VERBS
            or any(w in QUESTION_WORDS for w in words[:2])
            or any(w in COMMAND_VERBS for w in words[:3])
        )
        if has_intent:
            return "COMMAND"

        # No recognised intent. Only a genuinely long block reads as pasted
        # material; anything a person would say in one breath goes to the model
        # untouched, which is precisely how the spoken path treats it.
        if len(words) > TEXT_BLOB_MIN_WORDS:
            return "TEXT_BLOB"
        return "COMMAND"

    def _has_command_verb(self, text: str) -> bool:
        words = set(self.normalize_transcript(text).split())
        return bool(words & COMMAND_VERBS)

    def _is_bare_url(self, text: str) -> bool:
        if not URL_RE.search(text):
            return False
        return not self._has_command_verb(text)

    def record_blocked(self, transcript: str):
        now = time.time()
        self._recent_blocked_inputs.append((transcript, now))
        cutoff = now - RECENT_BLOCK_WINDOW
        self._recent_blocked_inputs = [
            (t, ts) for t, ts in self._recent_blocked_inputs if ts > cutoff
        ]

    # ── Normalization ─────────────────────────────────────────────

    def normalize_transcript(self, text: str) -> str:
        if not text:
            return ""
        text = text.lower().strip()
        text = re.sub(r"[^\w\s'-]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _contains_raw_path(self, text: str) -> bool:
        """Detect raw Windows/UNC paths before normalization."""
        if QUOTED_WINDOWS_PATH_RE.search(text):
            return True
        if WINDOWS_PATH_RE.search(text):
            return True
        if UNC_PATH_RE.search(text):
            return True
        return False

    # ── State checks ──────────────────────────────────────────────

    def _check_state(self, snapshot: dict, *, typed: bool = False) -> SafetyDecision | None:
        state = snapshot.get("current_state")
        # Mute silences the MICROPHONE. It has never meant "stop reading what I
        # write", and applying it to text made Era unreachable from Telegram
        # whenever the mic happened to be off at the desk — silently, with no
        # reply, which from another network is indistinguishable from her being
        # down. The audio path keeps its own mute gate in _send_realtime.
        is_muted = snapshot.get("is_muted", False) and not typed

        if is_muted:
            return SafetyDecision(
                action="BLOCK_SILENTLY",
                reason="Era One is muted",
                reason_code="MUTED",
            )

        if state == EraState.STANDBY:
            wake_active = snapshot.get("wake_word_active", False)
            if not wake_active:
                return SafetyDecision(
                    action="BLOCK_SILENTLY",
                    reason="Standby without wake word",
                    reason_code="STANDBY_NO_WAKE",
                )

        if state == EraState.RECONNECTING:
            return SafetyDecision(
                action="BLOCK_SILENTLY",
                reason="Era One is reconnecting",
                reason_code="RECONNECTING",
            )

        return None

    # ── Validation helpers ────────────────────────────────────────

    def is_safe_short_command(self, text: str) -> bool:
        return self.normalize_transcript(text) in SAFE_SHORT_COMMANDS

    def _is_weather_command(self, text: str) -> bool:
        return text.startswith("weather") or text in (
            "read weather",
            "weather today",
            "weather now",
        )

    def _has_city_conflict(self, text: str) -> bool:
        has_tunis = "tunis" in text
        has_turin = "turin" in text
        return has_tunis and has_turin

    def _is_single_filler(self, text: str) -> bool:
        words = text.split()
        return len(words) == 1 and words[0] in FILLER_WORDS

    def _is_noise_like(self, text: str) -> bool:
        noise_patterns = [
            r"^[aeiou]+$",
            r"^h+m+$",
            r"^[ck]h+$",
            r"^(ha|he|hu|ho)+$",
            r"^(cough|ahem|clears? throat)",
        ]
        for pattern in noise_patterns:
            if re.match(pattern, text):
                return True

        if len(text) <= 2 and text not in SAFE_SHORT_COMMANDS:
            return True

        return False

    def _looks_like_url_or_path(self, text: str) -> bool:
        windows_path = re.search(r"^[a-z]:[\\/]", text) or re.search(r"[a-z]:[\\/]", text)
        has_command_verb = bool(
            re.search(r"\b(open|find|search|show|list|create|write|copy|move|delete|read)\b", text)
        )

        if windows_path:
            return not has_command_verb

        url_patterns = [
            r"\.[a-z]{2,}(?:\/|$)",
            r"^https?://",
            r"^www\.",
            r"^/[a-z]",
        ]
        for pattern in url_patterns:
            if re.search(pattern, text):
                if has_command_verb:
                    return False
                return True
        return False

    def _is_recently_blocked(self, text: str) -> bool:
        now = time.time()
        count = 0
        for blocked_text, ts in self._recent_blocked_inputs:
            if now - ts <= RECENT_BLOCK_WINDOW:
                distance = self._text_distance(text, blocked_text)
                if distance <= 0.4:
                    count += 1
        return count >= MAX_RECENT_BLOCKS

    def _text_distance(self, a: str, b: str) -> float:
        """Normalized similarity distance between 0.0 (identical) and 1.0 (no overlap)."""
        if not a or not b:
            return 1.0
        a_lower = a.lower().strip()
        b_lower = b.lower().strip()
        if a_lower == b_lower:
            return 0.0
        return 1.0 - difflib.SequenceMatcher(None, a_lower, b_lower).ratio()
