/**
 * Data.O.G 数据标准子面板 A：单库贯标执行 (StandardMigratorView)
 * 100% 保留物理列保序重建 (TABLE_REBUILD)、Kahn 拓扑改名、分区继承与多线程批量并发
 */

import { migratorApi } from "../core/api.js";
import { badge, clearEl, el, pickLocalFile, statusDot } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { Modal } from "../ui/modal.js";
import { SqlCodeBox } from "../ui/sql_code_box.js";
import { Toast } from "../ui/toast.js";

const STATUS_META = {
  COMPLETED: { label: "已达标", tone: "success" },
  PENDING: { label: "待改造", tone: "warn" },
  PARTIAL: { label: "部分合规", tone: "warn" },
  FAILED: { label: "失败", tone: "danger" },
  MISSING_TABLE: { label: "缺表", tone: "danger" },
  MISSING_COLUMN: { label: "缺列", tone: "danger" },
  UNCHECKED: { label: "未核验", tone: "muted" },
};

export class StandardMigratorView {
  constructor({ onStateUpdate = null } = {}) {
    this.onStateUpdate = onStateUpdate;
    this.ws = null;
    this.statusFilter = "ALL";
    this.searchKeyword = "";
    this.selectedIds = new Set();
    this.maxWorkers = 5;
    this.keepBackup = true;
    this.loadedOnce = false;

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    this.subbarMount = el("div", {});
    this.gridMount = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });

    this.searchInput = el("input", {
      className: "mac-input mac-input-sm",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "筛选表名 / 字段 / 注释...",
      style: { width: "200px" },
      onInput: (e) => {
        this.searchKeyword = e.target.value;
        this._renderSubbarAndGrid();
      },
    });
  }

  _isRebuildItem(it) {
    return (
      it?.strategy === "TABLE_REBUILD" ||
      it?.migration_mode === "TABLE_REBUILD" ||
      Boolean(it?.is_distribute_key) ||
      String(it?.table_orientation || "").toUpperCase() === "COLUMN"
    );
  }

  async ensureLoaded(force = false) {
    if (this.loadedOnce && !force) return;
    await this.reloadState();
  }

  async reloadState() {
    try {
      const res = await migratorApi.getState();
      this.loadedOnce = true;
      this.ws = res;
      if (res.logs) store.updateLogs(res.logs);
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _applyStateResponse(res) {
    this.ws = res;
    if (res.logs) store.updateLogs(res.logs);
    if (res.warning) Toast.warn(res.warning);
    else if (res.message) Toast.success(res.message);
    this.render();
    this.onStateUpdate?.();
  }

  renderHeaderActions() {
    const ws = this.ws || { connections: [] };
    const conns = ws.connections?.length ? ws.connections : store.state.connections || [];

    const connSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "贯标目标库",
        onChange: async (e) => {
          const cid = e.target.value;
          if (!cid) return;
          try {
            const res = await migratorApi.bindConnection(cid, true);
            this._applyStateResponse(res);
          } catch (err) {
            Toast.error(err.message);
          }
        },
      },
      [
        el("option", { value: "", text: "选择目标库" }),
        ...conns.map((c) => {
          const o = el("option", { value: c.connection_id, text: `${c.name} · ${c.db_type}` });
          if (c.connection_id === ws.active_connection_id) o.selected = true;
          return o;
        }),
      ]
    );

    return el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      connSelect,
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "导入 Excel",
        onClick: async () => {
          const f = await pickLocalFile(".xlsx");
          if (!f) return;
          try {
            const res = await migratorApi.uploadChecklist(f);
            this._applyStateResponse(res);
          } catch (err) {
            Toast.error(err.message);
          }
        },
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "从库读取",
        onClick: () => this.openLoadFromDbModal(),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "同步",
        onClick: async () => {
          try {
            const res = await migratorApi.sync();
            this._applyStateResponse(res);
          } catch (err) {
            Toast.error(err.message);
          }
        },
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm mac-btn-primary",
        text: `执行 (${this.selectedIds.size})`,
        onClick: () => this.executeSelectedBatch(),
      })
    );
  }

  async executeSelectedBatch() {
    const ids = [...this.selectedIds].map((x) => Number(x)).filter((n) => n > 0);
    if (!ids.length) {
      Toast.warn("请先勾选待执行记录");
      return;
    }
    const ok = await Modal.confirm({
      title: "批量执行贯标",
      message: `将启动 ${this.maxWorkers} 个并发线程执行已选中的 ${ids.length} 条字段规则（涉及分布键或列存表自动采用整表保序重建），确认继续？`,
      confirmText: "开始执行",
    });
    if (!ok) return;
    try {
      const res = await migratorApi.executeBatch({
        item_ids: ids,
        max_workers: this.maxWorkers,
        keep_backup: this.keepBackup,
      });
      this.selectedIds.clear();
      this._applyStateResponse(res);
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async openLoadFromDbModal() {
    const schemaInput = el("input", {
      className: "mac-input",
      placeholder: "默认 Schema",
      value: this.ws?.active_connection?.default_schema || "",
    });
    const tableInput = el("input", {
      className: "mac-input",
      placeholder: "留空读取多表",
    });
    const maxInput = el("input", {
      className: "mac-input",
      type: "number",
      value: "30",
      min: "1",
      max: "200",
    });

    const body = el(
      "div",
      { className: "form-grid" },
      el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "Schema" }), schemaInput),
      el("div", { className: "form-field" }, el("label", { className: "form-label", text: "指定表名" }), tableInput),
      el("div", { className: "form-field" }, el("label", { className: "form-label", text: "最大表数" }), maxInput)
    );

    const okBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-primary",
      text: "载入",
      onClick: async () => {
        try {
          okBtn.disabled = true;
          const res = await migratorApi.loadFromDb({
            connection_id: this.ws?.active_connection_id || null,
            schema_name: schemaInput.value.trim(),
            table_name: tableInput.value.trim(),
            max_tables: Number(maxInput.value) || 30,
          });
          Modal.close("ok");
          this._applyStateResponse(res);
        } catch (err) {
          Toast.error(err.message);
        } finally {
          okBtn.disabled = false;
        }
      },
    });

    Modal.open({
      title: "从数据库载入表结构",
      body,
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close() }),
        okBtn,
      ],
    });
  }

  async openRebuildPreviewModal(schemaName, tableName) {
    try {
      const preview = await migratorApi.previewRebuild({
        schema_name: schemaName,
        table_name: tableName,
        keep_backup: this.keepBackup,
      });
      const sqlBox = new SqlCodeBox({
        title: `${schemaName}.${tableName}`,
        value: preview.sql_preview || preview.rebuild_sql || preview.sql || "-- 暂无重建 SQL",
        readOnly: true,
        minHeight: "280px",
        filename: `rebuild_${schemaName}_${tableName}.sql`,
      });

      const backupCb = el("input", {
        type: "checkbox",
        checked: this.keepBackup,
        onChange: (e) => (this.keepBackup = e.target.checked),
      });

      const body = el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "10px" } },
        el(
          "div",
          { style: { display: "flex", gap: "6px", flexWrap: "wrap" } },
          badge(`${preview.column_count ?? "-"} 列`, "info"),
          badge(preview.distribute_clause || "继承原表分布", "warn"),
          preview.partition_clause ? badge("继承分区边界", "success") : null
        ),
        sqlBox.root,
        el(
          "label",
          { style: { display: "flex", alignItems: "center", gap: "7px", fontSize: "12px" } },
          backupCb,
          el("span", { text: "保留原表时间戳备份 (_bak_...)" })
        )
      );

      const execBtn = el("button", {
        type: "button",
        className: "mac-btn mac-btn-primary",
        text: "执行保序重建",
        onClick: async () => {
          try {
            execBtn.disabled = true;
            const res = await migratorApi.executeRebuild({
              schema_name: schemaName,
              table_name: tableName,
              keep_backup: this.keepBackup,
            });
            Modal.close("done");
            this._applyStateResponse(res);
          } catch (err) {
            Toast.error(err.message);
          } finally {
            execBtn.disabled = false;
          }
        },
      });

      Modal.open({
        title: `整表保序重建预览 · ${schemaName}.${tableName}`,
        width: "min(820px, 94vw)",
        body,
        footer: [
          el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close() }),
          execBtn,
        ],
      });
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _filteredItems() {
    const all = this.ws?.items || [];
    const kw = this.searchKeyword.trim().toLowerCase();
    return all.filter((it) => {
      if (this.statusFilter === "REBUILD") {
        if (!this._isRebuildItem(it)) return false;
      } else if (this.statusFilter === "FAILED") {
        if (!["FAILED", "MISSING_TABLE", "MISSING_COLUMN"].includes(it.status)) return false;
      } else if (this.statusFilter !== "ALL" && it.status !== this.statusFilter) {
        return false;
      }
      if (!kw) return true;
      return [
        it.schema_name,
        it.table_name,
        it.old_col_name,
        it.db_current_col_name,
        it.new_col_name,
        it.old_col_comment,
        it.new_col_comment,
      ].some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  render() {
    this.searchInput.value = this.searchKeyword;
    this._renderSubbarAndGrid();
    clearEl(this.root, this.subbarMount, this.gridMount);
  }

  _renderSubbarAndGrid() {
    const ws = this.ws || { items: [], summary: {} };
    const summary = ws.summary || {};
    const rebuildCount =
      summary.rebuild_required ??
      (ws.items || []).filter((it) => it.status !== "COMPLETED" && this._isRebuildItem(it)).length;
    const failedCount = (summary.failed ?? 0) + (summary.missing ?? 0);

    const pills = [
      { key: "ALL", label: `全部 ${summary.total ?? (ws.items || []).length}` },
      { key: "PENDING", label: `待改 ${summary.pending ?? 0}` },
      { key: "PARTIAL", label: `部分合规 ${summary.partial ?? 0}` },
      { key: "REBUILD", label: `整表重建 ${rebuildCount}` },
      { key: "COMPLETED", label: `已达标 ${summary.completed ?? 0}` },
      { key: "FAILED", label: `异常 ${failedCount}` },
    ];

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el(
        "div",
        { className: "filter-pills" },
        pills.map((p) =>
          el("button", {
            type: "button",
            className: `filter-pill ${this.statusFilter === p.key ? "is-active" : ""}`,
            text: p.label,
            onClick: () => {
              this.statusFilter = p.key;
              this._renderSubbarAndGrid();
            },
          })
        )
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        el("search", {}, this.searchInput),
        el(
          "select",
          {
            className: "mac-input mac-input-sm",
            "aria-label": "并发线程",
            onChange: (e) => (this.maxWorkers = Number(e.target.value) || 5),
          },
          [1, 3, 5, 8].map((n) => {
            const o = el("option", { value: String(n), text: `${n} 线程` });
            if (this.maxWorkers === n) o.selected = true;
            return o;
          })
        ),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "模板",
          onClick: () => migratorApi.downloadTemplate().catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "导出报告",
          onClick: () => migratorApi.exportReport().catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-danger",
          text: "清空",
          onClick: async () => {
            const ok = await Modal.confirm({
              title: "清空清单",
              message: "确定清空当前贯标清单吗？",
              confirmText: "清空",
              danger: true,
            });
            if (!ok) return;
            const res = await migratorApi.clearItems();
            this.selectedIds.clear();
            this._applyStateResponse(res);
          },
        })
      )
    );

    const rows = this._filteredItems();
    const grid = new DataGrid({
      ariaLabel: "贯标字段清单",
      selectable: true,
      selectedKeys: this.selectedIds,
      onSelectionChange: () => this.onStateUpdate?.(),
      rowKey: "id",
      rows,
      emptyText: "暂无贯标记录，可点击右上角「导入 Excel」、「从库读取」或从「比对」推送",
      columns: [
        { key: "table_name", label: "库表", render: (r) => el("span", { className: "mono", text: `${r.schema_name}.${r.table_name}` }) },
        { key: "ordinal_position", label: "#", render: (r) => (r.ordinal_position ? String(r.ordinal_position) : "-") },
        {
          key: "old_col_name",
          label: "原字段 → 标准字段",
          render: (r) =>
            el(
              "div",
              {},
              el("div", {
                className: "mono",
                style: { fontWeight: "600" },
                text: `${r.db_current_col_name || r.current_db_col_name || r.old_col_name} → ${r.new_col_name}`,
              }),
              el("div", {
                className: "mono",
                style: { fontSize: "11px", color: "var(--os-muted)" },
                text: `${r.db_current_type || r.current_db_col_type || r.old_col_type || "?"} → ${r.new_col_type}`,
              })
            ),
        },
        { key: "new_col_comment", label: "注释", render: (r) => r.new_col_comment || r.old_col_comment || "-" },
        {
          key: "strategy",
          label: "策略",
          render: (r) =>
            this._isRebuildItem(r) ? badge("保序重建", "warn") : badge("原地修改", "neutral"),
        },
        {
          key: "status",
          label: "状态",
          render: (r) => {
            const m = STATUS_META[r.status] || { label: r.status || "-", tone: "muted" };
            return el(
              "span",
              { title: r.error_message || r.status_detail || r.last_error || r.sync_detail || "" },
              statusDot(m.label, m.tone)
            );
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
                className: "btn btn-xs",
                text: "执行",
                disabled: r.status === "COMPLETED",
                onClick: async (e) => {
                  e.stopPropagation();
                  if (this._isRebuildItem(r)) {
                    await this.openRebuildPreviewModal(r.schema_name, r.table_name);
                    return;
                  }
                  try {
                    const res = await migratorApi.executeSingle(r.id);
                    this._applyStateResponse(res);
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-ghost",
                text: "SQL",
                onClick: (e) => {
                  e.stopPropagation();
                  this.openRebuildPreviewModal(r.schema_name, r.table_name);
                },
              })
            ),
        },
      ],
    });

    clearEl(this.subbarMount, subbar);
    clearEl(this.gridMount, grid.root);
  }
}
