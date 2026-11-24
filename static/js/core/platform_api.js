/**
 * Data.O.G 平台公共底座 API 客户端 (platform_api.js)
 *
 * 仅覆盖微内核底座能力：认证授权、任务流、外观资产、AI 模型、JDBC 驱动、
 * 数据源连接、元数据目录、通用只读查询、团队共享工作区与企业标准字典。
 */

import {
  downloadApiFile,
  requestJson,
  uploadFormData,
  withRequestOpts,
} from "./http.js";

export const authApi = {
  status: () => requestJson("/api/platform/auth/status", { cache: "no-store" }),
  activate: (payload) =>
    requestJson("/api/platform/auth/activate", { method: "POST", body: payload }),
  lock: () => requestJson("/api/platform/auth/lock", { method: "POST" }),
};

export const tasksApi = {
  get: (taskId) =>
    requestJson(`/api/platform/tasks/${encodeURIComponent(taskId)}`, { cache: "no-store" }),
  cancel: (taskId) =>
    requestJson(`/api/platform/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),
  streamUrl: (taskId) => `/api/platform/tasks/${encodeURIComponent(taskId)}/stream`,
};

export const platformApi = {
  bootstrap: () => requestJson("/api/platform/bootstrap", { cache: "no-store" }),

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
  updateConnectionStandardFormat: (connId, standardFormat) =>
    requestJson(`/api/platform/connections/${encodeURIComponent(connId)}/standard-format`, {
      method: "PUT",
      body: { standard_format: standardFormat || "AUTO" },
    }),
  deleteConnection: (connId) =>
    requestJson(`/api/platform/connections/${encodeURIComponent(connId)}`, { method: "DELETE" }),
  testUnsavedConnection: (payload) =>
    requestJson("/api/platform/connections/test", { method: "POST", body: payload }),
  testSavedConnection: (connId) =>
    requestJson(`/api/platform/connections/${encodeURIComponent(connId)}/test`, { method: "POST" }),

  // 元数据中枢 (MetadataHub)
  getMetadataTree: (connId, autoRefresh = false, opts = {}) =>
    requestJson(
      `/api/platform/metadata/${encodeURIComponent(connId)}/tree?auto_refresh=${autoRefresh ? "true" : "false"}`,
      withRequestOpts({}, opts)
    ),
  refreshMetadata: (connId, schemaName = null, opts = {}) =>
    requestJson(
      `/api/platform/metadata/${encodeURIComponent(connId)}/refresh`,
      withRequestOpts({ method: "POST", body: { schema_name: schemaName || null } }, opts)
    ),
  refreshMetadataTree: (connectionId, schemaName = null, opts = {}) =>
    requestJson(
      `/api/platform/metadata/${encodeURIComponent(connectionId)}/refresh`,
      withRequestOpts({ method: "POST", body: { schema_name: schemaName || null } }, opts)
    ),
  getTableDetail: (connId, schemaName, tableName, forceRefresh = false, opts = {}) => {
    const qs = new URLSearchParams({
      schema_name: schemaName || "",
      table_name: tableName || "",
      force_refresh: forceRefresh ? "true" : "false",
    });
    return requestJson(
      `/api/platform/metadata/${encodeURIComponent(connId)}/table?${qs.toString()}`,
      withRequestOpts({}, opts)
    );
  },
  searchMetadata: (keyword, connectionId = "", limit = 100) => {
    const qs = new URLSearchParams({ keyword: keyword || "", limit: String(limit) });
    if (connectionId) qs.set("connection_id", connectionId);
    return requestJson(`/api/platform/metadata/search?${qs.toString()}`);
  },

  // 查询预览中心 (QueryEngine)
  executeSql: (payload, opts = {}) =>
    requestJson("/api/platform/query/execute", withRequestOpts({ method: "POST", body: payload }, opts)),
  executeQuery: (payload, opts = {}) =>
    requestJson("/api/platform/query/execute", withRequestOpts({ method: "POST", body: payload }, opts)),
  executeReadonlySql: (payload, opts = {}) =>
    requestJson(
      "/api/platform/query/execute-readonly",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  previewTable: (connectionId, schemaName, tableName, limit = 200, opts = {}) => {
    const qs = new URLSearchParams({
      connection_id: connectionId,
      schema_name: schemaName || "",
      table_name: tableName || "",
      limit: String(limit),
    });
    return requestJson(`/api/platform/query/preview?${qs.toString()}`, withRequestOpts({}, opts));
  },
  exportQuery: (payload, fallbackName = "query_result.csv") =>
    downloadApiFile("/api/platform/query/export", {
      method: "POST",
      body: payload,
      fallbackName,
    }),

  getLogs: (limit = 150) => requestJson(`/api/platform/logs?limit=${limit}`),
  clearLogs: () => requestJson("/api/platform/logs/clear", { method: "POST" }),
  getImportProgress: (progressId) =>
    requestJson(`/api/platform/import-progress/${encodeURIComponent(progressId)}`, {
      cache: "no-store",
    }),
};

export const workspaceApi = {
  list: () => requestJson("/api/platform/workspaces", { cache: "no-store" }),
  create: (payload) =>
    requestJson("/api/platform/workspaces", { method: "POST", body: payload }),
  update: (workspaceId, payload) =>
    requestJson(`/api/platform/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: "PUT",
      body: payload,
    }),
  switchTo: (workspaceId) =>
    requestJson(`/api/platform/workspaces/${encodeURIComponent(workspaceId)}/switch`, {
      method: "POST",
    }),
  remove: (workspaceId) =>
    requestJson(`/api/platform/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: "DELETE",
    }),
  exportFile: ({ workspaceId = "", includeMetadataCache = true, fallbackName = "DataOG_Workspace.dataog.db" } = {}) => {
    const qs = new URLSearchParams();
    if (workspaceId) qs.set("workspace_id", workspaceId);
    qs.set("include_metadata_cache", includeMetadataCache ? "true" : "false");
    return downloadApiFile(`/api/platform/workspaces/export?${qs.toString()}`, {
      fallbackName,
    });
  },
  importAsNew: (file, { name = "", description = "", switchImmediately = true } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    if (name) fd.append("name", name);
    if (description) fd.append("description", description);
    fd.append("switch_immediately", switchImmediately ? "true" : "false");
    return uploadFormData("/api/platform/workspaces/import", fd);
  },
  inspectMerge: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return uploadFormData("/api/platform/workspaces/merge/inspect", fd);
  },
  executeMerge: (payload) =>
    requestJson("/api/platform/workspaces/merge/execute", {
      method: "POST",
      body: payload,
    }),
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
  uploadDictionary: (file, mode = "upsert", { progressId = "", onUploadProgress = null } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("mode", mode || "upsert");
    if (progressId) fd.append("progress_id", progressId);
    return uploadFormData("/api/platform/standards/upload", fd, { onUploadProgress });
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

export const knowledgeApi = {
  summary: () => requestJson("/api/platform/knowledge/summary", { cache: "no-store" }),
  listDocuments: ({ dialect = "", includeBuiltin = true, keyword = "" } = {}) => {
    const qs = new URLSearchParams({
      dialect: dialect || "",
      include_builtin: includeBuiltin ? "true" : "false",
      keyword: keyword || "",
    });
    return requestJson(`/api/platform/knowledge/documents?${qs.toString()}`, { cache: "no-store" });
  },
  getDocument: (docId, includeChunks = true) =>
    requestJson(
      `/api/platform/knowledge/documents/${encodeURIComponent(docId)}?include_chunks=${includeChunks ? "true" : "false"}`
    ),
  uploadDocument: (file, { dialect = "GENERIC", title = "", tags = "", progressId = "" } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("dialect", dialect || "GENERIC");
    if (title) fd.append("title", title);
    if (tags) fd.append("tags", tags);
    if (progressId) fd.append("progress_id", progressId);
    return uploadFormData("/api/platform/knowledge/documents/upload", fd);
  },
  createTextDocument: (payload) =>
    requestJson("/api/platform/knowledge/documents/text", { method: "POST", body: payload }),
  updateDocument: (docId, payload) =>
    requestJson(`/api/platform/knowledge/documents/${encodeURIComponent(docId)}`, {
      method: "PUT",
      body: payload,
    }),
  deleteDocument: (docId) =>
    requestJson(`/api/platform/knowledge/documents/${encodeURIComponent(docId)}`, {
      method: "DELETE",
    }),
  search: (payload) =>
    requestJson("/api/platform/knowledge/search", { method: "POST", body: payload }),
  getChunkSection: (chunkId, windowSize = 1) =>
    requestJson(
      `/api/platform/knowledge/chunks/${encodeURIComponent(chunkId)}?window=${encodeURIComponent(String(windowSize))}`
    ),
  exportPack: ({ dialect = "", docIds = [] } = {}) => {
    const qs = new URLSearchParams();
    if (dialect) qs.set("dialect", dialect);
    if (Array.isArray(docIds) && docIds.length) qs.set("doc_ids", docIds.join(","));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return downloadApiFile(`/api/platform/knowledge/export${suffix}`, {
      fallbackName: "DataOG_KB_Pack.json.gz",
    });
  },
  importPack: (file, { overwriteExisting = false } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("overwrite_existing", overwriteExisting ? "true" : "false");
    return uploadFormData("/api/platform/knowledge/import", fd);
  },
  getEmbeddingConfig: () => requestJson("/api/platform/knowledge/embedding-config", { cache: "no-store" }),
  saveEmbeddingConfig: (payload) =>
    requestJson("/api/platform/knowledge/embedding-config", { method: "PUT", body: payload }),
  reindexEmbeddings: (docId = "", opts = {}) => {
    const qs = docId ? `?doc_id=${encodeURIComponent(docId)}` : "";
    return requestJson(
      `/api/platform/knowledge/embeddings/reindex${qs}`,
      withRequestOpts({ method: "POST" }, opts)
    );
  },
};

export const diagnosticsApi = {
  ping: () =>
    requestJson("/api/platform/diagnostics/ping", {
      cache: "no-store",
      trackInRing: false,
    }),
  overview: () =>
    requestJson("/api/platform/diagnostics/overview", { cache: "no-store" }),
  getTrace: (traceId) =>
    requestJson(`/api/platform/diagnostics/traces/${encodeURIComponent(traceId)}`, {
      cache: "no-store",
    }),
  runBenchmark: (payload = {}) =>
    requestJson("/api/platform/diagnostics/benchmark", {
      method: "POST",
      body: payload,
    }),
  exportBundle: (payload = {}) =>
    downloadApiFile("/api/platform/diagnostics/export", {
      method: "POST",
      body: payload,
      fallbackName: "DataOG_Diagnostics.json",
    }),
  clear: () => requestJson("/api/platform/diagnostics/clear", { method: "POST" }),
  updateConfig: (payload) =>
    requestJson("/api/platform/diagnostics/config", {
      method: "PUT",
      body: payload,
    }),
};
