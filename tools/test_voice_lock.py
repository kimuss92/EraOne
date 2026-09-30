"""voice_lock: turn the owner-voice check on/off by voice, safely.

Turning it OFF opens Era to every voice, so it must be grounded in the user's
own words — never the model's choice alone or injected content.
"""

import json
import re
import time

import numpy as np
import pytest

import core.speaker_recognition as sr
import core.user_prefs as up
from app.era_live.tool_dispatch import (
    _VOICE_LOCK_OFF_COMMAND,
    _VOICE_LOCK_ON_COMMAND,
    ToolDispatchMixin,
)
from core.speaker_recognition import SpeakerProfile, SpeakerVerifier, load_settings


@pytest.mark.parametrize(
    "text",
    [
        "turn off voice recognition",
        "Era, disable the voice lock please",
        "switch off speaker verification",
        "voice recognition off",
        "désactive la reconnaissance vocale",
        "stop voice verification for now",
    ],
)
def test_off_phrases(text):
    assert _VOICE_LOCK_OFF_COMMAND.search(text)
    assert not _VOICE_LOCK_ON_COMMAND.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "turn on voice recognition",
        "enable voice lock",
        "turn voice recognition back on",
        "active la reconnaissance vocale",
        "réactive la reconnaissance vocale",
    ],
)
def test_on_phrases(text):
    assert _VOICE_LOCK_ON_COMMAND.search(text)
    assert not _VOICE_LOCK_OFF_COMMAND.search(text)


@pytest.mark.parametrize(
    "text",
    ["what's the weather", "turn off the lights", "open voice memos", "play some music"],
)
def test_unrelated_phrases_match_neither(text):
    assert not _VOICE_LOCK_OFF_COMMAND.search(text)
    assert not _VOICE_LOCK_ON_COMMAND.search(text)


class _Embedder:
    model_name = "fake.onnx"
    model_path = None

    def embed(self, samples):
        v = np.ones(32, np.float32)
        return v / np.linalg.norm(v)


class _UI:
    def __init__(self):
        self.lines = []

    def write_log(self, msg):
        self.lines.append(msg)


class _Era(ToolDispatchMixin):
    def __init__(self):
        self._last_user_text = ""
        self._recent_user_texts = []
        self.ui = _UI()


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    """Point the prefs store at a throwaway file, starting from the fail-closed default.

    voice_lock is now PERSISTED to user_prefs.json, and load_settings() reads that
    file back — so without this these tests inherit whatever the developer happens
    to have set on their own machine and flip red when the lock is off. conftest
    seeds the test memory dir with a COPY of the real store, so redirecting
    ERA_MEMORY_DIR is not enough on its own: the value still rides along.
    """
    path = tmp_path / "user_prefs.json"
    path.write_text(json.dumps({"version": 1, "voice_lock_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(up, "PREFS_PATH", path)
    return path


@pytest.fixture
def verifier(tmp_path, monkeypatch, isolated_prefs):
    path = tmp_path / "profile.json"
    SpeakerProfile("fake.onnx", np.ones(32, np.float32) / np.sqrt(32)).save(path)
    v = SpeakerVerifier(load_settings(tmp_path / "none.json"), path, _Embedder())
    monkeypatch.setattr(sr, "_verifier", v)
    return v


def _said(era, text, age_s=0.0):
    era._recent_user_texts.append((time.time() - age_s, text))


def test_disable_requires_the_user_to_have_said_it(verifier):
    era = _Era()
    out = era._handle_voice_lock({"enabled": False})
    assert out.startswith("Not done") and verifier.enabled


def test_disable_ignores_stale_request(verifier):
    era = _Era()
    _said(era, "turn off voice recognition", age_s=300)
    assert era._handle_voice_lock({"enabled": False}).startswith("Not done")
    assert verifier.enabled


def test_disable_then_enable_by_voice(verifier):
    era = _Era()
    _said(era, "turn off voice recognition")
    assert "now OFF" in era._handle_voice_lock({"enabled": False})
    assert not verifier.enabled
    assert verifier.verify(b"\x00\x01" * 100).accepted  # anyone passes while off
    _said(era, "turn voice recognition back on")
    assert "now ON" in era._handle_voice_lock({"enabled": True})
    assert verifier.enabled


def test_enable_refused_when_it_would_lock_everyone_out(tmp_path, monkeypatch):
    v = SpeakerVerifier(
        dict(load_settings(tmp_path / "none.json"), enabled=False),
        tmp_path / "missing.json",
        _Embedder(),
    )
    monkeypatch.setattr(sr, "_verifier", v)
    era = _Era()
    _said(era, "enable voice recognition")
    assert "Could not" in era._handle_voice_lock({"enabled": True})
    assert not v.enabled


def test_status_query(verifier):
    assert "currently ON" in _Era()._handle_voice_lock({})


def test_not_hijacked_by_action_registry_or_safety_guard():
    from core.action_registry import canonicalize_tool_call
    from core.safety_guard import classify_action

    tool, params = canonicalize_tool_call("voice_lock", {"enabled": False})[:2]
    assert tool == "voice_lock" and params == {"enabled": False}
    assert not re.search(r"power|shutdown|restart", str(classify_action("voice_lock", {})), re.I)


# ── Persistence: the setting must survive a restart ──────────────────────
# voice_lock used to be runtime-only, so every reconnect silently re-armed the
# lock and the user had to turn it off again. The state is now written to
# memory/user_prefs.json and read back by load_settings() on the next start.


def test_disable_is_written_to_user_prefs(verifier, isolated_prefs, tmp_path):
    era = _Era()
    _said(era, "turn off voice recognition")
    era._handle_voice_lock({"enabled": False})

    assert json.loads(isolated_prefs.read_text(encoding="utf-8"))["voice_lock_enabled"] is False
    # A fresh settings load — what a restart does — comes back OFF.
    assert load_settings(tmp_path / "none.json")["enabled"] is False


def test_enable_is_written_to_user_prefs(verifier, isolated_prefs, tmp_path):
    era = _Era()
    _said(era, "turn off voice recognition")
    era._handle_voice_lock({"enabled": False})
    _said(era, "turn voice recognition back on")
    era._handle_voice_lock({"enabled": True})

    assert json.loads(isolated_prefs.read_text(encoding="utf-8"))["voice_lock_enabled"] is True
    assert load_settings(tmp_path / "none.json")["enabled"] is True


def test_refused_disable_does_not_persist(verifier, isolated_prefs, tmp_path):
    """The grounding gate is the injection defense: a refused disable must not
    leave the lock off on disk either."""
    era = _Era()  # user never said it
    assert era._handle_voice_lock({"enabled": False}).startswith("Not done")

    assert json.loads(isolated_prefs.read_text(encoding="utf-8"))["voice_lock_enabled"] is True
    assert load_settings(tmp_path / "none.json")["enabled"] is True


def test_unreadable_prefs_fail_closed(verifier, isolated_prefs, tmp_path):
    """A corrupt prefs file must re-arm the lock, never silently leave it off."""
    isolated_prefs.write_text("{ not json", encoding="utf-8")
    assert load_settings(tmp_path / "none.json")["enabled"] is True


def test_missing_key_fails_closed(verifier, isolated_prefs, tmp_path):
    isolated_prefs.write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert load_settings(tmp_path / "none.json")["enabled"] is True
