"""Declared alternatives: try the first healthy provider, move on only when allowed.

Era Fabric F3 (docs/era-enhancement/ERA_FABRIC_DESIGN.md 5.3). The data lives in
config/fabric/pathways.yaml; this module loads it, REFUSES a file that breaks a rule
(see `validate`), and runs a capability through its steps:

    result = pathways.run("web_search", {
        "llm.gemini.search": lambda: ...,      # each returns a ToolResult (or raises)
        "tool.web_search.ddgs": lambda: ...,
        "deterministic": lambda: ...,
    }, ctx_id=ctx_id)

Rules the runner enforces, whatever the providers do:
* a terminal fault (Denied / NeedsConfirmation / Cancelled) stops the pathway: no
  later provider is tried, ever. Only `fault.alternative` lets it move on;
* a provider whose breaker is OPEN is skipped without being called (core/fabric/health);
  the deterministic floor is never skipped;
* cancellation is never swallowed: CancelledTask / asyncio.CancelledError propagate;
* every hop is a `task.step` event and, with a ctx_id, a step on the TaskContext;
* the result carries `data["provenance"]`: provider, fallback_from, hops. A `quality:
  lower` step that answers after another failed gets its `say` clause (Karim,
  2026-11-10: disclose only when quality differs).

Health is reported by each provider adapter, not here (the crew already reports
through its events; reporting twice would double-count).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from core.fabric import events, faults

DETERMINISTIC = "deterministic"
QUALITIES = ("same", "lower")
WIRINGS = ("pathway", "existing")
# Section 5.4: these fail CLOSED and may never have a pathway around them.
FAIL_CLOSED = frozenset(
    {
        "gate.speaker",
        "gate.input_safety",
        "safety_guard",
        "pending_confirmation",
        "development.activate",
        "vault.write",
        "protocol_one",
        "panic_stop",
    }
)
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "fabric" / "pathways.yaml"
_lock = threading.Lock()
_cache: dict | None = None


class PathwayError(ValueError):
    """pathways.yaml breaks a rule; the fabric refuses it rather than guess."""


@dataclass(frozen=True)
class Step:
    provider: str
    scope: frozenset = field(default_factory=frozenset)
    quality: str = "same"
    fn: str = ""
    say: str = ""


@dataclass(frozen=True)
class Pathway:
    name: str
    wired: str
    steps: tuple[Step, ...]


def _step(name: str, i: int, raw) -> Step:
    if not isinstance(raw, dict) or not str(raw.get("provider") or "").strip():
        raise PathwayError(f"{name} step {i}: needs a provider")
    scope = raw.get("scope") or []
    if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
        raise PathwayError(f"{name} step {i}: scope must be a list of names")
    quality = str(raw.get("quality") or "same")
    if quality not in QUALITIES:
        raise PathwayError(f"{name} step {i}: quality must be one of {QUALITIES}")
    return Step(
        provider=str(raw["provider"]).strip(),
        scope=frozenset(scope),
        quality=quality,
        fn=str(raw.get("fn") or ""),
        say=str(raw.get("say") or ""),
    )


def validate(data) -> dict[str, Pathway]:
    """Parse and check the whole file. Raises PathwayError on the first broken rule."""
    if not isinstance(data, dict) or not data:
        raise PathwayError("pathways.yaml must map capability names to pathways")
    out: dict[str, Pathway] = {}
    for name, body in data.items():
        if name in FAIL_CLOSED:
            raise PathwayError(f"{name} fails closed (section 5.4): it may not have a pathway")
        if not isinstance(body, dict):
            raise PathwayError(f"{name}: expected wired + steps")
        wired = str(body.get("wired") or "")
        if wired not in WIRINGS:
            raise PathwayError(f"{name}: wired must be one of {WIRINGS}")
        raw_steps = body.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PathwayError(f"{name}: needs at least one step")
        steps = tuple(_step(name, i, s) for i, s in enumerate(raw_steps))
        if any(s.provider in FAIL_CLOSED for s in steps):
            raise PathwayError(f"{name}: a fail-closed component cannot be a step")
        for prev, nxt in zip(steps, steps[1:], strict=False):
            wider = nxt.scope - prev.scope
            if wider:
                raise PathwayError(
                    f"{name}: {nxt.provider} would hold {sorted(wider)} that "
                    f"{prev.provider} lacks (a fallback is equal or stricter, never wider)"
                )
        if steps[-1].provider != DETERMINISTIC:
            raise PathwayError(f"{name}: the last step must be deterministic")
        if len({s.provider for s in steps}) != len(steps):
            raise PathwayError(f"{name}: a provider appears twice")
        out[str(name)] = Pathway(str(name), wired, steps)
    return out


def load(path: Path | None = None, *, fresh: bool = False) -> dict[str, Pathway]:
    """The validated pathways (cached). Raises PathwayError / OSError when unusable."""
    global _cache
    with _lock:
        if _cache is not None and path is None and not fresh:
            return _cache
    import yaml

    data = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    parsed = validate(data)
    if path is None:
        with _lock:
            _cache = parsed
    return parsed


def reset() -> None:
    global _cache
    with _lock:
        _cache = None


# ----------------------------------------------------------------------- runner


def _hop(capability: str, provider: str, outcome: str, summary: str, ctx_id, fault=None):
    events.emit(
        "task.step",
        component=provider,
        outcome=outcome,
        summary=summary,
        ctx_id=ctx_id,
        fault=fault,
        provenance={"capability": capability},
    )
    if ctx_id:
        try:
            from core.fabric.context import add_step

            add_step(ctx_id, provider, outcome, summary, capability=capability)
        except Exception:  # noqa: BLE001
            pass


def _call(provider: str, attempt: Callable):
    from core.cancel_registry import CancelledTask
    from core.tool_result import ToolResult

    try:
        return ToolResult.from_any(attempt())
    except (CancelledTask, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 -- asyncio.CancelledError is BaseException
        fault = faults.from_exception(exc, provider)
        return ToolResult.fail(
            message=f"{provider} failed: {fault['detail']}",
            error=fault["detail"],
            error_type=type(exc).__name__,
            data={"fault": fault},
        )


def _finish(result, step: Step | None, hops: list, capability: str, first_failed: str):
    prov = {
        "capability": capability,
        "provider": step.provider if step else None,
        "fallback_from": first_failed or None,
        "hops": hops,
        "disclosed": False,
    }
    if step and first_failed and result.success and step.quality == "lower" and step.say:
        result.message = f"{step.say} {result.message or ''}".strip()
        prov["disclosed"] = True
    if not isinstance(result.data, dict):
        result.data = {}
    inner = result.data.get("provenance")
    if isinstance(inner, dict):
        prov["via"] = inner  # e.g. the crew's own Hermes -> in-process hop (F1)
    result.data["provenance"] = prov
    return result


def run(
    capability: str,
    attempts: Mapping[str, Callable],
    *,
    ctx_id: str | None = None,
    pathway: Pathway | None = None,
):
    """Run `capability` through its declared steps. `attempts` maps a provider name
    to a zero-argument callable; a step with no attempt is skipped (not wired here)."""
    from core.fabric import health
    from core.tool_result import ToolResult

    pw = pathway or load()[capability]
    hops: list[dict] = []
    first_failed = ""
    last = None
    for step in pw.steps:
        attempt = attempts.get(step.provider)
        if attempt is None:
            hops.append({"provider": step.provider, "outcome": "not_wired"})
            continue
        if step.provider != DETERMINISTIC and not health.allow(step.provider):
            hops.append({"provider": step.provider, "outcome": "skipped_open"})
            _hop(capability, step.provider, "info", "skipped: breaker open", ctx_id)
            first_failed = first_failed or step.provider
            continue
        result = _call(step.provider, attempt)
        fault = faults.classify(result, step.provider)
        first = str(result.message or result.error or "").strip().splitlines()
        summary = first[0][:160] if first else ""
        if fault is None:
            hops.append({"provider": step.provider, "outcome": "ok"})
            _hop(capability, step.provider, "ok", summary, ctx_id)
            return _finish(result, step, hops, capability, first_failed)
        hops.append({"provider": step.provider, "outcome": "fault", "kind": fault["kind"]})
        _hop(capability, step.provider, "fault", summary, ctx_id, fault)
        last = (result, step)
        if not faults.may_try_alternative(fault):
            return _finish(result, step, hops, capability, first_failed)
        first_failed = first_failed or step.provider
    if last is not None:
        return _finish(last[0], last[1], hops, capability, first_failed)
    fault = faults.make("Unavailable", capability, "no provider is wired for this pathway")
    return _finish(
        ToolResult.fail(message=fault["detail"], error_type="Unavailable", data={"fault": fault}),
        None,
        hops,
        capability,
        first_failed,
    )
