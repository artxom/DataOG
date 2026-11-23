/**
 * Data.O.G 数据标准第 2 步：落标绑定与贯标 (StandardMigratorView)
 * 左树（DatabaseTree + 绑标/落标徽标）× 右工作台（「字段落标绑定与核验」 | 「贯标改造执行队列」）
 * 100% 保留物理列保序重建 (TABLE_REBUILD)、Kahn 拓扑改名、分区继承与多线程批量并发
 */


import { migratorApi } from "../api/migrator_api.js";
import { isAbortError } from "../core/http.js";
import { platformApi } from "../core/platform_api.js";
import { badge, clearEl, el, statusDot, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { DataGrid } from "../ui/data_grid.js";
import { DatabaseTree } from "../ui/database_tree.js";
import { Modal } from "../ui/modal.js";
import { openStandardPickerModal } from "../ui/standard_picker_modal.js";
import { TaskActivityBar } from "../ui/task_activity_bar.js";
import { Toast } from "../ui/toast.js";
import { StandardMigratorModalsMixin } from "./standard_migrator/modals.js";

function makeAbortError() {
  const err = new Error("AbortError");
  err.name = "AbortError";
  return err;
}

const MIG_STATUS_META = {
  COMPLETED: { label: "已达标", tone: "success" },
  PENDING: { label: "待改造", tone: "warn" },
  PARTIAL: { label: "部分合规", tone: "warn" },
  FAILED: { label: "失败", tone: "danger" },
  MISSING_TABLE: { label: "缺表", tone: "danger" },
  MISSING_COLUMN: { label: "缺列", tone: "danger" },
  UNCHECKED: { label: "未核验", tone: "muted" },
};

const COMP_STATUS_META = {
  COMPLIANT: { label: "已落标", tone: "success" },
  COMMENT_ONLY: { label: "仅差注释", tone: "info" },
  NON_COMPLIANT: { label: "未落标", tone: "danger" },
  STD_MISSING: { label: "标准缺失", tone: "warn" },
  UNBOUND: { label: "未绑标", tone: "muted" },
};

const DEFAULT_FORMAT_PROFILES = [
  { value: "AUTO", label: "自动按库匹配" },
  { value: "type_dws", label: "仓库DWS数据格式" },
  { value: "type_dw_mart", label: "数据仓库 集市" },
  { value: "type_argodb", label: "argodb数据格式" },
  { value: "type_mysql", label: "Mysql物理类型" },
  { value: "type_oracle", label: "Oracle物理类型" },
  { value: "type_ob_oracle", label: "ocenbase-oracle数据格式" },
  { value: "type_ob_mysql", label: "ocenbase-mysql数据格式" },
  { value: "type_oceanbase", label: "ocenbase数据格式" },
  { value: "type_dameng", label: "DaMeng数据格式" },
  { value: "type_goldendb", label: "GoldenDb(MySQL模式)" },
];

const SHORT_FORMAT_LABELS = {
  AUTO: "自动",
  type_dws: "DWS",
  type_dw_mart: "集市",
  type_argodb: "Argo",
  type_mysql: "MySQL",
  type_oracle: "Oracle",
  type_ob_oracle: "OB-O",
  type_ob_mysql: "OB-M",
  type_oceanbase: "OB",
  type_dameng: "达梦",
  type_goldendb: "Golden",
};

export class StandardMigratorView {
  constructor({ onStateUpdate = null } = {}) {
    this.onStateUpdate = onStateUpdate;
    this.subMode = "bindings"; // bindings | queue
    this.loadedOnce = false;

    // 绑定与落标检查状态
    this.activeConnId = "";
    this.activeSchema = "";
    this.activeTable = "";
    this.formatProfile = "AUTO";
    this.formatProfiles = DEFAULT_FORMAT_PROFILES;
    this.tableStats = {};
    this.tableCheck = null;
    this.bindingFilter = "ALL"; // ALL | COMPLIANT | COMMENT_ONLY | NON_COMPLIANT | UNBOUND
    this.bindingKeyword = "";
    this.selectedBindCols = new Set();
    this._stagingInFlight = false;
    this._bindingsDirtyAfterMigration = false;
    this._bindingSearchTimer = null;
    this._queueSearchTimer = null;
    this._bindingStageBtn = null;
    this._queueExecBatchBtn = null;
    this._queueRemoveBtn = null;

    // 贯标改造执行队列状态
    this.ws = null;
    this.statusFilter = "ALL";
    this.searchKeyword = "";
    this.selectedIds = new Set();
    this.maxWorkers = 5;
    this.keepBackup = true;

    this.root = el("div", {
      className: "mac-split-view",
    });
    this.sidebarMount = el("aside", { className: "mac-sidebar mac-sidebar-wide" });
    this.sidebarFormatMount = el("div", { className: "sidebar-format-inspector" });
    this.treeMount = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    clearEl(this.sidebarMount, this.sidebarFormatMount, this.treeMount);

    this.mainMount = el("section", { className: "mac-pane", style: { overflow: "hidden" } });
    this.tablistMount = el("div", {});
    this.subbarMount = el("div", {});
    this.activityBar = new TaskActivityBar();
    this.activityBar.el.style.margin = "8px 14px 0";
    this.activityBar.el.style.flexShrink = "0";
    this.gridMount = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    this._destroyed = false;
    clearEl(this.mainMount, this.tablistMount, this.subbarMount, this.activityBar.el, this.gridMount);

    this.tree = new DatabaseTree(this.treeMount, {
      selectionMode: "single",
      renderConnectionBadge: (conn) => {
        const fmt = conn.standard_format || "AUTO";
        const shortTxt = SHORT_FORMAT_LABELS[fmt] || fmt;
        return badge(shortTxt, fmt !== "AUTO" ? "info" : "neutral");
      },
      onActiveConnectionChange: async (cid) => {
        if (cid && cid !== this.activeConnId) {
          this.activeConnId = cid;
          this.activeSchema = "";
          this.activeTable = "";
          this.tableCheck = null;
          this.selectedBindCols.clear();
          this._syncFormatFromConnection(cid);
          await this.loadBindingsOverview();
        }
      },
      onPreviewTable: async ({ connectionId, schemaName, tableName }) => {
        const switchedConn = connectionId !== this.activeConnId;
        this.activeConnId = connectionId;
        this.activeSchema = schemaName;
        this.activeTable = tableName;
        this.selectedBindCols.clear();
        if (this.subMode !== "bindings") {
          this.subMode = "bindings";
        }
        if (switchedConn) {
          this._syncFormatFromConnection(connectionId);
          await this.loadBindingsOverview({ skipRender: true });
        }
        await this.loadTableCompliance(false);
      },
      renderTableBadge: (cid, schemaName, tbl) => {
        if (cid !== this.activeConnId) return null;
        const key = `${String(schemaName || "public").toLowerCase()}.${String(tbl.table_name || "").toLowerCase()}`;
        const st = this.tableStats[key];
        if (!st || !st.bound_count) return null;
        const tone =
          st.non_compliant_count > 0
            ? "warn"
            : st.compliant_count >= st.bound_count
            ? "success"
            : "info";
        const bEl = badge(`${st.compliant_count}/${st.bound_count}`, tone);
        const totalHint = st.total_cols ? `（物理表共 ${st.total_cols} 列）` : "";
        bEl.title = `已落标 ${st.compliant_count} / 已绑标 ${st.bound_count}${totalHint}`;
        return bEl;
      },
    });

    this.bindingSearchInput = el("input", {
      className: "mac-input mac-input-sm",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "筛选字段 / 编号 / 标准名...",
      style: { width: "200px" },
      onInput: (e) => {
        this.bindingKeyword = e.target.value;
        clearTimeout(this._bindingSearchTimer);
        this._bindingSearchTimer = setTimeout(() => {
          if (!this._destroyed) this._renderBindingsView();
        }, 120);
      },
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
        clearTimeout(this._queueSearchTimer);
        this._queueSearchTimer = setTimeout(() => {
          if (!this._destroyed) this._renderQueueView();
        }, 120);
      },
    });

    this._unsubStore = store.subscribe((state, reason) => {
      if (reason === "connections" || reason === "bootstrap") {
        if (this.ws && Array.isArray(state.connections)) {
          this.ws.connections = state.connections;
        }
        if (this.loadedOnce && this.activeConnId) {
          this._syncFormatFromConnection(this.activeConnId);
          this._renderSidebarFormatCard();
        }
      }
    });
  }

  destroy() {
    this._destroyed = true;
    clearTimeout(this._bindingSearchTimer);
    clearTimeout(this._queueSearchTimer);
    this._unsubStore?.();
    this._unsubStore = null;
    this.activityBar?.destroy();
    this.tree?.destroy();
  }

  async _runTracked(title, fn, { cancellable = true, exclusive = true } = {}) {
    return this.activityBar.track(
      title,
      async ({ taskId, signal }) => {
        const res = await fn({ taskId, signal });
        if (signal?.aborted) throw makeAbortError();
        return res;
      },
      { prefix: "mig", cancellable, exclusive }
    );
  }

  _isCommentOnlyItem(it) {
    const oldN = String(it?.db_current_col_name || it?.current_db_col_name || it?.old_col_name || "")
      .trim()
      .toLowerCase();
    const newN = String(it?.new_col_name || oldN)
      .trim()
      .toLowerCase();
    const oldT = String(it?.db_current_type || it?.current_db_col_type || it?.old_col_type || "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, " ");
    const newT = String(it?.new_col_type || oldT)
      .trim()
      .toLowerCase()
      .replace(/\s+/g, " ");
    const oldC = String(it?.db_current_comment ?? it?.old_col_comment ?? "").trim();
    const newC = String(it?.new_col_comment || "").trim();
    return Boolean(oldN && newN && oldN === newN && oldT && newT && oldT === newT && newC && newC !== oldC);
  }

  _isRebuildItem(it) {
    const strategy = String(it?.strategy || "").toUpperCase();
    if (strategy === "TABLE_REBUILD") return true;
    if (strategy === "DIRECT_ALTER") return false;
    if (this._isCommentOnlyItem(it)) return false;
    return (
      it?.migration_mode === "TABLE_REBUILD" ||
      Boolean(it?.is_distribute_key) ||
      String(it?.table_orientation || "").toUpperCase() === "COLUMN"
    );
  }

  _isColumnInQueue(connId, schemaName, tableName, colName) {
    const items = this.ws?.items || [];
    if (!items.length) return false;
    const sLower = String(schemaName || "").trim().toLowerCase();
    const tLower = String(tableName || "").trim().toLowerCase();
    const cLower = String(colName || "").trim().toLowerCase();
    const cid = String(connId || "").trim();
    return items.some((it) => {
      const itCid = String(it.connection_id || "").trim();
      if (itCid && cid && itCid !== cid) return false;
      if (String(it.schema_name || "").trim().toLowerCase() !== sLower) return false;
      if (String(it.table_name || "").trim().toLowerCase() !== tLower) return false;
      const oldN = String(it.old_col_name || "").trim().toLowerCase();
      const newN = String(it.new_col_name || "").trim().toLowerCase();
      const dbN = String(it.db_current_col_name || "").trim().toLowerCase();
      return oldN === cLower || newN === cLower || dbN === cLower;
    });
  }

  async ensureLoaded(force = false) {
    if (this.loadedOnce && !force) return;
    await this.reloadState();
  }

  async refreshOnActivate() {
    if (!this.loadedOnce) {
      await this.ensureLoaded(false);
      return;
    }
    if (this.subMode === "queue") {
      await this.reloadState();
      return;
    }
    await this.loadBindingsOverview({ skipRender: Boolean(this.activeTable) });
    if (this.activeTable) {
      await this.loadTableCompliance(false);
    }
  }

  _findConnection(connId = this.activeConnId) {
    const conns = store.state.connections?.length ? store.state.connections : this.ws?.connections || [];
    return conns.find((c) => c.connection_id === connId) || null;
  }

  _syncFormatFromConnection(connId = this.activeConnId) {
    const conn = this._findConnection(connId);
    this.formatProfile = conn?.standard_format || "AUTO";
    return this.formatProfile;
  }

  async _updateActiveConnectionFormat(nextFormat) {
    if (!this.activeConnId) {
      Toast.warn("请先选择一个数据源连接");
      return;
    }
    const prev = this.formatProfile;
    this.formatProfile = nextFormat || "AUTO";
    try {
      const res = await platformApi.updateConnectionStandardFormat(this.activeConnId, this.formatProfile);
      if (Array.isArray(res.connections)) {
        store.updateConnections(res.connections);
        if (this.ws) this.ws.connections = res.connections;
      }
      Toast.success(res.message || "当前库落标标准已保存");
      await this.loadBindingsOverview({ skipRender: Boolean(this.activeTable) });
      if (this.activeTable) {
        await this.loadTableCompliance(false);
      }
    } catch (err) {
      this.formatProfile = prev;
      this._renderSidebarFormatCard();
      Toast.error(err.message);
    }
  }

  _renderSidebarFormatCard() {
    const conn = this._findConnection(this.activeConnId);
    const fmtSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "当前库落标标准",
        disabled: !conn,
        style: { width: "100%" },
        onChange: (e) => this._updateActiveConnectionFormat(e.target.value),
      },
      this.formatProfiles.map((p) => {
        const o = el("option", { value: p.value, text: p.label });
        if (p.value === this.formatProfile) o.selected = true;
        return o;
      })
    );

    clearEl(
      this.sidebarFormatMount,
      el(
        "div",
        { className: "sidebar-format-card" },
        el(
          "div",
          { className: "sidebar-format-head" },
          el("span", { className: "sidebar-format-title", text: "当前库落标标准" }),
          conn ? badge(conn.db_type || "DB", "neutral") : null
        ),
        el("div", {
          className: "sidebar-format-dbname",
          text: conn ? conn.name : "未选择数据源",
          title: conn ? `${conn.name} (${conn.db_type})` : "",
        }),
        fmtSelect
      )
    );
  }

  async reloadState() {
    try {
      const [wsRes, ovRes] = await Promise.all([
        migratorApi.getState(),
        migratorApi.getBindingsOverview(this.activeConnId, "AUTO"),
      ]);
      this.loadedOnce = true;
      this.ws = wsRes;
      if (wsRes.logs) store.updateLogs(wsRes.logs);
      if (Array.isArray(ovRes.connections)) store.updateConnections(ovRes.connections);

      this.activeConnId =
        (this.subMode === "queue" ? wsRes.active_connection_id : ovRes.active_connection_id) ||
        ovRes.active_connection_id ||
        wsRes.active_connection_id ||
        store.state.connections[0]?.connection_id ||
        "";
      this.formatProfile = ovRes.active_connection?.standard_format || ovRes.format_profile || this._syncFormatFromConnection(this.activeConnId);
      this.tableStats = ovRes.table_stats || {};
      if (ovRes.format_profiles?.length) this.formatProfiles = ovRes.format_profiles;
      if (this.activeConnId) {
        await this.tree.setActiveConnection(this.activeConnId, { expand: true });
      }
      if (this.activeConnId && this.activeTable) {
        await this.loadTableCompliance(false);
      } else {
        this.render();
        this.onStateUpdate?.();
      }
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async loadBindingsOverview({ skipRender = false } = {}) {
    try {
      const ovRes = await migratorApi.getBindingsOverview(this.activeConnId, "AUTO");
      if (Array.isArray(ovRes.connections)) store.updateConnections(ovRes.connections);
      this.activeConnId = ovRes.active_connection_id || this.activeConnId;
      this.formatProfile = ovRes.active_connection?.standard_format || ovRes.format_profile || this._syncFormatFromConnection(this.activeConnId);
      this.tableStats = ovRes.table_stats || {};
      if (ovRes.format_profiles?.length) this.formatProfiles = ovRes.format_profiles;
      this.tree.renderTreeBody();
      if (!skipRender) {
        this.render();
        this.onStateUpdate?.();
      } else {
        this._renderSidebarFormatCard();
      }
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async loadTableCompliance(forceRefresh = false) {
    if (!this.activeConnId || !this.activeTable) {
      this.render();
      return;
    }
    const targetLabel = this.activeSchema
      ? `${this.activeSchema}.${this.activeTable}`
      : this.activeTable;
    try {
      const res = await this._runTracked(
        `核验 ${targetLabel} 落标`,
        ({ taskId, signal }) =>
          migratorApi.getTableBindings(
            this.activeConnId,
            this.activeSchema,
            this.activeTable,
            this.formatProfile,
            forceRefresh,
            { taskId, signal }
          ),
        { exclusive: true }
      );
      if (this._destroyed) return;
      this.tableCheck = res;
      if (res.table_stats) this.tableStats = res.table_stats;
      if (res.table) {
        this.activeSchema = res.table.schema_name || this.activeSchema;
        this.activeTable = res.table.table_name || this.activeTable;
      }
      this.tree.renderTreeBody();
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  _applyStateResponse(res) {
    this.ws = res;
    if (res.logs) store.updateLogs(res.logs);
    if (res.table_stats_by_conn && this.activeConnId && res.table_stats_by_conn[this.activeConnId]) {
      this.tableStats = res.table_stats_by_conn[this.activeConnId];
      this.tree?.render();
    } else if (
      res.table_stats &&
      (!res.table_stats_connection_id || res.table_stats_connection_id === this.activeConnId)
    ) {
      this.tableStats = res.table_stats;
      this.tree?.render();
    }
    if (Array.isArray(res.migrated_tables) && res.migrated_tables.length > 0) {
      this._bindingsDirtyAfterMigration = true;
    }
    if (res.warning) Toast.warn(res.warning);
    else if (res.message) Toast.success(res.message);
    this.render();
    this.onStateUpdate?.();
  }

  _activeDbType() {
    const conns = this.ws?.connections?.length ? this.ws.connections : store.state.connections || [];
    return conns.find((c) => c.connection_id === this.activeConnId)?.db_type || "DWS";
  }

  async runCheckCompliance() {
    if (!this.activeConnId) {
      Toast.warn("请先选择要检查的数据源");
      return;
    }
    const targets = this.activeTable
      ? [{ schema_name: this.activeSchema || "public", table_name: this.activeTable }]
      : [];
    const title = this.activeTable
      ? `检查 ${this.activeSchema || "public"}.${this.activeTable} 落标`
      : "批量检查已绑表落标";
    try {
      const res = await this._runTracked(title, ({ taskId, signal }) =>
        migratorApi.checkCompliance(
          {
            connection_id: this.activeConnId,
            tables: targets,
            format_profile: this.formatProfile,
            force_refresh: true,
          },
          { taskId, signal }
        )
      );
      if (this._destroyed) return;
      if (res.table_stats) this.tableStats = res.table_stats;
      if (res.active_table) {
        this.tableCheck = res.active_table;
      } else if (this.activeTable && res.tables?.length) {
        this.tableCheck = res.tables[0];
      }
      if (res.warning) Toast.warn(res.warning);
      else Toast.success(res.message || "落标检查完成");
      this.tree.renderTreeBody();
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  async runAutoMatch() {
    if (!this.activeConnId) {
      Toast.warn("请先选择数据源");
      return;
    }
    const title = this.activeTable
      ? `智能预匹配 ${this.activeTable}`
      : "智能预匹配数据标准";
    try {
      const res = await this._runTracked(title, ({ taskId, signal }) =>
        migratorApi.autoMatchBindings(
          {
            connection_id: this.activeConnId,
            schema_name: this.activeSchema,
            table_name: this.activeTable,
            format_profile: this.formatProfile,
          },
          { taskId, signal }
        )
      );
      if (this._destroyed) return;
      if (res.table_stats) this.tableStats = res.table_stats;
      if (res.columns && res.table) {
        this.tableCheck = { ...res, ok: true };
      } else if (this.activeTable) {
        await this.loadTableCompliance(false);
      }
      if (res.warning) Toast.warn(res.warning);
      else Toast.success(res.message || "智能预匹配完成");
      this.tree.renderTreeBody();
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  async runSyncItems() {
    try {
      const res = await this._runTracked("实时核验目标库状态", ({ taskId, signal }) =>
        migratorApi.sync({ taskId, signal })
      );
      if (this._destroyed) return;
      this._applyStateResponse(res);
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  async _runExportReportTask(title, fn, { syncCatalog = true, connId = "" } = {}) {
    try {
      const filename = await this._runTracked(title, ({ taskId, signal }) => fn({ taskId, signal }), {
        exclusive: false,
      });
      if (this._destroyed) return;
      Toast.success(`已导出：${filename}`);
      const targetCid = connId || this.activeConnId;
      if (syncCatalog && targetCid) {
        try {
          const cat = await platformApi.getMetadataTree(targetCid, false);
          if (!this._destroyed && cat) {
            store.updateCatalogTree(targetCid, cat);
            await this.loadBindingsOverview({ skipRender: false });
          }
        } catch (_ignore) {
          // 忽略后置目录刷新非致命错误
        }
      }
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  async executeSingleItem(itemRow) {
    if (!itemRow?.id) return;
    const label = `${itemRow.schema_name}.${itemRow.table_name}.${itemRow.old_col_name || itemRow.new_col_name || itemRow.id}`;
    try {
      const res = await this._runTracked(`执行单字段贯标 · ${label}`, ({ taskId, signal }) =>
        migratorApi.executeSingle(itemRow.id, { taskId, signal })
      );
      if (this._destroyed) return;
      this._applyStateResponse(res);
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  async stageSelectedToQueue(explicitCols = null, { stayInBindings = true } = {}) {
    if (this._stagingInFlight) return;
    if (!this.activeConnId || !this.activeTable) {
      Toast.warn("请先在左侧选择目标库表");
      return;
    }
    const cols = explicitCols || [...this.selectedBindCols];
    this._stagingInFlight = true;
    if (this._bindingStageBtn) this._bindingStageBtn.disabled = true;
    try {
      const res = await migratorApi.stageMigration({
        connection_id: this.activeConnId,
        schema_name: this.activeSchema,
        table_name: this.activeTable,
        column_names: cols,
        format_profile: this.formatProfile,
        append: true,
      });
      if (!explicitCols) {
        this.selectedBindCols.clear();
      } else {
        for (const c of explicitCols) {
          this.selectedBindCols.delete(c);
        }
      }
      if (!stayInBindings) {
        this.subMode = "queue";
      }
      this._applyStateResponse(res);
    } catch (err) {
      Toast.error(err.message);
    } finally {
      this._stagingInFlight = false;
      if (this._bindingStageBtn) this._bindingStageBtn.disabled = false;
    }
  }

  renderHeaderActions() {
    const ws = this.ws || { connections: [] };
    const conns = ws.connections?.length ? ws.connections : store.state.connections || [];

    const connSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "选择数据源",
        onChange: async (e) => {
          const cid = e.target.value;
          if (!cid) return;
          this.activeConnId = cid;
          this.activeSchema = "";
          this.activeTable = "";
          this.tableCheck = null;
          this.selectedBindCols.clear();
          this._syncFormatFromConnection(cid);
          try {
            if (this.subMode === "queue") {
              const res = await this._runTracked("绑定并同步数据源", ({ taskId, signal }) =>
                migratorApi.bindConnection(cid, true, { taskId, signal })
              );
              if (!this._destroyed) this._applyStateResponse(res);
            } else {
              await this.tree.setActiveConnection(cid, { expand: true });
              await this.loadBindingsOverview();
            }
          } catch (err) {
            if (isAbortError(err)) return;
            Toast.error(err.message);
          }
        },
      },
      [
        el("option", { value: "", text: "选择数据源" }),
        ...conns.map((c) => {
          const o = el("option", { value: c.connection_id, text: `${c.name} · ${c.db_type}` });
          if (c.connection_id === (this.subMode === "queue" ? ws.active_connection_id : this.activeConnId)) {
            o.selected = true;
          }
          return o;
        }),
      ]
    );

    if (this.subMode === "queue") {
      this._queueExecBatchBtn = el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm mac-btn-primary",
        text: `执行 (${this.selectedIds.size})`,
        onClick: () => this.executeSelectedBatch(),
      });
      return el(
        "div",
        { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
        connSelect,
        el("button", {
          type: "button",
          className: "mac-btn mac-btn-sm",
          text: "导入改造清单",
          onClick: () => this.openChecklistUploadWizard(),
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
          text: "实时核验",
          onClick: () => this.runSyncItems(),
        }),
        this._queueExecBatchBtn
      );
    }

    this._queueExecBatchBtn = null;

    return el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      connSelect,
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm",
          onClick: () => this.openBindingUploadWizard(),
        },
        svgIcon("upload", 13),
        el("span", { text: "上传对照关系" })
      ),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "智能预匹配",
        onClick: () => this.runAutoMatch(),
      }),
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm mac-btn-primary",
          onClick: () => this.runCheckCompliance(),
        },
        svgIcon("check", 13),
        el("span", { text: "检查是否落标" })
      )
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
      const res = await this._runTracked(`批量并发贯标 (${ids.length} 项)`, ({ taskId, signal }) =>
        migratorApi.executeBatch(
          {
            item_ids: ids,
            max_workers: this.maxWorkers,
            keep_backup: this.keepBackup,
          },
          { taskId, signal }
        )
      );
      if (this._destroyed) return;
      this.selectedIds.clear();
      this._applyStateResponse(res);
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  async removeQueueItems(itemIds, { skipConfirm = false } = {}) {
    const ids = [...new Set((itemIds || []).map((x) => Number(x)).filter((n) => n > 0))];
    if (!ids.length) {
      Toast.warn("请先勾选要移出队列的任务");
      return;
    }
    if (!skipConfirm) {
      const ok = await Modal.confirm({
        title: "移出贯标队列",
        message: `确定从全局贯标执行队列中移出选中的 ${ids.length} 条任务吗？（不会影响已绑定的字段数据标准）`,
        confirmText: "移出",
        danger: true,
      });
      if (!ok) return;
    }
    try {
      const res = await migratorApi.deleteItems(ids);
      for (const id of ids) {
        this.selectedIds.delete(String(id));
      }
      this._applyStateResponse(res);
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _filteredQueueItems() {
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

  _filteredBindingCols() {
    const cols = this.tableCheck?.columns || [];
    const kw = this.bindingKeyword.trim().toLowerCase();
    return cols.filter((c) => {
      if (this.bindingFilter !== "ALL" && c.compliance_status !== this.bindingFilter) {
        return false;
      }
      if (!kw) return true;
      return [
        c.column_name,
        c.column_comment,
        c.std_code,
        c.std_cn_name,
        c.std_en_abbr,
        c.std_domain_name,
      ].some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  async _saveColumnBinding(colRow, pickedItem) {
    try {
      const res = await migratorApi.saveBindings({
        connection_id: this.activeConnId,
        format_profile: this.formatProfile,
        active_schema: this.activeSchema,
        active_table: this.activeTable,
        bindings: [
          {
            schema_name: this.activeSchema,
            table_name: this.activeTable,
            column_name: colRow.column_name,
            std_code: pickedItem ? pickedItem.std_code : "",
          },
        ],
      });
      this.tableCheck = res;
      if (res.table_stats) this.tableStats = res.table_stats;
      Toast.success(pickedItem ? `已绑定 [${pickedItem.std_code} · ${pickedItem.cn_name}]` : "已解除绑定");
      this.tree.renderTreeBody();
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _openPickerForColumn(colRow) {
    openStandardPickerModal({
      title: `为字段 ${this.activeSchema}.${this.activeTable}.${colRow.column_name} 绑定数据标准`,
      initialQuery: colRow.std_code || colRow.column_comment || colRow.column_name,
      currentStdCode: colRow.std_code || "",
      dbType: this._activeDbType(),
      formatProfile: this.formatProfile,
      onSelect: (pickedItem) => this._saveColumnBinding(colRow, pickedItem),
    });
  }

  render() {
    this._renderSidebarFormatCard();
    const queueCount = (this.ws?.items || []).length;
    const tablist = el(
      "div",
      { className: "content-tablist", role: "tablist", "aria-label": "落标绑定与贯标子模式" },
      [
        { id: "bindings", label: "字段落标绑定与核验" },
        { id: "queue", label: `贯标改造执行队列 (${queueCount})` },
      ].map((t) =>
        el("button", {
          type: "button",
          role: "tab",
          className: "content-tab",
          "aria-selected": String(this.subMode === t.id),
          text: t.label,
          onClick: () => {
            this.subMode = t.id;
            if (
              t.id === "bindings" &&
              this._bindingsDirtyAfterMigration &&
              this.activeConnId &&
              this.activeTable
            ) {
              this._bindingsDirtyAfterMigration = false;
              this.loadTableCompliance(false);
              return;
            }
            this.render();
            this.onStateUpdate?.();
          },
        })
      )
    );
    clearEl(this.tablistMount, tablist);

    if (this.subMode === "bindings") {
      this._renderBindingsView();
    } else {
      this._renderQueueView();
    }

    const mainChildren = this.mainMount.children;
    if (
      mainChildren.length !== 4 ||
      mainChildren[0] !== this.tablistMount ||
      mainChildren[1] !== this.subbarMount ||
      mainChildren[2] !== this.activityBar.el ||
      mainChildren[3] !== this.gridMount
    ) {
      clearEl(this.mainMount, this.tablistMount, this.subbarMount, this.activityBar.el, this.gridMount);
    }
    if (
      this.root.children.length !== 2 ||
      this.root.children[0] !== this.sidebarMount ||
      this.root.children[1] !== this.mainMount
    ) {
      clearEl(this.root, this.sidebarMount, this.mainMount);
    }
  }

  _renderBindingsView() {
    const tc = this.tableCheck;
    const sum = tc?.summary || {};
    const tMeta = tc?.table || {};

    this.bindingSearchInput.value = this.bindingKeyword;

    const pills = [
      { key: "ALL", label: `全部 ${sum.total_cols ?? 0}` },
      { key: "COMPLIANT", label: `已落标 ${sum.compliant_cols ?? 0}` },
      { key: "COMMENT_ONLY", label: `仅差注释 ${sum.comment_only_cols ?? 0}` },
      { key: "NON_COMPLIANT", label: `未落标 ${sum.non_compliant_cols ?? 0}` },
      { key: "UNBOUND", label: `未绑标 ${sum.unbound_cols ?? 0}` },
    ];

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el(
        "div",
        { className: "mac-subbar-group" },
        tc
          ? el(
              "span",
              { className: "mono", style: { fontWeight: "600", marginRight: "4px" }, text: `${tMeta.schema_name}.${tMeta.table_name}` }
            )
          : null,
        tc ? badge(`绑标率 ${sum.coverage_pct ?? 0}%`, "info") : null,
        tc
          ? el(
              "div",
              { className: "filter-pills" },
              pills.map((p) =>
                el("button", {
                  type: "button",
                  className: `filter-pill ${this.bindingFilter === p.key ? "is-active" : ""}`,
                  text: p.label,
                  onClick: () => {
                    this.bindingFilter = p.key;
                    this._renderBindingsView();
                  },
                })
              )
            )
          : el("span", { style: { fontSize: "12px", color: "var(--os-muted)" }, text: "请从左侧目录树选择数据表，或点击右上角「上传对照关系」批量铺底" })
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        tc ? el("search", {}, this.bindingSearchInput) : null,
        tc
          ? (this._bindingStageBtn = el("button", {
              type: "button",
              className: "btn btn-xs mac-btn-primary",
              text:
                this.selectedBindCols.size > 0
                  ? `纳入贯标任务 (${this.selectedBindCols.size})`
                  : "纳入本表待改字段",
              onClick: () => this.stageSelectedToQueue(),
            }))
          : (this._bindingStageBtn = null),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "导出报告",
          onClick: () => this.openExportDialog(),
        })
      )
    );
    clearEl(this.subbarMount, subbar);

    if (!tc) {
      clearEl(
        this.gridMount,
        el(
          "div",
          { className: "explorer-empty-state" },
          el(
            "div",
            { className: "explorer-empty-card", style: { maxWidth: "500px", padding: "24px" } },
            el("div", { style: { marginBottom: "8px", color: "var(--os-accent)" } }, svgIcon("table", 30)),
            el("h3", { style: { margin: "0 0 6px 0", fontSize: "14px" }, text: "读取库表字段并绑定数据标准" }),
            el("p", {
              style: { margin: "0 0 14px 0", fontSize: "12px", color: "var(--os-text-secondary)", lineHeight: "1.55" },
              text: "在左侧目录树点击任意表即可查看其字段并逐列绑定标准；也可先下载系统预填对照模板，批量维护「数据标准-表字段」关系后一键核验落标。",
            }),
            el(
              "div",
              { style: { display: "flex", justifyContent: "center", gap: "8px", flexWrap: "wrap" } },
              el("button", {
                type: "button",
                className: "mac-btn mac-btn-sm mac-btn-primary",
                text: "上传对照关系（含系统模板）",
                onClick: () => this.openBindingUploadWizard(),
              }),
              el("button", {
                type: "button",
                className: "mac-btn mac-btn-sm",
                text: "批量检查已绑表落标",
                onClick: () => this.runCheckCompliance(),
              })
            )
          )
        )
      );
      return;
    }

    const rows = this._filteredBindingCols();
    const grid = new DataGrid({
      ariaLabel: "表字段落标核验清单",
      selectable: true,
      selectedKeys: this.selectedBindCols,
      onSelectionChange: (sel) => {
        if (this._bindingStageBtn) {
          this._bindingStageBtn.textContent =
            sel.size > 0 ? `纳入贯标任务 (${sel.size})` : "纳入本表待改字段";
        }
      },
      rowKey: "column_name",
      rows,
      emptyText: "当前筛选条件下无字段",
      columns: [
        { key: "ordinal_position", label: "#", render: (r) => String(r.ordinal_position || "-") },
        {
          key: "column_name",
          label: "库内字段 / 物理类型",
          render: (r) =>
            el(
              "div",
              {},
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "5px" } },
                el("span", { className: "mono", style: { fontWeight: "600" }, text: r.column_name }),
                r.is_primary_key ? badge("PK", "info") : null,
                r.is_distribute_key ? badge("DK", "warn") : null,
                r.is_partition_key ? badge("PT", "warn") : null
              ),
              el("div", {
                className: "mono",
                style: { fontSize: "11px", color: "var(--os-muted)" },
                text: `${r.data_type || "?"}${r.column_comment ? " · " + r.column_comment : ""}`,
              })
            ),
        },
        {
          key: "std_code",
          label: "绑定数据标准（点击选绑）",
          render: (r) => {
            if (!r.std_code) {
              return el(
                "button",
                {
                  type: "button",
                  className: "btn btn-xs btn-ghost",
                  style: { color: "var(--os-muted)", border: "1px dashed var(--os-border-strong)" },
                  text: "+ 绑定数据标准",
                  onClick: (e) => {
                    e.stopPropagation();
                    this._openPickerForColumn(r);
                  },
                }
              );
            }
            return el(
              "div",
              {
                className: "std-cell-clickable",
                title: r.std_biz_definition || "点击更换或解除绑定",
                onClick: (e) => {
                  e.stopPropagation();
                  this._openPickerForColumn(r);
                },
              },
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "5px", flexWrap: "wrap" } },
                badge(r.std_code, "info"),
                el("strong", { text: r.std_cn_name || "未知标准" }),
                r.std_en_abbr
                  ? el("span", { className: "mono", style: { color: "var(--os-accent)" }, text: r.std_en_abbr })
                  : null
              ),
              r.std_target_type
                ? el("div", {
                    className: "mono",
                    style: { fontSize: "11px", color: "var(--os-muted)" },
                    text: `标准类型: ${r.std_target_type}${r.std_domain_name ? " · " + r.std_domain_name : ""}`,
                  })
                : null
            );
          },
        },
        {
          key: "compliance_status",
          label: "落标核验结果",
          render: (r) => {
            const meta = COMP_STATUS_META[r.compliance_status] || { label: r.compliance_status, tone: "muted" };
            return el(
              "div",
              {},
              statusDot(meta.label, meta.tone),
              r.compliance_status !== "COMPLIANT" && r.compliance_detail
                ? el("div", {
                    style: { fontSize: "11px", color: "var(--os-muted)", marginTop: "2px" },
                    text: r.compliance_detail,
                  })
                : null
            );
          },
        },
        {
          key: "actions",
          label: "操作",
          sortable: false,
          render: (r) => {
            const canStage = Boolean(
              r.std_code && r.compliance_status !== "COMPLIANT" && r.compliance_status !== "STD_MISSING"
            );
            const inQueue = canStage
              ? this._isColumnInQueue(
                  this.activeConnId,
                  tMeta.schema_name || this.activeSchema,
                  tMeta.table_name || this.activeTable,
                  r.column_name
                )
              : false;
            return el(
              "div",
              { style: { display: "flex", gap: "4px" } },
              el("button", {
                type: "button",
                className: "btn btn-xs",
                text: r.std_code ? "换绑" : "选标",
                onClick: (e) => {
                  e.stopPropagation();
                  this._openPickerForColumn(r);
                },
              }),
              r.std_code
                ? el("button", {
                    type: "button",
                    className: "btn btn-xs btn-ghost",
                    text: "解绑",
                    onClick: (e) => {
                      e.stopPropagation();
                      this._saveColumnBinding(r, null);
                    },
                  })
                : null,
              canStage
                ? el("button", {
                    type: "button",
                    className: inQueue ? "btn btn-xs btn-ghost" : "btn btn-xs",
                    text: inQueue ? "已入队" : "纳入贯标",
                    title: inQueue ? "已在全局贯标队列中，点击可按当前标准重新同步" : "加入全局贯标改造执行队列",
                    onClick: (e) => {
                      e.stopPropagation();
                      this.stageSelectedToQueue([r.column_name]);
                    },
                  })
                : null
            );
          },
        },
      ],
    });
    clearEl(this.gridMount, grid.root);
  }

  _renderQueueView() {
    this.searchInput.value = this.searchKeyword;
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
              this._renderQueueView();
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
          text: "导出报告",
          onClick: () => this.openExportDialog(),
        }),
        (this._queueRemoveBtn = el("button", {
          type: "button",
          className: "btn btn-xs",
          text: `移除所选 (${this.selectedIds.size})`,
          disabled: this.selectedIds.size === 0,
          onClick: () =>
            this.removeQueueItems(
              [...this.selectedIds].map((x) => Number(x)).filter((n) => n > 0)
            ),
        })),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-danger",
          text: "清空",
          onClick: async () => {
            const ok = await Modal.confirm({
              title: "清空贯标队列",
              message: "确定清空当前全部全局贯标改造队列吗？（不会删除已绑定的字段数据标准）",
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

    const rows = this._filteredQueueItems();
    const grid = new DataGrid({
      ariaLabel: "贯标字段队列",
      selectable: true,
      selectedKeys: this.selectedIds,
      onSelectionChange: (sel) => {
        if (this._queueExecBatchBtn) {
          this._queueExecBatchBtn.textContent = `执行 (${sel.size})`;
        }
        if (this._queueRemoveBtn) {
          this._queueRemoveBtn.textContent = `移除所选 (${sel.size})`;
          this._queueRemoveBtn.disabled = sel.size === 0;
        }
      },
      rowKey: "id",
      rows,
      emptyText: "当前全局贯标队列为空，可在「字段落标绑定与核验」勾选字段后点击「纳入贯标」，或点击右上角「导入改造清单」",
      columns: [
        {
          key: "table_name",
          label: "库表",
          render: (r) => {
            const srcConn = r.connection_id ? this._findConnection(r.connection_id) : null;
            return el(
              "div",
              { style: { display: "flex", flexDirection: "column", gap: "2px" } },
              el("span", { className: "mono", text: `${r.schema_name}.${r.table_name}` }),
              srcConn
                ? el("span", { style: { fontSize: "10.5px", color: "var(--os-muted)" }, text: `${srcConn.name} (${srcConn.db_type})` })
                : null
            );
          },
        },
        { key: "ordinal_position", label: "#", render: (r) => (r.ordinal_position ? String(r.ordinal_position) : "-") },
        {
          key: "old_col_name",
          label: "原字段 → 标准字段",
          render: (r) => {
            const curN = r.db_current_col_name || r.current_db_col_name || r.old_col_name;
            const tgtN = r.new_col_name || curN;
            const curT = r.db_current_type || r.current_db_col_type || r.old_col_type || "?";
            const tgtT = r.new_col_type || curT;
            const nameText =
              String(curN).toLowerCase() === String(tgtN).toLowerCase()
                ? String(curN)
                : `${curN} → ${tgtN}`;
            const typeText =
              String(curT).trim().toLowerCase() === String(tgtT).trim().toLowerCase()
                ? String(curT)
                : `${curT} → ${tgtT}`;
            return el(
              "div",
              {},
              el("div", {
                className: "mono",
                style: { fontWeight: "600" },
                text: nameText,
              }),
              el("div", {
                className: "mono",
                style: { fontSize: "11px", color: "var(--os-muted)" },
                text: typeText,
              })
            );
          },
        },
        {
          key: "new_col_comment",
          label: "注释",
          render: (r) => {
            const oldC = String(r.db_current_comment ?? r.old_col_comment ?? "").trim();
            const newC = String(r.new_col_comment || "").trim();
            if (newC && oldC && oldC !== newC) {
              return `${oldC} → ${newC}`;
            }
            return newC || oldC || "-";
          },
        },
        {
          key: "strategy",
          label: "策略",
          render: (r) =>
            this._isRebuildItem(r)
              ? badge("保序重建", "warn")
              : this._isCommentOnlyItem(r)
              ? badge("仅更新注释", "info")
              : badge("原地 ALTER", "neutral"),
        },
        {
          key: "status",
          label: "状态",
          render: (r) => {
            const m = MIG_STATUS_META[r.status] || { label: r.status || "-", tone: "muted" };
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
                    await this.openRebuildPreviewModal(
                      r.schema_name,
                      r.table_name,
                      r.connection_id || ""
                    );
                    return;
                  }
                  await this.executeSingleItem(r);
                },
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-ghost",
                text: "SQL",
                onClick: (e) => {
                  e.stopPropagation();
                  this.openItemSqlPreviewModal(r);
                },
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-ghost",
                text: "移出",
                title: "从贯标队列移除",
                onClick: (e) => {
                  e.stopPropagation();
                  this.removeQueueItems([r.id], { skipConfirm: true });
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

function applyViewMixins(TargetClass, ...MixinClasses) {
  for (const Mixin of MixinClasses) {
    for (const key of Object.getOwnPropertyNames(Mixin.prototype)) {
      if (key === "constructor") continue;
      Object.defineProperty(
        TargetClass.prototype,
        key,
        Object.getOwnPropertyDescriptor(Mixin.prototype, key)
      );
    }
  }
}
applyViewMixins(StandardMigratorView, StandardMigratorModalsMixin);
