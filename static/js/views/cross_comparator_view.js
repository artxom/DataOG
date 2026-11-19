/**
 * Data.O.G 数据标准第 3 步：跨环境比对 (CrossComparatorView)
 * macOS 风格五虚拟列工作台：
 *   1. 来源表待选字段 | 2. 来源表已绑定数标字段 | 3. 数据标准 | 4. 目标表已绑定数标字段 | 5. 目标表待选字段
 * 支持拖拽与点选双向绑标配对，两侧自动沿用各自数据库配置的落标标准格式。
 */

import { crossApi, isAbortError, platformApi, standardsApi } from "../core/api.js";
import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { analyzeCrossColumnCompat } from "../core/type_compat.js";
import { openExportReportModal } from "../ui/export_report_modal.js";
import { Modal } from "../ui/modal.js";
import { openStandardPickerModal } from "../ui/standard_picker_modal.js";
import { TaskActivityBar } from "../ui/task_activity_bar.js";
import { Toast } from "../ui/toast.js";
import { openUploadWizardModal } from "../ui/upload_wizard_modal.js";

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

export class CrossComparatorView {
  constructor({ onPushToMigrator = null, onStateUpdate = null } = {}) {
    this.onPushToMigrator = onPushToMigrator;
    this.onStateUpdate = onStateUpdate;
    this.overview = null;
    this.activeTableKey = "";
    this.loadedOnce = false;
    this.formatProfile = "AUTO";
    this.sourceFormatProfile = "AUTO";
    this.targetFormatProfile = "AUTO";
    this.formatProfiles = DEFAULT_FORMAT_PROFILES;
    this.dirty = false;

    this.dragPayload = null; // { kind: 'candidate'|'bound', side: 'source'|'target', colName: string, rowIdx?: number }
    this.selectedCandidate = null; // { side: 'source'|'target', columnName: string }
    this.srcCandidateFilter = "";
    this.tgtCandidateFilter = "";
    this.matrixFilter = "ALL"; // ALL | UNIFIED | SPLIT_DIFF | SPLIT_PARTIAL | MIGRATION | CAPACITY_RISK | TYPE_CONV
    this.matrixKeyword = "";
    this._lastHydratedPairSig = "";
    this.srcCandidateListMount = el("div", { className: "cross-cand-list" });
    this.tgtCandidateListMount = el("div", { className: "cross-cand-list" });
    this.matrixListMount = el("div", { className: "cross-matrix-list" });
    this.srcCandCountBadge = badge("0", "neutral");
    this.tgtCandCountBadge = badge("0", "neutral");
    this.centerSrcCountBadge = badge("0", "neutral");
    this.centerStdCountBadge = badge("0", "neutral");
    this.centerTgtCountBadge = badge("0", "neutral");
    this.filterBarMount = el("div", {
      className: "cross-filter-bar",
      role: "group",
      "aria-label": "跨环境比对状态筛选",
      onScroll: () => this._syncFilterBarOverflowMask(),
      onWheel: (e) => {
        const bar = this.filterBarMount;
        if (!bar || bar.scrollWidth <= bar.clientWidth + 2) return;
        if (Math.abs(e.deltaY || 0) > Math.abs(e.deltaX || 0)) {
          e.preventDefault?.();
          bar.scrollLeft += e.deltaY;
          this._syncFilterBarOverflowMask();
        }
      },
    });
    this.subbarMount = el("div", { className: "mac-subbar cross-toolbar" });
    this.envStripMount = el("div", { className: "cross-env-strip" });
    this._mountedBodyEl = null;
    this.srcSearchInput = el("input", {
      className: "mac-input cross-cand-search-input",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "筛选待选字段...",
      onInput: (e) => {
        this.srcCandidateFilter = e.target.value;
        this._populateCandidateList("source");
      },
    });
    this.tgtSearchInput = el("input", {
      className: "mac-input cross-cand-search-input",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "筛选待选字段...",
      onInput: (e) => {
        this.tgtCandidateFilter = e.target.value;
        this._populateCandidateList("target");
      },
    });
    this.matrixSearchInput = el("input", {
      className: "mac-input cross-matrix-search-input",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      "aria-label": "筛选已绑定字段与标准",
      placeholder: "筛选字段 / 标准...",
      onInput: (e) => {
        this.matrixKeyword = e.target.value;
        this._populateCenterMatrix();
      },
    });

    this.draftTableBind = {
      source_schema: "",
      source_table: "",
      target_schema: "",
      target_table: "",
    };
    this.sourceCandidates = [];
    this.targetCandidates = [];
    this.centerRows = [];
    this.stdItemCache = new Map();
    this._pendingLookupCodes = new Set();
    this._prefetchedSignature = "";
    this._destroyed = false;

    this.pairPickerOpen = false;
    this.pairSearchQuery = "";
    this.pairSearchTimer = null;
    this.pairSearchAbort = null;
    this.remotePairSearchResults = null;
    this.pairHighlightIdx = -1;
    this._filteredPairItems = [];

    this.pairSearchInput = el("input", {
      className: "mac-input input-xs cross-pair-input",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      "aria-label": "已保存映射表对",
      placeholder: "已保存映射表对 (搜表/字段)...",
      onFocus: () => this._openPairPicker(),
      onClick: () => {
        if (!this.pairPickerOpen) this._openPairPicker();
      },
      onInput: (e) => this._handlePairSearchInput(e.target.value),
      onKeydown: (e) => this._handlePairPickerKeydown(e),
    });
    this.pairPickerBadge = el("span", { className: "badge cross-pair-count", text: "0" });
    this.pairClearBtn = el("button", {
      type: "button",
      className: "cross-chip-remove cross-pair-clear-btn",
      title: "清空搜索或取消已选表对",
      text: "×",
      hidden: true,
      onClick: (e) => {
        e.stopPropagation();
        if (this.pairPickerOpen && this.pairSearchQuery) {
          this.pairSearchQuery = "";
          this.pairSearchInput.value = "";
          this.remotePairSearchResults = null;
          this._renderPairPopover();
          this._syncPairPickerDisplay();
          this.pairSearchInput.focus();
          return;
        }
        if (this.activeTableKey) {
          this.activeTableKey = "";
          this._closePairPicker();
          if (this.loadedOnce && this.overview) {
            this.loadPairComparison("", false);
          } else {
            this.loadOverview("", false);
          }
        }
      },
    });
    this.pairPopover = el("div", { className: "cross-pair-popover", hidden: true });
    this.pairPickerRoot = el(
      "div",
      { className: "cross-pair-picker" },
      el(
        "div",
        { className: "cross-pair-input-wrap" },
        el("span", { className: "cross-pair-icon" }, svgIcon("search", 12)),
        this.pairSearchInput,
        this.pairClearBtn,
        this.pairPickerBadge
      ),
      this.pairPopover
    );
    this._onDocPointerDown = (e) => {
      if (this.pairPickerOpen && this.pairPickerRoot && !this.pairPickerRoot.contains(e.target)) {
        this._closePairPicker();
      }
      for (const role of ["source", "target"]) {
        const ctrl = this.epControls?.[role];
        if (ctrl?.pickerOpen && ctrl.bodyEl && !ctrl.bodyEl.contains(e.target)) {
          this._closeTablePicker(role);
        }
      }
    };
    this._onDocKeyDown = (e) => {
      if (this._isImeComposing(e)) return;
      if (e.key === "Escape") {
        if (this.root?.isConnected === false) return;
        if (globalThis.document?.querySelector?.("dialog[open]")) return;
        if (this._isAnyTablePickerOpen()) {
          e.preventDefault?.();
          this._closeAllTablePickers({ restoreFocus: true });
          return;
        }
        if (this.pairPickerOpen) {
          e.preventDefault?.();
          this._closePairPicker();
          this.pairSearchInput?.blur?.();
          return;
        }
        if (this.selectedCandidate) {
          this.selectedCandidate = null;
          this.render();
        }
      }
    };
    this._onWinResize = () => {
      this._syncFilterBarOverflowMask();
      if (this.pairPickerOpen) this._positionPairPopover();
      for (const role of ["source", "target"]) {
        if (this.epControls?.[role]?.pickerOpen) this._positionTablePicker(role);
      }
    };
    document.addEventListener("pointerdown", this._onDocPointerDown);
    document.addEventListener("keydown", this._onDocKeyDown);
    globalThis.window?.addEventListener?.("resize", this._onWinResize);

    clearEl(
      this.subbarMount,
      el("div", { className: "cross-toolbar-left" }, this.pairPickerRoot),
      el(
        "div",
        { className: "cross-toolbar-right" },
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm",
            title: "强制重新从数据源读取当前表对结构",
            onClick: () => {
              if (this.loadedOnce && this.overview) {
                if (this.activeTableKey) {
                  this.loadPairComparison(this.activeTableKey, true, {
                    sourceFormatProfile: this.sourceFormatProfile,
                    targetFormatProfile: this.targetFormatProfile,
                  });
                } else if (this.draftTableBind.source_table || this.draftTableBind.target_table) {
                  this.previewSelectedTables(true);
                } else {
                  this.loadOverview("", true);
                }
              } else {
                this.loadOverview(this.activeTableKey, true);
              }
            },
          },
          svgIcon("refresh", 12),
          el("span", { text: "强制刷新" })
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm",
            onClick: () => this.openPairingUploadWizard(),
          },
          svgIcon("upload", 12),
          el("span", { text: "上传映射铺底" })
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm",
            onClick: () => this.openExportDialog(),
          },
          svgIcon("download", 12),
          el("span", { text: "导出报告" })
        ),
        el("button", {
          type: "button",
          className: "mac-btn mac-btn-sm mac-btn-ghost",
          text: "送入多邻国 →",
          onClick: () =>
            store.openApp("polyglot", {
              mode: "dialect",
              connectionId: this.overview?.source_connection?.connection_id || "",
              schemaName: this.draftTableBind.source_schema,
              tableName: this.draftTableBind.source_table,
              stdTableKey: this.activeTableKey,
            }),
        })
      )
    );

    this.epControls = {
      source: this._createSideEndpointControls("source"),
      target: this._createSideEndpointControls("target"),
    };
    this.srcConnSelect = this.epControls.source.connSelect;
    this.tgtConnSelect = this.epControls.target.connSelect;
    this.srcFmtSelect = this.epControls.source.fmtSelect;
    this.tgtFmtSelect = this.epControls.target.fmtSelect;
    this.srcSchemaInput = this.epControls.source.schemaInput;
    this.srcTableInput = this.epControls.source.tableInput;
    this.tgtSchemaInput = this.epControls.target.schemaInput;
    this.tgtTableInput = this.epControls.target.tableInput;

    this.centerBridgeCountEl = el("span", { className: "mono", text: "0 组" });
    this.centerBridgeEl = el(
      "div",
      { className: "cross-env-center-pill" },
      el("span", { className: "cross-bridge-icon" }, svgIcon("swap", 12)),
      el("span", { style: { fontWeight: "600", color: "var(--os-text)" }, text: "标准对齐" }),
      this.centerBridgeCountEl
    );
    clearEl(
      this.envStripMount,
      this.epControls.source.cardEl,
      this.centerBridgeEl,
      this.epControls.target.cardEl
    );

    this.srcCandColEl = this._buildCandidateColumnShell("source");
    this.tgtCandColEl = this._buildCandidateColumnShell("target");
    this.matrixPaneEl = el(
      "div",
      { className: "cross-matrix-pane" },
      el(
        "div",
        { className: "cross-center-sticky-top" },
        el(
          "div",
          { className: "cross-center-col-headers" },
          el(
            "div",
            { className: "cross-col-head" },
            el("span", { className: "cross-col-head-title", text: "来源表已绑定数标字段" }),
            this.centerSrcCountBadge
          ),
          el(
            "div",
            { className: "cross-col-head is-center" },
            el("span", { className: "cross-col-head-title", text: "数据标准" }),
            this.centerStdCountBadge
          ),
          el(
            "div",
            { className: "cross-col-head" },
            el("span", { className: "cross-col-head-title", text: "目标表已绑定数标字段" }),
            this.centerTgtCountBadge
          )
        ),
        el(
          "div",
          { className: "cross-center-filterbar" },
          this.filterBarMount,
          el(
            "search",
            { className: "cross-inline-search" },
            svgIcon("search", 12),
            this.matrixSearchInput
          )
        )
      ),
      this.matrixListMount
    );
    this.centerStageEl = el("section", { className: "cross-center-stage" }, this.matrixPaneEl);
    this.workspaceEl = el(
      "div",
      { className: "cross-five-workspace" },
      el(
        "div",
        { className: "cross-five-inner" },
        this.srcCandColEl,
        this.centerStageEl,
        this.tgtCandColEl
      )
    );

    this.activityBar = new TaskActivityBar();
    this.activityBar.el.style.margin = "6px 14px 0";
    this.activityBar.el.style.flexShrink = "0";

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });

    this._unsubStore = store.subscribe((state, reason) => {
      if (reason === "connections" || reason === "bootstrap") {
        this._syncConnectionsFromStore(state.connections || []);
      }
    });
  }

  destroy() {
    this._destroyed = true;
    clearTimeout(this.pairSearchTimer);
    this.pairSearchAbort?.abort();
    document.removeEventListener("pointerdown", this._onDocPointerDown);
    document.removeEventListener("keydown", this._onDocKeyDown);
    globalThis.window?.removeEventListener?.("resize", this._onWinResize);
    this.activityBar?.destroy();
    this._unsubStore?.();
    this._unsubStore = null;
  }

  _leaveDropTarget(e) {
    if (e?.relatedTarget && e.currentTarget?.contains?.(e.relatedTarget)) return;
    e.currentTarget?.classList?.remove("is-drop-target");
  }

  _resetScrollPositions({ centerOnly = false } = {}) {
    if (this.matrixPaneEl) this.matrixPaneEl.scrollTop = 0;
    if (!centerOnly) {
      if (this.srcCandidateListMount) this.srcCandidateListMount.scrollTop = 0;
      if (this.tgtCandidateListMount) this.tgtCandidateListMount.scrollTop = 0;
      if (this.workspaceEl) this.workspaceEl.scrollLeft = 0;
    }
  }

  _syncConnectionsFromStore(connections) {
    if (!this.overview || !Array.isArray(connections)) return;
    this.overview.connections = connections;
    const srcCid = this.overview.source_connection?.connection_id;
    const tgtCid = this.overview.target_connection?.connection_id;
    let changed = false;
    if (srcCid) {
      const sc = connections.find((c) => c.connection_id === srcCid);
      if (sc && sc.standard_format && sc.standard_format !== this.overview.source_connection.standard_format) {
        this.overview.source_connection.standard_format = sc.standard_format;
        this.sourceFormatProfile = sc.standard_format;
        changed = true;
      }
    }
    if (tgtCid) {
      const tc = connections.find((c) => c.connection_id === tgtCid);
      if (tc && tc.standard_format && tc.standard_format !== this.overview.target_connection.standard_format) {
        this.overview.target_connection.standard_format = tc.standard_format;
        this.targetFormatProfile = tc.standard_format;
        changed = true;
      }
    }
    if (changed) {
      this.render();
    } else {
      this._syncEndpointDeck();
    }
  }

  async ensureLoaded(force = false) {
    if (this.loadedOnce && !force) return;
    await this.loadOverview(this.activeTableKey, false);
  }

  async _reloadCurrentComparison(extra = {}, { pairOnly = false } = {}) {
    const loader =
      pairOnly && this.loadedOnce && this.overview
        ? this.loadPairComparison.bind(this)
        : this.loadOverview.bind(this);
    const savedTables = this.overview?.tables || [];
    const hasSavedActive = Boolean(
      this.activeTableKey && savedTables.some((t) => t.std_table_key === this.activeTableKey)
    );
    if (hasSavedActive) {
      return loader(this.activeTableKey, false, extra);
    }
    if (this.draftTableBind.source_table || this.draftTableBind.target_table) {
      return loader("", false, {
        sourceSchema: this.draftTableBind.source_schema,
        sourceTable: this.draftTableBind.source_table,
        targetSchema: this.draftTableBind.target_schema,
        targetTable: this.draftTableBind.target_table,
        ...extra,
      });
    }
    return loader(this.activeTableKey, false, extra);
  }

  async refreshOnActivate() {
    if (!this.loadedOnce) {
      await this.ensureLoaded(false);
      return;
    }
    if (this.dirty) {
      this.render();
      this.onStateUpdate?.();
      return;
    }
    await this._reloadCurrentComparison();
  }

  _markDirty() {
    this.dirty = true;
    this.render();
    this.onStateUpdate?.();
  }

  _hasFormatColumns(item) {
    if (!item || typeof item !== "object") return false;
    return (
      "type_dws" in item ||
      "type_dw_mart" in item ||
      "type_argodb" in item ||
      "type_mysql" in item ||
      "type_oracle" in item
    );
  }

  _cacheStdItem(item) {
    if (item?.std_code && this._hasFormatColumns(item)) {
      this.stdItemCache.set(String(item.std_code), item);
    }
  }

  _isSameSideFormatAndDialect() {
    const ov = this.overview || {};
    const sDb = String(ov.source_connection?.db_type || "DWS").toUpperCase();
    const tDb = String(ov.target_connection?.db_type || "DWS").toUpperCase();
    return (
      this.sourceFormatProfile === this.targetFormatProfile &&
      (this.sourceFormatProfile !== "AUTO" || sDb === tDb)
    );
  }

  async _resolvePendingStdTypesAsync(codes) {
    if (!standardsApi?.lookup) return;
    const unique = [
      ...new Set((codes || []).map((c) => String(c || "").trim()).filter(Boolean)),
    ].filter((c) => !this.stdItemCache.has(c) && !this._pendingLookupCodes.has(c));
    if (!unique.length) return;
    unique.forEach((c) => this._pendingLookupCodes.add(c));
    try {
      await Promise.all(
        unique.map(async (code) => {
          try {
            const res = await standardsApi.lookup(code, 10);
            const match = (res?.items || []).find(
              (it) => String(it.std_code || "").toUpperCase() === code.toUpperCase()
            );
            if (match) this._cacheStdItem(match);
          } catch (_err) {
            // 忽略单条异步补全失败
          } finally {
            this._pendingLookupCodes.delete(code);
          }
        })
      );
      let updated = false;
      for (const row of this.centerRows) {
        const sCode = String(row.source_std_code || "");
        if (row.source_std?._pendingType && this.stdItemCache.has(sCode)) {
          row.source_std = this._toStdBrief(this.stdItemCache.get(sCode), "source");
          updated = true;
        }
        const tCode = String(row.target_std_code || "");
        if (row.target_std?._pendingType && this.stdItemCache.has(tCode)) {
          row.target_std = this._toStdBrief(this.stdItemCache.get(tCode), "target");
          const ev = this._evaluateProvisionalTargetCompliance(row.target_col, row.target_std);
          row.target_compliance = ev.target_compliance;
          row.target_needs_migration = ev.target_needs_migration;
          updated = true;
        }
      }
      if (updated) {
        this.render();
      }
    } finally {
      unique.forEach((c) => this._pendingLookupCodes.delete(c));
    }
  }

  _pickResolvedType(stdItem, formatProfile = "AUTO", dbType = "DWS") {
    if (!stdItem) return "";
    if (formatProfile && formatProfile !== "AUTO" && stdItem[formatProfile]) {
      return stdItem[formatProfile];
    }
    const dt = String(dbType || "DWS").toUpperCase();
    if (dt === "INCEPTOR") {
      return stdItem.type_argodb || stdItem.type_dws || stdItem.type_mysql || "";
    }
    if (dt === "MYSQL") {
      return stdItem.type_mysql || stdItem.type_ob_mysql || stdItem.type_dws || "";
    }
    if (dt === "ORACLE") {
      return stdItem.type_oracle || stdItem.type_ob_oracle || stdItem.type_dws || "";
    }
    if (dt === "DM8") {
      return stdItem.type_dameng || stdItem.type_oracle || stdItem.type_dws || "";
    }
    return stdItem.type_dws || stdItem.type_dw_mart || stdItem.type_oracle || stdItem.type_mysql || "";
  }

  _toStdBrief(picked, side, { fromSide = null } = {}) {
    if (!picked) return null;
    this._cacheStdItem(picked);
    const ov = this.overview || {};
    const dbType =
      side === "source"
        ? ov.source_connection?.db_type || "DWS"
        : ov.target_connection?.db_type || "DWS";
    const fmt = side === "source" ? this.sourceFormatProfile : this.targetFormatProfile;
    const fullItem = this._hasFormatColumns(picked)
      ? picked
      : this.stdItemCache.get(String(picked.std_code || "")) || null;
    let resolvedType = "";
    let pendingType = false;
    if (fullItem) {
      resolvedType = this._pickResolvedType(fullItem, fmt, dbType);
    } else if (!fromSide || fromSide === side || this._isSameSideFormatAndDialect()) {
      resolvedType = picked.resolved_type || "";
    } else {
      resolvedType = "";
      pendingType = true;
    }
    return {
      std_code: picked.std_code,
      cn_name: picked.cn_name,
      en_abbr: picked.en_abbr,
      resolved_type: resolvedType,
      domain_name: picked.domain_name || "",
      missing: Boolean(picked.missing),
      _pendingType: pendingType,
    };
  }

  _recomputeRowState(row) {
    const paired = Boolean(row.source_col && row.target_col);
    const sCode = String(row.source_std_code || "").trim();
    const tCode = String(row.target_std_code || "").trim();
    row.paired = paired;
    if (paired && sCode && sCode === tCode) {
      row.bookmark_state = "UNIFIED";
    } else if (sCode && tCode && sCode !== tCode) {
      row.bookmark_state = "SPLIT_DIFF";
    } else if (sCode || tCode) {
      row.bookmark_state = "SPLIT_PARTIAL";
    } else {
      row.bookmark_state = "SPLIT_UNBOUND";
    }
    const sDb = String(this.overview?.source_connection?.db_type || "");
    const tDb = String(this.overview?.target_connection?.db_type || "");
    if (!paired) {
      row.cross_compat = analyzeCrossColumnCompat("", "");
    } else {
      const sType = String(row.source_col?.data_type || "");
      const tType = String(row.target_col?.data_type || "");
      const prev = row.cross_compat;
      if (
        !prev ||
        prev._srcType !== sType ||
        prev._tgtType !== tType ||
        prev._srcDb !== sDb ||
        prev._tgtDb !== tDb
      ) {
        const nextCc = analyzeCrossColumnCompat(sType, tType, sDb, tDb);
        nextCc._srcType = sType;
        nextCc._tgtType = tType;
        nextCc._srcDb = sDb;
        nextCc._tgtDb = tDb;
        row.cross_compat = nextCc;
      }
    }
    return row;
  }

  _computeBookmarkSummary() {
    const sum = {
      total_rows: this.centerRows.length,
      paired_rows: 0,
      unified: 0,
      split_diff: 0,
      split_partial: 0,
      unbound_src: this.sourceCandidates.length,
      unbound_tgt: this.targetCandidates.length,
      target_needs_migration: 0,
      capacity_insufficient: 0,
      capacity_risk: 0,
      type_conversion: 0,
    };
    for (const r of this.centerRows) {
      this._recomputeRowState(r);
      if (r.paired) sum.paired_rows += 1;
      if (r.bookmark_state === "UNIFIED") sum.unified += 1;
      else if (r.bookmark_state === "SPLIT_DIFF") sum.split_diff += 1;
      else sum.split_partial += 1;
      if (r.target_needs_migration) sum.target_needs_migration += 1;
      const cc = r.cross_compat;
      if (cc?.applicable) {
        if (cc.capacity_status === "INSUFFICIENT") sum.capacity_insufficient += 1;
        else if (cc.capacity_status === "RISK") sum.capacity_risk += 1;
        if (cc.has_type_conversion) sum.type_conversion += 1;
      }
    }
    return sum;
  }

  _sortCandidates(list) {
    return list.sort((a, b) => {
      const ao = Number(a.ordinal_position || 999999);
      const bo = Number(b.ordinal_position || 999999);
      if (ao !== bo) return ao - bo;
      return String(a.column_name || "").localeCompare(String(b.column_name || ""));
    });
  }

  _hydrateDraftFromComparison(comp) {
    this.dirty = false;
    this.selectedCandidate = null;
    this.dragPayload = null;
    this.sourceCandidates = [];
    this.targetCandidates = [];
    this.centerRows = [];
    this._closeAllTablePickers();
    if (!comp) {
      this._lastHydratedPairSig = "";
      this.matrixFilter = "ALL";
      this.matrixKeyword = "";
      this.matrixSearchInput.value = "";
      this._syncEndpointInputsFromDraft({ force: true });
      this._resetScrollPositions();
      return;
    }

    const tb = comp.table_binding || {};
    this.draftTableBind = {
      source_schema: tb.source_schema || comp.source_panel?.schema_name || "",
      source_table: tb.source_table || comp.source_panel?.table_name || "",
      target_schema: tb.target_schema || comp.target_panel?.schema_name || "",
      target_table: tb.target_table || comp.target_panel?.table_name || "",
    };
    this._syncEndpointInputsFromDraft({ force: true });
    const nextPairSig = `${this.draftTableBind.source_schema}.${this.draftTableBind.source_table}::${this.draftTableBind.target_schema}.${this.draftTableBind.target_table}`.toLowerCase();
    if (nextPairSig !== this._lastHydratedPairSig) {
      this._lastHydratedPairSig = nextPairSig;
      this.matrixFilter = "ALL";
      this.matrixKeyword = "";
      this.matrixSearchInput.value = "";
      this._resetScrollPositions();
    }

    if (comp.source_format_profile) this.sourceFormatProfile = comp.source_format_profile;
    if (comp.target_format_profile) this.targetFormatProfile = comp.target_format_profile;

    const seenSrc = new Set();
    const seenTgt = new Set();

    for (const dr of comp.dual_rows || []) {
      const sCol = dr.source_col ? { ...dr.source_col } : null;
      const tCol = dr.target_col ? { ...dr.target_col } : null;
      const sCode = String(dr.source_std_code || "").trim();
      const tCode = String(dr.target_std_code || "").trim();

      if (sCol && !sCode) {
        const k = String(sCol.column_name || "").toLowerCase();
        if (k && !seenSrc.has(k)) {
          seenSrc.add(k);
          this.sourceCandidates.push(sCol);
        }
      }
      if (tCol && !tCode) {
        const k = String(tCol.column_name || "").toLowerCase();
        if (k && !seenTgt.has(k)) {
          seenTgt.add(k);
          this.targetCandidates.push(tCol);
        }
      }

      const boundSrc = sCol && sCode ? sCol : null;
      const boundTgt = tCol && tCode ? tCol : null;
      if (boundSrc) seenSrc.add(String(boundSrc.column_name || "").toLowerCase());
      if (boundTgt) seenTgt.add(String(boundTgt.column_name || "").toLowerCase());

      if (boundSrc || boundTgt) {
        const sDb = String(this.overview?.source_connection?.db_type || "");
        const tDb = String(this.overview?.target_connection?.db_type || "");
        let initCc = null;
        if (boundSrc && boundTgt && dr.cross_compat && dr.cross_compat.capacity_status !== undefined) {
          initCc = {
            ...dr.cross_compat,
            _srcType: String(boundSrc.data_type || ""),
            _tgtType: String(boundTgt.data_type || ""),
            _srcDb: sDb,
            _tgtDb: tDb,
          };
        }
        this.centerRows.push(
          this._recomputeRowState({
            row_id: dr.row_id || `row_${this.centerRows.length + 1}`,
            source_col: boundSrc,
            target_col: boundTgt,
            source_std_code: boundSrc ? sCode : "",
            target_std_code: boundTgt ? tCode : "",
            source_std: boundSrc && dr.source_std ? { ...dr.source_std } : null,
            target_std: boundTgt && dr.target_std ? { ...dr.target_std } : null,
            target_compliance: boundTgt && dr.target_compliance ? { ...dr.target_compliance } : null,
            target_needs_migration: Boolean(boundTgt && dr.target_needs_migration),
            cross_compat: initCc,
          })
        );
      }
    }

    for (const sc of comp.source_panel?.columns || []) {
      const k = String(sc.column_name || "").toLowerCase();
      if (k && !seenSrc.has(k)) {
        seenSrc.add(k);
        this.sourceCandidates.push({ ...sc });
      }
    }
    for (const tc of comp.target_panel?.columns || []) {
      const k = String(tc.column_name || "").toLowerCase();
      if (k && !seenTgt.has(k)) {
        seenTgt.add(k);
        this.targetCandidates.push({ ...tc });
      }
    }

    this._sortCandidates(this.sourceCandidates);
    this._sortCandidates(this.targetCandidates);
  }

  _syncSideFormatsFromOverview(res) {
    if (!res) return;
    if (res.source_format_profile) {
      this.sourceFormatProfile = res.source_format_profile;
    } else if (res.source_connection?.standard_format) {
      this.sourceFormatProfile = res.source_connection.standard_format;
    }
    if (res.target_format_profile) {
      this.targetFormatProfile = res.target_format_profile;
    } else if (res.target_connection?.standard_format) {
      this.targetFormatProfile = res.target_connection.standard_format;
    }
  }

  _mergeRoleConnectionBrief(nextConn, prevConn) {
    if (!nextConn) return prevConn || null;
    if (
      !Array.isArray(nextConn.tables) &&
      prevConn &&
      prevConn.connection_id === nextConn.connection_id
    ) {
      return {
        ...prevConn,
        ...nextConn,
        schemas: prevConn.schemas || [],
        tables: prevConn.tables || [],
      };
    }
    return nextConn;
  }

  _applyComparisonResponse(res, { mergeBrief = false } = {}) {
    const prevOv = this.overview || {};
    this.loadedOnce = true;
    if (mergeBrief) {
      this.overview = {
        ...prevOv,
        ...res,
        source_connection: this._mergeRoleConnectionBrief(
          res.source_connection,
          prevOv.source_connection
        ),
        target_connection: this._mergeRoleConnectionBrief(
          res.target_connection,
          prevOv.target_connection
        ),
      };
    } else {
      this.overview = res;
    }
    if (res.format_profiles?.length) this.formatProfiles = res.format_profiles;
    this._syncSideFormatsFromOverview(this.overview);
    if (res.active_table_key !== undefined) {
      this.activeTableKey = res.active_table_key || "";
    }
    this._hydrateDraftFromComparison(res.active_comparison);
    this.render();
    this.onStateUpdate?.();
  }

  _maybePrefetchPairs() {
    if (this._destroyed || !this.overview || (this.overview.tables?.length || 0) <= 1) return;
    const srcCid = String(this.overview.source_connection?.connection_id || "");
    const tgtCid = String(this.overview.target_connection?.connection_id || "");
    if (!srcCid && !tgtCid) return;
    const sig = `${srcCid}::${tgtCid}`;
    if (this._prefetchedSignature === sig) return;
    this._prefetchedSignature = sig;
    setTimeout(() => {
      if (this._destroyed) return;
      crossApi.prefetchPairs({ max_pairs: 60 }).catch(() => {});
    }, 80);
  }

  async loadOverview(tableKey = "", forceRefresh = false, extra = {}) {
    try {
      const reqSourceFmt = extra.sourceFormatProfile ?? "AUTO";
      const reqTargetFmt = extra.targetFormatProfile ?? "AUTO";
      const res = await this.activityBar.track(
        "加载跨环境比对",
        async ({ taskId, signal }) =>
          crossApi.getOverview(
            tableKey,
            forceRefresh,
            {
              formatProfile: this.formatProfile,
              sourceFormatProfile: reqSourceFmt,
              targetFormatProfile: reqTargetFmt,
              ...extra,
            },
            { signal, taskId }
          ),
        { prefix: "cmp" }
      );
      this._applyComparisonResponse(res, { mergeBrief: false });
      this._maybePrefetchPairs();
      return true;
    } catch (err) {
      if (isAbortError(err)) return false;
      Toast.error(err.message);
      return false;
    }
  }

  async loadPairComparison(tableKey = "", forceRefresh = false, extra = {}) {
    try {
      const reqSourceFmt = extra.sourceFormatProfile ?? "AUTO";
      const reqTargetFmt = extra.targetFormatProfile ?? "AUTO";
      const res = await this.activityBar.track(
        "切换比对表对",
        async ({ taskId, signal }) =>
          crossApi.comparePair(
            {
              tableKey,
              forceRefresh,
              formatProfile: this.formatProfile,
              sourceFormatProfile: reqSourceFmt,
              targetFormatProfile: reqTargetFmt,
              ...extra,
            },
            { signal, taskId }
          ),
        { prefix: "cmp" }
      );
      this._applyComparisonResponse(res, { mergeBrief: true });
      return true;
    } catch (err) {
      if (isAbortError(err)) return false;
      Toast.error(err.message);
      return false;
    }
  }

  _isImeComposing(e) {
    return Boolean(e?.isComposing || e?.keyCode === 229);
  }

  _cleanQualPart(v) {
    return String(v || "")
      .trim()
      .replace(/^["'“”‘’`「」『』\s]+|["'“”‘’`「」『』\s]+$/g, "");
  }

  _normalizeDraftEndpoints() {
    let mutated = false;
    for (const role of ["source", "target"]) {
      const schKey = `${role}_schema`;
      const tblKey = `${role}_table`;
      const rawTbl = this._cleanQualPart(this.draftTableBind[tblKey]);
      const dotIdx = rawTbl.lastIndexOf(".");
      if (dotIdx > 0 && dotIdx < rawTbl.length - 1) {
        const prefix = this._cleanQualPart(rawTbl.slice(0, dotIdx));
        const suffix = this._cleanQualPart(rawTbl.slice(dotIdx + 1));
        if (prefix && suffix) {
          this.draftTableBind[schKey] = prefix;
          this.draftTableBind[tblKey] = suffix;
          mutated = true;
        }
      }
    }
    if (mutated) {
      this._syncEndpointInputsFromDraft({ force: true });
    }
  }

  _splitTbl(rawSch, rawTbl) {
    const cleanLow = (v) => this._cleanQualPart(v).toLowerCase();
    let sch = cleanLow(rawSch);
    let tbl = cleanLow(rawTbl);
    const dotIdx = tbl.lastIndexOf(".");
    if (dotIdx !== -1) {
      const pfx = cleanLow(tbl.slice(0, dotIdx));
      const sfx = cleanLow(tbl.slice(dotIdx + 1));
      if (pfx && sfx) {
        sch = pfx;
        tbl = sfx;
      } else {
        if (!sch && pfx) sch = pfx;
        tbl = sfx;
      }
    }
    return [sch, tbl];
  }

  _deriveSaveTableKey() {
    if (this.activeTableKey) return this.activeTableKey;
    const [sSch, sTbl] = this._splitTbl(
      this.draftTableBind.source_schema,
      this.draftTableBind.source_table
    );
    const [tSch, tTbl] = this._splitTbl(
      this.draftTableBind.target_schema,
      this.draftTableBind.target_table
    );
    const baseKey = tTbl || sTbl;
    if (!baseKey) return "";

    const savedTables = this.overview?.tables || [];
    const isSamePair = (t) => {
      const [tsSch, tsTbl] = this._splitTbl(t.source_schema, t.source_table);
      const [ttSch, ttTbl] = this._splitTbl(t.target_schema, t.target_table);
      return (
        tsTbl === sTbl &&
        ttTbl === tTbl &&
        (!sSch || !tsSch || tsSch === sSch) &&
        (!tSch || !ttSch || ttSch === tSch)
      );
    };

    const exactMatch = savedTables.find(isSamePair);
    if (exactMatch?.std_table_key) {
      return String(exactMatch.std_table_key).trim().toLowerCase();
    }

    const candidates = [baseKey];
    if (sTbl && tTbl) candidates.push(`${sTbl}__${tTbl}`);
    if (sSch && sTbl && tTbl) candidates.push(`${sSch}_${sTbl}__${tTbl}`);
    if (sSch && sTbl && tSch && tTbl) candidates.push(`${sSch}_${sTbl}__${tSch}_${tTbl}`);

    for (const cand of candidates) {
      const existing = savedTables.find(
        (t) => String(t.std_table_key || "").trim().toLowerCase() === cand
      );
      if (!existing || isSamePair(existing)) {
        return cand;
      }
    }
    const tail = candidates[candidates.length - 1];
    let idx = 2;
    while (
      savedTables.some(
        (t) => String(t.std_table_key || "").trim().toLowerCase() === `${tail}_${idx}`
      )
    ) {
      idx += 1;
    }
    return `${tail}_${idx}`;
  }

  async previewSelectedTables(forceRefresh = false) {
    this._normalizeDraftEndpoints();
    const { source_schema, source_table, target_schema, target_table } = this.draftTableBind;
    if (!source_table && !target_table) {
      Toast.warn("请先在左侧或右侧选择表名");
      return;
    }
    const [sSch, sTbl] = this._splitTbl(source_schema, source_table);
    const [tSch, tTbl] = this._splitTbl(target_schema, target_table);
    const schCompat = (qSch, savedSch) => !qSch || !savedSch || savedSch === qSch;
    const savedTables = this.overview?.tables || [];
    let matched = null;
    if (sTbl && tTbl) {
      matched =
        savedTables.find((t) => {
          const [tsSch, tsTbl] = this._splitTbl(t.source_schema, t.source_table);
          const [ttSch, ttTbl] = this._splitTbl(t.target_schema, t.target_table);
          return (
            tsTbl === sTbl &&
            ttTbl === tTbl &&
            (!sSch || tsSch === sSch) &&
            (!tSch || ttSch === tSch)
          );
        }) ||
        savedTables.find((t) => {
          const [tsSch, tsTbl] = this._splitTbl(t.source_schema, t.source_table);
          const [ttSch, ttTbl] = this._splitTbl(t.target_schema, t.target_table);
          return (
            tsTbl === sTbl &&
            ttTbl === tTbl &&
            schCompat(sSch, tsSch) &&
            schCompat(tSch, ttSch)
          );
        });
    }
    this.activeTableKey = "";
    const targetKey = matched?.std_table_key || "";
    const extraOpts = {
      sourceSchema: source_schema,
      sourceTable: source_table,
      targetSchema: target_schema,
      targetTable: target_table,
      sourceFormatProfile: this.sourceFormatProfile,
      targetFormatProfile: this.targetFormatProfile,
    };
    if (this.loadedOnce && this.overview) {
      await this.loadPairComparison(targetKey, forceRefresh, extraOpts);
    } else {
      await this.loadOverview(targetKey, forceRefresh, extraOpts);
    }
  }

  async saveCurrentPairings(forceRefresh = false) {
    this._normalizeDraftEndpoints();
    const key = this._deriveSaveTableKey();
    if (!key) {
      Toast.warn("请先选择或输入来源库/目标库表名");
      return false;
    }
    try {
      const pairings = [
        ...this.centerRows.map((r) => ({
          source_col: r.source_col?.column_name || "",
          target_col: r.target_col?.column_name || "",
          source_std_code: r.source_std_code || "",
          target_std_code: r.target_std_code || "",
        })),
        ...this.sourceCandidates.map((c) => ({
          source_col: c.column_name || "",
          target_col: "",
          source_std_code: "",
          target_std_code: "",
        })),
        ...this.targetCandidates.map((c) => ({
          source_col: "",
          target_col: c.column_name || "",
          source_std_code: "",
          target_std_code: "",
        })),
      ];
      const res = await this.activityBar.track(
        "保存跨环境映射",
        async ({ taskId, signal }) =>
          crossApi.savePairings(
            {
              std_table_key: key,
              source_schema: this.draftTableBind.source_schema,
              source_table: this.draftTableBind.source_table,
              target_schema: this.draftTableBind.target_schema,
              target_table: this.draftTableBind.target_table,
              format_profile: this.targetFormatProfile || this.formatProfile || "AUTO",
              source_format_profile: this.sourceFormatProfile || "AUTO",
              target_format_profile: this.targetFormatProfile || "AUTO",
              pairings,
              force_refresh: forceRefresh,
              sync_to_field_bindings: true,
            },
            { signal, taskId }
          ),
        { prefix: "cmp", cancellable: false }
      );
      this._applyComparisonResponse(res, { mergeBrief: false });
      Toast.success(res.message || "映射关系与数标绑定已保存");
      return true;
    } catch (err) {
      if (isAbortError(err)) return false;
      Toast.error(err.message);
      return false;
    }
  }

  async _runExportReportTask(title, fn, { syncCatalogs = true } = {}) {
    try {
      const filename = await this.activityBar.track(
        title,
        async ({ taskId, signal }) => fn({ taskId, signal }),
        { prefix: "cmp", cancellable: true, exclusive: false }
      );
      if (this._destroyed) return;
      Toast.success(`已导出：${filename}`);
      if (syncCatalogs) {
        const ov = this.overview || {};
        const sideEntries = [
          ov.source_connection?.configured ? ["source_connection", ov.source_connection.connection_id] : null,
          ov.target_connection?.configured ? ["target_connection", ov.target_connection.connection_id] : null,
        ].filter(Boolean);
        let updated = false;
        await Promise.all(
          sideEntries.map(async ([sideKey, cid]) => {
            try {
              const cat = await platformApi.getMetadataTree(cid, false);
              if (!this._destroyed && cat) {
                store.updateCatalogTree(cid, cat);
                if (this.overview?.[sideKey]) {
                  this.overview[sideKey].tables = cat.tables || [];
                  this.overview[sideKey].schemas = (cat.schemas || []).map((s) =>
                    typeof s === "string" ? s : s.schema_name
                  );
                  updated = true;
                }
              }
            } catch (_ignore) {
              // 忽略非致命目录同步异常
            }
          })
        );
        if (!this._destroyed && updated) {
          this.render();
        }
      }
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  openExportDialog() {
    const ov = this.overview || {};
    const srcConn = ov.source_connection || {};
    const tgtConn = ov.target_connection || {};
    if (!srcConn.configured && !tgtConn.configured && !(ov.tables || []).length) {
      Toast.warn("请先配置跨环境来源库与目标库，或先保存/导入映射表对");
      return;
    }
    const srcName = srcConn.configured ? srcConn.name || "来源库" : "未配来源库";
    const tgtName = tgtConn.configured ? tgtConn.name || "目标库" : "未配目标库";

    openExportReportModal({
      title: "导出跨环境库级比对报告",
      scopeBadge: `${srcName} ↔ ${tgtName}`,
      scopeHint:
        "汇集全部已保存映射表对的五虚拟列比对情况与双方落标标准，同时附上未配对同名表的推荐清单（不做自动字段预比对）；若库目录为空将先自动触发全库目录采集。",
      options: [
        {
          id: "html",
          title: "离线交互网页报告 (.html)",
          badge: "推荐",
          badgeTone: "success",
          icon: "explorer",
          shortExt: ".html",
          ctaLabel: "导出离线网页报告 (.html)",
          desc: "独立离线 HTML 文件，双击即可离线还原「五虚拟列」图形化对标看板与「推荐同名表」清单，并支持随时一键导出单 Sheet Excel。",
          onExport: () =>
            this._runExportReportTask("导出跨环境离线网页报告 (.html)", ({ taskId, signal }) =>
              crossApi.exportReportHtml({
                formatProfile: this.formatProfile,
                sourceFormatProfile: this.sourceFormatProfile,
                targetFormatProfile: this.targetFormatProfile,
                taskId,
                signal,
              })
            ),
        },
        {
          id: "xlsx",
          title: "直接导出库级 Excel (.xlsx)",
          badge: "单 Sheet 全库表",
          badgeTone: "info",
          icon: "table",
          shortExt: ".xlsx",
          ctaLabel: "导出库级 Excel (.xlsx)",
          desc: "单个 Sheet「跨环境库级比对报告」同时包含所有已配对表五栏字段明细及未配对同名表推荐清单，附状态配色与自动筛选。",
          onExport: () =>
            this._runExportReportTask("导出跨环境库级 Excel (.xlsx)", ({ taskId, signal }) =>
              crossApi.exportReportExcel({
                formatProfile: this.formatProfile,
                sourceFormatProfile: this.sourceFormatProfile,
                targetFormatProfile: this.targetFormatProfile,
                taskId,
                signal,
              })
            ),
        },
      ],
      secondaryActions: [
        {
          label: "导出三方绑定铺底表 (.xlsx)",
          icon: "download",
          hint: "保留旧版可回传重导入系统的数据标准铺底 Excel 格式",
          onClick: () => {
            if (!this.activeTableKey && !(ov.tables || []).length) {
              Toast.warn("暂无可导出的已保存映射表对");
              return;
            }
            this._runExportReportTask(
              "导出三方绑定铺底表 (.xlsx)",
              () => crossApi.exportExcel(this.activeTableKey),
              { syncCatalogs: false }
            );
          },
        },
        {
          label: "导出跨环境配置包 (.json)",
          icon: "download",
          hint: "导出已保存跨环境映射表对与字段绑定的 JSON 备份包",
          onClick: () =>
            this._runExportReportTask(
              "导出跨环境配置包 (.json)",
              () => crossApi.exportJson(),
              { syncCatalogs: false }
            ),
        },
      ],
    });
  }

  _evaluateProvisionalTargetCompliance(colObj, tgtStdBrief) {
    if (!colObj || !tgtStdBrief || tgtStdBrief._pendingType) {
      return { target_compliance: null, target_needs_migration: false };
    }
    const nameMatch =
      !tgtStdBrief.en_abbr ||
      String(colObj.column_name || "").toLowerCase() === String(tgtStdBrief.en_abbr || "").toLowerCase();
    const typeMatch =
      !tgtStdBrief.resolved_type ||
      String(colObj.data_type || "").trim().toUpperCase() ===
        String(tgtStdBrief.resolved_type || "").trim().toUpperCase();
    const commMatch =
      !tgtStdBrief.cn_name ||
      String(colObj.column_comment || "").trim() === String(tgtStdBrief.cn_name || "").trim();
    const compliant = nameMatch && typeMatch && commMatch;
    return {
      target_compliance: { status: compliant ? "COMPLIANT" : "NON_COMPLIANT" },
      target_needs_migration: !compliant,
    };
  }

  _takeCandidate(side, columnName) {
    const list = side === "source" ? this.sourceCandidates : this.targetCandidates;
    const idx = list.findIndex(
      (c) => String(c.column_name || "").toLowerCase() === String(columnName || "").toLowerCase()
    );
    if (idx === -1) return null;
    const [picked] = list.splice(idx, 1);
    return picked;
  }

  _returnToCandidates(side, colObj) {
    if (!colObj) return;
    const list = side === "source" ? this.sourceCandidates : this.targetCandidates;
    const exists = list.some(
      (c) => String(c.column_name || "").toLowerCase() === String(colObj.column_name || "").toLowerCase()
    );
    if (!exists) {
      list.push(colObj);
      this._sortCandidates(list);
    }
  }

  _bindFieldToRow(side, colObj, rowIdx, { removeFromRowIdx = -1, stdItem = null } = {}) {
    const row = this.centerRows[rowIdx];
    if (!row || !colObj) return;

    const oppSide = side === "source" ? "target" : "source";
    const sameSideBrief = row[`${side}_std`];
    const oppSideBrief = row[`${oppSide}_std`];
    const targetCode =
      stdItem?.std_code ||
      (side === "source"
        ? row.source_std_code || row.target_std_code || ""
        : row.target_std_code || row.source_std_code || "");
    if (!targetCode) return;

    const displaced = row[`${side}_col`];
    if (removeFromRowIdx !== -1 && removeFromRowIdx !== rowIdx) {
      const prevRow = this.centerRows[removeFromRowIdx];
      if (prevRow) {
        prevRow[`${side}_col`] = null;
        prevRow[`${side}_std_code`] = "";
        prevRow[`${side}_std`] = null;
        if (side === "target") {
          prevRow.target_compliance = null;
          prevRow.target_needs_migration = false;
        }
        if (!prevRow.source_col && !prevRow.target_col) {
          this.centerRows.splice(removeFromRowIdx, 1);
        } else {
          this._recomputeRowState(prevRow);
        }
      }
    }

    const effBrief = stdItem
      ? this._toStdBrief(stdItem, side)
      : sameSideBrief
      ? this._toStdBrief(sameSideBrief, side, { fromSide: side })
      : oppSideBrief
      ? this._toStdBrief(oppSideBrief, side, { fromSide: oppSide })
      : null;
    row[`${side}_col`] = colObj;
    row[`${side}_std_code`] = targetCode;
    row[`${side}_std`] = effBrief;
    if (side === "target") {
      const ev = this._evaluateProvisionalTargetCompliance(colObj, effBrief);
      row.target_compliance = ev.target_compliance;
      row.target_needs_migration = ev.target_needs_migration;
    }
    this._recomputeRowState(row);

    if (displaced) {
      this._returnToCandidates(side, displaced);
    }
    this.selectedCandidate = null;
    this._markDirty();
    if (effBrief?._pendingType) {
      this._resolvePendingStdTypesAsync([targetCode]);
    }
  }

  _unbindBoundField(side, rowIdx) {
    const row = this.centerRows[rowIdx];
    if (!row || !row[`${side}_col`]) return;
    const colObj = row[`${side}_col`];
    row[`${side}_col`] = null;
    row[`${side}_std_code`] = "";
    row[`${side}_std`] = null;
    if (side === "target") {
      row.target_compliance = null;
      row.target_needs_migration = false;
    }
    this._returnToCandidates(side, colObj);
    if (!row.source_col && !row.target_col) {
      this.centerRows.splice(rowIdx, 1);
    } else {
      this._recomputeRowState(row);
    }
    this._markDirty();
  }

  _assignCandidateToRow(side, columnName, rowIdx) {
    const colObj = this._takeCandidate(side, columnName);
    if (!colObj) return;
    this._bindFieldToRow(side, colObj, rowIdx);
  }

  _handleDropOnCenterTarget(targetRowIdx, fixedSide = null) {
    const payload = this.dragPayload;
    this.dragPayload = null;
    if (!payload) return;
    const side = fixedSide || payload.side;
    if (payload.side !== side) return;
    if (targetRowIdx < 0 || targetRowIdx >= this.centerRows.length) return;

    if (payload.kind === "candidate") {
      this._assignCandidateToRow(side, payload.colName, targetRowIdx);
      return;
    }
    if (payload.kind === "bound" && typeof payload.rowIdx === "number" && payload.rowIdx !== targetRowIdx) {
      const fromRow = this.centerRows[payload.rowIdx];
      const colObj = fromRow?.[`${side}_col`];
      if (!colObj) return;
      this._bindFieldToRow(side, colObj, targetRowIdx, { removeFromRowIdx: payload.rowIdx });
    }
  }

  _handleDropOnCandidatePool(targetSide) {
    const payload = this.dragPayload;
    this.dragPayload = null;
    if (!payload || payload.kind !== "bound" || payload.side !== targetSide) return;
    if (typeof payload.rowIdx === "number") {
      this._unbindBoundField(targetSide, payload.rowIdx);
    }
  }

  _ensureRowVisibleInMatrix(targetRow) {
    if (!targetRow) return;
    this._recomputeRowState(targetRow);
    if (this._matchesMatrixRow(targetRow)) return;
    this.matrixFilter = "ALL";
    if (!this._matchesMatrixRow(targetRow)) {
      this.matrixKeyword = "";
      this.matrixSearchInput.value = "";
    }
    Toast.info("已绑定标准，已切换至「全部」以显示新绑定行");
  }

  _commitNewStandardBinding(side, colObj, picked, { removeFromRowIdx = -1 } = {}) {
    if (!picked || !colObj) return;
    this._cacheStdItem(picked);
    const stdCode = picked.std_code;
    const brief = this._toStdBrief(picked, side);
    const oppSide = side === "source" ? "target" : "source";

    // 若已存在同标准编号且当前侧为空槽的半边行，自动合并对齐成 UNIFIED
    const existingRow = this.centerRows.find(
      (r, idx) => idx !== removeFromRowIdx && !r[`${side}_col`] && r[`${oppSide}_std_code`] === stdCode
    );
    if (existingRow) {
      const existingIdx = this.centerRows.indexOf(existingRow);
      this._bindFieldToRow(side, colObj, existingIdx, { removeFromRowIdx, stdItem: picked });
      if (!this._matchesMatrixRow(existingRow)) {
        this._ensureRowVisibleInMatrix(existingRow);
        this.render();
      }
      return;
    }

    if (removeFromRowIdx !== -1) {
      const prevRow = this.centerRows[removeFromRowIdx];
      if (prevRow) {
        prevRow[`${side}_col`] = null;
        prevRow[`${side}_std_code`] = "";
        prevRow[`${side}_std`] = null;
        if (side === "target") {
          prevRow.target_compliance = null;
          prevRow.target_needs_migration = false;
        }
        if (!prevRow.source_col && !prevRow.target_col) {
          this.centerRows.splice(removeFromRowIdx, 1);
        } else {
          this._recomputeRowState(prevRow);
        }
      }
    }

    const newRow = {
      row_id: `${side}_${colObj.column_name}_${Date.now()}`,
      source_col: side === "source" ? colObj : null,
      target_col: side === "target" ? colObj : null,
      source_std_code: side === "source" ? stdCode : "",
      target_std_code: side === "target" ? stdCode : "",
      source_std: side === "source" ? brief : null,
      target_std: side === "target" ? brief : null,
      target_compliance: null,
      target_needs_migration: false,
    };
    if (side === "target") {
      const ev = this._evaluateProvisionalTargetCompliance(colObj, brief);
      newRow.target_compliance = ev.target_compliance;
      newRow.target_needs_migration = ev.target_needs_migration;
    }
    this._recomputeRowState(newRow);
    this.centerRows.push(newRow);
    this.selectedCandidate = null;
    this._ensureRowVisibleInMatrix(newRow);
    this._markDirty();
  }

  _openPickerForCandidate(side, columnName) {
    const list = side === "source" ? this.sourceCandidates : this.targetCandidates;
    const colObj = list.find(
      (c) => String(c.column_name || "").toLowerCase() === String(columnName || "").toLowerCase()
    );
    if (!colObj) return;
    const ov = this.overview || {};
    const dbType =
      side === "source"
        ? ov.source_connection?.db_type || "DWS"
        : ov.target_connection?.db_type || "DWS";
    const fmt = side === "source" ? this.sourceFormatProfile : this.targetFormatProfile;

    openStandardPickerModal({
      title: `绑定${side === "source" ? "来源" : "目标"}字段标准 · ${colObj.column_name}`,
      initialQuery: colObj.column_comment || colObj.column_name,
      currentStdCode: "",
      dbType,
      formatProfile: fmt,
      onSelect: (picked) => {
        if (!picked) return;
        const taken = this._takeCandidate(side, colObj.column_name);
        if (!taken) return;
        this._commitNewStandardBinding(side, taken, picked);
      },
    });
  }

  _openPickerForBoundFieldNewRow(side, fromRowIdx) {
    const fromRow = this.centerRows[fromRowIdx];
    const colObj = fromRow?.[`${side}_col`];
    if (!colObj) return;
    const ov = this.overview || {};
    const dbType =
      side === "source"
        ? ov.source_connection?.db_type || "DWS"
        : ov.target_connection?.db_type || "DWS";
    const fmt = side === "source" ? this.sourceFormatProfile : this.targetFormatProfile;
    const curCode = fromRow[`${side}_std_code`] || "";

    openStandardPickerModal({
      title: `另绑${side === "source" ? "来源" : "目标"}字段新标准 · ${colObj.column_name}`,
      initialQuery: colObj.column_comment || colObj.column_name || curCode,
      currentStdCode: curCode,
      dbType,
      formatProfile: fmt,
      onSelect: (picked) => {
        if (!picked) return;
        const liveIdx = this.centerRows.indexOf(fromRow);
        if (liveIdx === -1 || !fromRow[`${side}_col`]) return;
        this._commitNewStandardBinding(side, fromRow[`${side}_col`], picked, {
          removeFromRowIdx: liveIdx,
        });
      },
    });
  }

  _openBookmarkPicker(rowIdx, side) {
    const row = this.centerRows[rowIdx];
    if (!row) return;
    const ov = this.overview || {};
    const isBoth = side === "both";
    const colObj = isBoth ? row.target_col || row.source_col : row[`${side}_col`];
    if (!colObj) return;

    const currentCode = isBoth
      ? row.target_std_code || row.source_std_code
      : row[`${side}_std_code`] || "";
    const dbType =
      side === "source"
        ? ov.source_connection?.db_type || "DWS"
        : ov.target_connection?.db_type || "DWS";
    const fmt = side === "source" ? this.sourceFormatProfile : this.targetFormatProfile;

    openStandardPickerModal({
      title: isBoth
        ? `调整数据标准 · ${row.source_col?.column_name} ↔ ${row.target_col?.column_name}`
        : `调整${side === "source" ? "来源" : "目标"}字段标准 · ${colObj.column_name}`,
      initialQuery: currentCode || colObj.column_comment || colObj.column_name,
      currentStdCode: currentCode,
      dbType,
      formatProfile: fmt,
      onSelect: (picked) => {
        if (!picked) {
          if (isBoth) {
            if (row.source_col) this._returnToCandidates("source", row.source_col);
            if (row.target_col) this._returnToCandidates("target", row.target_col);
            this.centerRows.splice(rowIdx, 1);
          } else {
            this._unbindBoundField(side, rowIdx);
            return;
          }
        } else {
          const code = picked.std_code;
          this._cacheStdItem(picked);
          if (isBoth || side === "source") {
            row.source_std_code = code;
            row.source_std = this._toStdBrief(picked, "source");
          }
          if (isBoth || side === "target") {
            row.target_std_code = code;
            row.target_std = this._toStdBrief(picked, "target");
            const ev = this._evaluateProvisionalTargetCompliance(row.target_col, row.target_std);
            row.target_compliance = ev.target_compliance;
            row.target_needs_migration = ev.target_needs_migration;
          }
          this._recomputeRowState(row);
        }
        this._markDirty();
      },
    });
  }

  _unifyDiffRow(rowIdx, keepSide) {
    const row = this.centerRows[rowIdx];
    if (!row) return;
    const pendingCodes = [];
    if (keepSide === "source" && row.source_std_code) {
      row.target_std_code = row.source_std_code;
      row.target_std = row.source_std
        ? this._toStdBrief(row.source_std, "target", { fromSide: "source" })
        : null;
      if (row.target_std?._pendingType) pendingCodes.push(row.target_std_code);
      const ev = this._evaluateProvisionalTargetCompliance(row.target_col, row.target_std);
      row.target_compliance = ev.target_compliance;
      row.target_needs_migration = ev.target_needs_migration;
    } else if (keepSide === "target" && row.target_std_code) {
      row.source_std_code = row.target_std_code;
      row.source_std = row.target_std
        ? this._toStdBrief(row.target_std, "source", { fromSide: "target" })
        : null;
      if (row.source_std?._pendingType) pendingCodes.push(row.source_std_code);
    }
    this._recomputeRowState(row);
    this._markDirty();
    if (pendingCodes.length) {
      this._resolvePendingStdTypesAsync(pendingCodes);
    }
  }

  _splitRow(rowIdx) {
    const row = this.centerRows[rowIdx];
    if (!row || !row.source_col || !row.target_col) return;
    const srcOnly = this._recomputeRowState({
      row_id: `${row.row_id}_s`,
      source_col: row.source_col,
      target_col: null,
      source_std_code: row.source_std_code,
      target_std_code: "",
      source_std: row.source_std,
      target_std: null,
      target_compliance: null,
      target_needs_migration: false,
    });
    const tgtOnly = this._recomputeRowState({
      row_id: `${row.row_id}_t`,
      source_col: null,
      target_col: row.target_col,
      source_std_code: "",
      target_std_code: row.target_std_code,
      source_std: null,
      target_std: row.target_std,
      target_compliance: row.target_compliance,
      target_needs_migration: row.target_needs_migration,
    });
    this.centerRows.splice(rowIdx, 1, srcOnly, tgtOnly);
    this._markDirty();
  }

  smartPairRows() {
    let changed = false;
    // 1. 合并同标准编号的单侧行
    const srcSingles = [];
    const tgtSingles = [];
    const keptRows = [];

    for (const r of this.centerRows) {
      if (r.source_col && !r.target_col && r.source_std_code) {
        srcSingles.push(r);
      } else if (r.target_col && !r.source_col && r.target_std_code) {
        tgtSingles.push(r);
      } else {
        keptRows.push(r);
      }
    }

    const usedTgtIdx = new Set();
    for (const sr of srcSingles) {
      const mIdx = tgtSingles.findIndex(
        (tr, i) => !usedTgtIdx.has(i) && tr.target_std_code === sr.source_std_code
      );
      if (mIdx !== -1) {
        usedTgtIdx.add(mIdx);
        const tr = tgtSingles[mIdx];
        keptRows.push(
          this._recomputeRowState({
            row_id: `pair_${sr.source_col.column_name}_${tr.target_col.column_name}`,
            source_col: sr.source_col,
            target_col: tr.target_col,
            source_std_code: sr.source_std_code,
            target_std_code: tr.target_std_code,
            source_std: sr.source_std,
            target_std: tr.target_std,
            target_compliance: tr.target_compliance,
            target_needs_migration: tr.target_needs_migration,
          })
        );
        changed = true;
      } else {
        keptRows.push(sr);
      }
    }
    tgtSingles.forEach((tr, i) => {
      if (!usedTgtIdx.has(i)) keptRows.push(tr);
    });

    // 2. 对仍缺对侧字段的半边标准行，尝试从对侧待选池按同名自动绑标配对
    const pendingCodes = [];
    for (const r of keptRows) {
      if (r.source_col && !r.target_col && r.source_std_code) {
        const sLower = String(r.source_col.column_name || "").toLowerCase();
        const matchCand = this.targetCandidates.find(
          (tc) => String(tc.column_name || "").toLowerCase() === sLower
        );
        if (matchCand) {
          this._takeCandidate("target", matchCand.column_name);
          r.target_col = matchCand;
          r.target_std_code = r.source_std_code;
          r.target_std = r.source_std
            ? this._toStdBrief(r.source_std, "target", { fromSide: "source" })
            : null;
          if (r.target_std?._pendingType) pendingCodes.push(r.target_std_code);
          const ev = this._evaluateProvisionalTargetCompliance(matchCand, r.target_std);
          r.target_compliance = ev.target_compliance;
          r.target_needs_migration = ev.target_needs_migration;
          this._recomputeRowState(r);
          changed = true;
        }
      } else if (r.target_col && !r.source_col && r.target_std_code) {
        const tLower = String(r.target_col.column_name || "").toLowerCase();
        const matchCand = this.sourceCandidates.find(
          (sc) => String(sc.column_name || "").toLowerCase() === tLower
        );
        if (matchCand) {
          this._takeCandidate("source", matchCand.column_name);
          r.source_col = matchCand;
          r.source_std_code = r.target_std_code;
          r.source_std = r.target_std
            ? this._toStdBrief(r.target_std, "source", { fromSide: "target" })
            : null;
          if (r.source_std?._pendingType) pendingCodes.push(r.source_std_code);
          this._recomputeRowState(r);
          changed = true;
        }
      }
    }

    this.centerRows = keptRows;
    if (changed) {
      Toast.info("已完成智能配对，点击「保存」持久化");
      this._markDirty();
      if (pendingCodes.length) {
        this._resolvePendingStdTypesAsync(pendingCodes);
      }
    } else {
      Toast.info("当前已绑定字段无可自动合并或同名补齐项");
      this.render();
    }
  }

  openPairingUploadWizard() {
    openUploadWizardModal({
      title: "批量铺底跨环境映射关系",
      description:
        "先下载系统映射模板（支持按当前已选来源/目标表直接导出预填模板），批量铺底后可直接在五虚拟列工作台微调。",
      templateColumns: [
        { name: "来源模式名" },
        { name: "来源表名", required: true },
        { name: "来源字段名", required: true },
        { name: "来源数据项编号" },
        { name: "目标模式名" },
        { name: "目标表名", required: true },
        { name: "目标字段名", required: true },
        { name: "目标数据项编号" },
      ],
      onDownloadBlankTemplate: () => crossApi.downloadPairingTemplate({}),
      prefillButtonLabel:
        this.draftTableBind.source_table || this.draftTableBind.target_table
          ? "导出当前两表预填映射模板 (.xlsx)"
          : "",
      onDownloadPrefilledTemplate:
        this.draftTableBind.source_table || this.draftTableBind.target_table
          ? () =>
              crossApi.downloadPairingTemplate({
                tableKey: this.activeTableKey,
                sourceSchema: this.draftTableBind.source_schema,
                sourceTable: this.draftTableBind.source_table,
                targetSchema: this.draftTableBind.target_schema,
                targetTable: this.draftTableBind.target_table,
              })
          : null,
      accept: ".xlsx,.xlsm",
      onUpload: async (file, _mode, uploadCtx) => {
        const res = await crossApi.uploadPairings(file, this.targetFormatProfile || "AUTO", {
          ...uploadCtx,
          sourceFormatProfile: this.sourceFormatProfile,
          targetFormatProfile: this.targetFormatProfile,
        });
        this.overview = res;
        this._syncSideFormatsFromOverview(res);
        this.activeTableKey = res.active_table_key || "";
        this._hydrateDraftFromComparison(res.active_comparison);
        Toast.success(res.message || "跨环境映射铺底成功");
        this.render();
        this.onStateUpdate?.();
      },
    });
  }

  async pushToMigrator() {
    if (this.dirty || (!this.activeTableKey && this.centerRows.length > 0)) {
      const saved = await this.saveCurrentPairings(false);
      if (!saved) return false;
    }
    if (!this.activeTableKey) {
      Toast.warn("请先选择并保存包含已绑标准字段的映射表对后再推送贯标");
      return false;
    }
    const ok = await Modal.confirm({
      title: "推送至第 2 步贯标执行",
      message: "将当前目标库已绑标的待改造规则推送至「2. 落标绑定与贯标 → 贯标改造执行队列」，是否继续？",
      confirmText: "推送并切换",
    });
    if (!ok) return false;
    try {
      const res = await this.activityBar.track(
        "推送贯标规则",
        async ({ taskId, signal }) =>
          crossApi.pushToMigrator(
            this.activeTableKey,
            this.targetFormatProfile || this.formatProfile || "AUTO",
            {
              sourceFormatProfile: this.sourceFormatProfile,
              targetFormatProfile: this.targetFormatProfile,
            },
            { signal, taskId }
          ),
        { prefix: "cmp", cancellable: false }
      );
      Toast.success(`已推送 ${res.pushed_count ?? 0} 条规则`);
      this.onPushToMigrator?.();
      return true;
    } catch (err) {
      if (isAbortError(err)) return false;
      Toast.error(err.message);
      return false;
    }
  }

  renderHeaderActions() {
    return el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "智能配对",
        onClick: () => this.smartPairRows(),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: this.dirty ? "保存 *" : "保存",
        onClick: () => this.saveCurrentPairings(true),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm mac-btn-primary",
        text: "推送贯标 →",
        onClick: () => this.pushToMigrator(),
      })
    );
  }

  async bindRoleConnection(role, connectionId) {
    const sideLabel = role === "source" ? "来源库" : "目标库";
    try {
      const hasUnsavedDraft =
        !this.activeTableKey &&
        Boolean(this.draftTableBind.source_table || this.draftTableBind.target_table);
      const res = await this.activityBar.track(
        `绑定${sideLabel}连接`,
        async ({ taskId, signal }) =>
          crossApi.bindRole(
            {
              role,
              connection_id: connectionId,
              active_table_key: this.activeTableKey,
              auto_refresh: true,
            },
            { signal, taskId }
          ),
        { prefix: "cmp" }
      );
      this.loadedOnce = true;
      this.overview = res;
      if (role === "source") {
        this.sourceFormatProfile =
          res.source_connection?.standard_format || res.source_format_profile || "AUTO";
      } else {
        this.targetFormatProfile =
          res.target_connection?.standard_format || res.target_format_profile || "AUTO";
      }
      if (!hasUnsavedDraft && res.active_table_key !== undefined) {
        this.activeTableKey = res.active_table_key || "";
      }
      if (
        hasUnsavedDraft ||
        res.source_format_profile !== this.sourceFormatProfile ||
        res.target_format_profile !== this.targetFormatProfile
      ) {
        await this._reloadCurrentComparison(
          {
            sourceFormatProfile: this.sourceFormatProfile,
            targetFormatProfile: this.targetFormatProfile,
          },
          { pairOnly: true }
        );
        this._maybePrefetchPairs();
        return true;
      }
      this._hydrateDraftFromComparison(res.active_comparison);
      this.render();
      this.onStateUpdate?.();
      this._maybePrefetchPairs();
      return true;
    } catch (err) {
      if (isAbortError(err)) return false;
      Toast.error(err.message);
      return false;
    }
  }

  _createSideEndpointControls(role) {
    const isSrc = role === "source";
    const schKey = `${role}_schema`;
    const tblKey = `${role}_table`;

    const ctrl = {
      role,
      pickerOpen: false,
      pickerQuery: "",
      pickerHighlightIdx: -1,
      filteredTables: [],
      _lastConnSig: null,
      _lastFmtSig: null,
    };

    ctrl.connSelect = el("select", {
      className: "mac-input cross-ep-select",
      "aria-label": isSrc ? "选择来源库" : "选择目标库",
      onChange: (e) => {
        this._closeAllTablePickers();
        this.bindRoleConnection(role, e.target.value);
      },
    });
    const connSelectWrap = el(
      "span",
      { className: "cross-popup-select-wrap is-conn" },
      ctrl.connSelect,
      el("span", { className: "cross-popup-chev", "aria-hidden": "true" }, svgIcon("chevronsUpDown", 11))
    );

    ctrl.fmtSelect = el("select", {
      className: "mac-input cross-ep-fmt-select",
      "aria-label": isSrc ? "来源库落标标准" : "目标库落标标准",
      title: isSrc ? "来源库沿用的落标标准格式" : "目标库沿用的落标标准格式",
      onChange: (e) => {
        if (isSrc) this.sourceFormatProfile = e.target.value;
        else this.targetFormatProfile = e.target.value;
        this._reloadCurrentComparison(
          {
            sourceFormatProfile: this.sourceFormatProfile,
            targetFormatProfile: this.targetFormatProfile,
          },
          { pairOnly: true }
        );
      },
    });
    const fmtSelectWrap = el(
      "span",
      { className: "cross-popup-select-wrap is-fmt" },
      ctrl.fmtSelect,
      el("span", { className: "cross-popup-chev", "aria-hidden": "true" }, svgIcon("chevronsUpDown", 11))
    );

    const listId = `cross-tbl-listbox-${role}`;
    ctrl.schemaInput = el("input", {
      className: "cross-ep-schema-input",
      "aria-label": isSrc ? "来源 Schema" : "目标 Schema",
      placeholder: "Schema",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      onInput: (e) => {
        this.draftTableBind[schKey] = e.target.value.trim();
        if (ctrl.pickerOpen) this._renderTablePickerList(role);
      },
      onKeydown: (e) => {
        if (this._isImeComposing(e)) return;
        if (e.key === "Enter") {
          e.preventDefault?.();
          this._closeAllTablePickers();
          this.previewSelectedTables(false);
        }
      },
    });

    ctrl.tableInput = el("input", {
      className: "cross-ep-tbl-input",
      "aria-label": isSrc ? "来源表或视图名" : "目标表名",
      placeholder: isSrc ? "来源表 / 视图名" : "目标表名",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      onInput: (e) => {
        this.draftTableBind[tblKey] = e.target.value.trim();
        if (ctrl.pickerOpen) this._renderTablePickerList(role);
      },
      onKeydown: (e) => {
        if (this._isImeComposing(e)) return;
        if (e.key === "Enter") {
          e.preventDefault?.();
          this._closeAllTablePickers();
          this.previewSelectedTables(false);
        }
      },
    });

    ctrl.quickBtnLabel = el("span", { className: "cross-ep-quickpick-label", text: "选表" });
    ctrl.quickBtn = el(
      "button",
      {
        type: "button",
        className: "cross-ep-quickpick is-empty",
        "aria-haspopup": "listbox",
        "aria-expanded": "false",
        "aria-controls": listId,
        "aria-label": isSrc ? "选择来源库表" : "选择目标库表",
        onClick: (e) => {
          e.stopPropagation?.();
          this._toggleTablePicker(role);
        },
      },
      ctrl.quickBtnLabel,
      svgIcon("chevronDown", 10)
    );

    ctrl.pickerSearchInput = el("input", {
      className: "mac-input input-xs cross-table-search-input",
      type: "search",
      role: "combobox",
      "aria-autocomplete": "list",
      "aria-expanded": "true",
      "aria-controls": listId,
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      "aria-label": isSrc ? "搜索来源库缓存表" : "搜索目标库缓存表",
      placeholder: "搜索 Schema / 表名 / 视图 / 注释...",
      onInput: (e) => this._handleTablePickerSearch(role, e.target.value),
      onKeydown: (e) => this._handleTablePickerKeydown(role, e),
    });
    ctrl.pickerMetaEl = el("span", { className: "cross-table-popover-meta", text: "0" });
    ctrl.pickerRefreshBtn = el(
      "button",
      {
        type: "button",
        className: "cross-table-refresh-btn",
        title: isSrc ? "重新采集来源库表与视图目录" : "重新采集目标库表与视图目录",
        "aria-label": isSrc ? "刷新来源库表目录" : "刷新目标库表目录",
        onClick: (e) => {
          e.stopPropagation?.();
          this._refreshSideCatalogFromPicker(role);
        },
      },
      svgIcon("refresh", 11)
    );
    ctrl.pickerListEl = el("div", {
      id: listId,
      className: "cross-table-popover-list",
      role: "listbox",
      "aria-label": isSrc ? "来源库缓存表列表" : "目标库缓存表列表",
    });
    ctrl.pickerFootEl = el("div", { className: "cross-table-popover-foot", hidden: true });
    ctrl.popoverEl = el(
      "div",
      { className: "cross-table-popover", hidden: true },
      el(
        "div",
        { className: "cross-table-popover-head" },
        el(
          "div",
          { className: "cross-table-search-wrap" },
          svgIcon("search", 12),
          ctrl.pickerSearchInput
        ),
        ctrl.pickerMetaEl,
        ctrl.pickerRefreshBtn
      ),
      ctrl.pickerListEl,
      ctrl.pickerFootEl
    );

    ctrl.lineageTextEl = el("span", { className: "mono cross-ep-lineage-text" });
    ctrl.lineageEl = el(
      "div",
      { className: "cross-ep-lineage", hidden: true },
      svgIcon("link", 10),
      el("span", { className: "cross-ep-lineage-label", text: "视图穿透:" }),
      ctrl.lineageTextEl
    );

    ctrl.bodyEl = el(
      "div",
      { className: "cross-ep-body" },
      el(
        "div",
        { className: "cross-ep-pathbar" },
        ctrl.schemaInput,
        el("span", { className: "cross-ep-path-sep", text: "." }),
        ctrl.tableInput,
        ctrl.quickBtn,
        el("button", {
          type: "button",
          className: "cross-ep-load-btn",
          text: "读取",
          onClick: () => {
            this._closeAllTablePickers();
            this.previewSelectedTables(true);
          },
        })
      ),
      ctrl.popoverEl
    );

    ctrl.cardEl = el(
      "div",
      { className: `cross-endpoint-card is-${role}` },
      el(
        "div",
        { className: "cross-ep-head" },
        el(
          "span",
          { className: `cross-role-pill is-${role}` },
          el("span", { className: `dot ${isSrc ? "dot-info" : "dot-success"}` }),
          el("span", { text: isSrc ? "来源库" : "目标库" })
        ),
        connSelectWrap,
        el(
          "span",
          { className: "cross-ep-fmt-wrap" },
          el("span", { className: "cross-ep-fmt-label", text: "标准格式" }),
          fmtSelectWrap
        )
      ),
      ctrl.bodyEl,
      ctrl.lineageEl
    );

    return ctrl;
  }

  _syncEndpointInputsFromDraft({ force = false } = {}) {
    if (!this.epControls) return;
    const pairs = [
      [this.epControls.source.schemaInput, this.draftTableBind.source_schema || ""],
      [this.epControls.source.tableInput, this.draftTableBind.source_table || ""],
      [this.epControls.target.schemaInput, this.draftTableBind.target_schema || ""],
      [this.epControls.target.tableInput, this.draftTableBind.target_table || ""],
    ];
    for (const [inp, nextVal] of pairs) {
      if (!inp) continue;
      if (!force && globalThis.document?.activeElement === inp) continue;
      if (inp.value !== nextVal) inp.value = nextVal;
    }
  }

  _isAnyTablePickerOpen() {
    return Boolean(this.epControls?.source?.pickerOpen || this.epControls?.target?.pickerOpen);
  }

  _closeAllTablePickers({ restoreFocus = false } = {}) {
    if (!this.epControls) return;
    for (const role of ["source", "target"]) {
      if (this.epControls[role]?.pickerOpen) {
        this._closeTablePicker(role, { restoreFocus });
      }
    }
  }

  _toggleTablePicker(role) {
    const ctrl = this.epControls?.[role];
    if (!ctrl) return;
    if (ctrl.pickerOpen) {
      this._closeTablePicker(role, { restoreFocus: true });
    } else {
      this._openTablePicker(role);
    }
  }

  _getClipBoundsRect() {
    const host = this.root?.closest?.(".win-body") || this.root;
    if (host?.getBoundingClientRect) {
      return host.getBoundingClientRect();
    }
    const vh = Number(globalThis.window?.innerHeight || 0);
    const vw = Number(globalThis.window?.innerWidth || 0);
    return vh > 0 ? { top: 0, bottom: vh, left: 0, right: vw } : null;
  }

  _positionTablePicker(role) {
    const ctrl = this.epControls?.[role];
    if (!ctrl?.bodyEl?.getBoundingClientRect) return;
    const clipRect = this._getClipBoundsRect();
    if (!clipRect) return;
    const anchorRect = ctrl.bodyEl.getBoundingClientRect();
    const spaceBelow = clipRect.bottom - anchorRect.bottom - 10;
    const spaceAbove = anchorRect.top - clipRect.top - 10;
    const shouldFlipUp = spaceBelow < 210 && spaceAbove > spaceBelow;
    ctrl.popoverEl.classList.toggle("is-flip-up", shouldFlipUp);
    const avail = shouldFlipUp ? spaceAbove : spaceBelow;
    const maxH = Math.max(128, Math.min(332, Math.floor(avail)));
    ctrl.popoverEl.style.maxHeight = `${maxH}px`;
  }

  _openTablePicker(role) {
    const ctrl = this.epControls?.[role];
    if (!ctrl) return;
    const opp = role === "source" ? "target" : "source";
    if (this.epControls?.[opp]?.pickerOpen) {
      this._closeTablePicker(opp);
    }
    if (this.pairPickerOpen) {
      this._closePairPicker();
    }
    ctrl.pickerOpen = true;
    ctrl.pickerQuery = "";
    ctrl.pickerSearchInput.value = "";
    const curSch = String(this.draftTableBind[`${role}_schema`] || "").trim().toLowerCase();
    const curTbl = String(this.draftTableBind[`${role}_table`] || "").trim().toLowerCase();
    const initialList = this._getFilteredSideTables(role).slice(0, 150);
    ctrl.pickerHighlightIdx = curTbl
      ? initialList.findIndex((t) => {
          const tSch = String(t.schema_name || "").trim().toLowerCase();
          const tName = String(t.table_name || "").trim().toLowerCase();
          return tName === curTbl && (!curSch || !tSch || tSch === curSch);
        })
      : -1;
    ctrl.cardEl.classList.add("is-picker-open");
    ctrl.quickBtn.classList.add("is-open");
    ctrl.quickBtn.setAttribute("aria-expanded", "true");
    this._renderTablePickerList(role, { scrollHighlight: ctrl.pickerHighlightIdx >= 0 });
    ctrl.popoverEl.hidden = false;
    this._positionTablePicker(role);
    ctrl.pickerSearchInput.focus();
  }

  _closeTablePicker(role, { restoreFocus = false } = {}) {
    const ctrl = this.epControls?.[role];
    if (!ctrl || !ctrl.pickerOpen) return;
    ctrl.pickerOpen = false;
    ctrl.pickerQuery = "";
    ctrl.pickerHighlightIdx = -1;
    ctrl.cardEl.classList.remove("is-picker-open");
    ctrl.quickBtn.classList.remove("is-open");
    ctrl.quickBtn.setAttribute("aria-expanded", "false");
    ctrl.popoverEl.classList.remove("is-flip-up");
    ctrl.popoverEl.style.maxHeight = "";
    ctrl.popoverEl.hidden = true;
    if (restoreFocus) {
      ctrl.quickBtn.focus();
    }
  }

  _handleTablePickerSearch(role, val) {
    const ctrl = this.epControls?.[role];
    if (!ctrl) return;
    ctrl.pickerQuery = val;
    ctrl.pickerHighlightIdx = 0;
    this._renderTablePickerList(role);
  }

  _handleTablePickerKeydown(role, e) {
    if (this._isImeComposing(e)) return;
    const ctrl = this.epControls?.[role];
    if (!ctrl) return;
    if (e.key === "Escape") {
      e.preventDefault?.();
      e.stopPropagation?.();
      this._closeTablePicker(role, { restoreFocus: true });
      return;
    }
    const items = ctrl.filteredTables || [];
    if (e.key === "ArrowDown") {
      e.preventDefault?.();
      if (!items.length) return;
      ctrl.pickerHighlightIdx = (ctrl.pickerHighlightIdx + 1) % items.length;
      this._renderTablePickerList(role, { scrollHighlight: true });
    } else if (e.key === "ArrowUp") {
      e.preventDefault?.();
      if (!items.length) return;
      ctrl.pickerHighlightIdx = ctrl.pickerHighlightIdx <= 0 ? items.length - 1 : ctrl.pickerHighlightIdx - 1;
      this._renderTablePickerList(role, { scrollHighlight: true });
    } else if (e.key === "Enter") {
      if (items.length > 0) {
        e.preventDefault?.();
        const idx = ctrl.pickerHighlightIdx >= 0 && ctrl.pickerHighlightIdx < items.length ? ctrl.pickerHighlightIdx : 0;
        this._selectTableFromPicker(role, items[idx]);
      }
    }
  }

  _getSideTables(role) {
    const ov = this.overview || {};
    const sideConn = role === "source" ? ov.source_connection : ov.target_connection;
    if (Array.isArray(sideConn?.tables) && sideConn.tables.length) {
      return sideConn.tables;
    }
    const cid = sideConn?.connection_id;
    if (cid && store.state.catalogTrees?.[cid]?.tables?.length) {
      return store.state.catalogTrees[cid].tables;
    }
    return sideConn?.tables || [];
  }

  _getFilteredSideTables(role) {
    const ctrl = this.epControls?.[role];
    const tablesList = this._getSideTables(role);
    const kw = String(ctrl?.pickerQuery || "").trim().toLowerCase();
    if (!kw) return tablesList;
    return tablesList.filter((t) => {
      const sch = String(t.schema_name || "").toLowerCase();
      const tbl = String(t.table_name || "").toLowerCase();
      const fq = sch ? `${sch}.${tbl}` : tbl;
      const comm = String(t.table_comment || "").toLowerCase();
      const objType = t.object_type === "VIEW" ? "view 视图" : "table 表";
      return fq.includes(kw) || tbl.includes(kw) || sch.includes(kw) || comm.includes(kw) || objType.includes(kw);
    });
  }

  _selectTableFromPicker(role, tblItem) {
    if (!tblItem) return;
    const schKey = `${role}_schema`;
    const tblKey = `${role}_table`;
    this.draftTableBind[schKey] = String(tblItem.schema_name || "").trim();
    this.draftTableBind[tblKey] = String(tblItem.table_name || "").trim();
    this._syncEndpointInputsFromDraft({ force: true });
    this._closeTablePicker(role);
    this.previewSelectedTables(false);
  }

  async _refreshSideCatalogFromPicker(role) {
    const ctrl = this.epControls?.[role];
    if (ctrl?._refreshingCatalog) return;
    const ov = this.overview || {};
    const sideKey = role === "source" ? "source_connection" : "target_connection";
    const cid = ov[sideKey]?.connection_id;
    if (!cid) {
      Toast.warn(role === "source" ? "请先选择来源库连接" : "请先选择目标库连接");
      return;
    }
    if (ctrl) {
      ctrl._refreshingCatalog = true;
      if (ctrl.pickerRefreshBtn) ctrl.pickerRefreshBtn.disabled = true;
    }
    try {
      const res = await this.activityBar.track(
        `采集${role === "source" ? "来源库" : "目标库"}表目录`,
        async () => platformApi.refreshMetadata(cid, null),
        { prefix: "cmp" }
      );
      if (this._destroyed) return;
      store.updateCatalogTree(cid, res);
      if (this.overview?.[sideKey]) {
        this.overview[sideKey].tables = res?.tables || [];
        this.overview[sideKey].schemas = (res?.schemas || []).map((s) =>
          typeof s === "string" ? s : s.schema_name
        );
      }
      this._syncEndpointDeck();
      Toast.success(`已刷新目录：共 ${(res?.tables || []).length} 张表/视图`);
    } catch (err) {
      if (!isAbortError(err)) Toast.error(err.message);
    } finally {
      if (ctrl) {
        ctrl._refreshingCatalog = false;
        if (ctrl.pickerRefreshBtn) ctrl.pickerRefreshBtn.disabled = false;
      }
    }
  }

  _renderTablePickerList(role, { scrollHighlight = false } = {}) {
    const ctrl = this.epControls?.[role];
    if (!ctrl) return;
    const allTables = this._getSideTables(role);
    const filtered = this._getFilteredSideTables(role);
    const MAX_VISIBLE = 150;
    const capped = filtered.slice(0, MAX_VISIBLE);
    ctrl.filteredTables = capped;
    if (ctrl.pickerHighlightIdx >= capped.length) {
      ctrl.pickerHighlightIdx = capped.length ? 0 : -1;
    }

    const kw = String(ctrl.pickerQuery || "").trim();
    ctrl.pickerMetaEl.textContent = kw ? `${filtered.length}/${allTables.length}` : `${allTables.length} 项`;

    const schKey = `${role}_schema`;
    const tblKey = `${role}_table`;
    const curSch = String(this.draftTableBind[schKey] || "").trim().toLowerCase();
    const curTbl = String(this.draftTableBind[tblKey] || "").trim().toLowerCase();

    let highlightedEl = null;
    let highlightedId = "";
    if (capped.length) {
      const optNodes = capped.map((t, idx) => {
        const tSch = String(t.schema_name || "").trim();
        const tName = String(t.table_name || "").trim();
        const fq = tSch ? `${tSch}.${tName}` : tName;
        const optId = `cross-tbl-opt-${role}-${idx}`;
        const isActive =
          Boolean(curTbl) &&
          tName.toLowerCase() === curTbl &&
          (!curSch || !tSch || tSch.toLowerCase() === curSch);
        const isHl = idx === ctrl.pickerHighlightIdx;
        const isView = String(t.object_type || "").toUpperCase() === "VIEW";
        const optBtn = el(
          "button",
          {
            id: optId,
            type: "button",
            role: "option",
            "aria-selected": isActive ? "true" : "false",
            className: `cross-table-option ${isActive ? "is-active" : ""} ${isHl ? "is-highlighted" : ""}`.trim(),
            title: `${fq}${t.table_comment ? " · " + t.table_comment : ""}`,
            onClick: (e) => {
              e.stopPropagation?.();
              this._selectTableFromPicker(role, t);
            },
          },
          el(
            "div",
            { className: "cross-table-option-main" },
            el("span", { className: "mono cross-table-option-fq", text: fq }),
            t.table_comment
              ? el("span", { className: "cross-table-option-comment", text: t.table_comment })
              : null
          ),
          el(
            "span",
            { style: { display: "inline-flex", alignItems: "center", gap: "4px", flexShrink: "0" } },
            isView ? badge("视图", "info") : null,
            isActive ? badge("当前", "success") : null
          )
        );
        if (isHl) {
          highlightedEl = optBtn;
          highlightedId = optId;
        }
        return optBtn;
      });
      clearEl(ctrl.pickerListEl, optNodes);
    } else {
      const emptyBox = el(
        "div",
        { className: "cross-table-empty" },
        el("span", {
          text: allTables.length
            ? `未找到匹配「${kw}」的表或视图`
            : "当前数据源暂无已缓存表，可直接输入表名读取或采集目录",
        }),
        !allTables.length
          ? el("button", {
              type: "button",
              className: "mac-btn btn-xs",
              text: "↻ 立即采集库表目录",
              onClick: (e) => {
                e.stopPropagation?.();
                this._refreshSideCatalogFromPicker(role);
              },
            })
          : null
      );
      clearEl(ctrl.pickerListEl, emptyBox);
    }
    if (highlightedId) {
      ctrl.pickerSearchInput.setAttribute("aria-activedescendant", highlightedId);
    } else {
      ctrl.pickerSearchInput.setAttribute("aria-activedescendant", "");
    }

    if (filtered.length > MAX_VISIBLE) {
      ctrl.pickerFootEl.textContent = `仅展示前 ${MAX_VISIBLE} 项（匹配 ${filtered.length} 项），请继续输入关键字缩小范围`;
      ctrl.pickerFootEl.hidden = false;
    } else {
      ctrl.pickerFootEl.hidden = true;
    }

    if (scrollHighlight && highlightedEl) {
      highlightedEl.scrollIntoView?.({ block: "nearest" });
    }
  }

  _syncEndpointDeck() {
    if (!this.epControls) return;
    const ov = this.overview || { active_comparison: null };
    const comp = ov.active_comparison;
    const conns = ov.connections?.length ? ov.connections : store.state.connections || [];
    const connSig = conns.map((c) => `${c.connection_id}:${c.name}:${c.db_type}`).join("|");
    const fmtSig = (this.formatProfiles || []).map((p) => `${p.value}:${p.label}`).join("|");

    for (const role of ["source", "target"]) {
      const isSrc = role === "source";
      const ctrl = this.epControls[role];
      const sideConn = isSrc ? ov.source_connection : ov.target_connection;
      const curCid = String(sideConn?.connection_id || "");
      const placeholder = isSrc ? "选择来源库" : "选择目标库";

      if (ctrl._lastConnSig !== connSig) {
        ctrl._lastConnSig = connSig;
        clearEl(ctrl.connSelect, [
          el("option", { value: "", text: placeholder }),
          ...conns.map((c) => {
            const o = el("option", { value: c.connection_id, text: `${c.name} · ${c.db_type}` });
            if (c.connection_id === curCid) o.selected = true;
            return o;
          }),
        ]);
      } else {
        for (const opt of ctrl.connSelect.children || []) {
          opt.selected = opt.value === curCid;
        }
      }
      if (ctrl.connSelect.value !== curCid) {
        ctrl.connSelect.value = curCid;
      }

      const curFmt = isSrc ? this.sourceFormatProfile : this.targetFormatProfile;
      if (ctrl._lastFmtSig !== fmtSig) {
        ctrl._lastFmtSig = fmtSig;
        clearEl(
          ctrl.fmtSelect,
          (this.formatProfiles || []).map((p) => {
            const o = el("option", { value: p.value, text: p.label });
            if (p.value === curFmt) o.selected = true;
            return o;
          })
        );
      } else {
        for (const opt of ctrl.fmtSelect.children || []) {
          opt.selected = opt.value === curFmt;
        }
      }
      if (ctrl.fmtSelect.value !== curFmt) {
        ctrl.fmtSelect.value = curFmt;
      }

      const sideTables = this._getSideTables(role);
      ctrl.quickBtnLabel.textContent = sideTables.length ? `选表 (${sideTables.length})` : "选表";
      ctrl.quickBtn.classList.toggle("is-empty", sideTables.length === 0);
      ctrl.quickBtn.title = sideTables.length
        ? `从 ${sideTables.length} 张已缓存表与视图中搜索选取`
        : "当前数据源暂无缓存表，可点击采集目录或直接输入表名读取";
      if (ctrl.pickerOpen) {
        this._renderTablePickerList(role);
      }

      const panel = isSrc ? comp?.source_panel : comp?.target_panel;
      const penetrated = panel?.penetrated_base_tables || [];
      if (penetrated.length) {
        const joined = penetrated.join(", ");
        ctrl.lineageTextEl.textContent = joined;
        ctrl.lineageEl.title = `视图血缘穿透底表：${joined}`;
        ctrl.lineageEl.hidden = false;
      } else {
        ctrl.lineageEl.hidden = true;
      }
    }

    this._syncEndpointInputsFromDraft({ force: false });
    if (this.centerBridgeCountEl) {
      this.centerBridgeCountEl.textContent = `${this.centerRows.length} 组`;
    }
  }

  _syncFilterBarOverflowMask() {
    const bar = this.filterBarMount;
    if (!bar) return;
    const sw = Number(bar.scrollWidth || 0);
    const cw = Number(bar.clientWidth || 0);
    const sl = Number(bar.scrollLeft || 0);
    const hasLeft = sl > 2;
    const hasRight = sw - cw - sl > 2;
    bar.classList.toggle("is-mask-left", hasLeft);
    bar.classList.toggle("is-mask-right", hasRight);
  }

  _populateCandidateList(side) {
    const isSrc = side === "source";
    const list = isSrc ? this.sourceCandidates : this.targetCandidates;
    const kw = (isSrc ? this.srcCandidateFilter : this.tgtCandidateFilter).trim().toLowerCase();
    const filtered = kw
      ? list.filter(
          (c) =>
            String(c.column_name || "").toLowerCase().includes(kw) ||
            String(c.column_comment || "").toLowerCase().includes(kw) ||
            String(c.data_type || "").toLowerCase().includes(kw)
        )
      : list;

    const countBadge = isSrc ? this.srcCandCountBadge : this.tgtCandCountBadge;
    countBadge.textContent = kw ? `${filtered.length}/${list.length}` : String(list.length);

    const cards = filtered.length
      ? filtered.map((col) => {
          const isSelected =
            this.selectedCandidate &&
            this.selectedCandidate.side === side &&
            this.selectedCandidate.columnName.toLowerCase() === String(col.column_name || "").toLowerCase();
          return el(
            "div",
            {
              className: `cross-cand-card ${isSelected ? "is-selected" : ""}`.trim(),
              draggable: true,
              title: `${col.column_name} (${col.data_type || "-"})${col.column_comment ? " · " + col.column_comment : ""}`,
              onClick: () => {
                if (isSelected) {
                  this.selectedCandidate = null;
                } else {
                  this.selectedCandidate = { side, columnName: col.column_name };
                }
                this.render();
              },
              onDblclick: () => this._openPickerForCandidate(side, col.column_name),
              onDragstart: (e) => {
                this.dragPayload = { kind: "candidate", side, colName: col.column_name };
                if (e.dataTransfer) {
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData("text/plain", `${side}:${col.column_name}`);
                }
                e.currentTarget.classList.add("is-dragging");
              },
              onDragend: (e) => {
                this.dragPayload = null;
                e.currentTarget.classList.remove("is-dragging");
              },
            },
            el(
              "div",
              { className: "cross-cand-top" },
              el("span", { className: "mono cross-cand-name", text: col.column_name }),
              el("button", {
                type: "button",
                className: "cross-cand-add-btn",
                title: "选择数据标准绑定",
                text: "+",
                onClick: (e) => {
                  e.stopPropagation();
                  this._openPickerForCandidate(side, col.column_name);
                },
              })
            ),
            el(
              "div",
              { className: "cross-cand-meta" },
              el("span", { className: "cross-type-pill", text: col.data_type || "-" }),
              col.column_comment
                ? el("span", { className: "cross-cand-comment", text: col.column_comment })
                : null
            )
          );
        })
      : [
          el("div", {
            style: { padding: "22px 10px", textAlign: "center", fontSize: "11.5px", color: "var(--os-muted)" },
            text: list.length ? "无匹配待选字段" : "全部字段均已绑定",
          }),
        ];

    const listMount = isSrc ? this.srcCandidateListMount : this.tgtCandidateListMount;
    clearEl(listMount, cards);
  }

  _buildCandidateColumnShell(side) {
    const isSrc = side === "source";
    const searchInput = isSrc ? this.srcSearchInput : this.tgtSearchInput;
    const countBadge = isSrc ? this.srcCandCountBadge : this.tgtCandCountBadge;
    const listMount = isSrc ? this.srcCandidateListMount : this.tgtCandidateListMount;

    return el(
      "aside",
      {
        className: `cross-cand-col ${isSrc ? "is-left" : "is-right"}`.trim(),
        onDragover: (e) => {
          if (!this.dragPayload || this.dragPayload.kind !== "bound" || this.dragPayload.side !== side) return;
          e.preventDefault();
          e.currentTarget.classList.add("is-drop-target");
        },
        onDragleave: (e) => this._leaveDropTarget(e),
        onDrop: (e) => {
          e.preventDefault();
          e.currentTarget.classList.remove("is-drop-target");
          this._handleDropOnCandidatePool(side);
        },
      },
      el(
        "div",
        { className: "cross-col-head" },
        el("span", {
          className: "cross-col-head-title",
          text: isSrc ? "来源表待选字段" : "目标表待选字段",
        }),
        countBadge
      ),
      el(
        "div",
        { className: "cross-cand-search" },
        el(
          "search",
          { className: "cross-cand-search-box" },
          svgIcon("search", 12),
          searchInput
        )
      ),
      listMount
    );
  }

  _syncCandidateColumn(side) {
    const isSrc = side === "source";
    const searchInput = isSrc ? this.srcSearchInput : this.tgtSearchInput;
    searchInput.value = isSrc ? this.srcCandidateFilter : this.tgtCandidateFilter;
    this._populateCandidateList(side);
  }

  _renderBoundChipOrSlot(side, row, rowIdx) {
    const colObj = row[`${side}_col`];
    const hasSelectedForSide = this.selectedCandidate && this.selectedCandidate.side === side;

    if (!colObj) {
      const sideLabel = side === "source" ? "来源" : "目标";
      return el(
        "div",
        {
          className: `cross-slot-target ${hasSelectedForSide ? "is-click-target" : ""}`.trim(),
          title: hasSelectedForSide ? "点击绑定选中字段到此标准" : `拖入或点选${sideLabel}待选字段至此`,
          onClick: () => {
            if (hasSelectedForSide) {
              this._assignCandidateToRow(side, this.selectedCandidate.columnName, rowIdx);
            }
          },
          onDragover: (e) => {
            if (!this.dragPayload || this.dragPayload.side !== side) return;
            e.preventDefault();
            e.currentTarget.classList.add("is-drop-target");
          },
          onDragleave: (e) => this._leaveDropTarget(e),
          onDrop: (e) => {
            e.preventDefault();
            e.currentTarget.classList.remove("is-drop-target");
            this._handleDropOnCenterTarget(rowIdx, side);
          },
        },
        el("span", { text: hasSelectedForSide ? `+ 点击绑定「${this.selectedCandidate.columnName}」` : `+ 拖入${sideLabel}字段` })
      );
    }

    const isUnified = row.bookmark_state === "UNIFIED";
    const isTgt = side === "target";
    const tgtCompliant = row.target_compliance?.status === "COMPLIANT";
    const cc = row.cross_compat;
    const showCompat = Boolean(isTgt && row.paired && cc && cc.applicable);
    const hasCapError = Boolean(showCompat && cc?.capacity_status === "INSUFFICIENT");
    const capTone =
      cc?.capacity_status === "CONTAINED"
        ? "success"
        : cc?.capacity_status === "INSUFFICIENT"
        ? "danger"
        : cc?.capacity_status === "RISK"
        ? "warn"
        : "neutral";
    const convTone =
      cc?.type_conv_status === "CAST_REQUIRED"
        ? "warn"
        : cc?.type_conv_status === "SAFE_CAST"
        ? "info"
        : "neutral";
    const compatBadges = [];
    if (showCompat) {
      if (cc.capacity_status === "INSUFFICIENT" || cc.capacity_status === "RISK") {
        compatBadges.push(badge(cc.capacity_short || cc.capacity_label || "-", capTone));
      }
      if (cc.has_type_conversion) {
        compatBadges.push(badge(cc.type_conv_short || cc.type_conv_label || "-", convTone));
      }
    }

    return el(
      "div",
      {
        className: `cross-bound-chip ${isUnified ? "is-unified" : ""} ${hasCapError ? "has-cap-error" : ""}`.trim(),
        draggable: true,
        title: showCompat
          ? `${colObj.column_name} (${colObj.data_type || "-"}) | ${cc.capacity_label || "-"} | ${cc.type_conv_label || "-"}`
          : `${colObj.column_name} (${colObj.data_type || "-"})`,
        onDragstart: (e) => {
          this.dragPayload = { kind: "bound", side, colName: colObj.column_name, rowIdx };
          if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", `bound:${side}:${rowIdx}`);
          }
          e.currentTarget.classList.add("is-dragging");
        },
        onDragend: (e) => {
          this.dragPayload = null;
          e.currentTarget.classList.remove("is-dragging");
        },
        onDragover: (e) => {
          if (!this.dragPayload || this.dragPayload.side !== side) return;
          e.preventDefault();
          e.currentTarget.classList.add("is-drop-target");
        },
        onDragleave: (e) => this._leaveDropTarget(e),
        onDrop: (e) => {
          e.preventDefault();
          e.currentTarget.classList.remove("is-drop-target");
          this._handleDropOnCenterTarget(rowIdx, side);
        },
        onClick: () => {
          if (hasSelectedForSide) {
            this._assignCandidateToRow(side, this.selectedCandidate.columnName, rowIdx);
          }
        },
      },
      el(
        "div",
        { className: "cross-bound-chip-top" },
        el("span", { className: "mono cross-cand-name", text: colObj.column_name }),
        el(
          "div",
          { className: "cross-bound-chip-actions" },
          isTgt && row.target_needs_migration
            ? badge("待贯标", "warn")
            : isTgt && tgtCompliant
            ? badge("已落标", "success")
            : null,
          el("button", {
            type: "button",
            className: "cross-chip-remove",
            title: "移回待选字段",
            text: "×",
            onClick: (e) => {
              e.stopPropagation();
              this._unbindBoundField(side, rowIdx);
            },
          })
        )
      ),
      el(
        "div",
        { className: "cross-cand-meta" },
        el("span", { className: "cross-type-pill", text: colObj.data_type || "-" }),
        colObj.column_comment
          ? el("span", { className: "cross-cand-comment", text: colObj.column_comment })
          : null
      ),
      compatBadges.length
        ? el(
            "div",
            {
              className: "cross-compat-tags",
              title: [cc.capacity_detail, cc.type_conv_detail].filter(Boolean).join("；"),
            },
            compatBadges
          )
        : null
    );
  }

  _renderCenterStandardCard(row, rowIdx) {
    const st = row.bookmark_state;
    const hasSelected = Boolean(this.selectedCandidate);

    const commonDropHandlers = {
      onDragover: (e) => {
        if (!this.dragPayload) return;
        e.preventDefault();
        e.currentTarget.classList.add("is-drop-target");
      },
      onDragleave: (e) => this._leaveDropTarget(e),
      onDrop: (e) => {
        e.preventDefault();
        e.currentTarget.classList.remove("is-drop-target");
        this._handleDropOnCenterTarget(rowIdx);
      },
    };

    if (st === "SPLIT_DIFF") {
      const sStd = row.source_std || { std_code: row.source_std_code, cn_name: "未知标准", resolved_type: "" };
      const tStd = row.target_std || { std_code: row.target_std_code, cn_name: "未知标准", resolved_type: "" };
      return el(
        "div",
        {
          className: `cross-std-card is-diff ${hasSelected ? "is-click-target" : ""}`.trim(),
          ...commonDropHandlers,
        },
        el(
          "div",
          { className: "cross-std-split-grid" },
          el(
            "button",
            {
              type: "button",
              className: "cross-std-split-pill",
              title: `${sStd.std_code} · ${sStd.cn_name} (${sStd.resolved_type || "-"})`,
              onClick: () => {
                if (this.selectedCandidate) {
                  this._assignCandidateToRow(this.selectedCandidate.side, this.selectedCandidate.columnName, rowIdx);
                  return;
                }
                this._openBookmarkPicker(rowIdx, "source");
              },
            },
            el("span", { className: "mono cross-std-split-code", style: { color: "var(--os-accent)" }, text: sStd.std_code }),
            el("span", { className: "cross-cand-comment", style: { fontSize: "10.5px" }, text: sStd.cn_name })
          ),
          el("span", {
            className: "cross-std-split-neq",
            text: "≠",
          }),
          el(
            "button",
            {
              type: "button",
              className: "cross-std-split-pill",
              title: `${tStd.std_code} · ${tStd.cn_name} (${tStd.resolved_type || "-"})`,
              onClick: () => {
                if (this.selectedCandidate) {
                  this._assignCandidateToRow(this.selectedCandidate.side, this.selectedCandidate.columnName, rowIdx);
                  return;
                }
                this._openBookmarkPicker(rowIdx, "target");
              },
            },
            el("span", { className: "mono cross-std-split-code", style: { color: "var(--status-warn)" }, text: tStd.std_code }),
            el("span", { className: "cross-cand-comment", style: { fontSize: "10.5px" }, text: tStd.cn_name })
          )
        ),
        el(
          "div",
          { className: "cross-std-split-actions" },
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "采用源标",
            title: "两侧均统一为左侧来源标准",
            onClick: () => this._unifyDiffRow(rowIdx, "source"),
          }),
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "采用目标",
            title: "两侧均统一为右侧目标标准",
            onClick: () => this._unifyDiffRow(rowIdx, "target"),
          }),
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "拆为两行",
            title: "拆解为独立的来源单侧行与目标单侧行",
            onClick: () => this._splitRow(rowIdx),
          })
        )
      );
    }

    const rep = row.source_std || row.target_std;
    const isUnified = st === "UNIFIED";
    const partialSideCls = !isUnified
      ? row.source_col
        ? "has-source"
        : row.target_col
        ? "has-target"
        : ""
      : "";
    const sType = row.source_std?.resolved_type || "";
    const tType = row.target_std?.resolved_type || "";
    const typeLabel =
      sType && tType && sType !== tType
        ? `${sType} → ${tType}`
        : tType || sType || "";

    return el(
      "div",
      {
        className: `cross-std-card ${isUnified ? "is-unified" : "is-partial"} ${partialSideCls} ${hasSelected ? "is-click-target" : ""}`.trim(),
        title: `${rep?.std_code || "-"} · ${rep?.cn_name || "未知标准"}${typeLabel ? " · " + typeLabel : ""}`,
        ...commonDropHandlers,
        onClick: () => {
          if (this.selectedCandidate) {
            this._assignCandidateToRow(this.selectedCandidate.side, this.selectedCandidate.columnName, rowIdx);
            return;
          }
          const pickerSide =
            isUnified ? "both" : row.source_col ? "source" : "target";
          this._openBookmarkPicker(rowIdx, pickerSide);
        },
      },
      el(
        "div",
        { className: "cross-std-card-main" },
        el(
          "div",
          { className: "cross-std-title-wrap" },
          el(
            "span",
            { className: "cross-std-code-pill" },
            el("span", { className: `dot ${isUnified ? "dot-success" : "dot-info"}` }),
            el("span", { className: "cross-std-code-text", text: rep?.std_code || "-" })
          ),
          el("span", {
            className: "cross-std-cn-name",
            text: rep?.cn_name || "未知标准",
          })
        ),
        isUnified
          ? el("button", {
              type: "button",
              className: "cross-chip-remove",
              title: "拆解此配对",
              text: "×",
              onClick: (e) => {
                e.stopPropagation();
                this._splitRow(rowIdx);
              },
            })
          : null
      ),
      el(
        "div",
        { className: "cross-std-sub" },
        el("span", { className: "mono cross-std-abbr", text: rep?.en_abbr || "" }),
        typeLabel ? el("span", { className: "mono cross-std-types", text: typeLabel }) : null
      )
    );
  }

  _matchesMatrixRow(row) {
    const f = this.matrixFilter || "ALL";
    if (f === "UNIFIED" && row.bookmark_state !== "UNIFIED") return false;
    if (f === "SPLIT_DIFF" && row.bookmark_state !== "SPLIT_DIFF") return false;
    if (f === "SPLIT_PARTIAL" && row.bookmark_state !== "SPLIT_PARTIAL" && row.bookmark_state !== "SPLIT_UNBOUND") {
      return false;
    }
    if (f === "MIGRATION" && !row.target_needs_migration) return false;
    if (f === "CAPACITY_RISK") {
      const st = row.cross_compat?.capacity_status;
      if (!row.cross_compat?.applicable || (st !== "INSUFFICIENT" && st !== "RISK")) return false;
    }
    if (f === "TYPE_CONV") {
      if (!row.cross_compat?.applicable || !row.cross_compat?.has_type_conversion) return false;
    }

    const kw = String(this.matrixKeyword || "").trim().toLowerCase();
    if (!kw) return true;
    const hay = [
      row.source_col?.column_name,
      row.source_col?.column_comment,
      row.source_col?.data_type,
      row.target_col?.column_name,
      row.target_col?.column_comment,
      row.target_col?.data_type,
      row.source_std_code,
      row.target_std_code,
      row.source_std?.cn_name,
      row.source_std?.en_abbr,
      row.target_std?.cn_name,
      row.target_std?.en_abbr,
      row.cross_compat?.capacity_label,
      row.cross_compat?.type_conv_label,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(kw);
  }

  _populateCenterMatrix() {
    const visibleEntries = [];
    let totalSrcBound = 0;
    let totalTgtBound = 0;
    this.centerRows.forEach((row, realIdx) => {
      this._recomputeRowState(row);
      if (row.source_col) totalSrcBound += 1;
      if (row.target_col) totalTgtBound += 1;
      if (this._matchesMatrixRow(row)) {
        visibleEntries.push({ row, realIdx });
      }
    });

    const isFiltered = this.matrixFilter !== "ALL" || Boolean(String(this.matrixKeyword || "").trim());
    this.centerSrcCountBadge.textContent = isFiltered
      ? `${visibleEntries.filter((x) => Boolean(x.row.source_col)).length}/${totalSrcBound}`
      : String(totalSrcBound);
    this.centerStdCountBadge.textContent = isFiltered
      ? `${visibleEntries.length}/${this.centerRows.length}`
      : String(this.centerRows.length);
    this.centerTgtCountBadge.textContent = isFiltered
      ? `${visibleEntries.filter((x) => Boolean(x.row.target_col)).length}/${totalTgtBound}`
      : String(totalTgtBound);

    const matrixRows = visibleEntries.map(({ row, realIdx }) => {
      const isDiffRow = row.bookmark_state === "SPLIT_DIFF";
      const isRiskRow =
        !isDiffRow &&
        row.cross_compat?.applicable &&
        row.cross_compat?.capacity_status === "INSUFFICIENT";
      return el(
        "div",
        {
          className: `cross-matrix-row ${isDiffRow ? "is-diff-row" : ""} ${isRiskRow ? "is-risk-row" : ""}`.trim(),
        },
        this._renderBoundChipOrSlot("source", row, realIdx),
        this._renderCenterStandardCard(row, realIdx),
        this._renderBoundChipOrSlot("target", row, realIdx)
      );
    });

    const emptyFilterHint =
      this.centerRows.length > 0 && visibleEntries.length === 0
        ? el(
            "div",
            { className: "cross-filter-empty" },
            el("span", { text: "当前筛选条件下暂无匹配的已绑定字段" }),
            el("button", {
              type: "button",
              className: "mac-btn mac-btn-sm",
              text: `显示全部 (${this.centerRows.length})`,
              onClick: () => {
                this.matrixFilter = "ALL";
                this.matrixKeyword = "";
                this.matrixSearchInput.value = "";
                this.render();
              },
            })
          )
        : null;

    const hasSelected = Boolean(this.selectedCandidate);
    const newStdDropzone = el(
      "div",
      {
        className: "cross-matrix-row is-dropzone-row",
      },
      el("div", {}),
      el(
        "div",
        {
          className: `cross-new-std-dropzone ${hasSelected ? "is-click-target" : ""}`.trim(),
          onClick: () => {
            if (this.selectedCandidate) {
              this._openPickerForCandidate(this.selectedCandidate.side, this.selectedCandidate.columnName);
              return;
            }
            if (this.sourceCandidates.length) {
              this._openPickerForCandidate("source", this.sourceCandidates[0].column_name);
            } else if (this.targetCandidates.length) {
              this._openPickerForCandidate("target", this.targetCandidates[0].column_name);
            } else {
              Toast.info("当前无可绑定的待选字段");
            }
          },
          onDragover: (e) => {
            if (!this.dragPayload || (this.dragPayload.kind !== "candidate" && this.dragPayload.kind !== "bound")) return;
            e.preventDefault();
            e.currentTarget.classList.add("is-drop-target");
          },
          onDragleave: (e) => this._leaveDropTarget(e),
          onDrop: (e) => {
            e.preventDefault();
            e.currentTarget.classList.remove("is-drop-target");
            const payload = this.dragPayload;
            this.dragPayload = null;
            if (!payload) return;
            if (payload.kind === "candidate") {
              this._openPickerForCandidate(payload.side, payload.colName);
            } else if (payload.kind === "bound" && typeof payload.rowIdx === "number") {
              this._openPickerForBoundFieldNewRow(payload.side, payload.rowIdx);
            }
          },
        },
        el("span", {
          text: hasSelected
            ? `+ 为「${this.selectedCandidate.columnName}」绑定新标准`
            : "+ 绑定新标准",
        })
      ),
      el("div", {})
    );

    clearEl(this.matrixListMount, emptyFilterHint, ...matrixRows, newStdDropzone);
  }

  _splitColsCsv(csvText) {
    return String(csvText || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
  }

  _getFilteredSavedPairs() {
    const tables = this.overview?.tables || [];
    const kw = String(this.pairSearchQuery || "").trim();
    const kwLow = kw.toLowerCase();

    const byKey = new Map();
    const remoteItems =
      kwLow &&
      this.remotePairSearchResults &&
      String(this.remotePairSearchResults.keyword || "").trim().toLowerCase() === kwLow
        ? this.remotePairSearchResults.items || []
        : null;

    if (remoteItems) {
      for (const r of remoteItems) {
        byKey.set(String(r.std_table_key || "").toLowerCase(), r);
      }
    }

    if (!kwLow) {
      return tables.map((t) => ({
        std_table_key: t.std_table_key,
        source_schema: t.source_schema || "",
        source_table: t.source_table || t.std_table_name || "",
        target_schema: t.target_schema || "",
        target_table: t.target_table || t.std_table_name || "",
        std_table_name: t.std_table_name || "",
        column_count: Number(t.column_count || 0),
        match_scope: "all",
        matched_source_cols: [],
        matched_target_cols: [],
      }));
    }

    const out = [];
    const seen = new Set();

    for (const t of tables) {
      const tKey = String(t.std_table_key || "").toLowerCase();
      const sSch = String(t.source_schema || "");
      const sTbl = String(t.source_table || t.std_table_name || "");
      const tSch = String(t.target_schema || "");
      const tTbl = String(t.target_table || t.std_table_name || "");
      const remoteHit = byKey.get(tKey);

      const tblHit = [
        tKey,
        sSch.toLowerCase(),
        sTbl.toLowerCase(),
        tSch.toLowerCase(),
        tTbl.toLowerCase(),
        String(t.std_table_name || "").toLowerCase(),
        `${sSch}.${sTbl}`.toLowerCase(),
        `${tSch}.${tTbl}`.toLowerCase(),
      ].some((v) => v && v.includes(kwLow));

      const localSrcCols = this._splitColsCsv(t.source_cols_text).filter((c) =>
        c.toLowerCase().includes(kwLow)
      );
      const localTgtCols = this._splitColsCsv(t.target_cols_text).filter((c) =>
        c.toLowerCase().includes(kwLow)
      );
      const localStdCols = this._splitColsCsv(t.std_cols_text).filter((c) =>
        c.toLowerCase().includes(kwLow)
      );
      const localComments = this._splitColsCsv(t.std_comments_text).filter((c) =>
        c.toLowerCase().includes(kwLow)
      );

      const mSrc = [...localSrcCols];
      const mTgt = [...localTgtCols];
      if (remoteHit) {
        for (const rc of remoteHit.matched_source_cols || []) {
          if (!mSrc.some((x) => x.toLowerCase() === rc.toLowerCase())) mSrc.push(rc);
        }
        for (const rc of remoteHit.matched_target_cols || []) {
          if (!mTgt.some((x) => x.toLowerCase() === rc.toLowerCase())) mTgt.push(rc);
        }
      }

      const colHit =
        Boolean(remoteHit) ||
        mSrc.length > 0 ||
        mTgt.length > 0 ||
        localStdCols.length > 0 ||
        localComments.length > 0;
      if (!tblHit && !colHit) continue;

      seen.add(tKey);
      out.push({
        std_table_key: t.std_table_key,
        source_schema: sSch,
        source_table: sTbl,
        target_schema: tSch,
        target_table: tTbl,
        std_table_name: t.std_table_name || "",
        column_count: Number(t.column_count || 0),
        match_scope: tblHit && colHit ? "both" : tblHit ? "table" : "column",
        matched_source_cols: mSrc.slice(0, 4),
        matched_target_cols: mTgt.slice(0, 4),
        matched_std_hints: [...localStdCols, ...localComments].slice(0, 2),
      });
    }

    if (remoteItems) {
      for (const r of remoteItems) {
        const rk = String(r.std_table_key || "").toLowerCase();
        if (!seen.has(rk)) {
          seen.add(rk);
          out.push(r);
        }
      }
    }

    return out;
  }

  _syncPairPickerDisplay() {
    const tables = this.overview?.tables || [];
    this.pairPickerBadge.textContent = String(tables.length);
    if (!this.pairPickerOpen && document.activeElement !== this.pairSearchInput) {
      const active = tables.find((t) => t.std_table_key === this.activeTableKey);
      if (active) {
        this.pairSearchInput.value = `${active.source_table || active.std_table_name} ↔ ${active.target_table || active.std_table_name}`;
        this.pairSearchInput.classList.add("has-selection");
        this.pairClearBtn.hidden = false;
      } else {
        this.pairSearchInput.value = "";
        this.pairSearchInput.classList.remove("has-selection");
        this.pairClearBtn.hidden = true;
      }
    } else {
      this.pairClearBtn.hidden = !(this.pairSearchQuery || this.activeTableKey);
    }
  }

  _positionPairPopover() {
    if (!this.pairPickerRoot?.getBoundingClientRect) return;
    const clipRect = this._getClipBoundsRect();
    if (!clipRect) return;
    const anchorRect = this.pairPickerRoot.getBoundingClientRect();
    const spaceBelow = clipRect.bottom - anchorRect.bottom - 10;
    const maxH = Math.max(140, Math.min(372, Math.floor(spaceBelow)));
    this.pairPopover.style.maxHeight = `${maxH}px`;
  }

  _openPairPicker() {
    this._closeAllTablePickers();
    this.pairPickerOpen = true;
    this.pairPickerRoot.classList.add("is-open");
    this.pairSearchInput.classList.remove("has-selection");
    this.pairSearchInput.value = this.pairSearchQuery;
    this.pairHighlightIdx = -1;
    this._renderPairPopover();
    this.pairPopover.hidden = false;
    this._positionPairPopover();
    this._syncPairPickerDisplay();
  }

  _closePairPicker() {
    this.pairPickerOpen = false;
    this.pairPickerRoot.classList.remove("is-open");
    this.pairPopover.style.maxHeight = "";
    this.pairPopover.hidden = true;
    this.pairSearchQuery = "";
    this.remotePairSearchResults = null;
    this.pairHighlightIdx = -1;
    clearTimeout(this.pairSearchTimer);
    this.pairSearchAbort?.abort();
    this._syncPairPickerDisplay();
  }

  _selectSavedPair(tableKey) {
    this.activeTableKey = tableKey || "";
    this._closePairPicker();
    if (this.loadedOnce && this.overview) {
      this.loadPairComparison(this.activeTableKey, false);
    } else {
      this.loadOverview(this.activeTableKey, false);
    }
  }

  _handlePairSearchInput(val) {
    this.pairSearchQuery = val;
    if (!this.pairPickerOpen) {
      this.pairPickerOpen = true;
      this.pairPickerRoot.classList.add("is-open");
      this.pairPopover.hidden = false;
    }
    this.pairHighlightIdx = 0;
    this._syncPairPickerDisplay();
    this._renderPairPopover();

    clearTimeout(this.pairSearchTimer);
    const kw = String(val || "").trim();
    if (!kw) {
      this.remotePairSearchResults = null;
      this.pairSearchAbort?.abort();
      return;
    }
    this.pairSearchTimer = setTimeout(async () => {
      if (this._destroyed || !this.pairPickerOpen) return;
      this.pairSearchAbort?.abort();
      this.pairSearchAbort = new AbortController();
      try {
        const res = await crossApi.searchPairs(kw, 120, {
          signal: this.pairSearchAbort.signal,
        });
        if (!this._destroyed && this.pairPickerOpen && String(this.pairSearchQuery || "").trim() === kw) {
          this.remotePairSearchResults = res;
          this._renderPairPopover();
        }
      } catch (_ignore) {
        // 忽略中断或离线检索失败，本地字段匹配仍可用
      }
    }, 120);
  }

  _handlePairPickerKeydown(e) {
    if (this._isImeComposing(e)) return;
    if (e.key === "Escape") {
      e.preventDefault?.();
      e.stopPropagation?.();
      this._closePairPicker();
      this.pairSearchInput.blur();
      return;
    }
    if (!this.pairPickerOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        this._openPairPicker();
      }
      return;
    }
    const items = this._filteredPairItems || [];
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!items.length) return;
      this.pairHighlightIdx = (this.pairHighlightIdx + 1) % items.length;
      this._renderPairPopover({ scrollHighlight: true });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!items.length) return;
      this.pairHighlightIdx = this.pairHighlightIdx <= 0 ? items.length - 1 : this.pairHighlightIdx - 1;
      this._renderPairPopover({ scrollHighlight: true });
    } else if (e.key === "Enter") {
      if (items.length > 0) {
        e.preventDefault();
        const idx = this.pairHighlightIdx >= 0 && this.pairHighlightIdx < items.length ? this.pairHighlightIdx : 0;
        this._selectSavedPair(items[idx].std_table_key);
      }
    }
  }

  _renderPairPopover({ scrollHighlight = false } = {}) {
    const items = this._getFilteredSavedPairs();
    this._filteredPairItems = items;
    if (this.pairHighlightIdx >= items.length) {
      this.pairHighlightIdx = items.length ? 0 : -1;
    }
    const kw = String(this.pairSearchQuery || "").trim();

    const head = el(
      "div",
      { className: "cross-pair-popover-head" },
      el("span", {
        text: kw ? `匹配 ${items.length} 对映射（表名/字段）` : `已保存映射表对 (${items.length})`,
      }),
      this.activeTableKey
        ? el("button", {
            type: "button",
            className: "btn btn-xs btn-ghost",
            text: "清除选择",
            onClick: (e) => {
              e.stopPropagation();
              this._selectSavedPair("");
            },
          })
        : null
    );

    let highlightedEl = null;
    const listNodes = items.length
      ? items.map((item, idx) => {
          const isActive = item.std_table_key === this.activeTableKey;
          const isHl = idx === this.pairHighlightIdx;
          const hitParts = [];
          if ((item.matched_source_cols || []).length) {
            hitParts.push(`源: ${item.matched_source_cols.join(", ")}`);
          }
          if ((item.matched_target_cols || []).length) {
            hitParts.push(`目标: ${item.matched_target_cols.join(", ")}`);
          }
          if (!hitParts.length && (item.matched_std_hints || []).length) {
            hitParts.push(`标准: ${item.matched_std_hints.join(", ")}`);
          }
          const schemaLine =
            item.source_schema || item.target_schema
              ? `${item.source_schema ? item.source_schema + "." : ""}${item.source_table} → ${item.target_schema ? item.target_schema + "." : ""}${item.target_table}`
              : "";

          const optBtn = el(
            "button",
            {
              type: "button",
              className: `cross-pair-option ${isActive ? "is-active" : ""} ${isHl ? "is-highlighted" : ""}`.trim(),
              onClick: () => this._selectSavedPair(item.std_table_key),
            },
            el(
              "div",
              { className: "cross-pair-option-top" },
              el("span", {
                className: "mono cross-pair-option-title",
                text: `${item.source_table || item.std_table_name} ↔ ${item.target_table || item.std_table_name}`,
              }),
              el(
                "span",
                { style: { display: "inline-flex", gap: "4px", flexShrink: "0" } },
                isActive ? badge("当前", "info") : null,
                item.column_count ? badge(`${item.column_count}列`, "neutral") : null
              )
            ),
            schemaLine
              ? el("div", { className: "mono cross-pair-option-sub", text: schemaLine })
              : null,
            hitParts.length
              ? el("div", {
                  className: "cross-pair-option-hits",
                  text: `命中字段 · ${hitParts.join(" | ")}`,
                })
              : null
          );
          if (isHl) highlightedEl = optBtn;
          return optBtn;
        })
      : [
          el("div", {
            className: "cross-pair-empty",
            text: kw ? `未找到匹配「${kw}」的表或字段` : "暂无已保存映射表对，可在下方选择库表或上传铺底",
          }),
        ];

    clearEl(
      this.pairPopover,
      head,
      el("div", { className: "cross-pair-popover-list" }, listNodes)
    );
    if (scrollHighlight && highlightedEl) {
      highlightedEl.scrollIntoView?.({ block: "nearest" });
    }
  }

  render() {
    const ov = this.overview || { active_comparison: null, tables: [] };
    const bkSum = this._computeBookmarkSummary();

    this._syncPairPickerDisplay();
    if (this.pairPickerOpen) {
      this._renderPairPopover();
    }
    if (
      globalThis.document?.activeElement !== this.matrixSearchInput &&
      this.matrixSearchInput.value !== this.matrixKeyword
    ) {
      this.matrixSearchInput.value = this.matrixKeyword;
    }

    let activePillEl = null;
    const makeFilterPill = (id, label, count, { dotTone = "", title = "" } = {}) => {
      const isAct = this.matrixFilter === id;
      const pill = el(
        "button",
        {
          type: "button",
          className: `cross-filter-pill ${isAct ? "is-active" : ""}`.trim(),
          "aria-pressed": isAct ? "true" : "false",
          title: title || `按「${label}」筛选对标行`,
          onClick: () => {
            this.matrixFilter = this.matrixFilter === id && id !== "ALL" ? "ALL" : id;
            this._resetScrollPositions({ centerOnly: true });
            this.render();
          },
        },
        dotTone ? el("span", { className: `dot dot-${dotTone}` }) : null,
        el("span", { text: label }),
        el("span", { className: "cross-filter-count", text: String(count) })
      );
      if (isAct) activePillEl = pill;
      return pill;
    };

    const capIssueTotal = bkSum.capacity_insufficient + bkSum.capacity_risk;
    clearEl(
      this.filterBarMount,
      makeFilterPill("ALL", "全部", bkSum.total_rows),
      makeFilterPill("UNIFIED", "一致", bkSum.unified, { dotTone: "success" }),
      makeFilterPill("SPLIT_DIFF", "差异", bkSum.split_diff, { dotTone: "danger" }),
      makeFilterPill("SPLIT_PARTIAL", "单侧", bkSum.split_partial, { dotTone: "warn" }),
      bkSum.target_needs_migration > 0 || this.matrixFilter === "MIGRATION"
        ? makeFilterPill("MIGRATION", "待贯标", bkSum.target_needs_migration, { dotTone: "warn" })
        : null,
      capIssueTotal > 0 || this.matrixFilter === "CAPACITY_RISK"
        ? makeFilterPill(
            "CAPACITY_RISK",
            bkSum.capacity_insufficient > 0 ? "位长/精度不足" : "容量待核验",
            capIssueTotal,
            {
              dotTone: bkSum.capacity_insufficient > 0 ? "danger" : "warn",
              title: `位长/精度不足 ${bkSum.capacity_insufficient} · 容量待核验 ${bkSum.capacity_risk}`,
            }
          )
        : null,
      bkSum.type_conversion > 0 || this.matrixFilter === "TYPE_CONV"
        ? makeFilterPill("TYPE_CONV", "类型转换", bkSum.type_conversion, { dotTone: "info" })
        : null
    );
    if (activePillEl) {
      activePillEl.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    }
    this._syncFilterBarOverflowMask();
    globalThis.requestAnimationFrame?.(() => {
      if (!this._destroyed) this._syncFilterBarOverflowMask();
    });

    this._syncEndpointDeck();

    const hasAnyField =
      this.centerRows.length > 0 ||
      this.sourceCandidates.length > 0 ||
      this.targetCandidates.length > 0;

    let mainBody;
    if (hasAnyField) {
      this._syncCandidateColumn("source");
      this._populateCenterMatrix();
      this._syncCandidateColumn("target");
      mainBody = this.workspaceEl;
    } else {
      const savedPairCount = (ov.tables || []).length;
      mainBody = el(
        "div",
        { className: "explorer-empty-state" },
        el(
          "div",
          { className: "explorer-empty-card", style: { maxWidth: "460px", padding: "26px 28px" } },
          el("div", { className: "cross-empty-hero-icon" }, svgIcon("swap", 22)),
          el("h3", { style: { margin: "0 0 6px 0", fontSize: "14px", fontWeight: "600" }, text: "选择来源与目标库表开始比对" }),
          el("p", {
            style: { margin: "0 0 16px 0", fontSize: "12px", color: "var(--os-text-secondary)", lineHeight: "1.5" },
            text: "在上方左右两侧选取数据源与表名，或导入映射模板快速构建五栏跨环境数标对标视图。",
          }),
          el(
            "div",
            { style: { display: "inline-flex", alignItems: "center", gap: "8px", flexWrap: "wrap", justifyContent: "center" } },
            savedPairCount > 0
              ? el("button", {
                  type: "button",
                  className: "mac-btn mac-btn-sm",
                  text: `选择已保存表对 (${savedPairCount})`,
                  onClick: () => {
                    this._openPairPicker();
                    this.pairSearchInput.focus();
                  },
                })
              : null,
            el("button", {
              type: "button",
              className: "mac-btn mac-btn-sm mac-btn-primary",
              text: "上传映射铺底",
              onClick: () => this.openPairingUploadWizard(),
            })
          )
        )
      );
    }

    if (this._mountedBodyEl !== mainBody) {
      this._mountedBodyEl = mainBody;
      clearEl(this.root, this.subbarMount, this.activityBar.el, this.envStripMount, mainBody);
    }
  }
}
