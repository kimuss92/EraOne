"""Launcher splash and startup-error dialog (tkinter: stdlib, no console).

The splash shows REAL startup stages as the launcher observes them — which
processes hold their mutex, whether /api/health answers and what it reports.
The bar fills by how many of those stages are up (e.g. 3/6) — never an
invented percentage. It only appears if startup takes longer than a moment.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

BG = "#010208"
PANEL = "#070b1d"
LINE = "#1a2550"
TEXT = "#e6ecff"
DIM = "#7c88ad"
ACCENT = "#8fd3ff"
RING = "#a8d8ff"
DEEP = "#2747d9"
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
# A stage counts as done once it reports anything but one of these (the model
# and voice report their names, the core its agent state).
_PENDING = {"", "…", "waiting", "starting", "initializing", "connecting", "reconnecting", "failed"}


def _dpi_aware() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _mix(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colours (Tk canvas items have no alpha)."""
    t = max(0.0, min(1.0, t))
    pa = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb, strict=True))


def _stage_done(state: Any) -> bool:
    return str(state or "").lower().split(" ")[0] not in _PENDING


def run_with_splash(task: Callable[[Callable[[list], None]], Any], show_after_s: float = 1.2):
    """Run ``task(progress)`` on a worker thread while a splash reflects the
    stages it reports. Returns the task's result or re-raises its exception.
    ``progress`` takes a list of (name, state) pairs.

    The look matches the HUD's "Eclipse" theme: a black void ringed in blue
    light with inward fibres and an orbiting arc. The bar fills by the number of
    stages that have actually come up — never an invented percentage."""
    try:
        import tkinter as tk
    except Exception:
        return task(lambda _stages: None)

    import math

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
    w, h = 460, 430
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")
    cv = tk.Canvas(root, width=w, height=h, bg=BG, highlightthickness=0, bd=0)
    cv.pack(fill="both", expand=True)

    # backdrop: a deep-navy bloom behind the eclipse, drawn once
    cx, cy, R = w // 2, 112, 46
    for i in range(26, 0, -1):
        r = 30 + i * 9
        glow = (1 - i / 26) ** 1.6 * 0.9
        cv.create_oval(
            cx - r * 1.3, cy - r, cx + r * 1.3, cy + r, outline="", fill=_mix(BG, "#071466", glow)
        )
    cv.create_rectangle(0, 0, w - 1, h - 1, outline=LINE)
    corners = ((10, 10, 1, 1), (w - 11, 10, -1, 1), (10, h - 11, 1, -1), (w - 11, h - 11, -1, -1))
    for bx, by, dx, dy in corners:
        cv.create_line(bx, by + 12 * dy, bx, by, bx + 12 * dx, by, fill=_mix(BG, ACCENT, 0.55))

    # the eclipse: halo rings, void, fibres, rim, orbiting arcs
    for i in range(10, 0, -1):
        r = R + i * 2.2
        halo = _mix(BG, DEEP, (1 - i / 10) ** 2 * 0.8)
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=halo, width=2)
    cv.create_oval(cx - R, cy - R, cx + R, cy + R, fill="#000000", outline="")
    bleed = _mix("#000000", "#10308f", 0.16)
    cv.create_oval(
        cx - R * 0.62, cy + R * 0.5, cx + R * 0.62, cy + R * 0.97, fill=bleed, outline=""
    )
    n_fib = 72
    fibre_colour = _mix("#000000", RING, 0.55)
    fibres = [
        (k / n_fib * math.tau, cv.create_line(0, 0, 0, 0, fill=fibre_colour)) for k in range(n_fib)
    ]
    rim = cv.create_oval(cx - R, cy - R, cx + R, cy + R, outline=RING, width=2)
    o1, o2 = R + 12, R + 20
    orbit = cv.create_arc(
        cx - o1, cy - o1, cx + o1, cy + o1, start=90, extent=70, style="arc", outline=ACCENT
    )
    orbit2 = cv.create_arc(
        cx - o2,
        cy - o2,
        cx + o2,
        cy + o2,
        start=250,
        extent=40,
        style="arc",
        outline=_mix(BG, ACCENT, 0.45),
    )

    cv.create_text(cx, 196, text="E R A   O N E", fill=TEXT, font=("Segoe UI Semibold", 19))
    sub = cv.create_text(cx, 222, text="INITIALIZING", fill=DIM, font=("Consolas", 9))

    # progress: hairline track + fill + travelling glint, then the stage rows
    bx0, bx1, by_ = 70, w - 70, 250
    cv.create_line(bx0, by_, bx1, by_, fill=_mix(BG, ACCENT, 0.16), width=2)
    fill = cv.create_line(bx0, by_, bx0, by_, fill=DEEP, width=2)
    fill_hi = cv.create_line(bx0, by_, bx0, by_, fill=ACCENT, width=2)
    glint = cv.create_oval(0, 0, 0, 0, fill="#ffffff", outline="")
    count = cv.create_text(bx1, by_ + 14, text="", fill=DIM, font=("Consolas", 8), anchor="e")
    rows: dict[str, list] = {}  # name -> [label item, state item, born_at, colour]
    st = {"t": 0.0, "shown_p": 0.0, "target_p": 0.0}

    def render(stages: list) -> None:
        for name, state in stages:
            if name not in rows:
                ry = 284 + len(rows) * 22
                a = cv.create_text(bx0, ry, text=name, fill=BG, font=("Consolas", 10), anchor="w")
                b = cv.create_text(bx1, ry, text="", fill=BG, font=("Consolas", 10), anchor="e")
                rows[name] = [a, b, st["t"] + len(rows) * 0.09, TEXT]
            label = str(state or "…")
            rows[name][3] = _STATE_COLOR.get(label.lower().split(" ")[0], TEXT)
            cv.itemconfigure(rows[name][1], text=label.upper())
        done = sum(_stage_done(s) for _, s in stages)
        st["target_p"] = done / max(1, len(stages))
        cv.itemconfigure(count, text=f"{done}/{len(stages)} READY")

    def tick() -> None:
        if "result" in box or "error" in box:
            return
        st["t"] += 0.033
        t = st["t"]
        rot = (t * 120) % 360
        cv.itemconfigure(orbit, start=90 - rot)
        cv.itemconfigure(orbit2, start=250 + rot * 0.6)
        breathe = 0.5 + 0.5 * math.sin(t * 2.2)
        cv.itemconfigure(rim, outline=_mix(DEEP, RING, 0.65 + 0.35 * breathe))
        for a, item in fibres:
            below = max(0.0, math.sin(a))
            wave = max(0.0, math.sin(a * 3 - t * 3.0))
            ln = R * (0.06 + 0.22 * below * below + 0.12 * wave * wave)
            c, s = math.cos(a), math.sin(a)
            cv.coords(
                item,
                cx + c * (R - 1),
                cy + s * (R - 1),
                cx + c * (R - 1 - ln),
                cy + s * (R - 1 - ln),
            )
        st["shown_p"] += (st["target_p"] - st["shown_p"]) * 0.08
        fx = bx0 + (bx1 - bx0) * st["shown_p"]
        cv.coords(fill, bx0, by_, fx, by_)
        cv.coords(fill_hi, max(bx0, fx - 40), by_, fx, by_)
        gx = bx0 + ((t * 0.45) % 1.0) * (bx1 - bx0)
        cv.coords(glint, gx - 2, by_ - 2, gx + 2, by_ + 2)
        cv.itemconfigure(glint, fill=_mix(BG, "#ffffff", 0.9 if gx <= fx else 0.35))
        cv.itemconfigure(sub, text="INITIALIZING" + "." * (int(t * 2.5) % 4))
        for a_id, b_id, born, colour in rows.values():
            k = max(0.0, min(1.0, (t - born) / 0.35))  # staggered fade-in
            cv.itemconfigure(a_id, fill=_mix(BG, DIM, k))
            cv.itemconfigure(b_id, fill=_mix(BG, colour, k))
        root.after(33, tick)

    def show() -> None:
        if "result" not in box and "error" not in box:
            root.deiconify()
            tick()

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
