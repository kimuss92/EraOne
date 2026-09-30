# core/utils.py
import json
import os
import platform
import sys
from pathlib import Path


# ----------------------------------------------------------------------
# Base directory & config
# ----------------------------------------------------------------------
def get_base_dir() -> Path:
    """Return the root directory of the application (works for frozen executables)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
API_CONFIG_PATH = CONFIG_PATH


def get_memory_dir() -> Path:
    """Directory holding the persistent memory stores (long_term, user_prefs,
    runtime_intent, learned_patterns, feedback_memory...).

    Honours ``ERA_MEMORY_DIR``, the same escape hatch ``ERA_LOG_DIR`` gives
    core.logger. This exists so the TEST SUITE can point the stores at a
    throwaway copy: several tests legitimately need to write state and read it
    back (``save_state({})``, ``update_memory(...)``), and without a redirect
    those writes land in the user's live ``memory/`` — mutating real data and
    letting one test clobber the state another test just set up.

    Read the env var on every call rather than caching it, so a test that sets
    it after import still takes effect. Callers that bind it to a module-level
    constant should expect the value fixed at import time; that is fine, because
    conftest.py sets the variable before any project module is imported.

    Note this moves only the DATA files. ``memory/`` is also a Python package;
    its modules keep importing from the real location.
    """
    env_dir = os.environ.get("ERA_MEMORY_DIR")
    if env_dir:
        d = Path(env_dir)
    else:
        d = BASE_DIR / "memory"
    return d


def get_config_dir() -> Path:
    """Return the Era One config directory (~/.eraone/), creating it if needed."""
    cfg_dir = Path.home() / ".eraone"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir


def load_config() -> dict:
    """Load the API keys / settings JSON file."""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_api_key() -> str:
    """Retrieve the primary Gemini API key from config."""
    key = load_config().get("gemini_api_key", "")
    if not key:
        raise RuntimeError("gemini_api_key not found in config/api_keys.json")
    return key


# Config keys searched, in order, for Live-session credentials. The first is the
# primary; the rest are fallbacks used only when the one in play is rate-limited.
_LIVE_KEY_NAMES = (
    "gemini_api_key",
    "gemini_api_key_fallback",
    "gemini_api_key_fallback2",
    "gemini_api_key_backup",
)


def get_api_keys() -> list[str]:
    """Every Gemini key usable for the Live session, primary first.

    Quota (1011 / RESOURCE_EXHAUSTED) is per-KEY, not per-project-forever, so a
    second key turns a 15-minute-and-doubling cooldown — during which Era cannot
    hear her wake word, because the mic lives inside the session — into an
    immediate reconnect. That is the single biggest reconnection win available,
    which is why the connect loop rotates keys before it ever sleeps.

    Also accepts a JSON list under ``gemini_api_keys`` for more than the named
    slots. Duplicates and blanks are dropped so a key copied into two fields
    cannot make the rotation look longer than it is (rotating onto the SAME
    exhausted key would burn a retry and mask a real outage).

    Env vars are honoured for the primary only, matching get_vision_api_key()'s
    precedence; fallbacks are config-file-only by design, since they exist to be
    managed alongside the primary rather than injected per-shell.
    """
    cfg = load_config()
    keys: list[str] = []

    def _add(value) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in keys:
            keys.append(value.strip())

    for name in _LIVE_KEY_NAMES:
        _add(cfg.get(name, ""))

    extra = cfg.get("gemini_api_keys")
    if isinstance(extra, list):
        for v in extra:
            _add(v)

    if not keys:
        _add(os.environ.get("GEMINI_API_KEY", ""))

    if not keys:
        raise RuntimeError("gemini_api_key not found in config/api_keys.json")
    return keys


def get_vision_api_key() -> str:
    """Resolve vision-specific Gemini key with fallback chain.

    Resolution order:
    1. gemini_vision_api_key / GEMINI_VISION_API_KEY (config)
    2. GEMINI_VISION_API_KEY (environment variable)
    3. gemini_api_key / GEMINI_API_KEY (config — main key)
    4. GEMINI_API_KEY (environment variable)
    """
    cfg = load_config()
    for config_key in ("gemini_vision_api_key", "GEMINI_VISION_API_KEY"):
        val = cfg.get(config_key, "")
        if val:
            return val
    env_val = os.environ.get("GEMINI_VISION_API_KEY", "")
    if env_val:
        return env_val
    for config_key in ("gemini_api_key", "GEMINI_API_KEY"):
        val = cfg.get(config_key, "")
        if val:
            return val
    env_val = os.environ.get("GEMINI_API_KEY", "")
    if env_val:
        return env_val
    raise RuntimeError(
        "No Gemini API key found for vision "
        "(checked gemini_vision_api_key, GEMINI_VISION_API_KEY, "
        "gemini_api_key, GEMINI_API_KEY)"
    )


def get_music_api_key() -> str:
    """Resolve the music-generation Gemini key with the same fallback chain
    as get_vision_api_key(): a dedicated key is preferred (deliberately
    separate from the voice key — Lyria RealTime shares Gemini API quota with
    Era's own Live-API voice/vision path, which has a documented history of
    quota exhaustion; an isolated key keeps a content-production run from
    ever degrading Era's day-to-day voice responsiveness), falling back to
    the main key if no dedicated one is configured.

    Resolution order:
    1. gemini_music_api_key / GEMINI_MUSIC_API_KEY (config)
    2. GEMINI_MUSIC_API_KEY (environment variable)
    3. gemini_api_key / GEMINI_API_KEY (config — main key, shared quota)
    4. GEMINI_API_KEY (environment variable)
    """
    cfg = load_config()
    for config_key in ("gemini_music_api_key", "GEMINI_MUSIC_API_KEY"):
        val = cfg.get(config_key, "")
        if val:
            return val
    env_val = os.environ.get("GEMINI_MUSIC_API_KEY", "")
    if env_val:
        return env_val
    for config_key in ("gemini_api_key", "GEMINI_API_KEY"):
        val = cfg.get(config_key, "")
        if val:
            return val
    env_val = os.environ.get("GEMINI_API_KEY", "")
    if env_val:
        return env_val
    raise RuntimeError(
        "No Gemini API key found for music generation "
        "(checked gemini_music_api_key, GEMINI_MUSIC_API_KEY, "
        "gemini_api_key, GEMINI_API_KEY)"
    )


def get_transcription_api_key() -> str:
    """Resolve the speech-to-text Gemini key, same fallback chain as the vision
    and music keys and for the same reason: quota is per-KEY (see get_api_keys),
    and transcription must never be able to exhaust the key Era's own Live voice
    session runs on. A voice note is what you send when you are AWAY from the
    machine — burning her ears to read it would invert the whole point.

    Resolution order:
    1. gemini_transcription_api_key / GEMINI_TRANSCRIPTION_API_KEY (config)
    2. GEMINI_TRANSCRIPTION_API_KEY (environment variable)
    3. gemini_vision_api_key (config — the other non-voice side channel)
    4. gemini_api_key / GEMINI_API_KEY (config — main key, shared quota)
    5. GEMINI_API_KEY (environment variable)
    """
    cfg = load_config()
    for config_key in ("gemini_transcription_api_key", "GEMINI_TRANSCRIPTION_API_KEY"):
        val = cfg.get(config_key, "")
        if val:
            return val
    env_val = os.environ.get("GEMINI_TRANSCRIPTION_API_KEY", "")
    if env_val:
        return env_val
    val = cfg.get("gemini_vision_api_key", "")
    if val:
        return val
    for config_key in ("gemini_api_key", "GEMINI_API_KEY"):
        val = cfg.get(config_key, "")
        if val:
            return val
    env_val = os.environ.get("GEMINI_API_KEY", "")
    if env_val:
        return env_val
    raise RuntimeError(
        "No Gemini API key found for transcription "
        "(checked gemini_transcription_api_key, GEMINI_TRANSCRIPTION_API_KEY, "
        "gemini_vision_api_key, gemini_api_key, GEMINI_API_KEY)"
    )


def get_os() -> str:
    """Return normalized OS name: 'windows', 'darwin', or 'linux'."""
    return load_config().get("os_system", platform.system().lower())


def is_windows() -> bool:
    return get_os() == "windows"


def is_mac() -> bool:
    return get_os() == "darwin"


def is_linux() -> bool:
    return get_os() == "linux"


# ----------------------------------------------------------------------
# Standard user folders (cross‑platform)
# OneDrive detection: if the standard path doesn't exist but OneDrive variant does, use it
# ----------------------------------------------------------------------
def get_desktop() -> Path:
    # Default is strictly C:\Users\nesri\Desktop for routine tasks.
    # Any explicit path the user gives overrides this — it is not a fixed rule.
    desktop = Path(r"C:\Users\nesri\Desktop")
    return desktop


def get_downloads() -> Path:
    if is_linux():
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    standard = Path.home() / "Downloads"
    if standard.exists():
        return standard
    onedrive = Path.home() / "OneDrive" / "Downloads"
    if onedrive.exists():
        return onedrive
    return standard


def get_documents() -> Path:
    if is_linux():
        xdg = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    standard = Path.home() / "Documents"
    if standard.exists():
        return standard
    onedrive = Path.home() / "OneDrive" / "Documents"
    if onedrive.exists():
        return onedrive
    return standard


def get_pictures() -> Path:
    if is_linux():
        xdg = os.environ.get("XDG_PICTURES_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    standard = Path.home() / "Pictures"
    if standard.exists():
        return standard
    onedrive = Path.home() / "OneDrive" / "Pictures"
    if onedrive.exists():
        return onedrive
    return standard


def get_music() -> Path:
    if is_linux():
        xdg = os.environ.get("XDG_MUSIC_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    standard = Path.home() / "Music"
    if standard.exists():
        return standard
    onedrive = Path.home() / "OneDrive" / "Music"
    if onedrive.exists():
        return onedrive
    return standard


def get_videos() -> Path:
    if is_linux():
        xdg = os.environ.get("XDG_VIDEOS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    standard = Path.home() / "Videos"
    if standard.exists():
        return standard
    onedrive = Path.home() / "OneDrive" / "Videos"
    if onedrive.exists():
        return onedrive
    return standard


# ----------------------------------------------------------------------
# Safe path validation (prevent writes/deletes in protected locations)
# ----------------------------------------------------------------------
def _resolve_existing_or_parent(path: Path) -> Path:
    """Resolve a path even when the final file does not exist yet."""
    expanded = path.expanduser()
    try:
        return expanded.resolve(strict=False)
    except TypeError:  # Python < 3.9 compatibility
        try:
            return expanded.resolve()
        except FileNotFoundError:
            parent = expanded.parent.resolve()
            return parent / expanded.name


def _protected_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform == "win32":
        raw_roots = [
            os.environ.get("WINDIR", r"C:\Windows"),
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            os.environ.get("APPDATA", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
    else:
        raw_roots = ["/bin", "/sbin", "/usr", "/etc", "/var", "/System", "/Library"]
    for item in raw_roots:
        if item:
            try:
                roots.append(_resolve_existing_or_parent(Path(item)))
            except Exception:
                pass
    return roots


def _configured_safe_roots() -> list[Path]:
    """Allowed roots for user files. Extra roots can be added with ERA_SAFE_ROOTS."""
    roots = [Path.home(), BASE_DIR]
    for folder_getter in (
        get_desktop,
        get_downloads,
        get_documents,
        get_pictures,
        get_music,
        get_videos,
    ):
        try:
            roots.append(folder_getter())
        except Exception:
            pass
    extra_roots = os.environ.get("ERA_SAFE_ROOTS", "")
    for part in extra_roots.split(os.pathsep):
        if part.strip():
            roots.append(Path(part.strip()))

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = _resolve_existing_or_parent(root)
        except Exception:
            continue
        key = str(resolved).lower() if sys.platform == "win32" else str(resolved)
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


SAFE_ROOTS: list[Path] = _configured_safe_roots()


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _get_local_drives() -> list[Path]:
    """Get available local drives on Windows. Returns empty list on other OS."""
    if sys.platform != "win32":
        return []
    drives = []
    try:
        import ctypes

        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if bitmask & 1:
                p = Path(f"{letter}:\\")
                if p.exists():
                    drives.append(p)
            bitmask >>= 1
    except Exception:
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{letter}:\\")
            try:
                if p.exists():
                    drives.append(p)
            except Exception:
                continue
    return drives


LOCAL_DRIVES: list[Path] = _get_local_drives()


def is_safe_path(target: str | Path, allow_protected: bool = False) -> bool:
    """Return True only for paths inside allowed user/project roots.

    This prevents screenshots and file actions from writing to protected system
    locations by default. Set ERA_SAFE_ROOTS to add explicit trusted roots.
    When allow_protected=True (for read-only open action), AppData/ProgramData
    paths on local drives are allowed, but Windows/Program Files remain blocked.
    """
    try:
        resolved = _resolve_existing_or_parent(Path(target))
    except Exception:
        return False

    # Hard-blocked paths (never allowed, even for open)
    _hard_blocked = []
    if sys.platform == "win32":
        for env_var in ("WINDIR",):
            p = os.environ.get(env_var, "C:\\Windows")
            if p:
                try:
                    _hard_blocked.append(Path(p).resolve())
                except Exception:
                    pass
        for p_str in (r"C:\Program Files", r"C:\Program Files (x86)"):
            try:
                _hard_blocked.append(Path(p_str).resolve())
            except Exception:
                pass

    for danger in _hard_blocked:
        try:
            if resolved == danger or danger in resolved.parents:
                print(f"[PATH_SAFETY] denied path={resolved} reason=hard_blocked_system_path")
                return False
        except Exception:
            if str(resolved).lower().startswith(str(danger).lower()):
                print(f"[PATH_SAFETY] denied path={resolved} reason=hard_blocked_system_path")
                return False

    if not allow_protected:
        for protected in _protected_roots():
            if resolved == protected or _is_relative_to(resolved, protected):
                print(f"[PATH_SAFETY] denied path={resolved} reason=protected_system_path")
                return False

    for safe_root in _configured_safe_roots():
        if resolved == safe_root or _is_relative_to(resolved, safe_root):
            return True

    for drive in LOCAL_DRIVES:
        try:
            resolved.relative_to(drive)
            return True
        except ValueError:
            continue

    print(f"[PATH_SAFETY] denied path={resolved} reason=outside_allowed_roots")
    return False


def safe_screenshot_path(requested: str | None = None) -> Path:
    """Return a safe path for saving screenshots (default: Desktop/era_screenshot.png)."""
    fallback = get_desktop() / "era_screenshot.png"
    if not requested:
        return fallback
    try:
        p = _resolve_existing_or_parent(Path(requested))
        if is_safe_path(p):
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    return fallback


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------
def format_size(size_bytes: int) -> str:
    """Convert bytes to human readable string (KB, MB, GB, TB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ----------------------------------------------------------------------
# Unified logging
# ----------------------------------------------------------------------
def log(module: str, message: str, player=None) -> None:
    """Print to console and optionally to the UI log widget."""
    line = f"[{module}] {message}"
    try:
        print(line)
    except UnicodeEncodeError:
        # Console codepage (e.g. cp1252) can't render emoji — degrade to
        # ASCII rather than raise into the calling action.
        print(line.encode("ascii", errors="replace").decode("ascii"))
    if player:
        try:
            player.write_log(f"[{module}] {message[:60]}")
        except Exception:
            pass


# ----------------------------------------------------------------------
# OS‑specific browser user‑agent
# ----------------------------------------------------------------------
def get_user_agent() -> str:
    if is_windows():
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    if is_mac():
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
