# DataOG v2.0.0 — 类 DBeaver / DataGrip 底座的多功能数据工程瑞士军刀

**DataOG** 是一款基于 Python Web（`FastAPI` + `Pydantic` + 零构建原生 ES Modules 模块化前端）打造的**多功能数据工程与数据治理一体化工作台**。

它结合了 **DBeaver / DataGrip 式的核心数据底座**（双轨制广泛数据库连接、支持用户界面上传标准 JDBC `.jar` 驱动、全局持久化元数据缓存图谱、轻量 SQL 查询与表数据浏览）与 **面向企业级数仓实战的可插拔专业工具箱**（单库物理保序批量贯标、16:9 跨环境三栏数标比对、跨方言 DDL 翻译与四层视图血缘穿透），既可在 macOS / Windows 浏览器中提供统一现代的明暗双色 IDE 交互体验，又支持一键打包为独立免安装 Windows `DataOG.exe` 桌面程序。

- **GitHub 仓库**：https://github.com/artxom/DataOG
- **项目架构记忆与工程规范**：详见 [AGENTS.md](./AGENTS.md)
- **版本变更记录**：详见 [CHANGELOG.md](./CHANGELOG.md)

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

## 二、项目目录结构速览

```text
DataOG/
├── app.py                        # FastAPI 微内核入口（安全响应头、UTF-8/Windows 防挂起、路由装配）
├── core/
│   ├── platform/                 # 全局共享底座（挂载于 /api/platform）
│   │   ├── driver_manager.py     # 双轨驱动引擎（Python 原生驱动 + 自定义 JDBC .jar 懒加载）
│   │   ├── connection_hub.py     # 全局数据源档案池与连接会话上下文
│   │   ├── metadata_hub.py       # 双层元数据探查器与本地 SQLite 缓存检索中枢
│   │   ├── query_engine.py       # 跨驱动安全截断查询、表数据预览与 CSV/Excel 导出引擎
│   │   ├── storage.py            # data/dataog_state.db WAL 状态库、旧版无损迁移与幂等列演进
│   │   └── dialects/             # 标识符清洗、类型映射矩阵、DDL 生成与四层视图血缘穿透
│   ├── dws_engine.py             # 单库保序重建、Kahn 拓扑改名与分区继承核心引擎
│   └── cross_env_engine.py       # 跨环境数标匹配、差异分类与桥接计算核心引擎
├── modules/                      # 相互独立的插件化业务模块（零跨模块私有导入）
│   ├── standard_migrator/        # /api/modules/standard_migrator 单库批量落标贯标台
│   ├── cross_comparator/         # /api/modules/cross_comparator  16:9 跨环境三栏数标比对中心
│   └── ddl_translator/           # /api/modules/ddl_translator    跨方言建表翻译与视图穿透器
├── static/                       # 零 Node 构建链原生 ES Modules 前端 IDE 工作台
│   ├── css/                      # tokens.css（明暗双主题变量）与 workbench.css（IDE 骨架样式）
│   └── js/                       # core/（API/DOM/状态）、ui/（公共组件库）、views/（五大业务视图）
├── tests/                        # 全量自动化测试门禁（核心算法 / 平台底座 / 模块隔离与安全扫描）
├── build_exe.py                  # Windows DataOG.exe PyInstaller 自动化门禁打包脚本
├── AGENTS.md                     # 项目架构铁律、会话交接快照与工程协作规范
└── CHANGELOG.md                  # 遵循 Keep a Changelog 的版本演进日志
```

---

## 三、里程碑版本演进记录

| 里程碑版本 | 分支演进 | 核心交付物 |
| :--- | :--- | :--- |
| **`v1.0.0`**（已完成） | `main` | 完成单库保序贯标、16:9 三栏跨环境数标比对、多方言 DDL 翻译与视图穿透核心算法基线 |
| **`v2.0.0`**（当前正式版） | `refactor/dataog-workbench` → `main` | 完成 `FastAPI` 微内核升级、四大全局底座 (`DriverManager` / `ConnectionHub` / `MetadataHub` / `QueryEngine`)、`modules/*` 三大插件解耦、类 DataGrip 原生 ES Module 工作台与 `DataOG.exe` 正式版打包流水线 |

---

## 四、本地运行、测试打包与工程协作规范

### 1. 开发环境启动与全量自动化测试门禁
```bash
# 安装依赖
python3 -m pip install -r requirements.txt

# 运行全量自动化单元测试（24 项核心引擎、平台底座、跨模块隔离与前端零 XSS 安全校验）
python3 -m unittest discover -s tests -p "test_*.py" -v

# 运行应用启动与健康路由自检
python3 app.py --test

# 启动本地 IDE 工作台（严格监听 http://127.0.0.1:18080）
python3 app.py
```

### 2. 在 Windows 下打包为独立免安装程序 (`DataOG.exe`)
```bash
# 双击 build_windows.bat 或直接执行打包脚本（打包前自动运行全量单测门禁，产出于 dist/DataOG/DataOG.exe）
python build_exe.py
```

### 3. 持续演进与 GitHub 提交最佳实践
- **文档同步闭环**：每次完成功能开发、架构调整或缺陷修复，均同步更新 [AGENTS.md](./AGENTS.md)（最新架构基线与会话交接记忆）、[README.md](./README.md) 与 [CHANGELOG.md](./CHANGELOG.md)。
- **GitHub Flow 与门禁**：从 `main` 切分语义化工作分支（`feat/*`、`fix/*`、`refactor/*`、`docs/*`），采用原子化 Conventional Commits 提交，通过本地全量单测与 `python3 app.py --test` 双门禁后合并回 `main`、清理临时分支并同步推送到 GitHub `origin` 触发 CI 验证。
