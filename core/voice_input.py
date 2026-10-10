"""Pin WHERE ERA LISTENS, the counterpart to core/voice_output.py.

THE BUG THIS EXISTS FOR
-----------------------
Era pinned where she speaks but not where she hears. `sd.InputStream` was
opened with no `device=`, so she followed whatever Windows called the default
microphone. Connect a Bluetooth headset -- the Star Glass glasses -- and
Windows moves the default INPUT to it as well as the output, in Hands-Free
Profile: mono, 8 kHz, codec-mangled.

That is survivable for transcription and fatal for the owner voice gate. A
TitaNet embedding computed from 8 kHz HFP audio cannot match one enrolled on
the laptop's 48 kHz mic array; on 2026-10-10 the owner's score fell from a
1.0 enrollment pass rate to 0.16-0.47 against a 0.488 threshold. The gate is
fail-CLOSED, so every utterance became silence, Gemini received nothing and
answered "<no speech detected>" -- with no indication anywhere that the
microphone had moved. Era went deaf to her owner and could not say why.

So the microphone becomes a deliberate choice, like the voice already is.

FAIL OPEN, ALWAYS
-----------------
Every resolution failure returns None, which means "use the system default"
-- exactly today's behaviour. A pin naming a device that is unplugged, a
renamed endpoint, a typo, a PortAudio error: none of them may ever leave Era
unable to hear. Deafness is a worse failure than listening on the wrong mic,
which is the same trade core/attention.py makes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VOICE_CONFIG_PATH = BASE_DIR / "config" / "voice.json"

# config/voice.json key. A plain substring of the device name rather than an
# index: PortAudio indices are reassigned on every re-enumeration, so a stored
# index silently points at a different microphone after a reconnect.
PREF_KEY = "voice_input"

# Hands-Free Profile endpoints. Windows exposes a Bluetooth headset's mic this
# way at 8-16 kHz, and it is almost never what the owner wants Era to hear on
# when a real microphone exists -- see the module docstring.
_HFP_TOKENS = ("hands-free", "hands free", "bthhfenum", "headset")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def get_pin() -> str:
    """The microphone the owner asked for, as written. "" when unset."""
    try:
        raw = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8")).get(PREF_KEY)
    except Exception:
        return ""
    return str(raw or "").strip()


def set_pin(name: str) -> str:
    """Store the pin, keeping every other key in config/voice.json."""
    wanted = str(name or "").strip()
    if not wanted:
        raise ValueError("a microphone name is required")
    _store(wanted)
    return wanted


def clear_pin() -> None:
    """Go back to following the system default microphone."""
    _store(None)


def _store(value: str | None) -> None:
    try:
        data = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    if value is None:
        data.pop(PREF_KEY, None)
    else:
        data[PREF_KEY] = value
    VOICE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOICE_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _candidates(sd) -> list[tuple[int, dict]]:
    try:
        return [(i, d) for i, d in enumerate(sd.query_devices()) if d.get("max_input_channels", 0)]
    except Exception:
        return []


def _score(device: dict, wanted: str) -> int:
    """How well this input device answers to `wanted`. 0 means no match.

    Exact beats prefix beats substring, because MME truncates device names to
    31 characters and a short query would otherwise match several endpoints
    (the lesson from core/voice_output.py's endpoint matching).
    """
    name = _norm(device.get("name", ""))
    if not name or not wanted:
        return 0
    if name == wanted:
        return 100
    if name.startswith(wanted):
        return 80
    if wanted in name:
        return 60
    return 0


def is_hfp(device_name: str) -> bool:
    """A Bluetooth Hands-Free mic -- the 8 kHz endpoint that broke the gate."""
    name = _norm(device_name)
    return any(tok in name for tok in _HFP_TOKENS)


def resolve_input_index(sd=None) -> int | None:
    """The PortAudio index Era should open, or None for the system default.

    None is the safe answer and the default answer: with no pin, or a pin that
    resolves to nothing, Era behaves exactly as she did before this module.
    """
    wanted = _norm(get_pin())
    if not wanted:
        return None
    if sd is None:
        try:
            import sounddevice as sd  # noqa: PLC0415 — optional at import time
        except Exception:
            return None
    best_index, best_score = None, 0
    for index, device in _candidates(sd):
        score = _score(device, wanted)
        if score <= best_score:
            continue
        best_index, best_score = index, score
    if best_index is None:
        print(f"[MIC] Pinned microphone {get_pin()!r} is not connected; using the system default.")
    return best_index


def status(sd=None) -> str:
    """One line for the HUD and the logs."""
    pin = get_pin()
    if not pin:
        return "Microphone: system default (not pinned)."
    index = resolve_input_index(sd)
    if index is None:
        return f"Microphone: {pin!r} pinned but not connected; using the system default."
    return f"Microphone: {pin!r} (device {index})."
