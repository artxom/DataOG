/**
 * Data.O.G 前端桌面应用插件注册中心 (app_registry.js)
 *
 * 3.0 模块化架构核心：
 * 新增独立业务桌面应用时，只需通过 `registerDesktopApp({...})` 声明应用清单与按需懒加载 `loader`，
 * 即可自动接入 macOS Dock 栏、红黄绿窗口生命周期、系统外观图标自定义、工作区切换自动清理与按需样式表注入，
 * 彻底免修改 `main.js`、`state.js`、`dom.js`、`settings_view.js` 与 `index.html`。
 */

import { registerIconPath } from "./dom.js";

const SAFE_APP_ID_RE = /^[a-z][a-z0-9_-]{1,31}$/;

const _appsById = new Map();
const _legacyRouteMap = new Map();
const _loadedStylesheets = new Set();
const _listeners = new Set();

/**
 * 实时同步的已注册桌面应用列表（向后兼容旧代码对 DESKTOP_APPS 的直接引用）
 * @type {Array<{id: string, title: string, defaultSummary: string, iconName: string, iconGradient: string, order: number, resetOnWorkspaceSwitch: boolean, stylesheets: string[]}>}
 */
export const DESKTOP_APPS = [];

function _rebuildSortedList() {
  const sorted = [..._appsById.values()].sort((a, b) => {
    const diff = (a.order ?? 100) - (b.order ?? 100);
    return diff !== 0 ? diff : a.id.localeCompare(b.id);
  });
  DESKTOP_APPS.splice(0, DESKTOP_APPS.length, ...sorted);
}

function _notifyListeners() {
  for (const fn of _listeners) {
    try {
      fn(DESKTOP_APPS);
    } catch (_err) {
      // 忽略单个监听异常
    }
  }
}

/**
 * 注册一个独立的 macOS 桌面应用插件
 * @param {object} spec
 * @param {string} spec.id 唯一应用 ID（须与后端 ModuleSpec.desktop_app_id 一致以支持自定义图标）
 * @param {string} spec.title Dock 与窗口标题
 * @param {() => (object | Promise<object>)} spec.loader 视图懒加载工厂函数（返回实现 mount/destroy/getHeaderParts 的实例）
 * @param {string} [spec.defaultSummary] 最小化抽屉默认摘要
 * @param {string} [spec.iconName] 图标名（默认同 id）
 * @param {string} [spec.iconPath] 可选自定义 SVG `<path d="...">`，自动注册至 dom.js
 * @param {string} [spec.iconGradient] 可选图标背景渐变色（新 3.0 应用无需修改 workbench.css）
 * @param {Record<string, Record<string, any>>} [spec.legacyRoutes] 可选旧路由别名到上下文的映射
 * @param {string[]} [spec.stylesheets] 可选应用专属 CSS 路径列表，首次打开应用时按需加载
 * @param {boolean} [spec.resetOnWorkspaceSwitch=true] 切换工作区时是否自动销毁缓存实例
 * @param {number} [spec.order=100] Dock 栏排序权重
 */
export function registerDesktopApp(spec) {
  if (!spec || typeof spec !== "object") {
    throw new Error("registerDesktopApp 需要传入应用清单对象");
  }
  const id = String(spec.id || "").trim().toLowerCase();
  const title = String(spec.title || "").trim();
  if (!id || !SAFE_APP_ID_RE.test(id) || !title || typeof spec.loader !== "function") {
    throw new Error(`无效的桌面应用注册清单: id=${id}, title=${title}`);
  }

  const iconName = String(spec.iconName || id).trim();
  if (spec.iconPath) {
    registerIconPath(iconName, spec.iconPath);
  }

  const manifest = {
    id,
    title,
    defaultSummary: String(spec.defaultSummary || title),
    iconName,
    iconGradient: String(spec.iconGradient || ""),
    stylesheets: Array.isArray(spec.stylesheets)
      ? spec.stylesheets.map((s) => String(s || "").trim()).filter(Boolean)
      : [],
    resetOnWorkspaceSwitch: spec.resetOnWorkspaceSwitch !== false,
    order: Number.isFinite(Number(spec.order)) ? Number(spec.order) : 100,
    loader: spec.loader,
  };

  _appsById.set(id, manifest);

  if (spec.legacyRoutes && typeof spec.legacyRoutes === "object") {
    for (const [alias, defaultCtx] of Object.entries(spec.legacyRoutes)) {
      if (alias) {
        _legacyRouteMap.set(alias, {
          appId: id,
          defaultCtx: defaultCtx && typeof defaultCtx === "object" ? { ...defaultCtx } : {},
        });
      }
    }
  }

  _rebuildSortedList();
  _notifyListeners();
  return manifest;
}

export function getDesktopApps() {
  return DESKTOP_APPS;
}

export function getDesktopApp(appId) {
  return _appsById.get(String(appId || "").trim()) || null;
}

export function getDefaultAppSummary(appId) {
  return getDesktopApp(appId)?.defaultSummary || "";
}

export function resolveAppRoute(targetId, context = null) {
  const mapped = _legacyRouteMap.get(String(targetId || "").trim());
  const appId = mapped ? mapped.appId : String(targetId || "").trim();
  const mergedContext = mapped
    ? { ...mapped.defaultCtx, ...(context || {}) }
    : context;
  if (!_appsById.has(appId)) {
    return null;
  }
  return { appId, context: mergedContext };
}

export function onDesktopAppsChanged(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

/**
 * 幂等按需加载应用声明的独立 CSS 样式表（免修改 index.html 与 workbench.css）
 */
export function ensureAppStylesheets(appId) {
  const manifest = getDesktopApp(appId);
  if (!manifest || !manifest.stylesheets.length) return;
  if (typeof document === "undefined" || !document.head) return;

  for (const href of manifest.stylesheets) {
    if (_loadedStylesheets.has(href)) continue;
    let exists = false;
    if (typeof document.querySelectorAll === "function") {
      const existingLinks = document.querySelectorAll('link[rel="stylesheet"]');
      for (const linkEl of existingLinks) {
        if (linkEl.getAttribute?.("href") === href) {
          exists = true;
          break;
        }
      }
    }
    if (!exists && document.head?.children) {
      for (const child of document.head.children) {
        if (
          String(child.tagName || "").toLowerCase() === "link" &&
          child.getAttribute?.("rel") === "stylesheet" &&
          child.getAttribute?.("href") === href
        ) {
          exists = true;
          break;
        }
      }
    }
    if (!exists) {
      const link = document.createElement("link");
      link.setAttribute("rel", "stylesheet");
      link.setAttribute("href", href);
      link.setAttribute("data-app-style", appId);
      document.head.appendChild(link);
    }
    _loadedStylesheets.add(href);
  }
}

/**
 * 异步按需实例化指定桌面应用（自动注入专属样式表并动态加载 ES Module）
 */
export async function loadAppInstance(appId) {
  const manifest = getDesktopApp(appId);
  if (!manifest) {
    throw new Error(`未找到已注册的桌面应用: ${appId}`);
  }
  ensureAppStylesheets(manifest.id);
  return await manifest.loader();
}

const _loadedExternalEntryUrls = new Set();

/**
 * 动态加载后端已激活的 4.0 外置桌面应用插件入口 (如 /plugins/<id>/register.js)
 * @param {Array<{plugin_id: string, entry_url: string}>} plugins
 * @returns {Promise<{loaded: string[], failed: Array<{plugin_id: string, error: string}>}>}
 */
export async function loadExternalPluginApps(plugins = []) {
  const loaded = [];
  const failed = [];
  if (!Array.isArray(plugins) || !plugins.length) {
    return { loaded, failed };
  }
  for (const item of plugins) {
    const pluginId = String(item?.plugin_id || "").trim();
    const entryUrl = String(item?.entry_url || "").trim();
    if (!pluginId || !entryUrl) continue;
    if (!entryUrl.startsWith("/plugins/") || entryUrl.includes("..") || entryUrl.includes("\\")) {
      failed.push({ plugin_id: pluginId, error: `非法的插件前端入口路径: ${entryUrl}` });
      continue;
    }
    if (_loadedExternalEntryUrls.has(entryUrl)) {
      loaded.push(pluginId);
      continue;
    }
    try {
      await import(entryUrl);
      _loadedExternalEntryUrls.add(entryUrl);
      loaded.push(pluginId);
    } catch (err) {
      failed.push({
        plugin_id: pluginId,
        error: err?.message || String(err),
      });
    }
  }
  return { loaded, failed };
}
