"""Offline ownership, PCM buffering, native receipts and shutdown regressions."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.telegram_call_audio import CallAudio, CallPCM
from tools.probe_telegram_call_native import run_probe


def connected(capacity=25):
    call = CallAudio(42, capacity)
    generation = call.begin(42)
    assert call.connected(generation, 16000, 24000)
    return call, generation


@pytest.mark.parametrize(
    "owner,capacity", [(0, 25), (True, 25), (42, 0), (42, 101), (42.0, 25), (42, 2.5)]
)
def test_owner_and_queue_bounds_are_required(owner, capacity):
    with pytest.raises(ValueError):
        CallAudio(owner, capacity)


def test_unknown_peer_and_duplicate_call_are_rejected():
    call = CallAudio(42)
    with pytest.raises(PermissionError):
        call.begin(99)
    generation = call.begin(42)
    with pytest.raises(RuntimeError):
        call.begin(42)
    assert not call.connected("stale", 16000, 24000)
    assert call.connected(generation, 16000, 24000)


def test_negotiated_format_mismatch_fails_before_audio():
    call = CallAudio(42)
    generation = call.begin(42)
    assert not call.connected(generation, 48000, 24000)
    assert call.state == "failed" and call.failure == "pcm_format_mismatch"


def test_wrong_generation_and_peer_cannot_feed_remote_audio():
    call, generation = connected()
    assert not call.receive(99, generation, 0, bytes(640))
    assert not call.receive(42, "old", 0, bytes(640))
    assert call.take_input(generation) is None
    assert not call.interrupt("old")
    assert not call.end("old")


def test_input_bytes_are_preserved_and_duplicates_dropped():
    call, generation = connected()
    pcm = b"\x01\x02" * 320
    assert call.receive(42, generation, 0, pcm)
    assert not call.receive(42, generation, 0, pcm)
    packet = call.take_input(generation)
    assert packet and packet.data == pcm and packet.sample_rate == 16000
    assert call.take_input(generation) is None
    assert call.state == "connected"  # native output still unobserved


@pytest.mark.parametrize("pcm", [b"", b"x", bytes(3202)])
def test_malformed_input_fails_without_leaving_backlog(pcm):
    call, generation = connected()
    assert not call.receive(42, generation, 0, pcm)
    assert call.failure == "invalid_input_pcm"
    assert call.take_input(generation) is None


def test_missing_input_sequence_is_reported_and_clears_queue():
    call, generation = connected()
    assert call.receive(42, generation, 0, bytes(640))
    assert not call.receive(42, generation, 2, bytes(640))
    assert call.failure == "input_sequence_gap"
    assert call.take_input(generation) is None


def test_boolean_sequence_is_not_treated_as_an_integer_packet_id():
    call, generation = connected()
    assert not call.receive(42, generation, True, bytes(640))
    assert call.failure == "invalid_input_sequence"


def test_input_backpressure_has_bounded_failure():
    call, generation = connected(capacity=1)
    assert call.receive(42, generation, 0, bytes(640))
    assert not call.receive(42, generation, 1, bytes(640))
    assert call.failure == "input_backpressure"


def test_reply_fragmentation_matches_whole_pcm_and_preserves_last_sample():
    source = b"\x03\x04" * 641
    for fragment_size in (2, 46, 960, 1282):
        call, generation = connected()
        reply = call.begin_reply(generation)
        for offset in range(0, len(source), fragment_size):
            assert call.enqueue_reply(generation, reply, source[offset : offset + fragment_size])
        assert call.enqueue_reply(generation, reply, b"", final=True)
        packets = []
        while packet := call.take_output(generation):
            packets.append(packet)
        assert b"".join(packet.data for packet in packets) == source.ljust(1920, b"\0")
        assert all(packet.sample_rate == 24000 and len(packet.data) == 960 for packet in packets)
        assert not call.enqueue_reply(generation, reply, bytes(960))


def test_native_receipt_is_once_and_not_a_remote_playback_claim():
    call, generation = connected()
    reply = call.begin_reply(generation)
    assert call.receive(42, generation, 0, bytes(640))
    assert call.enqueue_reply(generation, reply, bytes(960))
    packet = call.take_output(generation)
    assert packet
    forged = CallPCM(
        packet.generation,
        packet.reply,
        packet.sequence,
        packet.sample_rate,
        packet.data,
    )
    assert not call.submitted(forged)
    assert call.submitted(packet) and not call.submitted(packet)
    assert call.state == "active"
    assert call.snapshot()["remote_playback_verified"] is False
    assert "data" not in call.snapshot()


def test_interruption_rejects_late_tts_and_receipts():
    call, generation = connected()
    reply = call.begin_reply(generation)
    assert call.enqueue_reply(generation, reply, bytes(960))
    packet = call.take_output(generation)
    assert packet and call.interrupt(generation)
    assert not call.submitted(packet)
    assert not call.enqueue_reply(generation, reply, bytes(960))
    assert call.take_output(generation) is None


def test_new_reply_invalidates_old_output():
    call, generation = connected()
    old = call.begin_reply(generation)
    assert call.enqueue_reply(generation, old, bytes(960))
    new = call.begin_reply(generation)
    assert not call.enqueue_reply(generation, old, bytes(960))
    assert call.enqueue_reply(generation, new, bytes(960))


@pytest.mark.parametrize(
    "pcm,reason", [(b"x", "invalid_output_pcm"), (bytes(962), "output_backpressure")]
)
def test_invalid_or_oversized_output_fails_boundedly(pcm, reason):
    call, generation = connected(capacity=1)
    reply = call.begin_reply(generation)
    assert not call.enqueue_reply(generation, reply, pcm)
    assert call.failure == reason


def test_unacknowledged_packets_count_toward_backpressure_limit():
    call, generation = connected(capacity=1)
    reply = call.begin_reply(generation)
    assert call.enqueue_reply(generation, reply, bytes(960))
    assert call.take_output(generation)
    assert not call.enqueue_reply(generation, reply, bytes(960))
    assert call.failure == "output_backpressure"


def test_end_is_idempotent_and_old_generation_cannot_affect_successor():
    call, generation = connected()
    reply = call.begin_reply(generation)
    assert call.enqueue_reply(generation, reply, bytes(960))
    assert call.end(generation) and not call.end(generation)
    assert call.take_output(generation) is None
    with pytest.raises(RuntimeError):
        call.begin_reply(generation)
    successor = call.begin(42)
    assert call.connected(successor, 16000, 24000)
    assert not call.end(generation)
    assert not call.receive(42, generation, 0, bytes(640))
    assert call.receive(42, successor, 0, bytes(640))


@pytest.mark.parametrize("timeout", [False, True])
def test_supervisor_distinguishes_native_success_from_process_cleanup(monkeypatch, timeout):
    result = {
        "sources_configured": True,
        "frame_submitted": True,
        "connection_stopped": True,
    }
    output = json.dumps(result).encode()
    runner = (
        Mock(side_effect=subprocess.TimeoutExpired("fixture", 8, output=output))
        if timeout
        else Mock(return_value=subprocess.CompletedProcess([], 0, stdout=output))
    )
    monkeypatch.setattr(subprocess, "run", runner)
    observed = run_probe(Path("fixture.exe"))
    assert observed["frame_submitted"]
    assert observed["clean_process_exit"] is not timeout
    assert observed["supervisor_reaped_child"] is timeout
    assert observed["shutdown_complete"]


def test_native_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, stdout=b"failed")),
    )
    result = run_probe(Path("fixture.exe"))
    assert not result["sources_configured"] and not result["clean_process_exit"]
    assert not result["supervisor_reaped_child"]
    assert not result["shutdown_complete"]
