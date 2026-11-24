/**
 * Data.O.G v3.0 独立旗舰 Dock 应用：基于大模型的智能数据助手 (AiAssistantView)
 *
 * 架构特性：
 * - 严格零 innerHTML / outerHTML / 阻塞式弹窗，全部节点通过 core/dom.js 安全构建；
 * - 全面适配在线连库模式与离线纯方言模式，支持渐进式多轮探索、澄清卡、知识库溯源与 DDL/写操作分级确认；
 * - 经典可折叠三栏布局（默认左会话栏 + 中央对话流展开，右侧库表/知识库抽屉按需滑出）。
 */

import { badge, clearEl, el, formatBytes, pickLocalFile, statusDot, svgIcon } from "../../core/dom.js";
import { isAbortError } from "../../core/http.js";
import { store } from "../../core/state.js";
import { DataGrid } from "../../ui/data_grid.js";
import { Modal } from "../../ui/modal.js";
import { TaskActivityBar } from "../../ui/task_activity_bar.js";
import { Toast } from "../../ui/toast.js";
import { assistantApi } from "./api.js";
import { AssistantInspectorDrawer } from "./drawer_pane.js";
import { renderSafeMarkdown } from "./markdown_dom.js";
import { renderProposalCard } from "./proposal_card.js";

const TASK_MODES = [
  { id: "auto", label: "自动识别" },
  { id: "query", label: "数据查询" },
  { id: "troubleshoot", label: "报错排障" },
  { id: "optimize", label: "性能优化" },
  { id: "syntax", label: "语法确认" },
  { id: "ddl", label: "DDL 助手" },
];

const DEFAULT_ATTACHMENT_LIMITS = {
  max_csv_bytes: 2 * 1024 * 1024,
  max_text_bytes: 5 * 1024 * 1024,
  inline_token_budget: 3500,
  max_attachments_per_turn: 5,
  csv_extensions: [".csv", ".tsv"],
  allowed_extensions: [
    ".txt",
    ".sql",
    ".ddl",
    ".md",
    ".log",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".ini",
    ".conf",
    ".py",
    ".sh",
    ".csv",
    ".tsv",
  ],
};

const PASTE_TO_ATTACHMENT_MIN_CHARS = 4000;
const PASTE_TO_ATTACHMENT_MIN_LINES = 80;

const STRATEGY_OPTIONS = [
  { id: "auto", label: "自动策略（SQL/文本分步拆解 · CSV 沙箱查询）" },
  { id: "stepped_batch", label: "强制分步拆解（逐片汇总防爆窗）" },
  { id: "on_demand", label: "按需工具检索（省 Token）" },
];

const FALLBACK_DIALECTS = [
  { code: "DWS", label: "华为云 GaussDB(DWS)" },
  { code: "PGSQL", label: "PostgreSQL / openGauss" },
  { code: "MYSQL", label: "MySQL / OceanBase / Doris" },
  { code: "ORACLE", label: "Oracle Database" },
  { code: "DM8", label: "达梦数据库 DM8" },
  { code: "KINGBASE", label: "人大金仓 KingbaseES" },
  { code: "INCEPTOR", label: "星环 Inceptor / Hive" },
  { code: "DB2", label: "IBM Db2" },
  { code: "GBASE", label: "南大通用 GBase" },
  { code: "SQLITE", label: "SQLite 3" },
  { code: "GENERIC", label: "通用 SQL 规范" },
];

const SCENARIO_STARTERS = [
  {
    taskMode: "query",
    title: "自主探查与数据查询",
    desc: "自动核查库表字段并跑只读 SQL，报错可自我修复",
    prompt: "帮我查看当前连接中有哪些核心表，并统计关键业务表的记录数和近期数据分布。",
  },
  {
    taskMode: "troubleshoot",
    title: "SQL 报错精准排障",
    desc: "结合方言知识库与表结构定位错误码和类型冲突",
    prompt: "请帮我排查以下 SQL 报错原因并给出修复方案：\n-- 请粘贴报错 SQL 与完整报错信息",
  },
  {
    taskMode: "optimize",
    title: "执行计划与性能优化",
    desc: "实跑只读 EXPLAIN 分析全表扫描、重分布与索引失效",
    prompt: "请分析这条慢查询的执行计划瓶颈，并结合表结构与索引给出等价优化改写建议：",
  },
  {
    taskMode: "syntax",
    title: "离线方言语法与改写",
    desc: "无库连接也能按目标数据库方言检索知识库确认语法",
    prompt: "请依据当前目标数据库方言，说明分页查询与 UPSERT 合并写入的标准写法与避坑点。",
  },
  {
    taskMode: "ddl",
    title: "受控 DDL / 变更生成",
    desc: "生成规范建表/改表或写入语句，展示卡片待您二次确认",
    prompt: "请帮我设计一张规范的业务明细表建表 DDL（含注释与推荐索引/分布策略），先生成提案待我确认。",
  },
];

export class AiAssistantView {
  constructor() {
    this.refreshHeader = null;
    this.sidebarOpen = true;
    this.drawerOpen = false;
    this.loading = false;

    const firstConn = store.state.connections?.[0] || null;
    this.workMode = firstConn ? "connection" : "dialect";
    this.connectionId = firstConn ? firstConn.connection_id : "";
    this.dialect = firstConn ? firstConn.db_type : "DWS";
    this.schemaName = firstConn ? firstConn.default_schema || "" : "";
    this.focusTables = [];
    this.taskMode = "auto";
    this.selectedModelKey = "";
    this.maxSteps = 6;
    this.sampleLimit = 100;

    this.dialects = [...FALLBACK_DIALECTS];
    this.sessions = [];
    this.sessionSearch = "";
    this.currentSession = null;
    this.draftPrompt = "";
    this._pendingUserPrompt = "";
    this._pendingUserAttachments = [];
    this.attachmentLimits = { ...DEFAULT_ATTACHMENT_LIMITS };
    this.pendingAttachments = [];
    this.largePayloadStrategy = "auto";
    this.isDraggingFile = false;
    this.uploadingAttachment = false;
    this.expandedStepsMsgIds = new Set();

    this.activityBar = new TaskActivityBar({ graceMs: 120 });
    this.activityBar.el.style.margin = "8px 18px 0";

    this.drawer = new AssistantInspectorDrawer({
      onToggleFocusTable: (fqTable) => this._toggleFocusTable(fqTable),
      onOpenSettingsKnowledge: () => {
        store.openApp("settings", { section: "knowledge" });
      },
      onInsertTablePrompt: (fqTable) => {
        if (!this.focusTables.some((x) => x.toLowerCase() === fqTable.toLowerCase())) {
          this.focusTables = [...this.focusTables, fqTable];
        }
        const prefix = this.draftPrompt ? `${this.draftPrompt}\n` : "";
        this.draftPrompt = `${prefix}针对表 ${fqTable}，`;
        this.render();
      },
    });

    this.root = el("div", { className: "ai-asst-shell" });
    this._unsub = store.subscribe((_st, reason) => {
      if (["llm", "connections", "catalog", "bootstrap"].includes(reason)) {
        this._ensureSelectedModel();
        this.render();
        this.refreshHeader?.();
      }
    });
  }

  destroy() {
    this.activityBar?.destroy();
    this._unsub?.();
  }

  _ensureSelectedModel() {
    const models = store.state.llmModels || [];
    if (!models.length) {
      this.selectedModelKey = "";
      return;
    }
    const readyModels = models.filter((m) => m.ready || m.has_api_key);
    const currentModel = models.find((m) => `${m.provider_id}::${m.model}` === this.selectedModelKey);
    if (!currentModel || (readyModels.length > 0 && !(currentModel.ready || currentModel.has_api_key))) {
      const pool = readyModels.length ? readyModels : models;
      const def = pool.find((m) => m.is_default) || pool[0];
      this.selectedModelKey = `${def.provider_id}::${def.model}`;
    }
  }

  _parseSelectedModel() {
    this._ensureSelectedModel();
    if (!this.selectedModelKey || !this.selectedModelKey.includes("::")) {
      return { providerId: "", model: "" };
    }
    const [providerId, ...rest] = this.selectedModelKey.split("::");
    return { providerId, model: rest.join("::") };
  }

  _getCurrentConnProfile() {
    if (this.workMode !== "connection" || !this.connectionId) return null;
    return (store.state.connections || []).find((c) => c.connection_id === this.connectionId) || null;
  }

  async mount(container, context = null, refreshHeader = null) {
    this.refreshHeader = refreshHeader;
    if (context?.connectionId) {
      this.workMode = "connection";
      this.connectionId = context.connectionId;
      const prof = this._getCurrentConnProfile();
      if (prof) {
        this.dialect = prof.db_type;
        this.schemaName = prof.default_schema || "";
      }
    }
    if (context?.dialect) {
      this.dialect = String(context.dialect).toUpperCase();
      if (!context.connectionId) {
        this.workMode = "dialect";
        this.connectionId = "";
      }
    }
    if (context?.prompt) {
      this.draftPrompt = String(context.prompt);
    }

    clearEl(container, this.root);
    this._ensureSelectedModel();
    this.render();
    this.refreshHeader?.();

    await this._bootstrapRemoteData();
  }

  async _bootstrapRemoteData() {
    try {
      const [dlRes, sessRes] = await Promise.all([
        assistantApi.getDialects(),
        assistantApi.listSessions(),
      ]);
      if (Array.isArray(dlRes.dialects) && dlRes.dialects.length) {
        this.dialects = dlRes.dialects;
      }
      if (dlRes.attachment_limits && typeof dlRes.attachment_limits === "object") {
        this.attachmentLimits = { ...DEFAULT_ATTACHMENT_LIMITS, ...dlRes.attachment_limits };
      }
      if (Array.isArray(dlRes.llm_models) && dlRes.llm_models.length) {
        store.updateLlmState(null, dlRes.llm_models);
      }
      this.sessions = sessRes.sessions || [];
      this.render();
      this.refreshHeader?.();
    } catch (_err) {
      // 保留本地兜底列表
    }
  }

  getHeaderParts() {
    this._ensureSelectedModel();
    const prof = this._getCurrentConnProfile();
    const conns = store.state.connections || [];
    const models = store.state.llmModels || [];

    const scopeVal = this.workMode === "connection" && prof
      ? `conn::${prof.connection_id}`
      : `dialect::${this.dialect || "DWS"}`;

    const scopeSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "切换数据源或纯方言模式",
        style: { maxWidth: "230px" },
        disabled: this.loading,
        onChange: (e) => this._handleScopeChange(e.target.value),
      },
      conns.length
        ? el(
            "optgroup",
            { label: "在线连库模式（实时查库 + 受控执行）" },
            conns.map((c) => {
              const v = `conn::${c.connection_id}`;
              const opt = el("option", { value: v, text: `${c.name} [${c.db_type}]` });
              if (v === scopeVal) opt.selected = true;
              return opt;
            })
          )
        : null,
      el(
        "optgroup",
        { label: "离线纯方言模式（知识库 + 语法排障）" },
        this.dialects.map((d) => {
          const v = `dialect::${d.code}`;
          const opt = el("option", { value: v, text: `纯方言 · ${d.code}` });
          if (v === scopeVal) opt.selected = true;
          return opt;
        })
      )
    );

    const statusBadge = prof
      ? statusDot(`在线 · ${prof.db_type}`, "success")
      : statusDot(`纯方言 · ${this.dialect}`, "info");

    store.setAppSummary("ai_assistant", prof ? `${prof.name} · ${prof.db_type}` : `${this.dialect} 智能助手`);

    const modelSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "选择 AI 模型",
        style: { maxWidth: "185px" },
        disabled: models.length === 0 || this.loading,
        onChange: (e) => {
          this.selectedModelKey = e.target.value;
        },
      },
      models.length
        ? models.map((m) => {
            const val = `${m.provider_id}::${m.model}`;
            const opt = el("option", { value: val, text: `${m.model} (${m.provider_name})` });
            if (val === this.selectedModelKey) opt.selected = true;
            return opt;
          })
        : [el("option", { value: "", text: "未配置 AI 模型" })]
    );

    const stepsSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "自主探索步数上限",
        disabled: this.loading,
        onChange: (e) => {
          this.maxSteps = Number(e.target.value) || 6;
        },
      },
      [3, 6, 10].map((n) => {
        const opt = el("option", { value: String(n), text: `最多 ${n} 步` });
        if (this.maxSteps === n) opt.selected = true;
        return opt;
      })
    );

    return {
      center: el(
        "div",
        { style: { display: "inline-flex", alignItems: "center", gap: "8px" } },
        statusBadge,
        scopeSelect
      ),
      right: el(
        "div",
        { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
        modelSelect,
        stepsSelect,
        el(
          "button",
          {
            type: "button",
            className: `mac-btn mac-btn-sm ${this.sidebarOpen ? "is-active" : ""}`,
            title: "展开/折叠会话列表",
            onClick: () => {
              this.sidebarOpen = !this.sidebarOpen;
              this.render();
            },
          },
          svgIcon("sidebar", 13)
        ),
        el(
          "button",
          {
            type: "button",
            className: `mac-btn mac-btn-sm ${this.drawerOpen ? "mac-btn-primary" : ""}`,
            onClick: () => {
              this.drawerOpen = !this.drawerOpen;
              this.render();
              this.refreshHeader?.();
            },
          },
          svgIcon("book", 12),
          "库表 · 智库"
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm",
            disabled: this.loading,
            onClick: () => this.startNewSession(),
          },
          svgIcon("plus", 12),
          "新对话"
        )
      ),
    };
  }

  _handleScopeChange(val) {
    if (!val) return;
    if (val.startsWith("conn::")) {
      const cid = val.slice("conn::".length);
      const prof = (store.state.connections || []).find((c) => c.connection_id === cid);
      this.workMode = "connection";
      this.connectionId = cid;
      if (prof) {
        this.dialect = prof.db_type;
        this.schemaName = prof.default_schema || "";
      }
      this.focusTables = [];
    } else if (val.startsWith("dialect::")) {
      this.workMode = "dialect";
      this.connectionId = "";
      this.dialect = val.slice("dialect::".length);
      this.schemaName = "";
      this.focusTables = [];
    }
    this.render();
    this.refreshHeader?.();
  }

  _toggleFocusTable(fqTable) {
    const low = fqTable.toLowerCase();
    if (this.focusTables.some((x) => x.toLowerCase() === low)) {
      this.focusTables = this.focusTables.filter((x) => x.toLowerCase() !== low);
    } else {
      this.focusTables = [...this.focusTables, fqTable];
    }
    this.render();
  }

  startNewSession() {
    this.currentSession = null;
    this.taskMode = "auto";
    this.focusTables = [];
    this.pendingAttachments = [];
    this.largePayloadStrategy = "auto";
    this.render();
    this.refreshHeader?.();
  }

  async selectSession(sessionId) {
    if (!sessionId) {
      this.startNewSession();
      return;
    }
    try {
      const res = await assistantApi.getSession(sessionId);
      const sess = res.session;
      if (!sess) return;
      this.currentSession = sess;
      this.workMode = sess.work_mode || (sess.connection_id ? "connection" : "dialect");
      this.connectionId = sess.connection_id || "";
      this.dialect = sess.dialect || "DWS";
      this.schemaName = sess.schema_name || "";
      this.focusTables = [...(sess.focus_tables || [])];
      this.taskMode = sess.task_mode || "auto";
      this.pendingAttachments = [];
      this.largePayloadStrategy = "auto";
      this.render();
      this.refreshHeader?.();
    } catch (err) {
      Toast.error(err.message || "加载会话详情失败");
    }
  }

  async removeSession(sessionId, title) {
    const ok = await Modal.confirm({
      title: "删除对话记录",
      message: `确定永久删除会话「${title || "未命名对话"}」及其全部提案与执行记录吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await assistantApi.deleteSession(sessionId);
      this.sessions = res.sessions || [];
      if (this.currentSession?.session_id === sessionId) {
        this.currentSession = null;
      }
      Toast.success("会话已删除");
      this.render();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _validateLocalAttachmentFile(file) {
    const name = String(file?.name || "").trim();
    if (!name) {
      Toast.error("请选择有效的文件");
      return false;
    }
    const ext = name.includes(".") ? `.${name.split(".").pop().toLowerCase()}` : "";
    const allowed = this.attachmentLimits.allowed_extensions || DEFAULT_ATTACHMENT_LIMITS.allowed_extensions;
    if (!allowed.includes(ext)) {
      Toast.error(`暂不支持文件类型 ${ext || "未知"}，仅支持 .txt/.sql/.ddl/.md/.log/.json 及 .csv/.tsv`);
      return false;
    }
    const size = Number(file?.size || 0);
    if (size <= 0) {
      Toast.error(`文件「${name}」为空，无法上传`);
      return false;
    }
    const csvExts = this.attachmentLimits.csv_extensions || DEFAULT_ATTACHMENT_LIMITS.csv_extensions;
    const isCsv = csvExts.includes(ext);
    const maxBytes = isCsv
      ? Number(this.attachmentLimits.max_csv_bytes || DEFAULT_ATTACHMENT_LIMITS.max_csv_bytes)
      : Number(this.attachmentLimits.max_text_bytes || DEFAULT_ATTACHMENT_LIMITS.max_text_bytes);
    if (size > maxBytes) {
      Toast.error(
        `文件「${name}」体积 (${formatBytes(size)}) 超出 ${isCsv ? "CSV" : "文本/SQL"} 上限 (${formatBytes(maxBytes)})`
      );
      return false;
    }
    return true;
  }

  async handleUploadFiles(fileList) {
    const files = [...(fileList || [])].filter(Boolean);
    if (!files.length || this.uploadingAttachment || this.loading) return;
    const maxPerTurn = Number(this.attachmentLimits.max_attachments_per_turn || 5);
    const slots = maxPerTurn - this.pendingAttachments.length;
    if (slots <= 0) {
      Toast.info(`单轮最多附带 ${maxPerTurn} 个文件，请先发送或移除部分附件`);
      return;
    }
    const targets = files.slice(0, slots);
    if (files.length > slots) {
      Toast.info(`单轮最多附带 ${maxPerTurn} 个文件，本次仅处理前 ${slots} 个`);
    }

    this.uploadingAttachment = true;
    this.render();
    try {
      for (const file of targets) {
        if (!this._validateLocalAttachmentFile(file)) continue;
        const res = await assistantApi.uploadAttachment(
          file,
          this.currentSession?.session_id || ""
        );
        if (res?.attachment) {
          this.pendingAttachments = [...this.pendingAttachments, res.attachment];
          if (res.attachment.oversized) {
            Toast.info(
              `已装载大文件「${res.attachment.filename}」（~${res.attachment.estimated_tokens} tokens · 切分 ${res.attachment.chunk_count} 片），发送时将自动防爆窗拆解`
            );
          } else {
            Toast.success(`已附加「${res.attachment.filename}」`);
          }
        }
      }
    } catch (err) {
      Toast.error(err.message || "附件上传解析失败");
    } finally {
      this.uploadingAttachment = false;
      this.render();
    }
  }

  async handlePastedLongText(rawText, { filename = "", fileHint = "auto" } = {}) {
    const content = String(rawText || "");
    if (!content.trim() || this.uploadingAttachment || this.loading) return;
    const maxPerTurn = Number(this.attachmentLimits.max_attachments_per_turn || 5);
    if (this.pendingAttachments.length >= maxPerTurn) {
      Toast.info(`单轮最多附带 ${maxPerTurn} 个附件，请先发送或移除部分附件`);
      return;
    }
    const maxBytes = Number(this.attachmentLimits.max_text_bytes || DEFAULT_ATTACHMENT_LIMITS.max_text_bytes);
    const estBytes =
      typeof TextEncoder !== "undefined" ? new TextEncoder().encode(content).length : content.length * 3;
    if (estBytes > maxBytes) {
      Toast.error(`粘贴文本超出上限 (${formatBytes(maxBytes)})，请拆分为较小片段后粘贴`);
      return;
    }

    this.uploadingAttachment = true;
    this.render();
    try {
      const res = await assistantApi.createPastedAttachment({
        content,
        filename,
        file_hint: fileHint,
        session_id: this.currentSession?.session_id || "",
      });
      if (res?.attachment) {
        this.pendingAttachments = [...this.pendingAttachments, res.attachment];
        if (res.attachment.oversized) {
          Toast.info(
            `已将超长粘贴内容转为附件「${res.attachment.filename}」（~${res.attachment.estimated_tokens} tokens · ${res.attachment.chunk_count} 片）`
          );
        } else {
          Toast.success(`已将粘贴内容转为附件「${res.attachment.filename}」`);
        }
      }
    } catch (err) {
      Toast.error(err.message || "处理粘贴文本失败");
    } finally {
      this.uploadingAttachment = false;
      this.render();
    }
  }

  async handlePasteEvent(e) {
    const cd = e?.clipboardData;
    if (!cd) return;
    const files = [];
    if (cd.files && cd.files.length) {
      for (const f of cd.files) files.push(f);
    } else if (cd.items && cd.items.length) {
      for (const it of cd.items) {
        if (it.kind === "file") {
          const f = typeof it.getAsFile === "function" ? it.getAsFile() : null;
          if (f) files.push(f);
        }
      }
    }
    if (files.length > 0) {
      e.preventDefault?.();
      await this.handleUploadFiles(files);
      return;
    }

    const plain = typeof cd.getData === "function" ? cd.getData("text/plain") : "";
    if (!plain) return;
    const lineCount = plain.split(/\r?\n/).length;
    if (plain.length >= PASTE_TO_ATTACHMENT_MIN_CHARS || lineCount >= PASTE_TO_ATTACHMENT_MIN_LINES) {
      e.preventDefault?.();
      await this.handlePastedLongText(plain);
    }
  }

  async removePendingAttachment(attachmentId) {
    this.pendingAttachments = this.pendingAttachments.filter((a) => a.attachment_id !== attachmentId);
    this.render();
    try {
      await assistantApi.deleteAttachment(attachmentId);
    } catch (_e) {
      // 忽略已清理异常
    }
  }

  async openAttachmentPreviewModal(attSummary) {
    if (!attSummary?.attachment_id) return;
    let detail = attSummary;
    if (!Array.isArray(detail.chunks) || (detail.file_kind === "csv" && !detail.csv_profile?.columns)) {
      try {
        const res = await assistantApi.getAttachment(attSummary.attachment_id, true);
        if (res?.attachment) detail = res.attachment;
      } catch (err) {
        Toast.error(err.message || "加载附件预览失败");
        return;
      }
    }

    const metaRow = el(
      "div",
      { style: { display: "flex", gap: "6px", flexWrap: "wrap", alignItems: "center" } },
      badge(String(detail.file_kind || "text").toUpperCase(), "info"),
      badge(formatBytes(detail.size_bytes || 0), "neutral"),
      badge(`${detail.line_count || 0} 行`, "neutral"),
      detail.file_kind === "csv" ? badge(`${detail.row_count || 0} 数据行`, "success") : null,
      badge(`~${detail.estimated_tokens || 0} tokens`, detail.oversized ? "warn" : "neutral"),
      badge(`共 ${detail.chunk_count || 0} 个分片`, "neutral")
    );

    const blocks = [metaRow];
    const prof = detail.csv_profile || {};
    if (detail.file_kind === "csv" && Array.isArray(prof.columns) && prof.columns.length) {
      const schemaBox = el(
        "div",
        { className: "mac-group-box", style: { padding: "10px 12px" } },
        el("div", { style: { fontWeight: "600", fontSize: "12.5px", marginBottom: "6px" }, text: `CSV 字段画像 (${prof.row_count || 0} 行 × ${prof.column_count || 0} 列 · 表名 csv_data)` }),
        el(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "4px", maxHeight: "180px", overflowY: "auto" } },
          prof.columns.map((c) => {
            const statParts = [`非空 ${c.non_null_count}`, `唯一 ${c.distinct_count}`];
            if (c.min !== undefined && c.max !== undefined) {
              statParts.push(`范围 ${c.min}..${c.max}`, `均值 ${c.mean}`);
            }
            return el(
              "div",
              { className: "ai-asst-col-row" },
              el("span", { className: "mono", style: { fontWeight: "600" }, text: `${c.name} (${c.inferred_type})` }),
              el("span", { className: "muted", text: statParts.join(" · ") })
            );
          })
        )
      );
      blocks.push(schemaBox);

      if (Array.isArray(prof.sample_rows) && prof.sample_rows.length) {
        const cols = prof.columns.map((c, idx) => ({ key: `c_${idx}`, label: c.name, sortable: false }));
        const rows = prof.sample_rows.map((arr) => {
          const obj = {};
          cols.forEach((c, idx) => {
            obj[c.key] = Array.isArray(arr) ? arr[idx] : "";
          });
          return obj;
        });
        const sampleGrid = new DataGrid({ columns: cols, rows, maxHeight: "170px" });
        blocks.push(sampleGrid.root);
      }
    }

    const chunks = Array.isArray(detail.chunks) ? detail.chunks : [];
    if (chunks.length) {
      blocks.push(
        el(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "8px", maxHeight: "300px", overflowY: "auto" } },
          chunks.slice(0, 20).map((ch) =>
            el(
              "div",
              { className: "mac-group-box", style: { padding: "8px 10px" } },
              el(
                "div",
                { style: { display: "flex", justifyContent: "space-between", gap: "8px", marginBottom: "4px" } },
                el("strong", { style: { fontSize: "12px" }, text: `#${Number(ch.chunk_index) + 1} ${ch.title || ""}` }),
                el("span", { className: "muted mono", style: { fontSize: "11px" }, text: `${ch.range_label || ""} (~${ch.estimated_tokens || 0} tok)` })
              ),
              ch.content
                ? el("pre", {
                    className: "mono",
                    style: { margin: 0, whiteSpace: "pre-wrap", fontSize: "11.5px", maxHeight: "140px", overflowY: "auto" },
                    text: String(ch.content).slice(0, 1200) + (String(ch.content).length > 1200 ? "\n...（预览已截断）" : ""),
                  })
                : null
            )
          )
        )
      );
    }

    Modal.open({
      title: `附件预览 · ${detail.filename}`,
      width: "min(760px, 94vw)",
      body: el("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } }, ...blocks),
      footer: [el("button", { type: "button", className: "mac-btn mac-btn-primary", text: "关闭", onClick: () => Modal.close("ok") })],
    });
  }

  async sendUserMessage(
    overrideText = null,
    {
      clarificationReply = null,
      taskModeOverride = null,
      attachmentIdsOverride = null,
      batchStartChunk = 0,
      batchMaxChunks = null,
      strategyOverride = null,
    } = {}
  ) {
    if (this.loading || this.uploadingAttachment) return;
    const text = (overrideText !== null ? String(overrideText) : this.draftPrompt).trim();
    const useOverrideAttach = Array.isArray(attachmentIdsOverride);
    const sentPendingSnapshot = useOverrideAttach ? [] : [...this.pendingAttachments];
    const effAttachIds = useOverrideAttach
      ? attachmentIdsOverride
      : sentPendingSnapshot.map((a) => a.attachment_id);

    if (!text && !effAttachIds.length) {
      Toast.info("请输入要咨询或执行的数据库问题，或先粘贴/上传文本、SQL 或 CSV 文件");
      return;
    }
    const { providerId, model } = this._parseSelectedModel();
    if (!providerId && !model) {
      Toast.info("请先在「系统配置 → AI 模型」中配置至少一个可用模型");
      return;
    }

    const effTaskMode = taskModeOverride || this.taskMode || "auto";
    if (overrideText === null) {
      this.draftPrompt = "";
    }
    if (!useOverrideAttach && sentPendingSnapshot.length) {
      this.pendingAttachments = [];
    }
    const effStrategy = strategyOverride || this.largePayloadStrategy || "auto";
    this._pendingUserAttachments = sentPendingSnapshot;
    this._pendingUserPrompt =
      text || `请结合附带的 ${effAttachIds.length} 份文件进行分析与诊断。`;
    this.loading = true;
    this.render();

    try {
      const payload = {
        session_id: this.currentSession?.session_id || null,
        prompt: text,
        work_mode: this.workMode,
        connection_id: this.workMode === "connection" ? this.connectionId : "",
        dialect: this.dialect,
        schema_name: this.schemaName,
        focus_tables: this.focusTables,
        task_mode: effTaskMode,
        provider_id: providerId,
        model,
        max_steps: this.maxSteps,
        sample_limit: this.sampleLimit,
        clarification_reply: clarificationReply,
        attachment_ids: effAttachIds,
        large_payload_strategy: effStrategy,
        batch_start_chunk: Number(batchStartChunk || 0),
      };
      if (batchMaxChunks !== null && batchMaxChunks !== undefined) {
        payload.batch_max_chunks = Number(batchMaxChunks);
      }

      const res = await this.activityBar.track(
        "Data.O.G 智能分析探索",
        async ({ taskId, signal }) => {
          const out = await assistantApi.sendChatTurn(payload, { taskId, signal });
          const stepCnt = out?.assistant_message?.steps?.length ?? 0;
          return {
            ...out,
            message: stepCnt > 0 ? `已完成分析（共 ${stepCnt} 步探索）` : "已完成智能分析",
          };
        },
        { prefix: "asst", cancellable: true }
      );

      this.currentSession = res.session || this.currentSession;
      if (res.assistant_message?.id && (res.assistant_message.steps || []).length > 0) {
        this.expandedStepsMsgIds.add(res.assistant_message.id);
      }
      const sessRes = await assistantApi.listSessions();
      this.sessions = sessRes.sessions || this.sessions;
    } catch (err) {
      if (overrideText === null && !this.draftPrompt) {
        this.draftPrompt = text;
      }
      if (!useOverrideAttach && sentPendingSnapshot.length && !this.pendingAttachments.length) {
        this.pendingAttachments = sentPendingSnapshot;
      }
      if (isAbortError(err)) {
        Toast.info("已取消本次分析");
      } else {
        Toast.error(err.message || "智能分析请求失败");
      }
    } finally {
      this._pendingUserAttachments = [];
      this._pendingUserPrompt = "";
      this.loading = false;
      this.render();
      this.refreshHeader?.();
      this._scrollToBottom();
    }
  }

  _scrollToBottom() {
    const stream = this.root.querySelector(".ai-asst-stream");
    if (stream) {
      stream.scrollTop = stream.scrollHeight;
    }
  }

  _renderSidebar() {
    if (!this.sidebarOpen) return null;
    const kw = this.sessionSearch.trim().toLowerCase();
    const list = kw
      ? this.sessions.filter((s) => `${s.title} ${s.dialect}`.toLowerCase().includes(kw))
      : this.sessions;

    return el(
      "aside",
      { className: "mac-sidebar ai-asst-sidebar" },
      el(
        "div",
        { className: "ai-asst-sidebar-top" },
        el("input", {
          className: "mac-input mac-input-sm",
          placeholder: "搜索历史会话...",
          value: this.sessionSearch,
          onInput: (e) => {
            this.sessionSearch = e.target.value;
            this.render();
          },
        }),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm mac-btn-primary",
            style: { width: "100%", justifyContent: "center" },
            disabled: this.loading,
            onClick: () => this.startNewSession(),
          },
          svgIcon("plus", 12),
          "新建智能对话"
        )
      ),
      el(
        "div",
        { className: "ai-asst-session-list" },
        list.length
          ? list.map((s) => {
              const active = this.currentSession?.session_id === s.session_id;
              return el(
                "div",
                { className: `ai-asst-session-item ${active ? "is-active" : ""}` },
                el(
                  "button",
                  {
                    type: "button",
                    className: "ai-asst-session-btn",
                    onClick: () => this.selectSession(s.session_id),
                  },
                  el("div", { className: "ai-asst-session-title", text: s.title || "新对话" }),
                  el(
                    "div",
                    { className: "ai-asst-session-meta" },
                    badge(s.dialect || "SQL", s.work_mode === "connection" ? "info" : "neutral"),
                    el("span", { className: "muted", text: `${s.turn_count || 0} 轮` })
                  )
                ),
                el(
                  "button",
                  {
                    type: "button",
                    className: "ai-asst-session-del",
                    title: "删除会话",
                    onClick: (e) => {
                      e.stopPropagation();
                      this.removeSession(s.session_id, s.title);
                    },
                  },
                  svgIcon("trash", 12)
                )
              );
            })
          : el("div", {
              className: "muted",
              style: { padding: "20px 10px", textAlign: "center", fontSize: "12px" },
              text: "暂无历史会话",
            })
      )
    );
  }

  _renderWelcomeHero() {
    const prof = this._getCurrentConnProfile();
    return el(
      "div",
      { className: "ai-asst-hero" },
      el(
        "div",
        { className: "ai-asst-hero-head" },
        el("div", { className: "ai-asst-hero-icon" }, svgIcon("sparkles", 24)),
        el(
          "div",
          {},
          el("h2", { style: { margin: "0", fontSize: "18px" }, text: "Data.O.G · 基于大模型的智能数据助手" }),
          el("p", {
            className: "muted",
            style: { margin: "4px 0 0", fontSize: "12.5px" },
            text: prof
              ? `当前已连接「${prof.name} (${prof.db_type})」，可自主探查元数据、查离线智库、跑只读 SQL/EXPLAIN；DDL 与写语句将展示卡片待您二次确认。`
              : `当前处于「离线纯方言模式 (${this.dialect})」，可按方言精准检索本地参考知识库，进行语法确认、报错排障与受控 DDL 生成。`,
          })
        )
      ),
      el(
        "div",
        { className: "ai-asst-starter-grid" },
        SCENARIO_STARTERS.map((st) =>
          el(
            "button",
            {
              type: "button",
              className: "ai-asst-starter-card",
              onClick: () => {
                this.taskMode = st.taskMode;
                this.draftPrompt = st.prompt;
                this.render();
                const ta = this.root.querySelector(".ai-asst-composer-input");
                ta?.focus();
              },
            },
            el("div", { className: "ai-asst-starter-title", text: st.title }),
            el("div", { className: "muted ai-asst-starter-desc", text: st.desc })
          )
        )
      )
    );
  }

  _renderResultTableBlock(resTable) {
    if (!resTable || !Array.isArray(resTable.columns) || !resTable.columns.length) return null;
    const cols = resTable.columns.map((c, idx) => ({
      key: `c_${idx}`,
      label: c.name || `col_${idx + 1}`,
      sortable: true,
    }));
    const rowObjs = (resTable.rows || []).map((arr) => {
      const obj = {};
      cols.forEach((c, idx) => {
        obj[c.key] = Array.isArray(arr) ? arr[idx] : arr?.[c.label];
      });
      return obj;
    });

    const grid = new DataGrid({
      columns: cols,
      rows: rowObjs,
      maxHeight: "240px",
    });

    return el(
      "div",
      { className: "ai-asst-result-box" },
      el(
        "div",
        { className: "ai-asst-result-head" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
          el("strong", { style: { fontSize: "12px" }, text: "只读采样结果" }),
          badge(`${resTable.row_count || rowObjs.length} 行${resTable.truncated ? "（已截断）" : ""}`, "info"),
          el("span", { className: "muted mono", style: { fontSize: "11px" }, text: `${resTable.elapsed_ms || 0} ms` })
        ),
        el(
          "div",
          { style: { display: "flex", gap: "6px" } },
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "导出 CSV",
            onClick: () =>
              assistantApi
                .exportResult({
                  columns: resTable.columns.map((c) => c.name),
                  rows: resTable.rows || [],
                  fmt: "csv",
                  title: "DataOG_Assistant_Query",
                })
                .catch((err) => Toast.error(err.message)),
          }),
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "导出 Excel",
            onClick: () =>
              assistantApi
                .exportResult({
                  columns: resTable.columns.map((c) => c.name),
                  rows: resTable.rows || [],
                  fmt: "xlsx",
                  title: "DataOG_Assistant_Query",
                })
                .catch((err) => Toast.error(err.message)),
          })
        )
      ),
      grid.root
    );
  }

  _renderMessageItem(msg, { isLastAssistant = false } = {}) {
    if (msg.role === "user") {
      const msgAtts = Array.isArray(msg.attachments) ? msg.attachments : [];
      const attachPills = msgAtts.length
        ? el(
            "div",
            { className: "ai-asst-msg-attachments" },
            msgAtts.map((att) =>
              el(
                "button",
                {
                  type: "button",
                  className: "ai-asst-msg-attach-pill",
                  title: att.summary || `${att.filename} (点击预览分片/画像)`,
                  onClick: () => this.openAttachmentPreviewModal(att),
                },
                badge(String(att.file_kind || "TXT").toUpperCase(), "info"),
                el("span", { text: att.filename }),
                el("span", {
                  className: "muted mono",
                  style: { fontSize: "10.5px" },
                  text: `~${att.estimated_tokens || 0} tok`,
                })
              )
            )
          )
        : null;
      return el(
        "div",
        { className: "ai-asst-msg ai-asst-msg-user" },
        el(
          "div",
          { className: "ai-asst-user-bubble" },
          attachPills,
          el("div", { style: { whiteSpace: "pre-wrap", wordBreak: "break-word" }, text: msg.content })
        )
      );
    }

    const steps = msg.steps || [];
    const stepsExpanded = this.expandedStepsMsgIds.has(msg.id);

    const traceSection = steps.length
      ? el(
          "div",
          { className: "ai-asst-trace-box" },
          el(
            "button",
            {
              type: "button",
              className: "ai-asst-trace-toggle",
              onClick: () => {
                if (stepsExpanded) this.expandedStepsMsgIds.delete(msg.id);
                else this.expandedStepsMsgIds.add(msg.id);
                this.render();
              },
            },
            statusDot(`自主探索 ${steps.length} 步`, "success"),
            el("span", { className: "muted", style: { fontSize: "11.5px" }, text: `${msg.total_elapsed_ms || 0} ms` }),
            el("span", { style: { marginLeft: "auto", fontSize: "11.5px" }, text: stepsExpanded ? "收起轨迹" : "展开轨迹" })
          ),
          stepsExpanded
            ? el(
                "div",
                { className: "ai-asst-trace-list" },
                steps.map((st) =>
                  el(
                    "div",
                    { className: `ai-asst-trace-step ${st.ok ? "is-ok" : "is-warn"}` },
                    el(
                      "div",
                      { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
                      badge(`#${st.step} ${st.tool}`, st.ok ? "info" : "warn"),
                      el("span", { style: { fontWeight: "600", fontSize: "12px" }, text: st.purpose || st.tool }),
                      el("span", { className: "muted mono", style: { marginLeft: "auto", fontSize: "11px" }, text: `${st.elapsed_ms} ms` })
                    ),
                    st.summary
                      ? el("div", { className: "muted", style: { fontSize: "11.5px", marginTop: "3px" }, text: st.summary })
                      : null
                  )
                )
              )
            : null
        )
      : null;

    const citations = msg.citations || [];
    const citationsSection = citations.length
      ? el(
          "div",
          { className: "ai-asst-citations" },
          el("span", { className: "muted", style: { fontSize: "11.5px" }, text: "知识库出处:" }),
          citations.map((c) =>
            el(
              "button",
              {
                type: "button",
                className: "ai-asst-citation-chip",
                title: c.snippet || c.heading_path,
                onClick: () => {
                  this.drawerOpen = true;
                  this.drawer.openCitation(c.chunk_id);
                  this.render();
                  this.refreshHeader?.();
                },
              },
              svgIcon("book", 11),
              `${c.doc_title} · ${c.heading_path}`
            )
          )
        )
      : null;

    // 澄清确认卡
    const clar = msg.clarification || {};
    let clarSection = null;
    if (msg.response_type === "clarify" && clar.question && Array.isArray(clar.options) && clar.options.length) {
      let noteText = "";
      clarSection = el(
        "div",
        { className: "ai-asst-clarify-card" },
        el("div", { style: { fontSize: "12.5px", fontWeight: "600", marginBottom: "8px" }, text: "请点选确认方向（点击后立即继续深入）：" }),
        el(
          "div",
          { className: "ai-asst-clarify-options" },
          clar.options.map((op) =>
            el(
              "button",
              {
                type: "button",
                className: "ai-asst-clarify-btn",
                disabled: this.loading,
                onClick: () =>
                  this.sendUserMessage(op.label, {
                    clarificationReply: { ...op, note: noteText },
                  }),
              },
              el("strong", { style: { fontSize: "12.5px" }, text: op.label }),
              op.detail ? el("span", { className: "muted", style: { fontSize: "11.5px" }, text: op.detail }) : null
            )
          )
        ),
        el("input", {
          className: "mac-input mac-input-sm",
          style: { marginTop: "8px" },
          placeholder: "可选：补充说明条件后再点击上方选项...",
          onInput: (e) => {
            noteText = e.target.value;
          },
        })
      );
    }

    // 写操作 / DDL 分级确认卡
    const propCard = msg.proposal
      ? renderProposalCard(msg.proposal, {
          activeConnectionId: this.connectionId,
          onProposalUpdated: (res) => {
            if (res?.session) {
              this.currentSession = res.session;
            }
            const refreshCid = res?.proposal?.connection_id || this.connectionId;
            if (res?.catalog_refreshed && refreshCid) {
              platformApiRefreshHelper(refreshCid);
            }
            this.render();
            this._scrollToBottom();
          },
          onDiagnoseFailure: ({ failedSql, errorText }) => {
            this.sendUserMessage(
              `刚才确认执行的 SQL 执行失败，请结合表结构与当前方言知识库帮我诊断原因并修复后重新生成提案：\n\n失败 SQL：\n\`\`\`sql\n${failedSql}\n\`\`\`\n\n数据库报错：${errorText}`,
              { taskModeOverride: "troubleshoot" }
            );
          },
        })
      : null;

    const resultTableSection = this._renderResultTableBlock(msg.result_table);

    const nextSteps = isLastAssistant ? msg.next_steps || [] : [];
    const nextStepsSection = nextSteps.length
      ? el(
          "div",
          { className: "ai-asst-next-steps" },
          el("span", { className: "muted", style: { fontSize: "11.5px" }, text: "下一步建议:" }),
          nextSteps.map((ns) =>
            el(
              "button",
              {
                type: "button",
                className: "ai-asst-next-chip",
                disabled: this.loading,
                onClick: () =>
                  this.sendUserMessage(ns.prompt, {
                    attachmentIdsOverride: Array.isArray(ns.attachment_ids) ? ns.attachment_ids : null,
                    batchStartChunk: Number(ns.batch_start_chunk || 0),
                    batchMaxChunks: ns.batch_max_chunks ? Number(ns.batch_max_chunks) : null,
                    strategyOverride: ns.large_payload_strategy || null,
                  }),
              },
              ns.label
            )
          )
        )
      : null;

    return el(
      "div",
      { className: "ai-asst-msg ai-asst-msg-assistant" },
      el(
        "div",
        { className: "ai-asst-assistant-card" },
        traceSection,
        msg.content ? renderSafeMarkdown(msg.content) : null,
        clarSection,
        propCard,
        resultTableSection,
        citationsSection,
        nextStepsSection,
        msg.model
          ? el(
              "div",
              { className: "ai-asst-msg-footer muted" },
              el("span", { text: `${msg.provider_name || "AI"} · ${msg.model}` }),
              el("span", { text: msg.created_at || "" })
            )
          : null
      )
    );
  }

  render() {
    this.drawer.setScope({
      connectionId: this.workMode === "connection" ? this.connectionId : "",
      dialect: this.dialect,
      focusTables: this.focusTables,
    });

    const msgs = this.currentSession?.messages || [];
    let lastAsstIdx = -1;
    for (let k = msgs.length - 1; k >= 0; k -= 1) {
      if (msgs[k].role === "assistant") {
        lastAsstIdx = k;
        break;
      }
    }

    const modeChips = el(
      "div",
      { className: "ai-asst-mode-bar" },
      el(
        "div",
        { className: "seg-group" },
        TASK_MODES.map((m) =>
          el("button", {
            type: "button",
            className: `seg-btn ${this.taskMode === m.id ? "is-active" : ""}`,
            text: m.label,
            onClick: () => {
              this.taskMode = m.id;
              this.render();
            },
          })
        )
      ),
      this.workMode === "connection"
        ? el(
            "button",
            {
              type: "button",
              className: "mac-btn mac-btn-sm",
              onClick: () => {
                this.drawerOpen = true;
                this.drawer.activeTab = "tables";
                this.render();
                this.refreshHeader?.();
              },
            },
            svgIcon("table", 12),
            this.focusTables.length ? `@焦点表 (${this.focusTables.length})` : "+ 选择 @焦点表"
          )
        : badge(`纯方言模式 (${this.dialect})`, "info")
    );

    const focusChipsRow =
      this.workMode === "connection" && this.focusTables.length
        ? el(
            "div",
            { className: "ai-asst-focus-bar" },
            el("span", { className: "muted", style: { fontSize: "11.5px" }, text: "已挂载焦点表:" }),
            this.focusTables.map((ft) =>
              el(
                "span",
                { className: "ai-asst-focus-chip mono" },
                el("span", { text: ft }),
                el("button", {
                  type: "button",
                  className: "ai-asst-focus-x",
                  text: "×",
                  onClick: () => this._toggleFocusTable(ft),
                })
              )
            )
          )
        : null;

    const pendingNodes = [];
    if (this.loading && this._pendingUserPrompt) {
      pendingNodes.push(
        this._renderMessageItem({
          role: "user",
          content: this._pendingUserPrompt,
          attachments: this._pendingUserAttachments || [],
        }),
        el(
          "div",
          { className: "ai-asst-msg ai-asst-msg-assistant" },
          el(
            "div",
            { className: "ai-asst-assistant-card" },
            statusDot("正在连接模型与执行自主探索...", "info")
          )
        )
      );
    }

    const stream = el(
      "div",
      { className: "ai-asst-stream" },
      msgs.length
        ? msgs.map((m, idx) => this._renderMessageItem(m, { isLastAssistant: idx === lastAsstIdx })).concat(pendingNodes)
        : pendingNodes.length
        ? pendingNodes
        : this._renderWelcomeHero()
    );

    const totalPendingTokens = this.pendingAttachments.reduce(
      (sum, a) => sum + Number(a?.estimated_tokens || 0),
      0
    );
    const inlineBudget = Number(
      this.attachmentLimits.inline_token_budget || DEFAULT_ATTACHMENT_LIMITS.inline_token_budget
    );
    const needsChunkProtection =
      totalPendingTokens > inlineBudget || this.pendingAttachments.some((a) => Boolean(a?.oversized));

    const attachBar = this.pendingAttachments.length
      ? el(
          "div",
          { className: "ai-asst-attach-bar" },
          this.pendingAttachments.map((att) =>
            el(
              "div",
              { className: `ai-asst-attach-chip ${att.oversized ? "is-oversized" : ""}` },
              el(
                "button",
                {
                  type: "button",
                  className: "ai-asst-attach-chip-main",
                  title: att.summary || `${att.filename} (点击预览画像或分片)`,
                  onClick: () => this.openAttachmentPreviewModal(att),
                },
                badge(String(att.file_kind || "txt").toUpperCase(), att.oversized ? "warn" : "info"),
                el("span", { className: "ai-asst-attach-name", text: att.filename }),
                el("span", {
                  className: "muted mono",
                  style: { fontSize: "11px" },
                  text:
                    att.file_kind === "csv"
                      ? `${att.row_count || 0}行 · ~${att.estimated_tokens || 0} tok`
                      : `~${att.estimated_tokens || 0} tok · ${att.chunk_count || 1}片`,
                })
              ),
              el("button", {
                type: "button",
                className: "ai-asst-focus-x",
                title: "移除附件",
                "aria-label": `移除附件 ${att.filename}`,
                disabled: this.loading || this.uploadingAttachment,
                text: "×",
                onClick: () => this.removePendingAttachment(att.attachment_id),
              })
            )
          )
        )
      : null;

    const strategyBar = needsChunkProtection
      ? el(
          "div",
          { className: "ai-asst-strategy-bar" },
          statusDot(
            `防爆窗保护（~${totalPendingTokens} > ${inlineBudget} tokens）：`,
            "warn"
          ),
          el(
            "select",
            {
              className: "mac-input mac-input-sm",
              "aria-label": "大文件提交拆解策略",
              style: { maxWidth: "300px" },
              disabled: this.loading,
              onChange: (e) => {
                this.largePayloadStrategy = e.target.value;
              },
            },
            STRATEGY_OPTIONS.map((optItem) => {
              const opt = el("option", { value: optItem.id, text: optItem.label });
              if (optItem.id === this.largePayloadStrategy) opt.selected = true;
              return opt;
            })
          )
        )
      : null;

    const acceptExts = (
      this.attachmentLimits.allowed_extensions || DEFAULT_ATTACHMENT_LIMITS.allowed_extensions
    ).join(",");

    const composer = el(
      "div",
      {
        className: `ai-asst-composer-wrap ${this.isDraggingFile ? "is-dragover" : ""}`,
        onDragover: (e) => {
          e.preventDefault();
          if (!this.isDraggingFile) {
            this.isDraggingFile = true;
            composer.classList?.add("is-dragover");
          }
        },
        onDragleave: (e) => {
          if (!composer.contains?.(e.relatedTarget)) {
            this.isDraggingFile = false;
            composer.classList?.remove("is-dragover");
          }
        },
        onDrop: async (e) => {
          e.preventDefault();
          this.isDraggingFile = false;
          composer.classList?.remove("is-dragover");
          const files = e.dataTransfer?.files;
          if (files && files.length) {
            await this.handleUploadFiles(files);
          }
        },
      },
      attachBar,
      strategyBar,
      el("textarea", {
        className: "mac-input ai-asst-composer-input",
        rows: 3,
        value: this.draftPrompt,
        disabled: this.loading,
        placeholder:
          this.workMode === "connection"
            ? "输入需求，或直接粘贴/拖拽 .txt/.sql/.csv（长文本自动切片防爆窗，Enter 发送 · Shift+Enter 换行）..."
            : `输入 ${this.dialect} 问题，或粘贴/拖拽 .txt/.sql/.csv（长文本自动分步拆解）...`,
        onInput: (e) => {
          this.draftPrompt = e.target.value;
        },
        onPaste: (e) => this.handlePasteEvent(e),
        onKeydown: (e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            this.sendUserMessage();
          }
        },
      }),
      el(
        "div",
        { className: "ai-asst-composer-actions" },
        el(
          "button",
          {
            type: "button",
            className: "btn btn-xs",
            disabled: this.loading || this.uploadingAttachment,
            onClick: async () => {
              const picked = await pickLocalFile(acceptExts);
              if (picked) await this.handleUploadFiles([picked]);
            },
          },
          svgIcon("upload", 12),
          this.uploadingAttachment ? "解析分片中..." : "附加 .sql/.txt/.csv"
        ),
        el(
          "button",
          {
            type: "button",
            className: "btn btn-xs",
            onClick: () => {
              this.drawerOpen = true;
              this.drawer.activeTab = "knowledge";
              this.render();
              this.refreshHeader?.();
            },
          },
          svgIcon("book", 12),
          "上传 / 查阅参考知识库"
        ),
        el("span", { className: "muted", style: { fontSize: "11px", marginRight: "auto" }, text: "CSV ≤ 2MB / 文本 ≤ 5MB · 写操作需二次确认" }),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-primary",
            disabled: this.loading || this.uploadingAttachment,
            onClick: () => this.sendUserMessage(),
          },
          svgIcon("send", 13),
          this.loading ? "探索中..." : "发送"
        )
      )
    );

    const mainCol = el(
      "div",
      { className: "ai-asst-main" },
      modeChips,
      focusChipsRow,
      stream,
      this.activityBar.el,
      composer
    );

    clearEl(
      this.root,
      this._renderSidebar(),
      mainCol,
      this.drawerOpen ? this.drawer.root : null
    );
  }
}

async function platformApiRefreshHelper(connectionId) {
  try {
    const { platformApi } = await import("../../core/platform_api.js");
    const res = await platformApi.getMetadataTree(connectionId, false);
    if (res) store.updateCatalogTree(connectionId, res);
  } catch (_e) {
    // ignore refresh failures
  }
}
