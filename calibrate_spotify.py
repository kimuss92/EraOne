# calibrate_spotify.py
import ctypes
import time

import pyautogui
import win32api
import win32con
import win32gui
import win32process

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

print("=== ERA ONE SPOTIFY DUAL-PROFILE CALIBRATION ===")
print("1. Set Spotify to the layout state you want to calibrate (Snapped OR Maximized).")
print("2. Hover your mouse directly over the center of the Green Play Circle.")
print("3. Keep your mouse still. Capturing in 5 seconds...")
print("===============================================\n")

for i in range(5, 0, -1):
    print(f"Capturing in {i}...")
    time.sleep(1)

x, y = pyautogui.position()

main_hwnd = 0


def enum_cb(hwnd_enum, extra):
    global main_hwnd
    if win32gui.IsWindowVisible(hwnd_enum):
        rect = win32gui.GetWindowRect(hwnd_enum)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w > 300 and h > 300:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd_enum)
                handle = win32api.OpenProcess(0x1000, False, pid)
                if "spotify.exe" in win32process.GetModuleFileNameEx(handle, 0).lower():
                    main_hwnd = hwnd_enum
                    return False
            except Exception:
                pass
    return True


win32gui.EnumWindows(enum_cb, None)

if main_hwnd != 0:
    rect = win32gui.GetWindowRect(main_hwnd)
    left, top, right, bottom = rect
    width, height = right - left, bottom - top

    # Check the actual window engine placement flags
    placement = win32gui.GetWindowPlacement(main_hwnd)
    is_maximized = placement[1] == win32con.SW_SHOWMAXIMIZED
    state_label = "MAXIMIZED PROFILE" if is_maximized else "SNAPPED/NORMAL PROFILE"

    x_ratio = (x - left) / width
    y_ratio = (y - top) / height

    print(f"\n✅ {state_label} CAPTURED!")
    print(f"Window Size: {width}x{height}")
    print("\n👉 COPY THIS MULTIPLIER VALUE:")
    if is_maximized:
        print(
            f"        # Maximized State Ratios\n        x_ratio = {x_ratio:.4f}\n        y_ratio = {y_ratio:.4f}"
        )
    else:
        print(
            f"        # Snapped State Ratios\n        x_ratio = {x_ratio:.4f}\n        y_ratio = {y_ratio:.4f}"
        )
    print("=============================================")
else:
    print("\n❌ Error: Could not find any active visible Spotify window layout.")
