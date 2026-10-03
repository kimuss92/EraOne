"""System-prompt loader (extracted from main.py)."""

from core.utils import BASE_DIR

PROMPT_PATH = BASE_DIR / "core" / "prompt.txt"


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8") + (
            "\nLIVE HUD AND AUTONOMOUS DEVELOPMENT:\n"
            "Translate visual requests to visual_engine JSON commands. Use data bindings for real metrics/progress. "
            "Never invent progress, HTML or scripts. For new features or own-source changes use autonomous_development "
            "propose (reload=true for visual changes), never code_helper. It locates sources, applies self_improve in "
            "a copied workspace, runs tests and self_diagnosis, and waits for final user approval in the HUD. "
            "Do not claim a staged change is live. Report verification and restart requirements. "
            "If Claude fails, report failure and give analysis only; do not silently edit through another model. "
            "Reloadable features can run through run_feature with explicit confirmation."
        )
    except Exception:
        return (
            "You are era, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results â€” always call the appropriate tool."
        )
