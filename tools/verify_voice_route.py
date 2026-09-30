"""Prove where Era's voice actually comes out - by measuring, not by asking.

Every layer in this stack can claim success and still be wrong: the pin is
stored, ``resolve_output_index()`` returns an index, the stream opens without
error, and the sound still arrives at the laptop speakers. The reasons are real
and invisible from inside Python:

* PortAudio freezes its device list at ``Pa_Initialize``, so a device connected
  after Era started resolves to None and the stream silently opens on the
  default output (see core/voice_output.refresh_between_sessions);
* a per-app device preference rewrites what "the default output" even means
  inside ``python.exe`` (see core/audio_policy.py);
* another application can be playing on the other endpoint, which sounds exactly
  like Era being misrouted.

So this asks the hardware. It plays a short quiet tone at a known amplitude on a
chosen PortAudio device and reads ``IAudioMeterInformation`` on EVERY render
endpoint. The endpoint whose peak rises by the tone's amplitude is where the
audio physically went. A silence baseline is taken first, because an endpoint
that is already busy (Spotify, a game) reads near full scale and would otherwise
look like it received the tone.

    python -m tools.verify_voice_route            # the pinned route vs. default
    python -m tools.verify_voice_route 4 5        # specific PortAudio indices

Read-only apart from the tone itself: it changes no pin, no default device and
no volume.
"""

from __future__ import annotations

import sys
import time
from ctypes import HRESULT, POINTER, c_float, c_uint32, cast

TONE_HZ = 440.0
TONE_AMPLITUDE = 0.05  # quiet on purpose - audible, not startling
RATE = 24000
RISE_THRESHOLD = 0.01  # a peak rise this much above silence counts as received
BUSY_BASELINE = 0.02  # above this at rest, another app owns the endpoint


def _meter_interface():
    from comtypes import COMMETHOD, GUID, IUnknown

    class IAudioMeterInformation(IUnknown):
        _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
        _methods_ = (
            COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "pfPeak")),
            COMMETHOD([], HRESULT, "GetMeteringChannelCount", (["out"], POINTER(c_uint32), "pn")),
        )

    return IAudioMeterInformation


def endpoint_meters() -> dict:
    """{endpoint name: peak meter} for every active render endpoint."""
    from comtypes import CLSCTX_ALL

    from core.audio_endpoints import OUTPUT, enumerate_endpoints

    iface_cls = _meter_interface()
    meters = {}
    for ep in enumerate_endpoints(OUTPUT):
        raw = getattr(ep.dev, "_dev", None)
        if raw is None:
            continue
        try:
            ptr = raw.Activate(iface_cls._iid_, CLSCTX_ALL, None)
            meters[ep.name] = cast(ptr, POINTER(iface_cls))
        except Exception as e:
            print(f"  ! no peak meter on {ep.name}: {type(e).__name__}: {e}")
    return meters


def _watch(meters: dict, seconds: float, feed=None) -> dict:
    """Max peak per endpoint over `seconds`, optionally while `feed()` plays."""
    seen = dict.fromkeys(meters, 0.0)
    deadline = time.monotonic() + seconds
    pump = feed() if feed is not None else None
    while time.monotonic() < deadline:
        if pump is not None:
            try:
                next(pump)
            except StopIteration:
                pump = None
        for name, m in meters.items():
            try:
                seen[name] = max(seen[name], float(m.GetPeakValue()))
            except Exception:
                pass
        time.sleep(0.005)
    return seen


def measure(device, *, seconds: float = 1.2) -> tuple[dict, str | None]:
    """Play the tone on `device`; return ({endpoint: (rise, baseline)}, error)."""
    import numpy as np
    import sounddevice as sd

    meters = endpoint_meters()
    if not meters:
        return {}, "no render endpoint exposed a peak meter"
    baseline = _watch(meters, 0.6)

    t = np.arange(int(RATE * seconds)) / RATE
    tone = (np.sin(2 * np.pi * TONE_HZ * t) * TONE_AMPLITUDE * 32767).astype(np.int16)
    try:
        stream = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", device=device)
    except Exception as e:
        return {}, f"could not open device {device!r}: {type(e).__name__}: {e}"

    def feed():
        chunk = 1200
        with stream:
            for i in range(0, len(tone), chunk):
                stream.write(tone[i : i + chunk].tobytes())
                yield

    loud = _watch(meters, seconds + 0.3, feed)
    return {n: (loud[n] - baseline.get(n, 0.0), baseline.get(n, 0.0)) for n in loud}, None


def _describe(device) -> str:
    import sounddevice as sd

    if device is None:
        return "(PortAudio default output)"
    try:
        return repr(str(sd.query_devices(device)["name"]))
    except Exception:
        return "(unknown device)"


def report(device, label: str) -> bool:
    """Print where the tone landed. True when exactly one endpoint received it."""
    print(f"--- {label}: device={device} {_describe(device)}")
    rows, err = measure(device)
    if err:
        print(f"    {err}\n")
        return False

    # An endpoint that was ALREADY playing something (another app) reads near
    # full scale, so its rise carries no information - judge only the quiet
    # ones, and among those prefer the rise closest to the amplitude we sent.
    quiet = {n: rise for n, (rise, base) in rows.items() if base <= BUSY_BASELINE}
    busy = {n: base for n, (_, base) in rows.items() if base > BUSY_BASELINE}
    got = sorted(
        (n for n, rise in quiet.items() if rise > RISE_THRESHOLD),
        key=lambda n: abs(quiet[n] - TONE_AMPLITUDE),
    )

    for name, (rise, _base) in sorted(rows.items(), key=lambda kv: -kv[1][0]):
        if name in busy:
            note = f"  (BUSY at {busy[name]:.2f} - another app owns it; meter unusable)"
        elif got and name == got[0]:
            note = "  <== THE TONE PHYSICALLY LANDED HERE"
        else:
            note = ""
        print(f"    {name:<42} rise={rise:+.4f}{note}")

    if not got:
        print("    !! no quiet endpoint rose - the tone reached none this meter can see")
    elif len(got) > 1:
        print(f"    !! {len(got)} quiet endpoints rose; closest to the tone is {got[0]!r}")
    print()
    return len(got) == 1


def main(argv: list[str]) -> int:
    import sounddevice as sd

    from core import voice_output as vo

    print(f"pin                     : {(vo.get_pin() or {}).get('name') or '(none)'}")
    print(f"resolve_output_index()  : {vo.resolve_output_index()}")
    print(f"PortAudio default out   : {sd.default.device[1]}")
    print(f"last opened by playback : {vo.active_route() or '(playback has not run here)'}")
    print()
    if argv:
        targets = [(f"explicit index {a}", int(a)) for a in argv]
    else:
        targets = [
            ("the pinned route", vo.resolve_output_index()),
            ("the default fallback", None),
        ]
    ok = all(report(dev, label) for label, dev in targets)
    print("RESULT:", "each target reached exactly one endpoint" if ok else "see the warnings above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
