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
    if (
      resp.status === 401 ||
      (payload && payload.code === "LICENSE_REQUIRED")
    ) {
      if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
        window.dispatchEvent(
          new CustomEvent("dataog:auth-required", { detail: payload || {} })
        );
      }
    }
    const msg =
      (payload && (payload.error || payload.detail || payload.message)) ||
      `请求失败 (HTTP ${resp.status})`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return payload;
}

function withRequestOpts(base = {}, opts = {}) {
  const next = { ...base };
  if (opts && typeof opts === "object") {
    if (opts.signal !== undefined) next.signal = opts.signal;
    if (opts.taskId) next.taskId = opts.taskId;
    if (opts.cache !== undefined && next.cache === undefined) next.cache = opts.cache;
  }
  return next;
}

export function isAbortError(err) {
  return Boolean(
    err && (err.name === "AbortError" || String(err.message || "").includes("AbortError"))
  );
}

export async function requestJson(
  url,
  { method = "GET", body = undefined, cache = undefined, signal = undefined, taskId = "" } = {}
) {
  const opts = {
    method,
    headers: { Accept: "application/json" },
  };
  if (cache) {
    opts.cache = cache;
  }
  if (signal) {
    opts.signal = signal;
  }
  const normalizedTaskId = taskId ? String(taskId).trim() : "";
  if (normalizedTaskId) {
    opts.headers["X-Task-Id"] = normalizedTaskId;
  }
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  return parseJsonOrThrow(resp);
}

export async function uploadFormData(url, formData, { onUploadProgress = null } = {}) {
  if (typeof XMLHttpRequest !== "undefined") {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Accept", "application/json");
      if (typeof onUploadProgress === "function" && xhr.upload) {
        let reportedDone = false;
        xhr.upload.addEventListener("progress", (ev) => {
          if (ev.lengthComputable && ev.total > 0) {
            if (ev.loaded >= ev.total) reportedDone = true;
            onUploadProgress(ev.loaded / ev.total, ev.loaded, ev.total);
          }
        });
        xhr.upload.addEventListener("load", () => {
          if (!reportedDone) {
            reportedDone = true;
            onUploadProgress(1, 1, 1);
          }
        });
      }
      xhr.addEventListener("load", () => {
        const rawText = xhr.responseText || "";
        let payload = null;
        try {
          payload = rawText ? JSON.parse(rawText) : { ok: xhr.status >= 200 && xhr.status < 300 };
        } catch (_e) {
          payload = {
            ok: xhr.status >= 200 && xhr.status < 300,
            error: rawText || `HTTP ${xhr.status}`,
          };
        }
        const httpOk = xhr.status >= 200 && xhr.status < 300;
        if (!httpOk || (payload && payload.ok === false)) {
          if (
            xhr.status === 401 ||
            (payload && payload.code === "LICENSE_REQUIRED")
          ) {
            if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
              window.dispatchEvent(
                new CustomEvent("dataog:auth-required", { detail: payload || {} })
              );
            }
          }
          const msg =
            (payload && (payload.error || payload.detail || payload.message)) ||
            `请求失败 (HTTP ${xhr.status})`;
          reject(new Error(typeof msg === "string" ? msg : JSON.stringify(msg)));
          return;
        }
        resolve(payload);
      });
      xhr.addEventListener("error", () => {
        reject(new Error("网络请求中断，请检查本地服务是否正在运行"));
      });
      xhr.addEventListener("abort", () => {
        reject(new Error("上传已取消"));
      });
      xhr.send(formData);
    });
  }

  const resp = await fetch(url, {
    method: "POST",
    body: formData,
  });
  return parseJsonOrThrow(resp);
}

export async function downloadApiFile(
  url,
  {
    method = "GET",
    body = undefined,
    fallbackName = "export.bin",
    taskId = "",
    signal = undefined,
  } = {}
) {
  const opts = { method, headers: {} };
  if (taskId) {
    opts.headers["X-Task-Id"] = taskId;
  }
  if (signal) {
    opts.signal = signal;
  }
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

export const migratorApi = {
  getState: () => requestJson("/api/modules/standard_migrator/state"),
  bindConnection: (connectionId, autoSync = true, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/bind-connection",
      withRequestOpts({ method: "POST", body: { connection_id: connectionId, auto_sync: autoSync } }, opts)
    ),
  sync: (opts = {}) =>
    requestJson("/api/modules/standard_migrator/sync", withRequestOpts({ method: "POST" }, opts)),
  syncItems: (opts = {}) =>
    requestJson("/api/modules/standard_migrator/sync", withRequestOpts({ method: "POST" }, opts)),
  uploadChecklist: (
    file,
    { defaultSchema = "", defaultTable = "" } = {},
    { progressId = "", onUploadProgress = null } = {}
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    if (defaultSchema) fd.append("default_schema", defaultSchema);
    if (defaultTable) fd.append("default_table", defaultTable);
    if (progressId) fd.append("progress_id", progressId);
    return uploadFormData("/api/modules/standard_migrator/checklist/upload", fd, { onUploadProgress });
  },
  loadFromDb: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/checklist/load-from-db",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  clearItems: () => requestJson("/api/modules/standard_migrator/checklist/clear", { method: "POST" }),
  downloadTemplate: () =>
    downloadApiFile("/api/modules/standard_migrator/checklist/template.xlsx", {
      fallbackName: "Data.O.G_字段贯标模板.xlsx",
    }),
  exportReport: ({ taskId = "", signal = undefined } = {}) =>
    downloadApiFile("/api/modules/standard_migrator/checklist/report.xlsx", {
      fallbackName: "Data.O.G_贯标报告.xlsx",
      taskId,
      signal,
    }),
  executeSingle: (itemId, opts = {}) =>
    requestJson(
      `/api/modules/standard_migrator/execute/item/${encodeURIComponent(itemId)}`,
      withRequestOpts({ method: "POST" }, opts)
    ),
  executeBatch: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/execute/batch",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  previewRebuild: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/rebuild/preview",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  executeRebuild: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/rebuild/execute",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),

  // 第 2 步：字段绑标 + 落标核验 + 对照模板
  getBindingsOverview: (connectionId = "", formatProfile = "AUTO", opts = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (formatProfile) qs.set("format_profile", formatProfile);
    return requestJson(
      `/api/modules/standard_migrator/bindings/overview?${qs.toString()}`,
      withRequestOpts({}, opts)
    );
  },
  getTableBindings: (
    connectionId,
    schemaName,
    tableName,
    formatProfile = "AUTO",
    forceRefresh = false,
    opts = {}
  ) => {
    const qs = new URLSearchParams({
      connection_id: connectionId,
      schema_name: schemaName || "",
      table_name: tableName || "",
      format_profile: formatProfile || "AUTO",
      force_refresh: forceRefresh ? "true" : "false",
    });
    return requestJson(
      `/api/modules/standard_migrator/bindings/table?${qs.toString()}`,
      withRequestOpts({}, opts)
    );
  },
  getTableCompliance: (params = {}, opts = {}) => {
    const p = params || {};
    const force = p.force_refresh ?? p.forceRefresh;
    const qs = new URLSearchParams({
      connection_id: p.connection_id ?? p.connectionId ?? "",
      schema_name: p.schema_name ?? p.schemaName ?? "",
      table_name: p.table_name ?? p.tableName ?? "",
      format_profile: p.format_profile ?? p.formatProfile ?? "AUTO",
      force_refresh: force ? "true" : "false",
    });
    return requestJson(
      `/api/modules/standard_migrator/bindings/table?${qs.toString()}`,
      withRequestOpts({}, opts)
    );
  },
  saveBindings: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/bindings/save",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  autoMatchBindings: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/bindings/auto-match",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  checkCompliance: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/bindings/check-compliance",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  downloadBindingTemplate: ({
    connectionId = "",
    schemaName = "",
    tables = "",
    scope = "",
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (schemaName) qs.set("schema_name", schemaName);
    if (tables) qs.set("tables", tables);
    if (scope) qs.set("scope", scope);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return downloadApiFile(`/api/modules/standard_migrator/bindings/template.xlsx${suffix}`, {
      fallbackName: "DataOG_字段与数据标准对照模板.xlsx",
      taskId,
      signal,
    });
  },
  uploadBindings: (
    file,
    { connectionId, defaultSchema = "", mode = "merge", formatProfile = "AUTO" },
    { progressId = "", onUploadProgress = null } = {}
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("connection_id", connectionId);
    fd.append("default_schema", defaultSchema || "");
    fd.append("mode", mode || "merge");
    fd.append("format_profile", formatProfile || "AUTO");
    if (progressId) fd.append("progress_id", progressId);
    return uploadFormData("/api/modules/standard_migrator/bindings/upload", fd, { onUploadProgress });
  },
  exportBindingReport: ({
    connectionId = "",
    formatProfile = "AUTO",
    tables = "",
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (formatProfile) qs.set("format_profile", formatProfile);
    if (tables) qs.set("tables", tables);
    return downloadApiFile(`/api/modules/standard_migrator/bindings/report.xlsx?${qs.toString()}`, {
      fallbackName: "DataOG_全库落标与贯标报告.xlsx",
      taskId,
      signal,
    });
  },
  exportBindingReportHtml: ({
    connectionId = "",
    formatProfile = "AUTO",
    tables = "",
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (formatProfile) qs.set("format_profile", formatProfile);
    if (tables) qs.set("tables", tables);
    return downloadApiFile(`/api/modules/standard_migrator/bindings/report.html?${qs.toString()}`, {
      fallbackName: "DataOG_全库落标与贯标离线报告.html",
      taskId,
      signal,
    });
  },
  stageMigration: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/bindings/stage-migration",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
};

export const crossApi = {
  getOverview: (tableKey = "", forceRefresh = false, extra = {}, opts = {}) => {
    const qs = new URLSearchParams();
    if (tableKey) qs.set("table_key", tableKey);
    if (forceRefresh) qs.set("force_refresh", "true");
    if (extra.sourceSchema) qs.set("source_schema", extra.sourceSchema);
    if (extra.sourceTable) qs.set("source_table", extra.sourceTable);
    if (extra.targetSchema) qs.set("target_schema", extra.targetSchema);
    if (extra.targetTable) qs.set("target_table", extra.targetTable);
    if (extra.formatProfile) qs.set("format_profile", extra.formatProfile);
    if (extra.sourceFormatProfile) qs.set("source_format_profile", extra.sourceFormatProfile);
    if (extra.targetFormatProfile) qs.set("target_format_profile", extra.targetFormatProfile);
    return requestJson(
      `/api/modules/cross_comparator/overview?${qs.toString()}`,
      withRequestOpts({}, opts)
    );
  },
  comparePair: (params = {}, opts = {}) => {
    const qs = new URLSearchParams();
    const aliasMap = {
      tableKey: "table_key",
      stdTableKey: "std_table_key",
      forceRefresh: "force_refresh",
      sourceSchema: "source_schema",
      sourceTable: "source_table",
      targetSchema: "target_schema",
      targetTable: "target_table",
      formatProfile: "format_profile",
      sourceFormatProfile: "source_format_profile",
      targetFormatProfile: "target_format_profile",
      sourceConnectionId: "source_connection_id",
      targetConnectionId: "target_connection_id",
    };
    for (const [k, v] of Object.entries(params || {})) {
      if (v === undefined || v === null || v === "") continue;
      const targetKey = aliasMap[k] || k;
      if (typeof v === "boolean") {
        qs.set(targetKey, v ? "true" : "false");
      } else {
        qs.set(targetKey, String(v));
      }
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return requestJson(
      `/api/modules/cross_comparator/compare-pair${suffix}`,
      withRequestOpts({}, opts)
    );
  },
  prefetchPairs: (payload = {}, opts = {}) =>
    requestJson(
      "/api/modules/cross_comparator/prefetch-pairs",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  searchPairs: (keyword = "", limit = 200, opts = {}) => {
    const qs = new URLSearchParams();
    if (keyword) qs.set("keyword", keyword);
    if (limit) qs.set("limit", String(limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return requestJson(
      `/api/modules/cross_comparator/pairs/search${suffix}`,
      withRequestOpts({}, opts)
    );
  },
  bindRole: (payload, opts = {}) =>
    requestJson(
      "/api/modules/cross_comparator/bind-role",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  saveBindings: (payload, opts = {}) =>
    requestJson(
      "/api/modules/cross_comparator/bindings/save",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  savePairings: (payload, opts = {}) =>
    requestJson(
      "/api/modules/cross_comparator/pairings/save",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
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
  uploadPairings: (
    file,
    formatProfile = "AUTO",
    { progressId = "", onUploadProgress = null, sourceFormatProfile = "", targetFormatProfile = "" } = {}
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("format_profile", formatProfile || "AUTO");
    if (sourceFormatProfile) fd.append("source_format_profile", sourceFormatProfile);
    if (targetFormatProfile) fd.append("target_format_profile", targetFormatProfile);
    if (progressId) fd.append("progress_id", progressId);
    return uploadFormData("/api/modules/cross_comparator/pairings/upload", fd, { onUploadProgress });
  },
  uploadStandard: (file, { progressId = "", onUploadProgress = null } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    if (progressId) fd.append("progress_id", progressId);
    return uploadFormData("/api/modules/cross_comparator/standard/upload", fd, { onUploadProgress });
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
  exportReportHtml: ({
    formatProfile = "AUTO",
    sourceFormatProfile = "AUTO",
    targetFormatProfile = "AUTO",
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (formatProfile) qs.set("format_profile", formatProfile);
    if (sourceFormatProfile) qs.set("source_format_profile", sourceFormatProfile);
    if (targetFormatProfile) qs.set("target_format_profile", targetFormatProfile);
    return downloadApiFile(`/api/modules/cross_comparator/report.html?${qs.toString()}`, {
      fallbackName: "DataOG_跨环境库级比对离线报告.html",
      taskId,
      signal,
    });
  },
  exportReportExcel: ({
    formatProfile = "AUTO",
    sourceFormatProfile = "AUTO",
    targetFormatProfile = "AUTO",
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (formatProfile) qs.set("format_profile", formatProfile);
    if (sourceFormatProfile) qs.set("source_format_profile", sourceFormatProfile);
    if (targetFormatProfile) qs.set("target_format_profile", targetFormatProfile);
    return downloadApiFile(`/api/modules/cross_comparator/report.xlsx?${qs.toString()}`, {
      fallbackName: "DataOG_跨环境库级比对报告.xlsx",
      taskId,
      signal,
    });
  },
  pushToMigrator: (
    stdTableKeyOrPayload = "",
    formatProfileOrOpts = "AUTO",
    profiles = {},
    opts = {}
  ) => {
    if (
      stdTableKeyOrPayload &&
      typeof stdTableKeyOrPayload === "object" &&
      !Array.isArray(stdTableKeyOrPayload)
    ) {
      const reqOptions =
        formatProfileOrOpts && typeof formatProfileOrOpts === "object" ? formatProfileOrOpts : opts;
      return requestJson(
        "/api/modules/cross_comparator/push-to-migrator",
        withRequestOpts({ method: "POST", body: stdTableKeyOrPayload }, reqOptions)
      );
    }
    const stdTableKey = stdTableKeyOrPayload || "";
    const formatProfile = typeof formatProfileOrOpts === "string" ? formatProfileOrOpts : "AUTO";
    const { sourceFormatProfile = "", targetFormatProfile = "" } = profiles || {};
    return requestJson(
      "/api/modules/cross_comparator/push-to-migrator",
      withRequestOpts(
        {
          method: "POST",
          body: {
            std_table_key: stdTableKey || null,
            format_profile: formatProfile || "AUTO",
            source_format_profile: sourceFormatProfile || "AUTO",
            target_format_profile: targetFormatProfile || "AUTO",
          },
        },
        opts
      )
    );
  },
};

export const ddlApi = {
  getOptions: () => requestJson("/api/modules/ddl_translator/options"),
  smartNaming: (payload, opts = {}) =>
    requestJson(
      "/api/modules/ddl_translator/naming",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  translate: (payload, opts = {}) =>
    requestJson(
      "/api/modules/ddl_translator/translate",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
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
  explore: (payload, opts = {}) =>
    requestJson(
      "/api/modules/data_explorer/explore",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
};
