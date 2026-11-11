/**
 * DataOG 公共 SQL 代码编辑与预览框组件 (SqlCodeBox)
 * 支持：快捷键 Ctrl/Cmd+Enter 运行、一键复制、一键下载 .sql 脚本
 */

import { downloadBlob, el } from "../core/dom.js";
import { Toast } from "./toast.js";

export class SqlCodeBox {
  /**
   * @param {{
   *   title?: string,
   *   value?: string,
   *   readOnly?: boolean,
   *   placeholder?: string,
   *   minHeight?: string,
   *   filename?: string,
   *   onRun?: (sql: string) => void,
   * }} opts
   */
  constructor(opts = {}) {
    this.title = opts.title || "SQL 脚本";
    this.filename = opts.filename || "script.sql";
    this.onRun = opts.onRun || null;

    this.textarea = el("textarea", {
      className: "sql-code-textarea",
      spellcheck: "false",
      readonly: Boolean(opts.readOnly),
      placeholder: opts.placeholder || "在此输入 SQL 语句 (按 Cmd/Ctrl + Enter 快速执行)...",
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
          text: "执行 SQL (⌘/Ctrl+Enter)",
          onClick: () => this.onRun(this.getValue()),
        })
      );
    }

    actions.push(
      el("button", {
        type: "button",
        className: "btn btn-xs btn-secondary",
        text: "复制 SQL",
        onClick: async () => {
          const text = this.getValue();
          if (!text.trim()) {
            Toast.warn("当前没有可复制的 SQL 内容");
            return;
          }
          try {
            await navigator.clipboard.writeText(text);
            Toast.success("已复制 SQL 到剪贴板");
          } catch (_e) {
            this.textarea.select();
            document.execCommand("copy");
            Toast.success("已复制 SQL 到剪贴板");
          }
        },
      }),
      el("button", {
        type: "button",
        className: "btn btn-xs btn-ghost",
        text: "下载 .sql",
        onClick: () => {
          const text = this.getValue();
          if (!text.trim()) {
            Toast.warn("当前没有可导出的 SQL 内容");
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
      { className: "sql-code-box" },
      el("div", { className: "sql-code-toolbar" }, this.titleNode, el("div", { style: { display: "flex", gap: "6px" } }, actions)),
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
