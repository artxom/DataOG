/**
 * DataOG 统一后端通信客户端 (FastAPI 微内核与各独立业务插件 API)
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

  executeSql: (payload) =>
    requestJson("/api/platform/query/execute", { method: "POST", body: payload }),
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

export const migratorApi = {
  getState: () => requestJson("/api/modules/standard_migrator/state"),
  bindConnection: (connectionId, autoSync = true) =>
    requestJson("/api/modules/standard_migrator/bind-connection", {
      method: "POST",
      body: { connection_id: connectionId, auto_sync: autoSync },
    }),
  sync: () => requestJson("/api/modules/standard_migrator/sync", { method: "POST" }),
  uploadChecklist: (file) => {
    const fd = new FormData();
    fd.append("file", file);
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
      fallbackName: "DataOG_字段贯标标准导入模板.xlsx",
    }),
  exportReport: () =>
    downloadApiFile("/api/modules/standard_migrator/checklist/report.xlsx", {
      fallbackName: "DataOG_单库贯标核验与执行报告.xlsx",
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
};

export const crossApi = {
  getOverview: (tableKey = "", forceRefresh = false) => {
    const qs = new URLSearchParams();
    if (tableKey) qs.set("table_key", tableKey);
    if (forceRefresh) qs.set("force_refresh", "true");
    return requestJson(`/api/modules/cross_comparator/overview?${qs.toString()}`);
  },
  bindRole: (payload) =>
    requestJson("/api/modules/cross_comparator/bind-role", { method: "POST", body: payload }),
  saveBindings: (payload) =>
    requestJson("/api/modules/cross_comparator/bindings/save", { method: "POST", body: payload }),
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
      fallbackName: "DataOG_跨环境三方数标绑定配置包.json",
    }),
  exportExcel: (tableKey = "") => {
    const qs = tableKey ? `?table_key=${encodeURIComponent(tableKey)}` : "";
    return downloadApiFile(`/api/modules/cross_comparator/bindings/export.xlsx${qs}`, {
      fallbackName: "DataOG_跨环境三栏数标比对与绑定报告.xlsx",
    });
  },
  pushToMigrator: (stdTableKey = "") =>
    requestJson("/api/modules/cross_comparator/push-to-migrator", {
      method: "POST",
      body: { std_table_key: stdTableKey || null },
    }),
};

export const ddlApi = {
  getOptions: () => requestJson("/api/modules/ddl_translator/options"),
  translate: (payload) =>
    requestJson("/api/modules/ddl_translator/translate", { method: "POST", body: payload }),
};
