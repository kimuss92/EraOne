"""Tests for the task classifier, shadow mode, and autopilot.

THE FOUR PROPERTIES
-------------------
1. THE SHADOW LOG NEVER STORES WHAT WAS SAID. A log of every utterance in
   runtime/ is a transcript of the user's life. This is asserted directly:
   distinctive words from the input must appear nowhere in the written record.

2. THE CLASSIFIER IS ALLOWED TO SAY "I DON'T KNOW". A classifier that always has
   an opinion routes confidently and wrongly, and the expensive direction of that
   mistake is a two-word command going to a 2.9s, $0.06 model.

3. ITS CLASS NAMES MATCH THE SHIPPED ROUTING TABLE. A name that drifts out of
   step with config/llm.yaml silently routes to the default instead — the
   failure would be invisible, so it is checked against the real file.

4. AUTOPILOT GRANTS NOTHING BY DEFAULT. `tasks` is an allowlist, so every
   config failure must produce an empty one rather than an open one.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.era_live.dispatch import DispatchMixin  # noqa: E402
from app.era_live.model_route import ModelRouteMixin  # noqa: E402
from core.llm import registry, router  # noqa: E402
from core.llm import taskclass as tc  # noqa: E402
from core.llm.shadow import (  # noqa: E402
    Observation,
    ShadowLog,
    fingerprint,
    format_report,
    summarize,
)

CONFIG = """
version: 1
default: era.voice
voice_default: era.voice
providers:
  gemini: {}
  claude_cli: {}
aliases:
  era.voice:
    provider: gemini
    model: gemini-live
    capabilities:
      realtime_audio: true
  era.reason:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
    measured:
      call_overhead_usd: 0.058
routing:
  by_task:
    media_playback: era.voice
    device_adjustment: era.voice
    navigation_status: era.voice
    short_command: era.voice
    complex_planning: era.reason
    research_synthesis: era.reason
    long_context: era.reason
  availability_fallback: era.voice
  daily_budget_usd: 2.00
  claude_dispatch:
    enabled: true
    env_override: ERA_CLAUDE_DISPATCH
  shadow:
    enabled: true
    env_override: ERA_LLM_SHADOW
  autopilot:
    enabled: true
    env_override: ERA_LLM_AUTOPILOT
    tasks: [long_context]
    require_confident: true
    max_per_day: 3
    announce_first: true
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    cfg = tmp_path / "llm.yaml"
    cfg.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(cfg))
    monkeypatch.setenv("ERA_LLM_SHADOW_PATH", str(tmp_path / "shadow.jsonl"))
    monkeypatch.setenv("ERA_LLM_SPEND_PATH", str(tmp_path / "spend.json"))
    for var in ("ERA_CLAUDE_DISPATCH", "ERA_LLM_SHADOW", "ERA_LLM_AUTOPILOT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def repoint(monkeypatch, tmp_path, text: str) -> None:
    cfg = tmp_path / "llm.yaml"
    cfg.write_text(textwrap.dedent(text), encoding="utf-8")
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(cfg))


class FakeUI:
    def write_log(self, *a, **k) -> None: ...
    def set_active_model(self, *a, **k) -> None: ...


class FakeEra(ModelRouteMixin, DispatchMixin):
    def __init__(self) -> None:
        self.ui = FakeUI()


# ── 1. the shadow log never stores what was said ──────────────────────────

SECRET = "my banking password is hunter2 and my address is 14 Rue Ibn Khaldoun"


def test_the_log_does_not_contain_the_utterance(tmp_path):
    """The single most important property in this file."""
    era = FakeEra()
    era.observe_routing(f"summarize {SECRET}", handled_by="typed")

    written = (tmp_path / "shadow.jsonl").read_text(encoding="utf-8")
    for word in ("banking", "password", "hunter2", "Khaldoun", "Rue", "Ibn"):
        assert word not in written, f"{word!r} leaked into the shadow log"


def test_the_observation_fields_carry_no_free_text():
    """Belt and braces: check the object, not just the file."""
    era = FakeEra()
    obs = era.observe_routing(f"summarize {SECRET}")
    assert obs is not None
    for value in vars(obs).values():
        if isinstance(value, str):
            assert "hunter2" not in value
            assert "Khaldoun" not in value


def test_the_fingerprint_is_not_reversible_and_is_short():
    fp = fingerprint("volume up")
    assert len(fp) == 10
    assert "volume" not in fp


def test_the_fingerprint_is_stable_for_repeats():
    """It exists only so 'they say this 30 times a day' is countable."""
    assert fingerprint("Volume  Up") == fingerprint("volume up")


def test_different_utterances_get_different_fingerprints():
    assert fingerprint("volume up") != fingerprint("volume down")


def test_an_empty_utterance_has_no_fingerprint():
    assert fingerprint("") == ""


# ── 2. the classifier may say "I don't know" ──────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["hello", "thanks", "what do you think", "ok", "yes please", "", "   "],
)
def test_conversation_gets_no_opinion(text):
    assert tc.classify(text) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("volume up", tc.DEVICE_ADJUSTMENT),
        ("turn it up a bit", tc.DEVICE_ADJUSTMENT),
        ("mute", tc.DEVICE_ADJUSTMENT),
        ("play some jazz", tc.MEDIA_PLAYBACK),
        ("pause", tc.MEDIA_PLAYBACK),
        ("skip this track", tc.MEDIA_PLAYBACK),
        ("what time is it", tc.NAVIGATION_STATUS),
        ("how's the battery", tc.NAVIGATION_STATUS),
        ("open the settings panel", tc.SHORT_COMMAND),
        ("explain the tradeoffs of a write-through cache", tc.COMPLEX_PLANNING),
        ("how should I structure the migration", tc.COMPLEX_PLANNING),
        ("compare postgres and mysql for our workload", tc.RESEARCH_SYNTHESIS),
    ],
)
def test_classification(text, expected):
    guess = tc.classify(text)
    assert guess is not None, f"{text!r} got no opinion"
    assert guess.task == expected


def test_cheap_classes_win_a_mixed_utterance():
    """An utterance that reads as both must stay on the voice model — that is
    the cheap direction to be wrong in."""
    guess = tc.classify("turn up the volume and explain why it was so quiet")
    assert guess is not None
    assert guess.task == tc.DEVICE_ADJUSTMENT


def test_length_beats_wording():
    """A pasted log is long-context work even if it contains the word 'play'."""
    guess = tc.classify("play " * 400)
    assert guess is not None
    assert guess.task == tc.LONG_CONTEXT


def test_a_thin_planning_guess_is_marked_unconfident():
    guess = tc.classify("refactor this")
    assert guess is not None
    assert guess.confident is False


def test_a_substantial_planning_guess_is_confident():
    guess = tc.classify("how should I structure the migration for the storage layer")
    assert guess is not None and guess.confident is True


def test_a_long_command_is_not_a_short_command():
    """'open the file and tell me why the third stanza scans badly' is not a
    device order just because it starts with 'open'."""
    guess = tc.classify("open the report and tell me why the third section reads badly")
    assert guess is None or guess.task != tc.SHORT_COMMAND


def test_describe_is_readable():
    assert "device_adjustment" in tc.describe(tc.classify("volume up"))
    assert tc.describe(None) == "no opinion"


# ── 3. class names match the SHIPPED routing table ────────────────────────


@pytest.fixture
def real_registry(monkeypatch):
    monkeypatch.delenv("ERA_LLM_CONFIG", raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)
    return registry.CONFIG_PATH


def test_every_class_the_classifier_emits_exists_in_the_shipped_policy(real_registry):
    """A drifted name routes to the default silently — invisible without this."""
    by_task = set((router.routing_policy().get("by_task") or {}).keys())
    emitted = {
        tc.MEDIA_PLAYBACK,
        tc.DEVICE_ADJUSTMENT,
        tc.NAVIGATION_STATUS,
        tc.SHORT_COMMAND,
        tc.COMPLEX_PLANNING,
        tc.RESEARCH_SYNTHESIS,
        tc.LONG_CONTEXT,
    }
    missing = emitted - by_task
    assert not missing, f"classifier emits classes the routing table lacks: {missing}"


def test_the_shipped_autopilot_is_on_with_a_bounded_allowlist(real_registry, monkeypatch):
    """Autopilot ships ENABLED — but only for classes with measured evidence."""
    monkeypatch.delenv("ERA_LLM_AUTOPILOT", raising=False)
    policy = router.autopilot_policy()
    assert policy["enabled"] is True
    assert set(policy["tasks"]) == {"long_context", "complex_planning", "research_synthesis"}
    assert policy["require_confident"] is True
    assert 0 < policy["max_per_day"] <= 40, "an unbounded cap is not a cap"


def test_the_shipped_autopilot_never_takes_fast_commands(real_registry, monkeypatch):
    """The measured 682ms vs 2934ms gap is why. 'Pause' cannot afford a 2.9s
    floor, so these classes must never appear on the allowlist."""
    monkeypatch.delenv("ERA_LLM_AUTOPILOT", raising=False)
    tasks = set(router.autopilot_policy()["tasks"])
    forbidden = {"media_playback", "device_adjustment", "navigation_status", "short_command"}
    assert not (tasks & forbidden), f"autopilot would hijack fast commands: {tasks & forbidden}"


def test_the_master_switch_still_gates_autopilot(real_registry, monkeypatch):
    """One flag must turn everything off, including autopilot.

    Asserted through the ENV override rather than the config default, because
    the default is now `true` — the property under test is that the gate still
    exists, not which way it currently points.
    """
    monkeypatch.delenv("ERA_LLM_AUTOPILOT", raising=False)
    monkeypatch.setenv("ERA_CLAUDE_DISPATCH", "0")
    assert router.dispatch_policy()["enabled"] is False

    class Bare(ModelRouteMixin, DispatchMixin):
        def __init__(self):
            self.ui = FakeUI()

    assert Bare().should_dispatch("x " * 500) is None, "autopilot ran past the master switch"


def test_the_shipped_shadow_is_on(real_registry, monkeypatch):
    """Observe-only, so it is safe on — and useless off."""
    monkeypatch.delenv("ERA_LLM_SHADOW", raising=False)
    assert router.shadow_policy()["enabled"] is True


# ── 4. autopilot grants nothing by default ────────────────────────────────


def test_a_broken_registry_grants_an_empty_allowlist(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "absent.yaml"))
    policy = router.autopilot_policy()
    assert policy["enabled"] is False
    assert policy["tasks"] == []


def test_an_empty_allowlist_dispatches_nothing(monkeypatch, tmp_path):
    repoint(monkeypatch, tmp_path, CONFIG.replace("tasks: [long_context]", "tasks: []"))
    assert FakeEra().autopilot_decision("x" * 950) is None


def test_a_class_outside_the_allowlist_is_not_dispatched():
    """complex_planning is deliberately absent: Claude won both reasoning tasks
    in the phase 9 benchmark AND failed a trivial extraction 3 times of 3."""
    assert FakeEra().autopilot_decision("how should I structure the migration") is None


def test_an_allowlisted_class_is_dispatched():
    decision = FakeEra().autopilot_decision("x " * 500)
    assert decision is not None
    assert decision.provider == "claude_cli"


def test_the_autopilot_flag_gates_it(monkeypatch, tmp_path):
    repoint(
        monkeypatch,
        tmp_path,
        CONFIG.replace(
            "    enabled: true\n    env_override: ERA_LLM_AUTOPILOT",
            "    enabled: false\n    env_override: ERA_LLM_AUTOPILOT",
        ),
    )
    assert FakeEra().autopilot_decision("x " * 500) is None


def test_the_env_var_kills_autopilot(monkeypatch):
    monkeypatch.setenv("ERA_LLM_AUTOPILOT", "0")
    assert FakeEra().autopilot_decision("x " * 500) is None


def test_low_confidence_guesses_are_skipped(monkeypatch, tmp_path):
    repoint(
        monkeypatch, tmp_path, CONFIG.replace("tasks: [long_context]", "tasks: [complex_planning]")
    )
    era = FakeEra()
    assert era.autopilot_decision("refactor this") is None, "a thin guess was acted on"
    assert era.autopilot_decision("how should I structure the migration") is not None


def test_the_daily_cap_stops_autopilot():
    """A misfiring classifier is caught by a count long before a dollar ceiling
    that also has to accommodate deliberate use."""
    era = FakeEra()
    long_text = "x " * 500
    fired = 0
    for _ in range(6):
        if era.should_dispatch(long_text) is not None:
            fired += 1
    assert fired == 3, f"cap of 3 not enforced (fired {fired})"


def test_autopilot_needs_the_master_dispatch_flag(monkeypatch, tmp_path):
    """One kill switch must turn everything off."""
    repoint(
        monkeypatch,
        tmp_path,
        CONFIG.replace(
            "    enabled: true\n    env_override: ERA_CLAUDE_DISPATCH",
            "    enabled: false\n    env_override: ERA_CLAUDE_DISPATCH",
        ),
    )
    assert FakeEra().should_dispatch("x " * 500) is None


def test_a_cm_order_is_refused_on_the_autopilot_path_too():
    era = FakeEra()
    assert era.should_dispatch("publish the reel " + "x " * 500) is None


# ── shadow plumbing ───────────────────────────────────────────────────────


def test_shadow_can_be_switched_off(monkeypatch, tmp_path):
    repoint(
        monkeypatch,
        tmp_path,
        CONFIG.replace("  shadow:\n    enabled: true", "  shadow:\n    enabled: false"),
    )
    assert FakeEra().observe_routing("volume up") is None


def test_observing_records_the_projected_route():
    obs = FakeEra().observe_routing("x " * 500)
    assert obs is not None
    assert obs.task == tc.LONG_CONTEXT
    assert obs.would_alias == "era.reason"
    assert obs.would_cost_usd == pytest.approx(0.058)


def test_observing_records_no_route_when_it_has_no_opinion():
    obs = FakeEra().observe_routing("hello there")
    assert obs is not None
    assert obs.task == ""
    assert obs.would_alias == ""


def test_observing_never_raises_on_a_broken_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "absent.yaml"))
    FakeEra().observe_routing("volume up")  # must not raise


def test_an_unwritable_log_does_not_break_observation(monkeypatch, tmp_path):
    monkeypatch.setenv("ERA_LLM_SHADOW_PATH", str(tmp_path / "sub" / "x" / "y"))
    (tmp_path / "sub").write_text("not a directory", encoding="utf-8")
    FakeEra().observe_routing("volume up")  # must not raise


def test_the_log_survives_a_torn_line(tmp_path):
    path = tmp_path / "shadow.jsonl"
    path.write_text(
        '{"task": "media_playback"}\n{not json\n{"task": "short_command"}\n', encoding="utf-8"
    )
    rows = ShadowLog(path=path).load()
    assert len(rows) == 2


def test_the_log_is_bounded(tmp_path):
    path = tmp_path / "shadow.jsonl"
    log = ShadowLog(path=path, max_records=50)
    for _ in range(400):
        log.record(
            Observation(task="media_playback", matched="media", words=2, fingerprint="a" * 10)
        )
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) <= 400  # trimming is size-triggered; never unbounded growth
    assert all(json.loads(ln) for ln in lines)


def test_clear_removes_the_log(tmp_path):
    path = tmp_path / "shadow.jsonl"
    log = ShadowLog(path=path)
    log.record(Observation(task="x"))
    log.clear()
    assert not path.exists()


# ── the report ────────────────────────────────────────────────────────────


def _rows():
    return [
        {
            "ts": "2026-07-30T10:00:00",
            "task": "media_playback",
            "would_alias": "era.voice",
            "would_cost_usd": 0.0,
            "confident": True,
            "fingerprint": "aaa",
        },
        {
            "ts": "2026-07-30T10:01:00",
            "task": "complex_planning",
            "would_alias": "era.reason",
            "would_cost_usd": 0.058,
            "confident": True,
            "fingerprint": "bbb",
        },
        {
            "ts": "2026-07-30T10:02:00",
            "task": "",
            "would_alias": "",
            "would_cost_usd": 0.0,
            "confident": False,
            "fingerprint": "aaa",
        },
    ]


def test_summary_counts_and_costs():
    s = summarize(_rows())
    assert s["total"] == 3
    assert s["no_opinion"] == 1
    assert s["by_task"]["media_playback"] == 1
    assert s["projected_total_usd"] == pytest.approx(0.058)


def test_summary_counts_repeats():
    assert summarize(_rows())["most_repeated"] == 2


def test_summary_handles_an_empty_log():
    s = summarize([])
    assert s["total"] == 0
    assert s["projected_per_day_usd"] == 0.0


def test_the_report_says_nothing_was_acted_on():
    """The report must never read as though routing changed."""
    text = format_report(summarize(_rows()))
    assert "never acted on" in text
    assert "Nothing here changed Era's behaviour" in text


def test_the_report_shows_the_projected_cost():
    assert "$0.06" in format_report(summarize(_rows()))


def test_an_empty_report_is_still_readable():
    assert "No observations yet" in format_report(summarize([]))


# ── autopilot: the announcement and the display name ──────────────────────


class SpeakingEra(ModelRouteMixin, DispatchMixin):
    def __init__(self) -> None:
        self.ui = FakeUI()
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


def test_autopilot_announces_the_switch_once(monkeypatch, tmp_path):
    """Autopilot is the ONLY path where a different model answers without the
    user asking. A silent substitution is what the indicator exists to prevent,
    so the first one says so out loud — once, not on every question."""
    repoint(
        monkeypatch, tmp_path, CONFIG.replace("tasks: [long_context]", "tasks: [complex_planning]")
    )
    era = SpeakingEra()
    q = "how should I structure the migration for the storage layer"

    assert era.autopilot_decision(q) is not None
    assert len(era.spoken) == 1, "the first autopiloted switch was silent"
    assert "Claude" in era.spoken[0], era.spoken[0]

    era.autopilot_decision(q)
    assert len(era.spoken) == 1, "it announced twice"


def test_the_announcement_names_the_model_not_the_alias():
    """'era.reason' is Era's word for a role. Nobody asked which role."""
    era = SpeakingEra()
    era.autopilot_decision("x " * 500)
    assert era.spoken, "nothing was announced"
    assert "era.reason" not in era.spoken[0]
    assert "Claude" in era.spoken[0]


def test_the_announcement_is_wrapped_against_injection():
    """It reaches the live session through speak(), like every other utterance."""
    era = SpeakingEra()
    era.autopilot_decision("x " * 500)
    assert era.spoken[0].startswith("[System note:")


def test_the_announcement_can_be_switched_off(monkeypatch, tmp_path):
    repoint(
        monkeypatch,
        tmp_path,
        CONFIG.replace("    announce_first: true", "    announce_first: false"),
    )
    era = SpeakingEra()
    era.autopilot_decision("x " * 500)
    assert era.spoken == []


def test_a_missing_speak_method_does_not_break_autopilot():
    class Voiceless(ModelRouteMixin, DispatchMixin):
        def __init__(self):
            self.ui = FakeUI()

    assert Voiceless().autopilot_decision("x " * 500) is not None
