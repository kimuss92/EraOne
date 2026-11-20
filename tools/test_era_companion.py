"""Real HTTP pairing, origin checks, normal input delivery and revocation."""

import socket
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio

from app.hud.companion import Companion


@pytest_asyncio.fixture
async def companion(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    commands = []
    server = SimpleNamespace(engine=lambda: object(), _on_command=commands.append)
    gateway = Companion(server, tmp_path / "devices.json")
    await gateway.start("127.0.0.1", port)
    yield gateway, commands
    await gateway.stop()


async def pair(client, gateway):
    offer = gateway.pairing_offer()
    code = offer["url"].split("#pair=")[1]
    async with client.post(
        gateway.snapshot()["url"] + "pair", json={"code": code, "name": "Test iPhone"}
    ) as response:
        assert response.status == 200
        return await response.json(), code


@pytest.mark.asyncio
async def test_pair_chat_and_revoke_real_http(companion):
    gateway, commands = companion
    base = gateway.snapshot()["url"]
    async with aiohttp.ClientSession() as client:
        credential, code = await pair(client, gateway)
        headers = {"Authorization": "Bearer " + credential["token"]}
        async with client.post(
            base + "message", headers=headers, json={"text": "Hello Era"}
        ) as response:
            assert response.status == 200 and (await response.json())["accepted"]
        assert commands == ["Hello Era"]
        assert credential["token"] not in gateway.storage.read_text()
        assert "digest" not in str(gateway.snapshot())
        async with client.post(base + "pair", json={"code": code, "name": "replay"}) as response:
            assert response.status == 401
        gateway.revoke(credential["id"])
        async with client.post(
            base + "message", headers=headers, json={"text": "Should not arrive"}
        ) as response:
            assert response.status == 401
        assert commands == ["Hello Era"]


@pytest.mark.asyncio
async def test_cross_origin_and_host_are_refused(companion):
    gateway, _ = companion
    async with aiohttp.ClientSession() as client:
        credential, _ = await pair(client, gateway)
        headers = {
            "Authorization": "Bearer " + credential["token"],
            "Origin": "https://evil.example",
        }
        async with client.get(
            gateway.snapshot()["url"] + "conversation", headers=headers
        ) as response:
            assert response.status == 403
        async with client.get(
            gateway.snapshot()["url"], headers={"Host": "evil.example"}
        ) as response:
            assert response.status == 403


@pytest.mark.asyncio
async def test_auth_required_and_internal_commands_rejected(companion):
    gateway, commands = companion
    base = gateway.snapshot()["url"]
    async with aiohttp.ClientSession() as client:
        async with client.get(base + "conversation") as response:
            assert response.status == 401
        credential, _ = await pair(client, gateway)
        for text in ["__shutdown__", "", "x" * 8001]:
            async with client.post(
                base + "message",
                headers={"Authorization": "Bearer " + credential["token"]},
                json={"text": text},
            ) as response:
                assert response.status == 400
        assert not commands


@pytest.mark.asyncio
async def test_expired_pairing_cannot_register(companion):
    gateway, _ = companion
    offer = gateway.pairing_offer()
    gateway.expires = 0
    async with aiohttp.ClientSession() as client:
        async with client.post(
            gateway.snapshot()["url"] + "pair",
            json={"code": offer["url"].split("#pair=")[1], "name": "Late phone"},
        ) as response:
            assert response.status == 401
    assert not gateway.devices


@pytest.mark.asyncio
async def test_pairing_rate_limit(companion):
    gateway, _ = companion
    async with aiohttp.ClientSession() as client:
        codes = []
        for _ in range(9):
            async with client.post(
                gateway.snapshot()["url"] + "pair", json={"code": "wrong", "name": "wrong"}
            ) as response:
                codes.append(response.status)
        assert codes == [401] * 8 + [429]


@pytest.mark.asyncio
async def test_gateway_does_not_expose_hud_api(companion):
    gateway, _ = companion
    async with aiohttp.ClientSession() as client:
        credential, _ = await pair(client, gateway)
        async with client.post(
            gateway.snapshot()["url"] + "api/approval",
            headers={"Authorization": "Bearer " + credential["token"]},
            json={"decision": "approve"},
        ) as response:
            assert response.status == 404


@pytest.mark.asyncio
async def test_listener_rejects_wildcard(tmp_path):
    gateway = Companion(SimpleNamespace(), tmp_path / "devices.json")
    with pytest.raises(ValueError):
        await gateway.start("0.0.0.0", 8766)
    assert not gateway.snapshot()["running"]


@pytest.mark.asyncio
async def test_conversation_includes_era_replies_but_not_internal_logs(companion, monkeypatch):
    from app.hud.events import hud

    gateway, _ = companion
    monkeypatch.setattr(
        hud,
        "conversation",
        lambda n: [
            {"role": "user", "text": "Hello", "ts": 1},
            {"role": "agent", "text": "Good morning", "ts": 2},
            {"role": "system", "text": "internal diagnostics", "ts": 3},
        ],
    )
    async with aiohttp.ClientSession() as client:
        credential, _ = await pair(client, gateway)
        async with client.get(
            gateway.snapshot()["url"] + "conversation",
            headers={"Authorization": "Bearer " + credential["token"]},
        ) as response:
            data = await response.json()
    assert [row["text"] for row in data["entries"]] == ["Hello", "Good morning"]
