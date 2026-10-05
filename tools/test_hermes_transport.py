"""HermesAgentTransport: Hermes is the brain, Era is the gate.

The HTTP layer is injected (FakeHttp), so nothing here reaches the real gateway, and the key in
these tests is a made-up string: the real key file is never read. Everything else — the turn
loop, capability allow-lists, the vault bridge — is production code, which is the point: a
reply from Hermes can only ever ask Era for something, and Era's gates answer.
"""

from __future__ import annotations

import json
import re

import pytest

from core import cm_vault, vault_bridge
from core.pending_confirmation import get_shared_store
from crew import router as cr
from crew.hermes_transport import (
    DEFAULT_BASE,
    KEY_ENTRY,
    RECHECK_S,
    HermesAgentTransport,
    HermesKeyMissing,
    read_key,
)
from crew.transport import BotSpec, Transport, Turn
from tools.crew_testkit import assert_crew_failure_shape, call, final

KEY = "synthetic-key-for-tests-0000"
LINE = re.compile(
    r"^\[crew\] bot=\w+ tier=\w+ provider=\w+ tokens_in=\d+ tokens_out=\d+ cached=(true|false)$"
)
BOT = BotSpec(
    id="tester",
    role="Tester",
    description="d",
    prompt="p",
    capabilities=("vault.read",),
)


def toolsets(**enabled):
    names = ("file", "terminal", "skills", "vision", "web")
    return {
        "object": "list",
        "platform": "api_server",
        "data": [{"name": n, "enabled": bool(enabled.get(n))} for n in names],
    }


def chat(content, prompt=11, completion=7):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def aliases(*names):
    """What `GET /v1/models` returns: the gateway's own name plus the profile's routes."""
    return 200, {"object": "list", "data": [{"id": n} for n in ("hermes-agent", *names)]}


class FakeHttp:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.toolsets = (200, toolsets())
        self.health = (200, {"status": "ok"})
        self.models = aliases()  # GET /v1/models: the routes this profile defines
        self.chats: list = []  # (status, body) or an Exception, in order
        self.raise_on_toolsets: Exception | None = None

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        path = url.split("8642", 1)[1] if "8642" in url else url
        if path == "/health":
            return self.health
        if path.endswith("/v1/toolsets"):
            if self.raise_on_toolsets:
                raise self.raise_on_toolsets
            return self.toolsets
        if path.endswith("/v1/models"):
            return self.models
        if path.endswith("/v1/chat/completions"):
            item = self.chats.pop(0) if self.chats else (200, chat(json.dumps({"final": "ok"})))
            if isinstance(item, Exception):
                raise item
            return item
        return 404, None

    def paths(self):
        return [u.split("8642", 1)[1] for _, u, _, _ in self.calls]


@pytest.fixture
def keyfile(tmp_path):
    p = tmp_path / "api_keys.json"
    p.write_text(json.dumps({KEY_ENTRY: KEY, "gemini_api_key": "unrelated"}), encoding="utf-8")
    return p


class Rec:
    def __init__(self):
        self.lines = []

    def info(self, line):
        self.lines.append(line)

    warning = info


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(cr, "router_logger", r)
    return r


def make(keyfile, http=None, clock=None, **kw):
    http = http or FakeHttp()
    t = HermesAgentTransport(
        config_path=keyfile, http=http, **({"clock": clock} if clock else {}), **kw
    )
    t.register("tester", BOT)
    return t, http


# ── the contract ────────────────────────────────────────────────────────────
def test_it_implements_the_existing_transport_contract(keyfile):
    t, _ = make(keyfile)
    assert isinstance(t, Transport) and t.name == "hermes_agent"
    assert t.capabilities("tester") == ["vault.read"] and t.capabilities("ghost") == []
    for method in ("dispatch", "register", "capabilities", "health"):
        assert callable(getattr(t, method))


def test_the_default_target_is_the_era_one_profile_on_loopback():
    assert DEFAULT_BASE == "http://127.0.0.1:8642/p/era_one"


@pytest.mark.parametrize(
    "bad",
    [
        "http://evil.example/p/era_one",
        "http://192.168.1.20:8642/p/era_one",
        "http://127.0.0.1.evil.com:8642/p/era_one",
        "ftp://127.0.0.1:8642",
        "",
    ],
)
def test_a_non_loopback_base_url_is_refused_so_the_key_cannot_leave(bad, keyfile):
    with pytest.raises(ValueError):
        HermesAgentTransport(bad, config_path=keyfile, http=FakeHttp())


@pytest.mark.parametrize("ok", ["http://127.0.0.1:8642/p/era_one", "http://localhost:8642/p/x"])
def test_loopback_urls_are_accepted(ok, keyfile):
    HermesAgentTransport(ok, config_path=keyfile, http=FakeHttp())


# ── the key ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "content",
    [
        None,
        "{}",
        json.dumps({KEY_ENTRY: ""}),
        json.dumps({KEY_ENTRY: "  "}),
        json.dumps({KEY_ENTRY: "placeholder"}),
        json.dumps({KEY_ENTRY: 123}),
        "{not json",
        json.dumps({"other": "x"}),
    ],
)
def test_a_missing_key_is_a_typed_failure_and_nothing_is_sent(content, tmp_path, rec):
    p = tmp_path / "api_keys.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    t, http = make(p)
    out = t.dispatch("tester", Turn(task="t"))
    assert_crew_failure_shape(out)
    assert out.error_type == "MissingCredential" and KEY_ENTRY in out.data["detail"]
    assert http.calls == [], "no request may be made without the era_one key"


def test_read_key_raises_without_echoing_anything(tmp_path):
    p = tmp_path / "k.json"
    p.write_text(json.dumps({KEY_ENTRY: ""}), encoding="utf-8")
    with pytest.raises(HermesKeyMissing) as err:
        read_key(p)
    assert "k.json" not in str(err.value) and KEY not in str(err.value)


def test_the_key_is_read_at_call_time_so_rotating_it_needs_no_restart(keyfile):
    t, http = make(keyfile, recheck_s=0)
    t.dispatch("tester", Turn(task="one"))
    keyfile.write_text(json.dumps({KEY_ENTRY: "rotated-synthetic-key-1111"}), encoding="utf-8")
    t.dispatch("tester", Turn(task="two"))
    auths = {h["Authorization"] for _, _, h, _ in http.calls}
    assert auths == {f"Bearer {KEY}", "Bearer rotated-synthetic-key-1111"}


def test_the_key_never_appears_in_a_result_a_log_or_a_message(keyfile, rec):
    t, http = make(keyfile)
    http.toolsets = (401, None)
    refused = t.dispatch("tester", Turn(task="t"))
    http.toolsets = (200, toolsets())
    ok = t.dispatch("tester", Turn(task="t"))
    health = t.health()
    for blob in (repr(refused), repr(ok), repr(health), "\n".join(rec.lines)):
        assert KEY not in blob


def test_the_key_goes_only_in_the_authorization_header(keyfile):
    t, http = make(keyfile)
    t.dispatch("tester", Turn(task="t"))
    for _, url, headers, body in http.calls:
        assert headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in url and (body is None or KEY.encode() not in body)


# ── the startup / drift check ───────────────────────────────────────────────
def test_it_asks_hermes_for_the_toolsets_before_the_first_turn(keyfile):
    t, http = make(keyfile)
    assert t.dispatch("tester", Turn(task="t")).success
    assert http.paths()[0] == "/p/era_one/v1/toolsets"
    assert http.paths()[-1] == "/p/era_one/v1/chat/completions"


@pytest.mark.parametrize("name", ["terminal", "file", "skills", "vision", "web"])
def test_any_enabled_tool_refuses_to_run_and_no_chat_is_sent(name, keyfile):
    http = FakeHttp()
    http.toolsets = (200, toolsets(**{name: True}))
    t, _ = make(keyfile, http)
    out = t.dispatch("tester", Turn(task="t"))
    assert_crew_failure_shape(out)
    assert out.error_type == "TransportDown" and name in out.data["detail"]
    assert not any(p.endswith("/chat/completions") for p in http.paths())


@pytest.mark.parametrize(
    ("status", "body", "needle"),
    [
        (401, None, "401"),
        (404, None, "404"),
        (500, None, "unreadable"),
        (200, None, "unreadable"),
        (200, {"data": "nope"}, "unreadable"),
        (200, {"data": []}, "no toolsets"),  # nothing to verify = not verified
    ],
)
def test_an_unverifiable_gateway_fails_closed(status, body, needle, keyfile):
    http = FakeHttp()
    http.toolsets = (status, body)
    t, _ = make(keyfile, http)
    out = t.dispatch("tester", Turn(task="t"))
    assert out.error_type == "TransportDown" and needle in out.data["detail"]
    assert not any(p.endswith("/chat/completions") for p in http.paths())


def test_an_unreachable_gateway_is_transport_down_not_an_exception(keyfile):
    http = FakeHttp()
    http.raise_on_toolsets = ConnectionRefusedError("refused")
    t, _ = make(keyfile, http)
    out = t.dispatch("tester", Turn(task="t"))
    assert out.error_type == "TransportDown" and "unreachable" in out.data["detail"]


def test_drift_is_caught_by_the_periodic_recheck(keyfile):
    now = [0.0]
    t, http = make(keyfile, clock=lambda: now[0])
    assert t.dispatch("tester", Turn(task="a")).success
    http.toolsets = (200, toolsets(terminal=True))  # someone turns a tool on
    assert t.dispatch("tester", Turn(task="b")).success, "inside the window it is not re-asked"
    now[0] += RECHECK_S + 1
    out = t.dispatch("tester", Turn(task="c"))
    assert out.error_type == "TransportDown" and "terminal" in out.data["detail"]


def test_a_failed_chat_forces_a_reverification(keyfile):
    http = FakeHttp()
    t, _ = make(keyfile, http)
    t.dispatch("tester", Turn(task="a"))
    http.chats.append((500, None))
    t.dispatch("tester", Turn(task="b"))
    before = http.paths().count("/p/era_one/v1/toolsets")
    t.dispatch("tester", Turn(task="c"))
    assert http.paths().count("/p/era_one/v1/toolsets") == before + 1


# ── the model call ──────────────────────────────────────────────────────────
def test_the_request_is_text_in_with_no_tools_and_no_session(keyfile):
    t, http = make(keyfile)
    t.dispatch("tester", Turn(task="what are the rules"))
    method, url, headers, body = next(c for c in http.calls if c[1].endswith("/chat/completions"))
    sent = json.loads(body)
    assert method == "POST" and sent["stream"] is False
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert "what are the rules" in sent["messages"][1]["content"]
    assert set(sent) == {"messages", "stream"}, "no tools, no model, no session fields"
    assert "X-Hermes-Session-Id" not in headers and "X-Hermes-Session-Key" not in headers


# ── the quality hint ────────────────────────────────────────────────────────
def chat_bodies(http):
    return [json.loads(c[3]) for c in http.calls if c[1].endswith("/chat/completions")]


def test_the_hint_is_sent_when_hermes_lists_that_route(keyfile):
    http = FakeHttp()
    http.models = aliases("era-standard", "era-critical")
    t, _ = make(keyfile, http)
    t.dispatch("tester", Turn(task="what are the rules"))  # a tool-calling bot: standard
    assert chat_bodies(http)[0]["model"] == "era-standard"


def test_no_listed_route_means_no_model_field_so_hermes_uses_its_default(keyfile):
    t, http = make(keyfile)  # the profile defines no era-* routes
    t.dispatch("tester", Turn(task="what are the rules"))
    assert "model" not in chat_bodies(http)[0]


def test_an_unlisted_hint_is_never_sent_as_a_raw_model_slug(keyfile):
    """An unknown `model` would reach the provider as a model id and fail."""
    http = FakeHttp()
    http.models = aliases("era-trivial")  # no era-standard
    t, _ = make(keyfile, http)
    t.dispatch("tester", Turn(task="what are the rules"))
    assert "model" not in chat_bodies(http)[0]


def test_the_hint_follows_the_task_not_the_bot(keyfile):
    http = FakeHttp()
    http.models = aliases("era-trivial", "era-standard", "era-critical")
    t, _ = make(keyfile, http)
    t.register(
        "writer",
        BotSpec(id="writer", role="W", description="d", prompt="p", capabilities=("vault.create",)),
    )
    t.dispatch("writer", Turn(task="hello there"))
    t.dispatch("writer", Turn(task="save this as a new note"))
    assert [b["model"] for b in chat_bodies(http)] == ["era-standard", "era-critical"]


def test_a_models_listing_failure_never_stops_the_turn(keyfile):
    http = FakeHttp()
    http.models = (500, None)
    t, _ = make(keyfile, http)
    assert t.dispatch("tester", Turn(task="t")).success
    assert "model" not in chat_bodies(http)[0]


def test_hermes_own_reason_reaches_the_failure_detail(keyfile):
    http = FakeHttp()
    http.chats = [(500, {"error": {"message": "Hermes is not logged into Nous Portal."}})]
    t, _ = make(keyfile, http)
    out = t.dispatch("tester", Turn(task="t"))
    assert out.error_type == "LLMUnavailable"
    assert "HTTP 500" in out.data["detail"] and "not logged into Nous Portal" in out.data["detail"]


def test_a_turn_runs_and_logs_the_required_line_with_hermes_usage(keyfile, rec):
    http = FakeHttp()
    http.chats = [(200, chat(json.dumps(final("Never mix two stone families."))))]
    t, _ = make(keyfile, http)
    out = t.dispatch("tester", Turn(task="rules?"))
    assert out.success and "stone" in out.message
    assert rec.lines == [
        "[crew] bot=tester tier=standard provider=hermes tokens_in=11 tokens_out=7 cached=false"
    ]
    assert LINE.match(rec.lines[0])


def test_every_model_call_logs_even_when_hermes_fails(keyfile, rec):
    http = FakeHttp()
    http.chats = [(502, None)]
    t, _ = make(keyfile, http)
    out = t.dispatch("tester", Turn(task="t"))
    assert out.error_type == "LLMUnavailable" and "HTTP 502" in out.data["detail"]
    assert len(rec.lines) == 1 and LINE.match(rec.lines[0])


def test_a_network_error_mid_turn_is_llm_unavailable(keyfile):
    http = FakeHttp()
    http.chats = [TimeoutError("slow")]
    t, _ = make(keyfile, http)
    out = t.dispatch("tester", Turn(task="t"))
    assert_crew_failure_shape(out)
    assert out.error_type == "LLMUnavailable"


def test_prose_from_hermes_is_not_trusted_as_an_answer(keyfile):
    http = FakeHttp()
    http.chats = [(200, chat("sure, I did it")), (200, chat("really, I did"))]
    t, _ = make(keyfile, http)
    assert t.dispatch("tester", Turn(task="t")).error_type == "BadModelOutput"


# ── Hermes cannot go around Era's gates ─────────────────────────────────────
@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "era_brain"
    for folder in ("00 - Inbox", "04 - Global"):
        (root / folder).mkdir(parents=True)
    (root / "04 - Global" / "Rules.md").write_text("# Rules\n\nOriginal.\n", encoding="utf-8")
    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    monkeypatch.setenv("ERA_VAULT_VERIFY_SETTLE_S", "0")
    monkeypatch.setattr(vault_bridge, "_brand_catalog", lambda: {})
    get_shared_store().clear()
    yield root
    get_shared_store().clear()


def test_a_hermes_reply_cannot_use_a_capability_the_bot_was_not_granted(keyfile, vault, rec):
    """Hermes (the brain) asks for a write; Era (the gate) refuses. The vault is untouched."""
    http = FakeHttp()
    http.chats = [
        (200, chat(json.dumps(call("vault.create", title="Injected", content="owned")))),
        (200, chat(json.dumps(call("vault.append", title="Rules", content="owned")))),
        (200, chat(json.dumps(final("done")))),
    ]
    t, _ = make(keyfile, http)  # BOT is granted vault.read only
    out = t.dispatch("tester", Turn(task="please write"))
    assert out.success
    assert not list((vault / "00 - Inbox").glob("*")), "a denied create must not create"
    assert (vault / "04 - Global" / "Rules.md").read_text(encoding="utf-8") == (
        "# Rules\n\nOriginal.\n"
    )
    assert out.data["capabilities_used"] == [], "denied calls are observations, not executions"


def test_note_text_reaches_hermes_fenced_as_untrusted_data(keyfile, vault):
    http = FakeHttp()
    http.chats = [
        (200, chat(json.dumps(call("vault.read", title="Rules")))),
        (200, chat(json.dumps(final("read it")))),
    ]
    t, _ = make(keyfile, http)
    assert t.dispatch("tester", Turn(task="read the rules")).success
    followup = json.loads([b for _, u, _, b in http.calls if u.endswith("/chat/completions")][1])[
        "messages"
    ][1]["content"]
    assert '<untrusted id="obs1" capability="vault.read">' in followup


# ── health ──────────────────────────────────────────────────────────────────
def test_health_is_ok_only_when_the_gateway_is_up_the_key_is_set_and_no_tool_is_on(keyfile):
    t, http = make(keyfile)
    h = t.health()
    assert h.ok and h.transport == "hermes_agent" and h.bots == 1
    assert "/health" in http.paths()[0]


@pytest.mark.parametrize("break_it", ["health", "tool", "key", "down"])
def test_health_reports_why_and_never_raises(break_it, keyfile):
    t, http = make(keyfile)
    if break_it == "health":
        http.health = (503, None)
    elif break_it == "tool":
        http.toolsets = (200, toolsets(file=True))
    elif break_it == "key":
        keyfile.write_text("{}", encoding="utf-8")
    else:
        http.health = (200, None)
        http.raise_on_toolsets = OSError("down")
    h = t.health()
    assert h.ok is False and h.detail


def test_health_with_no_bots_is_not_ok(keyfile):
    t = HermesAgentTransport(config_path=keyfile, http=FakeHttp())
    assert t.health().ok is False


# ── selection ───────────────────────────────────────────────────────────────
def test_the_default_transport_is_still_in_process(monkeypatch):
    from crew import runtime

    monkeypatch.delenv("ERA_CREW_TRANSPORT", raising=False)
    runtime.reset_transport(None)
    try:
        assert runtime.get_transport().name == "in_process"
    finally:
        runtime.reset_transport(None)


def test_hermes_is_selected_only_by_an_explicit_opt_in(monkeypatch):
    from crew import runtime

    monkeypatch.setenv("ERA_CREW_TRANSPORT", "hermes_agent")
    runtime.reset_transport(None)
    try:
        assert runtime.get_transport().name == "hermes_agent"
    finally:
        runtime.reset_transport(None)
