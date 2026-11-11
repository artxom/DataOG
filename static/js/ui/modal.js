/**
 * DataOG 标准语义化模态框组件 (Modal — 基于原生 <dialog>.showModal())
 * 严格遵循 Modern Web Guidance:
 * - 打开前重置 dialog.returnValue = ''
 * - 优先使用 closedby="any"，仅在不支持 closedBy 的旧浏览器挂载 pointerdown+pointerup 背景关闭回退
 * - 全面替代阻塞式 window.alert / window.confirm / window.prompt
 */

import { clearEl, el } from "../core/dom.js";

let fallbackBound = false;

function getDialogRefs() {
  const dialog = document.getElementById("global-modal");
  const titleEl = document.getElementById("global-modal-title");
  const bodyEl = document.getElementById("global-modal-body");
  const footerEl = document.getElementById("global-modal-footer");
  const closeBtn = document.getElementById("global-modal-close");

  if (dialog && !fallbackBound) {
    fallbackBound = true;
    closeBtn?.addEventListener("click", () => dialog.close("cancel"));

    if (!("closedBy" in HTMLDialogElement.prototype)) {
      let downOnBackdrop = false;
      dialog.addEventListener("pointerdown", (e) => {
        downOnBackdrop = e.target === dialog;
      });
      dialog.addEventListener("pointerup", (e) => {
        if (downOnBackdrop && e.target === dialog) {
          dialog.close("cancel");
        }
        downOnBackdrop = false;
      });
    }
  }
  return { dialog, titleEl, bodyEl, footerEl };
}

export const Modal = {
  /**
   * 打开通用模态弹窗
   * @param {{ title: string, body: Node | string, footer?: Node[], width?: string, onClose?: (returnValue: string) => void }} opts
   */
  open({ title, body, footer = [], width = "", onClose = null }) {
    const { dialog, titleEl, bodyEl, footerEl } = getDialogRefs();
    if (!dialog || !titleEl || !bodyEl || !footerEl) return;

    if (dialog.open) {
      dialog.close("");
    }
    dialog.returnValue = "";
    dialog.style.width = width || "";
    titleEl.textContent = String(title || "操作窗口");
    clearEl(bodyEl, body instanceof Node ? body : el("p", { text: String(body || "") }));
    clearEl(footerEl, footer);

    if (typeof onClose === "function") {
      const handleClose = () => {
        dialog.removeEventListener("close", handleClose);
        onClose(dialog.returnValue);
      };
      dialog.addEventListener("close", handleClose);
    }

    dialog.showModal();
  },

  close(returnValue = "") {
    const { dialog } = getDialogRefs();
    if (dialog && dialog.open) {
      dialog.close(returnValue);
    }
  },

  /**
   * 异步确认弹窗（完全替代 window.confirm）
   * @param {{ title: string, message: string | Node, confirmText?: string, danger?: boolean }} opts
   * @returns {Promise<boolean>}
   */
  confirm({ title = "请确认操作", message = "", confirmText = "确认执行", danger = false }) {
    return new Promise((resolve) => {
      const cancelBtn = el(
        "button",
        {
          type: "button",
          className: "btn btn-secondary",
          text: "取消",
          onClick: () => Modal.close("cancel"),
        }
      );
      const okBtn = el(
        "button",
        {
          type: "button",
          className: danger ? "btn btn-danger" : "btn btn-primary",
          text: confirmText,
          onClick: () => Modal.close("confirm"),
        }
      );

      Modal.open({
        title,
        width: "min(480px, 92vw)",
        body: message instanceof Node ? message : el("p", { style: { margin: "4px 0" }, text: String(message) }),
        footer: [cancelBtn, okBtn],
        onClose: (retVal) => resolve(retVal === "confirm"),
      });
    });
  },
};
