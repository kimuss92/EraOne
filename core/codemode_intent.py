"""Local intent detection; no provider request on the activation path."""

import re
from functools import lru_cache
from pathlib import Path

_ACTION = r"(?:fix|debug|implement|refactor|write|create|edit|update|optimi[sz]e|test|review|inspect|open|work on)"
_CODE = r"(?:bug|code|coding|function|class|module|script|unit tests?|codebase|project|repository|repo|new file|pull request)"
_FILE = r"(?:[\w./\\-]+\.(?:py|tsx?|jsx?|rs|go|java|cpp|c|cs|sh|sql|ya?ml|toml|json))\b"
_REQUEST = re.compile(rf"\b{_ACTION}\b.{{0,100}}(?:\b{_CODE}\b|{_FILE})", re.I)
_NEGATIVE = re.compile(
    rf"\b(?:do not|don't|never|stop|avoid)\s+(?:\w+\s+){{0,3}}(?:code|coding|{_ACTION})\b",
    re.I,
)


_CREW_DIR = Path(__file__).resolve().parents[1] / "config" / "crew"


@lru_cache(maxsize=1)
def _crew_names() -> tuple[str, ...]:
    """The crew bots (one yaml each in config/crew) plus the word "crew" itself."""
    try:
        names = {p.stem.lower() for p in _CREW_DIR.glob("*.yaml") if p.stem.isalpha()}
    except OSError:
        names = set()
    return tuple(sorted(names | {"crew"}))


def addresses_crew(text: str) -> bool:
    """PF-050: the owner handed this to the crew ("ask Cody to fix...", "Laura, plan...").

    Live 2026-11-19: "Era, ask Cody to fix this bug and stage it for my approval" was
    taken by implicit CodeMode (a coding request), which needs the logged-out Claude
    login, so the crew never saw it. An explicit crew hand-off is the crew's job.
    """
    who = "|".join(re.escape(n) for n in _crew_names())
    hand = r"(?:ask|tell|have|get|send|let|give|hand|assign|delegate)"
    return bool(
        re.search(rf"\b{hand}\b(?:\s+\w+){{0,3}}?\s+(?:the\s+)?(?:{who})\b", text, re.I)
        or re.match(rf"^\s*(?:era[, ]+)?(?:hey\s+)?(?:{who})\s*[,:]", text, re.I)
    )


def coding_intent(text: str) -> bool:
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        return False
    value = text.strip()
    if value.startswith(("__", '"', "'", "```")) or _NEGATIVE.search(value):
        return False
    if addresses_crew(value):
        return False
    if re.match(
        r"^(?:remember|forget|ignore|what do you remember|explain what|what is)\b", value, re.I
    ):
        return False
    if re.match(r"^(?:she said|he said|they said|what happens if|what if|if i)\b", value, re.I):
        return False
    return bool(
        re.fullmatch(r"(?:era[, ]+)?(?:start|let's start|begin) coding[.! ]*", value, re.I)
        or _REQUEST.search(value)
        or re.search(rf"\b(?:traceback|syntaxerror|debugging)\b.{{0,100}}{_FILE}", value, re.I)
        or re.search(
            rf"{_FILE}.{{0,80}}\b(?:error|broken|fails?|crashes?|debugging)\b", value, re.I
        )
    )


def model_order(text: str) -> str | None:
    match = re.fullmatch(
        r"\s*(?:switch(?:\s+(?:the\s+)?model)?\s+to|use|change\s+(?:the\s+)?model\s+to)\s+"
        r"(?:claude\s+)?((?:sonnet|opus|haiku)(?:\s+\d+(?:\.\d+)?)?)(?:\s+for\s+this\s+task)?[.! ]*",
        text,
        re.I,
    )
    return match.group(1).lower() if match else None
