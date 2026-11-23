/**
 * 跨环境比对视图 — Spotlight 风格已保存映射表对选择器混入类
 */

import { badge, clearEl, el } from "../../core/dom.js";

export class CrossPairPickerMixin {
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
    if (!this.pairPickerOpen) {
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
    if (globalThis.document?.activeElement === this.pairSearchInput) {
      this.pairSearchInput.blur?.();
    }
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
      this._positionPairPopover();
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
        const res = await this._searchSavedPairsApi(kw, 120, {
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
}
