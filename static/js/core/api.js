/**
 * Data.O.G 统一后端通信客户端 (FastAPI 微内核与各独立业务模块 API)
 */

import { downloadBlob } from "./dom.js";

async function parseJsonOrThrow(resp) {
  let payload = null;
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    payload = await resp.json();
  } else {
    const txt = await resp.text();
    try {
      payload = JSON.parse(txt);
    } catch (_e) {
      payload = { ok: resp.ok, error: txt || `HTTP ${resp.status}` };
    }
  }
  if (!resp.ok || (payload && payload.ok === false)) {
    const msg =
      (payload && (payload.error || payload.detail || payload.message)) ||
      `请求失败 (HTTP ${resp.status})`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return payload;
}

export async function requestJson(url, { method = "GET", body = undefined } = {}) {
  const opts = {
    method,
    headers: { Accept: "application/json" },
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  return parseJsonOrThrow(resp);
}

export async function uploadFormData(url, formData) {
  const resp = await fetch(url, {
    method: "POST",
    body: formData,
  });
  return parseJsonOrThrow(resp);
}

export async function downloadApiFile(url, { method = "GET", body = undefined, fallbackName = "export.bin" } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    await parseJsonOrThrow(resp);
  }
  const disp = resp.headers.get("content-disposition") || "";
  let filename = fallbackName;
  const utfMatch = disp.match(/filename\*=UTF-8''([^;]+)/i);
  const plainMatch = disp.match(/filename="?([^";]+)"?/i);
  if (utfMatch && utfMatch[1]) {
    filename = decodeURIComponent(utfMatch[1]);
  } else if (plainMatch && plainMatch[1]) {
    filename = plainMatch[1];
  }
  const blob = await resp.blob();
  downloadBlob(blob, filename);
  return filename;
}

export const platformApi = {
  bootstrap: () => requestJson("/api/platform/bootstrap"),

  // 外观壁纸与软件/模块图标本地持久化 (AssetStore)
  getAppearance: () => requestJson("/api/platform/appearance"),
  updateAppearance: (payload) =>
    requestJson("/api/platform/appearance", { method: "PUT", body: payload }),
  uploadAppearanceAsset: (file, slot) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("slot", slot);
    return uploadFormData("/api/platform/appearance/upload", fd);
  },
  resetAppearance: (slot) =>
    requestJson("/api/platform/appearance/reset", { method: "POST", body: { slot } }),

  // AI 大模型统一接入中枢 (LLMHub)
  listLlmProviders: () => requestJson("/api/platform/llm/providers"),
  saveLlmProvider: (payload) =>
    requestJson("/api/platform/llm/providers", { method: "POST", body: payload }),
  updateLlmProvider: (providerId, payload) =>
    requestJson(`/api/platform/llm/providers/${encodeURIComponent(providerId)}`, {
      method: "PUT",
      body: payload,
    }),
  deleteLlmProvider: (providerId) =>
    requestJson(`/api/platform/llm/providers/${encodeURIComponent(providerId)}`, {
      method: "DELETE",
    }),
  testLlmProvider: (payload) =>
    requestJson("/api/platform/llm/test", { method: "POST", body: payload }),
  fetchLlmModels: (payload) =>
    requestJson("/api/platform/llm/models", { method: "POST", body: payload }),

  // 驱动管理 (DriverManager)
  listDrivers: () => requestJson("/api/platform/drivers"),
  uploadDriver: (file, { name = "", dbTypeHint = "CUSTOM_JDBC", driverClassHint = "" } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("name", name);
    fd.append("db_type_hint", dbTypeHint);
    fd.append("driver_class_hint", driverClassHint);
    return uploadFormData("/api/platform/drivers/upload", fd);
  },
  deleteDriver: (driverId) =>
    requestJson(`/api/platform/drivers/${encodeURIComponent(driverId)}`, { method: "DELETE" }),

  // 数据源连接档案 (ConnectionHub)
  listConnections: () => requestJson("/api/platform/connections"),
  saveConnection: (payload) =>
    requestJson("/api/platform/connections", { method: "POST", body: payload }),
  updateConnection: (connId, payload) =>
    requestJson(`/api/platform/connections/${encodeURIComponent(connId)}`, { method: "PUT", body: payload }),
  deleteConnection: (connId) =>
    requestJson(`/api/platform/connections/${encodeURIComponent(connId)}`, { method: "DELETE" }),
  testUnsavedConnection: (payload) =>
    requestJson("/api/platform/connections/test", { method: "POST", body: payload }),
  testSavedConnection: (connId) =>
    requestJson(`/api/platform/connections/${encodeURIComponent(connId)}/test`, { method: "POST" }),

  // 元数据中枢 (MetadataHub)
  getMetadataTree: (connId, autoRefresh = false) =>
    requestJson(
      `/api/platform/metadata/${encodeURIComponent(connId)}/tree?auto_refresh=${autoRefresh ? "true" : "false"}`
    ),
  refreshMetadata: (connId, schemaName = null) =>
    requestJson(`/api/platform/metadata/${encodeURIComponent(connId)}/refresh`, {
      method: "POST",
      body: { schema_name: schemaName || null },
    }),
  getTableDetail: (connId, schemaName, tableName, forceRefresh = false) => {
    const qs = new URLSearchParams({
      schema_name: schemaName || "",
      table_name: tableName || "",
      force_refresh: forceRefresh ? "true" : "false",
    });
    return requestJson(`/api/platform/metadata/${encodeURIComponent(connId)}/table?${qs.toString()}`);
  },
  searchMetadata: (keyword, connectionId = "", limit = 100) => {
    const qs = new URLSearchParams({ keyword: keyword || "", limit: String(limit) });
    if (connectionId) qs.set("connection_id", connectionId);
    return requestJson(`/api/platform/metadata/search?${qs.toString()}`);
  },

  // 查询预览中心 (QueryEngine)
  executeSql: (payload) =>
    requestJson("/api/platform/query/execute", { method: "POST", body: payload }),
  executeReadonlySql: (payload) =>
    requestJson("/api/platform/query/execute-readonly", { method: "POST", body: payload }),
  previewTable: (connectionId, schemaName, tableName, limit = 200) => {
    const qs = new URLSearchParams({
      connection_id: connectionId,
      schema_name: schemaName || "",
      table_name: tableName || "",
      limit: String(limit),
    });
    return requestJson(`/api/platform/query/preview?${qs.toString()}`);
  },
  exportQuery: (payload, fallbackName = "query_result.csv") =>
    downloadApiFile("/api/platform/query/export", {
      method: "POST",
      body: payload,
      fallbackName,
    }),

  getLogs: (limit = 150) => requestJson(`/api/platform/logs?limit=${limit}`),
  clearLogs: () => requestJson("/api/platform/logs/clear", { method: "POST" }),
};

export const standardsApi = {
  list: ({ keyword = "", regCategory = "", dataCategory = "", page = 1, pageSize = 50 } = {}) => {
    const qs = new URLSearchParams({
      keyword: keyword || "",
      reg_category: regCategory || "",
      data_category: dataCategory || "",
      page: String(page || 1),
      page_size: String(pageSize || 50),
    });
    return requestJson(`/api/platform/standards?${qs.toString()}`);
  },
  lookup: (keyword = "", limit = 30) => {
    const qs = new URLSearchParams({ keyword: keyword || "", limit: String(limit) });
    return requestJson(`/api/platform/standards/lookup?${qs.toString()}`);
  },
  downloadTemplate: () =>
    downloadApiFile("/api/platform/standards/template.xlsx", {
      fallbackName: "DataOG_数据标准字典导入模板.xlsx",
    }),
  exportDictionary: ({ keyword = "", regCategory = "", dataCategory = "" } = {}) => {
    const qs = new URLSearchParams();
    if (keyword) qs.set("keyword", keyword);
    if (regCategory) qs.set("reg_category", regCategory);
    if (dataCategory) qs.set("data_category", dataCategory);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return downloadApiFile(`/api/platform/standards/export.xlsx${suffix}`, {
      fallbackName: "DataOG_企业数据标准字典导出.xlsx",
    });
  },
  uploadDictionary: (file, mode = "upsert") => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("mode", mode || "upsert");
    return uploadFormData("/api/platform/standards/upload", fd);
  },
  createItem: (payload) =>
    requestJson("/api/platform/standards/items", { method: "POST", body: payload }),
  updateItem: (stdCode, payload) =>
    requestJson(`/api/platform/standards/items/${encodeURIComponent(stdCode)}`, {
      method: "PUT",
      body: payload,
    }),
  deleteItem: (stdCode) =>
    requestJson(`/api/platform/standards/items/${encodeURIComponent(stdCode)}`, {
      method: "DELETE",
    }),
  clear: () => requestJson("/api/platform/standards/clear", { method: "POST" }),
};

export const migratorApi = {
  getState: () => requestJson("/api/modules/standard_migrator/state"),
  bindConnection: (connectionId, autoSync = true) =>
    requestJson("/api/modules/standard_migrator/bind-connection", {
      method: "POST",
      body: { connection_id: connectionId, auto_sync: autoSync },
    }),
  sync: () => requestJson("/api/modules/standard_migrator/sync", { method: "POST" }),
  uploadChecklist: (file, { defaultSchema = "", defaultTable = "" } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    if (defaultSchema) fd.append("default_schema", defaultSchema);
    if (defaultTable) fd.append("default_table", defaultTable);
    return uploadFormData("/api/modules/standard_migrator/checklist/upload", fd);
  },
  loadFromDb: (payload) =>
    requestJson("/api/modules/standard_migrator/checklist/load-from-db", {
      method: "POST",
      body: payload,
    }),
  clearItems: () => requestJson("/api/modules/standard_migrator/checklist/clear", { method: "POST" }),
  downloadTemplate: () =>
    downloadApiFile("/api/modules/standard_migrator/checklist/template.xlsx", {
      fallbackName: "Data.O.G_字段贯标模板.xlsx",
    }),
  exportReport: () =>
    downloadApiFile("/api/modules/standard_migrator/checklist/report.xlsx", {
      fallbackName: "Data.O.G_贯标报告.xlsx",
    }),
  executeSingle: (itemId) =>
    requestJson(`/api/modules/standard_migrator/execute/item/${encodeURIComponent(itemId)}`, {
      method: "POST",
    }),
  executeBatch: (payload) =>
    requestJson("/api/modules/standard_migrator/execute/batch", { method: "POST", body: payload }),
  previewRebuild: (payload) =>
    requestJson("/api/modules/standard_migrator/rebuild/preview", { method: "POST", body: payload }),
  executeRebuild: (payload) =>
    requestJson("/api/modules/standard_migrator/rebuild/execute", { method: "POST", body: payload }),

  // 第 2 步：字段绑标 + 落标核验 + 对照模板
  getBindingsOverview: (connectionId = "", formatProfile = "AUTO") => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (formatProfile) qs.set("format_profile", formatProfile);
    return requestJson(`/api/modules/standard_migrator/bindings/overview?${qs.toString()}`);
  },
  getTableBindings: (
    connectionId,
    schemaName,
    tableName,
    formatProfile = "AUTO",
    forceRefresh = false
  ) => {
    const qs = new URLSearchParams({
      connection_id: connectionId,
      schema_name: schemaName || "",
      table_name: tableName || "",
      format_profile: formatProfile || "AUTO",
      force_refresh: forceRefresh ? "true" : "false",
    });
    return requestJson(`/api/modules/standard_migrator/bindings/table?${qs.toString()}`);
  },
  saveBindings: (payload) =>
    requestJson("/api/modules/standard_migrator/bindings/save", { method: "POST", body: payload }),
  autoMatchBindings: (payload) =>
    requestJson("/api/modules/standard_migrator/bindings/auto-match", { method: "POST", body: payload }),
  checkCompliance: (payload) =>
    requestJson("/api/modules/standard_migrator/bindings/check-compliance", {
      method: "POST",
      body: payload,
    }),
  downloadBindingTemplate: ({ connectionId = "", schemaName = "", tables = "" } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (schemaName) qs.set("schema_name", schemaName);
    if (tables) qs.set("tables", tables);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return downloadApiFile(`/api/modules/standard_migrator/bindings/template.xlsx${suffix}`, {
      fallbackName: "DataOG_字段与数据标准对照模板.xlsx",
    });
  },
  uploadBindings: (
    file,
    { connectionId, defaultSchema = "", mode = "merge", formatProfile = "AUTO" }
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("connection_id", connectionId);
    fd.append("default_schema", defaultSchema || "");
    fd.append("mode", mode || "merge");
    fd.append("format_profile", formatProfile || "AUTO");
    return uploadFormData("/api/modules/standard_migrator/bindings/upload", fd);
  },
  exportBindingReport: ({ connectionId = "", formatProfile = "AUTO", tables = "" } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (formatProfile) qs.set("format_profile", formatProfile);
    if (tables) qs.set("tables", tables);
    return downloadApiFile(`/api/modules/standard_migrator/bindings/report.xlsx?${qs.toString()}`, {
      fallbackName: "DataOG_单库字段落标检查报告.xlsx",
    });
  },
  stageMigration: (payload) =>
    requestJson("/api/modules/standard_migrator/bindings/stage-migration", {
      method: "POST",
      body: payload,
    }),
};

export const crossApi = {
  getOverview: (tableKey = "", forceRefresh = false, extra = {}) => {
    const qs = new URLSearchParams();
    if (tableKey) qs.set("table_key", tableKey);
    if (forceRefresh) qs.set("force_refresh", "true");
    if (extra.sourceSchema) qs.set("source_schema", extra.sourceSchema);
    if (extra.sourceTable) qs.set("source_table", extra.sourceTable);
    if (extra.targetSchema) qs.set("target_schema", extra.targetSchema);
    if (extra.targetTable) qs.set("target_table", extra.targetTable);
    if (extra.formatProfile) qs.set("format_profile", extra.formatProfile);
    return requestJson(`/api/modules/cross_comparator/overview?${qs.toString()}`);
  },
  bindRole: (payload) =>
    requestJson("/api/modules/cross_comparator/bind-role", { method: "POST", body: payload }),
  saveBindings: (payload) =>
    requestJson("/api/modules/cross_comparator/bindings/save", { method: "POST", body: payload }),
  savePairings: (payload) =>
    requestJson("/api/modules/cross_comparator/pairings/save", { method: "POST", body: payload }),
  downloadPairingTemplate: ({
    tableKey = "",
    sourceSchema = "",
    sourceTable = "",
    targetSchema = "",
    targetTable = "",
  } = {}) => {
    const qs = new URLSearchParams();
    if (tableKey) qs.set("table_key", tableKey);
    if (sourceSchema) qs.set("source_schema", sourceSchema);
    if (sourceTable) qs.set("source_table", sourceTable);
    if (targetSchema) qs.set("target_schema", targetSchema);
    if (targetTable) qs.set("target_table", targetTable);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return downloadApiFile(`/api/modules/cross_comparator/pairings/template.xlsx${suffix}`, {
      fallbackName: "DataOG_跨环境字段映射铺底模板.xlsx",
    });
  },
  uploadPairings: (file, formatProfile = "AUTO") => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("format_profile", formatProfile || "AUTO");
    return uploadFormData("/api/modules/cross_comparator/pairings/upload", fd);
  },
  uploadStandard: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return uploadFormData("/api/modules/cross_comparator/standard/upload", fd);
  },
  syncFromMigrator: () =>
    requestJson("/api/modules/cross_comparator/standard/sync-from-migrator", { method: "POST" }),
  clearStandard: () =>
    requestJson("/api/modules/cross_comparator/standard/clear", { method: "POST" }),
  exportJson: () =>
    downloadApiFile("/api/modules/cross_comparator/bindings/export.json", {
      fallbackName: "Data.O.G_跨环境绑定包.json",
    }),
  exportExcel: (tableKey = "") => {
    const qs = tableKey ? `?table_key=${encodeURIComponent(tableKey)}` : "";
    return downloadApiFile(`/api/modules/cross_comparator/bindings/export.xlsx${qs}`, {
      fallbackName: "Data.O.G_三栏比对报告.xlsx",
    });
  },
  pushToMigrator: (stdTableKey = "", formatProfile = "AUTO") =>
    requestJson("/api/modules/cross_comparator/push-to-migrator", {
      method: "POST",
      body: { std_table_key: stdTableKey || null, format_profile: formatProfile || "AUTO" },
    }),
};

export const ddlApi = {
  getOptions: () => requestJson("/api/modules/ddl_translator/options"),
  smartNaming: (payload) =>
    requestJson("/api/modules/ddl_translator/naming", { method: "POST", body: payload }),
  translate: (payload) =>
    requestJson("/api/modules/ddl_translator/translate", { method: "POST", body: payload }),
};

export const polyglotApi = ddlApi;

export const explorerApi = {
  listSessions: (connectionId = "") => {
    const qs = connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "";
    return requestJson(`/api/modules/data_explorer/sessions${qs}`);
  },
  createSession: (payload) =>
    requestJson("/api/modules/data_explorer/sessions", { method: "POST", body: payload }),
  getSession: (sessionId) =>
    requestJson(`/api/modules/data_explorer/sessions/${encodeURIComponent(sessionId)}`),
  updateSession: (sessionId, payload) =>
    requestJson(`/api/modules/data_explorer/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      body: payload,
    }),
  deleteSession: (sessionId, connectionId = "") => {
    const qs = connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "";
    return requestJson(
      `/api/modules/data_explorer/sessions/${encodeURIComponent(sessionId)}${qs}`,
      { method: "DELETE" }
    );
  },
  explore: (payload) =>
    requestJson("/api/modules/data_explorer/explore", { method: "POST", body: payload }),
};
