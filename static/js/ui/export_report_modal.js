import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { Modal } from "./modal.js";

/**
 * 通用库级报告导出弹窗：
 * - 默认选中「离线交互网页报告 (.html)」（内嵌单 Sheet .xlsx 导出能力）
 * - 并列提供「直接导出库级 Excel (.xlsx)」（单 Sheet 包含全部库表）
 * - 下方附轻量可重导入模板 / 铺底配置入口
 */
export function openExportReportModal({
  title = "导出库级报告",
  scopeBadge = "库级全量",
  scopeHint = "",
  options = [],
  secondaryActions = [],
} = {}) {
  if (!options.length) return;

  let selectedId = options[0].id;
  const cardBody = el("div", { className: "export-report-modal" });
  const exportBtn = el("button", {
    type: "button",
    className: "btn btn-primary",
    text: "立即导出报告",
    onClick: () => {
      const chosen = options.find((o) => o.id === selectedId) || options[0];
      Modal.close("export");
      if (chosen && typeof chosen.onExport === "function") {
        chosen.onExport();
      }
    },
  });

  function renderBody() {
    const scopeBox = scopeHint
      ? el(
          "div",
          { className: "export-report-scope" },
          badge(scopeBadge, "info"),
          el("span", { className: "export-report-scope-txt", text: scopeHint })
        )
      : null;

    const optionCards = el(
      "div",
      { className: "export-report-grid", role: "radiogroup" },
      options.map((opt) => {
        const active = opt.id === selectedId;
        return el(
          "button",
          {
            type: "button",
            role: "radio",
            "aria-checked": active ? "true" : "false",
            className: `export-report-opt${active ? " is-selected" : ""}`,
            onClick: () => {
              selectedId = opt.id;
              renderBody();
            },
            onDblclick: () => {
              selectedId = opt.id;
              Modal.close("export");
              if (typeof opt.onExport === "function") {
                opt.onExport();
              }
            },
          },
          el(
            "div",
            { className: "export-report-opt-head" },
            el(
              "span",
              { className: "export-report-opt-title-wrap" },
              el("span", {
                className: `export-report-radio${active ? " is-checked" : ""}`,
              }),
              svgIcon(opt.icon || "download", 14),
              el("strong", { className: "export-report-opt-title", text: opt.title })
            ),
            opt.badge ? badge(opt.badge, opt.badgeTone || (active ? "ok" : "neutral")) : null
          ),
          el("div", { className: "export-report-opt-desc", text: opt.desc || "" })
        );
      })
    );

    const secondaryBox = secondaryActions.length
      ? el(
          "div",
          { className: "export-report-secondary" },
          el("div", {
            className: "export-report-secondary-label",
            text: "可重导入系统的铺底表 / 模板",
          }),
          el(
            "div",
            { className: "export-report-secondary-actions" },
            secondaryActions.map((act) =>
              el(
                "button",
                {
                  type: "button",
                  className: "btn btn-xs",
                  title: act.hint || "",
                  onClick: () => {
                    Modal.close("secondary");
                    if (typeof act.onClick === "function") {
                      act.onClick();
                    }
                  },
                },
                svgIcon(act.icon || "download", 12),
                act.label
              )
            )
          )
        )
      : null;

    const chosen = options.find((o) => o.id === selectedId) || options[0];
    if (chosen) {
      exportBtn.textContent = chosen.ctaLabel || `导出 ${chosen.shortExt || ""}`.trim();
    }

    clearEl(cardBody, scopeBox, optionCards, secondaryBox);
  }

  renderBody();

  Modal.open({
    title,
    width: "560px",
    body: cardBody,
    footer: [
      el("button", {
        type: "button",
        className: "btn",
        text: "取消",
        onClick: () => Modal.close("cancel"),
      }),
      exportBtn,
    ],
  });
}
