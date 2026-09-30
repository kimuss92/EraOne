"""Voice-mode config: Fish Audio (primary) <-> Gemini (fallback).

The local Kokoro/Voicebox engine was removed on 2026-09-16; these tests cover
what replaced it. The invariant that matters most is that a switch NEVER needs a
reconnect — both modes run an AUDIO session, which is what lets the fallback and
a spoken switch take effect mid-conversation.
"""

import pytest

from core.voice_mode import (
    MODE_FISH_AUDIO,
    MODE_GEMINI_AUDIO,
    VALID_MODES,
    VoiceModeConfig,
)


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """A config bound to a throwaway file, so tests never touch config/voice.json."""
    monkeypatch.setattr(VoiceModeConfig, "_write", lambda self: None, raising=False)
    c = VoiceModeConfig()
    c.mode = MODE_FISH_AUDIO
    return c


def test_only_two_modes_remain():
    assert set(VALID_MODES) == {MODE_FISH_AUDIO, MODE_GEMINI_AUDIO}


def test_fish_is_the_primary_default():
    assert VoiceModeConfig.__init__.__defaults__ in (None, ())
    # The in-code default before any config load is the primary voice.
    c = VoiceModeConfig.__new__(VoiceModeConfig)
    c.mode = MODE_FISH_AUDIO
    assert c.is_fish()


def test_predicates_are_mutually_exclusive(cfg):
    cfg.mode = MODE_FISH_AUDIO
    assert cfg.is_fish() and not cfg.is_gemini()
    assert cfg.uses_speech_out() and cfg.synthesizes_from_transcript()

    cfg.mode = MODE_GEMINI_AUDIO
    assert cfg.is_gemini() and not cfg.is_fish()
    # Gemini plays its OWN audio, so SpeechOut is not involved.
    assert not cfg.uses_speech_out()
    assert not cfg.synthesizes_from_transcript()


def test_every_mode_is_an_audio_session(cfg):
    for mode in VALID_MODES:
        cfg.mode = mode
        assert cfg.response_modalities == ["AUDIO"]


@pytest.mark.parametrize("a", VALID_MODES)
@pytest.mark.parametrize("b", VALID_MODES)
def test_switching_never_needs_a_reconnect(cfg, a, b):
    """The property the whole primary/fallback design rests on."""
    cfg.mode = a
    assert cfg.needs_reconnect_to(b) is False


@pytest.mark.parametrize(
    "spoken", ["fish", "Fish Audio", "fish voice", "primary", "clone", "cloned", MODE_FISH_AUDIO]
)
def test_fish_aliases(spoken):
    assert VoiceModeConfig._resolve_alias(spoken) == MODE_FISH_AUDIO


@pytest.mark.parametrize(
    "spoken", ["normal", "gemini", "cloud", "default", "fallback", MODE_GEMINI_AUDIO]
)
def test_gemini_aliases(spoken):
    assert VoiceModeConfig._resolve_alias(spoken) == MODE_GEMINI_AUDIO


def test_unknown_alias_is_rejected(cfg):
    assert VoiceModeConfig._resolve_alias("kokoro") is None  # removed engine
    assert VoiceModeConfig._resolve_alias("banana") is None
    with pytest.raises(ValueError):
        cfg.set_mode("banana")


def test_set_mode_applies_and_returns(cfg):
    assert cfg.set_mode("gemini") == MODE_GEMINI_AUDIO
    assert cfg.is_gemini()
    assert cfg.set_mode("fish") == MODE_FISH_AUDIO
    assert cfg.is_fish()


def test_quota_flag_only_set_when_leaving_the_primary(cfg):
    cfg.set_mode("gemini", by_quota=True)
    assert cfg.switched_by_quota is True
    # Coming back to Fish is always deliberate, so the flag clears.
    cfg.set_mode("fish", by_quota=True)
    assert cfg.switched_by_quota is False


def test_corrupt_config_keeps_a_working_voice(cfg, tmp_path, monkeypatch):
    """A broken voice.json must cost the preference, never the voice."""
    bad = tmp_path / "voice.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("core.voice_mode.VOICE_CONFIG_PATH", bad, raising=False)
    cfg.mode = MODE_FISH_AUDIO
    cfg.reload()
    assert cfg.mode in VALID_MODES


# ── Preference vs transient active mode (the "reverts to Fish" fix) ───────────


@pytest.fixture
def filed(tmp_path, monkeypatch):
    """A config bound to a REAL throwaway file, so persistence is observable."""
    import core.voice_mode as vm

    p = tmp_path / "voice.json"
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", p, raising=False)
    return vm, p


def _saved_mode(path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))["mode"]


def test_deliberate_switch_persists_and_survives_relaunch(filed):
    vm, p = filed
    c = vm.VoiceModeConfig()
    c.set_mode("gemini")  # deliberate choice
    assert c.is_gemini() and c.preferred == MODE_GEMINI_AUDIO
    assert _saved_mode(p) == MODE_GEMINI_AUDIO
    # A fresh instance is what a relaunch does.
    again = vm.VoiceModeConfig()
    assert again.is_gemini() and again.preferred == MODE_GEMINI_AUDIO


def test_transient_fallback_touches_neither_preference_nor_file(filed):
    vm, p = filed
    c = vm.VoiceModeConfig()  # seeds fish
    c.set_mode(MODE_GEMINI_AUDIO, persist=False)  # auto-fallback
    assert c.is_gemini()  # active voice moved
    assert c.preferred == MODE_FISH_AUDIO  # preference untouched
    assert _saved_mode(p) == MODE_FISH_AUDIO  # file untouched


def test_transient_fallback_does_not_survive_relaunch(filed):
    """The core guarantee: an auto-fallback never rewrites the saved choice."""
    vm, _ = filed
    c = vm.VoiceModeConfig()
    c.set_mode(MODE_GEMINI_AUDIO, persist=False)
    assert c.is_gemini()
    # Relaunch returns to the user's real preference, not the fallback.
    assert vm.VoiceModeConfig().is_fish()


def test_deliberate_gemini_survives_a_later_fish_recovery_pattern(filed):
    """User chooses Gemini during an outage; a restore-to-preference is a no-op."""
    vm, p = filed
    c = vm.VoiceModeConfig()
    c.set_mode(MODE_GEMINI_AUDIO, persist=False)  # fell back to Gemini
    c.set_mode("gemini")  # user says "stay on Gemini" -> deliberate
    assert c.preferred == MODE_GEMINI_AUDIO
    assert _saved_mode(p) == MODE_GEMINI_AUDIO
    # Preference now equals active, so there is nothing for a recovery to undo.
    assert vm.VoiceModeConfig().is_gemini()
