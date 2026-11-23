import { badge, clearEl, el, svgIcon } from "../core/dom.js";
import { Modal } from "./modal.js";

/**
 * 通用库级报告导出弹窗：
 * - 支持可选 Schema 多选分组筛选与布尔范围开关（如「仅包含已绑标表」）
 * - 默认选中「离线交互网页报告 (.html)」（内嵌单 Sheet .xlsx 导出能力）
 * - 并列提供「直接导出库级 Excel (.xlsx)」（单 Sheet 包含全部命中库表）
 * - 下方附轻量可重导入模板 / 铺底配置入口
 */
export function openExportReportModal({
  title = "导出库级报告",
  scopeBadge = "库级全量",
  scopeHint = "",
  getScopeSummary = null,
  filterGroups = [],
  toggles = [],
  options = [],
  secondaryActions = [],
} = {}) {
  if (!options.length) return null;

  let selectedId = options[0].id;
  let closed = false;

  const groups = (filterGroups || []).map((g) => ({
    ...g,
    items: Array.isArray(g.items) ? [...g.items] : [],
  }));
  const toggleList = Array.isArray(toggles) ? [...toggles] : [];

  const filterState = {};
  for (const g of groups) {
    filterState[g.id] = Array.isArray(g.initialSelected) ? [...g.initialSelected] : [];
  }
  for (const t of toggleList) {
    filterState[t.id] = t.defaultChecked !== false;
  }

  const getStateSnapshot = () => {
    const snap = {};
    for (const [k, v] of Object.entries(filterState)) {
      snap[k] = Array.isArray(v) ? [...v] : v;
    }
    return snap;
  };

  const cardBody = el("div", { className: "export-report-modal" });
  const scopeBoxMount = el("div");
  const filterBoxMount = el("div", { className: "export-filter-box" });
  const optionsMount = el("div", { className: "export-report-grid", role: "radiogroup" });
  const secondaryMount = el("div");

  const exportBtn = el("button", {
    type: "button",
    className: "btn btn-primary",
    text: "立即导出报告",
    onClick: () => {
      const chosen = options.find((o) => o.id === selectedId) || options[0];
      const snap = getStateSnapshot();
      Modal.close("export");
      if (chosen && typeof chosen.onExport === "function") {
        chosen.onExport(snap);
      }
    },
  });

  function renderScopeBox() {
    const summary =
      typeof getScopeSummary === "function"
        ? getScopeSummary(getStateSnapshot()) || {}
        : { badge: scopeBadge, badgeTone: "info", hint: scopeHint, isWarn: false };
    const bText = summary.badge || scopeBadge;
    const hText = summary.hint ?? scopeHint;
    if (!bText && !hText) {
      clearEl(scopeBoxMount);
      return;
    }
    clearEl(
      scopeBoxMount,
      el(
        "div",
        { className: `export-report-scope${summary.isWarn ? " is-warn" : ""}` },
        bText ? badge(bText, summary.badgeTone || "info") : null,
        hText ? el("span", { className: "export-report-scope-txt", text: hText }) : null
      )
    );
  }

  const groupRefs = new Map();

  function renderGroupSection(group) {
    const ref = groupRefs.get(group.id);
    if (!ref) return;
    const items = group.items || [];
    const selected = Array.isArray(filterState[group.id]) ? filterState[group.id] : [];
    const selectedSet = new Set(selected.map((x) => String(x).toLowerCase()));
    const onlyBound = filterState.onlyBoundTables !== false;
    const kw = ref.searchQuery.trim().toLowerCase();

    ref.searchInput.hidden = items.length <= 10;

    const hasBound = items.some((it) => Number(it.bound_count || 0) > 0);
    const hasUnbound = items.some((it) => Number(it.bound_count || 0) <= 0);
    ref.onlyBoundBtn.hidden = !(hasBound && hasUnbound);

    const visibleItems = kw
      ? items.filter((it) => String(it.name || "").toLowerCase().includes(kw))
      : items;

    if (!items.length) {
      clearEl(
        ref.chipsMount,
        el("div", {
          className: "export-filter-empty",
          text: group.emptyHint || "暂无已缓存的 Schema 目录（导出时将按全部或已绑标清单处理，也可点击右上角刷新目录）",
        })
      );
      return;
    }

    const totalBoundSum = items.reduce((s, it) => s + Number(it.bound_count || 0), 0);
    const totalAllSum = items.reduce((s, it) => s + Number(it.total_count || 0), 0);
    const allActive = selected.length === 0;
    const allChip = el(
      "button",
      {
        type: "button",
        className: `export-schema-chip${allActive ? " is-selected" : ""}`,
        onClick: () => {
          filterState[group.id] = [];
          renderGroupSection(group);
          renderScopeBox();
        },
      },
      el("span", { text: group.allLabel || `全部 Schema (${items.length})` }),
      totalAllSum > 0 || totalBoundSum > 0
        ? el("span", {
            className: "export-schema-chip-count",
            text: `${totalBoundSum}/${totalAllSum}`,
          })
        : null
    );

    const itemChips = visibleItems.map((it) => {
      const nameStr = String(it.name || "");
      const nameLow = nameStr.toLowerCase();
      const active = selectedSet.has(nameLow);
      const bCnt = Number(it.bound_count || 0);
      const tCnt = Number(it.total_count || 0);
      const dimmed = onlyBound && bCnt <= 0;

      return el(
        "button",
        {
          type: "button",
          className: `export-schema-chip${active ? " is-selected" : ""}${dimmed ? " is-dimmed" : ""}`,
          title:
            tCnt > 0 || bCnt > 0
              ? `Schema ${nameStr}：已绑标 ${bCnt} 张 / 共 ${tCnt} 张表`
              : `Schema ${nameStr}`,
          onClick: () => {
            const cur = Array.isArray(filterState[group.id]) ? [...filterState[group.id]] : [];
            if (cur.length === 0) {
              filterState[group.id] = [nameStr];
            } else {
              const idx = cur.findIndex((s) => String(s).toLowerCase() === nameLow);
              if (idx >= 0) {
                cur.splice(idx, 1);
              } else {
                cur.push(nameStr);
              }
              filterState[group.id] = cur;
            }
            renderGroupSection(group);
            renderScopeBox();
          },
        },
        el("span", { text: nameStr }),
        tCnt > 0 || bCnt > 0
          ? el("span", {
              className: "export-schema-chip-count",
              text: `${bCnt}/${tCnt}`,
            })
          : null
      );
    });

    if (!visibleItems.length) {
      clearEl(
        ref.chipsMount,
        allChip,
        el("span", { className: "export-filter-empty", text: "无匹配 Schema" })
      );
      return;
    }

    clearEl(ref.chipsMount, allChip, ...itemChips);
  }

  function buildFilterSection() {
    if (!groups.length && !toggleList.length) {
      filterBoxMount.hidden = true;
      return;
    }
    filterBoxMount.hidden = false;

    const groupNodes = groups.map((group) => {
      const searchInput = el("input", {
        type: "search",
        className: "mac-input mac-input-sm export-filter-search",
        placeholder: "过滤 Schema...",
        hidden: (group.items || []).length <= 10,
        onInput: (e) => {
          const ref = groupRefs.get(group.id);
          if (!ref) return;
          ref.searchQuery = e.target.value || "";
          renderGroupSection(group);
        },
      });

      const onlyBoundBtn = el("button", {
        type: "button",
        className: "btn btn-xs",
        text: "仅选含绑标",
        title: "快速选中所有至少有 1 张已绑标表的 Schema",
        hidden: true,
        onClick: () => {
          const boundNames = (group.items || [])
            .filter((it) => Number(it.bound_count || 0) > 0)
            .map((it) => String(it.name || ""))
            .filter(Boolean);
          if (boundNames.length) {
            filterState[group.id] = boundNames;
            renderGroupSection(group);
            renderScopeBox();
          }
        },
      });

      const refreshBtn =
        typeof group.onRefreshItems === "function"
          ? el("button", {
              type: "button",
              className: "btn btn-xs",
              text: "刷新目录",
              onClick: async () => {
                if (refreshBtn.disabled) return;
                refreshBtn.disabled = true;
                const origTxt = refreshBtn.textContent;
                refreshBtn.textContent = "刷新中...";
                try {
                  const nextItems = await group.onRefreshItems();
                  if (!closed && Array.isArray(nextItems)) {
                    group.items = [...nextItems];
                    renderGroupSection(group);
                    renderScopeBox();
                  }
                } finally {
                  if (!closed) {
                    refreshBtn.disabled = false;
                    refreshBtn.textContent = origTxt;
                  }
                }
              },
            })
          : null;

      const chipsMount = el("div", { className: "export-schema-chips" });
      groupRefs.set(group.id, {
        searchInput,
        searchQuery: "",
        onlyBoundBtn,
        chipsMount,
      });

      const sectionEl = el(
        "div",
        { className: "export-filter-group" },
        el(
          "div",
          { className: "export-filter-group-head" },
          el("span", { className: "export-filter-group-title", text: group.label || "筛选 Schema" }),
          el("div", { className: "export-filter-actions" }, searchInput, onlyBoundBtn, refreshBtn)
        ),
        chipsMount
      );
      renderGroupSection(group);
      return sectionEl;
    });

    const togglesMount = el(
      "div",
      { className: "export-filter-toggles", hidden: !toggleList.length },
      toggleList.map((t) => {
        const cb = el("input", {
          type: "checkbox",
          checked: Boolean(filterState[t.id]),
          onChange: (e) => {
            filterState[t.id] = Boolean(e.target.checked);
            for (const g of groups) renderGroupSection(g);
            renderScopeBox();
          },
        });
        return el(
          "label",
          { className: "export-filter-toggle" },
          cb,
          el(
            "span",
            { className: "export-filter-toggle-txt" },
            el("span", { className: "export-filter-toggle-label", text: t.label || "" }),
            t.desc ? el("span", { className: "export-filter-toggle-desc", text: t.desc }) : null
          )
        );
      })
    );

    clearEl(filterBoxMount, ...groupNodes, togglesMount);
  }

  function renderOptions() {
    clearEl(
      optionsMount,
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
              renderOptions();
            },
            onDblclick: () => {
              selectedId = opt.id;
              const snap = getStateSnapshot();
              Modal.close("export");
              if (typeof opt.onExport === "function") {
                opt.onExport(snap);
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
            opt.badge ? badge(opt.badge, opt.badgeTone || (active ? "success" : "neutral")) : null
          ),
          el("div", { className: "export-report-opt-desc", text: opt.desc || "" })
        );
      })
    );

    const chosen = options.find((o) => o.id === selectedId) || options[0];
    if (chosen) {
      exportBtn.textContent = chosen.ctaLabel || `导出 ${chosen.shortExt || ""}`.trim();
    }
  }

  function renderSecondary() {
    if (!secondaryActions.length) {
      clearEl(secondaryMount);
      return;
    }
    clearEl(
      secondaryMount,
      el(
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
                  const snap = getStateSnapshot();
                  Modal.close("secondary");
                  if (typeof act.onClick === "function") {
                    act.onClick(snap);
                  }
                },
              },
              svgIcon(act.icon || "download", 12),
              act.label
            )
          )
        )
      )
    );
  }

  buildFilterSection();
  renderScopeBox();
  renderOptions();
  renderSecondary();
  clearEl(cardBody, scopeBoxMount, filterBoxMount, optionsMount, secondaryMount);

  const hasFilters = groups.length > 0 || toggleList.length > 0;
  Modal.open({
    title,
    width: hasFilters ? "min(620px, 92vw)" : "560px",
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
    onClose: () => {
      closed = true;
    },
  });

  return {
    updateGroupItems(groupId, nextItems) {
      if (closed) return;
      const target = groups.find((g) => g.id === groupId);
      if (!target) return;
      target.items = Array.isArray(nextItems) ? [...nextItems] : [];
      const validSet = new Set(target.items.map((it) => String(it.name || "").toLowerCase()));
      if (Array.isArray(filterState[groupId]) && filterState[groupId].length) {
        filterState[groupId] = filterState[groupId].filter((n) =>
          validSet.has(String(n || "").toLowerCase())
        );
      }
      renderGroupSection(target);
      renderScopeBox();
    },
    rerender() {
      if (closed) return;
      for (const g of groups) renderGroupSection(g);
      renderScopeBox();
      renderOptions();
    },
    isClosed() {
      return closed;
    },
  };
}
