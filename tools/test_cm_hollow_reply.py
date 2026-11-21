"""PF-069 guard: a CM run whose model only offers help did not do the work.

Live 2026-11-20 14:12: local Qwen answered "I'm ready to help! ... What would you
like help with today?" to a concrete order, wrote nothing, and was logged ok.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import era_dispatch as d

HOLLOW = "I'm ready to help! I have access to many tools.\n\nWhat would you like help with today?"


def _proc(result, rc=0, is_error=False):
    return SimpleNamespace(
        returncode=rc, stdout=json.dumps({"result": result, "is_error": is_error}), stderr=""
    )


def test_an_offer_of_help_is_hollow_but_a_real_answer_is_not():
    assert d._hollow_reply(HOLLOW)
    assert d._hollow_reply("How can I help you today?")
    assert not d._hollow_reply("Facebook description 1: Découvrez notre Alya...")
    assert not d._hollow_reply("")
    assert not d._hollow_reply("What would you like? " + "x" * 2000)  # a long report is content


def test_a_hollow_reply_falls_through_to_the_next_route_but_a_real_one_does_not():
    assert d._is_route_failure(_proc(HOLLOW))
    assert not d._is_route_failure(_proc("Wrote the two descriptions to the vault."))


def test_run_task_reports_a_hollow_run_as_a_failure():
    import inspect

    src = inspect.getsource(d.run_task)
    assert "not wrote and _hollow_reply(result)" in src
