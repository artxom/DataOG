# DataOG v2.0.0 — 类 DBeaver / DataGrip 底座的多功能数据工程瑞士军刀

**DataOG** 是一款基于 Python Web（`FastAPI` + `Pydantic` + 零构建原生 ES Modules 模块化前端）打造的**多功能数据工程与数据治理一体化工作台**。

它结合了 **DBeaver / DataGrip 式的核心数据底座**（双轨制广泛数据库连接、支持用户界面上传标准 JDBC `.jar` 驱动、全局持久化元数据缓存图谱、轻量 SQL 查询与表数据浏览）与 **面向企业级数仓实战的可插拔专业工具箱**（单库物理保序批量贯标、16:9 跨环境三栏数标比对、跨方言 DDL 翻译与四层视图血缘穿透），既可在 macOS / Windows 浏览器中提供统一现代的明暗双色 IDE 交互体验，又支持一键打包为独立免安装 Windows `DataOG.exe` 桌面程序。

- **GitHub 仓库**：https://github.com/artxom/DataOG
- **项目架构记忆与工程规范**：详见 [AGENTS.md](./AGENTS.md)

---

## 一、核心架构设计：强底座共享 × 独立插件化模块

DataOG 采用 **「`core/platform/` 四大全局平台中枢 + `static/js/` 统一 IDE 工作台外壳 + `modules/*` 独立插件化业务模块」** 分层架构，数据库“一次连接、元数据全局零延迟共享”，业务模块间零私有互引：

### 1. DataOG 全局核心微内核底座 (`core/platform/`)
1. **双轨驱动引擎与全局连接中心 (`driver_manager.py` & `connection_hub.py`)**
   - **内置 Python 原生驱动通道（免 Java 开箱即用）**：开箱直接支持 **华为云 GaussDB(`DWS`)**、**PostgreSQL (`PGSQL`)**、**星环 Transwarp (`Inceptor`/Hive)**（内置纯 Python HiveServer2 Thrift 协议客户端）、**MySQL / OceanBase / Doris / StarRocks / TiDB (`PyMySQL`)**、**SQLite (`sqlite3`)**。
   - **自定义 JDBC `.jar` 驱动扩展通道**：内置达梦 `DM8`、人大金仓 `KingbaseES`、Oracle、DB2、GBase 模板，支持用户在界面上传标准 JDBC `.jar` 驱动包（安全隔离存放于 `data/drivers/`，自动校验 ZIP/JAR 魔数头、路径穿越防护与 SHA-256 指纹），按需懒加载 `JPype1` + 独立 `URLClassLoader`（未安装 JRE 时绝不影响内置原生驱动正常运行）。
2. **状态库无损迁移与模块幂等演进 (`storage.py`)**
   - 运行时状态库统一升级为 `data/dataog_state.db`；启动时自动检测旧版 `data/dws_migrator_state.db` 及历史连接键值（`dws_conn_saved`、`dws_connection_config`、`cross_source_conn`、`cross_target_conn`），无损迁移为全局命名数据源档案，并支持模块级 SQLite 表幂等列自检。
3. **全局持久化元数据共享中枢 (`metadata_hub.py`)**
   - 对深度适配库走定制高性能探查，对任意自定义 JDBC 库自动回退至标准 `java.sql.DatabaseMetaData` 通用探查，将 `Schema → 表/视图 → 字段/类型/注释/主键/分布键/分区键` 持久化缓存至本地 SQLite 索引表，支持增量刷新与全局关键词秒级检索。
4. **通用简单查询引擎与方言血缘库 (`query_engine.py` & `dialects/`)**
   - 提供跨驱动统一的 SQL 查询与双击表节点快速预览能力（默认 `LIMIT 500` 安全截断、耗时毫秒统计、超时保护、CSV / Excel `.xlsx` 导出），以及标识符清洗、跨方言类型归一化矩阵与上游视图四层递归血缘穿透解析。

### 2. 类 DataGrip 统一 IDE 工作台与三大独立插件模块 (`modules/` & `static/`)
- **零 Node 构建链前端**：原生 `<script type="module">` + 明暗双色 CSS Design Tokens（支持跟随系统/浅色/深色切换）+ 严格安全 DOM 渲染（零 `innerHTML`）。
- **标准 IDE 骨架**：`[最左 Activity Bar 模块图标栏]` + `[左侧可折叠 Database Navigator 全局数据库目录树]` + `[右侧模块专属工作区与上下文工具栏]` + `[底部可折叠全局任务与 SQL 审计日志抽屉]`。
- **公共组件库 (`static/js/ui/`)**：`DataGrid`、`DatabaseTree`、`ObjectPicker`、`SqlCodeBox`、`Modal`（原生 `<dialog>`）、`Toast`、`SplitPane`。
- **内置与插件模块矩阵**：
  - `connections`：**全局数据源与双轨驱动中心**（支持上传管理 `.jar` 驱动包）
  - `sql_console`：**轻量 SQL 查询与表数据浏览台**（双击左侧树表/视图节点直接预览数据、字段结构与原始 DDL）
  - `modules/standard_migrator/`：**单库批量落标贯标执行台**（100% 保留 `DIRECT_ALTER` / `TABLE_REBUILD` 物理列保序迁移、Kahn 拓扑改名排序、分区边界继承与多线程并发）
  - `modules/cross_comparator/`：**16:9 跨环境三栏数标比对中心**（来源库 × 数据标准 × 目标库 三方双向桥接绑定、Excel/JSON 导入导出、一键推送待改造项至贯标台闭环）
  - `modules/ddl_translator/`：**跨方言建表 DDL 翻译与四层视图穿透器**（独立一级模块，覆盖 DWS / PGSQL / Inceptor / MySQL / DM8 / Oracle / SQLite 建表互译与四层递归视图血缘穿透）

---

## 二、里程碑版本演进记录

| 里程碑版本 | 分支名称 | 核心交付物 |
| :--- | :--- | :--- |
| **`v1.0.0`**（已完成） | `main` | 完成单库保序贯标、16:9 三栏跨环境数标比对、多方言 DDL 翻译与视图穿透核心算法基线 |
| **`v2.0.0`**（当前正式版） | `refactor/dataog-workbench` → `main` | 完成 `FastAPI` 微内核升级、四大全局底座 (`DriverManager` / `ConnectionHub` / `MetadataHub` / `QueryEngine`)、`modules/*` 三大插件解耦、类 DataGrip 原生 ES Module 工作台与 `DataOG.exe` 正式版打包流水线 |

---

## 三、本地运行、测试与 Windows `DataOG.exe` 打包说明

### 1. 开发环境启动与全量自动化测试
```bash
# 安装依赖
python3 -m pip install -r requirements.txt

# 运行全量自动化单元测试与应用启动自检
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 app.py --test

# 启动本地 IDE 工作台（严格监听 http://127.0.0.1:18080）
python3 app.py
```

### 2. 在 Windows 下打包为独立免安装程序 (`DataOG.exe`)
```bash
# 双击 build_windows.bat 或直接执行打包脚本（打包前自动运行全量单测门禁）
python build_exe.py
```
