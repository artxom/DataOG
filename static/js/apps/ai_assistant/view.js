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
import { platformApi } from "../../core/platform_api.js";
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
  inline_token_budget: 32000,
  csv_inline_token_budget: 8000,
  chunk_target_tokens: 12000,
  chunk_hard_max_tokens: 16000,
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
  { id: "auto", label: "智能规划（按模型甜蜜区 + SQL血缘图纸自动择优）" },
  { id: "goal_driven_staged", label: "🎯 甜蜜区分段闭环（轻量模型 / 超长脚本推荐）" },
  { id: "lineage_sliced", label: "血缘手术刀精准切片（按目标表上下游抽取）" },
  { id: "full_context", label: "强制全量直通（一次性完整送入当前模型）" },
  { id: "agentic_explore", label: "结构大纲 + 按行/正则精准探索（省 Token）" },
  { id: "stepped_batch", label: "强制逐片拆解分析（超大脚本兜底）" },
];

const RUN_MODES = [
  { id: "standard", label: "标准对话" },
  { id: "goal", label: "🎯 Goal 模式" },
];

const MODEL_TIER_LABELS = {
  flagship: "旗舰",
  standard: "标准",
  lightweight: "轻量",
};

const THINKING_MODE_OPTIONS = [
  { id: "auto", label: "思考: 自动" },
  { id: "enabled", label: "思考: 深度开启" },
  { id: "disabled", label: "思考: 快速关闭" },
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
    this.thinkingMode = "auto";
    this.runMode = "standard";
    this.probingModel = false;
    this.activeRun = null;
    this._activeRunAbort = null;
    this._lastRunEventId = 0;
    this._stopRequested = false;
    this.isDraggingFile = false;
    this.uploadingAttachment = false;
    this.expandedStepsMsgIds = new Set();
    this.expandedReasoningMsgIds = new Set();
    this._liveState = null;
    this._liveSessionId = "";
    this._streamRenderTimer = null;

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
    if (this._streamRenderTimer) {
      clearTimeout(this._streamRenderTimer);
      this._streamRenderTimer = null;
    }
    if (this._activeRunAbort) {
      try {
        this._activeRunAbort.abort();
      } catch (_e) {
        // 仅断开本地 SSE 监听，不取消后台持久化任务
      }
      this._activeRunAbort = null;
    }
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

  _getSelectedModelMeta() {
    const { providerId, model } = this._parseSelectedModel();
    if (!providerId && !model) return null;
    const models = store.state.llmModels || [];
    return (
      models.find((m) => m.provider_id === providerId && m.model === model) ||
      models.find((m) => m.provider_id === providerId) ||
      null
    );
  }

  _formatTokenCount(n) {
    const v = Number(n || 0);
    if (!Number.isFinite(v) || v <= 0) return "0";
    if (v >= 1000000) return `${(v / 1000000).toFixed(v % 1000000 === 0 ? 0 : 1)}M`;
    if (v >= 1000) return `${Math.round(v / 1000)}K`;
    return String(Math.round(v));
  }

  stopStreaming() {
    if (!this.loading) return;
    if (this._activeRunAbort && this.activeRun?.run_id) {
      this._stopRequested = true;
      const rid = this.activeRun.run_id;
      assistantApi.cancelRun(rid).catch(() => {});
      try {
        this._activeRunAbort.abort();
      } catch (_e) {
        // ignore abort error
      }
      return;
    }
    this.activityBar?.cancel?.();
  }

  async probeCurrentModel({ forceRefresh = true } = {}) {
    if (this.probingModel || this.loading) return null;
    const { providerId, model } = this._parseSelectedModel();
    if (!providerId && !model) {
      Toast.info("请先配置并选择一个可用 AI 模型");
      return null;
    }
    this.probingModel = true;
    this.render();
    try {
      const res = await platformApi.probeLlmModel({
        provider_id: providerId,
        model,
        force_refresh: Boolean(forceRefresh),
      });
      if (res?.providers && res?.available_models) {
        store.updateLlmState(res.providers, res.available_models);
      }
      Toast.success(res?.message || `模型 ${model} 能力探查已更新`);
      return res;
    } catch (err) {
      Toast.error(err.message || "模型能力探查失败");
      return null;
    } finally {
      this.probingModel = false;
      this.render();
      this.refreshHeader?.();
    }
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
        style: { maxWidth: "240px" },
        disabled: models.length === 0 || this.loading,
        onChange: (e) => {
          this.selectedModelKey = e.target.value;
          this.render();
        },
      },
      models.length
        ? models.map((m) => {
            const val = `${m.provider_id}::${m.model}`;
            const ctxLabel = m.context_window ? ` · ${this._formatTokenCount(m.context_window)}` : "";
            const thinkLabel = m.supports_thinking ? " · 深度思考" : "";
            const opt = el("option", {
              value: val,
              text: `${m.model} (${m.provider_name}${ctxLabel}${thinkLabel})`,
            });
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
    if (this._activeRunAbort) {
      try {
        this._activeRunAbort.abort();
      } catch (_e) {
        // detach previous run watcher
      }
      this._activeRunAbort = null;
    }
    this.activeRun = null;
    this._lastRunEventId = 0;
    this.currentSession = null;
    this.taskMode = "auto";
    this.thinkingMode = "auto";
    this._liveSessionId = "";
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
    if (this._activeRunAbort) {
      try {
        this._activeRunAbort.abort();
      } catch (_e) {
        // detach previous run watcher
      }
      this._activeRunAbort = null;
    }
    try {
      const res = await assistantApi.getSession(sessionId);
      const sess = res.session;
      if (!sess) return;
      this.currentSession = sess;
      this._liveSessionId = sess.session_id || "";
      this.workMode = sess.work_mode || (sess.connection_id ? "connection" : "dialect");
      this.connectionId = sess.connection_id || "";
      this.dialect = sess.dialect || "DWS";
      this.schemaName = sess.schema_name || "";
      this.focusTables = [...(sess.focus_tables || [])];
      this.taskMode = sess.task_mode || "auto";
      this.thinkingMode = sess.thinking_mode || "auto";
      this.pendingAttachments = [];
      this.largePayloadStrategy = "auto";
      this.activeRun = null;
      this._lastRunEventId = 0;

      try {
        const activeRes = await assistantApi.getSessionActiveRun(sessionId);
        if (activeRes?.run && activeRes.run.run_id) {
          this.activeRun = activeRes.run;
        }
      } catch (_e) {
        // 忽略旧版本或无活跃任务时的查询异常
      }

      this.render();
      this.refreshHeader?.();

      const activeStatuses = ["queued", "probing", "planning", "running", "verifying"];
      if (this.activeRun?.run_id && activeStatuses.includes(this.activeRun.status)) {
        this._attachToRunStream(this.activeRun.run_id, { afterEventId: 0 });
      }
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
        const ctxWin = Number(this._getSelectedModelMeta()?.context_window || 64000);
        const attBud = Number(
          this._getSelectedModelMeta()?.attachment_budget || Math.floor(ctxWin * 0.65)
        );
        const tok = Number(res.attachment.estimated_tokens || 0);
        if (res.attachment.file_kind !== "csv" && tok <= attBud && tok > 0) {
          Toast.success(
            `已将长文本转为附件「${res.attachment.filename}」（~${tok} tokens，支持当前模型 ${this._formatTokenCount(ctxWin)} 窗口直接全量分析）`
          );
        } else if (res.attachment.oversized) {
          Toast.info(
            `已将超长粘贴内容转为附件「${res.attachment.filename}」（~${tok} tokens · ${res.attachment.chunk_count} 片）`
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
    const outline = detail.outline && typeof detail.outline === "object" ? detail.outline : null;
    if (
      outline &&
      (outline.summary_line ||
        (outline.objects || []).length ||
        (outline.sections || []).length ||
        (outline.referenced_tables || []).length)
    ) {
      const objs = Array.isArray(outline.objects) ? outline.objects : [];
      const secs = Array.isArray(outline.sections) ? outline.sections : [];
      const refs = Array.isArray(outline.referenced_tables) ? outline.referenced_tables : [];
      blocks.push(
        el(
          "div",
          { className: "mac-group-box ai-asst-outline-box" },
          el(
            "div",
            { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px", flexWrap: "wrap" } },
            el("strong", { style: { fontSize: "12.5px" }, text: "结构化源码大纲（零 Token 索引）" }),
            outline.is_stored_procedure ? badge("存储过程 / PL-SQL", "info") : null,
            outline.summary_line
              ? el("span", { className: "muted", style: { fontSize: "11.5px" }, text: outline.summary_line })
              : null
          ),
          objs.length
            ? el(
                "div",
                { className: "ai-asst-outline-chips" },
                objs.map((o) => badge(`L${o.line || 1} ${o.kind || ""} ${o.name || ""}`.trim(), "neutral"))
              )
            : null,
          refs.length
            ? el(
                "div",
                { className: "ai-asst-outline-chips" },
                el("span", { className: "muted", style: { fontSize: "11px" }, text: "涉及表:" }),
                refs.slice(0, 18).map((tbl) => badge(tbl, "info"))
              )
            : null,
          secs.length
            ? el(
                "div",
                { className: "ai-asst-outline-list" },
                secs.slice(0, 30).map((sec) =>
                  el("div", { className: "mono muted", text: `L${sec.line || 1} · ${sec.title || ""}` })
                )
              )
            : null
        )
      );
    }

    const analysis = detail.analysis && typeof detail.analysis === "object" ? detail.analysis : null;
    if (analysis) {
      const linLines = Array.isArray(analysis.lineage_dag?.summary_lines)
        ? analysis.lineage_dag.summary_lines
        : [];
      const findings = Array.isArray(analysis.static_findings) ? analysis.static_findings : [];
      if (linLines.length || findings.length) {
        blocks.push(
          el(
            "div",
            { className: "mac-group-box ai-asst-outline-box" },
            el(
              "div",
              { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
              el("strong", { style: { fontSize: "12.5px" }, text: "SQL 静态血缘与隐患雷达（确定性预检）" }),
              badge(`${(analysis.statements || []).length} 条语句`, "info"),
              findings.length
                ? badge(
                    `${findings.length} 项静态线索`,
                    findings.some((f) => f.severity === "HIGH") ? "danger" : "warn"
                  )
                : badge("零高危静态隐患", "success")
            ),
            linLines.length
              ? el(
                  "div",
                  { className: "ai-asst-outline-list" },
                  linLines.slice(0, 12).map((ln) => el("div", { className: "mono muted", text: ln }))
                )
              : null,
            findings.length
              ? el(
                  "div",
                  { className: "ai-asst-outline-list" },
                  findings.slice(0, 15).map((f) =>
                    el(
                      "div",
                      { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
                      badge(
                        `${f.finding_id} ${f.severity}`,
                        f.severity === "HIGH" ? "danger" : f.severity === "MEDIUM" ? "warn" : "neutral"
                      ),
                      el("span", { className: "mono", text: `L${f.line || 1}` }),
                      el("span", { text: `${f.title || f.code}` })
                    )
                  )
                )
              : null
          )
        );
      }
    }

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
    const effRunMode = clarificationReply ? "standard" : (this.runMode || "standard");

    if (effRunMode === "goal") {
      return await this._startGoalRun(
        {
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
          thinking_mode: this.thinkingMode || "auto",
          max_steps: this.maxSteps,
          sample_limit: this.sampleLimit,
          attachment_ids: effAttachIds,
          large_payload_strategy: effStrategy,
          batch_start_chunk: Number(batchStartChunk || 0),
          batch_max_chunks:
            batchMaxChunks !== null && batchMaxChunks !== undefined
              ? Number(batchMaxChunks)
              : 8,
          run_mode: "goal",
          max_goal_steps: 25,
        },
        { text, overrideText, useOverrideAttach, sentPendingSnapshot }
      );
    }

    this._liveSessionId = this.currentSession?.session_id || "";
    this._liveState = {
      statusText: "正在连接模型...",
      thinking: "",
      thinkingStarted: false,
      thinkingOpen: true,
      thinkingDone: false,
      thinkingMs: 0,
      steps: [],
      content: "",
      plan: null,
      stages: [],
      progressPct: 0,
      retryNotice: null,
      verifyResult: null,
      checkpointSummary: null,
      runId: "",
      runStatus: "",
      refs: null,
    };
    this._pendingUserAttachments = sentPendingSnapshot;
    this._pendingUserPrompt =
      text || `请结合附带的 ${effAttachIds.length} 份文件进行分析与诊断。`;
    this.loading = true;
    this.render();
    this.refreshHeader?.();
    this._scrollToBottom();

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
        thinking_mode: this.thinkingMode || "auto",
        max_steps: this.maxSteps,
        sample_limit: this.sampleLimit,
        clarification_reply: clarificationReply,
        attachment_ids: effAttachIds,
        large_payload_strategy: effStrategy,
        batch_start_chunk: Number(batchStartChunk || 0),
        run_mode: "standard",
      };
      if (batchMaxChunks !== null && batchMaxChunks !== undefined) {
        payload.batch_max_chunks = Number(batchMaxChunks);
      }

      const res = await this.activityBar.track(
        "Data.O.G 智能分析探索",
        async ({ taskId, signal }) => {
          const out = await assistantApi.streamChatTurn(payload, {
            taskId,
            signal,
            onEvent: (evType, ev, frameId) => this._handleStreamEvent(evType, ev, frameId),
          });
          const rawRes = out?.fallbackJson || out || {};
          const stepCnt = rawRes?.assistant_message?.steps?.length ?? 0;
          return {
            ...rawRes,
            message: stepCnt > 0 ? `已完成分析（共 ${stepCnt} 步探索）` : "已完成智能分析",
          };
        },
        { prefix: "asst", cancellable: true }
      );

      this.currentSession = res.session || this.currentSession;
      if (this.currentSession?.session_id) {
        this._liveSessionId = this.currentSession.session_id;
      }
      if (res.assistant_message?.id && (res.assistant_message.steps || []).length > 0) {
        this.expandedStepsMsgIds.add(res.assistant_message.id);
      }
      const sessRes = await assistantApi.listSessions();
      this.sessions = sessRes.sessions || this.sessions;
    } catch (err) {
      if (isAbortError(err)) {
        Toast.info("已停止生成");
        let recoveredTurn = false;
        if (this._liveSessionId) {
          try {
            const [sessRes, listRes] = await Promise.all([
              assistantApi.getSession(this._liveSessionId),
              assistantApi.listSessions(),
            ]);
            if (sessRes?.session) {
              this.currentSession = sessRes.session;
              recoveredTurn = true;
            }
            if (listRes?.sessions) {
              this.sessions = listRes.sessions;
            }
          } catch (_e) {
            // 忽略停止生成后的会话刷新错误
          }
        }
        if (!recoveredTurn && overrideText === null && !this.draftPrompt) {
          this.draftPrompt = text;
        }
        if (!recoveredTurn && !useOverrideAttach && sentPendingSnapshot.length && !this.pendingAttachments.length) {
          this.pendingAttachments = sentPendingSnapshot;
        }
      } else {
        if (overrideText === null && !this.draftPrompt) {
          this.draftPrompt = text;
        }
        if (!useOverrideAttach && sentPendingSnapshot.length && !this.pendingAttachments.length) {
          this.pendingAttachments = sentPendingSnapshot;
        }
        Toast.error(err.message || "智能分析请求失败");
      }
    } finally {
      if (this._streamRenderTimer) {
        clearTimeout(this._streamRenderTimer);
        this._streamRenderTimer = null;
      }
      this._liveState = null;
      this._pendingUserAttachments = [];
      this._pendingUserPrompt = "";
      this.loading = false;
      this.render();
      this.refreshHeader?.();
      this._scrollToBottom();
    }
  }

  async _startGoalRun(
    payload,
    { text = "", overrideText = null, useOverrideAttach = false, sentPendingSnapshot = [] } = {}
  ) {
    this._pendingUserAttachments = sentPendingSnapshot;
    this._pendingUserPrompt =
      text || `请对附带的 ${payload.attachment_ids?.length || 0} 份文件执行 🎯 Goal 全链路闭环分析。`;
    this.loading = true;
    this._liveState = {
      statusText: "正在启动持久化 🎯 Goal 分析引擎...",
      thinking: "",
      thinkingStarted: false,
      thinkingOpen: true,
      thinkingDone: false,
      thinkingMs: 0,
      steps: [],
      content: "",
      plan: null,
      stages: [],
      progressPct: 2,
      retryNotice: null,
      verifyResult: null,
      checkpointSummary: null,
      runId: "",
      runStatus: "queued",
      refs: null,
    };
    this.render();
    this.refreshHeader?.();
    this._scrollToBottom();

    let createdRun = null;
    try {
      const res = await assistantApi.createRun(payload);
      createdRun = res?.run || null;
      if (res?.session) {
        this.currentSession = res.session;
        this._liveSessionId = res.session.session_id || "";
      }
      if (!createdRun?.run_id) {
        throw new Error("创建后台 Goal 任务失败");
      }
      this.activeRun = createdRun;
      this._lastRunEventId = 0;
      this._pendingUserPrompt = "";
      this._pendingUserAttachments = [];
    } catch (err) {
      this._liveState = null;
      this._pendingUserAttachments = [];
      this._pendingUserPrompt = "";
      this.loading = false;
      if (overrideText === null && !this.draftPrompt) {
        this.draftPrompt = text;
      }
      if (!useOverrideAttach && sentPendingSnapshot.length && !this.pendingAttachments.length) {
        this.pendingAttachments = sentPendingSnapshot;
      }
      Toast.error(err.message || "启动 Goal 后台分析失败");
      this.render();
      this.refreshHeader?.();
      return;
    }

    await this._attachToRunStream(createdRun.run_id, { afterEventId: 0 });
  }

  async _attachToRunStream(runId, { afterEventId = 0 } = {}) {
    if (!runId) return;
    if (this._activeRunAbort) {
      try {
        this._activeRunAbort.abort();
      } catch (_e) {
        // detach previous listener
      }
      this._activeRunAbort = null;
    }

    const seedRun = this.activeRun && this.activeRun.run_id === runId ? this.activeRun : null;
    const seedPlan = seedRun?.plan && typeof seedRun.plan === "object" ? seedRun.plan : null;
    const seedDoneSet = new Set(seedRun?.checkpoint?.completed_stage_ids || []);
    const seedStages = Array.isArray(seedPlan?.stages)
      ? seedPlan.stages.map((s) => ({
          ...s,
          status: seedDoneSet.has(s.stage_id) ? "done" : s.status || "pending",
        }))
      : [];

    this._lastRunEventId = Math.max(Number(afterEventId || 0), 0);
    this._stopRequested = false;
    this.loading = true;
    this._liveState = {
      statusText: seedRun?.current_stage || "正在连接后台 Goal 任务流...",
      thinking: "",
      thinkingStarted: false,
      thinkingOpen: true,
      thinkingDone: false,
      thinkingMs: 0,
      steps: [],
      content: "",
      plan: seedPlan,
      stages: seedStages,
      progressPct: Number(seedRun?.progress_pct || 5),
      retryNotice: null,
      verifyResult: null,
      checkpointSummary: null,
      runId,
      runStatus: String(seedRun?.status || "running"),
      refs: null,
    };
    this.render();
    this.refreshHeader?.();
    this._scrollToBottom();

    const maxReconnects = 3;
    try {
      for (let attempt = 0; attempt <= maxReconnects; attempt += 1) {
        const ac = new AbortController();
        this._activeRunAbort = ac;
        try {
          const out = await assistantApi.streamRunEvents(runId, {
            afterEventId: this._lastRunEventId,
            signal: ac.signal,
            onEvent: (evType, ev, frameId) => this._handleStreamEvent(evType, ev, frameId),
          });
          if (Number(out?.lastEventId || 0) > this._lastRunEventId) {
            this._lastRunEventId = Number(out.lastEventId);
          }

          const evType = String(out?.type || "");
          if (evType === "done") {
            if (out.session) {
              this.currentSession = out.session;
              this._liveSessionId = out.session.session_id || this._liveSessionId;
            } else if (this._liveSessionId) {
              const sr = await assistantApi.getSession(this._liveSessionId).catch(() => null);
              if (sr?.session) this.currentSession = sr.session;
            }
            if (out.assistant_message?.id && (out.assistant_message.steps || []).length > 0) {
              this.expandedStepsMsgIds.add(out.assistant_message.id);
            }
            this.activeRun = null;
            const listRes = await assistantApi.listSessions().catch(() => null);
            if (listRes?.sessions) this.sessions = listRes.sessions;
            break;
          }
          if (evType === "run_paused") {
            this.activeRun = out.run || {
              ...(this.activeRun || {}),
              run_id: runId,
              session_id: this.currentSession?.session_id || this._liveSessionId,
              status: "paused_recoverable",
              plan: out.plan || this._liveState?.plan || {},
              checkpoint: out.checkpoint || {},
              progress_pct: Number(out.progress_pct ?? this._liveState?.progressPct ?? 0),
              error_message: out.reason || "",
            };
            Toast.info("已保存执行断点，点击「从断点继续执行」即可无缝接力");
            const listRes = await assistantApi.listSessions().catch(() => null);
            if (listRes?.sessions) this.sessions = listRes.sessions;
            break;
          }
          if (evType === "run_cancelled") {
            this.activeRun = out.run || {
              ...(this.activeRun || {}),
              run_id: runId,
              session_id: this.currentSession?.session_id || this._liveSessionId,
              status: "cancelled",
              plan: out.plan || this._liveState?.plan || {},
              checkpoint: out.checkpoint || {},
              progress_pct: Number(out.progress_pct ?? this._liveState?.progressPct ?? 0),
            };
            Toast.info("已停止后台分析任务");
            break;
          }

          // 若未收到终态事件且非主动停止，自动携 Last-Event-ID 断点重连
          if (!out?.reachedTerminal && !this._stopRequested && attempt < maxReconnects) {
            if (this._liveState) {
              this._liveState.statusText = `事件流短暂波动，正从事件 #${this._lastRunEventId} 自动断点续传...`;
              this._renderLiveStatusDOM();
            }
            await new Promise((r) => setTimeout(r, 250));
            continue;
          }
          break;
        } catch (err) {
          if (Number(err?.lastEventId || 0) > this._lastRunEventId) {
            this._lastRunEventId = Number(err.lastEventId);
          }
          if (isAbortError(err)) {
            if (this._stopRequested) {
              Toast.info("已停止生成");
              const runDetail = await assistantApi.getRun(runId, { includeEvents: false }).catch(() => null);
              if (runDetail?.run) {
                this.activeRun = runDetail.run.status === "completed" ? null : runDetail.run;
              }
            }
            break;
          }
          if (attempt < maxReconnects && !this._stopRequested) {
            if (this._liveState) {
              this._liveState.retryNotice = {
                attempt: attempt + 1,
                max_retries: maxReconnects,
                wait_sec: 1,
                reason: "事件流自动重连",
                detail: `连接暂时中断，正携 Last-Event-ID=${this._lastRunEventId} 零丢帧重连 (${attempt + 1}/${maxReconnects})...`,
              };
              this._renderLivePlanDOM();
            }
            await new Promise((r) => setTimeout(r, 250));
            continue;
          }
          const runDetail = await assistantApi.getRun(runId, { includeEvents: false }).catch(() => null);
          if (runDetail?.run && runDetail.run.status !== "completed") {
            this.activeRun = runDetail.run;
          }
          Toast.error(err.message || "后台 Goal 任务流读取失败");
          break;
        }
      }
    } finally {
      if (this._streamRenderTimer) {
        clearTimeout(this._streamRenderTimer);
        this._streamRenderTimer = null;
      }
      this._activeRunAbort = null;
      this._liveState = null;
      this._pendingUserAttachments = [];
      this._pendingUserPrompt = "";
      this.loading = false;
      this.render();
      this.refreshHeader?.();
      this._scrollToBottom();
    }
  }

  async resumeActiveRun(runId = "") {
    const targetId = runId || this.activeRun?.run_id || "";
    if (!targetId || this.loading) return;
    // 在 /resume 前锁定已有事件光标，避免切换会话或刷新后从 0 重放旧的 run_paused/error 帧
    const resumeCursor = Math.max(
      Number(this._lastRunEventId || 0),
      Number(this.activeRun?.last_event_id || 0)
    );
    try {
      const res = await assistantApi.resumeRun(targetId, { extra_steps: 15 });
      if (res?.run) {
        this.activeRun = res.run;
      }
      await this._attachToRunStream(targetId, { afterEventId: resumeCursor });
    } catch (err) {
      Toast.error(err.message || "从断点恢复执行失败");
    }
  }

  _handleStreamEvent(evType, ev, frameEventId = 0) {
    const state = this._liveState;
    if (!state) return;
    const refs = state.refs;
    if (Number(frameEventId || 0) > this._lastRunEventId) {
      this._lastRunEventId = Number(frameEventId);
    }

    if (evType === "session_init") {
      if (ev?.session?.session_id) {
        this._liveSessionId = ev.session.session_id;
      }
      if (ev?.run_id) {
        state.runId = ev.run_id;
      }
      const stratMap = {
        full_context: `已装入全量上下文（${this._formatTokenCount(ev?.context_window || 0)} 窗口）· 模型分析中...`,
        lineage_sliced: "已启动 SQL 血缘手术刀精准切片分析...",
        goal_driven_staged: "已启动 🎯 Goal 甜蜜区分段闭环分析...",
        agentic_explore: "已生成结构大纲 · 智能按行/正则探索中...",
        stepped_batch: "正在分步拆解长文本分析...",
      };
      state.statusText = stratMap[ev?.strategy] || "模型推理分析中...";
      this._renderLiveStatusDOM();
      return;
    }

    if (evType === "probe_ready") {
      const cw = Number(ev?.capabilities?.context_window || 0);
      const eff = Number(ev?.capabilities?.effective_reasoning_budget || 0);
      const tier = MODEL_TIER_LABELS[ev?.capabilities?.model_tier] || ev?.capabilities?.model_tier || "";
      state.statusText = `模型探查完成：${ev?.model || ""} (${this._formatTokenCount(cw)}窗 · 甜蜜区${this._formatTokenCount(eff)}${tier ? ` · ${tier}` : ""})`;
      state.progressPct = Math.max(state.progressPct || 0, 8);
      this._renderLiveStatusDOM();
      this._renderLivePlanDOM();
      return;
    }

    if (evType === "plan_ready") {
      const plan = ev?.plan && typeof ev.plan === "object" ? ev.plan : null;
      if (plan) {
        state.plan = plan;
        const prevById = new Map((state.stages || []).map((s) => [s.stage_id, s]));
        state.stages = (plan.stages || []).map((st) => {
          const prev = prevById.get(st.stage_id);
          return {
            ...st,
            status: st.status || prev?.status || "pending",
            elapsed_ms: st.elapsed_ms || prev?.elapsed_ms || 0,
            summary: st.summary || prev?.summary || "",
          };
        });
        state.statusText = `执行计划就绪：${plan.strategy_label || plan.strategy || "智能规划"}（共 ${state.stages.length} 个阶段）`;
        state.progressPct = Math.max(state.progressPct || 0, 12);
        this._renderLiveStatusDOM();
        this._renderLivePlanDOM();
        this._smartScrollToBottom();
      }
      return;
    }

    if (evType === "stage_start") {
      const sid = String(ev?.stage_id || "");
      const found = (state.stages || []).find((s) => s.stage_id === sid);
      if (found) {
        found.status = "running";
      } else if (sid) {
        state.stages.push({
          stage_id: sid,
          index: Number(ev?.index || state.stages.length + 1),
          title: String(ev?.title || sid),
          kind: String(ev?.kind || "analysis"),
          status: "running",
        });
      }
      state.retryNotice = null;
      if (Number(ev?.progress_pct || 0) > 0) {
        state.progressPct = Number(ev.progress_pct);
      }
      state.statusText = `正在推进 ${ev?.title || sid}...`;
      this._renderLiveStatusDOM();
      this._renderLivePlanDOM();
      this._smartScrollToBottom();
      return;
    }

    if (evType === "stage_done") {
      const sid = String(ev?.stage_id || "");
      const found = (state.stages || []).find((s) => s.stage_id === sid);
      if (found) {
        found.status = "done";
        found.elapsed_ms = Number(ev?.elapsed_ms || 0);
        found.summary = String(ev?.summary || "");
      }
      state.retryNotice = null;
      if (Number(ev?.progress_pct || 0) > 0) {
        state.progressPct = Number(ev.progress_pct);
      }
      state.statusText = `已完成 ${ev?.title || sid}`;
      this._renderLiveStatusDOM();
      this._renderLivePlanDOM();
      this._smartScrollToBottom();
      return;
    }

    if (evType === "checkpoint_saved") {
      state.checkpointSummary = ev;
      if (Number(ev?.progress_pct || 0) > 0) {
        state.progressPct = Number(ev.progress_pct);
      }
      this._renderLivePlanDOM();
      return;
    }

    if (evType === "retry_wait") {
      state.retryNotice = {
        attempt: Number(ev?.attempt || 1),
        max_retries: Number(ev?.max_retries || 3),
        wait_sec: Number(ev?.wait_sec || 2),
        reason: String(ev?.reason || "响应超时"),
        shed_load: Boolean(ev?.shed_load),
        detail: String(
          ev?.detail ||
            `供应商响应较慢，第 ${ev?.attempt || 1}/${ev?.max_retries || 3} 次自动退避重试（等待 ${ev?.wait_sec || 2}s）...`
        ),
      };
      state.statusText = state.retryNotice.detail;
      this._renderLiveStatusDOM();
      this._renderLivePlanDOM();
      return;
    }

    if (evType === "verify_result") {
      state.verifyResult = ev;
      state.retryNotice = null;
      state.progressPct = Math.max(state.progressPct || 0, ev?.passed ? 90 : 85);
      state.statusText = String(
        ev?.detail || (ev?.passed ? "硬验收通过，正在合成最终报告..." : "验收发现遗漏项，正自动生成补救任务...")
      );
      this._renderLiveStatusDOM();
      this._renderLivePlanDOM();
      this._smartScrollToBottom();
      return;
    }

    if (evType === "run_paused") {
      state.runStatus = "paused_recoverable";
      state.retryNotice = null;
      if (ev?.run) this.activeRun = ev.run;
      state.statusText = String(ev?.reason || "已保存断点，可随时点击继续执行");
      this._renderLiveStatusDOM();
      this._renderLivePlanDOM();
      return;
    }

    if (evType === "status") {
      state.statusText = String(ev?.detail || ev?.stage || "探索分析中...");
      if (Number(ev?.percent || 0) > 0) {
        state.progressPct = Math.max(state.progressPct || 0, Number(ev.percent));
      }
      this._renderLiveStatusDOM();
      return;
    }

    if (evType === "thinking_start") {
      if (state.thinking) {
        state.thinking += "\n\n";
      }
      state.thinkingStarted = true;
      state.thinkingDone = false;
      state.thinkingOpen = true;
      state.statusText = "模型正在深度思考...";
      this._renderLiveStatusDOM();
      this._renderLiveThinkingDOM();
      this._smartScrollToBottom();
      return;
    }

    if (evType === "thinking_delta") {
      const delta = String(ev?.delta || "");
      if (!delta) return;
      state.thinkingStarted = true;
      state.thinking += delta;
      if (state.thinkingOpen && refs?.thinkingPreEl) {
        refs.thinkingPreEl.textContent = state.thinking;
        refs.thinkingPreEl.scrollTop = refs.thinkingPreEl.scrollHeight;
      } else {
        this._renderLiveThinkingDOM();
      }
      this._smartScrollToBottom();
      return;
    }

    if (evType === "thinking_end") {
      state.thinkingDone = true;
      state.thinkingMs += Number(ev?.elapsed_ms || 0);
      state.thinkingOpen = false;
      state.statusText = "思考完成，正在生成回答...";
      this._renderLiveStatusDOM();
      this._renderLiveThinkingDOM();
      this._smartScrollToBottom();
      return;
    }

    if (evType === "tool_start") {
      state.steps.push({
        step: Number(ev?.step || state.steps.length + 1),
        tool: String(ev?.tool || "tool"),
        purpose: String(ev?.purpose || ev?.tool || "执行工具"),
        status: "running",
        ok: true,
        elapsed_ms: 0,
        summary: "",
      });
      state.statusText = `正在执行步骤 #${ev?.step || state.steps.length}: ${ev?.purpose || ev?.tool || ""}`;
      this._renderLiveStatusDOM();
      this._renderLiveStepsDOM();
      this._smartScrollToBottom();
      return;
    }

    if (evType === "tool_end") {
      const stepNo = Number(ev?.step || 0);
      const target =
        [...state.steps].reverse().find((s) => stepNo > 0 && s.step === stepNo) ||
        state.steps[state.steps.length - 1];
      if (target) {
        target.status = "done";
        target.ok = Boolean(ev?.ok);
        target.elapsed_ms = Number(ev?.elapsed_ms || 0);
        target.summary = String(ev?.summary || "");
      } else {
        state.steps.push({
          step: stepNo || state.steps.length + 1,
          tool: String(ev?.tool || "tool"),
          purpose: String(ev?.purpose || ev?.tool || "执行工具"),
          status: "done",
          ok: Boolean(ev?.ok),
          elapsed_ms: Number(ev?.elapsed_ms || 0),
          summary: String(ev?.summary || ""),
        });
      }
      this._renderLiveStepsDOM();
      this._smartScrollToBottom();
      return;
    }

    if (evType === "content_reset") {
      state.content = "";
      if (this._streamRenderTimer) {
        clearTimeout(this._streamRenderTimer);
        this._streamRenderTimer = null;
      }
      this._renderLiveMarkdownDOM();
      return;
    }

    if (evType === "content_delta") {
      const delta = String(ev?.delta || "");
      if (!delta) return;
      const wasEmpty = !state.content;
      state.content += delta;
      if (wasEmpty) {
        state.statusText = "正在流式输出回答...";
        this._renderLiveStatusDOM();
      }
      this._scheduleLiveMarkdown(wasEmpty);
    }
  }

  _scheduleLiveMarkdown(immediate = false) {
    if (immediate) {
      if (this._streamRenderTimer) {
        clearTimeout(this._streamRenderTimer);
        this._streamRenderTimer = null;
      }
      this._renderLiveMarkdownDOM();
      this._smartScrollToBottom();
      return;
    }
    if (this._streamRenderTimer) return;
    this._streamRenderTimer = setTimeout(() => {
      this._streamRenderTimer = null;
      this._renderLiveMarkdownDOM();
      this._smartScrollToBottom();
    }, 50);
  }

  _renderLiveStatusDOM() {
    const state = this._liveState;
    const slot = state?.refs?.statusSlot;
    if (!slot) return;
    clearEl(slot, statusDot(state.statusText || "正在分析...", "info"));
  }

  _renderPlanCardNode({
    plan = null,
    stages = [],
    progressPct = 0,
    retryNotice = null,
    verifyResult = null,
    runStatus = "",
    runReason = "",
    runId = "",
    checkpoint = null,
  } = {}) {
    if (!plan && !retryNotice && !verifyResult && !runReason) return null;
    const effStages = Array.isArray(stages) && stages.length ? stages : plan?.stages || [];
    const doneCnt = effStages.filter((s) => s.status === "done").length;
    const pct = Math.min(100, Math.max(0, Number(progressPct || 0)));
    const tierLbl = MODEL_TIER_LABELS[plan?.model_tier] || plan?.model_tier || "";
    const isRecoverable =
      runId && ["paused_recoverable", "failed", "cancelled"].includes(String(runStatus || ""));

    const lineageLines = Array.isArray(plan?.lineage_summary) ? plan.lineage_summary : [];

    return el(
      "div",
      { className: `ai-asst-plan-card ${isRecoverable ? "is-paused" : ""}` },
      el(
        "div",
        { className: "ai-asst-plan-head" },
        badge(plan?.strategy_label || "🎯 智能执行计划", "info"),
        tierLbl ? badge(`档位:${tierLbl}`, plan?.model_tier === "lightweight" ? "warn" : "neutral") : null,
        plan?.effective_reasoning_budget
          ? badge(`甜蜜区 ${this._formatTokenCount(plan.effective_reasoning_budget)}`, "neutral")
          : null,
        (plan?.high_medium_findings_count || plan?.high_medium_finding_count || 0) > 0
          ? badge(
              `必核隐患 ${plan.high_medium_findings_count ?? plan.high_medium_finding_count} 项`,
              "warn"
            )
          : null,
        el("span", {
          className: "muted mono",
          style: { marginLeft: "auto", fontSize: "11px" },
          text: effStages.length ? `${doneCnt}/${effStages.length} 阶段 · ${pct}%` : `${pct}%`,
        })
      ),
      (plan?.reason || plan?.decision_reason)
        ? el("div", { className: "ai-asst-plan-reason muted", text: plan.reason || plan.decision_reason })
        : null,
      pct > 0
        ? el(
            "div",
            { className: "ai-asst-goal-progress" },
            el("div", { className: "ai-asst-goal-progress-fill", style: { width: `${pct}%` } })
          )
        : null,
      lineageLines.length
        ? el(
            "div",
            { className: "ai-asst-plan-lineage" },
            el("div", { className: "muted", style: { fontSize: "11px", fontWeight: "600" }, text: "静态血缘摘要:" }),
            lineageLines.slice(0, 4).map((ln) =>
              el("div", { className: "mono muted", style: { fontSize: "11px" }, text: ln })
            )
          )
        : null,
      effStages.length
        ? el(
            "div",
            { className: "ai-asst-checklist" },
            effStages.map((st) => {
              const icon =
                st.status === "done"
                  ? "✓"
                  : st.status === "running"
                  ? "◉"
                  : st.kind === "remedial"
                  ? "↻"
                  : "○";
              return el(
                "div",
                { className: `ai-asst-check-item is-${st.status || "pending"} ${st.kind === "remedial" ? "is-remedial" : ""}` },
                el("span", { className: "ai-asst-check-icon mono", text: icon }),
                el("span", { className: "ai-asst-check-title", text: st.title || st.stage_id }),
                Array.isArray(st.finding_ids) && st.finding_ids.length
                  ? badge(`${st.finding_ids.length}隐患`, "warn")
                  : null,
                st.elapsed_ms > 0
                  ? el("span", {
                      className: "muted mono",
                      style: { marginLeft: "auto", fontSize: "10.5px" },
                      text: `${Math.round(st.elapsed_ms)}ms`,
                    })
                  : null
              );
            })
          )
        : null,
      retryNotice
        ? el(
            "div",
            { className: "ai-asst-retry-banner" },
            badge(`自动重试 ${retryNotice.attempt}/${retryNotice.max_retries}`, "warn"),
            el("span", { text: retryNotice.detail || retryNotice.reason })
          )
        : null,
      verifyResult
        ? el(
            "div",
            { className: `ai-asst-verify-banner ${verifyResult.passed ? "is-pass" : "is-remedial"}` },
            badge(
              verifyResult.passed ? "硬验收通过" : "触发自动补救",
              verifyResult.passed ? "success" : "warn"
            ),
            el("span", { text: verifyResult.detail || "" })
          )
        : null,
      isRecoverable
        ? el(
            "div",
            { className: "ai-asst-resume-bar" },
            el("div", {
              className: "muted",
              style: { fontSize: "12px", flex: "1" },
              text:
                runReason ||
                `已保存 Checkpoint（已完成 ${(checkpoint?.completed_stage_ids || []).length || doneCnt} 个阶段），可随时一键接续剩余目标。`,
            }),
            el("button", {
              type: "button",
              className: "mac-btn mac-btn-sm mac-btn-primary ai-asst-resume-btn",
              disabled: this.loading,
              text: "从断点继续执行",
              onClick: () => this.resumeActiveRun(runId),
            })
          )
        : null
    );
  }

  _renderLivePlanDOM() {
    const state = this._liveState;
    const slot = state?.refs?.planSlot;
    if (!slot) return;
    const node = this._renderPlanCardNode({
      plan: state.plan,
      stages: state.stages,
      progressPct: state.progressPct,
      retryNotice: state.retryNotice,
      verifyResult: state.verifyResult,
      runStatus: state.runStatus,
      runId: state.runId,
    });
    if (node) {
      clearEl(slot, node);
    } else {
      clearEl(slot);
    }
  }

  _renderRecoverableRunCard() {
    const run = this.activeRun;
    if (!run || !run.run_id || run.status === "completed") return null;
    const curSid = this.currentSession?.session_id || this._liveSessionId || "";
    if (curSid && run.session_id && run.session_id !== curSid) return null;
    const plan = run.plan && typeof run.plan === "object" ? run.plan : null;
    const ckpt = run.checkpoint && typeof run.checkpoint === "object" ? run.checkpoint : {};
    const doneSet = new Set(ckpt.completed_stage_ids || []);
    const stages = (plan?.stages || []).map((st) => ({
      ...st,
      status: doneSet.has(st.stage_id) ? "done" : st.status || "pending",
    }));
    const card = this._renderPlanCardNode({
      plan: plan || { strategy_label: "🎯 后台断点任务", stages },
      stages,
      progressPct: Number(run.progress_pct || 0),
      runStatus: run.status,
      runReason: run.error_message || run.current_stage || "",
      runId: run.run_id,
      checkpoint: ckpt,
    });
    if (!card) return null;
    return el(
      "div",
      { className: "ai-asst-msg ai-asst-msg-assistant" },
      el("div", { className: "ai-asst-assistant-card ai-asst-recoverable-card" }, card)
    );
  }

  _renderLiveThinkingDOM() {
    const state = this._liveState;
    const refs = state?.refs;
    const slot = refs?.thinkingSlot;
    if (!slot) return;
    if (!state.thinkingStarted && !state.thinking) {
      refs.thinkingPreEl = null;
      clearEl(slot);
      return;
    }
    const secLabel = state.thinkingMs > 0 ? ` (${(state.thinkingMs / 1000).toFixed(1)}s)` : "";
    const titleText = state.thinkingDone ? `已深度思考${secLabel}` : "深度思考中...";
    let preEl = null;
    const box = el(
      "div",
      { className: "ai-asst-thinking-box" },
      el(
        "button",
        {
          type: "button",
          className: "ai-asst-thinking-toggle",
          onClick: () => {
            state.thinkingOpen = !state.thinkingOpen;
            this._renderLiveThinkingDOM();
          },
        },
        svgIcon("lightbulb", 13),
        el("span", { text: titleText }),
        el("span", {
          className: "muted",
          style: { marginLeft: "auto", fontSize: "11.5px" },
          text: state.thinkingOpen ? "收起思考" : "展开思考",
        })
      ),
      state.thinkingOpen
        ? el(
            "div",
            { className: "ai-asst-thinking-body" },
            (preEl = el("pre", {
              className: "ai-asst-thinking-pre",
              text: state.thinking || "...",
            }))
          )
        : null
    );
    refs.thinkingPreEl = preEl;
    clearEl(slot, box);
    if (preEl) {
      preEl.scrollTop = preEl.scrollHeight;
    }
  }

  _renderLiveStepsDOM() {
    const state = this._liveState;
    const slot = state?.refs?.stepsSlot;
    if (!slot) return;
    if (!state.steps.length) {
      clearEl(slot);
      return;
    }
    const box = el(
      "div",
      { className: "ai-asst-trace-box" },
      el(
        "div",
        { className: "ai-asst-trace-toggle" },
        statusDot(`实时探索步骤 (${state.steps.length})`, "info")
      ),
      el(
        "div",
        { className: "ai-asst-trace-list" },
        state.steps.map((st) =>
          el(
            "div",
            {
              className: `ai-asst-trace-step ${
                st.status === "running" ? "is-running" : st.ok ? "is-ok" : "is-warn"
              }`,
            },
            el(
              "div",
              { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
              badge(
                `#${st.step} ${st.tool}`,
                st.status === "running" ? "info" : st.ok ? "info" : "warn"
              ),
              el("span", { style: { fontWeight: "600", fontSize: "12px" }, text: st.purpose || st.tool }),
              el("span", {
                className: "muted mono",
                style: { marginLeft: "auto", fontSize: "11px" },
                text: st.status === "running" ? "执行中..." : `${st.elapsed_ms || 0} ms`,
              })
            ),
            st.summary
              ? el("div", { className: "muted", style: { fontSize: "11.5px", marginTop: "3px" }, text: st.summary })
              : null
          )
        )
      )
    );
    clearEl(slot, box);
  }

  _renderLiveMarkdownDOM() {
    const state = this._liveState;
    const slot = state?.refs?.contentSlot;
    if (!slot) return;
    if (!state.content) {
      clearEl(slot);
      return;
    }
    clearEl(
      slot,
      el(
        "div",
        { className: "ai-asst-stream-body" },
        renderSafeMarkdown(state.content),
        el("span", { className: "ai-asst-stream-cursor", "aria-hidden": "true" })
      )
    );
  }

  _renderLiveAssistantCard() {
    const state = this._liveState;
    if (!state) return null;
    const statusSlot = el("div", { className: "ai-asst-stream-status" });
    const planSlot = el("div");
    const thinkingSlot = el("div");
    const stepsSlot = el("div");
    const contentSlot = el("div");
    state.refs = {
      statusSlot,
      planSlot,
      thinkingSlot,
      stepsSlot,
      contentSlot,
      thinkingPreEl: null,
    };
    this._renderLiveStatusDOM();
    this._renderLivePlanDOM();
    this._renderLiveThinkingDOM();
    this._renderLiveStepsDOM();
    this._renderLiveMarkdownDOM();
    return el(
      "div",
      { className: "ai-asst-msg ai-asst-msg-assistant" },
      el(
        "div",
        { className: "ai-asst-assistant-card ai-asst-live-card" },
        statusSlot,
        planSlot,
        thinkingSlot,
        stepsSlot,
        contentSlot
      )
    );
  }

  _scrollToBottom() {
    const stream = this.root.querySelector(".ai-asst-stream");
    if (stream) {
      stream.scrollTop = stream.scrollHeight;
    }
  }

  _smartScrollToBottom() {
    const stream = this.root.querySelector(".ai-asst-stream");
    if (!stream) return;
    const clientH = Number(stream.clientHeight || 0);
    const nearBottom = clientH <= 0 || stream.scrollHeight - stream.scrollTop - clientH < 140;
    if (nearBottom) {
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

    const reasoningText = String(msg.reasoning || "").trim();
    const reasoningExpanded = this.expandedReasoningMsgIds.has(msg.id);
    const reasoningMs = Number(msg.reasoning_ms || 0);
    const reasoningSection = reasoningText
      ? el(
          "div",
          { className: "ai-asst-thinking-box" },
          el(
            "button",
            {
              type: "button",
              className: "ai-asst-thinking-toggle",
              onClick: () => {
                if (reasoningExpanded) this.expandedReasoningMsgIds.delete(msg.id);
                else this.expandedReasoningMsgIds.add(msg.id);
                this.render();
              },
            },
            svgIcon("lightbulb", 13),
            el("span", {
              text:
                reasoningMs > 0
                  ? `已深度思考 (${(reasoningMs / 1000).toFixed(1)}s)`
                  : "已深度思考（思考过程）",
            }),
            el("span", {
              className: "muted",
              style: { marginLeft: "auto", fontSize: "11.5px" },
              text: reasoningExpanded ? "收起思考" : "展开思考",
            })
          ),
          reasoningExpanded
            ? el(
                "div",
                { className: "ai-asst-thinking-body" },
                el("pre", { className: "ai-asst-thinking-pre", text: reasoningText })
              )
            : null
        )
      : null;

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
        reasoningSection,
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

    const selModel = this._getSelectedModelMeta();
    const thinkingSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm ai-asst-think-select",
        "aria-label": "深度思考模式",
        disabled: this.loading,
        title: selModel?.supports_thinking
          ? "当前模型支持 Thinking 深度思考，可切换推理深度"
          : "控制模型推理 / Thinking 模式（自动探测或强制开/关）",
        onChange: (e) => {
          this.thinkingMode = e.target.value;
          this.render();
        },
      },
      THINKING_MODE_OPTIONS.map((optItem) => {
        const opt = el("option", { value: optItem.id, text: optItem.label });
        if (optItem.id === this.thinkingMode) opt.selected = true;
        return opt;
      })
    );

    const runModeToggle = el(
      "div",
      { className: "seg-group ai-asst-runmode-group", "aria-label": "执行模式切换" },
      RUN_MODES.map((rm) =>
        el("button", {
          type: "button",
          className: `seg-btn ai-asst-runmode-btn ${this.runMode === rm.id ? "is-active" : ""}`,
          disabled: this.loading,
          title:
            rm.id === "goal"
              ? "🎯 Goal 深度闭环模式：后台持久化断点执行 + 覆盖率与高危隐患硬验收 + 自动补救"
              : "标准实时流式对话模式",
          text: rm.label,
          onClick: () => {
            this.runMode = rm.id;
            this.render();
          },
        })
      )
    );

    const probeCapsule = selModel
      ? (() => {
          const cw = Number(selModel.context_window || 64000);
          const eff = Number(selModel.effective_reasoning_budget || selModel.attachment_budget || Math.floor(cw * 0.65));
          const tier = MODEL_TIER_LABELS[selModel.model_tier] || "标准";
          const ttft = Number(selModel.ttft_ms || 0);
          const ttftTxt = ttft > 0 ? ` · TTFT ${(ttft / 1000).toFixed(1)}s` : "";
          const probedTag = (selModel.source || selModel.probe_source) === "live_probe" ? " ✓实测" : "";
          return el(
            "button",
            {
              type: "button",
              className: `ai-asst-probe-capsule ${selModel.model_tier === "lightweight" ? "is-lightweight" : ""}`,
              disabled: this.probingModel || this.loading,
              title: "点击动态探查当前模型的真实窗口、Token 换算系数与首字延迟",
              onClick: () => this.probeCurrentModel({ forceRefresh: true }),
            },
            el("span", {
              text: this.probingModel
                ? "探查模型能力中..."
                : `${this._formatTokenCount(cw)}窗 · 甜蜜区${this._formatTokenCount(eff)} · ${tier}${ttftTxt}${probedTag}`,
            })
          );
        })()
      : null;

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
      runModeToggle,
      probeCapsule,
      thinkingSelect,
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
        })
      );
    }
    if (this.loading && this._liveState) {
      pendingNodes.push(this._renderLiveAssistantCard());
    } else if (!this.loading && this.activeRun) {
      const recCard = this._renderRecoverableRunCard();
      if (recCard) pendingNodes.push(recCard);
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
    const csvInlineBudget = Number(this.attachmentLimits.csv_inline_token_budget || 8000);
    const selModelMeta = this._getSelectedModelMeta();
    const modelCtxWindow = Number(selModelMeta?.context_window || 64000);
    const modelAttachBudget = Number(
      selModelMeta?.attachment_budget ||
        Math.max(inlineBudget, Math.floor(modelCtxWindow * 0.65))
    );
    const allCsvPending =
      this.pendingAttachments.length > 0 &&
      this.pendingAttachments.every((a) => a?.file_kind === "csv");
    const overModelBudget = totalPendingTokens > modelAttachBudget;
    const csvOverBudget = allCsvPending && totalPendingTokens > csvInlineBudget;
    const isWarnBar = overModelBudget || csvOverBudget;
    const ctxPct = Math.min(
      100,
      Math.round((totalPendingTokens / Math.max(1, modelAttachBudget)) * 100)
    );

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
                  title: att.summary || `${att.filename} (点击预览大纲/画像或分片)`,
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

    const strategyText = allCsvPending
      ? `CSV 沙箱数据（~${totalPendingTokens} tok · 支持 SQL 聚合探查）`
      : overModelBudget
      ? `超出当前模型单次直通预算（~${totalPendingTokens} > ${this._formatTokenCount(modelAttachBudget)} tok · 窗口 ${this._formatTokenCount(modelCtxWindow)}），将按大纲+按行探索：`
      : `上下文容量充裕（~${totalPendingTokens} / ${this._formatTokenCount(modelAttachBudget)} tok · ${this._formatTokenCount(modelCtxWindow)} 窗口 · 可全量直通）：`;

    const strategyBar = this.pendingAttachments.length
      ? el(
          "div",
          { className: `ai-asst-strategy-bar ${isWarnBar ? "is-warn" : "is-ok"}` },
          el(
            "div",
            { className: "ai-asst-ctx-meter-wrap" },
            statusDot(strategyText, isWarnBar ? "warn" : "success"),
            el(
              "div",
              { className: "ai-asst-ctx-meter", title: `附件占比约 ${ctxPct}%` },
              el("div", {
                className: "ai-asst-ctx-meter-fill",
                style: { width: `${Math.max(ctxPct, totalPendingTokens > 0 ? 6 : 0)}%` },
              })
            )
          ),
          el(
            "select",
            {
              className: "mac-input mac-input-sm",
              "aria-label": "大文件提交拆解策略",
              style: { maxWidth: "330px" },
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
            ? "输入需求，或直接粘贴/拖拽 .txt/.sql/.csv（长源码按当前模型窗口全量直通或大纲精准探索，实时流式输出 · Enter 发送）..."
            : `输入 ${this.dialect} 问题，或粘贴/拖拽 .txt/.sql/.csv（支持大纲探索与流式深度思考）...`,
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
          this.uploadingAttachment ? "解析大纲中..." : "附加 .sql/.txt/.csv"
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
        this.loading
          ? el(
              "button",
              {
                type: "button",
                className: "mac-btn btn-danger",
                onClick: () => this.stopStreaming(),
              },
              svgIcon("stop", 13),
              "停止生成"
            )
          : el(
              "button",
              {
                type: "button",
                className: "mac-btn mac-btn-primary",
                disabled: this.uploadingAttachment,
                onClick: () => this.sendUserMessage(),
              },
              svgIcon("send", 13),
              this.runMode === "goal" ? "🎯 启动 Goal" : "发送"
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
