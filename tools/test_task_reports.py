"""core.task_reports: how a scheduled-task run reaches the hive and the user.

The scheduler decides WHAT happened; this module decides what is said about it
and what stays open in the hive. Two rules here are safety-relevant rather than
cosmetic:

  * a run parked for approval is announced whatever `notify` says - suppressing
    a safety prompt would leave a task silently waiting forever, and
  * a successful run RESOLVES the task's open insight, so a transient failure
    does not stay in the briefing after the next run works.

`core.hive` is faked in every test: a real post would put a fabricated insight
into the live issue map Era briefs from (the conftest redirects ERA_HIVE_DIR for
the same reason).
"""

import pytest

import core.task_reports as tr


# ── fake hive ────────────────────────────────────────────────────────────────
class _FakeHive:
    def __init__(self):
        self.messages, self.insights, self.resolved = [], [], []

    def send_message(self, agent, to, kind, payload):
        self.messages.append({"agent": agent, "to": to, "kind": kind, "payload": payload})

    def post_insight(self, agent, sig, **kw):
        self.insights.append({"agent": agent, "sig": sig, **kw})

    def resolve_insight(self, sig, by=None, note=None):
        self.resolved.append({"sig": sig, "by": by, "note": note})

    def sigs(self):
        return [i["sig"] for i in self.insights]


@pytest.fixture
def hive(monkeypatch):
    fake = _FakeHive()
    monkeypatch.setattr(tr, "hive", fake)
    monkeypatch.setattr(tr, "describe_action", lambda t: f"<{t['name']} action>")
    return fake


def _task(**kw):
    return {"id": "t1", "name": "Morning brief", "kind": "tool", **kw}


def _entry(status="ok", message="done", trigger="schedule", n=1, tool="web_search"):
    return {"status": status, "message": message, "trigger": trigger, "n": n, "tool": tool}


# ── signature ────────────────────────────────────────────────────────────────
def test_insight_signature_is_namespaced_per_task():
    assert tr.insight_signature("abc") == "scheduler:abc"
    assert tr.insight_signature("abc") != tr.insight_signature("abd")


# ── feed_hive: every run is broadcast ────────────────────────────────────────
def test_every_run_sends_a_task_run_message(hive):
    tr.feed_hive(_task(), _entry(), None)
    assert len(hive.messages) == 1
    msg = hive.messages[0]
    assert msg["agent"] == tr.AGENT == "scheduler"
    assert msg["kind"] == "task_run"
    assert msg["payload"]["id"] == "t1"
    assert msg["payload"]["name"] == "Morning brief"
    assert msg["payload"]["status"] == "ok"


@pytest.mark.parametrize("status", ["failed", "blocked", "missed", "partial"])
def test_bad_outcomes_open_an_insight(hive, status):
    tr.feed_hive(_task(), _entry(status=status, message="boom"), None)
    posted = [i for i in hive.insights if i["sig"] == "scheduler:t1"]
    assert len(posted) == 1
    assert posted[0]["category"] == "scheduled_task"
    assert status in posted[0]["title"]
    assert posted[0]["evidence"] == "boom"
    assert posted[0]["location"] == "scheduled task t1"


def test_a_blocked_run_outranks_the_other_failures(hive):
    """Blocked means a safety gate stopped it - higher severity than a crash."""
    tr.feed_hive(_task(), _entry(status="blocked"), None)
    assert hive.insights[0]["severity"] == "high"


@pytest.mark.parametrize("status", ["failed", "missed", "partial"])
def test_other_failures_are_medium(hive, status):
    tr.feed_hive(_task(), _entry(status=status), None)
    assert hive.insights[0]["severity"] == "medium"


def test_a_successful_run_resolves_the_open_insight(hive):
    """The re-check that justifies 'resolved': the task actually ran fine."""
    tr.feed_hive(_task(), _entry(status="ok"), None)
    assert {"sig": "scheduler:t1", "by": "scheduler", "note": "a later run succeeded"} in (
        hive.resolved
    )


def test_a_failing_run_does_not_resolve_its_own_insight(hive):
    tr.feed_hive(_task(), _entry(status="failed"), None)
    assert "scheduler:t1" not in [r["sig"] for r in hive.resolved]


# ── feed_hive: the approval insight is tracked separately ────────────────────
def test_awaiting_confirmation_opens_an_approval_insight(hive):
    tr.feed_hive(
        _task(), _entry(status="awaiting_confirmation", message="needs ok"), {"reason": "high risk"}
    )
    approval = [i for i in hive.insights if i["sig"].endswith(":approval")]
    assert len(approval) == 1
    assert approval[0]["category"] == "needs_approval"
    assert approval[0]["evidence"] == "high risk", "the safety reason, when given"
    assert "waiting for your approval" in approval[0]["title"]


def test_approval_insight_falls_back_to_the_run_message(hive):
    tr.feed_hive(_task(), _entry(status="awaiting_confirmation", message="needs ok"), None)
    assert hive.insights[0]["evidence"] == "needs ok"


def test_awaiting_confirmation_does_not_open_a_failure_insight(hive):
    """Waiting for the user is not a fault; only the approval insight opens."""
    tr.feed_hive(_task(), _entry(status="awaiting_confirmation"), None)
    assert hive.sigs() == ["scheduler:t1:approval"]


@pytest.mark.parametrize("status", ["ok", "failed", "blocked", "skipped"])
def test_any_non_pending_outcome_clears_the_approval_insight(hive, status):
    """Once the run has an outcome the question is answered, however it went."""
    tr.feed_hive(_task(), _entry(status=status), None)
    assert {"sig": "scheduler:t1:approval", "by": "scheduler", "note": f"run {status}"} in (
        hive.resolved
    )


def test_resolve_all_clears_both_insights(hive):
    tr.resolve_all(_task(), "task deleted")
    assert [r["sig"] for r in hive.resolved] == ["scheduler:t1", "scheduler:t1:approval"]
    assert all(r["note"] == "task deleted" for r in hive.resolved)


# ── report_text: what the user actually hears ────────────────────────────────
@pytest.mark.parametrize("trigger", ["manual", "approved"])
def test_a_run_the_user_asked_for_in_the_live_turn_is_not_re_announced(trigger):
    """The tool result already carried it back; saying it again is a double report."""
    assert tr.report_text(_task(), _entry(trigger=trigger)) is None


def test_an_approval_prompt_is_never_suppressed():
    """Not even by notify='silent' - a safety prompt must reach the user."""
    text = tr.report_text(
        _task(notify="silent"), _entry(status="awaiting_confirmation", message="high risk")
    )
    assert text is not None
    assert "needs your approval" in text
    assert "'yes'" in text and "'always'" in text and "'skip'" in text


def test_the_approval_prompt_describes_what_would_run(hive):
    text = tr.report_text(_task(), _entry(status="awaiting_confirmation"))
    assert "<Morning brief action>" in text, "describe_action spells out the pending action"


def test_a_successful_run_is_announced_at_notify_all():
    text = tr.report_text(_task(notify="all"), _entry(status="ok", message="3 items"))
    assert text == "Scheduled task 'Morning brief' finished: 3 items"


def test_a_successful_run_is_quiet_at_any_other_notify_level():
    for level in ("errors", "silent"):
        assert tr.report_text(_task(notify=level), _entry(status="ok")) is None


def test_notify_defaults_to_all():
    assert tr.report_text(_task(), _entry(status="ok", message="x")) is not None


def test_a_reminder_is_spoken_as_the_reminder_text():
    text = tr.report_text(_task(kind="reminder"), _entry(status="ok", message="take pills"))
    assert text == "Reminder: take pills"


def test_a_reminder_is_announced_even_when_notify_is_silent():
    """Documented as-is: the reminder branch sits ABOVE the notify checks, so a
    reminder is the one 'ok' outcome silence does not suppress. That matches
    what a reminder is for - if this is ever meant to be silenceable, this test
    is the place that says so."""
    text = tr.report_text(
        _task(kind="reminder", notify="silent"), _entry(status="ok", message="up")
    )
    assert text == "Reminder: up"


@pytest.mark.parametrize("status", ["failed", "blocked", "missed", "skipped"])
def test_failures_are_reported_with_their_status_and_reason(status):
    text = tr.report_text(_task(), _entry(status=status, message="net down"))
    assert text == f"Scheduled task 'Morning brief' {status}: net down"


@pytest.mark.parametrize("status", ["failed", "blocked", "missed", "skipped", "partial"])
def test_silent_suppresses_every_failure_report(status):
    assert tr.report_text(_task(notify="silent"), _entry(status=status)) is None


def test_a_partial_run_is_not_announced_as_a_plain_failure():
    """The user needs to know what DID run before they retry it."""
    text = tr.report_text(_task(), _entry(status="partial", message="2 of 3 steps"))
    assert "only partly ran" in text
    assert "2 of 3 steps" in text
    assert "failed" not in text


# ── step_failure_text: a broken link in a chain that carries on ──────────────
def test_step_failure_is_reported_before_the_next_link_runs():
    text = tr.step_failure_text(
        _task(), _entry(status="failed", message="404", n=2, tool="web_search"), total=4
    )
    assert "step 2 of 4" in text
    assert "web_search" in text and "404" in text
    assert "continuing with the remaining steps" in text


def test_step_failure_is_suppressed_only_by_silent():
    assert tr.step_failure_text(_task(notify="silent"), _entry(status="failed"), 3) is None
    assert tr.step_failure_text(_task(notify="errors"), _entry(status="failed"), 3) is not None


# ── status_detail ────────────────────────────────────────────────────────────
def test_status_detail_counts_active_pending_and_failing():
    tasks = [
        {"enabled": True, "finished": False},
        {"enabled": True, "finished": False, "pending": True},
        {"enabled": True, "finished": False, "last_run": {"status": "failed"}},
        {"enabled": True, "finished": True},  # finished: not active
        {"enabled": False},  # disabled: not active
    ]
    assert tr.status_detail(tasks) == "3 active task(s), 1 awaiting approval, 1 failing"


@pytest.mark.parametrize("status", ["failed", "blocked", "partial"])
def test_every_bad_last_run_counts_as_failing(status):
    assert tr.status_detail([{"last_run": {"status": status}}]).endswith("1 failing")


def test_a_successful_last_run_is_not_failing():
    assert tr.status_detail([{"last_run": {"status": "ok"}}]).endswith("0 failing")


def test_status_detail_handles_an_empty_list():
    assert tr.status_detail([]) == "0 active task(s), 0 awaiting approval, 0 failing"


# ── briefing_line ────────────────────────────────────────────────────────────
def test_briefing_says_so_when_there_is_nothing_scheduled():
    assert tr.briefing_line([]) == "No scheduled tasks."


def test_briefing_counts_tasks():
    assert tr.briefing_line([{"name": "a"}, {"name": "b"}]) == "2 scheduled task(s)."


def test_briefing_names_tasks_awaiting_approval():
    line = tr.briefing_line([{"name": "Deploy", "pending": True}, {"name": "Brief"}])
    assert "Awaiting your approval: Deploy." in line
    assert "Brief" not in line.split("Awaiting your approval:")[1]


def test_briefing_names_tasks_with_unread_results():
    line = tr.briefing_line([{"name": "Brief", "unread": True}])
    assert "New results: Brief." in line


def test_briefing_reports_both_lists_together():
    line = tr.briefing_line(
        [{"name": "Deploy", "pending": True}, {"name": "Brief", "unread": True}]
    )
    assert "Awaiting your approval: Deploy." in line
    assert "New results: Brief." in line
