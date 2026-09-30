"""Canonical tool schema for Era, plus one emitter per provider dialect.

WHY THIS EXISTS
  ``app/tool_declarations.py`` holds Era's 42 tools in Gemini's dialect:
  ``{"name", "description", "parameters"}`` with UPPERCASE type names
  (``"type": "OBJECT"``). Anthropic wants ``{"name", "description",
  "input_schema"}`` with lowercase JSON Schema types. The difference is
  mechanical, but doing it by hand across 42 tools — and then again on every
  future tool — is how two dialects drift apart silently.

  So: ``TOOL_DECLARATIONS`` stays the single source of truth, this module is
  the only place that knows about dialects, and both providers are generated.

THE LOSSLESSNESS PROPERTY
  ``to_gemini_declarations(from_legacy_declarations(TOOL_DECLARATIONS))`` must
  be **byte-identical** to ``TOOL_DECLARATIONS``. That is what makes adopting
  this module a no-op for the live Gemini session rather than a rewrite of it,
  and it is asserted in tools/test_toolspec.py.

  Byte-identical means key ORDER survives too, which is why the type mapping
  rebuilds dicts in place over the original key sequence instead of
  constructing a normalised model and serialising it back out. A normalised
  model would emit ``{type, description, items}`` where the source says
  ``{type, items, description}`` — semantically equal, not byte-equal, and it
  would leave nothing to prove.

CANONICAL FORM
  Lowercase JSON Schema, because that IS the standard — Gemini's uppercase
  enum is the dialect that needs translating, not the other way round. A
  ToolSpec therefore carries a schema you can hand to a validator as-is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ── Dialect maps ────────────────────────────────────────────────────────────
# Gemini's Type enum ↔ JSON Schema's type names. Deliberately explicit rather
# than a .lower() call: an unrecognised value must fail loudly (see
# _map_type), because a typo'd type in a declaration would otherwise reach a
# provider as an unknown schema and be rejected at request time with a much
# worse error than "tool X, property Y has unknown type Z".
_GEMINI_TO_JSON: dict[str, str] = {
    "OBJECT": "object",
    "ARRAY": "array",
    "STRING": "string",
    "INTEGER": "integer",
    "NUMBER": "number",
    "BOOLEAN": "boolean",
    "NULL": "null",
}
_JSON_TO_GEMINI: dict[str, str] = {v: k for k, v in _GEMINI_TO_JSON.items()}

# Schema keywords whose value is itself a schema, or a list/dict of schemas.
# Only "properties" and "items" occur in Era's declarations today; the
# composition keywords are handled anyway so that adding an anyOf tomorrow
# cannot silently skip the type mapping and ship an UPPERCASE type to
# Anthropic.
_SCHEMA_MAP_KEYS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LIST_KEYS = ("anyOf", "allOf", "oneOf", "prefixItems")
_SCHEMA_VALUE_KEYS = ("items", "not", "additionalProperties", "contains")


class ToolSpecError(ValueError):
    """A declaration could not be converted. Names the offending tool."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool, provider-neutral.

    ``parameters`` is a JSON Schema object with lowercase types and the key
    order of the source declaration preserved.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def property_names(self) -> tuple[str, ...]:
        return tuple((self.parameters.get("properties") or {}).keys())

    @property
    def required_names(self) -> tuple[str, ...]:
        req = self.parameters.get("required")
        return tuple(req) if isinstance(req, list) else ()

    @property
    def optional_names(self) -> tuple[str, ...]:
        req = set(self.required_names)
        return tuple(p for p in self.property_names if p not in req)


# ── Type mapping ────────────────────────────────────────────────────────────


def _map_type(value: Any, table: dict[str, str], *, tool: str, path: str) -> Any:
    """Translate one ``type`` value. Idempotent, so a schema already in the
    target dialect passes through untouched — that keeps the converter safe to
    run twice (e.g. a tool authored in lowercase from the start)."""
    if isinstance(value, list):  # JSON Schema union type: ["string", "null"]
        return [_map_type(v, table, tool=tool, path=path) for v in value]
    if not isinstance(value, str):
        raise ToolSpecError(f"{tool}: {path} has a non-string type {value!r}")
    if value in table:
        return table[value]
    # Already in the target dialect?
    if value in table.values():
        return value
    known = ", ".join(sorted(table))
    raise ToolSpecError(f"{tool}: {path} has unknown type {value!r} (expected one of: {known})")


def _map_schema(schema: Any, table: dict[str, str], *, tool: str, path: str = "parameters") -> Any:
    """Deep-copy ``schema``, translating every ``type`` value, preserving key
    order at every level."""
    if isinstance(schema, list):
        return [_map_schema(s, table, tool=tool, path=f"{path}[{i}]") for i, s in enumerate(schema)]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():  # insertion order is the contract
        if key == "type":
            out[key] = _map_type(value, table, tool=tool, path=path)
        elif key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            out[key] = {
                k: _map_schema(v, table, tool=tool, path=f"{path}.{key}.{k}")
                for k, v in value.items()
            }
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            out[key] = [
                _map_schema(v, table, tool=tool, path=f"{path}.{key}[{i}]")
                for i, v in enumerate(value)
            ]
        elif key in _SCHEMA_VALUE_KEYS and isinstance(value, (dict, list)):
            out[key] = _map_schema(value, table, tool=tool, path=f"{path}.{key}")
        else:
            out[key] = value
    return out


# ── Import: legacy declarations → ToolSpec ──────────────────────────────────


def from_legacy_declaration(decl: dict[str, Any]) -> ToolSpec:
    name = decl.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolSpecError(f"declaration is missing a usable 'name': {decl!r}")
    description = decl.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ToolSpecError(f"{name}: missing or empty 'description'")

    params = decl.get("parameters", {})
    if params in (None, {}):
        # A tool with no inputs still needs an object schema on both providers.
        params = {"type": "object", "properties": {}}
    if not isinstance(params, dict):
        raise ToolSpecError(f"{name}: 'parameters' must be an object schema, got {type(params)}")

    return ToolSpec(
        name=name,
        description=description,
        parameters=_map_schema(params, _GEMINI_TO_JSON, tool=name),
    )


def from_legacy_declarations(decls: list[dict[str, Any]]) -> list[ToolSpec]:
    specs = [from_legacy_declaration(d) for d in decls]
    validate(specs)
    return specs


def load_era_tools() -> list[ToolSpec]:
    """Era's live tool surface, read from the single source of truth.

    Imported lazily so this module stays importable (and testable) without
    dragging in ``app.tool_declarations``.
    """
    from app.tool_declarations import TOOL_DECLARATIONS

    return from_legacy_declarations(TOOL_DECLARATIONS)


# ── Emit: Gemini ────────────────────────────────────────────────────────────


def to_gemini(spec: ToolSpec) -> dict[str, Any]:
    """One declaration in Gemini's dialect, ready for
    ``LiveConnectConfig(tools=[{"function_declarations": [...]}])``."""
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": _map_schema(spec.parameters, _JSON_TO_GEMINI, tool=spec.name),
    }


def to_gemini_declarations(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    return [to_gemini(s) for s in specs]


# ── Emit: Anthropic ─────────────────────────────────────────────────────────


def to_anthropic(spec: ToolSpec, *, strict: bool = False) -> dict[str, Any]:
    """One tool in Anthropic's Messages-API shape.

    ``strict=True`` adds ``strict: true`` + ``additionalProperties: false``,
    which guarantees the model's ``tool_use.input`` validates exactly. It
    REFUSES rather than mangles when a tool isn't eligible — closing an object
    whose optional parameters aren't in ``required`` would silently promote
    every optional to mandatory, and Era has 30+ tools with optional
    parameters. Use ``strict_readiness()`` to see which tools qualify.
    """
    schema = json.loads(json.dumps(spec.parameters))  # cheap deep copy
    if strict:
        issue = _strict_issue(spec)
        if issue.blockers:
            raise ToolSpecError(
                f"{spec.name}: not eligible for strict mode — "
                + "; ".join(issue.blockers)
                + ". Fix the declaration or emit this tool with strict=False."
            )
        schema["additionalProperties"] = False

    tool: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": schema,
    }
    if strict:
        tool["strict"] = True  # top-level on the tool, NOT inside tool_choice
    return tool


def to_anthropic_tools(specs: list[ToolSpec], *, strict: bool = False) -> list[dict[str, Any]]:
    return [to_anthropic(s, strict=strict) for s in specs]


# ── Validation and strict-mode readiness ────────────────────────────────────


def validate(specs: list[ToolSpec]) -> None:
    """Structural checks that hold for every provider. Raises on the first
    problem, naming the tool."""
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ToolSpecError(f"duplicate tool name: {spec.name}")
        seen.add(spec.name)

        if spec.parameters.get("type") != "object":
            raise ToolSpecError(
                f"{spec.name}: top-level parameters must be an object schema, "
                f"got {spec.parameters.get('type')!r}"
            )
        props = spec.parameters.get("properties")
        if props is None or not isinstance(props, dict):
            raise ToolSpecError(f"{spec.name}: parameters.properties must be an object")

        # A required name that isn't a declared property is an invalid schema
        # both providers will reject — and it reads as a typo in review, so it
        # is worth catching at import rather than at request time.
        for name in spec.required_names:
            if name not in props:
                raise ToolSpecError(f"{spec.name}: required lists undeclared property {name!r}")


@dataclass(frozen=True)
class StrictIssue:
    """Why a tool can or cannot use Anthropic's strict mode."""

    tool: str
    blockers: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.blockers


def _open_object_paths(schema: Any, path: str = "") -> list[str]:
    """Object subschemas with no ``properties`` — free-form maps. They cannot
    be closed with ``additionalProperties: false`` without rejecting every
    value, so they block strict mode."""
    found: list[str] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object" and not schema.get("properties"):
            found.append(path or "<root>")
        for key, value in (schema.get("properties") or {}).items():
            found += _open_object_paths(value, f"{path}.{key}" if path else key)
        items = schema.get("items")
        if isinstance(items, dict):
            found += _open_object_paths(items, f"{path}[]")
    return found


def _strict_issue(spec: ToolSpec) -> StrictIssue:
    blockers: list[str] = []
    if spec.parameters.get("required") is None and spec.property_names:
        blockers.append("no 'required' key, but the tool declares properties")
    if spec.optional_names:
        blockers.append(
            "optional properties would become mandatory: " + ", ".join(spec.optional_names)
        )
    # The root object of a no-input tool (enter_standby) is legitimately empty
    # and closing it is correct, so only NESTED open objects block.
    nested_open = [p for p in _open_object_paths(spec.parameters) if p != "<root>"]
    if nested_open:
        blockers.append("free-form object properties cannot be closed: " + ", ".join(nested_open))
    return StrictIssue(tool=spec.name, blockers=tuple(blockers))


def strict_readiness(specs: list[ToolSpec]) -> list[StrictIssue]:
    """Per-tool strict-mode audit — the report the migration assessment listed
    as 'still to be inspected'."""
    return [_strict_issue(s) for s in specs]


# ── CLI: regenerate the golden files ────────────────────────────────────────


def _dump(obj: Any) -> str:
    # sort_keys for the goldens: a stable diff matters more than key order in a
    # fixture. Byte-identity of the GEMINI round-trip is asserted separately in
    # the test, without sort_keys, so ordering is still covered.
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Emit Era's tool surface in a provider dialect.")
    parser.add_argument("--dialect", choices=("anthropic", "gemini"), default="anthropic")
    parser.add_argument("--strict", action="store_true", help="anthropic only; may refuse")
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    parser.add_argument("--audit", action="store_true", help="print the strict-readiness report")
    args = parser.parse_args(argv)

    specs = load_era_tools()

    if args.audit:
        report = strict_readiness(specs)
        ok = [i for i in report if i.eligible]
        print(f"{len(ok)}/{len(report)} tools are strict-mode eligible\n")
        for issue in report:
            if issue.eligible:
                print(f"  OK       {issue.tool}")
            else:
                print(f"  BLOCKED  {issue.tool}")
                for b in issue.blockers:
                    print(f"             - {b}")
        return 0

    if args.dialect == "gemini":
        payload: Any = to_gemini_declarations(specs)
    else:
        payload = to_anthropic_tools(specs, strict=args.strict)

    text = _dump(payload)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {len(specs)} tools to {args.out}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
