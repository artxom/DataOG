/**
 * DataOG 公共级联库表选择器 (ObjectPicker: Connection → Schema → Table/View)
 * 直接消费全局 MetadataHub 缓存，支持手动输入与一键在线刷新目录
 */

import { platformApi } from "../core/api.js";
import { clearEl, el } from "../core/dom.js";
import { store } from "../core/state.js";
import { Toast } from "./toast.js";

export class ObjectPicker {
  /**
   * @param {{
   *   connectionId?: string,
   *   schemaName?: string,
   *   tableName?: string,
   *   onChange?: (val: { connectionId: string, schemaName: string, tableName: string }) => void,
   * }} opts
   */
  constructor(opts = {}) {
    this.connectionId = opts.connectionId || "";
    this.schemaName = opts.schemaName || "";
    this.tableName = opts.tableName || "";
    this.onChange = opts.onChange || null;

    this.connSelect = el("select", {
      className: "input input-sm",
      "aria-label": "选择数据源连接",
      onChange: async () => {
        this.connectionId = this.connSelect.value;
        await this._ensureTreeLoaded(false);
        this.renderOptions();
        this._emitChange();
      },
    });

    this.schemaInput = el("input", {
      className: "input input-sm",
      placeholder: "模式名 (Schema)",
      style: { width: "130px" },
      value: this.schemaName,
      onInput: () => {
        this.schemaName = this.schemaInput.value.trim();
        this._renderTableDatalist();
        this._emitChange();
      },
    });

    this.tableInput = el("input", {
      className: "input input-sm",
      placeholder: "表名或视图名",
      style: { width: "180px" },
      value: this.tableName,
      onInput: () => {
        this.tableName = this.tableInput.value.trim();
        this._emitChange();
      },
    });

    const uid = Math.random().toString(36).slice(2, 8);
    this.schemaListId = `op-schemas-${uid}`;
    this.tableListId = `op-tables-${uid}`;
    this.schemaInput.setAttribute("list", this.schemaListId);
    this.tableInput.setAttribute("list", this.tableListId);

    this.schemaDatalist = el("datalist", { id: this.schemaListId });
    this.tableDatalist = el("datalist", { id: this.tableListId });

    this.refreshBtn = el("button", {
      type: "button",
      className: "btn btn-sm btn-ghost",
      text: "刷新库表",
      title: "实时采集并刷新当前连接在 MetadataHub 中的目录缓存",
      onClick: async () => {
        if (!this.connectionId) {
          Toast.warn("请先选择一个数据源连接");
          return;
        }
        try {
          this.refreshBtn.disabled = true;
          const res = await platformApi.refreshMetadata(this.connectionId, this.schemaName || null);
          store.updateCatalogTree(this.connectionId, res);
          this.renderOptions();
          Toast.success(res.message || "目录元数据已刷新");
        } catch (err) {
          Toast.error(err.message);
        } finally {
          this.refreshBtn.disabled = false;
        }
      },
    });

    this.root = el(
      "div",
      { className: "object-picker" },
      this.connSelect,
      this.schemaInput,
      this.tableInput,
      this.refreshBtn,
      this.schemaDatalist,
      this.tableDatalist
    );

    this.renderOptions();
  }

  async _ensureTreeLoaded(autoRefresh = false) {
    if (!this.connectionId) return;
    const cached = store.state.catalogTrees[this.connectionId];
    if (!cached || autoRefresh) {
      try {
        const res = await platformApi.getMetadataTree(this.connectionId, autoRefresh);
        store.updateCatalogTree(this.connectionId, res);
      } catch (_e) {
        // 离线或尚未连通时静默保留手动输入能力
      }
    }
  }

  setValue({ connectionId, schemaName, tableName }) {
    if (connectionId !== undefined) this.connectionId = connectionId;
    if (schemaName !== undefined) {
      this.schemaName = schemaName;
      this.schemaInput.value = schemaName;
    }
    if (tableName !== undefined) {
      this.tableName = tableName;
      this.tableInput.value = tableName;
    }
    this.renderOptions();
  }

  getValue() {
    return {
      connectionId: this.connectionId,
      schemaName: this.schemaInput.value.trim(),
      tableName: this.tableInput.value.trim(),
    };
  }

  _emitChange() {
    this.onChange?.(this.getValue());
  }

  renderOptions() {
    const conns = store.state.connections || [];
    if (!this.connectionId && conns.length > 0) {
      this.connectionId = conns[0].connection_id;
    }

    const opts = [el("option", { value: "", text: "-- 选择数据源连接 --" })];
    for (const c of conns) {
      const opt = el("option", {
        value: c.connection_id,
        text: `${c.name} [${c.db_type}]`,
      });
      if (c.connection_id === this.connectionId) opt.selected = true;
      opts.push(opt);
    }
    clearEl(this.connSelect, opts);

    const connObj = conns.find((x) => x.connection_id === this.connectionId);
    if (!this.schemaName && connObj && connObj.default_schema) {
      this.schemaName = connObj.default_schema;
      this.schemaInput.value = this.schemaName;
    }

    const tree = store.state.catalogTrees[this.connectionId] || { schemas: [], tables: [] };
    const schemaOpts = (tree.schemas || []).map((s) => {
      const name = typeof s === "string" ? s : s.schema_name;
      return el("option", { value: name });
    });
    clearEl(this.schemaDatalist, schemaOpts);
    this._renderTableDatalist();
  }

  _renderTableDatalist() {
    const tree = store.state.catalogTrees[this.connectionId] || { tables: [] };
    const curSchema = (this.schemaName || "").toLowerCase();
    const filtered = (tree.tables || []).filter(
      (t) => !curSchema || (t.schema_name || "").toLowerCase() === curSchema
    );
    clearEl(
      this.tableDatalist,
      filtered.slice(0, 500).map((t) =>
        el("option", {
          value: t.table_name,
          label: `${t.schema_name}.${t.table_name} (${t.object_type || "TABLE"})`,
        })
      )
    );
  }
}
