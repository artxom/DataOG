# Data.O.G 项目记忆与架构工程规范 (AGENTS.md)

## 1. 项目定位与核心设计哲学
- **官方品牌与产物**：**`Data.O.G`**（GitHub 仓库：`https://github.com/artxom/DataOG.git`，Windows 独立打包文件名 `DataOG.exe`），基于 `FastAPI` + `Pydantic` + 零构建原生 ES Modules 构建的 **macOS 桌面级多功能数据工程与治理一体化系统**。
- **核心设计原则**：
  1. **强底座共享**：连接档案、自定义 JDBC 驱动、只读/通用 SQL 执行、库表字段元数据缓存、全局数据标准字典 (`StandardCatalog`)、AI 大模型接入 (`LLMHub`)、离线激活授权 (`LicenseHub`) 及三层分离工作区持久化均由底座提供，一次配置、全模块零延迟共享。
  2. **macOS 桌面化模块隔离**：桌面保持纯净留白，底部居中悬浮毛玻璃 **Dock 栏**承载四大独立业务应用，支持红黄绿窗口生命周期（🔴关闭销毁重置、🟡最小化收纳至 Dock 分隔线右侧 100% 保留现场、🟢最大化全屏切换并自动下沉 Dock）；业务模块间严格**零私有互引**，跨模块协作一律通过 `PlatformContext` 数据契约或 `PlatformStateStore` 完成。

## 2. 核心分层与模块化铁律
所有迭代必须严格遵循 **「平台微内核底座 (`core/platform/`) + macOS 桌面外壳与组件 (`static/`) + 独立插件模块 (`modules/<module_id>/`)」** 架构：

### 2.1 全局共享底座层 (`core/platform/`)
业务模块严禁私自管理数据库密码、AI Key 或重复编写目录 SQL，一律通过 `PlatformContext` 注入调用七大中枢：
1. **双轨驱动与全局连接 (`DriverManager` & `ConnectionHub`)**：原生通道免 Java 覆盖 PG/华为 DWS/openGauss/金仓 (`psycopg2`)、MySQL/OceanBase/Doris/StarRocks/TiDB (`PyMySQL`)、`sqlite3` 及纯 Python `InceptorThriftClient`；扩展通道通过按需懒加载 `JPype1` + `URLClassLoader` 加载安全校验后的自定义 JDBC `.jar`（达梦 DM8/Oracle/DB2/GBase 等）；无 JRE 时绝不影响原生驱动运行；支持 `(db_type, host, port, dbname)` / `jdbc_url` 端点指纹匹配与 `readonly_session` / `probe_session` 60s 温连接复用。
2. **元数据共享与缓存检索 (`MetadataHub`)**：统一 `Schema → Table/View/MatView → ColumnMeta` 模型，深度库走批量高性能系统目录探查 + 锁超时降级 + 四层视图血缘穿透，自定义 JDBC 回退标准 `DatabaseMetaData`；持久化至 SQLite 支持 `120s` 缺失表负缓存、`⌘K` Spotlight 检索。
3. **通用查询与只读沙箱 (`QueryEngine` & `ReadOnlySqlGuard`)**：输出标准 `QueryResult`（默认 `LIMIT 500`/上限 `5000`、超时保护、CSV/Excel 导出），内置双模态词法白名单（`readonly_guard.py`）与会话级硬件只读执行通道。
4. **AI 大模型接入 (`LLMHub`)**：零额外依赖标准库实现 OpenAI 兼容协议管理 `llm_providers`（内置 DeepSeek、Gemini、自定义网关，连通性检测、模型列表拉取、结构化 JSON 输出）。
5. **壁纸与自定义图标 (`AssetStore`)**：光栅魔数头校验（PNG/JPEG/WebP/GIF，阻断 SVG）、UUID 重命名安全落盘与 `/favicon.ico` 联动。
6. **三层分离存储、标准字典与团队工作区 (`StoragePaths` / `PlatformStateStore` / `WorkspaceManager` / `StandardCatalog` / `Dialects` / `ExcelIO`)**：
   - **Layer 1 无状态程序包**：安装目录（`DataOG.exe`、`_internal/`、`static/`）严禁存放或写入运行期 `data/`，覆盖安装绝不丢失用户配置与进度。
   - **Layer 2 本地私有保险箱**（OS 混淆用户隐藏目录，支持 `DATAOG_HOME` 覆盖，**严禁在 UI/API/日志中暴露路径**）：隔离存放激活码/时钟水印、`llm_providers`、`connection_credentials`、`jdbc_drivers`、桌面壁纸图标资产、本地 `execution_logs`、`explorer_sessions`/`explorer_messages` 及 `workspaces` 注册表。
   - **Layer 3 团队共享工作区 (`*.dataog.db`)**：结构上物理剔除密码/API Key/激活码（含 `workspace_meta`、无密码 `connection_topology`、`standard_dictionary`、`field_standard_bindings`、`cross_table_bindings`、`cross_column_bindings`、`migration_items` 及可选 `meta_*` 缓存）；`WorkspaceManager` (`/workspaces*`) 提供新建/克隆/重命名/热切换/删除、零密钥 `VACUUM` 导出、安全导入与三步引导式增量合并（`mode=ro&immutable=1` + `query_only=ON`、端点指纹重映射、`supplement`/`newer`/`overwrite` 策略及表对差异预览）。
   - `StandardCatalog` 以 `std_code` 为主键管理 46 列标准字典、`field_standard_bindings` 与三级落标判定；`Dialects` 统一提供标识符清洗、跨方言类型归一化/转换与长度包络诊断、`analyze_item_changes`/`item_requires_table_rebuild`（严防仅注释差异误触发重建）、四层视图穿透及 Excel 公式求值/`#N/A` 置空。
7. **离线非对称激活码与原生防反编译护盾 (`LicenseHub`)**：
   - 嵌有混淆拆分的 Ed25519 公钥（零私钥）+ 编译态 C 动态库 `_license_shield` 64 位挑战-应答 MAC 校验 + Gate 1 (ASGI 中间件) / Gate 2 (`get_platform_context` HMAC 能力令牌) 双层门禁 + 私有库/混淆锚点双位置防时钟回拨密封；内置 60s 验签热缓存与 Gate 2 快速 HMAC 复核；独立 C 生成器位于 `tools/keygen_src/` 与 `tools/build_keygen.py`。

### 2.2 后端插件自动发现与前端应用注册中心 (`modules/` & `static/`)
- 后端采用 `ModuleSpec` 声明式插件自动发现 (`core/platform/module_registry.py`) 与共享任务/下载原语 (`core/platform/http_utils.py`)；新增 3.0 模块只需在 `modules/<module_id>/__init__.py` 导出 `MODULE_SPEC` 并用 `ctx.store.register_table_schema(..., scope=...)` 管理专属表，零改 `app.py`/`storage/`/`workspace_hub.py`/`asset_store.py` 即可自动接入路由、授权、图标槽位与 `.dataog.db` 工作区流转。
- 前端采用**零 Node 构建链**原生 ES Modules + 声明式桌面应用注册中心 (`static/js/core/app_registry.js` + `static/js/apps/index.js`) 按需懒加载 (`main.js` 零静态业务 View 依赖，`static/js/ui/*` 零业务 API 依赖)；新增 3.0 应用在 `static/js/apps/<app_id>/` 自包含 `api.js`/`view.js`/`style.css`/`register.js` 并仅从 `core/http.js` 与 `core/platform_api.js` 导入底座能力（详见 [docs/MODULE_SDK_3_0.md](file:///Users/artxom/code/DataOG/docs/MODULE_SDK_3_0.md)）。

## 3. 安全、数据兼容与打包红线
- **本地隔离与脱敏**：默认监听 `127.0.0.1:18080`（启用 CSP 与 `Sec-Fetch-Site`）；`data/`、`*.db`、`*.log`、`.keygen_secrets/`、`keygen/`、`demo_sandbox/output/` 及原生编译产物一律 `.gitignore` 排除，严禁私密凭据、Ed25519 私钥或私有物理路径出现在仓库、安装包、API 响应或日志中；上传驱动/图片强制校验大小、魔数头、`realpath` 边界，资产响应携带 `X-Content-Type-Options: nosniff`。
- **前端零 XSS 与非阻塞交互红线**：严禁出现 `innerHTML`、`outerHTML`、`insertAdjacentHTML` 及阻塞式 `window.alert`/`confirm`/`prompt`，一律通过 `static/js/core/dom.js` 安全节点工厂与 `<dialog>` 渲染（单测静态扫描强守卫）。
- **前后向无损兼容与打包自检**：启动自动检测旧版 `<app_dir>/data/` 并幂等无损迁移至三层分离架构；所有 SQLite 表结构演进须通过 `register_table_schema(..., scope=...)` 幂等自检新增列；保留 Windows CMD 防挂起、UTF-8 流保护、多线程 `attachThreadToJVM()` 与 PyInstaller 轻量裁剪（排除 `openai`/`httpx`/`lxml`/`pytest`/`tools`/`demo_sandbox`），模块零互引校验同时覆盖源码 AST 与冻结态字节码自省。

## 4. 工程交付闭环：文档同步、GitHub Flow 与强制自动发版（必须执行）
每次完成任何开发、重构、缺陷修复或工程变更任务时，**必须完整执行以下闭环**，不得遗漏：
1. **同步项目文档**：
   - **`AGENTS.md`**：更新第 5 节版本与基线摘要，并严格保持本文件总大小 **< 24,000 字节**，防止触发规则截断。
   - **`README.md`**：同步更新标题版本号、目录版本注释、版本演进表与测试总数。
   - **`CHANGELOG.md`**：遵循 Keep a Changelog 规范；涉及代码、前端、打包或运行行为的变更**严禁留在 `[Unreleased]`**，发版前一律晋升归并至正式版本章节 `## [X.Y.Z] - YYYY-MM-DD`。
2. **分支与提交规范**：
   - `main` 始终保持测试全绿、文档与版本号同步、可直接打包发布；工作分支一律从最新 `main` 切出（`feat/<name>` / `fix/<name>` / `refactor/<name>` / `docs/<name>` / `chore/<name>`）。
   - 遵循 Conventional Commits（`<type>(<scope>): <中文简明摘要>`），提交前运行 `git status` 与 `git diff` 核查，严禁混入 `.db`/日志/密钥。
3. **本地双门禁校验、强制升版打 Tag 与自动编译发版（核心铁律）**：
   - **合并门禁**：全量单元测试严守**发布前本地完成**原则；合入 `main` 前必须在本地 100% 通过 `python3 -m unittest discover -s tests -p "test_*.py" -v` 与 `python3 app.py --smoke-test`。
   - **每次变更强制升版并触发 GitHub Actions 纯打包发版**：`.github/workflows/ci.yml` 仅在推送 `v*` 标签或手动调度时运行，直接执行 `python build_exe.py --skip-tests` 编译 Windows `DataOG.exe` 并运行 `DataOG.exe --smoke-test`（2 秒冻结态冒烟，不重跑耗时单元测试且客户包不含 `tests/`）后发布 GitHub Release。因此，**任何涉及后端、前端、构建或运行时行为的变更合入 `main` 时，必须按 SemVer 递增版本号**（修复/优化/微调升 `PATCH`，新特性升 `MINOR`），按 [release-and-ship Skill](file:///Users/artxom/code/DataOG/.agents/skills/release-and-ship/SKILL.md) 同步 `app.py`、`core/platform/context.py`、`static/index.html`、`CHANGELOG.md`、`README.md`、`AGENTS.md`（**禁止改动 `WORKSPACE_FORMAT_VERSION`**），推送 `main` 后立即创建并推送附注标签 `git tag -a vX.Y.Z -m "..."` 与 `git push origin vX.Y.Z` 触发云端打包发布（仅纯规则/注释调整且用户明确要求不发版时例外）。
   - **分支清理**：合并与打标推送完成后及时删除已使命终结的临时工作分支，确认远端 `main` 与新标签就位。

## 5. 当前代码基线快照
- **当前主干最新正式版本**：**`v3.5.1`**（**全量 Schema 模板及全平台 Excel 导出非法控制字符清洗修复**：底座 `core/excel_handler.py` 与 `core/platform/excel_io.py` 统一导出 `sanitize_excel_cell`/`sanitize_excel_row`/`sanitize_excel_sheet_title`，剔除 `PK\x02`/`PK\x0b` 等 XML 1.0 非法控制字符并先清后防公式注入，全面覆盖 `scope=schema` 映射模板、贯标报告、SQL 导出、标准字典、离线报告与跨环境比对，延续 `DGP2` 增量包与 `v*` 双产物发布）。
- **核心入口、脚本与工具**：
  - `update_bootstrap.py`（纯标准库 `DGP2` 解密认证 + `zipimport` 增量验签装载器，禁依 `core.*`/`modules.*`）、`app.py`（`BASE_APP_VERSION = "3.5.1"`）、`core/app_kernel.py`（FastAPI `3.5.1` 可热更新微内核、防缓存头、`check_static_ui_version_sync`、Gate 1 快速通道 + 线程池卸载、`Server-Timing`、`mount_discovered_modules`、`--smoke-test`）、`build_update_pack.py`（`DGP2` 加密混淆签名构建与零明文网关安全校验）、`build_exe.py` / `build_windows.bat` / `.github/workflows/ci.yml`（`v*` 双产物发布与增量 `--smoke-test`）、`requirements.txt`（8 项纯运行时依赖，0 KB 增量）、`docs/MODULE_SDK_3_0.md`、`docs/AI_ASSISTANT_LONG_SQL_GOAL_PLAN.md`、`knowledge_packs/`。
  - `tools/admin_console/` & `DataOG_Console.command`、`tools/keygen_src/` & `tools/build_keygen.py`、`demo_sandbox/`（启明商业银行信创三引擎演示沙箱）：全部严格排除于客户安装包之外。
- **全局共享底座 (`core/platform/`，前缀 `/api/platform`) & 核心引擎 (`core/`)**：
  - `module_registry.py`、`http_utils.py`、`context.py`（`version="3.5.1"`、Gate 2 HMAC 复核与优雅 `close()`）、`router.py` + `routes/`（8 领域子路由含 `/llm/probe-model`、`knowledge.py`、`diagnostics.py`、`updates.py`）、`schemas.py`（含 `context_window`/`model_tier`/`LLMProbeModelPayload`）、`models.py`、`diagnostics_hub.py`、`update_hub.py`（Layer 2 `<private_root>/rt_upd/` 版本仓，透传 `format_version`/`encrypted`/`cipher`，零物理路径泄露）。
  - `storage_paths.py`（含 `rt_upd` 脱敏防线）、`storage/`（持久双库长连接复用、`temp_store=MEMORY`）、`workspace_hub.py`（维持 `WORKSPACE_FORMAT_VERSION = "2.7.0"`）、`license_hub.py` & `_license_native_src/`、`asset_store.py`、`standard_catalog.py`（含 `PARTIAL_COMPLIANT` 跨标准匹配）、`offline_report/`、`import_progress.py`、`excel_io.py`（统一导出 `sanitize_excel_cell`/`sanitize_excel_row`/`sanitize_excel_sheet_title`） & `core/excel_formula.py`、`readonly_guard.py`、`llm_hub.py`（四级动态探查与 `model_probes` 24h 缓存、`lightweight` 有效甜蜜区预算、L3 400 自校准、弹性超时、瞬态退避重试+减载降级+`length` 自动续写、`ThinkingStreamParser`、`chat_completion_stream`）、`driver_manager.py` & `inceptor_client.py`、`connection_hub.py`、`metadata_hub.py`、`query_engine.py`、`dialects/`、`core/dws_engine.py`。
- **五大独立插件模块 (`modules/`, 导出 `MODULE_SPEC`, 严格零互引)**：
  - `modules/standard_migrator/`（侧栏绑定与落标、自动跳过已落标入队、执行后实时核验 `auto_verified`、`scope=schema` 全量映射模板）、`modules/cross_comparator/`、`modules/ddl_translator/`、`modules/data_explorer/`（`scope="private"`）、`modules/ai_assistant/`（`scope="private"`，在线连库/纯方言双模式、确定性 `sql_static_analyzer.py` 血缘 DAG/隐患雷达/保行号语义压缩、14 工具含 3 手术刀工具、`ExecutionPlanner` 战前规划+轻量裁剪+防打转护栏、后台持久化 `goal_runner.py` 五态硬验收补救+`Last-Event-ID` 续流、`/runs*` & `/chat/stream` SSE、DDL/写提案分级确认）。
- **前端 ES Modules 工作台 (`static/`)**：
  - `index.html`、`css/tokens.css` & `workbench.css`、`js/main.js`、`js/core/`（含 `perf_monitor.js`、`streamSsePost`/`streamSseGet`、`probeLlmModel`）、`js/apps/`（含探查胶囊/Goal 切换/规划卡/Checklist/断点续跑 `ai_assistant/`）、`js/api/`、`js/ui/`（含 `data_grid.js`/`database_tree.js`/`upload_wizard_modal.js`）、`js/views/`（含可调侧栏/原位保滚动/3秒渐隐退场/Schema 全量模板 `standard_migrator_view.js` & `standard_migrator/modals.js`、`settings_view.js`、`knowledge_view.js`、`updates_view.js`、`diagnostics_view.js`、`workspace_merge_wizard.js`、`cross_comparator/*.js`）。
- **自动化测试套件 (`tests/`，共 116 项全绿)**：`test_core.py` (17)、`test_platform.py` (22)、`test_modules_and_api.py` (25)、`test_workspace_separation.py` (9)、`test_license_auth.py` (7)、`test_modular_architecture.py` (7)、`test_ai_assistant_and_kb.py` (10)、`test_diagnostics_benchmark.py` (6)、`test_incremental_updates.py` (8)、`test_admin_console.py` (3)、`test_demo_sandbox.py` (2)。

## 6. AI 只读探查四道硬隔离铁律
涉及 AI 自动查询必须复用 `QueryEngine.execute_readonly_sql`，严禁仅靠 Prompt 软约束：
1. **双模态词法白名单 (`readonly_guard.py`)**：标准 + 反斜杠转义双模式校验，强制单语句，顶层仅放行纯只读语句，校验 CTE 并封禁写/DDL/锁/`INTO` 与危险系统函数。
2. **会话级硬件只读锁 (`DriverManager.open_connection(..., readonly=True)`)**：SQLite `mode=ro` + `query_only=ON`、PG/DWS `set_session(readonly=True)`、PyMySQL `READ ONLY` 事务、JDBC `setReadOnly(True)`。
3. **零提交与无条件回滚 (`ConnectionHub.readonly_session`)**：只读路径零 `commit()`，退出 `finally` 无条件 `rollback()`。
4. **行数/超时/步数熔断**：单步上限 `200` 行、`12s` 超时，单轮最多 `8` 步熔断。
