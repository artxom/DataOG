/**
 * DataOG 内置核心视图 2：轻量 SQL 查询与表数据浏览台 (QueryEngine + MetadataHub)
 * 支持：
 * - 双击左侧全局目录树任意表/视图自动预览前 200 行数据、字段明细与原始建表/视图 DDL
 * - 自定义 SQL 查询执行（默认安全截断 LIMIT 500、耗时毫秒统计、超时保护）
 * - 结果集一键导出为 CSV 或 Excel (.xlsx)
 */

import { platformApi } from "../core/api.js";
import { badge, clearEl, el } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { ObjectPicker } from "../ui/object_picker.js";
import { SqlCodeBox } from "../ui/sql_code_box.js";
import { Toast } from "../ui/toast.js";

export class SqlConsoleView {
  constructor() {
    this.connectionId = "";
    this.schemaName = "";
    this.tableName = "";
    this.limit = 500;
    this.activeTab = "rows"; // rows | structure | ddl
    this.lastResult = null;
    this.lastTableMeta = null;

    this.sqlBox = new SqlCodeBox({
      title: "SQL 查询编辑器 (默认安全保护截断，支持 SELECT / SHOW / EXPLAIN / PRAGMA)",
      value: "SELECT 1 AS hello_dataog;",
      minHeight: "120px",
      filename: "dataog_query.sql",
      onRun: (sql) => this.runSql(sql),
    });

    this.picker = new ObjectPicker({
      onChange: ({ connectionId, schemaName, tableName }) => {
        this.connectionId = connectionId;
        this.schemaName = schemaName;
        this.tableName = tableName;
      },
    });

    this.root = el("div", { className: "module-page" });
  }

  async mount(container, context = null) {
    clearEl(container, this.root);
    if (!this.connectionId && store.state.connections.length > 0) {
      this.connectionId = store.state.connections[0].connection_id;
    }

    if (context?.connectionId) {
      this.connectionId = context.connectionId;
    }
    if (context?.schemaName !== undefined) {
      this.schemaName = context.schemaName;
    }
    if (context?.tableName !== undefined) {
      this.tableName = context.tableName;
    }

    this.picker.setValue({
      connectionId: this.connectionId,
      schemaName: this.schemaName,
      tableName: this.tableName,
    });

    this.render();

    if (context?.action === "preview_table" && this.connectionId && this.tableName) {
      await this.previewSelectedTable();
    }
  }

  async previewSelectedTable() {
    const { connectionId, schemaName, tableName } = this.picker.getValue();
    if (!connectionId || !tableName) {
      Toast.warn("请先选择数据源连接与表名/视图名");
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
      if (this.lastResult?.executed_sql) {
        this.sqlBox.setValue(this.lastResult.executed_sql);
      }
      this.render();
    } catch (err) {
      Toast.error(`预览表数据失败: ${err.message}`);
    }
  }

  async runSql(sqlText) {
    const { connectionId } = this.picker.getValue();
    const effConnId = connectionId || this.connectionId;
    if (!effConnId) {
      Toast.warn("请先在工具栏选择一个目标数据源连接");
      return;
    }
    if (!sqlText || !sqlText.trim()) {
      Toast.warn("请输入要执行的 SQL 查询语句");
      return;
    }
    try {
      const res = await platformApi.executeSql({
        connection_id: effConnId,
        sql: sqlText,
        limit: Number(this.limit) || 500,
        timeout_sec: 30,
      });
      this.lastResult = res.result;
      this.activeTab = "rows";
      Toast.success(`查询完成：返回 ${res.result.row_count} 行 (${res.result.elapsed_ms} ms)`);
      this.render();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async exportResult(fmt) {
    if (!this.lastResult || !(this.lastResult.columns || []).length) {
      Toast.warn("当前没有可导出的查询结果集");
      return;
    }
    try {
      const colNames = this.lastResult.columns.map((c) => (typeof c === "string" ? c : c.name));
      const title = this.tableName || "sql_query_result";
      await platformApi.exportQuery(
        {
          columns: colNames,
          rows: this.lastResult.rows || [],
          format: fmt,
          title,
        },
        `${title}.${fmt}`
      );
      Toast.success(`已导出 ${fmt.toUpperCase()} 文件`);
    } catch (err) {
      Toast.error(`导出失败: ${err.message}`);
    }
  }

  render() {
    this.picker.renderOptions();

    const limitSelect = el(
      "select",
      {
        className: "input input-sm",
        "aria-label": "最大返回行数限制",
        onChange: (e) => {
          this.limit = Number(e.target.value) || 500;
        },
      },
      [100, 200, 500, 1000, 2000].map((n) => {
        const o = el("option", { value: String(n), text: `LIMIT ${n} 行` });
        if (this.limit === n) o.selected = true;
        return o;
      })
    );

    const toolbar = el(
      "div",
      { className: "module-toolbar" },
      el(
        "div",
        { className: "module-toolbar-left" },
        el("h1", { className: "module-title", text: "SQL 查询与表数据浏览台" }),
        this.picker.root,
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "预览表数据与结构",
          onClick: () => this.previewSelectedTable(),
        })
      ),
      el(
        "div",
        { className: "module-toolbar-right" },
        limitSelect,
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "导出 CSV",
          onClick: () => this.exportResult("csv"),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "导出 Excel",
          onClick: () => this.exportResult("xlsx"),
        }),
        this.tableName
          ? el("button", {
              type: "button",
              className: "btn btn-sm btn-ghost",
              text: "翻译此对象 DDL →",
              onClick: () =>
                store.navigateTo("ddl_translator", {
                  connectionId: this.connectionId,
                  schemaName: this.schemaName,
                  tableName: this.tableName,
                }),
            })
          : null
      )
    );

    // 结果集 / 结构 / DDL 三标签面板
    const tabsBar = el(
      "div",
      { className: "tabs-bar" },
      el("button", {
        type: "button",
        className: `tab-btn ${this.activeTab === "rows" ? "is-active" : ""}`,
        text: `数据结果集 ${this.lastResult ? `(${this.lastResult.row_count} 行)` : ""}`,
        onClick: () => {
          this.activeTab = "rows";
          this.render();
        },
      }),
      el("button", {
        type: "button",
        className: `tab-btn ${this.activeTab === "structure" ? "is-active" : ""}`,
        text: `字段结构与键属性 ${this.lastTableMeta ? `(${(this.lastTableMeta.columns || []).length} 列)` : ""}`,
        onClick: () => {
          this.activeTab = "structure";
          this.render();
        },
      }),
      el("button", {
        type: "button",
        className: `tab-btn ${this.activeTab === "ddl" ? "is-active" : ""}`,
        text: "对象原始建表 / 视图 DDL",
        onClick: () => {
          this.activeTab = "ddl";
          this.render();
        },
      }),
      this.lastResult
        ? el(
            "div",
            { style: { marginLeft: "auto", display: "flex", gap: "6px", alignItems: "center" } },
            badge(`耗时 ${this.lastResult.elapsed_ms ?? 0} ms`, "info"),
            this.lastResult.truncated
              ? badge(`已触发安全行数截断 (LIMIT ${this.lastResult.limit})`, "warn")
              : badge("完整返回", "success")
          )
        : null
    );

    let tabBody;
    if (this.activeTab === "rows") {
      if (!this.lastResult) {
        tabBody = el("div", {
          className: "grid-empty",
          text: "双击左侧目录树中的任意表/视图，或在上方输入 SQL 按 Cmd/Ctrl+Enter 即可查看查询结果",
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
                return el("span", { style: { color: "var(--text-muted)", fontStyle: "italic" }, text: "NULL" });
              }
              return String(v);
            },
          };
        });
        const grid = new DataGrid({
          ariaLabel: "SQL 查询结果集",
          columns: cols,
          rows: this.lastResult.rows || [],
          rowKey: (_r, idx) => String(idx),
          emptyText: "查询执行成功，当前条件下返回 0 行记录",
          maxHeight: "52vh",
        });
        tabBody = grid.root;
      }
    } else if (this.activeTab === "structure") {
      const colsMeta = this.lastTableMeta?.columns || [];
      const structGrid = new DataGrid({
        ariaLabel: "表字段元数据明细",
        columns: [
          { key: "ordinal_position", label: "物理列序" },
          { key: "column_name", label: "字段名" },
          { key: "data_type", label: "数据类型" },
          {
            key: "not_null",
            label: "非空",
            render: (r) => (r.not_null ? badge("NOT NULL", "warn") : "可空"),
          },
          {
            key: "keys",
            label: "主键 / 分布键 / 分区键",
            render: (r) => {
              const tags = [];
              if (r.is_primary_key) tags.push(badge("PK", "info"));
              if (r.is_distribute_key) tags.push(badge("分布键", "warn"));
              if (r.is_partition_key) tags.push(badge("分区键", "success"));
              return tags.length ? el("div", { style: { display: "flex", gap: "4px" } }, tags) : "-";
            },
          },
          { key: "lineage_source", label: "视图血缘穿透源", render: (r) => r.lineage_source || "-" },
          { key: "column_comment", label: "字段中文注释", render: (r) => r.column_comment || "-" },
        ],
        rows: colsMeta,
        rowKey: "column_name",
        emptyText: "请先通过上方库表选择器点击「预览表数据与结构」或双击左侧树节点加载表结构",
        maxHeight: "52vh",
      });
      tabBody = structGrid.root;
    } else {
      const ddlText =
        this.lastTableMeta?.raw_ddl ||
        this.lastTableMeta?.view_definition ||
        "-- 请先选择并预览一张具体的数据表或视图，如需跨方言生成建表语句可点击右上角「翻译此对象 DDL →」";
      const ddlViewer = new SqlCodeBox({
        title: this.lastTableMeta
          ? `${this.lastTableMeta.schema_name}.${this.lastTableMeta.table_name} (${this.lastTableMeta.object_type || "TABLE"}) 定义`
          : "对象 DDL 定义",
        value: ddlText,
        readOnly: true,
        minHeight: "320px",
        filename: `${this.tableName || "object"}_ddl.sql`,
      });
      tabBody = ddlViewer.root;
    }

    const content = el(
      "div",
      { className: "module-content" },
      this.sqlBox.root,
      el("div", { className: "card", style: { display: "flex", flexDirection: "column", gap: "10px" } }, tabsBar, tabBody)
    );

    clearEl(this.root, toolbar, content);
  }
}
