# core/confirmation.py
from __future__ import annotations

from collections.abc import Callable


class ConfirmationManager:
    def request_confirmation(
        self,
        tool: str,
        action_name: str,
        message: str,
        risk_level: str,
        details: dict | None = None,
        speak: Callable | None = None,
        confirmed: bool = False,
        auto_confirm: bool = False,
    ) -> bool:
        """Return whether an action may proceed after risk assessment.

        MEDIUM and HIGH actions must be explicitly confirmed by the pending
        confirmation flow. LOW/SAFE actions are allowed immediately.
        """
        normalized_risk = (risk_level or "").upper().strip()

        if normalized_risk == "BLOCKED":
            return False

        if confirmed or auto_confirm:
            return True

        if normalized_risk in {"HIGH", "MEDIUM"}:
            if speak:
                speak("This action needs confirmation first, sir.")
            return False

        # LOW and SAFE
        return True
