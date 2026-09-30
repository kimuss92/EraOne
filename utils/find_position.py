# utils/find_position.py
import os
import time

import pyautogui
import win32gui


def run_calibration_tracker():
    print("==================================================")
    print("        ERA ONE MOUSE CALIBRATION TRACKER          ")
    print("==================================================")
    print("Instructions:")
    print("1. Open your Brave browser with the Netflix show grid open.")
    print("2. Open the preview drawer/modal for a show (like 'Berlin').")
    print("3. Hover your mouse directly over the center of the red PLAY button.")
    print("4. Keep the mouse still and read the multipliers printed below.\n")
    print("Press Ctrl+C in this console to stop tracking.")
    print("==================================================\n")

    time.sleep(2)  # Give you 2 seconds to prepare

    try:
        while True:
            # 1. Grab absolute mouse screen position
            mx, my = pyautogui.position()

            # 2. Grab the window currently in focus (Foreground)
            hwnd = win32gui.GetForegroundWindow()
            window_title = win32gui.GetWindowText(hwnd)

            # Isolate browser windows to calculate offsets cleanly
            if hwnd != 0 and window_title:
                try:
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    width = right - left
                    height = bottom - top

                    if width > 0 and height > 0:
                        # 3. Calculate exact percentage fractions relative to top-left of the window
                        rx = (mx - left) / width
                        ry = (my - top) / height

                        # Clear console screen for clean loop printing
                        os.system("cls" if os.name == "nt" else "clear")

                        print("==================================================")
                        print(f"Active Window Frame: {window_title[:45]}")
                        print(f"Window Dimensions:   {width}x{height} (at Left:{left}, Top:{top})")
                        print(f"Absolute Mouse Pos:  X={mx}, Y={my}")
                        print("==================================================")
                        print("👉 COPY THESE VALUES INTO ACTIONS/BROWSER_CONTROL.PY:")
                        print(f"   click_x = left + int(width * {rx:.2f})")
                        print(f"   click_y = top + int(height * {ry:.2f})")
                        print("==================================================")
                        print("\n[Tracking active... Move mouse to watch values change]")
                except Exception:
                    pass

            time.sleep(0.2)  # Update rate frequency (5Hz)

    except KeyboardInterrupt:
        print("\n[Registry] Calibration tracking terminated successfully.")


if __name__ == "__main__":
    run_calibration_tracker()
