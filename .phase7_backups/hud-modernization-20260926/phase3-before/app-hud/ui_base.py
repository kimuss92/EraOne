"""The UI API the engine talks to, without Qt.

``BroadcastUIMixin`` is the half of the old ``ui.EraUI`` that only ever
broadcast JSON: amplitude, theme, active model, model catalog. It is shared by
the legacy Qt window (ui.EraUI) and by ``HeadlessUI``, which is what main.py
uses when the HUD runs in the browser.

Every broadcast also passes through ``hud.observe_legacy`` — that is how the
new agent state machine is fed by the exact calls the engine already makes
(set_state, write_log, set_mute, browser_mic), with no change to those calls.
"""

from __future__ import annotations

import math
import threading
import time

from app.hud.events import hud


class BroadcastUIMixin:
    _ws_broadcast = None

    def set_ws_broadcast(self, broadcast_fn) -> None:
        """Called by main.py to link the WebSocket broadcast function."""
        self._ws_broadcast = broadcast_fn

    def _broadcast(self, msg: dict) -> None:
        hud.observe_legacy(msg)
        if self._ws_broadcast:
            try:
                self._ws_broadcast(msg)
            except Exception:
                pass

    def set_amplitude(self, level: float, bands: list[float] | None = None) -> None:
        """Broadcast Era's own speech amplitude (0..1) so the sphere animation
        syncs to the agent's voice. WS-only — deliberately lightweight, called
        at audio-chunk rate (~23 Hz).

        `bands` carries the per-band energies of the same chunk (low → high),
        which is what lets the sphere react as an EQUALIZER rather than just
        brightening. Omitted (None) means "no spectrum this frame".

        Every value is forced finite and clamped here. json.dumps serializes a
        float('nan') as a bare `NaN`, which is NOT valid JSON — the browser's
        JSON.parse rejects the message and the UI drops it whole. A single
        non-finite band would therefore freeze the sphere entirely.
        """

        def _clean(v: object) -> float:
            try:
                f = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0.0
            if not math.isfinite(f):
                return 0.0
            return round(min(1.0, max(0.0, f)), 3)

        msg: dict = {"type": "amplitude", "value": _clean(level)}
        if bands:
            msg["bands"] = [_clean(b) for b in bands]
        self._broadcast(msg)

    def set_theme(self, theme: str) -> None:
        """Switch the UI theme by voice/backend command ("dark"/"light"/"toggle")."""
        if theme in ("dark", "light", "toggle"):
            self._broadcast({"type": "theme", "theme": theme, "ts": time.time()})

    def set_active_model(
        self,
        label: str,
        detail: str = "",
        *,
        alias: str = "",
        fell_back_from: str = "",
        fallback_reason: str = "",
        pinned: bool = False,
    ) -> None:
        """Publish which model actually handled the request.

        The fallback fields are carried separately rather than folded into the
        label because the UI has to be able to show a DISPLACED choice
        differently from a first choice.
        """
        if not label:
            return
        self._broadcast(
            {
                "type": "model",
                "label": str(label),
                "alias": str(alias or ""),
                "detail": str(detail or ""),
                "fell_back_from": str(fell_back_from or ""),
                "fallback_reason": str(fallback_reason or ""),
                "pinned": bool(pinned),
                "ts": time.time(),
            }
        )

    def set_model_catalog(self, models: list[dict], *, selected: str = "") -> None:
        """Publish the models the user can switch to, and which one is pinned.

        Every field is coerced to a primitive here: one unserialisable entry
        from the registry must cost one row, not the whole menu.
        """
        if not models:
            return
        clean: list[dict] = []
        for m in models:
            try:
                clean.append(
                    {
                        "alias": str(m.get("alias", "")),
                        "label": str(m.get("label", "")),
                        "detail": str(m.get("detail", "")),
                        "role": str(m.get("role", "")),
                        "available": bool(m.get("available", True)),
                        "unavailable_reason": str(m.get("unavailable_reason", "") or ""),
                        "cost_per_call_usd": float(m.get("cost_per_call_usd", 0.0) or 0.0),
                        "realtime_audio": bool(m.get("realtime_audio", False)),
                    }
                )
            except (TypeError, ValueError, AttributeError):
                continue
        if not clean:
            return
        self._broadcast(
            {
                "type": "model_catalog",
                "models": clean,
                "selected": str(selected or ""),
                "ts": time.time(),
            }
        )


class _HeadlessRoot:
    """Stands in for ``ui.root`` so main.py can still call ``mainloop()``.

    Blocks the main thread until ``quit()`` — i.e. until an explicit shutdown —
    while staying responsive to Ctrl+C in the debug console (a bare
    ``Event.wait()`` with no timeout is not interruptible on Windows).
    """

    def __init__(self) -> None:
        self._exit = threading.Event()

    def mainloop(self) -> None:
        try:
            while not self._exit.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass

    def quit(self) -> None:
        self._exit.set()

    def protocol(self, *_: object) -> None:
        pass


class HeadlessUI(BroadcastUIMixin):
    """The engine-facing UI API with no window: the browser HUD is the view.

    Same public surface as ``ui.EraUI`` — set_state, write_log, set_mute,
    muted, start/stop_speaking, set_amplitude, window_control, on_text_command,
    on_file_upload, current_file — so the engine cannot tell the difference.
    """

    def __init__(self) -> None:
        self._muted = False
        self._ws_broadcast = None
        self.root = _HeadlessRoot()
        self.on_text_command = None
        self.current_file: str | None = None
        self.on_file_upload = None

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, v: bool) -> None:
        # Same semantics as EraUI: assigning routes through the standby toggle.
        if v != self._muted and self.on_text_command:
            threading.Thread(
                target=self.on_text_command,
                args=("__mute__" if v else "__unmute__",),
                daemon=True,
            ).start()

    def set_state(self, state: str) -> None:
        self._broadcast({"type": "state", "state": state, "ts": time.time()})

    def write_log(self, text: str) -> None:
        self._broadcast({"type": "log", "text": text, "ts": time.time()})

    def wait_for_api_key(self) -> None:
        pass

    def start_speaking(self) -> None:
        self.set_state("SPEAKING")

    def stop_speaking(self) -> None:
        if not self.muted:
            self.set_state("LISTENING")

    def set_mute(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._broadcast({"type": "mute", "muted": self._muted, "ts": time.time()})

    def window_control(self, action: str) -> None:
        """The browser owns its window; there is nothing to minimize here."""
        return None
