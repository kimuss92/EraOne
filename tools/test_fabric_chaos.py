"""Era Fabric F3 chaos tests: one link broken at a time (design sections 8 and 10).

Fakes only: no gateway, no model, no network, no real dev job. For every pathway the
suite asserts (1) a reply is produced, (2) a lower-quality fallback discloses itself,
(3) no gate is bypassed -- a terminal fault is never followed by another provider, on
the event log -- (4) an open breaker costs no call, (5) the hops are recorded in order.
Plus the scope rule: a pathway that would widen a step's rights fails the suite.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.cancel_registry import CancelledTask
from core.fabric import events as ev
from core.fabric import faults as ff
from core.fabric import health as hl
from core.fabric import pathways as pw
from core.tool_result import ToolResult
from crew import review_pathway as rp
from crew.transport import BotSpec, Turn, fail

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ZORO_TASK_LOG_DIR", raising=False)
    monkeypatch.delenv("ERA_FABRIC", raising=False)
    monkeypatch.setattr(ev, "_subscribers", [])
    hl.uninstall()
    hl.reset()
    pw.reset()
    return tmp_path / "logs"


def steps(logs, capability=None):
    files = list((logs / "fabric").glob("events-*.jsonl"))
    rows = [json.loads(x) for f in files for x in f.read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r["topic"] == "task.step"]
    if capability:
        rows = [r for r in rows if r["provenance"].get("capability") == capability]
    return [(r["component"], r["outcome"]) for r in rows]


def down(component="x"):
    return ToolResult.fail(
        message="down", error_type="Unavailable", data={"fault": ff.make("Unavailable", component)}
    )


# ── the file itself ─────────────────────────────────────────────────────────


class TestPathwaysFile:
    def test_the_shipped_file_is_valid_and_complete(self):
        loaded = pw.load(fresh=True)
        assert set(loaded) == {"review_change", "web_search", "speak", "voice_brain"}
        for p in loaded.values():
            assert p.steps[-1].provider == pw.DETERMINISTIC
        assert loaded["review_change"].wired == "pathway"
        assert [s.provider for s in loaded["review_change"].steps] == [
            "crew",
            "llm.claude",
            "deterministic",
        ]

    def test_every_shipped_pathway_is_equal_or_stricter(self):
        for p in pw.load(fresh=True).values():
            for prev, nxt in zip(p.steps, p.steps[1:], strict=False):
                assert nxt.scope <= prev.scope, (p.name, nxt.provider)

    @staticmethod
    def _base():
        return {
            "x": {
                "wired": "pathway",
                "steps": [
                    {"provider": "a", "scope": ["development.read"]},
                    {"provider": "deterministic", "scope": []},
                ],
            }
        }

    def test_a_widening_pathway_is_refused(self):
        data = self._base()
        data["x"]["steps"].insert(1, {"provider": "b", "scope": ["development.read", "shell"]})
        with pytest.raises(pw.PathwayError, match="never wider"):
            pw.validate(data)

    @pytest.mark.parametrize(
        "mutate, needle",
        [
            (lambda d: d["x"]["steps"].pop(), "deterministic"),
            (lambda d: d.update({"vault.write": d["x"]}), "fails closed"),
            (lambda d: d["x"]["steps"].insert(0, {"provider": "safety_guard"}), "fail-closed"),
            (lambda d: d["x"]["steps"][0].update(quality="better"), "quality"),
            (lambda d: d["x"].update(wired="maybe"), "wired"),
            (lambda d: d["x"]["steps"].insert(1, {"provider": "a"}), "twice"),
        ],
    )
    def test_every_rule_refuses_a_broken_file(self, mutate, needle):
        data = self._base()
        mutate(data)
        with pytest.raises(pw.PathwayError, match=needle):
            pw.validate(data)

    def test_existing_chains_name_code_that_exists(self):
        from core import tts_gemini, tts_kokoro, tts_router

        assert callable(tts_router.stream) and callable(tts_gemini.synthesize)
        assert hasattr(tts_kokoro, "KOKORO")
        for rel in ("app/era_live/claude_voice.py", "app/era_live/local_fallback.py"):
            assert (ROOT / rel).is_file(), rel


# ── the runner ──────────────────────────────────────────────────────────────

THREE = pw.Pathway(
    "t",
    "pathway",
    (
        pw.Step("p1", frozenset({"r"}), "same"),
        pw.Step("p2", frozenset({"r"}), "lower", say="(backup)"),
        pw.Step("deterministic", frozenset(), "lower", say="(floor)"),
    ),
)


def _gate(error_type):
    if error_type == "BlockedAction":
        return ToolResult.fail(message="blocked", error_type="BlockedAction")
    return fail(error_type, "held")


class TestRunner:
    @pytest.mark.parametrize(
        "error_type", ["ConfirmationRequired", "CapabilityDenied", "Cancelled", "BlockedAction"]
    )
    def test_a_gate_is_never_followed_by_another_provider(self, error_type, logs):
        called = []
        res = pw.run(
            "t",
            {
                "p1": lambda: _gate(error_type),
                "p2": lambda: called.append("p2"),
                "deterministic": lambda: called.append("d"),
            },
            pathway=THREE,
        )
        assert not res.success and called == []
        assert steps(logs) == [("p1", "fault")]  # asserted on the event log

    def test_alternative_faults_move_on_and_lower_quality_is_disclosed(self, logs):
        res = pw.run("t", {"p1": down, "p2": lambda: ToolResult.ok("answer")}, pathway=THREE)
        assert res.success and res.message == "(backup) answer"
        prov = res.data["provenance"]
        assert prov["provider"] == "p2" and prov["fallback_from"] == "p1" and prov["disclosed"]
        assert steps(logs) == [("p1", "fault"), ("p2", "ok")]

    def test_the_first_provider_answering_says_nothing_extra(self):
        res = pw.run("t", {"p1": lambda: ToolResult.ok("answer")}, pathway=THREE)
        assert res.message == "answer" and res.data["provenance"]["disclosed"] is False

    def test_an_open_breaker_costs_no_call(self, logs):
        for _ in range(3):
            hl.record("p1", ff.make("Unavailable", "p1"))
        calls = []
        res = pw.run(
            "t",
            {"p1": lambda: calls.append(1), "p2": lambda: ToolResult.ok("b")},
            pathway=THREE,
        )
        assert calls == [] and res.data["provenance"]["fallback_from"] == "p1"
        assert steps(logs) == [("p1", "info"), ("p2", "ok")]

    def test_the_floor_answers_when_everything_else_fails(self):
        def boom():
            raise RuntimeError("boom")

        res = pw.run(
            "t",
            {"p1": down, "p2": boom, "deterministic": lambda: ToolResult.ok("honest")},
            pathway=THREE,
        )
        assert res.success and res.message == "(floor) honest"

    def test_cancellation_is_never_swallowed(self):
        def cancelled():
            raise CancelledTask("stop")

        with pytest.raises(CancelledTask):
            pw.run("t", {"p1": cancelled, "p2": lambda: ToolResult.ok("x")}, pathway=THREE)

    def test_an_inner_provenance_is_kept(self):
        inner = ToolResult.ok("a", data={"provenance": {"transport": "in_process"}})
        res = pw.run("t", {"p1": lambda: inner}, pathway=THREE)
        assert res.data["provenance"]["via"] == {"transport": "in_process"}


# ── review_change (sections 7 and 8) ────────────────────────────────────────

BOT = BotSpec(id="cody", role="Reviewer", description="d", prompt="p", capabilities=())
JOB = {
    "id": "abc12def",
    "state": "awaiting_approval",
    "request": "improve the spotify search",
    "plan": "add a retry",
    "checks": {"passed": True},
    "files": [
        {
            "path": "actions/spotify_control.py",
            "new_file": False,
            "diff": "--- a\n+++ b\n+retry()\n+ceiling = 3\n-old()\n",
        }
    ],
    "verification_tail": "41 passed in 3.2s",
}


def review_turn():
    return Turn(
        task="review the change",
        context={"ctx_id": None, "artifacts": [{"kind": "dev_job", "ref": "abc12def"}]},
    )


@pytest.fixture
def staged(monkeypatch):
    import crew.era_access as ea

    monkeypatch.setattr(ea, "read_development", lambda args: ToolResult.ok("job", data=JOB))
    prompts = []

    def claude(prompt, system):
        prompts.append(prompt)
        return "Looks safe to approve. One concern: the retry has no ceiling."

    monkeypatch.setattr(rp, "_claude_generate", claude)
    return prompts


def inproc(reply):
    from crew.runtime import InProcessTransport

    t = InProcessTransport(llm=lambda s, u, tier: reply)
    t.register("cody", BOT)
    return t


class TestReviewChange:
    def test_the_crew_answers_when_it_is_up(self, staged, logs):
        res, used = rp.dispatch(inproc({"final": "crew verdict"}), "cody", review_turn())
        assert res.success and res.message == "crew verdict" and staged == []
        assert getattr(used, "name", "") == "in_process"
        assert steps(logs, "review_change") == [("crew", "ok")]

    def test_crew_down_means_claude_reviews_it_and_says_so(self, staged, logs):
        crew = inproc({"__unavailable__": "no model"})
        res, used = rp.dispatch(crew, "cody", review_turn())
        assert res.success and res.message.startswith("The crew was unreachable")
        assert "no ceiling" in res.message and used.name == "review.llm.claude"
        assert "+ceiling = 3" in staged[0] and "41 passed" in staged[0]  # Claude saw the diff
        assert steps(logs, "review_change") == [("crew", "fault"), ("llm.claude", "ok")]

    def test_crew_and_claude_down_means_the_verifier_summary(self, staged, monkeypatch, logs):
        from core.llm.types import LlmError

        def claude_down(prompt, system):
            raise LlmError("transient", "claude unreachable")

        monkeypatch.setattr(rp, "_claude_generate", claude_down)
        res, _ = rp.dispatch(inproc({"__unavailable__": "no model"}), "cody", review_turn())
        assert res.success and "both unreachable" in res.message
        assert "spotify_control.py (+2 / -1)" in res.message
        assert "Every verification check passed" in res.message
        assert "Nothing is live until you approve it" in res.message
        order = [s for s, _ in steps(logs, "review_change")]
        assert order == ["crew", "llm.claude", "deterministic"]

    @pytest.mark.parametrize("error_type", ["ReviewPaused", "ConfirmationRequired"])
    def test_a_held_review_is_never_handed_to_claude(self, staged, error_type, logs):
        class Held:
            name = "in_process"

            def dispatch(self, bot, turn):
                return fail(error_type, "held for the owner")

        res, _ = rp.dispatch(Held(), "cody", review_turn())
        assert res.error_type == error_type and staged == []
        assert steps(logs, "review_change") == [("crew", "fault")]

    def test_both_crew_breakers_open_skip_the_crew_without_a_call(self, staged):
        calls = []

        class Hermes:
            name = "hermes_agent"
            _bots = {"cody": BOT}

            def dispatch(self, bot, turn):
                calls.append(bot)
                return fail("TransportDown", "refused")

        for comp in ("crew.hermes", "crew.inproc"):
            for _ in range(3):
                hl.record(comp, ff.make("Unavailable", comp))
        t0 = time.perf_counter()
        res, _ = rp.dispatch(Hermes(), "cody", review_turn())
        assert res.success and calls == [] and len(staged) == 1
        assert time.perf_counter() - t0 < 1.0

    def test_a_plain_crew_ask_never_takes_the_review_pathway(self, staged, logs):
        res, _ = rp.dispatch(inproc({"final": "hi"}), "cody", Turn(task="say hi"))
        assert res.message == "hi" and steps(logs) == [] and "provenance" not in res.data

    def test_era_fabric_0_is_todays_crew_ask(self, staged, monkeypatch):
        monkeypatch.setenv("ERA_FABRIC", "0")
        res, _ = rp.dispatch(inproc({"__unavailable__": "x"}), "cody", review_turn())
        assert res.error_type == "LLMUnavailable" and staged == []

    def test_the_real_claude_call_refuses_to_run_under_pytest(self):
        from core.llm.types import LlmError

        with pytest.raises(LlmError, match="disabled under tests"):
            rp._claude_generate("p", "s")

    def test_the_named_job_wins_over_an_implied_one(self):
        artifacts = [
            {"kind": "dev_job", "ref": "implied1", "implicit": True},
            {"kind": "file", "ref": "x.py"},
            {"kind": "dev_job", "ref": "named22"},
        ]
        assert rp.dev_job_of(Turn(task="t", context={"artifacts": artifacts})) == "named22"


# ── web_search (section 8: "web search empty") ──────────────────────────────


@pytest.fixture
def search(monkeypatch):
    import actions.web_search as ws

    calls = {"gemini": 0, "ddg": 0}

    def gemini(q):
        calls["gemini"] += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota")

    def ddg(q, max_results=6):
        calls["ddg"] += 1
        return [{"title": "T", "href": "https://example.com", "body": "snippet"}]

    monkeypatch.setattr(ws, "_gemini_search", gemini)
    monkeypatch.setattr(ws, "_ddg_search", ddg)
    return ws, calls


class TestWebSearch:
    def test_gemini_down_means_duckduckgo_silently(self, search, logs):
        ws, calls = search
        text = ws.web_search({"query": "q"})
        assert "Search failed" not in text and "came up empty" not in text
        assert calls == {"gemini": 1, "ddg": 1}
        assert steps(logs, "web_search") == [
            ("llm.gemini.search", "fault"),
            ("tool.web_search.ddgs", "ok"),
        ]

    def test_a_spent_quota_stops_being_paid_for(self, search):
        ws, calls = search
        for _ in range(4):
            ws.web_search({"query": "q"})
        assert hl.state("llm.gemini.search") == hl.OPEN
        assert calls["gemini"] == 3 and calls["ddg"] == 4  # the 4th skipped Gemini

    def test_nothing_anywhere_is_an_honest_empty_answer(self, search, monkeypatch):
        ws, _ = search
        monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=6: [])
        text = ws.web_search({"query": "obscure"})
        assert text.startswith("Search came up empty for: obscure")

    def test_era_fabric_0_keeps_the_direct_chain(self, search, monkeypatch, logs):
        ws, calls = search
        monkeypatch.setenv("ERA_FABRIC", "0")
        ws.web_search({"query": "q"})
        assert calls == {"gemini": 1, "ddg": 1} and steps(logs) == []
