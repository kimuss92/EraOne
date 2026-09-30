"""Read the task trace: how each task was carried out, and where it failed.

    python scripts/task_log.py                 today, every task
    python scripts/task_log.py --failed        only tasks with a failure/guard
    python scripts/task_log.py --last 20       the last 20 tasks
    python scripts/task_log.py --date 20260925 --dir D:\\Z.O.R.O\\logs

Reads logs/task_trace-YYYYMMDD.jsonl (written by core/task_trace.py). ZORO
writes `turn` lines (the whole procedure of one request) and `tool` lines (each
tool call, from the tools server); Era writes `tool` lines. Tool calls are shown
under the turn they belong to (those that happened since the previous turn).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

RED, GREEN, YELLOW, DIM, RESET = (
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)


def _load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _short(v, n=110) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _declined(t: dict) -> bool:
    """A confirmation the user answered 'no' to: the safety gate working, not a failure."""
    return t.get("requested") == "confirm_action" and "Not confirmed" in str(t.get("result", ""))


def _tool_line(t: dict) -> tuple[str, bool]:
    ok = True if _declined(t) else t.get("ok")
    bad = ok is False and not t.get("held")
    mark = (
        f"{YELLOW}HELD{RESET}"
        if t.get("held")
        else (f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}")
    )
    routed = ""
    if t.get("tool") and t.get("tool") != t.get("requested"):
        routed = f" {DIM}-> routed to {t['tool']} {_short(t.get('args'), 60)}{RESET}"
    who = f"[{t.get('caller') or t.get('app', '?')}] "
    head = (
        f"    {who}{t.get('requested')} {_short(t.get('requested_args') or {}, 70)}{routed}"
        f"  {mark} {t.get('duration_ms', '?')}ms"
    )
    tail = f"\n        {DIM}{_short(t.get('result', ''), 150)}{RESET}"
    if t.get("safety") and "LOW" not in str(t.get("safety")):
        tail += f"\n        {DIM}safety: {_short(t['safety'], 120)}{RESET}"
    return head + tail, bad


def _turn_bad(turn: dict, tools: list[dict]) -> bool:
    if turn.get("outcome") not in (None, "ok"):
        return True
    for s in turn.get("steps") or []:
        if s.get("step") in ("error", "guard") or s.get("ok") is False:
            return True
    return any(_tool_line(t)[1] for t in tools)


def show(rows: list[dict], failed_only: bool, last: int | None) -> int:
    tasks: list[tuple[dict | None, list[dict]]] = []
    pending: list[dict] = []
    for r in rows:
        if r.get("kind") == "tool":
            pending.append(r)
        elif r.get("kind") == "turn":
            tasks.append((r, pending))
            pending = []
    if pending:  # Era (no turn lines), or tool calls after the last turn
        tasks.extend((None, [t]) for t in pending)
    if last:
        tasks = tasks[-last:]
    shown = 0
    for turn, tools in tasks:
        bad = _turn_bad(turn, tools) if turn else any(_tool_line(t)[1] for t in tools)
        if failed_only and not bad:
            continue
        shown += 1
        if turn:
            ts = turn["ts"][11:19]
            flag = f"{RED}  <-- problem{RESET}" if bad else ""
            print(f"\n{ts} [{turn.get('source')}] {turn.get('user_text')!r}{flag}")
            print(f"  route: {turn.get('route')}  outcome: {turn.get('outcome')}"
                  + (f"  stt_logprob: {turn['stt_logprob']:.2f}" if isinstance(turn.get("stt_logprob"), float) else ""))  # fmt: skip
            for s in turn.get("steps") or []:
                step = s.get("step")
                rest = {k: v for k, v in s.items() if k != "step"}
                color = RED if step in ("error", "guard") or rest.get("ok") is False else ""
                print(f"  {color}- {step}: {_short(rest, 140)}{RESET if color else ''}")
            for t in tools:
                print(_tool_line(t)[0])
            print(f"  {DIM}said: {_short(turn.get('spoken', ''), 160)}{RESET}")
        else:
            t = tools[0]
            print(f"\n{t['ts'][11:19]} {DIM}{_short(t.get('user_text', ''), 90)!r}{RESET}")
            print(_tool_line(t)[0])
    return shown


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent / "logs"))
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument(
        "--failed",
        action="store_true",
        help="only tasks that failed or tripped a guard",
    )
    ap.add_argument("--last", type=int, default=None)
    a = ap.parse_args()
    path = Path(a.dir) / f"task_trace-{a.date}.jsonl"
    if not path.exists():
        sys.exit(f"No task trace for {a.date} in {a.dir} (it starts once the app runs a task).")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    n = show(_load(path), a.failed, a.last)
    print(f"\n{n} task(s) shown from {path}")


if __name__ == "__main__":
    main()
