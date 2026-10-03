"""
test_hermes_wiring.py — Stage 3 wiring proof: hermes_dispatch behind Era's typed
text path (main.py's _on_text_command -> _route_text_through_hermes -> _run_cm_flow).

Voice is intentionally NOT covered here (see [[era-hermes-routing]] for why it's
deferred to its own stage). These four scenarios are the agreed acceptance tests
for THIS pass:

  1. Non-CM text falls straight through to Gemini, unchanged (zero regression).
  2. CM text with an unambiguous brand dispatches and speaks the result.
  3. CM text with an ambiguous/missing brand asks "which brand?", and a bare
     brand-name reply auto re-dispatches (the pending-ask guardrails).
  4. CM text while another job is running speaks the "queued behind" line before
     dispatching.

`era_dispatch` and `hermes_dispatch` are faked in sys.modules before `main` is
imported, same isolation style as test_hermes_router.py, so this runs without a
live Gemini/Claude account and without touching the real subprocess lock.

Run:  pytest test_hermes_wiring.py -v
"""

import asyncio
import sys
import time
import types

import pytest


# --- Fake era_dispatch -------------------------------------------------------
class _FakeEraDispatch:
    def __init__(self):
        self.current_job_return = None
        self.run_task_calls = []
        self.run_task_return = {"ok": True, "brand": "delta_cuisine", "result": "Done."}
        self.known_brands = {
            "delta_cuisine": ["delta cuisine", "delta", "dc"],
            "ceram_quartz": ["ceram quartz", "ceram", "quartz"],
        }

    def current_job(self):
        return self.current_job_return

    def resolve_brand(self, text, cfg=None):
        t = (text or "").lower()
        hits = [k for k, names in self.known_brands.items() if any(n in t for n in names)]
        hits = list(dict.fromkeys(hits))
        return hits[0] if len(hits) == 1 else None

    def load_brands(self):
        return {"brands": {k: {} for k in self.known_brands}}

    def run_task(self, text, task_type="weekly", brand_key=None, **kw):
        self.run_task_calls.append({"text": text, "task_type": task_type, "brand_key": brand_key})
        return dict(self.run_task_return)


# --- Fake hermes_dispatch -----------------------------------------------------
class _FakeHermesDispatch:
    """`handle()` mirrors the real contract but is driven by test-set fields
    instead of a real Hermes classification / era_dispatch call."""

    def __init__(self, fake_ed: _FakeEraDispatch):
        self._ed = fake_ed
        self.classify_is_cm = False
        self.dispatch_result = {"ok": True, "brand": "delta_cuisine", "result": "Done."}
        self.calls = []

    def handle(self, text, *, task_type="weekly", allowed_tools=None, dispatch=True, timeout=1800):
        self.calls.append({"text": text, "dispatch": dispatch, "task_type": task_type})
        base = {
            "handled": self.classify_is_cm,
            "category": "cm" if self.classify_is_cm else "assistant",
            "decision": {"category": "cm" if self.classify_is_cm else "assistant"},
        }
        if not self.classify_is_cm:
            return base
        if not dispatch:
            return {**base, "ok": True, "dispatched": False, "brand": None}
        result = dict(self.dispatch_result)
        return {**base, "dispatched": True, **result}


@pytest.fixture()
def fakes(monkeypatch):
    fake_ed = _FakeEraDispatch()
    fake_hd_module = types.ModuleType("hermes_dispatch")
    fake_hd = _FakeHermesDispatch(fake_ed)
    fake_hd_module.handle = fake_hd.handle

    fake_ed_module = types.ModuleType("era_dispatch")
    fake_ed_module.current_job = fake_ed.current_job
    fake_ed_module.resolve_brand = fake_ed.resolve_brand
    fake_ed_module.load_brands = fake_ed.load_brands
    fake_ed_module.run_task = fake_ed.run_task

    monkeypatch.setitem(sys.modules, "era_dispatch", fake_ed_module)
    monkeypatch.setitem(sys.modules, "hermes_dispatch", fake_hd_module)
    return types.SimpleNamespace(ed=fake_ed, hd=fake_hd)


@pytest.fixture(scope="module")
def main_module():
    # era_dispatch/hermes_dispatch are imported lazily INSIDE the methods under
    # test (matching main.py's existing defensive style), so this plain import
    # is safe before the fakes exist — the fakes are looked up at call time.
    import main

    return main


@pytest.fixture()
def era(main_module, fakes):
    """A bare EraLive-shaped stub carrying only the state/collaborators the
    three methods under test touch — avoids running EraLive.__init__ (mic,
    Vosk, file-index warm-up, ...), which this wiring test has no business
    exercising."""

    class _FakeSession:
        def __init__(self):
            self.sent = []

        async def send_realtime_input(self, text=None, **kw):
            self.sent.append(text)

    # Inherit the real routing mixin rather than stubbing it. _on_text_command
    # now consults should_dispatch() before the CM path, and that gate reads a
    # property (model_override) as well as a method — so a bare object would
    # need a hand-written fake of both, which is exactly the kind of drift this
    # fixture's "bind the real unbound methods" policy exists to prevent. With
    # the mixin attached the test exercises the actual gate: dispatch ships OFF,
    # so it declines and the CM flow proceeds untouched.
    from app.era_live.dispatch import DispatchMixin
    from app.era_live.model_route import ModelRouteMixin

    class _Stub(ModelRouteMixin, DispatchMixin):
        pass

    stub = _Stub()
    stub._pending_cm_ask = None
    stub._cm_handoff_until = 0.0
    stub._background_tasks = set()
    stub.session = _FakeSession()
    stub.spoken = []
    stub.speak = lambda text: stub.spoken.append(text)

    # Collaborators the CM path picked up after this stub was first written.
    # A CM report that finishes with no live session is HELD for the next
    # reconnect rather than dropped, so the flow needs somewhere to park it
    # and a log sink; the safety gate now sits on the hermes route itself,
    # not only behind _on_text_command.
    from core.input_safety_gate import InputSafetyGate

    stub.safety_gate = InputSafetyGate()
    stub._pending_cm_announcement = None
    # A live loop + session is what "connected" means to the CM report path
    # (_loop None means every finished report gets HELD for the next reconnect
    # instead of spoken). There is no running loop at fixture-build time, so
    # the drivers bind it: _run_and_drain and _wire_text_command_entrypoint.
    stub._loop = None
    stub.ui = types.SimpleNamespace(write_log=lambda *a, **k: None)
    # Every entry point that accepts a full user utterance must funnel through
    # _note_user_utterance — it feeds both tool routing and the save_memory
    # authorization window. Bind the real one so the stub cannot drift back
    # into assigning _last_user_text raw.
    stub._last_user_text = None
    stub._recent_user_texts = []

    # The typed path asks the gate to judge input WITH context — an action
    # awaiting a yes/no, and whether Era has a question outstanding — so both
    # sources have to exist wherever the route runs, not only behind
    # _on_text_command. Defaults are "nothing pending, nothing asked", i.e. the
    # strictest reading, so these tests still exercise the real guards.
    stub._pending_store = types.SimpleNamespace(is_pending=lambda: False)
    stub.session_manager = types.SimpleNamespace(get_state_snapshot=lambda: {})
    stub._awaiting_reply_until = 0.0
    # No Telegram order is in flight in these tests; _tg_ack must stay a no-op.
    stub._tg_reply_pending = False

    # Typed intercepts that run BEFORE the pending-ask branch these tests
    # target. Each one is real, not silenced: the point of the fixture is that a
    # brand reply reaches the CM path THROUGH the live gauntlet, so if a future
    # intercept ever started claiming "Delta Cuisine" these tests must fail.
    #
    #   _is_reboot_order  — "reboot Era One" is checked first of all, before any
    #                       session is needed. Self-contained; bind the real one.
    #   turn_memory       — the typed "continue"/"never mind" intercept consults
    #                       it. A real, EMPTY store answers has_pending() False
    #                       honestly, where a lambda would hard-code the answer.
    #   _standby_active   — read further down the same method.
    #
    # Deliberately NOT stubbed: _shutdown_era and _apply_voice_switch. Nothing
    # these tests type should ever reach them, so their ABSENCE is the assertion
    # — an intercept that started matching a brand name would raise here instead
    # of silently rebooting Era or switching her voice mid-suite.
    from core.turn_memory import TurnMemory

    stub.turn_memory = TurnMemory()
    stub._standby_active = False
    stub._is_reboot_order = main_module.EraLive._is_reboot_order.__get__(stub)
    #   _is_abort_order — "abort mission" outranks even the reboot order. The
    #       real matcher runs; trigger_abort is intentionally absent, like
    #       _shutdown_era, so a brand reply that tripped it would raise here.
    stub._is_abort_order = main_module.EraLive._is_abort_order.__get__(stub)
    #   _handle_typed_mic_order — "mute your mic" / "start listening", checked
    #       before the session gate so muting works with Era offline. Returns
    #       False without touching any state for anything that is not a mic
    #       order, so the real one runs here on its real regexes. Its
    #       collaborators (mute_microphone, _say_mic_ack) are intentionally
    #       absent for the same reason as _shutdown_era above.
    stub._handle_typed_mic_order = main_module.EraLive._handle_typed_mic_order.__get__(stub)

    # Bind the real (unmodified) unbound methods from EraLive onto the stub.
    stub._consume_pending_cm_ask = main_module.EraLive._consume_pending_cm_ask.__get__(stub)
    stub._route_text_through_hermes = main_module.EraLive._route_text_through_hermes.__get__(stub)
    stub._run_cm_flow = main_module.EraLive._run_cm_flow.__get__(stub)
    stub._on_text_command = main_module.EraLive._on_text_command.__get__(stub)
    stub._arm_cm_handoff = main_module.EraLive._arm_cm_handoff.__get__(stub)
    stub._cm_handoff_active = main_module.EraLive._cm_handoff_active.__get__(stub)
    stub._note_user_utterance = main_module.EraLive._note_user_utterance.__get__(stub)
    stub._deliver_or_queue_cm_report = main_module.EraLive._deliver_or_queue_cm_report.__get__(stub)
    stub._text_state_snapshot = main_module.EraLive._text_state_snapshot.__get__(stub)
    stub._awaiting_reply_active = main_module.EraLive._awaiting_reply_active.__get__(stub)
    stub._note_era_question = main_module.EraLive._note_era_question.__get__(stub)
    stub._tg_ack = main_module.EraLive._tg_ack.__get__(stub)
    return stub


def _wire_text_command_entrypoint(era, main_module, monkeypatch):
    """Give the stub the extra collaborators _on_text_command touches (safety
    gate, session_manager, ui, loop) and reroute run_coroutine_threadsafe so any
    CM job _on_text_command hands off runs on THIS test loop instead of a
    cross-thread handoff. Uses the REAL InputSafetyGate on purpose — that is the
    layer the precedence bug lived behind."""
    from core.input_safety_gate import InputSafetyGate

    era.safety_gate = InputSafetyGate()
    era._standby_active = False
    era._last_user_text = None
    era._last_interaction_timestamp = 0.0
    # _on_text_command reads self._pending_store.is_pending() to flag an awaiting
    # confirmation for the safety gate; the stub has no real confirmation store.
    era._pending_store = types.SimpleNamespace(is_pending=lambda: False)
    era.session_manager = types.SimpleNamespace(get_state_snapshot=lambda: {})
    era.ui = types.SimpleNamespace(write_log=lambda *a, **k: None)
    era._loop = asyncio.get_event_loop()
    # Collaborators added to _on_text_command after this stub was written: the
    # resume/drop intercept, the routing shadow and the Claude dispatch check.
    era.turn_memory = types.SimpleNamespace(has_pending=lambda: False)
    era.observe_routing = lambda *a, **k: None
    era.should_dispatch = lambda text: None
    era._maybe_record_feedback = lambda text: None

    scheduled = []
    def schedule(coro, loop):
        task = asyncio.ensure_future(coro)
        scheduled.append(task)
        return task

    monkeypatch.setattr(
        main_module.asyncio,
        "run_coroutine_threadsafe",
        schedule,
    )
    return scheduled


async def _run_and_drain(era, text):
    """Runs the route coroutine, then lets any background task it spawned via
    asyncio.create_task (the real, non-TaskGroup dispatch task) finish too."""
    # _make_stub runs outside any loop, so it cannot capture one. Bind the real
    # running loop here: a CM report only gets SPOKEN when a loop and session
    # are live, and "connected" is the state these tests describe.
    era._loop = asyncio.get_running_loop()
    await era._route_text_through_hermes(text)
    if era._background_tasks:
        await asyncio.gather(*list(era._background_tasks))


# --------------------------------------------------------------------------- #
# 1. Non-CM text: zero behavior change, falls straight through to Gemini.
# --------------------------------------------------------------------------- #
async def test_non_cm_text_falls_through_unchanged(era, fakes):
    fakes.hd.classify_is_cm = False
    await _run_and_drain(era, "what's the weather today")

    assert era.session.sent == ["what's the weather today"]
    assert era.spoken == []
    assert fakes.ed.run_task_calls == []


# --------------------------------------------------------------------------- #
# 2. CM text, unambiguous brand: dispatches, speaks the result, never reaches
#    Gemini.
# --------------------------------------------------------------------------- #
async def test_cm_text_with_brand_dispatches_and_speaks_result(era, fakes):
    fakes.hd.classify_is_cm = True
    fakes.hd.dispatch_result = {"ok": True, "brand": "delta_cuisine", "result": "Plan is ready."}

    await _run_and_drain(era, "prepare this week's content for Delta Cuisine")

    assert era.session.sent == []  # never handed to Gemini
    assert era.spoken == ["Plan is ready."]
    assert (
        fakes.ed.run_task_calls == []
    )  # went through hermes_dispatch.handle, not a direct run_task call
    assert era._pending_cm_ask is None


# --------------------------------------------------------------------------- #
# 3. CM text, ambiguous brand: asks "which brand?", then a bare brand-name
#    reply auto-combines with the stored instruction and re-dispatches, echoing
#    what it's about to do first.
# --------------------------------------------------------------------------- #
async def test_ambiguous_brand_then_bare_brand_reply_redispatches(era, fakes):
    fakes.hd.classify_is_cm = True
    fakes.hd.dispatch_result = {"ok": False, "need_brand": True, "message": "which brand?"}

    await _run_and_drain(era, "publish the approved carousel")

    assert len(era.spoken) == 1 and "Which brand is this for?" in era.spoken[0]
    assert era._pending_cm_ask is not None
    assert era._pending_cm_ask["text"] == "publish the approved carousel"
    era.spoken.clear()

    # Bare brand-name reply — resolves via era_dispatch.resolve_brand and goes
    # straight to a direct run_task call (bypassing hermes_dispatch.handle,
    # since the brand is already known — no need to re-classify).
    await _run_and_drain(era, "Delta Cuisine")

    assert era._pending_cm_ask is None  # one-shot: cleared
    # task_type is "publish" (not "weekly"): _infer_cm_task_type maps the "publish"
    # verb to the publish lane so the re-dispatch carries the Composio allowlist.
    assert fakes.ed.run_task_calls == [
        {
            "text": "publish the approved carousel",
            "task_type": "publish",
            "brand_key": "delta_cuisine",
        }
    ]
    assert era.spoken[0].startswith(
        'Got it — running "publish the approved carousel" for delta cuisine'
    )
    assert era.session.sent == []  # still never reached Gemini


async def test_pending_ask_drops_silently_on_unrelated_new_sentence(era, fakes):
    fakes.hd.classify_is_cm = True
    fakes.hd.dispatch_result = {"ok": False, "need_brand": True, "message": "which brand?"}
    await _run_and_drain(era, "publish the approved carousel")
    era.spoken.clear()

    # A genuinely new, unrelated sentence -> topic change, pending ask dropped
    # silently, and (since it isn't CM) it falls through to Gemini normally.
    fakes.hd.classify_is_cm = False
    await _run_and_drain(era, "what time is it right now")

    assert era._pending_cm_ask is None
    assert era.session.sent == ["what time is it right now"]
    assert fakes.ed.run_task_calls == []


# --------------------------------------------------------------------------- #
# 4. CM text while another brand's job is already running: speaks the
#    "queued behind" line before the dispatch result comes back.
# --------------------------------------------------------------------------- #
async def test_busy_job_speaks_queued_line_before_result(era, fakes):
    fakes.ed.current_job_return = {"brand": "ceram_quartz", "task_type": "weekly"}
    fakes.hd.classify_is_cm = True
    fakes.hd.dispatch_result = {"ok": True, "brand": "delta_cuisine", "result": "Plan is ready."}

    await _run_and_drain(era, "prepare this week's content for Delta Cuisine")

    assert (
        era.spoken[0]
        == "ceram_quartz's weekly is running right now, sir; yours will start right after."
    )
    assert era.spoken[-1] == "Plan is ready."


# --------------------------------------------------------------------------- #
# 5. PRECEDENCE regression: driven through the REAL entry point (_on_text_command)
#    with the REAL InputSafetyGate. A bare brand-name reply that the gate would
#    otherwise swallow as SHORT_UNCLEAR must be consumed by the pending CM ask
#    FIRST and re-dispatch — never clarified, never handed to Gemini.
# --------------------------------------------------------------------------- #
async def test_pending_ask_beats_safety_gate_on_short_brand_reply(
    era, fakes, main_module, monkeypatch
):
    scheduled = _wire_text_command_entrypoint(era, main_module, monkeypatch)

    # An outstanding "which brand?" ask, well within its 45s window.
    era._pending_cm_ask = {
        "text": "prepare this week's content plan",
        "task_type": "weekly",
        "expires_at": time.time() + 45,
    }

    # 2 words -> the gate once returned ASK_CLARIFICATION (SHORT_UNCLEAR) and
    # bailed before the pending check ever ran.
    era._on_text_command("Delta Cuisine")
    await asyncio.gather(*scheduled)

    assert era._pending_cm_ask is None  # one-shot: consumed
    assert fakes.ed.run_task_calls == [
        {
            "text": "prepare this week's content plan",
            "task_type": "weekly",
            "brand_key": "delta_cuisine",
        }
    ]
    assert era.spoken[0].startswith(
        'Got it — running "prepare this week\'s content plan" for delta cuisine'
    )
    assert era.session.sent == []  # never reached Gemini
    assert not any("didn't catch" in s for s in era.spoken)  # not clarified


# --------------------------------------------------------------------------- #
# 6. Fall-through guard: with a pending ask active, a reply that does NOT resolve
#    to a brand is consumed (one-shot) and then handled by the normal safety gate
#    — proving the fix doesn't blanket-swallow every reply during the window.
# --------------------------------------------------------------------------- #
async def test_pending_ask_nonbrand_reply_falls_through_to_safety_gate(
    era, fakes, main_module, monkeypatch
):
    scheduled = _wire_text_command_entrypoint(era, main_module, monkeypatch)

    era._pending_cm_ask = {
        "text": "prepare this week's content plan",
        "task_type": "weekly",
        "expires_at": time.time() + 45,
    }

    # "the weather" resolves to no brand -> pending consumed, then it's a normal
    # standalone input -> safety gate + Hermes, which does not claim it.
    era._on_text_command("the weather")
    await asyncio.gather(*scheduled)

    assert era._pending_cm_ask is None  # one-shot: consumed
    assert fakes.ed.run_task_calls == []  # no CM dispatch
    # Not re-dispatched as the CM order. It reaches Gemini as the user's own
    # words, exactly as the same phrase spoken would — no "this is DATA, do not
    # execute" wrapper, which is what made typed orders answer with a question.
    assert era.session.sent == ["the weather"]


# --------------------------------------------------------------------------- #
# 7. Brand-less CM entry: a clearly-CM phrase with NO brand named ("prepare this
#    week's content plan"), classified cm by Hermes, must enter the SAME
#    ambiguous-brand pending-ask flow as the brand-named case — driven through
#    the real entry point (_on_text_command), never handed to Gemini. This is the
#    typed-case gap from the live test (Gemini used to pick agent_task instead).
# --------------------------------------------------------------------------- #
async def test_brandless_cm_phrase_enters_pending_ask_flow(era, fakes, main_module, monkeypatch):
    scheduled = _wire_text_command_entrypoint(era, main_module, monkeypatch)

    # Hermes classifies it as CM; era_dispatch can't resolve a brand -> need_brand.
    fakes.hd.classify_is_cm = True
    fakes.hd.dispatch_result = {"ok": False, "need_brand": True, "message": "which brand?"}

    era._on_text_command("prepare this week's content plan")  # no brand named
    await asyncio.gather(*scheduled)
    if era._background_tasks:
        await asyncio.gather(*list(era._background_tasks))

    # Entered the pending-ask flow instead of falling through to Gemini.
    assert era._pending_cm_ask is not None
    assert era._pending_cm_ask["text"] == "prepare this week's content plan"
    assert any("Which brand is this for?" in s for s in era.spoken)
    assert era.session.sent == []  # never reached Gemini
    assert fakes.ed.run_task_calls == []  # need_brand -> no run yet


# --------------------------------------------------------------------------- #
# The gauntlet in front of the pending-ask branch.
#
# WHY THIS TEST EXISTS
#   _on_text_command runs a row of typed intercepts BEFORE the pending-ask
#   branch — reboot, voice switch, model override, contextual resume, dispatch,
#   standby, mic, theme. Every one of them gets first refusal on a brand reply.
#
#   The three tests above already fail loudly if an intercept claims one: they
#   assert run_task_calls, so an early `return` empties it. But they fail with a
#   confusing symptom (an empty CM call) several layers from the cause. This
#   pins the cause directly, and it is the guard the fixture's own stale-stub
#   history asks for — a missing stub attribute once made the whole row
#   unreachable, and the suite reported an AttributeError rather than the fact
#   that the gauntlet had stopped being exercised at all.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reply", ["Delta Cuisine", "Ceram Quartz", "the weather"])
def test_typed_intercepts_decline_a_brand_reply(era, reply):
    """None of the pre-branch intercepts may claim a brand-name answer."""
    assert era._is_reboot_order(reply) is False
    assert era._is_abort_order(reply) is False
    assert era._handle_typed_mic_order(reply) is False
    assert era.turn_memory.has_pending() is False
    assert era.should_dispatch(reply) is None
    assert era._standby_active is False
