/**
 * DataOG 通用高性能数据网格组件 (DataGrid)
 * 支持：无障碍滚动容器、粘性表头、列点击排序、行勾选、单击/双击回调与自定义安全节点渲染
 */

import { clearEl, el } from "../core/dom.js";

export class DataGrid {
  /**
   * @param {{
   *   ariaLabel?: string,
   *   columns: Array<{ key: string, label: string, sortable?: boolean, render?: (row: any, idx: number) => Node | string }>,
   *   rows?: any[],
   *   rowKey?: string | ((row: any, idx: number) => string),
   *   selectable?: boolean,
   *   selectedKeys?: Set<string>,
   *   emptyText?: string,
   *   maxHeight?: string,
   *   onRowClick?: (row: any, idx: number) => void,
   *   onRowDblClick?: (row: any, idx: number) => void,
   *   onSelectionChange?: (selectedSet: Set<string>) => void,
   * }} options
   */
  constructor(options) {
    this.ariaLabel = options.ariaLabel || "数据结果表格";
    this.columns = options.columns || [];
    this.rows = options.rows || [];
    this.rowKey = options.rowKey || "id";
    this.selectable = Boolean(options.selectable);
    this.selectedKeys = options.selectedKeys || new Set();
    this.emptyText = options.emptyText || "暂无数据记录";
    this.maxHeight = options.maxHeight || "";
    this.onRowClick = options.onRowClick || null;
    this.onRowDblClick = options.onRowDblClick || null;
    this.onSelectionChange = options.onSelectionChange || null;

    this.sortKey = "";
    this.sortAsc = true;

    this.root = el("div", {
      className: "table-wrapper",
      tabindex: "0",
      role: "region",
      "aria-label": this.ariaLabel,
      style: this.maxHeight ? { maxHeight: this.maxHeight } : {},
    });
    this.render();
  }

  _getKey(row, idx) {
    if (typeof this.rowKey === "function") return String(this.rowKey(row, idx));
    return String(row?.[this.rowKey] ?? idx);
  }

  setData(rows, columns = null) {
    if (columns) this.columns = columns;
    this.rows = rows || [];
    this.render();
  }

  _getSortedRows() {
    if (!this.sortKey) return this.rows;
    const key = this.sortKey;
    const dir = this.sortAsc ? 1 : -1;
    return [...this.rows].sort((a, b) => {
      const va = a?.[key];
      const vb = b?.[key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
      return String(va).localeCompare(String(vb), "zh-CN", { numeric: true }) * dir;
    });
  }

  render() {
    if (!this.columns.length || !this.rows.length) {
      clearEl(this.root, el("div", { className: "grid-empty", text: this.emptyText }));
      return;
    }

    const sortedRows = this._getSortedRows();
    const headerCells = [];

    if (this.selectable) {
      const allSelected = sortedRows.length > 0 && sortedRows.every((r, i) => this.selectedKeys.has(this._getKey(r, i)));
      const masterCb = el("input", {
        type: "checkbox",
        checked: allSelected,
        "aria-label": "全选当前表格行",
        onChange: (e) => {
          const checked = e.target.checked;
          sortedRows.forEach((r, i) => {
            const k = this._getKey(r, i);
            if (checked) this.selectedKeys.add(k);
            else this.selectedKeys.delete(k);
          });
          this.onSelectionChange?.(this.selectedKeys);
          this.render();
        },
      });
      headerCells.push(el("th", { style: { width: "34px" } }, masterCb));
    }

    for (const col of this.columns) {
      const sortable = col.sortable !== false;
      const indicator = this.sortKey === col.key ? (this.sortAsc ? " ▲" : " ▼") : "";
      const th = el("th", {
        className: sortable ? "is-sortable" : "",
        text: `${col.label}${indicator}`,
        onClick: sortable
          ? () => {
              if (this.sortKey === col.key) {
                this.sortAsc = !this.sortAsc;
              } else {
                this.sortKey = col.key;
                this.sortAsc = true;
              }
              this.render();
            }
          : undefined,
      });
      headerCells.push(th);
    }

    const tbodyRows = sortedRows.map((row, idx) => {
      const rk = this._getKey(row, idx);
      const isSel = this.selectedKeys.has(rk);
      const cells = [];

      if (this.selectable) {
        const cb = el("input", {
          type: "checkbox",
          checked: isSel,
          "aria-label": `选择第 ${idx + 1} 行`,
          onClick: (e) => e.stopPropagation(),
          onChange: (e) => {
            if (e.target.checked) this.selectedKeys.add(rk);
            else this.selectedKeys.delete(rk);
            this.onSelectionChange?.(this.selectedKeys);
            this.render();
          },
        });
        cells.push(el("td", {}, cb));
      }

      for (const col of this.columns) {
        let content;
        if (typeof col.render === "function") {
          content = col.render(row, idx);
        } else {
          const rawVal = row?.[col.key];
          content = rawVal === null || rawVal === undefined ? "NULL" : String(rawVal);
        }
        cells.push(
          el(
            "td",
            {
              title: typeof content === "string" ? content : undefined,
            },
            content instanceof Node ? content : String(content)
          )
        );
      }

      return el(
        "tr",
        {
          className: isSel ? "is-selected" : "",
          onClick: () => this.onRowClick?.(row, idx),
          onDblclick: () => this.onRowDblClick?.(row, idx),
        },
        cells
      );
    });

    const table = el(
      "table",
      { className: "data-grid-table" },
      el("thead", {}, el("tr", {}, headerCells)),
      el("tbody", {}, tbodyRows)
    );
    clearEl(this.root, table);
  }
}
