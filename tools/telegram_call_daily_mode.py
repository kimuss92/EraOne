"""Enable owner-only, persistent incoming calls. Never place a call during setup."""

from __future__ import annotations

import argparse

from core.telegram_call_activation import daily_settings
from core.telegram_call_setup import load_settings, private_directory, save_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    try:
        directory = private_directory()
        save_settings(directory, daily_settings(load_settings(directory), args.enable))
    except Exception:
        print(
            "Daily call settings could not be updated. Check local account setup; no call was placed."
        )
        return 1
    print(
        "Daily owner-only calls enabled. Incoming listening starts with Era; no test renewal is needed. No call was placed."
        if args.enable
        else "Daily call access disabled. Restart Era to apply the setting."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
