"""core/handled_exceptions: exceptions a tool CATCHES become self-heal evidence.

Several cases reproduce the 2026-09-28 security review of this module:
attribution across calls/threads, short-message matching, the model picking
the module, innermost frames, redaction before truncation, multi-arg str().
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core import handled_exceptions as he
from core import self_heal_incidents

TOOL_SRC = """
import weakref


def lookup(d):
    try:
        return d["missing"]["x"]
    except Exception as e:
        return f"Couldn't look that up: {e}"


def recovers():
    try:
        int("nope")
    except ValueError:
        return 0


class Boom(RuntimeError):
    pass  # a Python subclass is weak-referenceable; RuntimeError itself is not


def keep(box):
    try:
        raise Boom("boom-" + str(len(box)))
    except Boom as e:
        box.append(weakref.ref(e))
        return "failed"


def deep(n):
    if n == 0:
        raise ValueError("bottom of the stack reached")
    return deep(n - 1)


def catch_deep():
    try:
        deep(60)
    except ValueError as e:
        return f"deep failed: {e}"


def leaky(text):
    try:
        raise RuntimeError(text)
    except RuntimeError as e:
        return f"failed: {e}"


def big_args():
    try:
        raise ValueError("bad", b"x" * 5_000_000)
    except ValueError:
        return "failed"
"""


@pytest.fixture
def tool(tmp_path, monkeypatch):
    (tmp_path / "actions").mkdir()
    path = tmp_path / "actions" / "fake_tool.py"
    path.write_text(TOOL_SRC, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("fake_tool_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(he, "REPO_ROOT", tmp_path)
    he._reset_for_tests()
    if not he.install():
        pytest.skip("no sys.monitoring tool slot free (debugger/coverage active)")
    yield mod
    he.uninstall()
    he._reset_for_tests()


@pytest.fixture
def call():
    """Run as one tool call, the way tool_dispatch does."""
    cid = he.new_call_id()
    token = he.CURRENT_CALL.set(cid)
    yield cid
    he.CURRENT_CALL.reset(token)


def test_caught_exception_matched_by_message_tail(tool, call):
    msg = tool.lookup({})
    rec = he.match(call, msg)
    assert rec["type"] == "KeyError" and rec["correlation"] == "message_tail"
    assert rec["frames"][-1][2] == "lookup" and rec["frames"][-1][0].endswith("fake_tool.py")


def test_nothing_recorded_outside_a_tool_call(tool):
    tool.lookup({})  # no CURRENT_CALL: background thread, spy, scheduler...
    assert list(he._records) == []


def test_other_calls_exceptions_are_never_attributed(tool):
    """Finding 1: text a web page puts in one call's failure message must not
    reach an exception caught by a DIFFERENT call (or thread)."""
    token = he.CURRENT_CALL.set(he.new_call_id())
    tool.leaky("specific failure text from another call")
    he.CURRENT_CALL.reset(token)

    victim = he.new_call_id()
    token = he.CURRENT_CALL.set(victim)
    try:
        page = "Error loading content\n... specific failure text from another call ..."
        assert he.match(victim, page) is None
    finally:
        he.CURRENT_CALL.reset(token)


def test_short_quoted_message_only_matches_as_tail(tool, call):
    tool.lookup({})  # KeyError msg is "'missing'"
    assert he.match(call, "Result: 'missing' was quoted by a page") is None
    assert he.match(call, "Couldn't look that up: 'missing'") is not None


def test_module_fallback_needs_the_type_named(tool, call):
    """Finding 2: presence in the module is not cause."""
    tool.recovers()
    assert he.match(call, "Couldn't do it.", module_hint="actions/fake_tool.") is None
    rec = he.match(call, "Couldn't do it (ValueError).", module_hint="actions/fake_tool.")
    assert rec is not None and rec["correlation"] == "module"


def test_module_fallback_refuses_ambiguity(tool, call):
    tool.recovers()
    tool.keep([])  # a second, different exception caught in the same module
    assert he.match(call, "ValueError happened", module_hint="actions/fake_tool.") is None


def test_innermost_frames_are_kept(tool, call):
    """Finding 5: on a deep stack the raise site must survive the cap."""
    msg = tool.catch_deep()
    rec = he.match(call, msg)
    assert len(rec["frames"]) == he._MAX_FRAMES
    assert rec["frames"][-1][2] == "deep"


def test_secret_redacted_before_truncation(tool, call):
    """Finding 6: a key split by the 300-char cut must not survive half-masked."""
    tool.leaky("x" * 285 + " AIzaSyA1234567890abcdefghijklmnop")
    (rec,) = [r for r in he._records if r["call"] == call]
    assert "AIza" not in rec["msg"]


def test_multi_arg_exception_is_not_stringified(tool, call):
    """Finding 4: str() would repr a 5 MB payload before any cut."""
    tool.big_args()
    (rec,) = [r for r in he._records if r["call"] == call]
    assert rec["msg"] == "ValueError"


def test_exception_objects_are_not_kept_alive(tool, call):
    box: list = []
    tool.keep(box)
    gc.collect()
    assert box[0]() is None


def test_watch_cache_is_bounded_and_keyed_by_filename(tool, call, monkeypatch):
    """Finding 3: keyed by string (no code objects kept alive), capped."""
    monkeypatch.setattr(he, "_MAX_CACHE", 5)
    for i in range(20):
        he._watched(f"C:/elsewhere/gen_{i}.py")
    assert len(he._watch_cache) <= 5
    assert all(isinstance(k, str) for k in he._watch_cache)


def test_run_in_executor_carries_the_call_id(tool):
    async def run():
        loop = asyncio.get_running_loop()
        he.install_context_executor(loop)
        cid = he.new_call_id()
        he.CURRENT_CALL.set(cid)
        msg = await loop.run_in_executor(None, tool.lookup, {})
        return cid, msg

    cid, msg = asyncio.run(run())
    assert he.match(cid, msg) is not None


def test_install_is_idempotent(tool):
    assert he.install() and he.installed()


def test_logged_frames_become_a_self_heal_incident(tool, call, tmp_path, monkeypatch):
    from core import logger as era_logger

    rec = he.match(call, tool.lookup({}))
    lines: list[str] = []
    lg = logging.getLogger("era_test_handled")
    monkeypatch.setattr(lg, "log", lambda _lvl, m: lines.append(m))
    era_logger.log_frames(
        lg,
        "tool:fake_tool",
        rec["type"],
        rec["msg"],
        rec["frames"],
        event="tool.execution.caught_exception",
        call_id=call,
    )
    record = json.loads(lines[0])
    record.pop("test_run", None)  # the scan skips pytest-tagged records
    record["ts"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    log = tmp_path / "era.log"
    log.write_text(json.dumps(record) + "\n", encoding="utf-8")
    (inc,) = self_heal_incidents.scan(log_file=log, root=tmp_path)
    assert inc.fingerprint == "KeyError|actions/fake_tool.py|lookup"
    assert inc.uncaught == 0 and inc.caught_calls == {call}


def test_dispatch_logs_only_real_failures(tool, call, monkeypatch):
    from app.era_live import tool_dispatch

    logged: list = []
    monkeypatch.setattr(tool_dispatch, "log_frames", lambda *a, **kw: logged.append((a, kw)))
    mixin = tool_dispatch.ToolDispatchMixin()

    msg = tool.lookup({})
    fail = SimpleNamespace(error_type="Error", short_message=lambda n: msg)
    mixin._log_caught_exception("fake_tool", {"action": "x"}, fail)
    assert len(logged) == 1
    assert logged[0][1]["event"] == "tool.execution.caught_exception"
    assert logged[0][1]["call_id"] == call

    held = SimpleNamespace(error_type="ConfirmationRequired", short_message=lambda n: msg)
    mixin._log_caught_exception("fake_tool", {}, held)
    assert len(logged) == 1  # a held gate is not a defect


def test_redaction_patterns():
    from core.logger import redact_secrets

    for secret in (
        "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe",
        "ya29.a0AfH6SMBx3example_token_value",
        '{"access_token": "abc.def.ghi"}',
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    ):
        out = redact_secrets(secret)
        assert "<redacted>" in out, out
    assert "AAHdq" not in redact_secrets("bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
