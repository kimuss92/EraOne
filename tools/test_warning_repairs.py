"""Credential integrity, bounded context and truthful optional dependency checks."""

import json
import os
from pathlib import Path

import pytest

from core import protected_config as protected


@pytest.fixture
def credentials(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows DPAPI contract")
    path = tmp_path / "api_keys.json"
    data = {
        "gemini_api_key": "synthetic-primary",
        "gemini_api_keys": ["synthetic-fallback"],
        "fish_audio": {"api_key": "synthetic-fish"},
        "os_system": "windows",
        "settings": {"voice": "Gemini"},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def test_real_dpapi_migration_roundtrip_and_idempotency(credentials):
    path, data = credentials
    assert protected.migrate(path)
    raw = path.read_text(encoding="utf-8")
    assert "synthetic-" not in raw
    assert json.loads(raw)["os_system"] == "windows"
    assert protected.read_config(path) == data
    assert not protected.has_plaintext_credentials(json.loads(raw))
    assert not protected.migrate(path)
    assert path.read_text(encoding="utf-8") == raw


def test_protected_readers_and_fallbacks(credentials, monkeypatch):
    import config
    from config import get_config
    from core import utils
    from memory import config_manager

    path, data = credentials
    protected.migrate(path)
    monkeypatch.setattr(utils, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_PATH", path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", path)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", path.parent)
    assert utils.get_api_keys() == ["synthetic-primary", "synthetic-fallback"]
    assert get_config() == data
    assert config_manager.load_api_keys() == data
    config_manager.save_api_keys("synthetic-updated")
    assert protected.read_config(path)["settings"] == data["settings"]
    assert protected.read_config(path)["gemini_api_key"] == "synthetic-updated"
    assert "synthetic-" not in path.read_text(encoding="utf-8")


def test_rejected_atomic_write_preserves_original(credentials, monkeypatch):
    path, data = credentials
    original = path.read_bytes()

    def fail(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(protected.os, "replace", fail)
    with pytest.raises(OSError):
        protected.write_config(path, data)
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".api_keys-*.tmp"))


def test_concurrent_edit_is_preserved(credentials):
    path, data = credentials
    original = path.read_bytes()
    path.write_text(json.dumps({**data, "settings": {"voice": "Fish"}}), encoding="utf-8")
    changed = path.read_bytes()
    with pytest.raises(protected.ProtectedConfigError, match="changed"):
        protected.write_config(path, data, expected_bytes=original)
    assert path.read_bytes() == changed


def test_tampered_credential_fails_without_exposing_secret(credentials):
    path, data = credentials
    protected.migrate(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["gemini_api_key"] = {protected.MARKER: "invalid-base64!"}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(protected.ProtectedConfigError, match="decryption failed") as exc:
        protected.read_config(path)
    assert data["gemini_api_key"] not in str(exc.value)


def test_failed_decryption_cannot_erase_other_settings(credentials, monkeypatch):
    from memory import config_manager

    path, _ = credentials
    path.write_text(
        json.dumps({"gemini_api_key": {protected.MARKER: "invalid"}, "keep": "yes"}),
        encoding="utf-8",
    )
    original = path.read_bytes()
    monkeypatch.setattr(config_manager, "CONFIG_FILE", path)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", path.parent)
    with pytest.raises(protected.ProtectedConfigError):
        config_manager.save_api_keys("replacement")
    assert path.read_bytes() == original


def test_long_working_plan_is_discoverable_and_rules_stay_inline():
    from core.cm_vault import VaultNote, brief_block

    body = "# Campaign\n" + "Working details. " * 500
    plan = VaultNote(Path("Campaign.md"), "Projects", "Campaign", body, {"type": "plan"})
    rule = VaultNote(
        Path("Rule.md"),
        "Resources",
        "Rule",
        "Never publish without approval.",
        {"type": "reference"},
    )
    block = brief_block(plan)
    assert len(block) < 1400
    assert "Campaign.md" in block and "Read this file before" in block
    assert plan.body == body
    assert rule.body in brief_block(rule)


def test_optional_check_excludes_unused_audio_but_reports_active_missing(monkeypatch):
    from tools import era_doctor

    monkeypatch.setattr(
        era_doctor.importlib.util,
        "find_spec",
        lambda name: None if name in ("pyaudio", "vlc", "python_vlc") else object(),
    )
    assert era_doctor.check_optional_packages()[0] == "OK"
    monkeypatch.setattr(
        era_doctor.importlib.util,
        "find_spec",
        lambda name: None if name == "sounddevice" else object(),
    )
    status, detail = era_doctor.check_optional_packages()
    assert status == "WARN" and "sounddevice" in detail


def test_doctor_validates_protected_config_and_nested_plaintext(credentials, monkeypatch):
    from tools import era_doctor

    path, data = credentials
    folder = path.parent / "config"
    folder.mkdir()
    target = folder / "api_keys.json"
    target.write_bytes(path.read_bytes())
    monkeypatch.setattr(era_doctor, "REPO_ROOT", path.parent)
    assert era_doctor.check_api_key_config()[0] == "WARN"
    protected.migrate(target)
    assert era_doctor.check_api_key_config()[0] == "OK"
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["extra"] = {"api_key": "synthetic-extra"}
    target.write_text(json.dumps(raw), encoding="utf-8")
    assert era_doctor.check_api_key_config()[0] == "WARN"
