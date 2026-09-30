"""System-prompt loader (extracted from main.py)."""

from core.utils import BASE_DIR

PROMPT_PATH = BASE_DIR / "core" / "prompt.txt"


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are era, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results â€” always call the appropriate tool."
        )
