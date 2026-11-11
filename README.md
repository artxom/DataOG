# 数仓字段贯标与跨环境数标比对工作台 (`dws-column-migrator`)

专为 **华为云 GaussDB(DWS)**、**PostgreSQL (`PGSQL`)** 及 **星环科技 Transwarp (`Inceptor`)** 等数据仓库设计的字段标准化（贯标）无损转换与 **16:9 三纵列跨环境数标比对** 桌面/Web 控制台工具。

---

## 一、核心功能概览（双模式互通架构）

系统顶栏提供 **「单库批量贯标执行台」** 与 **「跨环境三栏数标比对中心 (16:9 三纵列)」** 双模式 Tab 一键切换，两套视图底层共享 SQLite 本地状态库与落标清单数据。

---

## 二、模式一：单库批量贯标执行台（已完成）

支持导入包含「修改前字段名、修改前类型、修改前注释、修改后字段名、修改后类型、修改后注释」的多 Sheet Excel 贯标清单，实时与数据库系统目录（`pg_attribute` / `pg_description` / `pg_partition`）比对状态，并在**保证全表字段物理顺序（`attnum`）100% 不变**的前提下完成字段改名、类型转换与注释更新。

### 1. 全表物理顺序 100% 保序（双引擎架构）
- **原生无损 `ALTER TABLE`（`DIRECT_ALTER`）**：
  在单个事务中依次执行 `RENAME COLUMN`、`ALTER COLUMN ... TYPE ... USING`、`COMMENT ON COLUMN`，并在提交前再次查询 `pg_attribute` 核验 `attnum` 物理序号未发生任何偏移。
- **按原表物理顺序重建表并迁移数据（`TABLE_REBUILD`）**：
  突破华为 DWS **分布列（`DISTRIBUTE BY HASH(...)`）不允许修改类型**、**分区键修改受限**以及**列存表（`ORIENTATION=COLUMN`）跨类型原地转换受限**的限制：
  - 自动通过 `pg_catalog.pg_get_tabledef` 与 `pg_catalog.pg_partition` 读取原表 `WITH (...)` 存储属性、`DISTRIBUTE BY` 分布键、`PARTITION BY` 分区策略与全部起始分区边界定义，以及按 `attnum ASC` 排列的全表所有字段；
  - 自动规范化含空格的存储属性（如将 `period=1 week` 规范包裹为 `period='1 week'`），并在重建表时完整继承原表的起始分区定义与默认值（`DEFAULT`）、非空约束（`NOT NULL`）；
  - 在单个事务内按原表精确列顺序创建新结构表、通过 `INSERT INTO ... SELECT CAST(...)` 转换迁移全量数据、核验前后数据行数 100% 一致、恢复全部表注释与列注释，最后原子切换表名（支持保留 `__bak_` 原表备份）。

### 2. 表级智能路由与多线程并发执行（默认 5 线程）
- **以表为单位智能决策**：批量提交任务时按 `(schema_name, table_name)` 自动聚合。只要某张表选中的任务里包含**分布键或分区键被调整**（或存在字段互换改名环路 `A <-> B`），该表所有选中任务统一采用**按原表顺序重建表（`TABLE_REBUILD`）**一次性完成；若全表选中任务均不涉及分布键/分区键调整，则采用**原生 `ALTER COLUMN`（`DIRECT_ALTER`）**执行；若执行中途触发 DWS 分布键/分区键限制，自动将该表剩余任务无缝升级为重建表模式。
- **多线程按表并发**：支持可配置的多线程并发池（默认 5 线程，可在前端切换 1/3/5/8/10 线程），每个线程独立负责不同的数据表，互不争抢同表锁。

### 3. 单表多字段智能编排与防冲突引擎
- **物理列 1 对 1 精确绑定**：按原表 `ordinal_position` 绑定改造任务，并在映射分布键与分区键时执行单次映射与去重，彻底杜绝链式改名（如 `col_a -> col_b` 且 `col_b -> col_c`）时在 `CREATE TABLE`、`INSERT INTO` 或 `DISTRIBUTE BY` 中出现 `column specified more than once` 报错。
- **链式改名拓扑排序（Kahn 算法）**：在原生 `ALTER` 模式下自动计算同一张表内多个字段的改名依赖顺序（例如先执行 `col_b -> col_c` 腾出列名，再执行 `col_a -> col_b`），杜绝因执行顺序颠倒引起的 `column already exists` 报错。
- **重名冲突预拦截**：若清单中将同一张表的两个不同原字段配置为同一个目标字段名，或目标字段名与表中未改名的现有列重名，执行前自动拦截并标注冲突详情。

### 4. 自然标识符、类型归一化与实时滚动日志
- **自然字段名生成**：自动剥离 Excel 清单或历史库表中混入的中英文引号（`"`、`'`、`“`、`”`），对标准 SQL 标识符统一生成不带双引号的自然字段名（如 `loan_status`）。
- **完备的类型等价识别**：内置 `normalize_dws_type` 与 `normalize_cross_type` 归一化器，完整识别 `time without time zone` / `time(0) without time zone` 与 `TIME`、`timestamp without time zone` 与 `TIMESTAMP`、`character varying(n)` / `varchar2(n)` 与 `VARCHAR(n)`、`numeric(p,s)` / `number(p,s)` 与 `DECIMAL(p,s)` 等等价类型。
- **450ms 前台实时滚动日志流**：内置轻量级 `/api/logs` 增量轮询通道，批量并发执行期间前台底部日志面板实时滚动打印后台各线程的执行进度与 SQL 明细。

---

## 三、模式二：跨环境三栏数标比对中心（16:9 三条纵贯线，已完成）

面向「来源库 × 数据标准 × 目标库」跨环境落标核验场景，将 16:9 屏幕一分为三，通过落标清单与绑定的数据标准将三者串联，**以表为单位**沿三条纵贯线直观对比。

### 1. 16:9 三纵列视觉与交互结构（三条纵贯线）
- **左列（来源库纵贯线 Source DB）**：
  - 顶部提供来源库连接配置按钮（支持 `PGSQL` / `华为 DWS` / `星环 Inceptor`）、模式与表名绑定检索框（支持自动补全列表）及表内字段搜索框；
  - 纵贯线主体按物理列顺序（`#1..#N`）展示来源表各字段的名称、数据类型、注释、分布/分区键标识，以及已绑定的中间数标字段。
- **中列（数据标准与三方绑定中枢 Data Standard Hub）**：
  - 顶部支持上传数标/落标清单、按状态筛选（全部 / 仅看待落标差异 / 仅看目标已落标 / 仅看两库不一致）及「智能同名重新绑定」；
  - 纵贯线主体逐项展示标准字段英文名、标准类型、标准注释与清单原字段参考；每张卡片内嵌 **`[来源库绑定列] ⟵ 数标绑定 ⟶ [目标库绑定列]`** 双向桥接器，支持随时手工调整左右映射并一键持久保存。
- **右列（目标库纵贯线 Target DB）**：
  - 顶部提供目标库连接配置（默认自动继承单库执行台的 DWS 连接配置，也可独立切换为任意 `PGSQL` / `DWS` / `Inceptor` 库）、目标表检索框及字段过滤框；
  - 纵贯线主体按物理列顺序展示目标表各字段的实际状态，并用色彩轴线高亮区分「已符合数标（绿）」、「待贯标改造（橙）」与「未绑定数标」。

### 2. 多异构数据源驱动支持
- **华为云 GaussDB (`DWS`)**：默认端口 `8000`，基于 `psycopg2` 驱动，支持 `pg_attribute`、`pgxc_class` 分布键与分区表元数据探查。
- **PostgreSQL (`PGSQL`)**：默认端口 `5432`，基于 `psycopg2` 驱动，完整支持模式与表结构探查。
- **星环科技 Transwarp (`Inceptor`)**：默认端口 `10000`，内置纯 Python `InceptorThriftClient`（标准 HiveServer2 Thrift Binary + SASL PLAIN 协议，支持 `LDAP` 用户名密码认证、`NONE` 免密认证与 `NOSASL` 传输模式，打包为 Windows `.exe` 时无需外部 C 编译依赖）。

### 3. 绑定关系本地持久化与批量导入导出
- **SQLite 本地持久存储**：所有表级映射（`cross_table_bindings`）、字段级双向绑定（`cross_column_bindings`）以及左右库连接参数均持久存储在本地 `data/dws_migrator_state.db` 中。
- **批量导入与导出**：
  - **导出绑定包 (`.json`)**：通过 `/api/cross/bindings/export.json` 一键导出包含全部表映射与字段绑定关系的 JSON 配置包；
  - **导出三方比对与绑定表 (`.xlsx`)**：通过 `/api/cross/bindings/export.xlsx` 导出包含来源库、数据标准、目标库三方完整字段对比状态与绑定关系的 Excel 报表（该 `.xlsx` 报表同样支持直接重新导入以批量修改绑定）；
  - **一键推送至贯标执行台**：点击顶部「推送至贯标执行台」，即可将目标库待落标字段一键推送至模式一执行台，直接复用 5 线程并发 `ALTER` 或按原序重建表能力完成落标闭环。

### 4. 跨环境建表语句导出、多方言智能翻译器与视图四层穿透 (`PGSQL` ⟷ `华为 DWS` ⟷ `星环 Inceptor`)
- **任意环境选表或视图，一键翻译为目标环境可执行建表语句**：
  - 在三栏比对中心顶栏点击「跨库建表 DDL 翻译器」，或在左列（来源库）、右列（目标库）表头直接点击「导出/翻译建表 DDL」（无需预先导入中间数标亦可直接读取单表或视图），即可生成带 `DROP TABLE IF EXISTS` 的目标方言可执行 `CREATE TABLE` 语句，支持一键复制或下载 `.sql` 脚本文件；
  - 支持勾选 **「同时套用中间数据标准」**，在导出与跨方言翻译的同时，自动将字段名、类型、注释以及分布键/分区键同步替换为标准定义。
- **上游视图（View）四层智能穿透还原物理实体表结构**：
  - 针对上游（如星环 Inceptor、DWS、PGSQL）仅开放视图（`CREATE VIEW ... AS SELECT ...`）导致常规 DDL 看不到字段类型与物理存储特征的场景，内置四层递进穿透引擎：
    1. **结果集 Schema 实时推断**：通过 `DESCRIBE EXTENDED / FORMATTED`、`SELECT * FROM view LIMIT 0` 结果集列元数据与系统目录提取视图输出列的精确计算后数据类型；
    2. **递归视图 SQL 血缘解析**：自动解析 `CREATE VIEW` 查询语句中的底层源表别名、表达式映射与嵌套视图，支持多层视图递归下钻直至物理基表；
    3. **底层物理基表特征继承**：当视图列源自底层基表时，自动回填视图本身丢失的**精确列类型（如 `VARCHAR(64)` 长度精度）**、**字段中文注释**、**分布键/分桶键**及**分区键**建议；
    4. **物化建表自动转换**：将无存储结构的上游视图一键转化为目标库带完整列定义、注释、分布键与分区策略的物理实体表 `DROP TABLE IF EXISTS` + `CREATE TABLE` 语句。
- **三大主流数仓方言特性深度适配**：
  - **华为云 GaussDB(`DWS`)**：自动生成 `WITH (orientation=row/column, compression=...)`、`DISTRIBUTE BY HASH(...)` 分布键子句、`PARTITION BY RANGE (...)` 分区子句及独立 `COMMENT ON TABLE / COLUMN`；当来源库（如 Inceptor）的分布键/分区键为无长度限制的 `STRING` / `TEXT` 时，自动转换为 `VARCHAR(255)`，防止 DWS 分布列建表报错。
  - **PostgreSQL (`PGSQL`)**：生成纯净的标准 PostgreSQL 建表语法（自动剥离 DWS 专有的 `DISTRIBUTE BY` 与 `orientation` 存储参数，以及特定库内部的 `::type` 默认值强转），自动将 `TINYINT` / `INT1` 映射为 `SMALLINT`、`STRING` 映射为 `TEXT`、`NVARCHAR2` / `VARCHAR2` 映射为 `VARCHAR`。
  - **星环科技 Transwarp (`Inceptor`)**：自动生成行内 `COMMENT '...'` 语法，自动将分区列移入 `PARTITIONED BY (...)` 子句并从主列清单中剔除（避免 Inceptor 报分区列重复定义错误），将 `TEXT` / `TIME` 映射为 `STRING`、`NUMERIC(p,s)` 映射为 `DECIMAL(p,s)`、`BYTEA` 映射为 `BINARY`，并支持一键切换 **ORC 事务分桶表 (`CLUSTERED BY (...) INTO 7 BUCKETS STORED AS ORC TBLPROPERTIES ('transactional'='true')`)**、**标准 ORC 表**、**Holodesk 内存分析表 (`STORED AS HOLODESK`)** 与 **TEXTFILE 表**。

---

## 四、目录结构说明

```text
├── .github/workflows/ci.yml   # GitHub Actions 自动化单元测试与 Windows .exe 云端打包流水线
├── .gitattributes             # 跨平台 (macOS / Windows) 换行符与二进制文件保护规则
├── .gitignore                 # Git 忽略规则（严格隔离本地 data/*.db 数据库凭据与构建产物）
├── app.py                     # 后端 ASGI 服务与 REST API 路由入口
├── core/
│   ├── dws_engine.py          # 华为 DWS 元数据探查、单表多字段拓扑编排、双模式迁移与多线程并发引擎
│   ├── cross_env_engine.py    # 跨环境三栏数标比对、视图穿透与多方言 DDL 翻译引擎
│   ├── excel_handler.py       # Excel 贯标清单多 Sheet 智能识别、自然标识符清洗与报告导出
│   └── state_store.py         # SQLite 本地状态存储（含跨分支/版本回退幂等列迁移保护）
├── static/
│   ├── index.html             # 前端双模式主界面（单库批量贯标执行台 + 16:9 三纵列跨环境数标比对中心）
│   ├── styles.css             # 响应式与明暗主题样式表（含三条纵贯线布局样式）
│   └── app.js                 # 前端交互逻辑、三纵列联动渲染与 450ms 实时日志轮询
├── tests/
│   └── test_core.py           # 自动化单元测试套件
├── build_exe.py               # Windows PyInstaller 独立可执行程序打包脚本（隔离临时目录）
├── build_windows.bat          # Windows 一键构建批处理脚本
└── requirements.txt           # Python 依赖声明
```

---

## 五、在 Windows 下打包与运行说明

### 1. 一键打包为独立可执行程序（目标机器无需安装 Python）
```bash
# 方式一：双击或命令行运行批处理
build_windows.bat

# 方式二：通过 Python 直接执行打包脚本
python -m pip install -r requirements.txt
python build_exe.py
```
- `build_exe.py` 会自动将 PyInstaller 的 `workpath` / `specpath` 隔离至系统临时目录，规避 Windows 杀毒软件或同步盘锁死 `localpycs` 导致的 `PermissionError: [WinError 5]`。
- 打包完成后，将 `dist\DWS字段贯标工具` 整个文件夹拷贝至目标 Windows 机器，双击 `DWS字段贯标工具.exe` 即可启动。

### 2. 开发环境运行与单元测试
```bash
# 运行自动化单元测试
python -m unittest discover -v -s tests

# 启动本地 Web 服务（默认 http://127.0.0.1:18080）
python app.py
```

---

## 六、GitHub 分支维护、版本标签与安全回退指南

本项目已针对 **macOS + Windows 跨平台协作**、**多分支并行开发** 以及 **随时版本回退** 场景完成工程化配置：
- **凭据安全隔离**：`.gitignore` 已严格排除 `data/*.db`、`data/*.log` 等运行时文件，本地保存的 DWS / PGSQL / Inceptor 账号密码绝不会上传至 GitHub。
- **跨版本状态库兼容**：`core/state_store.py` 内置幂等表结构自检（`_ensure_schema_columns`），切换新旧分支或回退历史版本时，本地 `data/dws_migrator_state.db` 中的连接配置与数标绑定关系可无缝保留，不会报缺列或列冲突错误。
- **云端自动测试与打包**：推送任意分支自动运行单元测试；推送 `v*` 版本标签（或在 GitHub Actions 页面手动触发 `CI & Windows Build`）会自动在云端构建 Windows `.exe` 压缩包并发布到 GitHub Releases。

### 1. 首次绑定 GitHub 远程仓库
1. 登录 GitHub 新建一个仓库（**建议选择 `Private` 私有仓库**，名称如 `dws-column-migrator`，创建时**不要**勾选初始化 README / .gitignore）。
2. 在项目根目录执行以下命令完成关联与首次推送（将仓库地址替换为你的实际地址）：
```bash
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main --tags
```

### 2. 推荐分支策略（精简主流模型）
- `main`：**稳定主干分支**，始终保持可运行、可打包状态。
- `feature/<功能名>`：**新功能或实验分支**，每次尝试大改动前先从 `main` 切出新分支，验证无误后再合并回 `main`。
- `v1.0.0` / `v1.1.0`：**里程碑版本标签（Tag）**，每完成一个阶段性稳定版打一个 Tag，作为永久锚点并自动触发云端打包 `.exe`。

```bash
# 1) 开启新功能开发分支
git switch -c feature/new-idea

# 2) 提交阶段性改动并推送备份
git add .
git commit -m "feat: 新增某某功能"
git push -u origin feature/new-idea

# 3) 功能稳定后合并回 main 主干，并打上新版本标签触发云端打包
git switch main
git merge feature/new-idea
git tag -a v1.1.0 -m "v1.1.0 稳定版"
git push origin main --tags
```

### 3. 三种常见「版本回退」标准操作
- **场景 A：新分支改坏了，想直接丢弃或切回稳定版**
  ```bash
  # 丢弃当前工作区未提交的改动并切回 main 稳定主干
  git restore .
  git switch main
  ```
- **场景 B：想临时回到某个历史版本（如 `v1.0.0`）查看或打包旧版**
  ```bash
  # 基于历史标签切出一个维护分支（安全、不破坏现有历史）
  git switch -c hotfix/from-v1.0.0 v1.0.0
  ```
- **场景 C：`main` 分支已经提交/推送了某次有问题的改动，需要安全回退**
  ```bash
  # 查看最近提交记录与 commit hash
  git log --oneline --decorate -n 10

  # 推荐方式（已推送到 GitHub）：生成一条反向撤销提交，历史清晰且不冲突
  git revert <有问题的commit-hash>
  git push origin main

  # 仅限本地尚未 push 的提交：硬重置回指定版本或标签
  git reset --hard v1.0.0
  ```

### 4. OneDrive 与 Git 多设备协作重要提示
> 若你同时在 Mac 和 Windows 上维护本项目，**强烈建议将 Git 仓库目录放在非同步盘本地路径**（例如 Mac 的 `~/Code/贯标小工具` 与 Windows 的 `C:\贯标`），统一通过 `git push` / `git pull` 同步代码。若仍在 OneDrive 目录内操作 Git，切换分支前请确保另一台设备未同时运行本项目，且 OneDrive 已完成同步，避免云盘文件锁导致 `.git/index.lock` 或冲突副本。
