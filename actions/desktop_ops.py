"""actions/desktop_ops.py — the vetted operation set behind `desktop_control action=task`.

This replaces the previous design, in which Gemini emitted free-form Python
that was `exec`'d against a restricted-`__builtins__` sandbox (ruff S102).
That sandbox was never a security boundary:

* it handed the generated code the real ``pathlib.Path`` class, so ``.open("w")``,
  ``.write_text`` and ``.unlink`` were reachable as ordinary (non-dunder)
  attributes that the AST blocklist did not cover — arbitrary file write and
  delete, despite the prompt asking for neither;
* it injected the whole of ``pyautogui``, and synthesised keystrokes are
  arbitrary command execution (``Win+R`` is enough).

So a prompt injection reaching the ``task`` string owned the machine, whatever
the sandbox said. The model now *chooses* from the fixed dispatch table below
instead of writing code: it returns a JSON plan of ``{"op": ..., "args": {...}}``
steps, every op name must be a key of :data:`DESKTOP_OPS`, every handler
validates its own arguments, and every path is resolved inside
:func:`_safe_roots`. Nothing is compiled and nothing is executed.

Raw mouse/keyboard synthesis is deliberately *not* offered here. Era already
exposes it as `computer_control`, which carries its own confirmation gate and
vision-based risk escalation; duplicating it on this path would have made the
dispatch table cosmetic.
"""


from __future__ import annotations

import errno
import fnmatch
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from core.utils import (
    get_base_dir,
    get_desktop,
    get_documents,
    get_downloads,
    get_music,
    get_pictures,
    get_videos,
)
from core.win_subprocess import NO_WINDOW

_OS = platform.system()

# A plan is a short sequence of bookkeeping steps, not a program. The caps stop
# a runaway plan from walking the whole user profile.
MAX_STEPS = 12
MAX_LISTED = 200
MAX_SCANNED = 20_000
MAX_SCAN_SECONDS = 10.0


class OpError(ValueError):
    """An operation was rejected: unknown op, bad argument, or a path out of bounds."""


# The `desktop_control` actions answered by a fixed handler. EVERYTHING else —
# including a bare `task` parameter and any unrecognised action string — is
# handed to the LLM planner instead. `core/safety_guard.py` imports this to
# decide when a call needs confirmation, so the two can never disagree about
# which calls reach the planner. `tools/test_confirmation_flow.py` pins it to
# the dispatch chain in `actions/desktop.py`.
DIRECT_ACTIONS = frozenset(
    {
        "wallpaper",
        "wallpaper_url",
        "current_wallpaper",
        "organize",
        "clean",
        "list",
        "stats",
    }
)

# Window verbs that belong to `window_control`. `desktop_control` has no handler
# for them, so they used to fall into the planner's catch-all — while
# `classify_action` matched them FIRST and returned LOW without confirmation.
# A call like `{"action": "minimize", "task": "<attacker text>"}` therefore ran
# a generated plan silently. The dispatcher now redirects these instead of
# planning them, and the gate checks the planner path before the window branch.
WINDOW_ACTIONS = frozenset(
    "focus activate switch switch_to select minimize maximize restore show raise "
    "bring_to_front move_to_monitor monitors close".split()
)


# The only folders a generated plan may touch. Deliberately excludes the user
# profile root, AppData, and anything under it (autostart, Era's own source).
_NAMED_ROOTS: dict[str, Callable[[], Path]] = {
    "desktop": get_desktop,
    "documents": get_documents,
    "downloads": get_downloads,
    "pictures": get_pictures,
    "music": get_music,
    "videos": get_videos,
}


def _denied_roots() -> list[Path]:
    """Folders a plan may never touch, even when a safe root happens to contain
    them. Era's own source tree is the case that matters: `get_documents()`
    falls back to ``~/OneDrive/Documents`` when the plain folder is absent, and
    this repo lives under exactly that path — so on a machine with Known Folder
    Move enabled, the Documents root would otherwise hand a generated plan
    write access to Era's code and `config/api_keys.json`."""
    denied: list[Path] = []
    try:
        denied.append(Path(get_base_dir()).resolve())
    except (OSError, ValueError):
        pass
    # Shell-profile directories live INSIDE Documents, so they are inside a safe
    # root. Dropping a .ps1 there is persistence: nothing has to open it, the
    # next PowerShell or Terminal launch runs it.
    for profile_dir in ("PowerShell", "WindowsPowerShell"):
        try:
            denied.append((Path(get_documents()) / profile_dir).resolve())
        except (OSError, ValueError):
            continue
    return denied


def _safe_roots() -> list[Path]:
    """Resolved safe roots. Computed per call so the `core.utils` helpers stay
    patchable in tests and so a folder created after start-up is still seen."""
    roots: list[Path] = []
    for getter in _NAMED_ROOTS.values():
        try:
            roots.append(Path(getter()).expanduser().resolve())
        except (OSError, ValueError):
            continue
    return roots


def _resolve(raw: object, *, must_exist: bool = True, want_dir: bool | None = None) -> Path:
    """Resolve `raw` to a real path inside a safe root, or raise OpError.

    Containment is checked *after* ``resolve()``, so ``..`` segments and
    symlinks pointing out of the safe roots are caught rather than followed.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise OpError("a path argument is required")

    text = raw.strip()
    named = _NAMED_ROOTS.get(text.strip("/\\").lower())
    if named is not None:
        path = Path(named())
    else:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path(get_desktop()) / path

    try:
        path = path.resolve()
    except (OSError, ValueError) as e:
        raise OpError(f"path could not be resolved: {e}") from e

    roots = _safe_roots()
    if not any(path == root or root in path.parents for root in roots):
        allowed = ", ".join(sorted(_NAMED_ROOTS))
        raise OpError(f"'{path}' is outside the folders this task may touch ({allowed})")
    if any(path == denied or denied in path.parents for denied in _denied_roots()):
        raise OpError(f"'{path}' is inside Era's own files and is off limits")

    if must_exist and not path.exists():
        raise OpError(f"no such path: {path}")
    if want_dir is True and path.exists() and not path.is_dir():
        raise OpError(f"not a folder: {path}")
    if want_dir is False and path.exists() and path.is_dir():
        raise OpError(f"expected a file, got a folder: {path}")
    return path


def _is_unsafe_in_step(ch: str) -> bool:
    """Control, line-separator and format characters — everything that lets an
    argument break out of its slot in the sentence and impersonate the prose
    around it. Written with ord() rather than a regex literal so no escape
    sequence can be mangled into the real byte it denotes."""
    o = ord(ch)
    return o < 32 or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029)


def _pattern(raw: object) -> str:
    """Validate a glob pattern. A pattern filters names inside ONE folder; it is
    never a path and never a way out of the folder it is applied to.

    Rejecting every separator is what makes `recursive=False` mean it: a lone
    ``**/*`` turns `Path.glob` recursive, which silently defeated the caller's
    choice and made `_transfer`'s "destination inside source is safe" reasoning
    false. A drive-qualified pattern (``C:foo``) is rejected here too — it is
    not absolute, so it slipped through, and `Path.glob` then raises
    NotImplementedError, which is outside this module's OpError contract.
    """
    if raw is None:
        return "*"
    if not isinstance(raw, str):
        raise OpError("pattern must be text")
    pat = raw.strip() or "*"
    if any(_is_unsafe_in_step(c) for c in pat):
        raise OpError("a pattern may not contain line breaks or control characters")
    if ".." in pat or "/" in pat or "\\" in pat or ":" in pat:
        raise OpError(f"invalid pattern {pat!r}: a pattern matches names in one folder")
    return pat


def _flag(raw: object, name: str, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false", "yes", "no"}:
        return raw.strip().lower() in {"true", "yes"}
    raise OpError(f"{name} must be true or false")


def _scan(folder: Path, pattern: str, recursive: bool):
    """Bounded glob.

    The cap counts entries *visited*, not entries matched. Counting matches let
    a pattern that matches nothing walk an entire tree to completion — on a
    OneDrive folder full of cloud placeholders that is a long hydrating stall,
    twelve times over per plan. A wall-clock stop covers the same case when the
    tree is slow rather than merely large.
    """
    it = folder.rglob("*") if recursive else folder.iterdir()
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    out = []
    for visited, entry in enumerate(it, 1):
        if visited > MAX_SCANNED or time.monotonic() > deadline:
            break
        if fnmatch.fnmatch(entry.name, pattern):
            out.append(entry)
    return out


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ── operations ───────────────────────────────────────────────────────────────
# Each handler takes only named arguments and returns a spoken-style line.
# Unexpected argument names surface as a TypeError, which run_plan reports.


def _op_list_dir(path: object = "desktop", pattern: object = "*") -> str:
    folder = _resolve(path, want_dir=True)
    pat = _pattern(pattern)
    entries = sorted(_scan(folder, pat, False), key=lambda p: (p.is_file(), p.name.lower()))
    if not entries:
        return f"{folder.name}: nothing matching '{pat}'."
    shown = entries[:MAX_LISTED]
    more = f" (+{len(entries) - len(shown)} more)" if len(entries) > len(shown) else ""
    listing = "\n".join(f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in shown)
    return f"{folder.name} — {len(entries)} item(s){more}:\n{listing}"


def _op_find_files(
    path: object = "desktop", pattern: object = "*", recursive: object = True
) -> str:
    folder = _resolve(path, want_dir=True)
    pat = _pattern(pattern)
    hits = sorted(
        (p for p in _scan(folder, pat, _flag(recursive, "recursive", True)) if p.is_file()),
        key=lambda p: p.name.lower(),
    )
    if not hits:
        return f"No files matching '{pat}' under {folder.name}."
    shown = hits[:MAX_LISTED]
    more = f" (+{len(hits) - len(shown)} more)" if len(hits) > len(shown) else ""
    listing = "\n".join(str(p.relative_to(folder)) for p in shown)
    return f"{len(hits)} file(s) matching '{pat}' under {folder.name}{more}:\n{listing}"


def _op_count_files(
    path: object = "desktop", pattern: object = "*", recursive: object = False
) -> str:
    folder = _resolve(path, want_dir=True)
    pat = _pattern(pattern)
    n = sum(1 for p in _scan(folder, pat, _flag(recursive, "recursive", False)) if p.is_file())
    return f"{n} file(s) matching '{pat}' in {folder.name}."


def _op_folder_stats(path: object = "desktop") -> str:
    folder = _resolve(path, want_dir=True)
    files = dirs = 0
    total = 0
    for entry in _scan(folder, "*", True):
        if entry.is_dir():
            dirs += 1
            continue
        files += 1
        try:
            total += entry.stat().st_size
        except OSError:
            pass
    return f"{folder.name}: {files} file(s), {dirs} folder(s), {_human(total)} total."


def _op_disk_usage(path: object = "desktop") -> str:
    target = _resolve(path)
    usage = shutil.disk_usage(target)
    return (
        f"Drive holding {target.name}: {_human(usage.free)} free "
        f"of {_human(usage.total)} ({_human(usage.used)} used)."
    )


def _op_make_folder(path: object) -> str:
    folder = _resolve(path, must_exist=False, want_dir=True)
    if folder.exists():
        return f"Folder already exists: {folder.name}"
    folder.mkdir(parents=True, exist_ok=True)
    return f"Created folder: {folder.name}"


def _move_no_clobber(item: Path, target: Path) -> None:
    """Move `item` to `target`, never overwriting it.

    `shutil.move` was wrong here: when `os.rename` fails it falls back to
    copy2 + unlink, which happily overwrites a file that appeared since the
    caller's `exists()` check. `os.rename` alone refuses to clobber on Windows
    but cannot cross drives, and the safe roots can sit on different ones.
    """
    try:
        os.rename(item, target)
        return
    except OSError as e:
        if getattr(e, "errno", None) != errno.EXDEV:
            raise
    # Cross-device: O_EXCL makes creating the destination the thing that fails
    # if another process got there first, so the no-overwrite promise holds.
    os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    shutil.copy2(item, target, follow_symlinks=False)
    os.unlink(item)


def _transfer(source: object, destination: object, pattern: object, *, move: bool) -> str:
    src = _resolve(source, want_dir=True)
    dst = _resolve(destination, must_exist=False, want_dir=True)
    pat = _pattern(pattern)
    if dst == src:
        raise OpError("source and destination are the same folder")
    # A destination *inside* the source is the normal case ("tidy the Desktop
    # into folders"), and is safe because `_pattern` now forbids separators, so
    # the scan really is one folder deep and files-only.

    # Every matched file is re-resolved, NOT just the two folder arguments.
    # Checking only the folders left two holes: a pattern that reached into a
    # denied subtree, and a symlink in the source whose target lives outside it
    # (shutil.copy2 follows links, so `copy_files(source="downloads")` would
    # have copied out whatever a planted link pointed at).
    matches = []
    for p in _scan(src, pat, False):
        try:
            item = _resolve(str(p), want_dir=False)
        except OpError:
            continue
        if item.is_file():
            matches.append(item)
    if not matches:
        return f"Nothing matching '{pat}' in {src.name} — nothing to {'move' if move else 'copy'}."

    dst.mkdir(parents=True, exist_ok=True)
    done, skipped = 0, 0
    for item in matches:
        target = dst / item.name
        if target.exists():
            # Never overwrite: a plan the model guessed wrong must not cost data.
            skipped += 1
            continue
        try:
            if move:
                _move_no_clobber(item, target)
            else:
                shutil.copy2(item, target, follow_symlinks=False)
            done += 1
        except OSError:
            skipped += 1
    verb = "Moved" if move else "Copied"
    tail = f", {skipped} skipped (name already taken)" if skipped else ""
    return f"{verb} {done} file(s) from {src.name} to {dst.name}{tail}."


def _op_move_files(source: object, destination: object, pattern: object = "*") -> str:
    return _transfer(source, destination, pattern, move=True)


def _op_copy_files(source: object, destination: object, pattern: object = "*") -> str:
    return _transfer(source, destination, pattern, move=False)


# `open_path` exists to SHOW the user something, so it takes an allowlist.
# A denylist was the wrong shape: `os.startfile` hands the file to its shell
# association, and the set of extensions Windows will happily execute is open-
# ended (.chm, .msc, .appinstaller, .settingcontent-ms, .appref-ms, .rdp, .wsb,
# .scf, .library-ms, .iqy, .diagcab, .jnlp, .pyz, macro-enabled Office docs...).
# Enumerating them is a losing game; enumerating what is safe to view is not.
_MAY_OPEN = frozenset(
    ".pdf .txt .md .rtf .csv .log .json .xml .ini .docx .xlsx .pptx .odt .ods .odp .epub "
    ".jpg .jpeg .png .gif .bmp .webp .tif .tiff .heic .svg .ico "
    ".mp3 .wav .flac .m4a .aac .ogg .opus .mp4 .mkv .mov .avi .webm .m4v".split()
)


def _op_open_path(path: object) -> str:
    target = _resolve(path)
    if target.is_file() and target.suffix.lower() not in _MAY_OPEN:
        raise OpError(
            f"I only open documents, images and media — '{target.name}' is not one, "
            "and opening it could run it"
        )
    if _OS == "Windows":
        os.startfile(str(target))  # noqa: S606 — no shell, path validated above
    else:
        opener = "open" if _OS == "Darwin" else "xdg-open"
        subprocess.Popen(
            [opener, str(target)],
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    return f"Opened {target.name}."


DESKTOP_OPS: dict[str, Callable[..., str]] = {
    "list_dir": _op_list_dir,
    "find_files": _op_find_files,
    "count_files": _op_count_files,
    "folder_stats": _op_folder_stats,
    "disk_usage": _op_disk_usage,
    "make_folder": _op_make_folder,
    "move_files": _op_move_files,
    "copy_files": _op_copy_files,
    "open_path": _op_open_path,
}

# Kept next to the table so a new op cannot be added without describing it.
OP_SPECS = """\
- list_dir(path, pattern="*") — list what is directly inside a folder.
- find_files(path, pattern, recursive=true) — search a folder tree for files.
- count_files(path, pattern="*", recursive=false) — how many files match.
- folder_stats(path) — file count, folder count and total size.
- disk_usage(path) — free / used space on the drive holding this path.
- make_folder(path) — create a folder (parents included).
- move_files(source, destination, pattern="*") — move matching files between folders.
- copy_files(source, destination, pattern="*") — copy matching files between folders.
- open_path(path) — open a file or folder in the file manager."""


def run_plan(plan: object) -> str:
    """Validate and run a model-produced plan. Raises OpError on any rejection."""
    if not isinstance(plan, list) or not plan:
        raise OpError("the plan must be a non-empty list of steps")
    if len(plan) > MAX_STEPS:
        raise OpError(f"too many steps ({len(plan)} > {MAX_STEPS})")

    results: list[str] = []
    for i, step in enumerate(plan, 1):
        if not isinstance(step, dict):
            raise OpError(f"step {i} is not an object")
        op = step.get("op")
        handler = DESKTOP_OPS.get(op) if isinstance(op, str) else None
        if handler is None:
            allowed = ", ".join(sorted(DESKTOP_OPS))
            raise OpError(f"step {i}: unknown operation {op!r}. Allowed: {allowed}")
        args = step.get("args", {}) or {}
        if not isinstance(args, dict) or any(not isinstance(k, str) for k in args):
            raise OpError(f"step {i}: 'args' must be an object with text keys")
        try:
            results.append(handler(**args))
        except OpError as e:
            raise OpError(f"step {i} ({op}): {e}") from e
        except TypeError as e:
            raise OpError(f"step {i} ({op}): bad arguments — {e}") from e
        except OSError as e:
            raise OpError(f"step {i} ({op}): {e}") from e
    return "\n".join(results)
