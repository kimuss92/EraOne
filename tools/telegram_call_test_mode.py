"""Enable/disable a 30-minute owner-only attended test window. Places no calls."""

from __future__ import annotations

import argparse

from core.telegram_call_activation import test_settings
from core.telegram_call_setup import load_settings, private_directory, save_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    try:
        directory = private_directory()
        save_settings(directory, test_settings(load_settings(directory), args.enable))
    except Exception:
        print(
            "Call test settings could not be updated. Check local account setup; no call was placed."
        )
        return 1
    print(
        "Attended test window enabled for 30 minutes. Restart Era, then use Call owner or Receive calls in the HUD. No call was placed."
        if args.enable
        else "New calls disabled. Hang up any current call and restart Era to refresh controls."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
