import sys
from pathlib import Path

from core.protected_config import read_config, write_config


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def config_exists() -> bool:
    return CONFIG_FILE.exists()


def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()

    # Never overwrite unrelated settings after a corrupt/unreadable config.
    data = read_config(CONFIG_FILE) if CONFIG_FILE.exists() else {}
    data["gemini_api_key"] = gemini_api_key.strip()

    write_config(CONFIG_FILE, data)


def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return read_config(CONFIG_FILE)
    except Exception as e:
        print(f"Failed to load API configuration ({type(e).__name__})")
        return {}


def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")


def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)
