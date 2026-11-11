# Changelog

本项目的所有重要版本演进与工程变更均记录于此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号严格遵循 [语义化版本 (SemVer 2.0.0)](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
