"""Workbench source review uses the existing authenticated, bounded source scope."""

import json
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from core import codemode, development
from crew import source
from tools.test_hud_server import get, server  # noqa: F401 - isolated HTTP fixture


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(source, "ROOT", tmp_path)
    monkeypatch.setattr(development, "ROOT", tmp_path)
    monkeypatch.setattr(codemode, "get_codemode", lambda: SimpleNamespace(enabled=True))
    (tmp_path / "core").mkdir()
    (tmp_path / "core/example.py").write_text("VALUE = 1\n" * 300, encoding="utf-8")
    return tmp_path


def test_source_requires_token_and_active_mode(server, workspace, monkeypatch):
    url = "/api/codemode/source?path=core/example.py"
    assert get(server, url)[0] == 401
    assert get(server, url, token="wrong")[0] == 401
    monkeypatch.setattr(codemode, "get_codemode", lambda: SimpleNamespace(enabled=False))
    code, body = get(server, url, server.session_token)
    assert code == 409 and "Enter CodeMode" in json.loads(body)["error"]


def test_source_pages_are_read_only_and_use_current_bytes(server, workspace):
    path = workspace / "core/example.py"
    before = path.read_bytes()
    code, body = get(server, "/api/codemode/source?path=core/example.py", server.session_token)
    data = json.loads(body)
    assert code == 200 and data["total_lines"] == 300 and data["truncated"]
    assert data["content"].startswith("1: VALUE = 1\n")
    assert data["content"].endswith("250: VALUE = 1")
    code, body = get(
        server, "/api/codemode/source?path=core/example.py&start_line=251", server.session_token
    )
    assert code == 200 and json.loads(body)["content"].startswith("251: VALUE = 1\n")
    assert not json.loads(body)["truncated"]
    assert path.read_bytes() == before
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.bound_port}/api/codemode/source",
        data=b'{"content":"overwrite"}',
        headers={"X-EraOne-Token": server.session_token, "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 405
    path.write_text("CHANGED = True\n", encoding="utf-8")
    _, body = get(server, "/api/codemode/source?path=core/example.py", server.session_token)
    assert json.loads(body)["sha256"] != data["sha256"]


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "core/../../outside.py",
        "C:/secret.py",
        "/secret.py",
        "core\\example.py",
        "runtime/task.py",
        ".env",
        "config/keys.json",
        "memory/private.md",
        "core/../example.py",
        "a" * 501,
    ],
)
def test_source_rejects_outside_scope_and_private_state(server, workspace, path):
    code, body = get(
        server, "/api/codemode/source?path=" + quote(path, safe=""), server.session_token
    )
    assert code == 400 and "error" in json.loads(body)


@pytest.mark.parametrize("start", ["0", "-1", "bad", "1.2"])
def test_source_rejects_invalid_line_ranges(server, workspace, start):
    code, _ = get(
        server,
        "/api/codemode/source?path=core/example.py&start_line=" + start,
        server.session_token,
    )
    assert code == 400


def test_source_missing_binary_and_oversized_inputs_are_contained(server, workspace):
    assert get(server, "/api/codemode/source?path=core/missing.py", server.session_token)[0] == 404
    path = workspace / "core/example.py"
    path.write_bytes(b"\xff\xfe\x00")
    assert get(server, "/api/codemode/source?path=core/example.py", server.session_token)[0] == 400
    path.write_bytes(b"x" * 2_000_001)
    assert get(server, "/api/codemode/source?path=core/example.py", server.session_token)[0] == 400


def test_source_redacts_known_secret_patterns(server, workspace):
    fake_key = "sk-" + "A" * 48
    (workspace / "core/example.py").write_text(f'KEY = "{fake_key}"\n', encoding="utf-8")
    code, body = get(server, "/api/codemode/source?path=core/example.py", server.session_token)
    assert code == 200 and fake_key.encode() not in body and json.loads(body)["redacted"]
