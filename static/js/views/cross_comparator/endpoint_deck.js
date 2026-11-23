/**
 * 跨环境比对视图 — 双端数据源配置甲板与可搜索库表浮层选择器混入类
 */

import { isAbortError } from "../../core/http.js";
import { platformApi } from "../../core/platform_api.js";
import { badge, clearEl, el, svgIcon } from "../../core/dom.js";
import { store } from "../../core/state.js";
import { Toast } from "../../ui/toast.js";

export class CrossEndpointDeckMixin {
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
    this.envStripMount?.classList?.add("has-open-picker");
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
    if (!this._isAnyTablePickerOpen()) {
      this.envStripMount?.classList?.remove("has-open-picker");
    }
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
}
