"""The three reasoning brains Era can use for tasks: Claude, OmniRoute and the local model.

WHAT THIS IS
  * Telemetry: a probe per brain that asks it to answer a real TOOL call (what Claude
    Code needs; a plain ping passed for models that then failed 400 on real runs) and
    records availability, latency, rate-limit signals and the reason when it is down.
  * Choice: the brain the owner asked for by voice ("use Claude", "use OmniRoute", "use
    the local model", "automatic"), kept for the session.
  * Pick: with no explicit choice, rank the live brains for the task's complexity and
    say why. An explicit choice that is down fails over to the next brain and says so.

"Claude" here is OmniRoute's Claude route (owner decision, 2026-11-19), not a separate
login. Paid models are allowed as a last resort inside the OmniRoute combo (owner:
"yes, automatically"); `describe()` names them so the spend is never silent.

Degrade, never raise: a probe that fails is a brain that is down, with a reason.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

BRAINS = ("claude", "omniroute", "local")
AUTO = "auto"

# complexity -> preference order. Local first for light work (owner: local primary);
# heavy reasoning prefers the strongest model that is up.
_ORDER = {
    "light": ("local", "omniroute", "claude"),
    "normal": ("local", "omniroute", "claude"),
    "heavy": ("claude", "omniroute", "local"),
}
_CACHE_TTL_S = 120.0
# The exact Claude model, never the `claude` combo: that combo silently falls through to a
# free model when the login is expired, which made a dead Claude look alive (live, 2026-11-19).
CLAUDE_MODEL = os.environ.get("ERA_BRAIN_CLAUDE_MODEL", "claude/claude-sonnet-5")
_PAID_MARKERS = ("gpt-", "openai/")

_TOOL = [
    {
        "name": "get_time",
        "description": "Returns the current time in a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


@dataclass
class BrainState:
    name: str
    available: bool
    latency_ms: int | None = None
    reason: str = ""
    model: str = ""
    rate_limit: dict = field(default_factory=dict)
    checked_at: float = 0.0

    @property
    def paid(self) -> bool:
        return any(m in self.model.lower() for m in _PAID_MARKERS)


@dataclass
class Decision:
    brain: str | None
    reason: str
    requested: str = AUTO
    failed_over: bool = False

    def describe(self) -> str:
        return self.reason


# ── choice (session memory) ──────────────────────────────────────────────────

_lock = threading.Lock()
_choice = AUTO


def get_choice() -> str:
    return _choice


def set_choice(name: str | None) -> str:
    """Pin a brain, or clear with None/'auto'. Returns the line to say."""
    global _choice
    word = (name or AUTO).strip().lower()
    if word in ("automatic", "default", "normal"):
        word = AUTO
    if word not in BRAINS + (AUTO,):
        return f"I don't know a brain called {name!r}. Routing is unchanged."
    with _lock:
        previous, _choice = _choice, word
    if word == AUTO:
        return "Brain routing is automatic again: I'll pick the best available one."
    return f"Using {_label(word)} for tasks (was {previous})."


def _label(name: str) -> str:
    return {"claude": "Claude (through OmniRoute)", "omniroute": "OmniRoute", "local": "the local model"}.get(
        name, name
    )


# ── probes ───────────────────────────────────────────────────────────────────


def _omniroute_base() -> str:
    return (os.environ.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:20128").rstrip("/")


def _omniroute_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""


def _rate_headers(headers) -> dict:
    out = {}
    try:
        for k, v in headers.items():
            if k.lower().startswith(("x-ratelimit", "retry-after")):
                out[k.lower()] = v
    except Exception:  # noqa: BLE001
        pass
    return out


def _tool_probe(model: str, name: str, timeout_s: float = 45.0, expect: str = "") -> BrainState:
    """Ask `model` through OmniRoute for a tool call; classify the outcome."""
    key = _omniroute_key()
    body = {
        "model": model,
        "max_tokens": 200,
        "tools": _TOOL,
        "tool_choice": {"type": "any"},
        "messages": [{"role": "user", "content": "What time is it in Tunis? Use the tool."}],
    }
    req = urllib.request.Request(
        _omniroute_base() + "/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "authorization": "Bearer " + key,
            "anthropic-version": "2023-06-01",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
            ms = round((time.monotonic() - started) * 1000)
            ok = any(c.get("type") == "tool_use" for c in data.get("content", []))
            answered = str(data.get("model") or model)
            reason = "" if ok else "answered without a tool call"
            if ok and expect and expect not in answered.lower():
                ok, reason = False, f"the route fell back to {answered}"
            return BrainState(
                name,
                ok,
                ms,
                reason,
                answered,
                _rate_headers(resp.headers),
                time.time(),
            )
    except urllib.error.HTTPError as e:
        ms = round((time.monotonic() - started) * 1000)
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        return BrainState(
            name, False, ms, _plain_reason(e.code, msg), model, _rate_headers(e.headers), time.time()
        )
    except Exception as e:  # noqa: BLE001 - down is an answer, not an error
        return BrainState(name, False, None, f"unreachable ({type(e).__name__})", model, {}, time.time())


def _plain_reason(code: int, msg: str) -> str:
    low = (msg or "").lower()
    if code == 401 or "expired" in low or "authenticate" in low:
        return "login expired" if "expired" in low else "not signed in"
    if code == 429 or "cooling down" in low or "rate" in low:
        return "rate limited"
    if code in (402,) or "credit" in low or "payment" in low:
        return "out of credits"
    if code in (502, 503, 504):
        return "gateway could not reach a provider"
    return f"HTTP {code}"


def probe_claude() -> BrainState:
    return _tool_probe(CLAUDE_MODEL, "claude", expect="claude")


def probe_omniroute() -> BrainState:
    return _tool_probe(os.environ.get("ERA_CM_GATEWAY_COMBO", "era-cm"), "omniroute")


def probe_local() -> BrainState:
    model = os.environ.get("ERA_CM_LOCAL_MODEL", "qwen3.5:9b")
    url = os.environ.get("ERA_CM_LOCAL_URL", "http://127.0.0.1:11434").rstrip("/")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=3) as resp:
            names = [m.get("name") for m in json.loads(resp.read()).get("models", [])]
        ms = round((time.monotonic() - started) * 1000)
        if model in names:
            return BrainState("local", True, ms, "", model, {}, time.time())
        return BrainState("local", False, ms, f"model {model} is not installed", model, {}, time.time())
    except Exception as e:  # noqa: BLE001
        return BrainState("local", False, None, f"Ollama unreachable ({type(e).__name__})", model, {}, time.time())


_PROBES: dict[str, Callable[[], BrainState]] = {
    "claude": probe_claude,
    "omniroute": probe_omniroute,
    "local": probe_local,
}
_cache: dict[str, BrainState] = {}


def snapshot(max_age_s: float = _CACHE_TTL_S, probes: dict | None = None) -> dict[str, BrainState]:
    """Live state of all three brains, re-probing any entry older than `max_age_s`."""
    probes = probes or _PROBES
    now = time.time()
    out = {}
    for name in BRAINS:
        cached = _cache.get(name)
        if cached is None or now - cached.checked_at > max_age_s or probes is not _PROBES:
            try:
                state = probes[name]()
            except Exception as e:  # noqa: BLE001
                state = BrainState(name, False, None, f"probe failed ({type(e).__name__})", "", {}, now)
            if probes is _PROBES:
                _cache[name] = state
            cached = state
        out[name] = cached
    return out


def cached_snapshot() -> dict[str, BrainState]:
    """What is known WITHOUT probing (a spoken order must never block on a slow probe).
    A brain never probed counts as up, flagged 'not probed yet'."""
    return {
        n: _cache.get(n) or BrainState(n, True, None, "not probed yet", "", {}, 0.0) for n in BRAINS
    }


def refresh_async() -> None:
    """Re-probe all three in the background; results land in the cache."""

    def _run():
        try:
            snapshot(max_age_s=0.0)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, name="brains-refresh", daemon=True).start()


# ── pick ─────────────────────────────────────────────────────────────────────


def pick(complexity: str = "normal", states: dict | None = None, choice: str | None = None) -> Decision:
    """Choose a brain. An explicit choice wins while it is up; otherwise rank by task."""
    states = states if states is not None else snapshot()
    requested = (choice if choice is not None else get_choice()) or AUTO
    order = _ORDER.get(complexity, _ORDER["normal"])

    def up(n: str) -> bool:
        s = states.get(n)
        return bool(s and s.available)

    def why(n: str) -> str:
        s = states.get(n)
        extra = f", {s.latency_ms} ms" if s and s.latency_ms else ""
        paid = " (PAID model)" if s and s.paid else ""
        return f"{_label(n)}{extra}{paid}"

    if requested in BRAINS:
        if up(requested):
            return Decision(requested, f"Using {why(requested)}, as you asked.", requested)
        # fail over to the STRONGEST remaining brain, whatever the task: the owner asked for
        # a specific one, so the substitute should be as close to it as possible
        for n in _ORDER["heavy"]:
            if n != requested and up(n):
                reason = (states.get(requested).reason if states.get(requested) else "unavailable")
                return Decision(
                    n,
                    f"{_label(requested)} is down ({reason}), so I'm using {why(n)} instead.",
                    requested,
                    True,
                )
        return Decision(None, f"{_label(requested)} is down and so is everything else I can use.", requested, True)

    for n in order:
        if up(n):
            skipped = [x for x in order[: order.index(n)] if not up(x)]
            note = ""
            if skipped:
                note = " " + "; ".join(
                    f"{_label(x)} is down ({states[x].reason or 'unavailable'})" for x in skipped if x in states
                ) + "."
            return Decision(n, f"Best available for {complexity} work: {why(n)}.{note}", AUTO, bool(skipped))
    return Decision(None, "No brain is available right now.", AUTO)


def status_line(states: dict | None = None) -> str:
    """One spoken sentence: what is up, what is down and why."""
    states = states if states is not None else snapshot()
    parts = []
    for n in BRAINS:
        s = states[n]
        parts.append(f"{_label(n)} {'up' if s.available else 'down (' + (s.reason or 'unavailable') + ')'}")
    return "; ".join(parts) + "."


# ── CM route order ───────────────────────────────────────────────────────────


def cm_order(choice: str | None = None) -> tuple[str, ...]:
    """Which route kinds a CM run tries, in order: 'local' and/or 'gateway' (+ combo).

    auto -> the owner's order (local first, OmniRoute fallback). A pinned brain goes
    first and the rest stay as fallbacks, so a pin never turns a recoverable failure
    into a dead job."""
    c = (choice if choice is not None else get_choice()) or AUTO
    if c == "omniroute":
        return ("gateway:omniroute", "local")
    if c == "claude":
        return ("gateway:claude", "gateway:omniroute", "local")
    return ("local", "gateway:omniroute")


if __name__ == "__main__":  # python -m core.llm.brains [status]  (used by the Motion Design launcher)
    import sys

    states = snapshot(0)
    print(status_line(states))
    claude = states.get("claude")
    sys.exit(0 if claude and claude.available else 1)
