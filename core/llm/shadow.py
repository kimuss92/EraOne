"""Shadow mode — what automatic routing WOULD have done, recorded but not acted on.

WHY SHADOW BEFORE CANARY
------------------------
Phase 10 dispatches only on an explicit pin, so automatic by-task routing has
never run against real traffic. Switching it on directly would mean discovering
its mistakes by paying for them. Shadow mode runs the classifier and the router
on every utterance, writes down the decision, and then lets Era behave exactly
as it always has. After a few days of ordinary use the question "should this be
automatic?" has an answer made of observations instead of opinions.

IT DOES NOT RECORD WHAT YOU SAID
--------------------------------
This is the whole reason the record is shaped the way it is. A log of every
utterance sitting in runtime/ is a transcript of the user's life — a privacy
liability far outweighing its diagnostic value, and one that would quietly
outlive the experiment that justified it.

So an observation stores the CLASSIFICATION and the ROUTING, never the text: the
task class, which pattern fired, word count, the alias it would have chosen, and
the projected cost. That is everything needed to answer "how often, to what, at
what price" and nothing needed to reconstruct a conversation. A short prefix
hash is kept only so repeated identical commands can be counted as repeats.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.utils import BASE_DIR

DEFAULT_PATH = BASE_DIR / "runtime" / "llm_shadow.jsonl"
PATH_ENV = "ERA_LLM_SHADOW_PATH"

# Keep the file bounded. Shadow mode is an experiment, not telemetry: a few
# thousand observations is plenty to decide a routing policy, and an unbounded
# append-only file in runtime/ is how a diagnostic becomes a disk problem.
MAX_RECORDS = 5000


def _path() -> Path:
    override = os.environ.get(PATH_ENV, "").strip()
    return Path(override) if override else DEFAULT_PATH


@dataclass
class Observation:
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    task: str = ""  # classified task class, "" when no opinion
    matched: str = ""  # which pattern fired
    confident: bool = False
    words: int = 0
    would_alias: str = ""  # what the router would have chosen
    would_provider: str = ""
    would_cost_usd: float = 0.0
    handled_by: str = ""  # what actually served it, today
    # A short hash, NOT the text. Enough to spot "they say this same thing 30
    # times a day"; useless for reconstructing what was said.
    fingerprint: str = ""


def fingerprint(text: str) -> str:
    body = " ".join((text or "").lower().split())
    if not body:
        return ""
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:10]


class ShadowLog:
    """Append-only observation log. Never raises; never stores utterances."""

    def __init__(self, *, path: Path | None = None, max_records: int = MAX_RECORDS) -> None:
        self._path = path
        self._max = max(1, int(max_records))
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else _path()

    def record(self, obs: Observation) -> bool:
        """Write one observation. Returns False if it could not be written.

        A shadow log is a diagnostic — failing to write one must never affect
        the request it was observing, so every failure is swallowed after being
        reported to the caller as False.
        """
        with self._lock:
            try:
                path = self.path
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(obs), ensure_ascii=False) + "\n")
                self._trim(path)
                return True
            except Exception:
                return False

    def _trim(self, path: Path) -> None:
        """Keep the newest `max_records` lines. Cheap enough at this size."""
        try:
            if path.stat().st_size < 256 * 1024:
                return  # far below any plausible cap; skip the read
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) <= self._max:
                return
            keep = lines[-self._max :]
            tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass

    def load(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue  # a torn line is not worth failing the whole report
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate observations into the numbers a routing decision needs."""
    total = len(rows)
    by_task: dict[str, int] = {}
    by_alias: dict[str, int] = {}
    projected = 0.0
    unopinionated = 0
    low_confidence = 0
    repeats: dict[str, int] = {}

    for row in rows:
        task = str(row.get("task") or "")
        if not task:
            unopinionated += 1
        else:
            by_task[task] = by_task.get(task, 0) + 1
        if not row.get("confident", True):
            low_confidence += 1
        alias = str(row.get("would_alias") or "")
        if alias:
            by_alias[alias] = by_alias.get(alias, 0) + 1
        try:
            projected += float(row.get("would_cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        fp = str(row.get("fingerprint") or "")
        if fp:
            repeats[fp] = repeats.get(fp, 0) + 1

    days = len({str(r.get("ts", ""))[:10] for r in rows if r.get("ts")}) or 1
    return {
        "total": total,
        "days": days,
        "no_opinion": unopinionated,
        "low_confidence": low_confidence,
        "by_task": dict(sorted(by_task.items(), key=lambda kv: -kv[1])),
        "by_alias": dict(sorted(by_alias.items(), key=lambda kv: -kv[1])),
        "projected_total_usd": round(projected, 4),
        "projected_per_day_usd": round(projected / days, 4),
        "most_repeated": sorted(repeats.values(), reverse=True)[:1][0] if repeats else 0,
    }


def format_report(summary: dict[str, Any], *, path: Path | None = None) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 72)
    lines.append("  Era routing — SHADOW report (observed, never acted on)")
    lines.append("=" * 72)
    if path:
        lines.append(f"  {path}")
    total = summary.get("total", 0)
    if not total:
        lines.append("")
        lines.append("  No observations yet. Shadow mode records while Era is used normally.")
        lines.append("")
        return "\n".join(lines)

    days = summary.get("days", 1)
    lines.append(f"  {total} utterance(s) over {days} day(s)")
    lines.append("")
    lines.append(f"  {'task class':<22}{'count':>8}{'share':>9}")
    lines.append("  " + "-" * 37)
    for task, count in summary.get("by_task", {}).items():
        lines.append(f"  {task:<22}{count:>8}{count / total * 100:>8.0f}%")
    no_op = summary.get("no_opinion", 0)
    lines.append(f"  {'(no opinion)':<22}{no_op:>8}{no_op / total * 100:>8.0f}%")
    lines.append("")
    lines.append("  Would have routed to:")
    for alias, count in summary.get("by_alias", {}).items():
        lines.append(f"    {alias:<20}{count:>6}")
    lines.append("")
    lines.append(
        f"  Projected cost if automatic: ${summary.get('projected_total_usd', 0):.2f} total, "
        f"${summary.get('projected_per_day_usd', 0):.2f}/day"
    )
    low = summary.get("low_confidence", 0)
    if low:
        lines.append(
            f"  {low} guess(es) were LOW confidence — autopilot can be told to skip these."
        )
    lines.append("")
    lines.append("  Nothing here changed Era's behaviour. These are observations only.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m core.llm.shadow` — read the report."""
    import argparse

    ap = argparse.ArgumentParser(description="Read Era's shadow routing report.")
    ap.add_argument("--clear", action="store_true", help="delete the observation log")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)

    log = ShadowLog()
    if args.clear:
        log.clear()
        print(f"cleared {log.path}")
        return 0

    rows = log.load()
    summary = summarize(rows)
    if args.as_json:
        print(json.dumps(summary, indent=1))
    else:
        print(format_report(summary, path=log.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
