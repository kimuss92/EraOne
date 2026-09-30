# computer_control.py
import io
import json
import random
import re
import string
import subprocess
import time
from pathlib import Path

from core.process_awareness import format_running_apps
from core.process_awareness import snapshot as process_snapshot
from core.utils import (
    get_api_key,
    get_base_dir,
    get_memory_dir,
    get_os,
    log,
)
from core.window_awareness import format_context as window_context
from core.window_awareness import get_active_window

try:
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip

    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False


_BASE = get_base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"
# Resolve through get_memory_dir(), NOT _BASE / "memory": the latter hard-binds
# the live store, so the test suite (which redirects ERA_MEMORY_DIR to a
# throwaway copy) was reading and writing the user's real long_term.json. Bound
# at import time is fine — conftest.py sets ERA_MEMORY_DIR before any project
# module is imported.
_MEMORY_PATH = get_memory_dir() / "long_term.json"


_SAFE_SCREENSHOT_ROOTS = (Path.home(),)


def safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "era_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback


def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


_FIRST_NAMES = [
    "Alex",
    "Jordan",
    "Taylor",
    "Morgan",
    "Casey",
    "Riley",
    "Drew",
    "Quinn",
    "Avery",
    "Blake",
    "Cameron",
    "Dakota",
    "Emerson",
    "Finley",
    "Harper",
]
_LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Wilson",
    "Moore",
    "Taylor",
    "Anderson",
    "Thomas",
    "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last = random.choice(_LAST_NAMES).lower()
        num = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200, 999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"


def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}


def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _require_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: str | int = "normal") -> str:
    _require_pyautogui()
    from utils.scroll import resolve_amount

    px = resolve_amount(amount)
    # pyautogui.scroll uses "clicks" — approximately 40 px per click on most systems
    clicks = max(1, round(px / 40))
    if direction in ("up", "left"):
        clicks = -clicks
    vertical = direction in ("up", "down")
    pyautogui.scroll(clicks) if vertical else pyautogui.hscroll(clicks)
    return f"Scrolled {direction} \u00d7{abs(clicks)} clicks ({px}px)"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_get() -> str:
    if _PYPERCLIP:
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        pyautogui.hotkey("ctrl", "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = safe_screenshot_path(save_path)
    img = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"


def _focus_window(title: str) -> str:
    os_name = get_os()

    if os_name == "windows":
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (Windows) failed: {e}"

    if os_name == "mac":
        script = (
            f'tell application "System Events" to '
            f'set frontmost of (first process whose name contains "{title}") to true'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (macOS) failed: {e}"

    if os_name == "linux":
        try:
            result = subprocess.run(
                ["wmctrl", "-a", title],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                time.sleep(0.3)
                return f"Focused window: {title}"
        except FileNotFoundError:
            pass
        try:
            result = subprocess.run(
                ["xdotool", "search", "--name", title, "windowactivate"],
                capture_output=True,
                timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except FileNotFoundError:
            return "focus_window (Linux) requires wmctrl or xdotool"
        except Exception as e:
            return f"focus_window (Linux) failed: {e}"

    return f"focus_window: unknown OS '{os_name}'"


def _screen_find(description: str) -> tuple[int, int] | None:
    api_key = get_api_key()
    if not api_key:
        print("[ComputerControl] ⚠️ No API key for screen_find")
        return None

    try:
        from google import genai
        from google.genai import types as gtypes

        _require_pyautogui()
        w, h = pyautogui.size()
        img = pyautogui.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        client = genai.Client(api_key=api_key)
        prompt = (
            f"This is a screenshot of a {w}×{h} pixel screen. "
            f"Locate the UI element described as: '{description}'. "
            f"Reply with ONLY the center coordinates as: x,y "
            f"If the element is not visible, reply: NOT_FOUND"
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=[
                gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
        )

        text = (response.text or "").strip()
        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            return int(match.group(1)), int(match.group(2))

    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")

    return None


def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount — preset (small/normal/large/page) or pixel number (default: normal)
      seconds       : wait duration
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      random_data   — generate fake form data
      user_data     — pull real data from memory
      running_apps  — list running processes via psutil
      active_window — return current active window/app
      system_snapshot — return window + process awareness snapshot
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    if not action:
        return "No action specified for computer_control."

    if player:
        player.write_log(f"[Computer] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

    try:
        if action == "type":
            return _type(params.get("text", ""))

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            return _move(int(params.get("x", 0)), int(params.get("y", 0)))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)),
                int(params.get("y1", 0)),
                int(params.get("x2", 0)),
                int(params.get("y2", 0)),
            )

        if action == "hotkey":
            raw = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return _hotkey(*keys)

        if action == "press":
            return _press(params.get("key", "enter"))

        if action == "scroll":
            direction = params.get("direction", "down")
            amount = params.get("amount", "normal")
            target_platform = params.get("target", "").lower().strip()

            # Normalize small scroll amounts (1–10) to normal 700px repeat
            if amount is not None:
                try:
                    num = int(amount)
                    if 1 <= abs(num) <= 10:
                        print(
                            f"[ComputerControl] Normalizing small scroll amount={num} to normal (700px)"
                        )
                        amount = "normal"
                except (ValueError, TypeError):
                    pass

            # If router injected a platform target, acquire the owned page first
            scroll_page = None
            if target_platform in ("youtube", "netflix", "yt", "nf"):
                try:
                    from actions.platform_tab import get_or_open_platform_tab

                    canon = "youtube" if target_platform in ("youtube", "yt") else "netflix"
                    tab_result = get_or_open_platform_tab(platform=canon)
                    scroll_page = tab_result.page
                except Exception as exc:
                    log("ComputerControl", f"scroll platform_tab acquire failed: {exc}")
                    scroll_page = None

            # Try browser JS scroll first
            try:
                from actions.browser_control import get_browser_session
                from utils.scroll import resolve_amount, resolve_direction

                sess = get_browser_session()
                if sess:
                    try:
                        page = (
                            scroll_page
                            if scroll_page is not None
                            else sess.run(sess._current_page())
                        )
                        if page is None:
                            raise RuntimeError("No page available for JS scroll")
                        url = str(page.url).lower() if page else ""
                        # Always attempt JS scroll on owned platform pages
                        if scroll_page is not None or (url and not url.startswith("about:")):
                            px = abs(resolve_amount(amount))
                            x_sign, y_sign = resolve_direction(direction)
                            scroll_px = y_sign * px
                            smart_scroll_js = f"""
                            const amount = {scroll_px};
                            const result = {{method:"js",target:"",amount_px:{px},before:{{}},after:{{}},changed:false}};
                            result.before = {{
                                windowScrollY: window.scrollY,
                                scrollingElement: document.scrollingElement ? document.scrollingElement.scrollTop : null,
                                activeElement: document.activeElement ? document.activeElement.scrollTop : null
                            }};
                            let scrolled = false;
                            const ae = document.activeElement;
                            if (ae && ae.scrollHeight > ae.clientHeight + 10 && typeof ae.scrollBy === 'function') {{
                                const st = ae.scrollTop;
                                ae.scrollBy({{top: amount, behavior: 'smooth'}});
                                if (ae.scrollTop !== st) {{ scrolled = true; result.target = 'activeElement'; }}
                            }}
                            if (!scrolled) {{
                                const all = document.querySelectorAll('*');
                                let best = null, bestSH = 0;
                                for (const el of all) {{
                                    if (el.scrollHeight > el.clientHeight + 50 && el.getBoundingClientRect().width > 0) {{
                                        if (el.scrollHeight > bestSH) {{ best = el; bestSH = el.scrollHeight; }}
                                    }}
                                }}
                                if (best) {{
                                    const st = best.scrollTop;
                                    best.scrollBy({{top: amount, behavior: 'smooth'}});
                                    if (best.scrollTop !== st) {{ scrolled = true; result.target = 'largestScrollable'; }}
                                }}
                            }}
                            if (!scrolled && document.scrollingElement) {{
                                const st = document.scrollingElement.scrollTop;
                                document.scrollingElement.scrollBy({{top: amount, behavior: 'smooth'}});
                                if (document.scrollingElement.scrollTop !== st) {{ scrolled = true; result.target = 'scrollingElement'; }}
                            }}
                            if (!scrolled) {{
                                window.scrollBy({{top: amount, behavior: 'smooth'}});
                                result.target = 'window';
                                scrolled = true;
                            }}
                            result.after = {{
                                windowScrollY: window.scrollY,
                                scrollingElement: document.scrollingElement ? document.scrollingElement.scrollTop : null,
                                activeElement: document.activeElement ? document.activeElement.scrollTop : null
                            }};
                            result.changed = (
                                result.before.windowScrollY !== result.after.windowScrollY ||
                                result.before.scrollingElement !== result.after.scrollingElement ||
                                (result.before.activeElement !== result.after.activeElement)
                            );
                            JSON.stringify(result);
                            """
                            js_result_raw = sess.run(page.evaluate(smart_scroll_js))
                            import json as _json

                            try:
                                js_data = (
                                    _json.loads(js_result_raw)
                                    if isinstance(js_result_raw, str)
                                    else js_result_raw
                                )
                            except Exception:
                                js_data = {"changed": False}
                            target = js_data.get("target", "window")
                            changed = js_data.get("changed", False)
                            domain = url.split("/")[2] if "://" in url else "browser"
                            if changed:
                                method_type = "js-window" if target == "window" else "js-container"
                                print(
                                    f"[ComputerControl] JS scroll {direction} ×{px}px on {domain} target={target}"
                                )
                                return f"Scrolled {direction} ×{px}px (method={method_type}, target={target})"
                            print(
                                f"[ComputerControl] JS scroll unchanged on {domain}, pyautogui fallback"
                            )
                    except Exception as js_err:
                        print(f"[ComputerControl] JS scroll failed ({js_err}), pyautogui fallback")
            except ImportError:
                pass

            # Default: OS-level pyautogui scroll
            result = _scroll(direction=direction, amount=amount)
            print(f"[ComputerControl] pyautogui scroll: {result}")
            return f"(method=pyautogui) {result}"

        if action == "copy":
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc = params.get("description", "")
            coords = _screen_find(desc)
            if coords:
                time.sleep(0.2)
                _click(x=coords[0], y=coords[1])
                return f"Clicked '{desc}' at {coords}"
            return f"Element not found on screen: '{desc}'"

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action == "random_data":
            dt = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field = params.get("field", "name")
            profile = _user_profile()
            value = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        if action == "running_apps":
            return "Running apps:\n" + format_running_apps(limit=int(params.get("limit", 60)))

        if action == "active_window":
            info = get_active_window()
            return f"Active window: {info.get('title') or 'Desktop'} | App: {info.get('app') or 'unknown'}"

        if action == "system_snapshot":
            proc = process_snapshot()
            win = window_context()
            return (
                "System awareness:\n"
                + win
                + "\n\nProcesses:\n"
                + "\n".join(f"- {name}" for name in proc.get("running_apps", [])[:60])
            )

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"
