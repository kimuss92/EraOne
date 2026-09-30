# desktop.py
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from actions.desktop_ops import DIRECT_ACTIONS, WINDOW_ACTIONS, OpError, run_plan
from core.utils import get_desktop

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


_PLAN_MISSING = object()

_NO_PLAN = (
    "I can't do that with the desktop operations I'm allowed to run. "
    "I can list, search, count, organise, copy, move and open files in "
    "your Desktop, Documents, Downloads, Pictures, Music and Videos folders. "
    "For clicking or typing, ask me to control the computer instead."
)


def _run_desktop_task(task: str, player=None, approved_plan=_PLAN_MISSING) -> str:
    """Run the plan the user approved. This function NEVER plans.

    Planning happens once, in `plan_desktop_preview`, so that the steps the user
    is shown are the steps that run. Treating a missing plan as "plan it now"
    was fail-OPEN and silently restored the original bug on two live paths: the
    executor's `confirm_pending_confirmation` re-dispatch stripped the carried
    plan before re-entry, and a preview that raised stored no plan at all — both
    then generated a fresh plan *after* the user had already said yes.

    Every planner call is classified HIGH and cannot be waived
    (`category_for_action("task")` is ""), so reaching execution without an
    approved plan means the confirmation layer was bypassed. Refuse.
    """
    if approved_plan is _PLAN_MISSING:
        print(f"[Desktop] Refused: no approved plan for task {task!r}")
        return (
            "I need to check that with you before I run it. "
            "Ask me again and I will show you exactly what I would do."
        )
    if approved_plan is None:
        return "I could not work out how to do that safely, so I have not done anything."
    if not approved_plan:
        return _NO_PLAN
    try:
        return run_plan(approved_plan) or "Done."
    except OpError as e:
        print(f"[Desktop] Rejected approved plan step: {e}")
        return f"This action was blocked for safety: {e}."


def set_wallpaper(image_path: str) -> str:
    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        return f"Image not found: {image_path}"
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        return f"Unsupported format: {path.suffix}. Use jpg, png, bmp or webp."

    try:
        if _OS == "Windows":
            import ctypes

            if path.suffix.lower() in {".webp", ".png"}:
                try:
                    from PIL import Image

                    # mkstemp, not mktemp: mktemp only names a file, so another
                    # process can create it first. The BMP must outlive this call
                    # (Windows reads the wallpaper from disk), so it is kept.
                    fd, bmp_name = tempfile.mkstemp(suffix=".bmp")
                    os.close(fd)
                    bmp_path = Path(bmp_name)
                    Image.open(path).convert("RGB").save(bmp_path, "BMP")
                    path = bmp_path
                except ImportError:
                    pass
            ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path), 3)
            return f"Wallpaper set: {path.name}"

        elif _OS == "Darwin":
            script = (
                f'tell application "System Events" to tell every desktop to '
                f'set picture to POSIX file "{path}"'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True)
            return f"Wallpaper set: {path.name}"

        else:
            desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            uri = f"file://{path}"

            if "gnome" in desktop_env or "unity" in desktop_env:
                subprocess.run(
                    ["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri],
                    capture_output=True,
                )
                subprocess.run(
                    ["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri],
                    capture_output=True,
                )

            elif "kde" in desktop_env:
                # KDE Plasma
                script = f"""
var allDesktops = desktops();
for (var i = 0; i < allDesktops.length; i++) {{
    d = allDesktops[i];
    d.wallpaperPlugin = "org.kde.image";
    d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
    d.writeConfig("Image", "file://{path}");
}}
"""
                subprocess.run(
                    [
                        "qdbus",
                        "org.kde.plasmashell",
                        "/PlasmaShell",
                        "org.kde.PlasmaShell.evaluateScript",
                        script,
                    ],
                    capture_output=True,
                )

            elif "xfce" in desktop_env:
                subprocess.run(
                    [
                        "xfconf-query",
                        "-c",
                        "xfce4-desktop",
                        "-p",
                        "/backdrop/screen0/monitor0/workspace0/last-image",
                        "-s",
                        str(path),
                    ],
                    capture_output=True,
                )

            else:
                result = subprocess.run(["feh", "--bg-scale", str(path)], capture_output=True)
                if result.returncode != 0:
                    return (
                        f"Could not set wallpaper automatically on {desktop_env}. "
                        f"Try manually or install 'feh'."
                    )

            return f"Wallpaper set: {path.name}"

    except Exception as e:
        return f"Could not set wallpaper: {e}"


def set_wallpaper_from_url(url: str) -> str:
    try:
        import urllib.request

        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        fd, tmp_name = tempfile.mkstemp(suffix=suffix)  # not mktemp: race-free
        os.close(fd)
        tmp = Path(tmp_name)
        urllib.request.urlretrieve(url, str(tmp))
        result = set_wallpaper(str(tmp))
        try:
            tmp.unlink()
        except Exception:
            pass
        return result
    except Exception as e:
        return f"Could not download wallpaper: {e}"


def get_current_wallpaper() -> str:
    try:
        if _OS == "Windows":
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
            val, _ = winreg.QueryValueEx(key, "Wallpaper")
            winreg.CloseKey(key)
            return f"Current wallpaper: {val}"

        elif _OS == "Darwin":
            script = 'tell application "System Events" to get picture of desktop 1'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            return f"Current wallpaper: {result.stdout.strip()}"

        else:
            desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            if "gnome" in desktop_env or "unity" in desktop_env:
                result = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.background", "picture-uri"],
                    capture_output=True,
                    text=True,
                )
                return f"Current wallpaper: {result.stdout.strip()}"
            return "Wallpaper path retrieval not supported for this desktop environment."

    except Exception as e:
        return f"Could not get wallpaper: {e}"


FILE_TYPE_MAP = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
    "Documents": {
        ".pdf",
        ".doc",
        ".docx",
        ".txt",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".csv",
        ".odt",
        ".ods",
        ".odp",
    },
    "Videos": {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
    "Music": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "Code": {
        ".py",
        ".js",
        ".ts",
        ".html",
        ".css",
        ".json",
        ".xml",
        ".cpp",
        ".java",
        ".cs",
        ".go",
        ".rs",
        ".sh",
        ".php",
    },
    "Executables": {".exe", ".msi", ".bat", ".cmd", ".sh", ".appimage", ".deb", ".rpm"},
}

_SKIP_EXTENSIONS = {
    "Windows": {".lnk", ".url"},
    "Darwin": {".webloc"},
    "Linux": {".desktop"},
}


def movable_desktop_items() -> list[Path]:
    """The Desktop files `organize`/`clean` would move.

    Shared with the confirmation preview so the count the user approves is
    produced by the SAME rule that later decides what moves. A second copy of
    this filter would be free to drift, which is exactly how a preview stops
    describing reality.
    """
    desktop = get_desktop()
    skip_exts = _SKIP_EXTENSIONS.get(_OS, set())
    items = []
    try:
        entries = list(desktop.iterdir())
    except OSError:
        return []
    for item in entries:
        if item.is_dir() or item.name.startswith("."):
            continue
        if item.suffix.lower() in skip_exts:
            continue
        items.append(item)
    return items


def organize_target_name(item: Path, mode: str) -> str:
    """The folder `organize_desktop` would move `item` into."""
    if mode == "by_date":
        return datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m")
    ext = item.suffix.lower()
    for folder, exts in FILE_TYPE_MAP.items():
        if ext in exts:
            return folder
    return "Others"


def clean_archive_name() -> str:
    """The archive folder `clean_desktop` would create. Naming it does NOT
    create it — the preview must not touch the disk."""
    return f"Desktop Archive {datetime.now().strftime('%Y-%m-%d')}"


def organize_desktop(mode: str = "by_type") -> str:
    desktop = get_desktop()
    moved, skipped = [], []

    for item in movable_desktop_items():
        folder_name = organize_target_name(item, mode)
        target_dir = desktop / folder_name
        target_dir.mkdir(exist_ok=True)
        new_path = target_dir / item.name

        if new_path.exists():
            skipped.append(item.name)
            continue

        shutil.move(str(item), str(new_path))
        moved.append(f"{item.name} → {folder_name}/")

    result = f"Desktop organized ({mode}): {len(moved)} files moved."
    if moved:
        result += "\n" + "\n".join(moved[:8])
        if len(moved) > 8:
            result += f"\n... and {len(moved) - 8} more."
    if skipped:
        result += f"\n{len(skipped)} file(s) skipped (name conflict)."
    return result


def list_desktop() -> str:
    desktop = get_desktop()
    items = []
    for item in sorted(desktop.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            try:
                count = len(list(item.iterdir()))
            except PermissionError:
                count = "?"
            items.append(f"📁 {item.name}/ ({count} items)")
        else:
            size = item.stat().st_size
            size_str = (
                f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
            )
            items.append(f"📄 {item.name} ({size_str})")

    if not items:
        return "Desktop is empty."
    return f"Desktop ({len(items)} items):\n" + "\n".join(items)


def clean_desktop() -> str:
    desktop = get_desktop()
    archive_dir = desktop / clean_archive_name()
    archive_dir.mkdir(exist_ok=True)

    moved = 0
    for item in movable_desktop_items():
        new_path = archive_dir / item.name
        if not new_path.exists():
            shutil.move(str(item), str(new_path))
            moved += 1

    return f"Desktop cleaned: {moved} files archived to '{archive_dir.name}'."


def get_desktop_stats() -> str:
    desktop = get_desktop()
    files = [i for i in desktop.iterdir() if i.is_file()]
    folders = [i for i in desktop.iterdir() if i.is_dir()]
    total_size = sum(f.stat().st_size for f in files if f.exists())
    size_str = (
        f"{total_size / 1024:.1f} KB"
        if total_size < 1024 * 1024
        else f"{total_size / 1024 / 1024:.1f} MB"
    )
    return (
        f"Desktop stats ({_OS}):\n"
        f"  Files   : {len(files)}\n"
        f"  Folders : {len(folders)}\n"
        f"  Size    : {size_str}\n"
        f"  Path    : {desktop}"
    )


def desktop_control(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action : wallpaper | wallpaper_url | current_wallpaper |
                 organize  | clean | list | stats |
                 task (AI-powered)
        path   : image path for 'wallpaper'
        url    : image URL for 'wallpaper_url'
        mode   : 'by_type' or 'by_date' for 'organize'
        task   : natural language description for AI-powered actions
    """
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()
    task = str(params.get("task") or "").strip()
    # Set only by the confirmation layer, from the plan the user was shown.
    # Absent on a first call; present (possibly []) on the approved re-call.
    approved_plan = params["_approved_plan"] if "_approved_plan" in params else _PLAN_MISSING
    # Anything not in DIRECT_ACTIONS falls through to the LLM planner below.
    # `core/safety_guard.py` reads the same frozenset to decide when this call
    # needs confirmation — keep the branches below and that list in step.

    if player:
        player.write_log(f"[desktop] {action or task[:40]}")

    try:
        if action == "wallpaper":
            path = params.get("path", "")
            return set_wallpaper(path) if path else "No image path provided."

        elif action == "wallpaper_url":
            url = params.get("url", "")
            return set_wallpaper_from_url(url) if url else "No URL provided."

        elif action == "current_wallpaper":
            return get_current_wallpaper()

        elif action == "organize":
            return organize_desktop(params.get("mode", "by_type"))

        elif action == "clean":
            return clean_desktop()

        elif action == "list":
            return list_desktop()

        elif action == "stats":
            return get_desktop_stats()

        elif action in DIRECT_ACTIONS:
            # Listed as directly handled, but no branch above claimed it — the
            # chain and DIRECT_ACTIONS have drifted. Refuse loudly instead of
            # quietly planning it, which is what safety_guard was told not to gate.
            return f"Desktop action '{action}' is not wired up."

        elif action in WINDOW_ACTIONS and not task:
            # A window verb with no task is a mis-route, not a desktop task.
            # Planning it sent the verb to the model for nothing; worse, the
            # matching branch in `classify_action` returns LOW, so anything that
            # did reach the planner this way ran unconfirmed.
            return f"'{action}' is a window action — ask me to control the window instead."

        elif action == "task" or task:
            actual_task = task or params.get("description", "")
            if not actual_task:
                return "Please describe what you want to do on the desktop."

            return _run_desktop_task(actual_task, player=player, approved_plan=approved_plan)

        elif action:
            return _run_desktop_task(action, player=player, approved_plan=approved_plan)

        else:
            return "No action or task specified."

    except Exception as e:
        print(f"[Desktop] Error: {e}")
        return f"Desktop control error: {e}"
