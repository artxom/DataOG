/**
 * DataOG 全局左侧数据库导航目录树 (DatabaseTree)
 * 特性：
 * - 连接一次，全平台零延迟共享 MetadataHub 缓存
 * - 支持展开 Connection → Schema → Tables / Views → 字段与主键/分布键/分区键徽章
 * - 双击任意表或视图节点：立即跳转至「SQL 查询与数据浏览台」预览前 200 行数据、字段结构及原始建表/视图 DDL
 * - 快捷按钮：一键送入「跨方言 DDL 翻译器」或「新建连接」
 */

import { platformApi } from "../core/api.js";
import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { store } from "../core/state.js";
import { Toast } from "./toast.js";

export class DatabaseTree {
  constructor(mountEl) {
    this.mountEl = mountEl;
    this.filterText = "";
    this.expandedConns = new Set();
    this.expandedSchemas = new Set();
    this.collapsedSchemas = new Set();
    this.expandedTables = new Set();
    this.tableColumnsCache = new Map();
    this.selectedTableKey = "";
    this._singleClickTimer = null;

    this.filterInput = el("input", {
      className: "input input-xs",
      type: "search",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "过滤树中模式 / 表 / 视图名...",
      "aria-label": "过滤目录树对象",
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
        el("span", { className: "nav-title", text: "Database Navigator" }),
        el(
          "button",
          {
            type: "button",
            className: "btn btn-xs btn-primary",
            text: "+ 新建数据源",
            onClick: () => store.navigateTo("connections", { createNew: true }),
          }
        )
      ),
      el("search", {}, this.filterInput)
    );

    clearEl(this.mountEl, header, this.treeBody);
    store.subscribe((_state, reason) => {
      if (["bootstrap", "connections", "catalog"].includes(reason)) {
        this.renderTreeBody();
      }
    });
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
        Toast.warn(`读取 [${conn.name}] 目录失败: ${err.message}`);
      }
    }
    this.renderTreeBody();
  }

  async _refreshConnection(conn, ev) {
    ev.stopPropagation();
    try {
      Toast.info(`正在探查并缓存 [${conn.name}] 元数据...`, 2200);
      const res = await platformApi.refreshMetadata(conn.connection_id);
      this.expandedConns.add(conn.connection_id);
      store.updateCatalogTree(conn.connection_id, res);
      Toast.success(res.message || "目录树刷新成功");
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
        Toast.error(`读取字段详情失败: ${err.message}`);
      }
    }
    this.renderTreeBody();
  }

  _openTablePreview(connId, schemaName, tableName, objectType) {
    const key = `${connId}::${schemaName}::${tableName}`;
    this.selectedTableKey = key;
    this.renderTreeBody();
    store.navigateTo("sql_console", {
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
          { className: "grid-empty" },
          el("div", { text: "尚未配置任何数据库连接" }),
          el("div", {
            style: { marginTop: "6px", fontSize: "11.5px" },
            text: "点击上方「+ 新建数据源」连接 DWS / PG / Inceptor / MySQL / SQLite 或自定义 JDBC 驱动",
          })
        )
      );
      return;
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
        el("span", { text: isExpanded ? "▾" : "▸", style: { width: "12px" } }),
        svgIcon("plug", 14),
        el("span", {
          className: "tree-node-title",
          style: { fontWeight: "700" },
          text: conn.name,
          title: `${conn.name} (${conn.db_type} / ${conn.driver_track})`,
        }),
        badge(conn.db_type, conn.driver_track === "JDBC" ? "warn" : "info"),
        el("button", {
          type: "button",
          className: "btn btn-xs btn-ghost",
          text: "↻",
          title: "重新采集此库表元数据",
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
              style: { padding: "6px 10px", fontSize: "11.5px", color: "var(--text-muted)" },
              text: "暂无缓存对象，点击右上角 ↻ 探查库表",
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
            el("span", { text: schemaExpanded ? "▾" : "▸", style: { width: "12px" } }),
            svgIcon("folder", 14),
            el("span", { className: "tree-node-title", text: schemaName }),
            badge(schemaTables.length, "neutral")
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
                  title: `双击预览数据与结构: ${schemaName}.${tbl.table_name}${tbl.table_comment ? " — " + tbl.table_comment : ""}`,
                  onClick: (e) => {
                    e.stopPropagation();
                    this.selectedTableKey = tKey;
                    for (const prev of this.treeBody.querySelectorAll(".tree-node-row.is-selected")) {
                      prev.classList.remove("is-selected");
                    }
                    tblRow.classList.add("is-selected");
                    if (this._singleClickTimer) {
                      clearTimeout(this._singleClickTimer);
                      this._singleClickTimer = null;
                    }
                    if (e.detail >= 2) {
                      this._openTablePreview(cid, schemaName, tbl.table_name, tbl.object_type);
                      return;
                    }
                    this._singleClickTimer = setTimeout(() => {
                      this._singleClickTimer = null;
                      this._toggleTableColumns(cid, schemaName, tbl.table_name, e);
                    }, 210);
                  },
                  onDblclick: (e) => {
                    e.stopPropagation();
                    if (this._singleClickTimer) {
                      clearTimeout(this._singleClickTimer);
                      this._singleClickTimer = null;
                    }
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
                  style: { width: "12px", display: "inline-block" },
                  title: colsExpanded ? "收起字段列表" : "展开字段列表",
                  onClick: (e) => {
                    if (this._singleClickTimer) {
                      clearTimeout(this._singleClickTimer);
                      this._singleClickTimer = null;
                    }
                    this._toggleTableColumns(cid, schemaName, tbl.table_name, e);
                  },
                }),
                svgIcon(isView ? "view" : "table", 13),
                el("span", { className: "tree-node-title", text: tbl.table_name }),
                isView ? badge("VIEW", "warn") : null,
                el("button", {
                  type: "button",
                  className: "btn btn-xs btn-ghost",
                  text: "查",
                  title: "浏览表数据与结构",
                  onClick: (e) => {
                    e.stopPropagation();
                    this._openTablePreview(cid, schemaName, tbl.table_name, tbl.object_type);
                  },
                }),
                el("button", {
                  type: "button",
                  className: "btn btn-xs btn-ghost",
                  text: "DDL",
                  title: "送入跨方言 DDL 翻译与四层穿透器",
                  onClick: (e) => {
                    e.stopPropagation();
                    store.navigateTo("ddl_translator", {
                      connectionId: cid,
                      schemaName,
                      tableName: tbl.table_name,
                    });
                  },
                })
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
                      `${c.ordinal_position}. ${c.column_name}`,
                      c.is_primary_key ? " [PK]" : "",
                      c.is_distribute_key ? " [DIST]" : "",
                      c.is_partition_key ? " [PART]" : ""
                    ),
                    el("span", { className: "tree-col-type", text: c.data_type })
                  )
                );
                tableListNodes.push(el("div", { className: "tree-children" }, colChildren));
              }
            }
          }

          childrenNodes.push(
            el("div", { className: "tree-children" }, schemaRow, schemaExpanded ? el("div", { className: "tree-children" }, tableListNodes) : null)
          );
        }
      }

      return el("div", { className: "tree-conn-card" }, connRow, childrenNodes);
    });

    clearEl(this.treeBody, cards);
  }
}
