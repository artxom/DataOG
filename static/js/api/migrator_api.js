/**
 * 单库落标贯标模块独立 API 客户端 (/api/modules/standard_migrator)
 */

import {
  downloadApiFile,
  requestJson,
  uploadFormData,
  withRequestOpts,
} from "../core/http.js";

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
  deleteItems: (itemIds, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/checklist/delete-items",
      withRequestOpts({ method: "POST", body: { item_ids: itemIds } }, opts)
    ),
  previewItemSql: (itemId, opts = {}) =>
    requestJson(
      `/api/modules/standard_migrator/items/${encodeURIComponent(itemId)}/sql`,
      withRequestOpts({}, opts)
    ),
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
      withRequestOpts(
        {
          method: "POST",
          body: {
            ...payload,
            connection_id: payload?.connection_id || payload?.connectionId || "",
          },
        },
        opts
      )
    ),
  executeRebuild: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/rebuild/execute",
      withRequestOpts(
        {
          method: "POST",
          body: {
            ...payload,
            connection_id: payload?.connection_id || payload?.connectionId || "",
          },
        },
        opts
      )
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
    schemas = "",
    scope = "",
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (schemaName) qs.set("schema_name", schemaName);
    if (tables) qs.set("tables", tables);
    if (schemas) qs.set("schemas", schemas);
    if (scope) qs.set("scope", scope);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return downloadApiFile(`/api/modules/standard_migrator/bindings/template.xlsx${suffix}`, {
      fallbackName: "DataOG_字段落标映射关系模板.xlsx",
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
    schemas = "",
    onlyBoundTables = true,
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (formatProfile) qs.set("format_profile", formatProfile);
    if (tables) qs.set("tables", tables);
    if (schemas) qs.set("schemas", schemas);
    qs.set("only_bound_tables", onlyBoundTables ? "true" : "false");
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
    schemas = "",
    onlyBoundTables = true,
    taskId = "",
    signal = undefined,
  } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (formatProfile) qs.set("format_profile", formatProfile);
    if (tables) qs.set("tables", tables);
    if (schemas) qs.set("schemas", schemas);
    qs.set("only_bound_tables", onlyBoundTables ? "true" : "false");
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

  // 第 3 步：结构化变更日志、Checkpoint 与一键回滚
  getChangelog: ({
    connectionId = "",
    riskLevel = "",
    status = "",
    keyword = "",
    limit = 200,
    offset = 0,
  } = {}, opts = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (riskLevel) qs.set("risk_level", riskLevel);
    if (status) qs.set("status", status);
    if (keyword) qs.set("keyword", keyword);
    qs.set("limit", String(limit || 200));
    qs.set("offset", String(offset || 0));
    return requestJson(
      `/api/modules/standard_migrator/changelog?${qs.toString()}`,
      withRequestOpts({}, opts)
    );
  },
  createCheckpoint: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/checkpoints",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  deleteCheckpoint: (checkpointId, opts = {}) =>
    requestJson(
      `/api/modules/standard_migrator/checkpoints/${encodeURIComponent(checkpointId)}`,
      withRequestOpts({ method: "DELETE" }, opts)
    ),
  previewRollback: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/rollback/preview",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  executeRollback: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/rollback/execute",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  exportChangelogJson: ({ connectionId = "", onlyApplied = true } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    qs.set("only_applied", onlyApplied ? "true" : "false");
    return downloadApiFile(`/api/modules/standard_migrator/changelog/export.json?${qs.toString()}`, {
      fallbackName: "DataOG_落标变更台账底座.json",
    });
  },
  exportChangelogExcel: ({ connectionId = "" } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    return downloadApiFile(`/api/modules/standard_migrator/changelog/export.xlsx?${qs.toString()}`, {
      fallbackName: "DataOG_落标变更台账与Checkpoint.xlsx",
    });
  },

  // 第 4 步：基于字段变更历史的 SQL 脚本自动改写
  getSqlRewriteContext: ({ connectionId = "", sinceCheckpointId = null } = {}, opts = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (sinceCheckpointId) qs.set("since_checkpoint_id", String(sinceCheckpointId));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return requestJson(
      `/api/modules/standard_migrator/sql-rewrite/context${suffix}`,
      withRequestOpts({}, opts)
    );
  },
  analyzeSqlRewrite: (payload, opts = {}) =>
    requestJson(
      "/api/modules/standard_migrator/sql-rewrite/analyze",
      withRequestOpts({ method: "POST", body: payload }, opts)
    ),
  exportSqlRewriteZip: (files, { taskId = "", signal = undefined } = {}) =>
    downloadApiFile("/api/modules/standard_migrator/sql-rewrite/export-zip", {
      method: "POST",
      body: { files },
      fallbackName: "DataOG_落标改写SQL脚本包.zip",
      taskId,
      signal,
    }),
};
