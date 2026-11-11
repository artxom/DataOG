/**
 * Data.O.G 数据标准子面板 B：跨环境三栏比对 (CrossComparatorView)
 * 左栏（来源库表/四层视图穿透） × 中栏（数据标准与双向桥接） × 右栏（目标库合规核验）
 */

import { crossApi } from "../core/api.js";
import { badge, clearEl, el, pickLocalFile, statusDot } from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

export class CrossComparatorView {
  constructor({ onPushToMigrator = null, onStateUpdate = null } = {}) {
    this.onPushToMigrator = onPushToMigrator;
    this.onStateUpdate = onStateUpdate;
    this.overview = null;
    this.activeTableKey = "";
    this.loadedOnce = false;
    this.draftTableBind = {
      source_schema: "",
      source_table: "",
      target_schema: "",
      target_table: "",
    };
    this.draftColBindings = new Map();

    this.root = el("div", {
      style: { flex: "1", minHeight: "0", display: "flex", flexDirection: "column", overflow: "hidden" },
    });
  }

  async ensureLoaded(force = false) {
    if (this.loadedOnce && !force) return;
    await this.loadOverview(this.activeTableKey, false);
  }

  _hydrateDraftFromComparison(comp) {
    this.draftColBindings.clear();
    if (!comp) return;
    const tb = comp.table_binding || {};
    this.draftTableBind = {
      source_schema: tb.source_schema || comp.source_panel?.schema_name || "",
      source_table: tb.source_table || comp.source_panel?.table_name || "",
      target_schema: tb.target_schema || comp.target_panel?.schema_name || "",
      target_table: tb.target_table || comp.target_panel?.table_name || "",
    };
    for (const sc of comp.standard_panel?.columns || []) {
      this.draftColBindings.set(sc.std_col_name, {
        id: sc.id ?? 0,
        std_col_name: sc.std_col_name,
        orig_col_name: sc.orig_col_name || sc.std_col_name,
        std_col_type: sc.std_col_type || "",
        std_col_comment: sc.std_col_comment || "",
        bound_source_col: sc.bound_source_col || sc.effective_source_col || "",
        bound_target_col: sc.bound_target_col || sc.effective_target_col || "",
      });
    }
  }

  async loadOverview(tableKey = "", forceRefresh = false) {
    try {
      const res = await crossApi.getOverview(tableKey, forceRefresh);
      this.loadedOnce = true;
      this.overview = res;
      this.activeTableKey = res.active_table_key || "";
      this._hydrateDraftFromComparison(res.active_comparison);
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async saveCurrentBindings(forceRefresh = false) {
    const key =
      this.activeTableKey ||
      this.draftTableBind.source_table ||
      this.draftTableBind.target_table;
    if (!key) {
      Toast.warn("请先选择标准表或输入来源/目标表名");
      return;
    }
    try {
      const res = await crossApi.saveBindings({
        std_table_key: key,
        source_schema: this.draftTableBind.source_schema,
        source_table: this.draftTableBind.source_table,
        target_schema: this.draftTableBind.target_schema,
        target_table: this.draftTableBind.target_table,
        column_bindings: [...this.draftColBindings.values()],
        force_refresh: forceRefresh,
      });
      this.overview = res;
      this.activeTableKey = res.active_table_key || key;
      this._hydrateDraftFromComparison(res.active_comparison);
      Toast.success("桥接已保存");
      this.render();
      this.onStateUpdate?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  autoMatchColumnsByName() {
    const comp = this.overview?.active_comparison;
    if (!comp) return;
    const srcCols = new Map((comp.source_panel?.columns || []).map((c) => [c.column_name.toLowerCase(), c.column_name]));
    const tgtCols = new Map((comp.target_panel?.columns || []).map((c) => [c.column_name.toLowerCase(), c.column_name]));

    let matched = 0;
    for (const [stdKey, item] of this.draftColBindings.entries()) {
      const candidates = [stdKey.toLowerCase(), (item.orig_col_name || "").toLowerCase()].filter(Boolean);
      for (const cand of candidates) {
        if (srcCols.has(cand)) {
          item.bound_source_col = srcCols.get(cand);
          matched++;
          break;
        }
      }
      for (const cand of candidates) {
        if (tgtCols.has(cand)) {
          item.bound_target_col = tgtCols.get(cand);
          matched++;
          break;
        }
      }
    }
    Toast.info(`已匹配 ${matched} 处同名字段`);
    this.render();
  }

  renderHeaderActions() {
    const ov = this.overview || { tables: [] };

    const tableSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "选择标准表",
        onChange: (e) => {
          this.activeTableKey = e.target.value;
          this.loadOverview(this.activeTableKey, false);
        },
      },
      [
        el("option", { value: "", text: "选择标准表" }),
        ...(ov.tables || []).map((t) => {
          const o = el("option", {
            value: t.std_table_key,
            text: `${t.std_schema_name ? t.std_schema_name + "." : ""}${t.std_table_name}`,
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
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "导入数标",
        onClick: async () => {
          const f = await pickLocalFile(".xlsx,.json");
          if (!f) return;
          try {
            const res = await crossApi.uploadStandard(f);
            this.overview = res;
            this.activeTableKey = res.active_table_key || "";
            this._hydrateDraftFromComparison(res.active_comparison);
            Toast.success("已导入");
            this.render();
            this.onStateUpdate?.();
          } catch (err) {
            Toast.error(err.message);
          }
        },
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "同名桥接",
        onClick: () => this.autoMatchColumnsByName(),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "保存桥接",
        onClick: () => this.saveCurrentBindings(true),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm mac-btn-primary",
        text: "推送贯标 →",
        onClick: async () => {
          const ok = await Modal.confirm({
            title: "推送至贯标",
            message: "将当前目标库未达标的改造规则写入「贯标」并切换执行，是否继续？",
            confirmText: "推送并切换",
          });
          if (!ok) return;
          try {
            const res = await crossApi.pushToMigrator(this.activeTableKey);
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
        style: { flex: "1", minWidth: "110px" },
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

  render() {
    const ov = this.overview || { active_comparison: null };
    const comp = ov.active_comparison;
    const sum = comp?.summary || {};

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el(
        "div",
        { className: "mac-subbar-group", style: { gap: "14px" } },
        statusDot(`合规 ${sum.compliant ?? 0}`, "success"),
        statusDot(`待改 ${sum.target_pending ?? 0}`, "warn"),
        statusDot(`缺失 ${sum.missing ?? 0}`, "danger")
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "同步贯标清单",
          onClick: async () => {
            try {
              const res = await crossApi.syncFromMigrator();
              this.overview = res;
              this.activeTableKey = res.active_table_key || "";
              this._hydrateDraftFromComparison(res.active_comparison);
              Toast.success("已同步");
              this.render();
              this.onStateUpdate?.();
            } catch (err) {
              Toast.error(err.message);
            }
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "Excel",
          onClick: () => crossApi.exportExcel(this.activeTableKey).catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "JSON",
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

    const srcPanel = comp?.source_panel || {};
    const stdPanel = comp?.standard_panel || {};
    const tgtPanel = comp?.target_panel || {};

    const srcDatalist = el(
      "datalist",
      { id: "cross-src-cols-dl" },
      (srcPanel.columns || []).map((c) => el("option", { value: c.column_name }))
    );
    const tgtDatalist = el(
      "datalist",
      { id: "cross-tgt-cols-dl" },
      (tgtPanel.columns || []).map((c) => el("option", { value: c.column_name }))
    );

    // 左栏：来源库（连接与表选择直接融入栏头）
    const leftLane = el(
      "section",
      { className: "tri-lane" },
      el(
        "div",
        { className: "tri-lane-header" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" } },
          el("strong", { text: "来源" }),
          this._makeRoleConnSelect("source", ov.source_connection?.connection_id || "", "选择来源库")
        ),
        el(
          "div",
          { style: { display: "flex", gap: "5px" } },
          el("input", {
            className: "mac-input input-xs",
            style: { width: "90px" },
            placeholder: "Schema",
            value: this.draftTableBind.source_schema,
            onInput: (e) => (this.draftTableBind.source_schema = e.target.value.trim()),
          }),
          el("input", {
            className: "mac-input input-xs",
            style: { flex: "1" },
            placeholder: "来源表 / 视图",
            value: this.draftTableBind.source_table,
            onInput: (e) => (this.draftTableBind.source_table = e.target.value.trim()),
          })
        ),
        (srcPanel.penetrated_base_tables || []).length
          ? badge(`穿透: ${srcPanel.penetrated_base_tables.join(", ")}`, "info")
          : null
      ),
      el(
        "div",
        { className: "tri-lane-body" },
        (srcPanel.columns || []).length
          ? (srcPanel.columns || []).map((c) =>
              el(
                "div",
                { className: "spine-row-card" },
                el(
                  "div",
                  { style: { display: "flex", justifyContent: "space-between", gap: "6px" } },
                  el("span", { className: "mono", style: { fontWeight: "600" }, text: c.column_name }),
                  el("span", { className: "mono", style: { fontSize: "11px", color: "var(--os-muted)" }, text: c.data_type })
                ),
                c.column_comment
                  ? el("div", { style: { fontSize: "11px", color: "var(--os-text-secondary)" }, text: c.column_comment })
                  : null
              )
            )
          : el("div", { className: "grid-empty", text: "选择来源库与表后点击右上角「保存桥接」" })
      )
    );

    // 中栏：数据标准与双向桥接
    const centerLane = el(
      "section",
      { className: "tri-lane" },
      el(
        "div",
        { className: "tri-lane-header" },
        el(
          "div",
          { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
          el("strong", { text: "数据标准" }),
          badge(`${(stdPanel.columns || []).length} 项`, "neutral")
        )
      ),
      el(
        "div",
        { className: "tri-lane-body" },
        (stdPanel.columns || []).length
          ? (stdPanel.columns || []).map((sc) => {
              const draft = this.draftColBindings.get(sc.std_col_name) || {
                id: sc.id ?? 0,
                std_col_name: sc.std_col_name,
                orig_col_name: sc.orig_col_name || sc.std_col_name,
                std_col_type: sc.std_col_type || "",
                std_col_comment: sc.std_col_comment || "",
                bound_source_col: sc.effective_source_col || "",
                bound_target_col: sc.effective_target_col || "",
              };
              const tgtTone =
                sc.target_status === "COMPLIANT"
                  ? "success"
                  : sc.target_status === "PENDING_MIGRATION"
                  ? "warn"
                  : "danger";
              return el(
                "div",
                { className: "spine-row-card" },
                el(
                  "div",
                  { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "6px" } },
                  el("span", { className: "mono", style: { fontWeight: "600" }, text: `${sc.std_col_name} · ${sc.std_col_type}` }),
                  statusDot(sc.target_status === "COMPLIANT" ? "达标" : sc.target_detail || "待改", tgtTone)
                ),
                sc.std_col_comment
                  ? el("div", { style: { fontSize: "11px", color: "var(--os-text-secondary)" }, text: sc.std_col_comment })
                  : null,
                el(
                  "div",
                  { className: "spine-bridge-inputs" },
                  el("input", {
                    className: "mac-input input-xs mono",
                    list: "cross-src-cols-dl",
                    placeholder: "← 来源字段",
                    value: draft.bound_source_col,
                    onInput: (e) => {
                      draft.bound_source_col = e.target.value.trim();
                      this.draftColBindings.set(sc.std_col_name, draft);
                    },
                  }),
                  el("input", {
                    className: "mac-input input-xs mono",
                    list: "cross-tgt-cols-dl",
                    placeholder: "目标字段 →",
                    value: draft.bound_target_col,
                    onInput: (e) => {
                      draft.bound_target_col = e.target.value.trim();
                      this.draftColBindings.set(sc.std_col_name, draft);
                    },
                  })
                )
              );
            })
          : el("div", { className: "grid-empty", text: "点击右上角「导入数标」或「同步贯标清单」" })
      )
    );

    // 右栏：目标库（连接与表选择直接融入栏头）
    const rightLane = el(
      "section",
      { className: "tri-lane" },
      el(
        "div",
        { className: "tri-lane-header" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" } },
          el("strong", { text: "目标" }),
          this._makeRoleConnSelect("target", ov.target_connection?.connection_id || "", "选择目标库")
        ),
        el(
          "div",
          { style: { display: "flex", gap: "5px" } },
          el("input", {
            className: "mac-input input-xs",
            style: { width: "90px" },
            placeholder: "Schema",
            value: this.draftTableBind.target_schema,
            onInput: (e) => (this.draftTableBind.target_schema = e.target.value.trim()),
          }),
          el("input", {
            className: "mac-input input-xs",
            style: { flex: "1" },
            placeholder: "目标表名",
            value: this.draftTableBind.target_table,
            onInput: (e) => (this.draftTableBind.target_table = e.target.value.trim()),
          })
        )
      ),
      el(
        "div",
        { className: "tri-lane-body" },
        (tgtPanel.columns || []).length
          ? (tgtPanel.columns || []).map((c) => {
              const tone =
                c.compliance_status === "COMPLIANT"
                  ? "success"
                  : c.compliance_status === "PENDING_MIGRATION"
                  ? "warn"
                  : "muted";
              return el(
                "div",
                { className: "spine-row-card" },
                el(
                  "div",
                  { style: { display: "flex", justifyContent: "space-between", gap: "6px" } },
                  el("span", { className: "mono", style: { fontWeight: "600" }, text: c.column_name }),
                  el("span", { className: "mono", style: { fontSize: "11px", color: "var(--os-muted)" }, text: c.data_type })
                ),
                el(
                  "div",
                  { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
                  el("span", { style: { fontSize: "11px", color: "var(--os-text-secondary)" }, text: c.column_comment || "-" }),
                  statusDot(c.compliance_status === "COMPLIANT" ? "达标" : c.compliance_detail || "未绑定", tone)
                )
              );
            })
          : el("div", { className: "grid-empty", text: "选择目标库与表后点击「保存桥接」" })
      )
    );

    clearEl(
      this.root,
      subbar,
      srcDatalist,
      tgtDatalist,
      el("div", { className: "tri-compare-grid" }, leftLane, centerLane, rightLane)
    );
  }
}
