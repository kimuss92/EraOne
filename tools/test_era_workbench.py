"""Behavioral coverage for actual artifacts, inspection and new tool boundaries."""

import json
from pathlib import Path

import pytest

from actions import office_architect, reverse_inspect
from actions.era_workbench import era_workbench
from core.safety_guard import classify_action


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(office_architect, "output_dir", lambda: tmp_path)
    return tmp_path


def test_word_contains_sections_table_and_bullets(outputs):
    from docx import Document

    result = office_architect.run(
        "docx",
        {
            "title": "Quarterly review",
            "sections": [
                {
                    "heading": "Results",
                    "text": "Revenue grew.",
                    "bullets": ["Hire one engineer"],
                    "table": [["Quarter", "Total"], ["Q1", 42]],
                }
            ],
        },
    )
    doc = Document(result["path"])
    assert "Revenue grew." in [p.text for p in doc.paragraphs]
    assert doc.tables[0].cell(1, 1).text == "42"
    assert result["bytes"] > 1000


def test_workbook_preserves_formulas_and_chart_but_not_implicit_formula(outputs):
    from openpyxl import load_workbook

    result = office_architect.run(
        "xlsx",
        {
            "sheets": [
                {
                    "name": "Budget",
                    "rows": [
                        ["Month", "Cost"],
                        ["Jan", 12],
                        ["Feb", 20],
                        ["Total", {"formula": "=SUM(B2:B3)"}],
                        ["Literal", "=1+1"],
                    ],
                    "charts": [{"type": "bar", "title": "Costs"}],
                },
                {"name": "Notes", "rows": [["Note"], ["Updated"]]},
            ]
        },
    )
    book = load_workbook(result["path"])
    assert book.sheetnames == ["Budget", "Notes"]
    assert book["Budget"]["B4"].value == "=SUM(B2:B3)"
    assert book["Budget"]["B4"].data_type == "f"
    assert book["Budget"]["B5"].data_type == "s"
    assert len(book["Budget"]._charts) == 1
    book.close()


def test_powerpoint_keeps_notes_and_widescreen(outputs):
    from pptx import Presentation

    result = office_architect.run(
        "pptx",
        {
            "slides": [
                {
                    "title": "Roadmap",
                    "bullets": ["Launch in June"],
                    "notes": "Explain the acceptance gates.",
                }
            ]
        },
    )
    deck = Presentation(result["path"])
    assert deck.slides[0].shapes.title.text == "Roadmap"
    assert "acceptance gates" in deck.slides[0].notes_slide.notes_text_frame.text
    assert deck.slide_width / deck.slide_height == pytest.approx(16 / 9, abs=0.01)


def test_pdf_merge_and_selected_text(outputs):
    first = office_architect.run(
        "pdf", {"title": "Report one", "sections": [{"text": "Alpha & beta"}]}
    )
    second = office_architect.run("pdf", {"title": "Report two", "sections": [{"text": "Gamma"}]})
    merged = office_architect.run("pdf_merge", {"paths": [first["path"], second["path"]]})
    result = office_architect.run("pdf_extract", {"path": merged["path"], "pages": [2]})
    assert result["page_count"] == 2
    assert "Gamma" in result["pages"][0]["text"]
    assert "Alpha" not in result["pages"][0]["text"]
    with pytest.raises(ValueError, match="Page numbers"):
        office_architect.run("pdf_extract", {"path": merged["path"], "pages": [0]})


@pytest.mark.parametrize(
    "filename", ["../outside.docx", "bad.exe", "C:\\bad.docx", "report/child.docx"]
)
def test_office_rejects_path_in_filename(outputs, filename):
    with pytest.raises(ValueError):
        office_architect.run("docx", {"filename": filename, "sections": [{"text": "test"}]})
    assert list(outputs.iterdir()) == []


def test_office_never_overwrites_existing_file(outputs):
    target = outputs / "report.docx"
    target.write_bytes(b"user's file")
    with pytest.raises(ValueError, match="already exists"):
        office_architect.run(
            "docx", {"filename": target.name, "sections": [{"text": "replacement"}]}
        )
    assert target.read_bytes() == b"user's file"


@pytest.mark.parametrize(
    "formula",
    [
        '=WEBSERVICE("https://example.com")',
        "='[external.xlsx]Data'!A1",
        '=cmd|"/c calc"!A0',
        '=HYPERLINK("https://example.com")',
    ],
)
def test_workbook_rejects_external_formulas(outputs, formula):
    with pytest.raises(ValueError, match="local workbook"):
        office_architect.run("xlsx", {"sheets": [{"rows": [[{"formula": formula}]]}]})
    assert not list(outputs.iterdir())


def test_invalid_spec_returns_truthful_tool_failure(outputs):
    result = era_workbench(
        {"capability": "office", "action": "xlsx", "arguments": '{"sheets": []}'}
    )
    assert result["success"] is False
    assert result["error_type"] == "ValueError"
    assert not list(outputs.iterdir())


def test_static_python_inspection_does_not_execute(tmp_path):
    marker = tmp_path / "executed"
    source = tmp_path / "sample.py"
    source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef useful(): return 1\n",
        encoding="utf-8",
    )
    result = reverse_inspect.run(str(source))
    assert result["analysis"]["symbols"][0]["name"] == "useful"
    assert result["analysis"]["imports"] == ["pathlib"]
    assert not marker.exists()


def test_source_scan_excludes_secret_and_vendor_directories(tmp_path):
    (tmp_path / "main.py").write_text("def main(): pass")
    for folder in ["config", "node_modules", ".git"]:
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "secret.py").write_text("SECRET=1")
    result = reverse_inspect.run(str(tmp_path))
    assert [f["name"] for f in result["files"]] == ["main.py"]


def test_web_inspection_reports_only_static_references(monkeypatch):
    import requests

    class Response:
        is_redirect = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b'<title>Demo</title><h1>Hello</h1><script>fetch("/api/items")</script>'

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response())
    result = reverse_inspect.run("https://example.com")
    assert result["analysis"]["api_references"] == ["/api/items"]
    assert "Static HTML" in result["analysis"]["note"]


@pytest.mark.parametrize(
    "capability,action",
    [
        ("desktop", "suspend"),
        ("desktop", "resume"),
        ("desktop", "clipboard_write"),
        ("smart_home", "turn_on"),
        ("smart_home", "toggle"),
        ("unknown", "anything"),
    ],
)
def test_state_changes_use_existing_confirmation_gate(capability, action):
    result = classify_action("era_workbench", {"capability": capability, "action": action})
    assert result["requires_confirmation"] is True


@pytest.mark.parametrize(
    "capability,action",
    [
        ("office", "docx"),
        ("desktop", "processes"),
        ("skills", "forge"),
        ("reverse_engineering", "inspect"),
        ("geography", "distance"),
    ],
)
def test_bounded_actions_remain_available(capability, action):
    assert (
        classify_action("era_workbench", {"capability": capability, "action": action})[
            "requires_confirmation"
        ]
        is False
    )


def test_skill_forge_uses_staging_without_activating(monkeypatch):
    from actions import skill_crucible
    from core import development

    seen = []
    monkeypatch.setattr(
        development, "propose", lambda *a, **k: seen.append((a, k)) or {"id": "candidate"}
    )
    result = skill_crucible.run(
        "forge", {"name": "invoice_totals", "goal": "Calculate invoice totals from supplied data"}
    )
    assert result["job"]["id"] == "candidate"
    assert seen[0][0][1] == ["features/invoice_totals.py", "tools/test_feature_invoice_totals.py"]
    assert "Mock remote services" in seen[0][0][0]
    assert "activation are not yet proven" in result["message"]


@pytest.mark.parametrize("name", ["../escape", "core/safety", "", "a", "Some Name"])
def test_skill_name_cannot_escape_feature_folder(name):
    from actions.skill_crucible import run

    with pytest.raises(ValueError):
        run("forge", {"name": name, "goal": "Some reusable function"})


def test_geographic_distance_is_explicitly_not_driving_route():
    from actions.world_tools import geography

    result = geography(
        "distance", {"latitude": 0, "longitude": 0, "to_latitude": 0, "to_longitude": 1}
    )
    assert result["distance_km"] == pytest.approx(111.2, abs=0.1)
    assert "not a driving route" in result["note"]


def test_circuit_voltage_is_board_specific():
    from actions.world_tools import circuit

    result = circuit({"board": "esp32", "sensor": "dht11"})
    assert result["connections"][0]["to"] == "3V3"
    assert any("not 5V tolerant" in note for note in result["notes"])


def test_smart_home_no_credentials_means_no_network(monkeypatch):
    from actions.world_tools import smart_home
    from core import empower_config

    monkeypatch.setattr(empower_config, "read", lambda: {})
    with pytest.raises(ValueError, match="Configure"):
        smart_home("states", {})


def test_process_identity_rejects_own_process():
    import os

    import psutil

    from actions.desktop_orchestrator import run

    with pytest.raises(ValueError, match="Protected"):
        run("suspend", {"pid": os.getpid(), "create_time": psutil.Process().create_time()})


def test_workbench_invalid_json_never_claims_success():
    for arguments in ["not json", "[]", 42]:
        assert not era_workbench(
            {"capability": "desktop", "action": "processes", "arguments": arguments}
        )["success"]


def test_workbench_registered_in_live_schema():
    from app.tool_declarations import TOOL_DECLARATIONS

    tool = next(t for t in TOOL_DECLARATIONS if t["name"] == "era_workbench")
    assert "office" in tool["parameters"]["properties"]["capability"]["enum"]
    assert set(tool["parameters"]["required"]) == {"capability", "action"}
    result = era_workbench(
        {
            "capability": "geography",
            "action": "map",
            "arguments": json.dumps({"latitude": 6.5, "longitude": 3.4}),
        }
    )
    assert result["success"] and "openstreetmap.org" in result["url"]


def test_cancelled_artifact_does_not_publish(outputs):
    import time

    from core.cancel_registry import CancelledTask, CancelToken, bind

    token = CancelToken("workbench-test", "era_workbench", 1, time.monotonic())
    token.event.set()
    with bind(token), pytest.raises(CancelledTask):
        office_architect.run("docx", {"sections": [{"text": "Must not be published"}]})
    assert not list(outputs.iterdir())


def test_workbench_is_backgroundable_and_cancellable():
    from app.era_live.tool_dispatch import _BG_GRACE_S

    assert _BG_GRACE_S["era_workbench"] <= 2


def test_rea_rejects_missing_target_and_arbitrary_mode(tmp_path):
    from actions.rea_analysis import run

    with pytest.raises(ValueError, match="Unsupported"):
        run("execute-rizin-command", {"target": str(tmp_path)})
    with pytest.raises(ValueError, match="does not exist"):
        run("javascript", {"target": str(tmp_path / "absent")})
