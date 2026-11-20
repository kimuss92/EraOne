"""Small Qt launcher/tray process; backend credentials travel through stdin only."""

from __future__ import annotations

import json
import subprocess
import sys
import threading

_process: subprocess.Popen | None = None
_lock = threading.Lock()
ORB_SIZE = 48
MAGNET_MARGIN = 12
# Consecutive refused connections before the launcher concludes Era is gone.
# Timeouts never count: a busy backend is not a dead one.
OFFLINE_EXIT_AFTER = 3


def snap_to_edge(x: int, y: int, area: tuple[int, int, int, int], size: int = ORB_SIZE):
    """Top-left for an orb at (x, y) pulled to the nearest edge of area (l, t, r, b).

    Inclusive Qt coordinates; the other axis is clamped so the orb stays on screen.
    """
    left, top, right, bottom = area
    hi_x, hi_y = right - size - MAGNET_MARGIN + 1, bottom - size - MAGNET_MARGIN + 1
    lo_x, lo_y = left + MAGNET_MARGIN, top + MAGNET_MARGIN
    x, y = min(max(x, lo_x), hi_x), min(max(y, lo_y), hi_y)
    distances = {"l": x - lo_x, "r": hi_x - x, "t": y - lo_y, "b": hi_y - y}
    edge = min(distances, key=distances.get)
    if edge == "l":
        x = lo_x
    elif edge == "r":
        x = hi_x
    elif edge == "t":
        y = lo_y
    else:
        y = hi_y
    return x, y


def _position_path():
    from app.hud import settings

    return settings.settings_path().parent / "floating_launcher_pos.json"


def load_position() -> tuple[int, int] | None:
    try:
        data = json.loads(_position_path().read_text(encoding="utf-8"))
        return int(data["x"]), int(data["y"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_position(x: int, y: int) -> None:
    try:
        path = _position_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
    except OSError:
        pass  # remembering the spot is a convenience, never a failure


def restore_access_when_closed(current: dict, *, workspace_was_open: bool = True) -> dict:
    """Restore access on a workspace-close transition, respecting a later Hide."""
    from app.hud import browser, settings

    if (
        workspace_was_open
        and not current.get("floating_icon", False)
        and browser.find_hud_window() is None
    ):
        current = {**current, **settings.update({"floating_icon": True})}
    return current


def ensure_started(port: int, token: str) -> bool:
    global _process
    from app.hud.launcher import REPO
    from core.win_subprocess import NO_WINDOW

    with _lock:
        if _process is not None and _process.poll() is None:
            return True
        _process = subprocess.Popen(
            [sys.executable, "-m", "app.hud.floating_launcher"],
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        assert _process.stdin is not None
        _process.stdin.write(json.dumps({"port": port, "token": token}).encode() + b"\n")
        _process.stdin.close()
        return True


def main() -> int:
    import math
    import queue
    import time
    import urllib.request

    from PyQt6.QtCore import (
        QEasingCurve,
        QElapsedTimer,
        QPoint,
        QPropertyAnimation,
        QRectF,
        Qt,
        QTimer,
    )
    from PyQt6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPen
    from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QPushButton, QSystemTrayIcon

    from app.hud import browser, settings, shortcuts
    from app.hud.instance import NamedMutex, _scoped
    from app.hud.launcher import log

    data = json.loads(sys.stdin.readline())
    port, token = int(data["port"]), data["token"]
    mutex = NamedMutex(_scoped("Local\\EraOne.FloatingLauncher"))
    for _ in range(20):
        if mutex.acquire():
            break
        time.sleep(0.5)
    else:
        log("Floating launcher: another instance holds the mutex")
        return 0
    base = f"http://127.0.0.1:{port}"
    updates: queue.Queue = queue.Queue()
    jobs: queue.Queue = queue.Queue()

    def request(path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-EraOne-Token": token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            result = json.load(response)
        if result.get("accepted") is False or result.get("error"):
            raise RuntimeError(result.get("error") or "Era did not accept the action")
        return result

    def work(fn) -> None:
        jobs.put(fn)

    def run_jobs() -> None:
        # Serialize settings writes and polls so an older response cannot undo
        # the user's latest click. Qt state is only touched by drain().
        while True:
            fn = jobs.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as exc:
                updates.put(("error", str(exc) or type(exc).__name__))

    threading.Thread(target=run_jobs, daemon=True).start()

    def open_hud() -> None:
        work(lambda: browser.open_hud(base + "/", prefer_app=settings.load()["app_mode"]))

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    icon = QIcon(str(shortcuts.icon_path()))
    app.setWindowIcon(icon)

    class Orb(QPushButton):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("ERA ONE Floating Launcher")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setFixedSize(ORB_SIZE, ORB_SIZE)
            self.setAccessibleName("Open ERA ONE")
            self.setWindowIcon(icon)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("ERA ONE · click to open · drag to move · right-click for menu")
            self.logo = icon.pixmap(ORB_SIZE * 2, ORB_SIZE * 2)
            self.clock = QElapsedTimer()
            self.clock.start()
            self.animation = QTimer(self)
            self.animation.setInterval(33)
            self.animation.timeout.connect(self.update)
            self.move(self.initial_position())
            self.start_pos = None
            self.dragged = False
            self.snap = QPropertyAnimation(self, b"pos", self)
            self.snap.setDuration(180)
            self.snap.setEasingCurve(QEasingCurve.Type.OutCubic)

        def initial_position(self) -> QPoint:
            saved = load_position()
            if saved is not None:
                point = QPoint(*saved)
                center = point + QPoint(ORB_SIZE // 2, ORB_SIZE // 2)
                if app.screenAt(center) is not None:  # that monitor is still attached
                    return point
            screen = app.primaryScreen().availableGeometry()
            return QPoint(screen.right() - 90, screen.bottom() - 110)

        def settle(self) -> None:
            """After a drag: pull to the nearest screen edge (magnet on) and remember."""
            target = self.pos()
            if state["magnet"]:
                center = target + QPoint(ORB_SIZE // 2, ORB_SIZE // 2)
                screen = app.screenAt(center) or app.primaryScreen()
                g = screen.availableGeometry()
                target = QPoint(
                    *snap_to_edge(
                        target.x(), target.y(), (g.left(), g.top(), g.right(), g.bottom())
                    )
                )
                self.snap.stop()
                self.snap.setStartValue(self.pos())
                self.snap.setEndValue(target)
                self.snap.start()
            save_position(target.x(), target.y())

        def showEvent(self, event):
            self.animation.start()
            super().showEvent(event)

        def hideEvent(self, event):
            self.animation.stop()
            super().hideEvent(event)

        def closeEvent(self, event):
            # Alt+F4 retains access. Explicit exit bypasses this close veto.
            event.ignore()
            self.show()
            work(lambda: request("/api/settings", {"floating_icon": True}))

        def paintEvent(self, _event):
            seconds = self.clock.elapsed() / 1000
            pulse = (math.sin(seconds * math.tau / 3.5) + 1) / 2
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            glow = QColor("#ac7bfa")
            glow.setAlpha(int(45 + 70 * pulse))
            painter.setPen(QPen(glow, 2))
            ring = QRectF(2, 2, ORB_SIZE - 4, ORB_SIZE - 4)
            painter.drawEllipse(ring)
            side = ORB_SIZE * (60 + 2 * pulse) / 72
            inset = (ORB_SIZE - side) / 2
            painter.drawPixmap(
                QRectF(inset, inset, side, side), self.logo, QRectF(self.logo.rect())
            )
            glow.setAlpha(230 if self.underMouse() else 180)
            painter.setPen(QPen(glow, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(ring, int(-seconds * 60 * 16), 48 * 16)
            painter.end()

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.RightButton:
                menu.popup(event.globalPosition().toPoint())
                return
            self.start_pos = event.globalPosition().toPoint()
            self.origin = self.pos()
            self.dragged = False

        def mouseMoveEvent(self, event):
            if self.start_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
                delta = event.globalPosition().toPoint() - self.start_pos
                if delta.manhattanLength() > 4:
                    self.dragged = True
                    self.snap.stop()
                    self.move(self.origin + delta)

        def mouseReleaseEvent(self, event):
            if (
                event.button() == Qt.MouseButton.LeftButton
                and self.start_pos is not None
                and not self.dragged
            ):
                open_hud()
            elif self.dragged:
                self.settle()
            self.start_pos = None

    state = {"magnet": settings.load()["floating_magnet"]}
    orb = Orb()
    menu = QMenu()
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("ERA ONE")
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: open_hud() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )

    def add(label, callback):
        action = QAction(label, menu)
        action.triggered.connect(callback)
        menu.addAction(action)
        return action

    add("Open App", open_hud)
    add("Open Workspace", open_hud)
    add("Close Workspace", lambda: work(browser.close_hud_windows))

    def save_settings(patch: dict) -> None:
        work(lambda: updates.put(("settings", request("/api/settings", patch))))

    startup = add(
        "Show Workspace On Startup",
        lambda checked: save_settings({"open_hud_on_start": checked}),
    )
    startup.setCheckable(True)
    startup.setChecked(settings.load()["open_hud_on_start"])
    menu.addSeparator()
    # Radio pair: the checkmark shows which one is active right now.
    visibility = QActionGroup(menu)
    visibility.setExclusive(True)
    show_icon = add("Show Floating Icon", lambda: save_settings({"floating_icon": True}))
    hide_icon = add("Hide Floating Icon", lambda: save_settings({"floating_icon": False}))
    for action in (show_icon, hide_icon):
        action.setCheckable(True)
        visibility.addAction(action)

    def set_magnet(checked: bool) -> None:
        state["magnet"] = checked
        if checked:
            orb.settle()
        save_settings({"floating_magnet": checked})

    magnet = add("Magnet To Screen Edge", set_magnet)
    magnet.setCheckable(True)
    magnet.setChecked(state["magnet"])

    def show_state(value: dict) -> None:
        visible = value.get("floating_icon", False)
        orb.setVisible(visible)
        (show_icon if visible else hide_icon).setChecked(True)
        startup.setChecked(value["open_hud_on_start"])
        if "floating_magnet" in value:
            state["magnet"] = value["floating_magnet"]
            magnet.setChecked(state["magnet"])

    show_state(settings.load())
    menu.addSeparator()

    def lifecycle(action: str) -> None:
        if (
            QMessageBox.question(orb, "ERA ONE", f"{action.title()} Era One?")
            == QMessageBox.StandardButton.Yes
        ):
            work(lambda: request("/api/" + action, {"confirm": True}))

    add("Restart", lambda: lifecycle("restart"))
    add("Quit", lambda: lifecycle("shutdown"))

    def exit_launcher() -> None:
        timer.stop()
        drain_timer.stop()
        menu.close()
        tray.hide()
        orb.hide()
        # QApplication.quit() sends close events and Orb.closeEvent vetoes it.
        # exit() ends the event loop unconditionally for intentional exits.
        app.exit(0)

    add("Exit Launcher Only", exit_launcher)
    tray.show()
    failures = 0
    polling = False
    workspace_was_open = browser.find_hud_window() is not None

    def poll() -> None:
        nonlocal polling
        if polling:
            return
        polling = True

        def fetch():
            nonlocal workspace_was_open
            try:
                current = restore_access_when_closed(
                    request("/api/launcher"), workspace_was_open=workspace_was_open
                )
                workspace_was_open = browser.find_hud_window() is not None
                updates.put(("poll", current))
            except Exception as exc:
                log(f"Floating launcher poll failed: {type(exc).__name__}")
                # Only a refused/closed connection means Era is gone; a slow
                # answer (TimeoutError) means Era is busy and the orb must stay.
                reason = getattr(exc, "reason", exc)
                busy = isinstance(reason, TimeoutError) or "timed out" in str(reason)
                updates.put(("busy" if busy else "offline", None))

        work(fetch)

    def drain() -> None:
        nonlocal failures, polling
        while not updates.empty():
            kind, value = updates.get_nowait()
            if kind in {"settings", "poll"}:
                if kind == "poll":
                    polling, failures = False, 0
                show_state(value)
            elif kind == "busy":
                polling, failures = False, 0  # it answered the connect: alive, just slow
            elif kind == "offline":
                polling = False
                failures += 1
                if failures >= OFFLINE_EXIT_AFTER:
                    exit_launcher()
            else:
                tray.showMessage("ERA ONE", "Launcher action failed: " + value)

    timer, drain_timer = QTimer(), QTimer()
    timer.timeout.connect(poll)
    timer.start(2000)
    drain_timer.timeout.connect(drain)
    drain_timer.start(100)
    poll()
    try:
        return app.exec()
    finally:
        jobs.put(None)
        tray.hide()
        orb.hide()
        mutex.release()


if __name__ == "__main__":
    raise SystemExit(main())
