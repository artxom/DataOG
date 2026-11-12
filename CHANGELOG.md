# Changelog

本项目的所有重要版本演进与工程变更均记录于此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号严格遵循 [语义化版本 (SemVer 2.0.0)](https://semver.org/lang/zh-CN/)。

## [Unreleased]

---

## [2.2.0] - 2026-11-12

### Added
- **AI 只读智能数据探查模块 (`modules/data_explorer/` — `/api/modules/data_explorer`)**：
  - 新增目标驱动的多轮大模型自动数据探查引擎（`POST /explore`），零新增第三方依赖直接复用 `LLMHub`（DeepSeek / Google Gemini / OpenAI 兼容网关）与 `MetadataHub` 库表字段上下文，支持数据分布画像、空值/重复/异常脏数据诊断、主外键与跨表关联推断、业务指标核对。
  - 新增探查会话与采样轨迹持久化管理接口（`GET /sessions`、`POST /sessions`、`GET /sessions/{session_id}`、`PATCH /sessions/{session_id}`、`DELETE /sessions/{session_id}`），通过 `PlatformStateStore.register_table_schema` 幂等注册 `explorer_sessions` 与 `explorer_messages` 表。
- **平台底座只读 SQL 沙箱与跨驱动硬件只读通道 (`core/platform/`)**：
  - 新增 `core/platform/readonly_guard.py`（`validate_readonly_sql`）与平台只读执行接口 `POST /api/platform/query/execute-readonly`。
  - 新增 `ConnectionHub.readonly_session` 与 `QueryEngine.execute_readonly_sql`，统一施加单步采样行数上限（默认 `100` 行，硬上限 `200` 行）与短超时熔断（默认 `12s`）。
- **macOS 风格「数据探查」双模工作台 (`static/js/ui/database_tree.js` & `static/js/views/`)**：
  - 顶栏新增 `SQL 查询` | `AI 探查` 分段切换，完整保留目录树浏览、双击表预览、自由 SQL 执行与 CSV/Excel 导出。
  - 左侧继续沿用 DataGrip 风格四级已连接数据库目录树（`连接` → `Schema` → `表/视图` → `字段`），在 `AI 探查` 模式下支持直接在树节点内联勾选焦点表（含 Schema 级全选与 `indeterminate` 半选态）。
  - 新增 `explorer_ai_pane.js`，采用原生 `<details>` 折叠面板透明展示每一步执行的采样 SQL、耗时、行集预览与结构化结论卡片，支持一键将探查 SQL 填入主编辑器复核导出，并兼容中文输入法（IME）安全回车发送。

### Changed
- 升级微内核与应用清单版本至 `2.2.0`，扩充全量自动化单元与红队集成测试至 **30 项**。

### Security
- **四道引擎级绝对只读硬隔离防线（绝不依赖 Prompt 软约束）**：
  1. **双模态词法/语法白名单（`ReadOnlySqlGuard`）**：同时在标准 SQL 与反斜杠转义双模式下确定性分词以消除转义歧义偷渡，阻断 MySQL 条件可执行注释 `/*! ... */` 与未闭合注释/Dollar-Quoting，强制单语句（禁 `;` 堆叠），顶层仅放行 `SELECT`、`WITH ... SELECT`（递归校验全部 CTE 子句严禁数据修改型 CTE）、元数据 `SHOW`、`DESCRIBE`/`DESC` 与非 `ANALYZE` `EXPLAIN`，全语句拦截一切写/DDL/DCL/TCL/锁/`INTO` 关键字及跨方言危险控制/文件函数（含双引号标识符包裹调用）。
  2. **数据库驱动会话级硬件只读锁**：PostgreSQL/DWS 强制 `set_session(readonly=True, autocommit=False)` + `SET TRANSACTION READ ONLY`；MySQL 系强制 `SET SESSION TRANSACTION READ ONLY` + `START TRANSACTION READ ONLY`；SQLite 强制 URI `?mode=ro` + `PRAGMA query_only = ON`；JDBC 强制 `setAutoCommit(False)` + `setReadOnly(True)` 并禁用 `commit()`。
  3. **零提交与退出无条件回滚**：只读执行路径零 `commit()` 调用，`readonly_session` 在 `finally:` 中无条件调用 `rollback()`。
  4. **输出二次清洗与步数熔断**：单轮自动探查硬上限 `8` 步熔断，并对 AI 返回的 `recommended_sql` 再次执行只读白名单校验，防止恶意写语句被诱导填入主编辑器。

---

## [2.1.1] - 2026-11-12

### Changed
- **全工程代码与文件深度瘦身**：
  - 清理移除已被 v2.x 模块化架构完全替代的历史冗余代码与一次性脚本：`core/cross_env_engine.py`（169 KB 历史副本）、`core/state_store.py`、`pytest.bat`、`run_tests.bat`、`static/js/ui/split_pane.js`、`static/styles.css`、`示例_DWS字段贯标清单.xlsx`，并精简 `app.py` 中 450+ 行未使用的 v1.0 遗留路由。
  - `static/index.html` 直接加载 `/css/tokens.css` 与 `/css/workbench.css`，消除 `@import` 二次串行请求。
- **核心依赖精简与零 SDK 大模型通信 (`requirements.txt` & `core/platform/llm_hub.py`)**：
  - `LLMHub` 改用 Python 标准库（`urllib.request` / `ssl` / `json` + 安全重定向协议校验）直连 DeepSeek、Google Gemini 及 OpenAI 兼容端点；`tests/test_modules_and_api.py` 改用轻量标准库内存 ASGI 客户端。
  - `requirements.txt` 从 15 项精简为 **8 项纯运行时核心依赖**，彻底移除 `openai`（含 `jiter` / `tqdm` / `distro`）、`httpx`（含 `httpcore` / `certifi`）、`pytest`，并将 `pyinstaller` 隔离至打包构建阶段按需安装。
- **Windows `DataOG.exe` 打包体积优化 (`build_exe.py`)**：
  - 消除 `static/` 目录在 `_internal/static` 与外层目录的重复拷贝；精细化收敛 `pydantic_core` / `psycopg2` 二进制收集规则，显式排除 `openai`、`httpx`、`lxml`、`cryptography`、`pytest`、`psycopg2.tests`、`uvicorn.protocols.websockets` 等非运行时模块，并在打包后自动剥离 `.pyi` / `.pxd` / `__pycache__` 冗余文件。

### Fixed
- 修复 `core/platform/dialects/view_lineage.py` 中上游视图投影列同时带有显式 `CAST(...)` 与泛化原类型（如 Hive/Inceptor `STRING`）时未优先升级为 `CAST` 推导精度类型的问题。
- 修复标准库内存 ASGI 测试客户端在 `BaseHTTPMiddleware` 流式文件响应期间过早返回 `http.disconnect` 导致响应体被提前取消的生命周期竞态。

---

## [2.1.0] - 2026-11-12

### Added
- **macOS 桌面窗口体系与四大独立应用模块 (`static/`)**：
  - 桌面主画布与顶部 28px 毛玻璃全局菜单栏（支持 `⌘K` Spotlight 库表字段秒级搜索、AI 状态指示、运行日志弹窗、自动/浅色/深色模式切换与实时时钟）。
  - 真实红黄绿窗口生命周期：🔴 **关闭 (`✕`)** 返回桌面并彻底重置当前工具界面状态；🟡 **最小化 (`−`)** 将当前工作缩略进屏幕右下角毛玻璃小抽屉（完整保留内存 DOM 与未保存编辑内容，点击抽屉卡片即刻恢复工作现场）；🟢 **最大化 (`+`)** 切换留白桌面窗口与全屏沉浸画布。
  - 桌面拆解为四大高内聚模块：**「系统配置」**（外观与图标 / 数据库连接 / JDBC 驱动 / AI 模型）、**「数据探查」**（目录树 + SQL 查询与导出）、**「数据标准」**（整合单库贯标与三栏跨环境比对）、**「多邻国」**（整合智能命名与跨方言翻译）。
- **本地桌面壁纸与软件/模块图标个性化中心 (`core/platform/asset_store.py`)**：
  - 支持在「系统配置 → 外观与图标」上传自定义桌面壁纸（支持 `铺满` / `适应` 切换）、自定义 `Data.O.G` 软件主图标（动态覆盖 `/favicon.ico`）及四大模块专属图标，本地持久化保存于 `data/assets/` 与 `data/dataog_state.db`，支持单槽位或一键恢复默认并自动清理旧文件。
- **多服务商 AI 大模型统一接入中枢 (`core/platform/llm_hub.py`)**：
  - 采用官方 `openai` SDK 兼容协议，内置 **DeepSeek**（`https://api.deepseek.com`）、**Google Gemini**（`https://generativelanguage.googleapis.com/v1beta/openai/`）与自定义 OpenAI 兼容网关预设，支持连通性毫秒级检测与远端模型列表拉取（自动兼容剥离 Gemini `models/` 前缀）。
- **「多邻国」AI 智能数仓命名与异构方言深度适配 (`modules/ddl_translator/`)**：
  - 新增 `POST /api/modules/ddl_translator/naming`：将中文表名/字段名按数仓分层前缀、词根缩写与命名风格（`snake_case` / `UPPER_SNAKE` / `camelCase`）转换为标准英文标识符与推荐 SQL 类型（支持大模型生成与内置 170+ 金融数仓词根引擎双驱动），一键生成建表 DDL 草稿。
  - 增强 `POST /api/modules/ddl_translator/translate`：新增直接粘贴 `CREATE TABLE` SQL 解析与 `ai_assist` 大模型跨方言语法/函数深度适配（`AST+AI` 融合模式）。

### Changed
- 正式升级产品官方品牌展示名为 **`Data.O.G`**，全面移除旧界面中冗长解释性副标题、提示段落与版本号徽标噪音，采用极简无框 macOS 视觉语言（`tokens.css` 升级 `color-scheme` 与 `light-dark()` 双色令牌）。
- 扩充全量自动化单测与集成测试至 **27 项**（新增 `AssetStore`、`LLMHub` 与 `Polyglot` 端到端测试），同步升级 `build_exe.py` 以收集 `openai` 依赖并初始化 `data/assets` 目录。

### Security
- 自定义壁纸与图标上传严格执行扩展名与光栅二进制魔数头双重白名单校验（仅允许 PNG / JPEG / WebP / GIF，彻底阻断 SVG / HTML 脚本注入），统一重命名为随机 UUID 隔离存储于非静态目录 `data/assets/`，读取接口强制附加 `X-Content-Type-Options: nosniff` 与 `Content-Security-Policy: default-src 'none'; sandbox` 响应头，并将 `data/assets/` 纳入 `.gitignore`。
- AI 大模型 API Key 严格加密脱敏回显（`mask_secret_key`），严禁在任何公开 API 或运行日志中泄露明文密钥，并拦截非 `http/https` 或内嵌凭据的异常 Base URL。

---

## [2.0.0] - 2026-11-11

### Added
- **FastAPI 微内核与四大全局共享底座 (`core/platform/`)**：
  - **双轨驱动引擎 (`DriverManager`) 与全局连接池 (`ConnectionHub`)**：内置免 Java 原生 Python 驱动通道（覆盖华为云 `DWS`、`PostgreSQL`、星环 `Inceptor`/Hive、`MySQL`/OceanBase/Doris/StarRocks/TiDB、`SQLite`），同时支持界面上传标准 JDBC `.jar` 驱动包（内置达梦 `DM8`、人大金仓 `KingbaseES`、`Oracle`、`DB2`、`GBase` 模板），按需懒加载 `JPype1` + 独立 `URLClassLoader`，无 Java 运行时绝不影响原生驱动启动。
  - **全局持久化元数据共享中枢 (`MetadataHub`)**：支持深度适配库高性能探查与任意自定义 JDBC 驱动 `java.sql.DatabaseMetaData` 通用回退探查，本地 SQLite 结构化缓存库表字段、主键、分布键、分区键与视图血缘，全模块零延迟共享与关键词秒级检索。
  - **通用简单查询引擎 (`QueryEngine`) 与方言血缘库 (`core/platform/dialects/`)**：提供跨驱动 SQL 执行与双击表节点快速预览（默认 `LIMIT 500`、最高 `5000` 行安全下推截断、耗时毫秒统计、CSV/Excel `.xlsx` 导出），抽离跨方言类型归一化矩阵与四层递归视图血缘穿透器。
- **插件化独立业务模块 (`modules/`)**：
  - `modules/standard_migrator`：单库批量落标贯标执行台（完整保留 `DIRECT_ALTER` / `TABLE_REBUILD` 物理列保序迁移、Kahn 拓扑改名排序、分区边界继承与多线程并发能力）。
  - `modules/cross_comparator`：16:9 跨环境三栏数标比对中心（来源库 × 数据标准 × 目标库 三方双向桥接绑定、Excel/JSON 导入导出、实时重算差异并一键推送待改造项至贯标台）。
  - `modules/ddl_translator`：提拔为独立一级模块「跨方言建表 DDL 翻译与四层视图穿透器」。
- **类 DataGrip / DBeaver 原生 ES Modules 统一 IDE 工作台 (`static/`)**：
  - 零 Node 构建链，支持浅色/深色/跟随系统主题切换，统一封装 `DatabaseTree`、`DataGrid`、`ObjectPicker`、`SqlCodeBox`、`Modal`（原生 `<dialog>`）、`Toast`、`SplitPane` 公共组件库，新增全局数据源驱动管理视图与轻量 SQL 查询浏览台。
- **自动化测试与 Windows `DataOG.exe` 持续交付流水线**：
  - 扩充至 24 项单元与集成红线测试（`tests/test_core.py`、`tests/test_platform.py`、`tests/test_modules_and_api.py`），升级 `build_exe.py` 与 `.github/workflows/ci.yml` 实现 Ubuntu + Windows 双平台自动测试、PyInstaller 独立打包、`DataOG.exe --test` 冻结态自检及 GitHub Release 自动发布。

### Changed
- 运行时状态库统一升级为 `data/dataog_state.db`（WAL 模式），启动时自动检测历史 `data/dws_migrator_state.db` 及旧版 `app_meta` 连接配置并无损迁移为全局命名数据源档案，支持模块表幂等列演进。

### Fixed
- 修复 Windows CI 控制台 `cp1252`/`gbk` 编码导致的中文输出 `UnicodeEncodeError` 及 PyInstaller 冻结态模块零互引字节码自检兼容问题。
- 修复多线程并发环境下 `JPype1` 跨线程调用 JDBC 缺少 `attachThreadToJVM()` 的潜在故障。
- 修复左侧数据库目录树单击重绘阻断双击表预览、单 Schema 无法折叠、贯标搜索框重绘失焦及跨环境新建手动字段桥接保存丢失等问题。

### Security
- 严格绑定 `127.0.0.1` 本地回环地址，注入 CSP 与 `Sec-Fetch-Site` 跨站请求拦截响应头，全接口密码脱敏返回。
- JDBC `.jar` 上传严格限制 64MB 体积、校验 ZIP/JAR 魔数头、拦截 POSIX 与反斜杠 Zip-Slip 路径穿越，并在加载前后双重校验 SHA-256 指纹。
- 前端全量采用 `createElement` / `textContent` 安全渲染 DOM（零 `innerHTML`），通过自动化静态扫描测试强制守卫。

---

## [1.0.0] - 2026-11-11

### Added
- 初始里程碑基线：完成华为云 DWS 单库物理列保序批量贯标引擎、16:9 跨环境（Inceptor/PG/DWS）三栏数标比对工作台、跨方言建表 DDL 翻译及上游四层视图血缘递归穿透解析核心能力。
