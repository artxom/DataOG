/**
 * DataOG 公共轻提示组件 (Toast)
 */

import { el } from "../core/dom.js";

function showToast(message, tone = "info", durationMs = 4200) {
  const root = document.getElementById("toast-root");
  if (!root) return;

  const closeBtn = el("button", {
    type: "button",
    className: "btn btn-xs btn-ghost",
    text: "×",
    "aria-label": "关闭通知",
  });

  const item = el(
    "div",
    { className: `toast-item toast-${tone}`, role: "status" },
    el("span", { text: String(message || "") }),
    closeBtn
  );

  const remove = () => {
    if (item.parentNode) item.remove();
  };
  closeBtn.addEventListener("click", remove);
  root.appendChild(item);

  if (durationMs > 0) {
    setTimeout(remove, durationMs);
  }
}

export const Toast = {
  info: (msg, ms) => showToast(msg, "info", ms),
  success: (msg, ms) => showToast(msg, "success", ms),
  warn: (msg, ms) => showToast(msg, "warn", ms),
  error: (msg, ms = 6000) => showToast(msg, "error", ms),
};
