/**
 * Data.O.G 数据标准快速检索与绑定弹窗 (StandardPickerModal)
 * 在第 2 步「落标绑定」与第 3 步「跨环境书签比对」中共用，实时检索全局标准字典
 */

import { standardsApi } from "../core/api.js";
import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { Modal } from "./modal.js";
import { Toast } from "./toast.js";

function pickDisplayType(item, formatProfile = "AUTO", dbType = "DWS") {
  if (!item) return "";
  if (formatProfile && formatProfile !== "AUTO" && item[formatProfile]) {
    return item[formatProfile];
  }
  const dt = String(dbType || "DWS").toUpperCase();
  if (dt === "INCEPTOR") {
    return item.type_argodb || item.type_dws || item.type_mysql || "";
  }
  if (dt === "MYSQL") {
    return item.type_mysql || item.type_ob_mysql || item.type_dws || "";
  }
  if (dt === "ORACLE") {
    return item.type_oracle || item.type_ob_oracle || item.type_dws || "";
  }
  if (dt === "DM8") {
    return item.type_dameng || item.type_oracle || item.type_dws || "";
  }
  return item.type_dws || item.type_dw_mart || item.type_oracle || item.type_mysql || "";
}

/**
 * @param {Object} opts
 * @param {string} [opts.title]
 * @param {string} [opts.initialQuery]
 * @param {string} [opts.currentStdCode]
 * @param {string} [opts.dbType]
 * @param {string} [opts.formatProfile]
 * @param {(stdItem: Object | null) => Promise<void> | void} opts.onSelect
 */
export function openStandardPickerModal({
  title = "选择数据标准",
  initialQuery = "",
  currentStdCode = "",
  dbType = "DWS",
  formatProfile = "AUTO",
  onSelect,
}) {
  let timer = null;
  const listMount = el("div", { className: "std-picker-list" });
  const input = el("input", {
    className: "mac-input",
    type: "search",
    autocomplete: "off",
    autocorrect: "off",
    autocapitalize: "none",
    spellcheck: "false",
    placeholder: "输入数据项编号、中文名称、英文简称或引用域...",
    value: initialQuery || "",
    style: { flex: "1" },
  });

  const renderRows = (items = []) => {
    if (!items.length) {
      clearEl(
        listMount,
        el("div", {
          className: "grid-empty",
          style: { padding: "28px 16px" },
          text: "未匹配到符合条件的数据标准（请先在「1. 标准字典」导入标准或更换关键词）",
        })
      );
      return;
    }

    const cards = items.map((it) => {
      const isCur = currentStdCode && it.std_code === currentStdCode;
      const physType = pickDisplayType(it, formatProfile, dbType);
      return el(
        "button",
        {
          type: "button",
          className: `std-picker-item ${isCur ? "is-current" : ""}`.trim(),
          onClick: async () => {
            try {
              Modal.close("picked");
              await onSelect?.(it);
            } catch (err) {
              Toast.error(err.message);
            }
          },
        },
        el(
          "div",
          { className: "std-picker-item-top" },
          el(
            "div",
            { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
            badge(it.std_code, isCur ? "success" : "info"),
            el("strong", { text: it.cn_name }),
            el("span", { className: "mono", style: { color: "var(--os-accent)", fontWeight: "600" }, text: it.en_abbr })
          ),
          el(
            "div",
            { style: { display: "flex", alignItems: "center", gap: "6px", flexShrink: "0" } },
            physType ? badge(physType, "neutral") : null,
            isCur ? badge("当前已绑", "success") : null
          )
        ),
        el(
          "div",
          { className: "std-picker-item-sub" },
          it.domain_name ? `域: ${it.domain_name}` : "",
          it.domain_name && it.biz_definition ? " · " : "",
          it.biz_definition || ""
        )
      );
    });
    clearEl(listMount, cards);
  };

  const runSearch = async () => {
    try {
      const res = await standardsApi.lookup(input.value.trim(), 40);
      renderRows(res.items || []);
    } catch (err) {
      clearEl(listMount, el("div", { className: "grid-empty", text: err.message }));
    }
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(runSearch, 180);
  });

  const searchRow = el(
    "div",
    { style: { display: "flex", alignItems: "center", gap: "8px" } },
    svgIcon("search", 15),
    input,
    el("button", {
      type: "button",
      className: "mac-btn mac-btn-sm",
      text: "搜索",
      onClick: runSearch,
    })
  );

  const body = el(
    "div",
    { style: { display: "flex", flexDirection: "column", gap: "10px" } },
    searchRow,
    listMount
  );

  const footer = [
    el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close("cancel") }),
  ];
  if (currentStdCode) {
    footer.unshift(
      el("button", {
        type: "button",
        className: "mac-btn btn-danger",
        style: { marginRight: "auto" },
        text: "解除当前绑定",
        onClick: async () => {
          try {
            Modal.close("unbind");
            await onSelect?.(null);
          } catch (err) {
            Toast.error(err.message);
          }
        },
      })
    );
  }

  Modal.open({
    title,
    width: "min(680px, 94vw)",
    body,
    footer,
  });

  setTimeout(() => {
    input.focus();
    input.select();
    runSearch();
  }, 40);
}
