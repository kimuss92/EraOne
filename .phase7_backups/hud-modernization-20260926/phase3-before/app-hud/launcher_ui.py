"""Launcher splash and startup-error dialog (tkinter: stdlib, no console).

The splash shows REAL startup stages as the launcher observes them — which
processes hold their mutex, whether /api/health answers and what it reports.
There is no progress percentage, because there is nothing honest to compute one
from. It only appears if startup takes longer than a moment.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

BG = "#03070c"
PANEL = "#08111b"
LINE = "#12304a"
TEXT = "#cfe8f5"
DIM = "#5f8aa3"
ACCENT = "#5fd4ff"
WARN = "#f2b24b"
BAD = "#ff7a6e"

_STATE_COLOR = {
    "ready": ACCENT,
    "online": ACCENT,
    "connected": ACCENT,
    "active": ACCENT,
    "starting": WARN,
    "initializing": WARN,
    "connecting": WARN,
    "reconnecting": WARN,
    "waiting": DIM,
    "failed": BAD,
}


def _dpi_aware() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def run_with_splash(task: Callable[[Callable[[list], None]], Any], show_after_s: float = 1.2):
    """Run ``task(progress)`` on a worker thread while a splash reflects the
    stages it reports. Returns the task's result or re-raises its exception.
    ``progress`` takes a list of (name, state) pairs."""
    try:
        import tkinter as tk
    except Exception:
        return task(lambda _stages: None)

    _dpi_aware()
    box: dict[str, Any] = {}
    updates: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            box["result"] = task(updates.put)
        except BaseException as e:  # handed back to the caller below
            box["error"] = e
        finally:
            updates.put(None)

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    w, h = 440, 290
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    frame = tk.Frame(root, bg=BG, highlightbackground=LINE, highlightthickness=1)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text="E R A   O N E", fg=TEXT, bg=BG, font=("Segoe UI Semibold", 20)).pack(
        pady=(26, 2)
    )
    tk.Label(frame, text="INITIALIZING", fg=DIM, bg=BG, font=("Consolas", 9)).pack(pady=(0, 16))
    rows_frame = tk.Frame(frame, bg=BG)
    rows_frame.pack(fill="x", padx=48)
    rows: dict[str, tuple[Any, Any]] = {}

    def render(stages: list) -> None:
        for name, state in stages:
            if name not in rows:
                r = tk.Frame(rows_frame, bg=BG)
                r.pack(fill="x", pady=2)
                a = tk.Label(r, text=name, fg=DIM, bg=BG, font=("Consolas", 10), anchor="w")
                a.pack(side="left")
                b = tk.Label(r, text="", fg=DIM, bg=BG, font=("Consolas", 10), anchor="e")
                b.pack(side="right")
                rows[name] = (a, b)
            label = str(state or "…")
            rows[name][1].configure(
                text=label.upper(), fg=_STATE_COLOR.get(label.lower().split(" ")[0], TEXT)
            )

    shown = {"v": False}

    def show() -> None:
        if "result" not in box and "error" not in box:
            root.deiconify()
            shown["v"] = True

    def poll() -> None:
        try:
            while True:
                item = updates.get_nowait()
                if item is None:
                    root.quit()
                    return
                render(item)
        except queue.Empty:
            pass
        root.after(120, poll)

    threading.Thread(target=worker, name="LauncherWait", daemon=True).start()
    root.after(int(show_after_s * 1000), show)
    root.after(120, poll)
    root.mainloop()
    root.destroy()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def error_dialog(reason: str, detail: str = "") -> str:
    """Blocking dialog. Returns "retry", "logs", "diagnostics" or "close"."""
    try:
        import tkinter as tk
    except Exception:
        return _messagebox_fallback(reason, detail)

    _dpi_aware()
    choice = {"v": "close"}
    root = tk.Tk()
    root.title("ERA ONE")
    root.configure(bg=BG)
    root.resizable(False, False)
    try:
        from app.hud.shortcuts import icon_path

        if icon_path().exists():
            root.iconbitmap(str(icon_path()))
    except Exception:
        pass
    pad = {"padx": 28}
    tk.Label(
        root, text="ERA ONE could not start", fg=TEXT, bg=BG, font=("Segoe UI Semibold", 15)
    ).pack(anchor="w", pady=(24, 6), **pad)
    tk.Label(
        root, text=reason, fg=WARN, bg=BG, font=("Segoe UI", 10), wraplength=460, justify="left"
    ).pack(anchor="w", **pad)
    if detail:
        tk.Label(
            root,
            text=detail,
            fg=DIM,
            bg=BG,
            font=("Consolas", 9),
            wraplength=460,
            justify="left",
        ).pack(anchor="w", pady=(8, 0), **pad)
    bar = tk.Frame(root, bg=BG)
    bar.pack(fill="x", pady=22, **pad)

    def pick(v: str) -> None:
        choice["v"] = v
        root.destroy()

    for text, value in (
        ("Retry", "retry"),
        ("Open Logs", "logs"),
        ("Diagnostics", "diagnostics"),
        ("Close", "close"),
    ):
        tk.Button(
            bar,
            text=text,
            command=lambda v=value: pick(v),
            bg=PANEL,
            fg=TEXT,
            activebackground=LINE,
            activeforeground=TEXT,
            relief="flat",
            padx=14,
            pady=6,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 8))
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 2
    root.geometry(f"+{x}+{y}")
    root.attributes("-topmost", True)
    root.mainloop()
    return choice["v"]


def _messagebox_fallback(reason: str, detail: str) -> str:
    try:
        import ctypes

        # MB_RETRYCANCEL | MB_ICONWARNING
        r = ctypes.windll.user32.MessageBoxW(
            None, f"{reason}\n\n{detail}", "ERA ONE could not start", 0x5 | 0x30
        )
        return "retry" if r == 4 else "close"
    except Exception:
        return "close"
