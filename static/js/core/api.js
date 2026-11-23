/**
 * Data.O.G 后端通信客户端兼容聚合入口 (api.js)
 *
 * v2.9.0 已按「HTTP 传输原语 / 平台共享底座 / 各业务模块独立 API」完成彻底解耦：
 * - `core/http.js`: 通用传输原语（统一注入 `X-Task-Id` 追踪头、`isAbortError`、JSON/上传/下载）
 * - `core/platform_api.js`: 平台底座共享接口（`authApi` / `tasksApi` / `platformApi` / `workspaceApi` / `standardsApi`）
 * - `api/*_api.js`: 2.X 现有业务模块独立接口（如 `crossApi.comparePair` / `crossApi.prefetchPairs`、`migratorApi`、`ddlApi`、`explorerApi`）
 *
 * 3.0 新增功能模块请直接从 `core/http.js` 与 `core/platform_api.js` 导入，
 * 并在自身目录独立维护 API 文件，无需修改本文件。
 */

export {
  downloadApiFile,
  isAbortError,
  requestJson,
  uploadFormData,
  withRequestOpts,
} from "./http.js";
export {
  authApi,
  platformApi,
  standardsApi,
  tasksApi,
  workspaceApi,
} from "./platform_api.js";
export { crossApi } from "../api/cross_api.js";
export { ddlApi, polyglotApi } from "../api/ddl_api.js";
export { explorerApi } from "../api/explorer_api.js";
export { migratorApi } from "../api/migrator_api.js";
