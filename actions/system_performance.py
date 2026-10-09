# actions/system_performance.py
"""MSI Center user-scenario switching and Microsoft PC Manager "PC Boost".

Both vendor apps are driven through UI Automation (pywinauto, UIA backend)
because neither exposes a public CLI or deep-link for these functions.

There IS a direct path — write MSI's own scenario key and make its service
re-read it — and we take it whenever we can, because it is instant and never
steals the user's screen. On this machine we can't: see "the direct path"
below. It is probed, not assumed, so the cost of not having it is one failed
OpenKey rather than a failed write plus a failed service restart per switch.

MSI Center: the User Scenario radios apply their scenario from a real mouse
Click, NOT from the checked-state change. UIA's SelectionItem.Select() (and
LegacyIAccessible.DoDefaultAction(), and posted WM_LBUTTONDOWN/UP) all flip
the radio's checkmark while the app never runs its apply handler — verified
2026-09-20 on the GL76: after Select() the UI read "Balanced" while the
machine was still in Super Battery, and is_selected() cheerfully returned
True, so we used to report a switch that never happened. Only a synthesized
physical click (SendInput, i.e. click_input) fires the handler. We therefore
click for real and verify against MSI's own registry state rather than
against the checkmark, which lies.

The User Scenario radio group carries language-independent
automation IDs whose *stem* is stable across app updates but whose suffix
is not — an update re-emits them with numeric / "_Copy" suffixes (observed
2026-09 on Pulse GL76: Smart_Auto1 = AI Engine, Performance_Mode_Copy1 =
Extreme Performance, Balanced_Mode_Copy1 = Balanced, Battery_Mode1 = Super
Battery). We therefore match on the stem as a substring, with the localized
control name as a fallback, so a suffix change never breaks us.

Two states legitimately hide a scenario even when the window is open:
  * Power: AI Engine and Extreme Performance are removed by MSI Center when
    the machine is on battery — only Balanced / Silent / Super Battery show.
  * Model: Silent_Mode is not present on every chassis (GL76 has no Silent).
And one state hides *all* controls: a minimized MSI Center window that the
OS has suspended exposes only its frame to UIA, so we restore/foreground the
window before reading. On failure we log the full radio inventory and tell
the user which scenarios ARE available rather than a dead-end "not found".

PC Manager: the UI is a WebView2 page whose Chromium accessibility tree is
dormant until a WM_GETOBJECT poke hits the Chrome_RenderWidgetHostHWND child
window; after that the Home tab exposes the "Boost" button to UIA.
"""

import ctypes
import subprocess
import time

from core.utils import is_windows, log
from core.win_subprocess import NO_WINDOW

try:  # Windows-only; the module still imports (and no-ops) elsewhere.
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None

_MSI_CENTER_AUMID = r"shell:appsFolder\9426MICRO-STARINTERNATION.MSICenter_kzh8wxbdkxb8p!App"
_PC_MANAGER_URI = "ms-pcmanager:"

# canonical scenario -> (automation_id regex tokens, localized name tokens)
_SCENARIOS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "extreme_performance": (("performance_mode",), ("extreme", "extrême", "performances")),
    "balanced": (("balanced_mode",), ("balanced", "équilibré", "equilibre")),
    "silent": (("silent_mode",), ("silent", "silencieux")),
    "super_battery": (("battery_mode",), ("battery", "batterie", "eco")),
    "ai_engine": (("smart_auto",), ("ai engine", "ai auto")),
}

# How long to keep re-reading the tree while MSI Center renders its scenario
# panel. Lowered by the tests so they do not sit through a real 25s scan.
_SCENARIO_SCAN_TIMEOUT = 25.0

# How long to wait for MSI's service to write the applied scenario back.
# Observed 3-5s on a GL76; 12s leaves room for a busy machine.
_SCENARIO_VERIFY_TIMEOUT = 12.0

_SCENARIO_ALIASES: dict[str, str] = {
    "extreme": "extreme_performance",
    "performance": "extreme_performance",
    "extreme_performance": "extreme_performance",
    "high_performance": "extreme_performance",
    "turbo": "extreme_performance",
    "gaming": "extreme_performance",
    "balanced": "balanced",
    "balance": "balanced",
    "normal": "balanced",
    "silent": "silent",
    "quiet": "silent",
    "super_battery": "super_battery",
    "battery": "super_battery",
    "battery_saver": "super_battery",
    "eco": "super_battery",
    "ai_engine": "ai_engine",
    "ai": "ai_engine",
    "auto": "ai_engine",
    "smart": "ai_engine",
}


# --- ground truth -----------------------------------------------------------
# MSI Center's service (MSIAPService) writes the applied scenario here a beat
# after the UI click; the radio's own checkmark does NOT imply it was applied.
#   Mode        = resolved scenario code (see _SCENARIO_MODE_CODES)
#   Intelligent = 1 when AI Engine is driving, and Mode then holds whatever
#                 scenario AI Engine currently resolved to.
_SCENARIO_KEY = r"SOFTWARE\MSI\MSI Center\Component\Base Module\User Scenario"

# Codes observed on a GL76 (Pulse) running MSI Center 2.0.97: switching to a
# scenario and reading Mode back. Silent isn't offered on this chassis, so its
# code is unverified — None means "verify by state change instead".
_SCENARIO_MODE_CODES: dict[str, int | None] = {
    "extreme_performance": 1,
    "balanced": 2,
    "silent": None,  # Silent mode has no verifiable code on this specific chassis.
    "super_battery": 4,
}


# --- the direct path (DISABLED — the key is output, not input) --------------
# Measured 2026-09-21, after granting this account SetValue on the key so the
# write actually lands: writing Mode=2/Intelligent=0 over a live AI Engine
# state left MSI Center's own UI still showing AI Engine selected, and the
# service did not revert us within the settle window. The key is a mirror the
# service *writes*, not a channel it reads. Writing it therefore changes
# nothing on the machine and silently desyncs the one signal we verify
# switches against — strictly worse than not having it.
#
# Note what this defeats: "we wrote it and the service didn't take it back"
# looked like evidence and isn't, because the service never looks at the key
# at all. The only independent signal is MSI Center's own UI, and the radio
# checkmark can't confirm a switch either (see the module docstring) — it can
# only *falsify* one. So the direct write has no verification available to it,
# and an unverifiable switch is exactly what this module exists to refuse.
#
# Untested: whether the service reads the key at *startup*, i.e. whether a
# write plus a service restart would apply. That needs admin per switch, which
# is the cost the direct path was meant to avoid. If it is ever shown to work,
# flip this flag back on — everything below still works.
_DIRECT_PATH_ENABLED = False

# --- direct-path machinery --------------------------------------------------
# Writing the key above and making the service re-read it switches the
# scenario with no window, no click and no stolen focus. Whether we can do it
# is a property of the process, not of the request, and on this machine the
# answer is no — verified 2026-09-20 on the GL76:
#   * HKLM grants BUILTIN\Users only ReadKey on the User Scenario key, and Era
#     runs unelevated, so SetValueEx fails with ERROR_ACCESS_DENIED (5).
#   * Bouncing the service needs administrator rights too.
#   * root\WMI's MSI_ACPI (Set_EC / Set_Data, the EC channel the service uses)
#     is likewise access-denied unelevated, so there is no side door.
# The non-elevated channel MSI Center's own UI uses is the \\.\pipe\MSI_SERVICE_2
# named pipe — reachable, but an undocumented protocol fronting fan and EC
# control, so we don't guess at it.
# Net effect: run Era elevated and switches become instant; run it as now and
# we pay one failed OpenKey before going to the UI. What we must never do is
# the first attempt's mistake — assume the write landed and report a switch
# that never happened.
#
# Answers that are properties of the installation, not of the call: which
# registry view holds the key, whether we may write it, what the service is
# called. Probed once, then free on every subsequent switch.
_DIRECT_CACHE: dict = {}

# `subprocess` here runs console tools on a machine whose user is usually
# mid-task; without this every probe flashes a black window at them.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The service that owns the scenario state. "MSIAPService" is the *process*
# name (MSIAPService.exe) — `net stop MSIAPService` fails with "the service
# name is invalid", which is one of the two reasons the first direct attempt
# silently did nothing. Resolved by probing, so an MSI Center update that
# renames it falls through to the next candidate instead of stranding us.
_MSI_SERVICE_CANDIDATES = ("MSI Foundation Service", "MSI_Center_Service")

# Written together: ModeCH mirrors Mode on the GL76 (both read 1 under Extreme
# Performance). Mirrors we don't know about are left alone — the service
# rewrites what it owns.
_SCENARIO_MODE_VALUES = ("Mode", "ModeCH")

# How long to let the service notice the key on its own before we bounce it.
# A restart briefly drops MSI's EC/fan control, so it is the second resort.
_SCENARIO_SETTLE_TIMEOUT = 3.0


def _scenario_key_view() -> int | None:
    """The WOW64 access flag of the registry view that actually holds the User
    Scenario key, or None if neither does.

    The key lives *only* in the 32-bit view (WOW6432Node); asking for it
    through the 64-bit view raises ERROR_FILE_NOT_FOUND, not access-denied,
    which is easy to misread as "MSI Center isn't installed"."""
    if winreg is None:
        return None
    if "view" not in _DIRECT_CACHE:
        found = None
        for flag in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, _SCENARIO_KEY, 0, winreg.KEY_READ | flag
                ).Close()
            except OSError:
                continue
            found = flag
            break
        _DIRECT_CACHE["view"] = found
    return _DIRECT_CACHE["view"]


def _scenario_key_writable() -> bool:
    """Can *this* process write the key?

    Out of the box, no: HKLM grants BUILTIN\\Users only ReadKey on it and Era
    runs unelevated, so the write needs administrator rights. Granting this
    account SetValue on the key lifts that, which is the one thing standing
    between us and instant switching.

    Deliberately NOT memoized, unlike the view and the service name. Those are
    properties of the installation; this one changes underneath a long-running
    Era — the moment the grant is applied, and again if an MSI Center update
    ever resets it. Caching it would mean a relaunch to notice either way, and
    the probe is a single OpenKey per switch."""
    view = _scenario_key_view()
    if view is None:
        return False
    try:
        winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _SCENARIO_KEY, 0, winreg.KEY_SET_VALUE | view
        ).Close()
    except OSError:
        return False
    return True


def _msi_service_name() -> str | None:
    """The installed name of MSI's scenario service, or None if none of the
    known names exist. `sc query` returns 1060 for a service that isn't
    installed, so we decide on the exit code rather than on localized text."""
    if "service" not in _DIRECT_CACHE:
        found = None
        for name in _MSI_SERVICE_CANDIDATES:
            try:
                done = subprocess.run(
                    ["sc.exe", "query", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=_NO_WINDOW,
                )
            except Exception:
                continue
            if done.returncode == 0:
                found = name
                break
        _DIRECT_CACHE["service"] = found
    return _DIRECT_CACHE["service"]


def _restart_msi_service() -> str | None:
    """Bounce the service so it re-reads the key. Returns an error string, or
    None if it stopped and started again."""
    name = _msi_service_name()
    if not name:
        return "MSI's scenario service isn't installed under a name I know"
    for verb, settle in (("stop", 1.0), ("start", 3.0)):
        try:
            done = subprocess.run(
                ["net", verb, name],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return f'`net {verb} "{name}"` timed out'
        except Exception as e:
            return f'`net {verb} "{name}"` failed ({e})'
        if done.returncode != 0:
            lines = [
                ln.strip() for ln in (done.stderr or done.stdout or "").splitlines() if ln.strip()
            ]
            return f'`net {verb} "{name}"` failed: {lines[-1] if lines else done.returncode}'
        time.sleep(settle)
    return None


def _write_scenario_values(mode: int, intelligent: int) -> str | None:
    """Write MSI's scenario state. Returns an error string, or None."""
    view = _scenario_key_view()
    if view is None:
        return "MSI Center's scenario key is not present"
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _SCENARIO_KEY, 0, winreg.KEY_SET_VALUE | view
        )
    except OSError as e:
        return f"cannot open the scenario key for writing ({e})"
    try:
        winreg.SetValueEx(key, "Intelligent", 0, winreg.REG_DWORD, intelligent)
        for name in _SCENARIO_MODE_VALUES:
            try:
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, mode)
            except OSError:
                # A mirror this installation doesn't have is not a failure;
                # only Mode itself is required.
                if name == "Mode":
                    raise
    except OSError as e:
        return f"the scenario key rejected the write ({e})"
    finally:
        key.Close()
    return None


def _set_msi_scenario_direct(canonical: str, before: tuple[int, int] | None) -> bool:
    """Apply `canonical` by writing MSI's scenario state and making its service
    take it, skipping the UI entirely. True only when the state then reads back
    as the requested scenario *and* differs from `before`.

    On verification: reading back a value we just wrote would confirm our own
    write, not the machine, so it is only half the check. The half that carries
    weight is that the value is still ours after the service has settled (and,
    if it had to be bounced, after it restarted and rewrote what it owns) — a
    service that disagreed would have reverted us. That is MSI's state store,
    not an EC read; we claim exactly that much and no more."""
    if not _DIRECT_PATH_ENABLED:
        return False
    if winreg is None or not _scenario_key_writable():
        log(
            "SystemPerformance",
            "Direct scenario write unavailable (the key is read-only for this "
            "process — Era is not elevated); using the UI.",
        )
        return False

    if canonical == "ai_engine":
        # AI Engine is a flag, not a mode: it keeps resolving Mode itself, so
        # we set Intelligent and leave Mode where the service last put it.
        mode = before[0] if before else _SCENARIO_MODE_CODES["balanced"]
        intelligent = 1
    else:
        mode = _SCENARIO_MODE_CODES.get(canonical)
        if mode is None:
            # Silent: no verified code on this chassis, so a write would be a
            # guess we could not check. The UI click is the honest route.
            log(
                "SystemPerformance",
                f"No verified Mode code for '{canonical}'; using the UI rather than guessing.",
            )
            return False
        intelligent = 0

    error = _write_scenario_values(mode, intelligent)
    if error:
        log("SystemPerformance", f"Direct scenario write failed: {error}.")
        return False

    # The service usually picks the key up on its own; bouncing it is the
    # second resort because a restart briefly drops MSI's EC/fan control.
    if _confirm_direct(canonical, before, _SCENARIO_SETTLE_TIMEOUT):
        return True
    log(
        "SystemPerformance",
        f"Direct write of '{canonical}' didn't settle; restarting MSI's service.",
    )
    error = _restart_msi_service()
    if error:
        log("SystemPerformance", f"Could not restart MSI's scenario service: {error}.")
        return False
    return _confirm_direct(canonical, before, _SCENARIO_SETTLE_TIMEOUT)


def _confirm_direct(canonical: str, before: tuple[int, int] | None, settle: float) -> bool:
    """Did our write survive the service having its say?

    The settle sleep is the whole point: the key reads back as `canonical` the
    instant we write it, so an immediate read confirms nothing but our own
    SetValueEx. Only a read taken after the service has had time to disagree —
    and it does revert writes it doesn't accept — carries any information. We
    also require the state to have moved off `before`, so a write that changed
    nothing can never be reported as a switch."""
    if settle > 0:
        time.sleep(settle)
    state = _scenario_state()
    return _state_matches(canonical, state) and state != before


def _scenario_state() -> tuple[int, int] | None:
    """(Mode, Intelligent) as MSI Center's service last wrote them, or None if
    the key is unreadable (different model / MSI Center not installed)."""
    view = _scenario_key_view()
    if view is None:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SCENARIO_KEY, 0, winreg.KEY_READ | view)
    except OSError:
        return None
    try:
        return (
            int(winreg.QueryValueEx(key, "Mode")[0]),
            int(winreg.QueryValueEx(key, "Intelligent")[0]),
        )
    except (OSError, ValueError, TypeError):
        return None
    finally:
        key.Close()


def _state_matches(canonical: str, state: tuple[int, int] | None) -> bool:
    """Is `state` the machine actually running `canonical`?"""
    if state is None:
        return False
    mode, intelligent = state
    if canonical == "ai_engine":
        return intelligent == 1
    if intelligent == 1:
        return False  # AI Engine is driving, so no fixed scenario is in force
    expected = _SCENARIO_MODE_CODES.get(canonical)
    return expected is not None and mode == expected


def _await_scenario(canonical: str, before: tuple[int, int] | None, timeout: float = 12.0) -> bool:
    """Poll MSI's own state until it reflects the scenario we clicked.

    For a scenario whose code we know we require an exact match. For one we
    don't (Silent), any move away from the pre-click state counts — the click
    demonstrably reached the app, which is the thing select() never did."""
    known = canonical == "ai_engine" or _SCENARIO_MODE_CODES.get(canonical) is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _scenario_state()
        if known:
            if _state_matches(canonical, state):
                return True
        elif state is not None and state != before:
            return True
        time.sleep(0.5)
    return False


_MODE_NAMES = {1: "Extreme Performance", 2: "Balanced", 4: "Super Battery"}


class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _on_ac_power() -> bool | None:
    """True on mains, False on battery, None if Windows won't say. Worth
    reporting alongside the scenario: MSI Center removes AI Engine and Extreme
    Performance on battery, so this is the difference between "that scenario
    is unavailable right now" and "something is broken"."""
    status = _PowerStatus()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
    except Exception:
        return None
    return {0: False, 1: True}.get(int(status.ACLineStatus))


def msi_scenario_status() -> str:
    """Report the active scenario without opening or touching anything.

    This is the one thing the registry key is genuinely good for. It is no use
    as an *input* — writing it applies nothing (see "the direct path") — but
    the service writes it to report what it applied, so as an *output* it is
    authoritative and instant. No window, no click, no stolen focus."""
    if not is_windows():
        return "MSI Center control is Windows-only."

    state = _scenario_state()
    if state is None:
        return "I can't read MSI Center's scenario state on this machine, sir."

    mode, intelligent = state
    resolved = _MODE_NAMES.get(mode, f"an unrecognised mode ({mode})")
    if intelligent == 1:
        headline = f"AI Engine is driving, currently sitting on {resolved}"
    else:
        headline = f"The scenario is {resolved}"

    on_ac = _on_ac_power()
    if on_ac is True:
        power = " — on mains power, so every scenario is available"
    elif on_ac is False:
        power = " — on battery, so AI Engine and Extreme Performance are hidden"
    else:
        power = ""
    return f"{headline}{power}, sir."


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos() -> tuple[int, int] | None:
    pt = _Point()
    try:
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:
        pass
    return None


def _restore_cursor(pos: tuple[int, int] | None) -> None:
    if not pos:
        return
    try:
        ctypes.windll.user32.SetCursorPos(int(pos[0]), int(pos[1]))
    except Exception:
        pass


def _click_for_real(target) -> str | None:
    """Synthesize a physical click on the radio. Returns an error string, or
    None if the click went out. Nothing cheaper works: select(),
    DoDefaultAction() and posted mouse messages all flip the checkmark and
    leave MSI Center's apply handler unrun."""
    try:
        rect = target.rectangle()
    except Exception as e:
        return f"could not locate the control on screen ({e})"
    if rect.width() <= 0 or rect.height() <= 0:
        return "the control is collapsed or off-screen"

    saved = _cursor_pos()
    try:
        # The click must land on a real, focused window — a background click
        # would hit whatever is on top at those coordinates. click_input()
        # focuses the window itself, but MSI Center's frame needs the nudge
        # first or the first click just activates it.
        try:
            target.set_focus()
            # Let the foreground transition finish: a click that lands mid-switch
            # only activates the window and is swallowed, costing us a retry.
            time.sleep(0.4)
        except Exception:
            pass
        target.click_input()
    except Exception as e:
        return f"the click failed ({e})"
    finally:
        # Put the pointer back so a scenario switch doesn't yank the user's
        # cursor onto whichever monitor MSI Center happens to live on.
        _restore_cursor(saved)
    return None


def _co_init() -> None:
    """UIA runs over COM; the executor thread may not have it initialized."""
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass


# Owning-process allowlists so a look-alike window title can never receive our
# clicks. MSI Center's UI window belongs to DCv2.exe; a sibling helper window
# (MSI.TerminalServer.exe) shares the same title and holds no controls.
_MSI_PROCS = ("dcv2.exe", "msi.terminalserver.exe", "msi center.exe", "applicationframehost.exe")
_PCM_PROCS = ("mspcmanager.exe", "applicationframehost.exe")


def _window_process_name(win) -> str | None:
    try:
        import psutil

        return psutil.Process(win.element_info.process_id).name().lower()
    except Exception:
        return None


def _find_windows(
    title_fragment: str, allowed_procs: tuple[str, ...], timeout: float = 5.0
) -> list:
    """All visible top-level windows matching the title AND owned by one of the
    expected processes. Windows whose process resolves to something else are
    rejected; unresolvable ones are kept (availability over strictness)."""
    from pywinauto import Desktop

    deadline = time.time() + timeout
    while True:
        matches = []
        for win in Desktop(backend="uia").windows():
            try:
                if title_fragment not in (win.window_text() or ""):
                    continue
            except Exception:
                continue
            proc = _window_process_name(win)
            if proc is not None and proc not in allowed_procs:
                log("SystemPerformance", f"Ignoring '{title_fragment}' window owned by {proc}")
                continue
            matches.append(win)
        if matches or time.time() >= deadline:
            return matches
        time.sleep(1.0)


def _normalize_scenario(value) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return None
    if raw in _SCENARIO_ALIASES:
        return _SCENARIO_ALIASES[raw]
    # Loose fallback: match any alias token inside the raw string.
    for alias, canonical in _SCENARIO_ALIASES.items():
        if alias in raw:
            return canonical
    return None


def _match_scenario_button(buttons, canonical: str):
    """Find the control for `canonical` by its id stem first, then by name.

    Both reads are wrapped: the full-tree fallback (see
    `_scenario_inventory_any`) hands us every control type, and a control that
    is mid-render raises rather than returning a value. An exception on one
    control must not lose the whole inventory — skip it and keep going.
    """
    id_tokens, name_tokens = _SCENARIOS[canonical]
    for btn in buttons:
        try:
            auto_id = (btn.element_info.automation_id or "").lower()
        except Exception:
            continue
        if any(tok in auto_id for tok in id_tokens):
            return btn
    for btn in buttons:
        try:
            name = (btn.window_text() or "").lower()
        except Exception:
            continue
        if any(tok in name for tok in name_tokens):
            return btn
    return None


def _scenario_inventory(buttons) -> dict:
    """Every canonical scenario currently visible in the tree -> its button.

    Lets us tell "this one scenario is unavailable right now" (UI readable,
    other scenarios present) apart from "the whole control group is missing"
    (window suspended / still loading)."""
    found = {}
    for canonical in _SCENARIOS:
        btn = _match_scenario_button(buttons, canonical)
        if btn is not None:
            found[canonical] = btn
    return found


def _scenario_inventory_any(win) -> dict:
    """Same as `_scenario_inventory`, but over the window's WHOLE tree.

    MSI Center's scenario tiles are RadioButtons on the chassis we measured,
    but an update can re-emit them as any control type — a custom XAML tile, a
    ToggleButton, a Button. Scanning only `control_type="RadioButton"` then
    returns an empty inventory and every switch reads as "controls not
    readable", which is exactly the symptom reported. The id/name tokens are
    what identify a scenario; the control type is not, so we stop requiring it
    and fall back to the full tree when the radio scan comes up empty.
    """
    try:
        all_controls = win.descendants()
    except Exception:
        return {}
    # Prefer radios when they're there — they're the unambiguous match — but
    # never let their absence become a hard failure.
    radios = [c for c in all_controls if _control_type_is(c, "RadioButton")]
    found = _scenario_inventory(radios)
    if found:
        return found
    return _scenario_inventory(all_controls)


def _control_type_is(control, expected: str) -> bool:
    """control_type is a method on the UIA element info, and a tolerant one:
    raising or returning None must not cost us the whole inventory."""
    try:
        return (control.element_info.control_type or "").lower() == expected.lower()
    except Exception:
        return False


def _wake_window(win) -> None:
    """Make a possibly-minimized/OS-suspended packaged-app window realize its
    content in the UIA tree. A minimized MSI Center window gets suspended after
    a while and then exposes only its frame — restoring/showing it resumes the
    app so the User Scenario radios re-enter the tree."""
    try:
        if win.is_minimized():
            win.restore()
    except Exception:
        pass
    try:
        win.set_focus()  # nudges a suspended UWP/WPF frame to render its content
    except Exception:
        pass


def _log_radio_inventory(wins) -> None:
    """Dump every radio button (id + name) across the candidate windows, so a
    future 'not found' has the real UI state on record instead of a dead end."""
    for i, win in enumerate(wins):
        try:
            rbs = win.descendants(control_type="RadioButton")
        except Exception as e:
            log("SystemPerformance", f"diag win[{i}] descendants failed: {e}")
            continue
        items = []
        for rb in rbs:
            try:
                items.append(f"{rb.element_info.automation_id!r}:{(rb.window_text() or '')!r}")
            except Exception:
                pass
        log("SystemPerformance", f"diag win[{i}] radios=[{', '.join(items)}]")


def _restore_window(win, should_minimize: bool) -> None:
    """Leave the window as we found it: minimize if we launched it, or if the
    user already had it minimized and we only woke it to read the controls.

    Clicking now forces MSI Center to the foreground, so tidying up afterwards
    matters more than it did when we only poked the UIA tree."""
    if not should_minimize:
        return
    try:
        win.minimize()
    except Exception:
        pass


def msi_set_scenario(scenario) -> str:
    """Switch the MSI Center user scenario (Extreme Performance / Balanced /
    Silent / Super Battery / AI Engine) via its sidebar radio group."""
    if not is_windows():
        return "MSI Center control is Windows-only."

    canonical = _normalize_scenario(scenario)
    if not canonical:
        options = ", ".join(sorted(_SCENARIOS))
        return f"Which scenario, sir? Options: {options}."

    # First, check current state before any attempts
    before = _scenario_state()
    if _state_matches(canonical, before):
        result = f"MSI scenario is already '{_SCENARIOS[canonical][1][0].title()}', sir."
        log("SystemPerformance", result)
        # No need to restore window if we didn't open/interact with UI
        return result

    # Direct first: it is instant and doesn't take the screen. It reports only
    # a verified switch, so an unverified one falls through to the UI rather
    # than being announced.
    if _set_msi_scenario_direct(canonical, before):
        result = f"MSI scenario switched to '{_SCENARIOS[canonical][1][0].title()}', sir."
        log("SystemPerformance", result)
        return result
    log(
        "SystemPerformance",
        f"Direct switch to '{canonical}' unavailable or unverified; using the UI.",
    )

    # --- Fallback to UI automation ---
    _co_init()

    wins = _find_windows("MSI Center", _MSI_PROCS, timeout=3.0)
    launched_here = False
    if not wins:
        try:
            subprocess.Popen(["explorer.exe", _MSI_CENTER_AUMID], creationflags=NO_WINDOW)
        except Exception as e:
            return f"Could not launch MSI Center: {e}"
        launched_here = True
        wins = _find_windows("MSI Center", _MSI_PROCS, timeout=45.0)
    if not wins:
        return "MSI Center did not open. Is it installed?"

    # The scenario radio group renders a few seconds after the window shows,
    # and only one of the same-titled MSI windows actually hosts it. A window
    # the OS has suspended (minimized for a while) shows only its frame, so we
    # wake each candidate before reading it.
    target = None
    win = wins[0]
    pre_minimized = False
    last_inventory: dict = {}
    deadline = time.time() + _SCENARIO_SCAN_TIMEOUT
    while time.time() < deadline and target is None:
        for candidate in wins:
            try:
                was_min = candidate.is_minimized()
            except Exception:
                was_min = False
            _wake_window(candidate)
            try:
                buttons = candidate.descendants(control_type="RadioButton")
            except Exception:
                continue
            # MSI's scenario tiles are RadioButtons on the chassis we measured,
            # but an update can re-emit them as any control type. Scanning only
            # radios then returns an empty inventory and every switch reads as
            # "controls not readable" — so fall back to the whole tree when the
            # radio scan comes up empty, and keep the radios when they're there.
            inventory = _scenario_inventory(buttons) or _scenario_inventory_any(candidate)
            if inventory:
                last_inventory = inventory
                win = candidate
                pre_minimized = was_min
            if canonical in inventory:
                target = inventory[canonical]
                win = candidate
                pre_minimized = was_min
                break
        if target is None:
            time.sleep(2.0)

    if target is None:
        _log_radio_inventory(wins)
        available = sorted(last_inventory)
        if available:
            # UI was readable and other scenarios are present, so this one is
            # genuinely unavailable in the current state rather than "broken".
            hint = ""
            if canonical in ("ai_engine", "extreme_performance"):
                hint = " AI Engine and Extreme Performance require AC power."
            elif canonical == "silent":
                hint = " Silent mode isn't offered on this model."
            return (
                f"The '{canonical}' scenario isn't available right now — MSI "
                f"Center is currently offering: {', '.join(available)}.{hint}"
            )
        return (
            "MSI Center is open but its User Scenario controls aren't readable "
            "yet (the window may still be loading or was suspended). I've woken "
            "it — please try once more, sir."
        )

    label = _control_label(target) or canonical

    # UI automation path. A click can fail to apply for reasons that have nothing
    # to do with the control itself — the window re-rendered, the focus moved,
    # the tile was re-emitted under a new automation id — so each attempt
    # re-acquires the control from the live tree rather than trusting the one
    # we found first. With no readable state there is nothing to wait for, so
    # one attempt.
    attempts = (1, 2, 3) if before is not None else (1,)
    error = None
    applied = False
    for attempt in attempts:
        fresh = _reacquire_control(win, canonical)
        # First attempt: the control was found moments ago, so a failed re-scan
        # (a transient UIA error) falls back to it. After a click that did not
        # apply, a control the live tree no longer has is GONE — clicking the
        # stale object again only lands on nothing. (`or target` on every
        # attempt made the "vanished" branch below unreachable.)
        if fresh is None and attempt > 1:
            target = None
        else:
            target = fresh or target
        if target is None:
            error = "the scenario control vanished from the UI before I could click it"
            log("SystemPerformance", f"UI click attempt {attempt}: {error}")
            break
        error = _click_for_real(target)
        if error:
            log("SystemPerformance", f"UI click attempt {attempt} failed: {error}")
            time.sleep(1.0)
            continue
        if before is None:
            break  # nothing to verify against; fall through to the UI check
        if _await_scenario(canonical, before, timeout=_SCENARIO_VERIFY_TIMEOUT):
            applied = True
            break
        log(
            "SystemPerformance",
            f"UI attempt {attempt}: no state change after clicking '{label}' — re-acquiring",
        )
        # The app may have re-rendered or the focus may have landed elsewhere;
        # the next iteration re-scans before trying again.
        time.sleep(1.0)

    if applied:
        result = f"MSI scenario switched to '{label}', sir."
    elif error:
        _restore_window(win, launched_here or pre_minimized)
        return f"Failed to select MSI scenario '{label}': {error}."
    elif before is None:
        # We clicked for real but can't read MSI's state on this machine, so
        # we fall back to the UI and say only what we actually know.
        time.sleep(1.5)  # let the radio settle before reading it back
        try:
            confirmed = target.is_selected()
        except Exception:
            confirmed = False
        result = (
            f"MSI scenario set to '{label}', sir — though I couldn't read MSI "
            f"Center's own state to confirm it took effect."
            if confirmed
            else f"I selected '{label}' but MSI Center did not confirm the switch."
        )
    else:
        result = (
            f"I selected '{label}' but MSI Center hasn't applied it — it's still "
            f"reporting the previous scenario."
        )

    _restore_window(win, launched_here or pre_minimized)
    log("SystemPerformance", result)
    return result


def _control_label(control) -> str | None:
    """The display name of a scenario control, read defensively.

    The full-tree fallback hands us control types whose `window_text()` raises,
    and a mid-render control returns nothing. Either way we still want a name
    for the report — fall back to the canonical scenario name rather than
    crashing the switch."""
    try:
        return control.window_text() or None
    except Exception:
        return None


def _reacquire_control(win, canonical: str):
    """Re-find `canonical`'s control in the live tree after a failed attempt.

    MSI Center re-renders its scenario panel on interaction and can re-emit
    the tiles under new automation ids (the `_Copy`/numeric suffix churn). The
    control object we clicked may still be referenced by the tree but no longer
    be the live one, so a click against it lands on nothing. Re-scanning — with
    the full-tree fallback — before each attempt is what makes the switch
    reliable instead of a one-shot that dies on the first re-render.
    """
    try:
        _wake_window(win)
        buttons = win.descendants(control_type="RadioButton")
    except Exception:
        return None
    inventory = _scenario_inventory(buttons) or _scenario_inventory_any(win)
    return inventory.get(canonical)


def _wake_webview_a11y(main_hwnd: int) -> None:
    """Chromium keeps its accessibility tree off until a UIA client pokes the
    render widget with WM_GETOBJECT; without this the Boost button is invisible."""
    import win32gui

    WM_GETOBJECT = 0x003D
    OBJID_CLIENT = 0xFFFFFFFC
    targets: list[int] = []

    def _collect(hwnd, _):
        if "Chrome_RenderWidgetHostHWND" in win32gui.GetClassName(hwnd):
            targets.append(hwnd)
        return True

    try:
        win32gui.EnumChildWindows(main_hwnd, _collect, None)
    except Exception:
        pass
    for hwnd in targets:
        try:
            ctypes.windll.user32.SendMessageTimeoutW(
                hwnd,
                WM_GETOBJECT,
                0,
                OBJID_CLIENT,
                0x0002,
                2000,
                ctypes.byref(ctypes.c_ulong()),
            )
        except Exception:
            pass


def pc_boost() -> str:
    """Trigger Microsoft PC Manager's "PC Boost" (memory + temp-file cleanup)."""
    if not is_windows():
        return "PC Manager control is Windows-only."

    _co_init()

    wins = _find_windows("PC Manager", _PCM_PROCS, timeout=3.0)
    launched_here = False
    if not wins:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", _PC_MANAGER_URI], creationflags=NO_WINDOW)
        except Exception as e:
            return f"Could not launch Microsoft PC Manager: {e}"
        launched_here = True
        wins = _find_windows("PC Manager", _PCM_PROCS, timeout=30.0)
    if not wins:
        return "Microsoft PC Manager did not open. Is it installed?"

    win = wins[0]
    boost_btn = None
    deadline = time.time() + 20.0
    while time.time() < deadline and boost_btn is None:
        try:
            _wake_webview_a11y(win.handle)
            for btn in win.descendants(control_type="Button"):
                name = (btn.window_text() or "").strip().lower()
                if name in ("boost", "booster") or name.startswith("boost "):
                    boost_btn = btn
                    break
        except Exception:
            pass
        if boost_btn is None:
            time.sleep(2.0)

    if boost_btn is None:
        return "PC Manager is open but I could not find the Boost button."

    try:
        boost_btn.invoke()
    except Exception:
        try:
            boost_btn.click_input()
        except Exception as e:
            return f"Failed to press Boost: {e}"

    if launched_here:
        time.sleep(3.0)  # let the boost animation start before hiding the window
        try:
            win.minimize()
        except Exception:
            pass

    result = "PC Boost triggered — memory and temporary files are being cleaned, sir."
    log("SystemPerformance", result)
    return result


def system_performance(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower().replace("-", "_").replace(" ", "_")
    value = params.get("value") or params.get("scenario") or params.get("mode")
    description = str(params.get("description", "")).strip()

    if action in (
        "msi_scenario_status",
        "scenario_status",
        "current_scenario",
        "performance_status",
        "which_scenario",
    ):
        # Must be matched before the scenario inference below: "performance_status"
        # contains "performance", which the loose alias match would read as a
        # request to switch to Extreme Performance.
        return msi_scenario_status()

    if action in ("pc_boost", "boost", "pc_manager_boost", "smart_boost"):
        return pc_boost()

    if action in ("msi_scenario", "set_scenario", "usage_scenario", "performance_mode", "scenario"):
        return msi_set_scenario(value or description)

    # No explicit action: infer from whatever text we received.
    intent = f"{action} {value or ''} {description}".lower()
    if "boost" in intent:
        return pc_boost()
    scenario = (
        _normalize_scenario(value)
        or _normalize_scenario(action)
        or _normalize_scenario(description)
    )
    if scenario:
        return msi_set_scenario(scenario)

    return (
        "Unknown system_performance request. Use action='msi_scenario' with a "
        "scenario value (extreme_performance, balanced, silent, super_battery, "
        "ai_engine) or action='pc_boost'."
    )
