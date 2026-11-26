/**
 * 多邻国（智能命名 & 跨方言 DDL 翻译）独立 API 客户端 (/api/modules/ddl_translator)
 */

import { requestJson, withRequestOpts } from "../core/http.js";

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
