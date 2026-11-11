/**
 * Data.O.G 桌面模块 4：多邻国 (PolyglotView)
 * 窗口顶栏通过 macOS 分段控制器整合两大翻译能力：
 * 1. 智能命名 — 中文表名/字段名 -> 数仓标准英文标识符 + 推荐 SQL 类型（支持 DeepSeek / Gemini 大模型与内置 170+ 数仓词根引擎双驱动）
 * 2. 方言翻译 — 跨数据库建表 DDL 互译、四层递归视图血缘穿透与 AI 异构 SQL 语法深度适配
 */

import { ddlApi } from "../core/api.js";
import { badge, clearEl, copyText, downloadBlob, el, statusDot, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { Modal } from "../ui/modal.js";
import { ObjectPicker } from "../ui/object_picker.js";
import { Toast } from "../ui/toast.js";

const SAMPLE_NAMING_COLUMN = [
  "客户唯一标识",
  "统一社会信用代码",
  "账户开户日期",
  "累计交易金额",
  "账户当前余额",
  "近30天交易笔数",
  "年化利率",
  "是否实名认证",
  "风险等级代码",
  "更新时间",
].join("\n");

const SAMPLE_NAMING_TABLE = [
  "客户基础信息表",
  "个人信贷合同明细表",
  "机构日度存款汇总表",
  "渠道交易流水表",
  "商户风险等级配置表",
].join("\n");

const SAMPLE_VIEW_SQL = `CREATE OR REPLACE VIEW public.v_cust_loan_summary AS
SELECT
  c.cust_id AS cust_id, -- 客户唯一标识
  c.cust_nm AS cust_nm, -- 客户名称
  COUNT(l.due_bill_no) AS loan_cnt, -- 借据笔数
  SUM(l.prin_amt) AS tot_prin_amt, -- 累计本金金额
  MAX(l.open_dt) AS last_open_dt -- 最近开户日期
FROM public.ods_cust_info c
LEFT JOIN public.dwd_loan_due_bill l
  ON c.cust_id = l.cust_id
GROUP BY c.cust_id, c.cust_nm;`;

const SAMPLE_CREATE_TABLE_SQL = `CREATE TABLE ods_fin.t_acct_txn_jnl (
  txn_seq_no VARCHAR(64) PRIMARY KEY COMMENT '交易流水号',
  cust_id VARCHAR(64) NOT NULL COMMENT '客户唯一标识',
  acct_no VARCHAR(64) NOT NULL COMMENT '账号',
  txn_amt DECIMAL(18,2) COMMENT '交易金额',
  int_rate DECIMAL(10,6) COMMENT '执行利率',
  is_ovd_flg TINYINT COMMENT '是否逾期标志',
  txn_dt DATE COMMENT '交易日期',
  create_tm DATETIME COMMENT '创建时间'
);`;

const DEFAULT_DIALECTS = [
  { value: "DWS", label: "GaussDB(DWS)" },
  { value: "PGSQL", label: "PostgreSQL / openGauss" },
  { value: "INCEPTOR", label: "Transwarp Inceptor" },
  { value: "MYSQL", label: "MySQL / StarRocks" },
  { value: "DM8", label: "达梦 DM8" },
  { value: "ORACLE", label: "Oracle" },
  { value: "SQLITE", label: "SQLite" },
];

const DEFAULT_STORAGE_MODES = [
  { value: "AUTO", label: "AUTO 默认存储" },
  { value: "DWS_ROW", label: "DWS 行存 (ROW)" },
  { value: "DWS_COLUMN", label: "DWS 列存 (COLUMN)" },
  { value: "INCEPTOR_ORC_TXN", label: "Inceptor ORC 事务表" },
  { value: "INCEPTOR_ORC", label: "Inceptor 标准 ORC" },
  { value: "INCEPTOR_HOLODESK", label: "Inceptor Holodesk" },
  { value: "INCEPTOR_TEXTFILE", label: "Inceptor TEXTFILE" },
];

export class PolyglotView {
  constructor() {
    this.mode = "naming"; // naming | dialect
    this.refreshHeader = null;
    this.optionsLoaded = false;
    this.busy = false;

    // 共享 AI 模型选择: "" (自动取默认已配密钥模型) | "local::dict" (仅本地词根/AST) | "<provider_id>::<model>"
    this.selectedModelKey = "";

    // 子模式 1：智能命名状态
    this.namingGranularity = "column"; // column | table
    this.namingStyle = "snake_case"; // snake_case | UPPER_SNAKE | camelCase
    this.tablePrefix = "dwd";
    this.useAbbrev = true;
    this.namingDialect = "DWS";
    this.namingInputText = SAMPLE_NAMING_COLUMN;
    this.namingResult = null;

    // 子模式 2：方言翻译状态
    this.targetDialects = DEFAULT_DIALECTS;
    this.storageModes = DEFAULT_STORAGE_MODES;
    this.standardTables = [];

    this.sourceMode = "connection"; // connection | sql_text | standard
    this.connectionId = store.state.connections[0]?.connection_id || "";
    this.sourceSchema = "";
    this.sourceTable = "";
    this.stdTableKey = "";
    this.applyStandardMapping = false;
    this.probeConnectionId = "";
    this.sourceDialectHint = "";
    this.sqlText = SAMPLE_VIEW_SQL;

    this.targetDialect = "DWS";
    this.storageMode = "AUTO";
    this.targetSchema = "";
    this.targetTable = "";
    this.aiAssist = true;
    this.translateResult = null;
    this.mappingExpanded = false;

    this.picker = new ObjectPicker({
      connectionId: this.connectionId,
      onChange: ({ connectionId, schemaName, tableName }) => {
        this.connectionId = connectionId;
        this.sourceSchema = schemaName;
        this.sourceTable = tableName;
      },
    });

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });

    this._unsub = store.subscribe((_state, reason) => {
      if (["llm", "connections", "catalog"].includes(reason)) {
        this.picker.renderOptions();
        this.refreshHeader?.();
        if (reason === "connections") {
          this.render();
        }
      }
    });
  }

  destroy() {
    this._unsub?.();
  }

  _resolveModelChoice() {
    if (this.selectedModelKey === "local::dict") {
      return { useAi: false, providerId: "", model: "" };
    }
    const models = store.state.llmModels || [];
    if (this.selectedModelKey && this.selectedModelKey.includes("::")) {
      const [providerId, model] = this.selectedModelKey.split("::", 2);
      return { useAi: true, providerId, model };
    }
    if (models.length > 0) {
      const def = models.find((m) => m.is_default) || models[0];
      return { useAi: true, providerId: def.provider_id, model: def.model };
    }
    return { useAi: true, providerId: "", model: "" };
  }

  getHeaderParts() {
    const summaryText = this.mode === "naming" ? "智能命名" : `方言 → ${this.targetDialect}`;
    store.setAppSummary("polyglot", summaryText);

    const center = el(
      "div",
      { className: "seg-group" },
      [
        { id: "naming", label: "智能命名" },
        { id: "dialect", label: "方言翻译" },
      ].map((tab) =>
        el("button", {
          type: "button",
          className: `seg-btn ${this.mode === tab.id ? "is-active" : ""}`,
          text: tab.label,
          onClick: () => {
            this.mode = tab.id;
            this.render();
            this.refreshHeader?.();
          },
        })
      )
    );

    const llmModels = store.state.llmModels || [];
    const readyModels = llmModels.filter((m) => m.ready || m.has_api_key);
    const modelOptions = [];
    if (llmModels.length > 0) {
      const def =
        readyModels.find((m) => m.is_default) ||
        readyModels[0] ||
        llmModels.find((m) => m.is_default) ||
        llmModels[0];
      if (!this.selectedModelKey) {
        this.selectedModelKey = `${def.provider_id}::${def.model}`;
      }
      for (const m of llmModels) {
        const val = `${m.provider_id}::${m.model}`;
        const suffix = m.ready || m.has_api_key ? "" : " (未配密钥)";
        const opt = el("option", {
          value: val,
          text: `${m.provider_name} · ${m.model}${suffix}`,
        });
        if (val === this.selectedModelKey) opt.selected = true;
        modelOptions.push(opt);
      }
    } else {
      const unconfOpt = el("option", { value: "", text: "内置词根 / AST (未配 AI)" });
      if (!this.selectedModelKey) unconfOpt.selected = true;
      modelOptions.push(unconfOpt);
    }

    const localOpt = el("option", { value: "local::dict", text: "纯本地词根 / AST 引擎" });
    if (this.selectedModelKey === "local::dict") localOpt.selected = true;
    modelOptions.push(localOpt);

    const modelSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "选择翻译引擎模型",
        style: { maxWidth: "205px" },
        onChange: (e) => {
          this.selectedModelKey = e.target.value;
          if (this.selectedModelKey === "local::dict") {
            this.aiAssist = false;
          } else {
            this.aiAssist = true;
          }
          this.render();
        },
      },
      modelOptions
    );

    const primaryBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-sm mac-btn-primary",
      disabled: this.busy,
      text: this.busy
        ? "处理中..."
        : this.mode === "naming"
        ? "开始命名"
        : "开始翻译",
      onClick: () => {
        if (this.mode === "naming") {
          this.runSmartNaming();
        } else {
          this.runDialectTranslate();
        }
      },
    });

    const right = el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      modelSelect,
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm mac-btn-ghost",
          title: "配置 DeepSeek / Gemini API",
          onClick: () => store.openApp("settings", { section: "llm" }),
        },
        svgIcon("settings", 14)
      ),
      primaryBtn
    );

    return { center, right };
  }

  async ensureOptionsLoaded() {
    if (this.optionsLoaded) return;
    try {
      const res = await ddlApi.getOptions();
      this.optionsLoaded = true;
      if (Array.isArray(res.target_dialects) && res.target_dialects.length) {
        this.targetDialects = res.target_dialects;
      }
      if (Array.isArray(res.storage_modes) && res.storage_modes.length) {
        this.storageModes = res.storage_modes;
      }
      this.standardTables = res.standard_tables || [];
      if (Array.isArray(res.llm_models) && res.llm_models.length && !store.state.llmModels.length) {
        store.updateLlmState(null, res.llm_models);
      }
    } catch (_err) {
      // 离线保持内置选项可用
    }
  }

  async mount(container, context = null, refreshHeader = null) {
    this.refreshHeader = refreshHeader;
    clearEl(container, this.root);

    await this.ensureOptionsLoaded();

    let autoTranslate = false;
    if (context) {
      if (context.mode === "dialect" || context.tableName || context.viewSql || context.sqlText) {
        this.mode = "dialect";
      } else if (context.mode === "naming") {
        this.mode = "naming";
      }
      if (context.connectionId) {
        this.connectionId = context.connectionId;
        this.sourceMode = "connection";
      }
      if (context.schemaName !== undefined) {
        this.sourceSchema = context.schemaName;
      }
      if (context.tableName !== undefined) {
        this.sourceTable = context.tableName;
        if (context.tableName) {
          this.sourceMode = "connection";
          autoTranslate = true;
        }
      }
      if (context.stdTableKey) {
        this.stdTableKey = context.stdTableKey;
        this.applyStandardMapping = true;
      }
      if (context.viewSql || context.sqlText) {
        this.sourceMode = "sql_text";
        this.sqlText = context.viewSql || context.sqlText;
        autoTranslate = true;
      }
    }

    if (!this.connectionId && store.state.connections.length > 0) {
      this.connectionId = store.state.connections[0].connection_id;
    }

    this.picker.setValue({
      connectionId: this.connectionId,
      schemaName: this.sourceSchema,
      tableName: this.sourceTable,
    });

    this.render();
    this.refreshHeader?.();

    if (autoTranslate) {
      await this.runDialectTranslate();
    }
  }

  async runSmartNaming() {
    const raw = (this.namingInputText || "").trim();
    if (!raw) {
      Toast.warn("请输入中文表名或字段名");
      return;
    }
    const { useAi, providerId, model } = this._resolveModelChoice();
    this.busy = true;
    this.refreshHeader?.();
    try {
      const res = await ddlApi.smartNaming({
        text: raw,
        granularity: this.namingGranularity,
        naming_style: this.namingStyle,
        table_prefix: this.tablePrefix,
        use_abbrev: this.useAbbrev,
        target_dialect: this.namingDialect,
        use_ai: useAi,
        provider_id: providerId,
        model,
      });
      this.namingResult = res;
      this.render();
      this.refreshHeader?.();
    } catch (err) {
      Toast.error(err.message);
    } finally {
      this.busy = false;
      this.refreshHeader?.();
    }
  }

  async runDialectTranslate() {
    const { connectionId, schemaName, tableName } = this.picker.getValue();
    if (this.sourceMode === "connection") {
      this.connectionId = connectionId;
      this.sourceSchema = schemaName;
      this.sourceTable = tableName;
      if (!this.connectionId || !this.sourceTable) {
        Toast.warn("请选择来源连接并输入表名或视图名");
        return;
      }
    } else if (this.sourceMode === "sql_text") {
      if (!(this.sqlText || "").trim()) {
        Toast.warn("请粘贴需要转换的 SQL、视图或建表语句");
        return;
      }
    }

    const { useAi, providerId, model } = this._resolveModelChoice();
    const effAiAssist = Boolean(this.aiAssist && useAi);

    this.busy = true;
    this.refreshHeader?.();
    try {
      const res = await ddlApi.translate({
        source_mode: this.sourceMode,
        connection_id: this.sourceMode === "connection" ? this.connectionId : "",
        probe_connection_id: this.sourceMode === "sql_text" ? this.probeConnectionId : "",
        source_schema: this.sourceSchema,
        source_table: this.sourceTable,
        source_dialect_hint: this.sourceDialectHint,
        std_table_key: this.stdTableKey,
        apply_standard_mapping: this.applyStandardMapping,
        sql_text: this.sourceMode === "sql_text" ? this.sqlText : "",
        view_sql: this.sourceMode === "sql_text" ? this.sqlText : "",
        target_dialect: this.targetDialect,
        storage_mode: this.storageMode,
        target_schema: this.targetSchema,
        target_table: this.targetTable,
        ai_assist: effAiAssist,
        provider_id: providerId,
        model,
      });
      this.translateResult = res;
      this.render();
      this.refreshHeader?.();
    } catch (err) {
      Toast.error(err.message);
    } finally {
      this.busy = false;
      this.refreshHeader?.();
    }
  }

  _renderNamingView() {
    const res = this.namingResult;
    const items = res?.items || [];

    const granSeg = el(
      "div",
      { className: "seg-group" },
      [
        { id: "column", label: "字段名" },
        { id: "table", label: "表名" },
      ].map((g) =>
        el("button", {
          type: "button",
          className: `seg-btn ${this.namingGranularity === g.id ? "is-active" : ""}`,
          text: g.label,
          onClick: () => {
            const wasDefaultSample =
              this.namingInputText === SAMPLE_NAMING_COLUMN ||
              this.namingInputText === SAMPLE_NAMING_TABLE;
            this.namingGranularity = g.id;
            if (wasDefaultSample) {
              this.namingInputText = g.id === "table" ? SAMPLE_NAMING_TABLE : SAMPLE_NAMING_COLUMN;
            }
            this.render();
          },
        })
      )
    );

    const styleSelect = el(
      "select",
      {
        className: "mac-input input-xs",
        "aria-label": "命名风格",
        onChange: (e) => (this.namingStyle = e.target.value),
      },
      [
        { v: "snake_case", l: "snake_case" },
        { v: "UPPER_SNAKE", l: "UPPER_SNAKE" },
        { v: "camelCase", l: "camelCase" },
      ].map((o) => {
        const opt = el("option", { value: o.v, text: o.l });
        if (o.v === this.namingStyle) opt.selected = true;
        return opt;
      })
    );

    const prefixSelect = el(
      "select",
      {
        className: "mac-input input-xs",
        "aria-label": "数仓分层前缀",
        title: "表分层前缀",
        onChange: (e) => (this.tablePrefix = e.target.value),
      },
      [
        { v: "", l: "无前缀" },
        { v: "ods", l: "ods_" },
        { v: "dwd", l: "dwd_" },
        { v: "dws", l: "dws_" },
        { v: "ads", l: "ads_" },
        { v: "dim", l: "dim_" },
      ].map((o) => {
        const opt = el("option", { value: o.v, text: o.l });
        if (o.v === this.tablePrefix) opt.selected = true;
        return opt;
      })
    );

    const dialectSelect = el(
      "select",
      {
        className: "mac-input input-xs",
        "aria-label": "推荐类型方言",
        onChange: (e) => (this.namingDialect = e.target.value),
      },
      this.targetDialects.map((d) => {
        const opt = el("option", { value: d.value, text: d.value });
        if (d.value === this.namingDialect) opt.selected = true;
        return opt;
      })
    );

    const abbrevToggle = el(
      "label",
      { style: { display: "inline-flex", alignItems: "center", gap: "4px", fontSize: "11.5px", cursor: "pointer" } },
      el("input", {
        type: "checkbox",
        checked: this.useAbbrev,
        onChange: (e) => (this.useAbbrev = e.target.checked),
      }),
      el("span", { text: "词根缩写" })
    );

    const leftSubbar = el(
      "div",
      { className: "mac-subbar" },
      el("div", { className: "mac-subbar-group" }, granSeg, styleSelect),
      el("div", { className: "mac-subbar-group" }, prefixSelect, dialectSelect, abbrevToggle)
    );

    const leftToolbar = el(
      "div",
      { className: "sql-code-toolbar" },
      el("span", { className: "sql-code-title", text: "中文名称（每行一项，⌘+Enter 执行）" }),
      el(
        "div",
        { style: { display: "inline-flex", gap: "4px" } },
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: "示例",
          onClick: () => {
            this.namingInputText =
              this.namingGranularity === "table" ? SAMPLE_NAMING_TABLE : SAMPLE_NAMING_COLUMN;
            this.render();
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: "清空",
          onClick: () => {
            this.namingInputText = "";
            this.render();
          },
        })
      )
    );

    const inputArea = el("textarea", {
      className: "ddl-pane-textarea",
      spellcheck: "false",
      placeholder: "每行输入一个中文字段名或表名...",
      value: this.namingInputText,
      onInput: (e) => {
        this.namingInputText = e.target.value;
      },
      onKeydown: (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
          e.preventDefault();
          this.runSmartNaming();
        }
      },
    });

    const leftPane = el("section", { className: "ddl-pane" }, leftSubbar, leftToolbar, inputArea);

    const engineStatusNode = res
      ? statusDot(
          `${res.engine_label}${res.elapsed_ms ? ` · ${Math.round(res.elapsed_ms)} ms` : ""}${
            res.fallback_reason ? " · 本地词根兜底" : ""
          }`,
          res.ai_used ? "success" : "info"
        )
      : statusDot("待生成", "muted");

    const rightSubbar = el(
      "div",
      { className: "mac-subbar" },
      el("div", { className: "mac-subbar-group" }, engineStatusNode),
      el(
        "div",
        { className: "mac-subbar-group" },
        items.length
          ? el("button", {
              type: "button",
              className: "btn btn-xs",
              text: "复制英文名",
              onClick: async () => {
                const ok = await copyText(items.map((x) => x.en).join("\n"));
                if (ok) Toast.success("已复制英文标识符列表");
              },
            })
          : null,
        res?.ddl_preview
          ? el("button", {
              type: "button",
              className: "btn btn-xs",
              text: "查看建表 DDL",
              onClick: () => {
                const ta = el("textarea", {
                  className: "sql-code-textarea",
                  readonly: true,
                  style: { minHeight: "260px", border: "1px solid var(--os-border)", borderRadius: "8px" },
                  value: res.ddl_preview,
                });
                Modal.open({
                  title: `建表草稿 (${res.target_dialect})`,
                  width: "min(640px, 92vw)",
                  body: ta,
                  footer: [
                    el("button", {
                      type: "button",
                      className: "mac-btn",
                      text: "复制 DDL",
                      onClick: async () => {
                        await copyText(res.ddl_preview);
                        Toast.success("已复制建表 DDL");
                      },
                    }),
                    el("button", {
                      type: "button",
                      className: "mac-btn mac-btn-primary",
                      text: "关闭",
                      onClick: () => Modal.close("ok"),
                    }),
                  ],
                });
              },
            })
          : null,
        res?.ddl_preview
          ? el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              text: "转入方言翻译 →",
              onClick: () => {
                this.mode = "dialect";
                this.sourceMode = "sql_text";
                this.sqlText = res.ddl_preview;
                this.render();
                this.refreshHeader?.();
              },
            })
          : null
      )
    );

    const grid = new DataGrid({
      ariaLabel: "智能命名结果",
      columns: [
        { key: "cn", label: "中文名称" },
        {
          key: "en",
          label: "标准英文名",
          render: (r) =>
            el(
              "span",
              {
                className: "mono",
                style: { fontWeight: "600", color: "var(--os-accent)", cursor: "pointer" },
                title: "点击复制",
                onClick: async (ev) => {
                  ev.stopPropagation();
                  await copyText(r.en);
                  Toast.success(`已复制 ${r.en}`);
                },
              },
              r.en
            ),
        },
        {
          key: "type",
          label: "推荐类型",
          render: (r) => el("span", { className: "mono", text: r.type }),
        },
        {
          key: "note",
          label: "词根映射",
          render: (r) => el("span", { style: { color: "var(--os-text-secondary)" }, text: r.note || "-" }),
        },
      ],
      rows: items,
      rowKey: "cn",
      emptyText: "点击右上角「开始命名」生成标准英文标识符",
    });

    const rightPane = el(
      "div",
      { className: "mac-pane", style: { overflow: "hidden" } },
      rightSubbar,
      grid.root
    );

    return el("div", { className: "polyglot-split" }, leftPane, rightPane);
  }

  _renderDialectView() {
    const res = this.translateResult;
    const conns = store.state.connections || [];

    const srcModeSeg = el(
      "div",
      { className: "seg-group" },
      [
        { id: "connection", label: "库表" },
        { id: "sql_text", label: "粘贴 SQL" },
        { id: "standard", label: "数据标准" },
      ].map((m) =>
        el("button", {
          type: "button",
          className: `seg-btn ${this.sourceMode === m.id ? "is-active" : ""}`,
          text: m.label,
          onClick: () => {
            this.sourceMode = m.id;
            this.render();
          },
        })
      )
    );

    let sourceControlNode = null;
    if (this.sourceMode === "connection") {
      this.picker.renderOptions();
      sourceControlNode = el(
        "div",
        { className: "mac-subbar-group" },
        this.picker.root,
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "4px", fontSize: "11.5px", cursor: "pointer" } },
          el("input", {
            type: "checkbox",
            checked: this.applyStandardMapping,
            onChange: (e) => (this.applyStandardMapping = e.target.checked),
          }),
          el("span", { text: "套用数标" })
        )
      );
    } else if (this.sourceMode === "sql_text") {
      const probeSelect = el(
        "select",
        {
          className: "mac-input mac-input-sm",
          "aria-label": "视图血缘穿透数据源",
          title: "可选：绑定数据源后自动穿透视图底层基表获取真实类型与注释",
          onChange: (e) => (this.probeConnectionId = e.target.value),
        },
        [
          el("option", { value: "", text: "血缘穿透: 纯静态解析" }),
          ...conns.map((c) => {
            const o = el("option", { value: c.connection_id, text: `穿透: ${c.name}` });
            if (c.connection_id === this.probeConnectionId) o.selected = true;
            return o;
          }),
        ]
      );

      const hintSelect = el(
        "select",
        {
          className: "mac-input mac-input-sm",
          "aria-label": "来源方言提示",
          onChange: (e) => (this.sourceDialectHint = e.target.value),
        },
        [
          { v: "", l: "来源: 自动识别" },
          { v: "ORACLE", l: "来源: Oracle" },
          { v: "INCEPTOR", l: "来源: Inceptor/Hive" },
          { v: "MYSQL", l: "来源: MySQL" },
          { v: "PGSQL", l: "来源: PostgreSQL" },
          { v: "DWS", l: "来源: DWS" },
          { v: "DM8", l: "来源: 达梦 DM8" },
        ].map((h) => {
          const o = el("option", { value: h.v, text: h.l });
          if (h.v === this.sourceDialectHint) o.selected = true;
          return o;
        })
      );
      sourceControlNode = el("div", { className: "mac-subbar-group" }, hintSelect, probeSelect);
    } else {
      const stdSelect = el(
        "select",
        {
          className: "mac-input mac-input-sm",
          "aria-label": "选择数据标准表",
          onChange: (e) => (this.stdTableKey = e.target.value),
        },
        [
          el("option", { value: "", text: "自动取首张标准表" }),
          ...this.standardTables.map((t) => {
            const o = el("option", {
              value: t.std_table_key,
              text: `${t.std_schema_name ? t.std_schema_name + "." : ""}${t.std_table_name}`,
            });
            if (t.std_table_key === this.stdTableKey) o.selected = true;
            return o;
          }),
        ]
      );
      sourceControlNode = el("div", { className: "mac-subbar-group" }, stdSelect);
    }

    const tgtDialectSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "目标方言",
        onChange: (e) => {
          this.targetDialect = e.target.value;
          this.refreshHeader?.();
        },
      },
      this.targetDialects.map((d) => {
        const o = el("option", { value: d.value, text: `目标: ${d.label}` });
        if (d.value === this.targetDialect) o.selected = true;
        return o;
      })
    );

    const storageSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "存储模式",
        onChange: (e) => (this.storageMode = e.target.value),
      },
      this.storageModes.map((s) => {
        const o = el("option", { value: s.value, text: s.label });
        if (s.value === this.storageMode) o.selected = true;
        return o;
      })
    );

    const targetSchemaInput = el("input", {
      className: "mac-input mac-input-sm",
      style: { width: "92px" },
      placeholder: "目标 Schema",
      value: this.targetSchema,
      onInput: (e) => (this.targetSchema = e.target.value.trim()),
    });

    const targetTableInput = el("input", {
      className: "mac-input mac-input-sm",
      style: { width: "126px" },
      placeholder: "目标表名",
      value: this.targetTable,
      onInput: (e) => (this.targetTable = e.target.value.trim()),
    });

    const aiToggle = el(
      "label",
      {
        style: { display: "inline-flex", alignItems: "center", gap: "4px", fontSize: "11.5px", cursor: "pointer" },
        title: "结合配置的大模型深度适配异构 SQL 函数、分析语法与方言细节",
      },
      el("input", {
        type: "checkbox",
        checked: this.aiAssist && this.selectedModelKey !== "local::dict",
        disabled: this.selectedModelKey === "local::dict",
        onChange: (e) => (this.aiAssist = e.target.checked),
      }),
      el("span", { text: "AI 语法适配" })
    );

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el("div", { className: "mac-subbar-group" }, srcModeSeg, sourceControlNode),
      el(
        "div",
        { className: "mac-subbar-group" },
        tgtDialectSelect,
        storageSelect,
        targetSchemaInput,
        targetTableInput,
        aiToggle
      )
    );

    const isEditableSql = this.sourceMode === "sql_text";
    const shownSourceSql = isEditableSql
      ? this.sqlText
      : res?.source_ddl || "-- 选择来源对象后点击右上角「开始翻译」";

    const leftToolbar = el(
      "div",
      { className: "sql-code-toolbar" },
      el(
        "div",
        { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
        el("span", {
          className: "sql-code-title",
          text: isEditableSql
            ? "来源 SQL / 视图 / DDL（⌘+Enter 执行）"
            : `来源定义${res?.source_dialect ? ` · ${res.source_dialect}` : ""}`,
        }),
        res?.penetrated_base_tables?.length
          ? badge(`穿透 ${res.penetrated_base_tables.length} 基表`, "info")
          : null
      ),
      isEditableSql
        ? el(
            "div",
            { style: { display: "inline-flex", gap: "4px" } },
            el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              text: "视图示例",
              onClick: () => {
                this.sqlText = SAMPLE_VIEW_SQL;
                this.render();
              },
            }),
            el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              text: "建表示例",
              onClick: () => {
                this.sqlText = SAMPLE_CREATE_TABLE_SQL;
                this.render();
              },
            }),
            el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              text: "清空",
              onClick: () => {
                this.sqlText = "";
                this.render();
              },
            })
          )
        : el("button", {
            type: "button",
            className: "btn btn-xs btn-ghost",
            text: "复制",
            onClick: async () => {
              if (res?.source_ddl) {
                await copyText(res.source_ddl);
                Toast.success("已复制来源 DDL");
              }
            },
          })
    );

    const leftTextarea = el("textarea", {
      className: "ddl-pane-textarea",
      spellcheck: "false",
      readonly: !isEditableSql,
      placeholder: isEditableSql ? "粘贴 CREATE TABLE、CREATE VIEW 或异构查询 SQL..." : "",
      value: shownSourceSql,
      onInput: isEditableSql
        ? (e) => {
            this.sqlText = e.target.value;
          }
        : undefined,
      onKeydown: isEditableSql
        ? (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              this.runDialectTranslate();
            }
          }
        : undefined,
    });

    const targetSqlText = res?.target_ddl || "-- 转换后的目标方言 SQL 将显示在此处";
    const rightToolbar = el(
      "div",
      { className: "sql-code-toolbar" },
      el(
        "div",
        { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
        el("span", {
          className: "sql-code-title",
          text: `目标 SQL · ${res?.target_dialect || this.targetDialect}`,
        }),
        res?.engine_mode
          ? badge(
              res.ai_used && res.ai_model ? `${res.engine_mode} · ${res.ai_model}` : res.engine_mode,
              res.ai_used ? "success" : "neutral"
            )
          : null
      ),
      el(
        "div",
        { style: { display: "inline-flex", gap: "4px" } },
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: "复制",
          onClick: async () => {
            if (!res?.target_ddl) {
              Toast.warn("暂无可复制的目标 SQL");
              return;
            }
            await copyText(res.target_ddl);
            Toast.success("已复制目标 SQL");
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: ".sql",
          onClick: () => {
            if (!res?.target_ddl) {
              Toast.warn("暂无可导出的目标 SQL");
              return;
            }
            const fname = `${res.target_table || "translated"}_${(res.target_dialect || this.targetDialect).toLowerCase()}.sql`;
            downloadBlob(new Blob([res.target_ddl], { type: "text/plain;charset=utf-8" }), fname);
            Toast.success(`已导出 ${fname}`);
          },
        })
      )
    );

    const rightTextarea = el("textarea", {
      className: "ddl-pane-textarea",
      spellcheck: "false",
      readonly: true,
      value: targetSqlText,
    });

    const dualSplit = el(
      "div",
      { className: "ddl-dual-split" },
      el("section", { className: "ddl-pane" }, leftToolbar, leftTextarea),
      el("section", { className: "ddl-pane" }, rightToolbar, rightTextarea)
    );

    const mappings = res?.column_mappings || [];
    const aiNotes = res?.ai_notes || [];

    const footerBar = el(
      "div",
      {
        className: "sql-code-toolbar",
        style: { borderTop: "1px solid var(--os-border)", borderBottom: this.mappingExpanded ? "1px solid var(--os-border)" : "none" },
      },
      el(
        "div",
        { style: { display: "inline-flex", alignItems: "center", gap: "8px", minWidth: "0", overflow: "hidden" } },
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: `${this.mappingExpanded ? "▾" : "▸"} 字段映射 (${mappings.length})`,
          onClick: () => {
            this.mappingExpanded = !this.mappingExpanded;
            this.render();
          },
        }),
        res?.penetrated_base_tables?.length
          ? el("span", {
              style: { fontSize: "11px", color: "var(--os-muted)" },
              text: `基表: ${res.penetrated_base_tables.join(", ")}`,
            })
          : null,
        res?.ai_warning ? statusDot(res.ai_warning, "warn") : null
      ),
      aiNotes.length
        ? el(
            "span",
            {
              style: {
                fontSize: "11px",
                color: "var(--os-text-secondary)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                maxWidth: "52%",
              },
              title: aiNotes.join("；"),
              text: `AI 适配：${aiNotes.join(" · ")}`,
            }
          )
        : null
    );

    const mappingGridNode =
      this.mappingExpanded && mappings.length
        ? new DataGrid({
            ariaLabel: "跨方言字段映射明细",
            maxHeight: "195px",
            columns: [
              { key: "ordinal_position", label: "#" },
              {
                key: "column_name",
                label: "字段",
                render: (r) => el("span", { className: "mono", style: { fontWeight: "600" }, text: r.column_name }),
              },
              {
                key: "source_type",
                label: "来源类型",
                render: (r) => el("span", { className: "mono", text: r.source_type }),
              },
              {
                key: "target_type",
                label: "目标类型",
                render: (r) =>
                  el("span", {
                    className: "mono",
                    style: { color: "var(--os-accent)", fontWeight: "600" },
                    text: r.target_type,
                  }),
              },
              {
                key: "column_comment",
                label: "注释",
                render: (r) => r.column_comment || "-",
              },
            ],
            rows: mappings,
            rowKey: "column_name",
          }).root
        : this.mappingExpanded
        ? el("div", { className: "grid-empty", style: { padding: "14px" }, text: "当前转换无结构化列映射" })
        : null;

    return el(
      "div",
      { style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" } },
      subbar,
      dualSplit,
      footerBar,
      mappingGridNode
    );
  }

  render() {
    clearEl(this.root, this.mode === "naming" ? this._renderNamingView() : this._renderDialectView());
  }
}

export { PolyglotView as DdlTranslatorView };
