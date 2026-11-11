# DataOG 项目记忆与架构工程规范 (AGENTS.md)

## 1. 项目定位与愿景
- **项目名称**：**DataOG**（远程仓库：`https://github.com/artxom/DataOG.git`）。
- **核心定位**：面向异构数仓与数据治理场景的“瑞士军刀”式多功能本地桌面/Web 一体化工具箱（支持 macOS / Windows 开发运行，支持一键打包为独立免安装 Windows `.exe`）。
- **演进方向**：以已验证成功的「数仓字段落标贯标 + 跨环境三栏数标比对 + 多方言建表翻译与视图穿透」（`v1.0.0` 基线）为起点，将通用数据底座能力向上提升为 DataOG 全局共享能力，将具体业务场景拆分为可插拔、低耦合的功能模块，持续迭代扩展新的数据工程小工具。

## 2. 核心分层与模块化铁律
在设计、重构或新增任何功能时，必须严格遵守 **「全局数据底座（Platform Core） + 业务场景模块（Feature Modules）」** 两层解耦架构，严禁在单一业务模块中重复实现通用数据能力：

### 2.1 全局数据底座层 (`core/platform/`)
凡属跨场景复用的基础能力，一律沉淀至全局底座：
1. **全局数据源连接中心 (`ConnectionHub`)**：
   - 统一管理命名数据源连接档案（Connection Profiles，含连接 ID、别名、环境标签、库类型、主机端口、库名、凭据及扩展参数）。
   - 所有业务子模块统一通过 `connection_id` 引用全局数据源，严禁各子模块私自维护孤立的账号密码配置。
2. **异构数据库驱动与元数据探查 (`Connectors`)**：
   - 基于统一的 `BaseConnector` 接口封装不同数据库驱动（当前支持：华为云 `DWS`、`PGSQL`、星环 `INCEPTOR` 纯 Python Thrift 客户端；后续新增数据源仅需新增对应 Connector 实现）。
   - 统一输出标准化的 `SchemaInfo`、`TableMeta`、`ColumnMeta` 元数据模型（涵盖物理列序 `ordinal_position`、数据类型、注释、默认值、非空约束、分布键/分桶键、分区键等）。
3. **通用方言、类型系统与血缘解析 (`Dialects`)**：
   - 统一提供 SQL 标识符清洗与自然格式化、跨异构库数据类型归一化、跨方言类型等价判断与映射、视图 SQL 四层穿透解析等通用能力。
4. **本地持久化与后台任务基础设施 (`StateStore` / `TaskRunner` / `ExcelIO`)**：
   - 基于 SQLite WAL 模式的线程安全本地存储，支持底座核心表与各业务模块按表名前缀独立注册表结构及幂等列迁移。
   - 统一管理多线程并发作业池、增量实时日志流及通用 Excel/JSON 导入导出辅助能力。

### 2.2 业务场景模块层 (`modules/<module_id>/`)
每个具体使用场景作为独立模块开发与挂载（如单库批量贯标 `standard_migrator`、跨环境三栏比对 `cross_comparator`、多方言建表翻译 `ddl_translator` 及后续新增工具）：
- **后端高内聚**：每个模块拥有独立的目录（包含路由 `routes.py`、业务逻辑 `service.py`），通过全局模块注册表挂载至 `/api/modules/<module_id>/...`，避免修改全局主入口产生分支合并冲突。
- **前端原生模块化**：前端采用 **App Shell（工作台外壳） + 浏览器原生 ES Modules (`<script type="module">`)** 组织在 `static/js/platform/` 与 `static/js/modules/<module_id>/` 下，**不引入 Node.js / Webpack / Vite 等前端重型打包链**，确保零前端编译门槛且兼容 PyInstaller 直接打包静态目录。
- **模块间协作**：模块间禁止直接耦合内部私有实现；如需跨模块流转数据（如从「三栏数标比对」推送待改造项至「单库贯标执行台」），通过标准数据结构或底座状态存储接口进行显式交接。

## 3. 技术栈与跨平台打包约束
- **主流极简技术栈**：坚持 `Python 3.11+` + `Starlette` + `Uvicorn` + 标准库 `sqlite3` + `psycopg2-binary` + `openpyxl`。非必要不引入重型 ORM、外部中间件或包含复杂 C 扩展的冷门第三方库。
- **Windows `.exe` 交付保障**：
  - 始终保持纯 Python 协议栈优势（如星环 Inceptor 的纯 Python HiveServer2 Thrift 实现），确保 Windows 目标机器无需安装 Python 或数据库客户端即可双击运行。
  - 保留 Windows CMD 快速编辑模式禁用（防点击挂起）、静态目录多路径探测、PyInstaller 临时目录隔离构建等工程保护逻辑。
- **本地数据前后向兼容红线**：
  - `data/*.db` 与 `data/*.log` 包含用户真实数据库凭据与工作状态，必须被 `.gitignore` 严格排除，严禁提交至 Git。
  - 任何 SQLite 表结构变更必须通过幂等自检机制（新增列提供安全 `DEFAULT` 值，旧配置升级提供自动无损迁移逻辑），确保用户在切换 Git 分支、升级新版本或回退历史版本时本地已保存的数据源与业务配置不丢失、不报错。

## 4. 现代化 Git 分支、合并与版本管理规范
- **分支模型（GitHub Flow 精简主流实践）**：
  - `main`：稳定主干分支，仅接收已完成验证与测试通过的合并，随时处于可运行、可打包发布状态。
  - `refactor/<主题>`：架构演进与模块化重构分支（如 `refactor/platform-core`、`refactor/modular-workbench`）。
  - `feat/<模块或功能名>`：新功能模块或现有模块特性迭代分支（如 `feat/table-data-diff`）。
  - `fix/<问题描述>`：缺陷修复分支。
- **提交与合并准入要求**：
  - 提交说明遵循语义化前缀：`feat:`、`refactor:`、`fix:`、`docs:`、`test:`、`chore:`。
  - 合并回 `main` 前，必须通过全量单元测试（`python -m unittest discover -s tests -p "test_*.py" -v`）及应用自检（`python app.py --test`）。
- **语义化版本（SemVer）与 Release 发布**：
  - `v1.0.0`：初始贯标与跨环境数标比对单应用基线。
  - 后续每次重大底座升级、新模块上线或缺陷修复，按 `vMAJOR.MINOR.PATCH` 打附注标签（`git tag -a vX.Y.Z -m "..."`）并推送至 GitHub，由 `.github/workflows/ci.yml` 自动完成云端测试、Windows `.exe` 构建与 GitHub Releases 归档。

