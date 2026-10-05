"""NousExecutor: Nous Portal's OpenAI-compatible API as a crew provider.

The HTTP layer is injected, so nothing here reaches Nous, and the key is a made-up string: the
real key file is never read. What is pinned: the key is read at call time and goes only into a
Bearer header on an https *.nousresearch.com URL; a 429 carries its Retry-After to the router;
a bad base URL or a missing key disables Nous without breaking Era; and, with the shipped
config, Nous is out of the `standard` tier.
"""

from __future__ import annotations

import json

import pytest

from crew import router as cr
from crew.cache import CrewCaches
from crew.providers import (
    DEFAULT_NOUS_BASE,
    DEFAULT_NOUS_MODEL,
    NOUS_KEY_ENTRY,
    NousExecutor,
    RateLimited,
    UnconfiguredExecutor,
    default_executors,
    read_nous_key,
)
from crew.router import Budget, Policy, RoutedLLM, Router
from crew.transport import BotSpec, Turn

KEY = "synthetic-nous-key-for-tests-0000"


class FakeHttp:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self.body = body if body is not None else self.ok_body("hello")
        self.headers = headers or {}
        self.calls = []

    @staticmethod
    def ok_body(text, model="poolside/laguna-xs-2.1:free", pin=12, pout=5):
        return {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": pin, "completion_tokens": pout},
        }

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.status, self.body, dict(self.headers)


@pytest.fixture
def keyfile(tmp_path):
    p = tmp_path / "api_keys.json"
    p.write_text(json.dumps({NOUS_KEY_ENTRY: KEY, "other": "x"}), encoding="utf-8")
    return p


def make(keyfile, http=None, **kw):
    http = http or FakeHttp()
    return NousExecutor(config_path=keyfile, http=http, **kw), http


# ── configuration ───────────────────────────────────────────────────────────
def test_defaults_and_config_overrides():
    ex = NousExecutor()
    assert ex._base == DEFAULT_NOUS_BASE and ex.alias_for("standard") == DEFAULT_NOUS_MODEL
    cfg = {
        "nous": {
            "base_url": "https://portal.nousresearch.com/v1/",
            "model": "a/b",
            "models": {"critical": "c/d"},
        }
    }
    ex = NousExecutor.from_config(cfg)
    assert ex._base == "https://portal.nousresearch.com/v1"
    assert ex.alias_for("trivial") == "a/b" and ex.alias_for("critical") == "c/d"


@pytest.mark.parametrize(
    "bad",
    [
        "http://inference-api.nousresearch.com/v1",  # not https
        "https://evil.example.com/v1",
        "https://nousresearch.com.evil.com/v1",  # suffix trick
        "https://evilnousresearch.com/v1",
        "https://127.0.0.1:8642/v1",
        "",
    ],
)
def test_the_base_url_must_be_https_on_nousresearch_so_the_key_cannot_leave(bad):
    with pytest.raises(ValueError):
        NousExecutor(base_url=bad)


@pytest.mark.parametrize(
    "ok", ["https://inference-api.nousresearch.com/v1", "https://nousresearch.com/v1"]
)
def test_nous_hosts_are_accepted(ok):
    NousExecutor(base_url=ok)


def test_a_bad_configured_url_disables_nous_without_breaking_era():
    execs = default_executors({"nous": {"base_url": "http://evil.example/v1"}})
    assert isinstance(execs["nous"], UnconfiguredExecutor)
    ok, why = execs["nous"].available()
    assert not ok and "nousresearch.com" in why
    assert {"ollama", "claude_cli", "nous", "openai"} == set(execs)


def test_the_shipped_config_builds_a_real_nous_executor():
    from core.llm.router import routing_policy

    ex = default_executors(routing_policy())["nous"]
    assert isinstance(ex, NousExecutor) and ex.alias_for("standard") == DEFAULT_NOUS_MODEL


# ── availability ────────────────────────────────────────────────────────────
def test_no_key_means_unavailable_with_the_reason(tmp_path):
    p = tmp_path / "k.json"
    p.write_text(json.dumps({"other": "x"}), encoding="utf-8")
    ok, why = NousExecutor(config_path=p, http=FakeHttp()).available()
    assert not ok and NOUS_KEY_ENTRY in why


@pytest.mark.parametrize("value", ["", "  ", "placeholder", 5, None])
def test_an_empty_or_placeholder_key_is_no_key(tmp_path, value):
    p = tmp_path / "k.json"
    p.write_text(json.dumps({NOUS_KEY_ENTRY: value}), encoding="utf-8")
    assert read_nous_key(p) == ""


def test_with_a_key_it_is_available(keyfile):
    assert make(keyfile)[0].available() == (True, "")


def test_the_real_http_layer_is_refused_under_pytest(keyfile):
    ok, why = NousExecutor(config_path=keyfile).available()  # no injected http
    assert not ok and "pytest" in why


# ── the call ────────────────────────────────────────────────────────────────
def test_the_request_shape_and_where_the_key_goes(keyfile):
    ex, http = make(keyfile)
    ex.call("be brief", "what are the rules", "standard", 30.0)
    method, url, headers, body, timeout = http.calls[0]
    sent = json.loads(body)
    assert method == "POST" and url == f"{DEFAULT_NOUS_BASE}/chat/completions" and timeout == 30.0
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in url and KEY.encode() not in body
    assert sent["model"] == DEFAULT_NOUS_MODEL and sent["stream"] is False
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert set(sent) == {"model", "messages", "stream"}, "no tools, no extras"


def test_the_key_is_read_at_call_time(keyfile):
    ex, http = make(keyfile)
    ex.call("s", "u", "standard", 5)
    keyfile.write_text(json.dumps({NOUS_KEY_ENTRY: "rotated-synthetic-2222"}), encoding="utf-8")
    ex.call("s", "u2", "standard", 5)
    assert [c[2]["Authorization"] for c in http.calls] == [
        f"Bearer {KEY}",
        "Bearer rotated-synthetic-2222",
    ]


def test_a_removed_key_fails_the_call_without_sending_anything(keyfile):
    ex, http = make(keyfile)
    keyfile.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match=NOUS_KEY_ENTRY):
        ex.call("s", "u", "standard", 5)
    assert http.calls == []


def test_the_reply_tokens_and_served_model_are_returned(keyfile):
    body = FakeHttp.ok_body('{"final":"x"}', model="served/m", pin=40, pout=9)
    ex, _ = make(keyfile, FakeHttp(body=body))
    reply = ex.call("s", "u", "standard", 5)
    assert (reply.text, reply.tokens_in, reply.tokens_out, reply.model) == (
        '{"final":"x"}',
        40,
        9,
        "served/m",
    )


def test_a_per_tier_model_override(keyfile):
    ex, http = make(keyfile, models={"critical": "big/model"})
    ex.call("s", "u", "critical", 5)
    ex.call("s", "u", "standard", 5)
    assert [json.loads(c[3])["model"] for c in http.calls] == ["big/model", DEFAULT_NOUS_MODEL]


def test_rate_limit_headers_are_kept_so_the_real_limits_can_be_observed(keyfile):
    http = FakeHttp(
        headers={
            "x-ratelimit-remaining-requests": "49",
            "x-ratelimit-limit-requests": "50",
            "content-type": "application/json",
            "retry-after": "3",
        }
    )
    ex, _ = make(keyfile, http)
    ex.call("s", "u", "standard", 5)
    assert ex.last_rate_limit == {
        "x-ratelimit-remaining-requests": "49",
        "x-ratelimit-limit-requests": "50",
        "retry-after": "3",
    }


# ── failures ────────────────────────────────────────────────────────────────
def test_a_429_raises_rate_limited_with_its_retry_after(keyfile):
    ex, _ = make(keyfile, FakeHttp(status=429, body=None, headers={"retry-after": "120"}))
    with pytest.raises(RateLimited) as err:
        ex.call("s", "u", "standard", 5)
    assert err.value.retry_after_s == 120.0


@pytest.mark.parametrize("header", ["soon", "", "-5"])
def test_an_unreadable_retry_after_is_zero(keyfile, header):
    ex, _ = make(keyfile, FakeHttp(status=429, body=None, headers={"retry-after": header}))
    with pytest.raises(RateLimited) as err:
        ex.call("s", "u", "standard", 5)
    assert err.value.retry_after_s == 0.0


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_reported_without_echoing_it(keyfile, status):
    ex, _ = make(keyfile, FakeHttp(status=status, body={"error": f"bad key {KEY}"}))
    with pytest.raises(RuntimeError) as err:
        ex.call("s", "u", "standard", 5)
    assert "rejected the key" in str(err.value) and KEY not in str(err.value)


@pytest.mark.parametrize("status", [500, 502, 404])
def test_other_failures_are_a_plain_runtime_error(keyfile, status):
    ex, _ = make(keyfile, FakeHttp(status=status, body=None))
    with pytest.raises(RuntimeError, match=str(status)):
        ex.call("s", "u", "standard", 5)


def test_an_unreadable_success_body_gives_empty_text_not_a_made_up_answer(keyfile):
    ex, _ = make(keyfile, FakeHttp(status=200, body={"unexpected": True}))
    assert ex.call("s", "u", "standard", 5).text == ""


# ── in the router ───────────────────────────────────────────────────────────
ROOMY = {"nous_portal": {"daily_turns": 50}, "ollama": {"always_available": True}}
TASK_BOT = BotSpec(id="plain", role="r", description="d", prompt="p", capabilities=())


class Down:
    name = "ollama"

    def available(self):
        return False, "not running"

    def alias_for(self, tier):
        return ""

    def call(self, *a):
        raise RuntimeError("down")


def routed(tmp_path, nous, clock=None):
    policy = Policy({"trivial": ("ollama", "nous")}, ROOMY, {})
    kw = {"clock": clock} if clock else {}
    execs = {"ollama": Down(), "nous": nous}
    router = Router(policy, Budget(ROOMY, tmp_path / "b.json"), execs, **kw)
    return RoutedLLM(router, CrewCaches.from_config({"response_dir": str(tmp_path / "rc")}))


class Rec:
    def __init__(self):
        self.lines = []

    def info(self, line):
        self.lines.append(line)

    warning = info


def test_nous_answers_a_trivial_turn_when_ollama_is_down_and_is_budgeted_as_nous_portal(
    tmp_path, keyfile, monkeypatch
):
    rec = Rec()
    monkeypatch.setattr(cr, "router_logger", rec)
    ex, http = make(keyfile, FakeHttp(body=FakeHttp.ok_body(json.dumps({"final": "hi"}))))
    llm = routed(tmp_path, ex)
    out = llm.ask(TASK_BOT, Turn(task="hello"), "sys", "usr")
    assert out == {"final": "hi"}
    assert llm.router.budget.used("nous") == 1
    assert rec.lines == [
        "[crew] bot=plain tier=trivial provider=nous tokens_in=12 tokens_out=5 cached=false"
    ]


def test_a_nous_429_cools_it_down_for_the_retry_after_it_gave(tmp_path, keyfile):
    t = [0.0]
    http = FakeHttp(status=429, body=None, headers={"retry-after": "300"})
    ex, _ = make(keyfile, http)
    llm = routed(tmp_path, ex, clock=lambda: t[0])
    first = llm.ask(TASK_BOT, Turn(task="hello"), "sys", "a")
    assert "__unavailable__" in first and "429" in first["__unavailable__"]
    calls = len(http.calls)
    t[0] += cr.COOLDOWN_S + 1
    llm.ask(TASK_BOT, Turn(task="hello"), "sys", "b")
    assert len(http.calls) == calls, "still inside the 300 s it asked for"
    t[0] += 300
    http.status, http.body = 200, FakeHttp.ok_body(json.dumps({"final": "back"}))
    assert llm.ask(TASK_BOT, Turn(task="hello"), "sys", "c") == {"final": "back"}


def test_with_no_key_nous_is_skipped_with_the_reason(tmp_path):
    p = tmp_path / "none.json"
    p.write_text("{}", encoding="utf-8")
    ex = NousExecutor(config_path=p, http=FakeHttp())
    out = routed(tmp_path, ex).ask(TASK_BOT, Turn(task="hello"), "sys", "usr")
    assert NOUS_KEY_ENTRY in out["__unavailable__"]


def test_the_shipped_config_keeps_nous_out_of_standard_but_in_trivial():
    from core.llm.router import routing_policy

    p = Policy.from_mapping(routing_policy())
    assert "nous" not in p.tiers["standard"]
    assert "nous" in p.tiers["trivial"] and "nous" in p.tiers["critical"]


# ── the probe script ────────────────────────────────────────────────────────
def test_the_probe_says_so_when_there_is_no_key(tmp_path, monkeypatch, capsys):
    import crew.providers as prov
    from tools import probe_nous

    p = tmp_path / "k.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(prov, "CONFIG_PATH", p)
    assert probe_nous.main([]) == 2
    assert "NO KEY" in capsys.readouterr().out


def test_the_probe_reports_limits_and_never_prints_the_key(tmp_path, monkeypatch, capsys):
    import crew.providers as prov
    from tools import probe_nous

    p = tmp_path / "k.json"
    p.write_text(json.dumps({NOUS_KEY_ENTRY: KEY}), encoding="utf-8")
    monkeypatch.setattr(prov, "CONFIG_PATH", p)
    listing = {"data": [{"id": DEFAULT_NOUS_MODEL, "context_length": 262144}, {"id": "x/y:free"}]}
    rl = {"x-ratelimit-remaining-requests": "49", "content-type": "x"}

    def fake_https(method, url, headers, body, timeout):
        if url.endswith("/models"):
            return 200, listing, rl
        return 200, FakeHttp.ok_body('{"final": "ok"}'), rl

    monkeypatch.setattr(prov, "_https_json", fake_https)
    monkeypatch.setattr(probe_nous, "_https_json", fake_https)
    assert probe_nous.main([]) == 0
    out = capsys.readouterr().out
    assert KEY not in out
    assert "configured slug listed: True" in out and "262144" in out
    assert "x-ratelimit-remaining-requests" in out and "tokens in/out: 12/5" in out
