"""Tests for the cascade voice mode: the OpenAI-compatible (CodeCraft) adapter,
the spoken voice switch, and the local loop's cascade hooks.

No network, no mic, no speaker.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace

from app.era_live import cascade
from app.era_live import local_fallback as lf
from core.llm.adapters import openai_compat as oa
from core.llm.types import LlmRequest, PromptBlock
from core.local_brain import CASCADE_SYSTEM_PROMPT, SYSTEM_PROMPT

CFG = {"base_url": "https://api.example.test/v1", "model": "m-1", "api_key": "k"}


def req(extra=None, tools=()):
    return LlmRequest(
        alias="era.codecraft",
        system=(PromptBlock(text="sys"),),
        tools=tuple(tools),
        request_id="r1",
        provider_options={"ollama": {"extra_messages": extra or []}},
    )


def sse(*chunks) -> io.BytesIO:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return io.BytesIO(body.encode())


# ── config ──────────────────────────────────────────────────────────────────


def test_placeholder_is_not_configured():
    ok, why = oa.is_configured({"base_url": "placeholder", "model": "placeholder", "api_key": "x"})
    assert not ok and "CODECRAFT_BASE_URL" in why and "CODECRAFT_MODEL" in why
    assert "x" not in why  # never echoes the key


def test_real_values_are_configured():
    assert oa.is_configured(CFG) == (True, "")
    assert not oa.is_configured({**CFG, "base_url": "api.example.test"})[0]


def test_env_wins(monkeypatch):
    monkeypatch.setenv("CODECRAFT_BASE_URL", "https://env.test/v1")
    monkeypatch.setenv("CODECRAFT_MODEL", "fast")
    monkeypatch.setenv("CODECRAFT_API_KEY", "secret")
    cfg = oa.codecraft_config()
    assert cfg == {"base_url": "https://env.test/v1", "model": "fast", "api_key": "secret"}


def test_top_level_keys_and_template_text(monkeypatch):
    import core.utils

    for k in ("CODECRAFT_BASE_URL", "CODECRAFT_MODEL", "CODECRAFT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(
        core.utils,
        "load_config",
        lambda: {
            "CODECRAFT_BASE_URL": "https://codecraftapi.com/v1",
            "CODECRAFT_MODEL": "<their fastest non-reasoning model>",
            "CODECRAFT_API_KEY": "k",
        },
    )
    cfg = oa.codecraft_config()
    assert cfg["base_url"] == "https://codecraftapi.com/v1" and cfg["api_key"] == "k"
    assert cfg["model"] == oa.PLACEHOLDER  # template text copied verbatim
    assert "CODECRAFT_MODEL" in oa.is_configured(cfg)[1]


def test_request_sends_a_non_default_user_agent(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(req.headers)
        return sse()

    monkeypatch.setattr(oa.urllib.request, "urlopen", fake_urlopen)
    list(oa.OpenAICompatProvider(CFG).stream(req()))
    # Cloudflare in front of CodeCraft 403s urllib's default agent (error 1010).
    assert seen.get("User-agent") == oa.USER_AGENT


# ── message translation ─────────────────────────────────────────────────────


def test_tool_history_translated_to_openai():
    extra = [
        {"role": "user", "content": "pause the music"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "spotify_control", "arguments": {"a": 1}}}],
        },
        {"role": "tool", "tool_name": "spotify_control", "content": '{"success": true}'},
        {"role": "assistant", "content": "Music paused."},
    ]
    msgs = oa.to_openai_messages(req(extra))
    assert msgs[0] == {"role": "system", "content": "sys"}
    call = msgs[2]["tool_calls"][0]
    assert call["type"] == "function" and json.loads(call["function"]["arguments"]) == {"a": 1}
    assert msgs[3] == {"role": "tool", "tool_call_id": call["id"], "content": '{"success": true}'}
    assert msgs[4] == {"role": "assistant", "content": "Music paused."}


def test_unanswered_call_is_closed_and_orphan_dropped():
    extra = [
        {"role": "tool", "tool_name": "x", "content": "orphan"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "a", "arguments": {}}},
                {"function": {"name": "b", "arguments": {}}},
            ],
        },
        {"role": "tool", "tool_name": "a", "content": "armed"},
        {"role": "assistant", "content": "Shall I proceed?"},
    ]
    msgs = oa.to_openai_messages(req(extra))
    assert all(m.get("content") != "orphan" for m in msgs)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    ids = [c["id"] for c in msgs[1]["tool_calls"]]
    assert [m["tool_call_id"] for m in tool_msgs] == ids  # b answered with "Not run."
    assert "Not run" in tool_msgs[1]["content"]
    assert msgs[-1]["content"] == "Shall I proceed?"


# ── streaming ───────────────────────────────────────────────────────────────


def provider(opens, sleeps=None):
    p = oa.OpenAICompatProvider(CFG, sleep=(sleeps.append if sleeps is not None else None))
    seq = iter(opens)

    def fake_open(body, cfg, timeout_s):
        item = next(seq)
        if isinstance(item, BaseException):
            raise item
        fake_open.body = body
        return item

    p._open = fake_open
    return p, fake_open


def test_text_and_fragmented_tool_call_stream():
    p, opener = provider(
        [
            sse(
                {"choices": [{"delta": {"content": "On it. "}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "audio_control", "arguments": '{"le'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": 'vel": 30}'}}]
                            }
                        }
                    ]
                },
                {"usage": {"prompt_tokens": 10, "completion_tokens": 4}, "choices": []},
            )
        ]
    )
    events = list(p.stream(req(tools=[{"type": "function", "function": {"name": "x"}}])))
    kinds = [e.kind for e in events]
    assert kinds == ["text_delta", "tool_call", "usage", "turn_complete"]
    call = events[1].tool_call
    assert (call.id, call.name, call.args) == ("call_1", "audio_control", {"level": 30})
    assert events[2].usage.input_tokens == 10
    assert opener.body["stream"] is True and opener.body["model"] == "m-1"
    assert opener.body["tools"]


def http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_retries_with_backoff_then_succeeds():
    sleeps = []
    p, _ = provider(
        [http_error(429), http_error(503), sse({"choices": [{"delta": {"content": "Hi."}}]})],
        sleeps,
    )
    events = list(p.stream(req()))
    assert events[0].text == "Hi."
    assert sleeps == [1.0, 2.0]


def test_auth_error_is_not_retried():
    sleeps = []
    p, _ = provider([http_error(401)], sleeps)
    events = list(p.stream(req()))
    assert events[0].kind == "error" and events[0].error.kind == "auth"
    assert sleeps == []


def test_unconfigured_errors_without_network():
    p = oa.OpenAICompatProvider({**CFG, "api_key": "placeholder"})
    p._open = lambda *a: (_ for _ in ()).throw(AssertionError("no network call expected"))
    events = list(p.stream(req()))
    assert events[0].kind == "error" and "CODECRAFT_API_KEY" in str(events[0].error)


class _Fake:
    def __init__(self, events):
        self.events = events

    def stream(self, request):
        yield from self.events

    def cancel(self, rid):
        pass


def test_first_that_answers_only_falls_back_before_output():
    from core.llm.types import LlmError, StreamEvent

    err = StreamEvent(kind="error", error=LlmError("transient", "down"))
    ok = StreamEvent(kind="text_delta", text="backup")
    assert [e.text for e in oa.FirstThatAnswers(_Fake([err]), _Fake([ok])).stream(req())] == [
        "backup"
    ]
    said = StreamEvent(kind="text_delta", text="half")
    out = list(oa.FirstThatAnswers(_Fake([said, err]), _Fake([ok])).stream(req()))
    assert [e.kind for e in out] == ["text_delta", "error"]  # never says it twice


# ── prompt + voice switch ───────────────────────────────────────────────────


def test_cascade_prompt_drops_backup_framing():
    assert "backup brain" in SYSTEM_PROMPT
    assert "backup brain" not in CASCADE_SYSTEM_PROMPT
    assert "offline" not in CASCADE_SYSTEM_PROMPT
    assert "TOOLS:" in CASCADE_SYSTEM_PROMPT and "LANGUAGE:" in CASCADE_SYSTEM_PROMPT


def test_voice_orders():
    for text in ("switch to my voice", "use cloned voice", "switch to fish", "use my cloned voice"):
        assert cascade.voice_order(text) == "fish", text
    for text in (
        "switch to premium voice",
        "use default voice",
        "switch to kokoro",
        "use natural voice",
        "Era, use the local voice.",
    ):
        assert cascade.voice_order(text) == "local", text
    for text in (
        "I like your voice",
        "switch to your voice",
        "play fish sounds",
        "use the default settings",
        "switch to spotify",
    ):
        assert cascade.voice_order(text) is None, text


# ── loop hooks ──────────────────────────────────────────────────────────────


class Stub(cascade.CascadeMixin, lf.LocalFallbackMixin):
    def __init__(self):
        self.logs, self.said, self.turns = [], [], []
        self.ui = SimpleNamespace(muted=False, write_log=self.logs.append, set_state=lambda s: None)
        self._standby_active = False
        self._shutdown_requested = False
        self._local_mode_active = True
        self._local_outage_reason = "cascade"
        self._local_exit = asyncio.Event()
        self._local_turn_task = None

    def _is_abort_order(self, text):
        return False

    def _is_reboot_order(self, text):
        return False

    async def _local_say(self, text):
        self.said.append(text)


def test_voice_order_handled_locally_without_the_brain(monkeypatch):
    from core import tts_fish, voice_mode

    modes = []
    monkeypatch.setattr(voice_mode.VOICE_CFG, "_write", lambda: None)
    monkeypatch.setattr(voice_mode.VOICE_CFG, "set_mode", lambda m, **k: modes.append(m))
    monkeypatch.setattr(tts_fish, "is_configured", lambda: True)
    s = Stub()
    asyncio.run(s._local_on_utterance("switch to premium voice", "en", 0.0))
    asyncio.run(s._local_on_utterance("use my cloned voice", "en", 0.0))
    assert modes == ["gemini", "fish"]
    assert s.said == ["Switched to premium voice.", "Switched to your cloned voice."]
    assert s._local_turn_task is None


def test_reconnect_in_cascade_pauses_it():
    s = Stub()
    asyncio.run(s._local_on_utterance("reconnect", "en", 0.0))
    assert s._local_exit.is_set() and s._cascade_paused
    assert s.cascade_ready()[0] is False


def test_fish_choice_only_applies_in_cascade(monkeypatch):
    from core import voice_mode

    s = Stub()
    monkeypatch.setattr(voice_mode.VOICE_CFG, "mode", voice_mode.MODE_GEMINI_AUDIO)
    assert s._local_fish_ok() is False
    s._local_outage_reason = "quota"  # a real outage always tries Fish first
    assert s._local_fish_ok() is True


def test_not_ready_without_codecraft(monkeypatch):
    for k in ("CODECRAFT_BASE_URL", "CODECRAFT_MODEL", "CODECRAFT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(oa, "codecraft_config", lambda: dict.fromkeys(CFG, "placeholder"))
    s = Stub()
    s._cascade_policy = lambda: {"enabled": True}
    ok, why = s.cascade_ready()
    assert not ok and "CodeCraft not configured" in why
    assert asyncio.run(s.serve_cascade()) is False
