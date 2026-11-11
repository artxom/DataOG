/**
 * Data.O.G 极简 SQL 代码编辑与预览框组件 (SqlCodeBox)
 */

import { copyText, downloadBlob, el } from "../core/dom.js";
import { Toast } from "./toast.js";

export class SqlCodeBox {
  constructor(opts = {}) {
    this.title = opts.title || "SQL";
    this.filename = opts.filename || "script.sql";
    this.onRun = opts.onRun || null;
    this.frameless = Boolean(opts.frameless);

    this.textarea = el("textarea", {
      className: "sql-code-textarea",
      spellcheck: "false",
      readonly: Boolean(opts.readOnly),
      placeholder: opts.placeholder || "输入 SQL (⌘+Enter 执行)...",
      style: opts.minHeight ? { minHeight: opts.minHeight } : {},
    });
    this.textarea.value = opts.value || "";

    this.textarea.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && this.onRun) {
        e.preventDefault();
        this.onRun(this.getValue());
      }
    });

    const actions = [];
    if (this.onRun && !opts.readOnly) {
      actions.push(
        el("button", {
          type: "button",
          className: "btn btn-xs btn-primary",
          text: "执行 ⌘↵",
          onClick: () => this.onRun(this.getValue()),
        })
      );
    }

    actions.push(
      el("button", {
        type: "button",
        className: "btn btn-xs btn-ghost",
        text: "复制",
        onClick: async () => {
          const ok = await copyText(this.getValue());
          if (!ok) {
            Toast.warn("内容为空");
            return;
          }
          Toast.success("已复制");
        },
      }),
      el("button", {
        type: "button",
        className: "btn btn-xs btn-ghost",
        text: ".sql",
        onClick: () => {
          const text = this.getValue();
          if (!text.trim()) {
            Toast.warn("内容为空");
            return;
          }
          const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
          downloadBlob(blob, this.filename);
        },
      })
    );

    this.titleNode = el("span", { className: "sql-code-title", text: this.title });
    this.root = el(
      "div",
      { className: `sql-code-box ${this.frameless ? "frameless" : ""}`.trim() },
      el("div", { className: "sql-code-toolbar" }, this.titleNode, el("div", { style: { display: "flex", gap: "4px" } }, actions)),
      this.textarea
    );
  }

  getValue() {
    return this.textarea.value || "";
  }

  setValue(val, newTitle = "") {
    this.textarea.value = val || "";
    if (newTitle) {
      this.titleNode.textContent = newTitle;
    }
  }
}
