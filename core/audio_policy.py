import os
import re
import sys
import threading
import uuid  # New import for generating unique identifiers

try:  # Windows-only; the module stays importable everywhere
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]

_HKLM_MMDEVICES = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
_HKCU_APP_PREFS = (
    r"Software\Microsoft\Internet Explorer\LowRegistry\Audio\PolicyConfig\PropertyStore"
)
# The device-interface path an app preference names, as MMDevices spells it.
_IFACE_VALUE = "{233164c8-1b2c-4c7d-bc68-b671687a2567},1"
_SUBKEY = {"output": "Render", "input": "Capture"}
_ROLE_INDEX = {"console": 0, "multimedia": 1, "communications": 2}
_DEVICE_STATE_ACTIVE = 1

# Reentrant on purpose: the public entry points nest (set_app_override calls
# clear_app_override, which takes the lock itself to drop the cache). A plain
# Lock deadlocks there, and behind @_never_raises the thread just stops without
# so much as an exception - on an Era tool call that is an executor thread that
# never answers. core/user_prefs uses an RLock for the same reason.
_lock = threading.RLock()
_cache: dict = {}  # {"stamp": <prefs last write>, "pins": {flow: guid}}


def guid_tail(endpoint_id: str) -> str:
    """'{0.0.0.00000000}.{abc-...}' -> '{abc-...}'. Bare guids pass through."""
    m = re.search(r"(\{[0-9a-fA-F-]{36}\})\s*$", endpoint_id or "")
    return m.group(1).lower() if m else (endpoint_id or "").strip().lower()


# -- the system's own ranking -------------------------------------------------


def _endpoint_key(flow: str, guid: str, sub: str = ""):
    path = rf"{_HKLM_MMDEVICES}\{_SUBKEY[flow]}\{guid}"
    if sub:
        path = path + "\\" + sub
    return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)


def _value(key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _never_raises(default):
    """The module contract (see the header) stated in code, not just in prose.

    The OSError guards inside these functions do not cover everything the
    registry can hand back - a REG_BINARY where a string was expected raises
    TypeError, which used to escape into the caller's generic "I couldn't reach
    that audio device" and misreport the cause.
    """

    def wrap(fn):
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:
                print(f"[AudioPolicy] {fn.__name__} failed: {type(e).__name__}: {e}")
                return default() if callable(default) else default

        inner.__name__, inner.__doc__ = fn.__name__, fn.__doc__
        return inner

    return wrap


@_never_raises(None)
def ranked_default_guid(flow: str, role: str, candidate_ids) -> str | None:
    """The endpoint the SYSTEM ranks highest for ``role``, among ``candidate_ids``.

    Windows bumps ``Level:<role>`` every time an endpoint is made default; the
    highest-ranked live endpoint is the default. Unlike GetDefaultAudioEndpoint
    this is not rewritten by a per-app preference. Returns a bare GUID, or None
    when nothing is rankable (then the caller keeps the API's answer).
    """
    if winreg is None or flow not in _SUBKEY or role not in _ROLE_INDEX:
        return None
    name = f"Level:{_ROLE_INDEX[role]}"
    best, best_level = None, -1
    for endpoint_id in candidate_ids or ():
        guid = guid_tail(endpoint_id)
        try:
            with _endpoint_key(flow, guid) as key:
                if _value(key, "DeviceState") != _DEVICE_STATE_ACTIVE:
                    continue
                level = _value(key, name)
        except OSError:
            continue
        if isinstance(level, int) and level > best_level:
            best, best_level = guid, level
    return best


# -- the per-application override ---------------------------------------------


def _dos_device_map() -> dict[str, str]:
    r"""{'\device\harddiskvolume3': 'c:'} - app preferences store NT paths."""
    import ctypes

    out: dict[str, str] = {}
    buf = ctypes.create_unicode_buffer(1024)
    for letter in "abcdefghijklmnopqrstuvwxyz":
        drive = letter + ":"
        try:
            if ctypes.windll.kernel32.QueryDosDeviceW(drive, buf, len(buf)):
                out[os.path.normcase(buf.value)] = drive
        except OSError:
            continue
    return out


def _this_exe_paths() -> tuple[str, ...]:
    """Our executable, in every spelling an app preference might use.

    ``sys.executable`` is tested BEFORE abspath: abspath("") is the working
    directory, not "", so a frozen/embedded host with no executable would
    otherwise silently start matching preferences against a folder path.
    """
    if not (sys.executable or "").strip():
        return ()
    exe = os.path.normcase(os.path.abspath(sys.executable))
    if not exe:
        return ()
    drive, tail = os.path.splitdrive(exe)
    forms = {exe}
    for nt_path, letter in _dos_device_map().items():
        if letter == drive:
            forms.add(nt_path + tail)
    return tuple(forms)


def _iface_signature(path: str) -> str | None:
    r"""Normalise a device-interface path so Windows' two spellings of it match.

    MMDevices stores   ``{2}.\\?\bthenum#<dev>#<instance>#{cls}\src``
    an app preference  ``{2}.\\?\bthenum#<dev>#{cls}\src/00010001``

    - the hardware instance is dropped, a pin suffix is added, and the pin name
    itself can differ between the two. Enumerator + device id identify the
    hardware, which is all this needs to decide.
    """
    if not path:
        return None
    if isinstance(path, (bytes, bytearray)):  # REG_BINARY: not ours to interpret
        return None
    parts = os.path.normcase(str(path)).split("#")
    return "#".join(parts[:2]) if len(parts) >= 2 else None


def _endpoint_signatures(flow: str) -> dict[str, list[str]]:
    """{interface signature: [endpoint guid, ...]} for every endpoint of one flow.

    A signature is enumerator + device id, so two headsets of the SAME model
    share one. The candidates are kept as a list rather than silently resolved
    by enumeration order - the caller must not name a device it cannot tell
    apart from another.
    """
    sigs: dict[str, list[str]] = {}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, rf"{_HKLM_MMDEVICES}\{_SUBKEY[flow]}"
        ) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    guid = winreg.EnumKey(root, i)
                    with _endpoint_key(flow, guid, "Properties") as props:
                        sig = _iface_signature(_value(props, _IFACE_VALUE) or "")
                except OSError:
                    continue
                if sig:
                    sigs.setdefault(sig, []).append(guid.lower())
    except OSError:
        return {}
    return sigs


def _app_pref_entries() -> list[tuple[str, str]]:
    """[(subkey name, endpoint interface path)] for entries naming THIS exe."""
    exes = _this_exe_paths()
    if not exes:
        return []
    found: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _HKCU_APP_PREFS) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    name = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, name) as entry:
                        raw = str(winreg.QueryValueEx(entry, "")[0])
                except OSError:
                    continue
                endpoint, sep, app = raw.partition("|")
                if not sep:
                    continue
                # The stored app path might contain %b<PID>, so we strip it for matching.
                if os.path.normcase(app.split("%b")[0]) in exes:
                    found.append((name, endpoint))
    except OSError:
        return []
    return found


def _read_pins() -> dict[str, list[str]]:
    """{flow: [candidate endpoint guid, ...]} this process is pinned to.

    A list because one interface signature can cover two devices of the same
    model. Detection ("is a pin in force?") only needs the list to be non-empty;
    naming the device needs it to hold exactly one.
    """
    entries = _app_pref_entries()
    if not entries:
        return {}
    pins: dict[str, list[str]] = {}
    for flow in _SUBKEY:
        sigs = _endpoint_signatures(flow)
        for _, endpoint in entries:
            sig = _iface_signature(endpoint)
            if sig and sig in sigs:
                pins.setdefault(flow, list(sigs[sig]))
    return pins


def _prefs_stamp():
    """Last-write time of the preference store - the cache key. None = unreadable."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _HKCU_APP_PREFS) as root:
            info = winreg.QueryInfoKey(root)
            return (info[0], info[2])  # (subkey count, last write time)
    except OSError:
        return None


def pinned_endpoint_guids(flow: str) -> list[str]:
    """Every endpoint THIS PROCESS could be pinned to for ``flow``; [] if free.

    Re-read whenever the preference store changes: the cache key is the store's
    own last-write time, so the answer is never stale, and the common path costs
    one registry open rather than a full scan.
    """
    if winreg is None or flow not in _SUBKEY:
        return []
    try:
        stamp = _prefs_stamp()
        if stamp is None:
            return []
        with _lock:
            if _cache.get("stamp") != stamp:
                _cache["stamp"], _cache["pins"] = stamp, _read_pins()
            return list(_cache["pins"].get(flow) or ())
    except Exception as e:  # degrade, never raise
        print(f"[AudioPolicy] pin lookup failed: {type(e).__name__}: {e}")
        return []


def pinned_endpoint_guid(flow: str) -> str | None:
    """The pinned endpoint, but ONLY when it can be told apart from its twins.

    Returns None both when nothing is pinned and when the signature matches
    several same-model devices - use ``pinned_endpoint_guids`` to ask whether a
    pin exists at all, and never name a device this returned None for.
    """
    guids = pinned_endpoint_guids(flow)
    return guids[0] if len(guids) == 1 else None


def has_app_override(flow: str) -> bool:
    return bool(pinned_endpoint_guids(flow))


def _delete_pref(name: str) -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _HKCU_APP_PREFS, 0, winreg.KEY_ALL_ACCESS
    ) as root:
        winreg.DeleteKey(root, name)


@_never_raises(lambda: (0, ["unexpected failure"]))
def clear_app_override(flow: str | None = None) -> tuple[int, list[str]]:
    """Delete this executable's app-preference entries. Returns (removed, errors).

    ``flow`` limits removal to one direction; None removes every entry naming
    this executable. This is what the Volume mixer's "Reset" does - an HKCU
    change that only ever touches entries whose app path IS ours.

    An unusable scope REFUSES rather than widening: if a direction was asked for
    and its endpoints cannot be enumerated, deleting everything would silently
    destroy the other direction's preference too.
    """
    if winreg is None:
        return 0, ["not Windows"]
    if flow is not None and flow not in _SUBKEY:
        return 0, [f"unknown audio direction {flow!r}"]
    keep_sigs: set[str] = set()
    if flow is not None:
        keep_sigs = set(_endpoint_signatures(flow))
        if not keep_sigs:
            return 0, [f"cannot scope the removal to {flow} - refusing to clear everything"]
    entries = _app_pref_entries()
    if not entries:
        return 0, []
    removed, errors = 0, []
    for name, endpoint in entries:
        if flow is not None and _iface_signature(endpoint) not in keep_sigs:
            continue
        try:
            _delete_pref(name)
            removed += 1
        except OSError as e:
            errors.append(f"{name}: {e}")
    with _lock:
        _cache.clear()  # Clear cache after any modification
    return removed, errors


# --- New functions for setting app overrides ---


@_never_raises(None)
def _get_interface_path_for_guid(flow: str, guid: str) -> str | None:
    """Gets the full device-interface path for a given flow and GUID."""
    if winreg is None or flow not in _SUBKEY:
        return None
    try:
        guid_lower = guid.lower()
        # To get the interface path, we need to open the "Properties" subkey of the endpoint.
        with _endpoint_key(flow, guid_lower, "Properties") as props:
            return _value(props, _IFACE_VALUE)
    except OSError:
        return None


@_never_raises(lambda: (0, ["unexpected failure"]))
def set_app_override(flow: str, target_guid: str) -> tuple[int, list[str]]:
    """Sets this executable's app-preference entry for `flow` to `target_guid`.

    Returns (changed, errors), where ``changed`` counts the preference this call
    WROTE - never the stale entries it cleared on the way. A successful call
    reports 1 whatever the previous state was: "I made two preferences" is a
    different claim from "I replaced one", and only the second is ever true.
    The removals are printed instead, so they stay visible without inflating
    the total the caller reports back to the user.
    """
    if winreg is None:
        return 0, ["not Windows"]
    if flow not in _SUBKEY:
        return 0, [f"unknown audio direction {flow!r}"]

    # 1. Get the interface path for the target GUID
    interface_path = _get_interface_path_for_guid(flow, target_guid)
    if not interface_path:
        return 0, [f"could not find interface path for target_guid {target_guid}"]

    # 2. Get the executable path for this process. _this_exe_paths is the guard
    # ("can we identify ourselves at all?"); it must NOT be the source of the
    # path we write. It returns the contents of a set, so its order is
    # arbitrary and half its entries are \device\ NT paths - indexing it would
    # make the value written to the registry differ run to run. The preference
    # store wants the drive-letter spelling, which is what abspath gives us.
    if not _this_exe_paths():
        return 0, ["could not determine executable path"]
    exe_path_for_pref = os.path.normcase(os.path.abspath(sys.executable))

    # 3. Construct the registry value string
    # The format is "{device_interface_path}|{executable_path}".
    # Windows might append %b<PID>, but we write the base path.
    reg_value = f"{interface_path}|{exe_path_for_pref}"

    changed = 0
    errors: list[str] = []

    # 4. Clear any existing override for this flow and executable, so we do not
    # leave conflicting entries behind. This runs OUTSIDE _lock: the lock
    # guards _cache, not the registry, and clear_app_override takes it itself.
    removed_count, clear_errors = clear_app_override(flow)
    errors.extend(clear_errors)
    if clear_errors:
        print(f"[AudioPolicy] Errors during pre-setting clear: {clear_errors}")
    if removed_count:
        print(f"[AudioPolicy] replaced {removed_count} existing {flow} preference(s)")

    try:
        # 5. Create a new registry entry under a new unique GUID subkey
        new_subkey_name = str(uuid.uuid4()).upper()

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _HKCU_APP_PREFS, 0, winreg.KEY_ALL_ACCESS
        ) as root:
            with winreg.CreateKey(root, new_subkey_name) as new_key:
                # Write the default (empty string) value for the subkey
                winreg.SetValueEx(new_key, "", 0, winreg.REG_SZ, reg_value)
                changed = 1

    except OSError as e:
        errors.append(f"Failed to set preference (OSError): {e}")
    except Exception as e:  # Catch any other unexpected exceptions
        errors.append(
            f"An unexpected error occurred while setting preference: {type(e).__name__}: {e}"
        )
    finally:
        # A failed write can still have created the subkey, so the cache is
        # invalidated on every path out - once, at the end.
        with _lock:
            _cache.clear()

    return changed, errors
