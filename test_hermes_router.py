"""
Unit tests for hermes_router — the top-level category router.

These test the DETERMINISTIC surface (fallback heuristic + model-output
validation + brand awareness) without any network or the full Era environment.
The two heavy imports hermes_router makes — `era_dispatch` (pulls in pyyaml) and
`core.ai_provider` (pulls in google-genai) — are replaced with lightweight fakes
in sys.modules BEFORE hermes_router is imported, so the test runs anywhere.

Run:  pytest test_hermes_router.py -v
"""

import os
import sys
import types

import pytest

# --- Fakes for the two heavy dependencies, installed before importing the SUT ---
_FAKE_BRANDS = {
    "brands": {
        "delta_cuisine": {"display_name": "Delta Cuisine", "aliases": ["delta", "dc"]},
        "ceram_quartz": {"display_name": "Ceram Quartz", "aliases": ["ceram", "quartz"]},
    }
}


def _fake_resolve_brand(text, cfg=None):
    """Mirror era_dispatch.resolve_brand: exactly-one-match else None."""
    cfg = cfg or _FAKE_BRANDS
    t = (text or "").lower()
    hits = []
    for key, b in cfg.get("brands", {}).items():
        names = [key.replace("_", " "), (b.get("display_name") or "").lower()]
        names += [a.lower() for a in b.get("aliases", [])]
        if any(n and n in t for n in names):
            hits.append(key)
    hits = list(dict.fromkeys(hits))
    return hits[0] if len(hits) == 1 else None


# The model call is monkeypatched per-test; default returns {} (forces fallback).
_MODEL_RETURN = {"value": {}}


def _fake_generate_json(prompt, *, model=None, system_instruction=None, fallback=None):
    return _MODEL_RETURN["value"] if _MODEL_RETURN["value"] is not None else (fallback or {})


# Local Ollama classifier fake. None => raise (Ollama unavailable); a dict => return it.
_LOCAL_RETURN = {"value": None}


def _fake_classify_local(prompt, system, model, base_url, timeout_s, keep_alive="30m"):
    if _LOCAL_RETURN["value"] is None:
        raise RuntimeError("fake ollama unavailable")
    return _LOCAL_RETURN["value"]


@pytest.fixture(scope="module", autouse=True)
def _install_fakes():
    # Snapshot every sys.modules key we touch so we can restore it on teardown —
    # otherwise the fake `core` / `core.ai_provider` leak into later test files
    # (e.g. test_hermes_wiring imports the real main -> core.utils) and break them.
    _touched = (
        "era_dispatch",
        "core",
        "core.ai_provider",
        "core.text_ollama",
        "core.vision_config",
    )
    _saved = {k: sys.modules.get(k) for k in _touched}
    _saved_env = os.environ.get("HERMES_PROVIDER_ORDER")

    fake_ed = types.ModuleType("era_dispatch")
    fake_ed.load_brands = lambda: _FAKE_BRANDS
    fake_ed.resolve_brand = _fake_resolve_brand
    sys.modules["era_dispatch"] = fake_ed

    fake_ai = types.ModuleType("core.ai_provider")
    fake_ai.generate_json = _fake_generate_json
    fake_ai.DEFAULT_FAST_MODEL = "fake-model"
    # Ensure the `core` package object exists so `from core.ai_provider import ...` works.
    core_pkg = sys.modules.get("core") or types.ModuleType("core")
    core_pkg.__path__ = getattr(core_pkg, "__path__", [])
    sys.modules["core"] = core_pkg
    sys.modules["core.ai_provider"] = fake_ai

    # Fake the local-provider deps so _classify_local never hits a real Ollama.
    fake_txt = types.ModuleType("core.text_ollama")
    fake_txt.classify_json_with_ollama = _fake_classify_local
    sys.modules["core.text_ollama"] = fake_txt
    fake_vc = types.ModuleType("core.vision_config")
    fake_vc.resolve_ollama_base_url = lambda: "http://fake:11434"
    fake_vc.resolve_ollama_timeout = lambda: 5
    sys.modules["core.vision_config"] = fake_vc

    # Existing tests assert Gemini/fallback semantics — pin them to the gemini
    # provider so the new default (ollama-first) doesn't change their meaning.
    # Local-path tests override this env per-test via monkeypatch.setenv.
    os.environ["HERMES_PROVIDER_ORDER"] = "gemini"
    yield

    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    if _saved_env is None:
        os.environ.pop("HERMES_PROVIDER_ORDER", None)
    else:
        os.environ["HERMES_PROVIDER_ORDER"] = _saved_env


@pytest.fixture()
def hermes():
    # Import fresh after fakes are installed.
    import importlib

    import hermes_router

    importlib.reload(hermes_router)
    return hermes_router


def _set_model(value):
    _MODEL_RETURN["value"] = value


def _set_local(value):
    _LOCAL_RETURN["value"] = value


# --------------------------------------------------------------------------- #
# Local provider path (Ollama) — the no-billing default. Uses monkeypatch.setenv
# to select the provider order per-test (fixture pins the rest to "gemini").
# --------------------------------------------------------------------------- #
def test_local_provider_classifies_brandless_cm(hermes, monkeypatch):
    monkeypatch.setenv("HERMES_PROVIDER_ORDER", "ollama")
    _set_local({"category": "cm", "brand": None, "confidence": 0.9, "reason": "content plan"})
    _set_model({"SHOULD_NOT_BE_USED": True})
    d = hermes.route("prepare this week's content plan")
    assert d.category == "cm"
    assert d.brand is None
    assert d.source == "local"  # answered locally, no Gemini quota touched


def test_local_unavailable_falls_through_to_gemini(hermes, monkeypatch):
    monkeypatch.setenv("HERMES_PROVIDER_ORDER", "ollama,gemini")
    _set_local(None)  # ollama down -> _classify_local returns None
    _set_model({"category": "cm", "brand": "delta_cuisine", "confidence": 0.8, "reason": "x"})
    d = hermes.route("plan Delta Cuisine's week")
    assert d.category == "cm"
    assert d.source == "model"  # fell through to Gemini


def test_all_providers_down_uses_deterministic_fallback(hermes, monkeypatch):
    monkeypatch.setenv("HERMES_PROVIDER_ORDER", "ollama,gemini")
    _set_local(None)  # ollama down
    _set_model({})  # gemini junk/quota -> {}
    d = hermes.route("publish the carousel for Ceram Quartz now")
    assert d.source == "fallback"
    assert d.category == "cm"  # brand + CM keyword via heuristic
    assert d.brand == "ceram_quartz"


# --------------------------------------------------------------------------- #
# Model path (Gemini returns valid JSON)
# --------------------------------------------------------------------------- #
def test_model_cm_with_known_brand(hermes):
    _set_model(
        {"category": "cm", "brand": "delta_cuisine", "confidence": 0.9, "reason": "brand week"}
    )
    d = hermes.route("plan Delta Cuisine's week")
    assert d.category == "cm"
    assert d.is_cm is True
    assert d.brand == "delta_cuisine"
    assert d.source == "model"
    assert 0.0 <= d.confidence <= 1.0


def test_model_cm_without_brand_stays_cm(hermes):
    # Brand-less but clearly-CM phrasing: the model returns cm + null brand, and
    # the pipeline must KEEP it in the cm lane (brand=None) so the downstream
    # pending-ask flow can disambiguate — not downgrade it to assistant.
    _set_model({"category": "cm", "brand": None, "confidence": 0.85, "reason": "content plan"})
    d = hermes.route("prepare this week's content plan")
    assert d.category == "cm"
    assert d.is_cm is True
    assert d.brand is None
    assert d.source == "model"


def test_model_assistant(hermes):
    _set_model({"category": "assistant", "brand": None, "confidence": 0.8, "reason": "media"})
    d = hermes.route("play some jazz on Spotify")
    assert d.category == "assistant"
    assert d.is_cm is False
    assert d.brand is None


def test_model_unknown_category_falls_back(hermes):
    # Hallucinated category must be rejected -> deterministic fallback runs.
    _set_model({"category": "banana", "brand": None, "confidence": 0.99})
    d = hermes.route("publish Delta Cuisine's approved carousel")
    assert d.source == "fallback"
    assert d.category == "cm"  # brand + CM keyword -> cm via fallback
    assert d.brand == "delta_cuisine"


def test_model_hallucinated_brand_is_dropped(hermes):
    _set_model({"category": "cm", "brand": "not_a_real_brand", "confidence": 0.7})
    d = hermes.route("plan the week")
    assert d.category == "cm"
    assert d.brand is None  # unknown brand key scrubbed, still CM lane


def test_confidence_clamped(hermes):
    _set_model({"category": "assistant", "brand": None, "confidence": 5.0})
    d = hermes.route("what's the weather")
    assert d.confidence == 1.0


# --------------------------------------------------------------------------- #
# Fallback path (model returns junk / empty)
# --------------------------------------------------------------------------- #
def test_fallback_cm_needs_brand_and_keyword(hermes):
    _set_model({})  # empty -> fallback
    d = hermes.route("publish the carousel for Ceram Quartz now")
    assert d.source == "fallback"
    assert d.category == "cm"
    assert d.brand == "ceram_quartz"


def test_fallback_brand_without_cm_word_is_assistant(hermes):
    _set_model({})
    d = hermes.route("what time is it in Delta Cuisine's timezone")  # brand but no CM verb...
    # "post"/"publish"/etc absent -> stays assistant (conservative)
    assert d.category == "assistant"
    assert d.source == "fallback"


def test_fallback_cm_word_without_brand_is_assistant(hermes):
    _set_model({})
    d = hermes.route("how do I publish a reel in general")
    # CM keyword present but no resolvable brand -> assistant (don't misroute)
    assert d.category == "assistant"


def test_empty_input_is_assistant(hermes):
    _set_model({"category": "cm", "brand": "delta_cuisine", "confidence": 1.0})
    d = hermes.route("   ")
    assert d.category == "assistant"
    assert d.source == "fallback"
    assert d.reason == "empty input"


def test_as_dict_shape(hermes):
    _set_model({"category": "assistant", "brand": None, "confidence": 0.5, "reason": "chat"})
    d = hermes.route("tell me a joke")
    out = d.as_dict()
    assert set(out) == {"category", "brand", "confidence", "reason", "source", "text"}
    assert out["text"] == "tell me a joke"
