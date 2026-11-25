"""DataOG 平台通用 Excel / CSV 导入导出安全桥接层."""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Sequence

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.excel_formula import (
    EXCEL_ERROR_TOKENS,
    ResolvedWorkbook,
    clean_excel_cell_str,
    is_excel_error_value,
)
from core.excel_handler import (
    EXCEL_ILLEGAL_CHARS_RE,
    export_report_excel,
    generate_template_excel,
    parse_excel_checklist,
    sanitize_excel_cell,
    sanitize_excel_row,
    sanitize_excel_sheet_title,
    validate_xlsx_bytes,
)


def export_tabular_csv_bytes(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    """将通用查询结果集导出为带 UTF-8 BOM 的 CSV 字节流（Excel 双击直接防乱码）."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(columns))
    for r in rows:
        writer.writerow(["" if v is None else str(v) for v in r])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def export_tabular_xlsx_bytes(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    sheet_title: str = "查询结果",
) -> bytes:
    """将通用查询结果集导出为格式美观的 .xlsx 字节流."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sanitize_excel_sheet_title(sheet_title, fallback="查询结果")

    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    cell_font = Font(name="Microsoft YaHei", size=10)

    for c_idx, col_name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=c_idx, value=sanitize_excel_cell(str(col_name)))
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="left", vertical="center")

    for r_idx, row in enumerate(rows, start=2):
        for c_idx, val in enumerate(row, start=1):
            c_cell = ws.cell(row=r_idx, column=c_idx, value=sanitize_excel_cell(val))
            c_cell.font = cell_font

    for c_idx, col_name in enumerate(columns, start=1):
        col_letter = get_column_letter(c_idx)
        max_w = max(12, min(48, len(str(col_name)) * 2 + 4))
        ws.column_dimensions[col_letter].width = max_w

    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


__all__ = [
    "EXCEL_ERROR_TOKENS",
    "EXCEL_ILLEGAL_CHARS_RE",
    "ResolvedWorkbook",
    "clean_excel_cell_str",
    "export_report_excel",
    "export_tabular_csv_bytes",
    "export_tabular_xlsx_bytes",
    "generate_template_excel",
    "is_excel_error_value",
    "parse_excel_checklist",
    "sanitize_excel_cell",
    "sanitize_excel_row",
    "sanitize_excel_sheet_title",
    "validate_xlsx_bytes",
]
