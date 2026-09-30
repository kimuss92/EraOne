"""Battery telemetry for connected audio devices (Windows SetupAPI).

Why SetupAPI and not the obvious routes: the battery level of a Bluetooth audio
device lives in ``DEVPKEY_Bluetooth_Battery`` on the device node, and the two
easy ways to read it are both unusable here.

* ``Get-PnpDevice`` + ``Get-PnpDeviceProperty`` works, but goes through CIM and
  measured **64 seconds** on this machine - unusable behind a spoken question.
* The same property in the registry (``HKLM\\SYSTEM\\CurrentControlSet\\Enum\\
  ...\\Properties``) is ACL'd to SYSTEM: reading it raises WinError 5 unless Era
  runs elevated, which she does not.

SetupAPI reads the same property from the live device node in ~20 ms without
elevation, so that is what this module wraps.

The battery does NOT live on the audio endpoint. The Star Glasses expose
``Star glass (CY03-CB36)`` as their render endpoint while the level sits on a
sibling node called ``CY03-CB36 Hands-Free AG`` - so a device is matched by the
hardware token shared by both names (``CY03-CB36``), never by endpoint identity.

Degrade, never raise: every public function returns a value, never an exception.
A machine with no battery-reporting device simply reports none.
"""

from __future__ import annotations

import ctypes
import re
import sys
import unicodedata
import uuid
from ctypes import wintypes

# DEVPKEY_Bluetooth_Battery {104EA319-6EE2-4701-BD47-8DDBF425BBE5}, 2 (byte, 0-100)
_PK_BATTERY = ("104ea319-6ee2-4701-bd47-8ddbf425bbe5", 2)
# DEVPKEY_NAME and DEVPKEY_Device_FriendlyName - tried in that order.
_PK_NAME = ("b725f130-47ef-101a-a5f1-02608c9eebac", 10)
_PK_FRIENDLY = ("a45c254e-df1c-4efd-8020-67d146a850e0", 14)

_DIGCF_PRESENT = 0x02
_DIGCF_ALLCLASSES = 0x04

# Noise words in an endpoint name that say nothing about WHICH hardware it is.
_STOPWORDS = {
    "hands",
    "free",
    "hands-free",
    "stereo",
    "headset",
    "headphones",
    "speakers",
    "audio",
    "output",
    "input",
    "casque",
    "ag",
    "avrcp",
    "transport",
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("d1", wintypes.DWORD),
        ("d2", wintypes.WORD),
        ("d3", wintypes.WORD),
        ("d4", ctypes.c_ubyte * 8),
    ]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulonglong)),
    ]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
    return g


def _key(spec: tuple[str, int]) -> _DEVPROPKEY:
    return _DEVPROPKEY(_guid(spec[0]), spec[1])


def _setupapi():
    """setupapi.dll with explicit signatures.

    The argtypes are not decoration: without an explicit ``restype``, ctypes
    truncates the 64-bit device-list HANDLE to an int and every enumeration
    silently returns zero devices.
    """
    api = ctypes.WinDLL("setupapi", use_last_error=True)
    api.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    api.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    api.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    api.SetupDiEnumDeviceInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(_SP_DEVINFO_DATA),
    ]
    api.SetupDiGetDevicePropertyW.restype = wintypes.BOOL
    api.SetupDiGetDevicePropertyW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_SP_DEVINFO_DATA),
        ctypes.POINTER(_DEVPROPKEY),
        ctypes.POINTER(wintypes.ULONG),
        ctypes.c_char_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    api.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    return api


def _read_string(api, h, did, spec: tuple[str, int]) -> str:
    buf = ctypes.create_string_buffer(1024)
    ptype, req = wintypes.ULONG(), wintypes.DWORD()
    pk = _key(spec)
    ok = api.SetupDiGetDevicePropertyW(
        h, ctypes.byref(did), ctypes.byref(pk), ctypes.byref(ptype), buf, 1024, ctypes.byref(req), 0
    )
    if not ok or not req.value:
        return ""
    try:
        return buf.raw[: req.value].decode("utf-16-le").rstrip("\x00").strip()
    except UnicodeDecodeError:
        return ""


def battery_devices() -> list[dict]:
    """Every present device reporting a battery level: [{'name', 'percent'}].

    One SetupAPI enumeration (~20 ms). Returns [] on any failure, and on any
    platform that is not Windows.
    """
    if not sys.platform.startswith("win"):
        return []
    try:
        api = _setupapi()
    except Exception as e:
        print(f"[AudioBattery] setupapi unavailable: {type(e).__name__}: {e}")
        return []

    h = None
    out: list[dict] = []
    try:
        h = api.SetupDiGetClassDevsW(None, None, None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES)
        if not h or h == wintypes.HANDLE(-1).value:
            return []
        did = _SP_DEVINFO_DATA()
        did.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        batt = _key(_PK_BATTERY)
        ptype, req = wintypes.ULONG(), wintypes.DWORD()
        buf = ctypes.create_string_buffer(8)
        index = 0
        while api.SetupDiEnumDeviceInfo(h, index, ctypes.byref(did)):
            index += 1
            ok = api.SetupDiGetDevicePropertyW(
                h,
                ctypes.byref(did),
                ctypes.byref(batt),
                ctypes.byref(ptype),
                buf,
                8,
                ctypes.byref(req),
                0,
            )
            if not ok or not req.value:
                continue
            level = buf.raw[0]
            if not 0 <= level <= 100:
                continue  # a driver reporting nonsense is not telemetry
            name = _read_string(api, h, did, _PK_NAME) or _read_string(api, h, did, _PK_FRIENDLY)
            out.append({"name": name, "percent": int(level)})
    except Exception as e:  # degrade, never raise
        print(f"[AudioBattery] enumeration failed: {type(e).__name__}: {e}")
    finally:
        if h:
            try:
                api.SetupDiDestroyDeviceInfoList(h)
            except Exception:
                pass
    return out


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def hardware_tokens(device_name: str) -> set[str]:
    """Tokens that identify the HARDWARE behind an endpoint name.

    ``Star glass (CY03-CB36)`` -> {'star', 'glass', 'cy03', 'cb36'}, minus the
    words that describe a ROLE rather than a device ('hands free', 'headset').
    Two-character tokens are dropped: they match far too much.
    """
    return {t for t in _norm(device_name).split() if len(t) > 2 and t not in _STOPWORDS}


def battery_for(device_name: str) -> dict | None:
    """Battery for the hardware behind ``device_name``, or None.

    Matching is by shared hardware token because the endpoint and the node that
    carries the battery have DIFFERENT names (see the module docstring). The
    best match is the candidate sharing the most tokens; a tie is treated as no
    match rather than a guess, since naming the wrong earbud's battery is worse
    than saying it is unavailable.
    """
    want = hardware_tokens(device_name)
    if not want:
        return None
    scored = []
    for row in battery_devices():
        shared = want & hardware_tokens(row.get("name", ""))
        if shared:
            scored.append((len(shared), row))
    if not scored:
        return None
    scored.sort(key=lambda s: s[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # ambiguous: two devices match equally well
    return scored[0][1]
