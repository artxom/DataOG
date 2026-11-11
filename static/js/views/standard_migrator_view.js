/**
 * DataOG 独立业务插件视图 1：单库批量落标贯标执行台 (modules/standard_migrator)
 * 完整保留：
 * - 模式 A (DIRECT_ALTER) 原生列改名/改类型/改注释 + attnum 物理列序校验
 * - 模式 B (TABLE_REBUILD) 分布键/分区键/列存表整表按原物理列序影子表无损重建 + Kahn 拓扑排序 + period 分区继承
 * - 5 线程按表并发批量落标
 */

import { migratorApi } from "../core/api.js";
import { badge, clearEl, el, pickLocalFile } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { Modal } from "../ui/modal.js";
import { SqlCodeBox } from "../ui/sql_code_box.js";
import { Toast } from "../ui/toast.js";

const STATUS_META = {
  COMPLETED: { label: "已达标完成", tone: "success" },
  PENDING: { label: "待执行改造", tone: "warn" },
  PARTIAL: { label: "部分合规待补齐", tone: "warn" },
  FAILED: { label: "执行报错", tone: "danger" },
  MISSING_TABLE: { label: "目标表不存在", tone: "danger" },
  MISSING_COLUMN: { label: "原字段不存在", tone: "danger" },
  UNCHECKED: { label: "未连库核验", tone: "neutral" },
};

export class StandardMigratorView {
  constructor() {
    this.ws = null;
    this.statusFilter = "ALL";
    this.searchKeyword = "";
    this.selectedIds = new Set();
    this.maxWorkers = 5;
    this.keepBackup = true;
    this.root = el("div", { className: "module-page" });
  }

  async mount(container) {
    clearEl(container, this.root);
    await this.reloadState();
  }

  async reloadState() {
    try {
      const res = await migratorApi.getState();
      this.ws = res;
      if (res.logs) store.updateLogs(res.logs);
      this.render();
    } catch (err) {
      Toast.error(`加载贯标台状态失败: ${err.message}`);
    }
  }

  _applyStateResponse(res) {
    this.ws = res;
    if (res.logs) store.updateLogs(res.logs);
    if (res.warning) Toast.warn(res.warning);
    else if (res.message) Toast.success(res.message);
    this.render();
  }

  async _openLoadFromDbModal() {
    const schemaInput = el("input", {
      className: "input",
      placeholder: "留空则按默认模式读取 (如 public)",
      value: this.ws?.active_connection?.default_schema || "",
    });
    const tableInput = el("input", {
      className: "input",
      placeholder: "可选：指定单张表名精确读取",
    });
    const maxInput = el("input", {
      className: "input",
      type: "number",
      value: "30",
      min: "1",
      max: "200",
    });

    const body = el(
      "div",
      { className: "form-grid" },
      el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "模式名 (Schema)" }), schemaInput),
      el("div", { className: "form-field" }, el("label", { className: "form-label", text: "指定表名 (可选)" }), tableInput),
      el("div", { className: "form-field" }, el("label", { className: "form-label", text: "最多载入表数量" }), maxInput)
    );

    const okBtn = el("button", {
      type: "button",
      className: "btn btn-primary",
      text: "读取库表字段载入工作台",
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
      title: "从当前数据源反向读取物理表结构",
      body,
      footer: [
        el("button", { type: "button", className: "btn btn-secondary", text: "取消", onClick: () => Modal.close() }),
        okBtn,
      ],
    });
  }

  async _openRebuildPreviewModal(schemaName, tableName) {
    try {
      const preview = await migratorApi.previewRebuild({
        schema_name: schemaName,
        table_name: tableName,
        keep_backup: this.keepBackup,
      });
      const sqlBox = new SqlCodeBox({
        title: `整表保序影子表重建事务脚本 (${schemaName}.${tableName})`,
        value: preview.rebuild_sql || preview.sql || "-- 暂无重建 SQL",
        readOnly: true,
        minHeight: "290px",
        filename: `rebuild_${schemaName}_${tableName}.sql`,
      });

      const backupCb = el("input", {
        type: "checkbox",
        checked: this.keepBackup,
        onChange: (e) => {
          this.keepBackup = e.target.checked;
        },
      });

      const body = el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "10px" } },
        el(
          "div",
          { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
          badge(`原表总列数: ${preview.column_count ?? "-"}`, "info"),
          badge(`分布策略: ${preview.distribute_clause || "默认继承"}`, "warn"),
          preview.partition_clause ? badge("包含原表分区定义继承", "success") : null
        ),
        sqlBox.root,
        el(
          "label",
          { style: { display: "flex", alignItems: "center", gap: "8px", fontSize: "12px" } },
          backupCb,
          el("span", { text: "执行重建切换后保留带时间戳的原表备份 (_bak_...)" })
        )
      );

      const execBtn = el("button", {
        type: "button",
        className: "btn btn-primary",
        text: "确认执行整表保序无损重建",
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
        title: `按原物理列序 (attnum) 重建表 — ${schemaName}.${tableName}`,
        width: "min(860px, 94vw)",
        body,
        footer: [
          el("button", { type: "button", className: "btn btn-secondary", text: "关闭预览", onClick: () => Modal.close() }),
          execBtn,
        ],
      });
    } catch (err) {
      Toast.error(`生成重建预览失败: ${err.message}`);
    }
  }

  _filteredItems() {
    const all = this.ws?.items || [];
    const kw = this.searchKeyword.trim().toLowerCase();
    return all.filter((it) => {
      if (this.statusFilter === "REBUILD") {
        if (it.migration_mode !== "TABLE_REBUILD" && !it.is_distribute_key) return false;
      } else if (this.statusFilter !== "ALL" && it.status !== this.statusFilter) {
        return false;
      }
      if (!kw) return true;
      return [
        it.schema_name,
        it.table_name,
        it.old_col_name,
        it.new_col_name,
        it.old_col_comment,
        it.new_col_comment,
      ].some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  render() {
    const ws = this.ws || { connections: [], items: [], summary: {} };
    const conns = ws.connections?.length ? ws.connections : store.state.connections || [];
    const summary = ws.summary || {};

    const connSelect = el(
      "select",
      {
        className: "input input-sm",
        "aria-label": "绑定目标数据库",
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
        el("option", { value: "", text: "-- 选择贯标目标库连接 --" }),
        ...conns.map((c) => {
          const o = el("option", { value: c.connection_id, text: `${c.name} [${c.db_type}]` });
          if (c.connection_id === ws.active_connection_id) o.selected = true;
          return o;
        }),
      ]
    );

    const toolbar = el(
      "div",
      { className: "module-toolbar" },
      el(
        "div",
        { className: "module-toolbar-left" },
        el("h1", { className: "module-title", text: "单库批量落标贯标执行台" }),
        connSelect,
        badge(`当前清单: ${ws.excel_name || "未导入"}`, "neutral")
      ),
      el(
        "div",
        { className: "module-toolbar-right" },
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "下载导入模板",
          onClick: () => migratorApi.downloadTemplate().catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "上传 Excel 清单",
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
          className: "btn btn-sm btn-secondary",
          text: "从库加载表",
          onClick: () => this._openLoadFromDbModal(),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "与目标库同步核验",
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
          className: "btn btn-sm btn-primary",
          text: `并发执行选中 (${this.selectedIds.size})`,
          onClick: async () => {
            const ids = [...this.selectedIds].map((x) => Number(x)).filter((n) => n > 0);
            if (!ids.length) {
              Toast.warn("请先在下方表格勾选至少一条待执行改造的字段记录");
              return;
            }
            const ok = await Modal.confirm({
              title: "执行多线程批量贯标改造",
              message: `即将启动 ${this.maxWorkers} 个工作线程对选中的 ${ids.length} 条字段规则执行保序落标（分布键/分区键将自动切换为整表保序重建），是否继续？`,
              confirmText: "立即启动批量执行",
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
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "导出报告",
          onClick: () => migratorApi.exportReport().catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-danger",
          text: "清空",
          onClick: async () => {
            const ok = await Modal.confirm({
              title: "清空贯标清单",
              message: "确定清空当前工作台中的所有字段贯标记录吗？",
              confirmText: "清空清单",
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

    const kpiDefs = [
      { key: "ALL", label: "全部字段规则", val: summary.total ?? (ws.items || []).length },
      { key: "PENDING", label: "待执行改造", val: summary.pending ?? 0 },
      { key: "PARTIAL", label: "部分合规待补齐", val: summary.partial ?? 0 },
      { key: "COMPLETED", label: "已贯标达标", val: summary.completed ?? 0 },
      { key: "REBUILD", label: "涉及分布/分区键重建", val: summary.rebuild_required ?? 0 },
      { key: "FAILED", label: "异常或缺失", val: summary.failed ?? 0 },
    ];

    const kpiRow = el(
      "div",
      { className: "kpi-grid" },
      kpiDefs.map((k) =>
        el(
          "div",
          {
            className: `kpi-card ${this.statusFilter === k.key ? "is-active" : ""}`,
            onClick: () => {
              this.statusFilter = k.key;
              this.render();
            },
          },
          el("div", { className: "kpi-label", text: k.label }),
          el("div", { className: "kpi-value", text: String(k.val) })
        )
      )
    );

    const searchBar = el(
      "div",
      { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px" } },
      el("input", {
        className: "input input-sm",
        style: { width: "360px" },
        placeholder: "按模式名 / 表名 / 原字段名 / 目标标准字段名 / 注释过滤...",
        value: this.searchKeyword,
        onInput: (e) => {
          this.searchKeyword = e.target.value;
          this.render();
        },
      }),
      el(
        "div",
        { style: { display: "flex", alignItems: "center", gap: "8px" } },
        el("span", { style: { fontSize: "12px", color: "var(--text-secondary)" }, text: "并发线程数:" }),
        el(
          "select",
          {
            className: "input input-xs",
            onChange: (e) => {
              this.maxWorkers = Number(e.target.value) || 5;
            },
          },
          [1, 3, 5, 8].map((n) => {
            const o = el("option", { value: String(n), text: `${n} 线程` });
            if (this.maxWorkers === n) o.selected = true;
            return o;
          })
        )
      )
    );

    const rows = this._filteredItems();
    const grid = new DataGrid({
      ariaLabel: "字段贯标任务明细表",
      selectable: true,
      selectedKeys: this.selectedIds,
      onSelectionChange: () => this.render(),
      rowKey: "id",
      rows,
      emptyText: "当前暂无贯标字段清单，请点击「上传 Excel 清单」、「从库加载表」或从「跨环境三栏比对中心」推送待改造项",
      maxHeight: "56vh",
      columns: [
        {
          key: "table_name",
          label: "模式.表名",
          render: (r) => `${r.schema_name}.${r.table_name}`,
        },
        {
          key: "ordinal_position",
          label: "列序",
          render: (r) => (r.ordinal_position ? `#${r.ordinal_position}` : "-"),
        },
        {
          key: "old_col_name",
          label: "库内原字段 → 标准字段",
          render: (r) =>
            el(
              "div",
              {},
              el("div", {
                style: { fontWeight: "700", fontFamily: "var(--font-mono)" },
                text: `${r.current_db_col_name || r.old_col_name} → ${r.new_col_name}`,
              }),
              el("div", {
                style: { fontSize: "11px", color: "var(--text-secondary)" },
                text: `${r.current_db_col_type || r.old_col_type || "?"} → ${r.new_col_type}`,
              })
            ),
        },
        {
          key: "new_col_comment",
          label: "目标标准注释",
          render: (r) => r.new_col_comment || r.old_col_comment || "-",
        },
        {
          key: "migration_mode",
          label: "保序策略",
          render: (r) =>
            r.migration_mode === "TABLE_REBUILD" || r.is_distribute_key
              ? badge("TABLE_REBUILD 整表保序", "warn")
              : badge("DIRECT_ALTER 原地修改", "info"),
        },
        {
          key: "status",
          label: "核验/执行状态",
          render: (r) => {
            const m = STATUS_META[r.status] || { label: r.status || "未知", tone: "neutral" };
            return el(
              "div",
              { title: r.last_error || r.sync_detail || "" },
              badge(m.label, m.tone)
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
              { style: { display: "flex", gap: "5px" } },
              el("button", {
                type: "button",
                className: "btn btn-xs btn-secondary",
                text: "单条执行",
                disabled: r.status === "COMPLETED",
                onClick: async (e) => {
                  e.stopPropagation();
                  if (r.migration_mode === "TABLE_REBUILD" || r.is_distribute_key) {
                    await this._openRebuildPreviewModal(r.schema_name, r.table_name);
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
                text: "重建预览",
                onClick: (e) => {
                  e.stopPropagation();
                  this._openRebuildPreviewModal(r.schema_name, r.table_name);
                },
              })
            ),
        },
      ],
    });

    const content = el("div", { className: "module-content" }, kpiRow, searchBar, grid.root);
    clearEl(this.root, toolbar, content);
  }
}
