"""跨环境库级比对离线 HTML / 单 Sheet Excel 报告生成器."""

from __future__ import annotations

import io
from typing import Any, Dict, List

import openpyxl

from core.platform.offline_report.runtime import (
    _apply_single_sheet_style,
    _now_str,
    _safe_excel_cell,
    _safe_sheet_title,
    wrap_offline_html,
)

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
    "目标长度/精度包络",
    "是否涉及类型转换",
    "差异与诊断详情",
]

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
            cc = cr.get("cross_compat") or {}
            bk_state = str(cr.get("bookmark_state") or "SPLIT_PARTIAL")
            t_comp_status = str(t_comp.get("status") or ("UNBOUND" if tc else ""))
            t_comp_label = str(t_comp.get("label") or ("未绑标" if tc else "-"))
            cap_status = str(cc.get("capacity_status") or "N/A")
            cap_label = str(cc.get("capacity_label") or "-")
            conv_status = str(cc.get("type_conv_status") or "N/A")
            conv_label = str(cc.get("type_conv_label") or "-")
            has_type_conv = bool(cc.get("has_type_conversion"))

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
            if cap_status in ("INSUFFICIENT", "RISK"):
                diffs.append(f"长度/精度: {cc.get('capacity_detail') or cap_label}")
            if has_type_conv:
                diffs.append(f"类型转换: {cc.get('type_conv_detail') or conv_label}")
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
                    "capacity_status": cap_status,
                    "capacity_label": cap_label,
                    "type_conv_status": conv_status,
                    "type_conv_label": conv_label,
                    "has_type_conversion": has_type_conv,
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
                    "capacity_status": "N/A",
                    "capacity_label": "-",
                    "type_conv_status": "N/A",
                    "type_conv_label": "-",
                    "has_type_conversion": False,
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
                    "capacity_status": "N/A",
                    "capacity_label": "-",
                    "type_conv_status": "N/A",
                    "type_conv_label": "-",
                    "has_type_conversion": False,
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
                "capacity_status": "N/A",
                "capacity_label": "-",
                "type_conv_status": "N/A",
                "type_conv_label": "-",
                "has_type_conversion": False,
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
                "capacity_status": "N/A",
                "capacity_label": "-",
                "type_conv_status": "N/A",
                "type_conv_label": "-",
                "has_type_conversion": False,
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
                "capacity_status": "N/A",
                "capacity_label": "-",
                "type_conv_status": "N/A",
                "type_conv_label": "-",
                "has_type_conversion": False,
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
                _safe_excel_cell(r["capacity_label"]),
                _safe_excel_cell(r["type_conv_label"]),
                _safe_excel_cell(r["detail"]),
            ]
        )
        bk = r["bookmark_state"]
        t_comp = r["target_compliance_status"]
        cap_st = r["capacity_status"]
        if bk == "RECOMMENDED":
            row_tags.append("LAVENDER")
        elif bk == "SPLIT_DIFF":
            row_tags.append("RED")
        elif bk == "UNIFIED":
            if t_comp == "NON_COMPLIANT" or cap_st == "INSUFFICIENT":
                row_tags.append("YELLOW")
            else:
                row_tags.append("GREEN")
        elif bk == "SPLIT_PARTIAL":
            row_tags.append("BLUE")
        else:
            row_tags.append("GRAY")

    widths = [7, 15, 20, 13, 22, 20, 16, 22, 15, 20, 16, 13, 22, 20, 16, 22, 15, 20, 16, 14, 13, 24, 22, 42]
    _apply_single_sheet_style(ws, _CROSS_ENV_HEADERS, widths, row_tags)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =============================================================================
# 4. 跨环境离线交互网页报告 (.html)
# =============================================================================

_CROSS_ENV_APP_JS = r"""
(() => {
  const DATA = JSON.parse(document.getElementById("report-data").textContent);
  const SRC = DATA.source_connection || {};
  const TGT = DATA.target_connection || {};
  const SCOPE = DATA.report_scope || {};
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
    tab: PAIRS.length ? "pairs" : (REC_PAIRS.length ? "recommended" : ((SRC_ONLY.length || TGT_ONLY.length) ? "solo" : "pairs")),
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

  function capTone(st) {
    if (st === "CONTAINED") return "ok";
    if (st === "INSUFFICIENT") return "bad";
    if (st === "RISK") return "warn";
    return "";
  }

  function convTone(st) {
    if (st === "CAST_REQUIRED") return "warn";
    if (st === "SAFE_CAST") return "info";
    if (st === "NONE") return "ok";
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
      const cc = cr.cross_compat || {};
      const bk = String(cr.bookmark_state || "SPLIT_PARTIAL");
      const tcStatus = String(tcComp.status || (tc.column_name ? "UNBOUND" : ""));
      const tcLabel = String(tcComp.label || (tc.column_name ? "未绑标" : "-"));
      const capStatus = String(cc.capacity_status || "N/A");
      const capLabel = String(cc.capacity_label || "-");
      const capShort = String(cc.capacity_short || capLabel);
      const capDetail = String(cc.capacity_detail || "");
      const convStatus = String(cc.type_conv_status || "N/A");
      const convLabel = String(cc.type_conv_label || "-");
      const convShort = String(cc.type_conv_short || convLabel);
      const convDetail = String(cc.type_conv_detail || "");
      const hasTypeConv = Boolean(cc.has_type_conversion);

      const diffs = [];
      if (bk === "SPLIT_DIFF") diffs.push("两侧绑标不一致 (" + (cr.source_std_code || "") + " ≠ " + (cr.target_std_code || "") + ")");
      else if (bk === "SPLIT_PARTIAL") diffs.push(!sc.column_name ? "仅目标端已绑标，缺少来源字段配对" : "仅来源端已绑标，缺少目标字段配对");
      if (tcComp.detail && tcStatus !== "COMPLIANT") diffs.push("目标端: " + tcComp.detail);
      if (capStatus === "INSUFFICIENT" || capStatus === "RISK") diffs.push("长度/精度: " + (capDetail || capLabel));
      if (hasTypeConv) diffs.push("类型转换: " + (convDetail || convLabel));
      if (!diffs.length) diffs.push(tcStatus === "COMPLIANT" ? "双侧标准一致且目标端已落标" : "双侧标准一致");

      let excelTone = "BLUE";
      if (bk === "SPLIT_DIFF") excelTone = "RED";
      else if (bk === "UNIFIED") excelTone = (tcStatus === "NON_COMPLIANT" || capStatus === "INSUFFICIENT") ? "YELLOW" : "GREEN";

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
        capacity_status: capStatus,
        capacity_label: capLabel,
        capacity_short: capShort,
        capacity_detail: capDetail,
        type_conv_status: convStatus,
        type_conv_label: convLabel,
        type_conv_short: convShort,
        type_conv_detail: convDetail,
        has_type_conversion: hasTypeConv,
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
        capacity_status: "N/A", capacity_label: "-", capacity_short: "-", capacity_detail: "",
        type_conv_status: "N/A", type_conv_label: "-", type_conv_short: "-", type_conv_detail: "", has_type_conversion: false,
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
        capacity_status: "N/A", capacity_label: "-", capacity_short: "-", capacity_detail: "",
        type_conv_status: "N/A", type_conv_label: "-", type_conv_short: "-", type_conv_detail: "", has_type_conversion: false,
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
          "推荐同名未配对", "-", "-", "-",
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
          "仅来源库存在", "-", "-", "-", "仅来源库存在该表（目标库无已保存配对或同名表）",
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
          "仅目标库存在", "-", "-", "-", "仅目标库存在该表（来源库无已保存配对或同名表）",
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
        r.bookmark_label, r.target_compliance_label, r.capacity_label, r.type_conv_label, r.detail,
      ],
    };
  }

  function matchFlatRow(r) {
    if (state.rowFilter === "MIGRATE") {
      if (!r.needsMigration) return false;
    } else if (state.rowFilter === "CAP_ISSUE") {
      if (r.capacity_status !== "INSUFFICIENT" && r.capacity_status !== "RISK") return false;
    } else if (state.rowFilter === "TYPE_CONV") {
      if (!r.has_type_conversion) return false;
    } else if (state.rowFilter !== "ALL" && r.bookmark_state !== state.rowFilter) {
      return false;
    }
    const kw = state.rowKeyword.trim().toLowerCase();
    if (!kw) return true;
    return [
      r.std_table_key, r.source_table, r.target_table, r.source_col_name, r.target_col_name,
      r.source_std_code, r.target_std_code, r.source_std_cn, r.target_std_cn,
      r.capacity_label, r.type_conv_label, r.detail
    ].some((v) => String(v || "").toLowerCase().includes(kw));
  }

  function filteredPairsList() {
    const kw = state.pairKeyword.trim().toLowerCase();
    return PAIRS.filter((p) => {
      const tb = p.table_binding || {};
      const bk = p.bookmark_summary || {};
      if (state.pairListFilter === "HAS_DIFF" && (bk.split_diff || 0) === 0) return false;
      if (state.pairListFilter === "HAS_MIG" && (bk.target_needs_migration || 0) === 0) return false;
      if (state.pairListFilter === "HAS_CAP_ISSUE" && (bk.capacity_insufficient || 0) === 0 && (bk.capacity_risk || 0) === 0) return false;
      if (state.pairListFilter === "HAS_TYPE_CONV" && (bk.type_conversion || 0) === 0) return false;
      if (state.pairListFilter === "UNIFIED_ONLY" && ((bk.split_diff || 0) > 0 || (bk.unified || 0) === 0)) return false;
      if (!kw) return true;
      if ([tb.std_table_key, tb.source_schema, tb.source_table, tb.target_schema, tb.target_table]
        .some((v) => String(v || "").toLowerCase().includes(kw))) {
        return true;
      }
      if ((p.center_rows || []).some((cr) => [
        (cr.source_col || {}).column_name, (cr.target_col || {}).column_name,
        (cr.source_std || {}).cn_name, (cr.target_std || {}).cn_name
      ].some((v) => String(v || "").toLowerCase().includes(kw)))) {
        return true;
      }
      return (p.source_candidates || []).some((c) => String(c.column_name || "").toLowerCase().includes(kw))
        || (p.target_candidates || []).some((c) => String(c.column_name || "").toLowerCase().includes(kw));
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
    if (state.tab === "pairs") {
      return hasPairsFilter();
    }
    return false;
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
    } else if (state.tab === "pairs") {
      const pairsSubset = state.selectedPairKey === "__ALL__"
        ? filteredPairsList()
        : PAIRS.filter((p) => (p.table_binding || {}).std_table_key === state.selectedPairKey);
      for (const p of pairsSubset) {
        for (const r of buildPairFlatRows(p)) {
          if (matchFlatRow(r)) allRows.push(rowToExcelObj(r));
        }
      }
    } else {
      return;
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
    className: "input", type: "search", placeholder: "搜索表对 / 来源或目标字段...",
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
        SCOPE.scope_label ? badge("范围: " + SCOPE.scope_label, "lav") : null,
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
    const skipCnt = SCOPE.only_bound_tables ? Number(SCOPE.skipped_unbound_tables_count || 0) : 0;
    const cards = [
      { label: "已保存映射表对", val: String(AGG.saved_pairs_count ?? PAIRS.length), sub: "来源总表 " + (AGG.source_total_tables ?? 0) + " · 目标总表 " + (AGG.target_total_tables ?? 0) + (skipCnt > 0 ? "（已过滤 " + skipCnt + " 张零绑标表）" : "") },
      { label: "推荐同名未配对表", val: String(AGG.recommended_pairs_count ?? REC_PAIRS.length), sub: "仅推荐清单（未自动预比对）" },
      { label: "标准一致组 (UNIFIED)", val: String(AGG.unified_cols ?? 0), sub: "共已映射 " + (AGG.total_center_rows ?? 0) + " 组" },
      { label: "标准异标 (SPLIT_DIFF)", val: String(AGG.split_diff_cols ?? 0), sub: "单侧绑标 " + (AGG.split_partial_cols ?? 0) + " 组" },
      { label: "长度/精度风险", val: String(AGG.capacity_insufficient_cols ?? 0), sub: "无法包住 " + (AGG.capacity_insufficient_cols ?? 0) + " · 需核验 " + (AGG.capacity_risk_cols ?? 0) },
      { label: "涉及类型转换", val: String(AGG.type_conversion_cols ?? 0), sub: "目标库待贯标 " + (AGG.target_needs_migration_cols ?? 0) + " 组" },
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
    ths.push("来源字段", "来源类型", "来源标准", "中缝对标状态", "目标标准", "目标字段", "目标类型", "目标落标", "长度/精度包络", "类型转换", "差异说明");

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
        el("td", { title: r.capacity_detail || "" }, r.capacity_status && r.capacity_status !== "N/A"
          ? badge(r.capacity_label || "-", capTone(r.capacity_status))
          : el("span", { style: { color: "var(--muted)" }, text: "-" })),
        el("td", { title: r.type_conv_detail || "" }, r.type_conv_status && r.type_conv_status !== "N/A"
          ? badge(r.type_conv_label || "-", convTone(r.type_conv_status))
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
      const cc = r.cross_compat || {};
      if (state.rowFilter === "MIGRATE" && !r.target_needs_migration) return false;
      if (state.rowFilter === "CAP_ISSUE" && cc.capacity_status !== "INSUFFICIENT" && cc.capacity_status !== "RISK") return false;
      if (state.rowFilter === "TYPE_CONV" && !cc.has_type_conversion) return false;
      if (["UNIFIED", "SPLIT_DIFF", "SPLIT_PARTIAL"].includes(state.rowFilter) && r.bookmark_state !== state.rowFilter) return false;
      const kw = state.rowKeyword.trim().toLowerCase();
      if (!kw) return true;
      return [
        (r.source_col || {}).column_name, (r.target_col || {}).column_name,
        r.source_std_code, r.target_std_code,
        (r.source_std || {}).cn_name, (r.target_std || {}).cn_name,
        cc.capacity_label, cc.type_conv_label
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
      const cc = row.cross_compat || {};
      const showCompat = isTgt && row.paired && cc.applicable;
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
        ),
        showCompat ? el("div", { style: { display: "flex", flexWrap: "wrap", gap: "4px", marginTop: "3px" }, title: [cc.capacity_detail, cc.type_conv_detail].filter(Boolean).join("；") },
          badge(cc.capacity_short || cc.capacity_label, capTone(cc.capacity_status)),
          badge(cc.type_conv_short || cc.type_conv_label, convTone(cc.type_conv_status))
        ) : null
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
      const tone = ((bk.split_diff || 0) > 0 || (bk.capacity_insufficient || 0) > 0)
        ? "bad"
        : (((bk.target_needs_migration || 0) > 0 || (bk.capacity_risk || 0) > 0) ? "warn" : "ok");
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
                  { k: "MIGRATE", l: "待贯标" }, { k: "CAP_ISSUE", l: "容量不足" },
                  { k: "TYPE_CONV", l: "类型转换" }, { k: "CANDIDATE", l: "待配对候选" },
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
    const capWarnCnt = (bk.capacity_insufficient || 0) + (bk.capacity_risk || 0);
    clearEl(
      pairSubbarMount,
      el("div", { className: "subbar-group" },
        el("span", { className: "mono", style: { fontWeight: "700" }, text: (tb.source_schema || "") + "." + (tb.source_table || "") + " ↔ " + (tb.target_schema || "") + "." + (tb.target_table || "") }),
        badge("一致 " + (bk.unified || 0), "ok"),
        badge("异标 " + (bk.split_diff || 0), (bk.split_diff || 0) > 0 ? "bad" : ""),
        badge("单侧 " + (bk.split_partial || 0), (bk.split_partial || 0) > 0 ? "info" : ""),
        capWarnCnt > 0 ? badge("容量风险 " + capWarnCnt, (bk.capacity_insufficient || 0) > 0 ? "bad" : "warn") : null,
        (bk.type_conversion || 0) > 0 ? badge("类型转换 " + (bk.type_conversion || 0), "info") : null,
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
            { k: "CAP_ISSUE", l: "容量不足" }, { k: "TYPE_CONV", l: "类型转换" },
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
                    ["映射表键", "来源表", "目标表", "已映射组", "标准一致", "标准异标", "单侧绑标", "容量不足", "类型转换", "目标待贯标", "源待选", "目标待选"].map((h) => el("th", { text: h }))
                  )),
                  el("tbody", {}, pList.map((p) => {
                    const tb = p.table_binding || {};
                    const bk = p.bookmark_summary || {};
                    const k = tb.std_table_key || "";
                    const capIns = bk.capacity_insufficient || 0;
                    const capRisk = bk.capacity_risk || 0;
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
                      el("td", {}, badge(String(capIns + capRisk), capIns > 0 ? "bad" : (capRisk > 0 ? "warn" : ""))),
                      el("td", {}, badge(String(bk.type_conversion || 0), (bk.type_conversion || 0) > 0 ? "info" : "")),
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
        { v: "HAS_CAP_ISSUE", t: "含长度/精度不足" },
        { v: "HAS_TYPE_CONV", t: "含类型转换" },
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


def build_cross_env_html_bytes(report_data: Dict[str, Any]) -> bytes:
    """生成跨环境库级比对离线交互 HTML 报告（含五栏视图、推荐同名清单与单 Sheet .xlsx 导出能力）."""
    payload = {
        **report_data,
        "generated_at": report_data.get("generated_at") or _now_str(),
        "excel_headers": list(_CROSS_ENV_HEADERS),
    }
    return wrap_offline_html(
        "Data.O.G · 跨环境库级比对与落标报告",
        payload,
        _CROSS_ENV_APP_JS,
    )
