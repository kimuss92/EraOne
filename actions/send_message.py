# actions/send_message.py
import os
import subprocess
import time

from core.utils import get_os

try:
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.06
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip

    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False


def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def _paste_text(text: str) -> None:
    _require_pyautogui()

    os_name = get_os()
    paste_hotkey = ("command", "v") if os_name == "mac" else ("ctrl", "v")

    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.15)
        pyautogui.hotkey(*paste_hotkey)
        time.sleep(0.1)
    else:
        pyautogui.write(text, interval=0.03)


def _clear_and_paste(text: str) -> None:
    _require_pyautogui()
    os_name = get_os()
    select_all = ("command", "a") if os_name == "mac" else ("ctrl", "a")
    pyautogui.hotkey(*select_all)
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    _paste_text(text)


def _open_app(app_name: str) -> bool:
    _require_pyautogui()
    os_name = get_os()

    try:
        if os_name == "windows":
            pyautogui.press("win")
            time.sleep(0.5)
            _paste_text(app_name)
            time.sleep(0.6)
            pyautogui.press("enter")
            time.sleep(2.5)
            return True

        elif os_name == "mac":
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["open", "-a", f"{app_name}.app"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            time.sleep(2.5)
            return result.returncode == 0

        else:
            launched = False
            for launcher in [
                ["gtk-launch", app_name.lower()],
                [app_name.lower()],
            ]:
                try:
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched = True
                    break
                except FileNotFoundError:
                    continue
            time.sleep(2.5)
            return launched

    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open {app_name}: {e}")
        return False


def _open_browser_url(url: str) -> bool:
    import webbrowser

    try:
        webbrowser.open(url)
        time.sleep(4.0)
        return True
    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open browser: {e}")
        return False


def _search_in_app(query: str) -> None:
    _require_pyautogui()
    os_name = get_os()
    search_hotkey = ("command", "f") if os_name == "mac" else ("ctrl", "f")

    pyautogui.hotkey(*search_hotkey)
    time.sleep(0.5)
    _clear_and_paste(query)
    time.sleep(1.0)


def _desktop_send(app_name: str, receiver: str, message: str) -> str:
    if not _open_app(app_name):
        return f"Could not open {app_name}."

    time.sleep(1.0)
    _search_in_app(receiver)
    pyautogui.press("enter")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return f"Message sent to {receiver} via {app_name}."


# ── ✅ NEW UTILITY: WINDOWS NATIVE CLIPBOARD FILE INJECTOR ────────────────────
def _inject_file_into_clipboard(file_path: str) -> bool:
    """Copies a physical local file to the Windows clipboard as a shell object descriptor."""
    try:
        import ctypes
        from ctypes import wintypes

        import win32clipboard

        class DROPFILES(ctypes.Structure):
            _fields_ = [
                ("pFiles", wintypes.DWORD),
                ("pt", wintypes.POINT),
                ("fNC", wintypes.BOOL),
                ("fWide", wintypes.BOOL),
            ]

        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            print(f"[ClipboardEngine] ❌ Target path does not exist: {abs_path}")
            return False

        # Dropfiles layout requires a double-null-terminated Unicode string array
        offsets = ctypes.sizeof(DROPFILES)
        path_bytes = (abs_path + "\0\0").encode("utf-16-le")

        df = DROPFILES()
        df.pFiles = offsets
        df.fWide = True  # Enforce character layout parameters

        buffer = bytes(df) + path_bytes

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            # Allocate global memory segment block (GMEM_MOVEABLE | GMEM_ZEROINIT)
            hGlobal = ctypes.windll.kernel32.GlobalAlloc(0x0042, len(buffer))
            lpGlobal = ctypes.windll.kernel32.GlobalLock(hGlobal)
            ctypes.memmove(lpGlobal, buffer, len(buffer))
            ctypes.windll.kernel32.GlobalUnlock(hGlobal)
            win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, hGlobal)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        print(f"[ClipboardEngine] ❌ Injection exception triggered: {e}")
        return False


def _send_whatsapp(receiver: str, message: str, file_path: str = "") -> str:
    """
    Scans your system windows to reuse an already running instance of WhatsApp Web,
    brings it to the foreground, isolates the contact, and dispatches the message or document.
    """
    import time

    import pyautogui
    import win32con
    import win32gui

    # Save original foreground window frame
    original_hwnd = win32gui.GetForegroundWindow()

    # 1. Look for a visible window containing "WhatsApp" in the title bar
    whatsapp_hwnd = 0

    def enum_cb(hwnd_enum, extra):
        nonlocal whatsapp_hwnd
        if win32gui.IsWindowVisible(hwnd_enum):
            title = win32gui.GetWindowText(hwnd_enum).lower()
            if "whatsapp" in title:
                whatsapp_hwnd = hwnd_enum
                return False  # Instance found, stop iterating immediately
        return True

    win32gui.EnumWindows(enum_cb, None)

    # 2. Open or reuse the shortcut window layout
    if whatsapp_hwnd != 0:
        win32gui.ShowWindow(whatsapp_hwnd, win32con.SW_RESTORE)
        time.sleep(0.1)
        try:
            win32gui.SetForegroundWindow(whatsapp_hwnd)
        except Exception:
            rect = win32gui.GetWindowRect(whatsapp_hwnd)
            pyautogui.click(rect[0] + 10, rect[1] + 10)
        time.sleep(0.5)
    else:
        if not _open_app("WhatsApp Web"):
            return "Could not open your WhatsApp Web shortcut, sir."
        time.sleep(5.5)  # Extended buffer to allow engine to authenticate

    # 3. Focus WhatsApp Web's internal contact search box
    pyautogui.hotkey("ctrl", "alt", "/")
    time.sleep(0.5)

    # 4. Wipe stale inputs and paste your target recipient's name string
    _clear_and_paste(receiver)
    time.sleep(2.0)  # Wait for network layouts to populate matching contacts

    # 5. Lock onto the filtered user chat panel card
    pyautogui.press("enter")
    time.sleep(1.2)  # Wait for chat window frame elements to stabilize

    # ── ✅ CORE HANDLING: DOCUMENT / FILE ATTACHMENT INJECTION TRACK ──────────
    if file_path:
        filename = os.path.basename(file_path)
        print(f"[WhatsApp] Staging file attachment vector path: {file_path}")

        if _inject_file_into_clipboard(file_path):
            # Paste the document directly into the viewport area
            pyautogui.hotkey("ctrl", "v")
            time.sleep(1.5)  # Let WhatsApp render its background overlay presentation framework

            # If a caption exists, type it directly into the staged upload pane's text container
            if message:
                _paste_text(message)
                time.sleep(0.3)

            # Dispatches the document attachment out to the recipient
            pyautogui.press("enter")
            time.sleep(1.0)
            result_str = (
                f"Document '{filename}' dispatched to {receiver} via WhatsApp Web shortcut, sir."
            )
        else:
            return (
                f"Failed to attach document '{filename}'. Verify that the file path is accessible."
            )
    else:
        # Standard fallback path if sending a simple text asset
        _paste_text(message)
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(0.3)
        result_str = f"Message sent to {receiver} via WhatsApp Web shortcut, sir."

    # 7. Return focus back to your code terminal/editor seamlessly
    if original_hwnd != 0:
        try:
            win32gui.SetForegroundWindow(original_hwnd)
        except Exception:
            pass

    return result_str


def _send_telegram(receiver: str, message: str) -> str:
    return _desktop_send("Telegram", receiver, message)


def _send_signal(receiver: str, message: str) -> str:
    return _desktop_send("Signal", receiver, message)


def _send_discord(receiver: str, message: str) -> str:
    return _desktop_send("Discord", receiver, message)


def _send_instagram(receiver: str, message: str) -> str:
    _require_pyautogui()
    if not _open_browser_url("https://www.instagram.com/direct/new/"):
        return "Could not open Instagram in browser."
    _paste_text(receiver)
    time.sleep(1.5)
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(0.4)
    for _ in range(4):
        pyautogui.press("tab")
        time.sleep(0.15)
    pyautogui.press("enter")
    time.sleep(2.0)
    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return f"Message sent to {receiver} via Instagram."


def _send_messenger(receiver: str, message: str) -> str:
    _require_pyautogui()
    if not _open_browser_url("https://www.messenger.com/"):
        return "Could not open Messenger in browser."
    _search_in_app(receiver)
    time.sleep(0.5)
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.0)
    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return f"Message sent to {receiver} via Messenger."


_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp"}, _send_whatsapp),
    ({"telegram", "tg"}, _send_telegram),
    ({"instagram", "ig", "insta"}, _send_instagram),
    ({"signal"}, _send_signal),
    ({"discord"}, _send_discord),
    ({"messenger", "facebook", "fb"}, _send_messenger),
]


def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, handler in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return handler
    return lambda r, m: _desktop_send(platform_str.strip().title(), r, m)


def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    receiver = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform = params.get("platform", "whatsapp").strip()
    file_path = params.get("file_path", "").strip()

    if not receiver:
        return "Please specify a recipient."
    if not message_text and not file_path:
        return "Please specify either message text context or an absolute attachment file path."
    if not _PYAUTOGUI:
        return "PyAutoGUI is not installed — cannot control the desktop."

    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(
        f"[SendMessage] 📨 {platform} → {receiver}: {preview} (Attachment: {os.path.basename(file_path) if file_path else 'None'})"
    )
    if player:
        player.write_log(f"[msg] {platform} → {receiver}")

    try:
        handler = _resolve_platform(platform)
        # Check if the chosen platform handler supports file parameters directly
        if handler == _send_whatsapp:
            result = handler(receiver, message_text, file_path=file_path)
        else:
            result = handler(receiver, message_text)
    except Exception as e:
        result = f"Could not complete transmission: {e}"

    print(
        f"[SendMessage] {'✅' if 'dispatched' in result.lower() or 'sent' in result.lower() else '❌'} {result}"
    )
    if player:
        player.write_log(f"[msg] {result}")

    return result
