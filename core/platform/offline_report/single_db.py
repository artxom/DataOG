"""单库库级落标与贯标离线 HTML / 单 Sheet Excel 报告生成器."""

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

_MIG_STATUS_LABELS: Dict[str, str] = {
    "COMPLETED": "已达标",
    "PENDING": "待改造",
    "PARTIAL": "部分合规",
    "FAILED": "执行报错",
    "MISSING_TABLE": "缺表",
    "MISSING_COLUMN": "缺列",
    "UNCHECKED": "未核验",
}

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
    if (state.tab === "overview") return hasOverviewFilter();
    if (state.tab === "queue") return hasQueueFilter();
    return false;
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
    if (state.tab === "queue") {
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
    if (state.tab !== "overview") return;
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


def build_single_db_html_bytes(report_data: Dict[str, Any]) -> bytes:
    """生成单库库级落标与贯标离线交互 HTML 报告（内嵌单 Sheet .xlsx 导出能力）."""
    conn_name = str((report_data.get("connection") or {}).get("name") or "数据源")
    payload = {
        **report_data,
        "generated_at": report_data.get("generated_at") or _now_str(),
        "excel_headers": list(_SINGLE_DB_HEADERS),
    }
    return wrap_offline_html(
        f"Data.O.G · {conn_name} 库级落标与贯标报告",
        payload,
        _SINGLE_DB_APP_JS,
    )

