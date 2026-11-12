# Data.O.G 项目记忆与架构工程规范 (AGENTS.md)

## 1. 项目定位与核心对标
- **官方品牌名称**：**`Data.O.G`**（GitHub 远程仓库：`https://github.com/artxom/DataOG.git`，Windows 独立打包文件名保持 `DataOG.exe`）。
- **产品形态**：基于 Python Web 技术栈构建的 **macOS 桌面级极简美学 × 多功能数据工程与数据治理“瑞士军刀”一体化系统**（支持 macOS / Windows 本地与内网运行，支持一键打包为独立 Windows `.exe`）。
- **核心设计哲学**：
  1. **强底座共享**：连接数据库、加载自定义 JDBC 驱动、执行基础数据查询、采集缓存库表字段元数据、管理 AI 大模型 API（DeepSeek / Gemini / OpenAI 兼容网关）及持久化本地壁纸与自定义图标，均属于**平台级公共能力**，配置一次、全平台所有子模块零延迟共享。
  2. **macOS 桌面化模块隔离**：界面严禁堆砌冗长解释性副标题与版本号噪音；桌面提供独立超椭圆应用图标与真实红黄绿窗口生命周期（关闭重置状态、最小化停靠右下角毛玻璃小抽屉保留现场、最大化全屏切换），每个业务模块各自独立定制演进，后端模块间严格零私有互引。

## 2. 核心分层与模块化铁律
所有代码设计与功能迭代必须严格遵循 **「平台微内核底座 (`core/platform/`) + macOS 桌面窗口外壳与公共组件 (`static/js/`) + 独立业务功能模块 (`modules/<module_id>/`)」** 架构：

### 2.1 全局共享底座层 (`core/platform/`)
严禁任何业务模块私自管理数据库密码或 AI API Key、或重复编写系统目录查询 SQL，一律通过 `PlatformContext` 依赖注入调用底座六大中枢：
1. **双轨驱动引擎与全局连接中心 (`DriverManager` & `ConnectionHub`)**：
   - **内置原生通道（免 Java 开箱即用）**：内置纯 Python / 主流 DB-API 驱动（`psycopg2` 覆盖 PG/华为 DWS/openGauss/金仓、`PyMySQL` 覆盖 MySQL/OceanBase/Doris/StarRocks/TiDB、标准库 `sqlite3`、纯 Python `InceptorThriftClient` 覆盖星环 Inceptor/Hive）。
   - **自定义 JDBC 扩展通道（用户上传 `.jar` 驱动）**：支持用户在界面上传标准 JDBC `.jar` 驱动包（安全隔离存放于非静态目录 `data/drivers/`，校验文件头与 SHA-256 指纹），通过按需懒加载的 `JPype1` + 独立 `URLClassLoader` 连接达梦 DM8、Oracle、DB2、GBase 等任意数据库（未安装 JRE 时绝不影响内置原生驱动正常启动运行）。
   - **全局连接池 (`ConnectionHub`)**：统一管理命名数据源档案（Connection Profiles），所有模块通过 `connection_id` 引用。
2. **全局元数据共享中枢 (`MetadataHub`)**：
   - 统一标准化元数据模型：`Catalog/Schema` → `Table/View/MatView` → `ColumnMeta`（物理列序 `ordinal_position`、原始类型与归一化类型、非空 `is_not_null`、默认值、注释、主键/索引、分布键/分桶键、分区键）。
   - **双层探查与本地持久化缓存**：针对深度适配库（DWS/PG/Inceptor/MySQL）使用定制高性能探查器；针对任意自定义 JDBC 驱动库自动回退使用标准 `java.sql.DatabaseMetaData` 通用探查器；采集结果持久化缓存至本地 SQLite 元数据索引表，支持增量刷新与 `⌘K` Spotlight 全局秒级检索。
3. **通用数据查询、预览与硬件级只读沙箱中心 (`QueryEngine` & `ReadOnlySqlGuard`)**：
   - 提供跨驱动统一的自由 SQL 查询与表数据快速预览能力，输出标准 `QueryResult`（列头类型描述、行集、耗时、默认 `LIMIT 500`/上限 `5000` 安全截断、查询超时保护、CSV/Excel 导出）。
   - 内置双模态词法/语法级只读白名单校验器（`readonly_guard.py`）与跨驱动会话级硬件只读执行通道（`execute_readonly_sql` + `ConnectionHub.readonly_session` 退出无条件 `rollback()` + 零 `commit()`），供 AI 探查等零副作用场景复用。
4. **AI 大模型统一接入中枢 (`LLMHub`)**：
   - 参考 Cherry Studio 多服务商范式，采用零额外第三方依赖的 Python 标准库（`urllib.request` / `ssl` / `json`）原生实现 OpenAI 兼容 REST 协议管理 `llm_providers` 表，内置 **DeepSeek**（`https://api.deepseek.com`）、**Google Gemini**（`https://generativelanguage.googleapis.com/v1beta/openai/`）及自定义 OpenAI 兼容网关预设，支持连通性检测、远端模型拉取（自动剥离 Gemini `models/` 前缀）与结构化 JSON 输出。
5. **本地壁纸与自定义图标资源中心 (`AssetStore`)**：
   - 支持用户上传自定义桌面壁纸（支持 `铺满`/`适应`）、`Data.O.G` 软件主图标（同步动态覆盖 `/favicon.ico`）及四大桌面模块图标；严格执行光栅图像魔数头白名单（PNG/JPEG/WebP/GIF，阻断 SVG 脚本）、UUID 随机重命名落盘于 `data/assets/` 并持久化映射至 `data/dataog_state.db`。
6. **通用方言、血缘与基础设施 (`Dialects` / `StateStore` / `ExcelIO`)**：
   - 统一提供 SQL 标识符清洗、跨方言类型归一化与转换矩阵、上游视图四层递归血缘穿透解析、SQLite WAL 模块表幂等注册迁移与 Excel 导入导出基础能力。

### 2.2 后端模块化路由与前端 macOS 桌面工作台 (`modules/` & `static/`)
- **后端技术选型**：采用 **`FastAPI` + `Pydantic` + `Uvicorn`**。每个业务模块位于独立的 `modules/<module_id>/` 目录（含 `router.py`、`schemas.py`、`service.py`），通过 `APIRouter` 注册至 `/api/modules/<module_id>`，模块之间严禁相互私有导入，跨模块协作一律经由底座定义的数据契约或状态存储接口完成。
- **前端 macOS 桌面级架构（零 Node 构建链）**：
  - 采用浏览器原生 **ES Modules (`<script type="module">`)** + CSS `light-dark()` / `color-scheme` 明暗双主题令牌，不引入 Webpack/Vite 等编译链，保持静态资源零构建运行与 PyInstaller 直接打包能力。
  - **macOS 桌面与窗口语义**：
    - 顶部 28px 毛玻璃菜单栏（`Data.O.G` 品牌归位、活动应用名、AI 状态指示、`⌘K` Spotlight 搜索、运行日志弹窗、深浅色切换、实时时钟）。
    - 桌面四大独立应用图标：**`系统配置`** (`settings`)、**`数据探查`** (`explorer`)、**`数据标准`** (`standard`：整合 `贯标` | `比对`)、**`多邻国`** (`polyglot`：整合 `智能命名` | `方言翻译`)。
    - 左上角红黄绿三键语义：🔴 **关闭 (`✕`)** 回到桌面并彻底销毁重置当前工具运行时状态；🟡 **最小化 (`−`)** 缩略停靠至屏幕右下角毛玻璃小抽屉（100% 保留内存 DOM、光标与未保存输入，点击抽屉即刻恢复工作现场）；🟢 **最大化 (`+`)** 切换常规窗口与全屏沉浸工作态。
  - **公共 UI 组件库 (`static/js/ui/`)**：统一复用 `DatabaseTree`、`DataGrid`、`ObjectPicker`、`SqlCodeBox`、`Modal`（原生 `<dialog>` + 坐标判定轻关闭兜底）、`Toast`，严格使用 `textContent` / `createElement` 等安全 DOM 操作杜绝 XSS。

## 3. 安全、数据兼容与跨平台打包红线
- **安全与本地隔离**：默认仅监听 `127.0.0.1:18080`（启用 CSP 与 `Sec-Fetch-Site` 跨站请求防护）；`data/*.db`、`data/*.log`、驱动目录 `data/drivers/` 及个性化图片目录 `data/assets/` 必须被 `.gitignore` 排除，严禁将任何数据库密码、AI API Key 或本地状态提交至 Git；所有接口返回与日志输出必须脱敏密码与密钥；`.jar` 驱动与壁纸/图标上传均须严格限制体积、校验二进制魔数头并校验 `realpath` 目录边界防路径穿越，资产读取响应强制携带 `X-Content-Type-Options: nosniff` 与独立 `sandbox` CSP。
- **前端零 XSS 与非阻塞交互红线**：前端代码严禁出现 `innerHTML`、`outerHTML`、`insertAdjacentHTML` 及阻塞式 `window.alert`/`confirm`/`prompt`，统一通过 `static/js/core/dom.js` 安全节点工厂与原生 `<dialog>` (`Modal`) 渲染（由单测静态扫描强制守卫）。
- **状态库前后向无损兼容**：运行时状态库统一为 `data/dataog_state.db`（启动时自动从历史 `dws_migrator_state.db` 及旧版连接键值无损迁移为命名数据源档案）；所有 SQLite 表结构演进必须通过 `register_table_schema` 幂等列自检机制（新增列带安全 `DEFAULT`），保障跨分支切换与版本回退零报错。
- **Windows `.exe` 与冻结态自检保障**：保留 Windows CMD 快速编辑模式禁用（防点击挂起）、标准流 UTF-8 重配置（防 `cp1252`/`gbk` 编码崩溃）、静态目录多路径探测、多线程 JPype `attachThreadToJVM()` 及 PyInstaller 轻量化裁剪保护（排除 `openai` / `httpx` / `lxml` / `cryptography` / `pytest` 等非必要包与 `.pyi` 类型桩）；模块零互引校验兼容源码 AST 与冻结态 `pkgutil` + `dis` 字节码自省，确保 `DataOG.exe --test` 独立自检通过。

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
- **当前主干里程碑版本**：`v2.2.0`（官方品牌名 `Data.O.G`，数据探查 · AI 只读智能探查会话版，`main` 分支与 `v2.2.0` 标签同步至 `origin`）。
- **微内核入口与构建脚本**：
  - `app.py`：FastAPI `2.2.0` 微内核入口（`create_app`、动态 `/favicon.ico`、UTF-8 流保护、Windows 防挂起、安全头/CSRF 中间件、挂载平台路由与四大插件模块路由，仅保留 `/api/bootstrap` 只读脱敏兼容探针）。
  - `requirements.txt`：保持 8 项纯运行时核心依赖（`fastapi`、`pydantic`、`uvicorn`、`psycopg2-binary`、`PyMySQL`、`JPype1`、`openpyxl`、`python-multipart`），零新增第三方依赖。
  - `build_exe.py` / `build_windows.bat` / `.github/workflows/ci.yml`：自动执行单测门禁后轻量化打包 `dist/DataOG/DataOG.exe`（去除重复静态资源拷贝、排除非运行时重型包与测试子模块、自动剥离 `.pyi`/`.pxd`/`__pycache__` 冗余元数据，保障冻结态 `--test` 自检）。
- **全局共享底座 (`core/platform/`) — 挂载前缀 `/api/platform`**：
  - `context.py` (`PlatformContext`) / `router.py` / `schemas.py` / `models.py`
  - `storage.py` (`PlatformStateStore`，SQLite WAL 状态库 `data/dataog_state.db`，暴露线程安全 `module_db()` 上下文，核心与模块表：`app_meta`、`connection_profiles`、`jdbc_drivers`、`llm_providers`、`meta_schemas`、`meta_tables`、`meta_columns`、`migration_items`、`cross_env_standards`、`cross_table_mappings`、`cross_column_bindings`、`explorer_sessions`、`explorer_messages`、`execution_logs`)
  - `readonly_guard.py` (`validate_readonly_sql` / `ReadOnlyViolationError`，双模态 ANSI + 反斜杠转义词法分词器、单语句校验、`SELECT`/`WITH ... SELECT`/`SHOW`/`DESCRIBE`/非 `ANALYZE` `EXPLAIN` 顶层白名单、全语句写/DDL/锁/`INTO` 与跨方言危险系统函数黑名单)
  - `asset_store.py` (`AssetStore`，管理壁纸预设、本地上传壁纸、`Data.O.G` 主图标与四大模块自定义图标持久化至 `data/assets/`，挂载 `/appearance*` 与 `/assets/{asset_name}`)
  - `llm_hub.py` (`LLMHub`，基于 Python 标准库 `urllib.request` 零额外依赖统一接入 DeepSeek、Google Gemini 与自定义 OpenAI 兼容网关，挂载 `/llm/providers*`、`/llm/test`、`/llm/models`)
  - `driver_manager.py` & `inceptor_client.py` (`DriverManager`，双轨驱动：`NATIVE` + 按需懒加载 `JDBC`，支持 `open_connection(..., readonly=True)` 会话级硬件只读锁)
  - `connection_hub.py`（含 `readonly_session` 退出无条件 `rollback()`） / `metadata_hub.py` / `query_engine.py`（含 `execute_readonly_sql` 与 `POST /query/execute-readonly`） / `excel_io.py` / `dialects/`
- **独立业务插件模块 (`modules/`，严格零跨模块互引)**：
  - `modules/standard_migrator/`（`/api/modules/standard_migrator`）：单库批量落标贯标执行台（依赖 `core/dws_engine.py` 实现 `DIRECT_ALTER` / `TABLE_REBUILD` 物理列保序迁移、Kahn 拓扑改名、分区继承、并发作业池）。
  - `modules/cross_comparator/`（`/api/modules/cross_comparator`）：跨环境三栏数标比对中心（三方双向桥接绑定、Excel/JSON 导入导出、一键推送至贯标台）。
  - `modules/ddl_translator/`（`/api/modules/ddl_translator`）：支撑「多邻国」模块，含 `POST /naming`（AI 大模型 + 内置 170+ 数仓标准词根双驱动智能命名与建表草稿生成）与 `POST /translate`（数据源库表/粘贴 `CREATE TABLE` 或 `VIEW`/自由异构 SQL 跨方言互译、四层递归视图穿透与 `AST+AI` 语法适配）。
  - `modules/data_explorer/`（`/api/modules/data_explorer`）：支撑「数据探查 · AI 探查」多轮目标驱动只读探查会话，含 `/sessions*` 会话历史管理与 `POST /explore` 多步自动采样推理循环（单步采样上限 `200` 行、`12s` 超时、单轮最多 `8` 步熔断及 `recommended_sql` 二次只读清洗）。
- **前端原生 ES Modules macOS 桌面工作台 (`static/`)**：
  - `index.html`、`css/tokens.css`（明暗双主题与内置壁纸变量）、`css/workbench.css`、`js/main.js`（macOS 桌面、红黄绿窗口控制器、右下角最小化抽屉、`⌘K` Spotlight、日志控制台）
  - 核心层 `js/core/`：`api.js`、`dom.js`、`state.js`
  - 公共组件 `js/ui/`：`database_tree.js`（DataGrip 风格四级已连接数据库目录树，支持 `single` 浏览与 `multi` 内联 Schema/表级勾选及半选态）、`data_grid.js`、`object_picker.js`、`sql_code_box.js`、`modal.js`、`toast.js`
  - 四大桌面应用视图 `js/views/`：
    - **系统配置**：`settings_view.js` + `connections_view.js`（外观与图标 / 数据库连接 / JDBC 驱动 / AI 模型）
    - **数据探查**：`sql_console_view.js` + `explorer_ai_pane.js`（`SQL 查询` | `AI 探查`，共用左侧 DataGrip 四级数据库清单并支持探查 SQL 一键同步至主编辑器）
    - **数据标准**：`standard_view.js` + `standard_migrator_view.js` + `cross_comparator_view.js`（贯标 | 比对）
    - **多邻国**：`ddl_translator_view.js`（智能命名 | 方言翻译）
- **全量自动化测试套件 (`tests/`，当前共 30 项测试全绿，内置标准库内存 ASGI 测试客户端免 `httpx` 依赖)**：
  - `tests/test_core.py`（13 项）：验证单库保序重建、拓扑改名、分区继承、三栏比对、DDL 互译与四层视图穿透核心引擎。
  - `tests/test_platform.py`（10 项）：验证旧版状态库无损迁移、双轨驱动 `.jar` 安全校验与无 Java 降级、`MetadataHub` 缓存检索、`QueryEngine` 安全截断、`AssetStore` 光栅魔数头与路径穿越防护、`LLMHub` 标准库 HTTP 回环与密钥脱敏、`ReadOnlySqlGuard` 红队绕过拦截矩阵及跨驱动硬件只读回滚防线。
  - `tests/test_modules_and_api.py`（7 项）：验证 FastAPI 全路由契约、外观与自定义图标端到端流转、多邻国智能命名与 `AST+AI` 方言适配、AI 只读数据探查多步采样与幻觉写语句实时拦截保护、跨环境推送贯标闭环、插件模块零互引（AST/字节码双模）及前端零 `innerHTML` 安全红线。

## 6. AI 只读探查硬隔离铁律与后续演进守则
- **绝对只读四道硬防线（后续任何涉及 AI 自动查询的功能均须严格复用 `QueryEngine.execute_readonly_sql`，严禁仅靠 Prompt 软约束）**：
  1. **双模态词法/语法白名单（`core/platform/readonly_guard.py`）**：同时在标准 SQL 与反斜杠转义双模式下分词，强制单语句，顶层仅放行纯只读语句，递归校验 CTE 子句，全语句封禁写/DDL/锁/`INTO` 与危险系统函数。
  2. **数据库会话级硬件只读锁（`DriverManager.open_connection(..., readonly=True)`）**：SQLite `mode=ro` + `PRAGMA query_only=ON`、PostgreSQL/DWS `set_session(readonly=True, autocommit=False)`、PyMySQL `SET SESSION TRANSACTION READ ONLY` + `START TRANSACTION READ ONLY`、JDBC `setAutoCommit(False)` + `setReadOnly(True)`。
  3. **零提交与退出无条件回滚（`ConnectionHub.readonly_session`）**：只读执行路径零 `commit()` 调用，会话 `finally:` 块无条件执行 `rollback()`。
  4. **采样行数、超时与步数熔断**：单步强制采样上限 `200` 行、`12s` 超时，单轮自动探查最多 `8` 步熔断。
