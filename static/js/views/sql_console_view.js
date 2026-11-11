/**
 * Data.O.G 桌面模块 2：数据探查 (SqlConsoleView)
 * 无框 Finder 风格左侧数据库目录树 + 沉浸式 SQL 编辑器 + 结果/结构/DDL 标签面板
 */

import { platformApi } from "../core/api.js";
import { badge, clearEl, el, statusDot, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { DatabaseTree } from "../ui/database_tree.js";
import { ObjectPicker } from "../ui/object_picker.js";
import { SqlCodeBox } from "../ui/sql_code_box.js";
import { Toast } from "../ui/toast.js";

export class SqlConsoleView {
  constructor() {
    this.connectionId = store.state.connections[0]?.connection_id || "";
    this.schemaName = "";
    this.tableName = "";
    this.limit = 200;
    this.activeTab = "rows"; // rows | structure | ddl
    this.sidebarVisible = true;
    this.lastResult = null;
    this.lastTableMeta = null;
    this.refreshHeader = null;

    this.treeHost = el("aside", { className: "mac-sidebar mac-sidebar-wide" });
    this.dbTree = new DatabaseTree(this.treeHost, {
      onPreviewTable: ({ connectionId, schemaName, tableName }) => {
        this.connectionId = connectionId;
        this.schemaName = schemaName;
        this.tableName = tableName;
        this.picker.setValue({ connectionId, schemaName, tableName });
        this.previewSelectedTable();
      },
    });

    this.sqlBox = new SqlCodeBox({
      title: "SQL",
      value: "SELECT 1;",
      frameless: true,
      minHeight: "128px",
      filename: "query.sql",
      onRun: (sql) => this.runSql(sql),
    });

    this.picker = new ObjectPicker({
      connectionId: this.connectionId,
      onChange: ({ connectionId, schemaName, tableName }) => {
        this.connectionId = connectionId;
        this.schemaName = schemaName;
        this.tableName = tableName;
      },
    });

    this.rightPane = el("div", { className: "mac-pane", style: { overflow: "hidden" } });
    this.root = el("div", { className: "mac-split-view" });
  }

  destroy() {
    this.dbTree.destroy();
  }

  getHeaderParts() {
    this.picker.renderOptions();
    const summaryText = this.tableName ? `${this.tableName}` : "SQL 查询";
    store.setAppSummary("explorer", summaryText);

    const limitSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "最大行数",
        onChange: (e) => {
          this.limit = Number(e.target.value) || 200;
        },
      },
      [100, 200, 500, 1000, 2000].map((n) => {
        const o = el("option", { value: String(n), text: `${n} 行` });
        if (this.limit === n) o.selected = true;
        return o;
      })
    );

    const center = el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm mac-btn-ghost",
        title: "显示/隐藏目录树",
        onClick: () => {
          this.sidebarVisible = !this.sidebarVisible;
          this.render();
        },
      }, svgIcon("sidebar", 14)),
      this.picker.root,
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "预览",
        onClick: () => this.previewSelectedTable(),
      })
    );

    const right = el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      limitSelect,
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "CSV",
        onClick: () => this.exportResult("csv"),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "Excel",
        onClick: () => this.exportResult("xlsx"),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm mac-btn-primary",
        text: "执行",
        onClick: () => this.runSql(this.sqlBox.getValue()),
      })
    );

    return { center, right };
  }

  async mount(container, context = null, refreshHeader = null) {
    this.refreshHeader = refreshHeader;
    clearEl(container, this.root);

    if (!this.connectionId && store.state.connections.length > 0) {
      this.connectionId = store.state.connections[0].connection_id;
    }
    if (context?.connectionId) this.connectionId = context.connectionId;
    if (context?.schemaName !== undefined) this.schemaName = context.schemaName;
    if (context?.tableName !== undefined) this.tableName = context.tableName;

    this.picker.setValue({
      connectionId: this.connectionId,
      schemaName: this.schemaName,
      tableName: this.tableName,
    });

    this.render();
    this.refreshHeader?.();

    if (context?.action === "preview_table" && this.connectionId && this.tableName) {
      await this.previewSelectedTable();
    }
  }

  async previewSelectedTable() {
    const { connectionId, schemaName, tableName } = this.picker.getValue();
    if (!connectionId || !tableName) {
      Toast.warn("请选择连接与表名");
      return;
    }
    this.connectionId = connectionId;
    this.schemaName = schemaName;
    this.tableName = tableName;

    try {
      const [previewRes, metaRes] = await Promise.all([
        platformApi.previewTable(connectionId, schemaName, tableName, Math.min(this.limit, 500)),
        platformApi.getTableDetail(connectionId, schemaName, tableName, false).catch(() => null),
      ]);
      this.lastResult = previewRes.result;
      this.lastTableMeta = metaRes?.table || null;
      const shownSql = this.lastResult?.sql || this.lastResult?.executed_sql;
      if (shownSql) {
        this.sqlBox.setValue(shownSql, `${schemaName ? schemaName + "." : ""}${tableName}`);
      }
      this.render();
      this.refreshHeader?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async runSql(sqlText) {
    const { connectionId } = this.picker.getValue();
    const effConnId = connectionId || this.connectionId;
    if (!effConnId) {
      Toast.warn("请先选择连接");
      return;
    }
    if (!sqlText || !sqlText.trim()) {
      Toast.warn("请输入 SQL");
      return;
    }
    try {
      const res = await platformApi.executeSql({
        connection_id: effConnId,
        sql: sqlText,
        limit: Number(this.limit) || 200,
        timeout_sec: 30,
      });
      this.lastResult = res.result;
      this.activeTab = "rows";
      this.render();
      this.refreshHeader?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async exportResult(fmt) {
    if (!this.lastResult || !(this.lastResult.columns || []).length) {
      Toast.warn("暂无结果可导出");
      return;
    }
    try {
      const colNames = this.lastResult.columns.map((c) => (typeof c === "string" ? c : c.name));
      const title = this.tableName || "query_result";
      await platformApi.exportQuery(
        {
          columns: colNames,
          rows: this.lastResult.rows || [],
          format: fmt,
          title,
        },
        `${title}.${fmt}`
      );
      Toast.success(`已导出 .${fmt}`);
    } catch (err) {
      Toast.error(err.message);
    }
  }

  render() {
    const tabDefs = [
      { id: "rows", label: this.lastResult ? `结果 (${this.lastResult.row_count})` : "结果" },
      { id: "structure", label: this.lastTableMeta ? `结构 (${(this.lastTableMeta.columns || []).length})` : "结构" },
      { id: "ddl", label: "DDL" },
    ];

    const tabsBar = el(
      "div",
      { className: "content-tablist", role: "tablist", "aria-label": "查询结果视图" },
      ...tabDefs.map((t) =>
        el("button", {
          type: "button",
          role: "tab",
          className: "content-tab",
          "aria-selected": String(this.activeTab === t.id),
          text: t.label,
          onClick: () => {
            this.activeTab = t.id;
            this.render();
          },
        })
      ),
      el(
        "div",
        { style: { marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: "10px" } },
        this.lastResult
          ? statusDot(
              `${this.lastResult.elapsed_ms ?? 0} ms${this.lastResult.truncated ? " · 已截断" : ""}`,
              this.lastResult.truncated ? "warn" : "success"
            )
          : null,
        this.tableName
          ? el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              text: "送入多邻国 →",
              onClick: () =>
                store.openApp("polyglot", {
                  mode: "dialect",
                  connectionId: this.connectionId,
                  schemaName: this.schemaName,
                  tableName: this.tableName,
                }),
            })
          : null
      )
    );

    let tabPanelBody;
    if (this.activeTab === "rows") {
      if (!this.lastResult) {
        tabPanelBody = el("div", {
          className: "grid-empty",
          text: "点击左侧表名预览，或输入 SQL 按 ⌘+Enter 执行",
        });
      } else {
        const cols = (this.lastResult.columns || []).map((c, colIdx) => {
          const colName = typeof c === "string" ? c : c.name;
          return {
            key: String(colIdx),
            label: colName,
            render: (row) => {
              const v = Array.isArray(row) ? row[colIdx] : row?.[colName];
              if (v === null || v === undefined) {
                return el("span", { style: { color: "var(--os-muted)" }, text: "NULL" });
              }
              return String(v);
            },
          };
        });
        tabPanelBody = new DataGrid({
          ariaLabel: "查询结果",
          columns: cols,
          rows: this.lastResult.rows || [],
          rowKey: (_r, idx) => String(idx),
          emptyText: "0 行记录",
        }).root;
      }
    } else if (this.activeTab === "structure") {
      const colsMeta = this.lastTableMeta?.columns || [];
      const distSet = new Set((this.lastTableMeta?.distribute_keys || []).map((k) => String(k).toLowerCase()));
      const partSet = new Set((this.lastTableMeta?.partition_keys || []).map((k) => String(k).toLowerCase()));
      tabPanelBody = new DataGrid({
        ariaLabel: "表结构",
        columns: [
          { key: "ordinal_position", label: "#" },
          { key: "column_name", label: "字段", render: (r) => el("span", { className: "mono", style: { fontWeight: "600" }, text: r.column_name }) },
          { key: "data_type", label: "类型", render: (r) => el("span", { className: "mono", text: r.data_type }) },
          { key: "not_null", label: "非空", render: (r) => (r.is_not_null || r.not_null ? "是" : "-") },
          {
            key: "keys",
            label: "键属性",
            render: (r) => {
              const tags = [];
              const low = String(r.column_name || "").toLowerCase();
              if (r.is_primary_key) tags.push(badge("PK", "info"));
              if (r.is_distribute_key || distSet.has(low)) tags.push(badge("分布键", "warn"));
              if (r.is_partition_key || partSet.has(low)) tags.push(badge("分区键", "success"));
              return tags.length ? el("div", { style: { display: "flex", gap: "4px" } }, tags) : "-";
            },
          },
          { key: "column_comment", label: "注释", render: (r) => r.column_comment || "-" },
        ],
        rows: colsMeta,
        rowKey: "column_name",
        emptyText: "请先选择一张表点击「预览」",
      }).root;
    } else {
      const ddlText =
        this.lastTableMeta?.raw_ddl ||
        this.lastTableMeta?.view_definition ||
        "-- 选择表后点击「预览」查看原始定义";
      tabPanelBody = new SqlCodeBox({
        title: this.lastTableMeta ? `${this.lastTableMeta.schema_name}.${this.lastTableMeta.table_name}` : "DDL",
        value: ddlText,
        readOnly: true,
        frameless: true,
        minHeight: "340px",
        filename: `${this.tableName || "table"}_ddl.sql`,
      }).root;
    }

    clearEl(
      this.rightPane,
      this.sqlBox.root,
      tabsBar,
      el(
        "div",
        { role: "tabpanel", style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column" } },
        tabPanelBody
      )
    );

    clearEl(this.root, this.sidebarVisible ? this.treeHost : null, this.rightPane);
  }
}
