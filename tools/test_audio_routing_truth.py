"""Regression evidence for requested versus observed voice routes.

No mixer, Bluetooth connection, user preference, or real stream is changed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import audio_endpoints as ae
from core import voice_output as vo


@pytest.fixture
def routing(monkeypatch):
    glass = ae.Endpoint("glass", "Star glass (CY03-CB36)", ae.OUTPUT, "headphones")
    pc = ae.Endpoint("pc", "PC speakers (Realtek)", ae.OUTPUT, "speakers")
    mic = ae.Endpoint("mic", "Microphone Array", ae.INPUT, "microphone")
    state = {"pin": None, "endpoints": [glass, pc, mic]}

    def save(pin):
        state["pin"] = pin
        return True

    monkeypatch.setattr(vo, "_active", None)
    monkeypatch.setattr(vo, "_generation", 0)
    monkeypatch.setattr(vo, "get_pin", lambda: state["pin"])
    monkeypatch.setattr(vo, "_store", save)
    monkeypatch.setattr(vo, "_voice_lock_active", lambda: False)
    monkeypatch.setattr(vo, "sd_index_for_name", lambda name: {glass.name: 4, pc.name: 5}.get(name))
    monkeypatch.setattr(
        ae,
        "enumerate_endpoints",
        lambda flow=None: [e for e in state["endpoints"] if flow in (None, e.flow)],
    )
    return SimpleNamespace(glass=glass, pc=pc, mic=mic, state=state)


def test_saved_pin_does_not_claim_playback_before_a_stream_opens(routing):
    result = vo.set_pin("glasses")
    assert result.ok  # the preference was accepted
    assert "now plays" not in result.message
    assert "from this sentence" not in result.message
    assert result.data.get("routing_verified") is False


def test_status_does_not_verify_a_route_that_has_never_opened(routing):
    vo.set_pin("glasses")
    result = vo.status()
    assert result.data["routed_as_pinned"] is None
    assert "My voice plays on" not in result.message


def test_connected_pin_does_not_hide_a_different_nonfallback_stream(routing):
    vo.set_pin("glasses")
    vo.note_active_route(5, fallback=False, name=routing.pc.name)
    result = vo.status()
    assert result.data["routed_as_pinned"] is False
    assert "My voice plays on Star" not in result.message
    assert routing.pc.name in result.message


def test_same_index_with_wrong_device_name_is_not_verified(routing):
    vo.set_pin("glasses")
    vo.note_active_route(4, fallback=False, name=routing.pc.name)
    assert vo.status().data["routed_as_pinned"] is False


def test_prompt_does_not_turn_a_saved_pin_into_an_observed_fact(routing):
    vo.set_pin("glasses")
    prompt = vo.summary_line().lower()
    assert "your spoken voice plays on" not in prompt
    assert "unverified" in prompt or "not yet verified" in prompt


def test_stale_acknowledgment_cannot_verify_new_request(routing):
    vo.set_pin("glasses")
    previous = vo.generation()
    vo.set_pin("glasses")
    vo.note_active_route(4, name=routing.glass.name, route_generation=previous)
    assert vo.status().data["routed_as_pinned"] is None
    vo.note_active_route(4, name=routing.glass.name, route_generation=vo.generation())
    assert vo.status().data["routed_as_pinned"] is True
    vo.clear_active_route()
    assert vo.status().data["routed_as_pinned"] is None


def test_hud_separates_requested_and_observed_output(routing):
    from app.hud.status import audio_output

    vo.set_pin("glasses")
    pending = audio_output()
    assert pending["requested_device"] == routing.glass.name
    assert pending["device"] is None
    assert pending["routing_verified"] is None
    vo.note_active_route(5, name=routing.pc.name, fallback=True)
    observed = audio_output()
    assert observed["device"] == routing.pc.name
    assert observed["routing_verified"] is False
    assert observed["fallback"] is True
    vo.clear_active_route()
    assert audio_output()["device"] is None


def test_same_device_request_reacknowledges_without_reopening(routing):
    from app.era_live.audio_output import ResilientOutputStream

    opens = []

    def opener(device):
        opens.append(device)
        return SimpleNamespace(device=4, start=lambda: None, stop=lambda: None, close=lambda: None)

    vo.set_pin("glasses")
    output = ResilientOutputStream(
        opener,
        4,
        on_open=lambda device, fallback: vo.note_active_route(
            device, fallback=fallback, name=routing.glass.name
        ),
        on_close=vo.clear_active_route,
    )
    assert output.open()
    vo.set_pin("glasses")
    assert vo.route_matches_pin(vo.get_pin()) is None
    output.set_preferred_device(4)
    assert vo.route_matches_pin(vo.get_pin()) is True
    assert opens == [4]
    output.close()
    assert vo.active_route() is None


def test_full_session_reenumeration_has_a_budget_across_reconnects(routing, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(vo.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(vo, "_reenum_generation", -1)
    monkeypatch.setattr(vo, "_reenum_attempts", 0)
    monkeypatch.setattr(vo, "_reenum_retry_at", 0.0)
    vo.set_pin("glasses")
    assert vo.claim_reenumeration_retry()
    assert not vo.claim_reenumeration_retry()
    clock[0] += 60
    assert vo.claim_reenumeration_retry()
    clock[0] += 60
    assert not vo.claim_reenumeration_retry()
    clock[0] += 60
    assert vo.claim_reenumeration_retry()
    clock[0] += 3600
    assert not vo.claim_reenumeration_retry()
    vo.set_pin("glasses")
    assert vo.claim_reenumeration_retry()


def test_output_resolution_cannot_select_an_input_by_exact_id(routing):
    endpoint, error = ae.resolve(routing.mic.id, ae.OUTPUT)
    assert endpoint is None
    assert error is not None and not error.ok


def test_same_named_replacement_is_ambiguous_when_original_id_is_gone(routing):
    vo.set_pin("glasses")
    routing.state["endpoints"] = [
        ae.Endpoint("other-a", routing.glass.name, ae.OUTPUT, "headphones"),
        ae.Endpoint("other-b", routing.glass.name, ae.OUTPUT, "headphones"),
    ]
    assert vo._live_endpoint(routing.state["pin"]) is None


def test_failed_reclaim_keeps_a_working_fallback_open_and_backs_off():
    from app.era_live.audio_output import ResilientOutputStream

    now = [100.0]
    attempts = []
    streams = []

    class Stream:
        def __init__(self, device):
            self.device = device
            self.closed = False
            self.played = []

        def start(self):
            attempts.append(self.device)
            if self.device == 4:
                raise OSError("connected endpoint cannot open")

        def write(self, chunk):
            assert not self.closed
            self.played.append(chunk)

        def stop(self):
            pass

        def close(self):
            self.closed = True

    def opener(device):
        stream = Stream(device)
        streams.append(stream)
        return stream

    output = ResilientOutputStream(opener, preferred_device=4, clock=lambda: now[0])
    assert output.open()
    fallback = streams[-1]
    output.reclaim_preferred()
    assert not fallback.closed
    count = len(attempts)
    for _ in range(20):
        output.reclaim_preferred()
    assert len(attempts) == count
    # Mono int16 needs complete two-byte samples. The prior 15-byte ASCII
    # fixture tested invalid-frame rejection instead of fallback preservation.
    phrase_pcm = b"\x01\x00" * 64
    assert asyncio.run(output.write(phrase_pcm))
    assert fallback.played == [phrase_pcm]
    now[0] += 65.0
    output.reclaim_preferred()
    assert len(attempts) > count
    assert not fallback.closed
    output.close()


@pytest.mark.parametrize("capture_idle", [False, True])
def test_reenumeration_closes_retained_capture_before_global_refresh(
    routing, monkeypatch, capture_idle
):
    vo.set_pin("glasses")
    monkeypatch.setattr(vo, "_lookup", lambda name: None)
    events = []
    monkeypatch.setattr(vo, "refresh_devices", lambda: events.append("refresh") or True)

    def close_capture():
        events.append("close")
        return capture_idle

    assert vo.refresh_between_sessions(before_refresh=close_capture) is capture_idle
    assert events == (["close", "refresh"] if capture_idle else ["close"])


def test_healthy_device_does_not_close_retained_capture_on_renewal(routing, monkeypatch):
    vo.set_pin("glasses")
    monkeypatch.setattr(vo, "_lookup", lambda name: 4)

    def forbidden():
        raise AssertionError("healthy stream must remain open")

    monkeypatch.setattr(vo, "refresh_devices", forbidden)
    assert not vo.refresh_between_sessions(before_refresh=forbidden)
