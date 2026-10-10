"""Chaos test for the voice cascade (master prompt Phase 2a, section 4.6).

Every backend is made to fail in each way the spec names, and each fall-through
is TIMED, not just observed:

    Fish fails   -> Gemini's held audio (Fish mode)  /  Kokoro (local loop)
    Kokoro fails -> text only, said once on the HUD, logged as an event
                    (Piper, the old CPU floor, was removed 2026-10-10)

Fish runs for real against a fake Fish server on loopback, so the real aiohttp
client, its timeouts and its cooldowns are what is measured. No network, no key,
no audio hardware: Kokoro and the Gemini engine are fakes.

Bound: a failure Fish REPORTS (401/402/429/5xx, empty 200, refused connect)
falls through in under 1 s. A Fish that says nothing (hang, mid-stream stall)
can only be detected by a deadline: stall_timeout_s (3 s; was 45 s of silence).
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest
from aiohttp import web

import app.era_live.speech_out as so
from core import local_tts, tts_fish, tts_router
from core.voice_mode import MODE_FISH_AUDIO, MODE_GEMINI_AUDIO, VOICE_CFG, VoiceModeConfig

FAST = 1.0  # seconds: the spec's fall-through bound for a reported failure
PCM = b"\x01\x00" * 2400  # 0.1 s of 24 kHz int16


# ── Fake Fish on loopback ─────────────────────────────────────────────────────
@pytest.fixture
async def fish_server():
    state = {"mode": "ok", "hits": 0}
    release = asyncio.Event()  # frees hanging handlers so teardown is instant

    async def tts(request: web.Request):
        state["hits"] += 1
        await request.read()
        mode = state["mode"]
        if mode == "ok":
            resp = web.StreamResponse()
            await resp.prepare(request)
            await resp.write(PCM)
            await resp.write_eof()
            return resp
        if mode == "empty":
            return web.Response(status=200, body=b"")
        if mode == "hang":  # accepts, never answers
            await release.wait()
            return web.Response(status=200, body=b"")
        if mode == "stall":  # first audio, then silence
            resp = web.StreamResponse()
            await resp.prepare(request)
            await resp.write(PCM)
            await release.wait()
            return resp
        return web.Response(status=int(mode), text="chaos")

    app = web.Application()
    app.router.add_post("/v1/tts", tts)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield state, f"http://127.0.0.1:{port}/v1/tts"
    release.set()
    await runner.cleanup()


@pytest.fixture
def fish(monkeypatch):
    """A fresh real Fish client (the singleton's session belongs to another
    loop), wired in everywhere the cascade looks it up."""
    client = tts_fish.FishAudioClient()
    monkeypatch.setattr(tts_fish, "FISH", client)
    monkeypatch.setattr(tts_router, "FISH", client)
    monkeypatch.setattr(so, "FISH", client)
    for var in ("FISH_AUDIO_API_KEY", "FISH_AUDIO_ENDPOINT", "FISH_AUDIO_STALL_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(VoiceModeConfig, "_write", lambda self: None, raising=False)
    monkeypatch.setattr(local_tts, "_outage", {"reason": "", "noticed": True})
    yield client


def _point_at(monkeypatch, url: str, **extra) -> None:
    cfg = {"fish_audio": {"api_key": "test-key", "endpoint": url, **extra}}
    monkeypatch.setattr("core.utils.load_config", lambda: cfg, raising=False)


async def _timed_stream(client, text="Testing the cascade."):
    t0 = time.perf_counter()
    chunks = [c async for c in client.stream(text)]
    return b"".join(chunks), time.perf_counter() - t0


# ── Fish itself: each failure class returns nothing, fast ────────────────────
async def test_a_healthy_fish_speaks(fish, fish_server, monkeypatch):
    state, url = fish_server
    _point_at(monkeypatch, url)
    audio, elapsed = await _timed_stream(fish)
    await fish.close()
    assert audio == PCM
    assert elapsed < FAST
    assert not fish.in_cooldown()


@pytest.mark.parametrize(
    "mode, cooldown_at_least, reason_has",
    [
        ("401", 800, "auth rejected"),
        ("403", 800, "auth rejected"),
        ("402", 800, "credit"),
        ("429", 20, "HTTP 429"),
        ("500", 20, "HTTP 500"),
        ("503", 20, "HTTP 503"),
        ("empty", 0, "empty response"),
    ],
)
async def test_a_reported_failure_falls_through_under_a_second(
    fish, fish_server, monkeypatch, mode, cooldown_at_least, reason_has
):
    state, url = fish_server
    state["mode"] = mode
    _point_at(monkeypatch, url)
    audio, elapsed = await _timed_stream(fish)
    await fish.close()
    assert audio == b"", "a failing Fish must yield nothing - that is the fallback signal"
    assert elapsed < FAST, f"{mode}: fell through in {elapsed:.2f}s"
    assert reason_has in fish.last_error
    assert fish.cooldown_remaining() >= cooldown_at_least


async def test_a_hung_fish_is_cut_at_the_stall_deadline_not_45s(fish, fish_server, monkeypatch):
    state, url = fish_server
    state["mode"] = "hang"
    _point_at(monkeypatch, url)  # default deadline
    audio, elapsed = await _timed_stream(fish)
    await fish.close()
    assert audio == b""
    assert tts_fish.STALL_TIMEOUT_S - 0.2 < elapsed < tts_fish.STALL_TIMEOUT_S + 1.0
    assert fish.last_error.startswith("stalled")
    assert fish.in_cooldown(), "a hung Fish must not cost every later sentence the deadline"


async def test_the_stall_deadline_is_configurable(fish, fish_server, monkeypatch):
    state, url = fish_server
    state["mode"] = "hang"
    _point_at(monkeypatch, url, stall_timeout_s=0.4)
    audio, elapsed = await _timed_stream(fish)
    await fish.close()
    assert audio == b""
    assert elapsed < FAST


async def test_a_mid_stream_stall_keeps_what_played_and_cools_down(fish, fish_server, monkeypatch):
    state, url = fish_server
    state["mode"] = "stall"
    _point_at(monkeypatch, url, stall_timeout_s=0.4)
    audio, elapsed = await _timed_stream(fish)
    await fish.close()
    assert audio == PCM, "audio that already arrived is kept, not replayed by Gemini"
    assert elapsed < FAST
    assert fish.in_cooldown()


async def test_a_refused_connection_falls_through(fish, monkeypatch):
    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    _point_at(monkeypatch, f"http://127.0.0.1:{port}/v1/tts")
    audio, elapsed = await _timed_stream(fish)
    await fish.close()
    assert audio == b""
    # Windows retries a refused loopback SYN for ~2 s before reporting it, so a
    # refusal can surface as the connect deadline instead. Either way: bounded.
    assert elapsed < 2 * tts_fish.STALL_TIMEOUT_S + 1.0, f"took {elapsed:.2f}s"
    assert fish.in_cooldown()


# ── Fish mode: Fish fails -> Gemini's held audio takes the turn ──────────────
class _Era:
    def __init__(self):
        self.audio_in_queue = asyncio.Queue()
        self._turn_done_event = asyncio.Event()
        self.fallbacks: list[tuple[float, str]] = []

    def _tts_fallback(self, reason, held_audio=None):
        self.fallbacks.append((time.perf_counter(), reason))


@pytest.mark.parametrize("mode, extra", [("429", {}), ("hang", {"stall_timeout_s": 0.4})])
async def test_fish_mode_hands_the_turn_to_gemini_fast(fish, fish_server, monkeypatch, mode, extra):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO, raising=False)
    state, url = fish_server
    state["mode"] = mode
    _point_at(monkeypatch, url, **extra)
    era = _Era()
    speech = so.SpeechOut(era)
    t0 = time.perf_counter()
    speech.say("This sentence cannot be voiced by Fish.")
    deadline = t0 + 3.0
    while not era.fallbacks and time.perf_counter() < deadline:
        await asyncio.sleep(0.01)
    await speech.stop()
    await fish.close()
    assert era.fallbacks, "Fish failed and nobody asked Gemini to take over"
    at, reason = era.fallbacks[0]
    assert at - t0 < FAST, f"{mode}: Gemini took over after {at - t0:.2f}s"
    assert "fish synthesis failed" in reason and fish.last_error in reason


def test_the_gemini_takeover_is_logged_with_from_to_and_reason(monkeypatch):
    from app.era_live import audio

    events = []
    monkeypatch.setattr(audio, "log_event", lambda _l, name, _lv, **f: events.append((name, f)))
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO, raising=False)
    monkeypatch.setattr(VoiceModeConfig, "_write", lambda self: None, raising=False)

    class _UI:
        def write_log(self, _m):
            pass

    class _Q:
        def put_nowait(self, _c):
            pass

    class Engine(audio.AudioIOMixin):
        def __init__(self):
            self.ui = _UI()
            self.audio_in_queue = _Q()
            self._cloud_audio_buffer = []
            self._tts_primary_retry_at = 0.0
            self._tts_primary_mode = ""

    Engine()._tts_fallback("fish synthesis failed: HTTP 429", held_audio=[b"a", b"b"])
    assert VOICE_CFG.mode == MODE_GEMINI_AUDIO
    assert events == [
        (
            "voice.fallback",
            {
                "from_backend": MODE_FISH_AUDIO,
                "to_backend": MODE_GEMINI_AUDIO,
                "reason": "fish synthesis failed: HTTP 429",
                "replayed_chunks": 2,
            },
        )
    ]


# ── Local loop (Gemini down): Fish -> Kokoro -> text only ────────────────────
@pytest.fixture
def events(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(local_tts, "_event", lambda name, **f: seen.append((name, f)))
    return seen


def _kokoro_says(monkeypatch, pcm=PCM, why=""):
    async def fake(text):
        return pcm, why

    monkeypatch.setattr(local_tts, "_kokoro_pcm", fake)


async def test_local_loop_falls_from_fish_to_kokoro_fast(fish, fish_server, monkeypatch, events):
    state, url = fish_server
    state["mode"] = "500"
    _point_at(monkeypatch, url)
    _kokoro_says(monkeypatch)
    t0 = time.perf_counter()
    pcm, backend = await local_tts.synthesize_best("Fish is down.", "en")
    elapsed = time.perf_counter() - t0
    assert (pcm, backend) == (PCM, "kokoro")
    assert elapsed < FAST
    assert events == [
        (
            "voice.fallback",
            {"from_backend": "fish", "to_backend": "kokoro", "reason": "HTTP 500: chaos"},
        )
    ]
    # Fish is cooling down now: the next sentence goes straight to Kokoro, no request.
    hits = state["hits"]
    t1 = time.perf_counter()
    assert (await local_tts.synthesize_best("Next one.", "en"))[1] == "kokoro"
    assert time.perf_counter() - t1 < 0.2
    assert state["hits"] == hits
    await fish.close()


async def test_kokoro_failing_too_means_text_only_said_once(fish, fish_server, monkeypatch, events):
    state, url = fish_server
    state["mode"] = "503"
    _point_at(monkeypatch, url)
    _kokoro_says(monkeypatch, pcm=b"", why="Kokoro failed: RuntimeError")
    t0 = time.perf_counter()
    assert await local_tts.synthesize_best("Nobody can say this.", "en") == (b"", "none")
    assert time.perf_counter() - t0 < FAST
    notice = local_tts.voice_unavailable_notice()
    assert notice.startswith("SYS: Voice unavailable (fish: HTTP 503: chaos; kokoro: Kokoro failed")
    assert "text only" in notice
    assert await local_tts.synthesize_best("Still nothing.", "en") == (b"", "none")
    assert local_tts.voice_unavailable_notice() == "", "one HUD line per outage, not per sentence"
    assert [n for n, _ in events].count("voice.unavailable") == 1

    # Kokoro comes back: the outage closes, and a NEW outage would be said again.
    _kokoro_says(monkeypatch)
    assert (await local_tts.synthesize_best("Back.", "en"))[1] == "kokoro"
    assert events[-1][0] == "voice.restored"
    await fish.close()


async def test_local_play_shows_the_notice_on_the_hud(monkeypatch):
    from app.era_live.local_fallback import LocalFallbackMixin

    monkeypatch.setattr(
        local_tts, "_outage", {"reason": "kokoro: Kokoro failed: X", "noticed": False}
    )
    logs = []

    class _UI:
        def write_log(self, m):
            logs.append(m)

    era = LocalFallbackMixin.__new__(LocalFallbackMixin)
    era.ui = _UI()
    await era._local_play(b"")
    await era._local_play(b"")
    assert logs == ["SYS: Voice unavailable (kokoro: Kokoro failed: X) - replying in text only."]
