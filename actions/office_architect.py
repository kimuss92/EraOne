"""Create real Office/PDF artifacts from bounded structured specifications."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

from core.cancel_registry import raise_if_cancelled
from core.empower_config import output_dir


def _list(value: object, name: str, maximum: int) -> list:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise ValueError(f"{name} must contain 1–{maximum} items")
    return value


def _text(value: object, maximum: int = 20000) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError("Expected bounded text")
    return value


def _document(path: Path, spec: dict) -> None:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading(_text(spec.get("title", "Document"), 200), 0)
    for section in _list(spec.get("sections"), "sections", 100):
        if section.get("heading"):
            doc.add_heading(_text(section["heading"], 300), 1)
        if section.get("text"):
            for paragraph in _text(section["text"]).split("\n"):
                doc.add_paragraph(paragraph)
        for bullet in section.get("bullets", [])[:100]:
            doc.add_paragraph(_text(bullet), style="List Bullet")
        if section.get("table"):
            rows = _list(section["table"], "table rows", 1000)
            columns = len(_list(rows[0], "table columns", 30))
            table = doc.add_table(rows=0, cols=columns)
            table.style = "Light Shading Accent 1"
            for row in rows:
                if not isinstance(row, list) or len(row) != columns:
                    raise ValueError("Table rows must have equal lengths")
                for cell, value in zip(table.add_row().cells, row, strict=True):
                    cell.text = str(value)[:2000]
    doc.save(path)
    Document(path)  # read-back, not just a successful save call


def _spreadsheet(path: Path, spec: dict) -> None:
    from openpyxl import Workbook, load_workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Font, PatternFill

    book = Workbook()
    book.remove(book.active)
    names = set()
    for index, sheet in enumerate(_list(spec.get("sheets"), "sheets", 30)):
        name = _text(sheet.get("name", f"Sheet {index + 1}"), 31)
        if not name or re.search(r"[\\/*?:\[\]]", name) or name.lower() in names:
            raise ValueError("Sheet names must be valid and unique")
        names.add(name.lower())
        ws = book.create_sheet(name)
        rows = _list(sheet.get("rows"), "rows", 10000)
        for row in rows:
            values = []
            for cell in _list(row, "columns", 100):
                if isinstance(cell, dict):
                    formula = _text(cell.get("formula"), 1000)
                    if not formula.startswith("=") or re.search(
                        r"[\[\]|]|https?://|WEBSERVICE|HYPERLINK|RTD\s*\(", formula, re.I
                    ):
                        raise ValueError("Only local workbook formulas are allowed")
                    values.append(formula)
                elif isinstance(cell, (str, int, float, bool)) or cell is None:
                    values.append(cell)
                else:
                    raise ValueError("Invalid cell value")
            ws.append(values)
            for col, cell in enumerate(row, 1):
                if isinstance(cell, str) and cell.startswith("="):
                    ws.cell(ws.max_row, col).data_type = "s"  # formula requires explicit object
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="164E63")
        for column in ws.columns:
            ws.column_dimensions[column[0].column_letter].width = min(
                55, max(14, max(len(str(c.value or "")) for c in column) + 2)
            )
        for chart_spec in sheet.get("charts", [])[:10]:
            chart_type = chart_spec.get("type", "bar")
            if chart_type not in {"bar", "line", "pie"}:
                raise ValueError("Chart type must be bar, line or pie")
            chart = {"bar": BarChart, "line": LineChart, "pie": PieChart}[chart_type]()
            chart.title = _text(chart_spec.get("title", "Chart"), 200)
            column = chart_spec.get("value_column", 2)
            if type(column) is not int or not 2 <= column <= ws.max_column or ws.max_row < 2:
                raise ValueError("Chart needs a value column and at least one data row")
            chart.add_data(
                Reference(ws, min_col=column, max_col=column, min_row=1, max_row=ws.max_row),
                titles_from_data=True,
            )
            chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
            ws.add_chart(chart, f"{chr(65 + min(ws.max_column + 1, 25))}2")
    book.save(path)
    check = load_workbook(path)
    check.close()


def _presentation(path: Path, spec: dict) -> None:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    accent = spec.get("accent", "164E63")
    if not isinstance(accent, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", accent):
        raise ValueError("Accent must be a six-digit RGB color")
    for slide_spec in _list(spec.get("slides"), "slides", 100):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = _text(slide_spec.get("title", ""), 160)
        for run in slide.shapes.title.text_frame.paragraphs[0].runs:
            run.font.color.rgb = RGBColor.from_string(accent)
        body = slide.placeholders[1].text_frame
        bullets = slide_spec.get("bullets", [])
        if not isinstance(bullets, list) or len(bullets) > 8:
            raise ValueError("Use up to eight short bullets per slide")
        for index, bullet in enumerate(bullets):
            paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
            paragraph.text = _text(bullet, 220)
            paragraph.font.size = Pt(22)
        slide.notes_slide.notes_text_frame.text = _text(slide_spec.get("notes", ""))
    deck.save(path)
    Presentation(path)


def _pdf(path: Path, spec: dict) -> None:
    from pypdf import PdfReader
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    story = [Paragraph(escape(_text(spec.get("title", "Document"), 200)), styles["Title"])]
    for section in _list(spec.get("sections"), "sections", 100):
        if section.get("heading"):
            story.append(Paragraph(escape(_text(section["heading"], 300)), styles["Heading2"]))
        story.append(
            Paragraph(
                escape(_text(section.get("text", ""))).replace("\n", "<br/>"), styles["BodyText"]
            )
        )
        story.append(Spacer(1, 12))
    SimpleDocTemplate(str(path)).build(story)
    PdfReader(path)


def _source(value: object) -> Path:
    path = Path(_text(value, 2000)).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf" or path.stat().st_size > 50_000_000:
        raise ValueError("Input must be an existing PDF under 50 MB")
    return path


def run(action: str, spec: dict) -> dict:
    """Return verified output paths; existing user files are never overwritten."""
    if not isinstance(spec, dict) or len(json.dumps(spec)) > 2_000_000:
        raise ValueError("Specification must be an object under 2 MB")
    if action == "pdf_extract":
        from pypdf import PdfReader

        reader = PdfReader(_source(spec.get("path")))
        pages = spec.get("pages", list(range(1, min(len(reader.pages), 50) + 1)))
        _list(pages, "pages", 100)
        if any(type(n) is not int or not 1 <= n <= len(reader.pages) for n in pages):
            raise ValueError("Page numbers are one-based and must exist")
        return {
            "success": True,
            "page_count": len(reader.pages),
            "pages": [
                {"page": n, "text": (reader.pages[n - 1].extract_text() or "")[:20000]}
                for n in pages
            ],
        }
    writers = {
        "docx": (_document, ".docx"),
        "xlsx": (_spreadsheet, ".xlsx"),
        "pptx": (_presentation, ".pptx"),
        "pdf": (_pdf, ".pdf"),
    }
    if action not in {*writers, "pdf_merge"}:
        raise ValueError("Use docx, xlsx, pptx, pdf, pdf_merge or pdf_extract")
    folder = output_dir()
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if action == "pdf_merge" else writers[action][1]
    filename = spec.get("filename") or f"era-{action}-{uuid.uuid4().hex[:10]}{suffix}"
    if (
        not isinstance(filename, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,120}", filename)
        or Path(filename).suffix.lower() != suffix
    ):
        raise ValueError(f"Use a plain filename ending with {suffix}")
    target = folder / filename
    if target.exists():
        raise ValueError("Output already exists; choose a new filename")
    with tempfile.TemporaryDirectory(prefix="era-office-") as tmp:
        staged = Path(tmp) / filename
        if action == "pdf_merge":
            from pypdf import PdfReader, PdfWriter

            writer = PdfWriter()
            for value in _list(spec.get("paths"), "paths", 30):
                writer.append(_source(value))
            with staged.open("wb") as stream:
                writer.write(stream)
            writer.close()
            PdfReader(staged)
        else:
            writers[action][0](staged, spec)
        # Exclusive creation prevents concurrent requests clobbering a file.
        raise_if_cancelled()
        with target.open("xb") as stream:
            try:
                with staged.open("rb") as source:
                    shutil.copyfileobj(source, stream)
            except BaseException:
                stream.close()
                target.unlink(missing_ok=True)
                raise
    return {
        "success": True,
        "path": str(target),
        "bytes": target.stat().st_size,
        "note": "File saved and parsed successfully. Excel recalculates formulas on open; application layout has not been visually verified.",
    }
