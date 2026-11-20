"""Subscription model preferences. These CLI aliases do not imply plan entitlement."""

import json
import re
from pathlib import Path

MODELS = ("sonnet", "opus", "haiku")
PATH = Path(__file__).resolve().parents[1] / "runtime/codemode/model.json"


def normalize(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Choose Sonnet, Opus or Haiku")
    match = re.fullmatch(
        r"(?:claude[ -])?(sonnet|opus|haiku)(?:[ -]\d+(?:[.-]\d+)*)?", value.strip(), re.I
    )
    if not match:
        raise ValueError("Choose Sonnet, Opus or Haiku")
    return match.group(1).lower()


def load(default: str = "sonnet") -> str:
    try:
        return normalize(json.loads(PATH.read_text(encoding="utf-8"))["model"])
    except (OSError, ValueError, TypeError, KeyError):
        try:
            return normalize(default)
        except ValueError:
            return "sonnet"


def save(model: str) -> None:
    from memory.safe_store import safe_write_json

    safe_write_json(PATH, {"model": normalize(model)})
