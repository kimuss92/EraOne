"""Characterization tests for core/ai_provider.py — the shared Gemini seam.

WHY THESE EXIST
  Six modules import this file (design_studio, self_diagnosis, self_improve,
  agent/{planner,executor,error_handler}) plus hermes_router, and none of them
  wrap it in a try/except. Its quirks are therefore load-bearing contract, not
  implementation detail:

    * generate_json swallows EVERY exception and returns the caller's fallback.
      Callers depend on never seeing an exception from it.
    * generate_json strips markdown code fences before parsing, because the
      model returns ```json blocks even when asked for raw JSON.
    * generate_json prints a DISTINCT [QUOTA] line for 429/RESOURCE_EXHAUSTED.
      That line exists because an exhausted quota once masqueraded as "the
      model chose the fallback" for a whole session — losing it would restore a
      bug that already cost a day.
    * generate_text does NOT catch. Its callers want the failure.
    * get_client caches one client per key, a deliberate latency fix: building
      a genai.Client sets up a fresh HTTP pool, and doing that per call added
      latency to every router, vision and prompt call Era makes.

  These tests pin that behaviour BEFORE the module is refactored to delegate to
  core/llm, so "no behaviour change" is a verified claim rather than an
  intention. They are written against the PUBLIC surface only, so they stay
  valid on both sides of the refactor.
"""

from __future__ import annotations

import importlib

import pytest

from core import ai_provider


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    """Stands in for client.models, recording what it was asked for."""

    def __init__(self, reply="", raises=None):
        self.reply = reply
        self.raises = raises
        self.calls = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.raises is not None:
            raise self.raises
        return FakeResponse(self.reply)


class FakeClient:
    def __init__(self, reply="", raises=None):
        self.models = FakeModels(reply=reply, raises=raises)


@pytest.fixture
def fake_client(monkeypatch):
    """Install a fake client and hand it back so tests can inspect the call."""

    def _install(reply="", raises=None):
        client = FakeClient(reply=reply, raises=raises)
        monkeypatch.setattr(ai_provider, "get_client", lambda api_key=None: client)
        return client

    return _install


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    """Never reach config/api_keys.json or a real network. get_api_key is what
    get_client calls; stubbing it keeps an accidental real path loud."""
    monkeypatch.setattr(ai_provider, "get_api_key", lambda: "test-key-not-real")
    ai_provider._CLIENT_CACHE.clear()
    yield
    ai_provider._CLIENT_CACHE.clear()


# ---------------------------------------------------------------------------
# Model constants — imported by name elsewhere (self_diagnosis, self_improve,
# hermes_router), so renaming or dropping one breaks those call sites.
# ---------------------------------------------------------------------------


def test_default_model_constants_exist():
    assert ai_provider.DEFAULT_FAST_MODEL
    assert ai_provider.DEFAULT_SMART_MODEL
    assert isinstance(ai_provider.DEFAULT_FAST_MODEL, str)
    assert isinstance(ai_provider.DEFAULT_SMART_MODEL, str)


def test_public_surface_is_unchanged():
    """The names six modules import. A refactor may change what is behind
    them; it may not change which exist."""
    for name in (
        "DEFAULT_FAST_MODEL",
        "DEFAULT_SMART_MODEL",
        "GeminiModel",
        "get_client",
        "generate_text",
        "generate_json",
    ):
        assert hasattr(ai_provider, name), f"core.ai_provider lost {name}"


# ---------------------------------------------------------------------------
# get_client caching — the latency fix
# ---------------------------------------------------------------------------


def test_get_client_caches_one_client_per_key(monkeypatch):
    built = []

    class CountingClient:
        def __init__(self, api_key=None):
            built.append(api_key)
            self.models = FakeModels()

    monkeypatch.setattr(ai_provider.genai, "Client", CountingClient)
    ai_provider._CLIENT_CACHE.clear()

    a1 = ai_provider.get_client("key-a")
    a2 = ai_provider.get_client("key-a")
    b1 = ai_provider.get_client("key-b")

    assert a1 is a2, "same key must reuse the cached client (this is the latency fix)"
    assert b1 is not a1, "a different key must get its own client"
    assert built == ["key-a", "key-b"]


def test_get_client_falls_back_to_the_configured_key(monkeypatch):
    seen = []

    class CountingClient:
        def __init__(self, api_key=None):
            seen.append(api_key)
            self.models = FakeModels()

    monkeypatch.setattr(ai_provider.genai, "Client", CountingClient)
    ai_provider._CLIENT_CACHE.clear()
    ai_provider.get_client()
    assert seen == ["test-key-not-real"]


# ---------------------------------------------------------------------------
# GeminiModel
# ---------------------------------------------------------------------------


def test_model_name_resolution_order():
    assert ai_provider.GeminiModel("explicit").model_name == "explicit"
    assert ai_provider.GeminiModel(model="via-kwarg").model_name == "via-kwarg"
    assert ai_provider.GeminiModel(model_name="via-kwarg2").model_name == "via-kwarg2"
    assert ai_provider.GeminiModel().model_name == ai_provider.DEFAULT_FAST_MODEL


def test_generate_content_omits_config_when_nothing_to_configure(fake_client):
    """Pinned because passing an empty GenerateContentConfig is not the same as
    passing none — it overrides provider defaults."""
    client = fake_client(reply="hi")
    ai_provider.GeminiModel("m").generate_content("prompt")
    assert client.models.calls[0]["config"] is None


def test_generate_content_passes_system_instruction(fake_client):
    client = fake_client(reply="hi")
    ai_provider.GeminiModel("m", "be terse").generate_content("prompt")
    cfg = client.models.calls[0]["config"]
    assert cfg is not None
    assert cfg.system_instruction == "be terse"


def test_generate_content_passes_response_mime_type(fake_client):
    client = fake_client(reply="{}")
    ai_provider.GeminiModel("m").generate_content("p", response_mime_type="application/json")
    assert client.models.calls[0]["config"].response_mime_type == "application/json"


def test_generate_content_forwards_model_and_contents(fake_client):
    client = fake_client(reply="hi")
    ai_provider.GeminiModel("my-model").generate_content("the prompt")
    call = client.models.calls[0]
    assert call["model"] == "my-model"
    assert call["contents"] == "the prompt"


# ---------------------------------------------------------------------------
# generate_text — deliberately does NOT catch
# ---------------------------------------------------------------------------


def test_generate_text_strips_whitespace(fake_client):
    fake_client(reply="  hello  \n")
    assert ai_provider.generate_text("p") == "hello"


def test_generate_text_handles_a_none_text_attribute(fake_client):
    fake_client(reply=None)
    assert ai_provider.generate_text("p") == ""


def test_generate_text_propagates_errors(fake_client):
    """Its callers want the failure — self_improve must not silently write code
    from an empty string."""
    fake_client(raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        ai_provider.generate_text("p")


def test_generate_text_uses_the_requested_model(fake_client):
    client = fake_client(reply="x")
    ai_provider.generate_text("p", model=ai_provider.DEFAULT_SMART_MODEL)
    assert client.models.calls[0]["model"] == ai_provider.DEFAULT_SMART_MODEL


# ---------------------------------------------------------------------------
# generate_json — swallows everything, strips fences, flags quota
# ---------------------------------------------------------------------------


def test_generate_json_parses_plain_json(fake_client):
    fake_client(reply='{"category": "cm", "confidence": 0.9}')
    assert ai_provider.generate_json("p") == {"category": "cm", "confidence": 0.9}


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '```json{"a": 1}```',
        '  ```json\n{"a": 1}\n```  ',
    ],
)
def test_generate_json_strips_code_fences(fake_client, reply):
    """The model returns fenced blocks even when asked for raw JSON."""
    fake_client(reply=reply)
    assert ai_provider.generate_json("p") == {"a": 1}


def test_generate_json_requests_the_json_mime_type(fake_client):
    client = fake_client(reply="{}")
    ai_provider.generate_json("p")
    assert client.models.calls[0]["config"].response_mime_type == "application/json"


def test_generate_json_returns_fallback_on_unparseable_output(fake_client):
    fake_client(reply="not json at all")
    assert ai_provider.generate_json("p", fallback={"category": "assistant"}) == {
        "category": "assistant"
    }


def test_generate_json_returns_empty_dict_when_no_fallback_given(fake_client):
    fake_client(reply="not json")
    assert ai_provider.generate_json("p") == {}


def test_generate_json_never_raises(fake_client):
    """hermes_router and design_studio call this with no try/except. If it can
    raise, an utterance takes Era's routing down."""
    for boom in (RuntimeError("x"), ValueError("y"), KeyError("z"), TimeoutError()):
        fake_client(raises=boom)
        assert ai_provider.generate_json("p", fallback={"ok": False}) == {"ok": False}


def test_generate_json_flags_quota_distinctly(fake_client, capsys):
    """The [QUOTA] line exists because an exhausted quota once looked exactly
    like 'the model chose the fallback' for an entire session. Losing this
    restores a bug that already cost a day of debugging."""
    fake_client(raises=RuntimeError("429 RESOURCE_EXHAUSTED: quota gone"))
    assert ai_provider.generate_json("p", fallback={"f": 1}) == {"f": 1}
    out = capsys.readouterr().out
    assert "[QUOTA]" in out
    assert "generate_json" in out


def test_generate_json_ordinary_error_is_logged_without_the_quota_marker(fake_client, capsys):
    fake_client(raises=RuntimeError("connection reset"))
    ai_provider.generate_json("p", fallback={})
    out = capsys.readouterr().out
    assert "[QUOTA]" not in out
    assert "connection reset" in out


def test_generate_json_passes_the_system_instruction(fake_client):
    client = fake_client(reply="{}")
    ai_provider.generate_json("p", system_instruction="classify this")
    assert client.models.calls[0]["config"].system_instruction == "classify this"


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------


def test_module_imports_without_credentials(monkeypatch):
    """Importing must not require a key — era_doctor and the test suite import
    this module on machines with no config at all."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    importlib.reload(ai_provider)
    assert ai_provider.DEFAULT_FAST_MODEL
