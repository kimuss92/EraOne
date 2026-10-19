"""Location evidence and proactive-call policy, without real Telegram traffic."""

import json
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from app.away_calls import AwayCalls
from core.home_presence import HomePresence


@pytest.fixture
def presence(tmp_path):
    clock = [datetime(2026, 10, 14, 12, tzinfo=ZoneInfo("Africa/Lagos")).timestamp()]
    service = HomePresence(path=tmp_path / "home.json", clock=lambda: clock[0])
    return service, clock


def message(
    clock,
    *,
    text="",
    lat=0,
    lon=0,
    live=None,
    accuracy=5,
    mid=2,
    date=None,
    edited=False,
):
    msg = {
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42},
        "date": date or clock[0],
        "message_id": mid,
    }
    if text:
        msg["text"] = text
    else:
        msg["location"] = {
            "latitude": lat,
            "longitude": lon,
            "horizontal_accuracy": accuracy,
        }
        if live is not None:
            msg["location"]["live_period"] = live
    if edited:
        msg["edit_date"] = clock[0]
    return msg


def sethome(service, clock):
    assert service.handle(message(clock, text="/sethome"), "42")[0]
    assert service.handle(message(clock, mid=1), "42")[0]
    assert service.state() == "home" and service.enabled


def leave(service, clock):
    origin = clock[0]
    service.handle(message(clock, lon=0.01, live=3600, date=origin), "42")
    assert not service.permitted()
    clock[0] += 31
    service.handle(message(clock, lon=0.01, live=3600, date=origin, edited=True), "42")
    assert service.permitted()
    return origin


def test_secure_home_restart_forgets_phone_evidence(presence):
    service, clock = presence
    sethome(service, clock)
    leave(service, clock)
    stored = json.loads(service.path.read_text())
    assert isinstance(stored["home_presence_secret"], dict)
    restarted = HomePresence(path=service.path, clock=lambda: clock[0])
    assert restarted.owner_id == 42 and restarted.enabled
    assert restarted.state() == "unknown" and not restarted.permitted()


def test_outside_requires_distinct_stable_updates_and_home_cancels(presence):
    service, clock = presence
    sethome(service, clock)
    origin = leave(service, clock)
    clock[0] += 1
    service.handle(message(clock, live=3600, date=origin, edited=True), "42")
    assert service.state() == "home" and not service.permitted()


@pytest.mark.parametrize("mode", ["stale", "expired", "stopped", "poor_accuracy", "boundary"])
def test_uncertain_location_blocks_calls(presence, mode):
    service, clock = presence
    sethome(service, clock)
    origin = leave(service, clock)
    clock[0] += 1
    if mode == "stale":
        clock[0] += 301
    elif mode == "expired":
        clock[0] += 3600
    elif mode == "stopped":
        service.handle(message(clock, lon=0.01, date=origin, edited=True), "42")
    elif mode == "poor_accuracy":
        service.handle(
            message(clock, lon=0.01, live=3600, accuracy=500, date=origin, edited=True),
            "42",
        )
    else:
        service.handle(message(clock, lon=0.0018, live=3600, date=origin, edited=True), "42")
    assert not service.permitted()


@pytest.mark.parametrize("change", ["foreign_sender", "foreign_chat", "group", "forward"])
def test_location_owner_filter(presence, change):
    service, clock = presence
    sethome(service, clock)
    data = message(clock, lon=0.01, live=3600)
    if change == "foreign_sender":
        data["from"]["id"] = 43
    elif change == "foreign_chat":
        data["chat"]["id"] = 43
    elif change == "group":
        data["chat"]["type"] = "group"
    else:
        data["forward_origin"] = {"type": "user"}
    service.handle(data, "42")
    assert service.state() == "home"


def test_old_or_duplicate_live_updates_do_not_confirm_departure(presence):
    service, clock = presence
    sethome(service, clock)
    origin = clock[0]
    data = message(clock, lon=0.01, live=3600)
    service.handle(data, "42")
    clock[0] += 35
    service.handle(data, "42")
    assert not service.permitted()
    service.handle(message(clock, lon=0.01, live=3600, date=origin - 1, mid=1, edited=True), "42")
    assert not service.permitted()


def test_disable_forget_and_expired_home_setup(presence):
    service, clock = presence
    sethome(service, clock)
    leave(service, clock)
    service.handle(message(clock, text="/autocalls off"), "42")
    assert not service.permitted()
    service.handle(message(clock, text="/forgethome"), "42")
    assert service.home is None and not service.path.exists()
    service.handle(message(clock, text="/sethome"), "42")
    clock[0] += 301
    service.handle(message(clock), "42")
    assert service.home is None


def notifier(presence):
    service, clock = presence
    sethome(service, clock)
    call = Mock(owner_id=42)
    call.generation = "a" * 32
    call.snapshot.return_value = {"canDial": True}
    return (
        AwayCalls(
            call,
            service,
            path=service.path.with_name("budget.json"),
            clock=lambda: clock[0],
        ),
        call,
        service,
        clock,
    )


@pytest.mark.parametrize(
    "state", ["home", "unknown", "busy", "quiet", "different_owner", "disabled"]
)
def test_proactive_calls_blocked_without_all_conditions(presence, state):
    policy, call, service, clock = notifier(presence)
    if state != "home":
        leave(service, clock)
    if state == "unknown":
        clock[0] += 301
    elif state == "busy":
        call.snapshot.return_value = {"canDial": False}
    elif state == "quiet":
        clock[0] = datetime(2026, 10, 14, 23, tzinfo=ZoneInfo("Africa/Lagos")).timestamp()
        service._sample_at = clock[0]
        service._expires = clock[0] + 3600
    elif state == "different_owner":
        call.owner_id = 43
    elif state == "disabled":
        service.enabled = False
    assert not policy.offer("Task complete", "background")
    call.request_call_owner.assert_not_called()


def test_away_call_reason_dedupe_cooldown_survive_restart(presence):
    policy, call, service, clock = notifier(presence)
    leave(service, clock)
    assert policy.offer("Your background job finished.", "background", "job-1")
    args = call.request_call_owner.call_args.kwargs
    assert args["notification"] == "Your background job finished." and args["permitted"]()
    assert not policy.offer("Another task finished", "background", "job-2")
    clock[0] += 901
    service._sample_at, service._expires = clock[0], clock[0] + 3600
    restarted = AwayCalls(call, service, path=policy.path, clock=lambda: clock[0])
    assert not restarted.offer("Changed wording same event", "background", "job-1")
    assert restarted.offer("Another task finished", "background", "job-2")
    service._state = "home"
    assert not args["permitted"]()


def test_persistence_failure_prevents_dial(presence, monkeypatch):
    policy, call, service, clock = notifier(presence)
    leave(service, clock)
    monkeypatch.setattr("app.away_calls.write_config", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(OSError):
        policy.offer("Task complete", "background")
    call.request_call_owner.assert_not_called()


def test_daily_budget_blocks_seventh_attempt(presence):
    policy, call, service, clock = notifier(presence)
    leave(service, clock)
    for index in range(6):
        if index:
            clock[0] += 901
        service._sample_at, service._expires = clock[0], clock[0] + 3600
        assert policy.offer(f"Job {index} done.", "background", f"job-{index}")
    clock[0] += 901
    service._sample_at, service._expires = clock[0], clock[0] + 3600
    assert not policy.offer("Job seven done.", "background", "job-7")
    assert call.request_call_owner.call_count == 6


def test_bot_poller_routes_authorized_location_and_edits_without_model_commands(
    presence, monkeypatch
):
    import threading

    import era_telegram

    service, clock = presence
    sethome(service, clock)
    origin = clock[0]
    clock[0] += 31
    initial = message([origin], lon=0.01, live=3600, date=origin)
    edited = message(clock, lon=0.01, live=3600, date=origin, edited=True)
    foreign = message(clock, lon=0, live=3600, mid=3)
    foreign["from"]["id"] = 43
    stop = threading.Event()
    response = Mock(status_code=200)
    response.json.return_value = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": initial},
            {"update_id": 2, "edited_message": edited},
            {"update_id": 3, "message": foreign},
            {"update_id": 4, "message": message(clock, text="open Brave")},
        ],
    }
    session = Mock()

    def get(*args, **kwargs):
        stop.set()
        return response

    session.get.side_effect = get
    monkeypatch.setattr(era_telegram, "enabled", lambda: True)
    monkeypatch.setattr(era_telegram, "preflight", lambda: {})
    monkeypatch.setattr(era_telegram, "_chat_id", lambda: "42")
    monkeypatch.setattr(era_telegram, "_url", lambda method: "unused")
    monkeypatch.setattr(era_telegram, "_load_offset", lambda: 0)
    monkeypatch.setattr(era_telegram, "_save_offset", Mock())
    monkeypatch.setattr(era_telegram, "send_message", Mock())
    monkeypatch.setattr(era_telegram.requests, "Session", lambda: session)
    monkeypatch.setattr("core.home_presence.get_home_presence", lambda: service)
    commands = []
    era_telegram.poll_forever(commands.append, stop_event=stop)
    assert commands == ["open Brave"]
    assert service.permitted()
    era_telegram._save_offset.assert_called_once_with(5)


def test_malformed_owner_location_invalidates_away_evidence(presence):
    service, clock = presence
    sethome(service, clock)
    leave(service, clock)
    data = message(clock, lat=float("nan"), lon=0.01, live=3600, edited=True)
    with pytest.raises(ValueError):
        service.handle(data, "42")
    assert not service.permitted()


def test_late_stop_edit_cannot_override_newer_away_update(presence):
    service, clock = presence
    sethome(service, clock)
    origin = leave(service, clock)
    old_clock = [origin + 1]
    service.handle(message(old_clock, date=origin, edited=True), "42")
    assert service.permitted()


@pytest.mark.parametrize(
    "text,state",
    [
        ("Era, je quitte la maison", "away"),
        ("Je sors.", "away"),
        ("Era, I'm leaving home", "away"),
        ("Je suis rentré", "home"),
        ("I'm back home", "home"),
        ("/home", "home"),
        ("/away", "away"),
    ],
)
def test_manual_presence_command_persists_without_coordinates(presence, text, state):
    service, clock = presence
    sethome(service, clock)
    assert service.command(text, 42)
    assert service.state() == state and service.permitted() == (state == "away")
    restarted = HomePresence(path=service.path, clock=lambda: clock[0])
    assert restarted.mode == "manual" and restarted.home is None
    assert restarted.state() == state
    from core.protected_config import read_config

    payload = json.loads(read_config(service.path)["home_presence_secret"])
    assert "latitude" not in payload and "longitude" not in payload


@pytest.mark.parametrize(
    "text",
    [
        "I'm not leaving home",
        "tell me how to say je quitte la maison",
        "je quitte la maison demain",
        "he said I'm leaving home",
        "I'm back home tomorrow",
    ],
)
def test_presence_negations_explanations_and_future_not_commands(presence, text):
    service, clock = presence
    sethome(service, clock)
    assert service.command(text, 42) is None
    assert service.mode == "location" and service.state() == "home"


def test_manual_expiry_and_home_override_ignore_location(presence):
    service, clock = presence
    service.use_manual(42, "away")
    clock[0] += 28801
    assert service.state() == "unknown" and not service.permitted()
    service.use_manual(42, "away")
    service.command("je suis rentre", 42)
    service.handle(message(clock, lon=0.01, live=3600), "42")
    assert service.state() == "home" and not service.permitted()
    assert "Manual mode" in service.handle(message(clock, text="/presence"), "42")[1]


def test_manual_away_calls_no_home_point_required(presence):
    service, clock = presence
    service.use_manual(42, "away")
    call = Mock(owner_id=42)
    call.generation = "a" * 32
    call.snapshot.return_value = {"canDial": True}
    policy = AwayCalls(
        call,
        service,
        path=service.path.with_name("budget.json"),
        clock=lambda: clock[0],
    )
    assert policy.offer("Job complete", "background")
    service.use_manual(42, "home")
    assert not call.request_call_owner.call_args.kwargs["permitted"]()


def test_manual_commands_route_through_owner_bot_without_location(presence):
    service, clock = presence
    reply = service.handle(message(clock, text="Era, je quitte la maison"), "42")[1]
    assert "eight hours" in reply and service.permitted()
    assert "home" in service.handle(message(clock, text="/home"), "42")[1].lower()
    assert not service.permitted()
    service.handle(message(clock, text="/autocalls off"), "42")
    assert not service.enabled


def test_local_presence_handler_uses_independently_verified_owner(presence, monkeypatch):
    from app.manual_presence import handle_presence_order

    service, _ = presence
    monkeypatch.setattr("app.manual_presence.get_home_presence", lambda: service)
    settings = dict(
        owner_user_id=42,
        expected_owner_user_id=42,
        authenticated=True,
        account_authenticated=True,
        owner_bound=True,
    )
    monkeypatch.setattr("app.manual_presence.load_settings", lambda path: settings)
    assert handle_presence_order("Je quitte la maison") and service.permitted()
    settings["expected_owner_user_id"] = 43
    assert "couldn't save" in handle_presence_order("Je suis rentre")
    assert service.permitted()  # A failed change is reported, never claimed as applied.


@pytest.mark.parametrize(
    "text,state",
    [
        ("Hey Era, I'm heading out now, see you later", "away"),
        ("I'm off to work", "away"),
        ("I'm going out shopping", "away"),
        ("I'm stepping outside for a walk", "away"),
        ("Je pars au travail maintenant", "away"),
        ("I just got home", "home"),
        ("I'm back at my place", "home"),
        ("Home again", "home"),
        ("Je viens de rentrer", "home"),
    ],
)
def test_natural_presence_context(presence, text, state):
    service, _ = presence
    assert service.command(text, 42)
    assert service.state() == state


@pytest.mark.parametrize(
    "text",
    [
        "Am I home?",
        "I'm heading out tomorrow",
        "I'm not going out",
        "I'm thinking of going out",
        "I'm going to the kitchen",
        "When I get home tomorrow",
        "I'm leaving work",
        "He said I'm heading out",
    ],
)
def test_ambiguous_and_non_home_context_not_presence_commands(presence, text):
    from core.home_presence import presence_intent

    assert presence_intent(text) is None


def test_pc_return_home_after_grace_and_expiry(presence):
    service, clock = presence
    service.use_manual(42, "away")
    assert not service.local_activity() and service.permitted()
    clock[0] += 31
    assert service.local_activity()
    assert service.state() == "home" and not service.permitted()
    assert not service.local_activity()
    assert HomePresence(path=service.path, clock=lambda: clock[0]).state() == "home"


@pytest.mark.parametrize(
    "kind,msg,flags,expected",
    [
        ("keyboard", 0x100, 0, True),
        ("keyboard", 0x100, 0x10, False),
        ("keyboard", 0x100, 0x2, False),
        ("mouse", 0x201, 0, True),
        ("mouse", 0x201, 1, False),
        ("mouse", 0x201, 2, False),
        ("mouse", 0x200, 0, False),
    ],
)
def test_local_input_ignores_injected_tools_and_mouse_motion(kind, msg, flags, expected):
    from core.local_presence import physical_input

    assert physical_input(kind, msg, flags) == expected
