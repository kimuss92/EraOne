"""Why the Live session dropped — the branch that decides how Era recovers.

The classification is not cosmetic: each kind routes to a different recovery
(immediate reconnect, long cooldown, revert-and-retry, or "go fix your key"),
so a mislabelled drop sends the user after the wrong problem entirely. That is
exactly what happened on 2026-07-29 — a bare "1007" substring match classified a
CONFIG rejection as bad credentials, and Era told the user to fix a
gemini_api_key that was working perfectly.

The strings below are verbatim from a live probe against the real API, not
invented, since the whole failure was a guess about wording that never held.
"""

import pytest

from app.era_live.reconnect_mixin import ReconnectMixin

classify = ReconnectMixin._classify_disconnect


# ── The 2026-07-29 regression ────────────────────────────────────────────────
def test_text_modality_rejection_is_config_not_auth():
    """Verbatim from the live probe. This exact message was reported to the user
    as an invalid API key while the key was valid."""
    exc = Exception(
        "APIError: 1007 None. The requested combination of response modalities "
        "(TEXT) is not supported by the model. models/gemini-3.1-flash-live-preview"
    )
    assert classify(exc) == "config"


def test_unknown_model_is_config_not_rotation():
    """Also verbatim. Carries 1008, which used to read as a routine rotation and
    would have hidden a dead model id behind an endless silent reconnect."""
    exc = Exception(
        "APIError: 1008 None. models/gemini-2.0-flash-live-001 is not found for "
        "API version v1beta, or is not supported for bidiGenerateContent."
    )
    assert classify(exc) == "config"


# ── The kinds that must keep working ─────────────────────────────────────────
def test_real_auth_failure_is_still_auth():
    """Narrowing 1007 must not stop genuine credential errors being caught."""
    for msg in (
        "400 API key not valid. Please pass a valid API key.",
        "PERMISSION_DENIED: caller does not have permission",
        "UNAUTHENTICATED: request had invalid authentication credentials",
        "API_KEY_INVALID",
    ):
        assert classify(Exception(msg)) == "auth", msg


def test_routine_rotation_is_still_rotation():
    for msg in (
        "received 1008 (policy violation) The operation was aborted",
        "The operation was aborted.",
    ):
        assert classify(Exception(msg)) == "rotation", msg


def test_quota_is_still_quota():
    for msg in (
        "1011 The resource has been exhausted",
        "RESOURCE_EXHAUSTED",
        "429 You exceeded your current quota",
    ):
        assert classify(Exception(msg)) == "quota", msg


def test_unrecognized_drop_is_transient():
    assert classify(Exception("connection reset by peer")) == "transient"


# ── Nesting ──────────────────────────────────────────────────────────────────
def test_classification_sees_through_a_taskgroup_group():
    """The connect loop runs under a TaskGroup, so the real cause always arrives
    wrapped — an ExceptionGroup str()s only as its sub-exception count."""
    inner = Exception(
        "APIError: 1007 None. The requested combination of response modalities "
        "(TEXT) is not supported by the model."
    )
    group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    assert classify(group) == "config"


# ── The rotation short-circuit ───────────────────────────────────────────────
# _is_expected_rotation runs AHEAD of _classify_disconnect in the connect loop,
# so a config error it wrongly claims is never classified at all.
def test_rotation_shortcircuit_rejects_a_config_1008():
    exc = Exception(
        "APIError: 1008 None. models/gemini-2.0-flash-live-001 is not found for "
        "API version v1beta, or is not supported for bidiGenerateContent."
    )
    assert ReconnectMixin._is_expected_rotation(exc) is False


def test_rotation_shortcircuit_still_accepts_a_real_rotation():
    exc = Exception("received 1008 (policy violation) The operation was aborted")
    assert ReconnectMixin._is_expected_rotation(exc) is True


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("1011 resource_exhausted", "quota"),
        ("response modalities (TEXT) is not supported", "config"),
        ("api key not valid", "auth"),
    ],
)
def test_rotation_check_precedes_the_rest(msg, expected):
    """Ordering guard: rotation is matched first, so a message that merely
    contains another kind's wording must not be swallowed by it."""
    assert classify(Exception(msg)) == expected
