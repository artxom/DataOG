/**
 * Data.O.G 3.0 智能数据助手前端 API 客户端 (apps/ai_assistant/api.js)
 * 严格遵守 3.0 隔离规范：仅从 core/http.js 导入基础请求原语。
 */

import { downloadApiFile, requestJson, withRequestOpts } from "../../core/http.js";

const BASE = "/api/modules/ai_assistant";

export const assistantApi = {
  getDialects: () => requestJson(`${BASE}/dialects`, { cache: "no-store" }),

  listSessions: ({ connectionId = "", dialect = "", limit = 80 } = {}) => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (dialect) qs.set("dialect", dialect);
    qs.set("limit", String(limit || 80));
    return requestJson(`${BASE}/sessions?${qs.toString()}`, { cache: "no-store" });
  },

  createSession: (payload) =>
    requestJson(`${BASE}/sessions`, { method: "POST", body: payload }),

  getSession: (sessionId) =>
    requestJson(`${BASE}/sessions/${encodeURIComponent(sessionId)}`, { cache: "no-store" }),

  updateSession: (sessionId, payload) =>
    requestJson(`${BASE}/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PUT",
      body: payload,
    }),

  deleteSession: (sessionId) =>
    requestJson(`${BASE}/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),

  sendChatTurn: (payload, opts = {}) =>
    requestJson(`${BASE}/chat`, withRequestOpts({ method: "POST", body: payload }, opts)),

  approveProposal: (proposalId, payload) =>
    requestJson(`${BASE}/proposals/${encodeURIComponent(proposalId)}/confirm`, {
      method: "POST",
      body: payload,
    }),

  declineProposal: (proposalId, reason = "") =>
    requestJson(`${BASE}/proposals/${encodeURIComponent(proposalId)}/reject`, {
      method: "POST",
      body: { reason: reason || "" },
    }),

  exportResult: ({ columns = [], rows = [], fmt = "xlsx", title = "DataOG_Result" } = {}) =>
    downloadApiFile(`${BASE}/export-result`, {
      method: "POST",
      body: { columns, rows, fmt, title },
      fallbackName: `${title || "DataOG_Result"}.${fmt === "csv" ? "csv" : "xlsx"}`,
    }),
};
