"""Tests for tools/llm_bench.py — the model benchmark harness.

THE TWO THINGS THAT MATTER
--------------------------
1. IT CANNOT SPEND MONEY BY ACCIDENT. A benchmark that calls a paid provider on
   an ordinary `pytest` run, or on a bare CLI invocation, is a bug with a bill
   attached. The default path is asserted to touch no provider at all.

2. THE ANSWER KEY IS CORRECT. A benchmark that grades against a wrong expected
   answer is worse than no benchmark: it produces a confident number pointing
   the wrong way. The reasoning tasks are therefore SOLVED here independently —
   the constraint puzzle by brute force over every permutation, the schedule by
   enumerating the actual start times — rather than trusting the literal I typed
   into the task suite.

The check functions get their own tests because they were the subtle part:
`contains("11")` passes on "11:15" and `exact_word("11")` fails on "11 times",
so both the score-inflating and the score-deflating failure are pinned.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import llm_bench as bench  # noqa: E402


class ExplodingProvider:
    """Fails every call. Stands in for a provider that is down."""

    name = "exploding"

    def generate(self, request):
        raise RuntimeError("provider is down")


class RecordingFactory:
    """A provider factory that must never be called on a dry run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, provider_name: str):
        self.calls.append(provider_name)
        return bench.StubProvider()


# ── it cannot spend money by accident ─────────────────────────────────────


def test_dry_run_never_builds_a_real_provider():
    """The safety property. If this fails, `pytest` bills the user."""
    factory = RecordingFactory()
    bench.run_bench(["era.fast"], provider_factory=factory, live=False)
    assert factory.calls == [], "a dry run reached the provider factory"


def test_dry_run_is_the_default():
    factory = RecordingFactory()
    bench.run_bench(["era.fast"], provider_factory=factory)
    assert factory.calls == []


def test_cli_without_live_calls_nothing(monkeypatch, capsys):
    built: list[str] = []
    monkeypatch.setattr(bench, "build_provider", lambda n: built.append(n))
    assert bench.main(["--aliases", "era.fast"]) == 0
    assert built == []
    assert "DRY RUN" in capsys.readouterr().out


def test_live_run_aborts_when_the_user_declines(monkeypatch, capsys):
    """The confirmation must actually gate the spend, not just print."""
    built: list[str] = []
    monkeypatch.setattr(bench, "build_provider", lambda n: built.append(n))
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    assert bench.main(["--aliases", "era.reason", "--live"]) == 1
    assert built == [], "declining the prompt still called the provider"
    assert "aborted" in capsys.readouterr().out


def test_live_run_shows_the_projected_cost_before_spending(monkeypatch, capsys):
    monkeypatch.setattr(bench, "build_provider", lambda n: bench.StubProvider())
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    bench.main(["--aliases", "era.reason", "--live"])
    out = capsys.readouterr().out
    assert "projected cost" in out
    assert "call(s)" in out


def test_projected_cost_uses_the_measured_overhead():
    """era.reason carries a measured $0.058/call floor in the registry."""
    tasks = bench.TASKS[:2]
    cost = bench.projected_cost(["era.reason"], tasks, repeats=2)
    assert cost > 0
    assert cost == pytest.approx(0.058 * 2 * 2, rel=0.01)


def test_projected_cost_ignores_unknown_aliases():
    assert bench.projected_cost(["era.nope"], bench.TASKS, 1) == 0.0


# ── the answer key is correct ─────────────────────────────────────────────


def test_the_constraint_puzzle_has_exactly_one_solution():
    """Solved independently, by brute force, rather than trusting the literal.

    Constraints from the task prompt: B after D, A before C, D before A,
    C not last.
    """
    valid = []
    for order in itertools.permutations("ABCD"):
        pos = {s: i for i, s in enumerate(order)}
        if pos["B"] <= pos["D"]:
            continue
        if pos["A"] >= pos["C"]:
            continue
        if pos["D"] >= pos["A"]:
            continue
        if order[-1] == "C":
            continue
        valid.append(order)

    assert len(valid) == 1, f"puzzle is ambiguous: {valid}"
    assert valid[0][-1] == "B", f"expected B last, got {valid[0]}"

    task = next(t for t in bench.TASKS if t.id == "reason.constraint")
    assert task.passed("B"), "the task's own check rejects the proven answer"


def test_the_schedule_task_answer_is_correct():
    """Enumerate the real start times rather than trusting '11'."""
    starts = []
    minute = 9 * 60
    while minute <= 17 * 60:
        starts.append(minute)
        minute += 45
    assert len(starts) == 11, f"schedule yields {len(starts)}, not 11"

    task = next(t for t in bench.TASKS if t.id == "reason.arithmetic")
    assert task.passed("11")


@pytest.mark.parametrize("task", bench.TASKS, ids=lambda t: t.id)
def test_every_task_is_passed_by_its_own_reference_answer(task):
    """The stub answers each task correctly; if a check disagrees with its own
    reference answer, the benchmark grades everything wrong."""
    answer = bench.StubProvider._ANSWERS[task.id]
    assert task.passed(answer), f"{task.id}: check rejects reference {answer!r}"


def test_the_task_suite_is_non_vacuous():
    assert len(bench.TASKS) >= 5
    assert len({t.category for t in bench.TASKS}) >= 2
    assert all(t.why for t in bench.TASKS), "every task must say what it probes"


def test_task_ids_are_unique():
    ids = [t.id for t in bench.TASKS]
    assert len(ids) == len(set(ids))


# ── the check functions ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("11", True),
        ("11 times", True),
        ("The answer is 11.", True),
        ("11:15", False),
        ("12", False),
        ("", False),
    ],
)
def test_number_is_grades_the_conclusion_not_the_formatting(text, expected):
    assert bench.number_is(11)(text) is expected


def test_number_is_rejects_an_echoed_line():
    """Echoing the source line must not pass — it contains the right digits."""
    assert bench.number_is(118)("512 GB total, 118 GB free, 394 GB used") is False


@pytest.mark.parametrize(
    "text,expected",
    [
        ("B", True),
        ("B.", True),
        ("Service B", True),
        ("The answer is B", True),
        ("A", False),
        ("", False),
    ],
)
def test_last_letter_is(text, expected):
    assert bench.last_letter_is("b")(text) is expected


def test_exact_word_rejects_a_hedged_label():
    """A classifier answering 'I think volume, probably' has not produced a
    label a deterministic layer could act on."""
    assert bench.exact_word("volume")("volume") is True
    assert bench.exact_word("volume")("I think it's volume, probably") is False


def test_json_has_keys_tolerates_a_code_fence():
    assert bench.json_has_keys("action")('```json\n{"action": "open"}\n```') is True


def test_json_has_keys_rejects_prose_and_missing_keys():
    assert bench.json_has_keys("action")("sure, here you go") is False
    assert bench.json_has_keys("action", "target")('{"action": "open"}') is False


# ── running and scoring ───────────────────────────────────────────────────


def test_run_task_never_raises_on_a_dead_provider():
    """An exception would abandon the sweep and lose results already paid for."""
    task = bench.TASKS[0]
    result = bench.run_task(ExplodingProvider(), "era.fast", task)
    assert result.ok is False
    assert "provider is down" in result.error
    assert result.passed is False


def test_a_dead_provider_still_produces_a_row_per_task():
    results = bench.run_bench(
        ["era.fast"], provider_factory=lambda _n: ExplodingProvider(), live=True
    )
    assert len(results) == len(bench.TASKS)
    assert all(not r.ok for r in results)


def test_an_unknown_alias_is_reported_not_raised():
    results = bench.run_bench(["era.doesnotexist"])
    assert len(results) == 1
    assert results[0].ok is False
    assert "unknown model alias" in results[0].error


def test_a_wrong_answer_scores_as_failed():
    provider = bench.StubProvider({"intent.volume": "media"})
    task = next(t for t in bench.TASKS if t.id == "intent.volume")
    result = bench.run_task(provider, "era.fast", task)
    assert result.ok is True
    assert result.passed is False


def test_repeats_multiply_the_calls():
    results = bench.run_bench(["era.fast"], repeats=3)
    assert len(results) == len(bench.TASKS) * 3


def test_repeats_must_be_positive():
    with pytest.raises(bench.BenchError):
        bench.run_bench(["era.fast"], repeats=0)


def test_pass_rate_excludes_failed_calls():
    """A provider outage must not read as a quality problem — nor be scored as
    a pass. Errors are excluded from the denominator and reported separately."""
    results = [
        bench.BenchResult("t1", "c", "era.fast", ok=True, passed=True, latency_ms=10),
        bench.BenchResult("t2", "c", "era.fast", ok=False, error="boom"),
    ]
    s = bench.summarize(results)["era.fast"]
    assert s.n == 2
    assert s.errors == 1
    assert s.pass_rate == 1.0


def test_summary_reports_the_worst_case_not_just_the_median():
    """A p50 hiding a 12s outlier is how a 'fast' model gets picked for an
    interactive path it cannot serve."""
    results = [
        bench.BenchResult("t", "c", "a", ok=True, passed=True, latency_ms=ms)
        for ms in (100, 110, 120, 12000)
    ]
    s = bench.summarize(results)["a"]
    assert s.p50_ms == 115
    assert s.worst_ms == 12000


def test_summary_tracks_whether_cost_was_reported_or_derived():
    derived = [bench.BenchResult("t", "c", "a", ok=True, cost_usd=0.01, reported_cost=False)]
    real = [bench.BenchResult("t", "c", "b", ok=True, cost_usd=0.01, reported_cost=True)]
    assert bench.summarize(derived)["a"].reported_cost is False
    assert bench.summarize(real)["b"].reported_cost is True


def test_empty_results_summarize_without_dividing_by_zero():
    s = bench.summarize([])
    assert s == {}


# ── the report ────────────────────────────────────────────────────────────


def test_report_says_it_is_a_dry_run():
    text = bench.format_report(bench.run_bench(["era.fast"]), live=False)
    assert "DRY RUN" in text
    assert "not the models" in text


def test_report_states_the_sample_size():
    text = bench.format_report(bench.run_bench(["era.fast"]), live=False)
    assert f"n = {len(bench.TASKS)}" in text


def test_report_admits_prose_quality_is_unmeasured():
    """The honesty guard: the report must not read as an overall quality verdict."""
    text = bench.format_report(bench.run_bench(["era.fast"]), live=False)
    assert "prose quality is not measured" in text


def test_report_marks_derived_costs():
    results = [bench.BenchResult("t", "c", "a", ok=True, cost_usd=0.01, reported_cost=False)]
    assert "~" in bench.format_report(results, live=True)


def test_report_lists_wrong_answers():
    provider = bench.StubProvider({"intent.volume": "search"})
    task = next(t for t in bench.TASKS if t.id == "intent.volume")
    text = bench.format_report([bench.run_task(provider, "era.fast", task)], live=True)
    assert "Wrong answers" in text
    assert "intent.volume" in text


def test_report_lists_errors():
    text = bench.format_report(bench.run_bench(["era.nope"]), live=False)
    assert "Errors:" in text


def test_category_filter_selects_a_subset(capsys):
    bench.main(["--aliases", "era.fast", "--category", "complex_planning"])
    out = capsys.readouterr().out
    expected = sum(1 for t in bench.TASKS if t.category == "complex_planning")
    assert f"n = {expected}" in out


def test_unknown_category_is_an_error_not_an_empty_run(capsys):
    assert bench.main(["--aliases", "era.fast", "--category", "nonsense"]) == 2


def test_json_output_is_parseable(capsys):
    import json as _json

    bench.main(["--aliases", "era.fast", "--json"])
    rows = _json.loads(capsys.readouterr().out)
    assert len(rows) == len(bench.TASKS)
    assert {"task", "alias", "passed", "latency_ms"} <= set(rows[0])
