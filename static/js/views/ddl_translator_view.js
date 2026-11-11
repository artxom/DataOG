/**
 * DataOG 独立业务插件视图 3：跨方言建表 DDL 翻译与视图四层血缘穿透器 (modules/ddl_translator)
 * 支持：
 * - 模式 1：从全局任意数据源连接读取物理表或视图（自动向上四层递归血缘穿透）
 * - 模式 2：直接粘贴 CREATE VIEW / SELECT 语句（支持离线语法推断 + 可选绑定在线连接四层穿透真实基表类型与注释）
 * - 模式 3：从已导入的数据标准清单直接生成任意目标方言建表脚本
 * - 目标方言覆盖：华为 DWS、PostgreSQL、星环 Inceptor (ORC事务表/Holodesk/TEXTFILE)、MySQL/Doris/OB、达梦 DM8、Oracle、SQLite
 */

import { ddlApi } from "../core/api.js";
import { badge, clearEl, el } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { ObjectPicker } from "../ui/object_picker.js";
import { SqlCodeBox } from "../ui/sql_code_box.js";
import { Toast } from "../ui/toast.js";

const SAMPLE_VIEW_SQL = `CREATE OR REPLACE VIEW public.v_cust_order_summary AS
WITH base_orders AS (
  SELECT cust_id, total_amt, order_dt
  FROM public.dwd_order_detail
)
SELECT
  b.cust_id AS customer_id, -- 客户唯一标识
  COUNT(1) AS order_cnt, -- 累计订单笔数
  SUM(b.total_amt) AS total_amount, -- 累计交易金额
  MAX(b.order_dt) AS latest_order_date -- 最近下单日期
FROM base_orders b
GROUP BY b.cust_id;`;

export class DdlTranslatorView {
  constructor() {
    this.sourceMode = "connection"; // connection | view_sql | standard
    this.options = {
      target_dialects: [],
      storage_modes: [],
      standard_tables: [],
    };
    this.targetDialect = "DWS";
    this.storageMode = "AUTO";
    this.targetSchema = "";
    this.targetTable = "";
    this.stdTableKey = "";
    this.applyStandardMapping = false;
    this.probeConnectionId = "";
    this.lastTranslated = null;

    this.picker = new ObjectPicker({});
    this.viewSqlInputBox = new SqlCodeBox({
      title: "粘贴来源视图 CREATE VIEW ... AS SELECT ... 或复杂多层 CTE 查询语句",
      value: SAMPLE_VIEW_SQL,
      minHeight: "150px",
      filename: "source_view.sql",
      onRun: () => this.runTranslate(),
    });
    this.sourceDdlBox = new SqlCodeBox({
      title: "来源对象 DDL / 视图定义",
      value: "-- 点击上方「执行跨方言翻译与穿透」生成对照脚本",
      readOnly: true,
      minHeight: "250px",
      filename: "source_ddl.sql",
    });
    this.targetDdlBox = new SqlCodeBox({
      title: "目标方言可执行建表 DDL 脚本",
      value: "-- 点击上方「执行跨方言翻译与穿透」生成目标建表脚本",
      readOnly: true,
      minHeight: "250px",
      filename: "target_ddl.sql",
    });

    this.root = el("div", { className: "module-page" });
  }

  async mount(container, context = null) {
    clearEl(container, this.root);
    try {
      const opts = await ddlApi.getOptions();
      this.options = opts;
    } catch (_e) {
      // 保留默认下拉项
    }

    let autoRun = false;
    if (context) {
      if (context.viewSql) {
        this.sourceMode = "view_sql";
        this.viewSqlInputBox.setValue(context.viewSql);
        autoRun = true;
      } else if (context.connectionId || context.tableName) {
        this.sourceMode = "connection";
        this.picker.setValue({
          connectionId: context.connectionId || store.state.connections[0]?.connection_id || "",
          schemaName: context.schemaName || "",
          tableName: context.tableName || "",
        });
        if (context.stdTableKey) {
          this.stdTableKey = context.stdTableKey;
        }
        if (context.connectionId && context.tableName) {
          autoRun = true;
        }
      }
    }

    this.render();
    if (autoRun) {
      await this.runTranslate();
    }
  }

  async runTranslate() {
    try {
      const picked = this.picker.getValue();
      const payload = {
        source_mode: this.sourceMode,
        connection_id: picked.connectionId,
        probe_connection_id: this.probeConnectionId || "",
        source_schema: picked.schemaName,
        source_table: picked.tableName,
        target_dialect: this.targetDialect,
        storage_mode: this.storageMode,
        target_schema: this.targetSchema.trim(),
        target_table: this.targetTable.trim(),
        apply_standard_mapping: this.applyStandardMapping,
        std_table_key: this.stdTableKey,
        view_sql: this.sourceMode === "view_sql" ? this.viewSqlInputBox.getValue() : "",
        force_refresh: false,
      };
      const res = await ddlApi.translate(payload);
      this.lastTranslated = res;
      this.sourceDdlBox.setValue(
        res.source_ddl || "",
        `来源定义 [${res.source_dialect} · ${res.source_schema}.${res.source_table} (${res.source_object_type})]`
      );
      this.targetDdlBox.setValue(
        res.target_ddl || "",
        `目标建表脚本 [${res.target_dialect} · ${res.target_schema}.${res.target_table} · 共 ${res.column_count} 列]`
      );
      Toast.success(`已完成 ${res.source_dialect} → ${res.target_dialect} 建表 DDL 翻译 (${res.column_count} 列)`);
      this.render();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  render() {
    this.picker.renderOptions();
    const dialects = this.options.target_dialects?.length
      ? this.options.target_dialects
      : [
          { value: "DWS", label: "华为云 GaussDB(DWS)" },
          { value: "PGSQL", label: "PostgreSQL / openGauss" },
          { value: "INCEPTOR", label: "星环 Transwarp Inceptor" },
          { value: "MYSQL", label: "MySQL / OceanBase / Doris / StarRocks" },
          { value: "DM8", label: "达梦数据库 DM8" },
          { value: "ORACLE", label: "Oracle Database" },
          { value: "SQLITE", label: "SQLite 3" },
        ];

    const storageModes = this.options.storage_modes?.length
      ? this.options.storage_modes
      : [
          { value: "AUTO", label: "智能默认存储格式 (AUTO)" },
          { value: "DWS_ROW", label: "华为 DWS 行存表" },
          { value: "DWS_COLUMN", label: "华为 DWS 列存压缩表" },
          { value: "INCEPTOR_ORC_TXN", label: "星环 Inceptor ORC 事务表" },
          { value: "INCEPTOR_ORC", label: "星环 Inceptor 标准 ORC 表" },
          { value: "INCEPTOR_HOLODESK", label: "星环 Inceptor Holodesk 内存表" },
        ];

    const toolbar = el(
      "div",
      { className: "module-toolbar" },
      el(
        "div",
        { className: "module-toolbar-left" },
        el("h1", { className: "module-title", text: "跨方言建表 DDL 翻译与四层视图穿透器" }),
        el(
          "div",
          { className: "tabs-bar", style: { borderBottom: "none", paddingBottom: "0" } },
          [
            { id: "connection", label: "1. 从数据源表/视图读取" },
            { id: "view_sql", label: "2. 粘贴视图 SQL (四层血缘穿透)" },
            { id: "standard", label: "3. 从数据标准清单生成" },
          ].map((m) =>
            el("button", {
              type: "button",
              className: `tab-btn ${this.sourceMode === m.id ? "is-active" : ""}`,
              text: m.label,
              onClick: () => {
                this.sourceMode = m.id;
                this.render();
              },
            })
          )
        )
      ),
      el(
        "div",
        { className: "module-toolbar-right" },
        el("button", {
          type: "button",
          className: "btn btn-sm btn-primary",
          text: "执行跨方言翻译与穿透",
          onClick: () => this.runTranslate(),
        })
      )
    );

    // 来源配置面板
    let sourceConfigNode;
    if (this.sourceMode === "connection") {
      sourceConfigNode = el(
        "div",
        { style: { display: "flex", alignItems: "center", flexWrap: "wrap", gap: "8px" } },
        el("span", { style: { fontWeight: "700" }, text: "选择来源表或视图:" }),
        this.picker.root
      );
    } else if (this.sourceMode === "view_sql") {
      const probeSelect = el(
        "select",
        {
          className: "input input-sm",
          onChange: (e) => {
            this.probeConnectionId = e.target.value;
          },
        },
        [
          el("option", { value: "", text: "纯离线 SQL 语法推断 (不连库)" }),
          ...(store.state.connections || []).map((c) => {
            const o = el("option", {
              value: c.connection_id,
              text: `在线四层穿透基表元数据: ${c.name} [${c.db_type}]`,
            });
            if (c.connection_id === this.probeConnectionId) o.selected = true;
            return o;
          }),
        ]
      );
      sourceConfigNode = el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "8px" } },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
          el("span", { style: { fontWeight: "700" }, text: "上游基表递归穿透连接 (可选):" }),
          probeSelect
        ),
        this.viewSqlInputBox.root
      );
    } else {
      const stdTables = this.options.standard_tables || [];
      sourceConfigNode = el(
        "div",
        { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
        el("span", { style: { fontWeight: "700" }, text: "选择已导入的数据标准表:" }),
        el(
          "select",
          {
            className: "input input-sm",
            onChange: (e) => {
              this.stdTableKey = e.target.value;
            },
          },
          [
            el("option", { value: "", text: "-- 默认首张标准表或贯标清单表 --" }),
            ...stdTables.map((t) => {
              const o = el("option", { value: t.std_table_key, text: t.std_table_name });
              if (t.std_table_key === this.stdTableKey) o.selected = true;
              return o;
            }),
          ]
        )
      );
    }

    // 目标方言与命名覆盖参数栏
    const targetConfigBar = el(
      "div",
      { className: "card", style: { display: "flex", flexDirection: "column", gap: "10px" } },
      sourceConfigNode,
      el(
        "div",
        { style: { display: "flex", alignItems: "center", flexWrap: "wrap", gap: "8px", paddingTop: "8px", borderTop: "1px dashed var(--border-subtle)" } },
        el("span", { style: { fontWeight: "700" }, text: "目标方言:" }),
        el(
          "select",
          {
            className: "input input-sm",
            onChange: (e) => {
              this.targetDialect = e.target.value;
            },
          },
          dialects.map((d) => {
            const o = el("option", { value: d.value, text: d.label });
            if (d.value === this.targetDialect) o.selected = true;
            return o;
          })
        ),
        el("span", { style: { fontWeight: "700" }, text: "存储模型:" }),
        el(
          "select",
          {
            className: "input input-sm",
            onChange: (e) => {
              this.storageMode = e.target.value;
            },
          },
          storageModes.map((s) => {
            const o = el("option", { value: s.value, text: s.label });
            if (s.value === this.storageMode) o.selected = true;
            return o;
          })
        ),
        el("input", {
          className: "input input-sm",
          style: { width: "130px" },
          placeholder: "目标Schema(可选)",
          value: this.targetSchema,
          onInput: (e) => (this.targetSchema = e.target.value),
        }),
        el("input", {
          className: "input input-sm",
          style: { width: "160px" },
          placeholder: "目标表名(可选)",
          value: this.targetTable,
          onInput: (e) => (this.targetTable = e.target.value),
        }),
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px" } },
          el("input", {
            type: "checkbox",
            checked: this.applyStandardMapping,
            onChange: (e) => {
              this.applyStandardMapping = e.target.checked;
            },
          }),
          el("span", { text: "套用数据标准字段命名与注释" })
        )
      )
    );

    const lineageBanner =
      this.lastTranslated && (this.lastTranslated.penetrated_base_tables || []).length
        ? el(
            "div",
            { className: "card", style: { display: "flex", alignItems: "center", gap: "8px", padding: "8px 12px" } },
            badge("四层递归视图血缘穿透生效", "success"),
            el("span", {
              style: { fontSize: "12px" },
              text: `已自动向上穿透解析底表结构与注释：${this.lastTranslated.penetrated_base_tables.join(" → ")}`,
            })
          )
        : null;

    const mappingGrid = new DataGrid({
      ariaLabel: "跨方言字段类型映射明细表",
      rows: this.lastTranslated?.column_mappings || [],
      rowKey: (_r, i) => String(i),
      emptyText: "执行翻译后将在此展示逐列源类型 → 归一化类型 → 目标方言类型的完整映射矩阵",
      maxHeight: "36vh",
      columns: [
        { key: "ordinal_position", label: "序号" },
        { key: "column_name", label: "字段名" },
        { key: "source_type", label: "来源数据类型" },
        { key: "target_type", label: "翻译后目标类型", render: (r) => badge(r.target_type, "info") },
        { key: "lineage_source", label: "血缘穿透来源", render: (r) => r.lineage_source || "-" },
        { key: "column_comment", label: "字段注释", render: (r) => r.column_comment || "-" },
      ],
    });

    const content = el(
      "div",
      { className: "module-content" },
      targetConfigBar,
      lineageBanner,
      el("div", { className: "ddl-dual-grid" }, this.sourceDdlBox.root, this.targetDdlBox.root),
      el(
        "div",
        { className: "card" },
        el("div", { className: "card-header" }, el("h2", { className: "card-title", text: "字段级跨方言类型转换与血缘映射明细" })),
        mappingGrid.root
      )
    );

    clearEl(this.root, toolbar, content);
  }
}
