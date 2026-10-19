"""Explicit presence commands, independent of a model's tool selection."""

from __future__ import annotations

from typing import Any, cast

from core.home_presence import HomePresence, get_home_presence, presence_intent
from core.local_presence import LocalPresence
from core.telegram_call_setup import load_settings, private_directory


def start_local_presence(engine: Any) -> LocalPresence:
    loop = engine._loop

    def apply() -> None:
        try:
            if get_home_presence().local_activity():
                engine.ui.write_log(
                    "SYS: Local keyboard/mouse use detected. Home mode restored; automatic calls blocked."
                )
        except Exception:
            engine.ui.write_log(
                "SYS: Could not save automatic Home mode; say I'm back home or send /home."
            )

    def observed() -> None:
        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:
            pass

    watcher = LocalPresence(observed)
    if not watcher.start():
        engine.ui.write_log(
            "SYS: Local presence watcher unavailable. Use I'm back home or /home to block automatic calls."
        )
    return cast(LocalPresence, watcher)


def handle_presence_order(text: str) -> str | None:
    state = presence_intent(text)
    if state is None:
        return None
    try:
        settings = load_settings(private_directory())
        owner = settings.get("owner_user_id")
        if (
            type(owner) is not int
            or owner <= 0
            or owner != settings.get("expected_owner_user_id")
            or not all(
                settings.get(key) is True
                for key in ("authenticated", "account_authenticated", "owner_bound")
            )
        ):
            raise PermissionError("Owner is not verified.")
        return cast(str, cast(HomePresence, get_home_presence()).use_manual(owner, state))
    except Exception:
        return "I couldn't save that presence change. Your previous setting still applies; check the Telegram account setup."
