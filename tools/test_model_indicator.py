"""Tests for the active-model indicator's data path (phase 8).

WHAT THIS PROTECTS
------------------
The requirement is that the model which actually handled a request is visible,
and that a FALLBACK is visible AS a fallback. That only holds if the displaced
alias survives every hop from the router to the browser:

    RouteDecision -> publish_route() -> ui.set_active_model() -> WS message

Any hop that folded the fallback into a single label would make a substitute
indistinguishable from what was asked for — the exact failure the indicator
exists to prevent. So the fallback fields are asserted at each hop, and the WS
payload is asserted to carry them as separate keys.

The React side is not covered here (this project ships no JS test runner); it is
covered by the field names being pinned on this side, since a rename would break
the contract the component reads.
"""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.era_live.model_route import ModelRouteMixin  # noqa: E402
from core.llm import registry  # noqa: E402
from core.llm.router import route  # noqa: E402

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
  era.cheap:
    provider: claude_cli
    model: haiku
    available: false
    unavailable_reason: the proxy it points at is unreachable
    capabilities:
      json_schema: true
routing:
  by_task:
    complex_planning: era.reason
    community_management: LANE:cm
  availability_fallback: era.voice
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    path = tmp_path / "llm.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(path))
    return tmp_path


class FakeUI:
    """Records what the indicator was told."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.catalogs: list[tuple[list[dict], str]] = []
        self.logs: list[str] = []

    def set_active_model(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))

    def set_model_catalog(self, models, *, selected: str = "") -> None:
        self.catalogs.append((models, selected))

    def write_log(self, text: str) -> None:
        self.logs.append(text)

    @property
    def last(self) -> dict:
        args, kwargs = self.calls[-1]
        return {"label": args[0], "detail": args[1] if len(args) > 1 else "", **kwargs}

    @property
    def last_catalog(self) -> tuple[list[dict], str]:
        return self.catalogs[-1]


class FakeEra(ModelRouteMixin):
    def __init__(self, ui=None) -> None:
        self.ui = ui if ui is not None else FakeUI()


# ── publish_route ─────────────────────────────────────────────────────────


def test_a_normal_route_publishes_its_alias():
    era = FakeEra()
    era.publish_route(route("complex_planning"))
    # The LABEL is what a person reads; the alias rides alongside for the
    # tooltip and the logs. "era.reason" is Era's word for a role, not an
    # answer to "which model is this?".
    assert era.ui.last["label"] == "Claude Sonnet"
    assert era.ui.last["alias"] == "era.reason"


def test_a_fallback_publishes_the_displaced_alias_separately():
    """Folding this into the label is what would make a substitute look like a
    first choice."""
    era = FakeEra()
    era.publish_route(route(override="era.cheap"))
    last = era.ui.last
    # "gemini-live" matches no display rule, so display_name falls through to
    # the raw id. That fallback is deliberate: an unknown model still shows
    # something truthful rather than a blank.
    assert last["label"] == "gemini-live"
    assert last["alias"] == "era.voice"
    assert last["fell_back_from"] == "era.cheap"
    assert "proxy" in last["fallback_reason"]


def test_a_first_choice_carries_no_fallback_fields():
    era = FakeEra()
    era.publish_route(route("complex_planning"))
    assert era.ui.last["fell_back_from"] == ""
    assert era.ui.last["fallback_reason"] == ""


def test_a_pinned_override_is_marked_pinned():
    era = FakeEra()
    era.apply_model_override("era.reason")
    assert era.ui.last["pinned"] is True


def test_an_unpinned_route_is_not_marked_pinned():
    era = FakeEra()
    era.publish_route(era.current_route("complex_planning"))
    assert era.ui.last["pinned"] is False


def test_clearing_the_override_republishes_without_the_pin():
    """The indicator must stop claiming 'pinned' the moment it stops being true."""
    era = FakeEra()
    era.apply_model_override("era.reason")
    era.apply_model_override("auto")
    assert era.ui.last["pinned"] is False
    assert era.ui.last["label"] == "gemini-live"


def test_an_unroutable_request_publishes_unrouted_not_silence():
    """Leaving the previous label up would claim a model is answering when none
    is — worse than showing nothing."""
    era = FakeEra()
    era.publish_route(None)
    assert era.ui.last["label"] == "unrouted"


def test_a_lane_publishes_the_lane_name():
    era = FakeEra()
    era.publish_route(route("community_management"))
    assert era.ui.last["label"] == "cm lane"
    assert era.ui.last["alias"] == "cm"


def test_current_route_publishes_automatically():
    """A decision nothing publishes is a decision the user cannot see."""
    era = FakeEra()
    era.current_route("complex_planning")
    assert era.ui.calls, "current_route did not reach the indicator"


# ── it must never cost the request it describes ───────────────────────────


def test_a_ui_that_raises_does_not_break_routing():
    class HostileUI:
        def set_active_model(self, *a, **k):
            raise RuntimeError("render exploded")

    era = FakeEra(HostileUI())
    assert era.current_route("complex_planning") is not None  # decision survives


def test_a_ui_without_the_method_is_tolerated():
    """Older UI, or a headless run — routing must not depend on a display."""

    class BareUI:
        pass

    era = FakeEra(BareUI())
    era.publish_route(route("complex_planning"))  # must not raise


def test_no_ui_at_all_is_tolerated():
    era = FakeEra(None)
    era.ui = None
    era.publish_route(route("complex_planning"))  # must not raise


# ── the wire format the React component reads ─────────────────────────────


def test_ui_broadcasts_the_documented_message_shape():
    """These key names are the contract with ModelIndicator.tsx / useEra.ts.
    Renaming one here silently blanks the indicator."""
    import ui as ui_module

    sent: list[dict] = []

    # Call the unbound method with a stub carrying just a _broadcast, so this
    # never constructs a Qt widget.
    class Stub:
        def _broadcast(self, msg):
            sent.append(msg)

    ui_module.EraUI.set_active_model(
        Stub(),
        "era.voice",
        "era.voice (gemini/live)",
        fell_back_from="era.cheap",
        fallback_reason="unreachable",
        pinned=True,
    )
    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "model"
    assert msg["label"] == "era.voice"
    assert msg["detail"] == "era.voice (gemini/live)"
    assert msg["fell_back_from"] == "era.cheap"
    assert msg["fallback_reason"] == "unreachable"
    assert msg["pinned"] is True
    assert "ts" in msg


def test_ui_refuses_to_broadcast_an_empty_label():
    """A blank indicator is worse than the previous one: it reads as 'no model'
    when the truth is 'someone forgot to pass a label'."""
    import ui as ui_module

    sent: list[dict] = []

    class Stub:
        def _broadcast(self, msg):
            sent.append(msg)

    ui_module.EraUI.set_active_model(Stub(), "")
    assert sent == []


def test_ui_message_is_json_serialisable():
    import json

    import ui as ui_module

    sent: list[dict] = []

    class Stub:
        def _broadcast(self, msg):
            sent.append(msg)

    ui_module.EraUI.set_active_model(Stub(), "era.reason", "claude_cli/sonnet")
    json.dumps(sent[0])  # must not raise


# ── the switcher ──────────────────────────────────────────────────────────
#
# The indicator SHOWS the model; these cover CHANGING it by click. The path is
#
#     WS model_select -> select_model() -> apply_model_override() -> publish
#
# and the catalogue that populates the menu is built from the registry, so a
# new alias in config/llm.yaml appears without a frontend edit.


def test_the_catalog_lists_every_alias_in_the_registry():
    era = FakeEra()
    aliases = [m["alias"] for m in era.model_catalog()]
    assert aliases == ["era.cheap", "era.reason", "era.voice"]


def test_the_catalog_names_models_not_aliases():
    """Same reason the indicator does: 'era.reason' is a role, not an answer to
    'which model is this?'."""
    era = FakeEra()
    reason = next(m for m in era.model_catalog() if m["alias"] == "era.reason")
    assert reason["label"] == "Claude Sonnet"
    assert reason["detail"] == "claude_cli/sonnet"


def test_an_unavailable_alias_is_listed_with_its_reason_not_hidden():
    """Hiding it leaves the user wondering where it went; showing it greyed out
    with the reason answers the question the absence would have raised."""
    era = FakeEra()
    cheap = next(m for m in era.model_catalog() if m["alias"] == "era.cheap")
    assert cheap["available"] is False
    assert "proxy" in cheap["unavailable_reason"]


def test_selecting_a_model_pins_it_and_republishes_both():
    era = FakeEra()
    era.select_model("era.reason")
    assert era.model_override == "era.reason"
    assert era.ui.last["alias"] == "era.reason"
    assert era.ui.last_catalog[1] == "era.reason"


def test_selecting_nothing_means_automatic():
    """The menu's first row sends '', and it is the only way off a pin by
    mouse — no click on it must be read as an unknown model."""
    era = FakeEra()
    era.select_model("era.reason")
    era.select_model("")
    assert era.model_override is None
    assert era.ui.last_catalog[1] == ""


def test_a_selection_is_echoed_into_the_transcript():
    """A switch that leaves no trace is one the user cannot find again when they
    later wonder why answers started costing money."""
    era = FakeEra()
    era.select_model("era.reason")
    assert any("era.reason" in line for line in era.ui.logs)


def test_the_pinned_alias_is_published_even_when_it_fell_back():
    """era.cheap is unavailable, so the ACTIVE model is era.voice — but the pin
    is still era.cheap. Ticking the substitute would tell the user they chose
    something they did not."""
    era = FakeEra()
    era.select_model("era.cheap")
    assert era.ui.last_catalog[1] == "era.cheap"
    assert era.ui.last["fell_back_from"] == "era.cheap"


def test_an_unknown_alias_changes_nothing():
    era = FakeEra()
    era.select_model("era.reason")
    era.select_model("gpt-4")
    assert era.model_override == "era.reason"


def test_publish_model_state_sends_the_catalog_and_the_current_model():
    """What a freshly connected window needs: without it the indicator sits on
    'resolving…' until something happens to route a request."""
    era = FakeEra()
    era.publish_model_state()
    assert era.ui.catalogs, "the switcher would have opened empty"
    assert era.ui.last["label"] == "gemini-live"


def test_the_switcher_never_costs_the_switch():
    class HostileUI:
        def set_active_model(self, *a, **k) -> None: ...

        def set_model_catalog(self, *a, **k):
            raise RuntimeError("menu exploded")

    era = FakeEra(HostileUI())
    era.select_model("era.reason")  # must not raise
    assert era.model_override == "era.reason"


def test_a_ui_without_the_catalog_method_is_tolerated():
    """Older UI, or a headless run — pinning must not depend on a menu."""

    class BareUI:
        def set_active_model(self, *a, **k) -> None: ...

    era = FakeEra(BareUI())
    era.select_model("era.reason")  # must not raise
    assert era.model_override == "era.reason"


def test_an_unreadable_registry_empties_the_menu_rather_than_raising(monkeypatch):
    monkeypatch.setenv("ERA_LLM_CONFIG", "no/such/llm.yaml")
    monkeypatch.setattr(registry, "_CACHE", None)
    assert FakeEra().model_catalog() == []


def test_ui_broadcasts_the_catalog_message_shape():
    """These key names are the contract with ModelIndicator.tsx / useEra.ts."""
    import json

    import ui as ui_module

    sent: list[dict] = []

    class Stub:
        def _broadcast(self, msg):
            sent.append(msg)

    ui_module.EraUI.set_model_catalog(
        Stub(),
        [
            {
                "alias": "era.reason",
                "label": "Claude Sonnet",
                "detail": "claude_cli/sonnet",
                "role": "planning",
                "available": True,
                "unavailable_reason": "",
                "cost_per_call_usd": 0.058,
                "realtime_audio": False,
            }
        ],
        selected="era.reason",
    )
    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "model_catalog"
    assert msg["selected"] == "era.reason"
    assert msg["models"][0]["alias"] == "era.reason"
    assert msg["models"][0]["cost_per_call_usd"] == 0.058
    json.dumps(msg)  # must not raise


def test_ui_refuses_to_broadcast_an_empty_catalog():
    """An empty menu reads as 'there are no other models', which is never what
    an unreadable registry means."""
    import ui as ui_module

    sent: list[dict] = []

    class Stub:
        def _broadcast(self, msg):
            sent.append(msg)

    ui_module.EraUI.set_model_catalog(Stub(), [])
    assert sent == []


def test_the_websocket_routes_a_click_to_the_selector():
    """The click's whole path is this message type; a rename here silently makes
    the switcher inert."""
    import asyncio
    import json

    from app.websocket_server import WebSocketServer

    picked: list[str] = []
    server = WebSocketServer()
    server._on_model_select = picked.append

    class FakeSocket:
        async def send_str(self, _msg):
            pass

    # Every inbound frame goes through _dispatch (the aiohttp handler loops over
    # frames and hands each one here).
    asyncio.run(
        server._dispatch(FakeSocket(), json.dumps({"type": "model_select", "alias": "era.deep"}))
    )
    # Handled on a worker thread; give it a moment to land.
    for _ in range(50):
        if picked:
            break
        time.sleep(0.01)
    assert picked == ["era.deep"]


def test_a_connecting_client_triggers_a_republish():
    """Otherwise a reopened window shows 'resolving…' until the next request."""
    import asyncio

    from app.websocket_server import WebSocketServer

    hits: list[int] = []
    server = WebSocketServer()
    server._on_client_connect = lambda: hits.append(1)

    class FakeSocket:
        async def send_str(self, _msg):
            pass

    # _greet is what the aiohttp handler runs for every newly accepted socket.
    asyncio.run(server._greet(FakeSocket()))
    for _ in range(50):
        if hits:
            break
        time.sleep(0.01)
    assert hits == [1]
