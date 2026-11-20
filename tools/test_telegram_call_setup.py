"""Private call-account setup uses local secrets and verifies both identities."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import telegram_call_setup as setup


@pytest.fixture
def settings():
    return {
        "era_phone": "+15550000001",
        "owner_phone": "+15550000002",
        "api_id": 1234,
        "api_hash": "a" * 32,
        "expected_owner_user_id": 42,
    }


@pytest.mark.parametrize("value", ["20508000", "+0 123456789", "+123", "+15550000000123456"])
def test_international_phone_is_required(value):
    with pytest.raises(ValueError):
        setup.normalize_phone(value)


def test_international_phone_separators_are_normalized():
    assert setup.normalize_phone("+1 (555) 000-0001") == "+15550000001"


@pytest.mark.parametrize(
    "change",
    [
        {"owner_phone": "+15550000001"},
        {"api_id": True},
        {"api_hash": "not-a-hash"},
        {"api_id": 0},
    ],
)
def test_setup_requires_separate_account_and_application_credentials(settings, change):
    with pytest.raises(ValueError):
        setup.validate_setup({**settings, **change})


def test_entire_session_payload_is_protected_on_disk(monkeypatch, tmp_path, settings):
    monkeypatch.setattr(setup, "secure_directory", lambda directory: None)
    settings["session_secret"] = "fixture-session-never-plaintext"
    setup.save_settings(tmp_path, settings)
    content = (tmp_path / "account.json").read_text()
    assert settings["session_secret"] not in content
    assert settings["api_hash"] not in content
    assert settings["era_phone"] not in content
    assert "$era_dpapi_v1" in content
    assert setup.load_settings(tmp_path) == settings


@pytest.fixture
def client(monkeypatch):
    state = SimpleNamespace(
        me=SimpleNamespace(id=1, phone="15550000001", bot=False),
        owner=SimpleNamespace(id=42, phone="15550000002", bot=False, access_hash=777),
        lookup_missing=False,
        import_resolvable=True,
        disconnected=False,
        imports=[],
    )

    class Client:
        def __init__(self, *args):
            self.session = SimpleNamespace(save=lambda: "fixture-verified-session")

        async def start(self, **kwargs):
            state.start = kwargs

        async def get_me(self):
            return state.me

        async def get_entity(self, phone):
            if state.lookup_missing and phone.startswith("+"):
                raise ValueError("not cached")
            return state.owner

        async def __call__(self, contacts):
            state.imports.extend(contacts)
            return SimpleNamespace(
                users=[state.owner] if state.import_resolvable else [],
                imported=(
                    [SimpleNamespace(client_id=1, user_id=state.owner.id)]
                    if state.import_resolvable
                    else []
                ),
            )

        async def disconnect(self):
            state.disconnected = True

    modules = {
        "telethon": SimpleNamespace(TelegramClient=Client),
        "telethon.sessions": SimpleNamespace(StringSession=lambda value: value),
        "telethon.tl.functions.contacts": SimpleNamespace(
            ImportContactsRequest=lambda values: values
        ),
        "telethon.tl.types": SimpleNamespace(InputPhoneContact=lambda **kw: SimpleNamespace(**kw)),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return state


def test_successful_auth_binds_owner_but_does_not_enable_calls(settings, client):
    result = asyncio.run(setup.authenticate(settings))
    assert result["owner_user_id"] == 42
    assert result["era_user_id"] == 1
    assert result["session_secret"] == "fixture-verified-session"
    assert result["authenticated"] and not result["calls_enabled"]
    assert client.disconnected and client.imports == []


def test_personal_account_is_rejected_before_any_contact_import(settings, client):
    client.me.phone = "15550000002"
    client.lookup_missing = True
    with pytest.raises(ValueError):
        asyncio.run(setup.authenticate(settings))
    assert client.disconnected and client.imports == []


def test_fresh_account_imports_only_explicit_owner(settings, client):
    client.lookup_missing = True
    result = asyncio.run(setup.authenticate(settings))
    assert result["owner_user_id"] == 42
    assert [contact.phone for contact in client.imports] == [settings["owner_phone"]]


def test_wrong_bot_owner_binding_is_rejected(settings, client):
    client.owner.id = 99
    with pytest.raises(ValueError):
        asyncio.run(setup.authenticate(settings))
    assert client.disconnected


@pytest.mark.parametrize("change", [{"id": 1}, {"bot": True}, {"phone": "15550000003"}])
def test_owner_identity_guards(settings, client, change):
    for name, value in change.items():
        setattr(client.owner, name, value)
    with pytest.raises(ValueError):
        setup.validate_identities(settings, client.me, client.owner)


def test_noninteractive_setup_never_requests_credentials(monkeypatch):
    monkeypatch.setattr(setup.os, "isatty", lambda fd: False)
    prompt = Mock(side_effect=AssertionError("no secret prompt in tool output"))
    monkeypatch.setattr(setup.getpass, "getpass", prompt)
    assert setup.main() == 2
    prompt.assert_not_called()


def test_missing_settings_are_unconfigured(tmp_path):
    assert setup.load_settings(tmp_path) == {}


def test_non_object_settings_are_rejected(monkeypatch, tmp_path):
    (tmp_path / "account.json").touch()
    monkeypatch.setattr(setup, "read_config", lambda path: {setup.CONFIG_FIELD: json.dumps([])})
    with pytest.raises(ValueError):
        setup.load_settings(tmp_path)


def test_main_collects_secrets_locally_and_keeps_calls_disabled(
    monkeypatch, tmp_path, settings, client
):
    saved = []
    monkeypatch.setattr(setup.os, "isatty", lambda fd: True)
    monkeypatch.setattr(setup, "private_directory", lambda: tmp_path)
    initial = {key: value for key, value in settings.items() if key not in {"api_id", "api_hash"}}
    monkeypatch.setattr(setup, "load_settings", lambda directory: initial)
    monkeypatch.setattr("builtins.input", lambda prompt: "1234")
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "a" * 32)
    monkeypatch.setattr(setup, "save_settings", lambda directory, value: saved.append(value))
    assert setup.main() == 0
    assert not saved[0]["authenticated"] and not saved[0]["calls_enabled"]
    assert saved[1]["account_authenticated"] and not saved[1]["owner_bound"]
    assert saved[-1]["authenticated"] and not saved[-1]["calls_enabled"]


def test_owner_resolution_failure_preserves_only_verified_era_login(settings, client):
    client.lookup_missing = True
    client.import_resolvable = False
    saved = []
    with pytest.raises(setup.SetupFault, match="could not resolve") as error:
        asyncio.run(setup.authenticate(settings, checkpoint=saved.append))
    assert error.value.code == "OWNER_PHONE_UNRESOLVED"
    assert saved[0]["session_secret"] == "fixture-verified-session"
    assert not saved[0]["owner_bound"] and not saved[0]["calls_enabled"]
    assert client.disconnected


def test_wrong_era_account_is_never_checkpointed(settings, client):
    client.me.phone = "15550000002"
    saved = []
    with pytest.raises(setup.SetupFault) as error:
        asyncio.run(setup.authenticate(settings, checkpoint=saved.append))
    assert error.value.code == "ERA_ACCOUNT_MISMATCH"
    assert saved == []


def test_failed_username_is_not_saved_into_reusable_account_checkpoint(settings, client):
    settings["owner_username"] = "wrongownerfixture"
    client.owner.id = client.me.id
    saved = []
    with pytest.raises(setup.SetupFault) as error:
        asyncio.run(setup.authenticate(settings, checkpoint=saved.append))
    assert error.value.code == "SELF_CALL"
    assert "owner_username" not in saved[0]
    assert saved[0]["session_secret"] == "fixture-verified-session"
    assert settings["owner_username"] == "wrongownerfixture"


def test_username_binding_requires_independent_owner_proof(settings, client):
    settings["owner_username"] = "ownerfixture"
    settings.pop("expected_owner_user_id")
    client.owner.phone = None
    with pytest.raises(setup.SetupFault) as error:
        asyncio.run(setup.authenticate(settings))
    assert error.value.code == "OWNER_UNVERIFIED"


def test_main_recovers_owner_phone_privacy_via_verified_username(
    monkeypatch, tmp_path, settings, client
):
    client.lookup_missing = True
    client.import_resolvable = False
    client.owner.phone = None
    current = dict(settings)

    def save(directory, value):
        current.clear()
        current.update(value)

    monkeypatch.setattr(setup.os, "isatty", lambda fd: True)
    monkeypatch.setattr(setup, "private_directory", lambda: tmp_path)
    monkeypatch.setattr(setup, "load_settings", lambda directory: dict(current))
    monkeypatch.setattr(setup, "save_settings", save)
    monkeypatch.setattr("builtins.input", lambda prompt: "@ownerfixture")
    assert setup.main() == 0
    assert current["owner_user_id"] == 42 and current["owner_bound"]
    assert not current["calls_enabled"]


def test_main_prints_controlled_diagnostic_not_generic_valueerror(
    monkeypatch, tmp_path, settings, client, capsys
):
    client.me.phone = "15550000002"
    monkeypatch.setattr(setup.os, "isatty", lambda fd: True)
    monkeypatch.setattr(setup, "private_directory", lambda: tmp_path)
    monkeypatch.setattr(setup, "load_settings", lambda directory: settings)
    monkeypatch.setattr(setup, "save_settings", lambda *args: None)
    assert setup.main() == 1
    assert "ERA_ACCOUNT_MISMATCH" in capsys.readouterr().out


def test_main_errors_do_not_echo_secret_exception_text(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(setup.os, "isatty", lambda fd: True)
    monkeypatch.setattr(setup, "private_directory", lambda: tmp_path)
    monkeypatch.setattr(
        setup,
        "load_settings",
        Mock(side_effect=ValueError("fixture-sensitive-session")),
    )
    assert setup.main() == 1
    assert "fixture-sensitive-session" not in capsys.readouterr().out


def test_private_location_requires_local_appdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(RuntimeError):
        setup.private_directory()


def test_private_location_is_outside_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert setup.private_directory() == tmp_path / "EraOne" / "telegram-calls" / "private"


@pytest.mark.parametrize("returncode", [0, 1])
def test_acl_protection_is_required_before_credentials(monkeypatch, tmp_path, returncode):
    import win32api
    import win32security

    token = SimpleNamespace(Close=Mock())
    monkeypatch.setattr(win32api, "GetCurrentProcess", lambda: 1)
    monkeypatch.setattr(win32security, "OpenProcessToken", lambda *args: token)
    monkeypatch.setattr(win32security, "GetTokenInformation", lambda *args: ("sid", 0))
    monkeypatch.setattr(win32security, "ConvertSidToStringSid", lambda sid: "S-1-5-21-123")
    runner = Mock(return_value=SimpleNamespace(returncode=returncode))
    monkeypatch.setattr(setup.subprocess, "run", runner)
    if returncode:
        with pytest.raises(RuntimeError):
            setup.secure_directory(tmp_path)
    else:
        setup.secure_directory(tmp_path)
    token.Close.assert_called_once()
    assert runner.call_args.args[0][1] == str(tmp_path)
    assert "/inheritance:r" in runner.call_args.args[0]
