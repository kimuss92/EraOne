"""Failure-path regressions: no cloud, desktop, model, or audio hardware needed."""

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.era_live.audio import AudioIOMixin
from app.era_live.local_fallback import LocalFallbackMixin
from app.era_live.session_runtime import live_model, managed_live_connection, stop_on_renewal
from core import utils
from core.llm.types import LlmError, StreamEvent
from core.local_brain import LocalBrain


class Provider:
    def __init__(self, stream):
        self._stream = stream
        self.cancelled = []
        self.release = threading.Event()

    def stream(self, request):
        yield from self._stream(self)

    def cancel(self, request_id):
        self.cancelled.append(request_id)
        self.release.set()


async def test_provider_thread_exception_reaches_the_turn():
    def fail(_):
        raise ValueError("malformed response")
        yield

    provider = Provider(fail)
    brain = LocalBrain(provider=provider, tools=[])
    with pytest.raises(LlmError, match="Model stream failed"):
        _ = [s async for s in brain.respond("hello")]
    assert brain._active_request == ""


async def test_silent_provider_has_a_total_deadline():
    def stall(provider):
        provider.release.wait(5)
        yield StreamEvent(kind="turn_complete")

    provider = Provider(stall)
    brain = LocalBrain(provider=provider, tools=[], timeout_s=0.05)
    with pytest.raises(LlmError) as caught:
        await asyncio.wait_for(anext(brain.respond("hello")), timeout=1)
    assert caught.value.kind == "timeout"
    assert provider.cancelled


async def test_cancel_without_provider_events_does_not_wait_for_socket_timeout():
    def stall(provider):
        provider.release.wait(5)
        yield StreamEvent(kind="turn_complete")

    provider = Provider(stall)
    brain = LocalBrain(provider=provider, tools=[], timeout_s=10)

    async def consume():
        return [s async for s in brain.respond("hello")]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    brain.cancel()
    assert await asyncio.wait_for(task, timeout=1) == []


async def test_stream_backpressure_and_generator_close_release_producer():
    produced = []

    def flood(provider):
        for i in range(10000):
            produced.append(i)
            yield StreamEvent(kind="text_delta", text="A complete sentence here. ")

    provider = Provider(flood)
    brain = LocalBrain(provider=provider, tools=[])
    stream = brain._pass([])
    await anext(stream)
    await asyncio.sleep(0.02)
    assert len(produced) < 100
    await stream.aclose()
    assert provider.cancelled


def test_env_key_serves_both_live_and_generate(monkeypatch):
    monkeypatch.setattr(utils, "load_config", lambda: {})
    monkeypatch.setenv("GEMINI_API_KEY", "test-env-key")
    assert utils.get_api_key() == utils.get_api_keys()[0] == "test-env-key"


@pytest.mark.parametrize(
    "placeholder", ["YOUR_GEMINI_API_KEY", "placeholder", "<key>", "CHANGE_ME"]
)
def test_example_credentials_do_not_shadow_env(monkeypatch, placeholder):
    monkeypatch.setattr(utils, "load_config", lambda: {"gemini_api_key": placeholder})
    monkeypatch.setenv("GEMINI_API_KEY", "test-env-key")
    assert utils.get_api_key() == "test-env-key"


@pytest.mark.parametrize("content", ["[]", "null", '"text"', "{bad json"])
def test_malformed_config_cannot_crash_key_lookup(tmp_path, monkeypatch, content):
    path = tmp_path / "keys.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(utils, "CONFIG_PATH", path)
    monkeypatch.setenv("GEMINI_API_KEY", "test-env-key")
    assert utils.get_api_key() == "test-env-key"


class Client:
    def __init__(self, *, stall=False):
        self.events = []
        self.stall = stall
        self.aio = self
        self.live = self

    async def __aenter__(self):
        self.events.append("client open")
        return self

    async def __aexit__(self, *_):
        self.events.append("client closed")

    def close(self):
        self.events.append("sync closed")

    @asynccontextmanager
    async def connect(self, **_):
        try:
            if self.stall:
                await asyncio.sleep(10)
            self.events.append("session open")
            yield self
        finally:
            self.events.append("session closed")


async def test_transports_close_after_worker_failure():
    client = Client()
    with pytest.raises(ValueError):
        async with managed_live_connection(client, model="fake", config={}):
            raise ValueError("worker failed")
    assert client.events == [
        "client open",
        "session open",
        "session closed",
        "client closed",
        "sync closed",
    ]


async def test_connection_deadline_closes_transports():
    client = Client(stall=True)
    with pytest.raises(TimeoutError):
        async with managed_live_connection(client, model="fake", config={}, timeout_s=0.02):
            pytest.fail("must not connect")
    assert client.events[-2:] == ["client closed", "sync closed"]


async def test_renewal_cancels_a_blocked_receiver():
    renewal = asyncio.Event()
    stopped = asyncio.Event()

    async def receive_forever():
        try:
            await asyncio.sleep(100)
        finally:
            stopped.set()

    async with asyncio.TaskGroup() as group:
        receiver = group.create_task(receive_forever())
        await asyncio.sleep(0)
        renewal.set()
        await stop_on_renewal(renewal, [receiver])
    assert receiver.cancelled() and stopped.is_set()


def test_live_model_override_requires_no_code_edit(monkeypatch):
    monkeypatch.setenv("ERA_LIVE_MODEL", "models/configured-model")
    assert live_model("fallback") == "models/configured-model"


async def test_mic_device_error_retries_without_touching_cloud(monkeypatch):
    import app.era_live.audio as audio

    owner = AudioIOMixin()
    owner._go_away_event = asyncio.Event()
    owner._shutdown_requested = False
    owner.out_queue = asyncio.Queue()
    owner.ui = SimpleNamespace(write_log=lambda _: None)
    attempts = []

    class Stream:
        def __enter__(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise audio.sd.PortAudioError("device unplugged")
            owner._go_away_event.set()
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(audio.sd, "InputStream", lambda **_: Stream())
    await asyncio.wait_for(owner._listen_audio(), timeout=2)
    assert len(attempts) == 2


async def test_empty_live_receive_does_not_spin():
    owner = AudioIOMixin()

    async def empty():
        if False:
            yield

    owner.session = SimpleNamespace(receive=empty)
    owner._go_away_event = asyncio.Event()
    owner._is_expected_rotation = lambda _: False
    owner._is_network_blip = lambda _: True
    with pytest.raises(ConnectionError, match="ended without messages"):
        await asyncio.wait_for(owner._receive_audio(), timeout=1)


async def test_model_and_tts_failure_still_returns_a_visible_reply(monkeypatch):
    import core.local_tts as tts

    class Brain:
        async def respond(self, *_):
            raise LlmError("transient", "offline")
            yield

        def cancel(self):
            pass

    async def no_tts(*_, **__):
        raise OSError("voice unavailable")

    async def no_audio(_):
        pass

    owner = LocalFallbackMixin()
    logs = []
    owner.ui = SimpleNamespace(set_state=lambda _: None, write_log=logs.append, muted=False)
    owner._standby_active = False
    owner._local_brain = Brain()
    owner._local_execute_tool = None
    owner._local_fish_ok = lambda: False
    owner._local_play = no_audio
    owner._tg_ack = lambda _: None
    monkeypatch.setattr(tts, "synthesize_best", no_tts)
    reply = await asyncio.wait_for(owner._local_turn("hello"), timeout=1)
    assert "isn't responding" in reply
    assert any("Era:" in line for line in logs)


@pytest.mark.parametrize("value", ["typo", "NaN", "inf", "-1", "0", "999999"])
def test_invalid_cooldown_is_not_a_crash_or_retry_storm(monkeypatch, value):
    monkeypatch.setenv("ERA_QUOTA_COOLDOWN_SECONDS", value)
    assert utils.env_float("ERA_QUOTA_COOLDOWN_SECONDS", 900, minimum=1, maximum=7200) == 900


async def test_cross_thread_failure_is_observed_and_reported(monkeypatch):
    from core import async_submit

    events = []
    monkeypatch.setattr(async_submit, "log_event", lambda *a, **kw: events.append(kw))

    async def fail():
        raise ValueError("command failed")

    future = async_submit.submit_coroutine(fail(), asyncio.get_running_loop())
    with pytest.raises(ValueError):
        await asyncio.wrap_future(future)
    assert events == [{"error_type": "ValueError"}]


def test_submission_to_a_closed_loop_releases_coroutine(monkeypatch):
    from core import async_submit

    async def command():
        return None

    coro = command()
    loop = asyncio.new_event_loop()
    loop.close()
    monkeypatch.setattr(async_submit, "log_event", lambda *a, **kw: None)
    assert async_submit.submit_coroutine(coro, loop) is None
    assert coro.cr_frame is None


def test_missing_manifest_is_an_exception_not_process_exit(monkeypatch, tmp_path):
    from tools import era_gate

    monkeypatch.setattr(era_gate, "MANIFEST", tmp_path / "missing.json")
    monkeypatch.setattr(era_gate, "REPO", tmp_path)
    with pytest.raises(FileNotFoundError, match="era_manifest.py"):
        era_gate.load_manifest()


@pytest.mark.parametrize(
    "path", [r"C:\Windows\System32", r"C:\Program Files\App", r"C:\temp\..\Windows\x"]
)
def test_windows_system_paths_keep_their_meaning_on_any_validator(path):
    assert not utils.is_safe_path(path)


async def test_engine_renewal_stops_all_workers_and_releases_the_sdk(monkeypatch, tmp_path):
    import main
    from app.hud.ui_base import HeadlessUI

    monkeypatch.setattr("actions.file_indexer.init_file_index", lambda: None)
    monkeypatch.setattr(main, "init_vosk", lambda: None)
    monkeypatch.setattr(main, "get_api_keys", lambda: ["test-key"])
    monkeypatch.setattr(main, "live_model", lambda _: "fake-model")
    monkeypatch.setenv("ERA_CONNECTION_STATUS_FILE", str(tmp_path / "connection.json"))
    engine = main.EraLive(HeadlessUI())
    client = Client()
    engine._current_client = lambda: client
    engine._build_config = lambda: {}
    engine.session_manager.load_from_disk = lambda: False
    engine.session_manager.save_to_disk = lambda: None
    engine.restore_mic_mute = lambda: None
    engine.speak = lambda _: None

    async def no_cascade():
        return False

    async def no_resume():
        await asyncio.sleep(0)

    stopped = []

    async def parked_worker():
        try:
            await asyncio.sleep(100)
        finally:
            stopped.append(True)

    async def receiver():
        engine._shutdown_requested = True
        engine._go_away_event.set()
        await parked_worker()

    engine.serve_cascade = no_cascade
    engine._resume_after_disconnect = no_resume
    for name in ("_send_realtime", "_listen_audio", "_play_audio", "_monitor_idle_state"):
        setattr(engine, name, parked_worker)
    engine._receive_audio = receiver
    await asyncio.wait_for(engine.run(), timeout=1)
    assert len(stopped) == 5
    assert engine.session is None
    assert client.events[-3:] == ["session closed", "client closed", "sync closed"]
