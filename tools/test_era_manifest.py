"""The manifest is only worth reading if it cannot be wrong.

A hand-maintained map decays silently: it keeps looking authoritative while the
code moves underneath it, and the first time anyone notices is when they act on
a stale entry. These tests are what make docs/MANIFEST.md safe to trust — and
safe for Era to ground an answer about herself on.

Four properties, in order of what each one protects:

  FRESH          the committed artifacts match a fresh run. This is the whole
                 anti-drift mechanism; everything else assumes it.
  HONEST         every tool the live model is offered resolves to a file that
                 exists on disk. A tool listed with no implementation is a
                 capability Era would claim and then fail to perform.
  PARSEABLE      no source module has a syntax error. A module that will not
                 parse cannot be imported, and this repo has shipped that
                 exact breakage before (a truncated actions/netflix_control.py
                 stopped Era importing at all for about a day).
  DETERMINISTIC  two runs produce identical bytes, so a diff in the artifacts
                 always means the code changed — never that the generator is
                 noisy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import era_manifest

REGENERATE = "python tools/era_manifest.py"


@pytest.fixture(scope="module")
def data() -> dict:
    return era_manifest.build()


def test_committed_artifacts_are_fresh(data: dict) -> None:
    """docs/MANIFEST.md and runtime/manifest.json match the current source tree."""
    expected = {
        era_manifest.JSON_PATH: era_manifest._serialize(data),
        era_manifest.MD_PATH: era_manifest.render_markdown(data),
    }
    stale = [
        path.name
        for path, want in expected.items()
        if not path.exists() or path.read_text(encoding="utf-8") != want
    ]
    assert not stale, (
        f"{', '.join(stale)} no longer matches the code. This is not a broken test — "
        f"the map went stale because the source moved. Run `{REGENERATE}`."
    )


def test_every_declared_tool_has_an_implementation(data: dict) -> None:
    """Era cannot be offered a tool the index cannot point at.

    `handler` is either a path ('actions/open_app.py') or a dispatch site
    ('app/era_live/tool_dispatch.py:1943'); both must name a real file.
    """
    caps = data["capabilities"]
    assert caps["tools"], "no tools were loaded at all — app.tool_declarations failed to import"

    missing = []
    for tool in caps["tools"]:
        handler = tool["handler"]
        if not handler:
            missing.append(f"{tool['name']}: no implementation resolved")
            continue
        rel = handler.split(":", 1)[0]
        if not (era_manifest.REPO / rel).exists():
            missing.append(f"{tool['name']}: {rel} does not exist")

    assert not missing, "tools declared to the model with no code behind them:\n  " + "\n  ".join(
        missing
    )


def test_capability_load_reported_no_errors(data: dict) -> None:
    """A partial capability list is worse than none — it reads as complete."""
    errors = data["capabilities"]["errors"]
    assert not errors, "the capability manifest is incomplete: " + "; ".join(errors)


def test_no_source_file_fails_to_parse(data: dict) -> None:
    broken = data["unparseable"]
    assert not broken, "modules that cannot be parsed (so cannot be imported):\n  " + "\n  ".join(
        f"{m['path']} — {m['error']}" for m in broken
    )


def test_build_is_deterministic() -> None:
    """Same tree, same bytes — so any diff means the code moved."""
    first = era_manifest._serialize(era_manifest.build())
    second = era_manifest._serialize(era_manifest.build())
    assert first == second


def test_json_artifact_is_valid_and_matches_schema() -> None:
    raw = Path(era_manifest.JSON_PATH).read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["schema"] == 1
    for key in ("counts", "capabilities", "modules", "reverse_deps"):
        assert key in payload, f"manifest.json is missing '{key}'"


def test_excluded_trees_are_not_indexed(data: dict) -> None:
    """Backup copies must never appear — they would double every symbol.

    The repo keeps dated `.phase*_backups/` trees holding whole copies of live
    modules. Indexing one would put two files behind the same symbol, and the
    map would start pointing fixes at a frozen copy.
    """
    leaked = [
        m["path"]
        for m in data["modules"]
        if m["path"].startswith(".phase") or "/node_modules/" in m["path"]
    ]
    assert not leaked, f"backup or vendored trees leaked into the index: {leaked[:5]}"
