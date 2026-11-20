"""Era Fabric F0: one fault taxonomy, a bulkhead decorator, a typed event log.

Design: docs/era-enhancement/ERA_FABRIC_DESIGN.md sections 4, 5.1 and 6. F0 changes
no behaviour: the boundary wraps nothing live yet, and the bus only records. What is
pinned here is the contract the later phases build on:

* every crew error type maps to a fault kind; gates (Denied, NeedsConfirmation,
  Cancelled) are terminal and can never offer an alternative;
* an unclassified failure is Internal, never success;
* the boundary lets no exception escape (except interpreter exits and an asyncio
  task's own cancellation), has no default fail_mode, and logs Internal faults
  with frames for self_heal;
* events are a closed set, persisted before fan-out, redacted, isolated per
  subscriber, safe across threads, and switched off by ERA_FABRIC=0;
* the live seams (tool trace, crew ask, development) emit their events.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from core.fabric import boundary as fb
from core.fabric import events as ev
from core.fabric import faults as ff
from core.tool_result import BENIGN_ERROR_TYPES, ToolResult


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ZORO_TASK_LOG_DIR", raising=False)
    monkeypatch.delenv("ERA_FABRIC", raising=False)
    monkeypatch.setattr(ev, "_subscribers", [])
    return tmp_path / "logs"


def _lines(logs):
    files = list((logs / "fabric").glob("events-*.jsonl"))
    if not files:
        return []
    return [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------------- faults


class TestFaults:
    def test_every_crew_error_type_is_mapped(self):
        from crew.transport import ERROR_TYPES

        assert set(ff.CREW_MAP) == set(ERROR_TYPES)
        assert set(ff.CREW_MAP.values()) <= set(ff.KINDS)

    @pytest.mark.parametrize("kind", sorted(ff.TERMINAL))
    def test_a_gate_never_offers_an_alternative(self, kind):
        fault = ff.make(kind, "x", alternative=True)
        assert fault["alternative"] is False and fault["retryable"] is False
        assert not ff.may_try_alternative(fault)

    @pytest.mark.parametrize("name", sorted(BENIGN_ERROR_TYPES))
    def test_every_benign_gate_type_is_terminal(self, name):
        assert ff.is_terminal(ff.from_error_type(name, "x"))

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("CapabilityDenied", "Denied"),
            ("SpeakerGateRefused", "Denied"),
            ("ReviewPaused", "Denied"),
            ("ConfirmationRequired", "NeedsConfirmation"),
            ("ConfirmationBusy", "NeedsConfirmation"),
            ("TransportDown", "Unavailable"),
            ("StepLimit", "Timeout"),
            ("QuotaExceeded", "Exhausted"),
            ("RateLimitError", "Exhausted"),
            ("PathMissing", "NotFound"),
            ("NoResults", "EmptyResult"),
            ("InvalidArgs", "BadInput"),
            ("VaultLockUnavailable", "Transient"),
        ],
    )
    def test_error_type_names(self, name, kind):
        assert ff.kind_for_error_type(name) == kind

    def test_an_unknown_failure_is_internal_never_success(self):
        assert ff.kind_for_error_type("Weird") is None
        assert ff.from_error_type("Weird", "x")["kind"] == "Internal"
        assert ff.classify(ToolResult.fail("boom"), "x")["kind"] == "Internal"

    @pytest.mark.parametrize(
        ("exc", "kind"),
        [
            (TimeoutError(), "Timeout"),
            (ConnectionRefusedError(), "Unavailable"),
            (ConnectionResetError(), "Transient"),
            (FileNotFoundError(), "NotFound"),
            (ValueError("bad"), "BadInput"),
            (RuntimeError("bug"), "Internal"),
        ],
    )
    def test_exceptions_classify_by_class(self, exc, kind):
        assert ff.from_exception(exc, "x")["kind"] == kind

    def test_cancelled_task_is_cancelled(self):
        from core.cancel_registry import CancelledTask

        assert ff.from_exception(CancelledTask("stop"), "x")["kind"] == "Cancelled"

    def test_classify_success_none_and_existing_fault(self):
        assert ff.classify(ToolResult.ok("fine"), "x") is None
        assert ff.classify(None, "x")["kind"] == "EmptyResult"
        own = ff.make("EmptyResult", "tool.web_search", "0 hits")
        res = ToolResult.ok("nothing", data={"fault": own})
        assert ff.classify(res, "x") == own  # structure wins over success=True

    def test_attach_never_marks_a_success(self):
        res = ff.attach(ToolResult.ok("fine"), "x")
        assert "fault" not in res.data
        res = ff.attach(ToolResult.fail("no", error_type="CapabilityDenied"), "crew.x")
        assert res.data["fault"]["kind"] == "Denied"

    def test_detail_is_redacted(self):
        fault = ff.make("Internal", "x", "failed with key AIza" + "A" * 30)
        assert "AIza" not in fault["detail"] and "<redacted>" in fault["detail"]


# ------------------------------------------------------------------------- events


class TestEvents:
    def test_persisted_before_fan_out(self, logs):
        seen = []
        ev.subscribe("tool.*", lambda e: seen.append(len(_lines(logs))))
        ev.emit("tool.result", component="tool.x", outcome="ok", summary="done")
        assert seen == [1]  # the subscriber already finds its own event on disk

    @pytest.mark.parametrize(("topic", "outcome"), [("tool.oops", "ok"), ("tool.result", "meh")])
    def test_closed_set_refuses_unknowns(self, logs, topic, outcome):
        before = ev.refused
        assert ev.emit(topic, component="x", outcome=outcome) is None
        assert ev.refused == before + 1 and _lines(logs) == []

    def test_a_throwing_subscriber_is_isolated(self, logs):
        got = []
        ev.subscribe("*", lambda e: 1 / 0)
        ev.subscribe("*", lambda e: got.append(e.topic))
        assert ev.emit("crew.result", component="crew.x", outcome="ok") is not None
        assert got == ["crew.result"]

    def test_unsubscribe(self, logs):
        got = []
        off = ev.subscribe("*", got.append)
        off()
        ev.emit("task.done", component="x", outcome="ok")
        assert got == []

    def test_era_fabric_0_turns_it_off(self, logs, monkeypatch):
        monkeypatch.setenv("ERA_FABRIC", "0")
        assert ev.emit("task.done", component="x", outcome="ok") is None
        assert _lines(logs) == []

    def test_ctx_binding_and_read_filters(self, logs):
        with ev.bind_ctx("ctx_a"):
            ev.emit("crew.asked", component="crew.x", outcome="progress")
        ev.emit("crew.result", component="crew.x", outcome="ok", ctx_id="ctx_b")
        assert [e["topic"] for e in ev.read(ctx_id="ctx_a")] == ["crew.asked"]
        assert [e["ctx_id"] for e in ev.read(topic="crew.*")] == ["ctx_a", "ctx_b"]
        assert ev.current_ctx() is None

    def test_summary_and_fault_detail_are_redacted(self, logs):
        from pathlib import Path

        secret = "sk-" + "a" * 24
        fault = {"kind": "Internal", "detail": f"token={secret}"}
        ev.emit("tool.result", component="x", outcome="fault",
                summary=f"{Path.home()}\\x used {secret}", fault=fault)  # fmt: skip
        raw = (next((logs / "fabric").glob("events-*.jsonl"))).read_text(encoding="utf-8")
        assert secret not in raw and str(Path.home()).replace("\\", "\\\\") not in raw

    def test_threads_never_interleave_lines(self, logs):
        def burst():
            for _ in range(50):
                ev.emit("tool.result", component="tool.t", outcome="ok", summary="x" * 150)

        threads = [threading.Thread(target=burst) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(_lines(logs)) == 400


# ----------------------------------------------------------------------- boundary


class TestBoundary:
    def test_fail_mode_has_no_default(self):
        with pytest.raises(TypeError):
            fb.boundary("tool.x")  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            fb.boundary("tool.x", fail_mode="maybe")

    def test_long_needs_async(self):
        with pytest.raises(TypeError):
            fb.boundary("tool.x", fail_mode="open", long=True)(lambda: 1)

    def test_no_exception_escapes_and_internal_is_logged_for_self_heal(self, logs, monkeypatch):
        crashes = []
        monkeypatch.setattr("core.logger.log_crash", lambda *a, **k: crashes.append(k))

        @fb.boundary("tool.x", fail_mode="open")
        def bad():
            raise RuntimeError("our bug")

        res = bad()
        assert res.success is False and res.data["fault"]["kind"] == "Internal"
        assert crashes and crashes[0]["event"] == "fabric.boundary.internal"
        event = _lines(logs)[-1]
        assert event["topic"] == "tool.result" and event["outcome"] == "fault"
        assert isinstance(event["provenance"]["duration_ms"], int)

    def test_a_returned_gate_stays_terminal(self, logs):
        @fb.boundary("crew.hermes", fail_mode="open")
        def denied():
            return ToolResult.fail("no", error_type="CapabilityDenied")

        res = denied()
        assert res.data["fault"]["kind"] == "Denied" and not res.data["fault"]["alternative"]
        assert _lines(logs)[-1]["topic"] == "crew.result"

    def test_fail_closed_never_offers_an_alternative(self, logs):
        @fb.boundary("vault.write", fail_mode="closed")
        def down():
            raise ConnectionRefusedError("lock host")

        fault = down().data["fault"]
        assert fault["kind"] == "Unavailable" and fault["alternative"] is False

    def test_success_passes_through_untouched(self, logs):
        @fb.boundary("tool.x", fail_mode="open")
        def fine():
            return ToolResult.ok("done", data={"n": 1})

        res = fine()
        assert res.success and res.data == {"n": 1} and res.duration_ms is not None

    def test_interpreter_exits_are_not_swallowed(self):
        @fb.boundary("tool.x", fail_mode="open")
        def stop():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            stop()

    def test_disabled_is_a_plain_call(self, monkeypatch):
        monkeypatch.setenv("ERA_FABRIC", "0")

        @fb.boundary("tool.x", fail_mode="open")
        def bad():
            raise RuntimeError("today's behaviour")

        with pytest.raises(RuntimeError):
            bad()

    def test_async_timeout_is_a_timeout_fault(self, logs):
        @fb.boundary("crew.hermes", fail_mode="open", timeout_s=0.05)
        async def slow():
            await asyncio.sleep(5)

        res = asyncio.run(slow())
        assert res.data["fault"]["kind"] == "Timeout" and res.data["fault"]["alternative"]

    def test_long_acks_now_and_reports_by_event(self, logs):
        release = None

        @fb.boundary("crew.hermes", fail_mode="open", long=True)
        async def ask():
            await release.wait()
            return ToolResult.ok("reviewed: one concern")

        async def main():
            nonlocal release
            release = asyncio.Event()
            with ev.bind_ctx("ctx_long"):
                ack = await ask()
            assert ack.success and ack.data == {"ctx_id": "ctx_long", "background": True}
            assert [e["topic"] for e in _lines(logs)] == ["crew.asked"]
            release.set()
            await asyncio.gather(*fb._background)

        asyncio.run(main())
        last = _lines(logs)[-1]
        assert last["topic"] == "crew.result" and last["ctx_id"] == "ctx_long"
        assert last["summary"] == "reviewed: one concern"

    def test_task_cancellation_is_recorded_then_reraised(self, logs):
        @fb.boundary("tool.x", fail_mode="open")
        async def forever():
            await asyncio.sleep(5)

        async def main():
            task = asyncio.ensure_future(forever())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())
        assert _lines(logs)[-1]["fault"]["kind"] == "Cancelled"


# -------------------------------------------------------------------- live seams


class TestSeams:
    def test_every_era_tool_call_is_a_tool_result_event(self, logs, monkeypatch):
        import core.task_lessons as tl
        from core import task_trace

        monkeypatch.setattr(tl, "observe_async", lambda *a, **k: None)
        task_trace.trace("tool", app="era", requested="web_search", ok=False,
                         error_type="TimeoutError", result="timed out\nmore", duration_ms=9)  # fmt: skip
        task_trace.trace("tool", app="zoro", requested="x", ok=True)  # not Era: no event
        (event,) = _lines(logs)
        assert event["component"] == "tool.web_search" and event["outcome"] == "fault"
        assert event["fault"]["kind"] == "Timeout" and event["summary"] == "timed out"

    def test_a_crew_ask_emits_asked_then_result_on_its_context(self, logs):
        from crew.registry import load_specs
        from crew.runtime import InProcessTransport, reset_transport, run_tool
        from tools.crew_testkit import final

        t = InProcessTransport(llm=lambda s, u, tier: final("hello"))
        specs = load_specs().specs
        t.register("cody", specs["cody"])
        reset_transport(t, {"cody": specs["cody"]})
        try:
            res = run_tool({"action": "ask", "bot": "cody", "task": "say hi"})
        finally:
            reset_transport(None)
        crew = [e for e in _lines(logs) if e["topic"].startswith("crew.")]
        assert [e["topic"] for e in crew] == ["crew.asked", "crew.result"]
        assert {e["ctx_id"] for e in crew} == {res.data["ctx_id"]}
        assert crew[1]["outcome"] == "ok" and crew[1]["provenance"]["bot"] == "cody"

    @pytest.mark.parametrize(
        ("state", "topic", "kind"),
        [
            ("awaiting_approval", "dev.verified", None),
            ("failed", "dev.failed", "BadOutput"),
            ("recovery_required", "dev.failed", "Internal"),
            ("cancelled", "dev.cancelled", None),
        ],
    )
    def test_development_end_states(self, logs, state, topic, kind):
        from core import development

        job = {"id": "e46dc718133843a3", "state": "planning", "request": "x"}
        job["ctx_id"] = development._open_context(job)
        job.update(state=state, error="verifier exit 1")
        development._record_context(job)
        dev = [e for e in _lines(logs) if e["topic"].startswith("dev.")]
        assert [e["topic"] for e in dev] == ["dev.proposed", topic]
        assert (dev[1]["fault"] or {}).get("kind") == kind
        assert {e["ctx_id"] for e in dev} == {job["ctx_id"]}
