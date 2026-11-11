/**
 * Data.O.G 标准语义化模态框组件 (Modal — 基于原生 <dialog>.showModal())
 * 严格遵循 Modern Web Guidance:
 * - 打开前重置 dialog.returnValue = ''
 * - 优先使用 closedby="any"，在不支持 closedBy 的浏览器使用 getBoundingClientRect 坐标判定实现背景点击轻关闭
 * - 全面替代阻塞式 window.alert / window.confirm / window.prompt
 */

import { clearEl, el } from "../core/dom.js";

const boundDialogs = new WeakSet();

export function bindDialogLightDismissFallback(dialog) {
  if (!dialog || boundDialogs.has(dialog)) return;
  boundDialogs.add(dialog);

  if (!("closedBy" in HTMLDialogElement.prototype)) {
    const isOutside = (ev) => {
      const r = dialog.getBoundingClientRect();
      return (
        ev.clientX < r.left ||
        ev.clientX > r.right ||
        ev.clientY < r.top ||
        ev.clientY > r.bottom
      );
    };
    let pressedOutside = false;
    dialog.addEventListener("pointerdown", (e) => {
      pressedOutside = e.target === dialog && isOutside(e);
    });
    dialog.addEventListener("pointerup", (e) => {
      if (pressedOutside && e.target === dialog && isOutside(e)) {
        dialog.close("cancel");
      }
      pressedOutside = false;
    });
  }
}

function getDialogRefs() {
  const dialog = document.getElementById("global-modal");
  const titleEl = document.getElementById("global-modal-title");
  const bodyEl = document.getElementById("global-modal-body");
  const footerEl = document.getElementById("global-modal-footer");
  const closeBtn = document.getElementById("global-modal-close");

  if (dialog && !boundDialogs.has(dialog)) {
    closeBtn?.addEventListener("click", () => dialog.close("cancel"));
    bindDialogLightDismissFallback(dialog);
  }
  return { dialog, titleEl, bodyEl, footerEl };
}

export const Modal = {
  open({ title, body, footer = [], width = "", onClose = null }) {
    const { dialog, titleEl, bodyEl, footerEl } = getDialogRefs();
    if (!dialog || !titleEl || !bodyEl || !footerEl) return;

    if (dialog.open) {
      dialog.close("");
    }
    dialog.returnValue = "";
    dialog.style.width = width || "";
    titleEl.textContent = String(title || "提示");
    clearEl(bodyEl, body instanceof Node ? body : el("p", { style: { margin: "0" }, text: String(body || "") }));
    clearEl(footerEl, footer);

    if (typeof onClose === "function") {
      const handleClose = () => {
        const val = dialog.returnValue;
        dialog.returnValue = "";
        dialog.removeEventListener("close", handleClose);
        onClose(val);
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

  confirm({ title = "请确认", message = "", confirmText = "确认", danger = false }) {
    return new Promise((resolve) => {
      const cancelBtn = el("button", {
        type: "button",
        className: "mac-btn",
        autofocus: danger ? true : undefined,
        text: "取消",
        onClick: () => Modal.close("cancel"),
      });
      const okBtn = el("button", {
        type: "button",
        className: danger ? "mac-btn btn-danger" : "mac-btn mac-btn-primary",
        autofocus: !danger ? true : undefined,
        text: confirmText,
        onClick: () => Modal.close("confirm"),
      });

      Modal.open({
        title,
        width: "min(440px, 90vw)",
        body:
          message instanceof Node
            ? message
            : el("p", { style: { margin: "2px 0", lineHeight: "1.55" }, text: String(message) }),
        footer: [cancelBtn, okBtn],
        onClose: (retVal) => resolve(retVal === "confirm"),
      });
    });
  },
};
