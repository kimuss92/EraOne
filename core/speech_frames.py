"""PCM-compatible speech metadata and per-turn playback evidence."""

from dataclasses import dataclass

from core.input_envelope import InputEnvelope


class EnginePCM(bytes):
    def __new__(cls, pcm: bytes, engine: str):
        instance = super().__new__(cls, pcm)
        instance.engine = engine
        return instance


@dataclass(frozen=True)
class SpeechPhrase:
    id: str
    text: str
    engine: str
    owner: InputEnvelope | None = None


@dataclass
class NativeSpeechTurn:
    owner: InputEnvelope
    queued_bytes: int = 0
    played_bytes: int = 0
    failed: bool = False
    revealed_text: str = ""

    def text_delta(self, text: str) -> str | None:
        """Reject revisions to already revealed text instead of duplicating it."""
        if text == self.revealed_text:
            return ""
        prefix = self.revealed_text + " " if self.revealed_text else ""
        if not text.startswith(prefix):
            self.failed = True
            return None
        delta = text[len(prefix) :]
        self.revealed_text = text
        return delta


class NativeSpeechFrame(bytes):
    def __new__(cls, pcm: bytes, turn: NativeSpeechTurn):
        instance = super().__new__(cls, pcm)
        instance.turn = turn
        turn.queued_bytes += len(pcm)
        return instance


class NativeSpeechEnd(bytes):
    def __new__(cls, turn: NativeSpeechTurn, text: str):
        instance = super().__new__(cls, b"")
        instance.turn = turn
        instance.text = text
        return instance


class NativeSpeechCheckpoint(bytes):
    """Transcript snapshot ordered after the audio received so far."""

    def __new__(cls, turn: NativeSpeechTurn, text: str):
        instance = super().__new__(cls, b"")
        instance.turn = turn
        instance.text = text
        instance.audio_bytes = turn.queued_bytes
        return instance


class SpeechEnd(bytes):
    """Zero-PCM marker consumed after an owned reply's final frame."""

    def __new__(cls, owner: InputEnvelope):
        instance = super().__new__(cls, b"")
        instance.owner = owner
        return instance


class SpeechFrame(bytes):
    def __new__(cls, pcm: bytes, phrase: SpeechPhrase, *, first: bool, last: bool):
        instance = super().__new__(cls, pcm)
        instance.phrase = phrase
        instance.first = first
        instance.last = last
        return instance
