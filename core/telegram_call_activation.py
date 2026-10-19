"""Explicit attended testing or daily owner-only call consent; no networking."""

from __future__ import annotations

import math
import time
from typing import Any

TEST_SECONDS = 1800


def calling_enabled(settings: dict[str, Any], now: float | None = None) -> bool:
    if settings.get("calls_enabled") is not True:
        return False
    if settings.get("attended_test_mode") is not True:
        if settings.get("daily_calls_opt_in") is True:
            try:
                test_settings(settings, True, now=0)
            except (ValueError, TypeError, KeyError):
                return False
            return True
        return settings.get("duplex_validated") is True
    until = settings.get("attended_test_until")
    current = time.time() if now is None else now
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return False
    return bool(
        settings.get("attended_test_mode") is True
        and type(until) in (int, float)
        and math.isfinite(until)
        and current < until <= current + TEST_SECONDS
    )


def test_settings(
    settings: dict[str, Any], enable: bool, now: float | None = None
) -> dict[str, Any]:
    from core.telegram_call_setup import validate_setup

    if type(enable) is not bool:
        raise ValueError("Explicit test opt-in required.")
    if enable:
        validate_setup(settings)
        if not all(
            settings.get(key) is True
            for key in ("authenticated", "owner_bound", "account_authenticated")
        ):
            raise ValueError("Finish local account/owner verification first.")
        owner, era = settings.get("owner_user_id"), settings.get("era_user_id")
        if (
            type(owner) is not int
            or type(era) is not int
            or owner <= 0
            or era <= 0
            or owner == era
            or owner != settings.get("expected_owner_user_id")
        ):
            raise ValueError("Verified separate owner binding required.")
    return {
        **settings,
        "calls_enabled": enable,
        "attended_test_mode": enable,
        "attended_test_until": (
            (time.time() if now is None else now) + TEST_SECONDS if enable else 0
        ),
    }


def daily_settings(settings: dict[str, Any], enable: bool) -> dict[str, Any]:
    """Explicit daily-use consent, separate from measured duplex validation."""
    verified = test_settings(settings, enable)
    return {
        **verified,
        "daily_calls_opt_in": enable,
        "auto_receive_calls": enable,
        "attended_test_mode": False,
        "attended_test_until": 0,
    }
