"""Kokoro (GPU) as Era's local voice: the engine's text handling, and its place
in the local chain Fish -> Kokoro (English) -> text only.

No test loads the real model: the engine is given a fake dictionary/token table,
and the chain patches `_kokoro_pcm`. Loading takes ~10 s and a GPU.
"""

from __future__ import annotations

import json

import pytest

from core import local_tts, tts_kokoro
from core.tts_kokoro import KokoroVoice, _espeak_to_kokoro

PCM = b"\x01\x00" * 2400


# ── engine: config, phonemes, tokens ─────────────────────────────────────────
def test_defaults_are_george_on_the_gpu(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_kokoro, "_VOICE_JSON", tmp_path / "missing.json")
    cfg = tts_kokoro.get_config()
    assert cfg["voice"] == "bm_george" and cfg["provider"] == "cuda" and cfg["speed"] == 1.0


def test_voice_json_overrides_and_speed_is_clamped(monkeypatch, tmp_path):
    vj = tmp_path / "voice.json"
    vj.write_text(json.dumps({"mode": "gemini_audio", "kokoro": {"voice": "af_heart", "speed": 9}}))
    monkeypatch.setattr(tts_kokoro, "_VOICE_JSON", vj)
    cfg = tts_kokoro.get_config()
    assert cfg["voice"] == "af_heart"
    assert cfg["speed"] == 2.0


def test_espeak_output_is_mapped_into_kokoro_symbols():
    # misaki's British table: diphthongs collapse to Kokoro's single letters.
    assert _espeak_to_kokoro("d^ʒˈɔːd^ʒ") == "ʤˈɔːʤ"
    assert _espeak_to_kokoro("hə^ʊm") == "hQm"
    assert _espeak_to_kokoro("ta^ɪm") == "tIm"
    assert _espeak_to_kokoro("ɹe^ɪn") == "ɹAn"


def _fake_engine(espeak=None) -> KokoroVoice:
    k = KokoroVoice()
    k._lexicon = {"good": ["ɡ", "ˈ", "ʊ", "d"], "sir": ["s", "ˌ", "ɜ", "ː"]}
    k._tokens = {s: i for i, s in enumerate(" ,.!?ɡˈʊdsˌɜːxyz", start=1)}
    k._espeak = espeak
    return k


class _Espeak:
    def __init__(self):
        self.asked = []

    def phonemize(self, words, strip=True):
        self.asked.append(words[0])
        return ["zy z"]


def test_dictionary_first_espeak_for_unknown_words_and_numbers():
    es = _Espeak()
    k = _fake_engine(es)
    syms = k.phonemes("Good, sir! Zorblax 10:30.")
    assert syms[:5] == ["ɡ", "ˈ", "ʊ", "d", ","]
    assert es.asked == ["Zorblax", "10:30"], "only unknown words and numbers reach espeak"
    assert "".join(syms).endswith("zy z.")


def test_without_espeak_unknown_words_are_skipped_not_fatal():
    k = _fake_engine(espeak=None)
    assert "".join(k.phonemes("good zorblax sir")) == "ɡˈʊd sˌɜː"


def test_token_ids_fit_kokoros_context():
    k = _fake_engine()
    assert len(k.token_ids("good " * 400)) == tts_kokoro._MAX_TOKENS - 2


def test_a_broken_engine_returns_nothing_and_says_why(monkeypatch):
    k = KokoroVoice()
    monkeypatch.setattr(k, "_load", lambda: (_ for _ in ()).throw(RuntimeError("no gpu")))
    assert k.synthesize("Hello there.") == b""
    assert k.last_error == "Kokoro failed: RuntimeError"


def test_background_warm_is_inert_under_pytest(monkeypatch):
    k = KokoroVoice()
    started = []
    monkeypatch.setattr(tts_kokoro.threading, "Thread", lambda *a, **kw: started.append(1))
    k.warm_in_background()
    assert started == [] and not k.ready


# ── the local chain: Fish -> Kokoro (English) -> text only (no Piper since 2026-10-10)
@pytest.fixture
def chain(monkeypatch):
    seen = {"events": [], "kokoro": []}
    monkeypatch.setattr(local_tts, "_outage", {"reason": "", "noticed": True})
    monkeypatch.setattr(local_tts, "_event", lambda name, **f: seen["events"].append((name, f)))
    monkeypatch.setattr("core.tts_fish.is_configured", lambda: False)  # Fish off: local voice
    return seen


def _kokoro(seen, monkeypatch, pcm=PCM, why=""):
    async def fake(text):
        seen["kokoro"].append(text)
        return pcm, why

    monkeypatch.setattr(local_tts, "_kokoro_pcm", fake)


async def test_english_goes_to_kokoro(chain, monkeypatch):
    _kokoro(chain, monkeypatch)
    assert await local_tts.synthesize_best("Good evening, sir.", "en") == (PCM, "kokoro")
    assert chain["events"] == []


async def test_french_is_text_only_never_george(chain, monkeypatch):
    _kokoro(chain, monkeypatch)
    assert await local_tts.synthesize_best("Bonjour monsieur, c'est prêt.", "fr") == (b"", "none")
    assert chain["kokoro"] == [], "George must never read French"
    assert "not English (fr)" in local_tts.voice_unavailable_notice()


async def test_kokoro_failing_means_text_only_with_the_reason(chain, monkeypatch):
    _kokoro(chain, monkeypatch, pcm=b"", why="Kokoro failed: RuntimeError")
    assert await local_tts.synthesize_best("Good evening, sir.", "en") == (b"", "none")
    assert chain["events"] == [
        (
            "voice.unavailable",
            {
                "from_backend": "kokoro",
                "to_backend": "text",
                "reason": "kokoro: Kokoro failed: RuntimeError",
            },
        )
    ]


async def test_kokoro_still_loading_is_said_as_such(chain, monkeypatch):
    _kokoro(chain, monkeypatch, pcm=b"", why="")
    assert await local_tts.synthesize_best("Good evening, sir.", "en") == (b"", "none")
    assert "kokoro: still loading" in local_tts.voice_unavailable_notice()


async def test_a_cold_kokoro_starts_warming_and_never_blocks(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class Cold:
        ready = False
        warmed = 0

        def warm_in_background(self):
            Cold.warmed += 1

        def synthesize(self, text):  # pragma: no cover - must not be reached
            raise AssertionError("a cold Kokoro must not synthesize inline")

    monkeypatch.setattr(tts_kokoro, "KOKORO", Cold())
    assert await local_tts._kokoro_pcm("Hello.") == (b"", "")
    assert Cold.warmed == 1


async def test_a_warm_kokoro_speaks(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class Warm:
        ready = True
        last_error = ""

        def synthesize(self, text):
            return PCM

    monkeypatch.setattr(tts_kokoro, "KOKORO", Warm())
    assert await local_tts._kokoro_pcm("Hello.") == (PCM, "")


# ── kokoro_audio voice mode: Gemini's brain, George's voice ──────────────────
from core import tts_router  # noqa: E402
from core.voice_mode import (  # noqa: E402
    MODE_FISH_AUDIO,
    MODE_GEMINI_AUDIO,
    MODE_KOKORO_AUDIO,
    SPEECH_OUT_MODES,
    TRANSCRIPT_SYNTH_MODES,
    VOICE_CFG,
    VoiceModeConfig,
)


class _FakeKokoro:
    def __init__(self, ready=True, ok=True):
        self.ready = ready
        self.ok = ok
        self.last_error = ""
        self.warm_calls = 0

    def available(self):
        return (True, "") if self.ok else (False, "Kokoro files missing")

    def warm_in_background(self, on_done=None):
        self.warm_calls += 1
        if on_done:
            self.ready = True
            on_done(True)

    async def stream(self, text, lang=""):
        yield PCM


@pytest.fixture
def kokoro_mode(monkeypatch):
    monkeypatch.setattr(VoiceModeConfig, "_write", lambda self: None, raising=False)
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_KOKORO_AUDIO, raising=False)
    fake = _FakeKokoro()
    monkeypatch.setattr(tts_kokoro, "KOKORO", fake)
    return fake


def test_kokoro_is_a_transcript_voice_like_fish():
    assert MODE_KOKORO_AUDIO in SPEECH_OUT_MODES and MODE_KOKORO_AUDIO in TRANSCRIPT_SYNTH_MODES
    for word in ("kokoro", "local", "offline voice", "george"):
        assert VoiceModeConfig._resolve_alias(word) == MODE_KOKORO_AUDIO


async def test_router_speaks_with_george_in_kokoro_mode(kokoro_mode):
    assert [c async for c in tts_router.stream("Good evening, sir.")] == [PCM]


async def test_router_never_waits_for_a_cold_kokoro(kokoro_mode):
    kokoro_mode.ready = False
    assert [c async for c in tts_router.stream("Good evening, sir.")] == []
    assert kokoro_mode.warm_calls == 1, "it starts loading instead"
    assert kokoro_mode.last_error == "Kokoro is still loading"


def test_restore_checks_the_voice_it_returns_to_not_fish(kokoro_mode, monkeypatch):
    from app.era_live import audio

    monkeypatch.setattr("core.tts_fish.is_configured", lambda: False)  # Fish is irrelevant
    assert audio._backend_ready(MODE_KOKORO_AUDIO) is True
    kokoro_mode.ready = False
    assert audio._backend_ready(MODE_KOKORO_AUDIO) is False
    assert audio._backend_ready(MODE_FISH_AUDIO) is False


class _Engine:
    """Just enough of EraLive for the set_voice tool and the switch."""

    def __init__(self):
        from app.era_live.tool_dispatch import ToolDispatchMixin

        self._handle_set_voice = ToolDispatchMixin._handle_set_voice.__get__(self)
        self.logs, self.switched = [], []
        self._tts_primary_retry_at = 0.0
        self._tts_primary_mode = ""
        self.ui = self

    def write_log(self, m):
        self.logs.append(m)

    def _flush_audio_in_queue(self):
        pass

    def _kokoro_ready_for_switch(self, target):
        self.switched.append(target)
        return False


def test_set_voice_to_a_cold_kokoro_loads_first_and_keeps_the_current_voice(
    kokoro_mode, monkeypatch
):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_GEMINI_AUDIO, raising=False)
    kokoro_mode.ready = False
    eng = _Engine()
    reply = eng._handle_set_voice({"voice": "kokoro"})
    assert "loading" in reply and "automatically" in reply
    assert eng.switched == ["kokoro"], "the switch is handed to the loader"
    assert VOICE_CFG.mode == MODE_GEMINI_AUDIO, "never switch onto a voice that cannot speak yet"


def test_set_voice_to_a_warm_kokoro_switches_at_once(kokoro_mode, monkeypatch):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_GEMINI_AUDIO, raising=False)
    eng = _Engine()
    reply = eng._handle_set_voice({"voice": "local"})
    assert VOICE_CFG.mode == MODE_KOKORO_AUDIO
    assert "Kokoro" in reply and any("Kokoro" in m for m in eng.logs)


def test_set_voice_reports_a_missing_install(kokoro_mode, monkeypatch):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_GEMINI_AUDIO, raising=False)
    kokoro_mode.ready, kokoro_mode.ok = False, False
    assert "not installed" in _Engine()._handle_set_voice({"voice": "kokoro"})
    assert VOICE_CFG.mode == MODE_GEMINI_AUDIO


def test_the_typed_switch_reruns_once_george_is_loaded(kokoro_mode):
    import main

    kokoro_mode.ready = False
    calls = []

    class Self:
        ui = _Engine()

        def _apply_voice_switch(self, target):
            calls.append(target)

    assert main.EraLive._kokoro_ready_for_switch(Self(), "kokoro") is False
    assert calls == ["kokoro"], "once loaded, the same switch runs again by itself"
    assert any("Loading the Kokoro voice" in m for m in Self.ui.logs)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("switch to kokoro", "kokoro"),
        ("use your local voice", "kokoro"),
        ("switch to george", "kokoro"),
        ("switch to local", None),  # bare "local" may mean the local brain
        ("use the local model", None),
        ("tell me about kokoro", None),
        ("switch to fish", "fish"),
        ("use gemini", "gemini"),
    ],
)
def test_typed_voice_switch_phrases(text, expected):
    from app.text_intents import _voice_switch_intent

    assert _voice_switch_intent(text) == expected


# ── 2026-10-10 live test: streamed transcript lost its word breaks ───────────
def test_a_streamed_chunk_keeps_its_edge_spaces():
    from app.text_intents import _clean_stream_chunk, _clean_transcript

    assert _clean_stream_chunk(" the next<ctrl46>") == " the next"
    assert _clean_transcript(" the next ") == "the next"  # unchanged for its other callers


async def test_gemini_chunks_reach_the_voice_as_separate_words_and_sentences(monkeypatch):
    """Replays the live log line 'Kokorois active.What isthe nextobjective?'."""
    import asyncio

    import app.era_live.speech_out as so
    from app.text_intents import _clean_stream_chunk

    heard: list[str] = []

    async def fake_stream(text, lang=""):
        heard.append(text)
        yield PCM

    monkeypatch.setattr(so.tts_router, "stream", fake_stream)

    class Era:
        audio_in_queue = asyncio.Queue()
        _turn_done_event = asyncio.Event()

    s = so.SpeechOut(Era())
    for chunk in ["Kokoro", " is active.", " What is", " the next", " objective?"]:
        s.feed(_clean_stream_chunk(chunk))
    s.end_turn()
    for _ in range(200):
        if len(heard) >= 2:
            break
        await asyncio.sleep(0.01)
    await s.stop()
    assert heard == ["Kokoro is active.", "What is the next objective?"]


# ── loudness: raw Kokoro was ~5 dB quieter than Fish/Gemini (2026-10-10) ─────
def _speech_db(x):
    import numpy as np

    s = x[np.abs(x) > 0.01]
    return 20 * np.log10(np.sqrt(np.mean(s**2)))


def _kokoro_like_sentence():
    """Speech at Kokoro's measured raw level: RMS ~-20.5 dBFS, peaks ~-7 dBFS."""
    import numpy as np

    t = np.linspace(0, 1.5, 36000, endpoint=False)
    x = 0.094 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
    x[:2400] = 0.0  # a leading pause must not drag the level down
    return x.astype(np.float32)


def test_a_quiet_sentence_is_brought_up_to_the_target():
    from core.tts_kokoro import normalize_loudness

    y = normalize_loudness(_kokoro_like_sentence(), -15.0)
    assert abs(_speech_db(y) - -15.0) < 1.0


def test_the_limiter_never_lets_a_peak_clip():
    import numpy as np

    from core.tts_kokoro import _PEAK_CEILING, normalize_loudness

    y = normalize_loudness(_kokoro_like_sentence(), -9.0)  # the loudest allowed target
    assert np.abs(y).max() <= _PEAK_CEILING


def test_silence_stays_silent_and_the_boost_is_capped():
    import numpy as np

    from core.tts_kokoro import _MAX_BOOST_DB, normalize_loudness

    assert not normalize_loudness(np.zeros(2400, np.float32), -15.0).any()
    whisper = np.full(2400, 0.02, np.float32)  # -34 dBFS: needs +19 dB to reach -15
    assert abs(_speech_db(normalize_loudness(whisper, -15.0)) - (-34.0 + _MAX_BOOST_DB)) < 0.5


def test_loudness_comes_from_voice_json_and_is_clamped(monkeypatch, tmp_path):
    vj = tmp_path / "voice.json"
    monkeypatch.setattr(tts_kokoro, "_VOICE_JSON", vj)
    assert tts_kokoro.get_config()["loudness_db"] == -15.0  # file missing: default
    vj.write_text(json.dumps({"kokoro": {"loudness_db": -12}}))
    assert tts_kokoro.get_config()["loudness_db"] == -12.0
    vj.write_text(json.dumps({"kokoro": {"loudness_db": 0}}))
    assert tts_kokoro.get_config()["loudness_db"] == -9.0


# ── 2026-10-10 live: French mangled, accented words split, phrases cut ───────
@pytest.mark.parametrize(
    "text, lang",
    [
        ("Certainement, monsieur.", "fr"),
        ("Quelle est la tâche sur laquelle vous aimeriez travailler maintenant ?", "fr"),
        ("مرحبا سيدي", "ar"),
        ("Yes, sir.", "en"),
        ("Céramique Negro Marquina Matt.", "en"),  # one accented brand name stays English
        ("I hear you, sir — café ready.", "en"),
        ("Merci.", "en"),  # a lone loanword is not a French sentence
    ],
)
def test_language_of_sends_only_english_to_george(text, lang):
    assert tts_kokoro.language_of(text) == lang


def test_accented_words_stay_whole_and_symbols_are_spoken():
    from core.tts_kokoro import _WORD, _spoken_symbols

    assert _WORD.findall("la tâche, café") == ["la", "tâche", ",", "café"]
    assert _WORD.findall(_spoken_symbols("over 10 m².")) == ["over", "10", "square", "metres", "."]


def test_george_refuses_french_without_loading_anything(monkeypatch):
    k = KokoroVoice()
    monkeypatch.setattr(k, "_load", lambda: pytest.fail("must not load for French"))
    assert k.synthesize("Certainement, monsieur.") == b""
    assert k.last_error.startswith("not English (fr)")


async def test_router_stays_silent_once_gemini_has_taken_over(monkeypatch):
    """After a Kokoro fallback the queued sentences must not reach Fish: two voices."""
    from core.tts_fish import FISH

    async def fish_would_speak(text, lang=""):  # pragma: no cover - must not run
        yield PCM

    monkeypatch.setattr(tts_router, "FISH", FISH)
    monkeypatch.setattr(FISH, "stream", fish_would_speak)
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_GEMINI_AUDIO, raising=False)
    assert [c async for c in tts_router.stream("The rest of the reply.")] == []
