# DataOG — 多功能数据工程与数据治理瑞士军刀工作台

**DataOG** 是一款面向异构数据仓库（**华为云 GaussDB(`DWS`)**、**PostgreSQL (`PGSQL`)**、**星环科技 Transwarp (`Inceptor`)** 等）打造的**多功能数据工程“瑞士军刀”桌面/Web 一体化应用**。

项目以经过生产验证的「数仓字段无损贯标 + 16:9 跨环境三栏数标比对 + 多方言 DDL 翻译与视图穿透」（`v1.0.0` 基线）为起点，采用 **「全局共享数据底座（Platform Core） + 可插拔业务场景模块（Modular Toolkits）」** 架构持续演进，支持通过现代化 Git 分支协作不断扩展新工具、迭代旧模块，并支持一键打包为免安装的独立 Windows `.exe` 程序。

- **GitHub 仓库**：https://github.com/artxom/DataOG
- **AI 协作记忆与架构规范**：详见仓库根目录 [AGENTS.md](./AGENTS.md)

---

## 一、平台架构愿景：全局底座 × 模块化工具箱

DataOG 将「与具体场景无关的底层数据通用能力」与「具体业务使用场景」彻底解耦，确保每新增一个数据小工具都能直接复用全局连接与方言能力：

1. **DataOG 全局数据底座（Global Platform Capabilities）**
   - **全局数据源连接中心**：统一管理多环境、多异构数仓连接档案（DWS / PGSQL / Inceptor 及后续扩展数据源），一处配置，全平台各子工具按需选用。
   - **异构元数据探查与纯 Python 驱动栈**：内置基于 `psycopg2` 的 PG/DWS 探查引擎与纯 Python 实现的 `InceptorThriftClient`（标准 HiveServer2 Thrift Binary + SASL 协议，兼容 `LDAP` / `NONE` / `NOSASL`，Windows `.exe` 打包零 C 编译依赖）。
   - **通用 SQL 方言、类型归一化与视图血缘解析**：自然标识符清洗、跨库数据类型等价识别与转换矩阵、上游视图（View）四层递归血缘穿透还原物理表特征。
   - **本地持久化与并发执行基础设施**：基于 SQLite WAL 模式的幂等状态存储（支持跨分支切换与版本回退前后向兼容）、多线程并发任务池、450ms 实时增量日志流与通用 Excel/JSON 导入导出组件。

2. **DataOG 业务场景工具箱（Feature Modules）**
   - **模块 A：单库批量落标贯标执行台**（已上线 `v1.0.0`）
   - **模块 B：跨环境三栏数标比对中心（16:9 三纵列）**（已上线 `v1.0.0`）
   - **模块 C：跨方言建表 DDL 翻译与视图物化穿透器**（已上线 `v1.0.0`，正独立化演进）
   - **后续规划扩展槽位**：跨库表数据抽样核对、表结构数据字典批量生成、数据质量探查与批量巡检、测试数据智能生成等独立子模块。

---

## 二、已上线核心工具能力详解

### 1. 工具模块一：单库批量贯标执行台
支持导入包含「修改前字段名、修改前类型、修改前注释、修改后字段名、修改后类型、修改后注释」的多 Sheet Excel 贯标清单，实时与数据库系统目录（`pg_attribute` / `pg_description` / `pg_partition`）比对状态，并在**保证全表字段物理顺序（`attnum`）100% 不变**的前提下完成字段改名、类型转换与注释更新。

- **全表物理顺序 100% 保序（双引擎架构）**
  - **原生无损 `ALTER TABLE`（`DIRECT_ALTER`）**：在单个事务中依次执行 `RENAME COLUMN`、`ALTER COLUMN ... TYPE ... USING`、`COMMENT ON COLUMN`，并在提交前再次查询 `pg_attribute` 核验 `attnum` 物理序号未发生任何偏移。
  - **按原表物理顺序重建表并迁移数据（`TABLE_REBUILD`）**：突破华为 DWS **分布列（`DISTRIBUTE BY HASH(...)`）不允许修改类型**、**分区键修改受限**以及**列存表（`ORIENTATION=COLUMN`）跨类型原地转换受限**的限制：
    - 自动通过 `pg_catalog.pg_get_tabledef` 与 `pg_catalog.pg_partition` 读取原表 `WITH (...)` 存储属性、`DISTRIBUTE BY` 分布键、`PARTITION BY` 分区策略与全部起始分区边界定义，以及按 `attnum ASC` 排列的全表所有字段；
    - 自动规范化含空格的存储属性（如将 `period=1 week` 规范包裹为 `period='1 week'`），并在重建表时完整继承原表的起始分区定义与默认值（`DEFAULT`）、非空约束（`NOT NULL`）；
    - 在单个事务内按原表精确列顺序创建新结构表、通过 `INSERT INTO ... SELECT CAST(...)` 转换迁移全量数据、核验前后数据行数 100% 一致、恢复全部表注释与列注释，最后原子切换表名（支持保留 `__bak_` 原表备份）。
- **表级智能路由与多线程并发执行（默认 5 线程）**
  - **以表为单位智能决策**：批量提交任务时按 `(schema_name, table_name)` 自动聚合。若涉及分布键/分区键调整或存在字段互换改名环路（`A <-> B`），自动采用 `TABLE_REBUILD` 一次性完成；否则采用原生 `DIRECT_ALTER`，中途遇引擎限制可无缝自动升级为重建表模式。
  - **多线程按表并发**：支持前端动态切换 1/3/5/8/10 并发线程，按表隔离锁竞争。
- **单表多字段智能编排与防冲突引擎**
  - **物理列 1 对 1 精确绑定**：按原表 `ordinal_position` 绑定改造任务，在映射分布键与分区键时执行单次映射与去重，杜绝链式改名时出现 `column specified more than once`。
  - **链式改名拓扑排序（Kahn 算法）**：在原生 `ALTER` 模式下自动计算同一张表内多个字段的改名依赖顺序（如先 `col_b -> col_c` 腾出列名，再 `col_a -> col_b`），杜绝 `column already exists` 报错。
  - **重名冲突预拦截**：清单内多列指向同一目标列名或与未改名现有列冲突时，执行前自动精准拦截。

### 2. 工具模块二：跨环境三栏数标比对中心（16:9 三条纵贯线）
面向「来源库 × 数据标准 × 目标库」跨环境落标核验场景，将 16:9 屏幕一分为三，通过落标清单与绑定的数据标准将三者串联，**以表为单位**沿三条纵贯线直观对比：

- **左列（来源库纵贯线 Source DB）**：支持连接 `PGSQL` / `华为 DWS` / `星环 Inceptor`，提供模式与表名自动补全检索，按物理列序（`#1..#N`）展示来源字段、类型、注释、分布/分区键及已绑定数标。
- **中列（数据标准与三方绑定中枢 Data Standard Hub）**：支持上传数标/落标清单、多维度差异筛选及「智能同名重新绑定」；每张卡片内嵌 **`[来源库绑定列] ⟵ 数标绑定 ⟶ [目标库绑定列]`** 双向桥接器，支持手工微调并持久保存。
- **右列（目标库纵贯线 Target DB）**：展示目标表物理列实际状态，通过色彩轴线区分「已符合数标（绿）」、「待贯标改造（橙）」与「未绑定数标」。
- **绑定包导入导出与模块联动闭环**：
  - 支持导出/导入 `.json` 绑定配置包与 `.xlsx` 三方完整比对报表；
  - 支持点击 **「推送至贯标执行台」**，将目标库待落标差异一键交接至单库批量贯标模块执行改造。

### 3. 工具模块三：多方言建表 DDL 翻译与上游视图四层穿透器
支持在 `PGSQL` ⟷ `华为 DWS` ⟷ `星环 Inceptor` 之间任选表或视图，一键翻译生成目标方言可执行 `DROP TABLE IF EXISTS` + `CREATE TABLE` 脚本（支持可选叠加中间数据标准）：

- **上游视图（View）四层智能穿透还原物理实体表**：
  1. **结果集 Schema 实时推断**：通过 `DESCRIBE EXTENDED / FORMATTED`、`SELECT * FROM view LIMIT 0` 结果集元数据与系统目录提取计算列类型；
  2. **递归视图 SQL 血缘解析**：自动解析 `CREATE VIEW` 查询中的源表别名、表达式映射与多层嵌套视图，递归下钻至物理基表；
  3. **底层物理基表特征继承**：自动回填视图本身丢失的精确列长度精度（如 `VARCHAR(64)`）、中文注释、分布键/分桶键及分区键建议；
  4. **物化建表自动转换**：一键将无存储属性的上游视图转化为目标数仓带完整存储、分布与分区策略的实体建表 DDL。
- **三大数仓方言深度适配**：
  - **华为云 GaussDB(`DWS`)**：生成 `WITH (orientation=..., compression=...)`、`DISTRIBUTE BY HASH(...)`、`PARTITION BY RANGE (...)` 及独立 `COMMENT ON`；分布键/分区键遇无界 `STRING`/`TEXT` 自动转为 `VARCHAR(255)`。
  - **PostgreSQL (`PGSQL`)**：剥离特定专有子句与内部类型强转，规范映射标准 PG 类型。
  - **星环 Transwarp (`Inceptor`)**：生成行内 `COMMENT`、自动分离 `PARTITIONED BY (...)` 分区列防止列重复定义，支持一键切换 **ORC 事务分桶表**、**标准 ORC 表**、**Holodesk 内存表** 与 **TEXTFILE 表**。

---

## 三、项目目录结构（当前基线与模块化演进方向）

### 当前基线结构 (`v1.0.0`)
```text
├── AGENTS.md                  # DataOG 项目架构记忆与 AI 协同开发工程规范
├── README.md                  # 项目总体说明、功能手册与版本维护指南
├── .github/workflows/ci.yml   # GitHub Actions 自动化单元测试与 Windows .exe 云端打包流水线
├── .gitattributes             # 跨平台 (macOS / Windows) 换行符与二进制文件保护规则
├── .gitignore                 # Git 忽略规则（严格隔离本地 data/*.db 数据库凭据与构建产物）
├── app.py                     # 后端 ASGI 服务与路由装配入口
├── core/                      # 核心引擎层（正按计划拆分为 platform 全局底座与业务服务）
│   ├── dws_engine.py          # DWS 元数据探查、单表多字段拓扑编排、双模式迁移与多线程引擎
│   ├── cross_env_engine.py    # 异构连接器、三栏数标比对、视图四层穿透与多方言 DDL 翻译引擎
│   ├── excel_handler.py       # Excel 清单多 Sheet 识别、标识符清洗与报告导出
│   └── state_store.py         # SQLite 本地状态存储（内置幂等列迁移保护）
├── static/                    # 前端 Web 控制台静态资源（零 Node 编译依赖，支持 ES Modules 拆分）
├── tests/                     # 自动化单元测试套件
├── build_exe.py               # Windows PyInstaller 独立可执行程序打包脚本（隔离临时目录）
├── build_windows.bat          # Windows 一键构建批处理脚本
└── requirements.txt           # Python 依赖声明
```

### 下一步模块化演进蓝图（`v1.1.0` → `v2.0.0`）
1. **阶段一（全局底座抽离）**：构建 `core/platform/`，将全局数据源连接中心（`ConnectionHub`）、异构驱动适配器（`DWS` / `PGSQL` / `Inceptor`）、方言与类型工具、幂等存储注册与任务日志服务提升为全局基础设施。
2. **阶段二（场景模块化与工作台升级）**：构建 `modules/` 插件化业务包（`standard_migrator`、`cross_comparator`、`ddl_translator` 等），前端升级为 **DataOG 侧边栏瑞士军刀工作台 + 原生 ES Modules**，实现新功能模块按分支独立插拔开发、合并零冲突。

---

## 四、本地运行与 Windows `.exe` 打包说明

### 1. 开发环境启动与单元测试
```bash
# 安装依赖
python -m pip install -r requirements.txt

# 运行全量自动化单元测试与应用自检
python -m unittest discover -v -s tests
python app.py --test

# 启动本地 Web 工作台（默认监听 http://127.0.0.1:18080）
python app.py
```

### 2. 在 Windows 下打包为独立免安装程序
```bash
# 方式一：双击或命令行执行批处理
build_windows.bat

# 方式二：直接运行 Python 打包脚本
python build_exe.py
```
- `build_exe.py` 自动将 PyInstaller 构建中间目录隔离至系统临时目录，规避 Windows 杀毒软件或云盘句柄锁引发的 `PermissionError: [WinError 5]`。
- 打包完成后，将 `dist` 下生成的程序文件夹整体拷贝至目标 Windows 机器双击 `.exe` 即可运行（目标机器无需安装 Python）。

---

## 五、现代化 GitHub 分支开发、合并与版本管理指南

本项目已关联远程仓库 `https://github.com/artxom/DataOG.git`，并配套 GitHub Actions 持续集成与本地状态库跨版本兼容保护：
- **凭据安全隔离**：`.gitignore` 严格忽略 `data/*.db` 与 `data/*.log`，本地保存的数据库账号密码绝不会上传至 GitHub。
- **跨分支数据兼容**：`StateStore` 内置幂等表结构自检与配置自动迁移，切换新旧开发分支或回退版本时，本地连接档案与数标绑定关系无缝保留。
- **云端 CI/CD 流水线**：推送任意分支或发起 PR 时自动运行单元测试校验；推送 `v*` 版本标签（或手动触发 Actions）自动在云端构建 Windows `.exe` 压缩包并归档至 GitHub Releases。

### 1. 标准分支策略（GitHub Flow）
- `main`：**稳定主干分支**，始终保持测试全绿、随时可打包发布。
- `refactor/<主题>`：**底座升级与模块化改造分支**（例如 `refactor/platform-core`、`refactor/modular-workbench`）。
- `feat/<模块名>`：**新功能模块或特性迭代分支**（例如 `feat/ddl-standalone`、`feat/data-profiler`）。
- `fix/<问题名>`：**缺陷修复分支**。
- `vX.Y.Z`：**语义化版本标签（Tag）**，标记每一个稳定里程碑（当前基线为 `v1.0.0`）。

### 2. 日常新功能开发与合并标准闭环
```bash
# 1) 确保本地 main 最新，并切出独立功能/重构分支
git switch main
git pull origin main
git switch -c feat/my-new-tool

# 2) 开发过程中分阶段提交并推送到远程分支（云端自动跑单测）
git add .
git commit -m "feat(my-new-tool): 完成新工具核心逻辑与单测"
git push -u origin feat/my-new-tool

# 3) 验证通过后合并回 main 主干（也可在 GitHub 网页端发起 Pull Request 合并）
git switch main
git merge --no-ff feat/my-new-tool
git push origin main

# 4) 清理已合并的本地开发分支（可选）
git branch -d feat/my-new-tool
```

### 3. 发布里程碑版本（自动触发云端打包 Windows `.exe`）
```bash
git switch main
git tag -a v1.1.0 -m "DataOG v1.1.0: 完成全局数据源底座抽象"
git push origin v1.1.0
```

### 4. 安全回退操作速查
- **开发分支试验不理想，直接放弃未提交改动切回稳定版**：
  ```bash
  git restore .
  git switch main
  ```
- **基于历史稳定标签（如 `v1.0.0`）拉出临时维护或对比分支**：
  ```bash
  git switch -c hotfix/from-v1.0.0 v1.0.0
  ```
- **`main` 分支已推送某次提交需安全撤销（保留清晰历史）**：
  ```bash
  git revert <commit-hash>
  git push origin main
  ```
