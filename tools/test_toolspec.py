"""Tests for core/llm/toolspec.py — the canonical tool schema and its emitters.

The properties under test are the ones whose failure is expensive and silent:

  * LOSSLESSNESS — re-emitting Era's tools in Gemini's dialect must be
    BYTE-identical to app/tool_declarations.py, key order included. This is
    what makes adopting toolspec a no-op for the live session rather than a
    rewrite of it. If it ever stops holding, the live voice loop is the thing
    that breaks, and it breaks at connect time on a user's machine.
  * DIALECT COMPLETENESS — not one UPPERCASE Gemini type may survive into the
    Anthropic output, at any nesting depth. A missed nested type is exactly the
    failure a hand migration produces, and it surfaces as a 400 from the
    provider rather than as anything readable.
  * NON-VACUITY — every assertion below is paired with a count check. This repo
    has a documented case of a source-text validator that read 0 of 0 files and
    passed, which is strictly worse than failing: it reported health while
    seeing nothing. A test that asserts "no uppercase types in an empty list"
    is the same bug wearing a different hat.
  * NO SHARED MUTABLE STATE — the round trip must not achieve identity by
    handing back the source dicts. If it did, a later caller mutating the
    emitted schema would silently rewrite TOOL_DECLARATIONS for the whole
    process, and the losslessness test would pass while proving nothing.

The golden file (tools/golden/tool_declarations_anthropic.json) is regenerated
with:

    python -m core.llm.toolspec --dialect anthropic --out tools/golden/tool_declarations_anthropic.json

A diff there is not automatically a failure — it is the review surface for any
change to Era's tool surface, which is the point of committing it.
"""

import json
from pathlib import Path

import pytest

from app.tool_declarations import TOOL_DECLARATIONS
from core.llm.toolspec import (
    ToolSpec,
    ToolSpecError,
    from_legacy_declarations,
    load_era_tools,
    strict_readiness,
    to_anthropic,
    to_anthropic_tools,
    to_gemini,
    to_gemini_declarations,
    validate,
)

# Era's real tool count. Hardcoded on purpose: it is the tripwire that turns a
# silently-empty import into a failure. Update it deliberately when a tool is
# added or removed — that edit is the review signal.
# 42 -> 43 on 2026-07-31: `vault_note` added, Era's write path into her own
# Obsidian vault. She could read the vault but not write it, so an interview
# produced nothing (actions/vault_writer.py).
# 43 -> 44 on 2026-09-15: `gmail_control` added — read, importance-ranking and
# (confirmation-gated) sending against the user's Gmail (actions/gmail_control.py).
# 44 -> 45 on 2026-09-16: `protocol_one` added — the project health guardian
# (actions/protocol_one.py); launch is gated on the user's own words.
# 45 -> 46 on 2026-09-16: `protocol_zero` added — keeps prompt.txt's capability
# index in sync with these declarations (actions/protocol_zero.py).
# 46 -> 47 on 2026-09-17: `voice_lock` added — owner voice check on/off by voice;
# turning it off is gated on the user's own words.
# 47 -> 48 on 2026-09-17: `scheduled_task` added — the user's own scheduled tasks
# (core/task_scheduler.py); risky runs park for approval unless waived.
# 50 -> 51 on 2026-09-19: `claude_bridge` added — two-way conversation with the
# Claude Code desktop app; sends are gated on the user naming Claude.
# 51 -> 52 on 2026-09-28: `self_heal` added — autonomous, scoped self-repair from
# logged tracebacks (core/self_heal.py); propose-only until the user enables auto.
# 52 -> 53 on 2026-09-29: `capability_check` added — answers "what can you do?"
# from the live declarations instead of from the prompt (acceptance point 3).
# 53 -> 56: visual_engine, autonomous_development and run_feature.
# 56 -> 58 on 2026-10-04: `vault_bridge` (locked, read-back-verified vault read/search/
# create/append plus a user-approved `replace`; core/vault_bridge.py) and `crew` (Laura
# and the specialists; crew/). Neither has a delete.
# 58 -> 57 on 2026-10-06: `claude_bridge` removed — Era no longer drives or reads the
# Claude Code GUI (window-polling gave false statuses, e.g. "still working, 3 min").
# Self-improve uses `claude -p` internally; core/claude_headless.py is kept as a library.
EXPECTED_TOOL_COUNT = 57

# Tools declaring at least one required parameter. Pinned for the same reason as
# EXPECTED_TOOL_COUNT: it is the non-vacuity floor for test_required_sets_survive,
# and a change means a tool gained or lost a required parameter — which is a
# behavioural change to how the model is obliged to call it, worth reviewing.
# 29 -> 30 on 2026-07-31: `vault_note` requires `content` — there is nothing to
# save without it, and defaulting it to "" would let the model file empty notes.
# 30 -> 31 on 2026-09-15: `gmail_control` requires `action` — every other
# parameter is action-specific, and a call with no action has nothing to do.
# 31 -> 32 on 2026-09-16: `protocol_one` requires `action`, so a bare call can
# never be read as a launch; the dispatcher also checks the user's own words.
# 32 -> 33 on 2026-09-16: `protocol_zero` requires `action`, for the same reason.
# 33 -> 34 on 2026-09-17: `scheduled_task` requires `action`.
# 36 -> 37 on 2026-09-19: `claude_bridge` requires `action`.
# Three new tools each require a command/action/feature_id.
# 40 -> 42 on 2026-10-04: `vault_bridge` and `crew` each require `action`, so a bare
# call can never be read as a write or as a task for the crew.
# 42 -> 41 on 2026-10-06: `claude_bridge` (required `action`) removed.
EXPECTED_TOOLS_WITH_REQUIRED = 41

GOLDEN_PATH = Path(__file__).parent / "golden" / "tool_declarations_anthropic.json"

_GEMINI_TYPE_TOKENS = frozenset(
    {"OBJECT", "ARRAY", "STRING", "INTEGER", "NUMBER", "BOOLEAN", "NULL"}
)


@pytest.fixture(scope="module")
def specs() -> list[ToolSpec]:
    return load_era_tools()


# ---------------------------------------------------------------------------
# Non-vacuity: the subject actually exists
# ---------------------------------------------------------------------------


def test_source_declarations_are_present():
    """Guards against the empty-subject failure mode directly."""
    assert len(TOOL_DECLARATIONS) == EXPECTED_TOOL_COUNT, (
        f"app/tool_declarations.py has {len(TOOL_DECLARATIONS)} tools, expected "
        f"{EXPECTED_TOOL_COUNT}. If a tool was added or removed on purpose, update "
        f"EXPECTED_TOOL_COUNT here and regenerate the golden file."
    )


def test_every_declaration_converts(specs):
    assert len(specs) == EXPECTED_TOOL_COUNT
    assert len({s.name for s in specs}) == EXPECTED_TOOL_COUNT, "tool names must be unique"
    assert all(s.description.strip() for s in specs), "every tool needs a description"


# ---------------------------------------------------------------------------
# Losslessness: Gemini round trip is byte-identical
# ---------------------------------------------------------------------------


def test_gemini_round_trip_is_byte_identical(specs):
    """The load-bearing test. No sort_keys — key ORDER is part of the claim."""
    emitted = to_gemini_declarations(specs)
    assert json.dumps(emitted, ensure_ascii=False) == json.dumps(
        TOOL_DECLARATIONS, ensure_ascii=False
    )


def test_gemini_round_trip_is_identical_per_tool(specs):
    """Same property, asserted per tool so a failure names the culprit instead
    of dumping 55 KB of JSON diff."""
    emitted = {t["name"]: t for t in to_gemini_declarations(specs)}
    source = {t["name"]: t for t in TOOL_DECLARATIONS}
    assert emitted.keys() == source.keys()
    for name in source:
        assert json.dumps(emitted[name], ensure_ascii=False) == json.dumps(
            source[name], ensure_ascii=False
        ), f"{name}: Gemini re-emission differs from the source declaration"


def test_round_trip_does_not_alias_the_source(specs):
    """Identity must come from faithful conversion, not from returning the same
    objects — otherwise a caller mutating the result would rewrite
    TOOL_DECLARATIONS process-wide and this suite would still pass."""
    emitted = to_gemini_declarations(specs)
    by_name = {t["name"]: t for t in emitted}
    for src in TOOL_DECLARATIONS:
        out = by_name[src["name"]]
        assert out is not src
        assert out["parameters"] is not src["parameters"]
        src_props = src["parameters"].get("properties") or {}
        out_props = out["parameters"].get("properties") or {}
        for prop in src_props:
            assert out_props[prop] is not src_props[prop], f"{src['name']}.{prop} is aliased"

    # Prove it: mutating the emitted copy must not touch the source.
    emitted[0]["parameters"]["properties"].clear()
    assert TOOL_DECLARATIONS[0]["parameters"]["properties"], "source was mutated through the copy"


# ---------------------------------------------------------------------------
# Dialect completeness: the Anthropic output
# ---------------------------------------------------------------------------


def _collect_types(schema, out: list[str]) -> list[str]:
    """Every ``type`` value at every depth."""
    if isinstance(schema, dict):
        t = schema.get("type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend(x for x in t if isinstance(x, str))
        for value in schema.values():
            _collect_types(value, out)
    elif isinstance(schema, list):
        for item in schema:
            _collect_types(item, out)
    return out


def test_anthropic_shape(specs):
    tools = to_anthropic_tools(specs)
    assert len(tools) == EXPECTED_TOOL_COUNT
    for tool in tools:
        assert set(tool) == {"name", "description", "input_schema"}, (
            f"{tool.get('name')}: unexpected keys {sorted(tool)}"
        )
        assert "parameters" not in tool, "Anthropic uses input_schema, not parameters"
        assert tool["input_schema"]["type"] == "object"
        assert isinstance(tool["input_schema"].get("properties"), dict)


def test_anthropic_has_no_gemini_types(specs):
    """The completeness check, with its own non-vacuity guard: we must have
    actually inspected a realistic number of type values."""
    tools = to_anthropic_tools(specs)
    found: list[str] = []
    for tool in tools:
        _collect_types(tool["input_schema"], found)

    assert len(found) >= EXPECTED_TOOL_COUNT, (
        f"only {len(found)} type values inspected across {len(tools)} tools — "
        "the walker is not reaching the schemas"
    )
    leaked = sorted({t for t in found if t in _GEMINI_TYPE_TOKENS})
    assert not leaked, f"UPPERCASE Gemini types survived into the Anthropic output: {leaked}"
    assert all(t == t.lower() for t in found), "every JSON Schema type must be lowercase"


def test_anthropic_nested_schemas_are_converted(specs):
    """The two non-trivial shapes in Era's surface — an array with a typed
    ``items`` and a free-form object property. A converter that only walked the
    top level would pass every other test in this file."""
    by_name = {t["name"]: t for t in to_anthropic_tools(specs)}

    items = by_name["web_search"]["input_schema"]["properties"]["items"]
    assert items["type"] == "array"
    assert items["items"]["type"] == "string", "nested items schema was not converted"

    fields = by_name["browser_control"]["input_schema"]["properties"]["fields"]
    assert fields["type"] == "object"
    assert "properties" not in fields, "free-form object must stay free-form"


def test_required_sets_survive(specs):
    """``required`` is what makes the model supply the parameter at all; a
    dropped entry degrades quietly into a missing-argument tool call."""
    source = {t["name"]: t["parameters"].get("required") for t in TOOL_DECLARATIONS}
    checked = 0
    for tool in to_anthropic_tools(specs):
        assert tool["input_schema"].get("required") == source[tool["name"]]
        if source[tool["name"]]:
            checked += 1
    assert checked == EXPECTED_TOOLS_WITH_REQUIRED, (
        f"{checked} tools have a non-empty required set, expected "
        f"{EXPECTED_TOOLS_WITH_REQUIRED}. If a required parameter was added or "
        f"removed on purpose, update EXPECTED_TOOLS_WITH_REQUIRED."
    )


def test_descriptions_survive_verbatim(specs):
    """Descriptions carry Era's routing rules and em dashes; a re-encoding or a
    truncation here changes model behaviour without failing anything else."""
    source = {t["name"]: t["description"] for t in TOOL_DECLARATIONS}
    for tool in to_anthropic_tools(specs):
        assert tool["description"] == source[tool["name"]]
    assert any("—" in d for d in source.values()), "expected non-ASCII text in the fixtures"


# ---------------------------------------------------------------------------
# Golden file
# ---------------------------------------------------------------------------


def test_anthropic_output_matches_golden(specs):
    assert GOLDEN_PATH.exists(), (
        f"missing golden file {GOLDEN_PATH}. Regenerate with:\n"
        "  python -m core.llm.toolspec --dialect anthropic "
        "--out tools/golden/tool_declarations_anthropic.json"
    )
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert len(golden) == EXPECTED_TOOL_COUNT

    current = json.loads(json.dumps(to_anthropic_tools(specs), sort_keys=True))
    golden_sorted = json.loads(json.dumps(golden, sort_keys=True))
    assert current == golden_sorted, (
        "the emitted Anthropic tool surface differs from the golden file. If the "
        "change is intended, regenerate it and review the diff:\n"
        "  python -m core.llm.toolspec --dialect anthropic "
        "--out tools/golden/tool_declarations_anthropic.json"
    )


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------


def test_strict_readiness_covers_every_tool(specs):
    report = strict_readiness(specs)
    assert len(report) == EXPECTED_TOOL_COUNT
    assert {i.tool for i in report} == {s.name for s in specs}
    # Era's tools are optional-parameter-heavy, so most are legitimately
    # ineligible. Asserting BOTH sides are non-empty keeps this test honest: it
    # would otherwise pass if the checker started returning "eligible" for
    # everything, or for nothing.
    eligible = [i for i in report if i.eligible]
    blocked = [i for i in report if not i.eligible]
    assert eligible, "no tool is strict-eligible — the checker is over-rejecting"
    assert blocked, "every tool is strict-eligible — the checker is not checking"
    assert all(i.blockers for i in blocked)


def test_strict_refuses_ineligible_tools(specs):
    """Silently closing a schema with optional parameters would promote every
    optional to mandatory. Refusing is the safe direction."""
    by_name = {s.name for s in specs}
    assert "browser_control" in by_name
    victim = next(s for s in specs if s.name == "browser_control")
    with pytest.raises(ToolSpecError, match="not eligible for strict mode"):
        to_anthropic(victim, strict=True)


def test_strict_emits_for_eligible_tools(specs):
    eligible = [s for s in specs if strict_readiness([s])[0].eligible]
    assert eligible, "expected at least one strict-eligible tool"
    tool = to_anthropic(eligible[0], strict=True)
    assert tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False


def test_strict_flag_is_top_level_not_in_schema(specs):
    """A recurring API mistake: ``strict`` belongs on the tool object, not
    inside input_schema and not on tool_choice."""
    eligible = next(s for s in specs if strict_readiness([s])[0].eligible)
    tool = to_anthropic(eligible, strict=True)
    assert "strict" in tool
    assert "strict" not in tool["input_schema"]


# ---------------------------------------------------------------------------
# Validation and error handling
# ---------------------------------------------------------------------------


def test_era_surface_validates(specs):
    validate(specs)  # must not raise


def test_unknown_type_is_rejected_loudly():
    """A typo'd type must fail at import with the tool named, not reach a
    provider as an unknown schema."""
    with pytest.raises(ToolSpecError, match="unknown type"):
        from_legacy_declarations(
            [
                {
                    "name": "broken",
                    "description": "d",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {"x": {"type": "STRNIG"}},
                        "required": [],
                    },
                }
            ]
        )


def test_nested_unknown_type_is_rejected():
    """Depth matters — a top-level-only check would miss this."""
    with pytest.raises(ToolSpecError, match="unknown type"):
        from_legacy_declarations(
            [
                {
                    "name": "broken",
                    "description": "d",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {"x": {"type": "ARRAY", "items": {"type": "TEXT"}}},
                        "required": [],
                    },
                }
            ]
        )


def test_dangling_required_is_rejected():
    with pytest.raises(ToolSpecError, match="undeclared property"):
        from_legacy_declarations(
            [
                {
                    "name": "broken",
                    "description": "d",
                    "parameters": {"type": "OBJECT", "properties": {}, "required": ["ghost"]},
                }
            ]
        )


def test_duplicate_names_are_rejected():
    decl = {
        "name": "dupe",
        "description": "d",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    }
    with pytest.raises(ToolSpecError, match="duplicate tool name"):
        from_legacy_declarations([decl, dict(decl)])


def test_missing_description_is_rejected():
    with pytest.raises(ToolSpecError, match="description"):
        from_legacy_declarations(
            [
                {
                    "name": "broken",
                    "description": "  ",
                    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
                }
            ]
        )


def test_conversion_is_idempotent():
    """A tool authored in lowercase from the start must pass through unharmed,
    so the converter stays safe to apply twice."""
    lowercase = {
        "name": "modern",
        "description": "already JSON Schema",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }
    spec = from_legacy_declarations([lowercase])[0]
    assert spec.parameters["properties"]["q"]["type"] == "string"
    assert to_gemini(spec)["parameters"]["properties"]["q"]["type"] == "STRING"


def test_tool_with_no_parameters_is_usable():
    """enter_standby takes no inputs; both providers still need an object
    schema rather than a null."""
    spec = from_legacy_declarations(
        [{"name": "ping", "description": "no inputs", "parameters": None}]
    )[0]
    assert spec.parameters == {"type": "object", "properties": {}}
    assert to_gemini(spec)["parameters"]["type"] == "OBJECT"
    assert to_anthropic(spec)["input_schema"]["type"] == "object"


def test_spec_property_helpers(specs):
    """optional_names drives the strict audit, so it is worth pinning."""
    web = next(s for s in specs if s.name == "web_search")
    assert web.required_names == ("query",)
    assert set(web.optional_names) == {"mode", "items", "aspect"}
    assert set(web.property_names) == {"query", "mode", "items", "aspect"}
