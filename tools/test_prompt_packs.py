"""Domain rule packs: moved out of the always-on prompt, delivered with a tool's first
result, and the safety rules never left the prompt."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.era_live.tool_batch import ToolBatchMixin
from core import prompt_packs

PROMPT = (Path(__file__).resolve().parent.parent / "core" / "prompt.txt").read_text(
    encoding="utf-8-sig"
)


def test_every_pack_file_exists_and_has_content():
    for tool, name in prompt_packs.TOOL_PACKS.items():
        assert len(prompt_packs.load(name)) > 100, (tool, name)


def test_safety_rules_stay_in_the_always_on_prompt():
    # Gmail: the untrusted-content law and the send gate must never depend on a lazy load.
    assert "12.1 UNTRUSTED CONTENT LAW" in PROMPT and "UNTRUSTED_EMAIL" in PROMPT
    assert "12.3 SENDING — CONFIRMATION-GATED" in PROMPT
    assert "NEVER invent an email address" in PROMPT
    # Netflix misrouting traps that bite on the very first call.
    assert "never computer_settings restart" in PROMPT
    assert "NETFLIX's volume, not the system mixer" in PROMPT
    assert 'netflix_control(action="open")' in PROMPT


def test_moved_text_lives_in_the_pack_not_in_the_prompt():
    moved = {
        "gmail": "watch_start",
        "netflix": "previous_episode",
        "premiere": "presetPath",
        "community": "manage_offer",
    }
    for name, marker in moved.items():
        assert marker in prompt_packs.load(name), name
    for marker in ("watch_start", "previous_episode", "presetPath"):
        assert marker not in PROMPT, marker


def test_the_pack_rides_the_first_result_once_per_connection():
    sent: dict = {}
    first = prompt_packs.pack_for_tool("netflix_control", sent, generation=1)
    assert first.startswith(prompt_packs.HEADER) and "13.1" in first
    assert prompt_packs.pack_for_tool("netflix_control", sent, generation=1) == ""
    assert prompt_packs.pack_for_tool("netflix_control", sent, generation=2) != ""  # new connection
    assert prompt_packs.pack_for_tool("spotify_control", sent, generation=1) == ""  # no pack


def test_a_missing_pack_degrades_to_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt_packs, "PACK_DIR", tmp_path)
    monkeypatch.setattr(prompt_packs, "_cache", {})
    assert prompt_packs.pack_for_tool("gmail_control", {}, 1) == ""


def test_batch_runner_attaches_the_pack_to_the_tool_result():
    era = SimpleNamespace(_conn_generation=3)
    attach = ToolBatchMixin._attach_domain_pack

    result = SimpleNamespace(response={"result": "3 new emails"})
    attach(era, result, "gmail_control")
    assert result.response["result"] == "3 new emails"
    assert "domain_rules" in result.response and "12.2" in result.response["domain_rules"]

    again = SimpleNamespace(response={"result": "x"})
    attach(era, again, "gmail_control")
    assert "domain_rules" not in again.response  # already delivered this connection

    other = SimpleNamespace(response={"result": "x"})
    attach(era, other, "audio_control")
    assert "domain_rules" not in other.response

    era._conn_generation = 4
    later = SimpleNamespace(response={"result": "x"})
    attach(era, later, "gmail_control")
    assert "domain_rules" in later.response


@pytest.mark.parametrize("response", [None, "not a dict"])
def test_an_odd_response_object_is_left_alone(response):
    era = SimpleNamespace(_conn_generation=1)
    ToolBatchMixin._attach_domain_pack(era, SimpleNamespace(response=response), "gmail_control")
