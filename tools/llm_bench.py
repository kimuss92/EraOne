"""Benchmark harness for Era's model aliases — latency, cost, and correctness.

WHY THIS EXISTS
---------------
"Gemini is fast, Claude reasons better" is a hypothesis, not an architecture.
Routing policy is currently justified by two probe calls from phase 4; this is
the thing that turns that into evidence, and that can be re-run when a provider
renames a model or changes its pricing.

WHAT IT MEASURES, AND WHAT IT REFUSES TO
----------------------------------------
Latency and cost are measured directly. Correctness is measured ONLY on tasks
with a checkable answer — classification into a fixed label set, extraction of a
value that is present, arithmetic with one right result, JSON that either has
the required keys or does not.

It does NOT score prose quality. Doing that needs a judge model, which makes the
benchmark's headline number depend on a third model's opinion and quietly turns
"which is better" into "which does the judge prefer". A number like that reads
as evidence while being nearly unfalsifiable, so this reports pass rates on
verifiable work and says plainly that style is unmeasured.

SPENDING
--------
Nothing calls a provider unless `--live` is passed. The default is a dry run
that exercises the whole harness against a stub, so the plumbing can be tested
and the report format reviewed for free. `--live` prints the projected cost from
the registry's measured per-call overhead and requires `--yes` to skip the
confirmation prompt.

SAMPLE SIZE
-----------
One call is an anecdote. Every report states n, and `summarize()` reports the
spread, not just the mean — a p50 that hides a 12s outlier is how a "fast" model
gets chosen for an interactive path it cannot serve.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm.registry import RegistryError, resolve  # noqa: E402
from core.llm.types import LlmMessage, LlmRequest, LlmResponse  # noqa: E402


class BenchError(RuntimeError):
    """The benchmark could not run. Always says what was being attempted."""


# ── checks ────────────────────────────────────────────────────────────────
# Named rather than inline lambdas so a failing task names the rule it broke.


def exact_word(expected: str) -> Callable[[str], bool]:
    """The reply, stripped of punctuation and case, IS this word.

    Deliberately strict: a classifier that answers "I think it's volume,
    probably" has not produced a label a deterministic layer could act on.
    """

    def check(text: str) -> bool:
        cleaned = "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()
        return cleaned == expected.lower()

    check.__name__ = f"exact_word({expected!r})"
    return check


def contains(expected: str) -> Callable[[str], bool]:
    def check(text: str) -> bool:
        return expected.lower() in text.lower()

    check.__name__ = f"contains({expected!r})"
    return check


# The next two exist because the obvious checks measure the wrong thing.
#
#   contains("11")     passes on "11:15", which is not the answer.
#   exact_word("11")   fails on "11 times", which IS the answer.
#
# The first inflates the score, the second conflates reasoning with formatting
# and would mark a model wrong for adding a word. Both take the LAST token of
# the right kind instead: a model that shows its working puts the answer at the
# end, so this grades the conclusion without demanding a bare token.


def _tokens(text: str) -> list[str]:
    return "".join(c if c.isalnum() else " " for c in text).split()


def number_is(expected: int) -> Callable[[str], bool]:
    """The last standalone integer in the reply is this number."""

    def check(text: str) -> bool:
        nums = [int(t) for t in _tokens(text) if t.isdigit()]
        return bool(nums) and nums[-1] == expected

    check.__name__ = f"number_is({expected})"
    return check


def last_letter_is(expected: str) -> Callable[[str], bool]:
    """The last standalone single letter in the reply is this one."""

    def check(text: str) -> bool:
        letters = [t.lower() for t in _tokens(text) if len(t) == 1 and t.isalpha()]
        return bool(letters) and letters[-1] == expected.lower()

    check.__name__ = f"last_letter_is({expected!r})"
    return check


def json_has_keys(*keys: str) -> Callable[[str], bool]:
    def check(text: str) -> bool:
        body = text.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            return False
        return isinstance(parsed, dict) and all(k in parsed for k in keys)

    check.__name__ = f"json_has_keys{keys}"
    return check


# ── the task suite ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BenchTask:
    id: str
    category: str  # mirrors routing.by_task, so results map onto policy
    prompt: str
    check: Callable[[str], bool]
    why: str

    def passed(self, text: str) -> bool:
        try:
            return bool(self.check(text))
        except Exception:
            return False


_LABELS = "volume, media, search, other"

TASKS: tuple[BenchTask, ...] = (
    BenchTask(
        id="intent.volume",
        category="short_command",
        prompt=(
            f"Classify the command into exactly one label from: {_LABELS}. "
            "Reply with the single label word and nothing else.\n"
            "Command: turn it up a bit"
        ),
        check=exact_word("volume"),
        why="The class the deterministic-bypass question is about.",
    ),
    BenchTask(
        id="intent.media",
        category="short_command",
        prompt=(
            f"Classify the command into exactly one label from: {_LABELS}. "
            "Reply with the single label word and nothing else.\n"
            "Command: put on something chill"
        ),
        check=exact_word("media"),
        why="Same class, vaguer wording — where a regex starts to struggle.",
    ),
    BenchTask(
        id="extract.value",
        category="short_command",
        prompt=(
            "From this line, reply with ONLY the number of gigabytes free.\n"
            "Disk C: 512 GB total, 118 GB free, 394 GB used."
        ),
        check=number_is(118),
        why="Extraction of a value that is present — no reasoning needed.",
    ),
    BenchTask(
        id="reason.constraint",
        category="complex_planning",
        prompt=(
            "Four services start in order. B starts after D. A starts before C. "
            "D starts before A. C is not last. Which service starts LAST? "
            "Reply with only the letter."
        ),
        check=last_letter_is("b"),
        why="Multi-constraint ordering with exactly one valid answer.",
    ),
    BenchTask(
        id="reason.arithmetic",
        category="complex_planning",
        prompt=(
            "A job runs every 45 minutes starting at 09:00. It is skipped if it "
            "would start after 17:00. How many times does it run in one day? "
            "Reply with only the number."
        ),
        check=number_is(11),
        why="Multi-step arithmetic where an off-by-one is the natural failure.",
    ),
    BenchTask(
        id="format.json",
        category="complex_planning",
        prompt=(
            "Reply with ONLY a JSON object, no prose and no code fence, with "
            'exactly the keys "action" and "target", describing: open the '
            "budget spreadsheet."
        ),
        check=json_has_keys("action", "target"),
        why="Structured output Era's deterministic layer could actually parse.",
    ),
)


# ── results ───────────────────────────────────────────────────────────────


@dataclass
class BenchResult:
    task_id: str
    category: str
    alias: str
    provider: str = ""
    model: str = ""
    ok: bool = False  # the call itself completed
    passed: bool = False  # the answer was correct
    latency_ms: int = 0
    first_token_ms: int | None = None
    cost_usd: float = 0.0
    reported_cost: bool = False
    text: str = ""
    error: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "category": self.category,
            "alias": self.alias,
            "provider": self.provider,
            "ok": self.ok,
            "passed": self.passed,
            "latency_ms": self.latency_ms,
            "cost_usd": round(self.cost_usd, 4),
            "reported_cost": self.reported_cost,
            "error": self.error[:120],
        }


@dataclass
class AliasSummary:
    alias: str
    provider: str = ""
    model: str = ""
    n: int = 0
    errors: int = 0
    passes: int = 0
    latencies: list[int] = field(default_factory=list)
    total_cost: float = 0.0
    reported_cost: bool = False

    @property
    def pass_rate(self) -> float:
        graded = self.n - self.errors
        return (self.passes / graded) if graded else 0.0

    @property
    def p50_ms(self) -> int:
        return int(statistics.median(self.latencies)) if self.latencies else 0

    @property
    def worst_ms(self) -> int:
        """Reported alongside p50 on purpose: a median that hides a 12s outlier
        is how a 'fast' model gets picked for a path it cannot serve."""
        return max(self.latencies) if self.latencies else 0

    @property
    def cost_per_call(self) -> float:
        return (self.total_cost / self.n) if self.n else 0.0


# ── running ───────────────────────────────────────────────────────────────


class StubProvider:
    """Answers every task correctly, instantly, for free.

    The dry-run default. It exercises the whole harness — task loop, timing,
    scoring, summary, report — without a network or a bill, which is what makes
    the plumbing testable and keeps an accidental run from costing anything.
    """

    name = "stub"

    _ANSWERS = {
        "intent.volume": "volume",
        "intent.media": "media",
        "extract.value": "118",
        "reason.constraint": "B",
        "reason.arithmetic": "11",
        "format.json": '{"action": "open", "target": "budget spreadsheet"}',
    }

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = dict(self._ANSWERS)
        if answers:
            self.answers.update(answers)
        self.calls: list[LlmRequest] = []

    def generate(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(
            request_id=request.request_id,
            provider=self.name,
            model_id="stub",
            alias=request.alias,
            text=self.answers.get(request.request_id, ""),
        )


def build_provider(provider_name: str) -> Any:
    """Real adapter for a provider name. Imported lazily so a dry run needs
    neither the genai SDK nor the claude binary."""
    if provider_name == "claude_cli":
        from core.llm.adapters.claude_cli import ClaudeCliProvider

        return ClaudeCliProvider()
    if provider_name == "gemini":
        from core.llm.adapters.gemini_generate import GeminiGenerateProvider

        return GeminiGenerateProvider()
    raise BenchError(f"no benchmark adapter for provider {provider_name!r}")


def run_task(provider: Any, alias: str, task: BenchTask, *, timeout_s: float = 60.0) -> BenchResult:
    """One task against one alias. Never raises — a provider error is a RESULT.

    An exception here would abandon the rest of the sweep and lose the results
    already paid for.
    """
    result = BenchResult(task_id=task.id, category=task.category, alias=alias)
    request = LlmRequest(
        alias=alias,
        messages=(LlmMessage.text("user", task.prompt),),
        request_id=task.id,
        max_output_tokens=512,
        timeout_s=timeout_s,
    )
    started = time.perf_counter()
    try:
        response = provider.generate(request)
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    wall_ms = int((time.perf_counter() - started) * 1000)
    result.ok = True
    result.provider = response.provider
    result.model = response.model_id
    result.text = (response.text or "").strip()
    result.passed = task.passed(result.text)
    # Prefer the adapter's own figure (it excludes harness overhead), fall back
    # to the wall clock so a provider that reports nothing still gets timed.
    result.latency_ms = response.usage.latency_ms or wall_ms
    result.first_token_ms = response.usage.first_token_ms
    result.cost_usd = response.usage.est_cost_usd
    result.reported_cost = response.usage.reported_cost
    return result


def run_bench(
    aliases: Sequence[str],
    tasks: Sequence[BenchTask] = TASKS,
    *,
    repeats: int = 1,
    provider_factory: Callable[[str], Any] = build_provider,
    live: bool = False,
) -> list[BenchResult]:
    """Run every task against every alias. `live=False` uses the stub."""
    if repeats < 1:
        raise BenchError("repeats must be >= 1")
    results: list[BenchResult] = []
    for alias in aliases:
        try:
            entry = resolve(alias)
        except RegistryError as e:
            results.append(BenchResult(task_id="-", category="-", alias=alias, error=str(e)))
            continue
        provider = provider_factory(entry.provider) if live else StubProvider()
        for _ in range(repeats):
            for task in tasks:
                res = run_task(provider, alias, task)
                if not res.provider:
                    res.provider = entry.provider
                    res.model = entry.model
                results.append(res)
    return results


def projected_cost(aliases: Sequence[str], tasks: Sequence[BenchTask], repeats: int) -> float:
    """What a live sweep will cost, from the registry's MEASURED per-call
    overhead. Shown before spending, not after."""
    total = 0.0
    for alias in aliases:
        try:
            total += resolve(alias).call_overhead_usd * len(tasks) * repeats
        except RegistryError:
            continue
    return total


# ── reporting ─────────────────────────────────────────────────────────────


def summarize(results: Sequence[BenchResult]) -> dict[str, AliasSummary]:
    out: dict[str, AliasSummary] = {}
    for r in results:
        s = out.setdefault(r.alias, AliasSummary(alias=r.alias))
        s.provider = s.provider or r.provider
        s.model = s.model or r.model
        s.n += 1
        if not r.ok:
            s.errors += 1
            continue
        if r.passed:
            s.passes += 1
        s.latencies.append(r.latency_ms)
        s.total_cost += r.cost_usd
        s.reported_cost = s.reported_cost or r.reported_cost
    return out


def format_report(results: Sequence[BenchResult], *, live: bool) -> str:
    summary = summarize(results)
    n_per_alias = max((s.n for s in summary.values()), default=0)
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 78)
    lines.append(f"  Era model benchmark — {'LIVE' if live else 'DRY RUN (stub provider)'}")
    lines.append("=" * 78)
    if not live:
        lines.append("  Numbers below are from a stub. They test the harness, not the models.")
    lines.append(f"  n = {n_per_alias} calls per alias")
    lines.append("")
    header = f"  {'alias':<14}{'provider':<12}{'pass':>7}{'p50 ms':>9}{'worst':>8}{'$/call':>10}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for alias in sorted(summary):
        s = summary[alias]
        cost = f"${s.cost_per_call:.4f}" if s.cost_per_call else "-"
        if s.cost_per_call and not s.reported_cost:
            cost += "~"  # derived from prices, not reported by the provider
        lines.append(
            f"  {alias:<14}{s.provider:<12}{s.pass_rate * 100:>6.0f}%"
            f"{s.p50_ms:>9}{s.worst_ms:>8}{cost:>10}"
        )
        if s.errors:
            lines.append(f"  {'':<14}{s.errors} call(s) failed outright")
    lines.append("")
    lines.append("  ~ = cost derived from registry prices, not reported by the provider.")
    lines.append("  pass rate covers VERIFIABLE tasks only; prose quality is not measured.")

    failures = [r for r in results if r.ok and not r.passed]
    if failures:
        lines.append("")
        lines.append("  Wrong answers:")
        for r in failures:
            lines.append(f"    {r.alias:<14}{r.task_id:<20}-> {r.text[:52]!r}")
    errors = [r for r in results if not r.ok]
    if errors:
        lines.append("")
        lines.append("  Errors:")
        for r in errors:
            lines.append(f"    {r.alias:<14}{r.task_id:<20}{r.error[:60]}")
    lines.append("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark Era's model aliases.")
    ap.add_argument("--aliases", default="era.fast,era.reason", help="comma-separated")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--category", default="", help="only tasks in this category")
    ap.add_argument("--live", action="store_true", help="call real providers (COSTS MONEY)")
    ap.add_argument("--yes", action="store_true", help="skip the live confirmation")
    ap.add_argument("--json", dest="as_json", action="store_true", help="emit rows as JSON")
    args = ap.parse_args(argv)

    aliases = [a.strip() for a in args.aliases.split(",") if a.strip()]
    if not aliases:
        print("no aliases given", file=sys.stderr)
        return 2
    tasks = tuple(t for t in TASKS if not args.category or t.category == args.category)
    if not tasks:
        print(f"no tasks in category {args.category!r}", file=sys.stderr)
        return 2

    if args.live:
        estimate = projected_cost(aliases, tasks, args.repeats)
        calls = len(aliases) * len(tasks) * args.repeats
        print(f"LIVE run: {calls} call(s), projected cost ~${estimate:.2f}")
        if not args.yes:
            reply = input("proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted — nothing was called.")
                return 1

    results = run_bench(aliases, tasks, repeats=args.repeats, live=args.live)
    if args.as_json:
        print(json.dumps([r.to_row() for r in results], indent=1))
    else:
        print(format_report(results, live=args.live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
