"""Summarize a window of Era's own log into a stability report.

Phase 11's daily-use window needs the same numbers every time: errors, duplicate
actions, reconnects, latency. This reads ``logs/era.log`` (JSON lines, UTC) and
prints them; it never writes and never touches the running process.

    python tools/era_observe.py                  # last 60 minutes
    python tools/era_observe.py --since 3h
    python tools/era_observe.py --since last-start

Exit code 1 means at least one HARD signal (a fault that is wrong in any
amount); expected tool refusals and network blips are reported, not failed.
Memory growth is not logged by Era, so it is deliberately not reported here.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Wrong in any amount: a hit is a defect to investigate, not a statistic.
HARD_EVENTS = (
    "audio.output.write_failed",
    "awareness.devices.observer_failed",
    "awareness.devices.read_failed",
    "tool.duplicate.in_flight",
    "engine.crash",
)
# Normal operation that is still worth showing next to the verdict.
NOTE_EVENTS = (
    "engine.network.blip",
    "engine.session.rotated",
    "engine.session.go_away",
    "voice.fallback",
    "task.cancelled",
    "tool.grounding.refused",
)
# Bounded handler outcomes (bad time, unknown job, existing file): the tool did
# its job by refusing, so they are listed but never fail the run.
EXPECTED_ERROR_TYPES = frozenset({"ValidationError", "TaskError", "VisualCommandRejected"})


def parse_since(text: str, now: datetime, events: list[dict]) -> datetime:
    if text == "last-start":
        starts = [e["_t"] for e in events if e.get("event") == "process.start"]
        return starts[-1] if starts else now - timedelta(hours=1)
    unit = re.fullmatch(r"(\d+)([mhd])", text)
    if unit:
        span = {"m": "minutes", "h": "hours", "d": "days"}[unit.group(2)]
        return now - timedelta(**{span: int(unit.group(1))})
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_events(log_dir: Path) -> list[dict]:
    events: list[dict] = []
    for name in ("era.log.1", "era.log"):
        path = log_dir / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                row["_t"] = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError):
                continue
            if row["_t"].tzinfo is None:
                row["_t"] = row["_t"].replace(tzinfo=UTC)
            events.append(row)
    return events


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


# era_crash.log is the supervisor's own diary (local time, no zone). A relaunch
# the user asked for logs "exit code 0"; these are the lines that are not that.
_SUPERVISOR_FAULT = re.compile(r"killing it|crash|exited with code [1-9]|exit code [1-9]", re.I)
_SUPERVISOR_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def supervisor_faults(log_dir: Path, since: datetime, until: datetime) -> list[str]:
    path = log_dir / "era_crash.log"
    if not path.exists():
        return []
    faults = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stamp = _SUPERVISOR_STAMP.match(line)
        if not stamp or not _SUPERVISOR_FAULT.search(line):
            continue
        local = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
        if since <= local.astimezone(UTC) <= until:
            faults.append(line[:160])
    return faults


def starts_without_request(events: list[dict], window: list[dict]) -> int:
    """Starts with no relaunch request in the 5 minutes before: crash, kill or manual launch."""
    requests = [e["_t"] for e in events if e.get("event") == "era.relaunch.requested"]
    return sum(
        1
        for e in window
        if e.get("event") == "process.start"
        and not any(timedelta(0) <= e["_t"] - r <= timedelta(minutes=5) for r in requests)
    )


def build_report(
    events: list[dict], since: datetime, until: datetime, log_dir: Path | None = None
) -> dict:
    window = [e for e in events if since <= e["_t"] <= until]
    count = Counter(e.get("event") for e in window)
    faults = supervisor_faults(log_dir or LOG_DIR, since, until)
    cold = starts_without_request(events, window)
    turns = [e for e in window if e.get("event") == "turn.latency"]
    spoken = [e for e in turns if not e.get("interrupted") and e.get("response_ms")]
    tools = Counter(e.get("tool") for e in window if e.get("event") == "tool.call.received")
    failures = [e for e in window if e.get("event") == "tool.execution.failed"]
    unexpected = [e for e in failures if e.get("error_type") not in EXPECTED_ERROR_TYPES]
    slow = [e for e in failures if (e.get("duration_ms") or 0) >= 10_000]
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "events": len(window),
        "process_starts": count["process.start"],
        "relaunches_requested": count["era.relaunch.requested"],
        "turns": len(turns),
        "interrupted_turns": sum(1 for e in turns if e.get("interrupted")),
        "latency_ms": {
            key: {"p50": percentile(v, 50), "p95": percentile(v, 95), "n": len(v)}
            for key in ("response_ms", "total_ms")
            if (v := [e[key] for e in spoken if e.get(key) is not None])
        },
        "tool_calls": dict(tools.most_common()),
        "tool_failures": len(failures),
        "tool_failures_unexpected": [
            f"{e.get('tool')}.{e.get('action')}: {e.get('error_type')}" for e in unexpected
        ],
        "slow_tool_failures": [
            f"{e.get('tool')}.{e.get('action')} {e.get('duration_ms')}ms" for e in slow
        ],
        "error_level_events": sum(1 for e in window if e.get("level") == "ERROR"),
        "unattributed_replies": dict(
            Counter(e.get("reason") for e in window if e.get("event") == "model.reply.unattributed")
        ),
        "notes": {
            **{name: count[name] for name in NOTE_EVENTS if count[name]},
            **({"starts_without_relaunch_request": cold} if cold else {}),
        },
        "hard": {
            **{name: count[name] for name in HARD_EVENTS if count[name]},
            **({"supervisor_faults": faults} if faults else {}),
        },
    }


def render(report: dict) -> str:
    lines = [f"Era stability window {report['since']} -> {report['until']}"]
    lines.append(
        f"  {report['events']} events, {report['turns']} turns "
        f"({report['interrupted_turns']} interrupted), {report['process_starts']} process "
        f"start(s), {report['relaunches_requested']} requested relaunch(es)"
    )
    for key, stat in report["latency_ms"].items():
        lines.append(f"  {key}: p50 {stat['p50']:.0f} ms, p95 {stat['p95']:.0f} ms (n={stat['n']})")
    lines.append(f"  tool calls: {report['tool_calls'] or 'none'}")
    lines.append(
        f"  tool failures: {report['tool_failures']} "
        f"(other than bounded refusals: {report['tool_failures_unexpected'] or 'none'})"
    )
    if report["slow_tool_failures"]:
        lines.append(f"  SLOW failures (>=10 s, user waited): {report['slow_tool_failures']}")
    if report["notes"]:
        lines.append(f"  normal-but-notable: {report['notes']}")
    if report["unattributed_replies"]:
        lines.append(
            f"  replies with no input owner (informational): {report['unattributed_replies']}"
        )
    hard = report["hard"]
    lines.append("  HARD signals: " + (str(hard) if hard else "none"))
    lines.append("VERDICT: " + ("ATTENTION - investigate the HARD signals" if hard else "CLEAN"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--since", default="60m", help="30m, 3h, 2d, last-start or an ISO time (UTC)"
    )
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)

    events = load_events(args.log_dir)
    if not events:
        print(f"No readable events in {args.log_dir}", file=sys.stderr)
        return 2
    until = events[-1]["_t"]
    report = build_report(events, parse_since(args.since, until, events), until, args.log_dir)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 1 if report["hard"] else 0


if __name__ == "__main__":
    sys.exit(main())
