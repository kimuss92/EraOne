"""Settings never reveal stored credentials or destroy unrelated config."""

import json

import pytest

from core import empower_config


@pytest.fixture
def config(tmp_path, monkeypatch):
    target = tmp_path / "api_keys.json"
    target.write_text(json.dumps({"gemini_api_key": "test-secret", "existing": {"keep": True}}))
    monkeypatch.setattr(empower_config, "CONFIG_PATH", target)
    return target


def test_snapshot_masks_keys(config):
    result = empower_config.snapshot()
    assert "test-secret" not in json.dumps(result)
    field = next(f for f in result["fields"] if f["key"] == "gemini_api_key")
    assert field["configured"] and field["value"] == ""


def test_update_preserves_unrelated_config_and_seals_secret(config):
    from core.protected_config import read_config

    empower_config.update({"ollama_vision_model": "test-model"})
    actual = read_config(config)
    assert actual["existing"] == {"keep": True}
    assert actual["gemini_api_key"] == "test-secret"
    assert "test-secret" not in config.read_text()


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": "x"},
        {"home_assistant_url": "file:///etc/passwd"},
        {"home_assistant_url": "http://user:pass@example.com"},
        {"empower_output_dir": "../relative"},
        {"gemini_api_key": False},
        {},
    ],
)
def test_bad_settings_leave_original_bytes(config, patch):
    before = config.read_bytes()
    with pytest.raises(ValueError):
        empower_config.update(patch)
    assert config.read_bytes() == before


def test_damaged_config_is_not_replaced(config):
    config.write_text("{broken")
    with pytest.raises(ValueError):
        empower_config.update({"ollama_vision_model": "new"})
    assert config.read_text() == "{broken"


# A DPAPI blob sealed by another account/master key: valid base64, never decrypts.
STALE = {"$era_dpapi_v1": "AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAAstale0000"}


@pytest.fixture
def stale_config(tmp_path, monkeypatch):
    target = tmp_path / "api_keys.json"
    target.write_text(
        json.dumps(
            {
                "gemini_api_key": STALE,
                "fish_audio": {"api_key": STALE},
                "ollama_base_url": "http://h:1",
            }
        )
    )
    monkeypatch.setattr(empower_config, "CONFIG_PATH", target)
    return target


def test_stale_key_does_not_empty_the_config(stale_config, monkeypatch):
    from core import utils

    monkeypatch.setattr(utils, "CONFIG_PATH", stale_config)
    assert utils.load_config() == {"fish_audio": {}, "ollama_base_url": "http://h:1"}
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        utils.get_api_keys()


def test_snapshot_survives_stale_key_and_flags_it(stale_config):
    field = next(f for f in empower_config.snapshot()["fields"] if f["key"] == "gemini_api_key")
    assert field["stale"] and not field["configured"]


def test_reentering_key_keeps_other_stale_blobs(stale_config):
    from core.protected_config import read_config_lenient

    empower_config.update({"gemini_api_key": "new-secret"})
    data, stale = read_config_lenient(stale_config)
    assert data["gemini_api_key"] == "new-secret"
    assert stale == ["fish_audio.api_key"]
    raw = json.loads(stale_config.read_text())
    assert raw["fish_audio"]["api_key"] == STALE
    assert "new-secret" not in stale_config.read_text()
