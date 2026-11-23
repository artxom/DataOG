"""Data.O.G 通用离线交互 HTML 报告引擎与单 Sheet Excel 样式工具 (core/platform/offline_report/runtime.py).

可被任意 2.X 与 3.0 业务模块复用：调用 `wrap_offline_html(title, payload, app_js)` 即可零依赖生成
自带浅深色自适应 CSS、安全 DOM 工厂与纯 JS 单 Sheet `.xlsx` 导出能力的单文件离线交互报告。
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any, Dict, Sequence

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_ILLEGAL_SHEET_CHARS_RE = re.compile(r"[:\\/?*\[\]]")


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_excel_cell(val: Any) -> Any:
    if isinstance(val, str) and val != "-" and val[:1] in ("=", "+", "-", "@"):
        return "'" + val
    return val


def _safe_sheet_title(raw: str, fallback: str = "Report") -> str:
    cleaned = _ILLEGAL_SHEET_CHARS_RE.sub("_", str(raw or "").strip())
    return (cleaned or fallback)[:31]


def _safe_json_for_script_tag(payload: Any) -> str:
    """安全序列化 JSON 用于嵌入 HTML <script type="application/json">，彻底阻断 </script> 与 HTML 注入."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _escape_html_text(val: Any) -> str:
    s = str(val or "")
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _apply_single_sheet_style(
    ws: Any,
    headers: Sequence[str],
    widths: Sequence[int],
    row_status_tags: Sequence[str],
) -> None:
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="Microsoft YaHei", size=10)
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )
    fills = {
        "GREEN": PatternFill(start_color="E6F4EA", end_color="E6F4EA", fill_type="solid"),
        "BLUE": PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid"),
        "RED": PatternFill(start_color="FCE8E6", end_color="FCE8E6", fill_type="solid"),
        "YELLOW": PatternFill(start_color="FEF7E0", end_color="FEF7E0", fill_type="solid"),
        "LAVENDER": PatternFill(start_color="EEF2FF", end_color="EEF2FF", fill_type="solid"),
        "GRAY": PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid"),
    }

    col_count = len(headers)
    for c_i in range(1, col_count + 1):
        cell = ws.cell(row=1, column=c_i)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border
        w = widths[c_i - 1] if c_i - 1 < len(widths) else 18
        ws.column_dimensions[get_column_letter(c_i)].width = w

    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"
    if ws.max_row >= 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(col_count)}{ws.max_row}"

    for r_offset, tag in enumerate(row_status_tags, start=2):
        fill = fills.get(tag)
        for c_i in range(1, col_count + 1):
            c = ws.cell(row=r_offset, column=c_i)
            c.font = body_font
            c.border = thin_border
            c.alignment = Alignment(vertical="center")
            if fill is not None:
                c.fill = fill


# =============================================================================
# 2. 离线交互网页公共样式与纯前端无依赖单 Sheet .xlsx 生成器脚本
# =============================================================================

_OFFLINE_CSS = r"""
:root {
  color-scheme: light dark;
  --bg: #f5f5f7;
  --surface: #ffffff;
  --subtle: #fafafa;
  --toolbar: rgba(255, 255, 255, 0.88);
  --border: rgba(0, 0, 0, 0.09);
  --border-strong: rgba(0, 0, 0, 0.16);
  --text: #1d1d1f;
  --text-2: #52525b;
  --muted: #71717a;
  --accent: #0071e3;
  --select: rgba(0, 113, 227, 0.12);
  --hover: rgba(0, 0, 0, 0.04);
  --ok: #16a34a;
  --ok-bg: #eafaf0;
  --ok-bd: #b8dfc3;
  --info: #0284c7;
  --info-bg: #e8f4fe;
  --warn: #d97706;
  --warn-bg: #fef7e0;
  --bad: #dc2626;
  --bad-bg: #fdecec;
  --lav-bg: #eef2ff;
  --lav: #4338ca;
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
  --mono: "SF Mono", SFMono-Regular, ui-monospace, Menlo, Consolas, monospace;
}
html[data-theme="dark"] {
  color-scheme: dark;
  --bg: #111216;
  --surface: #18191e;
  --subtle: #141519;
  --toolbar: rgba(28, 29, 35, 0.88);
  --border: rgba(255, 255, 255, 0.1);
  --border-strong: rgba(255, 255, 255, 0.18);
  --text: #f4f4f5;
  --text-2: #b4b4bd;
  --muted: #8e8e98;
  --accent: #0a84ff;
  --select: rgba(10, 132, 255, 0.22);
  --hover: rgba(255, 255, 255, 0.05);
  --ok: #34d399;
  --ok-bg: rgba(16, 185, 129, 0.15);
  --ok-bd: rgba(52, 211, 153, 0.35);
  --info: #38bdf8;
  --info-bg: rgba(56, 189, 248, 0.15);
  --warn: #fbbf24;
  --warn-bg: rgba(251, 191, 36, 0.15);
  --bad: #f87171;
  --bad-bg: rgba(248, 113, 113, 0.15);
  --lav-bg: rgba(99, 102, 241, 0.18);
  --lav: #a5b4fc;
}
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body {
  margin: 0; padding: 0; height: 100%; overflow: hidden;
  font-family: var(--font); font-size: 12.5px; color: var(--text); background: var(--bg);
}
.mono { font-family: var(--mono); }
.app { display: flex; flex-direction: column; height: 100vh; }
.topbar {
  min-height: 48px; padding: 8px 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  background: var(--toolbar); backdrop-filter: blur(16px); border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.topbar-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.brand-mark {
  width: 28px; height: 28px; border-radius: 7px; display: inline-flex; align-items: center; justify-content: center;
  background: linear-gradient(145deg, #0071e3 0%, #00c6ff 100%); color: #fff; font-weight: 800; font-size: 11px; flex-shrink: 0;
}
.title-main { font-size: 14px; font-weight: 700; margin: 0; }
.topbar-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.btn {
  height: 28px; padding: 0 11px; border-radius: 7px; border: 1px solid var(--border-strong);
  background: var(--surface); color: var(--text); font-family: inherit; font-size: 12px; font-weight: 500;
  display: inline-flex; align-items: center; gap: 5px; cursor: pointer; white-space: nowrap;
}
.btn:hover { background: var(--hover); }
.btn-primary { background: var(--accent); border-color: transparent; color: #fff; }
.btn-primary:hover { filter: brightness(1.08); background: var(--accent); }
.btn-xs { height: 23px; padding: 0 8px; font-size: 11px; border-radius: 5px; }
.input {
  height: 28px; padding: 0 9px; border-radius: 7px; border: 1px solid var(--border-strong);
  background: var(--surface); color: var(--text); font-family: inherit; font-size: 12px; outline: none;
}
.input:focus { border-color: var(--accent); }
.badge {
  display: inline-flex; align-items: center; height: 20px; padding: 0 7px; border-radius: 6px;
  font-size: 11px; font-weight: 600; background: var(--hover); color: var(--text-2); white-space: nowrap;
}
.badge-ok { background: var(--ok-bg); color: var(--ok); }
.badge-info { background: var(--info-bg); color: var(--info); }
.badge-warn { background: var(--warn-bg); color: var(--warn); }
.badge-bad { background: var(--bad-bg); color: var(--bad); }
.badge-lav { background: var(--lav-bg); color: var(--lav); }
.kpi-strip {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px;
  padding: 10px 16px; background: var(--subtle); border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.kpi-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 9px 12px;
  display: flex; flex-direction: column; gap: 4px; min-width: 0;
}
.kpi-label { font-size: 11px; color: var(--muted); font-weight: 500; }
.kpi-val { font-size: 19px; font-weight: 700; line-height: 1.15; font-variant-numeric: tabular-nums; }
.kpi-sub { font-size: 11px; color: var(--text-2); }
.progress-mini { height: 5px; border-radius: 999px; background: var(--border); overflow: hidden; margin-top: 2px; }
.progress-mini > span { display: block; height: 100%; background: var(--accent); border-radius: 999px; }
.tabbar {
  display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap;
  padding: 0 16px; min-height: 36px; background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.tabs { display: flex; align-items: center; gap: 4px; overflow-x: auto; }
.tab {
  position: relative; height: 36px; padding: 0 12px; border: none; background: transparent; color: var(--muted);
  font-family: inherit; font-size: 12.5px; font-weight: 500; cursor: pointer; white-space: nowrap;
}
.tab.is-active { color: var(--accent); font-weight: 600; }
.tab.is-active::after {
  content: ""; position: absolute; left: 8px; right: 8px; bottom: 0; height: 2px; background: var(--accent);
}
.split { flex: 1; min-height: 0; display: flex; overflow: hidden; }
.sidebar {
  width: 268px; flex-shrink: 0; border-right: 1px solid var(--border); background: var(--subtle);
  display: flex; flex-direction: column; min-height: 0;
}
.sidebar-head { padding: 10px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 7px; }
.sidebar-list { flex: 1; overflow-y: auto; padding: 6px; display: flex; flex-direction: column; gap: 3px; }
.nav-item {
  width: 100%; border: none; border-radius: 7px; background: transparent; color: var(--text);
  padding: 6px 8px; display: flex; align-items: center; justify-content: space-between; gap: 6px;
  cursor: pointer; text-align: left; font-family: inherit; font-size: 12px;
}
.nav-item:hover { background: var(--hover); }
.nav-item.is-active { background: var(--select); color: var(--accent); font-weight: 600; }
.nav-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.schema-hdr { font-size: 11px; font-weight: 700; color: var(--muted); padding: 8px 8px 3px; text-transform: uppercase; }
.main-pane { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; background: var(--surface); }
.subbar {
  padding: 8px 14px; border-bottom: 1px solid var(--border); background: var(--subtle);
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; flex-shrink: 0;
}
.subbar-group { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pills { display: inline-flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.pill {
  height: 24px; padding: 0 9px; border-radius: 999px; border: 1px solid var(--border-strong);
  background: var(--surface); color: var(--text-2); font-family: inherit; font-size: 11.5px; cursor: pointer;
}
.pill:hover { background: var(--hover); color: var(--text); }
.pill.is-active { background: var(--accent); border-color: transparent; color: #fff; font-weight: 600; }
.table-wrap { flex: 1; min-height: 0; overflow: auto; }
table.grid { width: 100%; border-collapse: collapse; font-size: 12px; }
table.grid thead th {
  position: sticky; top: 0; z-index: 2; height: 31px; padding: 0 10px; text-align: left; font-weight: 600;
  color: var(--muted); background: var(--subtle); border-bottom: 1px solid var(--border); white-space: nowrap;
}
table.grid tbody td {
  padding: 6px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; line-height: 1.4;
}
table.grid tbody tr:hover { background: var(--hover); }
table.grid tbody tr.is-clickable { cursor: pointer; }
.banner {
  margin: 10px 14px 0; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--subtle); font-size: 12px; color: var(--text-2); display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap;
}
.banner.is-warn { background: var(--warn-bg); border-color: rgba(217, 119, 6, 0.35); color: var(--text); }
.five-wrap { flex: 1; min-height: 0; overflow: auto; padding: 10px 14px; }
.five-grid {
  display: grid; min-width: 960px;
  grid-template-columns: minmax(150px, 0.9fr) minmax(160px, 1fr) minmax(220px, 1.2fr) minmax(160px, 1fr) minmax(150px, 0.9fr);
  gap: 8px;
}
.five-col-hdr {
  padding: 7px 10px; border-radius: 8px; background: var(--subtle); border: 1px solid var(--border);
  font-size: 11.5px; font-weight: 700; color: var(--text-2); display: flex; justify-content: space-between; align-items: center;
}
.five-body { display: grid; min-width: 960px; grid-template-columns: minmax(150px, 0.9fr) minmax(550px, 3.2fr) minmax(150px, 0.9fr); gap: 8px; margin-top: 8px; align-items: start; }
.cand-list { display: flex; flex-direction: column; gap: 6px; background: var(--subtle); border: 1px solid var(--border); border-radius: 9px; padding: 8px; min-height: 120px; }
.cand-card { border: 1px solid var(--border-strong); border-radius: 7px; background: var(--surface); padding: 6px 8px; display: flex; flex-direction: column; gap: 2px; }
.matrix-list { display: flex; flex-direction: column; gap: 7px; }
.matrix-row { display: grid; grid-template-columns: minmax(160px, 1fr) minmax(220px, 1.2fr) minmax(160px, 1fr); gap: 8px; align-items: stretch; }
.std-card {
  border: 1px solid var(--border-strong); border-radius: 8px; padding: 6px 9px; background: var(--subtle);
  display: flex; flex-direction: column; justify-content: center; gap: 3px;
}
.std-card.is-unified { background: var(--ok-bg); border-color: var(--ok-bd); }
.std-card.is-diff { background: var(--bad-bg); border-color: rgba(220, 38, 38, 0.35); }
.std-card.is-partial { background: var(--info-bg); border-color: rgba(2, 132, 199, 0.35); }
.slot-empty {
  border: 1px dashed var(--border-strong); border-radius: 8px; display: flex; align-items: center; justify-content: center;
  color: var(--muted); font-size: 11px; min-height: 44px; background: var(--subtle);
}
.empty { padding: 36px 16px; text-align: center; color: var(--muted); }
.pager { padding: 8px 14px; border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; background: var(--subtle); flex-shrink: 0; }
.sql-pre {
  margin: 4px 0 0; padding: 8px; border-radius: 6px; background: var(--subtle); border: 1px solid var(--border);
  font-family: var(--mono); font-size: 11px; white-space: pre-wrap; word-break: break-all; max-height: 180px; overflow: auto;
}
.dual-lists { flex: 1; min-height: 0; display: grid; grid-template-columns: 1fr 1fr; overflow: hidden; }
.dual-pane { display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--border); }
.dual-pane:last-child { border-right: none; }
@media (max-width: 900px) {
  .split { flex-direction: column; }
  .sidebar { width: 100%; max-height: 220px; border-right: none; border-bottom: 1px solid var(--border); }
  .dual-lists { grid-template-columns: 1fr; }
}
"""

_PURE_JS_XLSX_RUNTIME = r"""
function buildSingleSheetXlsxBlob(sheetTitle, headers, dataRows) {
  const te = new TextEncoder();
  const CRC_TABLE = (() => {
    const tbl = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      tbl[i] = c >>> 0;
    }
    return tbl;
  })();
  function crc32(bytes) {
    let crc = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) {
      crc = CRC_TABLE[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
    }
    return (crc ^ 0xFFFFFFFF) >>> 0;
  }
  function escXml(v) {
    return String(v == null ? "" : v)
      .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function safeCellStr(v) {
    const s = String(v == null ? "" : v);
    return s !== "-" && /^[=+\-@]/.test(s) ? "'" + s : s;
  }
  function colLetter(n) {
    let s = "";
    let x = n;
    while (x > 0) {
      const m = (x - 1) % 26;
      s = String.fromCharCode(65 + m) + s;
      x = Math.floor((x - 1) / 26);
    }
    return s;
  }
  const safeSheetName = String(sheetTitle || "Report")
    .replace(/[:\\/?*\[\]]/g, "_")
    .slice(0, 31) || "Report";

  const styleMap = { GREEN: 2, BLUE: 3, RED: 4, YELLOW: 5, LAVENDER: 6, GRAY: 7 };
  const colCount = headers.length;
  const maxWidths = headers.map((h) => Math.max(12, Math.min(40, String(h).length * 2 + 4)));
  for (const rowObj of dataRows) {
    const vals = rowObj.values || [];
    for (let i = 0; i < colCount; i++) {
      const len = String(vals[i] == null ? "" : vals[i]).length;
      const w = Math.min(42, Math.max(maxWidths[i], len + 3));
      maxWidths[i] = w;
    }
  }

  const colsXml = maxWidths
    .map((w, i) => '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + w + '" customWidth="1"/>')
    .join("");

  const sheetParts = [];
  sheetParts.push('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>');
  sheetParts.push('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">');
  sheetParts.push('<sheetViews><sheetView tabSelected="1" workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>');
  sheetParts.push('<cols>' + colsXml + '</cols>');
  sheetParts.push('<sheetData>');

  // Header row (row 1)
  let hRowXml = '<row r="1" ht="24" customHeight="1">';
  for (let c = 0; c < colCount; c++) {
    const ref = colLetter(c + 1) + "1";
    hRowXml += '<c r="' + ref + '" s="1" t="inlineStr"><is><t xml:space="preserve">' + escXml(headers[c]) + '</t></is></c>';
  }
  hRowXml += '</row>';
  sheetParts.push(hRowXml);

  for (let r = 0; r < dataRows.length; r++) {
    const rowNum = r + 2;
    const rowObj = dataRows[r];
    const sIdx = styleMap[rowObj.tone] || 0;
    const vals = rowObj.values || [];
    let rXml = '<row r="' + rowNum + '">';
    for (let c = 0; c < colCount; c++) {
      const ref = colLetter(c + 1) + rowNum;
      const rawVal = vals[c];
      if (typeof rawVal === "number" && Number.isFinite(rawVal)) {
        rXml += '<c r="' + ref + '" s="' + sIdx + '" t="n"><v>' + String(rawVal) + '</v></c>';
      } else {
        const textVal = safeCellStr(rawVal);
        rXml += '<c r="' + ref + '" s="' + sIdx + '" t="inlineStr"><is><t xml:space="preserve">' + escXml(textVal) + '</t></is></c>';
      }
    }
    rXml += '</row>';
    sheetParts.push(rXml);
  }
  sheetParts.push('</sheetData>');
  const lastRowNum = Math.max(1, dataRows.length + 1);
  sheetParts.push('<autoFilter ref="A1:' + colLetter(colCount) + lastRowNum + '"/>');
  sheetParts.push('</worksheet>');

  const stylesXml =
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
    '<fonts count="2">' +
      '<font><sz val="10"/><name val="Microsoft YaHei"/></font>' +
      '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>' +
    '</fonts>' +
    '<fills count="9">' +
      '<fill><patternFill patternType="none"/></fill>' +
      '<fill><patternFill patternType="gray125"/></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FF1E3A8A"/><bgColor indexed="64"/></patternFill></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FFE6F4EA"/><bgColor indexed="64"/></patternFill></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FFE8F0FE"/><bgColor indexed="64"/></patternFill></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FFFCE8E6"/><bgColor indexed="64"/></patternFill></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FFFEF7E0"/><bgColor indexed="64"/></patternFill></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FFEEF2FF"/><bgColor indexed="64"/></patternFill></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/><bgColor indexed="64"/></patternFill></fill>' +
    '</fills>' +
    '<borders count="2">' +
      '<border><left/><right/><top/><bottom/><diagonal/></border>' +
      '<border>' +
        '<left style="thin"><color rgb="FFCBD5E1"/></left>' +
        '<right style="thin"><color rgb="FFCBD5E1"/></right>' +
        '<top style="thin"><color rgb="FFCBD5E1"/></top>' +
        '<bottom style="thin"><color rgb="FFCBD5E1"/></bottom>' +
        '<diagonal/>' +
      '</border>' +
    '</borders>' +
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
    '<cellXfs count="8">' +
      '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>' +
      '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>' +
      '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>' +
      '<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>' +
      '<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>' +
      '<xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>' +
      '<xf numFmtId="0" fontId="0" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>' +
      '<xf numFmtId="0" fontId="0" fillId="8" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>' +
    '</cellXfs>' +
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
    '</styleSheet>';

  const files = [
    {
      name: "[Content_Types].xml",
      data: te.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
          '<Default Extension="xml" ContentType="application/xml"/>' +
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
        '</Types>'
      ),
    },
    {
      name: "_rels/.rels",
      data: te.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
        '</Relationships>'
      ),
    },
    {
      name: "xl/workbook.xml",
      data: te.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
          '<sheets><sheet name="' + escXml(safeSheetName) + '" sheetId="1" r:id="rId1"/></sheets>' +
        '</workbook>'
      ),
    },
    {
      name: "xl/_rels/workbook.xml.rels",
      data: te.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
          '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' +
        '</Relationships>'
      ),
    },
    { name: "xl/styles.xml", data: te.encode(stylesXml) },
    { name: "xl/worksheets/sheet1.xml", data: te.encode(sheetParts.join("")) },
  ];

  const localChunks = [];
  const cdChunks = [];
  let offset = 0;

  for (const f of files) {
    const nameBytes = te.encode(f.name);
    const dataBytes = f.data;
    const crc = crc32(dataBytes);
    const lh = new ArrayBuffer(30 + nameBytes.length);
    const ldv = new DataView(lh);
    ldv.setUint32(0, 0x04034B50, true);
    ldv.setUint16(4, 20, true);
    ldv.setUint16(6, 0x0800, true);
    ldv.setUint16(8, 0, true);
    ldv.setUint16(10, 0, true);
    ldv.setUint16(12, 0x5421, true);
    ldv.setUint32(14, crc, true);
    ldv.setUint32(18, dataBytes.length, true);
    ldv.setUint32(22, dataBytes.length, true);
    ldv.setUint16(26, nameBytes.length, true);
    ldv.setUint16(28, 0, true);
    new Uint8Array(lh, 30).set(nameBytes);
    localChunks.push(new Uint8Array(lh), dataBytes);

    const cd = new ArrayBuffer(46 + nameBytes.length);
    const cdv = new DataView(cd);
    cdv.setUint32(0, 0x02014B50, true);
    cdv.setUint16(4, 20, true);
    cdv.setUint16(6, 20, true);
    cdv.setUint16(8, 0x0800, true);
    cdv.setUint16(10, 0, true);
    cdv.setUint16(12, 0, true);
    cdv.setUint16(14, 0x5421, true);
    cdv.setUint32(16, crc, true);
    cdv.setUint32(20, dataBytes.length, true);
    cdv.setUint32(24, dataBytes.length, true);
    cdv.setUint16(28, nameBytes.length, true);
    cdv.setUint16(30, 0, true);
    cdv.setUint16(32, 0, true);
    cdv.setUint16(34, 0, true);
    cdv.setUint16(36, 0, true);
    cdv.setUint32(38, 0, true);
    cdv.setUint32(42, offset, true);
    new Uint8Array(cd, 46).set(nameBytes);
    cdChunks.push(new Uint8Array(cd));

    offset += 30 + nameBytes.length + dataBytes.length;
  }

  const cdSize = cdChunks.reduce((acc, a) => acc + a.length, 0);
  const eocd = new ArrayBuffer(22);
  const edv = new DataView(eocd);
  edv.setUint32(0, 0x06054B50, true);
  edv.setUint16(4, 0, true);
  edv.setUint16(6, 0, true);
  edv.setUint16(8, files.length, true);
  edv.setUint16(10, files.length, true);
  edv.setUint32(12, cdSize, true);
  edv.setUint32(16, offset, true);
  edv.setUint16(20, 0, true);

  return new Blob([...localChunks, ...cdChunks, new Uint8Array(eocd)], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}
"""

_OFFLINE_DOM_HELPERS_JS = r"""
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "className") node.className = String(v);
      else if (k === "text") node.textContent = String(v);
      else if (k === "style" && typeof v === "object") {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv !== undefined && sv !== null) node.style[sk] = String(sv);
        }
      } else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v === true) {
        node.setAttribute(k, "");
      } else {
        node.setAttribute(k, String(v));
        if (k === "value" && "value" in node) node.value = String(v);
      }
    }
  }
  const add = (ch) => {
    if (ch === undefined || ch === null || ch === false) return;
    if (Array.isArray(ch)) ch.forEach(add);
    else if (ch instanceof Node) node.appendChild(ch);
    else node.appendChild(document.createTextNode(String(ch)));
  };
  children.forEach(add);
  return node;
}
function clearEl(c, ...kids) {
  c.replaceChildren();
  for (const ch of kids) {
    if (ch === undefined || ch === null || ch === false) continue;
    if (Array.isArray(ch)) ch.forEach((x) => { if (x instanceof Node) c.appendChild(x); else if (x != null && x !== false) c.appendChild(document.createTextNode(String(x))); });
    else if (ch instanceof Node) c.appendChild(ch);
    else c.appendChild(document.createTextNode(String(ch)));
  }
}
function badge(text, tone = "") {
  return el("span", { className: tone ? "badge badge-" + tone : "badge", text: String(text || "") });
}
"""



def _wrap_offline_html(title: str, payload: Dict[str, Any], app_js: str) -> bytes:
    safe_title = _escape_html_text(title)
    safe_json = _safe_json_for_script_tag(payload)
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="UTF-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"  <title>{safe_title}</title>\n"
        f"  <style>\n{_OFFLINE_CSS}\n  </style>\n"
        "</head>\n"
        "<body>\n"
        '  <div id="app-root" class="app"></div>\n'
        f'  <script id="report-data" type="application/json">{safe_json}</script>\n'
        "  <script>\n"
        f"{_PURE_JS_XLSX_RUNTIME}\n"
        f"{_OFFLINE_DOM_HELPERS_JS}\n"
        f"{app_js}\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )
    return html.encode("utf-8")


wrap_offline_html = _wrap_offline_html
