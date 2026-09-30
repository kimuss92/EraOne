"""
ui.py — Transparent QWebEngineView wrapper for ERA ONE.

Replaces the native PyQt6 drawing logic with a React/Vite/Tailwind frontend
compiled into a single HTML file via vite-plugin-singlefile.

Architecture:
    React app (ui/dist/index.html) ←→ WebSocket ←→ Python backend (main.py)
    QWebEngineView loads the compiled React app.
    EraUI broadcasts state changes to all connected WebSocket clients.
    Title bridge (React sets document.title = "cmd:xxx") exposes
    minimize/maximize/close to Python via titleChanged signal.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QShortcut
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

from core.utils import get_os, log

_OS = get_os()

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W, _MIN_H = 820, 580

WS_PORT = 8765  # Must match WebSocketServer in main.py

# ── UI zoom ──────────────────────────────────────────────────────────────────
# The React layout is responsive, so nothing is ever CLIPPED at 100%. What
# changes is room: measured at the default 980x700 window, the sphere's column
# is 436px at 100% and 608px at 85%. The rails and the conversation panel have
# fixed widths, so every point of zoom you give back goes to the centre column —
# which is why zooming out reads as "everything is visible" rather than "smaller".
#
# 85% is the default. It is not a guess dressed as a measurement: it sits inside
# the 0.85-0.90 range measured off the reference screenshot, and the exact value
# matters less than it used to because a Ctrl+wheel adjustment now PERSISTS.
_DEFAULT_ZOOM = 0.85
_MIN_ZOOM, _MAX_ZOOM = 0.5, 2.0
_ZOOM_STEP = 0.05
_ZOOM_PREFS_PATH = Path(__file__).resolve().parent / "runtime" / "ui_prefs.json"


def _load_zoom() -> float:
    """Last zoom the user chose, or the default. Never raises: a broken prefs
    file must cost the preference, not the window."""
    try:
        raw = json.loads(_ZOOM_PREFS_PATH.read_text(encoding="utf-8"))
        value = float(raw.get("zoom", _DEFAULT_ZOOM))
    except Exception:
        return _DEFAULT_ZOOM
    if not (_MIN_ZOOM <= value <= _MAX_ZOOM):
        return _DEFAULT_ZOOM
    return value


def _save_zoom(value: float) -> None:
    try:
        _ZOOM_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        try:
            existing = json.loads(_ZOOM_PREFS_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["zoom"] = round(value, 3)
        _ZOOM_PREFS_PATH.write_text(json.dumps(existing, indent=1), encoding="utf-8")
    except Exception as e:
        log("UI", f"could not persist zoom ({e})")


class MainWindow(QMainWindow):
    """Frameless window hosting a transparent QWebEngineView."""

    _log_sig = pyqtSignal(str)
    _state_sig = pyqtSignal(str)
    _mute_sig = pyqtSignal(bool)
    _win_ctrl_sig = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ERA ONE — LIVE")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        # WindowMinimizeButtonHint alongside Frameless is what makes the
        # TASKBAR button work. A bare frameless window has no WS_MINIMIZEBOX, so
        # Windows does not consider it minimizable and click-to-minimize on the
        # taskbar silently does nothing — the in-app button still worked, which
        # is why the window looked fine until you tried the taskbar. The hint
        # restores the style bit without drawing any native chrome, so the
        # frameless look is unchanged.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowMinimizeButtonHint
        )
        # No WA_TranslucentBackground: QWebEngineView doesn't support layered
        # windows on Windows and intermittently composites torn/black frames.
        self.setStyleSheet("background: #00060a;")

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width() - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command = None
        self._muted = False
        self._dragging = False
        self._drag_start: QPoint | None = None
        self._window_start = QPoint()
        # True while the pointer is over a text-selection zone (conversation
        # log). React reports it via the title bridge; while set, mouse drags
        # SELECT TEXT instead of moving the window — this is what makes the
        # conversation copyable at all.
        self._no_drag = False

        central = QWidget()
        central.setStyleSheet("background: #00060a;")
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._web = QWebEngineView()
        self._web.setStyleSheet("background: #00060a;")
        self._web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        # Opaque dark page background — avoids white flash while React loads.
        self._web.page().setBackgroundColor(QColor("#00060a"))

        # Global event filter to catch mouse events from native Chromium child windows
        QApplication.instance().installEventFilter(self)

        settings = self._web.page().settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        # Copy-to-clipboard from the conversation log (navigator.clipboard needs
        # this on a file:// origin; without it writeText silently rejects).
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)

        dist_path = Path(__file__).resolve().parent / "ui" / "dist" / "index.html"
        if dist_path.exists():
            self._web.setUrl(QUrl.fromLocalFile(str(dist_path)))
            log("UI", f"Loaded React frontend from {dist_path}")
            # Applied AFTER setUrl: QWebEngineView resets zoom on navigation, so
            # setting it before the load would be silently discarded.
            self._zoom = _load_zoom()
            self._web.setZoomFactor(self._zoom)
            self._web.loadFinished.connect(lambda _ok: self._web.setZoomFactor(self._zoom))
            log("UI", f"Zoom {self._zoom:.0%}")
        else:
            self._web.setHtml(
                "<html><body style='background:#00060a;display:flex;"
                "align-items:center;justify-content:center;height:100vh;"
                "font-family:monospace;color:#133c4f'>"
                "<div style='text-align:center'>"
                "<h1>UI dist not found</h1>"
                "<p style='color:#555'>Run: <code>cd ui_src && npm install && npm run build</code></p>"
                "</div></body></html>"
            )
            log("UI", f"WARNING: dist/index.html not found at {dist_path}")

        layout.addWidget(self._web)

        # Grant microphone permission for voice meter (file:// origin needs explicit grant)
        self._web.page().featurePermissionRequested.connect(self._on_feature_permission)

        # Listen for document.title changes (React sends cmd via title)
        self._web.page().titleChanged.connect(self._on_title_changed)

        self._log_sig.connect(self._on_log)
        self._state_sig.connect(self._on_state)
        self._mute_sig.connect(self._on_mute)
        self._win_ctrl_sig.connect(self._on_win_ctrl)

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._enter_standby)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)

        # F1 toggles browser mic on/off for voice meter
        sc_mic = QShortcut(QKeySequence("F1"), self)
        sc_mic.activated.connect(self._toggle_mic)

    # ── Title bridge: React sets document.title = "cmd:xxx" ──────
    def _on_title_changed(self, title: str) -> None:
        if title.startswith("cmd:"):
            cmd = title[4:]
            if cmd.startswith("nodrag:"):
                self._no_drag = cmd[len("nodrag:") :] == "1"
            elif cmd == "minimize":
                self.showMinimized()
            elif cmd == "maximize":
                if self.isMaximized():
                    self.showNormal()
                else:
                    self.showMaximized()
            elif cmd == "close":
                self.close()

    def _owns(self, obj: object) -> bool:
        """Is this event ours? The filter is installed on QApplication so it can
        catch events from the native Chromium child windows that bypass the
        QWidget system, which means it also sees every OTHER window's events."""
        if not isinstance(obj, QObject):
            return False
        w: QObject | None = obj
        while w is not None:
            if w is self:
                return True
            w = w.parent() if hasattr(w, "parent") else None
        return False

    def _apply_zoom(self, value: float) -> None:
        self._zoom = max(_MIN_ZOOM, min(_MAX_ZOOM, round(value, 3)))
        self._web.setZoomFactor(self._zoom)
        _save_zoom(self._zoom)

    # ── Event filter: Ctrl+wheel zoom, and window drag from anywhere ────
    # Installed on QApplication to catch events from native Chromium child
    # windows that bypass the QWidget event system.
    def eventFilter(self, obj: object, event: QEvent) -> bool:
        etype = event.type()

        # ── Ctrl+wheel zoom, intercepted so it can be PERSISTED ──
        # QWebEngineView zooms on Ctrl+wheel by itself, but it forgets the level
        # on the next launch — so a layout the user had deliberately set came
        # back wrong every time. Handling it here keeps the same gesture, clamps
        # it to a sane range, and writes it to runtime/ui_prefs.json.
        if etype == QEvent.Type.Wheel and self._owns(obj):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta:
                    self._apply_zoom(self._zoom + (_ZOOM_STEP if delta > 0 else -_ZOOM_STEP))
                return True  # consumed: do not also let the page scroll

        if etype not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonRelease,
        ):
            return super().eventFilter(obj, event)

        # Only handle events belonging to this window or its descendants
        if not self._owns(obj):
            return super().eventFilter(obj, event)

        if etype == QEvent.Type.MouseButtonPress or etype == QEvent.Type.MouseButtonDblClick:
            if event.button() == Qt.MouseButton.LeftButton and not self._no_drag:
                self._drag_start = event.globalPosition().toPoint()
                self._window_start = self.pos()
                self._dragging = False
            return False

        if etype == QEvent.Type.MouseMove:
            if self._drag_start is not None and not self._dragging:
                delta = event.globalPosition().toPoint() - self._drag_start
                if delta.manhattanLength() > 10:
                    self._dragging = True
            if self._dragging:
                delta = event.globalPosition().toPoint() - self._drag_start
                self.move(self._window_start + delta)
                return True
            return False

        if etype == QEvent.Type.MouseButtonRelease:
            if self._dragging:
                self._dragging = False
                self._drag_start = None
                return True
            self._drag_start = None
            return False

        return super().eventFilter(obj, event)

    def _on_log(self, text: str) -> None:
        try:
            self._web.page().runJavaScript(
                f"window.__era_on_log && window.__era_on_log({json.dumps(text)})"
            )
        except Exception:
            pass

    def _on_state(self, state: str) -> None:
        try:
            self._web.page().runJavaScript(
                f"window.__era_on_state && window.__era_on_state({json.dumps(state)})"
            )
        except Exception:
            pass

    def _on_mute(self, muted: bool) -> None:
        self._muted = muted
        try:
            self._web.page().runJavaScript(
                f"window.__era_on_mute && window.__era_on_mute({json.dumps(muted)})"
            )
        except Exception:
            pass

    def _on_win_ctrl(self, action: str) -> None:
        """Execute window control on the main Qt thread."""
        if action == "minimize":
            self.showMinimized()
        elif action == "maximize":
            if self.isMaximized():
                self.showNormal()
            else:
                self.showMaximized()
        elif action == "restore":
            if self.isMaximized():
                self.showNormal()
            self.resize(_DEFAULT_W, _DEFAULT_H)
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(
                (screen.width() - _DEFAULT_W) // 2,
                (screen.height() - _DEFAULT_H) // 2,
            )
        elif action == "close":
            self.close()

    def _on_feature_permission(self, origin: QUrl, feature: QWebEnginePage.Feature) -> None:
        """Grant microphone permission for voice meter."""
        if feature == QWebEnginePage.Feature.MediaAudioCapture:
            self._web.page().setFeaturePermission(
                origin, feature, QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
            )

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        if self.on_text_command:
            threading.Thread(
                target=self.on_text_command,
                args=("__mute__" if self._muted else "__unmute__",),
                daemon=True,
            ).start()

    def _enter_standby(self) -> None:
        """F4 toggles: if in standby, wake up; if active, enter standby."""
        if self.on_text_command:
            # _muted reflects standby state — toggle accordingly
            cmd = "__unstandby__" if self._muted else "__standby__"
            threading.Thread(
                target=self.on_text_command,
                args=(cmd,),
                daemon=True,
            ).start()

    def _toggle_mic(self) -> None:
        """F1 toggles browser microphone on/off for voice meter."""
        try:
            self._web.page().runJavaScript("window.__era_toggle_mic && window.__era_toggle_mic()")
        except Exception:
            pass

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        """Detect maximize/restore and broadcast window state to React."""
        if event.type() == QEvent.Type.WindowStateChange:
            if hasattr(self, "_ui_ref") and self._ui_ref._ws_broadcast:
                self._ui_ref._broadcast_window_state()
        super().changeEvent(event)


class _RootShim:
    """Minimal shim so main.py can call ui.root.mainloop()."""

    def __init__(self, app: QApplication) -> None:
        self._app = app

    def mainloop(self) -> None:
        self._app.exec()

    def protocol(self, *_: object) -> None:
        pass


class EraUI:
    """
    Public API — unchanged from original. main.py calls these methods:
        set_state(state), write_log(text), set_mute(muted),
        start_speaking(), stop_speaking(), wait_for_api_key(),
        on_text_command setter.
    """

    def __init__(self, face_path: str, size: object = None) -> None:
        if not QApplication.instance():
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow()
        self._win._ui_ref = self  # Back-reference for changeEvent broadcast
        self._win.show()
        self.root = _RootShim(self._app)
        self._ws_broadcast = None  # Set by main.py after WebSocket server starts
        self.current_file: str | None = None  # Path of the most recently uploaded file
        self.on_file_upload = None  # Set by main.py; called with (name, mime, data_b64)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool) -> None:
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def on_text_command(self):  # type: ignore[override]
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb) -> None:
        self._win.on_text_command = cb

    def _emit_win(self, sig_name: str, *args) -> None:
        """Emit a MainWindow signal, tolerating a window whose underlying C++
        object was already destroyed during shutdown/relaunch. Accessing or
        emitting on a deleted QObject raises ``RuntimeError: wrapped C/C++
        object of type MainWindow has been deleted`` — harmless here, since the
        UI is going away anyway. WS broadcast still proceeds for live clients."""
        try:
            getattr(self._win, sig_name).emit(*args)
        except RuntimeError:
            pass

    def set_state(self, state: str) -> None:
        self._emit_win("_state_sig", state)
        self._broadcast({"type": "state", "state": state, "ts": time.time()})

    def write_log(self, text: str) -> None:
        self._emit_win("_log_sig", text)
        self._broadcast({"type": "log", "text": text, "ts": time.time()})

    def wait_for_api_key(self) -> None:
        pass

    def start_speaking(self) -> None:
        self.set_state("SPEAKING")

    def stop_speaking(self) -> None:
        if not self.muted:
            self.set_state("LISTENING")

    def set_mute(self, muted: bool) -> None:
        self._emit_win("_mute_sig", muted)
        self._broadcast({"type": "mute", "muted": muted, "ts": time.time()})

    def set_amplitude(self, level: float, bands: list[float] | None = None) -> None:
        """Broadcast Era's own speech amplitude (0..1) so the sphere animation
        syncs to the agent's voice. WS-only — deliberately lightweight, called
        at audio-chunk rate (~23 Hz).

        `bands` carries the per-band energies of the same chunk (low → high),
        which is what lets the sphere react as an EQUALIZER rather than just
        brightening: the UI maps each band to a latitude ring and displaces the
        shell by its energy. Omitted (None) means "no spectrum this frame" and
        the sphere falls back to the scalar level.

        Every value is forced finite and clamped here. json.dumps serializes a
        float('nan') as a bare `NaN`, which is NOT valid JSON — the browser's
        JSON.parse rejects the message and the UI drops it whole, `value` and
        all. A single non-finite band would therefore not just break the
        equalizer, it would freeze the sphere entirely. Clamping at this
        boundary means no upstream arithmetic slip can ever do that again.
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
        """Switch the UI theme by voice/backend command.

        theme: "dark", "light", or "toggle". Broadcast over WS; the React app
        re-emits it as an 'era:theme' event and App applies + persists it.
        """
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
        differently from a first choice. Collapsing them here would make a
        substitute indistinguishable from what was asked for — which is the
        exact thing this indicator exists to prevent.
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

        `selected` is the pinned ALIAS, or "" for automatic routing — and it is
        carried here rather than inferred from the `model` message because a
        pinned alias that fell back publishes a DIFFERENT alias as active. The
        switcher would then tick the substitute, telling the user they had
        chosen something they did not.

        Every field is coerced to a primitive here. json.dumps raises on a value
        it cannot serialise, and this runs through the same _broadcast as the
        state and log messages — one unserialisable entry from the registry
        would take the whole message down, not just the menu.
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
                continue  # a malformed entry costs one row, not the menu
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

    def set_ws_broadcast(self, broadcast_fn) -> None:
        """Called by main.py to link the WebSocket broadcast function."""
        self._ws_broadcast = broadcast_fn

    def window_control(self, action: str) -> None:
        """Handle window_control messages from the React frontend.
        Emits a signal so the actual widget manipulation happens on the main Qt thread.
        """
        self._emit_win("_win_ctrl_sig", action)
        # Broadcast state after a short delay to let the action complete.
        # daemon=True so a pending timer can never hold up interpreter exit —
        # this is a cosmetic UI notification, not work worth delaying shutdown for.
        import threading

        t = threading.Timer(0.3, self._broadcast_window_state)
        t.daemon = True
        t.start()

    def _broadcast_window_state(self) -> None:
        """Broadcast current window state (maximized) to all React clients.

        Runs on a threading.Timer 300ms after the window action, so the window
        can be GONE by the time it fires — closing Era right after a
        maximize/restore destroys the C++ MainWindow while this is still
        pending, and PyQt then raises "RuntimeError: wrapped C/C++ object of
        type MainWindow has been deleted" on the widget access. That happened 8
        times across the logs, all at shutdown. There is no useful state to
        report once the window is gone, so treat it as nothing to broadcast.
        """
        try:
            maximized = self._win.isMaximized()
        except RuntimeError:
            return  # window already destroyed — nothing to report
        self._broadcast(
            {
                "type": "window_state",
                "maximized": maximized,
                "ts": time.time(),
            }
        )

    def _broadcast(self, msg: dict) -> None:
        if self._ws_broadcast:
            try:
                self._ws_broadcast(msg)
            except Exception:
                pass
