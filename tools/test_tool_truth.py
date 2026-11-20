"""PF-022: Era must know what her tools REALLY did.

Live 2026-11-15: "type instagram.com in Brave" became a 49 s Netflix search,
because a Netflix flag with no expiry hijacked every later keypress; the
router's own reply ("Netflix state: unknown") renewed the flag; the model got
that reply under the name of the tool it had asked for; slow calls were detached
with no outcome event; and Era had no way to read back what had happened.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.era_live.tool_dispatch import ToolDispatchMixin
from core import intent_memory, self_healing_router
from core.tool_result import ToolResult

NOW = 1_800_000_000.0


def netflix_row(age_s, result="Playing Wild Robot", error=None, now=NOW):
    return {"time": now - age_s, "media_app": "netflix", "result": result, "error": error}


def state(*rows, flag="netflix"):
    return {"last_media_app": flag, "history": list(rows)}


class TestNetflixContextExpires:
    def test_a_bare_flag_keeps_its_old_meaning(self):
        assert intent_memory.netflix_context_fresh({"last_media_app": "netflix"}, now=NOW)

    def test_another_app_is_never_netflix(self):
        assert not intent_memory.netflix_context_fresh(
            state(netflix_row(5), flag="spotify"), now=NOW
        )

    def test_recent_real_use_is_fresh(self):
        assert intent_memory.netflix_context_fresh(state(netflix_row(60)), now=NOW)

    def test_old_use_expires(self):
        age = intent_memory.NETFLIX_CONTEXT_TTL_S + 60
        assert not intent_memory.netflix_context_fresh(state(netflix_row(age)), now=NOW)

    def test_the_hijacks_own_unknown_reply_does_not_renew_the_flag(self):
        old = netflix_row(intent_memory.NETFLIX_CONTEXT_TTL_S + 600)
        own_reply = netflix_row(5, result="Netflix state: unknown")
        assert not intent_memory.netflix_context_fresh(state(old, own_reply), now=NOW)

    def test_a_failed_netflix_call_is_not_evidence(self):
        failed = netflix_row(5, result="", error="could not identify any matching results")
        assert not intent_memory.netflix_context_fresh(state(failed), now=NOW)


class TestRouterLeavesOtherAppsAlone:
    @pytest.fixture(autouse=True)
    def no_scroll_context(self, monkeypatch):
        import actions.netflix_control as nc

        monkeypatch.setattr(self_healing_router, "get_scroll_context", lambda: None)
        # A separate guard holds a bare Enter on the Netflix page for 120 s after
        # a REAL Netflix failure (PF-023, test_browser_media_routing). That global leaks from
        # earlier tests in a full run; these cases are about the stale flag.
        monkeypatch.setattr(nc, "_LAST_NETFLIX_FAILURE_AT", 0.0)
        monkeypatch.setattr(nc, "_VISION_COOLDOWN_UNTIL", 0.0)

    @pytest.mark.parametrize(
        "params",
        [
            {"action": "type", "text": "https://www.instagram.com"},
            {"action": "press", "key": "enter"},
            {"action": "hotkey", "keys": "ctrl+l"},
        ],
    )
    def test_stale_netflix_does_not_hijack_a_keypress(self, monkeypatch, params):
        stale = state(
            netflix_row(intent_memory.NETFLIX_CONTEXT_TTL_S + 3600, now=time.time()),
            flag="netflix",
        )
        monkeypatch.setattr(self_healing_router, "load_state", lambda: stale)
        tool, routed = self_healing_router.route_tool_call(
            "computer_control", dict(params), "Open Instagram on Brave browser."
        )
        assert tool == "computer_control"
        assert routed == params

    def test_live_netflix_still_gets_the_guard(self, monkeypatch):
        # The guard exists for a reason: a generic key on a playing Netflix page.
        fresh = state(netflix_row(30, now=time.time()))
        monkeypatch.setattr(self_healing_router, "load_state", lambda: fresh)
        tool, _ = self_healing_router.route_tool_call(
            "computer_control", {"action": "press", "key": "space"}, "pause it"
        )
        assert tool == "netflix_control"


def test_the_model_can_say_key_for_a_keypress(monkeypatch):
    from actions import computer_control as cc

    pressed = []
    monkeypatch.setattr(cc, "_press", lambda key: pressed.append(key) or "ok")
    monkeypatch.setattr(cc, "resolve_keys", lambda params: params.get("key") or params.get("keys"))
    assert cc.computer_control({"action": "key", "key": "enter"}) == "ok"
    assert pressed == ["enter"]


class TestRoutingNoticeReachesTheModel:
    def fc(self, name, args):
        return SimpleNamespace(name=name, args=args, id="1")

    def host(self, routed):
        return SimpleNamespace(_learn_routed=routed)

    def test_a_swapped_tool_is_named_and_the_outcome_is_untouched(self):
        result = ToolResult.ok(message="Netflix state: unknown")
        out = ToolDispatchMixin._attach_route_notice(
            self.host(("netflix_control", {"action": "status"})),
            result,
            "computer_control",
            self.fc("computer_control", {"action": "press", "key": "enter"}),
        )
        assert out.message.startswith("ROUTING NOTE")
        assert "computer_control(press)" in out.message and "netflix_control(status)" in out.message
        assert "does not prove the requested action happened" in out.message
        assert out.message.endswith("Netflix state: unknown")
        assert out.data["routed"] == {
            "asked": "computer_control(press)",
            "ran": "netflix_control(status)",
        }
        assert out.success is True and out.error_type == result.error_type

    def test_a_failure_stays_a_failure(self):
        failed = ToolResult.fail("no match", error_type="ActionError")
        out = ToolDispatchMixin._attach_route_notice(
            self.host(("netflix_control", {"action": "search"})),
            failed,
            "computer_control",
            self.fc("computer_control", {"action": "type"}),
        )
        assert out.success is False and out.error_type == "ActionError"

    def test_no_notice_when_the_same_tool_ran(self):
        result = ToolResult.ok(message="done")
        out = ToolDispatchMixin._attach_route_notice(
            self.host(("open_app", {"app_name": "brave"})),
            result,
            "open_app",
            self.fc("open_app", {"app_name": "brave"}),
        )
        assert out is result


def write_trace(tmp_path, rows):
    path = tmp_path / f"task_trace-{datetime.now():%Y%m%d}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def call(**kw):
    base = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "kind": "tool",
        "app": "era",
        "user_text": "open it",
        "requested": "open_app",
        "requested_args": {"app_name": "brave"},
        "tool": "open_app",
        "args": {"app_name": "brave"},
        "ok": True,
        "verification": "NOT_VERIFIED",
        "result": "brave: process running",
    }
    base.update(kw)
    return base


class TestEraCanReadBackWhatHappened:
    @pytest.fixture(autouse=True)
    def trace_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path))
        monkeypatch.delenv("ZORO_TASK_LOG_DIR", raising=False)
        self.tmp = tmp_path

    def test_a_swapped_call_names_both_tools(self):
        from core.task_trace import render_recent_calls

        text = render_recent_calls(
            [
                call(
                    requested="computer_control",
                    requested_args={"action": "press", "key": "enter"},
                    tool="netflix_control",
                    args={"action": "status"},
                    result="Netflix state: unknown",
                )
            ]
        )
        assert "computer_control(press) -> Era actually ran netflix_control(status)" in text

    def test_outcomes_are_never_upgraded(self):
        from core.task_trace import outcome_word

        assert outcome_word(call(ok=False, error_type="ActionError")) == "FAILED (ActionError)"
        assert outcome_word(call(verification="VERIFIED")) == "CONFIRMED"
        assert outcome_word(call()) == "returned but NOT CONFIRMED"
        assert "read-only" in outcome_word(call(args={"action": "status"}))

    def test_recent_calls_come_back_oldest_first_and_capped(self):
        write_trace(self.tmp, [call(result=f"r{i}") for i in range(6)] + [{"kind": "recovery"}])
        from core.task_trace import recent_tool_calls

        got = recent_tool_calls(3)
        assert [g["result"] for g in got] == ["r3", "r4", "r5"]

    def test_old_calls_age_out(self):
        write_trace(self.tmp, [call(ts="2020-01-01T00:00:00.000")])
        from core.task_trace import recent_tool_calls

        assert recent_tool_calls(5) == []

    def test_era_reads_it_through_protocol_one(self):
        from actions import protocol_one as p1

        write_trace(
            self.tmp, [call(ok=False, error_type="ActionError", result="Brave has no debugging")]
        )
        text = p1.read_recent("5")
        assert "NOT CONFIRMED and FAILED mean the request did NOT demonstrably happen" in text
        assert "FAILED (ActionError)" in text and "Brave has no debugging" in text

    def test_a_bad_limit_falls_back_instead_of_raising(self):
        from actions import protocol_one as p1

        write_trace(self.tmp, [call()])
        assert "brave: process running" in p1.read_recent("not-a-number")

    def test_an_empty_record_says_so_instead_of_inventing_one(self):
        from actions import protocol_one as p1

        assert "No tool calls recorded recently." in p1.read_recent(None)


def test_recent_is_a_declared_read_only_action():
    from app.tool_declarations import TOOL_DECLARATIONS
    from core import safety_guard

    decl = next(d for d in TOOL_DECLARATIONS if d["name"] == "protocol_one")
    assert "action=recent" in decl["description"]
    assert "limit" in decl["parameters"]["properties"]
    risk = safety_guard.classify_action("protocol_one", {"action": "recent"})
    assert risk["risk_level"] == "LOW" and risk["requires_confirmation"] is False


def test_tool_time_is_counted_in_the_turn_latency_row():
    from app.era_live.audio import _latency_mark

    host = SimpleNamespace()
    _latency_mark(host, "note_tool", 120)
    _latency_mark(host, "note_tool", 0)
    assert host.turn_latency._tool_count == 2 and host.turn_latency._tool_ms == 120
