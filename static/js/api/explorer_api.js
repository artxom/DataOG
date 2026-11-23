/**
 * AI 只读智能数据探查模块独立 API 客户端 (/api/modules/data_explorer)
 */

import { requestJson, withRequestOpts } from "../core/http.js";

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
