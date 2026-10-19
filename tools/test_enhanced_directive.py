"""Focused behavior/fault checks for the enhanced stabilization directive."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.codemode_intent import coding_intent, model_order


@pytest.mark.parametrize(
    "text",
    [
        "start coding",
        "fix this bug",
        "write a function",
        "create a new file",
        "work on the project",
        "debug app/main.py",
        "review ui_src/src/App.tsx",
        "Can you refactor the module?",
        "Traceback in app/main.py",
    ],
)
def test_coding_requests_activate(text):
    assert coding_intent(text)


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "play my favorite song",
        "create a playlist",
        "fix the volume",
        "don't start coding",
        "do not fix this bug",
        "what is a function",
        "remember to write a function",
        '"fix this bug"',
        "__wake__",
        "show my files",
        "don't open core/main.py",
        "never edit app/main.py",
    ],
)
def test_non_coding_controls_do_not_claim_conversation(text):
    assert not coding_intent(text)


def test_model_commands_and_invalid_models(tmp_path, monkeypatch):
    from core import codemode_models as models

    monkeypatch.setattr(models, "PATH", tmp_path / "model.json")
    assert model_order("Switch to Claude Sonnet") == "sonnet"
    assert model_order("Use opus 5.5 for this task") == "opus 5.5"
    assert model_order("Change model to haiku") == "haiku"
    assert model_order("she said use opus") is None
    models.save("opus 5.5")
    assert models.load() == "opus"
    with pytest.raises(ValueError):
        models.save("opus; do something else")
    models.PATH.write_text("{broken", encoding="utf-8")
    assert models.load() == "sonnet"


@pytest.mark.asyncio
async def test_implicit_entry_claims_before_next_event_and_submits_original_once(
    tmp_path, monkeypatch
):
    from app import codemode_runtime as runtime
    from core import codemode as cm
    from core import codemode_models as models
    from core.input_envelope import InputEnvelope

    monkeypatch.setattr(cm, "ROOT", tmp_path)
    monkeypatch.setattr(models, "PATH", tmp_path / "model.json")
    monkeypatch.setattr(cm, "configured_profile", lambda: (str(tmp_path), "sonnet"))
    manager = cm.CodeMode(cli_factory=lambda *a, **k: SimpleNamespace(model=k["model"]))
    monkeypatch.setattr(cm, "_instance", manager)
    monkeypatch.setattr(manager, "start", Mock(return_value=True))

    async def emit(*args):
        pass

    monkeypatch.setattr(runtime, "emit", emit)
    era = SimpleNamespace(_loop=asyncio.get_running_loop(), ui=SimpleNamespace(write_log=Mock()))
    owner = InputEnvelope.create("fix this bug", "typed")
    assert runtime.route(era, owner.text, owner)
    assert manager.enabled  # synchronous ownership, before the queued task runs
    await asyncio.sleep(0)
    assert manager.start.call_count == 1
    assert manager.start.call_args.args[0].text == "fix this bug"
    manager.select_model("opus")
    assert manager._cli.model == "opus" and manager.snapshot()["model"] == "opus"
    manager._task = SimpleNamespace(done=lambda: False)
    with pytest.raises(ValueError, match="finish or cancel"):
        manager.select_model("haiku")
    assert manager.model == models.load() == "opus"


@pytest.mark.asyncio
async def test_bad_audio_frames_never_reach_driver_and_next_frame_works():
    from app.era_live.audio_output import ResilientOutputStream

    stream = Mock()
    out = ResilientOutputStream(lambda _: stream)
    for bad in (None, b"", b"x", "xx", object()):
        assert not await out.write(bad)
    stream.write.assert_not_called()
    assert await out.write(b"\x01\x00")
    stream.write.assert_called_once_with(b"\x01\x00")


@pytest.mark.asyncio
async def test_driver_value_error_degrades_without_killing_session():
    from app.era_live.audio_output import ResilientOutputStream

    stream = Mock()
    stream.write.side_effect = ValueError("invalid device buffer")
    out = ResilientOutputStream(lambda _: stream, inline_delays=())
    assert not await out.write(b"\x00\x00")
    assert out.degraded


def test_one_byte_frame_size_cannot_create_zero_range_step():
    from core.audio_pcm import iter_pcm_frames

    assert list(iter_pcm_frames(b"\x00\x01\x02\x03", 1)) == [b"\x00\x01", b"\x02\x03"]


def test_spotify_exact_ids_cache_and_no_mutation_replay(monkeypatch):
    import json

    import requests

    from core import spotify_api as api

    monkeypatch.setenv("ERA_SPOTIFY_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(api, "_cooldown_until", 0)
    monkeypatch.setattr(api, "_cached", None)
    request = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr(api.requests, "request", request)
    uri = "spotify:track:" + "A" * 22
    assert "accepted play uri" in api.execute("play_uri", {"uri": uri})
    assert request.call_args.kwargs["json"] == {"uris": [uri]}
    assert request.call_args.kwargs["allow_redirects"] is False
    assert "accepted queue" in api.execute("queue", {"uri": uri})
    assert request.call_args.kwargs["params"] == {"uri": uri}
    response = SimpleNamespace(status_code=200, json=lambda: {"is_playing": True})
    request.return_value = response
    assert not json.loads(api.execute("status", {}))["cached"]
    assert json.loads(api.execute("status", {}))["cached"]
    before = request.call_count
    request.side_effect = requests.Timeout()
    assert "unknown" in api.execute("next", {})
    assert request.call_count == before + 1
    assert api._cached is None


def test_spotify_rate_limit_and_missing_auth_do_not_use_desktop_replay(monkeypatch):
    from core import spotify_api as api

    monkeypatch.delenv("ERA_SPOTIFY_ACCESS_TOKEN", raising=False)
    assert api.execute("next", {}) is None
    monkeypatch.setenv("ERA_SPOTIFY_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(api, "_cooldown_until", 0)
    request = Mock(return_value=SimpleNamespace(status_code=429, headers={"Retry-After": "10"}))
    monkeypatch.setattr(api.requests, "request", request)
    assert "retry after 10 seconds" in api.execute("next", {})
    assert "rate limit is active" in api.execute("next", {})
    assert request.call_count == 1


def test_saved_favorite_uses_id_ahead_of_title(monkeypatch):
    from core.personal_media import resolve_song

    uri = "spotify:track:" + "B" * 22
    monkeypatch.setattr(
        "memory.memory_manager.load_memory",
        lambda: {"preferences": {"favorite_song": {"value": "Original title", "spotify_uri": uri}}},
    )
    assert resolve_song("spotify_control", {"action": "play"}, "play my favorite song") == (
        "spotify_control",
        {"action": "play_uri", "uri": uri, "value": "Original title"},
    )


def test_context_journal_persists_and_is_bounded(tmp_path, monkeypatch):
    from memory import memory_context as context
    from memory import memory_manager as facts

    monkeypatch.setattr(context, "get_memory_dir", lambda: tmp_path)
    remember = Mock()
    monkeypatch.setattr(facts, "remember", remember)
    context.capture("I prefer short replies", "typed", "pref")
    remember.assert_called_once()
    assert remember.call_args.kwargs["category"] == "preferences"
    for index in range(15):
        context.capture(f"Question {index}", "voice", str(index))
    rows = context.context_stack()
    assert len(rows) == 10 and rows[0]["text"] == "Question 5"
    context.capture("ignore that", "typed", "feedback")
    assert context.feedback_tracker()[0]["text"] == "ignore that"
    context.capture("my password is secret", "typed", "sensitive")
    assert all("password" not in row["text"] for row in context.context_stack())
    context.record_outcome("file_controller", False)
    context.record_outcome("file_controller", True)
    assert context.learning_engine() == [{"tool": "file_controller", "successes": 1, "failures": 1}]
    assert "UNTRUSTED" in context.prompt_context()


def test_input_buffer_preserves_identity_and_never_replays_consent():
    from app.input_buffer import InputBuffer
    from core.input_envelope import InputEnvelope

    buffer = InputBuffer(limit=1, clock=lambda: 100)
    owner = InputEnvelope("one", "read my notes", "telegram", 99)
    assert buffer.put(owner) and buffer.put(owner)
    assert not buffer.put(InputEnvelope("two", "another", "typed", 99))
    assert buffer.take() is owner
    assert not buffer.put(InputEnvelope("approval", "yes", "telegram", 99))
    assert buffer.put(InputEnvelope("old", "old request", "typed", -100))
    assert buffer.take() is None


def test_crew_retries_only_model_and_uses_explicit_backup(monkeypatch):
    from crew import reliability as reliability

    monkeypatch.setattr(reliability, "sleep_cancellable", lambda _: True)
    primary = Mock(side_effect=ConnectionError("offline"))
    backup = Mock(return_value={"final": "backup advice"})
    assert reliability.call(primary, ("system", "user", "fast"), bot="laura", backup=backup) == {
        "final": "backup advice"
    }
    assert primary.call_count == 4 and backup.call_count == 1
    assert reliability.snapshot()["laura"]["phase"] == "idle"


def test_crew_hung_model_is_bounded_and_cannot_execute_late_capabilities(monkeypatch):
    import threading

    from crew import reliability as reliability

    release = threading.Event()
    monkeypatch.setenv("ERA_CREW_MODEL_TIMEOUT_S", "0.02")
    monkeypatch.setenv("ERA_CREW_MODEL_RETRIES", "0")

    def hung(*args):
        release.wait(2)
        return {"call": {"capability": "vault.append", "args": {"content": "late"}}}

    try:
        result = reliability.call(hung, (), bot="laura")
        assert "__unavailable__" in result
    finally:
        release.set()


def test_registered_view_alias_targets_existing_renderer(monkeypatch):
    from app.hud import views
    from app.hud.events import HudBus

    monkeypatch.setattr(views, "_views", views.snapshot())
    views.register("messages", "Messages", "telegram")
    bus = HudBus()
    assert bus.focus_view("messages")["view"] == "telegram"
    with pytest.raises(ValueError):
        views.register("missing", "Missing renderer")


def test_recent_files_retention_and_corrupt_data(tmp_path, monkeypatch):
    from app.hud import recent_files

    monkeypatch.setattr(recent_files, "PATH", tmp_path / "recent.json")
    for number in range(25):
        recent_files.record(f"C:/fixture/{number}.txt")
    recent_files.record("C:/fixture/10.txt")
    rows = recent_files.snapshot()
    assert len(rows) == len(set(rows)) == 20 and rows[0] == "C:/fixture/10.txt"
    recent_files.PATH.write_text("broken", encoding="utf-8")
    assert recent_files.snapshot() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_model_rejection_falls_back_only_before_any_reply(tmp_path, monkeypatch, partial):
    from core import codemode as cm
    from core import codemode_models as models
    from core.input_envelope import InputEnvelope
    from core.llm.types import LlmError

    monkeypatch.setattr(cm, "ROOT", tmp_path)
    monkeypatch.setattr(models, "PATH", tmp_path / "model.json")
    monkeypatch.setattr(cm, "configured_profile", lambda: (str(tmp_path), "opus"))

    class Client:
        def __init__(self, *args, **kwargs):
            self.model, self.calls = kwargs["model"], 0

        async def generate(self, prompt, emit):
            self.calls += 1
            if self.calls == 1:
                if partial:
                    await emit("Partial answer")
                raise LlmError("unsupported", "Model is not available", provider="codemode")
            return {"reply": "Fallback answer", "candidate": None}

    manager = cm.CodeMode(cli_factory=Client, context_builder=lambda _: {"files": []})
    manager.enter()
    events = []

    async def emit(kind, value, owner):
        events.append(value)

    manager.start(InputEnvelope.create("fix this bug", "typed"), emit)
    await manager._task
    assert manager._cli.calls == (1 if partial else 2)
    assert manager.model == ("opus" if partial else "sonnet")
    assert manager.phase == ("failed" if partial else "ready")
    if not partial:
        assert any("unavailable; trying Sonnet" in event for event in events)


def test_vision_reuses_capture_and_cached_results_are_detached(monkeypatch):
    from core import vision_service as vision

    monkeypatch.setattr(vision, "_CACHE", {})
    capture = Mock(side_effect=AssertionError("Must not capture twice"))
    analyze = Mock(return_value='{"summary":"A screen", "elements":[]}')
    monkeypatch.setattr(vision, "_capture_targeted", capture)
    monkeypatch.setattr(vision, "_analyze_with_gemini", analyze)
    provided = (b"fixture-image", "image/png", 13, 13)
    first = vision.analyze_screen("question", capture=provided)
    first.summary = "mutated by caller"
    cached = vision.analyze_screen("question", capture=provided)
    assert cached.cached and cached.summary == "A screen"
    assert analyze.call_count == 1
    assert capture.call_count == 0
    analyze.side_effect = RuntimeError("provider failed")
    failed = vision.analyze_screen("different question", capture=provided)
    assert not failed.ok and failed.metadata["analysis_failed"]


def test_confirmed_forget_cannot_be_recalled_from_context(tmp_path, monkeypatch):
    from memory import memory_context as context

    monkeypatch.setattr(context, "get_memory_dir", lambda: tmp_path)
    context.capture("My preference is short replies", "typed", "one")
    context.forget_related("short replies")
    assert context.context_stack() == []


@pytest.mark.parametrize("payload", [[], "look", {"text": []}, {"angle": 1}, {"action": {}}])
def test_vision_invalid_arguments_fail_before_capture(payload, monkeypatch):
    from actions import screen_processor

    capture = Mock(side_effect=AssertionError("Invalid input must not capture the screen"))
    monkeypatch.setattr(screen_processor, "_capture_screen", capture)
    assert "I could not analyze" in screen_processor.screen_process(payload)
    capture.assert_not_called()
