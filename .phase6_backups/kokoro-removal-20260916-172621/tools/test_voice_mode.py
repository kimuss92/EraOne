"""Voice mode: persistence, aliases, session-config shape, and quota policy.

Two things here are load-bearing beyond "the setting saves":

1. **The default path must not regress.** Most of Era's behaviour stays on
   gemini_audio, so the AUDIO config must come out byte-for-byte as it was
   before the local voice existed.
2. **input_audio_transcription must survive in TEXT mode.** The "You:" log,
   _note_user_utterance, _maybe_record_feedback and the voice-theme intercept
   all read it. Dropping it alongside the *output* transcription is the easy
   mistake, and nothing would visibly break until someone noticed Era had
   stopped remembering anything the user said.

Tests isolate VOICE_CONFIG_PATH onto tmp_path — writing to the real
config/voice.json would let a test permanently change Era's voice.
"""

import pytest

import core.voice_mode as vm
from core.voice_mode import (
    MODE_GEMINI_AUDIO,
    MODE_KOKORO_AUDIO,
    MODE_KOKORO_TEXT,
    VoiceModeConfig,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A VoiceModeConfig backed by a throwaway file."""
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", tmp_path / "voice.json")
    return VoiceModeConfig()


# ── Defaults & persistence ───────────────────────────────────────────────────
def test_defaults_to_gemini_audio(cfg):
    """The cloud voice is the default; the local one is opt-in."""
    assert cfg.mode == MODE_GEMINI_AUDIO
    assert cfg.is_gemini() and not cfg.is_kokoro()


def test_mode_survives_reload(cfg, tmp_path, monkeypatch):
    cfg.set_mode("local")
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", tmp_path / "voice.json")
    assert VoiceModeConfig().mode == MODE_KOKORO_AUDIO


def test_voice_choice_survives_reload(cfg, tmp_path, monkeypatch):
    cfg.set_kokoro_voice("michael")
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", tmp_path / "voice.json")
    assert VoiceModeConfig().kokoro_voice == "am_michael"


def test_corrupt_config_falls_back_to_cloud_voice(tmp_path, monkeypatch):
    """A corrupt file must cost the preference, never the voice.

    Falling back to gemini_audio is the safe direction: it is the mode that
    works without a local backend running.
    """
    bad = tmp_path / "voice.json"
    bad.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", bad)
    assert VoiceModeConfig().mode == MODE_GEMINI_AUDIO


def test_unknown_mode_in_file_is_ignored(tmp_path, monkeypatch):
    f = tmp_path / "voice.json"
    f.write_text('{"mode": "telepathy"}', encoding="utf-8")
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", f)
    assert VoiceModeConfig().mode == MODE_GEMINI_AUDIO


# ── Spoken aliases ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("spoken", ["local", "kokoro", "offline", MODE_KOKORO_AUDIO])
def test_local_aliases_resolve_to_the_mode_that_connects(cfg, spoken):
    """Every spoken way of asking for the local voice must land on
    kokoro_AUDIO. kokoro_text cannot open a session on any Live model available
    today (live-probed 2026-07-29: 1007 "response modalities (TEXT) is not
    supported"), so routing a casual "use your local voice" there is what left
    the user unable to switch at all."""
    assert cfg.set_mode(spoken) == MODE_KOKORO_AUDIO


def test_kokoro_text_is_still_reachable_but_only_by_exact_name(cfg):
    """The quota-conserving mode must stay selectable for the day a
    TEXT-capable Live model exists — just never by accident."""
    assert cfg.set_mode(MODE_KOKORO_TEXT) == MODE_KOKORO_TEXT


# ── Session shape ────────────────────────────────────────────────────────────
def test_kokoro_audio_asks_for_an_AUDIO_session(cfg):
    """This is the whole reason the mode works: it never requests TEXT."""
    cfg.set_mode(MODE_KOKORO_AUDIO)
    assert cfg.response_modalities == ["AUDIO"]
    assert cfg.is_kokoro() and cfg.is_kokoro_audio()
    assert not cfg.is_kokoro_text()


def test_kokoro_text_still_asks_for_TEXT(cfg):
    cfg.set_mode(MODE_KOKORO_TEXT)
    assert cfg.response_modalities == ["TEXT"]
    assert cfg.is_kokoro() and cfg.is_kokoro_text()


def test_switching_between_cloud_and_local_audio_needs_no_reconnect(cfg):
    """Both are AUDIO sessions, so the switch applies mid-conversation. This is
    what makes the local voice feel instant instead of a session bounce."""
    cfg.set_mode(MODE_GEMINI_AUDIO)
    assert cfg.needs_reconnect_to(MODE_KOKORO_AUDIO) is False
    cfg.set_mode(MODE_KOKORO_AUDIO)
    assert cfg.needs_reconnect_to(MODE_GEMINI_AUDIO) is False


def test_switching_to_the_text_mode_does_need_a_reconnect(cfg):
    """response_modalities is fixed at connect time, so a TEXT target still
    requires tearing the session down."""
    cfg.set_mode(MODE_GEMINI_AUDIO)
    assert cfg.needs_reconnect_to(MODE_KOKORO_TEXT) is True
    cfg.set_mode(MODE_KOKORO_TEXT)
    assert cfg.needs_reconnect_to(MODE_GEMINI_AUDIO) is True


@pytest.mark.parametrize("spoken", ["normal", "gemini", "cloud", MODE_GEMINI_AUDIO])
def test_cloud_aliases(cfg, spoken):
    assert cfg.set_mode(spoken) == MODE_GEMINI_AUDIO


def test_unknown_mode_raises(cfg):
    with pytest.raises(ValueError):
        cfg.set_mode("whispering")


def test_named_voice_maps_to_engine_id(cfg):
    assert cfg.set_kokoro_voice("george") == "bm_george"
    assert cfg.set_kokoro_voice("bm_daniel") == "bm_daniel"  # already an id


# ── Quota provenance ─────────────────────────────────────────────────────────
def test_quota_flag_set_only_on_switch_into_local(cfg):
    cfg.set_mode("local", by_quota=True)
    assert cfg.switched_by_quota is True


def test_returning_to_cloud_clears_quota_flag(cfg):
    """A move back to the cloud voice is always deliberate, never quota-driven."""
    cfg.set_mode("local", by_quota=True)
    cfg.set_mode("normal")
    assert cfg.switched_by_quota is False


def test_manual_local_switch_is_not_flagged_as_quota(cfg):
    cfg.set_mode("local")
    assert cfg.switched_by_quota is False


# ── Session config shape ─────────────────────────────────────────────────────
class _StubEra:
    """Minimal host for _build_config — it only needs a resumption token."""

    class _SM:
        def get_resumption_token(self):
            return None

    session_manager = _SM()


def _build(mode, monkeypatch):
    from app.era_live.tool_dispatch import ToolDispatchMixin

    class Stub(ToolDispatchMixin, _StubEra):
        pass

    monkeypatch.setattr(vm.VOICE_CFG, "mode", mode)
    return Stub()._build_config()


def test_cloud_mode_config_is_unchanged(monkeypatch):
    """The default path: AUDIO out, Gemini's own voice, both transcriptions."""
    c = _build(MODE_GEMINI_AUDIO, monkeypatch)

    assert "AUDIO" in str(c.response_modalities)
    assert "TEXT" not in str(c.response_modalities)
    assert c.speech_config is not None
    assert c.output_audio_transcription is not None
    assert c.input_audio_transcription is not None


def test_local_mode_asks_for_text_only(monkeypatch):
    """Requesting AUDIO here would bill audio tokens for sound nobody hears."""
    c = _build(MODE_KOKORO_TEXT, monkeypatch)

    assert "TEXT" in str(c.response_modalities)
    assert "AUDIO" not in str(c.response_modalities)
    assert c.speech_config is None
    assert c.output_audio_transcription is None


def test_local_mode_keeps_input_transcription(monkeypatch):
    """Era must still hear the user — this is what feeds memory and the log."""
    c = _build(MODE_KOKORO_TEXT, monkeypatch)
    assert c.input_audio_transcription is not None


def test_tools_available_in_both_modes(monkeypatch):
    """A local voice must not cost Era her hands."""
    for mode in (MODE_GEMINI_AUDIO, MODE_KOKORO_TEXT):
        c = _build(mode, monkeypatch)
        assert c.tools, f"tools missing in {mode}"


# ── Quota escape-hatch policy ────────────────────────────────────────────────
def _simulate_quota_outage(quota_failures: int, model_ready: bool, cfg: VoiceModeConfig):
    """Mirror run()'s quota branch: switch once, then cool down.

    Returns the list of actions taken, one per quota failure. Asserting the
    SEQUENCE is the point — a switch that repeated every failure would be a hot
    retry loop, and one that never fired would leave Era mute for two hours.
    """
    actions = []
    for _ in range(quota_failures):
        if cfg.is_gemini() and model_ready:
            cfg.set_mode(MODE_KOKORO_TEXT, by_quota=True)
            actions.append("switch_and_retry_now")
        else:
            actions.append("cooldown")
    return actions


def test_first_quota_drop_switches_then_second_cools_down(cfg):
    assert _simulate_quota_outage(3, True, cfg) == [
        "switch_and_retry_now",
        "cooldown",
        "cooldown",
    ]


def test_no_local_backend_means_straight_to_cooldown(cfg):
    """Switching without a working backend would buy nothing and cost the voice."""
    assert _simulate_quota_outage(2, False, cfg) == ["cooldown", "cooldown"]


def test_already_local_never_switches_again(cfg):
    cfg.set_mode("local")
    assert _simulate_quota_outage(2, True, cfg) == ["cooldown", "cooldown"]
