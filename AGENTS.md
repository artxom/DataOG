# DataOG 项目记忆与架构工程规范 (AGENTS.md)

## 1. 项目定位与核心对标
- **项目名称**：**DataOG**（远程仓库：`https://github.com/artxom/DataOG.git`）。
- **产品形态**：基于 Python Web 技术栈构建的**类 DBeaver / DataGrip 体验的多功能数据工程与数据治理“瑞士军刀”一体化工作台**（支持 macOS / Windows 本地与内网运行，支持一键打包为独立 Windows `.exe`）。
- **核心设计哲学**：
  1. **强底座共享**：连接数据库、加载自定义驱动、执行基础数据查询、采集并缓存核心库表字段元数据属于**平台级公共能力**，连接一次、全平台所有子模块零延迟共享。
  2. **高内聚插件化**：具体业务场景（单库落标贯标、跨环境三栏数标比对、跨方言 DDL 翻译与视图穿透，以及未来新增的数据工具）一律作为**相互独立的插件模块**挂载，拥有独立路由与独立前端视图，复用统一 UI 组件库与全局元数据中枢。

## 2. 核心分层与模块化铁律
所有代码设计与功能迭代必须严格遵循 **「平台微内核底座 (`core/platform/`) + 统一 IDE 工作台外壳 (`static/js/ui/` & `shell`) + 独立业务功能模块 (`modules/<module_id>/`)」** 架构：

### 2.1 全局数据底座层 (`core/platform/`)
严禁任何业务模块私自管理数据库账号密码或重复编写系统目录查询 SQL，一律通过 `PlatformContext` 依赖注入调用底座四大中枢：
1. **双轨驱动引擎与全局连接中心 (`DriverManager` & `ConnectionHub`)**：
   - **内置原生通道（免 Java 开箱即用）**：内置纯 Python / 主流 DB-API 驱动（`psycopg2` 覆盖 PG/华为 DWS/openGauss/金仓、`PyMySQL` 覆盖 MySQL/OceanBase/Doris/StarRocks/TiDB、标准库 `sqlite3`、纯 Python `InceptorThriftClient` 覆盖星环 Inceptor/Hive）。
   - **自定义 JDBC 扩展通道（用户上传 `.jar` 驱动）**：支持用户在界面上传标准 JDBC `.jar` 驱动包（安全隔离存放于非静态目录 `data/drivers/`，校验文件头与 SHA-256 指纹），通过按需懒加载的 `JPype1` + 独立 `URLClassLoader` 连接达梦 DM8、Oracle、DB2、GBase 等任意数据库（未安装 JRE 时绝不影响内置原生驱动正常启动运行）。
   - **全局连接池 (`ConnectionHub`)**：统一管理命名数据源档案（Connection Profiles），所有模块通过 `connection_id` 引用。
2. **全局元数据共享中枢 (`MetadataHub`)**：
   - 统一标准化元数据模型：`Catalog/Schema` → `Table/View/MatView` → `ColumnMeta`（物理列序 `ordinal_position`、原始类型与归一化类型、非空、默认值、注释、主键/索引、分布键/分桶键、分区键）。
   - **双层探查与本地持久化缓存**：针对深度适配库（DWS/PG/Inceptor/MySQL）使用定制高性能探查器；针对任意自定义 JDBC 驱动库自动回退使用标准 `java.sql.DatabaseMetaData` 通用探查器；采集结果持久化缓存至本地 SQLite 元数据索引表，支持增量刷新与全文检索，供全平台所有功能模块直接读取。
3. **通用数据查询与预览中心 (`QueryEngine`)**：
   - 提供跨驱动统一的 SQL 查询与表数据快速预览能力，输出标准 `QueryResult`（列头类型描述、行集、耗时、安全行数截断如默认 `LIMIT 500`、查询超时保护）。
4. **通用方言、血缘与基础设施 (`Dialects` / `StateStore` / `TaskLogger` / `ExcelIO`)**：
   - 统一提供 SQL 标识符清洗、跨方言类型归一化与转换矩阵、上游视图四层递归血缘穿透解析、SQLite WAL 模块表幂等注册迁移、多线程作业池、实时增量日志流及 Excel 导入导出基础能力。

### 2.2 后端模块化路由与前端 IDE 式工作台 (`modules/` & `static/`)
- **后端技术选型**：采用 **`FastAPI` + `Pydantic` + `Uvicorn`**（平滑兼容底层 Starlette）。每个业务模块位于独立的 `modules/<module_id>/` 目录（含 `router.py`、`schemas.py`、`service.py`），通过 `APIRouter` 注册至 `/api/modules/<module_id>`，模块之间严禁相互私有导入，跨模块协作（如从三栏比对推送改造项至贯标执行台）一律经由底座定义的数据契约或状态存储接口完成。
- **前端统一 IDE 交互架构（零 Node 构建链）**：
  - 采用浏览器原生 **ES Modules (`<script type="module">`)** + 统一 CSS Design Tokens（支持亮暗双主题），不引入 Webpack/Vite 等前端重型编译链，保持静态资源零构建运行与 PyInstaller 直接打包能力。
  - **界面标准骨架**：`[最左侧 Activity Bar 模块图标栏]` + `[左侧可折叠 Database Navigator 全局数据库目录树]` + `[右侧 Module Workspace 模块专属画布与上下文工具栏]` + `[底部可折叠全局任务与 SQL 日志面板]`。
  - **公共 UI 组件库 (`static/js/ui/`)**：所有模块统一复用标准组件（`DataGrid` 数据网格、`ObjectPicker` 库表级联选择器、`SqlCodeBox` SQL 编辑预览框、`Modal`、`Toast`、`SplitPane`），确保全应用视觉与交互高度一致，并严格使用 `textContent` / `createElement` 等安全 DOM 操作杜绝 XSS。

## 3. 安全、数据兼容与跨平台打包红线
- **安全与本地隔离**：默认仅监听 `127.0.0.1:18080`（启用 CSP 与 `Sec-Fetch-Site` 跨站请求防护）；`data/*.db`、`data/*.log` 及用户上传的驱动目录 `data/drivers/` 必须被 `.gitignore` 排除，严禁将任何数据库密码或本地状态提交至 Git；所有接口返回与日志输出必须脱敏密码；`.jar` 驱动上传严格限制大小、校验 `PK\x03\x04` 魔数头、拦截 POSIX/Windows 路径穿越并校验 SHA-256 指纹。
- **前端零 XSS 与非阻塞交互红线**：前端代码严禁出现 `innerHTML`、`outerHTML`、`insertAdjacentHTML` 及阻塞式 `window.alert`/`confirm`/`prompt`，统一通过 `static/js/core/dom.js` 安全节点工厂与原生 `<dialog>` (`Modal`) 渲染（由单测静态扫描强制守卫）。
- **状态库前后向无损兼容**：运行时状态库统一为 `data/dataog_state.db`（启动时自动从历史 `dws_migrator_state.db` 及 `dws_conn_saved` / `dws_connection_config` / `cross_source_conn` / `cross_target_conn` 无损迁移为命名数据源档案）；所有 SQLite 表结构演进必须通过 `register_table_schema` 幂等列自检机制（新增列带安全 `DEFAULT`），保障跨分支切换与版本回退零报错。
- **Windows `.exe` 与冻结态自检保障**：保留 Windows CMD 快速编辑模式禁用（防点击挂起）、标准流 UTF-8 重配置（防 `cp1252`/`gbk` 编码崩溃）、静态目录多路径探测、多线程 JPype `attachThreadToJVM()` 及 PyInstaller 隔离构建保护；模块零互引校验需同时兼容源码 AST 与 PyInstaller 冻结态 `pkgutil` + `dis` 字节码自省，确保 `DataOG.exe --test` 独立自检通过。新增任何第三方依赖前必须先通过依赖安全扫描校验。

## 4. 工程交付闭环：文档同步、GitHub Flow 与最佳实践（强制执行）
每次完成任何开发、重构、缺陷修复或工程变更任务时，**必须完整执行以下「文档同步 + 规范提交 + GitHub 远端闭环」流程**，不得遗漏：
1. **每次收尾强制同步项目文档（供下次会话无缝衔接）**：
   - **`AGENTS.md`**：若模块职责、目录拓扑、数据表、路由契约、版本号或工程红线发生变化，立即更新本文档第 5 节基线快照与对应规范章节，确保下一次新会话启动时零成本获取最新上下文。
   - **`README.md`**：同步更新对外架构说明、功能模块列表、目录速览与运行/打包指引。
   - **`CHANGELOG.md`**：遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，在 `[Unreleased]` 或对应正式版本下按 `Added` / `Changed` / `Fixed` / `Security` 记录核心变更。
2. **分支模型（标准 GitHub Flow）**：
   - `main`：受保护语义的稳定主干分支，随时保持测试全绿、文档同步、可直接运行与打包。
   - 工作分支一律从最新干净的 `main` 切出：`feat/<模块名>`（新特性）、`fix/<问题名>`（缺陷修复）、`refactor/<主题>`（架构重构）、`docs/<主题>`（文档与规范更新）、`chore/<主题>`（构建与工程维护）。
3. **原子提交、双门禁校验与 GitHub 同步规范**：
   - **提交规范**：遵循 Conventional Commits（格式：`<type>(<scope>): <中文简明摘要>`），保持提交原子化；提交前执行 `git status` 与 `git diff` 逐项核查，严禁混入未跟踪的临时文件或本地状态库。
   - **合并门禁**：合并回 `main` 前必须 100% 通过全量自动化单测（`python3 -m unittest discover -s tests -p "test_*.py" -v`）与启动自检（`python3 app.py --test`）。
   - **推送、发版与分支清理**：合并回 `main` 后推送至 `origin`；里程碑发版按 SemVer（如 `v2.0.0`、`v2.1.0`）创建附注标签（`git tag -a vX.Y.Z -m "..."`）并推送标签以触发 `.github/workflows/ci.yml` 自动测试与 Windows `.exe` Release 发布；合并完成后及时删除已使命终结的临时本地与远端工作分支，保持 GitHub 仓库分支树整洁，并确认远端 GitHub Actions CI 状态全绿。

## 5. 当前代码基线快照（供下次会话快速定位）
- **当前主干里程碑版本**：`v2.0.0`（`main` 分支已同步至 `origin/main`，GitHub Release `v2.0.0` 已发布含 `DataOG-Windows-x64-v2.0.0.zip`）。
- **微内核入口与构建脚本**：
  - `app.py`：FastAPI 微内核入口（`create_app`、UTF-8 流保护、Windows 防挂起、安全头/CSRF 中间件、挂载平台路由与三大插件模块路由，并保留 `/api/*` 脱敏兼容路由）。
  - `build_exe.py` / `build_windows.bat` / `.github/workflows/ci.yml`：自动执行单测门禁后打包 `dist/DataOG/DataOG.exe`（含 `--hidden-import=unittest.mock` 与 `--collect-submodules=httpx` 保障冻结态 `--test` 自检）。
- **全局共享底座 (`core/platform/`) — 挂载前缀 `/api/platform`**：
  - `context.py` (`PlatformContext`) / `router.py` / `schemas.py` / `models.py`
  - `storage.py` (`PlatformStateStore`，SQLite WAL 状态库 `data/dataog_state.db`，核心表：`app_meta`、`connection_profiles`、`jdbc_drivers`、`meta_schemas`、`meta_tables`、`meta_columns`、`migration_items`、`cross_env_standards`、`cross_table_mappings`、`cross_column_bindings`、`execution_logs`)
  - `driver_manager.py` & `inceptor_client.py` (`DriverManager`，双轨驱动：`NATIVE` 原生通道覆盖 `DWS`/`PGSQL`/`INCEPTOR`/`MYSQL`/`SQLITE`；`JDBC` 通道按需懒加载 `JPype1` + `URLClassLoader` 覆盖 `DM8`/`KINGBASE`/`ORACLE`/`DB2`/`GBASE` 及自定义 `.jar`)
  - `connection_hub.py` (`ConnectionHub`，统一 `connection_id` 档案管理、密码脱敏与连接会话上下文)
  - `metadata_hub.py` (`MetadataHub`，定制探查器 + `DatabaseMetaData` 通用回退、四层视图穿透、SQLite 元数据索引与全局搜索)
  - `query_engine.py` & `excel_io.py` (`QueryEngine`，默认 `LIMIT 500`/上限 `5000` SQL 下推截断 + JDBC `setMaxRows`、耗时统计、CSV/XLSX 导出)
  - `dialects/`：`identifiers.py`、`types.py`、`dws_ddl.py`、`ddl_builder.py`、`view_lineage.py`（底层核心算法亦兼容复用于 `core/dws_engine.py` 与 `core/cross_env_engine.py`）
- **独立业务插件模块 (`modules/`，严格零跨模块互引)**：
  - `modules/standard_migrator/`（`/api/modules/standard_migrator`）：单库批量落标贯标执行台（`DIRECT_ALTER` / `TABLE_REBUILD` 物理列保序迁移、Kahn 拓扑改名、分区继承、并发作业池）。
  - `modules/cross_comparator/`（`/api/modules/cross_comparator`）：16:9 跨环境三栏数标比对中心（三方双向桥接绑定、Excel/JSON 导入导出、实时差异计算并一键推送至贯标台）。
  - `modules/ddl_translator/`（`/api/modules/ddl_translator`）：跨方言建表 DDL 翻译与四层递归视图血缘穿透器。
- **前端原生 ES Modules IDE 工作台 (`static/`)**：
  - `index.html`、`css/tokens.css`（明暗双主题变量）、`css/workbench.css`、`js/main.js`
  - 核心层 `js/core/`：`api.js`、`dom.js`、`state.js`
  - 公共组件 `js/ui/`：`database_tree.js`、`data_grid.js`、`object_picker.js`、`sql_code_box.js`、`modal.js`、`toast.js`、`split_pane.js`
  - 模块视图 `js/views/`：`connections_view.js`、`sql_console_view.js`、`standard_migrator_view.js`、`cross_comparator_view.js`、`ddl_translator_view.js`
- **全量自动化测试套件 (`tests/`，当前共 24 项测试全绿)**：
  - `tests/test_core.py`：保留并验证单库保序重建、拓扑改名、分区继承、三栏比对、DDL 互译与四层视图穿透核心引擎测试。
  - `tests/test_platform.py`：验证旧版状态库无损迁移、双轨驱动 `.jar` 安全校验与无 Java 降级、`MetadataHub` 缓存检索、`QueryEngine` 安全截断与导出。
  - `tests/test_modules_and_api.py`：验证 FastAPI 全路由契约、跨环境推送贯标闭环、插件模块零互引（AST/字节码双模）及前端零 `innerHTML` 安全红线。
