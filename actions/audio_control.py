# actions/audio_control.py
"""audio_control - list / switch audio devices and control any ONE device.

Baseline Routing Rule (enforced HERE, in code, not left to the model):
a volume/mute command with NO device named - or a device that means "the
system" (system / default / master / computer ...) - goes to the global SYSTEM
mixer, i.e. the same computer_settings logic 'volume up' has always used. Only
an explicit device name redirects the command to that one endpoint.
"""

from __future__ import annotations

from core import audio_endpoints as ae

# Friendly spellings the model (or a scheduled task) might use. The declared
# vocabulary is small; tolerance here costs nothing.
_ALIASES = {
    "list": "list_devices",
    "devices": "list_devices",
    "list_audio_devices": "list_devices",
    "list_devices": "list_devices",
    "status": "status",
    "device_status": "status",
    "get_volume": "status",
    "volume_status": "status",
    "volume": "status",
    "set_default": "set_default",
    "set_default_device": "set_default",
    "switch": "set_default",
    "switch_device": "set_default",
    "use": "set_default",
    "volume_set": "set",
    "set_volume": "set",
    "set": "set",
    "app_preference": "app_preference",
    "app_default": "app_preference",
    "app_override": "app_preference",
    "clear_app_preference": "clear_app_preference",
    "clear_app_default": "clear_app_preference",
    "clear_app_override": "clear_app_preference",
    "reset_app_preference": "clear_app_preference",
    # Where ERA'S OWN VOICE plays. A separate namespace from set_default on
    # purpose: set_default moves MEDIA, these move only her speech.
    "set_voice_output": "set_voice_output",
    "voice_output_set": "set_voice_output",
    "set_voice_device": "set_voice_output",
    "pin_voice": "set_voice_output",
    "speak_on": "set_voice_output",
    "clear_voice_output": "clear_voice_output",
    "reset_voice_output": "clear_voice_output",
    "unpin_voice": "clear_voice_output",
    "voice_output_auto": "clear_voice_output",
    "voice_output": "voice_output",
    "voice_output_status": "voice_output",
    "voice_device": "voice_output",
    "battery": "battery",
    "battery_status": "battery",
    "battery_level": "battery",
    "device_battery": "battery",
    "volume_up": "up",
    "up": "up",
    "louder": "up",
    "volume_down": "down",
    "down": "down",
    "quieter": "down",
    "mute": "mute",
    "unmute": "unmute",
    "toggle_mute": "toggle_mute",
}


def _norm_action(raw: str) -> str:
    a = str(raw or "").lower().strip().replace("-", "_").replace(" ", "_")
    for prefix in ("device_", "audio_"):
        if a.startswith(prefix):
            a = a[len(prefix) :]
    return _ALIASES.get(a, a)


# Volume/mute/status actions the glasses shortcut is allowed to redirect. A
# glass-scoped set_default or set_voice_output would be nonsense, so only these.
_GLASS_SCOPABLE = frozenset({"status", "set", "up", "down", "mute", "unmute", "toggle_mute"})
# Tokens in an ACTION name that scope it to the glasses. The model number is
# included so 'cy03cb36_volume_up' works, matching the resolver's glasses aliases.
_GLASS_ACTION_TOKENS = frozenset({"glass", "glasses", "star", "cy03", "cb36", "cy03cb36"})


def _strip_glass_scope(raw: str) -> tuple[bool, str]:
    """Split a glasses-scoped action name into (scoped?, base action).

    A dedicated, code-enforced path so 'glass_volume_up', 'glasses_set_volume',
    'glass_mute' etc. drive ONLY the Star Glasses, WITHOUT depending on the model
    also passing device='glasses'. The glass token is removed and whatever remains
    is the ordinary action; a bare 'glasses' means status.
    """
    a = str(raw or "").lower().strip().replace("-", "_").replace(" ", "_")
    words = [w for w in a.split("_") if w]
    kept = [w for w in words if w not in _GLASS_ACTION_TOKENS]
    scoped = len(kept) != len(words)
    return scoped, "_".join(kept) or "status"


def _is_system(device: str) -> bool:
    d = (device or "").strip().lower()
    return not d or d in ae.SYSTEM_WORDS


# "Speak on the computer" and "follow the system default" sound alike and mean
# opposite things. The first names the laptop's own speakers - a DEVICE, which
# must be pinned, or Era would bounce straight back to the glasses. The second
# asks for the automatic route, which means dropping the pin. Guessing between
# them is what would make "switch your voice to the computer" do nothing.
_VOICE_AUTO_WORDS = {"system", "default", "auto", "automatic", "normal", "whatever"}
_VOICE_LAPTOP_WORDS = {"computer", "pc", "laptop", "machine", "speakers", "speaker"}


def _voice_target(device: str) -> tuple[str, str]:
    """('ask'|'clear'|'pin', resolution query) for a set_voice_output request."""
    d = (device or "").strip().lower()
    if not d:
        return "ask", ""
    if d in _VOICE_AUTO_WORDS:
        return "clear", ""
    if d in _VOICE_LAPTOP_WORDS:
        # Resolved by FORM FACTOR, not by name, so it works on a French install
        # ("Haut-parleurs") and survives the user renaming the endpoint.
        return "pin", "speakers"
    return "pin", device


def _battery(device: str) -> str:
    """Battery of a named audio device, or of every device that reports one."""
    from core import audio_battery as ab

    if device and not _is_system(device):
        ep, err = ae.resolve(device, ae.OUTPUT)
        if err:
            return err.message
        row = ab.battery_for(ep.name)
        if row is None:
            return f"{ep.name} doesn't report a battery level, sir."
        return f"{ep.name} is at {row['percent']}% battery, sir."

    rows = ab.battery_devices()
    if not rows:
        return "No connected audio device is reporting a battery level, sir."
    return (
        "Battery: "
        + "; ".join(f"{r['name'] or 'unnamed device'} {r['percent']}%" for r in rows)
        + ", sir."
    )


def _step(params: dict) -> int:
    for key in ("step", "amount", "value"):
        raw = params.get(key)
        if raw not in (None, ""):
            try:
                return max(1, min(100, abs(int(float(str(raw).strip().rstrip("%"))))))
            except ValueError:
                continue
    return 10


def _system(action: str, params: dict) -> str:
    """The unchanged system-mixer path (computer_settings)."""
    from actions import computer_settings as cs

    if action == "set":
        v = cs._parse_volume_value(params.get("value"))
        if v is None:
            return "Tell me a level from 0 to 100, sir."
        return cs.volume_set(v, device="system")
    if action == "up":
        return cs.volume_up(_step(params))
    if action == "down":
        return cs.volume_down(_step(params))
    if action == "mute":
        return cs.volume_mute("mute")
    if action == "unmute":
        return cs.volume_mute("unmute")
    if action == "toggle_mute":
        return cs.volume_mute("toggle")
    return cs.get_volume()


def audio_control(
    parameters: dict | None = None, response=None, player=None, session_memory=None
) -> str:
    params = parameters or {}
    # Glasses-scoped actions ('glass_volume_up', 'glasses_set_volume', 'glass_mute')
    # drive the Star Glasses deterministically, in code, so the routing never
    # depends on the model remembering to attach device='glasses'.
    glass_scoped, base_action = _strip_glass_scope(params.get("action", ""))
    action = _norm_action(base_action)
    device = str(params.get("device") or params.get("target") or "").strip()
    if glass_scoped and action in _GLASS_SCOPABLE:
        device = "glasses"
    flow = str(params.get("flow") or "").lower().strip() or None
    if flow in ("mic", "microphone", "recording", "capture"):
        flow = ae.INPUT
    elif flow in ("speaker", "speakers", "playback"):
        flow = ae.OUTPUT

    try:
        if action == "list_devices":
            return ae.list_devices(flow).message

        if action == "set_default":
            if _is_system(device):
                return "Which device should become the default, sir?"
            return ae.set_default_device(device, flow, str(params.get("role") or "default")).message

        # Windows' own per-app device preference: what pins Era's audio to one
        # endpoint no matter what the system default is.
        if action == "app_preference":
            return ae.app_preference_status(flow).message
        if action == "clear_app_preference":
            return ae.clear_app_preference(flow).message

        # ── Era's OWN voice output ──────────────────────────────────────────
        # Deliberately NOT set_default: pinning her voice binds her playback
        # stream to one device and leaves the Windows default — the one Spotify,
        # YouTube and every other app follow — exactly where the user put it.
        if action == "set_voice_output":
            from core import voice_output as vo

            intent, query = _voice_target(device)
            if intent == "ask":
                return "Which device should I speak on, sir?"
            if intent == "clear":
                return vo.clear_pin().message
            return vo.set_pin(query).message

        if action == "clear_voice_output":
            from core import voice_output as vo

            return vo.clear_pin().message

        if action == "voice_output":
            from core import voice_output as vo

            return vo.status().message

        if action == "battery":
            return _battery(device)

        if action not in ("status", "set", "up", "down", "mute", "unmute", "toggle_mute"):
            return (
                f"I don't know the audio action '{params.get('action')}', sir. "
                "Try list_devices, set_default, device_volume_set/up/down, "
                "device_mute/unmute, device_status, set_voice_output, "
                "clear_voice_output, voice_output, battery, app_preference, or "
                "clear_app_preference."
            )

        # ── Baseline Routing Rule ───────────────────────────────────────────
        if _is_system(device):
            return _system(action, params)

        # ── Explicit device ─────────────────────────────────────────────────
        # One path for EVERY device. A per-model branch used to sit here for
        # "cy03-cb36", calling four helpers that never existed on
        # audio_endpoints - so every command naming the glasses by their model
        # number raised AttributeError and was swallowed into "I couldn't
        # complete that audio command, sir." Devices are resolved by name and
        # form factor; no model is special-cased.
        if action == "status":
            return ae.device_status(device, flow).message
        if action == "set":
            from actions.computer_settings import _parse_volume_value

            v = _parse_volume_value(params.get("value"))
            if v is None:
                return f"Tell me a level from 0 to 100 for {device}, sir."
            return ae.set_device_volume(device, v, flow).message
        if action in ("up", "down"):
            step = _step(params)
            return ae.step_device_volume(device, step if action == "up" else -step, flow).message
        muted = {"mute": True, "unmute": False, "toggle_mute": None}[action]
        return ae.set_device_mute(device, muted, flow).message
    except Exception as e:  # last-resort: the tool must always answer
        print(f"[audio_control] unexpected failure: {type(e).__name__}: {e}")
        return "I couldn't complete that audio command, sir."
