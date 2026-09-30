import sys
import time
from pathlib import Path


def run():
    # Ensure repo root is on sys.path so `actions.*` imports resolve
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        print("[SMOKE] Importing actions...")
        from actions.media_coordinator import media_coordinator
        from actions.netflix_control import netflix_control
        from actions.open_app import open_app

        print("[SMOKE] 1) Resuming Netflix (background DOM control)")
        try:
            r1 = netflix_control({"action": "play"})
        except Exception as e:
            r1 = f"EXCEPTION: {e}"
        print("[SMOKE] Netflix ->", r1)

        time.sleep(1)
        print("[SMOKE] 2) Opening Spotify (open_app)")
        try:
            r2 = open_app({"app_name": "Spotify"})
        except Exception as e:
            r2 = f"EXCEPTION: {e}"
        print("[SMOKE] open_app ->", r2)

        time.sleep(1)
        print("[SMOKE] 3) Resuming Spotify (media_coordinator)")
        try:
            r3 = media_coordinator({"target": "spotify", "action": "play"})
        except Exception as e:
            r3 = f"EXCEPTION: {e}"
        print("[SMOKE] media_coordinator ->", r3)

    except Exception as exc:
        print(f"[SMOKE] Fatal exception running smoke test: {exc}")


if __name__ == "__main__":
    run()
