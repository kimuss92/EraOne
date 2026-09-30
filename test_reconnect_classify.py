"""Phase-1 reliability: Gemini disconnect classification + quota-cooldown growth.

These are the decisions that stop the reconnect deadlock:
  - a quota (1011) error must be recognised as "quota" (long cooldown), not a
    generic blip that hammers the API and then wedges standby;
  - the routine 1008 rotation stays "rotation";
  - a bad API key is "auth";
  - the cooldown grows (doubles) but is capped, then resets on recovery.

All pure-logic — no network, no real Gemini calls.
"""

import main


def _classify(text: str) -> str:
    return main.EraLive._classify_disconnect(RuntimeError(text))


def test_quota_message_is_quota():
    assert _classify("1011 You exceeded your current quota, please check your plan") == "quota"
    assert _classify("Resource has been exhausted (e.g. check quota).") == "quota"
    assert _classify("RESOURCE_EXHAUSTED") == "quota"
    assert _classify("HTTP 429 Too Many Requests") == "quota"


def test_routine_rotation_is_rotation():
    assert _classify("1008 policy violation: The operation was aborted") == "rotation"


def test_bad_key_is_auth():
    assert _classify("1007 API key not valid. Please pass a valid API key.") == "auth"
    assert _classify("PERMISSION_DENIED") == "auth"
    assert _classify("UNAUTHENTICATED") == "auth"


def test_other_errors_are_transient():
    assert _classify("keepalive ping timeout") == "transient"
    assert _classify("The service is currently unavailable") == "transient"
    assert _classify("Connection reset by peer") == "transient"


def test_quota_wins_when_nested_in_exceptiongroup():
    # Gemini's close reason arrives wrapped in a TaskGroup ExceptionGroup; the
    # classifier must dig into the leaves to find it.
    inner = RuntimeError("1011 You exceeded your current quota")
    try:
        group = ExceptionGroup("task group failed", [inner])  # noqa: F821 (py3.11+)
    except NameError:
        # Python < 3.11 fallback: chain via __cause__, which _flatten_exc follows.
        group = RuntimeError("wrapper")
        group.__cause__ = inner
    assert main.EraLive._classify_disconnect(group) == "quota"


def test_cooldown_grows_and_caps():
    base = 900.0  # 15 min
    cap = 7200.0  # 2 h
    seq = [base]
    for _ in range(6):
        seq.append(main.grow_cooldown(seq[-1], cap))
    # 900 -> 1800 -> 3600 -> 7200 -> capped at 7200 thereafter
    assert seq[1] == 1800.0
    assert seq[2] == 3600.0
    assert seq[3] == 7200.0
    assert all(v <= cap for v in seq)
    assert seq[-1] == cap  # never exceeds the 2-hour ceiling
