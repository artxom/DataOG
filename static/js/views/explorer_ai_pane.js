/**
 * Data.O.G 数据探查 · AI 只读智能探查会话面板 (ExplorerAiPane)
 * 严格遵循安全与极简红线：
 * - 100% 物理级只读后端通道 (/api/modules/data_explorer/explore)
 * - 零 innerHTML / outerHTML / alert / confirm / prompt
 * - 焦点表直接从左侧 DataGrip 四级数据库清单勾选联动，右侧不堆叠方块胶囊
 */

import { explorerApi, platformApi } from "../core/api.js";
import { badge, clearEl, copyText, el, statusDot, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

const GOAL_STARTERS = [
  {
    goalType: "profile",
    label: "分布画像",
    prompt: "统计关键表的总行数、主键去重唯一率，并画像核心数值与日期分区字段的分布区间。",
  },
  {
    goalType: "quality",
    label: "脏数据排查",
    prompt: "全面排查关键字段的空值率、主键重复记录、异常负值/极值以及越界脏数据。",
  },
  {
    goalType: "relation",
    label: "主外键关联推断",
    prompt: "推断表之间的候选主外键关联字段，采样验证关联基数关系与 LEFT JOIN 未匹配孤儿率。",
  },
  {
    goalType: "metric",
    label: "业务指标核对",
    prompt: "按核心业务维度聚合明细指标，核对汇总口径一致性并定位差异集中的批次或维度。",
  },
];

export class ExplorerAiPane {
  constructor({
    onSendSqlToEditor = null,
    onSyncFocusToTree = null,
    onClearTreeFocus = null,
    onHeaderStateChange = null,
  } = {}) {
    this.onSendSqlToEditor = onSendSqlToEditor;
    this.onSyncFocusToTree = onSyncFocusToTree;
    this.onClearTreeFocus = onClearTreeFocus;
    this.onHeaderStateChange = onHeaderStateChange;

    this.connectionId = store.state.connections[0]?.connection_id || "";
    this.schemaName = "";
    this.focusTables = [];
    this.selectedModelKey = ""; // "providerId::model"
    this.maxSteps = 5;
    this.sampleLimit = 100;
    this.activeGoalType = "general";

    this.sessions = [];
    this.currentSession = null; // 含 messages
    this.loading = false;
    this.draftPrompt = "";
    this.openStepKeys = new Set(); // "${msgId}:${stepNo}"

    this._promptId = `exp-prompt-${Math.random().toString(36).slice(2, 8)}`;
    this.root = el("div", { className: "explorer-ai-wrap" });

    this._unsub = store.subscribe((_st, reason) => {
      if (["llm", "connections", "bootstrap"].includes(reason)) {
        this._ensureSelectedModel();
        this.render();
        this.onHeaderStateChange?.();
      }
    });
  }

  destroy() {
    this._unsub?.();
  }

  _getAvailableModels() {
    return store.state.llmModels || [];
  }

  _ensureSelectedModel() {
    const models = this._getAvailableModels();
    if (!models.length) {
      this.selectedModelKey = "";
      return;
    }
    const exists = models.some(
      (m) => `${m.provider_id}::${m.model}` === this.selectedModelKey
    );
    if (!exists) {
      const def = models.find((m) => m.is_default) || models[0];
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

  /**
   * 供父级窗口顶栏右侧渲染：只读沙箱绿灯 + 模型选择 + 最大步数 + 新建会话
   */
  buildHeaderRightControls() {
    this._ensureSelectedModel();
    const models = this._getAvailableModels();

    const modelSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "选择探查大模型",
        style: { maxWidth: "196px" },
        disabled: models.length === 0 || this.loading,
        onChange: (e) => {
          this.selectedModelKey = e.target.value;
        },
      },
      models.length
        ? models.map((m) => {
            const val = `${m.provider_id}::${m.model}`;
            const opt = el("option", {
              value: val,
              text: `${m.model} (${m.provider_name})`,
            });
            if (val === this.selectedModelKey) opt.selected = true;
            return opt;
          })
        : [el("option", { value: "", text: "未配置 AI 模型" })]
    );

    const stepSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "最大自动采样步数",
        title: "单轮最大只读采样步数上限",
        disabled: this.loading,
        onChange: (e) => {
          this.maxSteps = Number(e.target.value) || 5;
        },
      },
      [3, 5, 8].map((n) => {
        const opt = el("option", { value: String(n), text: `上限 ${n} 步` });
        if (this.maxSteps === n) opt.selected = true;
        return opt;
      })
    );

    return el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      statusDot("只读沙箱", "success"),
      modelSelect,
      stepSelect,
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm",
          disabled: this.loading,
          onClick: () => this.startFreshSession(),
        },
        svgIcon("plus", 12),
        "新会话"
      )
    );
  }

  async syncScope({ connectionId, schemaName, focusTables, reloadSessions = false } = {}) {
    const connChanged = Boolean(connectionId && connectionId !== this.connectionId);
    if (connectionId !== undefined) this.connectionId = connectionId;
    if (schemaName !== undefined) this.schemaName = schemaName;
    if (Array.isArray(focusTables)) this.focusTables = [...focusTables];

    if (connChanged || reloadSessions) {
      this.currentSession = null;
      await this.loadSessions(this.connectionId);
    } else {
      this.render();
    }
  }

  async loadSessions(connectionId = this.connectionId) {
    if (!connectionId) {
      this.sessions = [];
      this.currentSession = null;
      this.render();
      return;
    }
    try {
      const res = await explorerApi.listSessions(connectionId);
      this.sessions = res.sessions || [];
      if (Array.isArray(res.llm_models) && res.llm_models.length) {
        store.updateLlmState(null, res.llm_models);
      }
    } catch (_err) {
      this.sessions = [];
    }
    this.render();
  }

  startFreshSession() {
    this.currentSession = null;
    this.activeGoalType = "general";
    this.render();
    const ta = this.root.querySelector("textarea");
    ta?.focus();
  }

  async selectSession(sessionId) {
    if (!sessionId) {
      this.startFreshSession();
      return;
    }
    try {
      const res = await explorerApi.getSession(sessionId);
      this.currentSession = res.session || null;
      if (this.currentSession) {
        this.connectionId = this.currentSession.connection_id || this.connectionId;
        this.schemaName = this.currentSession.schema_name || "";
        this.focusTables = this.currentSession.focus_tables || [];
        // 默认展开最新一轮的最后一步采样以便快速查看
        const msgs = this.currentSession.messages || [];
        const lastAsst = [...msgs].reverse().find((m) => m.role === "assistant");
        if (lastAsst && (lastAsst.steps || []).length > 0) {
          this.openStepKeys.add(`${lastAsst.id}:1`);
        }
        this.onSyncFocusToTree?.({
          connectionId: this.connectionId,
          focusTables: this.focusTables,
        });
      }
      this.render();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async confirmDeleteCurrentSession() {
    if (!this.currentSession?.session_id) return;
    const ok = await Modal.confirm({
      title: "删除探查会话",
      message: `确定永久删除会话「${this.currentSession.title}」及其采样记录吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await explorerApi.deleteSession(
        this.currentSession.session_id,
        this.connectionId
      );
      this.sessions = res.sessions || [];
      this.currentSession = null;
      Toast.success("会话已删除");
      this.render();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async submitExplore(customPrompt = null, customGoal = null) {
    if (this.loading) return;
    const text = (customPrompt !== null ? customPrompt : this.draftPrompt).trim();
    if (!text) {
      Toast.warn("请输入探查目标或问题");
      return;
    }
    if (!this.connectionId) {
      Toast.warn("请先在左侧选择或连接一个数据源");
      return;
    }
    const models = this._getAvailableModels();
    if (!models.length) {
      Toast.warn("请先在「系统配置 → AI 模型」填写大模型 API Key");
      store.openApp("settings", { section: "llm" });
      return;
    }

    const { providerId, model } = this._parseSelectedModel();
    const effGoal = customGoal || this.activeGoalType || "general";

    this.loading = true;
    this.draftPrompt = "";
    this._pendingUserPrompt = text;
    this.render();
    this.onHeaderStateChange?.();

    try {
      const res = await explorerApi.explore({
        session_id: this.currentSession?.session_id || "",
        connection_id: this.connectionId,
        schema_name: this.schemaName || "",
        focus_tables: this.focusTables,
        prompt: text,
        goal_type: effGoal,
        provider_id: providerId,
        model,
        max_steps: this.maxSteps,
        sample_limit: this.sampleLimit,
      });
      this.currentSession = res.session || null;
      this.sessions = res.sessions || this.sessions;
      const newAsst = res.assistant_message;
      if (newAsst && (newAsst.steps || []).length > 0) {
        this.openStepKeys.add(`${newAsst.id}:1`);
      }
      this.activeGoalType = "general";
    } catch (err) {
      this.draftPrompt = text;
      Toast.error(err.message);
    } finally {
      this.loading = false;
      this._pendingUserPrompt = "";
      this.render();
      this.onHeaderStateChange?.();
      this._scrollToBottom();
    }
  }

  _scrollToBottom() {
    const stream = this.root.querySelector(".explorer-stream");
    if (stream) {
      stream.scrollTop = stream.scrollHeight;
    }
  }

  async _exportStepCsv(step) {
    if (!step?.columns?.length) return;
    try {
      await platformApi.exportQuery(
        {
          columns: step.columns,
          rows: step.rows || [],
          format: "csv",
          title: `probe_step_${step.step_no}`,
        },
        `probe_step_${step.step_no}.csv`
      );
      Toast.success("采样数据已导出 CSV");
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _renderStepTraceList(msg) {
    const steps = msg.steps || [];
    if (!steps.length) return null;

    return el(
      "div",
      { className: "probe-steps-group", "aria-label": "只读采样轨迹" },
      steps.map((st) => {
        const stepKey = `${msg.id}:${st.step_no}`;
        const isOpen = this.openStepKeys.has(stepKey);
        const isBlocked = st.status === "blocked";
        const isError = st.status === "error";

        let statusNode;
        if (isBlocked) {
          statusNode = badge("沙箱拦截", "danger");
        } else if (isError) {
          statusNode = badge("自动修正", "warn");
        } else {
          statusNode = el("span", {
            className: "mono",
            style: { color: "var(--os-muted)", fontSize: "11px" },
            text: `${st.row_count ?? 0} 行${st.truncated ? "+" : ""} · ${st.elapsed_ms ?? 0} ms`,
          });
        }

        const sampleCols = (st.columns || []).map((colName, idx) => ({
          key: String(idx),
          label: String(colName),
          render: (row) => {
            const v = Array.isArray(row) ? row[idx] : row?.[colName];
            if (v === null || v === undefined) {
              return el("span", { style: { color: "var(--os-muted)" }, text: "NULL" });
            }
            return String(v);
          },
        }));

        return el(
          "details",
          {
            className: "probe-step-card",
            id: `probe-step-${msg.id}-${st.step_no}`,
            open: isOpen,
            onToggle: (e) => {
              if (e.currentTarget.open) this.openStepKeys.add(stepKey);
              else this.openStepKeys.delete(stepKey);
            },
          },
          el(
            "summary",
            { className: "probe-step-summary" },
            el(
              "div",
              { className: "probe-step-title-wrap" },
              el("span", { className: "probe-step-chev", text: "▸" }),
              el("span", { className: "probe-step-idx mono", text: `Step ${st.step_no}` }),
              el("span", { className: "probe-step-purpose", text: st.purpose || "只读采样查询" })
            ),
            el("div", { className: "probe-step-meta" }, statusNode)
          ),
          el(
            "div",
            { className: "probe-step-body" },
            el(
              "div",
              { className: "probe-sql-bar" },
              el("pre", { className: "probe-sql-pre mono", text: st.sql || "" }),
              el(
                "div",
                { className: "probe-sql-actions" },
                !isBlocked && st.sql
                  ? el("button", {
                      type: "button",
                      className: "btn btn-xs",
                      text: "填入编辑器 →",
                      onClick: () => this.onSendSqlToEditor?.(st.sql),
                    })
                  : null,
                st.sql
                  ? el("button", {
                      type: "button",
                      className: "btn btn-xs btn-ghost",
                      text: "复制",
                      onClick: async () => {
                        await copyText(st.sql);
                        Toast.success("已复制探查 SQL");
                      },
                    })
                  : null,
                sampleCols.length > 0 && (st.rows || []).length > 0
                  ? el("button", {
                      type: "button",
                      className: "btn btn-xs btn-ghost",
                      text: "CSV",
                      onClick: () => this._exportStepCsv(st),
                    })
                  : null
              )
            ),
            st.error
              ? el("div", {
                  className: `probe-step-alert ${isBlocked ? "is-danger" : "is-warn"}`,
                  text: st.error,
                })
              : null,
            sampleCols.length > 0
              ? el(
                  "div",
                  { className: "probe-grid-box" },
                  new DataGrid({
                    ariaLabel: `Step ${st.step_no} 采样数据`,
                    columns: sampleCols,
                    rows: (st.rows || []).slice(0, 50),
                    rowKey: (_r, idx) => String(idx),
                    emptyText: "0 行采样记录",
                  }).root
                )
              : null
          )
        );
      })
    );
  }

  _renderAssistantCard(msg) {
    const findings = msg.findings || [];

    return el(
      "div",
      { className: "explorer-turn-assistant" },
      this._renderStepTraceList(msg),
      el(
        "div",
        { className: "explorer-conclusion-card" },
        el("div", { className: "explorer-summary-text", text: msg.content || "" }),
        findings.length > 0
          ? el(
              "div",
              { className: "explorer-findings-list" },
              findings.map((f) => {
                const dotCls =
                  f.tone === "error"
                    ? "dot-danger"
                    : f.tone === "warning"
                    ? "dot-warn"
                    : f.tone === "success"
                    ? "dot-success"
                    : "dot-info";
                return el(
                  "div",
                  { className: "finding-item" },
                  el(
                    "div",
                    { className: "finding-main" },
                    el("span", { className: `dot ${dotCls}`, style: { marginTop: "5px" } }),
                    el(
                      "div",
                      { className: "finding-text-wrap" },
                      el("span", { className: "finding-title", text: f.title }),
                      f.detail
                        ? el("span", { className: "finding-detail", text: f.detail })
                        : null
                    )
                  ),
                  f.step_ref > 0
                    ? el("button", {
                        type: "button",
                        className: "btn btn-xs btn-ghost mono",
                        text: `[Step ${f.step_ref}]`,
                        onClick: () => {
                          this.openStepKeys.add(`${msg.id}:${f.step_ref}`);
                          this.render();
                          const targetEl = document.getElementById(
                            `probe-step-${msg.id}-${f.step_ref}`
                          );
                          targetEl?.scrollIntoView({ behavior: "smooth", block: "nearest" });
                        },
                      })
                    : null
                );
              })
            )
          : null,
        msg.recommended_sql
          ? el(
              "div",
              { className: "explorer-rec-sql-bar" },
              el("span", {
                className: "mono explorer-rec-sql-preview",
                title: msg.recommended_sql,
                text: msg.recommended_sql.replace(/\s+/g, " "),
              }),
              el("button", {
                type: "button",
                className: "mac-btn mac-btn-sm mac-btn-primary",
                text: "在 SQL 编辑器打开",
                onClick: () => this.onSendSqlToEditor?.(msg.recommended_sql),
              })
            )
          : null,
        msg.model
          ? el(
              "div",
              { className: "explorer-turn-foot mono" },
              `${msg.provider_name ? msg.provider_name + " · " : ""}${msg.model} · ${(msg.steps || []).length} 步采样 · ${msg.total_elapsed_ms || 0} ms`
            )
          : null
      )
    );
  }

  render() {
    const conns = store.state.connections || [];
    const activeConn = conns.find((c) => c.connection_id === this.connectionId);
    const connTitle = activeConn ? activeConn.name : "未选择数据源";
    const models = this._getAvailableModels();

    // 1. 顶部超薄状态与历史会话切换条（不堆叠方块胶囊，仅回显左树勾选计数）
    const sessionSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "历史探查会话",
        style: { maxWidth: "230px" },
        disabled: this.loading,
        onChange: (e) => this.selectSession(e.target.value),
      },
      el("option", { value: "", text: "当前新会话" }),
      ...this.sessions.map((s) => {
        const opt = el("option", {
          value: s.session_id,
          text: `${s.title} (${s.turn_count || 0}轮)`,
        });
        if (this.currentSession?.session_id === s.session_id) {
          opt.selected = true;
        }
        return opt;
      })
    );

    const scopeBar = el(
      "div",
      { className: "explorer-scope-bar" },
      el(
        "div",
        { className: "explorer-scope-left" },
        el("span", { style: { fontWeight: "600" }, text: connTitle }),
        el("span", { style: { color: "var(--os-muted)" }, text: "·" }),
        this.focusTables.length > 0
          ? el(
              "span",
              {
                className: "explorer-scope-summary",
                title: this.focusTables.join(", "),
                text: `左树已勾选 ${this.focusTables.length} 张焦点表 (${this.focusTables.join(", ")})`,
              }
            )
          : el("span", {
              className: "explorer-scope-summary",
              text: "未勾选单表（全库自动召回，可在左侧数据库清单勾选焦点表）",
            }),
        this.focusTables.length > 0
          ? el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              text: "清空勾选",
              disabled: this.loading,
              onClick: () => this.onClearTreeFocus?.(),
            })
          : null
      ),
      el(
        "div",
        { className: "explorer-scope-right" },
        sessionSelect,
        this.currentSession?.session_id
          ? el(
              "button",
              {
                type: "button",
                className: "mac-btn mac-btn-sm mac-btn-ghost",
                title: "删除当前会话",
                disabled: this.loading,
                onClick: () => this.confirmDeleteCurrentSession(),
              },
              svgIcon("trash", 13)
            )
          : null
      )
    );

    // 2. 会话主体区
    const msgs = this.currentSession?.messages || [];
    let streamNode;

    if (!msgs.length && !this.loading) {
      streamNode = el(
        "div",
        { className: "explorer-empty-state" },
        !models.length
          ? el(
              "div",
              { className: "explorer-empty-card" },
              el("div", {
                style: { fontWeight: "600", marginBottom: "6px" },
                text: "尚未配置可用 AI 模型",
              }),
              el("button", {
                type: "button",
                className: "mac-btn mac-btn-sm mac-btn-primary",
                text: "前往「系统配置 → AI 模型」→",
                onClick: () => store.openApp("settings", { section: "llm" }),
              })
            )
          : el(
              "div",
              { className: "explorer-starter-wrap" },
              el(
                "div",
                { className: "explorer-starter-grid" },
                GOAL_STARTERS.map((st) =>
                  el("button", {
                    type: "button",
                    className: "explorer-starter-btn",
                    text: st.label,
                    onClick: () => this.submitExplore(st.prompt, st.goalType),
                  })
                )
              )
            )
      );
    } else {
      const turnNodes = msgs.map((m) => {
        if (m.role === "user") {
          return el(
            "div",
            { className: "explorer-turn-user" },
            el("div", { className: "explorer-bubble-user", text: m.content })
          );
        }
        return this._renderAssistantCard(m);
      });

      if (this.loading) {
        if (this._pendingUserPrompt) {
          turnNodes.push(
            el(
              "div",
              { className: "explorer-turn-user" },
              el("div", { className: "explorer-bubble-user", text: this._pendingUserPrompt })
            )
          );
        }
        turnNodes.push(
          el(
            "div",
            { className: "explorer-loading-row" },
            statusDot("正在只读沙箱内规划并执行多步采样 SQL...", "info")
          )
        );
      }

      streamNode = el("div", { className: "explorer-stream" }, turnNodes);
    }

    // 3. 底部输入区（严格遵循 IME 安全回车提交与自适应高度规范）
    let lastCompositionEndAt = null;
    const textareaEl = el("textarea", {
      id: this._promptId,
      className: "mac-input explorer-prompt-input",
      rows: "1",
      placeholder: "输入探查目标（Enter 发送，Shift+Enter 换行）...",
      value: this.draftPrompt,
      disabled: this.loading,
      onInput: (e) => {
        this.draftPrompt = e.target.value;
      },
      onCompositionend: (e) => {
        lastCompositionEndAt = e.timeStamp;
      },
      onKeydown: (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          if (e.isComposing || e.keyCode === 229) {
            return;
          }
          if (
            lastCompositionEndAt !== null &&
            Math.abs(e.timeStamp - lastCompositionEndAt) < 50
          ) {
            return;
          }
          e.currentTarget.form?.requestSubmit();
        }
      },
    });

    const composerForm = el(
      "form",
      {
        className: "explorer-composer",
        onSubmit: (e) => {
          e.preventDefault();
          this.submitExplore();
        },
      },
      el("label", {
        for: this._promptId,
        className: "visually-hidden",
        text: "输入数据探查问题",
      }),
      textareaEl,
      el(
        "button",
        {
          type: "submit",
          className: "mac-btn mac-btn-primary",
          disabled: this.loading,
        },
        svgIcon("send", 13),
        this.loading ? "探查中..." : "探查"
      )
    );

    clearEl(this.root, scopeBar, streamNode, composerForm);
  }
}
