"""Tests for the Fish Audio primary / Gemini fallback TTS architecture."""

import asyncio

import pytest

from core import tts_fish, tts_router
from core.voice_mode import (
    MODE_FISH_AUDIO,
    MODE_GEMINI_AUDIO,
    VOICE_CFG,
    VoiceModeConfig,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Never read the real config, and never WRITE the user's voice.json.

    set_mode() persists, so without stubbing _write these tests would rewrite
    the user's actual voice preference as a side effect.
    """
    monkeypatch.setattr("core.utils.load_config", lambda: {}, raising=False)
    monkeypatch.setattr(VoiceModeConfig, "_write", lambda self: None, raising=False)
    for var in (
        "FISH_AUDIO_API_KEY",
        "FISH_AUDIO_MODEL",
        "FISH_AUDIO_REFERENCE_ID",
        "FISH_AUDIO_ENDPOINT",
        "FISH_AUDIO_LATENCY",
    ):
        monkeypatch.delenv(var, raising=False)
    original = VOICE_CFG.mode
    tts_fish.FISH.reset_cooldown()
    yield
    VOICE_CFG.mode = original
    tts_fish.FISH.reset_cooldown()


# ── Config / key resolution (never hardcoded) ─────────────────────────────────
def test_no_key_by_default():
    assert tts_fish.get_fish_config()["api_key"] == ""
    assert tts_fish.is_configured() is False


def test_key_from_env(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "env-secret")
    assert tts_fish.get_fish_config()["api_key"] == "env-secret"
    assert tts_fish.is_configured() is True


def test_config_file_beats_env(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "env-secret")
    monkeypatch.setattr(
        "core.utils.load_config",
        lambda: {"fish_audio": {"api_key": "file-secret", "model": "s1"}},
        raising=False,
    )
    cfg = tts_fish.get_fish_config()
    assert cfg["api_key"] == "file-secret"
    assert cfg["model"] == "s1"


def test_defaults_are_free_tier():
    cfg = tts_fish.get_fish_config()
    assert cfg["model"] == "s2.1-pro-free"
    assert cfg["endpoint"].startswith("https://")


# ── Failure handling ──────────────────────────────────────────────────────────
def test_synthesize_without_key_returns_empty_and_cools():
    pcm = asyncio.run(tts_fish.FISH.synthesize("hello"))
    assert pcm == b""
    assert tts_fish.FISH.in_cooldown() is True  # don't retry once per sentence


def test_cooldown_short_circuits_without_network(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "k")
    tts_fish.FISH._cool(60, "test")
    called = False

    async def _boom(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(tts_fish.FISH, "_get_session", _boom)
    assert asyncio.run(tts_fish.FISH.synthesize("hi")) == b""
    assert called is False  # never touched the network while cooling


def test_empty_text_is_not_a_failure():
    assert asyncio.run(tts_fish.FISH.synthesize("   ")) == b""
    assert tts_fish.FISH.in_cooldown() is False


# ── Router dispatches on the active voice mode ────────────────────────────────
def test_router_uses_fish_in_fish_mode(monkeypatch):
    VOICE_CFG.mode = MODE_FISH_AUDIO

    async def _fish(text):
        return b"FISHPCM"

    monkeypatch.setattr(tts_router.FISH, "synthesize", _fish)
    pcm, backend = asyncio.run(tts_router.synthesize("hello"))
    assert pcm == b"FISHPCM"
    assert backend == tts_router.BACKEND_FISH


def test_router_signals_fallback_when_fish_fails(monkeypatch):
    VOICE_CFG.mode = MODE_FISH_AUDIO

    async def _fail(text, lang=""):
        return b""

    monkeypatch.setattr(tts_router.FISH, "synthesize", _fail)
    pcm, backend = asyncio.run(tts_router.synthesize("hello"))
    assert pcm == b""
    assert backend == tts_router.BACKEND_FISH  # caller must fall back to Gemini


# ── Voice-mode wiring ─────────────────────────────────────────────────────────
def test_fish_mode_predicates():
    VOICE_CFG.mode = MODE_FISH_AUDIO
    assert VOICE_CFG.is_fish() is True
    assert VOICE_CFG.uses_speech_out() is True
    assert VOICE_CFG.synthesizes_from_transcript() is True
    assert VOICE_CFG.response_modalities == ["AUDIO"]


def test_fish_to_gemini_needs_no_reconnect():
    """The property that makes the fallback instant and mid-conversation."""
    VOICE_CFG.mode = MODE_FISH_AUDIO
    assert VOICE_CFG.needs_reconnect_to(MODE_GEMINI_AUDIO) is False


def test_spoken_aliases_resolve_to_fish():
    for alias in ("fish", "fish audio", "fish voice", "primary"):
        assert VoiceModeConfig._resolve_alias(alias) == MODE_FISH_AUDIO


# ── The fallback itself ───────────────────────────────────────────────────────
class _FakeUI:
    def __init__(self):
        self.logs = []

    def write_log(self, msg):
        self.logs.append(msg)


class _FakeQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def _fake_engine():
    from app.era_live.audio import AudioIOMixin

    class Engine(AudioIOMixin):
        def __init__(self):
            self.ui = _FakeUI()
            self.audio_in_queue = _FakeQueue()
            self._cloud_audio_buffer = []
            self._tts_primary_retry_at = 0.0
            self._tts_primary_mode = ""

    return Engine()


def test_fallback_flushes_buffered_gemini_audio_and_switches():
    VOICE_CFG.mode = MODE_FISH_AUDIO
    eng = _fake_engine()
    eng._buffer_cloud_audio(b"aaa")
    eng._buffer_cloud_audio(b"bbb")

    eng._tts_fallback("fish synthesis failed")

    # The reply still reaches the speakers: uninterrupted service.
    assert eng.audio_in_queue.items == [b"aaa", b"bbb"]
    # And Era is now on the Gemini fallback, remembering what to restore.
    assert VOICE_CFG.mode == MODE_GEMINI_AUDIO
    assert eng._tts_primary_mode == MODE_FISH_AUDIO
    assert eng._tts_primary_retry_at > 0
    assert eng._cloud_audio_buffer == []
    assert any("Gemini" in m for m in eng.ui.logs)


def test_fallback_is_idempotent():
    VOICE_CFG.mode = MODE_GEMINI_AUDIO
    eng = _fake_engine()
    eng._buffer_cloud_audio(b"zzz")
    eng._tts_fallback("second call")
    assert eng.audio_in_queue.items == []  # already on Gemini; nothing replayed


def test_cloud_buffer_is_bounded():
    from app.era_live.audio import CLOUD_AUDIO_BUFFER_MAX_CHUNKS

    eng = _fake_engine()
    for _ in range(CLOUD_AUDIO_BUFFER_MAX_CHUNKS + 50):
        eng._buffer_cloud_audio(b"x")
    assert len(eng._cloud_audio_buffer) == CLOUD_AUDIO_BUFFER_MAX_CHUNKS


def test_restore_primary_after_window(monkeypatch):
    # Pin the saved preference: otherwise it is whatever the user's live
    # config/voice.json says, and a deliberate Gemini choice there (correctly)
    # means there is nothing to restore.
    monkeypatch.setattr(VOICE_CFG, "preferred", MODE_FISH_AUDIO)
    VOICE_CFG.mode = MODE_GEMINI_AUDIO
    eng = _fake_engine()
    eng._tts_primary_mode = MODE_FISH_AUDIO
    eng._tts_primary_retry_at = 1.0  # long past
    monkeypatch.setattr("core.tts_fish.is_configured", lambda: True)
    monkeypatch.setattr(tts_fish.FISH, "in_cooldown", lambda: False)

    eng._maybe_restore_primary_tts()
    assert VOICE_CFG.mode == MODE_FISH_AUDIO


def test_restore_waits_while_fish_still_cooling(monkeypatch):
    VOICE_CFG.mode = MODE_GEMINI_AUDIO
    eng = _fake_engine()
    eng._tts_primary_mode = MODE_FISH_AUDIO
    eng._tts_primary_retry_at = 1.0
    monkeypatch.setattr("core.tts_fish.is_configured", lambda: True)
    monkeypatch.setattr(tts_fish.FISH, "in_cooldown", lambda: True)

    eng._maybe_restore_primary_tts()
    assert VOICE_CFG.mode == MODE_GEMINI_AUDIO  # stayed on the fallback


# ── Streaming path (the latency fix) ─────────────────────────────────────────
def test_stream_yields_nothing_without_key_and_cools():
    async def _collect():
        return [c async for c in tts_fish.FISH.stream("hello")]

    chunks = asyncio.run(_collect())
    assert chunks == []
    assert tts_fish.FISH.in_cooldown() is True  # dead key must not cost a hop per sentence


def test_stream_short_circuits_during_cooldown(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "k")
    tts_fish.FISH._cool(60, "test")
    called = False

    async def _boom(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(tts_fish.FISH, "_get_session", _boom)

    async def _collect():
        return [c async for c in tts_fish.FISH.stream("hi")]

    assert asyncio.run(_collect()) == []
    assert called is False


def test_synthesize_drains_the_stream(monkeypatch):
    """The buffered wrapper (used by the clone/check tools) concatenates stream()."""

    async def _fake_stream(text, lang=""):
        yield b"AB"
        yield b"CD"

    monkeypatch.setattr(tts_fish.FISH, "stream", _fake_stream)
    assert asyncio.run(tts_fish.FISH.synthesize("x")) == b"ABCD"


def test_router_stream_passes_through(monkeypatch):
    async def _fake_stream(text, lang=""):
        yield b"one"
        yield b"two"

    monkeypatch.setattr(tts_router.FISH, "stream", _fake_stream)

    async def _collect():
        return [c async for c in tts_router.stream("hello")]

    assert asyncio.run(_collect()) == [b"one", b"two"]


def test_default_latency_is_low():
    # Fastest time-to-first-audio; the default (non-cloned) voice tolerates
    # 'low' fine, and streaming still carries most of the latency win.
    assert tts_fish.get_fish_config()["latency"] == "low"


def test_fallback_plays_handed_over_turn_audio_before_the_live_buffer():
    VOICE_CFG.mode = MODE_FISH_AUDIO
    eng = _fake_engine()
    eng._buffer_cloud_audio(b"next-turn")
    eng._tts_fallback("fish synthesis failed", held_audio=[b"t1a", b"t1b"])
    assert eng.audio_in_queue.items == [b"t1a", b"t1b", b"next-turn"]
    assert eng._cloud_audio_buffer == []


# ── DNS / connect failure: one immediate retry before the cooldown ─────────────
class _FakeResp:
    status = 200

    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def content(self):
        data = self._data

        class _C:
            async def iter_chunked(self, n):
                yield data

        return _C()


class _FlakySession:
    closed = False

    def __init__(self, failures):
        self.failures = failures
        self.posts = 0

    def post(self, *a, **k):
        import aiohttp

        self.posts += 1
        if self.posts <= self.failures:
            raise aiohttp.ClientConnectorError(None, OSError("dns"))
        return _FakeResp(b"\x00\x01" * 10)


def _stream_with(monkeypatch, session):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "k")
    monkeypatch.setattr(tts_fish, "CONNECT_RETRY_PAUSE_S", 0)
    client = tts_fish.FishAudioClient()
    client._session = session
    return client, asyncio.run(client.synthesize("hello there"))


def test_one_dns_failure_is_retried_and_speech_still_arrives(monkeypatch):
    session = _FlakySession(failures=1)
    client, pcm = _stream_with(monkeypatch, session)
    assert pcm and session.posts == 2
    assert not client.in_cooldown()


def test_a_dns_failure_that_persists_still_cools_down(monkeypatch):
    session = _FlakySession(failures=5)
    client, pcm = _stream_with(monkeypatch, session)
    assert pcm == b"" and session.posts == 2
    assert client.in_cooldown()


# ── Pre-warm: a tiny synthesis while the user talks, only when Fish went cold ──
def test_prewarm_synthesizes_once_when_cold_and_skips_when_warm(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "k")
    client = tts_fish.FishAudioClient()
    said = []

    async def _fake_stream(text, lang=""):
        said.append(text)
        client._last_use = __import__("time").monotonic()
        yield b"\x00\x00"

    monkeypatch.setattr(client, "stream", _fake_stream)
    assert asyncio.run(client.prewarm()) is True
    assert said == [tts_fish.PREWARM_TEXT]
    assert asyncio.run(client.prewarm()) is False  # just used: still warm
    assert said == [tts_fish.PREWARM_TEXT]


def test_prewarm_does_nothing_without_a_key():
    client = tts_fish.FishAudioClient()
    assert asyncio.run(client.prewarm()) is False


# ── Per-language Fish voices: config/voice.json "fish_voices" ─────────────────
@pytest.mark.parametrize(
    "text, lang",
    [
        ("Good evening, sir.", "en"),
        ("Bonsoir monsieur, c'est prêt.", "fr"),
        ("مساء الخير سيدي.", "ar"),
        ("عسلامة سيدي، الرابور حاضر.", "ar"),
    ],
)
def test_detect_lang(text, lang):
    assert tts_fish.detect_lang(text) == lang


@pytest.fixture
def voice_map(tmp_path, monkeypatch):
    import json

    p = tmp_path / "voice.json"
    monkeypatch.setattr(tts_fish, "_VOICE_JSON", p)
    monkeypatch.setattr(tts_fish, "_voice_map_cache", (-1.0, {}))

    def write(mapping):
        p.write_text(json.dumps({"mode": "fish_audio", "fish_voices": mapping}), "utf-8")

    return write


def test_a_mapped_language_gets_its_own_voice(voice_map):
    voice_map({"fr": "FRVOICE", "ar": "ARVOICE"})
    assert tts_fish.fish_voice_for("fr") == "FRVOICE"
    assert tts_fish.fish_voice_for("ar") == "ARVOICE"
    assert tts_fish.fish_voice_for("en") == ""  # unmapped -> the default voice


def test_no_voice_file_means_the_default_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_fish, "_VOICE_JSON", tmp_path / "missing.json")
    monkeypatch.setattr(tts_fish, "_voice_map_cache", (-1.0, {}))
    assert tts_fish.fish_voice_for("fr") == ""


class _BodySession:
    """Captures the JSON body Fish would receive."""

    closed = False

    def __init__(self):
        self.bodies = []

    def post(self, url, json=None, **k):
        self.bodies.append(json)
        return _FakeResp(b"\x00\x01" * 4)


def test_the_request_uses_the_sentence_language_voice(voice_map, monkeypatch):
    voice_map({"fr": "FRVOICE"})
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "k")
    monkeypatch.setenv("FISH_AUDIO_REFERENCE_ID", "DEFAULTVOICE")
    client = tts_fish.FishAudioClient()
    client._session = sess = _BodySession()
    asyncio.run(client.synthesize("Bonsoir monsieur.", "fr"))
    asyncio.run(client.synthesize("Good evening, sir.", "en"))
    assert [b["reference_id"] for b in sess.bodies] == ["FRVOICE", "DEFAULTVOICE"]
