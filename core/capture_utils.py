from __future__ import annotations

import io
import sys

try:
    import PIL.Image

    _PIL = True
except ImportError:
    _PIL = False

_WIN32 = False
if sys.platform == "win32":
    try:
        import psutil
        import win32gui
        import win32process

        _WIN32 = True
    except Exception:
        pass


def get_active_window_rect() -> tuple[int, int, int, int] | None:
    if not _WIN32:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = (win32gui.GetWindowText(hwnd) or "").lower()
        if any(kw in title for kw in ["mark-xxxix", "j.a.r.v.i.s", "main.py"]):
            return None
        rect = win32gui.GetWindowRect(hwnd)
        if rect is None:
            return None
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return None
        return (left, top, right, bottom)
    except Exception:
        return None


def _is_browser_process(pid: int) -> bool:
    browser_exes = {"chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe"}
    try:
        proc = psutil.Process(pid)
        return proc.name().lower() in browser_exes
    except Exception:
        return False


def get_browser_viewport_rect() -> tuple[int, int, int, int] | None:
    if not _WIN32:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not _is_browser_process(pid):
            return None
        rect = get_active_window_rect()
        if rect is None:
            return None
        left, top, right, bottom = rect
        viewport_top = top + 70
        if viewport_top >= bottom:
            return None
        return (left, viewport_top, right, bottom)
    except Exception:
        return None


def _clamp_rect(
    rect: tuple[int, int, int, int],
    img_width: int,
    img_height: int,
    offset_x: int,
    offset_y: int,
) -> tuple[int, int, int, int] | None:
    left, top, right, bottom = rect
    px_left = left - offset_x
    px_top = top - offset_y
    px_right = right - offset_x
    px_bottom = bottom - offset_y
    px_left = max(0, min(px_left, img_width))
    px_top = max(0, min(px_top, img_height))
    px_right = max(0, min(px_right, img_width))
    px_bottom = max(0, min(px_bottom, img_height))
    if px_right <= px_left or px_bottom <= px_top:
        return None
    return (px_left, px_top, px_right, px_bottom)


def crop_raw_image(
    raw_png: bytes,
    rect: tuple[int, int, int, int],
    offset_x: int,
    offset_y: int,
) -> bytes | None:
    if not _PIL:
        return None
    try:
        img = PIL.Image.open(io.BytesIO(raw_png)).convert("RGB")
        w, h = img.size
        clamped = _clamp_rect(rect, w, h, offset_x, offset_y)
        if clamped is None:
            return None
        cropped = img.crop(clamped)
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def compress_for_mode(
    img_bytes: bytes,
    mode: str,
    max_width: int,
    jpeg_quality: int,
) -> tuple[bytes, str, int, int]:
    if not _PIL:
        return img_bytes, "image/png", len(img_bytes), len(img_bytes)
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        if w > max_width:
            ratio = max_width / w
            new_w = max_width
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=False)
        compressed = buf.getvalue()
        return compressed, "image/jpeg", len(img_bytes), len(compressed)
    except Exception as e:
        print(f"[Vision] ⚠️  Compress for mode failed: {e}")
        return img_bytes, "image/png", len(img_bytes), len(img_bytes)
