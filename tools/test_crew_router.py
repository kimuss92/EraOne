"""The crew router: five axes -> tier, tiers -> providers, budgets, caches, the required line.

Only the MODEL providers are faked (FakeExec). The classifier, the budget, the router, the
caches, the turn loop and the vault bridge are the production code.

Each axis is tested ALONE (flip one input from the all-minimal baseline), as asked.
"""

from __future__ import annotations

import json
import re

import pytest

from core import cm_vault, vault_bridge
from core.pending_confirmation import get_shared_store
from crew import router as cr
from crew.cache import CrewCaches, ProviderRequestCache, ResponseCache, ToolResultCache
from crew.registry import load_specs
from crew.router import (
    Axes,
    Budget,
    Policy,
    ProviderReply,
    RoutedLLM,
    Router,
    axis_context_length_tier,
    axis_cost_of_error,
    axis_hop_count,
    axis_needs_tool_calling,
    axis_output_verified,
    choose_tier,
    classify,
    coerce_shape,
    tier_for,
)
from crew.runtime import InProcessTransport
from crew.transport import BotSpec, Turn
from tools.crew_testkit import ScriptedLLM, assert_crew_failure_shape, call, final

LINE = re.compile(
    r"^\[crew\] bot=\w+ tier=\w+ provider=\w+ tokens_in=\d+ tokens_out=\d+ cached=(true|false)$"
)


def spec(caps=(), bot="tester", **kw) -> BotSpec:
    return BotSpec(
        id=bot,
        role="Tester",
        description="d",
        prompt=kw.pop("prompt", "p"),
        capabilities=tuple(caps),
        **kw,
    )


BASE = Axes(
    needs_tool_calling=False,
    context_length_tier="short",
    output_verified=False,
    hop_count="single",
    cost_of_error="low",
)


# ── each axis, alone ────────────────────────────────────────────────────────
def test_axis_needs_tool_calling():
    assert axis_needs_tool_calling(spec(["vault.read"])) is True
    assert axis_needs_tool_calling(spec([])) is False


def test_axis_context_length_tier_is_a_length_test_with_a_sharp_boundary():
    edge = cr.LONG_CONTEXT_CHARS
    assert axis_context_length_tier("a" * (edge - 1)) == "short"
    assert axis_context_length_tier("a" * edge) == "long"
    assert axis_context_length_tier("a" * (edge // 2), "b" * (edge // 2)) == "long"  # summed
    assert axis_context_length_tier("", None) == "short"


@pytest.mark.parametrize(
    ("task", "context", "expected"),
    [
        ("find the rules about tone", {}, False),
        ("draft a warm caption", {}, False),
        ("return the post as JSON", {}, True),
        ("make sure it compiles", {}, True),
        ("all the unit tests must pass", {}, True),
        ("validate it against the schema", {}, True),
        ("a jsonlines remark", {}, False),  # whole words only
        ("anything at all", {"output_verified": True}, True),  # the caller says so
    ],
)
def test_axis_output_verified_reads_the_task_and_the_callers_flag(task, context, expected):
    assert axis_output_verified(Turn(task=task, context=context)) is expected


@pytest.mark.parametrize(("depth", "expected"), [(0, "single"), (1, "multi"), (3, "multi")])
def test_axis_hop_count_is_about_the_turn(depth, expected):
    assert axis_hop_count(Turn(task="t", depth=depth)) == expected


@pytest.mark.parametrize("cap", sorted(cr._EFFECTFUL))
def test_cost_of_error_needs_a_write_task_AND_a_bot_that_can_write(cap):
    assert axis_cost_of_error(spec([cap]), Turn(task="save this note")) == "high"
    assert axis_cost_of_error(spec([cap]), Turn(task="find that note")) == "low"


def test_a_bot_that_cannot_write_is_never_high_cost_whatever_its_task_says():
    reader = spec(["vault.read", "vault.search", "crew.handoff", "code.propose", "code.check"])
    assert axis_cost_of_error(reader, Turn(task="save write publish send replace")) == "low"


def test_classify_assembles_all_five():
    s = spec(["vault.create"])
    turn = Turn(task="save the JSON for the post", depth=1)
    axes = classify(s, turn, "x" * cr.LONG_CONTEXT_CHARS, "")
    assert axes == Axes(True, "long", True, "multi", "high")


def test_coerce_shape_keeps_only_valid_known_keys():
    assert coerce_shape(
        {"needs_tool_calling": True, "cost_of_error": "HIGH", "hop_count": "multi"}
    ) == {"needs_tool_calling": True, "cost_of_error": "high", "hop_count": "multi"}
    assert coerce_shape({"needs_tool_calling": "yes", "cost_of_error": "huge", "tier": "x"}) == {}
    assert coerce_shape({"output_verified": 1}) == {}  # a bool, not a truthy int
    assert coerce_shape("critical") == {} and coerce_shape(None) == {}


# ── shape -> tier ───────────────────────────────────────────────────────────
def test_the_baseline_is_trivial():
    assert tier_for(BASE) == "trivial"


@pytest.mark.parametrize(
    ("change", "tier"),
    [
        ({"needs_tool_calling": True}, "standard"),
        ({"context_length_tier": "long"}, "standard"),
        ({"hop_count": "multi"}, "standard"),
        ({"cost_of_error": "high"}, "critical"),
        # Output that must VALIDATE needs a model that gets it right: a weak one fails it.
        ({"output_verified": True}, "critical"),
    ],
)
def test_one_axis_at_a_time(change, tier):
    assert tier_for(Axes(**{**BASE.__dict__, **change})) == tier


def test_either_critical_signal_is_enough_and_both_stay_critical():
    assert tier_for(Axes(True, "short", True, "single", "low")) == "critical"
    assert tier_for(Axes(True, "short", False, "single", "high")) == "critical"
    assert tier_for(Axes(True, "long", True, "multi", "high")) == "critical"


@pytest.mark.parametrize(
    "change",
    [{}, {"needs_tool_calling": True}, {"cost_of_error": "high"}, {"output_verified": True}],
)
def test_offline_overrides_every_axis(change):
    assert tier_for(Axes(**{**BASE.__dict__, **change}), online=False) == "offline"


# ── the tier belongs to the TASK, not the bot ───────────────────────────────
DREW = spec(
    ["vault.search", "vault.read", "vault.create", "vault.append", "vault.replace_staged"],
    bot="drew",
)
MAYA = spec(["vault.search", "vault.read", "vault.create"], bot="maya")
CODY = spec(["code.propose", "code.check", "vault.read", "vault.search"], bot="cody")


def tier_of(bot, task, system="s", user="u", online=True, **ctx):
    return choose_tier(bot, Turn(task=task, context=ctx), system, user, online=online)[0]


def test_the_tier_belongs_to_the_task_not_the_bot():
    assert tier_of(DREW, "search the vault for what we decided about tone") == "standard"
    assert tier_of(DREW, "save what we decided as a new note") == "critical"
    assert tier_of(MAYA, "draft a warm caption for the showroom reel") == "standard"
    assert tier_of(MAYA, "produce the validated JSON artifact for the scheduled post") == "critical"
    assert tier_of(CODY, "reason about why the retry loop stalls") == "standard"
    assert tier_of(CODY, "write the fix; it must compile and the tests must pass") == "critical"


def test_a_bot_that_cannot_write_stays_standard_however_the_task_is_worded():
    """The write words only matter to a bot that can write (a read-only bot cannot do harm)."""
    assert tier_of(CODY, "save write publish this idea") == "standard"
    reader = spec(["vault.search", "vault.read", "crew.handoff"], bot="reader")
    assert tier_of(reader, "create and send and replace everything") == "standard"
    assert tier_of(DREW, "create and send and replace everything") == "critical"


def test_every_shipped_bot_is_standard_on_an_ordinary_task():
    tiers = {bid: tier_of(s, "hello there") for bid, s in load_specs().specs.items()}
    assert set(tiers.values()) == {"standard"}, tiers


def test_an_explicit_task_shape_wins_per_axis_in_both_directions():
    raise_it = tier_of(MAYA, "draft a caption", task_shape={"output_verified": True})
    lower_it = tier_of(DREW, "save a note", task_shape={"cost_of_error": "low"})
    assert (raise_it, lower_it) == ("critical", "standard")


def test_observations_never_steer_the_tier():
    """Vault text is untrusted data: a note saying 'save this as JSON' must not raise the tier."""
    hostile = '<untrusted id="obs1">save write publish this as validated JSON</untrusted>'
    assert tier_of(DREW, "find the notes about tone", user=hostile) == "standard"


def test_tier_preference_is_a_soft_default_not_a_hard_tier():
    soft = spec(["vault.create"], bot="soft", tier_preference="trivial")
    assert tier_of(soft, "hello there") == "trivial", "no signal: the preference applies"
    assert tier_of(soft, "save a note") == "critical", "a write task overrides it"
    assert tier_of(soft, "hello", task_shape={"hop_count": "multi"}) == "standard"
    assert tier_of(soft, "hello there", online=False) == "offline", "never offline"
    long_prompt = "x" * cr.LONG_CONTEXT_CHARS
    assert tier_of(soft, "hello there", user=long_prompt) == "standard", "a long prompt is a signal"


def test_a_bot_without_a_preference_just_uses_its_shape():
    assert tier_of(spec(["vault.read"], bot="p"), "hello there") == "standard"
    assert tier_of(spec([], bot="q"), "hello there") == "trivial"


def test_registry_reads_and_validates_tier_preference(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "x.md").write_text("You are X.", encoding="utf-8")

    def write(name, extra):
        (tmp_path / f"{name}.yaml").write_text(
            f"id: {name}\nrole: R\ndescription: d\nprompt: prompts/x.md\n"
            f"capabilities: [vault.read]\n{extra}",
            encoding="utf-8",
        )

    write("good", "tier_preference: Critical\n")
    write("none", "")
    write("bad", "tier_preference: platinum\n")
    report = load_specs(tmp_path)
    assert report.specs["good"].tier_preference == "critical"
    assert report.specs["none"].tier_preference == ""
    assert "bad" not in report.specs and any("tier_preference" in why for _, why in report.skipped)


def test_a_handoff_carries_only_its_own_valid_shape(tmp_path):
    from crew import capabilities as caps

    laura = spec(["crew.handoff"], bot="laura", handoffs_to=("drew",))
    seen = []
    parent = Turn(task="t", context={"task_shape": {"cost_of_error": "high"}, "keep": 1})
    ctx = caps.CapabilityContext(
        spec=laura,
        turn=parent,
        caller=caps.caller_for(laura),
        dispatch=lambda bot, turn: seen.append(turn) or None,
    )
    fn = caps.CAPABILITIES["crew.handoff"].fn
    fn(ctx, {"to": "drew", "task": "do it", "shape": {"output_verified": True, "tier": "x"}})
    fn(ctx, {"to": "drew", "task": "do it too"})
    assert seen[0].context == {"keep": 1, "task_shape": {"output_verified": True}}
    assert seen[1].context == {"keep": 1}, "a parent's shape is never inherited"


# ── config ──────────────────────────────────────────────────────────────────
def test_no_tiers_means_no_policy():
    assert Policy.from_mapping({}) is None
    assert Policy.from_mapping({"tiers": {}}) is None
    assert Policy.from_mapping(None) is None


def test_unknown_tier_names_are_dropped():
    p = Policy.from_mapping({"tiers": {"standard": ["ollama"], "bogus": ["x"]}})
    assert list(p.tiers) == ["standard"]


def test_the_shipped_config_is_the_decided_one():
    from core.llm.router import routing_policy

    p = Policy.from_mapping(routing_policy())
    assert p.tiers == {
        "trivial": ("ollama", "nous"),
        "standard": ("claude_cli", "openai"),  # nous is OUT until its terms and limits are known
        "critical": ("claude_cli", "openai", "nous"),
        "offline": ("ollama",),
    }
    assert "nous" not in p.tiers["standard"]
    assert p.budget["nous_portal"] == {"daily_turns": 500, "on_exhaust": "next_tier"}
    assert p.budget["claude_cli"] == {"daily_turns": 200, "on_exhaust": "next_tier"}
    assert p.budget["openai"] == {"daily_turns": 200, "on_exhaust": "next_tier"}
    assert p.budget["ollama"] == {"always_available": True, "keep_alive": "30m"}


def test_the_supervisor_launches_era_with_a_warm_ollama_default():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "era_supervisor.py").read_text("utf-8-sig")
    key = '"OLLAMA_KEEP_ALIVE": "30m"'
    assert key in src
    start = src.index(key)
    spread = src.index("**os.environ", start)
    assert "}" not in src[start:spread], "the default must sit in the same dict, before the spread"
    # being BEFORE `**os.environ` is what lets a value the user already set win


def test_real_executors_refuse_to_run_under_pytest():
    assert cr.OllamaExecutor().available()[0] is False
    assert cr.ClaudeCliExecutor().available()[0] is False


# ── the budget ──────────────────────────────────────────────────────────────
ROWS = {
    "claude_cli": {"daily_turns": 2, "on_exhaust": "next_tier"},
    "nous_portal": {"daily_turns": 1},
    "ollama": {"always_available": True},
    "norow_cap": {},
}


def test_the_budget_caps_a_provider_and_ollama_never_runs_out(tmp_path):
    b = Budget(ROWS, tmp_path / "b.json")
    assert b.allowed("claude_cli") == (True, "")
    b.take("claude_cli")
    b.take("claude_cli")
    ok, why = b.allowed("claude_cli")
    assert not ok and "2/2" in why
    for _ in range(1000):
        b.take("ollama")
    assert b.allowed("ollama") == (True, "")


def test_nous_is_budgeted_as_nous_portal(tmp_path):
    b = Budget(ROWS, tmp_path / "b.json")
    b.take("nous")
    assert b.used("nous") == 1 and not b.allowed("nous")[0]


def test_a_provider_with_no_budget_row_fails_closed(tmp_path):
    b = Budget(ROWS, tmp_path / "b.json")
    ok, why = b.allowed("openai")
    assert not ok and "no budget" in why
    ok, why = b.allowed("norow_cap")
    assert not ok


def test_the_count_survives_a_restart_but_not_a_new_day(tmp_path):
    day = ["2026-10-04"]
    b = Budget(ROWS, tmp_path / "b.json", today=lambda: day[0])
    b.take("claude_cli")
    assert Budget(ROWS, tmp_path / "b.json", today=lambda: day[0]).used("claude_cli") == 1
    day[0] = "2026-10-05"
    assert b.used("claude_cli") == 0
    assert Budget(ROWS, tmp_path / "b.json", today=lambda: day[0]).used("claude_cli") == 0


def test_a_corrupt_budget_file_starts_the_day_at_zero(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("{not json", encoding="utf-8")
    assert Budget(ROWS, p).used("claude_cli") == 0


# ── fakes for the router ────────────────────────────────────────────────────
class FakeExec:
    def __init__(self, name, replies=None, avail=True, why="down"):
        self.name, self.avail, self._why = name, avail, why
        self.replies = list(replies or [])
        self.calls: list[tuple[str, str]] = []

    def available(self):
        return (True, "") if self.avail else (False, self._why)

    def alias_for(self, tier):
        return f"{self.name}.{tier}"

    def call(self, system, user, tier, timeout_s):
        self.calls.append((tier, user))
        item = self.replies.pop(0) if self.replies else json.dumps({"final": "ok"})
        if isinstance(item, Exception):
            raise item
        return ProviderReply(item, 11, 7, "m")


class Rec:
    def __init__(self):
        self.lines: list[str] = []

    def info(self, line):
        self.lines.append(line)

    warning = info


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(cr, "router_logger", r)
    return r


TIERS = {
    "trivial": ("ollama", "nous"),
    "standard": ("nous", "claude_cli", "openai"),
    "critical": ("claude_cli", "openai", "nous"),
    "offline": ("ollama",),
}


ROOMY = {
    "claude_cli": {"daily_turns": 100},
    "nous_portal": {"daily_turns": 100},
    "openai": {"daily_turns": 100},
    "ollama": {"always_available": True},
}


def build(tmp_path, execs, rows=None, tiers=None, clock=None):
    rows = rows or ROOMY
    policy = Policy(tiers or TIERS, rows, {})
    kw = {"clock": clock} if clock else {}
    router = Router(policy, Budget(rows, tmp_path / "b.json"), {e.name: e for e in execs}, **kw)
    return RoutedLLM(router, CrewCaches.from_config({"response_dir": str(tmp_path / "rc")}))


def down(name, why="down"):
    return FakeExec(name, avail=False, why=why)


STANDARD_BOT = spec(["vault.read"])  # tool calling -> standard
CRITICAL_BOT = spec(["vault.create"], bot="writer")  # can write, unverified -> critical
TRIVIAL_BOT = spec([], bot="plain")


def ask(llm, bot=STANDARD_BOT, system="sys", user="usr", task="t", **ctx):
    return llm.ask(bot, Turn(task=task, context=ctx), system, user)


# ── routing ─────────────────────────────────────────────────────────────────
def test_the_first_usable_provider_in_the_tier_answers_and_the_line_is_exact(tmp_path, rec):
    nous = down("nous", "no adapter")
    claude = FakeExec("claude_cli", [json.dumps({"final": "hi"})])
    llm = build(tmp_path, [nous, claude, down("openai")])
    assert ask(llm) == {"final": "hi"}
    assert claude.calls and not nous.calls
    assert rec.lines == [
        "[crew] bot=tester tier=standard provider=claude_cli tokens_in=11 tokens_out=7 cached=false"
    ]
    assert all(LINE.match(line) for line in rec.lines)


def test_an_identical_request_is_served_from_the_provider_cache_for_free(tmp_path, rec):
    claude = FakeExec("claude_cli")
    llm = build(tmp_path, [down("nous"), claude])
    ask(llm)
    used = llm.router.budget.used("claude_cli")
    assert ask(llm) == {"final": "ok"}
    assert len(claude.calls) == 1, "the second identical request must not reach the provider"
    assert llm.router.budget.used("claude_cli") == used, "a cache hit costs no budget"
    assert rec.lines[-1].endswith("cached=true") and LINE.match(rec.lines[-1])


def test_a_different_request_is_not_served_from_the_cache(tmp_path, rec):
    claude = FakeExec("claude_cli")
    llm = build(tmp_path, [down("nous"), claude])
    ask(llm, user="one")
    ask(llm, user="two")
    ask(llm, system="other", user="one")
    assert len(claude.calls) == 3


def test_a_spent_provider_is_skipped_for_the_next_in_the_SAME_tier(tmp_path, rec):
    rows = {**ROOMY, "claude_cli": {"daily_turns": 1}, "openai": {"daily_turns": 5}}
    claude, openai = FakeExec("claude_cli"), FakeExec("openai")
    llm = build(tmp_path, [down("nous"), claude, openai], rows=rows)
    ask(llm, user="a")
    ask(llm, user="b")  # claude_cli is spent after one turn
    assert len(claude.calls) == 1 and len(openai.calls) == 1
    assert "provider=openai" in rec.lines[-1]


def test_an_exhausted_tier_fails_and_never_crosses_without_the_flag(tmp_path, rec):
    ollama = FakeExec("ollama")
    llm = build(
        tmp_path,
        [down("nous", "no adapter"), down("claude_cli"), down("openai"), ollama],
    )
    out = ask(llm)
    assert "__unavailable__" in out and "no adapter" in out["__unavailable__"]
    assert not ollama.calls, "silent cross-tier routing is a bug"
    assert rec.lines == [
        "[crew] bot=tester tier=standard provider=none tokens_in=0 tokens_out=0 cached=false"
    ]
    assert not any("cross_tier" in line for line in rec.lines)


def test_the_task_flag_allows_a_logged_crossing(tmp_path, rec):
    ollama = FakeExec("ollama", [json.dumps({"final": "local"})])
    llm = build(tmp_path, [down("nous"), down("claude_cli"), down("openai"), ollama])
    # standard -> critical (all down) -> trivial (ollama answers)
    out = ask(llm, allow_cross_tier=True)
    assert out == {"final": "local"}
    crossings = [line for line in rec.lines if "cross_tier" in line]
    assert len(crossings) == 2 and "from=standard to=critical" in crossings[0]
    assert "to=trivial" in crossings[1]
    assert LINE.match(rec.lines[-1]) and "tier=trivial provider=ollama" in rec.lines[-1]


@pytest.mark.parametrize("flag", [False, "yes", 1, None])
def test_only_a_literal_true_flag_allows_crossing(tmp_path, rec, flag):
    ollama = FakeExec("ollama")
    llm = build(tmp_path, [down("nous"), down("claude_cli"), down("openai"), ollama])
    assert "__unavailable__" in ask(llm, allow_cross_tier=flag)
    assert not ollama.calls


def test_a_provider_that_raises_is_skipped_and_cooled_down(tmp_path, rec):
    t = [0.0]
    claude = FakeExec("claude_cli", [RuntimeError("boom"), json.dumps({"final": "later"})])
    openai = FakeExec("openai")
    llm = build(
        tmp_path,
        [down("nous"), claude, openai],
        clock=lambda: t[0],
        rows={**ROOMY, "openai": {"daily_turns": 9}},
    )
    assert ask(llm, user="a") == {"final": "ok"}  # openai answered after claude_cli failed
    assert len(claude.calls) == 1
    ask(llm, user="b")  # still cooling down: claude_cli is not retried
    assert len(claude.calls) == 1
    t[0] += cr.COOLDOWN_S + 1
    assert ask(llm, user="c") == {"final": "later"}  # back in rotation
    assert len(claude.calls) == 2


def test_a_no_network_failure_latches_the_offline_tier(tmp_path, rec):
    t = [0.0]
    claude = FakeExec("claude_cli", [ConnectionError("getaddrinfo failed")])
    ollama = FakeExec("ollama", [json.dumps({"final": "offline answer"})])
    llm = build(
        tmp_path,
        [down("nous"), claude, down("openai"), ollama],
        clock=lambda: t[0],
    )
    assert "__unavailable__" in ask(llm, user="a")  # the failure itself does not cross tiers
    assert not llm.router.online()
    assert ask(llm, user="b") == {"final": "offline answer"}
    assert "tier=offline provider=ollama" in rec.lines[-1]
    t[0] += cr.OFFLINE_LATCH_S + 1
    assert llm.router.online()


def test_offline_never_crosses_into_the_online_tiers(tmp_path, rec):
    llm = build(tmp_path, [down("ollama", "not running"), FakeExec("claude_cli")])
    llm.router._offline_until = 1e12
    out = ask(llm, allow_cross_tier=True)
    assert "__unavailable__" in out and "ollama" in out["__unavailable__"]


def test_text_with_no_json_is_reported_not_trusted(tmp_path, rec):
    llm = build(tmp_path, [down("nous"), FakeExec("claude_cli", ["sure thing, I did it"])])
    assert ask(llm) == {"__bad_output__": "sure thing, I did it"}


def test_the_critical_tier_and_trivial_tier_use_their_own_lists(tmp_path, rec):
    ollama, claude = FakeExec("ollama"), FakeExec("claude_cli")
    llm = build(tmp_path, [ollama, down("nous"), claude, down("openai")])
    ask(llm, bot=TRIVIAL_BOT)
    assert "tier=trivial provider=ollama" in rec.lines[-1]
    ask(llm, bot=CRITICAL_BOT, task="save this idea")
    assert "tier=critical provider=claude_cli" in rec.lines[-1]


def test_every_emitted_line_has_the_required_shape(tmp_path, rec):
    llm = build(
        tmp_path, [FakeExec("ollama"), down("nous"), FakeExec("claude_cli"), down("openai")]
    )
    ask(llm, bot=TRIVIAL_BOT)
    ask(llm, bot=TRIVIAL_BOT)  # cached
    ask(llm, bot=CRITICAL_BOT, task="save this idea")
    ask(llm, bot=spec(["vault.read"], bot="nothing"), user="y")
    llm.router.executors["claude_cli"].avail = False
    llm.router.executors["ollama"].avail = False
    ask(llm, bot=CRITICAL_BOT, user="z", task="save this idea")  # no provider at all
    assert len(rec.lines) == 5
    assert all(LINE.match(line) for line in rec.lines), rec.lines


def test_the_same_bot_gets_a_different_tier_for_a_different_task(tmp_path, rec):
    llm = build(tmp_path, [down("nous"), FakeExec("claude_cli"), down("openai")])
    ask(llm, bot=CRITICAL_BOT, task="find the note about tone")
    assert "bot=writer tier=standard" in rec.lines[-1]
    ask(llm, bot=CRITICAL_BOT, task="save this idea", user="other")
    assert "bot=writer tier=critical" in rec.lines[-1]


def test_a_rate_limited_provider_is_skipped_for_as_long_as_it_asked(tmp_path, rec):
    from crew.providers import RateLimited

    t = [0.0]
    claude = FakeExec("claude_cli", [RateLimited("slow down", retry_after_s=300)])
    openai = FakeExec("openai")
    llm = build(tmp_path, [down("nous"), claude, openai], clock=lambda: t[0])
    ask(llm, user="a")  # claude_cli rate-limits, openai answers
    t[0] += cr.COOLDOWN_S + 1  # the default cooldown has passed, Retry-After has not
    ask(llm, user="b")
    assert len(claude.calls) == 1, "must wait the 300 s it asked for, not the default 60"
    t[0] += 300
    ask(llm, user="c")
    assert len(claude.calls) == 2


# ── the caches ──────────────────────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_the_provider_cache_ttl_is_configurable_and_zero_disables_it():
    c = Clock()
    cache = ProviderRequestCache(5.0, clock=c)
    cache.put("k", "v")
    assert cache.get("k") == "v"
    c.t += 6
    assert cache.get("k") is None
    off = ProviderRequestCache(0.0, clock=c)
    off.put("k", "v")
    assert off.get("k") is None


def test_caches_can_be_switched_off_by_config(tmp_path):
    caches = CrewCaches.from_config({"enabled": False, "response_dir": str(tmp_path)})
    caches.provider.put("k", "v")
    assert caches.provider.get("k") is None


def test_the_tool_cache_key_includes_the_brand_scope():
    from core.vault_bridge import Caller

    args = {"title": "Pricing"}
    wide = ToolResultCache.key(Caller(actor="a", brands=None), args)
    delta = ToolResultCache.key(Caller(actor="b", brands=frozenset({"delta_cuisine"})), args)
    ceram = ToolResultCache.key(Caller(actor="c", brands=frozenset({"ceram_quartz"})), args)
    assert len({wide, delta, ceram}) == 3, "a narrower bot must never be served a wider read"


def test_the_tool_cache_expires_after_60_seconds_and_returns_copies():
    from core.tool_result import ToolResult

    c = Clock()
    cache = ToolResultCache(60.0, clock=c)
    cache.put("k", ToolResult.ok(message="m", data={"x": [1]}))
    first = cache.get("k")
    first.data["x"].append(2)  # a caller mutating its copy ...
    assert cache.get("k").data == {"x": [1]}  # ... cannot corrupt the cached one
    c.t += 61
    assert cache.get("k") is None


def test_a_failed_read_is_never_cached():
    from core.tool_result import ToolResult

    cache = ToolResultCache(60.0)
    cache.put("k", ToolResult.fail(message="locked", error_type="VaultBusy"))
    assert cache.get("k") is None


def _ok(msg="answer", **data):
    from core.tool_result import ToolResult

    return ToolResult.ok(message=msg, data={"bot": "tester", "steps": 1, **data})


def test_only_an_answer_that_did_nothing_is_cacheable():
    from core.tool_result import ToolResult

    s, turn = spec(["vault.read"]), Turn(task="t")
    assert ResponseCache.cacheable(s, turn, _ok(capabilities_used=["vault.read", "vault.search"]))
    assert ResponseCache.cacheable(s, turn, _ok(capabilities_used=[]))
    assert not ResponseCache.cacheable(s, turn, _ok(capabilities_used=["vault.create"]))
    assert not ResponseCache.cacheable(s, turn, _ok(capabilities_used=["crew.handoff"]))
    assert not ResponseCache.cacheable(s, turn, _ok(capabilities_used=["code.propose"]))
    assert not ResponseCache.cacheable(s, turn, _ok(effects=[{"capability": "vault.append"}]))
    assert not ResponseCache.cacheable(s, turn, _ok(review={"status": "ok"}))
    assert not ResponseCache.cacheable(s, turn, ToolResult.fail(message="x", error_type="BotError"))
    assert not ResponseCache.cacheable(spec([], bot="nabar"), turn, _ok())
    assert not ResponseCache.cacheable(s, Turn(task="t", context={"review": True}), _ok())


def test_the_response_cache_persists_expires_and_survives_corruption(tmp_path):
    clock = Clock()
    cache = ResponseCache(tmp_path, ttl_s=100, clock=clock)
    s, turn = spec(["vault.read"]), Turn(task="what is the cadence")
    cache.put(s, turn, _ok("five reels", capabilities_used=["vault.read"]))
    # a NEW instance over the same directory = a restart
    again = ResponseCache(tmp_path, ttl_s=100, clock=clock).get(s, turn)
    assert again.success and again.message == "five reels" and again.data["response_cache"] is True
    clock.t += 101
    assert ResponseCache(tmp_path, ttl_s=100, clock=clock).get(s, turn) is None
    clock.t -= 101
    next(tmp_path.glob("*.json")).write_text("{corrupt", encoding="utf-8")
    assert ResponseCache(tmp_path, ttl_s=100, clock=clock).get(s, turn) is None


def test_editing_a_bot_invalidates_what_it_said_before(tmp_path):
    cache = ResponseCache(tmp_path, ttl_s=100, clock=Clock())
    turn = Turn(task="t")
    cache.put(spec(["vault.read"], prompt="old"), turn, _ok(capabilities_used=[]))
    assert cache.get(spec(["vault.read"], prompt="old"), turn) is not None
    assert cache.get(spec(["vault.read"], prompt="new"), turn) is None
    assert cache.get(spec(["vault.read"], prompt="old", brands=("delta_cuisine",)), turn) is None


def test_a_vault_write_drops_cached_answers_and_reads(tmp_path, monkeypatch):
    root = tmp_path / "era_brain"
    (root / "00 - Inbox").mkdir(parents=True)
    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    monkeypatch.setenv("ERA_VAULT_VERIFY_SETTLE_S", "0")
    cache = ResponseCache(tmp_path / "rc", ttl_s=100, clock=Clock())
    tools = ToolResultCache(60.0)
    from core.tool_result import ToolResult

    s, turn = spec(["vault.read"]), Turn(task="t")
    cache.put(s, turn, _ok(capabilities_used=["vault.read"]))
    tools.put("k", ToolResult.ok(message="m"))
    assert cache.get(s, turn) and tools.get("k")
    vault_bridge.write_verified(root / "00 - Inbox" / "n.md", "# n\n")
    assert cache.get(s, turn) is None, "an answer from before the write must not be replayed"
    assert tools.get("k") is None, "a read from before the write must not be served"


# ── the transport, wired to the router and the caches ───────────────────────
@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "era_brain"
    for folder in ("00 - Inbox", "01 - Daily Notes", "04 - Global", "06 - Resources"):
        (root / folder).mkdir(parents=True)
    (root / "04 - Global" / "Rules.md").write_text(
        "---\ntype: reference\nstatus: active\n---\n# Rules\n\nNever mix two stone families.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    monkeypatch.setenv("ERA_VAULT_VERIFY_SETTLE_S", "0")
    monkeypatch.setattr(vault_bridge, "_brand_catalog", lambda: {"delta_cuisine": ["delta"]})
    vault_bridge._STAGED.clear()
    get_shared_store().clear()
    yield root
    vault_bridge._STAGED.clear()
    get_shared_store().clear()


def transport(tmp_path, execs, bot):
    llm = build(tmp_path, execs)
    t = InProcessTransport(routed=llm)
    t.register(bot.id, bot)
    return t


def reply(obj):
    return json.dumps(obj)


def test_a_turn_through_the_transport_logs_one_line_per_model_call(tmp_path, vault, rec):
    claude = FakeExec(
        "claude_cli",
        [reply(call("vault.read", title="Rules")), reply(final("Never mix stone families."))],
    )
    t = transport(tmp_path, [down("nous"), claude], STANDARD_BOT)
    out = t.dispatch("tester", Turn(task="what are the rules"))
    assert out.success and "stone" in out.message
    assert len(rec.lines) == 2 and all(LINE.match(line) for line in rec.lines), rec.lines
    assert all("bot=tester tier=standard provider=claude_cli" in line for line in rec.lines)


def test_no_provider_is_a_typed_llm_unavailable_with_the_reasons(tmp_path, vault, rec):
    t = transport(
        tmp_path,
        [down("nous", "no Nous adapter yet"), down("claude_cli"), down("openai")],
        STANDARD_BOT,
    )
    out = t.dispatch("tester", Turn(task="t"))
    assert_crew_failure_shape(out)
    assert out.error_type == "LLMUnavailable" and "no Nous adapter yet" in out.data["detail"]


def test_an_injected_llm_keeps_the_old_behaviour_and_still_logs_the_line(tmp_path, vault, rec):
    llm = ScriptedLLM({"tester": [final("done")]})
    t = InProcessTransport(llm)
    t.register("tester", STANDARD_BOT)
    assert t.dispatch("tester", Turn(task="t")).success
    assert len(rec.lines) == 1 and LINE.match(rec.lines[0]) and "provider=injected" in rec.lines[0]
    # The script is now empty. A cache would answer the same request again; the old
    # behaviour asks the model again, and the model has nothing left to say.
    again = t.dispatch("tester", Turn(task="t"))
    assert again.error_type == "LLMUnavailable"


def test_a_pure_read_answer_is_replayed_until_the_vault_changes(tmp_path, vault, rec):
    claude = FakeExec(
        "claude_cli",
        [
            reply(call("vault.read", title="Rules")),
            reply(final("Never mix.")),
            reply(final("fresh")),
        ],
    )
    t = transport(tmp_path, [down("nous"), claude], STANDARD_BOT)
    first = t.dispatch("tester", Turn(task="rules?"))
    n = len(claude.calls)
    second = t.dispatch("tester", Turn(task="rules?"))
    assert second.message == first.message and len(claude.calls) == n, "served from cache"
    assert second.data.get("response_cache") is True
    assert rec.lines[-1] == (
        "[crew] bot=tester tier=cache provider=response_cache tokens_in=0 tokens_out=0 cached=true"
    )
    # Change the very note the answer was built from.
    note = vault / "04 - Global" / "Rules.md"
    vault_bridge.write_verified(note, note.read_text(encoding="utf-8") + "\nAlways say why.\n")
    third = t.dispatch("tester", Turn(task="rules?"))
    assert not third.data.get("response_cache"), (
        "an answer from before the write must not be replayed"
    )
    assert len(claude.calls) > n, "the changed note makes a different prompt, so the model is asked"
    assert third.success and third.message == "fresh"


def test_an_unrelated_vault_write_still_drops_the_response_cache(tmp_path, vault, rec):
    """Coarse on purpose: ANY verified write drops cached answers. The provider cache
    (keyed on the full prompt, observations included) is what spares the model call."""
    claude = FakeExec("claude_cli", [reply(final("one")), reply(final("two"))])
    t = transport(tmp_path, [down("nous"), claude], STANDARD_BOT)
    t.dispatch("tester", Turn(task="hello"))
    vault_bridge.write_verified(vault / "00 - Inbox" / "x.md", "# x\n")
    again = t.dispatch("tester", Turn(task="hello"))
    assert not again.data.get("response_cache")
    assert not any("provider=response_cache" in line for line in rec.lines)


def test_an_answer_that_wrote_something_is_never_replayed(tmp_path, vault, rec):
    claude = FakeExec(
        "claude_cli",
        [reply(call("vault.create", title="Idea one", content="a thought")), reply(final("saved"))],
    )
    t = transport(tmp_path, [claude, down("nous"), down("openai")], CRITICAL_BOT)
    a = t.dispatch("writer", Turn(task="save an idea"))
    b = t.dispatch("writer", Turn(task="save an idea"))
    assert a.success and "vault.create" in a.data["capabilities_used"]
    assert not b.data.get("response_cache"), "'saved' must never be replayed without saving"
    assert not any("provider=response_cache" in line for line in rec.lines)
    assert "vault.create" in b.data["capabilities_used"], "the capability ran again for real"


def test_vault_read_is_cached_inside_the_transport_for_the_same_scope(tmp_path, vault, rec):
    from crew import capabilities as caps

    ran = []
    real = caps.CAPABILITIES["vault.read"]

    def counting(ctx, args):
        ran.append(args)
        return real.fn(ctx, args)

    caps.CAPABILITIES["vault.read"] = caps.Capability(
        real.name, real.description, real.args, counting
    )
    try:
        claude = FakeExec(
            "claude_cli",
            [
                reply(call("vault.read", title="Rules")),
                reply(call("vault.read", title="Rules")),
                reply(final("done")),
            ],
        )
        t = transport(tmp_path, [down("nous"), claude], STANDARD_BOT)
        assert t.dispatch("tester", Turn(task="read it twice")).success
    finally:
        caps.CAPABILITIES["vault.read"] = real
    assert len(ran) == 1, "the second read inside 60 s is served from the tool cache"
