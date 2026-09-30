import re
import time

import pyautogui
import pygetwindow as gw

# ---- mapping of user phrases to window title keywords ----
APP_KEYWORDS = {
    "spotify": ["spotify", "music"],  # "play music" -> spotify
    "youtube": ["youtube"],
    "netflix": ["netflix"],
}

# ---- mapping of app to its play/pause hotkey ----
# For Spotify we'll use the system media play/pause key.
# For web apps (YouTube/Netflix) we focus the window and press space or 'k'
APP_HOTKEY = {
    "spotify": "playpause",  # media key
    "youtube": "space",  # space toggles play/pause on YouTube
    "netflix": "space",
}


def get_media_windows():
    """Return a list of window objects that match media apps."""
    media_windows = []
    all_titles = gw.getAllTitles()
    for title in all_titles:
        title_lower = title.lower()
        if any(kw in title_lower for kw in ["youtube", "netflix", "spotify"]):
            media_windows.append(gw.getWindowsWithTitle(title)[0])
    return media_windows


def which_app(window_title):
    """Identify the app from a window title."""
    title_lower = window_title.lower()
    for app, keywords in APP_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                return app
    return None


def get_playing_state(window):
    """Heuristic: if the window title contains 'Playing' or '▪' (paused icon maybe?), assume it's playing.
    You can refine this later."""
    # many sites add " - YouTube" or " - Playing" when media is active
    # Spotify window title often contains the current track name; we can't easily know if it's playing.
    # We'll keep a manual state (see below).
    return False  # We'll track state ourselves


# ---- global state for playback tracking ----
# This dictionary will hold 'app_name': bool (True = playing, False = paused)
playback_state = {}


def update_state():
    """Scan all media windows and initialise state if not set."""
    global playback_state
    windows = get_media_windows()
    for w in windows:
        app = which_app(w.title)
        if app and app not in playback_state:
            playback_state[app] = False  # assume paused on first detection


def resume_app(target_app):
    """Pause everything else, then resume the target app."""
    global playback_state
    update_state()
    windows = get_media_windows()

    # 1. Pause all currently playing apps EXCEPT the target
    for w in windows:
        app = which_app(w.title)
        if app and app != target_app and playback_state.get(app, False):
            # Pause this app
            focus_and_send_hotkey(w, APP_HOTKEY.get(app, "space"))
            playback_state[app] = False
            time.sleep(0.3)

    # 2. Now resume the target app (if it is paused)
    target_window = None
    for w in windows:
        if which_app(w.title) == target_app:
            target_window = w
            break
    if target_window is None:
        print(f"No {target_app} window found.")
        return

    if not playback_state.get(target_app, False):
        # It's paused, so resume it
        focus_and_send_hotkey(target_window, APP_HOTKEY.get(target_app, "space"))
        playback_state[target_app] = True
        print(f"Resumed {target_app}")
    else:
        print(f"{target_app} is already playing.")


def pause_app(target_app):
    """Pause the specified app if it's playing."""
    global playback_state
    update_state()
    windows = get_media_windows()
    for w in windows:
        if which_app(w.title) == target_app and playback_state.get(target_app, False):
            focus_and_send_hotkey(w, APP_HOTKEY.get(target_app, "space"))
            playback_state[target_app] = False
            print(f"Paused {target_app}")
            return
    print(f"{target_app} is not playing or not found.")


def focus_and_send_hotkey(window, hotkey):
    """Bring window to front and send the hotkey."""
    try:
        window.activate()
        time.sleep(0.2)  # wait for focus
        if hotkey == "playpause":
            pyautogui.press("playpause")
        else:
            pyautogui.press(hotkey)
    except Exception as e:
        print(f"Error focusing window: {e}")


def handle_media_command(text):
    """
    Parse a natural language command and perform actions.
    Supports: 'play music', 'pause spotify', 'resume youtube',
              'pause spotify and resume youtube', etc.
    """
    text = text.lower().strip()
    # split into multiple sub-commands if 'and' or ',' is present
    sub_commands = re.split(r"\,| and ", text)
    # also split on ' then ' if used
    sub_commands = [cmd.strip() for cmd in sub_commands if cmd.strip()]

    for cmd in sub_commands:
        action = None
        target_app = None

        # Determine action
        if "pause" in cmd or "stop" in cmd:
            action = "pause"
        elif "resume" in cmd or "play" in cmd or "start" in cmd:
            action = "resume"

        # Find target app
        for app, keywords in APP_KEYWORDS.items():
            for kw in keywords:
                if kw in cmd:
                    target_app = app
                    break
            if target_app:
                break

        if action and target_app:
            if action == "pause":
                pause_app(target_app)
            elif action == "resume":
                resume_app(target_app)
        else:
            print(f"Could not parse command: {cmd}")
