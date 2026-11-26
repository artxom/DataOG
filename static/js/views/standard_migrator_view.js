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

const SIDEBAR_WIDTH_STORAGE_KEY = "dataog:standard_migrator:sidebar_width";
const SIDEBAR_WIDTH_DEFAULT = 248;
const SIDEBAR_WIDTH_MIN = 180;
const SIDEBAR_WIDTH_MAX = 560;
const SIDEBAR_IGNORE_PARTIAL_STORAGE_KEY = "dataog:standard_migrator:ignore_partial";
const SIDEBAR_IGNORE_UNBOUND_STORAGE_KEY = "dataog:standard_migrator:ignore_unbound";

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
  PARTIAL_COMPLIANT: { label: "部分落标", tone: "warn" },
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
    this.selectedSchema = "";
    this.activeTable = "";
    this.formatProfile = "AUTO";
    this.formatProfiles = DEFAULT_FORMAT_PROFILES;
    this.tableStats = {};
    this.ignorePartial = this._loadBoolPref(SIDEBAR_IGNORE_PARTIAL_STORAGE_KEY, true);
    this.ignoreUnbound = this._loadBoolPref(SIDEBAR_IGNORE_UNBOUND_STORAGE_KEY, true);
    this._knownTableColCounts = new Map();
    this.tableCheck = null;
    this.bindingFilter = "ALL"; // ALL | COMPLIANT | COMMENT_ONLY | PARTIAL_COMPLIANT | NON_COMPLIANT | UNBOUND
    this.bindingKeyword = "";
    this.selectedBindCols = new Set();
    this._stagingInFlight = false;
    this._bindingsDirtyAfterMigration = false;
    this._bindingSearchTimer = null;
    this._queueSearchTimer = null;
    this._bindingStageBtn = null;
    this._queueExecBatchBtn = null;
    this._queueRemoveBtn = null;
    this._bindingsGrid = null;
    this._bindingsGridTableKey = "";
    this._bindingsSort = { sortKey: "", sortAsc: true };
    this._stageBtnByCol = new Map();
    this._queueTabBtn = null;
    this._queueGrid = null;
    this._queueSort = { sortKey: "", sortAsc: true };
    this._clearingCompletedIds = new Set();
    this._clearingBatches = new Set();
    this.drawerOpen = false;
    this.schemaRefreshTarget = "__AUTO__";
    this._schemaRefreshInFlight = false;
    this._lastSchemaRefreshSummary = null;
    this._drawerToggleBtn = null;
    this._drawerToggleTag = null;

    // 贯标改造执行队列状态
    this.ws = null;
    this.statusFilter = "ALL";
    this.searchKeyword = "";
    this.selectedIds = new Set();
    this.maxWorkers = 5;
    this.keepBackup = true;

    let initWidth = SIDEBAR_WIDTH_DEFAULT;
    try {
      const savedW = globalThis.localStorage?.getItem?.(SIDEBAR_WIDTH_STORAGE_KEY);
      if (savedW !== null && savedW !== undefined && savedW !== "") {
        initWidth = Math.max(
          SIDEBAR_WIDTH_MIN,
          Math.min(SIDEBAR_WIDTH_MAX, Math.round(Number(savedW) || SIDEBAR_WIDTH_DEFAULT))
        );
      }
    } catch (_e) {
      // ignore storage access errors
    }
    this._sidebarWidth = initWidth;
    this._resizeCleanup = null;

    this.root = el("div", {
      className: "mac-split-view",
    });
    this.sidebarMount = el("aside", {
      className: "mac-sidebar mac-sidebar-wide is-resizable",
      style: { width: `${this._sidebarWidth}px` },
    });
    this.sidebarResizer = el("div", {
      className: "mac-split-resizer",
      role: "separator",
      "aria-orientation": "vertical",
      "aria-label": "调整左侧栏宽度",
      "aria-valuemin": String(SIDEBAR_WIDTH_MIN),
      "aria-valuemax": String(SIDEBAR_WIDTH_MAX),
      "aria-valuenow": String(this._sidebarWidth),
      tabindex: "0",
      title: "左右拖拽调整侧栏宽度（双击恢复默认宽度）",
      onPointerdown: (e) => this._startSidebarResize(e),
      onDblclick: (e) => {
        e.preventDefault?.();
        this._applySidebarWidth(SIDEBAR_WIDTH_DEFAULT, { persist: true });
      },
      onKeydown: (e) => this._handleSidebarResizerKeydown(e),
    });
    this.treeMount = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    clearEl(this.sidebarMount, this.treeMount);

    this.mainMount = el("section", { className: "mac-pane", style: { overflow: "hidden" } });
    this.tablistMount = el("div", {});
    this.workspaceBodyMount = el("div", { className: "migrator-workspace-body" });
    this.contentColMount = el("div", { className: "migrator-content-col" });
    this.subbarMount = el("div", {});
    this.activityBar = new TaskActivityBar();
    this.activityBar.el.style.margin = "8px 14px 0";
    this.activityBar.el.style.flexShrink = "0";
    this.gridMount = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
    this.settingsDrawerMount = el("aside", {
      id: "migrator-settings-drawer",
      className: "migrator-drawer",
      role: "region",
      "aria-label": "对标设置",
      "aria-hidden": "true",
      onKeydown: (e) => {
        if (e.key === "Escape" && this.drawerOpen) {
          e.stopPropagation?.();
          this.toggleSettingsDrawer(false);
        }
      },
    });
    this._destroyed = false;
    clearEl(this.contentColMount, this.subbarMount, this.activityBar.el, this.gridMount);
    clearEl(this.workspaceBodyMount, this.contentColMount, this.settingsDrawerMount);
    clearEl(this.mainMount, this.tablistMount, this.workspaceBodyMount);

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
          this.selectedSchema = "";
          this.activeTable = "";
          this.schemaRefreshTarget = "__AUTO__";
          this._lastSchemaRefreshSummary = null;
          this.tableCheck = null;
          this.selectedBindCols.clear();
          this._syncFormatFromConnection(cid);
          await this.loadBindingsOverview();
        }
      },
      onSelectSchema: async ({ connectionId, schemaName }) => {
        const switchedConn = connectionId && connectionId !== this.activeConnId;
        if (switchedConn) {
          this.activeConnId = connectionId;
          this.activeSchema = "";
          this.activeTable = "";
          this.schemaRefreshTarget = "__AUTO__";
          this._lastSchemaRefreshSummary = null;
          this.tableCheck = null;
          this.selectedBindCols.clear();
          this.selectedSchema = schemaName || "";
          this._syncFormatFromConnection(connectionId);
          await this.loadBindingsOverview();
          return;
        }
        this.selectedSchema = schemaName || "";
        this._renderSettingsDrawer();
      },
      onPreviewTable: async ({ connectionId, schemaName, tableName }) => {
        const switchedConn = connectionId !== this.activeConnId;
        this.activeConnId = connectionId;
        this.activeSchema = schemaName;
        this.selectedSchema = schemaName;
        this.activeTable = tableName;
        this.selectedBindCols.clear();
        if (this.subMode !== "bindings") {
          this.subMode = "bindings";
        }
        if (switchedConn) {
          this.schemaRefreshTarget = "__AUTO__";
          this._lastSchemaRefreshSummary = null;
          this._syncFormatFromConnection(connectionId);
          await this.loadBindingsOverview({ skipRender: true });
        }
        await this.loadTableCompliance(false);
      },
      renderTableBadge: (cid, schemaName, tbl) => {
        const metrics = this._computeTableBadgeMetrics(cid, schemaName, tbl);
        if (!metrics) return null;
        const bEl = badge(metrics.text, metrics.tone);
        if (metrics.title) bEl.title = metrics.title;
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
      if (["connections", "bootstrap", "catalog"].includes(reason)) {
        if (reason !== "catalog" && this.ws && Array.isArray(state.connections)) {
          this.ws.connections = state.connections;
        }
        if (this.loadedOnce && this.activeConnId) {
          if (reason !== "catalog") {
            this._syncFormatFromConnection(this.activeConnId);
          }
          this._renderSettingsDrawer();
          this._syncDrawerToggleButton();
        }
      }
    });
  }

  destroy() {
    this._destroyed = true;
    this._stopSidebarResize();
    clearTimeout(this._bindingSearchTimer);
    clearTimeout(this._queueSearchTimer);
    for (const batch of [...this._clearingBatches]) {
      clearTimeout(batch.collapseTimer);
      clearTimeout(batch.removeTimer);
    }
    this._clearingBatches.clear();
    if (this._clearingCompletedIds.size > 0) {
      const pendingDelete = [...this._clearingCompletedIds];
      this._clearingCompletedIds.clear();
      migratorApi.deleteItems(pendingDelete).catch(() => {});
    }
    this._unsubStore?.();
    this._unsubStore = null;
    this.activityBar?.destroy();
    this.tree?.destroy();
  }

  _applySidebarWidth(widthPx, { persist = false, maxWidth = SIDEBAR_WIDTH_MAX } = {}) {
    const clamped = Math.max(
      SIDEBAR_WIDTH_MIN,
      Math.min(maxWidth, Math.round(Number(widthPx) || SIDEBAR_WIDTH_DEFAULT))
    );
    this._sidebarWidth = clamped;
    this.sidebarMount.style.width = `${clamped}px`;
    this.sidebarResizer.setAttribute("aria-valuenow", String(clamped));
    if (persist) {
      try {
        globalThis.localStorage?.setItem?.(SIDEBAR_WIDTH_STORAGE_KEY, String(clamped));
      } catch (_e) {
        // ignore storage restriction
      }
    }
  }

  _stopSidebarResize() {
    if (typeof this._resizeCleanup === "function") {
      this._resizeCleanup();
      this._resizeCleanup = null;
    }
  }

  _startSidebarResize(e) {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault?.();
    this._stopSidebarResize();

    const startX = Number(e.clientX ?? 0);
    const rectW = this.sidebarMount.getBoundingClientRect?.().width;
    const startWidth = Number.isFinite(rectW) && rectW > 0 ? rectW : this._sidebarWidth;
    const rootW = this.root.getBoundingClientRect?.().width || globalThis.window?.innerWidth || 1200;
    const dynamicMax = Math.max(320, Math.min(SIDEBAR_WIDTH_MAX, Math.floor(rootW * 0.55)));
    const pointerId = e.pointerId;

    this.sidebarResizer.classList.add("is-resizing");
    this.root.classList.add("is-col-resizing");
    globalThis.document?.body?.classList?.add?.("is-col-resizing");
    if (pointerId !== undefined && typeof this.sidebarResizer.setPointerCapture === "function") {
      try {
        this.sidebarResizer.setPointerCapture(pointerId);
      } catch (_e) {
        // ignore in synthetic test environments
      }
    }

    const onMove = (ev) => {
      const dx = Number(ev.clientX ?? 0) - startX;
      this._applySidebarWidth(startWidth + dx, { persist: false, maxWidth: dynamicMax });
    };

    const finish = () => {
      this._stopSidebarResize();
      this._applySidebarWidth(this._sidebarWidth, { persist: true, maxWidth: dynamicMax });
    };

    const win = globalThis.window || globalThis;
    win.addEventListener?.("pointermove", onMove);
    win.addEventListener?.("pointerup", finish);
    win.addEventListener?.("pointercancel", finish);

    this._resizeCleanup = () => {
      win.removeEventListener?.("pointermove", onMove);
      win.removeEventListener?.("pointerup", finish);
      win.removeEventListener?.("pointercancel", finish);
      this.sidebarResizer.classList.remove("is-resizing");
      this.root.classList.remove("is-col-resizing");
      globalThis.document?.body?.classList?.remove?.("is-col-resizing");
      if (pointerId !== undefined && typeof this.sidebarResizer.releasePointerCapture === "function") {
        try {
          this.sidebarResizer.releasePointerCapture(pointerId);
        } catch (_e) {
          // ignore
        }
      }
    };
  }

  _handleSidebarResizerKeydown(e) {
    if (e.key === "ArrowLeft") {
      e.preventDefault?.();
      this._applySidebarWidth(this._sidebarWidth - 16, { persist: true });
    } else if (e.key === "ArrowRight") {
      e.preventDefault?.();
      this._applySidebarWidth(this._sidebarWidth + 16, { persist: true });
    } else if (e.key === "Home") {
      e.preventDefault?.();
      this._applySidebarWidth(SIDEBAR_WIDTH_MIN, { persist: true });
    } else if (e.key === "Enter" || e.key === "0") {
      e.preventDefault?.();
      this._applySidebarWidth(SIDEBAR_WIDTH_DEFAULT, { persist: true });
    }
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
      this._renderSettingsDrawer();
      this._syncDrawerToggleButton();
      Toast.error(err.message);
    }
  }

  _loadBoolPref(key, fallback = true) {
    try {
      const raw = globalThis.localStorage?.getItem?.(key);
      if (raw === "0" || raw === "false") return false;
      if (raw === "1" || raw === "true") return true;
    } catch (_e) {
      // ignore storage access errors
    }
    return Boolean(fallback);
  }

  _persistBoolPref(key, val) {
    try {
      globalThis.localStorage?.setItem?.(key, val ? "1" : "0");
    } catch (_e) {
      // ignore storage access errors
    }
  }

  _setIgnorePartial(next) {
    this.ignorePartial = Boolean(next);
    this._persistBoolPref(SIDEBAR_IGNORE_PARTIAL_STORAGE_KEY, this.ignorePartial);
    this.tree?.renderTreeBody?.();
  }

  _setIgnoreUnbound(next) {
    this.ignoreUnbound = Boolean(next);
    this._persistBoolPref(SIDEBAR_IGNORE_UNBOUND_STORAGE_KEY, this.ignoreUnbound);
    this.tree?.renderTreeBody?.();
  }

  _computeTableBadgeMetrics(cid, schemaName, tbl) {
    if (cid !== this.activeConnId) return null;
    const key = `${String(schemaName || "public").toLowerCase()}.${String(tbl?.table_name || "").toLowerCase()}`;
    const st = this.tableStats[key] || null;
    const boundCount = Number(st?.bound_count || 0);
    const compliantCount = Number(st?.compliant_count || 0);
    const partialCount = Number(st?.partial_compliant_count || 0);
    const nonCompliantCount = Number(st?.non_compliant_count || 0);
    const knownCols = Number(this._knownTableColCounts?.get(`${cid}::${key}`) || 0);
    const physicalTotalCols = Math.max(
      Number(st?.total_cols || 0),
      Number(tbl?.col_count || 0),
      knownCols
    );
    const hasPhysicalTotal = physicalTotalCols > 0;

    let denom = 0;
    if (this.ignoreUnbound) {
      if (boundCount <= 0) return null;
      denom = boundCount;
    } else {
      if (boundCount <= 0 && !hasPhysicalTotal) return null;
      denom = hasPhysicalTotal ? Math.max(physicalTotalCols, boundCount) : boundCount;
    }

    const effectiveCompliant = Math.min(
      denom,
      this.ignorePartial ? compliantCount + partialCount : compliantCount
    );
    const unboundCount = hasPhysicalTotal ? Math.max(0, denom - boundCount) : 0;
    const hasUnresolvedBad =
      nonCompliantCount > 0 || (!this.ignorePartial && partialCount > 0);

    let tone = "info";
    if (boundCount === 0) {
      tone = "neutral";
    } else if (hasUnresolvedBad) {
      tone = "warn";
    } else if (effectiveCompliant >= denom && denom > 0) {
      tone = !this.ignoreUnbound && !hasPhysicalTotal ? "info" : "success";
    }

    const numText =
      this.ignorePartial && partialCount > 0
        ? `已落标 ${effectiveCompliant}（含部分落标 ${partialCount}）`
        : `已落标 ${compliantCount}`;
    let title = "";
    if (this.ignoreUnbound) {
      const partialSuffix =
        !this.ignorePartial && partialCount > 0 ? `，含部分落标 ${partialCount}` : "";
      const totalSuffix = hasPhysicalTotal ? `（物理表共 ${physicalTotalCols} 列）` : "";
      title = `${numText} / 已绑标 ${boundCount}${partialSuffix}${totalSuffix}`;
    } else {
      const partialSuffix =
        !this.ignorePartial && partialCount > 0 ? `，部分落标 ${partialCount}` : "";
      title = hasPhysicalTotal
        ? `${numText} / 表总字段 ${denom}（已绑标 ${boundCount}，未绑标 ${unboundCount}${partialSuffix}）`
        : `${numText} / 已绑标 ${boundCount}${partialSuffix}（物理表总列数尚未缓存，暂按已绑标计）`;
    }

    return { text: `${effectiveCompliant}/${denom}`, tone, title };
  }

  _buildMacSwitchRow({ key, label, title = "", checked = true, onChange }) {
    const isChecked = Boolean(checked);
    const input = el("input", {
      type: "checkbox",
      role: "switch",
      className: "mac-switch-input",
      checked: isChecked,
      "aria-checked": String(isChecked),
      "aria-label": label,
      dataset: { statToggle: key },
      onChange: (e) => {
        const next = Boolean(e.target?.checked);
        input.setAttribute?.("aria-checked", String(next));
        onChange?.(next);
      },
    });
    return el(
      "label",
      { className: "sidebar-stat-toggle-row", title },
      el("span", { className: "sidebar-stat-toggle-label", text: label }),
      el(
        "span",
        { className: "mac-switch", "aria-hidden": "true" },
        input,
        el(
          "span",
          { className: "mac-switch-track" },
          el("span", { className: "mac-switch-thumb" })
        )
      )
    );
  }

  _syncDrawerToggleButton() {
    if (!this._drawerToggleBtn) return;
    this._drawerToggleBtn.classList?.toggle?.("is-active", Boolean(this.drawerOpen));
    this._drawerToggleBtn.setAttribute?.("aria-expanded", String(Boolean(this.drawerOpen)));
    if (this._drawerToggleTag) {
      this._drawerToggleTag.textContent =
        SHORT_FORMAT_LABELS[this.formatProfile] || this.formatProfile || "自动";
    }
  }

  toggleSettingsDrawer(forceState = undefined) {
    const next = forceState === undefined ? !this.drawerOpen : Boolean(forceState);
    this.drawerOpen = next;
    this.settingsDrawerMount.classList?.toggle?.("is-open", next);
    this.settingsDrawerMount.setAttribute?.("aria-hidden", String(!next));
    this._syncDrawerToggleButton();
    if (next) {
      this._renderSettingsDrawer();
    }
  }

  _collectAvailableSchemas(connId = this.activeConnId) {
    const tree = store.state.catalogTrees?.[connId] || { schemas: [], tables: [] };
    const conn = this._findConnection(connId);
    const byLow = new Map();
    const ensureSch = (rawName) => {
      const name = String(rawName || "").trim();
      if (!name) return null;
      const low = name.toLowerCase();
      if (!byLow.has(low)) {
        byLow.set(low, { name, tableCount: 0 });
      }
      return byLow.get(low);
    };

    for (const s of tree.schemas || []) {
      const sn = typeof s === "string" ? s : s?.schema_name;
      ensureSch(sn);
    }
    let totalTables = 0;
    const defSch = conn?.effective_default_schema || conn?.default_schema || "public";
    for (const t of tree.tables || []) {
      totalTables += 1;
      const entry = ensureSch(t?.schema_name || defSch);
      if (entry) entry.tableCount += 1;
    }
    ensureSch(this.selectedSchema);
    ensureSch(this.activeSchema);
    if (byLow.size === 0 && conn) {
      ensureSch(defSch);
    }
    return {
      totalTables,
      schemas: Array.from(byLow.values()),
    };
  }

  _resolveSchemaRefreshScope(schemaItems = null) {
    const target = String(this.schemaRefreshTarget || "__AUTO__").trim();
    if (target === "__ALL__") return "__ALL__";
    if (target && target !== "__AUTO__") {
      if (Array.isArray(schemaItems) && schemaItems.length > 0) {
        const matched = schemaItems.find((s) => s.name.toLowerCase() === target.toLowerCase());
        if (matched) return matched.name;
      } else {
        return target;
      }
    }
    const preferred = String(this.selectedSchema || this.activeSchema || "").trim();
    if (preferred) {
      if (Array.isArray(schemaItems) && schemaItems.length > 0) {
        const matched = schemaItems.find((s) => s.name.toLowerCase() === preferred.toLowerCase());
        if (matched) return matched.name;
      }
      return preferred;
    }
    return "__ALL__";
  }

  _buildDrawerSection({ key = "", title, badgeEl = null, desc = "", children = [] }) {
    return el(
      "section",
      {
        className: "migrator-drawer-section",
        dataset: key ? { drawerSection: key } : undefined,
      },
      el(
        "div",
        { className: "migrator-drawer-section-head" },
        el("span", { className: "migrator-drawer-section-title", text: title }),
        badgeEl
      ),
      desc ? el("p", { className: "migrator-drawer-desc", text: desc }) : null,
      ...children
    );
  }

  _renderSettingsDrawer() {
    const conn = this._findConnection(this.activeConnId);
    const fmtSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "当前库落标标准",
        dataset: { formatSelect: "true" },
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

    const { totalTables, schemas: schemaItems } = this._collectAvailableSchemas(this.activeConnId);
    const resolvedScope = this._resolveSchemaRefreshScope(schemaItems);
    const isAllScope = resolvedScope === "__ALL__";

    const schemaSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "刷新比对范围",
        dataset: { schemaRefreshSelect: "true" },
        disabled: !conn || this._schemaRefreshInFlight,
        style: { width: "100%" },
        onChange: (e) => {
          this.schemaRefreshTarget = e.target.value || "__ALL__";
          this._renderSettingsDrawer();
        },
      },
      [
        el("option", {
          value: "__ALL__",
          selected: isAllScope,
          text: totalTables > 0 ? `全部 Schema（当前库全量 · ${totalTables} 表）` : "全部 Schema（当前库全量）",
        }),
        ...schemaItems.map((s) =>
          el("option", {
            value: s.name,
            selected: !isAllScope && resolvedScope.toLowerCase() === s.name.toLowerCase(),
            text: s.tableCount > 0 ? `${s.name}（${s.tableCount} 表）` : s.name,
          })
        ),
      ]
    );

    const refreshBtnText = this._schemaRefreshInFlight
      ? "正在刷新并比对..."
      : isAllScope
      ? "全库 Schema 刷新并比对落标"
      : `刷新并比对「${resolvedScope}」全部表`;

    const summary =
      this._lastSchemaRefreshSummary &&
      this._lastSchemaRefreshSummary.connectionId === this.activeConnId
        ? this._lastSchemaRefreshSummary
        : null;

    const summaryCard = summary
      ? el(
          "div",
          { className: "migrator-drawer-summary", dataset: { schemaRefreshSummary: "true" } },
          el(
            "div",
            { className: "migrator-drawer-summary-head" },
            el("strong", { text: `${summary.scopeLabel} · ${summary.checkedTables} 表 / ${summary.totalCols} 列` }),
            el("span", { style: { fontSize: "10.5px", color: "var(--os-muted)" }, text: summary.finishedAt || "" })
          ),
          el(
            "div",
            { className: "migrator-drawer-summary-badges" },
            badge(`已落标 ${summary.compliantCols}`, "success"),
            summary.partialCols > 0 ? badge(`部分 ${summary.partialCols}`, "warn") : null,
            summary.commentOnlyCols > 0 ? badge(`仅差注释 ${summary.commentOnlyCols}`, "info") : null,
            summary.nonCompliantCols > 0 ? badge(`未落标 ${summary.nonCompliantCols}`, "danger") : null,
            summary.unboundCols > 0 ? badge(`未绑标 ${summary.unboundCols}`, "neutral") : null
          )
        )
      : null;

    const inner = el(
      "div",
      { className: "migrator-drawer-inner" },
      el(
        "div",
        { className: "migrator-drawer-header" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "6px", minWidth: "0" } },
          svgIcon("settings", 14),
          el("span", { className: "migrator-drawer-title", text: "对标设置" }),
          conn ? badge(conn.db_type || "DB", "neutral") : null
        ),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          dataset: { action: "close_settings_drawer" },
          "aria-label": "收起对标设置",
          title: "收起对标设置",
          text: "×",
          onClick: () => this.toggleSettingsDrawer(false),
        })
      ),
      el(
        "div",
        { className: "migrator-drawer-body" },
        this._buildDrawerSection({
          key: "format",
          title: "当前库落标标准",
          desc: "为当前数据源指定物理类型对标档位，仅在需要切换库级标准时调整。",
          children: [
            el("div", {
              className: "migrator-drawer-dbname",
              text: conn ? conn.name : "未选择数据源",
              title: conn ? `${conn.name} (${conn.db_type})` : "",
            }),
            fmtSelect,
          ],
        }),
        this._buildDrawerSection({
          key: "tree_stats",
          title: "目录树统计口径",
          desc: "控制左侧目录树表徽标的达标分子与总字段分母统计方式。",
          children: [
            el(
              "div",
              { className: "sidebar-stat-toggles", role: "group", "aria-label": "左侧统计口径" },
              this._buildMacSwitchRow({
                key: "ignore_partial",
                label: "忽略部分落标",
                title: "开启后，已满足其他数据标准格式的「部分落标」字段视作已完成落标",
                checked: this.ignorePartial,
                onChange: (v) => this._setIgnorePartial(v),
              }),
              this._buildMacSwitchRow({
                key: "ignore_unbound",
                label: "忽略未绑标字段",
                title: "开启后，表右侧统计数字仅计已绑标字段；关闭后按表全部物理字段统计",
                checked: this.ignoreUnbound,
                onChange: (v) => this._setIgnoreUnbound(v),
              })
            ),
          ],
        }),
        this._buildDrawerSection({
          key: "schema_refresh",
          title: "全 Schema 刷新与落标比对",
          desc: "同步目标 Schema 最新目录与表列元数据，并全量重算落标结果与树徽标。",
          children: [
            schemaSelect,
            el(
              "button",
              {
                type: "button",
                className: "mac-btn mac-btn-sm mac-btn-primary migrator-drawer-full-btn",
                dataset: { action: "refresh_schema_compliance" },
                disabled: !conn || this._schemaRefreshInFlight,
                onClick: () => this.runSchemaRefreshCompliance(),
              },
              svgIcon("refresh", 13),
              el("span", { text: refreshBtnText })
            ),
            summaryCard,
          ],
        })
      )
    );

    clearEl(this.settingsDrawerMount, inner);
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
        this._renderSettingsDrawer();
        this._syncDrawerToggleButton();
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
      const schKey = String(res.table?.schema_name || this.activeSchema || "public").toLowerCase();
      const tblKey = String(res.table?.table_name || this.activeTable || "").toLowerCase();
      const totCols = Number(res.summary?.total_cols || (res.columns || []).length || 0);
      if (this.activeConnId && tblKey && totCols > 0) {
        this._knownTableColCounts.set(`${this.activeConnId}::${schKey}.${tblKey}`, totCols);
      }
      if (res.table) {
        this.activeSchema = res.table.schema_name || this.activeSchema;
        this.selectedSchema = this.activeSchema || this.selectedSchema;
        this.activeTable = res.table.table_name || this.activeTable;
      }
      this._pruneSelectedBindCols();
      this.tree.renderTreeBody();
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  async runSchemaRefreshCompliance() {
    if (this._schemaRefreshInFlight) return;
    if (!this.activeConnId) {
      Toast.warn("请先选择要刷新并比对的数据源");
      return;
    }
    const { schemas: schemaItems } = this._collectAvailableSchemas(this.activeConnId);
    const resolved = this._resolveSchemaRefreshScope(schemaItems);
    const isAll = resolved === "__ALL__";
    const targetSchema = isAll ? "" : resolved;
    const title = isAll
      ? "全库 Schema 刷新并比对落标"
      : `刷新并比对 ${targetSchema} 全 Schema 落标`;

    this._schemaRefreshInFlight = true;
    this._renderSettingsDrawer();
    try {
      const res = await this._runTracked(title, ({ taskId, signal }) =>
        migratorApi.checkCompliance(
          {
            connection_id: this.activeConnId,
            scope: isAll ? "all_schemas" : "schema",
            schema_name: targetSchema,
            schemas: targetSchema ? [targetSchema] : [],
            refresh_catalog: true,
            only_bound_tables: false,
            format_profile: this.formatProfile,
            force_refresh: true,
          },
          { taskId, signal }
        )
      );
      if (this._destroyed) return;
      if (res.catalog) {
        store.updateCatalogTree(this.activeConnId, res.catalog);
      }
      if (this.tree?.tableColumnsCache) {
        const prefix = `${this.activeConnId}::`;
        for (const k of [...this.tree.tableColumnsCache.keys()]) {
          if (k.startsWith(prefix)) this.tree.tableColumnsCache.delete(k);
        }
      }
      if (Array.isArray(res.tables)) {
        for (const tr of res.tables) {
          const schK = String(tr?.table?.schema_name || "public").toLowerCase();
          const tblK = String(tr?.table?.table_name || "").toLowerCase();
          const totC = Number(tr?.summary?.total_cols || (tr?.columns || []).length || 0);
          if (tblK && totC > 0) {
            this._knownTableColCounts.set(`${this.activeConnId}::${schK}.${tblK}`, totC);
          }
        }
      }
      if (res.table_stats) this.tableStats = res.table_stats;
      if (this.activeTable) {
        const actTblLow = String(this.activeTable || "").trim().toLowerCase();
        const actSchLow = String(this.activeSchema || "").trim().toLowerCase();
        const matched = (res.tables || []).find((tr) => {
          const ts = String(tr?.table?.schema_name || "").trim().toLowerCase();
          const tt = String(tr?.table?.table_name || "").trim().toLowerCase();
          return tt === actTblLow && (!actSchLow || ts === actSchLow);
        });
        if (matched) {
          this.tableCheck = matched;
          if (matched.table) {
            this.activeSchema = matched.table.schema_name || this.activeSchema;
            this.selectedSchema = this.activeSchema || this.selectedSchema;
            this.activeTable = matched.table.table_name || this.activeTable;
          }
          this._pruneSelectedBindCols();
        } else if (res.active_table) {
          this.tableCheck = res.active_table;
          this._pruneSelectedBindCols();
        }
      }
      const agg = res.aggregate || {};
      this._lastSchemaRefreshSummary = {
        connectionId: this.activeConnId,
        scopeLabel: isAll ? "全部 Schema" : targetSchema,
        checkedTables: Number(agg.checked_tables ?? (res.tables || []).length ?? 0),
        totalCols: Number(agg.total_cols ?? 0),
        compliantCols: Number(agg.compliant_cols ?? 0),
        partialCols: Number(agg.partial_compliant_cols ?? 0),
        commentOnlyCols: Number(agg.comment_only_cols ?? 0),
        nonCompliantCols: Number(agg.non_compliant_cols ?? 0),
        unboundCols: Number(agg.unbound_cols ?? 0),
        finishedAt: new Date().toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }),
      };
      if (res.warning) Toast.warn(res.warning);
      else Toast.success(res.message || "全 Schema 刷新并比对完成");
      this.tree.renderTreeBody();
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    } finally {
      this._schemaRefreshInFlight = false;
      if (!this._destroyed) {
        this._renderSettingsDrawer();
      }
    }
  }

  _pruneSelectedBindCols() {
    const cols = this.tableCheck?.columns || [];
    if (!cols.length || !this.selectedBindCols.size) return;
    const blocked = new Set();
    const present = new Set();
    for (const c of cols) {
      const name = String(c.column_name || "");
      present.add(name);
      if (!c.std_code || c.compliance_status === "COMPLIANT" || c.compliance_status === "STD_MISSING") {
        blocked.add(name);
      }
    }
    for (const k of [...this.selectedBindCols]) {
      if (!present.has(k) || blocked.has(k)) {
        this.selectedBindCols.delete(k);
      }
    }
  }

  _refreshBindingsStageStateInPlace() {
    const queueCount = (this.ws?.items || []).length;
    if (this._queueTabBtn) {
      this._queueTabBtn.textContent = `贯标改造执行队列 (${queueCount})`;
    }
    const sch = this.tableCheck?.table?.schema_name || this.activeSchema;
    const tbl = this.tableCheck?.table?.table_name || this.activeTable;
    if (this._stageBtnByCol && this._stageBtnByCol.size > 0) {
      for (const [colLower, btn] of this._stageBtnByCol.entries()) {
        const rawCol = btn.dataset?.stageCol || colLower;
        const inQueue = this._isColumnInQueue(this.activeConnId, sch, tbl, rawCol);
        btn.className = inQueue ? "btn btn-xs btn-ghost" : "btn btn-xs";
        btn.textContent = inQueue ? "已入队" : "纳入贯标";
        btn.title = inQueue
          ? "已在全局贯标队列中，点击可按当前标准重新同步"
          : "加入全局贯标改造执行队列";
      }
    }
    this._bindingsGrid?._syncSelectionDom?.();
    if (this._bindingStageBtn) {
      this._bindingStageBtn.textContent =
        this.selectedBindCols.size > 0
          ? `纳入贯标任务 (${this.selectedBindCols.size})`
          : "纳入本表待改字段";
    }
  }

  _scheduleCompletedFadeOut(ids) {
    const targetIds = [...new Set((ids || []).map((x) => Number(x)).filter((n) => n > 0))];
    if (!targetIds.length) return;
    const idKeySet = new Set(targetIds.map((id) => String(id)));
    const entry = {
      ids: targetIds,
      collapseTimer: null,
      removeTimer: null,
    };
    this._clearingBatches.add(entry);

    entry.collapseTimer = setTimeout(() => {
      if (this._destroyed) return;
      const rootEl = this._queueGrid?.root;
      if (rootEl && typeof rootEl.querySelectorAll === "function") {
        const rows = rootEl.querySelectorAll("tr[data-row-key]");
        for (const tr of rows) {
          if (idKeySet.has(String(tr.dataset?.rowKey || ""))) {
            tr.classList.add("is-collapsing");
          }
        }
      }
    }, 2450);

    entry.removeTimer = setTimeout(async () => {
      this._clearingBatches.delete(entry);
      if (this._destroyed) return;
      try {
        const delRes = await migratorApi.deleteItems(targetIds);
        if (this._destroyed) return;
        for (const id of targetIds) {
          this._clearingCompletedIds.delete(id);
          this.selectedIds.delete(String(id));
        }
        this.ws = delRes;
        if (delRes?.logs) store.updateLogs(delRes.logs);
        this.render();
        this.onStateUpdate?.();
      } catch (err) {
        for (const id of targetIds) {
          this._clearingCompletedIds.delete(id);
        }
        if (!this._destroyed) {
          this.render();
          Toast.error(err?.message || "清理已达标队列任务失败");
        }
      }
    }, 3000);
  }

  _applyStateResponse(res, { inPlaceBindingsStage = false, animateCompletedOut = false } = {}) {
    this.ws = res;
    if (res.logs) store.updateLogs(res.logs);
    if (res.table_stats_by_conn && this.activeConnId && res.table_stats_by_conn[this.activeConnId]) {
      this.tableStats = res.table_stats_by_conn[this.activeConnId];
      this.tree?.renderTreeBody?.();
    } else if (
      res.table_stats &&
      (!res.table_stats_connection_id || res.table_stats_connection_id === this.activeConnId)
    ) {
      this.tableStats = res.table_stats;
      this.tree?.renderTreeBody?.();
    }
    if (Array.isArray(res.migrated_tables) && res.migrated_tables.length > 0) {
      this._bindingsDirtyAfterMigration = true;
    }
    if (res.warning) Toast.warn(res.warning);
    else if (res.message) Toast.success(res.message);

    const curTableKey = this.tableCheck
      ? `${this.activeConnId}::${this.tableCheck.table?.schema_name || this.activeSchema}::${this.tableCheck.table?.table_name || this.activeTable}`
      : "";
    if (
      inPlaceBindingsStage &&
      this.subMode === "bindings" &&
      this._bindingsGrid &&
      this._bindingsGridTableKey === curTableKey &&
      this.gridMount.children?.[0] === this._bindingsGrid.root
    ) {
      this._refreshBindingsStageStateInPlace();
      this.onStateUpdate?.();
      return;
    }

    let newlyCompletedIds = [];
    if (animateCompletedOut) {
      const rawIds = Array.isArray(res.verified_completed_ids)
        ? res.verified_completed_ids
        : (res.items || [])
            .filter((it) => it.status === "COMPLETED")
            .map((it) => it.id);
      newlyCompletedIds = [
        ...new Set(rawIds.map((x) => Number(x)).filter((n) => n > 0)),
      ].filter((id) => !this._clearingCompletedIds.has(id));
      for (const id of newlyCompletedIds) {
        this._clearingCompletedIds.add(id);
        this.selectedIds.delete(String(id));
      }
    }

    this.render();
    this.onStateUpdate?.();
    if (newlyCompletedIds.length > 0) {
      this._scheduleCompletedFadeOut(newlyCompletedIds);
    }
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
      this._pruneSelectedBindCols();
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
        this._pruneSelectedBindCols();
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
      this._applyStateResponse(res, { animateCompletedOut: true });
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
    const colsMeta = this.tableCheck?.columns || [];
    const colByLower = new Map(
      colsMeta.map((c) => [String(c.column_name || "").trim().toLowerCase(), c])
    );

    let targetCols = [];
    if (Array.isArray(explicitCols) && explicitCols.length > 0) {
      targetCols = explicitCols.filter((name) => {
        const meta = colByLower.get(String(name || "").trim().toLowerCase());
        return !meta || meta.compliance_status !== "COMPLIANT";
      });
      if (!targetCols.length) {
        Toast.info("所选字段已落标，已自动跳过，无需纳入贯标队列");
        return;
      }
    } else if (this.selectedBindCols.size === 0) {
      if (colsMeta.length > 0) {
        const compliantCnt = colsMeta.filter((c) => c.compliance_status === "COMPLIANT").length;
        const stageable = colsMeta.filter(
          (c) =>
            c.std_code &&
            c.compliance_status !== "COMPLIANT" &&
            c.compliance_status !== "STD_MISSING"
        );
        if (!stageable.length) {
          Toast.info(
            compliantCnt > 0
              ? `已自动跳过 ${compliantCnt} 个已落标字段，本表暂无需要纳入贯标的待改字段`
              : "本表暂无需要纳入贯标的已绑标待改字段"
          );
          return;
        }
      }
      targetCols = [];
    } else {
      const selectedList = [...this.selectedBindCols];
      const nonCompliant = selectedList.filter((name) => {
        const meta = colByLower.get(String(name || "").trim().toLowerCase());
        return (
          !meta ||
          (meta.std_code &&
            meta.compliance_status !== "COMPLIANT" &&
            meta.compliance_status !== "STD_MISSING")
        );
      });
      if (!nonCompliant.length) {
        this.selectedBindCols.clear();
        this._bindingsGrid?._syncSelectionDom?.();
        if (this._bindingStageBtn) {
          this._bindingStageBtn.textContent = "纳入本表待改字段";
        }
        Toast.info("所选字段均已落标或未绑标，已自动跳过，无需纳入贯标队列");
        return;
      }
      targetCols = nonCompliant;
    }

    this._stagingInFlight = true;
    if (this._bindingStageBtn) this._bindingStageBtn.disabled = true;
    try {
      const res = await migratorApi.stageMigration({
        connection_id: this.activeConnId,
        schema_name: this.activeSchema,
        table_name: this.activeTable,
        column_names: targetCols,
        format_profile: this.tableCheck?.format_profile || this.formatProfile,
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
      this._applyStateResponse(res, {
        inPlaceBindingsStage: stayInBindings && this.subMode === "bindings",
      });
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
          this.selectedSchema = "";
          this.activeTable = "";
          this.schemaRefreshTarget = "__AUTO__";
          this._lastSchemaRefreshSummary = null;
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
        el("span", { text: "上传字段落标映射关系" })
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
      this._applyStateResponse(res, { animateCompletedOut: true });
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
      const isClearing = this._clearingCompletedIds.has(Number(it.id));
      if (!isClearing) {
        if (this.statusFilter === "REBUILD") {
          if (!this._isRebuildItem(it)) return false;
        } else if (this.statusFilter === "FAILED") {
          if (!["FAILED", "MISSING_TABLE", "MISSING_COLUMN"].includes(it.status)) return false;
        } else if (this.statusFilter !== "ALL" && it.status !== this.statusFilter) {
          return false;
        }
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
      const altTxt = (c.matched_alt_profiles || [])
        .map((p) => `${p.label || ""} ${p.short_label || ""} ${p.target_type || ""}`)
        .join(" ");
      return [
        c.column_name,
        c.column_comment,
        c.std_code,
        c.std_cn_name,
        c.std_en_abbr,
        c.std_domain_name,
        c.compliance_detail,
        altTxt,
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
      this._pruneSelectedBindCols();
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
    this.settingsDrawerMount.classList?.toggle?.("is-open", this.drawerOpen);
    this.settingsDrawerMount.setAttribute?.("aria-hidden", String(!this.drawerOpen));
    this._renderSettingsDrawer();

    const queueCount = (this.ws?.items || []).length;
    this._queueTabBtn = null;
    const tablist = el(
      "div",
      { className: "content-tablist", role: "tablist", "aria-label": "落标绑定与贯标子模式" },
      [
        { id: "bindings", label: "字段落标绑定与核验" },
        { id: "queue", label: `贯标改造执行队列 (${queueCount})` },
      ].map((t) => {
        const btn = el("button", {
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
        });
        if (t.id === "queue") this._queueTabBtn = btn;
        return btn;
      })
    );

    const fmtShort = SHORT_FORMAT_LABELS[this.formatProfile] || this.formatProfile || "自动";
    this._drawerToggleTag = el("span", {
      className: "migrator-drawer-toggle-tag",
      text: fmtShort,
    });
    this._drawerToggleBtn = el(
      "button",
      {
        type: "button",
        className: `migrator-drawer-toggle-btn ${this.drawerOpen ? "is-active" : ""}`.trim(),
        dataset: { action: "toggle_settings_drawer" },
        "aria-expanded": String(Boolean(this.drawerOpen)),
        "aria-controls": "migrator-settings-drawer",
        title: "展开或收起对标设置抽屉",
        onClick: () => this.toggleSettingsDrawer(),
      },
      svgIcon("settings", 13),
      el("span", { text: "对标设置" }),
      this._drawerToggleTag
    );

    clearEl(
      this.tablistMount,
      el("div", { className: "migrator-tablist-bar" }, tablist, this._drawerToggleBtn)
    );

    if (this.subMode === "bindings") {
      this._renderBindingsView();
    } else {
      this._renderQueueView();
    }

    const contentChildren = this.contentColMount.children;
    if (
      contentChildren.length !== 3 ||
      contentChildren[0] !== this.subbarMount ||
      contentChildren[1] !== this.activityBar.el ||
      contentChildren[2] !== this.gridMount
    ) {
      clearEl(this.contentColMount, this.subbarMount, this.activityBar.el, this.gridMount);
    }
    const bodyChildren = this.workspaceBodyMount.children;
    if (
      bodyChildren.length !== 2 ||
      bodyChildren[0] !== this.contentColMount ||
      bodyChildren[1] !== this.settingsDrawerMount
    ) {
      clearEl(this.workspaceBodyMount, this.contentColMount, this.settingsDrawerMount);
    }
    const mainChildren = this.mainMount.children;
    if (
      mainChildren.length !== 2 ||
      mainChildren[0] !== this.tablistMount ||
      mainChildren[1] !== this.workspaceBodyMount
    ) {
      clearEl(this.mainMount, this.tablistMount, this.workspaceBodyMount);
    }
    if (
      this.root.children.length !== 3 ||
      this.root.children[0] !== this.sidebarMount ||
      this.root.children[1] !== this.sidebarResizer ||
      this.root.children[2] !== this.mainMount
    ) {
      clearEl(this.root, this.sidebarMount, this.sidebarResizer, this.mainMount);
    }
  }

  _renderBindingsView() {
    this._queueGrid = null;
    const tc = this.tableCheck;
    const sum = tc?.summary || {};
    const tMeta = tc?.table || {};

    this.bindingSearchInput.value = this.bindingKeyword;

    const pills = [
      { key: "ALL", label: `全部 ${sum.total_cols ?? 0}` },
      { key: "COMPLIANT", label: `已落标 ${sum.compliant_cols ?? 0}` },
      { key: "COMMENT_ONLY", label: `仅差注释 ${sum.comment_only_cols ?? 0}` },
      { key: "PARTIAL_COMPLIANT", label: `部分落标 ${sum.partial_compliant_cols ?? 0}` },
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
          : el("span", { style: { fontSize: "12px", color: "var(--os-muted)" }, text: "请从左侧目录树选择数据表，或点击右上角「上传字段落标映射关系」批量铺底" })
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        tc ? el("search", {}, this.bindingSearchInput) : null,
        tc
          ? (this._bindingStageBtn = el("button", {
              type: "button",
              className: "btn btn-xs mac-btn-primary",
              title: "不勾选时自动纳入本表待改字段，并自动跳过「已落标」字段",
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
      this._bindingsGrid = null;
      this._bindingsGridTableKey = "";
      this._stageBtnByCol = new Map();
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
                text: "上传字段落标映射关系（含系统模板）",
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

    const curTableKey = `${this.activeConnId}::${tMeta.schema_name || this.activeSchema}::${tMeta.table_name || this.activeTable}`;
    const sameTable = Boolean(
      curTableKey &&
        curTableKey === this._bindingsGridTableKey &&
        this._bindingsGrid &&
        this.gridMount.children?.[0] === this._bindingsGrid.root
    );
    const prevScrollTop = sameTable ? this._bindingsGrid.root.scrollTop || 0 : 0;
    const prevScrollLeft = sameTable ? this._bindingsGrid.root.scrollLeft || 0 : 0;
    let focusedAction = null;
    if (sameTable) {
      const activeEl = globalThis.document?.activeElement;
      if (activeEl && this._bindingsGrid.root.contains?.(activeEl)) {
        const ds = activeEl.dataset || {};
        if (ds.stageCol) focusedAction = { attr: "stageCol", val: ds.stageCol };
        else if (ds.bindCol) focusedAction = { attr: "bindCol", val: ds.bindCol };
        else if (ds.unbindCol) focusedAction = { attr: "unbindCol", val: ds.unbindCol };
      }
    } else {
      this._bindingsSort = { sortKey: "", sortAsc: true };
    }

    this._stageBtnByCol = new Map();
    const rows = this._filteredBindingCols();
    const grid = new DataGrid({
      ariaLabel: "表字段落标核验清单",
      selectable: true,
      selectedKeys: this.selectedBindCols,
      initialSortKey: this._bindingsSort.sortKey,
      initialSortAsc: this._bindingsSort.sortAsc,
      onSortChange: (k, a) => {
        this._bindingsSort = { sortKey: k, sortAsc: a };
      },
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
            const altProfiles = Array.isArray(r.matched_alt_profiles) ? r.matched_alt_profiles : [];
            const isPartial = r.compliance_status === "PARTIAL_COMPLIANT" && altProfiles.length > 0;
            const altTitle = isPartial
              ? `符合其他落标：${altProfiles
                  .map((p) => `${p.label || p.value}${p.target_type ? ` (${p.target_type})` : ""}`)
                  .join("、")}`
              : "";
            const detailTxt = isPartial ? (r.compliance_current_diff || r.compliance_detail) : r.compliance_detail;
            return el(
              "div",
              {},
              statusDot(meta.label, meta.tone),
              isPartial
                ? el(
                    "div",
                    {
                      style: {
                        display: "flex",
                        alignItems: "center",
                        gap: "4px",
                        flexWrap: "wrap",
                        marginTop: "3px",
                      },
                      title: altTitle,
                    },
                    el("span", { style: { fontSize: "10.5px", color: "var(--os-muted)" }, text: "满足:" }),
                    altProfiles.slice(0, 2).map((p) =>
                      badge(`${p.label || p.value}${p.target_type ? ` · ${p.target_type}` : ""}`, "warn")
                    ),
                    altProfiles.length > 2 ? badge(`+${altProfiles.length - 2} 项`, "neutral") : null
                  )
                : null,
              r.compliance_status !== "COMPLIANT" && detailTxt
                ? el("div", {
                    style: { fontSize: "11px", color: "var(--os-muted)", marginTop: "2px" },
                    text: detailTxt,
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
            const stageBtn = canStage
              ? el("button", {
                  type: "button",
                  className: inQueue ? "btn btn-xs btn-ghost" : "btn btn-xs",
                  dataset: { stageCol: r.column_name },
                  text: inQueue ? "已入队" : "纳入贯标",
                  title: inQueue ? "已在全局贯标队列中，点击可按当前标准重新同步" : "加入全局贯标改造执行队列",
                  onClick: (e) => {
                    e.stopPropagation();
                    this.stageSelectedToQueue([r.column_name]);
                  },
                })
              : null;
            if (stageBtn) {
              this._stageBtnByCol.set(String(r.column_name || "").trim().toLowerCase(), stageBtn);
            }
            return el(
              "div",
              { style: { display: "flex", gap: "4px" } },
              el("button", {
                type: "button",
                className: "btn btn-xs",
                dataset: { bindCol: r.column_name },
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
                    dataset: { unbindCol: r.column_name },
                    text: "解绑",
                    onClick: (e) => {
                      e.stopPropagation();
                      this._saveColumnBinding(r, null);
                    },
                  })
                : null,
              stageBtn
            );
          },
        },
      ],
    });
    clearEl(this.gridMount, grid.root);
    this._bindingsGrid = grid;
    this._bindingsGridTableKey = curTableKey;
    if (sameTable) {
      grid.root.scrollTop = prevScrollTop;
      grid.root.scrollLeft = prevScrollLeft;
      if (focusedAction) {
        let targetBtn = null;
        if (focusedAction.attr === "stageCol") {
          targetBtn = this._stageBtnByCol.get(String(focusedAction.val).trim().toLowerCase());
        } else {
          const cssAttr = focusedAction.attr === "bindCol" ? "data-bind-col" : "data-unbind-col";
          const safeVal = String(focusedAction.val).replace(/"/g, '\\"');
          targetBtn = grid.root.querySelector?.(`[${cssAttr}="${safeVal}"]`) || null;
        }
        targetBtn?.focus?.({ preventScroll: true });
      }
    }
  }

  _renderQueueView() {
    this._bindingsGrid = null;
    this._bindingsGridTableKey = "";
    this._stageBtnByCol = new Map();
    this.searchInput.value = this.searchKeyword;
    const sameQueue = Boolean(
      this._queueGrid && this.gridMount.children?.[0] === this._queueGrid.root
    );
    const prevScrollTop = sameQueue ? this._queueGrid.root.scrollTop || 0 : 0;
    const prevScrollLeft = sameQueue ? this._queueGrid.root.scrollLeft || 0 : 0;

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
      initialSortKey: this._queueSort.sortKey,
      initialSortAsc: this._queueSort.sortAsc,
      onSortChange: (k, a) => {
        this._queueSort = { sortKey: k, sortAsc: a };
      },
      rowClassName: (r) =>
        this._clearingCompletedIds.has(Number(r.id)) ? "queue-row-verified-clearing" : "",
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
          render: (r) => {
            const isClearing = this._clearingCompletedIds.has(Number(r.id));
            return el(
              "div",
              { style: { display: "flex", gap: "4px" } },
              el("button", {
                type: "button",
                className: "btn btn-xs",
                text: "执行",
                disabled: isClearing || r.status === "COMPLETED",
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
                disabled: isClearing,
                onClick: (e) => {
                  e.stopPropagation();
                  this.openItemSqlPreviewModal(r);
                },
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-ghost",
                text: "移出",
                disabled: isClearing,
                title: "从贯标队列移除",
                onClick: (e) => {
                  e.stopPropagation();
                  this.removeQueueItems([r.id], { skipConfirm: true });
                },
              })
            );
          },
        },
      ],
    });

    clearEl(this.subbarMount, subbar);
    clearEl(this.gridMount, grid.root);
    this._queueGrid = grid;
    if (sameQueue) {
      grid.root.scrollTop = prevScrollTop;
      grid.root.scrollLeft = prevScrollLeft;
    }
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
