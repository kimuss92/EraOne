"""crew/registry.py — load and validate the crew from config/crew/*.yaml.

Adding a bot is a data change: one yaml file, one prompt file, and capability NAMES
that already exist. Nothing here (or anywhere in core/app) has to be edited, which is
the property `tools/test_crew_registry.py::TestAddingABot` pins.

Degrade, never raise: a bad file is skipped and the reason recorded; every other bot
still loads. A bot is never half-loaded — an unknown capability name skips the whole
bot rather than quietly granting it less (or, worse, more) than its file says.

yaml schema (all keys required except the last four):

    id: drew                       # must equal the file name; ^[a-z][a-z0-9_]{1,31}$
    role: Research
    description: One line for `crew list`.
    prompt: prompts/drew.md        # relative to this directory; may not leave it
    capabilities: [vault.search, vault.read]
    model_tier: fast               # fast | smart          (default fast)
    brands: ["*"]                  # "*" or brand keys     (default all)
    write_zones: [inbox]           # inbox|brand|global|other  (default inbox only)
    handoffs_to: []                # bot ids this bot may hand a task to
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.logger import log_event, setup_logger
from crew.transport import BotSpec

registry_logger = setup_logger("era_one.crew.registry")

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "config" / "crew"
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_BRAND_RE = re.compile(r"^(\*|[a-z][a-z0-9_]{1,40})$")
_TIERS = ("fast", "smart")
_ZONES = frozenset({"inbox", "brand", "global", "other"})
_MAX_PROMPT_CHARS = 20_000


def crew_dir() -> Path:
    return Path(os.getenv("ERA_CREW_DIR") or DEFAULT_DIR)


@dataclass
class LoadReport:
    specs: dict[str, BotSpec] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)


def _str_list(raw, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{key} must be a list of strings")
    return tuple(x.strip() for x in raw if x.strip())


def _build(path: Path, directory: Path, known_capabilities: frozenset[str]) -> BotSpec:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("file is not a mapping")
    bot_id = str(data.get("id") or "").strip()
    if not _ID_RE.match(bot_id):
        raise ValueError(f"id {bot_id!r} is not a valid bot id")
    if bot_id != path.stem:
        raise ValueError(f"id {bot_id!r} must match the file name {path.stem!r}")
    role = str(data.get("role") or "").strip()
    description = str(data.get("description") or "").strip()
    if not role or not description:
        raise ValueError("role and description are required")

    prompt_rel = str(data.get("prompt") or "").strip()
    if not prompt_rel:
        raise ValueError("prompt is required")
    root = directory.resolve()
    prompt_path = (root / prompt_rel).resolve()
    if not prompt_path.is_relative_to(root):
        raise ValueError("prompt path leaves the crew directory")
    if not prompt_path.is_file():
        raise ValueError(f"prompt file {prompt_rel!r} does not exist")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt or len(prompt) > _MAX_PROMPT_CHARS:
        raise ValueError("prompt is empty or too long")

    capabilities = _str_list(data.get("capabilities"), "capabilities")
    unknown = [c for c in capabilities if c not in known_capabilities]
    if unknown:
        raise ValueError(f"unknown capability {unknown[0]!r}")
    if len(set(capabilities)) != len(capabilities):
        raise ValueError("duplicate capability")

    tier = str(data.get("model_tier") or "fast").strip().lower()
    if tier not in _TIERS:
        raise ValueError(f"model_tier must be one of {_TIERS}")
    brands = _str_list(data.get("brands", ["*"]), "brands") or ("*",)
    if not all(_BRAND_RE.match(b) for b in brands):
        raise ValueError("brands must be '*' or brand keys")
    zones = _str_list(data.get("write_zones", ["inbox"]), "write_zones")
    if not set(zones) <= _ZONES:
        raise ValueError(f"write_zones must be a subset of {sorted(_ZONES)}")
    handoffs = _str_list(data.get("handoffs_to"), "handoffs_to")
    if not all(_ID_RE.match(h) for h in handoffs):
        raise ValueError("handoffs_to must be bot ids")

    return BotSpec(
        id=bot_id,
        role=role,
        description=description,
        prompt=prompt,
        capabilities=capabilities,
        model_tier=tier,
        brands=brands,
        write_zones=zones,
        handoffs_to=handoffs,
    )


def load_specs(directory: Path | str | None = None) -> LoadReport:
    """Load every bot in `directory` (default config/crew). Never raises."""
    from crew import capabilities as caps

    report = LoadReport()
    root = Path(directory) if directory else crew_dir()
    try:
        files = sorted(root.glob("*.yaml"))
    except OSError as exc:
        report.skipped.append((str(root), f"crew directory unreadable: {exc}"))
        return report
    known = frozenset(caps.CAPABILITIES)
    for path in files:
        try:
            spec = _build(path, root, known)
            if spec.id in report.specs:
                raise ValueError("duplicate bot id")
            report.specs[spec.id] = spec
        except Exception as exc:  # noqa: BLE001 — one bad file must not cost the crew
            report.skipped.append((path.name, f"{type(exc).__name__}: {exc}"))
            log_event(
                registry_logger, "crew.spec.skipped", "WARNING", file=path.name, error=str(exc)
            )
    log_event(
        registry_logger,
        "crew.registry.loaded",
        "INFO",
        bots=sorted(report.specs),
        skipped=len(report.skipped),
    )
    return report
