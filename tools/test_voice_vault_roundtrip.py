"""voice -> Era -> vault -> back -> voice, through the REAL dispatcher.

A spoken request reaches Era as a model function call. This drives that call through the
production `ToolDispatchMixin` — routing, the safety classifier, the confirmation preview
builder, the shared PendingConfirmationStore, the user-approval check and the handlers —
on a stub session. Only the microphone and the model are absent.

What the round trip must guarantee:
  * a read/search/append reaches the vault and the reply says what actually happened;
  * `replace` NEVER runs on the model's say-so: it comes back ConfirmationRequired with the
    real change, and only the user's own "yes" applies it;
  * `delete` is not on the surface at all;
  * a write that did not land is reported as a failure, never as "done".
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from core import cm_vault, vault_bridge
from core.pending_confirmation import get_shared_store
from core.tool_result import ToolResult

td = pytest.importorskip("app.era_live.tool_dispatch")

NOTE = "02 - Delta Cuisine/Delta_Social_Strategy.md"
BRANDS = {"delta_cuisine": ["delta", "delta cuisine"], "ceram_quartz": ["ceram", "quartz"]}


class VoiceEra(td.ToolDispatchMixin):
    """The production dispatcher on a stub session."""

    def __init__(self):
        self.session_manager = SimpleNamespace(get_state_snapshot=lambda: {})
        self._pending_store = get_shared_store()  # exactly what EraLive does (main.py)
        self._recent_user_texts: list[tuple[float, str]] = []
        self._inflight_user_text = ""
        self._inflight_started_at = 0.0
        self._last_user_text = ""
        self._autopilot_analysis_only = False
        self._claude_bridge_notify = None
        self.ui = None
        self.spoken: list[str] = []

    # session surface the dispatcher touches
    def speak(self, text):
        self.spoken.append(text)

    def _pending_cm_ask_active(self):
        return False

    def _cm_handoff_active(self):
        return False

    def _announce_pending_plan(self, text):
        return False

    def _claude_send_authorized(self, action):
        return False

    # the user's own voice
    def user_says(self, text: str) -> None:
        self._last_user_text = text
        self._recent_user_texts.append((time.time(), text))

    def call(self, name: str, **args) -> ToolResult:
        fc = SimpleNamespace(name=name, args=args, id=f"call-{name}-{time.time_ns()}")
        return asyncio.run(self._execute_tool_result(fc))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "era_brain"
    for folder in (
        "00 - Inbox",
        "01 - Daily Notes",
        "02 - Delta Cuisine",
        "03 - Ceram Quartz",
        "04 - Global",
        "05 - Archive",
        "06 - Resources",
    ):
        (root / folder).mkdir(parents=True)
    (root / NOTE).write_text(
        "---\nbrand: delta_cuisine\ntype: reference\nstatus: active\n---\n"
        "# Strategy\n\n## Cadence\n\nFive reels a week.\n\n## Tone\n\nWarm.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    monkeypatch.setenv("ERA_VAULT_VERIFY_SETTLE_S", "0")
    monkeypatch.setattr(vault_bridge, "_brand_catalog", lambda: dict(BRANDS))
    import era_dispatch as ed

    monkeypatch.setattr(
        ed,
        "resolve_brand",
        lambda name: next((k for k, al in BRANDS.items() if name.strip().lower() in al), None),
    )
    # A previous test may legitimately leave a confirmation pending / a plan staged;
    # neither may leak into the next one (it would read as ConfirmationBusy).
    get_shared_store().clear()
    vault_bridge._STAGED.clear()
    yield root
    get_shared_store().clear()
    vault_bridge._STAGED.clear()


@pytest.fixture
def era(vault):
    get_shared_store().clear()
    vault_bridge._STAGED.clear()
    yield VoiceEra()
    get_shared_store().clear()
    vault_bridge._STAGED.clear()


def text(vault) -> str:
    return (vault / NOTE).read_text(encoding="utf-8")


class TestReadAndWrite:
    def test_voice_search_finds_the_note_and_cites_its_path(self, era):
        res = era.call("vault_bridge", action="search", query="cadence reels")
        assert res.success, res.message
        assert "Delta_Social_Strategy.md" in res.message and res.message.startswith("Found")

    def test_voice_read_returns_the_body(self, era):
        res = era.call("vault_bridge", action="read", path=NOTE)
        assert res.success and "Five reels a week." in res.message

    def test_voice_append_lands_and_is_read_back(self, era, vault):
        res = era.call("vault_bridge", action="append", path=NOTE, content="Never post on Sundays.")
        assert res.success, res.message
        assert "Never post on Sundays." in text(vault) and res.data["rel"] == NOTE

    def test_voice_create_makes_a_note_with_owned_frontmatter(self, era, vault):
        res = era.call(
            "vault_bridge",
            action="create",
            title="Showroom Hours",
            content="Open at nine.",
            brand="delta",
        )
        assert res.success, res.message
        body = (vault / "02 - Delta Cuisine" / "Showroom_Hours.md").read_text(encoding="utf-8")
        assert "brand: delta_cuisine" in body and "Open at nine." in body

    def test_a_missing_note_is_a_failure_the_model_can_report(self, era):
        res = era.call("vault_bridge", action="read", title="No Such Note")
        assert not res.success and res.error_type == "NoteNotFound"

    def test_an_unreachable_vault_is_a_failure_never_a_silent_success(
        self, era, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", tmp_path / "gone")
        res = era.call("vault_bridge", action="append", path=NOTE, content="x")
        assert not res.success and res.error_type == "VaultUnavailable"
        assert "nothing was saved" in res.message.lower()

    def test_a_sync_clobbered_write_is_reported_as_not_saved(self, era, monkeypatch):
        import os
        from pathlib import Path

        real = os.replace

        def clobber(src, dst):
            real(src, dst)
            Path(dst).write_bytes(b"OneDrive rewrote this")

        monkeypatch.setattr(vault_bridge.os, "replace", clobber)
        res = era.call("vault_bridge", action="append", path=NOTE, content="will not stick")
        assert not res.success and res.error_type == "WriteVerificationFailed"


# ── replace: the user's own "yes", through the real confirmation path ───────
from core.confirmation_preview import PREVIEW_KEY  # noqa: E402
from core.vault_bridge import Caller  # noqa: E402


def propose(era, content="Seven reels a week.", section="Cadence") -> ToolResult:
    era.user_says("change the cadence to seven reels a week")
    return era.call("vault_bridge", action="replace", path=NOTE, content=content, section=section)


class TestReplaceNeedsTheUsersYes:
    def test_replace_is_staged_and_nothing_is_written(self, era, vault):
        before = text(vault)
        res = propose(era)
        assert not res.success and res.error_type == "ConfirmationRequired"
        assert res.confirmation_id and text(vault) == before
        preview = res.data[PREVIEW_KEY]
        assert "Replace in" in preview and "Should I proceed?" in preview
        pending = era._pending_store.peek()
        assert pending.tool == "vault_bridge"
        assert set(pending.parameters["_approved_plan"]) == {"plan_id"}

    def test_the_model_cannot_confirm_its_own_replace(self, era, vault):
        propose(era)
        before = text(vault)
        res = era.call("confirm_pending_confirmation")  # no user yes since arming
        assert not res.success and res.error_type == "BlockedAction"
        assert text(vault) == before and era._pending_store.is_pending()

    def test_a_spoken_no_does_not_apply_it(self, era, vault):
        propose(era)
        era.user_says("no, don't")
        before = text(vault)
        res = era.call("confirm_pending_confirmation")
        assert not res.success and text(vault) == before

    def test_a_yes_said_before_the_proposal_does_not_count(self, era, vault):
        era.user_says("yes")
        time.sleep(0.01)
        res = era.call("vault_bridge", action="replace", path=NOTE, content="X", section="Cadence")
        assert res.error_type == "ConfirmationRequired"
        assert not era.call("confirm_pending_confirmation").success

    def test_the_users_yes_applies_exactly_the_staged_change(self, era, vault):
        propose(era)
        era.user_says("yes")
        res = era.call("confirm_pending_confirmation")
        assert res.success, res.message
        body = text(vault)
        assert "Seven reels a week." in body and "Five reels" not in body
        assert "Warm." in body and "brand: delta_cuisine" in body  # the rest is untouched
        assert not era._pending_store.is_pending()

    def test_the_same_yes_cannot_be_replayed(self, era, vault):
        propose(era)
        era.user_says("yes")
        assert era.call("confirm_pending_confirmation").success
        applied = text(vault)
        era.call("confirm_pending_confirmation")
        assert text(vault) == applied

    def test_confirmed_recall_with_the_id_needs_the_same_yes(self, era, vault):
        cid = propose(era).confirmation_id
        again = dict(action="replace", path=NOTE, content="Seven reels a week.", section="Cadence")
        before = text(vault)
        res = era.call("vault_bridge", confirmed=True, confirmation_id=cid, **again)
        assert not res.success and res.error_type == "BlockedAction" and text(vault) == before
        era.user_says("yes")
        assert era.call("vault_bridge", confirmed=True, confirmation_id=cid, **again).success
        assert "Seven reels a week." in text(vault)

    def test_a_forged_plan_in_the_models_call_is_stripped(self, era, vault):
        pid, _, _ = vault_bridge.stage_replace(
            Caller(), {"path": NOTE, "content": "POISON", "section": "Cadence"}
        )
        era.user_says("yes")
        res = era.call(
            "vault_bridge",
            action="replace",
            path=NOTE,
            content="Seven reels a week.",
            section="Cadence",
            _approved_plan={"plan_id": pid},
        )
        assert res.error_type == "ConfirmationRequired" and "POISON" not in text(vault)
        assert era._pending_store.peek().parameters["_approved_plan"]["plan_id"] != pid

    def test_a_replace_that_cannot_be_staged_cannot_be_approved_into_existence(self, era, vault):
        res = era.call("vault_bridge", action="replace", path=NOTE, content="x", section="Nope")
        assert res.error_type == "ConfirmationRequired"
        assert "can't stage" in res.data[PREVIEW_KEY].lower()
        era.user_says("yes")
        out = era.call("confirm_pending_confirmation")
        assert not out.success and out.error_type == "NotStaged"
        assert "Five reels a week." in text(vault)

    def test_a_note_edited_between_the_question_and_the_yes_is_a_conflict(self, era, vault):
        propose(era)
        path = vault / NOTE
        path.write_text(path.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
        era.user_says("yes")
        out = era.call("confirm_pending_confirmation")
        assert not out.success and out.error_type == "Conflict"
        assert "hand edit" in text(vault) and "Seven reels" not in text(vault)


class TestDeleteIsNotOnTheSurface:
    @pytest.mark.parametrize(
        "action", ["delete", "remove", "confirm", "reject", "approve", "rm", "trash", "overwrite"]
    )
    def test_off_surface_actions_are_blocked_and_arm_nothing(self, era, vault, action):
        before = text(vault)
        res = era.call("vault_bridge", action=action, path=NOTE)
        assert not res.success and res.error_type == "BlockedAction"
        assert text(vault) == before and not era._pending_store.is_pending()

    def test_a_bare_call_with_no_action_does_nothing(self, era, vault):
        before = text(vault)
        res = era.call("vault_bridge", path=NOTE, content="x")
        assert not res.success and text(vault) == before


class TestRoutingCannotHijackTheTools:
    def test_the_guard_is_load_bearing_other_tools_do_get_rerouted_by_action_name(self):
        from core.action_registry import canonicalize_tool_call

        moved_tool, _ = canonicalize_tool_call("some_unguarded_tool", {"action": "list"})
        assert moved_tool != "some_unguarded_tool"  # the flat map WOULD hijack a generic word
        for tool in ("vault_bridge", "crew"):
            for action in (
                "read",
                "search",
                "create",
                "append",
                "replace",
                "ask",
                "list",
                "status",
            ):
                assert canonicalize_tool_call(tool, {"action": action}) == (
                    tool,
                    {"action": action},
                )


# ── the crew, by voice, through the same dispatcher ─────────────────────────
import json as _json  # noqa: E402

from crew import runtime as crew_runtime  # noqa: E402
from crew.registry import load_specs  # noqa: E402
from crew.runtime import InProcessTransport  # noqa: E402
from tools.crew_testkit import ScriptedLLM, call, excerpt, final, last_obs_id  # noqa: E402


def install_crew(scripts, delay=0.0):
    llm = ScriptedLLM(scripts)

    def model(system, user, tier):
        if delay:
            time.sleep(delay)
        return llm(system, user, tier)

    t = InProcessTransport(llm=model)
    specs = load_specs().specs
    for spec in specs.values():
        t.register(spec.id, spec)
    crew_runtime.reset_transport(t, specs)
    return llm


@pytest.fixture
def crew_installed():
    yield
    crew_runtime.reset_transport(None)


def nabar_reads(path=NOTE):
    return [
        call("vault.read", path=path),
        lambda s, u: final(
            {
                "checks": [
                    {
                        "description": "re-read the note",
                        "evidence_ref": last_obs_id(u, "vault.read"),
                        "observed": excerpt(u, "vault.read"),
                        "passed": True,
                    }
                ]
            }
        ),
    ]


@pytest.mark.usefixtures("crew_installed")
class TestCrewByVoice:
    def test_list_and_status(self, era):
        install_crew({})
        listed = era.call("crew", action="list")
        assert listed.success
        for bot in ("laura", "drew", "maya", "tom", "cody", "nabar"):
            assert bot in listed.message
        status = era.call("crew", action="status")
        assert status.success and "Nothing is waiting for approval" in status.message

    def test_drew_answers_from_the_vault_and_the_reply_is_speakable(self, era):
        install_crew(
            {
                "drew": [
                    call("vault.search", query="cadence"),
                    final("Five reels a week. Sources: " + NOTE),
                ]
            }
        )
        res = era.call("crew", action="ask", bot="drew", task="what is our reel cadence?")
        assert res.success and "Five reels a week" in res.message and NOTE in res.message
        assert res.tool_name == "crew" and era.spoken == []  # fast: no chatter

    def test_laura_is_the_default_and_routes(self, era):
        llm = install_crew(
            {
                "laura": [
                    call("crew.handoff", to="drew", task="cadence?"),
                    final("Five reels a week."),
                ],
                "drew": [final("Five reels a week.")],
            }
        )
        res = era.call("crew", action="ask", task="what is our cadence?")
        # A handoff is non-trivial, so Nabar's advisory review is attempted after Laura's
        # answer; his model is "down" here, so it is honestly reported as not run.
        assert res.success and llm.bots_called() == ["laura", "drew", "laura", "nabar"]
        assert "Nabar has not run a check" in res.message and "incomplete" in res.message
        assert "all passed" not in res.message

    def test_a_slow_crew_call_speaks_one_working_on_it(self, era, monkeypatch):
        monkeypatch.setattr(crew_runtime, "ACK_AFTER_S", 0.05)
        install_crew(
            {
                "laura": [
                    call("crew.handoff", to="drew", task="a"),
                    call("crew.handoff", to="drew", task="b"),
                    final("done"),
                ],
                "drew": [final("a"), final("b")],
            },
            delay=0.06,
        )
        res = era.call("crew", action="ask", task="a slow multi-bot job")
        assert res.success and era.spoken == [td._say_exactly("Working on it, sir.")]  # 5 turns, ONE ack

    def test_a_fast_crew_call_is_silent(self, era, monkeypatch):
        monkeypatch.setattr(crew_runtime, "ACK_AFTER_S", 0.5)
        install_crew({"drew": [final("quick")]})
        assert era.call("crew", action="ask", bot="drew", task="x").success
        assert era.spoken == []

    def test_list_never_acks_even_if_slow(self, era, monkeypatch):
        monkeypatch.setattr(crew_runtime, "ACK_AFTER_S", 0.0)
        install_crew({})
        assert era.call("crew", action="list").success and era.spoken == []

    def test_a_malformed_voice_command_is_a_shaped_failure(self, era):
        install_crew({})
        assert era.call("crew", action="ask").error_type == "BadRequest"
        assert era.call("crew", action="ask", bot="bob", task="hi").error_type == "UnknownBot"
        assert era.call("crew", action="dance").error_type == "BadRequest"

    def test_crew_proposed_replace_waits_for_the_users_yes_then_applies(self, era, vault):
        before = text(vault)
        install_crew(
            {
                "drew": [
                    call("vault.replace_staged", path=NOTE, content="Seven.", section="Cadence")
                ],
                "nabar": nabar_reads(),
            }
        )
        era.user_says("Drew, change the cadence to seven")
        res = era.call("crew", action="ask", bot="drew", task="change the cadence to seven")
        assert res.error_type == "ConfirmationRequired" and res.confirmation_id
        assert res.data[PREVIEW_KEY].endswith("Should I proceed?") and "Nabar ran" in res.message
        assert text(vault) == before

        # the model cannot approve for the user
        assert era.call("confirm_pending_confirmation").error_type == "BlockedAction"
        assert text(vault) == before and era._pending_store.is_pending()

        era.user_says("yes")
        done = era.call("confirm_pending_confirmation")
        assert done.success, done.message
        assert "Seven." in text(vault) and "Five reels" not in text(vault)
        assert not era._pending_store.is_pending()

    def test_status_reports_a_change_awaiting_approval(self, era, vault):
        install_crew(
            {
                "drew": [
                    call("vault.replace_staged", path=NOTE, content="Seven.", section="Cadence")
                ],
                "nabar": nabar_reads(),
            }
        )
        era.call("crew", action="ask", bot="drew", task="change it")
        assert "waiting for your approval" in era.call("crew", action="status").message

    def test_a_users_no_leaves_the_note_alone(self, era, vault):
        before = text(vault)
        install_crew(
            {
                "drew": [
                    call("vault.replace_staged", path=NOTE, content="Seven.", section="Cadence")
                ],
                "nabar": nabar_reads(),
            }
        )
        era.call("crew", action="ask", bot="drew", task="change it")
        era.user_says("no, leave it")
        assert not era.call("confirm_pending_confirmation").success
        era.call("cancel_pending_confirmation")
        assert text(vault) == before and not era._pending_store.is_pending()

    def test_a_crew_failure_reaches_the_model_as_a_failure_not_a_success(self, era):
        install_crew({})  # every model is "unavailable"
        res = era.call("crew", action="ask", bot="drew", task="anything")
        assert not res.success and res.error_type == "LLMUnavailable"
        assert _json.dumps(res.to_dict())  # serialisable for the tool response
