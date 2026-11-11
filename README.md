# DataOG — 类 DBeaver/DataGrip 底座的多功能数据工程瑞士军刀

**DataOG** 是一款基于 Python Web（`FastAPI` + 原生模块化前端）打造的**多功能数据工程与数据治理一体化工作台**。

它结合了 **DBeaver / DataGrip 式的核心数据底座**（双轨制广泛数据库连接、支持用户上传标准 JDBC `.jar` 驱动、全局共享元数据缓存图谱、轻量 SQL 查询与表数据浏览）与 **面向中国数仓实战的可插拔专业工具箱**（单库物理保序批量贯标、16:9 跨环境三栏数标比对、跨方言 DDL 翻译与四层视图血缘穿透等），既可在 macOS / Windows 浏览器中提供统一现代的 IDE 交互体验，又支持一键打包为独立 Windows `.exe` 桌面程序。

- **GitHub 仓库**：https://github.com/artxom/DataOG
- **项目架构记忆与工程规范**：详见 [AGENTS.md](./AGENTS.md)

---

## 一、核心架构设计：强底座共享 × 独立插件化模块

DataOG 采用 **「四大全局平台中枢 + IDE 式统一工作台 + 插件化业务模块」** 分层架构，确保数据库“一次连接、元数据全局共享”，每个新工具模块只需专注自身业务逻辑：

### 1. DataOG 全局核心底座 (`core/platform/`)
1. **双轨驱动引擎与全局连接中心 (`DriverManager` & `ConnectionHub`)**
   - **内置 Python 原生驱动通道（免 Java 开箱即用）**：开箱直接支持 **华为云 GaussDB(`DWS`)**、**PostgreSQL (`PGSQL`)**、**星环 Transwarp (`Inceptor`)**（内置纯 Python HiveServer2 Thrift 协议客户端）、**MySQL / OceanBase / Doris / StarRocks / TiDB**、**SQLite** 等常见数据源。
   - **自定义 JDBC `.jar` 驱动扩展通道（类 DBeaver 体验）**：支持用户在界面直接上传任意数据库的 JDBC `.jar` 驱动包（如达梦 DM8、人大金仓、Oracle、DB2、GBase 等），通过本地 JRE 与独立 `URLClassLoader` 隔离桥接，彻底打破 Python 生态冷门数据库驱动缺失的限制。
2. **全局元数据共享中枢 (`MetadataHub`)**
   - 连接任意数据源后，通过定制方言探查器或通用 JDBC `DatabaseMetaData` 采集 **Catalog / Schema → 表 / 视图 → 字段 / 类型 / 注释 / 主键 / 分布键 / 分区键**，并结构化缓存在本地 SQLite 元数据索引中（支持一键增量刷新与全局秒级检索），无缝供给所有功能模块。
3. **通用简单查询与数据预览引擎 (`QueryEngine`)**
   - 跨异构驱动提供统一的 SQL 执行、表数据快速预览（默认安全分页截断）、结果集导出（CSV / Excel）及超时防护能力。
4. **方言、血缘与任务基础设施 (`Dialects` & `TaskInfra`)**
   - 提供标识符规范化、跨方言类型等价判断与映射矩阵、上游视图四层递归血缘穿透、多线程作业池及 450ms 实时增量日志总线。

### 2. IDE 式统一 Web 工作台与插件化业务模块
- **统一界面骨架**：`[最左模块图标栏 Activity Bar]` + `[左侧可折叠全局数据库目录树 Database Navigator]` + `[右侧独立模块工作区与上下文工具栏]` + `[底部全局任务与 SQL 日志抽屉]`。
- **内置核心工作区**：数据源与驱动管理中心、轻量 SQL 查询与表数据浏览台。
- **首批专业数据治理模块**：
  - **单库批量落标贯标执行台**（已实现 `v1.0.0`，正模块化接入新底座）
  - **跨环境三栏数标比对中心（16:9 三纵列）**（已实现 `v1.0.0`，正模块化接入新底座）
  - **跨方言建表 DDL 翻译与上游视图穿透器**（已实现 `v1.0.0`，正升级为独立一级模块）

---

## 二、已实现的核心业务工具能力详解（`v1.0.0` 基线资产）

### 1. 单库批量贯标执行台
支持导入包含「修改前字段名、修改前类型、修改前注释、修改后字段名、修改后类型、修改后注释」的多 Sheet Excel 贯标清单，实时与数据库系统目录比对状态，并在**保证全表字段物理顺序（`attnum`）100% 不变**的前提下完成字段改名、类型转换与注释更新：
- **双引擎 100% 保序**：
  - **原生无损 `ALTER TABLE`（`DIRECT_ALTER`）**：单事务执行改名、改类型、改注释并核验 `attnum` 零偏移，配合 **Kahn 拓扑排序算法**自动编排同表链式改名（如 `col_a -> col_b` 且 `col_b -> col_c`）。
  - **按原表物理顺序重建表（`TABLE_REBUILD`）**：针对华为 DWS 分布列、分区键或列存表限制，自动解析 `pg_get_tabledef` 与 `pg_partition` 继承原表存储属性、分布策略与全部起始分区边界，单事务建表、转换迁移数据、核验行数一致并原子切换表名。
- **表级智能路由与多线程并发**：默认 5 线程按表并发，互不争抢同表锁。

### 2. 跨环境三栏数标比对中心（16:9 三条纵贯线）
面向「来源库 × 数据标准 × 目标库」跨环境落标核验场景，沿三条纵贯线以表为单位直观对比：
- **三方双向桥接绑定**：中间数标列内嵌 `[来源库绑定列] ⟵ 数标绑定 ⟶ [目标库绑定列]` 桥接器，支持智能同名绑定、手工调整、导出/导入 `.json` 绑定包与 `.xlsx` 完整对比报表，并支持一键将差异推送至贯标执行台。

### 3. 多方言建表 DDL 翻译与上游视图四层穿透器
支持在异构数仓（如 `PGSQL` ⟷ `华为 DWS` ⟷ `星环 Inceptor`）之间任选表或视图，一键生成目标方言可执行建表脚本：
- **上游视图四层穿透物化**：通过「结果集 Schema 推断 → 递归 CTE/嵌套视图 SQL 血缘解析 → 底层物理基表类型精度/注释/分布分区键回填 → 实体建表转换」，解决上游仅开放视图无法直接获取物理建表 DDL 的痛点。
- **多方言存储特性深度适配**：支持 DWS 行列存与分布分区自动转换、标准 PG 语法净化，以及星环 Inceptor 的 **ORC 事务分桶表 / 标准 ORC 表 / Holodesk 内存表 / TEXTFILE 表** 一键切换。

---

## 三、渐进式架构演进路线图

| 里程碑版本 | 分支名称 | 核心目标与交付物 |
| :--- | :--- | :--- |
| **`v1.0.0`**（已完成） | `main` | 完成单库贯标、16:9 三栏跨环境比对、多方言 DDL 翻译与视图穿透核心闭环及 CI 自动打包流水线 |
| **`v1.1.0`**（下一步） | `refactor/platform-foundation` | 后端平滑升级 `FastAPI`；落地双轨 `DriverManager`（原生驱动 + JDBC `.jar` 上传桥接）、全局 `ConnectionHub`、持久化 `MetadataHub` 与统一 `QueryEngine` |
| **`v1.2.0`** | `feat/ide-workbench-and-query` | 前端升级类 DataGrip IDE 工作台骨架 + 公共 ES Module UI 组件库；上线全局数据库对象树、自定义驱动上传界面与轻量 SQL 查询/表数据预览台 |
| **`v2.0.0`** | `refactor/modularize-domain-tools` | 将单库贯标、三栏比对、DDL 翻译器彻底拆分为 `modules/*` 标准独立插件并全面接入共享 `MetadataHub`，完成 `DataOG` 桌面包更名发布 |

---

## 四、本地运行与 Windows `.exe` 打包说明

### 1. 开发环境启动与单元测试
```bash
# 安装依赖
python3 -m pip install -r requirements.txt

# 运行全量自动化单元测试与应用自检
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 app.py --test

# 启动本地 Web 工作台（默认监听 http://127.0.0.1:18080）
python3 app.py
```

### 2. 在 Windows 下打包为独立免安装程序
```bash
# 双击 build_windows.bat 或直接执行打包脚本
python build_exe.py
```

---

## 五、现代化 GitHub 分支开发、合并与版本管理指南

1. **开启重构或新模块分支**：
   ```bash
   git switch main && git pull origin main
   git switch -c feat/<模块名称>
   ```
2. **阶段性提交并推送云端自动跑单测**：
   ```bash
   git add .
   git commit -m "feat(<模块名称>): 描述核心改动"
   git push -u origin feat/<模块名称>
   ```
3. **全量测试通过后合并回 `main` 并打版本 Tag 发布 Release**：
   ```bash
   python3 -m unittest discover -s tests -p "test_*.py" -v && python3 app.py --test
   git switch main
   git merge --no-ff feat/<模块名称>
   git tag -a v1.1.0 -m "DataOG v1.1.0"
   git push origin main --tags
   ```
