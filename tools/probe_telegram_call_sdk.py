"""Optional SDK description check. No native instance, login or call is created."""

from __future__ import annotations

import asyncio
import json


async def prove() -> dict[str, bool]:
    from pytgcalls.methods.utilities.stream_params import StreamParams
    from pytgcalls.types import Frame, RecordStream
    from pytgcalls.types.raw import AudioParameters

    from core.telegram_call_helper import external_streams

    outgoing, incoming = external_streams()
    capture = await StreamParams.get_stream_params(outgoing)
    playback = await StreamParams.get_record_params(incoming)
    frame = Frame(1, b"\x01\x00", Frame.Info())
    assert capture.microphone.sample_rate == 24000
    sdk_record = await StreamParams.get_record_params(
        RecordStream(audio=True, audio_parameters=AudioParameters(16000, 1))
    )
    assert playback.microphone.sample_rate == sdk_record.microphone.sample_rate == 16000
    assert capture.microphone.channel_count == playback.microphone.channel_count == 1
    assert capture.speaker is None and playback.speaker is None
    assert sdk_record.speaker is None
    assert capture.camera is None and capture.screen is None
    assert playback.camera is None and playback.screen is None
    assert frame.frame == b"\x01\x00" and not hasattr(frame, "data")
    return {
        "external_sources_verified": True,
        "frame_field_verified": True,
        "network_opened": False,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(prove())))
