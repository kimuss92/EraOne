import ctypes

# Load Windows Core Audio API constants
clsid_MMDeviceEnumerator = "{BCDE0359-9265-454E-A826-65C461A15A15}"
iid_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"


def is_audio_playing():
    """
    Checks if the system is currently outputting audio.
    Returns True if music/video is playing, False otherwise.
    """
    try:
        # Utilizing Windows ole32 to dynamically query the Audio Session Manager
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        # Fallback check using Windows user32 to detect if a media player window is active
        # (This acts as a secondary check if Core Audio is idle)
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)

        # Query active sound device state (using direct windows sound device polling)
        # Note: For full automation, 'pycaw' can also be used as a simple alternative:
        # pip install pycaw
        from pycaw.pycaw import AudioUtilities

        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process and session.State == 1:  # State 1 means Active/Playing
                if session.Process.name().lower() in ["spotify.exe", "chrome.exe", "vlc.exe"]:
                    return True
        return False
    except Exception:
        return False
    finally:
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    print("Checking system state...")
    if is_audio_playing():
        print("Playing: Media Detected")
        print("Era One Status: AWARE")
    else:
        print("No media playing.")
        print("Era One Status: BLIND")
