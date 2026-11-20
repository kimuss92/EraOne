"""Setup progress must not replace an existing account before verification."""

import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import telegram_setup_wizard as wizard


@pytest.fixture
def store(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(wizard, "private_directory", lambda: tmp_path)
    monkeypatch.setattr(
        wizard, "load_settings", lambda path: copy.deepcopy(saved.get(str(path), {}))
    )
    monkeypatch.setattr(
        wizard, "save_settings", lambda path, value: saved.update({str(path): copy.deepcopy(value)})
    )
    return saved


@pytest.fixture
def client(monkeypatch):
    class PasswordNeeded(Exception):
        pass

    c = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="private-hash")),
        sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=17, phone="15550000001", bot=False)),
        session=SimpleNamespace(save=lambda: "private-session"),
    )
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=lambda *a: c))
    monkeypatch.setitem(
        sys.modules, "telethon.errors", SimpleNamespace(SessionPasswordNeededError=PasswordNeeded)
    )
    monkeypatch.setitem(
        sys.modules, "telethon.sessions", SimpleNamespace(StringSession=lambda s: s)
    )
    c.password_error = PasswordNeeded
    return c


DETAILS = {
    "action": "start",
    "era_phone": "+15550000001",
    "owner_phone": "+15550000002",
    "api_id": "123",
    "api_hash": "a" * 32,
}


@pytest.mark.asyncio
async def test_login_password_resume_and_no_secret_status(store, client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    store[str(wizard.private_directory())] = {"owner_bound": True, "session_secret": "working"}
    assert await wizard.run(DETAILS) == {"step": "code"}
    client.sign_in.side_effect = client.password_error
    assert await wizard.run({"action": "code", "code": "12345"}) == {"step": "password"}
    client.sign_in.side_effect = None
    assert await wizard.run({"action": "password", "password": "secret"}) == {"step": "owner"}
    draft = store[str(wizard.draft_directory())]
    assert draft["expected_owner_user_id"] == 42
    assert draft["session_secret"] == "private-session"
    assert "code" not in draft and "password" not in draft
    assert "private" not in str(wizard.status())
    assert store[str(wizard.private_directory())]["session_secret"] == "working"
    assert client.disconnect.await_count == 3


@pytest.mark.asyncio
async def test_invalid_code_keeps_progress_and_wrong_account_never_saved(store, client):
    await wizard.run(DETAILS)
    client.sign_in.side_effect = ValueError("private error")
    with pytest.raises(ValueError):
        await wizard.run({"action": "code", "code": "wrong"})
    assert wizard.status()["step"] == "code"
    client.sign_in.side_effect = None
    client.get_me.return_value.phone = "15550000002"
    with pytest.raises(wizard.SetupFault):
        await wizard.run({"action": "code", "code": "valid"})
    assert not store[str(wizard.draft_directory())].get("account_authenticated")


@pytest.mark.asyncio
async def test_unverified_enable_cannot_replace_account(store):
    store[str(wizard.draft_directory())] = {**DETAILS, "api_id": 123}
    store[str(wizard.private_directory())] = {"session_secret": "working"}
    with pytest.raises(ValueError):
        await wizard.run({"action": "enable"})
    assert store[str(wizard.private_directory())] == {"session_secret": "working"}


@pytest.mark.asyncio
async def test_owner_mismatch_preserves_working_account(store, monkeypatch):
    store[str(wizard.draft_directory())] = {"account_authenticated": True}
    auth = AsyncMock(side_effect=wizard.SetupFault("OWNER_ID_MISMATCH", "Wrong owner."))
    monkeypatch.setattr(wizard, "authenticate", auth)
    with pytest.raises(wizard.SetupFault):
        await wizard.run({"action": "owner", "username": "@wronguser"})
    assert str(wizard.private_directory()) not in store


@pytest.mark.asyncio
async def test_enable_backs_up_ciphertext_and_validates_binding(store, tmp_path):
    (tmp_path / "account.json").write_bytes(b"original encrypted data")
    store[str(wizard.draft_directory())] = {
        **DETAILS,
        "api_id": 123,
        "authenticated": True,
        "account_authenticated": True,
        "owner_bound": True,
        "owner_user_id": 42,
        "expected_owner_user_id": 42,
        "era_user_id": 17,
    }
    result = await wizard.run({"action": "enable"})
    assert result["restartRequired"]
    assert (
        next(tmp_path.glob("account.before-setup-*.json")).read_bytes()
        == b"original encrypted data"
    )
    assert store[str(tmp_path)]["daily_calls_opt_in"] is True
