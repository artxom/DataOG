/**
 * 跨环境比对模块独立 API 客户端 (/api/modules/cross_comparator)
 */

import {
  downloadApiFile,
  requestJson,
  uploadFormData,
  withRequestOpts,
} from "../core/http.js";

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
