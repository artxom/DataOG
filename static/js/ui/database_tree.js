/**
 * Data.O.G 无框 Finder 风格数据库目录树 (DatabaseTree)
 */

import { platformApi } from "../core/api.js";
import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { Toast } from "./toast.js";

export class DatabaseTree {
  constructor(mountEl, { onPreviewTable = null } = {}) {
    this.mountEl = mountEl;
    this.onPreviewTable = onPreviewTable;
    this.filterText = "";
    this.expandedConns = new Set();
    this.expandedSchemas = new Set();
    this.collapsedSchemas = new Set();
    this.expandedTables = new Set();
    this.tableColumnsCache = new Map();
    this.selectedTableKey = "";
    this._singleClickTimer = null;

    this.filterInput = el("input", {
      className: "mac-input mac-input-sm",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "筛选库表...",
      "aria-label": "筛选库表",
      style: { width: "100%" },
      onInput: () => {
        this.filterText = this.filterInput.value.trim().toLowerCase();
        this.renderTreeBody();
      },
    });

    this.treeBody = el("div", { className: "nav-tree-scroll", role: "tree" });

    const header = el(
      "div",
      { className: "nav-header" },
      el(
        "div",
        { className: "nav-header-row" },
        el("span", { className: "nav-title", text: "数据源" }),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: "+ 连接",
          onClick: () => store.openApp("settings", { section: "connections", createNew: true }),
        })
      ),
      el("search", {}, this.filterInput)
    );

    clearEl(this.mountEl, header, this.treeBody);
    this._unsub = store.subscribe((_state, reason) => {
      if (["bootstrap", "connections", "catalog"].includes(reason)) {
        this.renderTreeBody();
      }
    });
    this.renderTreeBody();
  }

  destroy() {
    this._unsub?.();
  }

  async _toggleConnection(conn) {
    const cid = conn.connection_id;
    if (this.expandedConns.has(cid)) {
      this.expandedConns.delete(cid);
      this.renderTreeBody();
      return;
    }
    this.expandedConns.add(cid);
    const cached = store.state.catalogTrees[cid];
    if (!cached || !(cached.tables || []).length) {
      try {
        const res = await platformApi.getMetadataTree(cid, true);
        store.updateCatalogTree(cid, res);
      } catch (err) {
        Toast.warn(`${conn.name}: ${err.message}`);
      }
    }
    this.renderTreeBody();
  }

  async _refreshConnection(conn, ev) {
    ev.stopPropagation();
    try {
      const res = await platformApi.refreshMetadata(conn.connection_id);
      this.expandedConns.add(conn.connection_id);
      store.updateCatalogTree(conn.connection_id, res);
      Toast.success("元数据已刷新");
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async _toggleTableColumns(connId, schemaName, tableName, ev) {
    ev.stopPropagation();
    const key = `${connId}::${schemaName}::${tableName}`;
    if (this.expandedTables.has(key)) {
      this.expandedTables.delete(key);
      this.renderTreeBody();
      return;
    }
    this.expandedTables.add(key);
    if (!this.tableColumnsCache.has(key)) {
      try {
        const res = await platformApi.getTableDetail(connId, schemaName, tableName, false);
        const distSet = new Set((res.table?.distribute_keys || []).map((k) => String(k).toLowerCase()));
        const partSet = new Set((res.table?.partition_keys || []).map((k) => String(k).toLowerCase()));
        const cols = (res.table?.columns || []).map((c) => ({
          ...c,
          is_distribute_key: distSet.has(String(c.column_name || "").toLowerCase()),
          is_partition_key: partSet.has(String(c.column_name || "").toLowerCase()),
        }));
        this.tableColumnsCache.set(key, cols);
      } catch (err) {
        Toast.error(err.message);
      }
    }
    this.renderTreeBody();
  }

  _openTablePreview(connId, schemaName, tableName, objectType) {
    const key = `${connId}::${schemaName}::${tableName}`;
    this.selectedTableKey = key;
    this.renderTreeBody();
    if (typeof this.onPreviewTable === "function") {
      this.onPreviewTable({ connectionId: connId, schemaName, tableName, objectType: objectType || "TABLE" });
      return;
    }
    store.openApp("explorer", {
      action: "preview_table",
      connectionId: connId,
      schemaName,
      tableName,
      objectType: objectType || "TABLE",
    });
  }

  renderTreeBody() {
    const conns = store.state.connections || [];
    if (!conns.length) {
      clearEl(
        this.treeBody,
        el(
          "div",
          { className: "grid-empty", style: { padding: "24px 12px" } },
          el("div", { text: "暂无连接" }),
          el("button", {
            type: "button",
            className: "mac-btn mac-btn-sm",
            style: { marginTop: "8px" },
            text: "添加数据库",
            onClick: () => store.openApp("settings", { section: "connections", createNew: true }),
          })
        )
      );
      return;
    }

    // 若仅有一个连接且尚未展开，默认展开以减少点击
    if (conns.length === 1 && !this._autoOpenedFirst) {
      this._autoOpenedFirst = true;
      this.expandedConns.add(conns[0].connection_id);
    }

    const cards = conns.map((conn) => {
      const cid = conn.connection_id;
      const isExpanded = this.expandedConns.has(cid);
      const tree = store.state.catalogTrees[cid] || { schemas: [], tables: [] };

      const connRow = el(
        "div",
        {
          className: "tree-node-row",
          role: "treeitem",
          "aria-expanded": String(isExpanded),
          onClick: () => this._toggleConnection(conn),
        },
        el("span", { text: isExpanded ? "▾" : "▸", style: { width: "10px", color: "var(--os-muted)" } }),
        el("span", { className: "dot dot-success" }),
        el("span", {
          className: "tree-node-title",
          style: { fontWeight: "600" },
          text: conn.name,
          title: `${conn.name} (${conn.db_type})`,
        }),
        el("span", { style: { fontSize: "10.5px", color: "var(--os-muted)" }, text: conn.db_type }),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: "↻",
          title: "刷新元数据",
          onClick: (e) => this._refreshConnection(conn, e),
        })
      );

      const childrenNodes = [];
      if (isExpanded) {
        const tables = tree.tables || [];
        const bySchema = new Map();
        for (const s of tree.schemas || []) {
          const sn = typeof s === "string" ? s : s.schema_name;
          if (sn) bySchema.set(sn, []);
        }
        for (const t of tables) {
          const sn = t.schema_name || conn.default_schema || "default";
          if (!bySchema.has(sn)) bySchema.set(sn, []);
          if (
            !this.filterText ||
            sn.toLowerCase().includes(this.filterText) ||
            (t.table_name || "").toLowerCase().includes(this.filterText) ||
            (t.table_comment || "").toLowerCase().includes(this.filterText)
          ) {
            bySchema.get(sn).push(t);
          }
        }

        if (bySchema.size === 0) {
          childrenNodes.push(
            el("div", {
              style: { padding: "6px 12px", fontSize: "11.5px", color: "var(--os-muted)" },
              text: "点击 ↻ 采集库表",
            })
          );
        }

        for (const [schemaName, schemaTables] of bySchema.entries()) {
          if (this.filterText && schemaTables.length === 0) continue;
          const skey = `${cid}::${schemaName}`;
          const schemaExpanded =
            Boolean(this.filterText) ||
            this.expandedSchemas.has(skey) ||
            (bySchema.size === 1 && !this.collapsedSchemas.has(skey));

          const schemaRow = el(
            "div",
            {
              className: "tree-node-row",
              role: "treeitem",
              "aria-expanded": String(schemaExpanded),
              onClick: () => {
                if (schemaExpanded) {
                  this.expandedSchemas.delete(skey);
                  this.collapsedSchemas.add(skey);
                } else {
                  this.collapsedSchemas.delete(skey);
                  this.expandedSchemas.add(skey);
                }
                this.renderTreeBody();
              },
            },
            el("span", { text: schemaExpanded ? "▾" : "▸", style: { width: "10px", color: "var(--os-muted)" } }),
            svgIcon("folder", 13),
            el("span", { className: "tree-node-title", text: schemaName }),
            el("span", { style: { fontSize: "10.5px", color: "var(--os-muted)" }, text: String(schemaTables.length) })
          );

          const tableListNodes = [];
          if (schemaExpanded) {
            for (const tbl of schemaTables) {
              const tKey = `${cid}::${schemaName}::${tbl.table_name}`;
              const colsExpanded = this.expandedTables.has(tKey);
              const isView = (tbl.object_type || "TABLE") !== "TABLE";

              const tblRow = el(
                "div",
                {
                  className: `tree-node-row ${this.selectedTableKey === tKey ? "is-selected" : ""}`,
                  role: "treeitem",
                  tabindex: "0",
                  "aria-expanded": String(colsExpanded),
                  dataset: { tableKey: tKey },
                  title: tbl.table_comment ? `${tbl.table_name} — ${tbl.table_comment}` : tbl.table_name,
                  onClick: (e) => {
                    e.stopPropagation();
                    this._openTablePreview(cid, schemaName, tbl.table_name, tbl.object_type);
                  },
                  onKeydown: (e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      this._openTablePreview(cid, schemaName, tbl.table_name, tbl.object_type);
                    }
                  },
                },
                el("span", {
                  text: colsExpanded ? "▾" : "▸",
                  style: { width: "10px", display: "inline-block", color: "var(--os-muted)" },
                  title: "展开字段",
                  onClick: (e) => this._toggleTableColumns(cid, schemaName, tbl.table_name, e),
                }),
                svgIcon(isView ? "view" : "table", 13),
                el("span", { className: "tree-node-title", text: tbl.table_name }),
                isView ? badge("V", "warn") : null
              );
              tableListNodes.push(tblRow);

              if (colsExpanded) {
                const cols = this.tableColumnsCache.get(tKey) || [];
                const colChildren = cols.map((c) =>
                  el(
                    "div",
                    { className: "tree-col-row", title: c.column_comment || "" },
                    el(
                      "span",
                      { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
                      c.column_name,
                      c.is_primary_key ? " · PK" : "",
                      c.is_distribute_key ? " · DK" : ""
                    ),
                    el("span", { className: "tree-col-type", text: c.data_type })
                  )
                );
                tableListNodes.push(el("div", { className: "tree-children" }, colChildren));
              }
            }
          }

          childrenNodes.push(
            el(
              "div",
              { className: "tree-children" },
              schemaRow,
              schemaExpanded ? el("div", { className: "tree-children" }, tableListNodes) : null
            )
          );
        }
      }

      return el("div", { className: "tree-conn-card" }, connRow, childrenNodes);
    });

    clearEl(this.treeBody, cards);
  }
}
