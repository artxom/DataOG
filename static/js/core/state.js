/**
 * Data.O.G 全局共享前端状态与 macOS 桌面窗口状态机 (WorkbenchStore)
 * 窗口控制三键语义：
 * - 关闭 (closeApp): 销毁该模块的运行时实例并重置状态，返回桌面
 * - 最小化 (minimizeApp): 保留模块实例与全部编辑状态，停靠至屏幕右下角最小化小抽屉
 * - 最大化 (toggleMaximize): 在常规留白窗口态与全屏工作态间切换
 */

export const DESKTOP_APPS = [
  { id: "settings", title: "系统配置" },
  { id: "explorer", title: "数据探查" },
  { id: "standard", title: "数据标准" },
  { id: "polyglot", title: "多邻国" },
];

const LEGACY_ROUTE_MAP = {
  connections: { appId: "settings", defaultCtx: { section: "connections" } },
  sql_console: { appId: "explorer", defaultCtx: {} },
  standard_migrator: { appId: "standard", defaultCtx: { mode: "migrator" } },
  cross_comparator: { appId: "standard", defaultCtx: { mode: "comparator" } },
  ddl_translator: { appId: "polyglot", defaultCtx: { mode: "dialect" } },
};

class WorkbenchStore {
  constructor() {
    this.state = {
      brandName: "Data.O.G",
      connections: [],
      catalogTrees: {},
      drivers: [],
      driverTemplates: [],
      javaRuntime: { available: false, message: "" },
      appearance: {
        wallpaper_id: "default",
        wallpaper_fit: "cover",
        custom_wallpaper_url: "",
        brand_icon_url: "",
        app_icons: {},
      },
      llmProviders: [],
      llmModels: [],
      modules: [],
      logs: [],

      // 桌面窗口状态机
      activeApp: null, // null 表示显示纯净桌面；否则为 settings | explorer | standard | polyglot
      minimizedApps: [], // 已最小化至右下角抽屉的应用 ID 列表（严格保留内存状态）
      maximizedApps: new Set(),
      appSummaries: {
        settings: "外观 · 连接 · AI",
        explorer: "SQL 查询",
        standard: "贯标 · 比对",
        polyglot: "命名 · 方言",
      },
      moduleContext: null,
    };
    this.listeners = new Set();
    this.destroyHandlers = new Set();
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onAppDestroy(fn) {
    this.destroyHandlers.add(fn);
    return () => this.destroyHandlers.delete(fn);
  }

  emit(reason = "update") {
    for (const fn of this.listeners) {
      try {
        fn(this.state, reason);
      } catch (_err) {
        // 避免单个监听异常阻断状态广播
      }
    }
  }

  applyBootstrap(data) {
    this.state.brandName = data.brand_name || "Data.O.G";
    this.state.connections = data.connections || [];
    this.state.catalogTrees = data.catalog_trees || {};
    this.state.drivers = data.drivers || [];
    this.state.driverTemplates = data.driver_templates || [];
    const rawJre = data.java_runtime || {};
    const jreReady = Boolean(rawJre.jdbc_extension_ready ?? rawJre.available);
    this.state.javaRuntime = {
      ...rawJre,
      jdbc_extension_ready: jreReady,
      available: jreReady,
      note: rawJre.note || rawJre.message || "",
      message: rawJre.note || rawJre.message || "",
    };
    if (data.appearance) {
      this.state.appearance = data.appearance;
    }
    this.state.llmProviders = data.llm_providers || [];
    this.state.llmModels = data.llm_models || [];
    this.state.modules = data.modules || [];
    this.state.logs = data.logs || [];
    this.emit("bootstrap");
  }

  updateAppearance(appearance) {
    if (!appearance) return;
    this.state.appearance = appearance;
    this.emit("appearance");
  }

  updateLlmState(providers, availableModels) {
    if (Array.isArray(providers)) {
      this.state.llmProviders = providers;
    }
    if (Array.isArray(availableModels)) {
      this.state.llmModels = availableModels;
    }
    this.emit("llm");
  }

  updateConnections(connections, singleCatalogTree = null, connId = "") {
    this.state.connections = connections || [];
    if (singleCatalogTree && connId) {
      this.state.catalogTrees[connId] = singleCatalogTree;
    }
    this.emit("connections");
  }

  updateCatalogTree(connId, tree) {
    if (!connId || !tree) return;
    this.state.catalogTrees[connId] = {
      schema_count: (tree.schemas || []).length,
      table_count: (tree.tables || []).length,
      schemas: tree.schemas || [],
      tables: tree.tables || [],
    };
    this.emit("catalog");
  }

  updateDrivers(drivers) {
    this.state.drivers = drivers || [];
    this.emit("drivers");
  }

  updateLogs(logs) {
    this.state.logs = logs || [];
    this.emit("logs");
  }

  setAppSummary(appId, summaryText) {
    if (!appId || !summaryText) return;
    if (this.state.appSummaries[appId] === summaryText) return;
    this.state.appSummaries[appId] = summaryText;
    this.emit("summary");
  }

  /**
   * 打开或恢复指定桌面应用模块
   */
  openApp(targetId, context = null) {
    const mapped = LEGACY_ROUTE_MAP[targetId];
    const appId = mapped ? mapped.appId : targetId;
    const mergedContext = mapped
      ? { ...mapped.defaultCtx, ...(context || {}) }
      : context;

    if (!DESKTOP_APPS.some((a) => a.id === appId)) return;

    // 若当前已有另一个活动窗口打开，自动将其最小化至右下角抽屉以保护现场状态
    const cur = this.state.activeApp;
    if (cur && cur !== appId && !this.state.minimizedApps.includes(cur)) {
      this.state.minimizedApps = [...this.state.minimizedApps, cur];
    }

    this.state.minimizedApps = this.state.minimizedApps.filter((id) => id !== appId);
    this.state.activeApp = appId;
    this.state.moduleContext = mergedContext;
    this.emit("window");
  }

  /**
   * 兼容公共组件 (DatabaseTree 等) 的跨模块路由调用
   */
  navigateTo(moduleId, context = null) {
    this.openApp(moduleId, context);
  }

  /**
   * 黄色最小化按钮 (−)：保留当前工作状态，缩略进屏幕右下角毛玻璃小抽屉并返回桌面
   */
  minimizeApp(appId = this.state.activeApp) {
    if (!appId) return;
    if (!this.state.minimizedApps.includes(appId)) {
      this.state.minimizedApps = [...this.state.minimizedApps, appId];
    }
    if (this.state.activeApp === appId) {
      this.state.activeApp = null;
    }
    this.emit("window");
  }

  /**
   * 点击左上角 Data.O.G 显示桌面（若有打开窗口则挂起进右下角抽屉）
   */
  showDesktop() {
    if (this.state.activeApp) {
      this.minimizeApp(this.state.activeApp);
    } else {
      this.emit("window");
    }
  }

  /**
   * 红色关闭按钮 (✕)：回到桌面并彻底销毁重置当前工具的运行时状态
   */
  closeApp(appId = this.state.activeApp) {
    if (!appId) return;
    this.state.minimizedApps = this.state.minimizedApps.filter((id) => id !== appId);
    this.state.maximizedApps.delete(appId);
    if (this.state.activeApp === appId) {
      this.state.activeApp = null;
    }
    for (const fn of this.destroyHandlers) {
      try {
        fn(appId);
      } catch (_e) {
        // ignore
      }
    }
    this.emit("window");
  }

  /**
   * 绿色最大化按钮 (+)：切换窗口常规留白态 / 沉浸铺满态
   */
  toggleMaximize(appId = this.state.activeApp) {
    if (!appId) return;
    if (this.state.maximizedApps.has(appId)) {
      this.state.maximizedApps.delete(appId);
    } else {
      this.state.maximizedApps.add(appId);
    }
    this.emit("window");
  }

  consumeModuleContext() {
    const ctx = this.state.moduleContext;
    this.state.moduleContext = null;
    return ctx;
  }
}

export const store = new WorkbenchStore();
