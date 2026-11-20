"""Protect existing Windows credentials after closing Era: python -m tools.protect_api_config --apply."""

import argparse
import json

from app.hud.instance import NamedMutex, backend_mutex_name
from core.protected_config import has_plaintext_credentials, migrate, read_config
from core.utils import CONFIG_PATH


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    guard = NamedMutex(backend_mutex_name())
    try:
        if args.apply:
            if not guard.acquire():
                print("Close Era before protecting credentials; no changes made.")
                return 1
            changed = migrate(CONFIG_PATH)
            print("Credential protection applied." if changed else "Credentials already protected.")
        read_config(CONFIG_PATH)
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        if has_plaintext_credentials(raw):
            print("Plaintext credentials remain; close Era and rerun with --apply.")
            return 1
        print("Read-back verified; no plaintext credentials in current configuration.")
        return 0
    except Exception as exc:
        print(
            f"Credential protection failed ({type(exc).__name__}); inspect configuration without sharing secrets."
        )
        return 1
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
