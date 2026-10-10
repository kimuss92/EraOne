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


def test_three_modes_kokoro_is_back():
    # Kokoro George on the GPU was re-added 2026-10-10 (Karim) as the fast local voice.
    assert set(VALID_MODES) == {MODE_FISH_AUDIO, MODE_GEMINI_AUDIO, "kokoro_audio"}


def test_gemini_is_the_first_run_default(monkeypatch):
    # Karim, 2026-10-09: Gemini first; the user switches by voice and it persists.
    # reload() is stubbed so this reads the in-code default, not config/voice.json.
    monkeypatch.setattr(VoiceModeConfig, "reload", lambda self: None)
    c = VoiceModeConfig()
    assert c.is_gemini() and c.preferred == MODE_GEMINI_AUDIO


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
    assert VoiceModeConfig._resolve_alias("kokoro") == "kokoro_audio"  # re-added 2026-10-10
    assert VoiceModeConfig._resolve_alias("local voice") == "kokoro_audio"
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
    c = vm.VoiceModeConfig()
    c.set_mode(MODE_FISH_AUDIO)  # the user's deliberate, saved choice
    c.set_mode(MODE_GEMINI_AUDIO, persist=False)  # auto-fallback
    assert c.is_gemini()  # active voice moved
    assert c.preferred == MODE_FISH_AUDIO  # preference untouched
    assert _saved_mode(p) == MODE_FISH_AUDIO  # file untouched


def test_transient_fallback_does_not_survive_relaunch(filed):
    """The core guarantee: an auto-fallback never rewrites the saved choice."""
    vm, _ = filed
    c = vm.VoiceModeConfig()
    c.set_mode(MODE_FISH_AUDIO)  # the user's deliberate, saved choice
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


def test_a_voice_switch_keeps_other_voice_settings(filed):
    # config/voice.json also holds the per-language Fish voice map (2026-10-09).
    import json

    vm, p = filed
    p.write_text(json.dumps({"mode": "gemini_audio", "fish_voices": {"fr": "abc"}}), "utf-8")
    c = vm.VoiceModeConfig()
    c.set_mode("fish")
    saved = json.loads(p.read_text("utf-8"))
    assert saved == {"mode": "fish_audio", "fish_voices": {"fr": "abc"}}


# ── Gemini's own voice is a setting, not code (2026-10-10) ───────────────────
def test_gemini_voice_defaults_to_algenib(monkeypatch, tmp_path):
    import core.voice_mode as vm

    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", tmp_path / "voice.json")
    assert vm.gemini_voice_name() == "Algenib"


@pytest.mark.parametrize(
    "raw, expected",
    [("charon", "Charon"), (" ORUS ", "Orus"), ("Finn", "Algenib"), ("", "Algenib")],
)
def test_gemini_voice_is_read_from_voice_json_and_validated(monkeypatch, tmp_path, raw, expected):
    import json

    import core.voice_mode as vm

    p = tmp_path / "voice.json"
    p.write_text(json.dumps({"mode": "gemini_audio", "gemini_voice": raw}))
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", p)
    assert vm.gemini_voice_name() == expected


def test_the_live_session_uses_the_configured_voice():
    import inspect

    from app.era_live import tool_dispatch

    src = inspect.getsource(tool_dispatch)
    assert "voice_name=gemini_voice_name()" in src and 'voice_name="Algenib"' not in src


def test_a_voice_gemini_refuses_falls_back_for_this_run(monkeypatch, tmp_path):
    """Live 2026-10-10: 3.1 answered 1007 "No matching speaker voice ... Arlo"."""
    import json

    import core.voice_mode as vm

    p = tmp_path / "voice.json"
    p.write_text(json.dumps({"gemini_voice": "Charon"}))
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", p)
    monkeypatch.setattr(vm, "_refused_voice", {"name": ""})
    assert vm.gemini_voice_name() == "Charon"
    assert vm.reject_gemini_voice() == "Charon"
    assert vm.gemini_voice_name() == "Algenib"
    assert vm.reject_gemini_voice() == "", "the default refused too: no retry loop"


def test_main_turns_a_voice_refusal_into_a_quick_reconnect(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    import core.voice_mode as vm
    import main

    p = tmp_path / "voice.json"
    p.write_text(json.dumps({"gemini_voice": "Charon"}))
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", p)
    monkeypatch.setattr(vm, "_refused_voice", {"name": ""})
    logs = []
    era = SimpleNamespace(_flatten_exc=lambda e: [e], ui=SimpleNamespace(write_log=logs.append))
    err = Exception("1007 None. No matching speaker voice found for name: Charon and language: ")
    assert main._voice_refused(era, err) is True
    assert vm.gemini_voice_name() == "Algenib" and "Charon" in logs[0]
    assert main._voice_refused(era, Exception("1007 modalities (TEXT) not supported")) is False


# ── 2026-10-10: Gemini voice from the HUD; standby lines in Era's own words ──
def test_set_gemini_voice_keeps_other_keys_and_rejects_odd_names(monkeypatch, tmp_path):
    import json

    import core.voice_mode as vm

    p = tmp_path / "voice.json"
    p.write_text(json.dumps({"mode": "kokoro_audio", "listening": {"echo_hold_ms": 500}}))
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", p)
    monkeypatch.setattr(vm, "_refused_voice", {"name": "Finn"})
    assert vm.set_gemini_voice(" charon ") == "Charon"
    data = json.loads(p.read_text())
    assert data == {
        "mode": "kokoro_audio",
        "listening": {"echo_hold_ms": 500},
        "gemini_voice": "Charon",
    }
    assert vm._refused_voice["name"] == "", "a new choice gets a fresh chance"
    for bad in ["", "x" * 41, "Arlo; rm -rf", "../voice", "<script>", "Finn", "en-us-finn"]:
        with pytest.raises(ValueError):
            vm.set_gemini_voice(bad)
    assert json.loads(p.read_text())["gemini_voice"] == "Charon"


def _engine(**kw):
    from types import SimpleNamespace

    import main

    sent, said = [], []

    class Loop:
        def call_soon_threadsafe(self, fn, *a):
            fn(*a)

        def is_running(self):
            return True

    eng = SimpleNamespace(
        session=kw.get("session", object()),
        _loop=Loop(),
        _is_speaking=kw.get("speaking", False),
        _tool_is_running=lambda: kw.get("tool", False),
        session_manager=SimpleNamespace(
            get_state_snapshot=lambda: {"current_state": kw.get("state", "listening")}
        ),
        _go_away_event=SimpleNamespace(set=lambda: sent.append("renew")),
        speak=said.append,
    )
    eng._era_busy = main.EraLive._era_busy.__get__(eng)
    eng._renew_for_voice = main.EraLive._renew_for_voice.__get__(eng)
    eng.apply = main.EraLive.apply_gemini_voice_now.__get__(eng)
    eng.brief = main.EraLive.speak_brief.__get__(eng)
    return eng, sent, said


def test_a_voice_change_applies_at_once_or_as_soon_as_era_is_idle(monkeypatch):
    import main

    started = []
    monkeypatch.setattr(
        main.threading,
        "Thread",
        lambda **kw: type("T", (), {"start": lambda self: started.append(kw["name"])})(),
    )
    eng, sent, _ = _engine()
    assert "Reconnecting" in eng.apply() and sent == ["renew"], "idle: at once"
    for busy in ({"speaking": True}, {"state": "processing"}, {"tool": True}):
        eng, sent, _ = _engine(**busy)
        assert "as soon as Era finishes" in eng.apply() and sent == [], "never mid-turn"
    assert started == ["voice-apply"] * 3, "a waiter applies it the moment she is idle"
    eng, sent, _ = _engine(session=None)
    assert "when Gemini connects" in eng.apply() and sent == []


def test_standby_line_is_composed_by_era_under_rules(monkeypatch):
    import main

    notes = []
    monkeypatch.setattr(main, "submit_coroutine", lambda coro, loop: notes.append(coro))

    class Session:
        def send_realtime_input(self, text):
            return text

    eng, _, said = _engine(session=Session())
    eng.brief("You are going into standby now.", fallback="Going quiet, sir.")
    note = notes[0]
    assert "in your own words" in note and "ONE brief sentence" in note
    assert "Never say the wake word" in note and main.WAKE_CFG.word in note
    assert "Do not ask a question" in note and said == []


def test_standby_line_without_a_session_is_the_short_fallback():
    eng, _, said = _engine(session=None)
    eng.brief("You are going into standby now.", fallback="Going quiet, sir.")
    assert len(said) == 1 and "Going quiet, sir." in said[0]


def test_hud_settings_show_the_30_live_voices_with_gender(monkeypatch, tmp_path):
    import json

    import core.voice_mode as vm
    from app.hud.api import _gemini_voice_view

    p = tmp_path / "voice.json"
    p.write_text(json.dumps({"gemini_voice": "charon"}))
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", p)
    monkeypatch.setattr(vm, "_refused_voice", {"name": ""})
    view = _gemini_voice_view()
    assert view["gemini_voice"] == "Charon" == view["gemini_voice_active"]
    assert view["gemini_voice_note"] == ""
    assert len(view["gemini_voices"]) == 30
    assert {"name": "Charon", "style": "Informative", "gender": "male"} in view["gemini_voices"]
    assert {v["gender"] for v in view["gemini_voices"]} == {"male", "female", "neutral"}


def test_a_library_voice_is_explained_not_silently_swapped(monkeypatch, tmp_path):
    """Live 2026-10-10: Live accepted "Finn" (male in AI Studio) and spoke with a
    default FEMALE voice (~220 Hz). It is now never sent; the HUD says why."""
    import json

    import core.voice_mode as vm

    p = tmp_path / "voice.json"
    p.write_text(json.dumps({"gemini_voice": "Finn"}))
    monkeypatch.setattr(vm, "VOICE_CONFIG_PATH", p)
    view = vm.gemini_voice_view()
    assert view["gemini_voice_active"] == "Algenib"
    assert "Finn" in view["gemini_voice_note"] and "cannot use it" in view["gemini_voice_note"]
