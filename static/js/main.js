/**
 * Data.O.G — macOS 桌面级一体化数据工程工作台主入口 (main.js)
 * 架构职责：
 * 1. 顶部 28px 毛玻璃菜单栏（品牌归位、当前活动应用、AI 状态胶囊、⌘K Spotlight、执行日志、深浅色切换、实时时钟）
 * 2. 个性化壁纸与超椭圆桌面图标渲染（支持本地自定义图片持久化覆盖）
 * 3. macOS 红黄绿三键窗口管理器：
 *    - 红色关闭 (✕): 销毁并重置当前模块运行时状态，返回纯净桌面
 *    - 黄色最小化 (−): 保留完整 DOM 与编辑状态，缩略停靠至右下角毛玻璃小抽屉，点击即刻恢复现场
 *    - 绿色最大化 (+): 在留白桌面窗口与全屏沉浸画布间平滑切换
 */

import { platformApi } from "./core/api.js";
import {
  badge,
  clearEl,
  el,
  renderAppSquircle,
  renderBrandMark,
  svgIcon,
} from "./core/dom.js";
import { DESKTOP_APPS, store } from "./core/state.js";
import { bindDialogLightDismissFallback } from "./ui/modal.js";
import { Toast } from "./ui/toast.js";
import { PolyglotView } from "./views/ddl_translator_view.js";
import { SettingsView } from "./views/settings_view.js";
import { SqlConsoleView } from "./views/sql_console_view.js";
import { StandardView } from "./views/standard_view.js";

const DEFAULT_SUMMARIES = {
  settings: "外观 · 连接 · AI",
  explorer: "SQL 查询",
  standard: "贯标 · 比对",
  polyglot: "命名 · 方言",
};

const THEME_MODES = [
  { value: "light dark", label: "自动" },
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
];

const THEME_STORAGE_KEY = "dataog_color_scheme";

const appInstances = new Map();
let activeWindowDom = null;
let activeWindowAppId = null;

function createAppInstance(appId) {
  switch (appId) {
    case "settings":
      return new SettingsView();
    case "explorer":
      return new SqlConsoleView();
    case "standard":
      return new StandardView();
    case "polyglot":
      return new PolyglotView();
    default:
      return new SettingsView();
  }
}

function getAppTitle(appId) {
  return DESKTOP_APPS.find((a) => a.id === appId)?.title || "桌面";
}

/* =========================================================================
   1. 深浅色主题模式（遵循 Modern Web Guidance: 同步 <meta name="color-scheme">）
   ========================================================================= */

function applyColorScheme(schemeValue) {
  const valid = THEME_MODES.find((m) => m.value === schemeValue) || THEME_MODES[0];
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) {
    meta.setAttribute("content", valid.value);
  }
  const labelEl = document.getElementById("menubar-theme-label");
  if (labelEl) {
    labelEl.textContent = valid.label;
  }
  try {
    localStorage.setItem(THEME_STORAGE_KEY, valid.value);
  } catch (_e) {
    // 忽略隐私模式存储异常
  }
}

function initThemeToggle() {
  let saved = "light dark";
  try {
    saved = localStorage.getItem(THEME_STORAGE_KEY) || "light dark";
  } catch (_e) {
    saved = "light dark";
  }
  applyColorScheme(saved);

  document.getElementById("btn-toggle-theme")?.addEventListener("click", () => {
    const meta = document.querySelector('meta[name="color-scheme"]');
    const cur = meta?.getAttribute("content") || "light dark";
    const idx = THEME_MODES.findIndex((m) => m.value === cur);
    const next = THEME_MODES[(idx + 1) % THEME_MODES.length];
    applyColorScheme(next.value);
  });
}

/* =========================================================================
   2. 顶部菜单栏、时钟与桌面外观（壁纸 + 自定义图标）
   ========================================================================= */

function startMenubarClock() {
  const clockEl = document.getElementById("menubar-clock");
  if (!clockEl) return;
  const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const tick = () => {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    clockEl.textContent = `${weekdays[now.getDay()]} ${hh}:${mm}`;
  };
  tick();
  setInterval(tick, 15000);
}

function syncDesktopAppearance() {
  const app = store.state.appearance || {};
  const osRoot = document.getElementById("os-root");
  if (osRoot) {
    const wpId = app.wallpaper_id || "default";
    osRoot.dataset.wallpaper = wpId;
    if (wpId === "custom" && app.custom_wallpaper_url) {
      osRoot.style.backgroundImage = `url("${app.custom_wallpaper_url}")`;
      osRoot.style.backgroundSize = app.wallpaper_fit === "contain" ? "contain" : "cover";
      osRoot.classList.add("has-photo-wallpaper");
    } else {
      osRoot.style.backgroundImage = "";
      osRoot.style.backgroundSize = "";
      osRoot.classList.remove("has-photo-wallpaper");
    }
  }

  const faviconEl = document.getElementById("dynamic-favicon");
  if (faviconEl) {
    faviconEl.setAttribute("href", app.brand_icon_url || "/favicon.ico");
  }

  const menuBrandIcon = document.getElementById("menubar-brand-icon");
  if (menuBrandIcon) {
    clearEl(menuBrandIcon, renderBrandMark(app, "sm"));
  }

  const heroEmblem = document.getElementById("desktop-brand-emblem");
  if (heroEmblem) {
    clearEl(heroEmblem, renderBrandMark(app, "lg"));
  }

  renderDesktopLaunchers();
}

function renderDesktopLaunchers() {
  const gridEl = document.getElementById("desktop-icon-grid");
  if (!gridEl) return;

  const appearance = store.state.appearance || {};
  const activeId = store.state.activeApp;
  const minimizedSet = new Set(store.state.minimizedApps || []);

  const nodes = DESKTOP_APPS.map((appItem) => {
    const isRunning =
      activeId === appItem.id ||
      minimizedSet.has(appItem.id) ||
      appInstances.has(appItem.id);

    return el(
      "button",
      {
        type: "button",
        className: "app-launcher",
        dataset: { appId: appItem.id },
        onClick: () => store.openApp(appItem.id),
      },
      renderAppSquircle(appItem.id, appearance, "lg"),
      el("span", { className: "app-launcher-label", text: appItem.title }),
      isRunning ? el("span", { className: "app-running-dot", title: "运行中" }) : null
    );
  });

  clearEl(gridEl, nodes);
}

function syncMenubarIndicators() {
  const activeTitleEl = document.getElementById("menubar-active-app");
  if (activeTitleEl) {
    activeTitleEl.textContent = store.state.activeApp
      ? getAppTitle(store.state.activeApp)
      : "桌面";
  }

  const aiDot = document.getElementById("menubar-ai-dot");
  const aiLabel = document.getElementById("menubar-ai-label");
  const readyModels = (store.state.llmModels || []).filter((m) => m.ready || m.has_api_key);
  if (aiDot && aiLabel) {
    if (readyModels.length > 0) {
      const def = readyModels.find((m) => m.is_default) || readyModels[0];
      aiDot.className = "dot dot-success";
      aiLabel.textContent = def.model;
    } else {
      aiDot.className = "dot dot-muted";
      aiLabel.textContent = "AI 未配密钥";
    }
  }

  const logBadge = document.getElementById("menubar-log-badge");
  if (logBadge) {
    const errCount = (store.state.logs || []).filter((l) => l.level === "ERROR").length;
    if (errCount > 0) {
      logBadge.hidden = false;
      logBadge.textContent = String(errCount);
    } else {
      logBadge.hidden = true;
    }
  }
}

/* =========================================================================
   3. macOS 应用窗口宿主与右下角毛玻璃最小化抽屉
   ========================================================================= */

function renderMinimizedDrawer() {
  const drawerEl = document.getElementById("minimized-drawer");
  if (!drawerEl) return;

  const list = store.state.minimizedApps || [];
  if (!list.length) {
    drawerEl.hidden = true;
    clearEl(drawerEl);
    return;
  }

  const appearance = store.state.appearance || {};
  const chips = list.map((appId) =>
    el(
      "button",
      {
        type: "button",
        className: "drawer-chip",
        dataset: { drawerApp: appId },
        title: `点击恢复「${getAppTitle(appId)}」工作现场`,
        onClick: () => store.openApp(appId),
      },
      renderAppSquircle(appId, appearance, "sm"),
      el(
        "span",
        { className: "drawer-chip-meta" },
        el("span", { className: "drawer-chip-title", text: getAppTitle(appId) }),
        el("span", {
          className: "drawer-chip-sub",
          text: store.state.appSummaries[appId] || DEFAULT_SUMMARIES[appId] || "",
        })
      )
    )
  );

  drawerEl.hidden = false;
  clearEl(drawerEl, chips);
}

async function syncWindowHost() {
  const hostEl = document.getElementById("window-host");
  if (!hostEl) return;

  const appId = store.state.activeApp;
  if (!appId) {
    hostEl.hidden = true;
    // 清空宿主但绝不销毁 appInstances 里的实例引用，确保最小化窗口 DOM 状态完整保留在内存中
    clearEl(hostEl);
    activeWindowDom = null;
    activeWindowAppId = null;
    return;
  }

  hostEl.hidden = false;
  const isMax = store.state.maximizedApps.has(appId);
  const ctx = store.consumeModuleContext();

  // 若当前已是同一窗口挂载态且仅切换最大化/还原（无新上下文），仅原地切换 CSS 类以触发平滑过渡动画
  if (activeWindowDom && activeWindowAppId === appId && !ctx && hostEl.contains(activeWindowDom)) {
    activeWindowDom.className = `app-window ${isMax ? "maximized" : "normal"}`;
    return;
  }

  let inst = appInstances.get(appId);
  const isFreshInstance = !inst;
  if (!inst) {
    inst = createAppInstance(appId);
    appInstances.set(appId, inst);
  }

  const centerSlot = el("div", { className: "win-titlebar-center" });
  const rightSlot = el("div", { className: "win-titlebar-right" });

  const refreshHeader = () => {
    const parts = inst.getHeaderParts ? inst.getHeaderParts() : { center: null, right: null };
    clearEl(centerSlot, parts?.center || null);
    clearEl(rightSlot, parts?.right || null);
  };

  const trafficLights = el(
    "div",
    { className: "traffic-lights", role: "group", "aria-label": "窗口控制" },
    el("button", {
      type: "button",
      className: "tl-btn tl-close",
      title: "关闭并重置当前工具状态",
      "aria-label": "关闭并重置状态",
      text: "✕",
      onClick: (e) => {
        e.stopPropagation();
        store.closeApp(appId);
      },
    }),
    el("button", {
      type: "button",
      className: "tl-btn tl-min",
      title: "最小化至右下角小抽屉（保留当前工作状态）",
      "aria-label": "最小化至抽屉",
      text: "−",
      onClick: (e) => {
        e.stopPropagation();
        store.minimizeApp(appId);
      },
    }),
    el("button", {
      type: "button",
      className: "tl-btn tl-max",
      title: "最大化 / 还原窗口",
      "aria-label": "最大化或还原窗口",
      text: "+",
      onClick: (e) => {
        e.stopPropagation();
        store.toggleMaximize(appId);
      },
    })
  );

  const titlebar = el(
    "header",
    {
      className: "win-titlebar",
      onDblclick: (e) => {
        if (e.target.closest("button, select, input, label")) return;
        store.toggleMaximize(appId);
      },
    },
    el(
      "div",
      { className: "win-titlebar-left" },
      trafficLights,
      el("span", { className: "win-app-title", text: getAppTitle(appId) })
    ),
    centerSlot,
    rightSlot
  );

  const winBody = el("div", { className: "win-body" });
  const winEl = el(
    "article",
    {
      className: `app-window ${isMax ? "maximized" : "normal"}`,
      dataset: { windowApp: appId },
    },
    titlebar,
    winBody
  );

  activeWindowDom = winEl;
  activeWindowAppId = appId;
  clearEl(hostEl, winEl);

  if (isFreshInstance || ctx) {
    inst._mountedOnce = true;
    await inst.mount(winBody, ctx, refreshHeader);
  } else {
    // 从右下角最小化小抽屉恢复：原封不动挂回内存中的 inst.root DOM 树，100% 保留未保存输入、光标与滚动位置
    inst.refreshHeader = refreshHeader;
    clearEl(winBody, inst.root);
    refreshHeader();
  }
}

/* =========================================================================
   4. Spotlight 全局元数据秒级检索 (⌘K) & 运行日志弹窗
   ========================================================================= */

function initSpotlightDialog() {
  const dialog = document.getElementById("spotlight-modal");
  const input = document.getElementById("spotlight-input");
  const resultsEl = document.getElementById("spotlight-results");
  const closeBtn = document.getElementById("spotlight-close");
  if (!dialog || !input || !resultsEl) return;

  bindDialogLightDismissFallback(dialog);
  closeBtn?.addEventListener("click", () => dialog.close("cancel"));

  let timer = null;

  const renderResults = (tables = [], columns = []) => {
    const rows = [];
    for (const t of tables) {
      rows.push(
        el(
          "button",
          {
            type: "button",
            className: "spotlight-item",
            onClick: () => {
              dialog.close("pick");
              store.openApp("explorer", {
                action: "preview_table",
                connectionId: t.connection_id,
                schemaName: t.schema_name,
                tableName: t.table_name,
              });
            },
          },
          el(
            "div",
            { style: { display: "flex", alignItems: "center", gap: "8px", minWidth: "0" } },
            svgIcon(t.object_type === "VIEW" ? "view" : "table", 15),
            el(
              "div",
              { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
              el("span", { className: "mono", style: { fontWeight: "600" }, text: `${t.schema_name}.${t.table_name}` }),
              t.table_comment
                ? el("span", { style: { marginLeft: "8px", color: "var(--os-muted)", fontSize: "11.5px" }, text: t.table_comment })
                : null
            )
          ),
          badge(t.object_type || "TABLE", "neutral")
        )
      );
    }

    for (const c of columns) {
      rows.push(
        el(
          "button",
          {
            type: "button",
            className: "spotlight-item",
            onClick: () => {
              dialog.close("pick");
              store.openApp("explorer", {
                action: "preview_table",
                connectionId: c.connection_id,
                schemaName: c.schema_name,
                tableName: c.table_name,
              });
            },
          },
          el(
            "div",
            { style: { display: "flex", alignItems: "center", gap: "8px", minWidth: "0" } },
            badge("列", "info"),
            el(
              "div",
              { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
              el("span", {
                className: "mono",
                style: { fontWeight: "600" },
                text: `${c.schema_name}.${c.table_name}.${c.column_name}`,
              }),
              c.column_comment
                ? el("span", { style: { marginLeft: "8px", color: "var(--os-muted)", fontSize: "11.5px" }, text: c.column_comment })
                : null
            )
          ),
          el("span", { className: "mono", style: { fontSize: "11px", color: "var(--os-muted)" }, text: c.data_type || "" })
        )
      );
    }

    if (!rows.length) {
      clearEl(resultsEl, el("div", { className: "grid-empty", style: { padding: "24px" }, text: "无匹配的缓存库表或字段" }));
    } else {
      clearEl(resultsEl, rows);
    }
  };

  const triggerSearch = async () => {
    const kw = input.value.trim();
    try {
      const res = await platformApi.searchMetadata(kw, "", 50);
      renderResults(res.tables || [], res.columns || []);
    } catch (err) {
      clearEl(resultsEl, el("div", { className: "grid-empty", text: err.message }));
    }
  };

  const openSpotlight = () => {
    if (!dialog.open) {
      dialog.returnValue = "";
      dialog.showModal();
    }
    input.focus();
    input.select();
    triggerSearch();
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(triggerSearch, 150);
  });

  document.getElementById("btn-menubar-search")?.addEventListener("click", openSpotlight);

  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (dialog.open) {
        dialog.close("toggle");
      } else {
        openSpotlight();
      }
    }
  });
}

function initLogsDialog() {
  const dialog = document.getElementById("logs-modal");
  const listEl = document.getElementById("drawer-log-list");
  const filterSelect = document.getElementById("log-level-filter");
  const refreshBtn = document.getElementById("btn-refresh-logs");
  const clearBtn = document.getElementById("btn-clear-logs");
  const closeBtn = document.getElementById("logs-modal-close");
  if (!dialog || !listEl) return;

  bindDialogLightDismissFallback(dialog);
  closeBtn?.addEventListener("click", () => dialog.close("cancel"));

  const renderLogItems = () => {
    const lvl = filterSelect?.value || "ALL";
    const logs = (store.state.logs || []).filter((item) => lvl === "ALL" || item.level === lvl);
    if (!logs.length) {
      clearEl(listEl, el("div", { className: "grid-empty", text: "暂无执行日志" }));
      return;
    }
    const entries = logs.map((item) => {
      const tone =
        item.level === "ERROR" ? "danger" : item.level === "WARN" ? "warn" : "info";
      return el(
        "div",
        { className: "log-entry" },
        el(
          "div",
          { className: "log-entry-head" },
          badge(item.level || "INFO", tone),
          el("strong", { text: item.category || "系统" }),
          el("span", { style: { color: "var(--os-muted)", marginLeft: "auto" }, text: item.created_at || "" })
        ),
        el("div", { text: item.message || "" }),
        item.sql_text
          ? el("pre", {
              style: {
                margin: "2px 0 0 0",
                padding: "6px 8px",
                borderRadius: "5px",
                background: "var(--os-win-bg)",
                border: "1px solid var(--os-border)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
                color: "var(--os-text-secondary)",
              },
              text: item.sql_text,
            })
          : null
      );
    });
    clearEl(listEl, entries);
  };

  const fetchLatestLogs = async () => {
    try {
      const res = await platformApi.getLogs(150);
      store.updateLogs(res.logs || []);
      renderLogItems();
    } catch (err) {
      Toast.error(err.message);
    }
  };

  filterSelect?.addEventListener("change", renderLogItems);
  refreshBtn?.addEventListener("click", fetchLatestLogs);
  clearBtn?.addEventListener("click", async () => {
    try {
      await platformApi.clearLogs();
      store.updateLogs([]);
      renderLogItems();
      Toast.success("日志已清空");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  document.getElementById("btn-menubar-logs")?.addEventListener("click", async () => {
    if (!dialog.open) {
      dialog.returnValue = "";
      dialog.showModal();
    }
    await fetchLatestLogs();
  });
}

/* =========================================================================
   5. 启动引导 (bootstrapWorkbench)
   ========================================================================= */

export async function bootstrapWorkbench() {
  // 静态挂载菜单栏小图标
  const searchIconSlot = document.getElementById("menubar-icon-search");
  if (searchIconSlot) clearEl(searchIconSlot, svgIcon("search", 13));
  const logsIconSlot = document.getElementById("menubar-icon-logs");
  if (logsIconSlot) clearEl(logsIconSlot, svgIcon("logs", 13));

  initThemeToggle();
  startMenubarClock();
  initSpotlightDialog();
  initLogsDialog();

  document.getElementById("btn-menubar-home")?.addEventListener("click", () => {
    store.showDesktop();
  });
  document.getElementById("btn-menubar-ai")?.addEventListener("click", () => {
    store.openApp("settings", { section: "llm" });
  });

  // 红色关闭按钮 (✕) 销毁回调：清理对应模块实例并重置摘要
  store.onAppDestroy((appId) => {
    const inst = appInstances.get(appId);
    if (inst) {
      inst.destroy?.();
      appInstances.delete(appId);
    }
    if (DEFAULT_SUMMARIES[appId]) {
      store.state.appSummaries[appId] = DEFAULT_SUMMARIES[appId];
    }
  });

  store.subscribe((_state, reason) => {
    if (reason === "bootstrap" || reason === "appearance") {
      syncDesktopAppearance();
    }
    if (reason === "window") {
      syncWindowHost();
      renderMinimizedDrawer();
      renderDesktopLaunchers();
      syncMenubarIndicators();
    } else if (reason === "summary") {
      renderMinimizedDrawer();
    } else if (reason === "llm" || reason === "logs" || reason === "bootstrap") {
      syncMenubarIndicators();
    }
  });

  // 初次渲染空态桌面，随后拉取平台 Bootstrap 数据
  syncDesktopAppearance();
  syncMenubarIndicators();

  try {
    const boot = await platformApi.bootstrap();
    store.applyBootstrap(boot);
  } catch (err) {
    Toast.error(`初始化失败: ${err.message}`);
  }
}

bootstrapWorkbench();
