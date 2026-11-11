/**
 * DataOG 全局共享前端状态总线 (WorkbenchStore)
 * 负责维护 ConnectionHub 连接列表、MetadataHub 目录树缓存、全局日志与跨模块导航上下文
 */

class WorkbenchStore {
  constructor() {
    this.state = {
      connections: [],
      catalogTrees: {},
      drivers: [],
      driverTemplates: [],
      javaRuntime: { available: false, message: "" },
      modules: [],
      logs: [],
      activeModule: "connections",
      moduleContext: null,
    };
    this.listeners = new Set();
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(reason = "update") {
    for (const fn of this.listeners) {
      try {
        fn(this.state, reason);
      } catch (err) {
        console.error("[WorkbenchStore] listener error:", err);
      }
    }
  }

  applyBootstrap(data) {
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
    this.state.modules = data.modules || [];
    this.state.logs = data.logs || [];
    if (this.state.connections.length > 0 && this.state.activeModule === "connections" && !this._initialRouted) {
      this._initialRouted = true;
    } else {
      this._initialRouted = true;
    }
    this.emit("bootstrap");
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

  navigateTo(moduleId, context = null) {
    this.state.activeModule = moduleId;
    this.state.moduleContext = context;
    this.emit("navigate");
  }

  consumeModuleContext() {
    const ctx = this.state.moduleContext;
    this.state.moduleContext = null;
    return ctx;
  }
}

export const store = new WorkbenchStore();
