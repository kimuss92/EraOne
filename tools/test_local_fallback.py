"""Tests for the local fallback wiring (app/era_live/local_fallback.py) and the
local stack's pure helpers (Ollama adapter, local STT/TTS).

No mic, no speaker, no Ollama: the mixin runs on a stub EraLive.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np

from app.era_live import local_fallback as lf
from core import local_stt, local_tts
from core.llm.adapters import ollama_chat as oc
from core.llm.types import LlmMessage, LlmRequest


class Stub(lf.LocalFallbackMixin):
    def __init__(self, gate_action="ALLOW"):
        self.logs, self.said, self.acks, self.noted = [], [], [], []
        self.ui = SimpleNamespace(muted=False, write_log=self.logs.append, set_state=lambda s: None)
        self._standby_active = False
        self._browser_mic_active = True
        self._shutdown_requested = False
        self._loop = None
        self.safety_gate = SimpleNamespace(
            validate_input=lambda **kw: SimpleNamespace(action=gate_action, reason="noise")
        )
        self._local_exit = asyncio.Event()
        self._local_turn_task = None
        self.turns = []
        self.aborted, self.shutdowns, self.cleared = [], [], 0

    # EraLive surface the mixin relies on
    def set_status(self, s):
        pass

    def _text_state_snapshot(self):
        return {}

    def _note_user_utterance(self, text, said_at=None):
        self.noted.append(text)

    def _tg_ack(self, m):
        self.acks.append(m)

    def _wake_word_found(self, text):
        return "shazam" in text.lower()

    async def _resume_from_standby(self, reason, speak_ack=True):
        self._standby_active = False

    async def _local_say(self, text):
        self.said.append(text)

    async def _local_turn(self, text, lang=""):
        self.turns.append(text)
        return "ok"

    def _is_abort_order(self, text):
        return text.lower() == "abort mission"

    def trigger_abort(self, source, text=""):
        self.aborted.append(text)

    def _is_reboot_order(self, text):
        return text.lower() == "reboot era one"

    def _shutdown_era(self, args):
        self.shutdowns.append(args)

    def mute_microphone(self, source, speak_ack=True):
        self._mic_muted = True

    def _clear_pending_work(self):
        self.cleared += 1


def utter(stub, text):
    async def go():
        await stub._local_on_utterance(text, "en", 0.0)
        if stub._local_turn_task is not None:
            await stub._local_turn_task

    asyncio.run(go())


# ── readiness and the run-loop entry point ──────────────────────────────────


def test_not_ready_returns_false_and_never_runs(monkeypatch):
    stub = Stub()
    monkeypatch.setattr(stub, "local_fallback_ready", lambda: (False, "models missing"))
    ran = []

    async def fake_run(seconds, reason):
        ran.append(seconds)

    stub._run_local_fallback = fake_run
    assert asyncio.run(stub.serve_locally_during_outage(900, "quota")) is False
    assert ran == []


def test_duration_is_capped_at_the_gemini_probe_interval(monkeypatch):
    stub = Stub()
    monkeypatch.setattr(stub, "local_fallback_ready", lambda: (True, ""))
    monkeypatch.setattr(
        stub, "_local_policy", lambda: {"enabled": True, "probe_gemini_every_s": 300}
    )
    ran = []

    async def fake_run(seconds, reason):
        ran.append((seconds, reason))

    stub._run_local_fallback = fake_run
    assert asyncio.run(stub.serve_locally_during_outage(7200, "quota")) is True
    assert ran == [(300, "quota")]


def test_loop_failure_degrades_to_false(monkeypatch):
    stub = Stub()
    monkeypatch.setattr(stub, "local_fallback_ready", lambda: (True, ""))

    async def boom(seconds, reason):
        raise OSError("no input device")

    stub._run_local_fallback = boom
    assert asyncio.run(stub.serve_locally_during_outage(60, "network")) is False


def test_disabled_policy_is_not_ready(monkeypatch):
    stub = Stub()
    monkeypatch.setattr(stub, "_local_policy", lambda: {"enabled": False})
    ok, why = stub.local_fallback_ready()
    assert ok is False and "disabled" in why


def test_cloud_restored_logs_once():
    stub = Stub()
    stub._local_outage_reason = "quota"
    stub.note_cloud_restored()
    stub.note_cloud_restored()
    assert len([m for m in stub.logs if "restored" in m]) == 1


# ── utterance handling ──────────────────────────────────────────────────────


def test_normal_utterance_starts_a_turn_and_is_noted():
    stub = Stub()
    utter(stub, "open notepad")
    assert stub.turns == ["open notepad"]
    assert stub.noted == ["open notepad"]


def test_gate_block_is_silent():
    stub = Stub(gate_action="BLOCK_SILENTLY")
    utter(stub, "uh")
    assert stub.turns == [] and stub.said == []


def test_gate_clarification_asks():
    stub = Stub(gate_action="ASK_CLARIFICATION")
    utter(stub, "the thing")
    assert stub.turns == [] and len(stub.said) == 1


def test_reconnect_order_ends_the_local_loop():
    stub = Stub()
    utter(stub, "reconnect to the cloud")
    assert stub._local_exit.is_set() and stub.turns == []


def test_standby_only_wakes_on_the_wake_word():
    stub = Stub()
    stub._standby_active = True
    utter(stub, "open notepad")
    assert stub.turns == [] and stub._standby_active
    utter(stub, "shazam")
    assert not stub._standby_active and stub.said == ["Yes, sir."]


def test_stop_word_does_not_start_a_turn():
    stub = Stub()
    utter(stub, "stop")
    assert stub.turns == []


def test_submit_text_refused_when_not_in_local_mode():
    assert Stub().local_submit_text("hello") is False
    assert Stub().local_speak("hello") is False


def test_tool_bridge_uses_the_gemini_gate():
    stub = Stub()
    seen = []

    async def _execute_tool(fc):
        seen.append((fc.name, dict(fc.args), fc.id))
        return SimpleNamespace(response={"success": True, "message": "Paused."})

    stub._execute_tool = _execute_tool
    out = asyncio.run(stub._local_execute_tool("spotify_control", {"action": "pause"}))
    assert out == {"success": True, "message": "Paused."}
    assert seen[0][:2] == ("spotify_control", {"action": "pause"})
    assert seen[0][2].startswith("local-")


# ── Ollama adapter ──────────────────────────────────────────────────────────


def test_tool_slimming_keeps_names_types_enums_required():
    spec = SimpleNamespace(
        name="audio_control",
        description="Controls audio. " + "Long detail. " * 50,
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "mute"], "description": "x " * 80},
                "description": {"type": "string"},  # a PARAMETER named description
            },
            "required": ["action"],
        },
    )
    tool = oc.to_ollama_tools([spec])[0]["function"]
    assert tool["name"] == "audio_control"
    assert tool["description"] == "Controls audio."
    assert tool["parameters"]["properties"]["action"]["enum"] == ["set", "mute"]
    assert tool["parameters"]["properties"]["description"] == {"type": "string"}
    assert tool["parameters"]["required"] == ["action"]
    assert len(tool["parameters"]["properties"]["action"]["description"]) <= 70


def test_stream_parses_ndjson_text_tools_and_usage(monkeypatch):
    lines = [
        {"message": {"content": "Hi "}},
        {"message": {"content": "sir."}},
        {
            "message": {
                "tool_calls": [{"function": {"name": "open_app", "arguments": '{"app": "x"}'}}]
            }
        },
        {"done": True, "prompt_eval_count": 12, "eval_count": 3},
    ]
    body = [(json.dumps(x) + "\n").encode() for x in lines]
    captured = {}

    class Resp(list):
        def close(self):
            pass

    def fake_open(self, payload, timeout_s):
        captured.update(payload)
        return Resp(body)

    monkeypatch.setattr(oc.OllamaChatProvider, "_open", fake_open)
    req = LlmRequest(alias="era.local", messages=(LlmMessage.text("user", "hi"),))
    resp = oc.OllamaChatProvider(model="m").generate(req)
    assert resp.text == "Hi sir."
    assert resp.tool_calls[0].name == "open_app" and resp.tool_calls[0].args == {"app": "x"}
    assert resp.usage.input_tokens == 12 and resp.usage.output_tokens == 3
    assert captured["think"] is False
    assert captured["options"]["num_ctx"] == oc.DEFAULT_NUM_CTX


def test_unreachable_ollama_is_a_transient_llm_error(monkeypatch):
    def refuse(self, payload, timeout_s):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(oc.OllamaChatProvider, "_open", refuse)
    events = list(oc.OllamaChatProvider(model="m").stream(LlmRequest(alias="era.local")))
    assert events[0].kind == "error" and events[0].error.kind == "transient"


# ── local STT / TTS helpers ─────────────────────────────────────────────────


def test_whisper_hallucinations_are_dropped():
    assert local_stt.is_hallucination("Thank you.")
    assert local_stt.is_hallucination("   ")
    assert local_stt.is_hallucination("...")
    assert not local_stt.is_hallucination("merci de fermer spotify")


def test_language_guess():
    assert local_tts.guess_lang("Bonjour monsieur, je suis là.") == "fr"
    assert local_tts.guess_lang("Done, sir.") == "en"


def test_piper_output_is_resampled_to_the_player_rate():
    one_second = np.zeros(22050, dtype=np.float32)
    pcm = local_tts.to_pcm16(one_second, 22050)
    assert len(pcm) == local_tts.RECEIVE_SAMPLE_RATE * 2  # int16 mono, 1 s


def test_missing_models_are_reported_not_raised(tmp_path):
    ok, why = local_stt.available(tmp_path)
    assert ok is False and "missing" in why
    ok, why = local_tts.available(tmp_path)
    assert ok is False and "missing" in why


# ── security review fixes (2026-09-23) ──────────────────────────────────────


def test_abort_order_trips_the_latch_and_starts_nothing():
    stub = Stub()
    utter(stub, "abort mission")
    assert stub.aborted == ["abort mission"] and stub.turns == []


def test_reboot_and_mic_off_orders_bypass_the_model():
    stub = Stub()
    utter(stub, "reboot Era One")
    assert stub.shutdowns == [{"relaunch": True, "_source": "voice"}]
    utter(stub, "stop listening")
    assert stub._mic_muted and stub.turns == []


def test_stop_also_drops_pending_confirmations():
    stub = Stub()
    utter(stub, "cancel")
    assert stub.cleared == 1 and stub.turns == []


def test_tool_bridge_refuses_while_abort_latched(monkeypatch):
    from core.panic_stop import panic

    stub = Stub()
    ran = []

    async def _execute_tool(fc):
        ran.append(fc.name)

    stub._execute_tool = _execute_tool
    monkeypatch.setattr(panic, "active", lambda *a, **k: True)
    out = asyncio.run(stub._local_execute_tool("open_app", {}))
    assert out["error_type"] == "BlockedAction" and ran == []


def _armed(stub, created_at):
    stub._pending_store = SimpleNamespace(
        peek=lambda cid="": SimpleNamespace(created_at=created_at),
        is_pending=lambda: True,
    )


def test_confirm_refused_without_a_user_yes_after_arming():
    from app.era_live.local_guards import user_approved

    stub = Stub()
    _armed(stub, created_at=100.0)
    stub._recent_user_texts = [(90.0, "yes"), (101.0, "clean up my documents")]
    assert user_approved(stub) is False
    stub._recent_user_texts.append((102.0, "yes, go ahead"))
    assert user_approved(stub) is True
    stub._recent_user_texts.append((102.5, "okay, set a timer for five minutes"))
    assert user_approved(stub) is False  # moved on; did not answer the question
    stub._recent_user_texts.append((103.0, "no, don't do it"))
    assert user_approved(stub) is False


def test_tool_bridge_blocks_model_confirmation(monkeypatch):
    stub = Stub()
    _armed(stub, created_at=100.0)
    stub._recent_user_texts = [(101.0, "read my inbox")]
    ran = []

    async def _execute_tool(fc):
        ran.append(fc.name)

    stub._execute_tool = _execute_tool
    out = asyncio.run(stub._local_execute_tool("confirm_pending_confirmation", {}))
    assert out["error_type"] == "BlockedAction" and ran == []


def _verifier(monkeypatch, accepted, enabled=True):
    import core.speaker_recognition as sr

    purposes = []

    def verify(pcm, purpose="command"):
        purposes.append(purpose)
        return SimpleNamespace(accepted=accepted, reason="test")

    monkeypatch.setattr(sr, "get_verifier", lambda: SimpleNamespace(enabled=enabled, verify=verify))
    return purposes


def test_voice_lock_refuses_a_stranger(monkeypatch):
    from app.era_live.local_guards import owner_voice

    _verifier(monkeypatch, accepted=False)
    assert owner_voice(Stub(), np.zeros(16000, dtype=np.float32)) is False


def test_voice_lock_off_lets_everyone_through(monkeypatch):
    from app.era_live.local_guards import owner_voice

    _verifier(monkeypatch, accepted=False, enabled=False)
    assert owner_voice(Stub(), np.zeros(16000, dtype=np.float32)) is True


def test_pending_confirmation_forces_full_verification(monkeypatch):
    from app.era_live.local_guards import owner_voice

    purposes = _verifier(monkeypatch, accepted=True)
    stub = Stub()
    owner_voice(stub, np.zeros(16000, dtype=np.float32))  # verified command
    owner_voice(stub, np.zeros(16000, dtype=np.float32))  # recent -> followup
    _armed(stub, created_at=0.0)
    owner_voice(stub, np.zeros(16000, dtype=np.float32))  # pending -> command
    assert purposes == ["command", "followup", "command"]


def test_voice_check_error_fails_closed(monkeypatch):
    import core.speaker_recognition as sr
    from app.era_live.local_guards import owner_voice

    def boom():
        raise RuntimeError("model missing")

    monkeypatch.setattr(sr, "get_verifier", boom)
    assert owner_voice(Stub(), np.zeros(16000, dtype=np.float32)) is False


def test_model_cannot_pass_its_own_confirmed_flag():
    stub = Stub()
    seen = []

    async def _execute_tool(fc):
        seen.append(dict(fc.args))
        return SimpleNamespace(response={"success": True})

    stub._execute_tool = _execute_tool
    asyncio.run(
        stub._local_execute_tool(
            "file_controller", {"action": "delete", "confirmed": True, "confirmation_id": "c9"}
        )
    )
    assert seen == [{"action": "delete"}]


def test_followup_window_comes_from_the_verifier_settings(monkeypatch):
    import time as _time

    import core.speaker_recognition as sr
    from app.era_live.local_guards import owner_voice

    purposes = []

    def verify(pcm, purpose="command"):
        purposes.append(purpose)
        return SimpleNamespace(accepted=True, reason="")

    monkeypatch.setattr(
        sr,
        "get_verifier",
        lambda: SimpleNamespace(enabled=True, verify=verify, settings={"followup_window_s": 8.0}),
    )
    stub = Stub()
    stub._local_owner_at = _time.monotonic() - 12.0  # outside 8 s, inside the old 20 s
    owner_voice(stub, np.zeros(16000, dtype=np.float32))
    assert purposes == ["command"]


# ── fast switch on "no internet" (2026-09-23: a 28 s Wi-Fi outage never
# reached local mode because the first version waited for 6 retries) ────────


def _offline(exc):
    from app.era_live.reconnect_mixin import ReconnectMixin

    stub = Stub()
    stub._flatten_exc = ReconnectMixin._flatten_exc
    return stub.failure_is_offline(exc)


def test_dns_failure_inside_a_taskgroup_is_offline():
    import socket

    group = ExceptionGroup(
        "unhandled errors in a TaskGroup", [socket.gaierror(11001, "getaddrinfo failed")]
    )
    assert _offline(group) is True


def test_aborted_connection_is_offline():
    assert (
        _offline(ConnectionAbortedError("[WinError 1236] The network connection was aborted"))
        is True
    )


def test_plain_socket_close_is_not_offline():
    class ConnectionClosedError(Exception):  # websockets' type is not a ConnectionError
        pass

    assert _offline(ConnectionClosedError("no close frame received or sent")) is False
    assert _offline(RuntimeError("1006 None. abnormal closure [internal]")) is False
