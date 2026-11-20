"""Era Fabric F1: the health registry, its breakers, and what reads them.

Design: docs/era-enhancement/ERA_FABRIC_DESIGN.md sections 5.2 and 9. Fakes only: no
real gateway, no real model, no real key (the Hermes key below is made up).
Acceptance pinned here: with the Hermes gateway refused, a crew ask answers through
the in-process transport, and once the breaker is open it pays no Hermes time (< 1 s).
"""

from __future__ import annotations

import json
import time

import pytest

from core.fabric import events as ev
from core.fabric import faults as ff
from core.fabric import health as hl
from core.tool_result import ToolResult
from crew import fallback as fb
from crew.transport import BotSpec, Turn, fail

COMPONENT = "test.thing"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ZORO_TASK_LOG_DIR", raising=False)
    monkeypatch.delenv("ERA_FABRIC", raising=False)
    monkeypatch.setattr(ev, "_subscribers", [])
    hl.uninstall()
    hl.reset()
    return tmp_path / "logs"


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(hl, "_clock", c)
    return c


def _events(logs, prefix=""):
    files = list((logs / "fabric").glob("events-*.jsonl"))
    rows = [json.loads(x) for f in files for x in f.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["topic"].startswith(prefix)]


def down(detail="refused"):
    return ff.make("Unavailable", COMPONENT, detail)


# ── the breaker ─────────────────────────────────────────────────────────────


class TestBreaker:
    def test_an_unknown_component_is_closed_and_allowed(self):
        assert hl.state("never.seen") == hl.CLOSED and hl.allow("never.seen")

    def test_three_health_faults_in_the_window_open_it(self, clock, logs):
        assert hl.record(COMPONENT, down()) == hl.CLOSED
        assert hl.record(COMPONENT, down()) == hl.CLOSED
        assert hl.record(COMPONENT, down()) == hl.OPEN
        assert not hl.allow(COMPONENT)
        (opened,) = _events(logs, "health.")
        assert opened["topic"] == "health.open" and opened["component"] == COMPONENT
        assert opened["fault"]["kind"] == "Unavailable"

    def test_faults_older_than_the_window_do_not_count(self, clock):
        hl.record(COMPONENT, down())
        hl.record(COMPONENT, down())
        clock.t += hl.DEFAULT_POLICY["window_s"] + 1
        assert hl.record(COMPONENT, down()) == hl.CLOSED

    @pytest.mark.parametrize(
        "kind", ["Denied", "NeedsConfirmation", "Cancelled", "NotFound", "BadInput", "Transient"]
    )
    def test_answers_and_gates_never_trip_it(self, kind, clock):
        for _ in range(10):
            hl.record(COMPONENT, ff.make(kind, COMPONENT, "x"))
        assert hl.state(COMPONENT) == hl.CLOSED and hl.allow(COMPONENT)
        assert hl.snapshot()[0]["faults"] == 10  # still counted, just not as ill health

    def test_after_the_cool_down_exactly_one_probe_goes_through(self, clock, logs):
        for _ in range(3):
            hl.record(COMPONENT, down())
        clock.t += hl.DEFAULT_POLICY["cooldown_s"] - 1
        assert not hl.allow(COMPONENT)
        clock.t += 2
        assert hl.state(COMPONENT) == hl.HALF_OPEN  # reading it grants nothing
        assert hl.allow(COMPONENT) is True  # the probe
        assert hl.allow(COMPONENT) is False  # everyone else waits for it
        assert [e["topic"] for e in _events(logs, "health.")] == ["health.open", "health.half_open"]

    def test_a_successful_probe_closes_it(self, clock, logs):
        for _ in range(3):
            hl.record(COMPONENT, down())
        clock.t += 61
        assert hl.allow(COMPONENT)
        assert hl.record(COMPONENT) == hl.CLOSED
        assert hl.allow(COMPONENT) and hl.allow(COMPONENT)
        assert _events(logs, "health.")[-1]["topic"] == "health.closed"

    def test_a_failed_probe_reopens_it_at_once(self, clock):
        for _ in range(3):
            hl.record(COMPONENT, down())
        clock.t += 61
        assert hl.allow(COMPONENT)
        assert hl.record(COMPONENT, down()) == hl.OPEN  # one fault, not three
        assert not hl.allow(COMPONENT)

    def test_a_probe_that_never_reports_is_retried_after_another_cool_down(self, clock):
        for _ in range(3):
            hl.record(COMPONENT, down())
        clock.t += 61
        assert hl.allow(COMPONENT)
        clock.t += 61
        assert hl.allow(COMPONENT)

    def test_exhausted_cools_down_for_the_quotas_own_reset_time(self, clock):
        quota = ff.make("Exhausted", COMPONENT, "429")
        for _ in range(3):
            hl.record(COMPONENT, quota, reset_s=600)
        clock.t += 300
        assert not hl.allow(COMPONENT)
        clock.t += 301
        assert hl.allow(COMPONENT)

    def test_omniroute_gets_the_longer_cold_start_cool_down(self, clock):
        for _ in range(3):
            hl.record("gateway.omniroute", ff.make("Unavailable", "gateway.omniroute"))
        clock.t += 90
        assert not hl.allow("gateway.omniroute")
        clock.t += 31
        assert hl.allow("gateway.omniroute")

    def test_era_fabric_0_is_todays_behaviour(self, monkeypatch, clock):
        monkeypatch.setenv("ERA_FABRIC", "0")
        for _ in range(5):
            hl.record(COMPONENT, down())
        assert hl.allow(COMPONENT) and hl.snapshot() == []

    def test_snapshot_lists_the_sick_first_and_is_json_safe(self, clock):
        hl.record("a.fine")
        for _ in range(3):
            hl.record("crew.hermes_agent", down())  # aliased to crew.hermes
        rows = hl.snapshot()
        assert [r["component"] for r in rows] == ["crew.hermes", "a.fine"]
        assert rows[0]["state"] == hl.OPEN and rows[0]["retry_in_s"] == 60
        json.dumps(rows)


# ── fed by the F0 events ────────────────────────────────────────────────────


class TestEventFeed:
    def test_install_is_idempotent(self):
        assert hl.install() and hl.install()
        assert len(ev._subscribers) == 3

    def test_tool_results_feed_the_registry(self, clock):
        from core import task_trace

        hl.install()
        for _ in range(3):
            task_trace._event({"tool": "web_search", "ok": False, "error_type": "TimeoutError"})
        assert hl.state("tool.web_search") == hl.OPEN
        task_trace._event({"tool": "web_search", "ok": True})
        assert hl.state("tool.web_search") == hl.CLOSED

    def test_crew_events_use_the_registry_names(self, clock):
        hl.install()
        fault = ff.make("Unavailable", "crew.hermes_agent", "refused")
        for _ in range(3):
            ev.emit("crew.result", component="crew.hermes_agent", outcome="fault", fault=fault)
        ev.emit("crew.result", component="crew.in_process", outcome="ok")
        assert hl.state("crew.hermes") == hl.OPEN and hl.state("crew.inproc") == hl.CLOSED

    def test_health_events_do_not_feed_back_into_the_registry(self, clock, logs):
        hl.install()
        for _ in range(3):
            hl.record(COMPONENT, down())
        assert [r["component"] for r in hl.snapshot()] == [COMPONENT]


# ── existing signals that now report to it ──────────────────────────────────


class TestSignals:
    def test_live_upstream_errors_and_replies(self, clock):
        from core.live_health import LiveHealth

        lh = LiveHealth()
        for _ in range(3):
            lh.note_upstream_error()
        assert hl.state("live.gemini") == hl.OPEN
        lh.note_reply()
        assert hl.state("live.gemini") == hl.CLOSED

    def test_a_quota_latch_is_an_exhausted_fault(self, clock):
        from core.llm import quota_failover

        quota_failover._latch("synthetic-key", "gemini-test", 30)
        try:
            (row,) = hl.snapshot()
            assert row["component"] == "llm.gemini" and row["last_fault_kind"] == "Exhausted"
        finally:
            quota_failover._LATCH.clear()

    def test_an_open_omniroute_breaker_is_skipped_without_a_call(self, clock, monkeypatch):
        from core.llm.adapters import omniroute
        from core.llm.types import LlmMessage, LlmRequest

        monkeypatch.setattr(omniroute, "enabled", lambda: True)
        calls = []

        def boom(request, **kw):
            calls.append(request)
            raise omniroute.LlmError("transient", "OmniRoute unreachable", provider="omniroute")

        monkeypatch.setattr(omniroute, "generate", boom)
        req = LlmRequest(alias="era.reason", messages=(LlmMessage.text("user", "hi"),))
        for _ in range(3):
            assert omniroute.try_first(req) is None
        assert hl.state("gateway.omniroute") == hl.OPEN
        assert omniroute.try_first(req) is None
        assert len(calls) == 3  # the fourth went straight to the fallback


# ── capability_check reads it ───────────────────────────────────────────────


class TestSelfModel:
    def test_a_healthy_answer_is_unchanged(self):
        from core import capability_index as ci

        assert hl.self_model_note() == ""
        assert ci.summary() == ci._summary()
        assert ci.check("play music on spotify") == ci._check("play music on spotify")

    def test_what_is_down_is_part_of_the_answer(self, clock):
        from core import capability_index as ci

        for _ in range(3):
            hl.record("crew.hermes", ff.make("Unavailable", "crew.hermes", "refused"))
        text = ci.check("ask the crew to review my change")
        assert "LIVE HEALTH" in text and "Hermes gateway is DOWN" in text
        assert "local loop" in text
        assert ci.summary().count("LIVE HEALTH") == 1
        assert ci.check("").count("LIVE HEALTH") == 1  # check("") is summary(): once

    def test_the_hud_diagnostics_carry_the_breakers(self, clock):
        from app.hud import status

        for _ in range(3):
            hl.record("crew.hermes", down())
        (row,) = status._fabric_health()
        assert row["component"] == "crew.hermes" and row["state"] == hl.OPEN


# ── crew transport fallback (the section 9 acceptance) ──────────────────────

BOT = BotSpec(id="tester", role="Tester", description="d", prompt="p", capabilities=())
KEY_ENTRY = "hermes_era_one_api_key"


@pytest.fixture
def hermes(tmp_path, monkeypatch, clock):
    """A Hermes transport whose gateway refuses connections, and an in-process
    fallback whose model answers instantly."""
    from crew import hermes_transport as ht
    from crew.runtime import InProcessTransport, reset_transport

    keyfile = tmp_path / "api_keys.json"
    keyfile.write_text(json.dumps({KEY_ENTRY: "synthetic-key-0000"}), encoding="utf-8")
    calls = []

    def refused(method, url, headers, body, timeout):
        calls.append(url)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(ht, "PROBE_RETRY_PAUSE_S", 0.0)
    t = ht.HermesAgentTransport(config_path=keyfile, http=refused)
    t.register("tester", BOT)

    def build(primary):
        inproc = InProcessTransport(llm=lambda s, u, tier: {"final": "local answer"})
        for bot_id, spec in primary._bots.items():
            inproc.register(bot_id, spec)
        return inproc

    monkeypatch.setattr(fb, "_build_inproc", build)
    reset_transport(t, {"tester": BOT})
    yield t, calls
    reset_transport(None)


def ask():
    from crew.runtime import run_tool

    return run_tool({"action": "ask", "bot": "tester", "task": "say hi"})


class TestCrewFallback:
    def test_a_refused_gateway_answers_through_the_local_loop(self, hermes, logs):
        res = ask()
        assert res.success, res.message
        assert "local answer" in res.message and fb.NOTE in res.message
        prov = res.data["provenance"]
        assert prov == {
            "transport": "in_process",
            "fallback_from": "hermes_agent",
            "reason": "refused:TransportDown",
        }
        crew = [(e["topic"], e["component"], e["outcome"]) for e in _events(logs, "crew.")]
        assert crew == [
            ("crew.asked", "crew.hermes_agent", "progress"),
            ("crew.step", "crew.hermes_agent", "fault"),
            ("crew.result", "crew.in_process", "ok"),
        ]

    def test_with_the_breaker_open_hermes_is_not_called_and_it_is_fast(self, hermes):
        _, calls = hermes
        for _ in range(3):
            assert ask().success
        assert hl.state("crew.hermes") == hl.OPEN
        before = len(calls)
        t0 = time.perf_counter()
        res = ask()
        elapsed = time.perf_counter() - t0
        assert res.success and res.data["provenance"]["reason"] == "breaker_open"
        assert len(calls) == before  # not one request to the dead gateway
        assert elapsed < 1.0, f"took {elapsed:.2f}s"

    def test_the_fallback_runs_the_same_bot_specs(self, hermes):
        t, _ = hermes
        assert fb.inproc_for(t)._bots["tester"] is BOT

    def test_era_fabric_0_keeps_todays_refusal(self, hermes, monkeypatch):
        monkeypatch.setenv("ERA_FABRIC", "0")
        res = ask()
        assert not res.success and res.error_type == "TransportDown"


class FakeHermes:
    name = "hermes_agent"
    _bots = {"tester": BOT}

    def __init__(self, result):
        self.result, self.calls = result, 0

    def dispatch(self, bot, turn):
        self.calls += 1
        return self.result


class TestNeverAroundAGate:
    @pytest.mark.parametrize(
        "error_type", ["ConfirmationRequired", "CapabilityDenied", "Cancelled", "ReviewPaused"]
    )
    def test_a_terminal_fault_is_never_rerun_elsewhere(self, error_type, monkeypatch):
        built = []
        monkeypatch.setattr(fb, "_build_inproc", lambda p: built.append(p))
        primary = FakeHermes(fail(error_type, "held"))
        result, used = fb.dispatch(primary, "tester", Turn(task="t"))
        assert used is primary and result.error_type == error_type and built == []

    def test_a_mid_turn_model_failure_is_not_rerun(self, monkeypatch):
        built = []
        monkeypatch.setattr(fb, "_build_inproc", lambda p: built.append(p))
        primary = FakeHermes(fail("LLMUnavailable", "provider login expired"))
        result, used = fb.dispatch(primary, "tester", Turn(task="t"))
        assert used is primary and result.error_type == "LLMUnavailable" and built == []

    def test_empty_bad_output_hands_the_untouched_task_to_the_local_loop(self, monkeypatch):
        local = FakeHermes(ToolResult.ok(message="local answer"))
        local.name = "in_process"
        fb.reset()
        monkeypatch.setattr(fb, "_build_inproc", lambda _primary: local)
        primary = FakeHermes(
            fail(
                "BadModelOutput",
                "cody gave no usable reply",
                safe_local_handoff=True,
                capabilities_used=[],
            )
        )

        result, used = fb.dispatch(primary, "tester", Turn(task="three bugs"))

        assert used is local and result.success
        assert "local answer" in result.message and fb.EMPTY_NOTE in result.message
        assert result.data["provenance"]["reason"] == "empty:BadModelOutput"

    def test_bad_output_after_a_side_effect_is_never_replayed(self, monkeypatch):
        built = []
        monkeypatch.setattr(fb, "_build_inproc", lambda p: built.append(p))
        primary = FakeHermes(
            fail(
                "BadModelOutput",
                "cody stopped after writing",
                safe_local_handoff=False,
                capabilities_used=["vault.append"],
            )
        )
        result, used = fb.dispatch(primary, "tester", Turn(task="t"))
        assert used is primary and result.error_type == "BadModelOutput" and built == []

    def test_a_hermes_stall_after_reads_hands_the_task_to_the_local_loop(self, monkeypatch):
        """PF-051: live 2026-11-19 Cody's 3rd Hermes call hung 90 s twice; the turn died."""
        local = FakeHermes(ToolResult.ok(message="local answer"))
        local.name = "in_process"
        fb.reset()
        monkeypatch.setattr(fb, "_build_inproc", lambda _primary: local)
        primary = FakeHermes(
            fail(
                "LLMUnavailable",
                "cody: the Hermes gateway failed (TimeoutError)",
                safe_local_handoff=True,
                capabilities_used=["source.read", "era.read"],
            )
        )
        result, used = fb.dispatch(primary, "tester", Turn(task="fix the log line"))
        assert used is local and result.success
        assert result.data["provenance"]["reason"] == "unavailable:LLMUnavailable"
        assert "stopped answering before any side effect" in result.message

    def test_a_hermes_stall_after_a_side_effect_is_never_replayed(self, monkeypatch):
        built = []
        monkeypatch.setattr(fb, "_build_inproc", lambda p: built.append(p))
        primary = FakeHermes(
            fail(
                "LLMUnavailable",
                "cody: the Hermes gateway failed (TimeoutError)",
                safe_local_handoff=False,
                capabilities_used=["source.stage"],
            )
        )
        result, used = fb.dispatch(primary, "tester", Turn(task="t"))
        assert used is primary and result.error_type == "LLMUnavailable" and built == []

    def test_a_transport_that_is_not_hermes_is_dispatched_untouched(self, monkeypatch):
        class Plain(FakeHermes):
            name = "in_process"

        primary = Plain(fail("TransportDown", "down"))
        result, used = fb.dispatch(primary, "tester", Turn(task="t"))
        assert used is primary and primary.calls == 1 and hl.snapshot() == []
