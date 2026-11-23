# Data.O.G 3.0 模块化开发指南 (Module SDK & Architecture Blueprint)

> 本文档面向 **v3.0+** 新功能开发，目标是让任何新业务模块与现有 2.X 功能实现 **100% 物理与逻辑解耦**——新功能只需聚焦自身目录并复用平台公共底座，**无需修改 `app.py`、`storage/`、`workspace_hub.py`、`asset_store.py`、`main.js`、`state.js`、`dom.js`、`api.js`、`index.html` 或 `workbench.css`**。

---

## 1. 分层与边界铁律

| 层级 | 目录 | 职责与依赖规则 |
| :--- | :--- | :--- |
| **微内核底座 (Platform Kernel)** | `core/platform/` + `static/js/core/` + `static/js/ui/` | 提供全局共享能力：认证授权、连接与驱动、元数据缓存、只读/通用 SQL 查询、AI 模型、企业标准字典、三层工作区存储、任务进度流、离线报告外壳、桌面窗口宿主与基础 UI 组件。**严禁反向依赖任何业务模块。** |
| **独立后端插件 (Backend Plugins)** | `modules/<module_id>/` | 通过 `MODULE_SPEC` 自动发现挂载，仅依赖 `core.platform.*`。**严禁跨模块私有导入**（`test_plugin_modules_strict_zero_cross_import_isolation` 强守卫）。 |
| **独立前端应用 (Frontend Apps)** | `static/js/apps/<app_id>/` | 按需 ES Module 懒加载，仅依赖 `static/js/core/{http,platform_api,app_registry,dom,state}.js` 与 `static/js/ui/*`。**严禁直接引用其他业务模块 View 或 API。** |

---

## 2. 新增一个 3.0 后端模块（3 步零侵入）

假设开发新模块 `data_quality`（归属桌面应用 `quality`）：

### Step 1：创建模块目录 `modules/data_quality/`
- `schemas.py`：Pydantic 请求体定义
- `service.py`：业务逻辑（通过 `PlatformContext` 注入共享能力，使用 `ctx.store.register_table_schema(..., scope="workspace" | "private")` 与 `ctx.store.module_db(scope=...)` 管理本模块专属表）
- `router.py`：薄路由层，直接复用 `core/platform/http_utils.py` 提供的 `json_error`、`file_download_response`、`run_tracked_task`、`run_tracked_upload`

### Step 2：在 `modules/data_quality/__init__.py` 导出 `MODULE_SPEC`
```python
from core.platform.module_registry import ModuleSpec
from modules.data_quality.router import router
from modules.data_quality.service import DataQualityService

def _init_module(ctx):
    DataQualityService.ensure_schema(ctx.store)

MODULE_SPEC = ModuleSpec(
    module_id="data_quality",
    title="数据质量检核",
    subtitle="规则编排、异常采样与离线质检报告",
    api_prefix="/api/modules/data_quality",
    router=router,
    desktop_app_id="quality",                  # 自动向 AssetStore 注册自定义图标槽位 app:quality
    workspace_tables=("dq_rule_sets",),        # 自动纳入 .dataog.db 工作区克隆/导出白名单
    private_tables=("dq_local_run_cache",),    # 仅留存本机私有保险箱，严禁导出
    workspace_meta_keys=("dq_default_conn_id",),
    init_hook=_init_module,
    order=50,
)
```

### Step 3：完成！
启动时 `app.py` 的 `mount_discovered_modules(app, ctx)` 会自动发现并挂载 `modules/data_quality`，零改动接入：
- FastAPI 路由 + Gate 1/Gate 2 离线授权鉴权 + 错误路径脱敏
- `AssetStore` 图标自定义槽位 (`app:quality`)
- Layer 2 / Layer 3 三层存储与 `.dataog.db` 团队工作区导出/克隆

---

## 3. 新增一个 3.0 前端桌面应用（3 步零侵入）

### Step 1：创建独立目录 `static/js/apps/quality/`
- `api.js`：仅从 `../../core/http.js` 导入 `requestJson` / `uploadFormData` / `downloadApiFile` / `withRequestOpts`
- `view.js`：导出实现 `mount(container, context, refreshHeader)`、`destroy()`、`getHeaderParts()` 的视图类
- `style.css`：本应用专属样式（完全不写入 `workbench.css`）
- `register.js`：声明式注册清单：
```js
import { registerDesktopApp } from "../../core/app_registry.js";

registerDesktopApp({
  id: "quality",
  title: "数据质量",
  defaultSummary: "规则 · 检核 · 报告",
  iconName: "quality",
  iconPath: "M12 3l8 4v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z",
  iconGradient: "linear-gradient(145deg, #10b981, #0ea5e9)",
  stylesheets: ["/js/apps/quality/style.css"],
  resetOnWorkspaceSwitch: true,
  order: 50,
  loader: async () => {
    const mod = await import("./view.js");
    return new mod.QualityView();
  },
});
```

### Step 2：在 `static/js/apps/index.js` 追加一行导入
```js
import "./quality/register.js";
```

### Step 3：完成！
新应用自动获得：
- macOS Dock 栏超椭圆图标与红黄绿三键窗口生命周期
- 按需懒加载（未打开时不加载任何 JS/CSS）
- `系统配置 → 外观与图标` 自定义图标上传与重置
- 工作区热切换时自动销毁旧工作区实例

---

## 4. 平台公共底座能力速查表

| 能力 | 后端调用入口 (`ctx.*` / `core.platform.*`) | 前端调用入口 |
| :--- | :--- | :--- |
| 数据源连接与只读会话 | `ctx.connection_hub` (`readonly_session` / `probe_session`) | `platformApi.listConnections` / `store.state.connections` |
| 库表元数据与四层视图血缘 | `ctx.metadata_hub` (`get_catalog_tree` / `get_table_meta`) | `DatabaseTree` / `ObjectPicker` / `platformApi.getTableDetail` |
| 硬件级只读 SQL 执行 | `ctx.query_engine.execute_readonly_sql(...)` | `platformApi.executeReadonlySql` |
| AI 大模型（含 TLS 回退） | `ctx.llm_hub.chat_completion(...)` | `store.state.llmModels` |
| 全局标准字典 | `ctx.standard_catalog` | `standardsApi` / `openStandardPickerModal` |
| 三层存储与插件表 | `ctx.store.register_table_schema(..., scope=...)` / `module_db(scope)` | `workspaceApi` |
| 实时进度条与 SSE 取消 | `core.platform.http_utils.run_tracked_task` / `ctx.task_progress` | `TaskActivityBar` / `tasksApi` |
| 单文件离线 HTML+Excel 报告 | `core.platform.offline_report.wrap_offline_html(...)` | `openExportReportModal` |
| 跨方言标识符/类型/DDL | `core.platform.dialects.*` | `static/js/core/type_compat.js` |
