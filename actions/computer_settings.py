# actions/computer_settings.py
import json
import os
import re
import subprocess
import time
from pathlib import Path

from core.utils import get_api_key, get_os, is_windows
from core.win_subprocess import NO_WINDOW

_OS = get_os()

if is_windows():
    pass

try:
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip

    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False


def _get_macos_wifi_interface() -> str:
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line or "AirPort" in line:
                for j in range(i, min(i + 4, len(lines))):
                    if lines[j].startswith("Device:"):
                        return lines[j].split(":", 1)[1].strip()
    except Exception:
        pass
    return "en0"


# ─────────────────────────────────────────────────────────────────────────────
# VOLUME HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def _parse_volume_value(value) -> int | None:
    """
    Robustly extract an integer 0-100 from any value:
      "50%"  "50 %"  "50percent"  "50"  50  "half"  "max"  "min"  etc.
    Returns None if unparseable.
    """
    if value is None:
        return None

    # Named levels
    named = {
        "max": 100,
        "maximum": 100,
        "full": 100,
        "min": 0,
        "minimum": 0,
        "mute": 0,
        "silent": 0,
        "half": 50,
        "medium": 50,
        "mid": 50,
        "middle": 50,
        "low": 25,
        "quiet": 25,
        "high": 75,
        "loud": 75,
    }
    s = str(value).strip().lower()
    if s in named:
        return named[s]

    # Extract first integer in the string (handles "50%", "50 %", "vol 50", etc.)
    m = re.search(r"\d+", s)
    if m:
        return max(0, min(100, int(m.group())))

    return None


def _step_from(params: dict, value=None) -> int:
    """Percent step for volume up/down: explicit step/amount/value, else 10."""
    for raw in (params.get("step"), params.get("amount"), value):
        parsed = _parse_volume_value(raw) if raw not in (None, "") else None
        if parsed:
            return parsed
    return 10


_SYSTEM_DEVICE_WORDS = ("", "system", "default", "master", "computer", "pc", "global", "main")
_DEVICE_ACTIONS = {
    "volume_set": "volume_set",
    "set_volume": "volume_set",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "mute": "mute",
    "unmute": "unmute",
    "toggle_mute": "toggle_mute",
    "get_volume": "status",
    "volume_status": "status",
}


def _route_named_device(action: str, params: dict, value) -> str | None:
    """Baseline Routing Rule, other half: only an EXPLICIT device name redirects a
    volume/mute command away from the system mixer. Returns the audio_control
    reply, or None when no device was named (caller stays on the system path)."""
    device = str(params.get("device") or params.get("target") or "").strip()
    if device.lower() in _SYSTEM_DEVICE_WORDS or action not in _DEVICE_ACTIONS:
        return None
    from actions.audio_control import audio_control

    return audio_control(
        {
            "action": _DEVICE_ACTIONS[action],
            "device": device,
            "flow": params.get("flow"),
            "value": value,
            "step": params.get("step"),
        }
    )


def _pycaw_endpoint_volume():
    """Return the IAudioEndpointVolume interface, handling both modern and
    legacy pycaw APIs. Modern pycaw (>=2020) exposes it as AudioDevice.EndpointVolume;
    older builds require Activate() on the raw IMMDevice."""
    from pycaw.pycaw import AudioUtilities

    speakers = AudioUtilities.GetSpeakers()
    # Modern API: the interface is already resolved on the device wrapper.
    if hasattr(speakers, "EndpointVolume"):
        return speakers.EndpointVolume
    # Legacy API: activate the interface manually.
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def _glass_endpoint_volume():
    """IAudioEndpointVolume for the Star Glasses render endpoint, or None.

    Enumerates ALL endpoints (not just the default) and matches the configured
    glass identifiers by name, so it targets the glasses even while the laptop
    speakers stay the default device carrying music. Degrades to None on any
    failure — the caller turns that into a spoken message, never an exception.
    """
    from core.audio_devices import matches_glass

    try:
        from pycaw.pycaw import AudioUtilities

        devices = AudioUtilities.GetAllDevices()
    except Exception as e:
        print(f"[Settings] glass endpoint enumeration failed: {e}")
        return None

    for dev in devices:
        name = getattr(dev, "FriendlyName", "") or ""
        if not matches_glass(name):
            continue
        # Modern pycaw: the interface is already resolved on the wrapper.
        ev = getattr(dev, "EndpointVolume", None)
        if ev is not None:
            return ev
        raw = getattr(dev, "_dev", None)
        if raw is None:
            continue
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume

            iface = raw.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return cast(iface, POINTER(IAudioEndpointVolume))
        except Exception as e:
            print(f"[Settings] glass endpoint activate failed: {e}")
            return None
    return None


def glass_volume(action: str = "status", value=None, step: int = 10) -> str:
    """Adjust ONLY the Star Glasses output — never the system mixer.

    This is the explicit 'glass volume' path. Generic 'volume up/down' stays on
    the system endpoint (volume_up / volume_down). ``action``: up | down | set |
    status. Refuses cleanly when the glasses are not connected.
    """
    if not is_windows():
        return "Glass volume control is Windows-only, sir."

    from core.audio_devices import glasses_connected

    if not glasses_connected():
        return "The Star Glasses aren't connected, sir — there's nothing to adjust there."

    ev = _glass_endpoint_volume()
    if ev is None:
        return "I can see the glasses but couldn't reach their volume control, sir."

    try:
        current = round(ev.GetMasterVolumeLevelScalar() * 100)
        act = (action or "status").lower().strip()
        if act in ("up", "louder", "increase"):
            target = current + int(step)
        elif act in ("down", "quieter", "decrease"):
            target = current - int(step)
        elif act in ("set", "absolute"):
            parsed = _parse_volume_value(value)
            if parsed is None:
                return "Tell me a level from 0 to 100 for the glasses, sir."
            target = parsed
        else:  # status
            return f"The glasses are at {current}%, sir."
        target = max(0, min(100, int(target)))
        ev.SetMute(0, None)
        ev.SetMasterVolumeLevelScalar(target / 100.0, None)
        verified = round(ev.GetMasterVolumeLevelScalar() * 100)
        return f"Glasses volume set to {verified}%, sir."
    except Exception as e:
        print(f"[Settings] glass volume write failed: {e}")
        return "I couldn't change the glasses volume, sir."


def glass_volume_up(step: int = 10) -> str:
    return glass_volume("up", step=step)


def glass_volume_down(step: int = 10) -> str:
    return glass_volume("down", step=step)


def glass_volume_set(value) -> str:
    return glass_volume("set", value)


def _get_volume_pycaw() -> int | None:
    try:
        vol_iface = _pycaw_endpoint_volume()
        return round(vol_iface.GetMasterVolumeLevelScalar() * 100)
    except Exception as e:
        print(f"[Settings] pycaw volume read failed: {e}")
        return None


def _get_volume_ctypes_com() -> int | None:
    """Pure ctypes COM read of the master volume scalar — no pip packages required."""
    try:
        import ctypes

        CLSCTX_ALL = 0x17
        CLSID_str = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
        IID_enum_str = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
        IID_dev_str = "{D666063F-1587-4E43-81F1-B948E807363F}"
        IID_vol_str = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        def _guid(s: str):
            g = ctypes.c_buffer(16)
            ole32.CLSIDFromString(ctypes.c_wchar_p(s), g)
            return g

        clsid = _guid(CLSID_str)
        iid_e = _guid(IID_enum_str)
        _guid(IID_dev_str)
        iid_v = _guid(IID_vol_str)

        p_enum = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(clsid, None, CLSCTX_ALL, iid_e, ctypes.byref(p_enum))
        if hr:
            raise OSError(f"CoCreateInstance hr={hr:#010x}")

        GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        )(ctypes.cast(p_enum, ctypes.POINTER(ctypes.c_void_p * 10)).contents[4])

        p_dev = ctypes.c_void_p()
        hr = GetDefaultAudioEndpoint(p_enum, 0, 1, ctypes.byref(p_dev))
        if hr:
            raise OSError(f"GetDefaultAudioEndpoint hr={hr:#010x}")

        Activate = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(ctypes.cast(p_dev, ctypes.POINTER(ctypes.c_void_p * 10)).contents[3])

        p_vol = ctypes.c_void_p()
        hr = Activate(p_dev, iid_v, CLSCTX_ALL, None, ctypes.byref(p_vol))
        if hr:
            raise OSError(f"IMMDevice::Activate hr={hr:#010x}")

        # Vtable slot 9 = IAudioEndpointVolume::GetMasterVolumeLevelScalar
        GetVolume = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
        )(ctypes.cast(p_vol, ctypes.POINTER(ctypes.c_void_p * 20)).contents[9])

        scalar = ctypes.c_float()
        hr = GetVolume(p_vol, ctypes.byref(scalar))
        if hr:
            raise OSError(f"GetMasterVolumeLevelScalar hr={hr:#010x}")

        return round(scalar.value * 100)
    except Exception as e:
        print(f"[Settings] ctypes COM volume read failed: {e}")
        return None


def _get_volume_powershell() -> int | None:
    ps_get = r"""
try {
Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume2{int a();int b();int c();int d();int e();int f();
int GetMasterVolumeLevelScalar(out float v);int g();int h();int i();int j();int k();int l();}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice2{int Activate(ref Guid g,uint c,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator2{int x();int GetDefaultAudioEndpoint(uint d,uint r,out IMMDevice2 e);}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]class MMDECom2{}
public class VCReader{public static float Get(){
var e=(IMMDeviceEnumerator2)new MMDECom2();
IMMDevice2 d;e.GetDefaultAudioEndpoint(0,1,out d);
var g=typeof(IAudioEndpointVolume2).GUID;object o;d.Activate(ref g,23,IntPtr.Zero,out o);
var a=(IAudioEndpointVolume2)o;float v;a.GetMasterVolumeLevelScalar(out v);return v;}}
'@ -Language CSharp -ErrorAction Stop
[math]::Round([VCReader]::Get() * 100)
} catch { Write-Output "ERR" }
"""
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_get],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=NO_WINDOW,
        )
        if res.returncode == 0:
            out = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
            if out.isdigit():
                return max(0, min(100, int(out)))
    except Exception as e:
        print(f"[Settings] PowerShell volume read failed: {e}")
    return None


def _get_volume_macos() -> int | None:
    try:
        res = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = res.stdout.strip()
        if res.returncode == 0 and out.isdigit():
            return max(0, min(100, int(out)))
    except Exception as e:
        print(f"[Settings] macOS volume read failed: {e}")
    return None


def _get_volume_linux() -> int | None:
    try:
        res = subprocess.run(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        m = re.search(r"(\d+)%", res.stdout)
        if m:
            return max(0, min(100, int(m.group(1))))
    except Exception as e:
        print(f"[Settings] Linux volume read failed: {e}")
    return None


def _get_volume_endpoint_layer() -> int | None:
    """The system output's level, immune to this process's app device preference."""
    try:
        from core import audio_endpoints as ae

        res = ae.system_status()
        return res.data.get("volume") if res.ok else None
    except Exception as e:
        print(f"[Settings] endpoint volume read unavailable: {e}")
        return None


def get_current_volume() -> int | None:
    """
    Read the exact master output volume (0-100) WITHOUT changing it.

    Windows tries the endpoint layer → pycaw → pure ctypes COM → PowerShell C#,
    in that order, falling through only on failure so the returned value is
    always a real reading rather than a guess. The endpoint layer leads because
    the three below resolve the default in-process, where a per-app device
    preference silently redirects them onto the pinned device.
    """
    if is_windows():
        for reader in (
            _get_volume_endpoint_layer,
            _get_volume_pycaw,
            _get_volume_ctypes_com,
            _get_volume_powershell,
        ):
            result = reader()
            if result is not None:
                return result
        return None
    elif _OS == "Darwin":
        return _get_volume_macos()
    else:
        return _get_volume_linux()


def get_volume() -> str:
    if is_windows():
        msg = _system_result("system_status")
        if msg:
            return msg
    vol = get_current_volume()
    if vol is None:
        return "Could not read the current volume level, sir."
    return f"Volume is currently at {vol}%."


def _set_volume_powershell(value: int) -> bool:
    """
    Set the master volume via an inline C# COM call.

    Mirrors the interface layout of the working _get_volume_powershell reader:
    the placeholder methods pad the vtable so SetMasterVolumeLevelScalar lands on
    slot 7 and SetMute on slot 14. Returns True on success.
    """
    scalar = round(value / 100.0, 4)
    ps_script = r"""
try {
Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume3{int a();int b();int c();int d();
int SetMasterVolumeLevelScalar(float v,Guid g);int f();int GetMasterVolumeLevelScalar(out float v);
int g();int h();int i();int j();int SetMute([MarshalAs(UnmanagedType.Bool)]bool m,Guid g);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice3{int Activate(ref Guid g,uint c,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator3{int x();int GetDefaultAudioEndpoint(uint d,uint r,out IMMDevice3 e);}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]class MMDECom3{}
public class VCWriter{public static void Set(float v){
var e=(IMMDeviceEnumerator3)new MMDECom3();
IMMDevice3 d;e.GetDefaultAudioEndpoint(0,1,out d);
var g=typeof(IAudioEndpointVolume3).GUID;object o;d.Activate(ref g,23,IntPtr.Zero,out o);
var a=(IAudioEndpointVolume3)o;a.SetMute(false,Guid.Empty);
a.SetMasterVolumeLevelScalar(v,Guid.Empty);}}
'@ -Language CSharp -ErrorAction Stop
[VCWriter]::Set([float]__SCALAR__)
Write-Output "OK"
} catch { Write-Output ("ERR:" + $_.Exception.Message) }
""".replace("__SCALAR__", repr(scalar))
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        out = (res.stdout or "").strip()
        if res.returncode == 0 and out.splitlines() and out.splitlines()[-1] == "OK":
            return True
        print(f"[Settings] PowerShell C# set failed: {out or res.stderr.strip()[:300]}")
    except Exception as e:
        print(f"[Settings] PowerShell C# set error: {e}")
    return False


def _system_result(fn, *args):
    """Run a core.audio_endpoints system op. Returns its message on success, else None
    so the caller can fall back to the legacy key-press path."""
    try:
        from core import audio_endpoints as ae

        res = getattr(ae, fn)(*args)
        return res.message if res.ok else None
    except Exception as e:
        print(f"[Settings] endpoint volume path unavailable ({fn}): {e}")
        return None


def volume_up(step: int = 10) -> str:
    """Raise the SYSTEM volume by `step` percent and report the verified level."""
    if is_windows():
        msg = _system_result("system_step_volume", abs(int(step)))
        if msg:
            return msg
        import ctypes

        # Legacy fallback: each key press is 2%. The level can't be read back here,
        # so say so instead of implying a verified result.
        for _ in range(max(1, round(abs(int(step)) / 2))):
            ctypes.windll.user32.keybd_event(0xAF, 0, 0, 0)
        return "Volume increased, sir (I couldn't read the new level back)."
    elif _OS == "Darwin":
        subprocess.run(
            [
                "osascript",
                "-e",
                f"set volume output volume (output volume of (get volume settings) + {int(step)})",
            ],
            capture_output=True,
        )
    else:
        subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"+{int(step)}%"], capture_output=True
        )
    return "Volume increased, sir."


def volume_down(step: int = 10) -> str:
    """Lower the SYSTEM volume by `step` percent and report the verified level."""
    if is_windows():
        msg = _system_result("system_step_volume", -abs(int(step)))
        if msg:
            return msg
        import ctypes

        for _ in range(max(1, round(abs(int(step)) / 2))):
            ctypes.windll.user32.keybd_event(0xAE, 0, 0, 0)
        return "Volume decreased, sir (I couldn't read the new level back)."
    elif _OS == "Darwin":
        subprocess.run(
            [
                "osascript",
                "-e",
                f"set volume output volume (output volume of (get volume settings) - {int(step)})",
            ],
            capture_output=True,
        )
    else:
        subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"-{int(step)}%"], capture_output=True
        )
    return "Volume decreased, sir."


def volume_mute(mode: str = "toggle") -> str:
    """SYSTEM mute. mode: 'mute' | 'unmute' set the state explicitly (idempotent);
    'toggle' flips the state we just read. Never a blind key press when the real
    state is readable - a blind toggle is how 'unmute' used to MUTE."""
    mode = (mode or "toggle").lower()
    want = {"mute": True, "unmute": False}.get(mode)
    if is_windows():
        msg = _system_result("system_set_mute", want)
        if msg:
            return msg
        if want is not None:
            return f"I couldn't {mode} the system audio reliably, sir - I can't read its state."
        import ctypes

        ctypes.windll.user32.keybd_event(0xAD, 0, 0, 0)
        return "Mute toggled, sir (I couldn't read the state back)."
    elif _OS == "Darwin":
        flag = "without" if want is False else "with"
        subprocess.run(["osascript", "-e", f"set volume {flag} output muted"], capture_output=True)
    else:
        arg = {True: "1", False: "0"}.get(want, "toggle")
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", arg], capture_output=True)
    return {True: "Muted, sir.", False: "Unmuted, sir."}.get(want, "Mute toggled, sir.")


def volume_set(value: int, device: str = "system") -> str:
    """
    Set volume to an exact percentage. Default targets the global master
    endpoint (system). Only targets secondary device if device explicitly
    names it (e.g., glasses, smart_glasses). Generic terms baseline to
    global level.
    """
    value = max(0, min(100, int(value)))
    target_device = (device or "system").lower().strip()
    is_glasses = target_device in ("glasses", "smart_glasses", "glasses_output")

    # Glasses are a DISTINCT endpoint: route there explicitly and return, never
    # falling through to the system master mixer. Anything not explicitly glasses
    # stays on the system endpoint below.
    if is_glasses:
        return glass_volume("set", value)

    if is_windows():
        # The endpoint layer FIRST: every path below resolves "the system output"
        # with an in-process GetDefaultAudioEndpoint, which Windows rewrites when
        # this executable has a per-app device preference - that is how an
        # absolute "set volume to 30" used to land on the Star Glasses while
        # volume_up / volume_down (already on this path) hit the speakers.
        msg = _system_result("system_set_volume", value)
        if msg:
            return msg
        # ── Method 1: pycaw (global endpoint only unless glasses explicitly named) ─
        try:
            vol_iface = _pycaw_endpoint_volume()
            # If user explicitly wants glasses, we'd need endpoint selection (not implemented);
            # instead enforce: unless glasses specified, use master endpoint (global).
            vol_iface.SetMute(0, None)
            vol_iface.SetMasterVolumeLevelScalar(value / 100.0, None)
            # Verification: read back from same endpoint to confirm applied to correct device
            verified = round(vol_iface.GetMasterVolumeLevelScalar() * 100)
            if abs(verified - value) > 2:
                return f"Volume verification failed: set to {value}% but endpoint reports {verified}%. Device targeted: {'glasses' if is_glasses else 'system (global)'}"
            print(
                f"[Settings] Volume set to {value}%  (pycaw, device={target_device}, verified={verified}%)"
            )
            return f"Volume set to {value}% (verified {verified}%, device={target_device})."
        except Exception as e:
            print(f"[Settings] pycaw failed: {e}")

        # ── Method 2: pure ctypes COM — no external packages ─────────────────
        try:
            import ctypes
            import ctypes.wintypes

            CLSCTX_ALL = 0x17
            CLSID_str = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
            IID_enum_str = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
            IID_dev_str = "{D666063F-1587-4E43-81F1-B948E807363F}"
            IID_vol_str = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)

            def _guid(s: str):
                g = ctypes.c_buffer(16)
                ole32.CLSIDFromString(ctypes.c_wchar_p(s), g)
                return g

            clsid = _guid(CLSID_str)
            iid_e = _guid(IID_enum_str)
            _guid(IID_dev_str)
            iid_v = _guid(IID_vol_str)

            p_enum = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(clsid, None, CLSCTX_ALL, iid_e, ctypes.byref(p_enum))
            if hr:
                raise OSError(f"CoCreateInstance hr={hr:#010x}")

            GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p),
            )(ctypes.cast(p_enum, ctypes.POINTER(ctypes.c_void_p * 10)).contents[4])

            p_dev = ctypes.c_void_p()
            hr = GetDefaultAudioEndpoint(p_enum, 0, 1, ctypes.byref(p_dev))
            if hr:
                raise OSError(f"GetDefaultAudioEndpoint hr={hr:#010x}")

            Activate = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(ctypes.cast(p_dev, ctypes.POINTER(ctypes.c_void_p * 10)).contents[3])

            p_vol = ctypes.c_void_p()
            hr = Activate(p_dev, iid_v, CLSCTX_ALL, None, ctypes.byref(p_vol))
            if hr:
                raise OSError(f"IMMDevice::Activate hr={hr:#010x}")

            SetVolume = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.c_float,
                ctypes.c_void_p,
            )(ctypes.cast(p_vol, ctypes.POINTER(ctypes.c_void_p * 20)).contents[7])

            SetMute = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.c_bool,
                ctypes.c_void_p,
            )(ctypes.cast(p_vol, ctypes.POINTER(ctypes.c_void_p * 20)).contents[13])

            SetMute(p_vol, False, None)
            SetVolume(p_vol, ctypes.c_float(value / 100.0), None)
            # Post-set verification (global endpoint read-back) to prevent glasses misroute
            verify_iface = _pycaw_endpoint_volume()
            verified = round(verify_iface.GetMasterVolumeLevelScalar() * 100)
            if abs(verified - value) > 2:
                return f"Volume set but verification mismatch (abs src device={target_device}): set {value}%, read back {verified}%."
            print(
                f"[Settings] Volume set to {value}%  (ctypes COM, device={target_device}, verified={verified}%)"
            )
            return f"Volume set to {value}% (verified {verified}%, device={target_device})."
        except Exception as e:
            print(f"[Settings] ctypes COM failed: {e}")

        # ── Method 3: PowerShell inline C# (global master endpoint) ─────────
        if _set_volume_powershell(value):
            # Verify: read back via pycaw global endpoint to confirm not glasses
            try:
                verified = round(_pycaw_endpoint_volume().GetMasterVolumeLevelScalar() * 100)
            except Exception:
                verified = None
            diag = f" (verified={verified}%" if verified is not None else ""
            print(
                f"[Settings] Volume set to {value}%  (PowerShell C#, device={target_device}{diag})"
            )
            return f"Volume set to {value}% (device={target_device}{diag})."

        # ── Method 4: relative keystroke adjustment ──────────────────────────
        try:
            import ctypes

            current = get_current_volume()
            if current is None:
                current = 50

            print(f"[Settings] Current volume: {current}% -> target: {value}%")

            KEYDOWN, KEYUP = 0, 2
            VK_VOL_DOWN = 0xAE
            VK_VOL_UP = 0xAF

            def _press(vk: int):
                ctypes.windll.user32.keybd_event(vk, 0, KEYDOWN, 0)
                time.sleep(0.015)
                ctypes.windll.user32.keybd_event(vk, 0, KEYUP, 0)
                time.sleep(0.015)

            delta = value - current
            steps = abs(round(delta / 2))
            vk = VK_VOL_UP if delta > 0 else VK_VOL_DOWN

            for _ in range(steps):
                _press(vk)

            print(f"[Settings] Volume adjusted by {delta:+d}%  (relative keystrokes)")
            return f"Volume set to approximately {value}%."
        except Exception as e:
            return f"All volume methods failed. Run: pip install pycaw comtypes  (last error: {e})"

    elif _OS == "Darwin":
        try:
            subprocess.run(
                ["osascript", "-e", f"set volume output volume {value}"],
                capture_output=True,
                check=True,
            )
            return f"Volume set to {value}%."
        except Exception as e:
            return f"Failed to set volume on macOS: {e}"

    else:  # Linux
        try:
            subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"],
                capture_output=True,
                check=True,
            )
            return f"Volume set to {value}%."
        except Exception as e:
            return f"Failed to set volume on Linux: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# BRIGHTNESS & WINDOWS LAYOUTS
# ─────────────────────────────────────────────────────────────────────────────


def brightness_up():
    if _OS == "Darwin":
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 144'],
            capture_output=True,
        )
    elif _OS == "Linux":
        subprocess.run(["brightnessctl", "set", "+10%"], capture_output=True)
    else:
        subprocess.run(
            [
                "powershell",
                "-Command",
                "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, [math]::Min(100, (Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness + 10))",
            ],
            capture_output=True,
            creationflags=NO_WINDOW,
        )


def brightness_down():
    if _OS == "Darwin":
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 145'],
            capture_output=True,
        )
    elif _OS == "Linux":
        subprocess.run(["brightnessctl", "set", "10%-"], capture_output=True)
    else:
        subprocess.run(
            [
                "powershell",
                "-Command",
                "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, [math]::Max(0, (Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness - 10))",
            ],
            capture_output=True,
            creationflags=NO_WINDOW,
        )


def close_app():
    if _OS == "Darwin":
        pyautogui.hotkey("command", "q")
    else:
        pyautogui.hotkey("alt", "f4")


def close_window():
    if _OS == "Darwin":
        pyautogui.hotkey("command", "w")
    else:
        pyautogui.hotkey("ctrl", "w")


def full_screen():
    """OS-level fullscreen via F11 (desktop/apps).  NOT for Netflix — see netflix_control."""
    if _OS == "Darwin":
        pyautogui.hotkey("ctrl", "command", "f")
    else:
        pyautogui.press("f11")


def minimize_window():
    if _OS == "Darwin":
        pyautogui.hotkey("command", "m")
    else:
        pyautogui.hotkey("win", "down")


def maximize_window():
    if _OS == "Windows":
        pyautogui.hotkey("win", "up")
    elif _OS == "Darwin":
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to keystroke "f" using {control down, command down}',
            ],
            capture_output=True,
        )


def snap_left():
    pyautogui.hotkey("win", "left") if is_windows() else None


def snap_right():
    pyautogui.hotkey("win", "right") if is_windows() else None


def switch_window():
    pyautogui.hotkey("command", "tab") if _OS == "Darwin" else pyautogui.hotkey("alt", "tab")


def show_desktop():
    pyautogui.hotkey("win", "d") if is_windows() else None


def show_desktop_robust() -> str:
    """Focus-independent Show Desktop (win+d often fails on Windows)."""
    if is_windows():
        try:
            import win32com.client

            win32com.client.Dispatch("Shell.Application").MinimizeAll()
            return "Desktop shown."
        except Exception as e:
            print(f"[Settings] MinimizeAll failed: {e}")
        try:
            pyautogui.hotkey("win", "d")
            return "Desktop shown (keystroke)."
        except Exception as e:
            return f"Show desktop failed: {e}"
    if _OS == "Darwin":
        pyautogui.hotkey("fn", "f11")
        return "Desktop shown."
    return "Show desktop not supported on this OS."


def open_task_manager():
    subprocess.Popen(["open", "-a", "Activity Monitor"]) if _OS == "Darwin" else pyautogui.hotkey(
        "ctrl", "shift", "esc"
    )


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER & NAVIGATION
# ─────────────────────────────────────────────────────────────────────────────


def focus_search():
    pyautogui.hotkey("command", "l") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "l")


def pause_video():
    pyautogui.press("space")


def refresh_page():
    pyautogui.hotkey("command", "r") if _OS == "Darwin" else pyautogui.press("f5")


def close_tab():
    pyautogui.hotkey("command", "w") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "w")


def new_tab():
    pyautogui.hotkey("command", "t") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "t")


def next_tab():
    pyautogui.hotkey("command", "shift", "bracketright") if _OS == "Darwin" else pyautogui.hotkey(
        "ctrl", "tab"
    )


def prev_tab():
    pyautogui.hotkey("command", "shift", "bracketleft") if _OS == "Darwin" else pyautogui.hotkey(
        "ctrl", "shift", "tab"
    )


def go_back():
    pyautogui.hotkey("command", "left") if _OS == "Darwin" else pyautogui.hotkey("alt", "left")


def go_forward():
    pyautogui.hotkey("command", "right") if _OS == "Darwin" else pyautogui.hotkey("alt", "right")


def zoom_in():
    pyautogui.hotkey("command", "equal") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "equal")


def zoom_out():
    pyautogui.hotkey("command", "minus") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "minus")


def zoom_reset():
    pyautogui.hotkey("command", "0") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "0")


def find_on_page():
    pyautogui.hotkey("command", "f") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "f")


def reload_page_n(n: int):
    for _ in range(max(1, n)):
        refresh_page()
        time.sleep(0.8)


def scroll_up(amount: int = 500):
    pyautogui.scroll(amount)


def scroll_down(amount: int = 500):
    pyautogui.scroll(-amount)


def scroll_top():
    pyautogui.hotkey("command", "up") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "home")


def scroll_bottom():
    pyautogui.hotkey("command", "down") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "end")


def page_up():
    pyautogui.press("pageup")


def page_down():
    pyautogui.press("pagedown")


# ─────────────────────────────────────────────────────────────────────────────
# CLIPBOARD / EDITING
# ─────────────────────────────────────────────────────────────────────────────


def copy():
    pyautogui.hotkey("command", "c") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "c")


def paste():
    pyautogui.hotkey("command", "v") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "v")


def cut():
    pyautogui.hotkey("command", "x") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "x")


def undo():
    pyautogui.hotkey("command", "z") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "z")


def redo():
    pyautogui.hotkey("command", "shift", "z") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "y")


def select_all():
    pyautogui.hotkey("command", "a") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "a")


def save_file():
    pyautogui.hotkey("command", "s") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "s")


def press_enter():
    pyautogui.press("enter")


def press_escape():
    pyautogui.press("escape")


def press_key(key: str):
    pyautogui.press(key)


def type_text(text: str, press_enter_after: bool = False):
    if not text:
        return
    if _PYPERCLIP:
        pyperclip.copy(str(text))
        time.sleep(0.15)
        paste()
    else:
        pyautogui.write(str(text), interval=0.03)
    if press_enter_after:
        time.sleep(0.1)
        pyautogui.press("enter")


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM INFRASTRUCTURE & BLUETOOTH CONTROL
# ─────────────────────────────────────────────────────────────────────────────


def take_screenshot():
    pyautogui.hotkey("win", "shift", "s") if is_windows() else pyautogui.hotkey(
        "command", "shift", "3"
    )


def lock_screen():
    pyautogui.hotkey("win", "l") if is_windows() else subprocess.run(
        ["pmset", "displaysleepnow"], capture_output=True
    )


def open_system_settings():
    pyautogui.hotkey("win", "i") if is_windows() else subprocess.Popen(
        ["open", "-a", "System Preferences"]
    )


def open_file_explorer():
    pyautogui.hotkey("win", "e") if is_windows() else subprocess.Popen(["open", str(Path.home())])


def open_run():
    pyautogui.hotkey("win", "r") if is_windows() else None


def sleep_display():
    if is_windows():
        import ctypes

        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"], capture_output=True)


def dark_mode():
    if is_windows():
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_ALL_ACCESS,
        )
        current, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, 1 - current)
        winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, 1 - current)
        winreg.CloseKey(key)


def toggle_wifi():
    if is_windows():
        subprocess.run(
            [
                "powershell",
                "-Command",
                "$adapter = Get-NetAdapter | Where-Object {$_.PhysicalMediaType -eq 'Native 802.11'}; if ($adapter.Status -eq 'Up') { Disable-NetAdapter -Name $adapter.Name -Confirm:$false } else { Enable-NetAdapter -Name $adapter.Name -Confirm:$false }",
            ],
            capture_output=True,
            creationflags=NO_WINDOW,
        )


# WinRT radio control, driven from PowerShell.
#
# The obvious script — call GetRadiosAsync() and poll the returned object's
# .Status until it reads 'Completed' — does NOT work. PowerShell hands back a
# bare System.__ComObject with no WinRT projection applied, so .Status is $null,
# the poll loop exits on its first pass and the script reports a timeout that
# never happened. Every Bluetooth request failed that way, and because the
# message said "timed out" the result classifier (core/tool_result.py) turned it
# into an ActionError, so Era reported a hang rather than a broken call.
#
# The projection has to be brought in explicitly: load System.Runtime.WindowsRuntime,
# grab the single-argument AsTask overload for IAsyncOperation`1, and await the
# real Task. RequestAccessAsync() must also come first — without it Windows
# refuses SetStateAsync and returns DeniedBySystem instead of switching the radio.
_BT_RADIO_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $resultType) {
    $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
    if (-not $task.Wait(5000)) { throw 'Windows did not answer the radio call in time' }
    $task.Result
}
[Void][Windows.Devices.Radios.Radio, Windows, ContentType=WindowsRuntime]
[Void][Windows.Devices.Radios.RadioAccessStatus, Windows, ContentType=WindowsRuntime]
[Void][Windows.Devices.Radios.RadioState, Windows, ContentType=WindowsRuntime]

$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
if ($access -ne 'Allowed') { Write-Output "Bluetooth radio access failed - Windows answered $access"; exit 0 }

$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$bth = $radios | Where-Object { $_.Kind -eq 'Bluetooth' } | Select-Object -First 1
if (-not $bth) { Write-Output 'Bluetooth hardware not found on this machine'; exit 0 }

$desired = '__DESIRED__'
if ($desired -eq 'Report') { Write-Output "Bluetooth is $($bth.State)"; exit 0 }
if ($desired -eq 'Toggle') { $desired = if ($bth.State -eq 'On') { 'Off' } else { 'On' } }
if ($bth.State -eq $desired) { Write-Output "Bluetooth is already $desired"; exit 0 }

$set = Await ($bth.SetStateAsync([Windows.Devices.Radios.RadioState]$desired)) ([Windows.Devices.Radios.RadioAccessStatus])
if ($set -eq 'Allowed') { Write-Output "Bluetooth turned $desired" }
else { Write-Output "Switching the Bluetooth radio failed - Windows answered $set" }
"""


def _set_bluetooth_radio(desired: str) -> str:
    """Switch the Bluetooth radio. `desired` is 'On', 'Off', 'Toggle' or 'Report'."""
    if not is_windows():
        return "Optimized for Windows only."

    ps_script = _BT_RADIO_PS.replace("__DESIRED__", desired)
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
            ],
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
            timeout=20,
        )
        out = (res.stdout or "").strip()
        if out:
            return out
        err = (res.stderr or "").strip().splitlines()
        # Surface the real PowerShell error rather than a blank success. The
        # wording of every branch here matters: core/tool_result.py decides
        # success purely from the text, so a failure that avoids its vocabulary
        # ("error", "failed", "timed out", "not found") is reported to the user
        # as if the radio had switched.
        return f"Bluetooth radio call failed: {err[0] if err else 'no output from PowerShell'}"
    except subprocess.TimeoutExpired:
        return "The Bluetooth radio call timed out."
    except Exception as e:
        return f"Bluetooth radio error: {e}"


def toggle_bluetooth() -> str:
    return _set_bluetooth_radio("Toggle")


def bluetooth_on() -> str:
    return _set_bluetooth_radio("On")


def bluetooth_off() -> str:
    return _set_bluetooth_radio("Off")


def bluetooth_status() -> str:
    return _set_bluetooth_radio("Report")


def _normalize_bt_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _get_bt_paired_devices() -> list[tuple[str, str]]:
    if not is_windows():
        return []

    devices: list[tuple[str, str]] = []
    mac_re = re.compile(
        r"([0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2})"
    )

    try:
        # Try btpair first with a strict timeout so it never hangs
        res = subprocess.run(
            ["btpair", "-l"], capture_output=True, text=True, creationflags=NO_WINDOW, timeout=2
        )
        out_text = (res.stdout or "") + "\n" + (res.stderr or "")
        for line in out_text.splitlines():
            m = mac_re.search(line)
            if m:
                devices.append((line, m.group(1)))
    except Exception:
        pass

    if not devices:
        try:
            # CRITICAL FIX: Direct Registry Query.
            # This bypasses PnP Class filters and instantly grabs ALL paired devices!
            ps_script = r"""
            $regPath = 'HKLM:\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices'
            $found = $false
            if (Test-Path $regPath) {
                foreach ($key in Get-ChildItem -Path $regPath -ErrorAction SilentlyContinue) {
                    $mac = $key.PSChildName
                    if ($mac -match '^[0-9a-fA-F]{12}$') {
                        $macFmt = $mac -replace '(..)(..)(..)(..)(..)(..)', '$1:$2:$3:$4:$5:$6'
                        $name = ''
                        $prop = Get-ItemProperty -Path $key.PSPath -Name 'Name' -ErrorAction SilentlyContinue
                        if ($null -ne $prop -and $null -ne $prop.Name) {
                            if ($prop.Name -is [byte[]]) {
                                $name = [System.Text.Encoding]::UTF8.GetString($prop.Name).Trim("`0")
                            } else {
                                $name = $prop.Name
                            }
                        }
                        if ([string]::IsNullOrWhiteSpace($name)) { $name = "Unknown" }
                        Write-Output "$macFmt $name"
                        $found = $true
                    }
                }
            }
            # Fallback to WMI if the registry is locked
            if (-not $found) {
                $devices = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue
                foreach ($dev in $devices) {
                    if ($dev.PNPDeviceID -match 'DEV_([0-9A-F]{12})') {
                        $mac = $Matches[1] -replace '(..)(..)(..)(..)(..)(..)', '$1:$2:$3:$4:$5:$6'
                        Write-Output "$mac $($dev.Name)"
                    }
                }
            }
            """
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                creationflags=NO_WINDOW,
                timeout=4,
            )
            for line in (res.stdout or "").splitlines():
                m = mac_re.search(line)
                if m:
                    devices.append((line, m.group(1)))
        except Exception:
            pass

    return devices


def _score_bt_name_match(target_norm: str, line_raw: str) -> int:
    if not target_norm:
        return 0

    # Strip the MAC address out of the raw text so we only score the actual name
    cleaned_line = re.sub(r"([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}", "", line_raw).strip()
    line_norm = _normalize_bt_name(cleaned_line)

    if not line_norm:
        return 0

    if target_norm == line_norm:
        return 100
    if target_norm in line_norm:
        return 80

    target_words = [w for w in target_norm.split(" ") if w]
    line_words = set(w for w in line_norm.split(" ") if w)
    overlap = sum(1 for w in target_words if w in line_words)

    # CRITICAL FIX: Increased points per word to 30.
    # Now, even saying just "Marley" will easily beat the 25-point threshold!
    score = overlap * 30

    # Small boost if the device name actually starts with the target name
    if target_words and line_norm.startswith(target_words[0]):
        score += 15

    return score


def _get_bt_mac_from_cache(device_name: str) -> str | None:
    if not is_windows():
        return None

    target_norm = _normalize_bt_name(device_name)
    if not target_norm:
        return None

    devices = _get_bt_paired_devices()
    if not devices:
        return None

    best_mac = None
    best_score = -1

    for line_raw, mac in devices:
        score = _score_bt_name_match(target_norm, line_raw)
        if score > best_score:
            best_score = score
            best_mac = mac

    return best_mac if best_score >= 25 else None


def connect_device(device_name: str) -> str:
    """Connects to a cached Windows Bluetooth device silently."""
    mac = _get_bt_mac_from_cache(device_name)
    if not mac:
        return f"Device '{device_name}' not found in paired system cache."

    try:
        # CRITICAL FIX: Windows throws Error 106 if the service is already marked "Enabled" in the registry.
        # We must force a state reset by disabling (-r) first, then immediately enabling (-c) to trigger the hardware.
        attempts = [
            "110b",  # Audio Sink (A2DP) - Priority for speakers/headphones
            "111e",  # Handsfree
            None,  # Fallback Auto/SP
        ]

        errors = []
        for service in attempts:
            # 1. Silently disable to reset the Windows registry flag
            cmd_disable = ["btcom", "-r", f"-b{mac}"]
            if service:
                cmd_disable.append(f"-s{service}")
            subprocess.run(cmd_disable, capture_output=True, creationflags=NO_WINDOW)

            # 2. Enable to force the physical connection
            cmd_enable = ["btcom", "-c", f"-b{mac}"]
            if service:
                cmd_enable.append(f"-s{service}")

            res = subprocess.run(
                cmd_enable,
                capture_output=True,
                text=True,
                creationflags=NO_WINDOW,
            )
            if res.returncode == 0:
                return f"Audio routed to {device_name}, sir."

            err_msg = (res.stderr or res.stdout or "").strip()
            if err_msg and err_msg not in errors:
                errors.append(err_msg)

        # If it still fails after the reset, the device is genuinely unreachable.
        err_out = " | ".join(errors[:2])
        if "106" in err_out or "105" in err_out:
            return f"Failed to connect. '{device_name}' may be turned off, out of range, or needs to be re-paired."

        return f"Failed to connect to {device_name}. btcom errors: {err_out}"
    except FileNotFoundError:
        return "Bluetooth Command Line Tools missing."
    except Exception as e:
        return f"Connection error: {e}"


def disconnect_device(device_name: str) -> str:
    """Disconnects from a connected Windows Bluetooth device silently."""
    mac = _get_bt_mac_from_cache(device_name)
    if not mac:
        return f"Device '{device_name}' not found in paired system cache."

    try:
        attempts = ["110b", "111e", None]

        errors = []
        success = False
        for service in attempts:
            cmd = ["btcom", "-r", f"-b{mac}"]
            if service:
                cmd.append(f"-s{service}")

            res = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
            if res.returncode == 0:
                success = True
            else:
                err_msg = (res.stderr or res.stdout or "").strip()
                if err_msg and err_msg not in errors:
                    errors.append(err_msg)

        if success:
            return f"Disconnected from {device_name}, sir."

        return f"Failed to disconnect from {device_name}. btcom errors: {' | '.join(errors[:2])}"
    except FileNotFoundError:
        return "Bluetooth Command Line Tools missing."
    except Exception as e:
        return f"Disconnection error: {e}"


def open_bluetooth_settings() -> str:
    os.system("start ms-settings:bluetooth") if is_windows() else None
    return "Opened Bluetooth settings window, sir."


# Power actions. The delay is what makes them recoverable: Windows counts down
# and `shutdown /a` aborts it, so a mistaken shutdown is one spoken sentence
# away from being cancelled. These are gated HIGH + requires_confirmation in
# core/safety_guard.py, so they only ever run after the user confirms.
_POWER_DELAY_S = 20


def _run_power_command(flag: str) -> subprocess.CompletedProcess | None:
    if not is_windows():
        return None
    return subprocess.run(
        ["shutdown", flag, "/t", str(_POWER_DELAY_S)],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=NO_WINDOW,
    )


def restart_computer() -> str:
    if not is_windows():
        return "Restarting the computer is only supported on Windows."
    try:
        res = _run_power_command("/r")
        if res and res.returncode != 0:
            return f"Restart failed: {(res.stderr or res.stdout or '').strip()}"
    except Exception as e:
        return f"Restart failed: {e}"
    return (
        f"Restarting in {_POWER_DELAY_S} seconds, sir. "
        "Say 'cancel the restart' if you change your mind."
    )


def shutdown_computer() -> str:
    if not is_windows():
        return "Shutting down the computer is only supported on Windows."
    try:
        res = _run_power_command("/s")
        if res and res.returncode != 0:
            return f"Shutdown failed: {(res.stderr or res.stdout or '').strip()}"
    except Exception as e:
        return f"Shutdown failed: {e}"
    return (
        f"Shutting down in {_POWER_DELAY_S} seconds, sir. "
        "Say 'cancel the shutdown' if you change your mind."
    )


def abort_shutdown() -> str:
    """Cancel a pending shutdown/restart during the countdown (`shutdown /a`)."""
    if not is_windows():
        return "Nothing to cancel."
    try:
        res = subprocess.run(
            ["shutdown", "/a"], capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW
        )
    except Exception as e:
        return f"Could not cancel: {e}"
    if res.returncode == 0:
        return "Cancelled — the computer stays on, sir."
    # 1116 == ERROR_NO_SHUTDOWN_IN_PROGRESS
    return "There's no shutdown in progress to cancel, sir."


# ─────────────────────────────────────────────────────────────────────────────
# ACTION MAP REGISTER
# ─────────────────────────────────────────────────────────────────────────────


def spotify_play():
    """Spotify play/resume entry point used by open_app.py.
    Prefer delegating to spotify_control to reuse its focus/timing logic.
    """
    try:
        from actions.spotify_control import spotify_control

        return spotify_control({"action": "play"})
    except Exception:
        try:
            from actions.media_coordinator import media_coordinator

            return media_coordinator({"target": "spotify", "action": "play"})
        except Exception as e:
            return f"spotify_play failed: {e}"


ACTION_MAP: dict[str, callable] = {
    "spotify_play": spotify_play,
    "volume_up": volume_up,
    "volume_down": volume_down,
    "decrease_volume": volume_down,
    "increase_volume": volume_up,
    "get_volume": get_volume,
    "volume_status": get_volume,
    "current_volume": get_volume,
    "check_volume": get_volume,
    "volume_level": get_volume,
    "mute": lambda: volume_mute("mute"),
    "unmute": lambda: volume_mute("unmute"),
    "toggle_mute": lambda: volume_mute("toggle"),
    "volume_mute": lambda: volume_mute("mute"),
    "brightness_up": brightness_up,
    "brightness_down": brightness_down,
    "sleep_display": sleep_display,
    "screen_off": sleep_display,
    "pause_video": pause_video,
    "play_pause": pause_video,
    "close_app": close_app,
    "close_window": close_window,
    "full_screen": full_screen,
    "fullscreen": full_screen,
    "minimize": minimize_window,
    "maximize": maximize_window,
    "snap_left": snap_left,
    "snap_right": snap_right,
    "switch_window": switch_window,
    # Was listed twice; the dict kept only the second, so the plain
    # show_desktop binding was dead. Keeping the robust one, which is also
    # what the show_desktop/minimize_all branch above dispatches to.
    "show_desktop": show_desktop_robust,
    "task_manager": open_task_manager,
    "focus_search": focus_search,
    "refresh_page": refresh_page,
    "reload": refresh_page,
    "close_tab": close_tab,
    "new_tab": new_tab,
    "next_tab": next_tab,
    "prev_tab": prev_tab,
    "go_back": go_back,
    "go_forward": go_forward,
    "zoom_in": zoom_in,
    "zoom_out": zoom_out,
    "zoom_reset": zoom_reset,
    "find_on_page": find_on_page,
    "scroll_up": scroll_up,
    "scroll_down": scroll_down,
    "scroll_top": scroll_top,
    "scroll_bottom": scroll_bottom,
    "page_up": page_up,
    "page_down": page_down,
    "copy": copy,
    "paste": paste,
    "cut": cut,
    "undo": undo,
    "redo": redo,
    "select_all": select_all,
    "save": save_file,
    "enter": press_enter,
    "escape": press_escape,
    "screenshot": take_screenshot,
    "lock_screen": lock_screen,
    "open_settings": open_system_settings,
    "file_explorer": open_file_explorer,
    "open_run": open_run,
    "dark_mode": dark_mode,
    "toggle_wifi": toggle_wifi,
    "restart": restart_computer,
    "reboot": restart_computer,
    "restart_computer": restart_computer,
    "shutdown": shutdown_computer,
    "shut_down": shutdown_computer,
    "power_off": shutdown_computer,
    "shutdown_computer": shutdown_computer,
    "cancel_shutdown": abort_shutdown,
    "abort_shutdown": abort_shutdown,
    "cancel_restart": abort_shutdown,
    "toggle_bluetooth": toggle_bluetooth,
    "bluetooth_on": bluetooth_on,
    "bluetooth_off": bluetooth_off,
    "bluetooth_status": bluetooth_status,
    "bluetooth_settings": open_bluetooth_settings,
    "connect_device": connect_device,
    "disconnect_device": disconnect_device,
}


def _detect_action(description: str) -> dict:
    from google import genai

    try:
        # Initializing via the upgraded standard client layout
        client = genai.Client(api_key=get_api_key())
        available = (
            ", ".join(sorted(ACTION_MAP.keys())) + ", volume_set, type_text, press_key, reload_n"
        )
        prompt = f'User instruction: "{description}"\nAvailable actions: {available}\nReturn JSON string ONLY: {{"action": "name", "value": null}}'

        resp = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
        text = re.sub(r"```(?:json)?", "", resp.text).strip().rstrip("`").strip()
        return json.loads(text)
    except Exception:
        return {"action": description.lower().replace(" ", "_"), "value": None}


def computer_settings(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    if not _PYAUTOGUI:
        return "pyautogui missing."
    params = parameters or {}
    raw_action = str(params.get("action", "")).strip()
    description = str(params.get("description", "")).strip()
    value = params.get("value", None)

    # 1. Combine ALL text fields to guarantee we never miss an app name
    intent_text = f"{raw_action} {description} {value} {params.get('text', '')} {params.get('command', '')}".lower()

    # 👇 SMART APP VOLUME INTERCEPTOR 👇
    # Added "amount" in params check to catch hijacked youtube_video payloads
    if "volume" in intent_text or "vol" in intent_text or "amount" in params:
        # If the word 'youtube' is there, OR if the 'amount' parameter was passed
        # (which is unique to the youtube_video schema), we know it belongs to YouTube.
        is_youtube = "youtube" in intent_text or "amount" in params

        if is_youtube:
            yt_action = (
                "volume_down"
                if any(w in intent_text for w in ("down", "decrease", "lower", "quieter"))
                else "volume_up"
            )

            # Grab 'amount' first, fallback to 'value', then regex
            amount = params.get("amount") or value
            if amount is None:
                m = re.search(r"\d+", intent_text)
                if m:
                    amount = int(m.group())

            try:
                from actions.youtube_video import youtube_video

                return youtube_video({"action": yt_action, "amount": amount}, player=player)
            except Exception as e:
                return f"YouTube delegation failed: {e}"

    # --- UPDATED: Integrated Bluetooth/System Volume Logic ---
    desc_lower = description.lower()
    if "volume" in desc_lower and (
        "bluetooth" in desc_lower or "system" in desc_lower or "computer" in desc_lower
    ):
        if any(w in desc_lower for w in ("up", "increase", "raise", "louder")):
            raw_action = "volume_up"
        elif any(w in desc_lower for w in ("down", "decrease", "lower", "quieter")):
            raw_action = "volume_down"
        elif "unmute" in desc_lower:
            raw_action = "unmute"
        elif "mute" in desc_lower:
            raw_action = "volume_mute"
        elif any(
            w in desc_lower for w in ("what", "current", "check", "level", "status", "how loud")
        ):
            raw_action = "get_volume"
        else:
            if value is None:
                # Extract the first number found in the description
                m = re.search(r"\d+", description)
                if m:
                    value = int(m.group())
            raw_action = "volume_set"
    # ---------------------------------------------------------

    if not raw_action and description:
        detected = _detect_action(description)
        raw_action = detected.get("action", "")
        if value is None:
            value = detected.get("value")

    action = raw_action.lower().strip().replace(" ", "_").replace("-", "_")

    # Normalize common volume/mute aliases
    if action == "volume" and value is not None:
        action = "volume_set"
    elif action == "volume_mute":
        action = "mute"

    parsed_int = _parse_volume_value(value)

    routed = _route_named_device(action, params, value)
    if routed is not None:
        return routed

    # Unified volume handler (works for System and Bluetooth speaker)
    # Generic terms baseline to global system master endpoint.
    # Only adjust secondary device (glasses) when user explicitly names it.
    if action in ("volume_set", "set_volume") or (action in ("volume",) and parsed_int is not None):
        vol = parsed_int if parsed_int is not None else 50
        # Segregate device: extract explicit device name from params/description
        explicit_device = (
            (str(params.get("device", "") or params.get("target", "") or "")).strip().lower()
        )
        desc_text = (str(description or "")).lower()
        # If user clearly names glasses/smart glasses, route specifically; else master.
        is_glasses_target = explicit_device in (
            "glasses",
            "smart_glasses",
            "glasses_output",
        ) or any(w in desc_text for w in ("glasses", "smart glasses", "glasses_output"))
        # Always pass device explicitly so volume_set can enforce segregation
        device_arg = "glasses" if is_glasses_target else "system"
        return volume_set(vol, device=device_arg)

    if action in ("mute", "unmute", "toggle_mute"):
        return volume_mute({"mute": "mute", "unmute": "unmute"}.get(action, "toggle"))

    if action == "volume_up":
        return volume_up(_step_from(params, value))

    if action == "volume_down":
        return volume_down(_step_from(params, value))

    if action in ("type_text", "write_on_screen", "type", "write"):
        text = str(value or params.get("text", "")).strip()
        if not text:
            return "No text provided."
        enter_after = str(params.get("press_enter", "false")).lower() in ("true", "1", "yes")
        type_text(text, press_enter_after=enter_after)
        return "Typed successfully, sir."

    # Map generic computer_control actions to existing primitives
    if action in {"press", "press_key", "key_press"}:
        key = str(value or params.get("key", "")).strip() or str(params.get("keys", "")).strip()
        if not key:
            return "No key specified."
        press_key(key)
        return f"Pressed: {key}"

    if action in {"hotkey", "hotkeys", "key_combo", "shortcut"}:
        keys = str(value or params.get("keys", "")).strip() or str(params.get("key", "")).strip()
        if not keys:
            return "No hotkey specified."
        # Expect either "ctrl+c" or "ctrl + c" style; pyautogui accepts separate args
        key_parts = [k.strip() for k in re.split(r"\+", keys) if k.strip()]
        if len(key_parts) < 2:
            # Fallback to simple key press
            press_key(keys)
            return f"Pressed: {keys}"
        pyautogui.hotkey(*key_parts)
        return f"Hotkey fired: {keys}"

    if action == "connect_device":
        target = str(value or params.get("description", "")).strip()
        if not target or target.lower().strip() in {
            "bluetooth",
            "speaker",
            "audio",
            "headphones",
            "headset",
            "soundbar",
            "device",
        }:
            return "Which Bluetooth device, sir?"
        return connect_device(target)

    if action in ("disconnect_device", "toggle_bluetooth", "bluetooth_on", "bluetooth_off"):
        desc_lower = str(description).lower()
        val_str = str(value or "")

        # Smart Intercept: If Era One chose 'toggle_bluetooth' but you actually meant to disconnect a device
        if (
            action == "disconnect_device"
            or "disconnect" in desc_lower
            or "disconnect" in val_str.lower()
        ):
            target = val_str.strip()

            # If the LLM missed the device name, cleanly extract it from the description
            invalid_targets = {
                "",
                "bluetooth",
                "speaker",
                "audio",
                "headphones",
                "headset",
                "soundbar",
                "device",
                "disconnect bluetooth",
                "disconnect",
            }
            if target.lower() in invalid_targets:
                desc_target = (
                    description.lower()
                    .replace("disconnect", "")
                    .replace("bluetooth", "")
                    .replace("from", "")
                    .replace("my", "")
                    .strip()
                )
                if desc_target and desc_target not in invalid_targets:
                    target = desc_target
                else:
                    return "Which Bluetooth device would you like to disconnect, sir?"

            return disconnect_device(target)

        # You wanted the radio itself. A blind toggle is wrong when the request
        # named a direction — "turn Bluetooth on" while it is already on would
        # switch it OFF — so honour an explicit direction whenever one is given
        # and only fall back to flipping when none was.
        spoken = f"{desc_lower} {val_str.lower()}"
        if action == "bluetooth_on" or any(
            w in spoken for w in ("turn on", "switch on", "enable", "activate")
        ):
            return bluetooth_on()
        if action == "bluetooth_off" or any(
            w in spoken for w in ("turn off", "switch off", "disable", "shut off", "deactivate")
        ):
            return bluetooth_off()
        return toggle_bluetooth()

    if action in ("hotkey", "key_combo", "shortcut"):
        raw = str(value or params.get("keys", "") or params.get("key", "")).strip()
        if not raw:
            return "No hotkey specified."
        parts = [k.strip() for k in re.split(r"[+\s]+", raw) if k.strip()]
        if len(parts) == 1:
            pyautogui.press(parts[0])
            return f"Pressed: {parts[0]}"
        pyautogui.hotkey(*parts)
        return f"Hotkey fired: {'+'.join(parts)}"

    if action in ("show_desktop", "minimize_all"):
        return show_desktop_robust()

    # The glasses' own volume (25/09/2026). glass_volume_up/down/set existed and
    # action_registry routed to them, but they were never in ACTION_MAP -- so
    # "glasses volume up" answered "Unknown action: 'glass_volume_up'" (found by
    # ZORO's full test). ACTION_MAP entries take no arguments; set needs a value.
    if action in (
        "glass_volume_up", "glass_volume_down", "glass_volume_set",
        "glass_volume", "glass_volume_status",
    ):  # fmt: skip
        how = {"glass_volume_up": "up", "glass_volume_down": "down", "glass_volume_set": "set"}
        how = how.get(action, "status")
        return glass_volume(how, value if how == "set" else None)

    # 👇 THE BRIDGE WE ARE RESTORING 👇
    if action.startswith("spotify_"):
        try:
            from actions.spotify_control import spotify_control

            # Remove the prefix so spotify_control understands it
            spotify_action = action.replace("spotify_", "")
            # Forward the payload, not just the verb: spotify_search arrives as
            # {"action": "spotify_search", "value": "Chill"}, and dropping `value`
            # answered every one of them "No song name provided" (2026-09-28 trace).
            forwarded = {k: v for k, v in params.items() if k != "action"}
            forwarded["action"] = spotify_action
            return spotify_control(forwarded, player=player)
        except Exception as e:
            return f"Spotify delegation failed: {e}"
    # 👆 END OF BRIDGE 👆

    func = ACTION_MAP.get(action)
    if not func:
        return f"Unknown action: '{raw_action}'."

    # CRITICAL FIX: Actually capture and return the function's message instead of blindly saying "Done"!
    result_msg = func()
    if isinstance(result_msg, str):
        return result_msg

    return f"Done: {action}."
