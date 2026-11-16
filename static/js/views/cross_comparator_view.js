/**
 * Data.O.G 数据标准第 3 步：跨环境比对 (CrossComparatorView)
 * 双列对照画布（来源库 | 目标库）× 内侧中缝拟物书签桥接：
 * - 左右已配对且数据标准一致：合拢为不惹眼的淡绿贯通拟物书签（若目标库物理未达标则附「待贯标」角标）
 * - 不一致 / 单侧 / 未绑标：中缝断开为左右燕尾独立书签，分别展示各自数标
 * - 支持直接拖拽字段卡片调整配对、拆解配对、点击书签选绑数标及两步模板批量铺底
 */

import { crossApi } from "../core/api.js";
import { badge, clearEl, el, statusDot, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { openStandardPickerModal } from "../ui/standard_picker_modal.js";
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
];

export class CrossComparatorView {
  constructor({ onPushToMigrator = null, onStateUpdate = null } = {}) {
    this.onPushToMigrator = onPushToMigrator;
    this.onStateUpdate = onStateUpdate;
    this.overview = null;
    this.activeTableKey = "";
    this.loadedOnce = false;
    this.formatProfile = "AUTO";
    this.formatProfiles = DEFAULT_FORMAT_PROFILES;
    this.dirty = false;
    this.dragPayload = null; // { side: 'source'|'target', rowIdx: number }

    this.draftTableBind = {
      source_schema: "",
      source_table: "",
      target_schema: "",
      target_table: "",
    };
    this.draftRows = [];

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
  }

  async ensureLoaded(force = false) {
    if (this.loadedOnce && !force) return;
    await this.loadOverview(this.activeTableKey, false);
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
    return row;
  }

  _computeBookmarkSummary() {
    const sum = {
      total_rows: this.draftRows.length,
      paired_rows: 0,
      unified: 0,
      split_diff: 0,
      split_partial: 0,
      split_unbound: 0,
      target_needs_migration: 0,
    };
    for (const r of this.draftRows) {
      this._recomputeRowState(r);
      if (r.paired) sum.paired_rows += 1;
      if (r.bookmark_state === "UNIFIED") sum.unified += 1;
      else if (r.bookmark_state === "SPLIT_DIFF") sum.split_diff += 1;
      else if (r.bookmark_state === "SPLIT_PARTIAL") sum.split_partial += 1;
      else sum.split_unbound += 1;
      if (r.target_needs_migration) sum.target_needs_migration += 1;
    }
    return sum;
  }

  _hydrateDraftFromComparison(comp) {
    this.dirty = false;
    if (!comp) {
      this.draftRows = [];
      return;
    }
    const tb = comp.table_binding || {};
    this.draftTableBind = {
      source_schema: tb.source_schema || comp.source_panel?.schema_name || "",
      source_table: tb.source_table || comp.source_panel?.table_name || "",
      target_schema: tb.target_schema || comp.target_panel?.schema_name || "",
      target_table: tb.target_table || comp.target_panel?.table_name || "",
    };
    this.draftRows = (comp.dual_rows || []).map((dr) =>
      this._recomputeRowState({
        row_id: dr.row_id,
        paired: Boolean(dr.paired),
        bookmark_state: dr.bookmark_state,
        source_col: dr.source_col ? { ...dr.source_col } : null,
        target_col: dr.target_col ? { ...dr.target_col } : null,
        source_std_code: dr.source_std_code || "",
        target_std_code: dr.target_std_code || "",
        source_std: dr.source_std ? { ...dr.source_std } : null,
        target_std: dr.target_std ? { ...dr.target_std } : null,
        target_compliance: dr.target_compliance ? { ...dr.target_compliance } : null,
        target_needs_migration: Boolean(dr.target_needs_migration),
      })
    );
  }

  async loadOverview(tableKey = "", forceRefresh = false, extra = {}) {
    try {
      const res = await crossApi.getOverview(tableKey, forceRefresh, {
        formatProfile: this.formatProfile,
        ...extra,
      });
      this.loadedOnce = true;
      this.overview = res;
      if (res.format_profiles?.length) this.formatProfiles = res.format_profiles;
      if (res.active_table_key !== undefined) {
        this.activeTableKey = res.active_table_key || "";
      }
      this._hydrateDraftFromComparison(res.active_comparison);
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async previewSelectedTables(forceRefresh = false) {
    const { source_schema, source_table, target_schema, target_table } = this.draftTableBind;
    if (!source_table && !target_table) {
      Toast.warn("请先在左侧或右侧选择表名");
      return;
    }
    const key = (this.activeTableKey || target_table || source_table || "").trim().toLowerCase();
    await this.loadOverview("", forceRefresh, {
      sourceSchema: source_schema,
      sourceTable: source_table,
      targetSchema: target_schema,
      targetTable: target_table,
    });
    if (!this.activeTableKey && key) {
      this.activeTableKey = key;
    }
  }

  async saveCurrentPairings(forceRefresh = false) {
    const key =
      this.activeTableKey ||
      this.draftTableBind.target_table ||
      this.draftTableBind.source_table;
    if (!key) {
      Toast.warn("请先选择或输入来源库/目标库表名");
      return;
    }
    try {
      const pairings = this.draftRows.map((r) => ({
        source_col: r.source_col?.column_name || "",
        target_col: r.target_col?.column_name || "",
        source_std_code: r.source_std_code || "",
        target_std_code: r.target_std_code || "",
      }));
      const res = await crossApi.savePairings({
        std_table_key: key,
        source_schema: this.draftTableBind.source_schema,
        source_table: this.draftTableBind.source_table,
        target_schema: this.draftTableBind.target_schema,
        target_table: this.draftTableBind.target_table,
        format_profile: this.formatProfile,
        pairings,
        force_refresh: forceRefresh,
        sync_to_field_bindings: true,
      });
      this.overview = res;
      this.activeTableKey = res.active_table_key || key;
      this._hydrateDraftFromComparison(res.active_comparison);
      Toast.success(res.message || "映射关系与书签绑定已保存");
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  smartPairRows() {
    // 提取全部现有左右节点，按相同非空 std_code 或同名重新配对
    const srcPool = [];
    const tgtPool = [];
    for (const r of this.draftRows) {
      if (r.source_col) {
        srcPool.push({
          col: r.source_col,
          std_code: r.source_std_code || "",
          std: r.source_std || null,
        });
      }
      if (r.target_col) {
        tgtPool.push({
          col: r.target_col,
          std_code: r.target_std_code || "",
          std: r.target_std || null,
          target_compliance: r.target_compliance || null,
          target_needs_migration: Boolean(r.target_needs_migration),
        });
      }
    }
    const usedTgt = new Set();
    const newRows = [];

    for (const s of srcPool) {
      let matchIdx = -1;
      if (s.std_code) {
        matchIdx = tgtPool.findIndex((t, i) => !usedTgt.has(i) && t.std_code === s.std_code);
      }
      if (matchIdx === -1) {
        const sLower = (s.col.column_name || "").toLowerCase();
        matchIdx = tgtPool.findIndex(
          (t, i) => !usedTgt.has(i) && (t.col.column_name || "").toLowerCase() === sLower
        );
      }
      if (matchIdx !== -1) {
        usedTgt.add(matchIdx);
        const t = tgtPool[matchIdx];
        newRows.push(
          this._recomputeRowState({
            row_id: `pair_${s.col.column_name}_${t.col.column_name}`,
            source_col: s.col,
            target_col: t.col,
            source_std_code: s.std_code,
            target_std_code: t.std_code,
            source_std: s.std,
            target_std: t.std,
            target_compliance: t.target_compliance,
            target_needs_migration: t.target_needs_migration,
          })
        );
      } else {
        newRows.push(
          this._recomputeRowState({
            row_id: `src_${s.col.column_name}`,
            source_col: s.col,
            target_col: null,
            source_std_code: s.std_code,
            target_std_code: "",
            source_std: s.std,
            target_std: null,
            target_compliance: null,
            target_needs_migration: false,
          })
        );
      }
    }
    tgtPool.forEach((t, i) => {
      if (usedTgt.has(i)) return;
      newRows.push(
        this._recomputeRowState({
          row_id: `tgt_${t.col.column_name}`,
          source_col: null,
          target_col: t.col,
          source_std_code: "",
          target_std_code: t.std_code,
          source_std: null,
          target_std: t.std,
          target_compliance: t.target_compliance,
          target_needs_migration: t.target_needs_migration,
        })
      );
    });

    this.draftRows = newRows;
    this.dirty = true;
    Toast.info("已按数标编号与同名自动对齐字段，点击「保存对应关系」持久化");
    this.render();
  }

  _handleDropOnSlot(targetSide, targetRowIdx) {
    const payload = this.dragPayload;
    this.dragPayload = null;
    if (!payload) return;
    const { side: dragSide, rowIdx: dragRowIdx } = payload;
    if (dragRowIdx < 0 || dragRowIdx >= this.draftRows.length) return;
    if (targetRowIdx < 0 || targetRowIdx >= this.draftRows.length) return;
    if (dragRowIdx === targetRowIdx) return;

    // 当把 source 卡片拖到 target 卡片（或目标侧空槽）上，或同侧拖放排序置换
    const dragRow = this.draftRows[dragRowIdx];
    const dropRow = this.draftRows[targetRowIdx];
    if (!dragRow || !dragRow[`${dragSide}_col`]) return;

    const pickedCol = dragRow[`${dragSide}_col`];
    const pickedCode = dragRow[`${dragSide}_std_code`];
    const pickedStd = dragRow[`${dragSide}_std`];
    const pickedComp = dragSide === "target" ? dragRow.target_compliance : null;
    const pickedNeedsMig = dragSide === "target" ? dragRow.target_needs_migration : false;

    const existingColAtDrop = dropRow[`${dragSide}_col`];
    const existingCodeAtDrop = dropRow[`${dragSide}_std_code`];
    const existingStdAtDrop = dropRow[`${dragSide}_std`];
    const existingCompAtDrop = dragSide === "target" ? dropRow.target_compliance : null;
    const existingNeedsMigAtDrop = dragSide === "target" ? dropRow.target_needs_migration : false;

    const oppSide = dragSide === "source" ? "target" : "source";
    const dragHadOpp = Boolean(dragRow[`${oppSide}_col`]);

    // 将拖拽字段放入 dropRow[dragSide]
    dropRow[`${dragSide}_col`] = pickedCol;
    dropRow[`${dragSide}_std_code`] = pickedCode;
    dropRow[`${dragSide}_std`] = pickedStd;
    if (dragSide === "target") {
      dropRow.target_compliance = pickedComp;
      dropRow.target_needs_migration = pickedNeedsMig;
    }

    if (!existingColAtDrop) {
      // 目标槽原本为空：清空 dragRow[dragSide]，若 dragRow 两侧均空则删除
      dragRow[`${dragSide}_col`] = null;
      dragRow[`${dragSide}_std_code`] = "";
      dragRow[`${dragSide}_std`] = null;
      if (dragSide === "target") {
        dragRow.target_compliance = null;
        dragRow.target_needs_migration = false;
      }
      if (!dragHadOpp) {
        this.draftRows.splice(dragRowIdx, 1);
      }
    } else if (dragHadOpp) {
      // 双方原来均有配对：直接互换同侧字段，保持两行完整
      dragRow[`${dragSide}_col`] = existingColAtDrop;
      dragRow[`${dragSide}_std_code`] = existingCodeAtDrop;
      dragRow[`${dragSide}_std`] = existingStdAtDrop;
      if (dragSide === "target") {
        dragRow.target_compliance = existingCompAtDrop;
        dragRow.target_needs_migration = existingNeedsMigAtDrop;
      }
    } else {
      // dragRow 原为单侧游离字段，替换 dropRow 原字段后，原字段拆成游离行放在 dragRow 原位
      dragRow[`${dragSide}_col`] = existingColAtDrop;
      dragRow[`${dragSide}_std_code`] = existingCodeAtDrop;
      dragRow[`${dragSide}_std`] = existingStdAtDrop;
      if (dragSide === "target") {
        dragRow.target_compliance = existingCompAtDrop;
        dragRow.target_needs_migration = existingNeedsMigAtDrop;
      }
    }

    this.dirty = true;
    this._computeBookmarkSummary();
    this.render();
  }

  _splitRow(rowIdx) {
    const row = this.draftRows[rowIdx];
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
    this.draftRows.splice(rowIdx, 1, srcOnly, tgtOnly);
    this.dirty = true;
    this.render();
  }

  _openBookmarkPicker(rowIdx, side) {
    const row = this.draftRows[rowIdx];
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

    openStandardPickerModal({
      title: isBoth
        ? `调整贯通书签标准 · ${row.source_col?.column_name} ↔ ${row.target_col?.column_name}`
        : `绑定${side === "source" ? "来源" : "目标"}字段标准 · ${colObj.column_name}`,
      initialQuery: currentCode || colObj.column_comment || colObj.column_name,
      currentStdCode: currentCode,
      dbType,
      formatProfile: this.formatProfile,
      onSelect: (picked) => {
        const brief = picked
          ? {
              std_code: picked.std_code,
              cn_name: picked.cn_name,
              en_abbr: picked.en_abbr,
              resolved_type:
                picked[this.formatProfile] || picked.type_dws || picked.type_dw_mart || "",
              domain_name: picked.domain_name || "",
            }
          : null;
        const code = picked ? picked.std_code : "";
        if (isBoth || side === "source") {
          row.source_std_code = code;
          row.source_std = brief ? { ...brief } : null;
        }
        if (isBoth || side === "target") {
          row.target_std_code = code;
          row.target_std = brief ? { ...brief } : null;
        }
        this._recomputeRowState(row);
        this.dirty = true;
        this.render();
      },
    });
  }

  openPairingUploadWizard() {
    openUploadWizardModal({
      title: "批量铺底跨环境映射关系",
      description:
        "先下载系统映射模板（支持按当前已选来源/目标表直接导出预填模板），批量铺底后可直接在书签画布拖拽微调。",
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
      onUpload: async (file) => {
        const res = await crossApi.uploadPairings(file, this.formatProfile);
        this.overview = res;
        this.activeTableKey = res.active_table_key || "";
        this._hydrateDraftFromComparison(res.active_comparison);
        Toast.success(res.message || "跨环境映射铺底成功");
        this.render();
        this.onStateUpdate?.();
      },
    });
  }

  renderHeaderActions() {
    const ov = this.overview || { tables: [] };

    const tableSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "选择映射表对",
        onChange: (e) => {
          this.activeTableKey = e.target.value;
          this.loadOverview(this.activeTableKey, false);
        },
      },
      [
        el("option", { value: "", text: "已保存映射表对" }),
        ...(ov.tables || []).map((t) => {
          const o = el("option", {
            value: t.std_table_key,
            text: `${t.source_table || t.std_table_name} ↔ ${t.target_table || t.std_table_name}`,
          });
          if (t.std_table_key === this.activeTableKey) o.selected = true;
          return o;
        }),
      ]
    );

    return el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      tableSelect,
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm",
          onClick: () => this.openPairingUploadWizard(),
        },
        svgIcon("upload", 13),
        el("span", { text: "上传映射铺底" })
      ),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "智能配对",
        onClick: () => this.smartPairRows(),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: this.dirty ? "保存对应关系 *" : "保存对应关系",
        onClick: () => this.saveCurrentPairings(true),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm mac-btn-primary",
        text: "推送贯标 →",
        onClick: async () => {
          if (this.dirty) {
            await this.saveCurrentPairings(false);
          }
          const ok = await Modal.confirm({
            title: "推送至第 2 步贯标执行",
            message: "将当前目标库已绑标的待改造规则推送至「2. 落标绑定与贯标 → 贯标改造执行队列」，是否继续？",
            confirmText: "推送并切换",
          });
          if (!ok) return;
          try {
            const res = await crossApi.pushToMigrator(this.activeTableKey, this.formatProfile);
            Toast.success(`已推送 ${res.pushed_count ?? 0} 条规则`);
            this.onPushToMigrator?.();
          } catch (err) {
            Toast.error(err.message);
          }
        },
      })
    );
  }

  _makeRoleConnSelect(role, currentId, placeholder) {
    const conns = this.overview?.connections?.length
      ? this.overview.connections
      : store.state.connections || [];

    return el(
      "select",
      {
        className: "mac-input mac-input-sm",
        style: { flex: "1", minWidth: "120px" },
        onChange: async (e) => {
          try {
            const res = await crossApi.bindRole({
              role,
              connection_id: e.target.value,
              active_table_key: this.activeTableKey,
              auto_refresh: true,
            });
            this.overview = res;
            this._hydrateDraftFromComparison(res.active_comparison);
            this.render();
          } catch (err) {
            Toast.error(err.message);
          }
        },
      },
      [
        el("option", { value: "", text: placeholder }),
        ...conns.map((c) => {
          const o = el("option", { value: c.connection_id, text: `${c.name} · ${c.db_type}` });
          if (c.connection_id === currentId) o.selected = true;
          return o;
        }),
      ]
    );
  }

  _makeTableDropdown(role, tablesList) {
    const schKey = `${role}_schema`;
    const tblKey = `${role}_table`;
    const curVal = this.draftTableBind[tblKey]
      ? `${this.draftTableBind[schKey] ? this.draftTableBind[schKey] + "." : ""}${this.draftTableBind[tblKey]}`
      : "";

    return el(
      "select",
      {
        className: "mac-input input-xs",
        style: { flex: "1", minWidth: "130px" },
        "aria-label": role === "source" ? "选择来源库表" : "选择目标库表",
        onChange: (e) => {
          const v = e.target.value;
          if (!v) return;
          if (v.includes(".")) {
            const [s, t] = v.split(".", 2);
            this.draftTableBind[schKey] = s;
            this.draftTableBind[tblKey] = t;
          } else {
            this.draftTableBind[tblKey] = v;
          }
          this.previewSelectedTables(false);
        },
      },
      [
        el("option", { value: "", text: tablesList.length ? "从已缓存库表快速选取..." : "无缓存库表，可手填右侧" }),
        ...tablesList.map((t) => {
          const fq = `${t.schema_name}.${t.table_name}`;
          const o = el("option", {
            value: fq,
            text: t.object_type === "VIEW" ? `${fq} (视图)` : fq,
          });
          if (fq.toLowerCase() === curVal.toLowerCase()) o.selected = true;
          return o;
        }),
      ]
    );
  }

  _renderFieldCard(side, colObj, rowIdx, extraMeta = {}) {
    if (!colObj) {
      return el(
        "div",
        {
          className: "drop-slot-empty",
          onDragover: (e) => {
            if (!this.dragPayload || this.dragPayload.side !== side) return;
            e.preventDefault();
            e.currentTarget.classList.add("is-drop-target");
          },
          onDragleave: (e) => e.currentTarget.classList.remove("is-drop-target"),
          onDrop: (e) => {
            e.preventDefault();
            e.currentTarget.classList.remove("is-drop-target");
            this._handleDropOnSlot(side, rowIdx);
          },
        },
        el("span", { text: `拖入${side === "source" ? "来源" : "目标"}字段以配对` })
      );
    }

    const isDraggingSource =
      this.dragPayload && this.dragPayload.side === side && this.dragPayload.rowIdx === rowIdx;

    return el(
      "div",
      {
        className: `dual-field-card ${isDraggingSource ? "is-dragging" : ""}`.trim(),
        draggable: true,
        title: "按住拖动该字段到对侧或同侧任意行调整配对关系",
        onDragstart: (e) => {
          this.dragPayload = { side, rowIdx };
          if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", `${side}:${rowIdx}`);
          }
          e.currentTarget.classList.add("is-dragging");
        },
        onDragend: (e) => {
          this.dragPayload = null;
          e.currentTarget.classList.remove("is-dragging");
        },
        onDragover: (e) => {
          if (!this.dragPayload) return;
          e.preventDefault();
          e.currentTarget.classList.add("is-drop-target");
        },
        onDragleave: (e) => e.currentTarget.classList.remove("is-drop-target"),
        onDrop: (e) => {
          e.preventDefault();
          e.currentTarget.classList.remove("is-drop-target");
          this._handleDropOnSlot(side, rowIdx);
        },
      },
      el(
        "div",
        { className: "dual-field-top" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "5px", minWidth: "0" } },
          el("span", { className: "drag-grip", text: "⋮⋮" }),
          el("span", {
            className: "mono",
            style: { fontWeight: "600", overflow: "hidden", textOverflow: "ellipsis" },
            text: colObj.column_name,
          })
        ),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "4px", flexShrink: "0" } },
          extraMeta.needsMigration
            ? badge("待贯标", "warn")
            : extraMeta.compliant
            ? badge("已落标", "success")
            : null,
          el("span", { className: "mono", style: { fontSize: "11px", color: "var(--os-muted)" }, text: colObj.data_type || "-" })
        )
      ),
      colObj.column_comment || colObj.lineage_source
        ? el(
            "div",
            { className: "dual-field-sub" },
            colObj.column_comment || "",
            colObj.lineage_source ? ` · 血缘: ${colObj.lineage_source}` : ""
          )
        : null
    );
  }

  _renderCenterBookmark(row, rowIdx) {
    const state = row.bookmark_state;

    if (state === "UNIFIED" && row.source_std) {
      const s = row.source_std;
      return el(
        "div",
        { className: "bookmark-bridge is-unified" },
        el(
          "button",
          {
            type: "button",
            className: "bookmark-ribbon is-unified",
            title: `${s.std_code} · ${s.cn_name} (${s.en_abbr}) — 点击可更换数标`,
            onClick: () => this._openBookmarkPicker(rowIdx, "both"),
          },
          el("span", { className: "bookmark-punch" }),
          el(
            "span",
            { className: "bookmark-ribbon-content" },
            el("span", { className: "mono bookmark-code", text: s.std_code }),
            el("span", { className: "bookmark-cn", text: s.cn_name }),
            el("span", { className: "mono bookmark-en", text: s.en_abbr })
          ),
          el("span", { className: "bookmark-punch" })
        ),
        el("button", {
          type: "button",
          className: "bookmark-unlink-btn",
          title: "拆解此对映射以便重新拖拽配对",
          text: "×",
          onClick: (e) => {
            e.stopPropagation();
            this._splitRow(rowIdx);
          },
        })
      );
    }

    const leftTag = row.source_col
      ? el(
          "button",
          {
            type: "button",
            className: `bookmark-tab bookmark-tab-left ${row.source_std ? "has-std" : "is-empty"}`.trim(),
            title: row.source_std
              ? `来源数标: ${row.source_std.std_code} · ${row.source_std.cn_name}（点击调整）`
              : "点击为来源字段绑定数据标准",
            onClick: () => this._openBookmarkPicker(rowIdx, "source"),
          },
          row.source_std
            ? el(
                "span",
                { className: "bookmark-tab-text" },
                el("span", { className: "mono", style: { fontWeight: "700" }, text: row.source_std.std_code }),
                el("span", { className: "bookmark-tab-cn", text: row.source_std.cn_name })
              )
            : el("span", { className: "bookmark-tab-placeholder", text: "+ 来源绑标" })
        )
      : el("span", { className: "bookmark-gap-holder" });

    const midSep = row.paired
      ? el("button", {
          type: "button",
          className: `bookmark-diff-badge ${state === "SPLIT_DIFF" ? "is-diff" : ""}`.trim(),
          title:
            state === "SPLIT_DIFF"
              ? "两侧数据标准不一致（点击可拆解配对）"
              : "点击可拆解当前配对",
          text: state === "SPLIT_DIFF" ? "≠" : "↔",
          onClick: () => this._splitRow(rowIdx),
        })
      : el("span", { className: "bookmark-diff-dot", text: "·" });

    const rightTag = row.target_col
      ? el(
          "button",
          {
            type: "button",
            className: `bookmark-tab bookmark-tab-right ${
              row.target_std
                ? state === "SPLIT_DIFF"
                  ? "has-diff"
                  : "has-std"
                : "is-empty"
            }`.trim(),
            title: row.target_std
              ? `目标数标: ${row.target_std.std_code} · ${row.target_std.cn_name}（点击调整）`
              : "点击为目标字段绑定数据标准",
            onClick: () => this._openBookmarkPicker(rowIdx, "target"),
          },
          row.target_std
            ? el(
                "span",
                { className: "bookmark-tab-text" },
                el("span", { className: "mono", style: { fontWeight: "700" }, text: row.target_std.std_code }),
                el("span", { className: "bookmark-tab-cn", text: row.target_std.cn_name })
              )
            : el("span", { className: "bookmark-tab-placeholder", text: "+ 目标绑标" })
        )
      : el("span", { className: "bookmark-gap-holder" });

    return el("div", { className: "bookmark-bridge is-split" }, leftTag, midSep, rightTag);
  }

  render() {
    const ov = this.overview || { active_comparison: null };
    const comp = ov.active_comparison;
    const srcPanel = comp?.source_panel || {};
    const tgtPanel = comp?.target_panel || {};
    const bkSum = this._computeBookmarkSummary();

    const formatSelect = el(
      "select",
      {
        className: "mac-input input-xs",
        "aria-label": "目标比对格式列",
        onChange: (e) => {
          this.formatProfile = e.target.value;
          this.loadOverview(this.activeTableKey, false);
        },
      },
      this.formatProfiles.map((p) => {
        const o = el("option", { value: p.value, text: p.label });
        if (p.value === this.formatProfile) o.selected = true;
        return o;
      })
    );

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el(
        "div",
        { className: "mac-subbar-group", style: { gap: "12px" } },
        statusDot(`一致贯通 ${bkSum.unified}`, "success"),
        statusDot(`数标差异 ${bkSum.split_diff}`, "danger"),
        statusDot(`单侧/待绑 ${bkSum.split_partial + bkSum.split_unbound}`, "warn"),
        bkSum.target_needs_migration > 0
          ? badge(`目标库待贯标 ${bkSum.target_needs_migration} 列`, "warn")
          : null
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        formatSelect,
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "导出比对 Excel",
          onClick: () => crossApi.exportExcel(this.activeTableKey).catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "导出 JSON",
          onClick: () => crossApi.exportJson().catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: "送入多邻国 →",
          onClick: () =>
            store.openApp("polyglot", {
              mode: "dialect",
              connectionId: ov.source_connection?.connection_id || "",
              schemaName: this.draftTableBind.source_schema,
              tableName: this.draftTableBind.source_table,
              stdTableKey: this.activeTableKey,
            }),
        })
      )
    );

    const srcTables = ov.source_connection?.tables || [];
    const tgtTables = ov.target_connection?.tables || [];

    const laneHeaders = el(
      "div",
      { className: "dual-lane-headers" },
      // 左侧来源库头部
      el(
        "div",
        { className: "dual-lane-head" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "6px" } },
          badge("来源库", "info"),
          this._makeRoleConnSelect("source", ov.source_connection?.connection_id || "", "选择来源库"),
          this._makeTableDropdown("source", srcTables)
        ),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "5px" } },
          el("input", {
            className: "mac-input input-xs",
            style: { width: "92px" },
            placeholder: "来源 Schema",
            value: this.draftTableBind.source_schema,
            onInput: (e) => (this.draftTableBind.source_schema = e.target.value.trim()),
          }),
          el("input", {
            className: "mac-input input-xs",
            style: { flex: "1" },
            placeholder: "来源表 / 视图名",
            value: this.draftTableBind.source_table,
            onInput: (e) => (this.draftTableBind.source_table = e.target.value.trim()),
            onKeydown: (e) => {
              if (e.key === "Enter") this.previewSelectedTables(false);
            },
          }),
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "读取",
            onClick: () => this.previewSelectedTables(true),
          }),
          (srcPanel.penetrated_base_tables || []).length
            ? badge(`穿透: ${srcPanel.penetrated_base_tables.join(", ")}`, "info")
            : null
        )
      ),
      // 中缝图例
      el(
        "div",
        { className: "dual-lane-head dual-center-legend" },
        el("div", { style: { fontWeight: "600", fontSize: "12px" }, text: "拟物数标书签桥" }),
        el("div", {
          style: { fontSize: "11px", color: "var(--os-muted)", textAlign: "center" },
          text: "淡绿贯通=两侧标准一致 · 断开=差异/待绑 · 支持直接拖拽字段",
        })
      ),
      // 右侧目标库头部
      el(
        "div",
        { className: "dual-lane-head" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "6px" } },
          badge("目标库", "neutral"),
          this._makeRoleConnSelect("target", ov.target_connection?.connection_id || "", "选择目标库"),
          this._makeTableDropdown("target", tgtTables)
        ),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "5px" } },
          el("input", {
            className: "mac-input input-xs",
            style: { width: "92px" },
            placeholder: "目标 Schema",
            value: this.draftTableBind.target_schema,
            onInput: (e) => (this.draftTableBind.target_schema = e.target.value.trim()),
          }),
          el("input", {
            className: "mac-input input-xs",
            style: { flex: "1" },
            placeholder: "目标表名",
            value: this.draftTableBind.target_table,
            onInput: (e) => (this.draftTableBind.target_table = e.target.value.trim()),
            onKeydown: (e) => {
              if (e.key === "Enter") this.previewSelectedTables(false);
            },
          }),
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "读取",
            onClick: () => this.previewSelectedTables(true),
          })
        )
      )
    );

    const canvasRows = this.draftRows.length
      ? this.draftRows.map((row, idx) => {
          const tgtCompliant = row.target_compliance?.status === "COMPLIANT";
          return el(
            "div",
            {
              className: `dual-compare-row ${row.bookmark_state === "UNIFIED" ? "is-unified-row" : ""}`.trim(),
            },
            this._renderFieldCard("source", row.source_col, idx, {}),
            this._renderCenterBookmark(row, idx),
            this._renderFieldCard("target", row.target_col, idx, {
              needsMigration: row.target_needs_migration,
              compliant: tgtCompliant,
            })
          );
        })
      : [
          el(
            "div",
            { className: "explorer-empty-state" },
            el(
              "div",
              { className: "explorer-empty-card", style: { maxWidth: "520px", padding: "24px" } },
              el("div", { style: { marginBottom: "8px", color: "var(--os-accent)" } }, svgIcon("bookmark", 30)),
              el("h3", { style: { margin: "0 0 6px 0", fontSize: "14px" }, text: "来源库 × 目标库 双列书签比对" }),
              el("p", {
                style: { margin: "0 0 14px 0", fontSize: "12px", color: "var(--os-text-secondary)", lineHeight: "1.55" },
                text: "请在左右两栏选择来源库表与目标库表，或点击右上角「上传映射铺底」下载系统模板批量导入对应关系。第 2 步已绑定的字段数据标准会自动呈现于中缝书签上。",
              }),
              el(
                "button",
                {
                  type: "button",
                  className: "mac-btn mac-btn-sm mac-btn-primary",
                  onClick: () => this.openPairingUploadWizard(),
                },
                "上传映射铺底（含系统模板）"
              )
            )
          ),
        ];

    const canvasBody = el("div", { className: "dual-compare-canvas" }, canvasRows);

    clearEl(this.root, subbar, laneHeaders, canvasBody);
  }
}
