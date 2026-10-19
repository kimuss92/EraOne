"""Local intent detection; no provider request on the activation path."""

import re

_ACTION = r"(?:fix|debug|implement|refactor|write|create|edit|update|optimi[sz]e|test|review|inspect|open|work on)"
_CODE = r"(?:bug|code|coding|function|class|module|script|unit tests?|codebase|project|repository|repo|new file|pull request)"
_FILE = r"(?:[\w./\\-]+\.(?:py|tsx?|jsx?|rs|go|java|cpp|c|cs|sh|sql|ya?ml|toml|json))\b"
_REQUEST = re.compile(rf"\b{_ACTION}\b.{{0,100}}(?:\b{_CODE}\b|{_FILE})", re.I)
_NEGATIVE = re.compile(
    rf"\b(?:do not|don't|never|stop|avoid)\s+(?:\w+\s+){{0,3}}(?:code|coding|{_ACTION})\b",
    re.I,
)


def coding_intent(text: str) -> bool:
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        return False
    value = text.strip()
    if value.startswith(("__", '"', "'", "```")) or _NEGATIVE.search(value):
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
