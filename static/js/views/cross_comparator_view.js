/**
 * DataOG 独立业务插件视图 2：16:9 跨环境三栏数标比对中心 (modules/cross_comparator)
 * 支持：
 * - 左栏（任意来源库表/自动穿透视图） × 中栏（数据标准与双向字段映射桥） × 右栏（任意目标库合规核验）
 * - Excel 数标 / JSON 三方绑定包导入导出
 * - 一键将未达标改造项推送至单库贯标执行台形成治理闭环，或联动唤起跨方言 DDL 翻译器
 */

import { crossApi } from "../core/api.js";
import { badge, clearEl, el, pickLocalFile } from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

export class CrossComparatorView {
  constructor() {
    this.overview = null;
    this.activeTableKey = "";
    this.draftTableBind = {
      source_schema: "",
      source_table: "",
      target_schema: "",
      target_table: "",
    };
    this.draftColBindings = new Map();
    this.root = el("div", { className: "module-page" });
  }

  async mount(container, context = null) {
    clearEl(container, this.root);
    if (context?.tableKey) {
      this.activeTableKey = context.tableKey;
    }
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
      this.overview = res;
      this.activeTableKey = res.active_table_key || "";
      this._hydrateDraftFromComparison(res.active_comparison);
      if (res.warning) Toast.warn(res.warning);
      else if (res.message) Toast.success(res.message);
      this.render();
    } catch (err) {
      Toast.error(`加载跨环境比对状态失败: ${err.message}`);
    }
  }

  async _saveCurrentBindings(forceRefresh = false) {
    const key =
      this.activeTableKey ||
      this.draftTableBind.source_table ||
      this.draftTableBind.target_table;
    if (!key) {
      Toast.warn("请先选择标准表或在左右栏输入要绑定的来源表/目标表名");
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
      Toast.success(res.message || "三方表与字段桥接关系已保存");
      this.render();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _autoMatchColumnsByName() {
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
    Toast.info(`已完成智能同名/原名字段自动匹配（命中 ${matched} 处桥接），点击「保存桥接并刷新比对」生效`);
    this.render();
  }

  render() {
    const ov = this.overview || {
      connections: [],
      tables: [],
      source_connection: {},
      target_connection: {},
      standard_meta: {},
      active_comparison: null,
    };
    const conns = ov.connections?.length ? ov.connections : store.state.connections || [];
    const comp = ov.active_comparison;

    const makeRoleConnSelect = (role, currentId, labelText) =>
      el(
        "label",
        { style: { display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px" } },
        el("span", { style: { fontWeight: "600", color: "var(--text-secondary)" }, text: labelText }),
        el(
          "select",
          {
            className: "input input-sm",
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
                if (res.warning) Toast.warn(res.warning);
                else if (res.message) Toast.success(res.message);
                this.render();
              } catch (err) {
                Toast.error(err.message);
              }
            },
          },
          [
            el("option", { value: "", text: `-- 选择${labelText} --` }),
            ...conns.map((c) => {
              const o = el("option", { value: c.connection_id, text: `${c.name} [${c.db_type}]` });
              if (c.connection_id === currentId) o.selected = true;
              return o;
            }),
          ]
        )
      );

    const toolbar = el(
      "div",
      { className: "module-toolbar" },
      el(
        "div",
        { className: "module-toolbar-left" },
        el("h1", { className: "module-title", text: "跨环境三栏数标比对中心" }),
        makeRoleConnSelect("source", ov.source_connection?.connection_id || "", "左栏来源库"),
        makeRoleConnSelect("target", ov.target_connection?.connection_id || "", "右栏目标库")
      ),
      el(
        "div",
        { className: "module-toolbar-right" },
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "导入数标/绑定包 (.xlsx/.json)",
          onClick: async () => {
            const f = await pickLocalFile(".xlsx,.json");
            if (!f) return;
            try {
              const res = await crossApi.uploadStandard(f);
              this.overview = res;
              this.activeTableKey = res.active_table_key || "";
              this._hydrateDraftFromComparison(res.active_comparison);
              Toast.success(res.message || "数标或绑定配置导入成功");
              this.render();
            } catch (err) {
              Toast.error(err.message);
            }
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "从贯标台同步数标",
          onClick: async () => {
            try {
              const res = await crossApi.syncFromMigrator();
              this.overview = res;
              this.activeTableKey = res.active_table_key || "";
              this._hydrateDraftFromComparison(res.active_comparison);
              Toast.success(res.message || "已同步执行台数标清单");
              this.render();
            } catch (err) {
              Toast.error(err.message);
            }
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "导出 JSON 绑定包",
          onClick: () => crossApi.exportJson().catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "导出三方比对 Excel",
          onClick: () => crossApi.exportExcel(this.activeTableKey).catch((e) => Toast.error(e.message)),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-primary",
          text: "推送未达标项至贯标台 →",
          onClick: async () => {
            const ok = await Modal.confirm({
              title: "推送待改造字段至单库贯标执行台",
              message: "将把目标库中未达标的字段改造规则一键写入「单库落标贯标台」并切换过去执行，是否确认？",
              confirmText: "推送并打开贯标台",
            });
            if (!ok) return;
            try {
              const res = await crossApi.pushToMigrator(this.activeTableKey);
              Toast.success(res.message || `已推送 ${res.pushed_count ?? 0} 条改造项`);
              store.navigateTo("standard_migrator");
            } catch (err) {
              Toast.error(err.message);
            }
          },
        })
      )
    );

    // 表级选择与桥接控制栏
    const tableSelect = el(
      "select",
      {
        className: "input input-sm",
        style: { minWidth: "220px" },
        onChange: (e) => {
          this.activeTableKey = e.target.value;
          this.loadOverview(this.activeTableKey, false);
        },
      },
      [
        el("option", { value: "", text: "-- 选择数据标准表对象 --" }),
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

    const bridgeBar = el(
      "div",
      { className: "card", style: { display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "8px" } },
      el(
        "div",
        { style: { display: "flex", alignItems: "center", flexWrap: "wrap", gap: "6px" } },
        el("span", { style: { fontWeight: "700" }, text: "当前比对标准表:" }),
        tableSelect,
        el("input", {
          className: "input input-sm",
          style: { width: "105px" },
          placeholder: "来源Schema",
          value: this.draftTableBind.source_schema,
          onInput: (e) => (this.draftTableBind.source_schema = e.target.value.trim()),
        }),
        el("input", {
          className: "input input-sm",
          style: { width: "145px" },
          placeholder: "来源表/视图名",
          value: this.draftTableBind.source_table,
          onInput: (e) => (this.draftTableBind.source_table = e.target.value.trim()),
        }),
        el("span", { text: "↔" }),
        el("input", {
          className: "input input-sm",
          style: { width: "105px" },
          placeholder: "目标Schema",
          value: this.draftTableBind.target_schema,
          onInput: (e) => (this.draftTableBind.target_schema = e.target.value.trim()),
        }),
        el("input", {
          className: "input input-sm",
          style: { width: "145px" },
          placeholder: "目标表名",
          value: this.draftTableBind.target_table,
          onInput: (e) => (this.draftTableBind.target_table = e.target.value.trim()),
        })
      ),
      el(
        "div",
        { style: { display: "flex", gap: "6px", flexWrap: "wrap" } },
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "同名自动桥接",
          onClick: () => this._autoMatchColumnsByName(),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-primary",
          text: "保存桥接并刷新比对",
          onClick: () => this._saveCurrentBindings(true),
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-ghost",
          text: "跨方言翻译 DDL →",
          onClick: () =>
            store.navigateTo("ddl_translator", {
              connectionId: ov.source_connection?.connection_id || "",
              schemaName: this.draftTableBind.source_schema,
              tableName: this.draftTableBind.source_table,
              stdTableKey: this.activeTableKey,
            }),
        })
      )
    );

    if (!comp) {
      const emptyCard = el("div", {
        className: "card grid-empty",
        text: "请先导入数据标准 Excel 清单（或从贯标执行台同步），也可以在上方输入来源表名/目标表名后点击「保存桥接并刷新比对」直接开启三栏比对！",
      });
      clearEl(this.root, toolbar, el("div", { className: "module-content" }, bridgeBar, emptyCard));
      return;
    }

    const srcPanel = comp.source_panel || {};
    const stdPanel = comp.standard_panel || {};
    const tgtPanel = comp.target_panel || {};
    const sum = comp.summary || {};

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

    // 左栏：来源库表/视图列
    const leftLane = el(
      "section",
      { className: "tri-lane" },
      el(
        "div",
        { className: "tri-lane-header" },
        el(
          "div",
          { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
          el("strong", { text: `左栏 · 来源库 (${srcPanel.db_type || "未绑定"})` }),
          badge(`${srcPanel.object_type || "TABLE"} · ${(srcPanel.columns || []).length}列`, srcPanel.exists ? "info" : "warn")
        ),
        el("div", {
          style: { fontSize: "11.5px", color: "var(--text-secondary)" },
          text: `${srcPanel.schema_name || "-"}.${srcPanel.table_name || "-"}`,
        }),
        (srcPanel.penetrated_base_tables || []).length
          ? badge(`已四层血缘穿透基表: ${srcPanel.penetrated_base_tables.join(", ")}`, "success")
          : null,
        srcPanel.error ? badge(`探查异常: ${srcPanel.error}`, "danger") : null
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
                  el("strong", { style: { fontFamily: "var(--font-mono)" }, text: `${c.ordinal_position}. ${c.column_name}` }),
                  badge(c.data_type, "neutral")
                ),
                c.lineage_source
                  ? el("div", { style: { fontSize: "11px", color: "var(--status-info)" }, text: `穿透源: ${c.lineage_source}` })
                  : null,
                el("div", { style: { fontSize: "11px", color: "var(--text-secondary)" }, text: c.column_comment || "无字段注释" })
              )
            )
          : el("div", { className: "grid-empty", text: "未读取到来源库表字段（请确认已选择左栏连接且表存在）" })
      )
    );

    // 中栏：数据标准中枢与左右双向字段桥接
    const centerLane = el(
      "section",
      { className: "tri-lane" },
      el(
        "div",
        { className: "tri-lane-header" },
        el(
          "div",
          { style: { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "4px" } },
          el("strong", { text: `中栏 · 数据标准与双向桥接 (${(stdPanel.columns || []).length}项)` }),
          el(
            "div",
            { style: { display: "flex", gap: "4px" } },
            badge(`合规 ${sum.compliant ?? 0}`, "success"),
            badge(`待改造 ${sum.target_pending ?? 0}`, "warn"),
            badge(`缺失 ${sum.missing ?? 0}`, "danger")
          )
        )
      ),
      el(
        "div",
        { className: "tri-lane-body" },
        (stdPanel.columns || []).map((sc) => {
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
              el("strong", { style: { fontFamily: "var(--font-mono)" }, text: `${sc.std_col_name} (${sc.std_col_type})` }),
              badge(sc.target_status === "COMPLIANT" ? "目标已合规" : sc.target_detail || sc.target_status, tgtTone)
            ),
            el("div", { style: { fontSize: "11.5px", color: "var(--text-secondary)" }, text: sc.std_col_comment || "无标准注释" }),
            el(
              "div",
              { className: "spine-bridge-inputs" },
              el("input", {
                className: "input input-xs",
                list: "cross-src-cols-dl",
                placeholder: "← 绑定来源库字段",
                value: draft.bound_source_col,
                onInput: (e) => {
                  draft.bound_source_col = e.target.value.trim();
                  this.draftColBindings.set(sc.std_col_name, draft);
                },
              }),
              el("input", {
                className: "input input-xs",
                list: "cross-tgt-cols-dl",
                placeholder: "绑定目标库字段 →",
                value: draft.bound_target_col,
                onInput: (e) => {
                  draft.bound_target_col = e.target.value.trim();
                  this.draftColBindings.set(sc.std_col_name, draft);
                },
              })
            )
          );
        })
      )
    );

    // 右栏：目标库合规核验
    const rightLane = el(
      "section",
      { className: "tri-lane" },
      el(
        "div",
        { className: "tri-lane-header" },
        el(
          "div",
          { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
          el("strong", { text: `右栏 · 目标库 (${tgtPanel.db_type || "未绑定"})` }),
          badge(`${tgtPanel.object_type || "TABLE"} · ${(tgtPanel.columns || []).length}列`, tgtPanel.exists ? "success" : "warn")
        ),
        el("div", {
          style: { fontSize: "11.5px", color: "var(--text-secondary)" },
          text: `${tgtPanel.schema_name || "-"}.${tgtPanel.table_name || "-"}`,
        }),
        tgtPanel.error ? badge(`探查异常: ${tgtPanel.error}`, "danger") : null
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
                  : "neutral";
              return el(
                "div",
                { className: "spine-row-card" },
                el(
                  "div",
                  { style: { display: "flex", justifyContent: "space-between", gap: "6px" } },
                  el("strong", { style: { fontFamily: "var(--font-mono)" }, text: `${c.ordinal_position}. ${c.column_name}` }),
                  badge(c.data_type, "neutral")
                ),
                el(
                  "div",
                  { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "4px" } },
                  el("span", { style: { fontSize: "11px", color: "var(--text-secondary)" }, text: c.column_comment || "无注释" }),
                  badge(c.compliance_status === "COMPLIANT" ? "达标" : c.compliance_detail || "未绑定", tone)
                )
              );
            })
          : el("div", { className: "grid-empty", text: "未读取到目标库表字段（或目标表尚待新建）" })
      )
    );

    const content = el(
      "div",
      { className: "module-content" },
      bridgeBar,
      srcDatalist,
      tgtDatalist,
      el("div", { className: "tri-compare-grid" }, leftLane, centerLane, rightLane)
    );

    clearEl(this.root, toolbar, content);
  }
}
