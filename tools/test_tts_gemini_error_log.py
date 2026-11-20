import asyncio

import pytest

from core.tts_gemini import synthesize


class _Resp:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _Session:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _Resp(self.status, self._text)


class _ClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return _Session(503, "Service Unavailable")

    async def __aexit__(self, *args):
        return False


async def test_log_includes_http_error(monkeypatch, capsys, tmp_path):
    import core.tts_gemini as tts

    monkeypatch.setattr(
        tts, "_post", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 503"))
    )
    monkeypatch.setattr(tts, "get_api_key", lambda: "fake-key")
    tts._retry_at = 0.0

    result = await synthesize("hello world")
    captured = capsys.readouterr().out

    assert result == b""
    assert "HTTP 503" in captured
