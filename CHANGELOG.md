# Changelog

本项目的所有重要版本演进与工程变更均记录于此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号严格遵循 [语义化版本 (SemVer 2.0.0)](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Changed
- 完善 `AGENTS.md` 与 `README.md` 中的工程交付闭环规范、代码目录图谱及 `v2.0.0` 会话交接基线快照，建立“每次任务完成同步更新项目文档并按 GitHub Flow 规范提交推送”的常态化工程制度。

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
