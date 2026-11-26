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

import { authApi, diagnosticsApi, platformApi } from "./core/platform_api.js";
import { perfMonitor } from "./core/perf_monitor.js";
import {
  getDefaultAppSummary,
  getDesktopApp,
  getDesktopApps,
  loadAppInstance,
  loadExternalPluginApps,
} from "./core/app_registry.js";
import {
  badge,
  clearEl,
  el,
  renderAppSquircle,
  renderBrandMark,
  svgIcon,
} from "./core/dom.js";
import { store } from "./core/state.js";
import { AuthGateController } from "./ui/auth_gate.js";
import { bindDialogLightDismissFallback } from "./ui/modal.js";
import { Toast } from "./ui/toast.js";

const THEME_MODES = [
  { value: "light dark", label: "自动" },
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
];

const THEME_STORAGE_KEY = "dataog_color_scheme";

const appInstances = new Map();
let activeWindowDom = null;
let activeWindowAppId = null;
let windowMountSeq = 0;
let isAuthorized = false;
let authGateController = null;

function getAppTitle(appId) {
  return getDesktopApp(appId)?.title || "桌面";
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

  renderMacDock();
}

function syncDockVisibility() {
  const dockEl = document.getElementById("os-dock");
  const hoverZone = document.getElementById("dock-hover-zone");
  if (!dockEl) return;

  const activeId = store.state.activeApp;
  const isFullMax = Boolean(activeId && store.state.maximizedApps.has(activeId));
  dockEl.classList.toggle("is-autohide", isFullMax);
  if (hoverZone) {
    hoverZone.classList.toggle("is-active", isFullMax);
  }
  if (!isFullMax) {
    dockEl.classList.remove("is-peek");
  }
}

function initDockAutoHideHover() {
  const dockEl = document.getElementById("os-dock");
  const hoverZone = document.getElementById("dock-hover-zone");
  if (!dockEl || !hoverZone) return;

  let hideTimer = null;
  const showPeek = () => {
    clearTimeout(hideTimer);
    if (dockEl.classList.contains("is-autohide")) {
      dockEl.classList.add("is-peek");
    }
  };
  const scheduleHide = () => {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      dockEl.classList.remove("is-peek");
    }, 220);
  };

  hoverZone.addEventListener("mouseenter", showPeek);
  hoverZone.addEventListener("mouseleave", scheduleHide);
  dockEl.addEventListener("mouseenter", showPeek);
  dockEl.addEventListener("mouseleave", scheduleHide);
}

function renderMacDock() {
  const dockEl = document.getElementById("os-dock");
  if (!dockEl) return;

  const appearance = store.state.appearance || {};
  const activeId = store.state.activeApp;
  const minimizedList = store.state.minimizedApps || [];
  const minimizedSet = new Set(minimizedList);

  const appNodes = getDesktopApps().map((appItem) => {
    const isActive = activeId === appItem.id;
    const isRunning =
      isActive ||
      minimizedSet.has(appItem.id) ||
      appInstances.has(appItem.id);

    return el(
      "button",
      {
        type: "button",
        className: `dock-item ${isActive ? "is-active" : ""}`.trim(),
        dataset: { appId: appItem.id },
        "aria-label": appItem.title,
        onClick: () => store.openApp(appItem.id),
      },
      el("span", { className: "dock-tooltip", text: appItem.title }),
      el("span", { className: "dock-icon-box" }, renderAppSquircle(appItem.id, appearance, "lg", appItem)),
      el("span", {
        className: `dock-dot ${isRunning ? "is-visible" : ""} ${isActive ? "is-focused" : ""}`.trim(),
        title: isRunning ? "运行中" : "",
      })
    );
  });

  const minimizedNodes = [];
  if (minimizedList.length > 0) {
    minimizedNodes.push(el("span", { className: "dock-separator", role: "separator" }));
    for (const appId of minimizedList) {
      const appManifest = getDesktopApp(appId);
      const sub = store.state.appSummaries[appId] || getDefaultAppSummary(appId) || "";
      const tip = sub ? `${getAppTitle(appId)} · ${sub}` : getAppTitle(appId);
      minimizedNodes.push(
        el(
          "button",
          {
            type: "button",
            className: "dock-item dock-minimized-item",
            dataset: { drawerApp: appId },
            title: `点击恢复「${getAppTitle(appId)}」工作现场`,
            "aria-label": `恢复最小化窗口 ${getAppTitle(appId)}`,
            onClick: () => store.openApp(appId),
          },
          el("span", { className: "dock-tooltip", text: tip }),
          el(
            "span",
            { className: "dock-minimized-tile" },
            el("span", { className: "dock-mini-win-chrome" }, el("span"), el("span"), el("span")),
            el("span", { className: "dock-mini-app-badge" }, renderAppSquircle(appId, appearance, "sm", appManifest))
          ),
          el("span", { className: "dock-dot is-visible" })
        )
      );
    }
  }

  clearEl(dockEl, ...appNodes, ...minimizedNodes);
  syncDockVisibility();
}

function syncMenubarIndicators() {
  const activeTitleEl = document.getElementById("menubar-active-app");
  if (activeTitleEl) {
    if (!isAuthorized) {
      activeTitleEl.textContent = "登录授权";
    } else {
      activeTitleEl.textContent = store.state.activeApp
        ? getAppTitle(store.state.activeApp)
        : "桌面";
    }
  }

  const wsBtn = document.getElementById("btn-menubar-workspace");
  const wsLabel = document.getElementById("menubar-workspace-label");
  const activeWs = store.state.activeWorkspace;
  if (wsBtn && wsLabel) {
    if (isAuthorized && activeWs) {
      const wsName = activeWs.name || "默认工作区";
      wsBtn.hidden = false;
      wsLabel.textContent = wsName;
      wsBtn.title = `当前工作区：${wsName}（点击切换、导出或合并）`;
    } else {
      wsBtn.hidden = true;
    }
  }

  const licBtn = document.getElementById("btn-menubar-license");
  const licDot = document.getElementById("menubar-license-dot");
  const licLabel = document.getElementById("menubar-license-label");
  const lic = store.state.license;
  if (licBtn && licDot && licLabel) {
    if (isAuthorized && lic && lic.authorized) {
      licBtn.hidden = false;
      const days = Number(lic.days_remaining ?? 0);
      licDot.className = `dot ${days <= 7 ? "dot-warn" : "dot-success"}`;
      licLabel.textContent = lic.expires_date ? `授权至 ${lic.expires_date}` : "已授权";
    } else {
      licBtn.hidden = true;
    }
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

  const ver = String(store.state.version || "").trim();
  if (ver) {
    const homeBtn = document.getElementById("btn-menubar-home");
    if (homeBtn) homeBtn.title = `显示桌面 (Data.O.G v${ver})`;
    const reloadBtn = document.getElementById("btn-menubar-reload");
    if (reloadBtn) reloadBtn.title = `当前版本 v${ver} · 点击一键无缓存重载最新界面`;
  }
}

/* =========================================================================
   3. macOS 应用窗口宿主
   ========================================================================= */

async function syncWindowHost() {
  const hostEl = document.getElementById("window-host");
  if (!hostEl) return;

  const appId = store.state.activeApp;
  if (!appId) {
    windowMountSeq += 1;
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

  const reqSeq = ++windowMountSeq;
  let inst = appInstances.get(appId);
  const isFreshInstance = !inst;
  if (!inst) {
    let loadedInst = null;
    try {
      loadedInst = await loadAppInstance(appId);
    } catch (err) {
      Toast.error(`加载模块「${getAppTitle(appId)}」失败: ${err.message}`);
      if (reqSeq === windowMountSeq && store.state.activeApp === appId) {
        store.closeApp(appId);
      }
      return;
    }
    // 防止用户快速切换 Dock / 关闭 / 锁定时异步懒加载竞态污染缓存或覆盖新窗口
    if (reqSeq !== windowMountSeq || store.state.activeApp !== appId) {
      loadedInst?.destroy?.();
      return;
    }
    inst = loadedInst;
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

  if (isFreshInstance || !inst._mountedOnce || ctx) {
    try {
      await inst.mount(winBody, ctx, refreshHeader);
      inst._mountedOnce = true;
    } catch (err) {
      Toast.error(`初始化模块「${getAppTitle(appId)}」失败: ${err.message}`);
      if (!inst._mountedOnce) {
        inst.destroy?.();
        if (appInstances.get(appId) === inst) {
          appInstances.delete(appId);
        }
        if (reqSeq === windowMountSeq && store.state.activeApp === appId) {
          store.closeApp(appId);
        }
      }
      return;
    }
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
    if (!isAuthorized) return;
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
    if (!isAuthorized) return;
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
  const diagBtn = document.getElementById("btn-open-diagnostics");
  const exportDiagBtn = document.getElementById("btn-export-diagnostics");
  const refreshBtn = document.getElementById("btn-refresh-logs");
  const clearBtn = document.getElementById("btn-clear-logs");
  const closeBtn = document.getElementById("logs-modal-close");
  if (!dialog || !listEl) return;

  bindDialogLightDismissFallback(dialog);
  closeBtn?.addEventListener("click", () => dialog.close("cancel"));
  diagBtn?.addEventListener("click", () => {
    if (dialog.open) dialog.close("diagnostics");
    if (isAuthorized) {
      store.openApp("settings", { section: "diagnostics" });
    }
  });
  exportDiagBtn?.addEventListener("click", async () => {
    if (!isAuthorized) return;
    exportDiagBtn.disabled = true;
    try {
      const clientSnapshot = perfMonitor.collectClientSnapshot();
      const fname = await diagnosticsApi.exportBundle({
        client_snapshot: clientSnapshot,
        include_service_log: true,
        include_execution_logs: true,
      });
      Toast.success(`已导出脱敏诊断日志包：${fname}`);
    } catch (err) {
      Toast.error(err.message || "导出诊断日志包失败");
    } finally {
      exportDiagBtn.disabled = false;
    }
  });

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
    if (!isAuthorized) return;
    if (!dialog.open) {
      dialog.returnValue = "";
      dialog.showModal();
    }
    await fetchLatestLogs();
  });
}

async function hardReloadWorkbench(triggerBtn = null) {
  if (triggerBtn) {
    triggerBtn.disabled = true;
  }
  try {
    if (typeof window !== "undefined" && "caches" in window && window.caches?.keys) {
      const cacheKeys = await window.caches.keys();
      await Promise.all(cacheKeys.map((key) => window.caches.delete(key)));
    }
  } catch (_err) {
    // 忽略部分浏览器或非安全上下文下的 Cache Storage 限制
  }
  const url = new URL(window.location.href);
  url.searchParams.set("_reload", String(Date.now()));
  window.location.replace(url.toString());
}

/* =========================================================================
   5. 启动引导 (bootstrapWorkbench)
   ========================================================================= */

export async function bootstrapWorkbench() {
  try {
    if (typeof window !== "undefined" && window.location?.href) {
      const cleanUrl = new URL(window.location.href);
      if (cleanUrl.searchParams.has("_reload")) {
        cleanUrl.searchParams.delete("_reload");
        const nextHref = cleanUrl.search
          ? `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`
          : `${cleanUrl.pathname}${cleanUrl.hash}`;
        window.history?.replaceState?.(null, "", nextHref);
      }
    }
  } catch (_err) {
    // 忽略受限历史 API 异常
  }

  // 静态挂载菜单栏小图标
  const wsIconSlot = document.getElementById("menubar-icon-workspace");
  if (wsIconSlot) clearEl(wsIconSlot, svgIcon("layers", 12));
  const searchIconSlot = document.getElementById("menubar-icon-search");
  if (searchIconSlot) clearEl(searchIconSlot, svgIcon("search", 13));
  const logsIconSlot = document.getElementById("menubar-icon-logs");
  if (logsIconSlot) clearEl(logsIconSlot, svgIcon("logs", 13));
  const reloadIconSlot = document.getElementById("menubar-icon-reload");
  if (reloadIconSlot) clearEl(reloadIconSlot, svgIcon("refresh", 13));

  const reloadBtn = document.getElementById("btn-menubar-reload");
  reloadBtn?.addEventListener("click", () => {
    hardReloadWorkbench(reloadBtn);
  });

  initThemeToggle();
  startMenubarClock();
  initSpotlightDialog();
  initLogsDialog();
  initDockAutoHideHover();

  const dockEl = document.getElementById("os-dock");
  const winHostEl = document.getElementById("window-host");
  const loginGateEl = document.getElementById("os-login-gate");

  const enterLoginGate = async (statusPayload = null) => {
    isAuthorized = false;
    windowMountSeq += 1;
    store.state.activeApp = null;
    if (winHostEl) {
      winHostEl.hidden = true;
      clearEl(winHostEl);
    }
    if (dockEl) dockEl.hidden = true;
    activeWindowDom = null;
    activeWindowAppId = null;

    let status = statusPayload;
    if (!status) {
      try {
        status = await authApi.status();
      } catch (_e) {
        status = { authorized: false, status: "UNACTIVATED", status_message: "请完成离线激活码验证" };
      }
    }
    if (status?.appearance) {
      store.updateAppearance(status.appearance);
    }
    store.updateLicense(status);
    syncMenubarIndicators();
    authGateController?.render(status);
  };

  const enterAuthorizedDesktop = async (licInfo = null) => {
    isAuthorized = true;
    authGateController?.hide();
    if (dockEl) dockEl.hidden = false;
    if (licInfo) {
      store.updateLicense(licInfo);
    }
    const boot = await platformApi.bootstrap();
    store.applyBootstrap(boot);
    const plugRes = await loadExternalPluginApps(boot.plugins || []);
    if (plugRes.failed?.length) {
      Toast.warn(`部分外置插件前端加载失败 (${plugRes.failed.map((f) => f.plugin_id).join(", ")})`);
    }
    syncMenubarIndicators();
  };

  authGateController = new AuthGateController({
    mountEl: loginGateEl,
    onAuthenticated: async (lic) => {
      await enterAuthorizedDesktop(lic);
    },
    onLocked: async (lic) => {
      await enterLoginGate(lic);
    },
  });

  window.addEventListener("dataog:auth-required", () => {
    if (isAuthorized) {
      enterLoginGate();
    }
  });

  document.getElementById("btn-menubar-home")?.addEventListener("click", () => {
    if (!isAuthorized) return;
    store.showDesktop();
  });
  document.getElementById("btn-menubar-workspace")?.addEventListener("click", () => {
    if (!isAuthorized) return;
    store.openApp("settings", { section: "workspaces" });
  });
  document.getElementById("btn-menubar-ai")?.addEventListener("click", () => {
    if (!isAuthorized) return;
    store.openApp("settings", { section: "llm" });
  });
  document.getElementById("btn-menubar-license")?.addEventListener("click", () => {
    if (!isAuthorized) return;
    authGateController?.openLicenseDetailModal();
  });

  // 红色关闭按钮 (✕) 销毁回调：清理对应模块实例并重置摘要
  store.onAppDestroy((appId) => {
    const inst = appInstances.get(appId);
    if (inst) {
      inst.destroy?.();
      appInstances.delete(appId);
    }
    const defSummary = getDefaultAppSummary(appId);
    if (defSummary) {
      store.state.appSummaries[appId] = defSummary;
    }
  });

  store.subscribe((_state, reason) => {
    if (reason === "bootstrap" || reason === "appearance" || reason === "apps_changed") {
      syncDesktopAppearance();
    }
    if (reason === "window") {
      syncWindowHost();
      renderMacDock();
      syncMenubarIndicators();
    } else if (reason === "summary") {
      renderMacDock();
    } else if (reason === "workspace_switched") {
      // 切换或合并工作区后，销毁所有声明为工作区态的已缓存模块实例（自动覆盖全部 2.X / 3.0 业务应用）
      const keptAppIds = new Set();
      for (const appManifest of getDesktopApps()) {
        if (!appManifest.resetOnWorkspaceSwitch) {
          keptAppIds.add(appManifest.id);
          continue;
        }
        const staleId = appManifest.id;
        const staleInst = appInstances.get(staleId);
        if (staleInst) {
          staleInst.destroy?.();
          appInstances.delete(staleId);
        }
        const defSummary = getDefaultAppSummary(staleId);
        if (defSummary) {
          store.state.appSummaries[staleId] = defSummary;
        }
        store.state.maximizedApps.delete(staleId);
      }
      store.state.minimizedApps = (store.state.minimizedApps || []).filter((id) =>
        keptAppIds.has(id)
      );
      if (store.state.activeApp && !keptAppIds.has(store.state.activeApp)) {
        windowMountSeq += 1;
        activeWindowDom = null;
        activeWindowAppId = null;
        syncWindowHost();
      }
      renderMacDock();
      syncMenubarIndicators();
    } else if (reason === "workspace_meta" || reason === "llm" || reason === "logs" || reason === "bootstrap" || reason === "license") {
      syncMenubarIndicators();
    }
  });

  // 初次渲染背景，随后探测离线授权状态：未激活则先拦截至登录门禁页，已激活自动进入桌面
  syncDesktopAppearance();
  syncMenubarIndicators();

  try {
    const authRes = await authApi.status();
    if (authRes.appearance) {
      store.updateAppearance(authRes.appearance);
    }
    if (authRes.authorized) {
      await enterAuthorizedDesktop(authRes.license || authRes);
    } else {
      await enterLoginGate(authRes.license || authRes);
    }
  } catch (err) {
    Toast.error(`初始化失败: ${err.message}`);
    await enterLoginGate();
  }
}

bootstrapWorkbench();
