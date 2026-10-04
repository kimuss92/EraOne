import pytest

from core.era_state import EraState
from core.input_safety_gate import InputSafetyGate, SafetyDecision


@pytest.fixture
def gate():
    return InputSafetyGate()


def make_state(
    current_state=EraState.LISTENING,
    is_muted=False,
    wake_word_active=False,
    is_reconnecting=False,
):
    return {
        "current_state": current_state,
        "is_muted": is_muted,
        "wake_word_active": wake_word_active,
        "is_reconnecting": is_reconnecting,
        "standby_active": current_state == EraState.STANDBY,
        "is_speaking": current_state == EraState.SPEAKING,
        "interruption_allowed": False,
        "last_wake_word_timestamp": None,
        "last_reconnect_timestamp": None,
    }


class TestStateChecks:
    def test_blocked_muted_state(self, gate):
        state = make_state(is_muted=True)
        d = gate.validate_input("play music", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"
        assert d.reason_code == "MUTED"

    def test_blocked_standby_no_wake_word(self, gate):
        state = make_state(current_state=EraState.STANDBY, wake_word_active=False)
        d = gate.validate_input("play music", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"
        assert d.reason_code == "STANDBY_NO_WAKE"

    def test_allowed_standby_with_wake_word(self, gate):
        state = make_state(current_state=EraState.STANDBY, wake_word_active=True)
        d = gate.validate_input("next", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_blocked_reconnecting(self, gate):
        state = make_state(current_state=EraState.RECONNECTING)
        d = gate.validate_input("play music", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"
        assert d.reason_code == "RECONNECTING"


class TestEmptyAndFiller:
    def test_blocked_empty_transcript(self, gate):
        state = make_state()
        d = gate.validate_input("", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"

    def test_blocked_whitespace_transcript(self, gate):
        state = make_state()
        d = gate.validate_input("   ", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"

    def test_blocked_filler_hmm(self, gate):
        state = make_state()
        d = gate.validate_input("hmm", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"

    def test_blocked_filler_uh(self, gate):
        state = make_state()
        d = gate.validate_input("uh", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"

    def test_blocked_va_fragment(self, gate):
        state = make_state()
        d = gate.validate_input("vá", state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"


class TestSafeShortCommands:
    def test_allowed_next(self, gate):
        state = make_state()
        d = gate.validate_input("next", state_snapshot=state)
        assert d.action == "ALLOW"
        assert d.reason_code == "SAFE_SHORT_COMMAND"

    def test_allowed_pause(self, gate):
        state = make_state()
        d = gate.validate_input("pause", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_resume(self, gate):
        state = make_state()
        d = gate.validate_input("resume", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_play(self, gate):
        state = make_state()
        d = gate.validate_input("play", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_stop(self, gate):
        state = make_state()
        d = gate.validate_input("stop", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_mute(self, gate):
        state = make_state()
        d = gate.validate_input("mute", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_unmute(self, gate):
        state = make_state()
        d = gate.validate_input("unmute", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_volume_up(self, gate):
        state = make_state()
        d = gate.validate_input("volume up", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_volume_down(self, gate):
        state = make_state()
        d = gate.validate_input("volume down", state_snapshot=state)
        assert d.action == "ALLOW"


class TestVagueShortCommands:
    def test_vague_volume_asks_clarification(self, gate):
        state = make_state()
        d = gate.validate_input("volume", state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"
        assert d.clarification_needed

    def test_vague_go_asks_clarification(self, gate):
        state = make_state()
        d = gate.validate_input("go", state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"

    def test_vague_read_asks_clarification(self, gate):
        state = make_state()
        d = gate.validate_input("read", state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"


class TestWeatherCommands:
    def test_allowed_weather_tunis(self, gate):
        state = make_state()
        d = gate.validate_input("weather Tunis", state_snapshot=state)
        assert d.action == "ALLOW"
        assert d.reason_code == "WEATHER_COMMAND"

    def test_allowed_weather_today(self, gate):
        state = make_state()
        d = gate.validate_input("weather today", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_read_weather(self, gate):
        state = make_state()
        d = gate.validate_input("read weather", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_tunis_turin_conflict_asks_clarification(self, gate):
        state = make_state()
        d = gate.validate_input("weather in Tunis not Turin", state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"
        assert d.reason_code == "WEATHER_CITY_CONFLICT"


class TestConfidence:
    def test_confidence_none_does_not_auto_block(self, gate):
        state = make_state()
        d = gate.validate_input("play some music", confidence=None, state_snapshot=state)
        assert d.action == "ALLOW"

    def test_low_confidence_asks_clarification(self, gate):
        state = make_state()
        d = gate.validate_input("play some music", confidence=0.3, state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"
        assert d.reason_code == "LOW_CONFIDENCE"

    def test_high_confidence_allows(self, gate):
        state = make_state()
        d = gate.validate_input("play some music", confidence=0.85, state_snapshot=state)
        assert d.action == "ALLOW"


class TestGeneralInput:
    def test_allowed_clear_command(self, gate):
        state = make_state()
        d = gate.validate_input("open Spotify and play my liked songs", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_allowed_general_chat(self, gate):
        state = make_state()
        d = gate.validate_input("what is the capital of France", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_short_unclear_asks_clarification(self, gate):
        state = make_state()
        d = gate.validate_input("do it", state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"

    def test_url_without_command_asks_clarification(self, gate):
        state = make_state()
        d = gate.validate_input("example.com", state_snapshot=state)
        assert d.action == "ASK_CLARIFICATION"


class TestNormalization:
    def test_normalize_lowercase(self, gate):
        assert gate.normalize_transcript("Play Music") == "play music"

    def test_normalize_trim_spaces(self, gate):
        assert gate.normalize_transcript("  play music  ") == "play music"

    def test_is_safe_short_command_true(self, gate):
        assert gate.is_safe_short_command("next") is True

    def test_is_safe_short_command_false(self, gate):
        assert gate.is_safe_short_command("volume") is False


class TestRecentBlockHistory:
    def test_repeated_unclear_blocked_silently(self, gate):
        state = make_state()
        for _ in range(3):
            gate.record_blocked("some unclear text")
        d = gate.validate_input("some unclear text", state_snapshot=state)
        assert d.action == "BLOCK_SILENTLY"
        assert d.reason_code == "RECENTLY_BLOCKED"


class TestTypedParity:
    """The typed channel must behave like the spoken one.

    Every rule exercised here exists to defend against SPEECH-recognition
    artefacts. Applying it to a keystroke is what produced the Telegram loop
    where "Yes" came back as "that looks like a word of agreement, sir; what
    would you like to do with it?" — the reply never reached the model that had
    asked the question.
    """

    @pytest.mark.parametrize("reply", ["Yes", "Proceed", "no", "go ahead", "oui", "vas-y"])
    def test_typed_confirmation_reply_reaches_the_model(self, gate, reply):
        # Deliberately NO pending confirmation: most of Era's questions are
        # conversational ("should I try again?") and register nothing in the
        # confirmation store, which is exactly why gating on it failed.
        d = gate.validate_input(reply, state_snapshot=make_state(), typed=True)
        assert d.action == "ALLOW"
        assert d.reason_code == "CONFIRMATION_REPLY"

    def test_spoken_confirmation_reply_still_needs_a_pending_action(self, gate):
        # The spoken path keeps the strict rule: a misheard fragment must never
        # read as consent.
        d = gate.validate_input("yes", state_snapshot=make_state())
        assert d.action == "ASK_CLARIFICATION"

    def test_spoken_confirmation_reply_allowed_while_pending(self, gate):
        state = make_state()
        state["pending_confirmation_active"] = True
        d = gate.validate_input("yes", state_snapshot=state)
        assert d.action == "ALLOW"

    def test_short_answer_allowed_while_era_has_a_question_open(self, gate):
        # "Delta Cuisine" is two words and no command verb — SHORT_UNCLEAR by the
        # letter of the rule, and the answer to "which brand?" by any other read.
        state = make_state()
        state["awaiting_reply"] = True
        d = gate.validate_input("Delta Cuisine", state_snapshot=state, typed=True)
        assert d.action == "ALLOW"

    def test_vague_word_is_an_answer_while_a_question_is_open(self, gate):
        # "read" answers "open it or read it?". Asking the user to clarify their
        # clarification is the loop this guard exists to prevent.
        state = make_state()
        assert gate.validate_input("read", state_snapshot=state).reason_code == "VAGUE_SHORT_INPUT"
        state["awaiting_reply"] = True
        assert gate.validate_input("read", state_snapshot=state, typed=True).action == "ALLOW"

    def test_short_input_is_unclear_only_when_spoken(self, gate):
        # Spoken, a two-word verb-less fragment may be a misdecode. Typed, it is
        # what the user meant, and the model reads it as it would a transcript.
        assert (
            gate.validate_input("Delta Cuisine", state_snapshot=make_state()).reason_code
            == "SHORT_UNCLEAR"
        )
        d = gate.validate_input("Delta Cuisine", state_snapshot=make_state(), typed=True)
        assert d.action == "ALLOW"

    @pytest.mark.parametrize(
        "order",
        [
            "minimize spotify",
            "maximize chrome",
            "bluetooth off",
            "spotify",
            "netflix",
            "screenshot",
            "ouvre spotify",
            "lance netflix",
            "ferme chrome",
            "open",
        ],
    )
    def test_typed_order_is_never_intercepted_by_the_gate(self, gate, order):
        d = gate.validate_input(order, state_snapshot=make_state(), typed=True)
        assert d.action == "ALLOW"

    @pytest.mark.parametrize(
        "order",
        [
            "minimize spotify",
            "take a screenshot",
            "bluetooth off",
            "netflix the wild robot",
            "I want to watch The Wild Robot on netflix",
            "mets de la musique",
            "baisse le son",
            "tu peux ouvrir youtube",
            "switch off wifi",
            "ouvre youtube.com",
            "minimize spotify\nthen open netflix",
        ],
    )
    def test_typed_order_is_never_wrapped_as_data(self, gate, order):
        # Anything but COMMAND sends the model a "do NOT execute this" note, so
        # the order gets a question back instead of being run — the exact
        # typed-vs-voice gap this guards.
        assert gate.classify_typed_input(order) == "COMMAND"

    def test_only_a_long_verbless_paste_is_data(self, gate):
        blob = " ".join(["lorem"] * 60)
        assert gate.classify_typed_input(blob) == "TEXT_BLOB"

    def test_mute_silences_the_mic_not_the_keyboard(self, gate):
        state = make_state(is_muted=True)
        assert gate.validate_input("close brave", state_snapshot=state).action == "BLOCK_SILENTLY"
        assert (
            gate.validate_input("close brave", state_snapshot=state, typed=True).action == "ALLOW"
        )

    def test_typed_repetition_is_insistence_not_noise(self, gate):
        state = make_state()
        for _ in range(3):
            gate.record_blocked("open the wardrobe folder")
        # Spoken: three near-identical decodes in five seconds are one noise
        # source. Typed: the user is retyping because nothing happened.
        assert (
            gate.validate_input("open the wardrobe folder", state_snapshot=state).reason_code
            == "RECENTLY_BLOCKED"
        )
        assert (
            gate.validate_input("open the wardrobe folder", state_snapshot=state, typed=True).action
            == "ALLOW"
        )

    def test_typed_short_word_is_not_treated_as_microphone_noise(self, gate):
        state = make_state()
        # "ok" is two characters, so _is_noise_like blocks it SILENTLY on the
        # audio path. Typed from Telegram that means no action and no reply.
        assert gate.validate_input("ok", state_snapshot=state).action == "BLOCK_SILENTLY"
        assert gate.validate_input("ok", state_snapshot=state, typed=True).action == "ALLOW"

    def test_conversational_reply_is_never_classified_as_pasted_data(self, gate):
        # classify_typed_input runs a SECOND time downstream of the gate (in the
        # lane-router route). Re-branding an accepted reply as data there is what put
        # "Proceed" back into the "what shall I do with this?" wrapper.
        for reply in ("Proceed", "yes", "cancel"):
            assert gate.classify_typed_input(reply) == "COMMAND"

    def test_short_answer_is_a_command_while_a_question_is_open(self, gate):
        assert gate.classify_typed_input("the second one", {"awaiting_reply": True}) == "COMMAND"
        assert gate.classify_typed_input("the second one") == "COMMAND"


class TestRawPathDetection:
    def test_windows_path_allowed(self, gate):
        state = make_state()
        d = gate.validate_input("D:\\telechargements\\ceramequartz", state_snapshot=state)
        assert d.action == "ALLOW"
        assert d.reason_code == "RAW_PATH_DETECTED"

    def test_quoted_windows_path_allowed(self, gate):
        state = make_state()
        d = gate.validate_input('"D:\\telechargements\\ceramequartz"', state_snapshot=state)
        assert d.action == "ALLOW"
        assert d.reason_code == "RAW_PATH_DETECTED"

    def test_command_with_windows_path_allowed(self, gate):
        state = make_state()
        d = gate.validate_input("open D:\\telechargements\\ceramequartz", state_snapshot=state)
        assert d.action == "ALLOW"
        assert d.reason_code == "RAW_PATH_DETECTED"

    def test_path_with_spaces_allowed(self, gate):
        state = make_state()
        d = gate.validate_input("D:\\Ctrl Z\\Freelence\\DC\\IG", state_snapshot=state)
        assert d.action == "ALLOW"
        assert d.reason_code == "RAW_PATH_DETECTED"

    def test_unc_path_allowed(self, gate):
        state = make_state()
        d = gate.validate_input("\\\\server\\share\\folder", state_snapshot=state)
        assert d.action == "ALLOW"
        assert d.reason_code == "RAW_PATH_DETECTED"

    def test_path_detected_before_normalization(self, gate):
        state = make_state()
        d = gate.validate_input("D:\\my folder", state_snapshot=state)
        assert d.action == "ALLOW"
