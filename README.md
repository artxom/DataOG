# Data.O.G v2.1.1 — macOS 桌面级多功能数据工程与治理一体化系统

**Data.O.G** 是一款基于 Python Web（`FastAPI` + `Pydantic` + 零构建原生 ES Modules 模块化前端）打造的 **macOS 桌面级数据工程与数据治理“瑞士军刀”系统**。

系统以 **“强共享平台底座 + 独立定制化桌面应用模块”** 为核心设计：底层一次配置数据库连接、JDBC 驱动与 AI 大模型（内置 **DeepSeek** 与 **Google Gemini** 接入），全平台零延迟共享元数据与智能能力；前端采用克制干练的 macOS 桌面与窗口交互体系，支持自定义桌面壁纸与软件/模块图标本地持久化，并可一键打包为独立免安装 Windows `DataOG.exe` 程序。

- **GitHub 仓库**：https://github.com/artxom/DataOG
- **项目架构记忆与工程规范**：详见 [AGENTS.md](./AGENTS.md)
- **版本变更记录**：详见 [CHANGELOG.md](./CHANGELOG.md)

---

## 一、macOS 桌面级交互与四大核心模块

### 1. 桌面窗口控制语义（红黄绿三键）
- **顶部 28px 毛玻璃全局菜单栏**：提供 `Data.O.G` 快速显示桌面、当前活动应用指示、AI 模型状态胶囊、`⌘K` Spotlight 秒级库表字段检索、SQL 执行审计日志弹窗、自动/浅色/深色外观切换与实时时钟。
- 🔴 **关闭 (`✕`)**：关闭当前窗口返回桌面，并**彻底销毁重置当前工具的运行时界面状态**（下次打开恢复初始干净状态，已落盘的 SQLite 配置数据不受影响）。
- 🟡 **最小化 (`−`)**：将当前工作窗口缩略停靠至**屏幕右下角毛玻璃小抽屉**，**100% 保留内存 DOM、未提交的编辑内容与滚动位置**；点击右下角小抽屉卡片即可瞬间恢复回离开前的工作现场。
- 🟢 **最大化 (`+`)**（或双击窗口标题栏）：在常规留白桌面窗口与全屏沉浸工作区之间平滑切换。

### 2. 桌面四大独立功能模块
1. **系统配置 (`settings`)**
   - **外观与图标**：内置多款亮暗自适应壁纸预设，支持上传本地自定义桌面壁纸（支持 `铺满` / `适应`）、上传自定义 `Data.O.G` 软件主图标（同步更新浏览器 Favicon）以及四大模块自定义图标，全部安全持久化保存于本地 `data/assets/`。
   - **数据库连接 & JDBC 驱动**：双轨驱动连接管理——免 Java 原生支持 **华为云 GaussDB(`DWS`)**、**PostgreSQL**、**星环 `Inceptor`/Hive**、**MySQL / StarRocks / Doris / OceanBase / TiDB**、**SQLite**；同时支持上传 `.jar` 驱动包连接达梦 `DM8`、人大金仓 `KingbaseES`、`Oracle`、`DB2`、`GBase` 等任意 JDBC 数据源。
   - **AI 模型**：内置 **DeepSeek**（`https://api.deepseek.com`）、**Google Gemini**（`https://generativelanguage.googleapis.com/v1beta/openai/`）及自定义 OpenAI 兼容网关配置，支持密钥脱敏存储、一键连通检测与远端可用模型拉取。
2. **数据探查 (`explorer`)**
   - 无框 Finder 风格数据库目录树 + 沉浸式 SQL 编辑器（`⌘+Enter` 执行）+ `结果` / `结构` / `DDL` 三合一面板，默认安全行数截断保护，支持一键导出 CSV / Excel `.xlsx` 或将当前表直接送入「多邻国」。
3. **数据标准 (`standard`)**
   - 顶栏分段切换 **`贯标` | `比对`**：整合单库物理列序保持批量贯标执行台（`DIRECT_ALTER` / `TABLE_REBUILD`、Kahn 拓扑改名排序、分区边界继承、多线程并发）与跨环境三栏数标比对中心（来源库 × 数据标准 × 目标库双向桥接，一键推送待改规则至贯标执行）。
4. **多邻国 (`polyglot`)**
   - **智能命名**：输入中文表名或字段名清单，结合系统配置中的大模型 API（未配密钥时自动回退内置 170+ 金融与通用数仓标准词根引擎），按命名规范（`snake_case` / `UPPER_SNAKE` / `camelCase`、数仓分层前缀 `ods_/dwd_/dws_/ads_/dim_`、标准词根缩写）生成标准英文名、推荐 SQL 类型与建表草稿。
   - **方言翻译**：支持从已连数据库表/视图、数据标准或直接粘贴 `CREATE TABLE` / `CREATE VIEW` / 异构查询 SQL，转换为目标数据库方言（`DWS` / `PGSQL` / `INCEPTOR` / `MYSQL` / `DM8` / `ORACLE` / `SQLITE`），融合确定性四层递归视图血缘穿透 AST 引擎与大模型 `AI 语法适配`。

---

## 二、项目目录结构速览

```text
DataOG/
├── app.py                        # FastAPI 2.1.1 微内核入口（安全响应头、动态 Favicon、路由装配）
├── core/
│   ├── platform/                 # 全局共享底座（挂载于 /api/platform）
│   │   ├── asset_store.py        # 本地桌面壁纸与软件/模块自定义图标安全持久化中心
│   │   ├── llm_hub.py            # 零额外依赖标准库 DeepSeek / Gemini / OpenAI 兼容协议大模型中枢
│   │   ├── driver_manager.py     # 双轨驱动引擎（Python 原生驱动 + 自定义 JDBC .jar 懒加载）
│   │   ├── connection_hub.py     # 全局数据源档案池与连接会话上下文
│   │   ├── metadata_hub.py       # 双层元数据探查器与本地 SQLite 缓存检索中枢
│   │   ├── query_engine.py       # 跨驱动安全截断查询、表数据预览与 CSV/Excel 导出引擎
│   │   ├── storage.py            # data/dataog_state.db WAL 状态库、旧版无损迁移与幂等列演进
│   │   └── dialects/             # 标识符清洗、类型映射矩阵、DDL 生成与四层视图血缘穿透
│   ├── dws_engine.py             # 单库保序重建、Kahn 拓扑改名与分区继承核心引擎
│   └── excel_handler.py          # Excel 安全解析（防 XXE / Zip-Slip）、模板生成与结果导出
├── modules/                      # 相互独立的插件化业务模块（严格零跨模块私有导入）
│   ├── standard_migrator/        # /api/modules/standard_migrator 单库批量落标贯标引擎
│   ├── cross_comparator/         # /api/modules/cross_comparator  跨环境三栏数标比对引擎
│   └── ddl_translator/           # /api/modules/ddl_translator    多邻国（智能命名 + 跨方言翻译）
├── static/                       # 零 Node 构建链原生 ES Modules macOS 桌面工作台
│   ├── css/                      # tokens.css（明暗双主题与壁纸令牌）与 workbench.css
│   └── js/                       # main.js（桌面与窗口管理器）、core/、ui/、views/（四大模块视图）
├── tests/                        # 27 项全量自动化测试（核心算法 / 平台底座 / 模块隔离与安全扫描）
├── build_exe.py                  # Windows DataOG.exe PyInstaller 轻量化门禁打包脚本
├── AGENTS.md                     # 项目架构铁律、会话交接快照与工程协作规范
└── CHANGELOG.md                  # 遵循 Keep a Changelog 的版本演进日志
```

---

## 三、里程碑版本演进记录

| 里程碑版本 | 分支演进 | 核心交付物 |
| :--- | :--- | :--- |
| **`v1.0.0`** | `main` | 单库保序贯标、16:9 三栏跨环境数标比对、多方言 DDL 翻译与四层视图穿透核心算法基线 |
| **`v2.0.0`** | `refactor/dataog-workbench` → `main` | `FastAPI` 微内核升级、四大全局数据底座、`modules/*` 插件解耦与 `DataOG.exe` 打包流水线 |
| **`v2.1.0`** | `feat/macos-desktop-v2.1` → `main` | 品牌升级 **`Data.O.G`**，全面重塑 **macOS 桌面与红黄绿窗口体系（含右下角最小化抽屉）**，拆解为 **系统配置 / 数据探查 / 数据标准 / 多邻国** 四大模块，新增本地自定义壁纸图标存储与 DeepSeek / Gemini 大模型接入 |
| **`v2.1.1`**（当前正式版） | `refactor/project-slim` → `main` | 全工程深度瘦身与轻量化打包：清理历史冗余模块与一次性脚本，以标准库替代 `openai` / `httpx` 重型依赖，优化 PyInstaller 打包体积 |

---

## 四、本地运行与打包指引

### 1. 开发环境启动与全量自动化测试门禁
```bash
# 安装依赖
python3 -m pip install -r requirements.txt

# 运行全量自动化单元测试（27 项核心引擎、平台底座、跨模块隔离与前端零 XSS 安全校验）
python3 -m unittest discover -s tests -p "test_*.py" -v

# 运行应用启动与健康路由自检
python3 app.py --test

# 启动本地 Data.O.G 桌面工作台（严格监听 http://127.0.0.1:18080）
python3 app.py
```

### 2. 在 Windows 下打包为独立免安装程序 (`DataOG.exe`)
```bash
# 安装打包器后直接执行打包脚本（或双击 build_windows.bat，产出于 dist/DataOG/DataOG.exe）
python -m pip install "pyinstaller>=6.10.0"
python build_exe.py
```
