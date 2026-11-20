"""Media-only speaker discrimination without enabling the global voice lock.

Quiet input passes immediately. During playback retain complete utterances for
speaker scoring, with a confident local Era address as a convenience fallback.
Unknown identity is explicitly fail-open for this convenience filter; the
separate security gate runs first and keeps its own policy.
"""

from __future__ import annotations

from core.logger import log_event, main_logger
from core.speaker_recognition import SpeakerVerifier, VerifyResult, get_verifier
from core.voice_gate import PENDING, UtteranceGate


class _MediaUtteranceGate(UtteranceGate):
    def _submit(self, pcm: bytes, purpose: str = "command") -> None:
        # A locally addressed utterance must finish rather than losing its tail
        # when the rolling window no longer contains the opening name.
        from core.voice_gate import ACCEPTED

        if (
            self.state == ACCEPTED
            and self.last_result is not None
            and self.last_result.reason == "media filter: explicit local Era address"
        ):
            self._job = self.last_result
            return
        super()._submit(pcm, purpose)

    def release_pending(self) -> list[bytes]:
        # A media meter can go quiet mid-request. Preserve unverified onset
        # when leaving this convenience filter; rejected playback stays dropped.
        with self._lock:
            pending = list(self._buf) if self.state == PENDING else []
            self._reset()
            return pending


class _MediaVerifier:
    enabled = True

    def __init__(self, verifier, recognizer_factory=None):
        self.verifier = verifier
        self._make_rec = recognizer_factory
        self.settings = dict(verifier.settings)
        # Short requests must not be thrown away before the verifier can report
        # insufficient evidence. No unscored follow-up inherits an owner verdict.
        self.settings["min_command_speech_s"] = 0.0
        self.settings["followup_window_s"] = 0.0

    def readiness(self):
        return True, "media filter degrades to unverified input when unavailable"

    def warm_up(self):
        self.verifier.warm_up()

    def verify(self, pcm, purpose="command"):
        try:
            result = self.verifier.verify(pcm, purpose)
        except Exception as exc:
            result = VerifyResult(False, None, f"media verifier error: {type(exc).__name__}")
        known = result.score is not None
        # This optional playback filter must not make an explicit address
        # impossible when a short/noisy voiceprint scores poorly. The global
        # security gate runs first and is never bypassed here.
        addressed = False
        if known and not result.accepted and self._make_rec is not None:
            try:
                from core.attention import _final_words, _named, _names, get_listening_config

                rec = self._make_rec()
                rec.AcceptWaveform(pcm)
                addressed = _named(
                    _final_words(rec), _names(), get_listening_config()["name_confidence"]
                )
            except Exception:
                pass  # Unknown/failed name recognition cannot override a mismatch.
        log_event(
            main_logger,
            "voice.media.verdict",
            "INFO" if known else "WARNING",
            identity_known=known,
            accepted=(result.accepted or addressed) if known else True,
            score=result.score,
            reason=result.reason,
            explicitly_addressed=addressed,
        )
        if addressed:
            return VerifyResult(True, result.score, "media filter: explicit local Era address")
        return (
            result if known else VerifyResult(True, None, "media identity unknown; input retained")
        )


class MediaVoiceGate:
    def __init__(
        self,
        *,
        verifier=None,
        media_active=None,
        synchronous=False,
        wearable=False,
        recognizer_factory=None,
    ):
        if media_active is None:
            from core.media_activity import media_playing

            media_active = media_playing
        self.media_active = media_active
        if verifier is None:
            settings = dict(get_verifier().settings)
            settings["enabled"] = True
            verifier = SpeakerVerifier(settings=settings)
            # UtteranceGate preloads on its worker; no persisted lock change.
        self._proxy = _MediaVerifier(verifier, recognizer_factory)
        self._gate = _MediaUtteranceGate(
            self._proxy, synchronous=synchronous, followup_allowed=lambda: False
        )
        self._gate.configure_input(wearable)
        self._active = False

    def configure_input(self, wearable: bool) -> None:
        self._gate.configure_input(wearable)

    def feed(self, frame: bytes) -> list[bytes]:
        try:
            active = bool(self.media_active())
        except Exception:
            active = False
        if not active:
            pending = []
            if self._active:
                pending = self._gate.release_pending()
            self._active = False
            return [*pending, frame]
        self._active = True
        return self._gate.feed(frame)
