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
- **安全与本地隔离**：默认仅监听 `127.0.0.1`；`data/*.db`、`data/*.log` 及用户上传的驱动目录 `data/drivers/` 必须被 `.gitignore` 排除，严禁将任何数据库密码或本地状态提交至 Git；日志输出严禁打印明文密码。
- **状态库前后向无损兼容**：运行时状态库统一为 `data/dataog_state.db`（启动时若检测到历史 `dws_migrator_state.db` 与旧版连接键值，必须自动无损迁移）；所有 SQLite 表结构演进必须通过幂等列自检机制（新增列带安全 `DEFAULT`），保障跨分支切换与版本回退零报错。
- **Windows `.exe` 交付保障**：保留 Windows CMD 快速编辑模式禁用（防点击挂起）、静态目录多路径探测、PyInstaller 隔离临时目录构建等工程保护逻辑。新增任何第三方依赖前必须先通过依赖安全扫描校验。

## 4. 现代化 Git 分支、合并与版本管理规范
- **分支模型（GitHub Flow）**：
  - `main`：稳定主干分支，随时保持测试全绿、可运行、可打包。
  - `refactor/<主题>`：底座升级与模块化重构分支（如 `refactor/platform-foundation`、`refactor/modularize-domain-tools`）。
  - `feat/<模块名>`：新功能或工作台特性分支（如 `feat/ide-workbench-and-query`）。
  - `fix/<问题名>`：缺陷修复分支。
- **准入与发版要求**：
  - 提交信息遵循 Conventional Commits（`feat:` / `refactor:` / `fix:` / `docs:` / `test:` / `chore:`）。
  - 合并回 `main` 前必须通过全量自动化单元测试（`python3 -m unittest discover -s tests -p "test_*.py" -v`）与 `python3 app.py --test` 启动自检。
  - 按语义化版本（SemVer：`v1.1.0`、`v1.2.0`、`v2.0.0`）打附注 Tag 推送 GitHub，触发 `.github/workflows/ci.yml` 自动测试与 Windows `.exe` Release 打包发布。
