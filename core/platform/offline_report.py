"""DataOG 库级离线交互网页报告 (.html) 与单 Sheet 库级 Excel (.xlsx) 生成引擎.

严格置于公共底座 `core/platform/`，供 `standard_migrator` 与 `cross_comparator` 共享复用，
零模块间私有互引、零外部网络依赖、严格防 XSS 与防 Excel 公式注入。
"""

from __future__ import annotations

import datetime
import io
import json
import re
from typing import Any, Dict, List, Optional, Sequence

import openpyxl
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


# =============================================================================
# 1. 单 Sheet 库级 Excel (.xlsx) 生成器
# =============================================================================

_SINGLE_DB_HEADERS: List[str] = [
    "序号",
    "模式名",
    "表名",
    "对象类型",
    "表注释",
    "列序",
    "库内字段名",
    "库内数据类型",
    "库内注释",
    "键标识",
    "数据项编号",
    "标准中文名称",
    "标准英文简称",
    "标准目标类型",
    "落标状态",
    "差异说明",
    "贯标队列状态",
    "贯标策略",
]

_CROSS_ENV_HEADERS: List[str] = [
    "序号",
    "记录类别",
    "映射表键",
    "来源模式名",
    "来源表名",
    "来源字段名",
    "来源物理类型",
    "来源字段注释",
    "来源数据项编号",
    "来源标准中文名",
    "来源标准目标类型",
    "目标模式名",
    "目标表名",
    "目标字段名",
    "目标物理类型",
    "目标字段注释",
    "目标数据项编号",
    "目标标准中文名",
    "目标标准目标类型",
    "对标状态",
    "目标库落标状态",
    "差异与诊断详情",
]

_MIG_STATUS_LABELS: Dict[str, str] = {
    "COMPLETED": "已达标",
    "PENDING": "待改造",
    "PARTIAL": "部分合规",
    "FAILED": "执行报错",
    "MISSING_TABLE": "缺表",
    "MISSING_COLUMN": "缺列",
    "UNCHECKED": "未核验",
}

_BOOKMARK_LABELS: Dict[str, str] = {
    "UNIFIED": "标准一致",
    "SPLIT_DIFF": "标准异标",
    "SPLIT_PARTIAL": "单侧绑标",
    "SPLIT_UNBOUND": "均未绑标",
    "CANDIDATE": "待配对候选",
    "RECOMMENDED": "推荐同名未配对",
    "SOURCE_ONLY": "仅来源库存在",
    "TARGET_ONLY": "仅目标库存在",
}


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


def build_single_db_excel_bytes(report_data: Dict[str, Any]) -> bytes:
    """将单库库级落标与贯标报告导出为单个 Sheet 包含全库所有表的 Excel (.xlsx)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = _safe_sheet_title("全库落标与贯标报告")
    ws.append(list(_SINGLE_DB_HEADERS))

    row_tags: List[str] = []
    seq = 1
    for tbl in report_data.get("tables") or []:
        t_info = tbl.get("table") or {}
        sch = str(t_info.get("schema_name") or "")
        t_name = str(t_info.get("table_name") or "")
        obj_type = str(t_info.get("object_type") or "TABLE")
        t_comm = str(t_info.get("table_comment") or "")
        cols = tbl.get("columns") or []
        if not cols:
            is_missing = t_info.get("exists") is False and not tbl.get("probe_error")
            probe_err = str(
                tbl.get("probe_error")
                or ("数据库中未找到该表（或已删除/尚未建表）" if is_missing else "表内暂无字段或探查受限")
            )
            status_txt = "未探查" if tbl.get("probe_error") else ("库内缺表" if is_missing else "无字段")
            ws.append(
                [
                    seq,
                    _safe_excel_cell(sch),
                    _safe_excel_cell(t_name),
                    _safe_excel_cell(obj_type),
                    _safe_excel_cell(t_comm),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status_txt,
                    _safe_excel_cell(probe_err),
                    "-",
                    "-",
                ]
            )
            row_tags.append("YELLOW" if tbl.get("probe_error") else ("RED" if is_missing else "GRAY"))
            seq += 1
            continue

        for c in cols:
            key_tags: List[str] = []
            if c.get("is_primary_key"):
                key_tags.append("PK")
            if c.get("is_distribute_key"):
                key_tags.append("DK")
            if c.get("is_partition_key"):
                key_tags.append("PT")

            st = str(c.get("compliance_status") or "UNBOUND")
            q_st = str(c.get("queue_status") or "")
            q_label = _MIG_STATUS_LABELS.get(q_st, q_st) if q_st else "-"
            q_strat = str(c.get("queue_strategy") or "")
            strat_label = (
                "保序重建"
                if q_strat == "TABLE_REBUILD"
                else ("原地修改" if q_strat else "-")
            )

            ws.append(
                [
                    seq,
                    _safe_excel_cell(sch),
                    _safe_excel_cell(t_name),
                    _safe_excel_cell(obj_type),
                    _safe_excel_cell(t_comm),
                    c.get("ordinal_position") or "",
                    _safe_excel_cell(c.get("column_name") or ""),
                    _safe_excel_cell(c.get("data_type") or ""),
                    _safe_excel_cell(c.get("column_comment") or ""),
                    ", ".join(key_tags),
                    _safe_excel_cell(c.get("std_code") or ""),
                    _safe_excel_cell(c.get("std_cn_name") or ""),
                    _safe_excel_cell(c.get("std_en_abbr") or ""),
                    _safe_excel_cell(c.get("std_target_type") or ""),
                    _safe_excel_cell(c.get("compliance_label") or st),
                    _safe_excel_cell(c.get("compliance_detail") or ""),
                    _safe_excel_cell(q_label),
                    _safe_excel_cell(strat_label),
                ]
            )
            tag_map = {
                "COMPLIANT": "GREEN",
                "COMMENT_ONLY": "BLUE",
                "NON_COMPLIANT": "RED",
                "STD_MISSING": "YELLOW",
                "UNBOUND": "GRAY",
            }
            row_tags.append(tag_map.get(st, "GRAY"))
            seq += 1

    widths = [7, 14, 24, 10, 22, 7, 22, 18, 24, 10, 16, 22, 20, 18, 12, 36, 13, 12]
    _apply_single_sheet_style(ws, _SINGLE_DB_HEADERS, widths, row_tags)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _iter_cross_flat_rows(report_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """统一抽取跨环境比对全库扁平明细行（供后端 1-Sheet Excel 与离线 HTML 共用一致口径）."""
    rows: List[Dict[str, Any]] = []

    for pair in report_data.get("pairs") or []:
        tb = pair.get("table_binding") or {}
        t_key = str(tb.get("std_table_key") or pair.get("std_table_key") or "")
        s_sch = str(tb.get("source_schema") or (pair.get("source_panel") or {}).get("schema_name") or "")
        s_tbl = str(tb.get("source_table") or (pair.get("source_panel") or {}).get("table_name") or "")
        t_sch = str(tb.get("target_schema") or (pair.get("target_panel") or {}).get("schema_name") or "")
        t_tbl = str(tb.get("target_table") or (pair.get("target_panel") or {}).get("table_name") or "")

        for cr in pair.get("center_rows") or []:
            sc = cr.get("source_col") or {}
            tc = cr.get("target_col") or {}
            s_std = cr.get("source_std") or {}
            t_std = cr.get("target_std") or {}
            t_comp = cr.get("target_compliance") or {}
            bk_state = str(cr.get("bookmark_state") or "SPLIT_PARTIAL")
            t_comp_status = str(t_comp.get("status") or ("UNBOUND" if tc else ""))
            t_comp_label = str(t_comp.get("label") or ("未绑标" if tc else "-"))

            diffs: List[str] = []
            if bk_state == "SPLIT_DIFF":
                diffs.append(
                    f"两侧绑标不一致 ({cr.get('source_std_code', '')} ≠ {cr.get('target_std_code', '')})"
                )
            elif bk_state == "SPLIT_PARTIAL":
                if not sc:
                    diffs.append("仅目标端已绑标，缺少来源字段配对")
                elif not tc:
                    diffs.append("仅来源端已绑标，缺少目标字段配对")
            if t_comp.get("detail") and t_comp_status != "COMPLIANT":
                diffs.append(f"目标端: {t_comp['detail']}")
            if not diffs:
                if bk_state == "UNIFIED" and t_comp_status == "COMPLIANT":
                    diffs.append("双侧标准一致且目标端已落标")
                elif bk_state == "UNIFIED":
                    diffs.append("双侧标准一致")

            rows.append(
                {
                    "row_kind": "已保存映射",
                    "std_table_key": t_key,
                    "source_schema": s_sch,
                    "source_table": s_tbl,
                    "source_col_name": str(sc.get("column_name") or ""),
                    "source_col_type": str(sc.get("data_type") or ""),
                    "source_col_comment": str(sc.get("column_comment") or ""),
                    "source_std_code": str(cr.get("source_std_code") or ""),
                    "source_std_cn": str(s_std.get("cn_name") or ""),
                    "source_std_type": str(s_std.get("resolved_type") or ""),
                    "target_schema": t_sch,
                    "target_table": t_tbl,
                    "target_col_name": str(tc.get("column_name") or ""),
                    "target_col_type": str(tc.get("data_type") or ""),
                    "target_col_comment": str(tc.get("column_comment") or ""),
                    "target_std_code": str(cr.get("target_std_code") or ""),
                    "target_std_cn": str(t_std.get("cn_name") or ""),
                    "target_std_type": str(t_std.get("resolved_type") or ""),
                    "bookmark_state": bk_state,
                    "bookmark_label": _BOOKMARK_LABELS.get(bk_state, bk_state),
                    "target_compliance_status": t_comp_status,
                    "target_compliance_label": t_comp_label,
                    "detail": "；".join(diffs),
                }
            )

        for sc in pair.get("source_candidates") or []:
            rows.append(
                {
                    "row_kind": "已保存映射",
                    "std_table_key": t_key,
                    "source_schema": s_sch,
                    "source_table": s_tbl,
                    "source_col_name": str(sc.get("column_name") or ""),
                    "source_col_type": str(sc.get("data_type") or ""),
                    "source_col_comment": str(sc.get("column_comment") or ""),
                    "source_std_code": "",
                    "source_std_cn": "",
                    "source_std_type": "",
                    "target_schema": t_sch,
                    "target_table": t_tbl,
                    "target_col_name": "",
                    "target_col_type": "",
                    "target_col_comment": "",
                    "target_std_code": "",
                    "target_std_cn": "",
                    "target_std_type": "",
                    "bookmark_state": "CANDIDATE",
                    "bookmark_label": "待配对候选",
                    "target_compliance_status": "",
                    "target_compliance_label": "-",
                    "detail": "来源表未绑标待选字段",
                }
            )

        for tc in pair.get("target_candidates") or []:
            rows.append(
                {
                    "row_kind": "已保存映射",
                    "std_table_key": t_key,
                    "source_schema": s_sch,
                    "source_table": s_tbl,
                    "source_col_name": "",
                    "source_col_type": "",
                    "source_col_comment": "",
                    "source_std_code": "",
                    "source_std_cn": "",
                    "source_std_type": "",
                    "target_schema": t_sch,
                    "target_table": t_tbl,
                    "target_col_name": str(tc.get("column_name") or ""),
                    "target_col_type": str(tc.get("data_type") or ""),
                    "target_col_comment": str(tc.get("column_comment") or ""),
                    "target_std_code": "",
                    "target_std_cn": "",
                    "target_std_type": "",
                    "bookmark_state": "CANDIDATE",
                    "bookmark_label": "待配对候选",
                    "target_compliance_status": "UNBOUND",
                    "target_compliance_label": "未绑标",
                    "detail": "目标表未绑标待选字段",
                }
            )

    for rec in report_data.get("recommended_pairings") or []:
        s_cnt = int(rec.get("source_bound_count") or 0)
        t_cnt = int(rec.get("target_bound_count") or 0)
        m_type = str(rec.get("match_type") or "同名表")
        rows.append(
            {
                "row_kind": "推荐同名(未保存)",
                "std_table_key": f"推荐: {rec.get('table_name', '')}",
                "source_schema": str(rec.get("source_schema") or ""),
                "source_table": str(rec.get("source_table") or ""),
                "source_col_name": "(整表推荐 · 未预比对)",
                "source_col_type": str(rec.get("source_object_type") or "TABLE"),
                "source_col_comment": str(rec.get("source_comment") or ""),
                "source_std_code": f"已绑 {s_cnt} 列" if s_cnt else "",
                "source_std_cn": "",
                "source_std_type": "",
                "target_schema": str(rec.get("target_schema") or ""),
                "target_table": str(rec.get("target_table") or ""),
                "target_col_name": "(整表推荐 · 未预比对)",
                "target_col_type": str(rec.get("target_object_type") or "TABLE"),
                "target_col_comment": str(rec.get("target_comment") or ""),
                "target_std_code": f"已绑 {t_cnt} 列" if t_cnt else "",
                "target_std_cn": "",
                "target_std_type": "",
                "bookmark_state": "RECOMMENDED",
                "bookmark_label": "推荐同名未配对",
                "target_compliance_status": "",
                "target_compliance_label": "-",
                "detail": f"{m_type}（尚未保存跨环境配对，未自动执行字段预比对）",
            }
        )

    for st_tbl in report_data.get("source_only_tables") or []:
        b_cnt = int(st_tbl.get("bound_count") or 0)
        rows.append(
            {
                "row_kind": "仅来源库存在",
                "std_table_key": f"仅源库: {st_tbl.get('table_name', '')}",
                "source_schema": str(st_tbl.get("schema_name") or ""),
                "source_table": str(st_tbl.get("table_name") or ""),
                "source_col_name": "(整表仅来源库存在)",
                "source_col_type": str(st_tbl.get("object_type") or "TABLE"),
                "source_col_comment": str(st_tbl.get("table_comment") or ""),
                "source_std_code": f"已绑 {b_cnt} 列" if b_cnt else "",
                "source_std_cn": "",
                "source_std_type": "",
                "target_schema": "",
                "target_table": "",
                "target_col_name": "",
                "target_col_type": "",
                "target_col_comment": "",
                "target_std_code": "",
                "target_std_cn": "",
                "target_std_type": "",
                "bookmark_state": "SOURCE_ONLY",
                "bookmark_label": "仅来源库存在",
                "target_compliance_status": "",
                "target_compliance_label": "-",
                "detail": "仅来源库存在该表（目标库无已保存配对或同名表）",
            }
        )

    for tt_tbl in report_data.get("target_only_tables") or []:
        b_cnt = int(tt_tbl.get("bound_count") or 0)
        rows.append(
            {
                "row_kind": "仅目标库存在",
                "std_table_key": f"仅目标库: {tt_tbl.get('table_name', '')}",
                "source_schema": "",
                "source_table": "",
                "source_col_name": "",
                "source_col_type": "",
                "source_col_comment": "",
                "source_std_code": "",
                "source_std_cn": "",
                "source_std_type": "",
                "target_schema": str(tt_tbl.get("schema_name") or ""),
                "target_table": str(tt_tbl.get("table_name") or ""),
                "target_col_name": "(整表仅目标库存在)",
                "target_col_type": str(tt_tbl.get("object_type") or "TABLE"),
                "target_col_comment": str(tt_tbl.get("table_comment") or ""),
                "target_std_code": f"已绑 {b_cnt} 列" if b_cnt else "",
                "target_std_cn": "",
                "target_std_type": "",
                "bookmark_state": "TARGET_ONLY",
                "bookmark_label": "仅目标库存在",
                "target_compliance_status": "",
                "target_compliance_label": "-",
                "detail": "仅目标库存在该表（来源库无已保存配对或同名表）",
            }
        )

    return rows


def build_cross_env_excel_bytes(report_data: Dict[str, Any]) -> bytes:
    """将跨环境库级比对报告导出为单个 Sheet 包含所有已配对表与推荐同名表的 Excel (.xlsx)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = _safe_sheet_title("跨环境库级比对报告")
    ws.append(list(_CROSS_ENV_HEADERS))

    flat_rows = _iter_cross_flat_rows(report_data)
    row_tags: List[str] = []
    for idx, r in enumerate(flat_rows, start=1):
        ws.append(
            [
                idx,
                _safe_excel_cell(r["row_kind"]),
                _safe_excel_cell(r["std_table_key"]),
                _safe_excel_cell(r["source_schema"]),
                _safe_excel_cell(r["source_table"]),
                _safe_excel_cell(r["source_col_name"]),
                _safe_excel_cell(r["source_col_type"]),
                _safe_excel_cell(r["source_col_comment"]),
                _safe_excel_cell(r["source_std_code"]),
                _safe_excel_cell(r["source_std_cn"]),
                _safe_excel_cell(r["source_std_type"]),
                _safe_excel_cell(r["target_schema"]),
                _safe_excel_cell(r["target_table"]),
                _safe_excel_cell(r["target_col_name"]),
                _safe_excel_cell(r["target_col_type"]),
                _safe_excel_cell(r["target_col_comment"]),
                _safe_excel_cell(r["target_std_code"]),
                _safe_excel_cell(r["target_std_cn"]),
                _safe_excel_cell(r["target_std_type"]),
                _safe_excel_cell(r["bookmark_label"]),
                _safe_excel_cell(r["target_compliance_label"]),
                _safe_excel_cell(r["detail"]),
            ]
        )
        bk = r["bookmark_state"]
        t_comp = r["target_compliance_status"]
        if bk == "RECOMMENDED":
            row_tags.append("LAVENDER")
        elif bk == "SPLIT_DIFF":
            row_tags.append("RED")
        elif bk == "UNIFIED":
            if t_comp == "NON_COMPLIANT":
                row_tags.append("YELLOW")
            else:
                row_tags.append("GREEN")
        elif bk == "SPLIT_PARTIAL":
            row_tags.append("BLUE")
        else:
            row_tags.append("GRAY")

    widths = [7, 15, 20, 13, 22, 20, 16, 22, 15, 20, 16, 13, 22, 20, 16, 22, 15, 20, 16, 14, 13, 38]
    _apply_single_sheet_style(ws, _CROSS_ENV_HEADERS, widths, row_tags)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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


# =============================================================================
# 3. 单库离线交互网页报告 (.html)
# =============================================================================

_SINGLE_DB_APP_JS = r"""
(() => {
  const DATA = JSON.parse(document.getElementById("report-data").textContent);
  const CONN = DATA.connection || {};
  const AGG = DATA.aggregate || {};
  const TABLES = DATA.tables || [];
  const MIG_ITEMS = DATA.migration_items || [];
  const WARNINGS = DATA.warnings || [];
  const HEADERS = DATA.excel_headers || [];
  const MIG_LABELS = {
    COMPLETED: "已达标", PENDING: "待改造", PARTIAL: "部分合规",
    FAILED: "执行报错", MISSING_TABLE: "缺表", MISSING_COLUMN: "缺列", UNCHECKED: "未核验"
  };

  const state = {
    theme: window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    tab: "overview", // overview | queue | warnings
    selectedTableKey: "__ALL__", // __ALL__ | schema.table
    allViewMode: "tables", // tables | fields
    tableListFilter: "ALL", // ALL | NON_COMPLIANT | COMPLIANT | HAS_UNBOUND | UNBOUND_ALL
    tableKeyword: "",
    colStatusFilter: "ALL", // ALL | COMPLIANT | COMMENT_ONLY | NON_COMPLIANT | STD_MISSING | UNBOUND | IN_QUEUE
    colKeyword: "",
    page: 1,
    pageSize: 200,
    qFilter: "ALL",
    qKeyword: "",
    expandedSqlIds: new Set(),
  };

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", state.theme);
  }
  applyTheme();

  function tblKey(t) {
    const ti = t.table || {};
    return String(ti.schema_name || "") + "." + String(ti.table_name || "");
  }

  function statusTone(st) {
    if (st === "COMPLIANT") return "ok";
    if (st === "COMMENT_ONLY") return "info";
    if (st === "NON_COMPLIANT") return "bad";
    if (st === "STD_MISSING") return "warn";
    return "";
  }

  function excelTone(st) {
    if (st === "COMPLIANT") return "GREEN";
    if (st === "COMMENT_ONLY") return "BLUE";
    if (st === "NON_COMPLIANT") return "RED";
    if (st === "STD_MISSING") return "YELLOW";
    return "GRAY";
  }

  function toExcelRows(tablesSubset, colFilterFn = null) {
    const out = [];
    let seq = 1;
    for (const tbl of tablesSubset) {
      const ti = tbl.table || {};
      const cols = tbl.columns || [];
      if (!cols.length && !colFilterFn) {
        const isMissing = ti.exists === false && !tbl.probe_error;
        const statusTxt = tbl.probe_error ? "未探查" : (isMissing ? "库内缺表" : "无字段");
        const detailTxt = tbl.probe_error || (isMissing ? "数据库中未找到该表（或已删除/尚未建表）" : "表内暂无字段");
        out.push({
          tone: tbl.probe_error ? "YELLOW" : (isMissing ? "RED" : "GRAY"),
          values: [
            seq++, ti.schema_name || "", ti.table_name || "", ti.object_type || "TABLE", ti.table_comment || "",
            "", "", "", "", "", "", "", "", "", statusTxt, detailTxt, "-", "-",
          ],
        });
        continue;
      }
      for (const c of cols) {
        if (colFilterFn && !colFilterFn(tbl, c)) continue;
        const keys = [];
        if (c.is_primary_key) keys.push("PK");
        if (c.is_distribute_key) keys.push("DK");
        if (c.is_partition_key) keys.push("PT");
        const qLabel = c.queue_status ? (MIG_LABELS[c.queue_status] || c.queue_status) : "-";
        const stratLabel = c.queue_strategy === "TABLE_REBUILD" ? "保序重建" : (c.queue_strategy ? "原地修改" : "-");
        out.push({
          tone: excelTone(c.compliance_status),
          values: [
            seq++, ti.schema_name || "", ti.table_name || "", ti.object_type || "TABLE", ti.table_comment || "",
            c.ordinal_position || "", c.column_name || "", c.data_type || "", c.column_comment || "",
            keys.join(", "), c.std_code || "", c.std_cn_name || "", c.std_en_abbr || "",
            c.std_target_type || "", c.compliance_label || c.compliance_status || "",
            c.compliance_detail || "", qLabel, stratLabel,
          ],
        });
      }
    }
    return out;
  }

  function matchColFilter(c) {
    if (state.colStatusFilter === "IN_QUEUE") {
      if (!c.queue_status) return false;
    } else if (state.colStatusFilter !== "ALL" && c.compliance_status !== state.colStatusFilter) {
      return false;
    }
    const kw = state.colKeyword.trim().toLowerCase();
    if (!kw) return true;
    return [c.column_name, c.column_comment, c.data_type, c.std_code, c.std_cn_name, c.std_en_abbr, c.compliance_detail]
      .some((v) => String(v || "").toLowerCase().includes(kw));
  }

  function hasOverviewFilter() {
    return (
      state.selectedTableKey !== "__ALL__" ||
      state.colStatusFilter !== "ALL" ||
      state.colKeyword.trim() !== "" ||
      state.tableListFilter !== "ALL" ||
      state.tableKeyword.trim() !== ""
    );
  }

  function hasQueueFilter() {
    return state.tab === "queue" && (state.qFilter !== "ALL" || state.qKeyword.trim() !== "");
  }

  function hasActiveFilter() {
    return hasOverviewFilter() || hasQueueFilter();
  }

  function filteredTablesList() {
    const kw = state.tableKeyword.trim().toLowerCase();
    return TABLES.filter((t) => {
      const ti = t.table || {};
      const s = t.summary || {};
      if (state.tableListFilter === "NON_COMPLIANT") {
        if ((s.non_compliant_cols || 0) === 0 && (s.comment_only_cols || 0) === 0 && (s.std_missing_cols || 0) === 0) return false;
      } else if (state.tableListFilter === "COMPLIANT") {
        if ((s.total_cols || 0) === 0 || (s.compliant_cols || 0) < (s.total_cols || 0)) return false;
      } else if (state.tableListFilter === "HAS_UNBOUND") {
        if ((s.unbound_cols || 0) === 0) return false;
      } else if (state.tableListFilter === "UNBOUND_ALL") {
        if ((s.bound_cols || 0) > 0) return false;
      }
      if (!kw) return true;
      return [ti.schema_name, ti.table_name, ti.table_comment]
        .some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  function filteredQueueList() {
    const kw = state.qKeyword.trim().toLowerCase();
    return MIG_ITEMS.filter((it) => {
      if (state.qFilter === "REBUILD") {
        if (it.strategy !== "TABLE_REBUILD" && !it.is_distribute_key && String(it.table_orientation || "").toUpperCase() !== "COLUMN") return false;
      } else if (state.qFilter !== "ALL" && it.status !== state.qFilter) {
        return false;
      }
      if (!kw) return true;
      return [it.schema_name, it.table_name, it.old_col_name, it.new_col_name, it.new_col_comment, it.status_detail]
        .some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  function exportAllExcel() {
    const rows = toExcelRows(TABLES);
    const dbSlug = String(CONN.name || "DataOG").replace(/[\\/:*?"<>|]/g, "_");
    const blob = buildSingleSheetXlsxBlob("全库落标与贯标报告", HEADERS, rows);
    triggerDownload(blob, "DataOG_" + dbSlug + "_库级落标与贯标报告.xlsx");
  }

  function exportFilteredExcel() {
    const dbSlug = String(CONN.name || "DataOG").replace(/[\\/:*?"<>|]/g, "_");
    if (hasQueueFilter()) {
      const qHeaders = ["序号", "模式名", "表名", "列序", "原字段名", "原类型", "标准字段名", "目标类型", "目标注释", "策略", "状态", "状态说明"];
      const qRows = filteredQueueList().map((it, i) => {
        const isReb = it.strategy === "TABLE_REBUILD" || Boolean(it.is_distribute_key) || String(it.table_orientation || "").toUpperCase() === "COLUMN";
        const stLabel = MIG_LABELS[it.status] || it.status || "-";
        const tone = it.status === "COMPLETED" ? "GREEN" : (it.status === "FAILED" || it.status === "MISSING_TABLE" || it.status === "MISSING_COLUMN" ? "RED" : "YELLOW");
        return {
          tone,
          values: [
            i + 1, it.schema_name || "", it.table_name || "", it.ordinal_position || "",
            it.old_col_name || "", it.old_col_type || "", it.new_col_name || "", it.new_col_type || "",
            it.new_col_comment || it.old_col_comment || "", isReb ? "保序重建" : "原地修改",
            stLabel, it.error_message || it.status_detail || "",
          ],
        };
      });
      const blob = buildSingleSheetXlsxBlob("贯标改造筛选清单", qHeaders, qRows);
      triggerDownload(blob, "DataOG_" + dbSlug + "_贯标筛选清单.xlsx");
      return;
    }
    const tbls = state.selectedTableKey === "__ALL__"
      ? filteredTablesList()
      : TABLES.filter((t) => tblKey(t) === state.selectedTableKey);
    const colFilterActive = state.colStatusFilter !== "ALL" || state.colKeyword.trim() !== "";
    const rows = toExcelRows(tbls, colFilterActive ? (_t, c) => matchColFilter(c) : null);
    const blob = buildSingleSheetXlsxBlob("落标核验筛选明细", HEADERS, rows);
    triggerDownload(blob, "DataOG_" + dbSlug + "_筛选落标报告.xlsx");
  }

  // Persistent DOM mounts & search inputs (never detached while typing)
  const themeBtn = el("button", {
    type: "button", className: "btn",
    text: state.theme === "dark" ? "切换浅色" : "切换深色",
    onClick: () => {
      state.theme = state.theme === "dark" ? "light" : "dark";
      applyTheme();
      themeBtn.textContent = state.theme === "dark" ? "切换浅色" : "切换深色";
    },
  });
  const exportFilteredBtn = el("button", {
    type: "button", className: "btn", hidden: !hasActiveFilter(),
    text: "导出当前筛选 (.xlsx)", onClick: exportFilteredExcel,
  });
  function syncExportFilteredBtn() {
    exportFilteredBtn.hidden = !hasActiveFilter();
  }

  const tblSearchInput = el("input", {
    className: "input", type: "search", placeholder: "搜索模式 / 表名 / 注释...",
    value: state.tableKeyword,
    onInput: (e) => {
      state.tableKeyword = e.target.value;
      state.page = 1;
      renderSidebarList();
      if (state.selectedTableKey === "__ALL__") {
        updateSummaryPillLabel();
        renderOverviewBody();
      }
      syncExportFilteredBtn();
    },
  });
  const colSearchInput = el("input", {
    className: "input", type: "search", style: { width: "220px" }, placeholder: "搜索字段名 / 标准编号 / 中文名...",
    value: state.colKeyword,
    onInput: (e) => {
      state.colKeyword = e.target.value;
      state.page = 1;
      renderOverviewBody();
      syncExportFilteredBtn();
    },
  });
  const qSearchInput = el("input", {
    className: "input", type: "search", style: { width: "220px" }, placeholder: "搜索表名 / 字段 / 注释...",
    value: state.qKeyword,
    onInput: (e) => {
      state.qKeyword = e.target.value;
      renderQueueBody();
      syncExportFilteredBtn();
    },
  });

  const sidebarListMount = el("div", { className: "sidebar-list" });
  const overviewSubbarMount = el("div", { className: "subbar" });
  const overviewBodyMount = el("div", { style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" } });
  const queueSubbarMount = el("div", { className: "subbar" });
  const queueBodyMount = el("div", { style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" } });
  const tabbarMount = el("nav", { className: "tabbar" });
  const workspaceMount = el("div", { style: { flex: "1", minHeight: "0", display: "flex", overflow: "hidden" } });
  let summaryPillBtn = null;

  function updateSummaryPillLabel() {
    if (summaryPillBtn) {
      summaryPillBtn.textContent = "库表汇总矩阵 (" + filteredTablesList().length + " 表)";
    }
  }

  function renderTopbar() {
    return el("header", { className: "topbar" },
      el("div", { className: "topbar-left" },
        el("span", { className: "brand-mark", text: "OG" }),
        el("h1", { className: "title-main", text: (CONN.name || "数据源") + " · 库级落标与贯标离线报告" }),
        badge(CONN.db_type || "DB", "info"),
        badge("格式: " + (DATA.format_profile_label || DATA.format_profile || "AUTO")),
        badge("生成: " + (DATA.generated_at || ""))
      ),
      el("div", { className: "topbar-actions" },
        themeBtn,
        exportFilteredBtn,
        el("button", { type: "button", className: "btn btn-primary", text: "导出全库 Excel (.xlsx)", onClick: exportAllExcel })
      )
    );
  }

  function renderKpis() {
    const cards = [
      { label: "库表总数", val: String(AGG.total_tables ?? TABLES.length), sub: "已绑标表 " + (AGG.bound_tables_count ?? 0) + " / 全达标表 " + (AGG.full_compliant_tables_count ?? 0) },
      { label: "总字段数", val: String(AGG.total_cols ?? 0), sub: "已绑标 " + (AGG.bound_cols ?? 0) + " · 未绑标 " + (AGG.unbound_cols ?? 0) },
      { label: "绑标覆盖率", val: String(AGG.coverage_pct ?? 0) + "%", pct: Number(AGG.coverage_pct ?? 0), sub: (AGG.bound_cols ?? 0) + " / " + (AGG.total_cols ?? 0) + " 列" },
      { label: "标准落标率", val: String(AGG.compliant_pct ?? 0) + "%", pct: Number(AGG.compliant_pct ?? 0), sub: "已落标 " + (AGG.compliant_cols ?? 0) + " · 仅差注释 " + (AGG.comment_only_cols ?? 0) },
      { label: "待整改字段", val: String((AGG.non_compliant_cols ?? 0) + (AGG.comment_only_cols ?? 0) + (AGG.std_missing_cols ?? 0)), sub: "未落标 " + (AGG.non_compliant_cols ?? 0) + " · 标准缺失 " + (AGG.std_missing_cols ?? 0) },
      { label: "贯标改造队列", val: String(MIG_ITEMS.length), sub: "待改 " + (AGG.queue_pending ?? 0) + " · 已达标 " + (AGG.queue_completed ?? 0) },
    ];
    return el("section", { className: "kpi-strip" },
      cards.map((c) => el("div", { className: "kpi-card" },
        el("div", { className: "kpi-label", text: c.label }),
        el("div", { className: "kpi-val", text: c.val }),
        c.pct !== undefined ? el("div", { className: "progress-mini" }, el("span", { style: { width: Math.min(100, Math.max(0, c.pct)) + "%" } })) : null,
        el("div", { className: "kpi-sub", text: c.sub })
      ))
    );
  }

  function renderColumnsTable(colEntries, showTableCols) {
    if (!colEntries.length) {
      return el("div", { className: "empty", text: "当前筛选条件下暂无字段记录" });
    }
    const startIdx = (state.page - 1) * state.pageSize;
    const endIdx = Math.min(colEntries.length, startIdx + state.pageSize);
    const slice = colEntries.slice(startIdx, endIdx);

    const ths = ["#"];
    if (showTableCols) ths.push("模式.表名");
    ths.push("物理字段名", "库内类型", "库内注释", "键", "数据项编号", "标准中文名", "标准英文简称", "标准目标类型", "落标状态", "差异说明", "贯标队列");

    const trs = slice.map(({ tbl, c }) => {
      const ti = tbl.table || {};
      const keys = [];
      if (c.is_primary_key) keys.push("PK");
      if (c.is_distribute_key) keys.push("DK");
      if (c.is_partition_key) keys.push("PT");
      const qTxt = c.queue_status ? (MIG_LABELS[c.queue_status] || c.queue_status) : "-";
      const cells = [
        el("td", { className: "mono", text: String(c.ordinal_position || "-") }),
      ];
      if (showTableCols) {
        cells.push(el("td", { className: "mono", text: (ti.schema_name || "") + "." + (ti.table_name || "") }));
      }
      cells.push(
        el("td", { className: "mono", style: { fontWeight: "600" }, text: c.column_name || "" }),
        el("td", { className: "mono", text: c.data_type || "" }),
        el("td", { text: c.column_comment || "-" }),
        el("td", { className: "mono", text: keys.join(",") || "-" }),
        el("td", { className: "mono", text: c.std_code || "-" }),
        el("td", { text: c.std_cn_name || "-" }),
        el("td", { className: "mono", text: c.std_en_abbr || "-" }),
        el("td", { className: "mono", text: c.std_target_type || "-" }),
        el("td", {}, badge(c.compliance_label || c.compliance_status, statusTone(c.compliance_status))),
        el("td", { style: { maxWidth: "280px" }, text: c.compliance_detail || "-" }),
        el("td", {}, c.queue_status ? badge(qTxt, c.queue_status === "COMPLETED" ? "ok" : "warn") : el("span", { style: { color: "var(--muted)" }, text: "-" }))
      );
      return el("tr", {}, cells);
    });

    const totalPages = Math.max(1, Math.ceil(colEntries.length / state.pageSize));
    const pager = colEntries.length > state.pageSize ? el("div", { className: "pager" },
      el("span", { style: { color: "var(--muted)", fontSize: "11.5px" }, text: "显示第 " + (startIdx + 1) + "-" + endIdx + " 条，共 " + colEntries.length + " 条（导出 Excel 将包含全部筛选行）" }),
      el("div", { className: "subbar-group" },
        el("button", { type: "button", className: "btn btn-xs", disabled: state.page <= 1, text: "上一页", onClick: () => { state.page--; renderOverviewBody(); } }),
        el("span", { className: "mono", text: state.page + " / " + totalPages }),
        el("button", { type: "button", className: "btn btn-xs", disabled: state.page >= totalPages, text: "下一页", onClick: () => { state.page++; renderOverviewBody(); } })
      )
    ) : null;

    return el("div", { style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column" } },
      el("div", { className: "table-wrap" },
        el("table", { className: "grid" },
          el("thead", {}, el("tr", {}, ths.map((h) => el("th", { text: h })))),
          el("tbody", {}, trs)
        )
      ),
      pager
    );
  }

  function renderSidebarList() {
    const tblList = filteredTablesList();
    const bySchema = new Map();
    for (const t of tblList) {
      const sch = (t.table || {}).schema_name || "public";
      if (!bySchema.has(sch)) bySchema.set(sch, []);
      bySchema.get(sch).push(t);
    }

    const sidebarNodes = [
      el("button", {
        type: "button",
        className: "nav-item " + (state.selectedTableKey === "__ALL__" ? "is-active" : ""),
        onClick: () => {
          state.selectedTableKey = "__ALL__";
          state.page = 1;
          renderSidebarList();
          renderOverviewMain();
          syncExportFilteredBtn();
        },
      },
        el("span", { className: "nav-title", text: "总览：全部库表 (" + tblList.length + ")" }),
        badge(String(AGG.total_cols ?? 0), "info")
      ),
    ];

    for (const [sch, items] of bySchema.entries()) {
      sidebarNodes.push(el("div", { className: "schema-hdr", text: sch + " (" + items.length + ")" }));
      for (const t of items) {
        const ti = t.table || {};
        const s = t.summary || {};
        const k = tblKey(t);
        let bNode = badge(String(s.total_cols || 0) + "列");
        if (s.bound_cols > 0) {
          const tone = s.non_compliant_cols > 0 ? "bad" : (s.compliant_cols >= s.bound_cols ? "ok" : "info");
          bNode = badge(String(s.compliant_cols || 0) + "/" + String(s.bound_cols || 0), tone);
        } else if (ti.exists === false) {
          bNode = badge("缺表", "bad");
        }
        sidebarNodes.push(el("button", {
          type: "button",
          className: "nav-item " + (state.selectedTableKey === k ? "is-active" : ""),
          title: ti.table_comment ? ti.table_name + " — " + ti.table_comment : ti.table_name,
          onClick: () => {
            state.selectedTableKey = k;
            state.page = 1;
            renderSidebarList();
            renderOverviewMain();
            syncExportFilteredBtn();
          },
        },
          el("span", { className: "nav-title", text: ti.table_name || "" }),
          bNode
        ));
      }
    }
    clearEl(sidebarListMount, sidebarNodes);
  }

  function renderOverviewSubbar() {
    const tblList = filteredTablesList();
    const activeTbl = state.selectedTableKey === "__ALL__"
      ? null
      : TABLES.find((t) => tblKey(t) === state.selectedTableKey) || null;
    const filterPillsDef = [
      { k: "ALL", l: "全部" },
      { k: "COMPLIANT", l: "已落标" },
      { k: "COMMENT_ONLY", l: "仅差注释" },
      { k: "NON_COMPLIANT", l: "未落标" },
      { k: "STD_MISSING", l: "标准缺失" },
      { k: "UNBOUND", l: "未绑标" },
      { k: "IN_QUEUE", l: "在贯标队列" },
    ];

    colSearchInput.value = state.colKeyword;
    summaryPillBtn = null;

    if (!activeTbl) {
      summaryPillBtn = el("button", {
        type: "button",
        className: "pill " + (state.allViewMode === "tables" ? "is-active" : ""),
        text: "库表汇总矩阵 (" + tblList.length + " 表)",
        onClick: () => {
          state.allViewMode = "tables";
          state.page = 1;
          renderOverviewMain();
          syncExportFilteredBtn();
        },
      });
      const fieldsPillBtn = el("button", {
        type: "button",
        className: "pill " + (state.allViewMode === "fields" ? "is-active" : ""),
        text: "全库字段平铺明细",
        onClick: () => {
          state.allViewMode = "fields";
          state.page = 1;
          renderOverviewMain();
          syncExportFilteredBtn();
        },
      });
      clearEl(
        overviewSubbarMount,
        el("div", { className: "subbar-group" },
          el("div", { className: "pills" }, summaryPillBtn, fieldsPillBtn),
          state.allViewMode === "fields"
            ? el("div", { className: "pills" }, filterPillsDef.map((fp) => el("button", {
                type: "button", className: "pill " + (state.colStatusFilter === fp.k ? "is-active" : ""),
                text: fp.l,
                onClick: () => {
                  state.colStatusFilter = fp.k;
                  state.page = 1;
                  renderOverviewSubbar();
                  renderOverviewBody();
                  syncExportFilteredBtn();
                },
              })))
            : null
        ),
        state.allViewMode === "fields" ? colSearchInput : null
      );
      return;
    }

    const ti = activeTbl.table || {};
    const s = activeTbl.summary || {};
    clearEl(
      overviewSubbarMount,
      el("div", { className: "subbar-group" },
        el("span", { className: "mono", style: { fontWeight: "700" }, text: tblKey(activeTbl) }),
        badge(ti.object_type || "TABLE", "info"),
        ti.exists === false
          ? badge("库内缺表", "bad")
          : badge("落标率 " + String(s.compliant_pct ?? 0) + "%", (s.non_compliant_cols || 0) > 0 ? "warn" : "ok"),
        el("div", { className: "pills" }, filterPillsDef.map((fp) => el("button", {
          type: "button", className: "pill " + (state.colStatusFilter === fp.k ? "is-active" : ""),
          text: fp.l,
          onClick: () => {
            state.colStatusFilter = fp.k;
            state.page = 1;
            renderOverviewSubbar();
            renderOverviewBody();
            syncExportFilteredBtn();
          },
        })))
      ),
      colSearchInput
    );
  }

  function renderOverviewBody() {
    const tblList = filteredTablesList();
    const activeTbl = state.selectedTableKey === "__ALL__"
      ? null
      : TABLES.find((t) => tblKey(t) === state.selectedTableKey) || null;

    if (!activeTbl) {
      if (state.allViewMode === "tables") {
        clearEl(
          overviewBodyMount,
          tblList.length
            ? el("div", { className: "table-wrap" },
                el("table", { className: "grid" },
                  el("thead", {}, el("tr", {},
                    ["模式名", "表名", "类型", "表注释", "总列数", "已绑标", "已落标", "仅差注释", "未落标", "未绑标", "绑标率", "落标率"].map((h) => el("th", { text: h }))
                  )),
                  el("tbody", {}, tblList.map((t) => {
                    const ti = t.table || {};
                    const s = t.summary || {};
                    const k = tblKey(t);
                    return el("tr", {
                      className: "is-clickable",
                      onClick: () => {
                        state.selectedTableKey = k;
                        state.page = 1;
                        renderSidebarList();
                        renderOverviewMain();
                        syncExportFilteredBtn();
                      },
                    },
                      el("td", { className: "mono", text: ti.schema_name || "" }),
                      el("td", { className: "mono", style: { fontWeight: "600", color: "var(--accent)" }, text: ti.table_name || "" }),
                      el("td", {}, badge(ti.object_type || "TABLE")),
                      el("td", { text: ti.table_comment || "-" }),
                      el("td", { className: "mono", text: String(s.total_cols || 0) }),
                      el("td", { className: "mono", text: String(s.bound_cols || 0) }),
                      el("td", {}, badge(String(s.compliant_cols || 0), (s.compliant_cols || 0) > 0 ? "ok" : "")),
                      el("td", {}, badge(String(s.comment_only_cols || 0), (s.comment_only_cols || 0) > 0 ? "info" : "")),
                      el("td", {}, badge(String(s.non_compliant_cols || 0), (s.non_compliant_cols || 0) > 0 ? "bad" : "")),
                      el("td", { className: "mono", text: String(s.unbound_cols || 0) }),
                      el("td", { className: "mono", text: String(s.coverage_pct ?? 0) + "%" }),
                      el("td", { className: "mono", text: String(s.compliant_pct ?? 0) + "%" })
                    );
                  }))
                )
              )
            : el("div", { className: "empty", text: "暂无匹配的库表" })
        );
        return;
      }
      const entries = [];
      for (const t of tblList) {
        for (const c of (t.columns || [])) {
          if (matchColFilter(c)) entries.push({ tbl: t, c });
        }
      }
      clearEl(overviewBodyMount, renderColumnsTable(entries, true));
      return;
    }

    const ti = activeTbl.table || {};
    const entries = (activeTbl.columns || []).filter(matchColFilter).map((c) => ({ tbl: activeTbl, c }));
    const isMissing = ti.exists === false && !activeTbl.probe_error;
    const infoBanner = (ti.table_comment || (ti.distribute_keys || []).length || (ti.partition_keys || []).length || activeTbl.probe_error || isMissing)
      ? el("div", { className: "banner " + ((activeTbl.probe_error || isMissing) ? "is-warn" : "") },
          el("div", { className: "subbar-group" },
            ti.table_comment ? el("span", { text: "表注释: " + ti.table_comment }) : null,
            ti.orientation ? badge("存储: " + ti.orientation) : null,
            (ti.distribute_keys || []).length ? badge("分布键: " + ti.distribute_keys.join(", "), "info") : null,
            (ti.partition_keys || []).length ? badge("分区键: " + ti.partition_keys.join(", "), "warn") : null,
            activeTbl.probe_error ? el("span", { text: "探查提示: " + activeTbl.probe_error }) : null,
            isMissing ? el("span", { text: "提示: 数据库中未找到该表（或已删除/尚未建表）" }) : null
          )
        )
      : null;
    clearEl(overviewBodyMount, infoBanner, renderColumnsTable(entries, false));
  }

  function renderOverviewMain() {
    renderOverviewSubbar();
    renderOverviewBody();
  }

  function renderOverviewTab() {
    tblSearchInput.value = state.tableKeyword;
    const filterSelect = el("select", {
      className: "input", "aria-label": "按落标状态筛表",
      onChange: (e) => {
        state.tableListFilter = e.target.value;
        state.page = 1;
        renderSidebarList();
        if (state.selectedTableKey === "__ALL__") {
          updateSummaryPillLabel();
          renderOverviewBody();
        }
        syncExportFilteredBtn();
      },
    },
      [
        { v: "ALL", t: "全部库表 (" + TABLES.length + ")" },
        { v: "NON_COMPLIANT", t: "含未落标/差注释" },
        { v: "COMPLIANT", t: "全字段已落标" },
        { v: "HAS_UNBOUND", t: "含未绑标字段" },
        { v: "UNBOUND_ALL", t: "整表均未绑标" },
      ].map((o) => {
        const opt = el("option", { value: o.v, text: o.t });
        if (state.tableListFilter === o.v) opt.selected = true;
        return opt;
      })
    );
    const sidebar = el("aside", { className: "sidebar" },
      el("div", { className: "sidebar-head" }, tblSearchInput, filterSelect),
      sidebarListMount
    );
    renderSidebarList();
    renderOverviewMain();
    return el("div", { className: "split" },
      sidebar,
      el("main", { className: "main-pane" }, overviewSubbarMount, overviewBodyMount)
    );
  }

  function renderQueueSubbar() {
    qSearchInput.value = state.qKeyword;
    clearEl(
      queueSubbarMount,
      el("div", { className: "pills" },
        [
          { k: "ALL", l: "全部 (" + MIG_ITEMS.length + ")" },
          { k: "PENDING", l: "待改造" },
          { k: "PARTIAL", l: "部分合规" },
          { k: "REBUILD", l: "保序重建" },
          { k: "COMPLETED", l: "已达标" },
          { k: "FAILED", l: "异常" },
        ].map((p) => el("button", {
          type: "button", className: "pill " + (state.qFilter === p.k ? "is-active" : ""),
          text: p.l,
          onClick: () => {
            state.qFilter = p.k;
            renderQueueSubbar();
            renderQueueBody();
            syncExportFilteredBtn();
          },
        }))
      ),
      qSearchInput
    );
  }

  function renderQueueBody() {
    const filtered = filteredQueueList();
    clearEl(
      queueBodyMount,
      filtered.length
        ? el("div", { className: "table-wrap" },
            el("table", { className: "grid" },
              el("thead", {}, el("tr", {},
                ["库表", "#", "原字段 → 标准字段", "原类型 → 目标类型", "目标注释", "策略", "状态", "状态说明 / SQL"].map((h) => el("th", { text: h }))
              )),
              el("tbody", {}, filtered.map((it) => {
                const isReb = it.strategy === "TABLE_REBUILD" || Boolean(it.is_distribute_key) || String(it.table_orientation || "").toUpperCase() === "COLUMN";
                const stLabel = MIG_LABELS[it.status] || it.status || "-";
                const stTone = it.status === "COMPLETED" ? "ok" : (it.status === "FAILED" || it.status === "MISSING_TABLE" || it.status === "MISSING_COLUMN" ? "bad" : "warn");
                const sqlOpen = state.expandedSqlIds.has(it.id);
                return el("tr", {},
                  el("td", { className: "mono", text: (it.schema_name || "") + "." + (it.table_name || "") }),
                  el("td", { className: "mono", text: String(it.ordinal_position || "-") }),
                  el("td", { className: "mono", text: (it.old_col_name || "") + " → " + (it.new_col_name || "") }),
                  el("td", { className: "mono", text: (it.old_col_type || "?") + " → " + (it.new_col_type || "?") }),
                  el("td", { text: it.new_col_comment || it.old_col_comment || "-" }),
                  el("td", {}, badge(isReb ? "保序重建" : "原地修改", isReb ? "warn" : "")),
                  el("td", {}, badge(stLabel, stTone)),
                  el("td", {},
                    el("div", { text: it.error_message || it.status_detail || "-" }),
                    it.generated_sql ? el("button", {
                      type: "button", className: "btn btn-xs", style: { marginTop: "4px" },
                      text: sqlOpen ? "收起 SQL" : "查看 SQL",
                      onClick: () => { if (sqlOpen) state.expandedSqlIds.delete(it.id); else state.expandedSqlIds.add(it.id); renderQueueBody(); },
                    }) : null,
                    sqlOpen && it.generated_sql ? el("pre", { className: "sql-pre", text: it.generated_sql }) : null
                  )
                );
              }))
            )
          )
        : el("div", { className: "empty", text: MIG_ITEMS.length ? "当前筛选条件下无贯标改造任务" : "当前数据源暂无贯标改造队列任务" })
    );
  }

  function renderQueueTab() {
    renderQueueSubbar();
    renderQueueBody();
    return el("main", { className: "main-pane" }, queueSubbarMount, queueBodyMount);
  }

  function renderWarningsTab() {
    return el("main", { className: "main-pane", style: { padding: "14px 16px", overflowY: "auto" } },
      WARNINGS.length
        ? WARNINGS.map((w) => el("div", { className: "banner is-warn", style: { margin: "0 0 8px 0" }, text: w }))
        : el("div", { className: "empty", text: "全库采集与核验无异常告警" })
    );
  }

  function renderTabbar() {
    const tabs = [
      { id: "overview", l: "1. 库表全景与落标核验 (" + TABLES.length + ")" },
      { id: "queue", l: "2. 贯标改造执行清单 (" + MIG_ITEMS.length + ")" },
    ];
    if (WARNINGS.length) {
      tabs.push({ id: "warnings", l: "3. 采集告警 (" + WARNINGS.length + ")" });
    }
    clearEl(
      tabbarMount,
      el("div", { className: "tabs" }, tabs.map((t) => el("button", {
        type: "button", className: "tab " + (state.tab === t.id ? "is-active" : ""),
        text: t.l,
        onClick: () => {
          state.tab = t.id;
          renderTabbar();
          renderWorkspace();
          syncExportFilteredBtn();
        },
      })))
    );
  }

  function renderWorkspace() {
    const body = state.tab === "overview"
      ? renderOverviewTab()
      : (state.tab === "queue" ? renderQueueTab() : renderWarningsTab());
    clearEl(workspaceMount, body);
  }

  function init() {
    const root = document.getElementById("app-root");
    renderTabbar();
    renderWorkspace();
    syncExportFilteredBtn();
    clearEl(root, renderTopbar(), renderKpis(), tabbarMount, workspaceMount);
  }

  init();
})();
"""

# =============================================================================
# 4. 跨环境离线交互网页报告 (.html)
# =============================================================================

_CROSS_ENV_APP_JS = r"""
(() => {
  const DATA = JSON.parse(document.getElementById("report-data").textContent);
  const SRC = DATA.source_connection || {};
  const TGT = DATA.target_connection || {};
  const AGG = DATA.aggregate || {};
  const PAIRS = DATA.pairs || [];
  const REC_PAIRS = DATA.recommended_pairings || [];
  const SRC_ONLY = DATA.source_only_tables || [];
  const TGT_ONLY = DATA.target_only_tables || [];
  const WARNINGS = DATA.warnings || [];
  const HEADERS = DATA.excel_headers || [];
  const BK_LABELS = {
    UNIFIED: "标准一致", SPLIT_DIFF: "标准异标", SPLIT_PARTIAL: "单侧绑标",
    SPLIT_UNBOUND: "均未绑标", CANDIDATE: "待配对候选", RECOMMENDED: "推荐同名未配对",
    SOURCE_ONLY: "仅来源库存在", TARGET_ONLY: "仅目标库存在"
  };

  const state = {
    theme: window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    tab: PAIRS.length ? "pairs" : (REC_PAIRS.length ? "recommended" : "pairs"),
    selectedPairKey: "__ALL__",
    pairViewMode: "five", // five | flat (for single pair)
    allPairMode: "summary", // summary | search (for __ALL__)
    pairListFilter: "ALL", // ALL | HAS_DIFF | HAS_MIG | UNIFIED_ONLY
    pairKeyword: "",
    rowFilter: "ALL", // ALL | UNIFIED | SPLIT_DIFF | SPLIT_PARTIAL | MIGRATE | CANDIDATE
    rowKeyword: "",
    page: 1,
    pageSize: 200,
    recFilter: "ALL", // ALL | SAME_SCHEMA | CROSS_SCHEMA | HAS_BOUND
    recKeyword: "",
    soloKeyword: "",
  };

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", state.theme);
  }
  applyTheme();

  function bkTone(bk) {
    if (bk === "UNIFIED") return "ok";
    if (bk === "SPLIT_DIFF") return "bad";
    if (bk === "SPLIT_PARTIAL") return "info";
    if (bk === "RECOMMENDED") return "lav";
    return "";
  }

  function buildPairFlatRows(pair) {
    const tb = pair.table_binding || {};
    const tKey = String(tb.std_table_key || pair.std_table_key || "");
    const sSch = String(tb.source_schema || (pair.source_panel || {}).schema_name || "");
    const sTbl = String(tb.source_table || (pair.source_panel || {}).table_name || "");
    const tSch = String(tb.target_schema || (pair.target_panel || {}).schema_name || "");
    const tTbl = String(tb.target_table || (pair.target_panel || {}).table_name || "");
    const out = [];

    for (const cr of (pair.center_rows || [])) {
      const sc = cr.source_col || {};
      const tc = cr.target_col || {};
      const sStd = cr.source_std || {};
      const tStd = cr.target_std || {};
      const tcComp = cr.target_compliance || {};
      const bk = String(cr.bookmark_state || "SPLIT_PARTIAL");
      const tcStatus = String(tcComp.status || (tc.column_name ? "UNBOUND" : ""));
      const tcLabel = String(tcComp.label || (tc.column_name ? "未绑标" : "-"));
      const diffs = [];
      if (bk === "SPLIT_DIFF") diffs.push("两侧绑标不一致 (" + (cr.source_std_code || "") + " ≠ " + (cr.target_std_code || "") + ")");
      else if (bk === "SPLIT_PARTIAL") diffs.push(!sc.column_name ? "仅目标端已绑标，缺少来源字段配对" : "仅来源端已绑标，缺少目标字段配对");
      if (tcComp.detail && tcStatus !== "COMPLIANT") diffs.push("目标端: " + tcComp.detail);
      if (!diffs.length) diffs.push(tcStatus === "COMPLIANT" ? "双侧标准一致且目标端已落标" : "双侧标准一致");

      let excelTone = "BLUE";
      if (bk === "SPLIT_DIFF") excelTone = "RED";
      else if (bk === "UNIFIED") excelTone = tcStatus === "NON_COMPLIANT" ? "YELLOW" : "GREEN";

      out.push({
        tone: excelTone,
        needsMigration: Boolean(cr.target_needs_migration),
        bookmark_state: bk,
        row_kind: "已保存映射",
        std_table_key: tKey,
        source_schema: sSch, source_table: sTbl,
        source_col_name: sc.column_name || "", source_col_type: sc.data_type || "", source_col_comment: sc.column_comment || "",
        source_std_code: cr.source_std_code || "", source_std_cn: sStd.cn_name || "", source_std_type: sStd.resolved_type || "",
        target_schema: tSch, target_table: tTbl,
        target_col_name: tc.column_name || "", target_col_type: tc.data_type || "", target_col_comment: tc.column_comment || "",
        target_std_code: cr.target_std_code || "", target_std_cn: tStd.cn_name || "", target_std_type: tStd.resolved_type || "",
        bookmark_label: BK_LABELS[bk] || bk,
        target_compliance_status: tcStatus,
        target_compliance_label: tcLabel,
        detail: diffs.join("；"),
      });
    }
    for (const sc of (pair.source_candidates || [])) {
      out.push({
        tone: "GRAY", needsMigration: false, bookmark_state: "CANDIDATE", row_kind: "已保存映射",
        std_table_key: tKey, source_schema: sSch, source_table: sTbl,
        source_col_name: sc.column_name || "", source_col_type: sc.data_type || "", source_col_comment: sc.column_comment || "",
        source_std_code: "", source_std_cn: "", source_std_type: "",
        target_schema: tSch, target_table: tTbl,
        target_col_name: "", target_col_type: "", target_col_comment: "",
        target_std_code: "", target_std_cn: "", target_std_type: "",
        bookmark_label: "待配对候选", target_compliance_status: "", target_compliance_label: "-",
        detail: "来源表未绑标待选字段",
      });
    }
    for (const tc of (pair.target_candidates || [])) {
      out.push({
        tone: "GRAY", needsMigration: false, bookmark_state: "CANDIDATE", row_kind: "已保存映射",
        std_table_key: tKey, source_schema: sSch, source_table: sTbl,
        source_col_name: "", source_col_type: "", source_col_comment: "",
        source_std_code: "", source_std_cn: "", source_std_type: "",
        target_schema: tSch, target_table: tTbl,
        target_col_name: tc.column_name || "", target_col_type: tc.data_type || "", target_col_comment: tc.column_comment || "",
        target_std_code: "", target_std_cn: "", target_std_type: "",
        bookmark_label: "待配对候选", target_compliance_status: "UNBOUND", target_compliance_label: "未绑标",
        detail: "目标表未绑标待选字段",
      });
    }
    return out;
  }

  function buildRecExcelRows(recList) {
    return (recList || REC_PAIRS).map((rec) => {
      const sCnt = Number(rec.source_bound_count || 0);
      const tCnt = Number(rec.target_bound_count || 0);
      return {
        tone: "LAVENDER",
        values: [
          "推荐同名(未保存)",
          "推荐: " + (rec.table_name || ""),
          rec.source_schema || "", rec.source_table || "", "(整表推荐 · 未预比对)",
          rec.source_object_type || "TABLE", rec.source_comment || "",
          sCnt ? "已绑 " + sCnt + " 列" : "", "", "",
          rec.target_schema || "", rec.target_table || "", "(整表推荐 · 未预比对)",
          rec.target_object_type || "TABLE", rec.target_comment || "",
          tCnt ? "已绑 " + tCnt + " 列" : "", "", "",
          "推荐同名未配对", "-",
          (rec.match_type || "同名表") + "（尚未保存跨环境配对，未自动执行字段预比对）",
        ],
      };
    });
  }

  function buildSoloExcelRows(srcList, tgtList) {
    const out = [];
    for (const st of (srcList || SRC_ONLY)) {
      const cnt = Number(st.bound_count || 0);
      out.push({
        tone: "GRAY",
        values: [
          "仅来源库存在", "仅源库: " + (st.table_name || ""),
          st.schema_name || "", st.table_name || "", "(整表仅来源库存在)",
          st.object_type || "TABLE", st.table_comment || "", cnt ? "已绑 " + cnt + " 列" : "", "", "",
          "", "", "", "", "", "", "", "",
          "仅来源库存在", "-", "仅来源库存在该表（目标库无已保存配对或同名表）",
        ],
      });
    }
    for (const tt of (tgtList || TGT_ONLY)) {
      const cnt = Number(tt.bound_count || 0);
      out.push({
        tone: "GRAY",
        values: [
          "仅目标库存在", "仅目标库: " + (tt.table_name || ""),
          "", "", "", "", "", "", "", "",
          tt.schema_name || "", tt.table_name || "", "(整表仅目标库存在)",
          tt.object_type || "TABLE", tt.table_comment || "", cnt ? "已绑 " + cnt + " 列" : "", "", "",
          "仅目标库存在", "-", "仅目标库存在该表（来源库无已保存配对或同名表）",
        ],
      });
    }
    return out;
  }

  function rowToExcelObj(r) {
    return {
      tone: r.tone,
      values: [
        r.row_kind, r.std_table_key,
        r.source_schema, r.source_table, r.source_col_name, r.source_col_type, r.source_col_comment,
        r.source_std_code, r.source_std_cn, r.source_std_type,
        r.target_schema, r.target_table, r.target_col_name, r.target_col_type, r.target_col_comment,
        r.target_std_code, r.target_std_cn, r.target_std_type,
        r.bookmark_label, r.target_compliance_label, r.detail,
      ],
    };
  }

  function matchFlatRow(r) {
    if (state.rowFilter === "MIGRATE") {
      if (!r.needsMigration) return false;
    } else if (state.rowFilter !== "ALL" && r.bookmark_state !== state.rowFilter) {
      return false;
    }
    const kw = state.rowKeyword.trim().toLowerCase();
    if (!kw) return true;
    return [
      r.std_table_key, r.source_table, r.target_table, r.source_col_name, r.target_col_name,
      r.source_std_code, r.target_std_code, r.source_std_cn, r.target_std_cn, r.detail
    ].some((v) => String(v || "").toLowerCase().includes(kw));
  }

  function filteredPairsList() {
    const kw = state.pairKeyword.trim().toLowerCase();
    return PAIRS.filter((p) => {
      const tb = p.table_binding || {};
      const bk = p.bookmark_summary || {};
      if (state.pairListFilter === "HAS_DIFF" && (bk.split_diff || 0) === 0) return false;
      if (state.pairListFilter === "HAS_MIG" && (bk.target_needs_migration || 0) === 0) return false;
      if (state.pairListFilter === "UNIFIED_ONLY" && ((bk.split_diff || 0) > 0 || (bk.unified || 0) === 0)) return false;
      if (!kw) return true;
      return [tb.std_table_key, tb.source_schema, tb.source_table, tb.target_schema, tb.target_table]
        .some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  function filteredRecList() {
    const kw = state.recKeyword.trim().toLowerCase();
    return REC_PAIRS.filter((r) => {
      if (state.recFilter === "SAME_SCHEMA" && r.match_type !== "同模式同名") return false;
      if (state.recFilter === "CROSS_SCHEMA" && r.match_type !== "跨模式同名") return false;
      if (state.recFilter === "HAS_BOUND" && (r.source_bound_count || 0) === 0 && (r.target_bound_count || 0) === 0) return false;
      if (!kw) return true;
      return [r.table_name, r.source_schema, r.source_table, r.target_schema, r.target_table, r.source_comment, r.target_comment]
        .some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  function filteredSoloLists() {
    const kw = state.soloKeyword.trim().toLowerCase();
    const filterList = (list) => list.filter((t) => !kw || [t.schema_name, t.table_name, t.table_comment].some((v) => String(v || "").toLowerCase().includes(kw)));
    return { sList: filterList(SRC_ONLY), tList: filterList(TGT_ONLY) };
  }

  function hasPairsFilter() {
    return (
      state.selectedPairKey !== "__ALL__" ||
      state.pairListFilter !== "ALL" ||
      state.pairKeyword.trim() !== "" ||
      state.rowFilter !== "ALL" ||
      state.rowKeyword.trim() !== ""
    );
  }

  function hasActiveFilter() {
    if (state.tab === "recommended") {
      return state.recFilter !== "ALL" || state.recKeyword.trim() !== "";
    }
    if (state.tab === "solo") {
      return state.soloKeyword.trim() !== "";
    }
    return hasPairsFilter();
  }

  function exportAllExcel() {
    const allRows = [];
    for (const p of PAIRS) {
      for (const r of buildPairFlatRows(p)) allRows.push(rowToExcelObj(r));
    }
    for (const rr of buildRecExcelRows(REC_PAIRS)) allRows.push(rr);
    for (const sr of buildSoloExcelRows(SRC_ONLY, TGT_ONLY)) allRows.push(sr);
    const numbered = allRows.map((o, idx) => ({ tone: o.tone, values: [idx + 1, ...o.values] }));
    const blob = buildSingleSheetXlsxBlob("跨环境库级比对报告", HEADERS, numbered);
    triggerDownload(blob, "DataOG_跨环境库级比对报告.xlsx");
  }

  function exportFilteredExcel() {
    let allRows = [];
    if (state.tab === "recommended") {
      allRows = buildRecExcelRows(filteredRecList());
    } else if (state.tab === "solo") {
      const { sList, tList } = filteredSoloLists();
      allRows = buildSoloExcelRows(sList, tList);
    } else {
      const pairsSubset = state.selectedPairKey === "__ALL__"
        ? filteredPairsList()
        : PAIRS.filter((p) => (p.table_binding || {}).std_table_key === state.selectedPairKey);
      for (const p of pairsSubset) {
        for (const r of buildPairFlatRows(p)) {
          if (matchFlatRow(r)) allRows.push(rowToExcelObj(r));
        }
      }
    }
    const numbered = allRows.map((o, idx) => ({ tone: o.tone, values: [idx + 1, ...o.values] }));
    const blob = buildSingleSheetXlsxBlob("跨环境筛选明细", HEADERS, numbered);
    triggerDownload(blob, "DataOG_跨环境筛选比对明细.xlsx");
  }

  // Persistent controls & mounts
  const themeBtn = el("button", {
    type: "button", className: "btn",
    text: state.theme === "dark" ? "切换浅色" : "切换深色",
    onClick: () => {
      state.theme = state.theme === "dark" ? "light" : "dark";
      applyTheme();
      themeBtn.textContent = state.theme === "dark" ? "切换浅色" : "切换深色";
    },
  });
  const exportFilteredBtn = el("button", {
    type: "button", className: "btn", hidden: !hasActiveFilter(),
    text: "导出当前筛选 (.xlsx)", onClick: exportFilteredExcel,
  });
  function syncExportFilteredBtn() {
    exportFilteredBtn.hidden = !hasActiveFilter();
  }

  const pairSearchInput = el("input", {
    className: "input", type: "search", placeholder: "搜索映射表对...",
    value: state.pairKeyword,
    onInput: (e) => {
      state.pairKeyword = e.target.value;
      state.page = 1;
      renderPairSidebarList();
      if (state.selectedPairKey === "__ALL__") {
        updatePairsSummaryLabel();
        renderPairBody();
      }
      syncExportFilteredBtn();
    },
  });
  const rowSearchInput = el("input", {
    className: "input", type: "search", style: { width: "200px" }, placeholder: "搜索字段名 / 标准编号 / 中文名...",
    value: state.rowKeyword,
    onInput: (e) => {
      state.rowKeyword = e.target.value;
      state.page = 1;
      renderPairBody();
      syncExportFilteredBtn();
    },
  });
  const recSearchInput = el("input", {
    className: "input", type: "search", style: { width: "220px" }, placeholder: "搜索推荐表名 / 模式 / 注释...",
    value: state.recKeyword,
    onInput: (e) => {
      state.recKeyword = e.target.value;
      renderRecBody();
      syncExportFilteredBtn();
    },
  });
  const soloSearchInput = el("input", {
    className: "input", type: "search", style: { width: "220px" }, placeholder: "搜索单侧独有表...",
    value: state.soloKeyword,
    onInput: (e) => {
      state.soloKeyword = e.target.value;
      renderSoloBody();
      syncExportFilteredBtn();
    },
  });

  const pairSidebarListMount = el("div", { className: "sidebar-list" });
  const pairSubbarMount = el("div", { className: "subbar" });
  const pairBodyMount = el("div", { style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" } });
  const recSubbarMount = el("div", { className: "subbar" });
  const recBodyMount = el("div", { style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" } });
  const soloBodyMount = el("div", { className: "dual-lists" });
  const tabbarMount = el("nav", { className: "tabbar" });
  const workspaceMount = el("div", { style: { flex: "1", minHeight: "0", display: "flex", overflow: "hidden" } });
  let pairsSummaryPillBtn = null;

  function updatePairsSummaryLabel() {
    if (pairsSummaryPillBtn) {
      pairsSummaryPillBtn.textContent = "映射表对汇总 (" + filteredPairsList().length + " 对)";
    }
  }

  function renderTopbar() {
    const srcLbl = (SRC.name || "未配置来源库") + (SRC.db_type ? " (" + SRC.db_type + " · " + (DATA.source_format_profile_label || DATA.source_format_profile || "AUTO") + ")" : "");
    const tgtLbl = (TGT.name || "未配置目标库") + (TGT.db_type ? " (" + TGT.db_type + " · " + (DATA.target_format_profile_label || DATA.target_format_profile || "AUTO") + ")" : "");
    return el("header", { className: "topbar" },
      el("div", { className: "topbar-left" },
        el("span", { className: "brand-mark", text: "OG" }),
        el("h1", { className: "title-main", text: "跨环境库级比对与落标离线报告" }),
        badge("来源: " + srcLbl, "info"),
        el("span", { style: { color: "var(--muted)" }, text: "↔" }),
        badge("目标: " + tgtLbl, "ok"),
        badge("生成: " + (DATA.generated_at || ""))
      ),
      el("div", { className: "topbar-actions" },
        themeBtn,
        exportFilteredBtn,
        el("button", { type: "button", className: "btn btn-primary", text: "导出全库 Excel (.xlsx)", onClick: exportAllExcel })
      )
    );
  }

  function renderKpis() {
    const cards = [
      { label: "已保存映射表对", val: String(AGG.saved_pairs_count ?? PAIRS.length), sub: "来源总表 " + (AGG.source_total_tables ?? 0) + " · 目标总表 " + (AGG.target_total_tables ?? 0) },
      { label: "推荐同名未配对表", val: String(AGG.recommended_pairs_count ?? REC_PAIRS.length), sub: "仅推荐清单（未自动预比对）" },
      { label: "标准一致组 (UNIFIED)", val: String(AGG.unified_cols ?? 0), sub: "共已映射 " + (AGG.total_center_rows ?? 0) + " 组" },
      { label: "标准异标 (SPLIT_DIFF)", val: String(AGG.split_diff_cols ?? 0), sub: "单侧绑标 " + (AGG.split_partial_cols ?? 0) + " 组" },
      { label: "目标库待贯标", val: String(AGG.target_needs_migration_cols ?? 0), sub: "未配对候选 源" + (AGG.unbound_src_cols ?? 0) + " / 目标" + (AGG.unbound_tgt_cols ?? 0) },
    ];
    return el("section", { className: "kpi-strip" },
      cards.map((c) => el("div", { className: "kpi-card" },
        el("div", { className: "kpi-label", text: c.label }),
        el("div", { className: "kpi-val", text: c.val }),
        el("div", { className: "kpi-sub", text: c.sub })
      ))
    );
  }

  function renderFlatTable(rows, showPairCols) {
    if (!rows.length) return el("div", { className: "empty", text: "当前筛选条件下暂无字段记录" });
    const startIdx = (state.page - 1) * state.pageSize;
    const endIdx = Math.min(rows.length, startIdx + state.pageSize);
    const slice = rows.slice(startIdx, endIdx);
    const ths = [];
    if (showPairCols) ths.push("表对 (来源 ↔ 目标)");
    ths.push("来源字段", "来源类型", "来源标准", "中缝对标状态", "目标标准", "目标字段", "目标类型", "目标落标", "差异说明");

    const trs = slice.map((r) => {
      const cells = [];
      if (showPairCols) cells.push(el("td", { className: "mono", text: (r.source_table || "") + " ↔ " + (r.target_table || "") }));
      cells.push(
        el("td", { className: "mono", style: { fontWeight: "600" }, text: r.source_col_name || "-" }),
        el("td", { className: "mono", text: r.source_col_type || "-" }),
        el("td", { text: r.source_std_code ? (r.source_std_code + " · " + r.source_std_cn) : "-" }),
        el("td", {}, badge(r.bookmark_label, bkTone(r.bookmark_state))),
        el("td", { text: r.target_std_code ? (r.target_std_code + " · " + r.target_std_cn) : "-" }),
        el("td", { className: "mono", style: { fontWeight: "600" }, text: r.target_col_name || "-" }),
        el("td", { className: "mono", text: r.target_col_type || "-" }),
        el("td", {}, r.target_compliance_status
          ? badge(r.target_compliance_label, r.target_compliance_status === "COMPLIANT" ? "ok" : (r.target_compliance_status === "NON_COMPLIANT" ? "bad" : (r.target_compliance_status === "COMMENT_ONLY" ? "info" : "")))
          : el("span", { style: { color: "var(--muted)" }, text: "-" })),
        el("td", { style: { maxWidth: "260px" }, text: r.detail || "-" })
      );
      return el("tr", {}, cells);
    });

    const totalPages = Math.max(1, Math.ceil(rows.length / state.pageSize));
    const pager = rows.length > state.pageSize ? el("div", { className: "pager" },
      el("span", { style: { color: "var(--muted)", fontSize: "11.5px" }, text: "显示第 " + (startIdx + 1) + "-" + endIdx + " 条，共 " + rows.length + " 条" }),
      el("div", { className: "subbar-group" },
        el("button", { type: "button", className: "btn btn-xs", disabled: state.page <= 1, text: "上一页", onClick: () => { state.page--; renderPairBody(); } }),
        el("span", { className: "mono", text: state.page + " / " + totalPages }),
        el("button", { type: "button", className: "btn btn-xs", disabled: state.page >= totalPages, text: "下一页", onClick: () => { state.page++; renderPairBody(); } })
      )
    ) : null;

    return el("div", { style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column" } },
      el("div", { className: "table-wrap" },
        el("table", { className: "grid" },
          el("thead", {}, el("tr", {}, ths.map((h) => el("th", { text: h })))),
          el("tbody", {}, trs)
        )
      ),
      pager
    );
  }

  function renderFiveColumnPair(pair) {
    const srcCands = pair.source_candidates || [];
    const tgtCands = pair.target_candidates || [];
    const cRows = (pair.center_rows || []).filter((r) => {
      if (state.rowFilter === "MIGRATE" && !r.target_needs_migration) return false;
      if (["UNIFIED", "SPLIT_DIFF", "SPLIT_PARTIAL"].includes(state.rowFilter) && r.bookmark_state !== state.rowFilter) return false;
      const kw = state.rowKeyword.trim().toLowerCase();
      if (!kw) return true;
      return [
        (r.source_col || {}).column_name, (r.target_col || {}).column_name,
        r.source_std_code, r.target_std_code,
        (r.source_std || {}).cn_name, (r.target_std || {}).cn_name
      ].some((v) => String(v || "").toLowerCase().includes(kw));
    });

    const renderCand = (c) => el("div", { className: "cand-card" },
      el("div", { className: "mono", style: { fontWeight: "600", fontSize: "12px" }, text: c.column_name || "" }),
      el("div", { style: { display: "flex", justifyContent: "space-between", gap: "6px", fontSize: "11px", color: "var(--muted)" } },
        el("span", { className: "mono", text: c.data_type || "-" }),
        c.column_comment ? el("span", { text: c.column_comment }) : null
      )
    );

    const renderBoundChip = (col, isTgt, row) => {
      if (!col) return el("div", { className: "slot-empty", text: "空槽（未配对）" });
      return el("div", { className: "cand-card", style: { borderColor: row.bookmark_state === "UNIFIED" ? "var(--ok-bd)" : "" } },
        el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "4px" } },
          el("span", { className: "mono", style: { fontWeight: "600" }, text: col.column_name || "" }),
          isTgt && row.target_needs_migration
            ? badge("待贯标", "warn")
            : (isTgt && (row.target_compliance || {}).status === "COMPLIANT" ? badge("已落标", "ok") : null)
        ),
        el("div", { style: { display: "flex", justifyContent: "space-between", gap: "6px", fontSize: "11px", color: "var(--muted)" } },
          el("span", { className: "mono", text: col.data_type || "-" }),
          col.column_comment ? el("span", { text: col.column_comment }) : null
        )
      );
    };

    const renderStdCard = (row) => {
      const bk = row.bookmark_state;
      if (bk === "SPLIT_DIFF") {
        const sS = row.source_std || {};
        const tS = row.target_std || {};
        return el("div", { className: "std-card is-diff" },
          el("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" } },
            el("div", {}, badge(row.source_std_code || "-", "bad"), el("div", { style: { fontSize: "11px" }, text: sS.cn_name || "" })),
            el("strong", { style: { color: "var(--bad)" }, text: "≠" }),
            el("div", {}, badge(row.target_std_code || "-", "bad"), el("div", { style: { fontSize: "11px" }, text: tS.cn_name || "" }))
          )
        );
      }
      const rep = row.source_std || row.target_std || {};
      const stCls = bk === "UNIFIED" ? "is-unified" : "is-partial";
      const sT = (row.source_std || {}).resolved_type || "";
      const tT = (row.target_std || {}).resolved_type || "";
      const tLabel = sT && tT && sT !== tT ? sT + " / " + tT : (tT || sT);
      return el("div", { className: "std-card " + stCls },
        el("div", { style: { display: "flex", alignItems: "center", gap: "6px" } },
          badge(rep.std_code || "-", bk === "UNIFIED" ? "ok" : "info"),
          el("strong", { style: { fontSize: "12px" }, text: rep.cn_name || "未知标准" })
        ),
        el("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "11px", color: "var(--text-2)" } },
          el("span", { className: "mono", text: rep.en_abbr || "" }),
          tLabel ? el("span", { className: "mono", text: tLabel }) : null
        )
      );
    };

    return el("div", { className: "five-wrap" },
      el("div", { className: "five-grid" },
        el("div", { className: "five-col-hdr" }, el("span", { text: "来源表待选字段" }), badge(String(srcCands.length))),
        el("div", { className: "five-col-hdr" }, el("span", { text: "来源表已绑定数标字段" }), badge(String(cRows.filter((r) => Boolean(r.source_col)).length), "info")),
        el("div", { className: "five-col-hdr" }, el("span", { text: "数据标准" }), badge(String(cRows.length), "ok")),
        el("div", { className: "five-col-hdr" }, el("span", { text: "目标表已绑定数标字段" }), badge(String(cRows.filter((r) => Boolean(r.target_col)).length), "info")),
        el("div", { className: "five-col-hdr" }, el("span", { text: "目标表待选字段" }), badge(String(tgtCands.length)))
      ),
      el("div", { className: "five-body" },
        el("div", { className: "cand-list" }, srcCands.length ? srcCands.map(renderCand) : el("div", { className: "empty", style: { padding: "14px" }, text: "无待选字段" })),
        el("div", { className: "matrix-list" }, cRows.length ? cRows.map((r) => el("div", { className: "matrix-row" },
          renderBoundChip(r.source_col, false, r),
          renderStdCard(r),
          renderBoundChip(r.target_col, true, r)
        )) : el("div", { className: "empty", text: "当前筛选条件下无已绑标字段行" })),
        el("div", { className: "cand-list" }, tgtCands.length ? tgtCands.map(renderCand) : el("div", { className: "empty", style: { padding: "14px" }, text: "无待选字段" }))
      )
    );
  }

  function renderPairSidebarList() {
    const pList = filteredPairsList();
    const sidebarNodes = [
      el("button", {
        type: "button",
        className: "nav-item " + (state.selectedPairKey === "__ALL__" ? "is-active" : ""),
        onClick: () => {
          state.selectedPairKey = "__ALL__";
          state.page = 1;
          renderPairSidebarList();
          renderPairMain();
          syncExportFilteredBtn();
        },
      },
        el("span", { className: "nav-title", text: "全部映射表对总览 (" + pList.length + ")" }),
        badge(String(PAIRS.length), "info")
      ),
    ];
    for (const p of pList) {
      const tb = p.table_binding || {};
      const bk = p.bookmark_summary || {};
      const tKey = tb.std_table_key || "";
      const tone = (bk.split_diff || 0) > 0 ? "bad" : ((bk.target_needs_migration || 0) > 0 ? "warn" : "ok");
      sidebarNodes.push(el("button", {
        type: "button",
        className: "nav-item " + (state.selectedPairKey === tKey ? "is-active" : ""),
        onClick: () => {
          state.selectedPairKey = tKey;
          state.page = 1;
          if (state.pairViewMode === "five" && state.rowFilter === "CANDIDATE") {
            state.rowFilter = "ALL";
          }
          renderPairSidebarList();
          renderPairMain();
          syncExportFilteredBtn();
        },
      },
        el("span", { className: "nav-title", text: (tb.source_table || tKey) + " ↔ " + (tb.target_table || tKey) }),
        badge((bk.unified || 0) + "/" + (bk.total_rows || 0), tone)
      ));
    }
    clearEl(pairSidebarListMount, sidebarNodes);
  }

  function renderPairSubbar() {
    const pList = filteredPairsList();
    const activePair = state.selectedPairKey === "__ALL__"
      ? null
      : PAIRS.find((p) => (p.table_binding || {}).std_table_key === state.selectedPairKey) || null;
    rowSearchInput.value = state.rowKeyword;
    pairsSummaryPillBtn = null;

    if (!activePair) {
      pairsSummaryPillBtn = el("button", {
        type: "button", className: "pill " + (state.allPairMode === "summary" ? "is-active" : ""),
        text: "映射表对汇总 (" + pList.length + " 对)",
        onClick: () => {
          state.allPairMode = "summary";
          state.page = 1;
          renderPairMain();
          syncExportFilteredBtn();
        },
      });
      const searchPillBtn = el("button", {
        type: "button", className: "pill " + (state.allPairMode === "search" ? "is-active" : ""),
        text: "全库字段对标检索",
        onClick: () => {
          state.allPairMode = "search";
          state.page = 1;
          renderPairMain();
          syncExportFilteredBtn();
        },
      });
      clearEl(
        pairSubbarMount,
        el("div", { className: "subbar-group" },
          el("div", { className: "pills" }, pairsSummaryPillBtn, searchPillBtn),
          state.allPairMode === "search"
            ? el("div", { className: "pills" },
                [
                  { k: "ALL", l: "全部" }, { k: "UNIFIED", l: "标准一致" },
                  { k: "SPLIT_DIFF", l: "标准异标" }, { k: "SPLIT_PARTIAL", l: "单侧绑标" },
                  { k: "MIGRATE", l: "待贯标" }, { k: "CANDIDATE", l: "待配对候选" },
                ].map((fp) => el("button", {
                  type: "button", className: "pill " + (state.rowFilter === fp.k ? "is-active" : ""),
                  text: fp.l,
                  onClick: () => {
                    state.rowFilter = fp.k;
                    state.page = 1;
                    renderPairSubbar();
                    renderPairBody();
                    syncExportFilteredBtn();
                  },
                }))
              )
            : null
        ),
        state.allPairMode === "search" ? rowSearchInput : null
      );
      return;
    }

    if (state.pairViewMode === "five" && state.rowFilter === "CANDIDATE") {
      state.rowFilter = "ALL";
    }
    const tb = activePair.table_binding || {};
    const bk = activePair.bookmark_summary || {};
    clearEl(
      pairSubbarMount,
      el("div", { className: "subbar-group" },
        el("span", { className: "mono", style: { fontWeight: "700" }, text: (tb.source_schema || "") + "." + (tb.source_table || "") + " ↔ " + (tb.target_schema || "") + "." + (tb.target_table || "") }),
        badge("一致 " + (bk.unified || 0), "ok"),
        badge("异标 " + (bk.split_diff || 0), (bk.split_diff || 0) > 0 ? "bad" : ""),
        badge("单侧 " + (bk.split_partial || 0), (bk.split_partial || 0) > 0 ? "info" : ""),
        el("div", { className: "pills" },
          [
            { m: "five", l: "五栏对标视图" },
            { m: "flat", l: "扁平明细列表" },
          ].map((vm) => el("button", {
            type: "button", className: "pill " + (state.pairViewMode === vm.m ? "is-active" : ""),
            text: vm.l,
            onClick: () => {
              state.pairViewMode = vm.m;
              if (state.pairViewMode === "five" && state.rowFilter === "CANDIDATE") {
                state.rowFilter = "ALL";
              }
              state.page = 1;
              renderPairMain();
              syncExportFilteredBtn();
            },
          }))
        )
      ),
      el("div", { className: "subbar-group" },
        el("div", { className: "pills" },
          [
            { k: "ALL", l: "全部" }, { k: "UNIFIED", l: "一致" }, { k: "SPLIT_DIFF", l: "异标" },
            { k: "SPLIT_PARTIAL", l: "单侧" }, { k: "MIGRATE", l: "待贯标" },
            ...(state.pairViewMode === "flat" ? [{ k: "CANDIDATE", l: "待配对" }] : []),
          ].map((fp) => el("button", {
            type: "button", className: "pill " + (state.rowFilter === fp.k ? "is-active" : ""),
            text: fp.l,
            onClick: () => {
              state.rowFilter = fp.k;
              state.page = 1;
              renderPairSubbar();
              renderPairBody();
              syncExportFilteredBtn();
            },
          }))
        ),
        rowSearchInput
      )
    );
  }

  function renderPairBody() {
    const pList = filteredPairsList();
    const activePair = state.selectedPairKey === "__ALL__"
      ? null
      : PAIRS.find((p) => (p.table_binding || {}).std_table_key === state.selectedPairKey) || null;

    if (!activePair) {
      if (state.allPairMode === "summary") {
        clearEl(
          pairBodyMount,
          pList.length
            ? el("div", { className: "table-wrap" },
                el("table", { className: "grid" },
                  el("thead", {}, el("tr", {},
                    ["映射表键", "来源表", "目标表", "已映射组", "标准一致", "标准异标", "单侧绑标", "目标待贯标", "源待选", "目标待选"].map((h) => el("th", { text: h }))
                  )),
                  el("tbody", {}, pList.map((p) => {
                    const tb = p.table_binding || {};
                    const bk = p.bookmark_summary || {};
                    const k = tb.std_table_key || "";
                    return el("tr", {
                      className: "is-clickable",
                      onClick: () => {
                        state.selectedPairKey = k;
                        state.page = 1;
                        renderPairSidebarList();
                        renderPairMain();
                        syncExportFilteredBtn();
                      },
                    },
                      el("td", { className: "mono", style: { fontWeight: "600", color: "var(--accent)" }, text: k }),
                      el("td", { className: "mono", text: (tb.source_schema || "") + "." + (tb.source_table || "") }),
                      el("td", { className: "mono", text: (tb.target_schema || "") + "." + (tb.target_table || "") }),
                      el("td", { className: "mono", text: String(bk.total_rows || 0) }),
                      el("td", {}, badge(String(bk.unified || 0), (bk.unified || 0) > 0 ? "ok" : "")),
                      el("td", {}, badge(String(bk.split_diff || 0), (bk.split_diff || 0) > 0 ? "bad" : "")),
                      el("td", {}, badge(String(bk.split_partial || 0), (bk.split_partial || 0) > 0 ? "info" : "")),
                      el("td", {}, badge(String(bk.target_needs_migration || 0), (bk.target_needs_migration || 0) > 0 ? "warn" : "")),
                      el("td", { className: "mono", text: String((p.source_candidates || []).length) }),
                      el("td", { className: "mono", text: String((p.target_candidates || []).length) })
                    );
                  }))
                )
              )
            : el("div", { className: "empty", text: "暂无已保存的跨环境映射表对，可查看「2. 推荐同名未配对表清单」" })
        );
        return;
      }
      const rows = [];
      for (const p of pList) {
        for (const r of buildPairFlatRows(p)) {
          if (matchFlatRow(r)) rows.push(r);
        }
      }
      clearEl(pairBodyMount, renderFlatTable(rows, true));
      return;
    }

    if (state.pairViewMode === "five" && state.rowFilter === "CANDIDATE") {
      state.rowFilter = "ALL";
    }
    clearEl(
      pairBodyMount,
      state.pairViewMode === "five"
        ? renderFiveColumnPair(activePair)
        : renderFlatTable(buildPairFlatRows(activePair).filter(matchFlatRow), false)
    );
  }

  function renderPairMain() {
    renderPairSubbar();
    renderPairBody();
  }

  function renderPairsTab() {
    pairSearchInput.value = state.pairKeyword;
    const filterSelect = el("select", {
      className: "input", "aria-label": "按对标情况筛表",
      onChange: (e) => {
        state.pairListFilter = e.target.value;
        state.page = 1;
        renderPairSidebarList();
        if (state.selectedPairKey === "__ALL__") {
          updatePairsSummaryLabel();
          renderPairBody();
        }
        syncExportFilteredBtn();
      },
    },
      [
        { v: "ALL", t: "全部已保存表对 (" + PAIRS.length + ")" },
        { v: "HAS_DIFF", t: "含标准异标" },
        { v: "HAS_MIG", t: "目标库待贯标" },
        { v: "UNIFIED_ONLY", t: "完全一致" },
      ].map((o) => {
        const opt = el("option", { value: o.v, text: o.t });
        if (state.pairListFilter === o.v) opt.selected = true;
        return opt;
      })
    );
    const sidebar = el("aside", { className: "sidebar" },
      el("div", { className: "sidebar-head" }, pairSearchInput, filterSelect),
      pairSidebarListMount
    );
    renderPairSidebarList();
    renderPairMain();
    return el("div", { className: "split" },
      sidebar,
      el("main", { className: "main-pane" }, pairSubbarMount, pairBodyMount)
    );
  }

  function renderRecSubbar() {
    recSearchInput.value = state.recKeyword;
    clearEl(
      recSubbarMount,
      el("div", { className: "pills" },
        [
          { k: "ALL", l: "全部推荐 (" + REC_PAIRS.length + ")" },
          { k: "SAME_SCHEMA", l: "同模式同名" },
          { k: "CROSS_SCHEMA", l: "跨模式同名" },
          { k: "HAS_BOUND", l: "已有数标绑定" },
        ].map((fp) => el("button", {
          type: "button", className: "pill " + (state.recFilter === fp.k ? "is-active" : ""),
          text: fp.l,
          onClick: () => {
            state.recFilter = fp.k;
            renderRecSubbar();
            renderRecBody();
            syncExportFilteredBtn();
          },
        }))
      ),
      recSearchInput
    );
  }

  function renderRecBody() {
    const filtered = filteredRecList();
    clearEl(
      recBodyMount,
      filtered.length
        ? el("div", { className: "table-wrap" },
            el("table", { className: "grid" },
              el("thead", {}, el("tr", {},
                ["#", "来源模式", "来源表名", "来源类型", "来源注释", "来源已绑标", "目标模式", "目标表名", "目标类型", "目标注释", "目标已绑标", "匹配类型"].map((h) => el("th", { text: h }))
              )),
              el("tbody", {}, filtered.map((r, i) => el("tr", {},
                el("td", { className: "mono", text: String(i + 1) }),
                el("td", { className: "mono", text: r.source_schema || "" }),
                el("td", { className: "mono", style: { fontWeight: "600" }, text: r.source_table || "" }),
                el("td", {}, badge(r.source_object_type || "TABLE")),
                el("td", { text: r.source_comment || "-" }),
                el("td", {}, r.source_bound_count ? badge(r.source_bound_count + " 列", "info") : el("span", { style: { color: "var(--muted)" }, text: "0" })),
                el("td", { className: "mono", text: r.target_schema || "" }),
                el("td", { className: "mono", style: { fontWeight: "600" }, text: r.target_table || "" }),
                el("td", {}, badge(r.target_object_type || "TABLE")),
                el("td", { text: r.target_comment || "-" }),
                el("td", {}, r.target_bound_count ? badge(r.target_bound_count + " 列", "info") : el("span", { style: { color: "var(--muted)" }, text: "0" })),
                el("td", {}, badge(r.match_type || "同名", r.match_type === "同模式同名" ? "ok" : "lav"))
              )))
            )
          )
        : el("div", { className: "empty", text: "暂无符合条件的推荐同名未配对表" })
    );
  }

  function renderRecommendedTab() {
    renderRecSubbar();
    renderRecBody();
    return el("main", { className: "main-pane" },
      el("div", { className: "banner" },
        el("span", { text: "以下库表在来源库与目标库中表名相同，但尚未在系统「跨环境比对」中保存配对关系。仅作推荐配对清单展示，未自动执行字段级预比对。" }),
        badge("共 " + REC_PAIRS.length + " 组推荐", "lav")
      ),
      recSubbarMount,
      recBodyMount
    );
  }

  function renderSoloBody() {
    const { sList, tList } = filteredSoloLists();
    const makePane = (title, tone, list) => el("div", { className: "dual-pane" },
      el("div", { className: "subbar" }, el("strong", { text: title }), badge(String(list.length), tone)),
      list.length
        ? el("div", { className: "table-wrap" },
            el("table", { className: "grid" },
              el("thead", {}, el("tr", {}, ["模式", "表名", "类型", "注释", "已绑标"].map((h) => el("th", { text: h })))),
              el("tbody", {}, list.map((t) => el("tr", {},
                el("td", { className: "mono", text: t.schema_name || "" }),
                el("td", { className: "mono", style: { fontWeight: "600" }, text: t.table_name || "" }),
                el("td", {}, badge(t.object_type || "TABLE")),
                el("td", { text: t.table_comment || "-" }),
                el("td", { className: "mono", text: String(t.bound_count || 0) })
              )))
            )
          )
        : el("div", { className: "empty", text: "无单侧独有表" })
    );
    clearEl(
      soloBodyMount,
      makePane("仅来源库存在 (" + sList.length + ")", "info", sList),
      makePane("仅目标库存在 (" + tList.length + ")", "ok", tList)
    );
  }

  function renderSoloTablesTab() {
    soloSearchInput.value = state.soloKeyword;
    renderSoloBody();
    return el("main", { className: "main-pane" },
      el("div", { className: "subbar" },
        el("span", { style: { color: "var(--text-2)" }, text: "展示未配对且在对侧无同名表的库表清单（便于核查新建或废弃表）" }),
        soloSearchInput
      ),
      soloBodyMount
    );
  }

  function renderWarningsTab() {
    return el("main", { className: "main-pane", style: { padding: "14px 16px", overflowY: "auto" } },
      WARNINGS.map((w) => el("div", { className: "banner is-warn", style: { margin: "0 0 8px 0" }, text: w }))
    );
  }

  function renderTabbar() {
    const tabs = [
      { id: "pairs", l: "1. 已配对表对比工作台 (" + PAIRS.length + ")" },
      { id: "recommended", l: "2. 推荐同名未配对表清单 (" + REC_PAIRS.length + ")" },
      { id: "solo", l: "3. 单侧独有表 (源 " + SRC_ONLY.length + " / 目标 " + TGT_ONLY.length + ")" },
    ];
    if (WARNINGS.length) tabs.push({ id: "warnings", l: "4. 采集告警 (" + WARNINGS.length + ")" });

    clearEl(
      tabbarMount,
      el("div", { className: "tabs" }, tabs.map((t) => el("button", {
        type: "button", className: "tab " + (state.tab === t.id ? "is-active" : ""),
        text: t.l,
        onClick: () => {
          state.tab = t.id;
          renderTabbar();
          renderWorkspace();
          syncExportFilteredBtn();
        },
      })))
    );
  }

  function renderWorkspace() {
    const body = state.tab === "pairs"
      ? renderPairsTab()
      : state.tab === "recommended"
      ? renderRecommendedTab()
      : (state.tab === "solo" ? renderSoloTablesTab() : renderWarningsTab());
    clearEl(workspaceMount, body);
  }

  function init() {
    const root = document.getElementById("app-root");
    renderTabbar();
    renderWorkspace();
    syncExportFilteredBtn();
    clearEl(root, renderTopbar(), renderKpis(), tabbarMount, workspaceMount);
  }

  init();
})();
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


def build_single_db_html_bytes(report_data: Dict[str, Any]) -> bytes:
    """生成单库库级落标与贯标离线交互 HTML 报告（内嵌单 Sheet .xlsx 导出能力）."""
    conn_name = str((report_data.get("connection") or {}).get("name") or "数据源")
    payload = {
        **report_data,
        "generated_at": report_data.get("generated_at") or _now_str(),
        "excel_headers": list(_SINGLE_DB_HEADERS),
    }
    return _wrap_offline_html(
        f"Data.O.G · {conn_name} 库级落标与贯标报告",
        payload,
        _SINGLE_DB_APP_JS,
    )


def build_cross_env_html_bytes(report_data: Dict[str, Any]) -> bytes:
    """生成跨环境库级比对离线交互 HTML 报告（含五栏视图、推荐同名清单与单 Sheet .xlsx 导出能力）."""
    payload = {
        **report_data,
        "generated_at": report_data.get("generated_at") or _now_str(),
        "excel_headers": list(_CROSS_ENV_HEADERS),
    }
    return _wrap_offline_html(
        "Data.O.G · 跨环境库级比对与落标报告",
        payload,
        _CROSS_ENV_APP_JS,
    )
