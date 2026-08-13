"""Small dependency-free XLSX/XML writer used by spreadsheet exports."""

from __future__ import annotations

import re
from html import unescape as html_unescape
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


MEETING_CHIP_RE = re.compile(
    r"<span\b(?=[^>]*meeting-chip)[^>]*>\s*zoom\s*</span>",
    re.IGNORECASE,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
XML10_INVALID_CHAR_RE = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]"
)


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def excel_cell_text(value: Any) -> str:
    """Convert calendar HTML to the text expected in an Excel cell."""

    text = "" if value is None else str(value)
    text = MEETING_CHIP_RE.sub("(Zoom)", text)
    text = HTML_TAG_RE.sub("", text)
    return html_unescape(text)


def _cell_xml(row: int, column: int, value: Any, style: int = 0) -> str:
    reference = f"{_column_name(column)}{row}"
    text = "" if value is None else str(value)
    safe_text = XML10_INVALID_CHAR_RE.sub("\ufffd", text)
    return (
        f'<c r="{reference}" t="inlineStr" s="{style}">'
        f"<is><t>{escape(safe_text)}</t></is></c>"
    )


def _sheet_xml(
    rows: list[list[Any]],
    widths: list[float],
    *,
    style_by_cell: dict[tuple[int, int], int] | None = None,
    merge_cells: list[str] | None = None,
) -> str:
    style_by_cell = style_by_cell or {}
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    row_xml = []
    for row_index, values in enumerate(rows, start=1):
        max_lines = max(
            (str(value).count("\n") + 1 for value in values if value is not None),
            default=1,
        )
        uses_wrapped_style = any(
            style_by_cell.get((row_index, column_index)) in {6, 7, 8}
            for column_index in range(1, len(values) + 1)
        )
        height = (
            min(180, max(22, max_lines * 18))
            if uses_wrapped_style
            else 26 if row_index == 1 else 22
        )
        cells = "".join(
            _cell_xml(
                row_index,
                column_index,
                value,
                style_by_cell.get(
                    (row_index, column_index), 1 if row_index == 1 else 0
                ),
            )
            for column_index, value in enumerate(values, start=1)
        )
        row_xml.append(
            f'<row r="{row_index}" ht="{height}" customHeight="1">{cells}</row>'
        )
    merges = ""
    if merge_cells:
        merge_xml = "".join(
            f'<mergeCell ref="{reference}"/>' for reference in merge_cells
        )
        merges = f'<mergeCells count="{len(merge_cells)}">{merge_xml}</mergeCells>'
    auto_filter = (
        f'<autoFilter ref="A1:{_column_name(len(widths))}{len(rows)}"/>'
        if rows
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{columns}</cols><sheetData>{''.join(row_xml)}</sheetData>"
        f"{auto_filter}{merges}</worksheet>"
    )


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="7">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/></font>
    <font><b/><color rgb="FF175CD3"/><sz val="11"/><name val="Calibri"/></font>
    <font><b/><color rgb="FFB42318"/><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="16"/><name val="Calibri"/></font>
    <font><color rgb="FF175CD3"/><sz val="11"/><name val="Calibri"/></font>
    <font><color rgb="FFB42318"/><sz val="11"/><name val="Calibri"/></font>
  </fonts>
  <fills count="6">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE2F0D9"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFEAF6FF"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFEEEE"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border/>
    <border><left style="thin"><color rgb="FFB7B7B7"/></left><right style="thin"><color rgb="FFB7B7B7"/></right><top style="thin"><color rgb="FFB7B7B7"/></top><bottom style="thin"><color rgb="FFB7B7B7"/></bottom></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="9">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" shrinkToFit="1"/></xf>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="0" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" shrinkToFit="1"/></xf>
    <xf numFmtId="0" fontId="3" fillId="0" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" shrinkToFit="1"/></xf>
    <xf numFmtId="0" fontId="1" fillId="3" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="4" fillId="0" borderId="0" applyFont="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="5" fillId="4" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="6" fillId="5" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def workbook_bytes(
    sheets: list[
        tuple[
            str,
            list[list[Any]],
            list[float],
            dict[tuple[int, int], int],
            list[str],
        ]
    ]
) -> bytes:
    """Return a minimal XLSX package for already prepared worksheet data."""

    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, *_rest) in enumerate(sheets, start=1)
    )
    workbook_rels = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    workbook_rels += (
        f'<Relationship Id="rId{len(sheets) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    content_types = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{content_types}</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{workbook_sheets}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{workbook_rels}</Relationships>",
        )
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, (_name, rows, widths, styles, merges) in enumerate(
            sheets, start=1
        ):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _sheet_xml(
                    rows, widths, style_by_cell=styles, merge_cells=merges
                ),
            )
    return buffer.getvalue()
