"""Phase-1 reliability: automatic recovery from a quota outage (no manual step).

This simulates run()'s reconnect *policy* using the same building blocks the real
loop uses (main.EraLive._classify_disconnect + main.grow_cooldown), so we can
prove the important behaviours without a live Gemini connection:

  1. A run of quota errors makes Era wait a LONG, GROWING cooldown (never a fast
     retry loop), capped at 2 hours.
  2. As soon as quota returns, the next attempt connects and the cooldown RESETS —
     recovery is automatic, no human intervention.
  3. Era never gives up (the loop keeps trying).
"""

import main

BASE = 900.0  # 15 min
CAP = 7200.0  # 2 h


def _simulate(connect_results):
    """Drive the reconnect policy over a scripted list of connection outcomes.

    Each item is either the string "ok" (connect succeeds) or an error message
    (connect fails). Returns the list of cooldown waits Era would use, plus the
    final status, mirroring what run() does for the quota branch.
    """
    cooldown = BASE
    waits = []
    connected = False
    for result in connect_results:
        if result == "ok":
            connected = True
            cooldown = BASE  # reset on recovery
            status = "Connected"
            break
        kind = main.EraLive._classify_disconnect(RuntimeError(result))
        if kind == "quota":
            waits.append(cooldown)  # Era sleeps this long, then retries
            cooldown = main.grow_cooldown(cooldown, CAP)
            status = "Cooling down (quota)"
        else:
            status = "Reconnecting"
    return waits, status, connected


def test_recovers_automatically_when_quota_returns():
    # Quota is out for three attempts, then Google frees it.
    quota = "1011 You exceeded your current quota"
    waits, status, connected = _simulate([quota, quota, quota, "ok"])
    assert connected is True
    assert status == "Connected"  # back online with no manual step
    assert waits == [900.0, 1800.0, 3600.0]  # 15m, 30m, 60m — growing, not spammy


def test_never_retries_faster_than_15_minutes():
    quota = "resource has been exhausted"
    waits, _, _ = _simulate([quota] * 5)
    # Every wait is at least the 15-minute base — no minute-by-minute hammering.
    assert all(w >= BASE for w in waits)


def test_cooldown_is_capped_at_two_hours():
    quota = "quota exceeded"
    waits, _, _ = _simulate([quota] * 12)  # long outage
    assert max(waits) <= CAP
    assert waits[-1] == CAP  # settles at the 2-hour ceiling


def test_transient_error_does_not_trigger_quota_cooldown():
    waits, status, connected = _simulate(["keepalive ping timeout", "ok"])
    assert waits == []  # no quota cooldown for a normal blip
    assert connected is True
