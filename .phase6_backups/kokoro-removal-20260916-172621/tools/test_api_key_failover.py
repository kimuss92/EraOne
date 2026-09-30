"""Multi-key Gemini failover, and the launch cost of the local voice.

Quota (1011 / RESOURCE_EXHAUSTED) is billed per KEY. Before this, a quota drop
meant a 15-minute-and-doubling cooldown during which Era cannot hear her wake
word at all — the mic lives inside the Live session. A spare key turns that into
an immediate reconnect on the full cloud voice, which is why rotation is tried
before every other recovery.

The autostart tests cover the other half: the local voice used to be loaded
before the first connect, so every launch paid a backend spawn plus a Kokoro
model load before Era could say anything.
"""

import core.utils as utils
from core.utils import get_api_keys
from core.voice_mode import voicebox_autostart


def _cfg(monkeypatch, data: dict):
    """Patch on core.utils: voicebox_autostart imports load_config from there
    at call time, so both readers resolve to this one stub."""
    monkeypatch.setattr(utils, "load_config", lambda: data)


# ── Key discovery ────────────────────────────────────────────────────────────
def test_primary_key_alone_still_works(monkeypatch):
    _cfg(monkeypatch, {"gemini_api_key": "primary"})
    assert get_api_keys() == ["primary"]


def test_fallback_keys_follow_the_primary_in_order(monkeypatch):
    """Order matters: the primary must be tried first on every fresh outage,
    or Era would drift onto a spare and stay there."""
    _cfg(
        monkeypatch,
        {
            "gemini_api_key": "primary",
            "gemini_api_key_fallback": "second",
            "gemini_api_key_fallback2": "third",
        },
    )
    assert get_api_keys() == ["primary", "second", "third"]


def test_duplicate_keys_are_collapsed(monkeypatch):
    """The same key in two slots must not look like two chances — rotating onto
    an equally-exhausted key burns a retry and hides a real outage."""
    _cfg(
        monkeypatch,
        {"gemini_api_key": "same", "gemini_api_key_fallback": "same"},
    )
    assert get_api_keys() == ["same"]


def test_blank_and_whitespace_slots_are_ignored(monkeypatch):
    _cfg(
        monkeypatch,
        {"gemini_api_key": "primary", "gemini_api_key_fallback": "   "},
    )
    assert get_api_keys() == ["primary"]


def test_a_json_list_of_keys_is_supported(monkeypatch):
    _cfg(monkeypatch, {"gemini_api_key": "primary", "gemini_api_keys": ["a", "b"]})
    assert get_api_keys() == ["primary", "a", "b"]


def test_missing_key_still_raises(monkeypatch):
    _cfg(monkeypatch, {})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    try:
        get_api_keys()
    except RuntimeError:
        return
    raise AssertionError("a config with no key at all must still raise")


# ── Rotation ─────────────────────────────────────────────────────────────────
class _Rotator:
    """Just the rotation state machine, lifted off EraLive."""

    from main import EraLive

    _rotate_api_key = EraLive._rotate_api_key

    def __init__(self, keys):
        self._api_keys = keys
        self._api_key_index = 0
        self._api_keys_tried = 0


def test_single_key_never_rotates():
    """With nothing to rotate to, the caller must fall through to the cooldown
    rather than spin."""
    r = _Rotator(["only"])
    assert r._rotate_api_key() is False
    assert r._api_key_index == 0


def test_rotation_walks_every_spare_then_stops():
    r = _Rotator(["a", "b", "c"])
    assert r._rotate_api_key() is True
    assert r._api_key_index == 1
    assert r._rotate_api_key() is True
    assert r._api_key_index == 2
    # All three tried: stop, so the quota branch reaches its real cooldown.
    assert r._rotate_api_key() is False


def test_the_counter_resets_so_the_next_outage_sweeps_again():
    r = _Rotator(["a", "b"])
    assert r._rotate_api_key() is True
    assert r._rotate_api_key() is False
    # A successful connect resets this (see run()); the next outage must be
    # able to rotate again rather than going straight to a cooldown forever.
    r._api_keys_tried = 0
    assert r._rotate_api_key() is True


def test_rotation_wraps_rather_than_running_off_the_end():
    r = _Rotator(["a", "b"])
    r._api_key_index = 1
    assert r._rotate_api_key() is True
    assert r._api_key_index == 0


# ── Launch cost ──────────────────────────────────────────────────────────────
def test_local_voice_does_not_autostart_by_default(monkeypatch):
    """The regression this exists for: loading Kokoro before the first connect
    delayed Era's greeting on every single launch."""
    _cfg(monkeypatch, {})
    assert voicebox_autostart() is False


def test_autostart_can_be_re_enabled(monkeypatch):
    _cfg(monkeypatch, {"voicebox": {"autostart": True}})
    assert voicebox_autostart() is True


def test_autostart_accepts_a_string_flag(monkeypatch):
    """Hand-edited JSON often carries "true" rather than true."""
    _cfg(monkeypatch, {"voicebox": {"autostart": "true"}})
    assert voicebox_autostart() is True
    _cfg(monkeypatch, {"voicebox": {"autostart": "no"}})
    assert voicebox_autostart() is False


def test_autostart_survives_a_non_dict_voicebox_block(monkeypatch):
    """A malformed config must cost the preference, never the launch."""
    _cfg(monkeypatch, {"voicebox": "yes please"})
    assert voicebox_autostart() is False
