/**
 * Data.O.G 单库落标贯标 — 结构化变更日志、Checkpoint 还原点与 LIFO 一键回滚面板
 */

import { migratorApi } from "../../api/migrator_api.js";
import { badge, clearEl, el, statusDot, svgIcon } from "../../core/dom.js";
import { isAbortError } from "../../core/http.js";
import { store } from "../../core/state.js";
import { DataGrid } from "../../ui/data_grid.js";
import { Modal } from "../../ui/modal.js";
import { SqlCodeBox } from "../../ui/sql_code_box.js";
import { Toast } from "../../ui/toast.js";

const RISK_SHORT_LABELS = {
  L1: "L1 改名",
  L2: "L2 转类型",
  L3: "L3 缩精度",
  L4: "L4 安全",
};

export function createRiskBadge(row) {
  const lvl = String(row?.risk_level || "L4").toUpperCase();
  const cls = `risk-badge risk-badge-${lvl.toLowerCase()}`;
  const label = row?.risk_short || RISK_SHORT_LABELS[lvl] || lvl;
  const factors = Array.isArray(row?.risk_factors) ? row.risk_factors.join("；") : "";
  const title = row?.risk_detail || factors || row?.risk_label || label;
  return el("span", { className: cls, title, text: label });
}

export class ChangelogPanel {
  constructor(parentView) {
    this.view = parentView;
    this.state = null;
    this.loading = false;
    this.riskFilter = "ALL";
    this.statusFilter = "ALL"; // ALL | APPLIED | ROLLED_BACK | ROLLBACK_EVENT
    this.keyword = "";
    this._searchTimer = null;
    this._grid = null;

    this.searchInput = el("input", {
      className: "mac-input mac-input-sm",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "筛选表名 / 字段 / 标准码 / 备份表...",
      style: { width: "220px" },
      onInput: (e) => {
        this.keyword = e.target.value;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => {
          if (!this.view._destroyed) this.load();
        }, 180);
      },
    });
  }

  destroy() {
    clearTimeout(this._searchTimer);
  }

  async load({ silent = false } = {}) {
    if (this.loading) return;
    this.loading = true;
    try {
      const res = await migratorApi.getChangelog({
        connectionId: "",
        riskLevel: this.riskFilter === "ALL" ? "" : this.riskFilter,
        status: this.statusFilter === "ALL" ? "" : this.statusFilter,
        keyword: this.keyword.trim(),
        limit: 300,
      });
      if (this.view._destroyed) return;
      this.state = res;
      if (this.view.subMode === "changelog") {
        this.render();
        this.view.onStateUpdate?.();
      }
    } catch (err) {
      if (!silent) Toast.error(err.message);
    } finally {
      this.loading = false;
    }
  }

  openCreateCheckpointModal(defaultConnId = "") {
    const connId = defaultConnId || this.view.activeConnId || this.view.ws?.active_connection_id || "";
    const conn = this.view._findConnection?.(connId);
    const nameInput = el("input", {
      className: "mac-input",
      placeholder: "例如：核心客户表贯标前还原点",
      value: `还原点 ${new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-")}`,
    });
    const descInput = el("input", {
      className: "mac-input",
      placeholder: "可选：记录此次打点原因或上线批次说明",
    });

    const body = el(
      "div",
      { className: "form-grid" },
      el(
        "div",
        { className: "form-field span-2" },
        el("label", { className: "form-label", text: "Checkpoint 名称 *" }),
        nameInput
      ),
      el(
        "div",
        { className: "form-field span-2" },
        el("label", { className: "form-label", text: "备注说明" }),
        descInput
      ),
      el(
        "div",
        { className: "form-field span-2", style: { fontSize: "11.5px", color: "var(--os-muted)" } },
        `作用范围：${conn ? `${conn.name} (${conn.db_type})` : "当前全部数据源"} · 创建后可在「变更日志与回滚」中一键回滚其后所有变更。`
      )
    );

    const saveBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-primary",
      text: "创建还原点",
      onClick: async () => {
        const name = nameInput.value.trim();
        if (!name) {
          Toast.warn("请输入 Checkpoint 名称");
          nameInput.focus();
          return;
        }
        try {
          saveBtn.disabled = true;
          const res = await migratorApi.createCheckpoint({
            name,
            description: descInput.value.trim(),
            connection_id: connId,
          });
          Modal.close("created");
          Toast.success(res.message || "Checkpoint 创建成功");
          await this.load({ silent: true });
        } catch (err) {
          Toast.error(err.message);
        } finally {
          saveBtn.disabled = false;
        }
      },
    });

    Modal.open({
      title: "创建落标 Checkpoint 还原点",
      body,
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close() }),
        saveBtn,
      ],
    });
  }

  async openRollbackPreviewModal({ checkpointId = null, changeIds = null, titlePrefix = "回滚预览" } = {}) {
    let preferBackup = true;

    const loadPreview = () =>
      migratorApi.previewRollback({
        checkpoint_id: checkpointId,
        change_ids: changeIds,
        prefer_backup_table: preferBackup,
      });

    let preview;
    try {
      preview = await loadPreview();
    } catch (err) {
      Toast.error(err.message);
      return;
    }

    const bodyMount = el("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } });
    const execBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-danger",
      text: "确认执行回滚",
    });

    const renderBody = (pv) => {
      const hasConflict = Boolean(pv.has_conflict);
      const steps = pv.steps || [];
      const warnings = pv.warnings || [];
      const canExec = !hasConflict && steps.length > 0 && Boolean(pv.can_execute_all_online);

      execBtn.disabled = !canExec;
      execBtn.textContent = steps.length
        ? `确认执行回滚 (${steps.length} 步 / ${pv.target_change_count || 0} 条)`
        : "无可回滚变更";

      const bakSwitch = el("input", {
        type: "checkbox",
        checked: preferBackup,
        onChange: async (e) => {
          preferBackup = Boolean(e.target.checked);
          try {
            preview = await loadPreview();
            renderBody(preview);
          } catch (err) {
            Toast.error(err.message);
          }
        },
      });

      const sqlBox = new SqlCodeBox({
        title: "LIFO 逆序回滚 SQL 预览",
        value: pv.combined_rollback_sql || "-- 暂无可执行的回滚 SQL",
        readOnly: true,
        minHeight: "200px",
        filename: checkpointId ? `rollback_checkpoint_${checkpointId}.sql` : "rollback_changes.sql",
      });

      clearEl(
        bodyMount,
        el(
          "div",
          { style: { display: "flex", gap: "6px", flexWrap: "wrap", alignItems: "center" } },
          badge(pv.mode === "CHECKPOINT" ? `还原点: ${pv.checkpoint?.name || "#" + checkpointId}` : "单条/批量撤销", "info"),
          badge(`涉及 ${pv.target_change_count || 0} 条变更 · ${steps.length} 个执行步`, "neutral"),
          pv.can_execute_all_online
            ? badge("支持在线直连回滚", "success")
            : badge("含离线/非原生连接，请复制 SQL 手工回滚", "warn")
        ),
        hasConflict
          ? el(
              "div",
              {
                style: {
                  padding: "8px 10px",
                  borderRadius: "8px",
                  border: "0.5px solid rgba(255,59,48,0.4)",
                  background: "rgba(255,59,48,0.09)",
                  color: "var(--status-danger)",
                  fontSize: "12px",
                },
                text: `冲突拦截：${pv.conflict_message || "存在更新的依赖变更"}`,
              }
            )
          : null,
        warnings.length > 0
          ? el(
              "div",
              {
                style: {
                  padding: "8px 10px",
                  borderRadius: "8px",
                  border: "0.5px solid rgba(255,159,10,0.4)",
                  background: "rgba(255,159,10,0.09)",
                  fontSize: "11.5px",
                  display: "flex",
                  flexDirection: "column",
                  gap: "3px",
                },
              },
              warnings.map((w) => el("div", { text: `提示：${w}` }))
            )
          : null,
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "7px", fontSize: "12px", cursor: "pointer" } },
          bakSwitch,
          el("span", { text: "若存在 _bak_ 重建备份表，优先使用备份表无损原子切回" })
        ),
        steps.length > 0
          ? el(
              "div",
              { className: "rollback-step-list" },
              steps.map((s) =>
                el(
                  "div",
                  { className: "rollback-step-item" },
                  el(
                    "div",
                    { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" } },
                    el("strong", { text: `Step ${s.step_index}. ${s.summary_text}` }),
                    el(
                      "div",
                      { style: { display: "inline-flex", gap: "4px" } },
                      badge(s.mode_label, s.mode === "BACKUP_SWAP" ? "success" : "info"),
                      s.backup_table_name && s.backup_status === "EXISTS"
                        ? badge("备份表就绪", "success")
                        : s.backup_table_name && s.backup_status === "MISSING"
                        ? badge("备份表缺失(已降级)", "warn")
                        : null
                    )
                  ),
                  ...(s.warnings || []).map((sw) =>
                    el("div", { style: { fontSize: "11px", color: "var(--status-warn)" }, text: sw })
                  )
                )
              )
            )
          : el("div", { className: "grid-empty", style: { padding: "16px" }, text: "该 Checkpoint 之后暂无可回滚的生效变更" }),
        sqlBox.root
      );
    };

    execBtn.onclick = async () => {
      try {
        execBtn.disabled = true;
        const res = await this.view._runTracked(
          checkpointId ? `一键回滚 Checkpoint #${checkpointId}` : "执行落标变更回滚",
          ({ taskId, signal }) =>
            migratorApi.executeRollback(
              {
                checkpoint_id: checkpointId,
                change_ids: changeIds,
                prefer_backup_table: preferBackup,
              },
              { taskId, signal }
            )
        );
        if (this.view._destroyed) return;
        Modal.close("done");
        if (res.changelog_state) this.state = res.changelog_state;
        if (res.workspace_state) {
          this.view.ws = res.workspace_state;
          if (res.workspace_state.logs) store.updateLogs(res.workspace_state.logs);
        }
        if (res.table_stats_by_conn && this.view.activeConnId && res.table_stats_by_conn[this.view.activeConnId]) {
          this.view.tableStats = res.table_stats_by_conn[this.view.activeConnId];
          this.view.tree?.renderTreeBody?.();
        }
        this.view._bindingsDirtyAfterMigration = true;
        if (res.ok) {
          Toast.success(res.message || "回滚执行完成");
        } else {
          Toast.warn(res.warning || res.error || "回滚在断点处中止");
        }
        await this.load({ silent: true });
        this.view.render();
        this.view.onStateUpdate?.();
      } catch (err) {
        if (isAbortError(err)) return;
        Toast.error(err.message);
      } finally {
        execBtn.disabled = false;
      }
    };

    renderBody(preview);
    Modal.open({
      title: titlePrefix,
      width: "min(860px, 94vw)",
      body: bodyMount,
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "关闭", onClick: () => Modal.close() }),
        execBtn,
      ],
    });
  }

  _openLogSqlModal(row) {
    const fwdBox = new SqlCodeBox({
      title: "正向落标 SQL",
      value: row.forward_sql || "-- 无正向 SQL 记录",
      readOnly: true,
      minHeight: "140px",
      filename: `forward_${row.schema_name}_${row.table_name}_${row.id}.sql`,
    });
    const rbBox = new SqlCodeBox({
      title: "对应逆向回滚 SQL",
      value: row.rollback_sql || "-- 无逆向回滚 SQL 记录",
      readOnly: true,
      minHeight: "140px",
      filename: `rollback_${row.schema_name}_${row.table_name}_${row.id}.sql`,
    });

    const body = el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "10px" } },
      el(
        "div",
        { style: { display: "flex", gap: "6px", flexWrap: "wrap", alignItems: "center" } },
        createRiskBadge(row),
        badge(`${row.schema_name}.${row.table_name}`, "neutral"),
        row.std_code ? badge(`标准: ${row.std_code}`, "info") : null,
        row.backup_table_name ? badge(`备份表: ${row.backup_table_name}`, "warn") : null,
        row.status === "ROLLED_BACK" ? badge(`已于 ${row.rolled_back_at} 回滚`, "muted") : badge("生效中", "success")
      ),
      fwdBox.root,
      rbBox.root
    );

    Modal.open({
      title: `变更日志 #${row.id} · ${row.before_col_name} → ${row.after_col_name}`,
      width: "min(800px, 92vw)",
      body,
      footer: [el("button", { type: "button", className: "mac-btn", text: "关闭", onClick: () => Modal.close() })],
    });
  }

  render() {
    const data = this.state || { logs: [], checkpoints: [], summary: {} };
    const summary = data.summary || {};
    const riskCounts = summary.risk_counts || {};
    const checkpoints = data.checkpoints || [];
    const logs = data.logs || [];

    this.searchInput.value = this.keyword;

    const statusPills = [
      { key: "ALL", label: `全部日志 ${summary.total_events ?? logs.length}` },
      { key: "APPLIED", label: `生效中 ${summary.applied_count ?? 0}` },
      { key: "ROLLED_BACK", label: `已回滚 ${summary.rolled_back_count ?? 0}` },
      { key: "ROLLBACK_EVENT", label: `回滚审计 ${summary.rollback_event_count ?? 0}` },
    ];
    const riskPills = [
      { key: "L1", label: `L1 改名 ${riskCounts.L1 ?? 0}` },
      { key: "L2", label: `L2 转类型 ${riskCounts.L2 ?? 0}` },
      { key: "L3", label: `L3 缩精度 ${riskCounts.L3 ?? 0}` },
      { key: "L4", label: `L4 安全 ${riskCounts.L4 ?? 0}` },
    ];

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el(
        "div",
        { className: "filter-pills" },
        statusPills.map((p) =>
          el("button", {
            type: "button",
            className: `filter-pill ${this.statusFilter === p.key ? "is-active" : ""}`,
            text: p.label,
            onClick: () => {
              this.statusFilter = p.key;
              this.load({ silent: true });
            },
          })
        ),
        riskPills.map((p) =>
          el("button", {
            type: "button",
            className: `filter-pill ${this.riskFilter === p.key ? "is-active" : ""}`,
            text: p.label,
            onClick: () => {
              this.riskFilter = this.riskFilter === p.key ? "ALL" : p.key;
              this.load({ silent: true });
            },
          })
        )
      ),
      el("div", { className: "mac-subbar-group" }, el("search", {}, this.searchInput))
    );

    const cpStrip = el(
      "div",
      { className: "checkpoint-strip-wrap" },
      el(
        "div",
        { className: "checkpoint-strip-head" },
        el(
          "div",
          { className: "checkpoint-strip-title" },
          svgIcon("bookmark", 13),
          el("span", { text: `Checkpoint 还原点 (${checkpoints.length})` })
        ),
        el(
          "button",
          {
            type: "button",
            className: "btn btn-xs",
            onClick: () => this.openCreateCheckpointModal(),
          },
          svgIcon("plus", 12),
          el("span", { text: "新建 Checkpoint" })
        )
      ),
      checkpoints.length
        ? el(
            "div",
            { className: "checkpoint-strip" },
            checkpoints.map((cp) => {
              const sinceCnt = Number(cp.changes_since_count || 0);
              return el(
                "div",
                { className: "checkpoint-card" },
                el(
                  "div",
                  { className: "checkpoint-card-top" },
                  el("div", { className: "checkpoint-card-name", title: cp.name, text: cp.name }),
                  badge(cp.auto_created ? "自动点" : "手动", cp.auto_created ? "info" : "neutral")
                ),
                el(
                  "div",
                  { className: "checkpoint-card-meta" },
                  el("span", { text: cp.created_at || "-" }),
                  el("span", { text: `· 锚点 #${cp.anchor_log_id}` }),
                  cp.connection_name ? el("span", { text: `· ${cp.connection_name}` }) : null
                ),
                cp.description
                  ? el("div", {
                      style: { fontSize: "11px", color: "var(--os-text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
                      title: cp.description,
                      text: cp.description,
                    })
                  : null,
                el(
                  "div",
                  { className: "checkpoint-card-actions" },
                  badge(
                    sinceCnt > 0 ? `其后 ${sinceCnt} 条待回滚` : "已是当前点",
                    sinceCnt > 0 ? "warn" : "success"
                  ),
                  el(
                    "div",
                    { style: { display: "inline-flex", gap: "4px" } },
                    el("button", {
                      type: "button",
                      className: "btn btn-xs",
                      disabled: sinceCnt <= 0,
                      title: sinceCnt > 0 ? "按逆序一键回滚该还原点之后的全部生效变更" : "当前还原点之后无待回滚变更",
                      text: "一键回滚",
                      onClick: () =>
                        this.openRollbackPreviewModal({
                          checkpointId: cp.id,
                          titlePrefix: `一键回滚至 Checkpoint · ${cp.name}`,
                        }),
                    }),
                    el("button", {
                      type: "button",
                      className: "btn btn-xs btn-ghost",
                      title: "仅删除此 Checkpoint 标记，不影响历史日志",
                      text: "删点",
                      onClick: async () => {
                        const ok = await Modal.confirm({
                          title: "删除 Checkpoint",
                          message: `确定删除还原点「${cp.name}」吗？（不会删除任何变更日志）`,
                          confirmText: "删除",
                          danger: true,
                        });
                        if (!ok) return;
                        try {
                          await migratorApi.deleteCheckpoint(cp.id);
                          Toast.success("Checkpoint 已删除");
                          await this.load({ silent: true });
                        } catch (err) {
                          Toast.error(err.message);
                        }
                      },
                    })
                  )
                )
              );
            })
          )
        : el("div", {
            style: { fontSize: "11.5px", color: "var(--os-muted)", padding: "4px 2px" },
            text: "暂无 Checkpoint 还原点；可在批量执行前自动打点，或点击右上角「新建 Checkpoint」记录安全基准点。",
          })
    );

    const grid = new DataGrid({
      ariaLabel: "落标结构化变更日志",
      rowKey: "id",
      rows: logs,
      emptyText: "暂无落标变更日志；完成单表、批量或重建贯标执行后，系统将自动在此留存精确的列前/列后变更台账。",
      rowClassName: (r) =>
        r.event_type === "MIGRATION"
          ? `queue-row-risk-${String(r.risk_level || "l4").toLowerCase()}`
          : "",
      columns: [
        { key: "id", label: "#", render: (r) => `#${r.id}` },
        {
          key: "created_at",
          label: "时间",
          render: (r) => el("span", { style: { fontSize: "11px", color: "var(--os-muted)" }, text: r.created_at || "-" }),
        },
        {
          key: "table_name",
          label: "库表",
          render: (r) =>
            el(
              "div",
              { style: { display: "flex", flexDirection: "column", gap: "2px" } },
              el("span", { className: "mono", text: `${r.schema_name}.${r.table_name}` }),
              r.connection_name || r.std_code
                ? el("span", {
                    style: { fontSize: "10.5px", color: "var(--os-muted)" },
                    text: [r.connection_name, r.std_code ? `标准:${r.std_code}` : ""].filter(Boolean).join(" · "),
                  })
                : null
            ),
        },
        {
          key: "before_col_name",
          label: "字段变更（前 → 后）",
          render: (r) => {
            const nameTxt =
              String(r.before_col_name || "").toLowerCase() === String(r.after_col_name || "").toLowerCase()
                ? String(r.after_col_name || r.before_col_name || "-")
                : `${r.before_col_name} → ${r.after_col_name}`;
            const typeTxt =
              String(r.before_col_type || "").toLowerCase() === String(r.after_col_type || "").toLowerCase()
                ? String(r.after_col_type || r.before_col_type || "-")
                : `${r.before_col_type} → ${r.after_col_type}`;
            return el(
              "div",
              {},
              el("div", { className: "mono", style: { fontWeight: "600" }, text: nameTxt }),
              el("div", { className: "mono", style: { fontSize: "11px", color: "var(--os-muted)" }, text: typeTxt })
            );
          },
        },
        {
          key: "risk_rank",
          label: "危险等级",
          render: (r) =>
            r.event_type === "ROLLBACK" ? badge("回滚审计", "info") : createRiskBadge(r),
        },
        {
          key: "strategy",
          label: "策略 / 备份表",
          render: (r) =>
            el(
              "div",
              { style: { display: "flex", flexDirection: "column", gap: "2px" } },
              badge(
                r.strategy === "TABLE_REBUILD"
                  ? "保序重建"
                  : r.strategy === "BACKUP_SWAP"
                  ? "备份切回"
                  : r.strategy === "REVERSE_ALTER"
                  ? "逆向 DDL"
                  : "原地 ALTER",
                r.strategy === "TABLE_REBUILD" ? "warn" : "neutral"
              ),
              r.backup_table_name
                ? el("span", {
                    className: "mono",
                    style: { fontSize: "10.5px", color: "var(--os-muted)" },
                    title: r.backup_table_name,
                    text: r.backup_table_name,
                  })
                : null
            ),
        },
        {
          key: "status",
          label: "状态",
          render: (r) => {
            if (r.event_type === "ROLLBACK") {
              return statusDot("已执行回滚", "info");
            }
            if (r.status === "ROLLED_BACK") {
              return el(
                "span",
                { title: r.rolled_back_at ? `已于 ${r.rolled_back_at} 回滚` : "已回滚" },
                statusDot("已回滚", "muted")
              );
            }
            return statusDot("生效中", "success");
          },
        },
        {
          key: "actions",
          label: "操作",
          sortable: false,
          render: (r) =>
            el(
              "div",
              { style: { display: "flex", gap: "4px" } },
              el("button", {
                type: "button",
                className: "btn btn-xs btn-ghost",
                text: "SQL",
                onClick: (e) => {
                  e.stopPropagation();
                  this._openLogSqlModal(r);
                },
              }),
              r.event_type === "MIGRATION" && r.status === "APPLIED"
                ? el("button", {
                    type: "button",
                    className: "btn btn-xs",
                    text: "撤销",
                    onClick: (e) => {
                      e.stopPropagation();
                      this.openRollbackPreviewModal({
                        changeIds: [r.id],
                        titlePrefix: `撤销变更 #${r.id} · ${r.schema_name}.${r.table_name}`,
                      });
                    },
                  })
                : null
            ),
        },
      ],
    });

    clearEl(this.view.subbarMount, subbar);
    clearEl(this.view.gridMount, cpStrip, grid.root);
    this._grid = grid;
  }
}
