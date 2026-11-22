# Data.O.G v2.8.0 — macOS 桌面级多功能数据工程与治理一体化系统

**Data.O.G** 是一款基于 Python Web（`FastAPI` + `Pydantic` + 零构建原生 ES Modules 模块化前端）打造的 **macOS 桌面级数据工程与数据治理“瑞士军刀”系统**。

系统采用 **「无状态程序包 × 本地私有保险箱 × 团队可共享工作区 (`.dataog.db`)」三层分离架构**：
- **升级覆盖零丢失**：程序目录完全无状态，后续版本升级直接覆盖安装目录绝不会抹掉个人授权、AI 密钥、数据库密码或业务进度；
- **个人私密与团队协同彻底解耦**：激活码授权、AI 大模型 API 密钥、个人数据库账号密码、自定义壁纸图标、JDBC 驱动与 AI 探查历史严格隔离在操作系统当前用户私有隐藏存储中；企业数据标准字典、字段绑标、跨环境表/字段映射、贯标队列与无密码数据源拓扑保存在纯净的 `.dataog.db` 工作区中，一键导出分享或引导式增量合并同事进度；
- **零延迟平台共享与离线安全授权**：底层一次配置数据库连接（支持按端点指纹自动匹配本机个人凭据）、JDBC 驱动与 AI 大模型（内置 **DeepSeek** 与 **Google Gemini** 接入），内建 **离线非对称激活码授权与 C 原生防反编译护盾**，并可一键打包为独立免安装 Windows `DataOG.exe` 程序。

- **GitHub 仓库**：https://github.com/artxom/DataOG
- **项目架构记忆与工程规范**：详见 [AGENTS.md](./AGENTS.md)
- **版本变更记录**：详见 [CHANGELOG.md](./CHANGELOG.md)

---

## 一、macOS 桌面级交互与四大核心模块

### 1. 离线登录授权、桌面 Dock 与窗口控制语义（红黄绿三键）
- **离线非对称激活码登录门禁（默认开启）**：未激活、到期、跨机拷贝或手动锁定时全屏毛玻璃门禁拦截，默认选中「激活码模式」（「账号密码登录」在离线试用期标注暂未开放），自动采集展示本机 IP 与机器码并支持一键复制设备申请码 (`REQ1-...`)；激活时弹出 `<dialog>` 提示 **`当前激活码可用日期至 YYYY-MM-DD`**，有效期内再次打开直接进入工作台，支持顶部菜单栏查看授权、在线续期或一键锁定退回登录页。
- **顶部 28px 毛玻璃全局菜单栏**：提供 `Data.O.G` 快速显示桌面、当前活动应用指示、当前团队工作区快速入口胶囊、`授权至 YYYY-MM-DD` 授权胶囊、AI 模型状态胶囊、`⌘K` Spotlight 秒级库表字段检索、SQL 执行审计日志弹窗、自动/浅色/深色外观切换与实时时钟。
- **底部居中悬浮毛玻璃 macOS Dock 栏**：纯净桌面壁纸留白，底部居中 Dock 承载四大应用图标（悬停放大上浮、名称气泡与运行圆点），常规窗口态常驻底部，全屏最大化时自动下沉隐藏、鼠标贴底即可唤出。
- 🔴 **关闭 (`✕`)**：关闭当前窗口返回桌面，并**彻底销毁重置当前工具的运行时界面状态**（已落盘的 SQLite 配置与数据标准不受影响）。
- 🟡 **最小化 (`−`)**：将当前工作窗口收纳至 **Dock 栏竖直分隔线右侧缩略窗口区**，**100% 保留内存 DOM、未提交的编辑内容与滚动位置**；点击 Dock 缩略窗口即可瞬间恢复回离开前的工作现场。
- 🟢 **最大化 (`+`)**（或双击窗口标题栏）：在常规留白桌面窗口与全屏沉浸工作区之间平滑切换。

### 2. 桌面四大独立功能模块
1. **系统配置 (`settings`)**
   - **外观与图标**：内置多款亮暗自适应壁纸预设，支持上传本地自定义桌面壁纸（支持 `铺满` / `适应`）、上传自定义 `Data.O.G` 软件主图标（同步更新浏览器 Favicon）以及四大模块自定义图标，全部安全持久化于本地私有存储。
   - **工作区协作**：支持多工作区新建（空白/克隆）、重命名、热切换、删除与业务统计看板；一键导出零密钥、自包含的 `.dataog.db` 团队共享工作区（可选是否携带元数据缓存），一键导入同事分享的工作区为独立工作区，或启动 **三步引导式「合并同事进度」向导**（同时兼容 `.dataog.db` 与旧版 `dataog_state.db`，严格白名单只读过滤私密表、按端点指纹自动对齐数据源、支持勾选模块与 `仅补充缺失/未绑定` / `按更新时间保留较新` / `覆盖冲突项` 三大策略，并在落盘前提供跨环境映射表对差异预览）。
   - **数据库连接 & JDBC 驱动**：双轨驱动连接管理——无密码连接拓扑随工作区协同共享，个人账号与密码严格落盘于本机私有凭据库并支持按 `(db_type, host, port, dbname)` / `jdbc_url` 端点指纹跨工作区自动匹配；免 Java 原生支持 **华为云 GaussDB(`DWS`)**、**PostgreSQL**、**星环 `Inceptor`/Hive**、**MySQL / StarRocks / Doris / OceanBase / TiDB**、**SQLite**；同时支持上传 `.jar` 驱动包连接达梦 `DM8`、人大金仓 `KingbaseES`、`Oracle`、`DB2`、`GBase` 等任意 JDBC 数据源。
   - **AI 模型**：内置 **DeepSeek**（`https://api.deepseek.com`）、**Google Gemini**（`https://generativelanguage.googleapis.com/v1beta/openai/`）及自定义 OpenAI 兼容网关配置，支持密钥脱敏存储、一键连通检测与远端可用模型拉取。
2. **数据探查 (`explorer`)**
   - 顶栏分段切换 **`SQL 查询` | `AI 探查`**，左侧统一维持 **DataGrip 风格四级已连接数据库目录树**（`连接` → `Schema` → `表/视图` → `字段`）。
   - **`SQL 查询`**：双击表快速抽样预览 + 沉浸式自由 SQL 编辑器（`⌘+Enter` 执行）+ `结果` / `结构` / `DDL` 三合一面板，支持一键导出 CSV / Excel `.xlsx` 或送入「多邻国」。
   - **`AI 探查`**：在左侧数据库清单直接勾选焦点表（支持 Schema 级全选/半选），结合 `MetadataHub` 与 `LLMHub` 自动规划执行多步只读采样查询（覆盖分布画像、空值/重复/脏数据诊断、主外键与跨表关联推断、业务指标核对），透明展开每一步采样 SQL 与行集依据，支持会话历史持久化与一键将探查 SQL 填入主编辑器；底层由**双模态词法白名单 + 数据库会话硬件只读事务 + 退出无条件回滚 + 行数/超时/步数熔断**四道硬防线 100% 阻断任何写操作。
3. **数据标准 (`standard`)**
   - 顶栏分段切换三步递进工作流：**`1. 标准字典` | `2. 落标绑定与贯标` | `3. 跨环境比对`**：
     - **1. 标准字典**：以「数据项编号」为主键统一维护全行最新数据标准，完整支持 46 列多库物理格式数据字典（含 `仓库DWS数据格式`、`数据仓库 集市`、`ArgoDB`、`MySQL`、`Oracle`、`OceanBase`、`达梦` 等），支持先下载系统模板后按编号增量合并导入或在线增删改查。
     - **2. 落标绑定与贯标**：左侧常驻数据库目录树（实时呈现各表绑标/达标状态徽标，并支持按数据库粒度设定并持久化各自的落标标准格式，如 `仓库DWS数据格式`、`数据仓库 集市`、`argodb数据格式` 等），右侧读取库表字段绑定标准，支持按当前表或全库（`scope=all`）导出预填字段对照模板批量上传及智能预匹配，一键执行三维分级“检查是否落标”（字段名 + 归一化类型 + 中文注释），支持免勾表直接导出**库级离线交互网页报告 (`.html`)** 与**单 Sheet 库级 Excel (`.xlsx`)**（空目录先自动采集再生成，离线网页支持全库/单表交互筛选与一键导出单 Sheet Excel），并可通过统一「纳入贯标」按钮将未达标字段累积纳入**全局持久化贯标队列**（跨表、跨库不覆盖，支持按单字段预览准确的多方言/仅注释 DDL）执行原地 `ALTER` 或 DWS 物理列保序重建。
     - **3. 跨环境比对**：采用 macOS Sequoia 桌面级**五虚拟列防重叠图形化工作台**（`来源表待选字段` | `来源表已绑定数标字段` | `数据标准` | `目标表已绑定数标字段` | `目标表待选字段`），自动沿用第 2 步按库设定的标准格式与两侧字段绑定；顶部配备原生感 `NSPopUpButton` 双端配置甲板与支持关键字检索/输入法保护/矮窗自适应翻转的缓存表浮层，中栏内置吸顶毛玻璃三列表头、平静分段状态筛选栏（`全部`/`一致`/`差异`/`单侧`/`待贯标`/`位长/精度不足`/`类型转换`）与实时搜索；支持拖拽或点选待选字段至中栏标准卡片完成绑标配对，中性卡片与桥接线清晰呈现一致（`UNIFIED`）/ 异标对比（`SPLIT_DIFF`）/ 单侧绑定（`SPLIT_PARTIAL`），按需紧凑展示**目标长度/精度包络**与**是否涉及类型转换**（如 `VARCHAR -> DATE`）；左上角 `.mac-subbar` 集成支持按**来源/目标表名及字段名**秒级检索的 `.cross-pair-picker` 映射表对选择器；支持一键导出含 24 列明细及包络/转换诊断的跨环境**库级离线交互网页报告 (`.html`)** 与**单 Sheet 库级 Excel (`.xlsx`)**（空目录先自动采集，同时输出已保存映射表对五栏看板与「推荐同名未配对表」清单，严格仅推荐不自动触发预比对）。
   - **全流程模板先导与智能公式解析**：全系统所有表格导入均采用统一的「先获取系统模板（含按已选库表导出预填模板）→ 再上传解析」两步导入向导，内置双阶段实时进度条（字节上传进度 + 服务端行级/表级解析核验进度与计时），自动提取 Excel 公式最终计算结果（支持跨表 `VLOOKUP` / `XLOOKUP` / `INDEX+MATCH` / 共享公式 / 超级表引用）并将 `#N/A` 等公式错误标记识别为空值。
4. **多邻国 (`polyglot`)**
   - **智能命名**：输入中文表名或字段名清单，结合系统配置中的大模型 API（未配密钥时自动回退内置 170+ 金融与通用数仓标准词根引擎），按命名规范（`snake_case` / `UPPER_SNAKE` / `camelCase`、数仓分层前缀 `ods_/dwd_/dws_/ads_/dim_`、标准词根缩写）生成标准英文名、推荐 SQL 类型与建表草稿。
   - **方言翻译**：支持从已连数据库表/视图、数据标准或直接粘贴 `CREATE TABLE` / `CREATE VIEW` / 异构查询 SQL，转换为目标数据库方言（`DWS` / `PGSQL` / `INCEPTOR` / `MYSQL` / `DM8` / `ORACLE` / `SQLITE`），融合确定性四层递归视图血缘穿透 AST 引擎与大模型 `AI 语法适配`。

---

## 二、项目目录结构速览

```text
DataOG/
├── app.py                        # FastAPI 2.8.0 微内核入口（无状态程序目录、安全响应头、Gate 1 全量 /api 授权拦截、动态 Favicon、路由装配）
├── core/
│   ├── platform/                 # 全局共享底座（挂载于 /api/platform）
│   │   ├── storage_paths.py      # 三层分离混淆私有路径解析器（OS 用户隐藏目录、时钟锚点混淆编码与 DATAOG_HOME 隔离）
│   │   ├── workspace_hub.py      # 团队共享工作区生命周期管理、零密钥 .dataog.db 导出与三步引导式增量合并引擎
│   │   ├── license_hub.py        # RFC 8032 Ed25519 离线激活码中枢、Gate 2 能力校验、双位置时钟防回拨与 C 原生护盾桥接
│   │   ├── _license_native_src/  # 纯标准 C 密码学核心与 _license_shield 原生动态库源码（64 位挑战-应答防补丁）
│   │   ├── asset_store.py        # 本地桌面壁纸与软件/模块自定义图标安全持久化中心
│   │   ├── standard_catalog.py   # 全局 46 列标准字典中枢、多库物理格式解析、单事务批量绑标与分级落标核验
│   │   ├── offline_report.py     # 库级单 Sheet Excel (.xlsx) 与零依赖离线交互网页报告 (.html，内嵌纯 JS .xlsx 生成器) 引擎
│   │   ├── import_progress.py    # 线程安全全平台任务/导入实时阶段进度、节流缩略微日志与 SSE 追踪中枢
│   │   ├── llm_hub.py            # 零额外依赖标准库 DeepSeek / Gemini / OpenAI 兼容协议大模型中枢
│   │   ├── readonly_guard.py     # 双模态词法/语法级只读 SQL 白名单沙箱校验器
│   │   ├── driver_manager.py     # 双轨驱动引擎（Python 原生驱动 + 自定义 JDBC .jar 懒加载 + 硬件只读模式）
│   │   ├── connection_hub.py     # 全局数据源档案池与连接会话上下文（无密码拓扑 × 私有凭据自动匹配、readonly_session 与 60s 温探查池）
│   │   ├── metadata_hub.py       # 双层单会话批量元数据探查器（含 120s 负缓存、单连接视图穿透与锁超时熔断）与本地 SQLite 缓存检索中枢
│   │   ├── query_engine.py       # 跨驱动安全截断查询、硬件级只读执行通道与 CSV/Excel 导出引擎
│   │   ├── storage.py            # 双库分离 WAL 引擎（本地私有保险箱 + 共享工作区 *.dataog.db）、旧版 <exe_dir>/data 无损自动迁移与幂等列演进
│   │   └── dialects/             # 标识符清洗、类型映射矩阵、跨库长度/精度包络与类型转换诊断、DDL 生成与四层视图血缘穿透
│   ├── dws_engine.py             # 单库保序重建、Kahn 拓扑改名与分区继承核心引擎
│   ├── excel_formula.py          # Excel 公式最终结果兜底求值（VLOOKUP/XLOOKUP/INDEX+MATCH）与 #N/A 错误值置空引擎
│   └── excel_handler.py          # Excel 安全解析（防 XXE / Zip-Slip）、模板生成与结果导出
├── modules/                      # 相互独立的插件化业务模块（严格零跨模块私有导入）
│   ├── standard_migrator/        # /api/modules/standard_migrator 落标绑定核验、单库批量贯标与库级离线报告导出引擎
│   ├── cross_comparator/         # /api/modules/cross_comparator  跨环境五虚拟列图形化比对、长度包络与转换诊断、/pairs/search 与库级报告导出引擎
│   ├── ddl_translator/           # /api/modules/ddl_translator    多邻国（智能命名 + 跨方言翻译）
│   └── data_explorer/            # /api/modules/data_explorer     AI 只读智能数据探查多轮会话引擎
├── static/                       # 零 Node 构建链原生 ES Modules macOS 桌面工作台
│   ├── css/                      # tokens.css（明暗双主题与壁纸令牌）与 workbench.css
│   └── js/                       # main.js（登录门禁联动、底部 Dock 与窗口管理器）、core/（含 type_compat.js）、ui/（含 AuthGate / TaskActivityBar / ExportReportModal）、views/（四大模块视图）
├── tools/                        # 独立本地管理员可视化总控台 (`admin_console/`，配合根目录 `DataOG_Console.command` 一键启动) 与零依赖 C 激活码生成器构建器 (`build_keygen.py`；私钥与产物严格隔离不入库)
├── demo_sandbox/                 # 独立「启明商业银行」信创数仓治理演示沙箱（Docker 三引擎同源矩阵 + 164 条标准 + 150 表工作区 + 演示剧本；绝不打入 .exe）
├── tests/                        # 70 项全量自动化测试（三层分离存储、混淆私有库、零密钥导出与引导式增量合并 / 核心算法与兼容矩阵 / 离线非对称授权与跨平台 C 原生护盾 / 平台底座与任务进度流 / 按库标准与五虚拟列防重叠比对 / 库级离线报告 / 只读红队矩阵 / 演示沙箱资产闭环 / 管理员总控台安全与功能回归 / 模块隔离与零 XSS 守卫）
├── build_exe.py                  # Windows DataOG.exe PyInstaller 轻量化门禁打包脚本（内置 static/dataog.ico 多尺寸品牌图标）
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
| **`v2.1.1`** | `refactor/project-slim` → `main` | 全工程深度瘦身与轻量化打包：清理历史冗余模块与一次性脚本，以标准库替代 `openai` / `httpx` 重型依赖，Windows 发行包缩减 `29.2%` 至 `19.42 MB` |
| **`v2.2.0`** | `feat/data-explorer-ai` → `main` | **数据探查 · AI 只读智能探查会话**：保留 DataGrip 四级数据库清单与自由 SQL 查询，新增目标驱动的多轮大模型自动采样探查、结论溯源与会话历史持久化，落地四道引擎级绝对只读硬隔离防线 |
| **`v2.3.0`** | `feat/standard-governance-dock` → `main` | **三步式数据标准治理闭环与原生 macOS Dock**：重构为「1. 标准字典 → 2. 落标绑定与贯标 → 3. 跨环境比对」，落地双列拟物书签桥与字段拖拽配对、全流程两步模板导入向导及 macOS 底部居中悬浮毛玻璃 Dock 栏 |
| **`v2.3.1`** | `feat/excel-progress-formula-na` → `main` | **Excel 导入实时进度条、公式最终值求值与 `#N/A` 置空清洗**：新增 `ImportProgressHub` 与向导双阶段实时进度条，支持缓存优先 + 纯 Python 跨表公式兜底求值、全谱系 `#N/A` 错误值置空与历史脏绑定自愈 |
| **`v2.3.2`** | `fix/excel-eval-progress-modal-races` → `main` | **导入向导竞态加固、经典多条件公式扩展求值与流式大表平滑进度**：补齐 `LOOKUP` / `CHOOSE({1,2},...)` / `INDIRECT` / `OFFSET` 与整行引用求值，保护共享公式结构化表引用列名，准确还原流式 `<dimension ref="A1:A1"/>` 行数，消除跨环境多表铺底冗余探查与 `<dialog>` / 重试轮询异步竞态 |
| **`v2.3.3`** | `fix/bulk-binding-persistence-batch-probe` → `main` | **大规模字段对照单事务批量持久化、单会话批量探查与锁超时熔断降噪**：落地 `bulk_save_field_bindings` 单事务分块写入与 B-Tree 区间删除、`get_tables_metadata_batch` 系统目录批量探查与缓存读写、DWS/PG 会话级锁等待超时降级与逐表异常熔断，并静默高频进度轮询 `200` 刷屏日志 |
| **`v2.4.0`** | `feat/standard-compare-ux` → `main` | **按库持久化落标标准、macOS 五虚拟列跨环境比对与顶栏防遮蔽重构**：支持在「2. 落标绑定与贯标」左侧按数据库独立设定并持久化落标标准格式且同步沿用至第 3 步；将「3. 跨环境比对」重构为 macOS 风格五虚拟列拖拽/点选图形化工作台，并将「已保存映射表对」迁入 `.mac-subbar` 彻底消除顶部遮挡 |
| **`v2.5.0`** | `feat/task-progress-smooth-ux` & `feat/offline-db-report` → `main` | **全平台实时任务进度微日志、60s 温连接秒切与库级离线交互网页 / 单 Sheet Excel 报告**：新增 `TaskProgressHub`（SSE + 节流缩略微日志 + 取消控制）与 `TaskActivityBar` 毛玻璃活动条；落地 `probe_session` 60 秒温连接池、120 秒负缓存与 `/compare-pair` 双库并发秒切预热；单库与跨环境统一升级免勾表库级离线交互网页报告（`.html`，内嵌纯 JS 单 Sheet `.xlsx` 导出器）与单 Sheet 库级 Excel（`.xlsx`，空目录先采再出、跨环境附带未配对同名推荐表清单） |
| **`v2.5.1`** | `chore/exe-icon-v2.5.1` → `main` | **Windows 独立程序 (`DataOG.exe`) 多尺寸品牌图标定制**：新增 7 档分辨率（`16px~256px`）标准 `.ico` 品牌图标 `static/dataog.ico` 并接入 `build_exe.py` PyInstaller `--icon` 打包参数 |
| **`v2.5.2`** | `feat/cross-compat-and-pair-search` → `main` | **跨库长度/精度包络与类型转换诊断、24 列报告及表/字段级映射检索**：新增跨方言字段位长/定点数整数小数双维精度包络与显式类型转换诊断（透传五虚拟列及 24 列 `.xlsx`/`.html` 报告），并在左上角 `.mac-subbar` 落地按表名与字段名秒级检索的 `.cross-pair-picker` |
| **`v2.6.0`** | `feat/offline-license-auth` → `main` | **离线非对称激活码登录拦截、C 原生防反编译护盾与零依赖独立生成器**：基于 RFC 8032 Ed25519 + C 编译态动态库 64 位挑战-应答桥接 + Gate 1/2 双门禁与单调时钟防回拨水印实现「一机一码/全局码」离线授权登录；新增全屏毛玻璃登录门禁与菜单栏授权管理，提供独立零环境依赖 C 可执行激活码生成器 (`tools/build_keygen.py`) |
| **`v2.6.1`** | `fix/cross-comparator-macos-layout` & `fix/windows-shield-release-v2.6.1` → `main` | **macOS Sequoia 跨环境比对防重叠工作台与 Windows 原生护盾云端打包闭环**：整体重塑「3. 跨环境比对」五虚拟列防重叠网格、吸顶毛玻璃表头/平静分段筛选与防遮挡向上翻转选表浮层；修复 Windows 云端打包 `_license_shield.dll` 注册表 `KEY_WOW64_64KEY` SDK 宏兼容、MinGW/Clang/MSVC 静态运行时构建与 DLL 文件锁并自动发布 `DataOG.exe` Release |
| **`v2.7.0`** | `feat/three-tier-storage-v2.7` → `main` | **三层分离存储架构（无状态程序包 + 混淆本地私有库 + 团队共享工作区 `.dataog.db`）与引导式增量合并**：彻底分离个人机密（激活码、AI API Key、个人 DB 凭据、外观、驱动、AI 探查会话）与团队共建业务数据（标准字典、绑标、跨环境映射、贯标队列、无密码连接拓扑、元数据缓存）；覆盖安装升级零丢数；新增系统配置「工作区协作」与三步引导式增量合并向导（含跨环境表对差异预览、端点指纹自动匹配个人凭据与旧版 `<exe_dir>/data/` 无损自动迁移） |
| **`v2.7.1`** | `fix/migrator-global-queue-perf-sql` & `chore/release-v2.7.1-and-auto-release-rule` → `main` | **全局持久化贯标队列、全链路响应优化、仅注释差异 SQL 修复与银行演示沙箱**：新增 `migration_items.connection_id` 跨表/跨库累加与单条多方言 SQL 预览；移除入队冗余远端重连并叠加 `LicenseHub` 热缓存/Gate 2 HMAC 快验与 `DataGrid` 原位勾选优化；精确隔离仅注释差异防误触发整表重建；随附「启明商业银行」Docker 三引擎混合演示沙箱与每次变更强制升版自动发版闭环规范 |
| **`v2.8.0`**（当前正式版） | `feat/admin-console` → `main` | **本地可视化管理员总控台 (`tools/admin_console/` & `DataOG_Console.command`)**：新增带 PBKDF2 加盐登录认证、`HttpOnly`+`SameSite=Strict` 会话、429 暴力破解限流与严格 CSP 零 XSS 前端的总控台，支持一键全套拉起三引擎演示沙箱并自动激活启动 `Data.O.G`、可视化解析 `REQ1` 申请码与签发/一键写入离线激活码、双工作台启停日志监控，以及三库 DBeaver JDBC URL 与 5 大暗雷 AI 提问一键复制 |

---

## 四、本地运行与打包指引

### 1. 开发环境启动与全量自动化测试门禁
```bash
# 安装依赖
python3 -m pip install -r requirements.txt

# 运行全量自动化单元测试（70 项三层分离存储与团队增量合并、核心引擎与兼容判定矩阵、离线非对称激活码与跨平台 C 原生护盾、平台底座与任务进度流、按库标准与五虚拟列防重叠比对、库级离线报告、只读沙箱红队矩阵、演示沙箱闭环、管理员总控台安全与功能回归、跨模块隔离与前端零 XSS 安全校验）
python3 -m unittest discover -s tests -p "test_*.py" -v

# 运行应用启动与健康路由自检
python3 app.py --test

# 启动本地 Data.O.G 桌面工作台（严格监听 http://127.0.0.1:18080）
python3 app.py
```

### 2. 本地银行信创演示沙箱（无甲方集群高保真全链路演练）
需要向管理层或客户现场演示**华为 DWS / 星环 Inceptor / 核心 MySQL** 全链路治理与 AI 探查能力时，可使用内置独立演示套件 [demo_sandbox/README.md](./demo_sandbox/README.md) 与剧本 [DEMO_PLAYBOOK.md](./demo_sandbox/DEMO_PLAYBOOK.md)：
```bash
# 一键启动三引擎同源容器（PG16 演 DWS + Spark Thrift 演 Inceptor + MySQL8）并灌入 3 万+ 行含 5 大业务暗雷的金融样本
./demo_sandbox/sandbox.sh up

# 向隔离演示目录预装双工作区（活库工作区 + 150 表大中台压测工作区）与连接凭据后启动演示桌面
./demo_sandbox/sandbox.sh bootstrap-demo-home
DATAOG_HOME="./demo_sandbox/.demo_home" python3 app.py

# 演练改表后 10 秒不重启容器极速复原现场
./demo_sandbox/sandbox.sh reset
```

### 3. 在 Windows 下打包为独立免安装程序 (`DataOG.exe`)
```bash
# 安装打包器后直接执行打包脚本（或双击 build_windows.bat，产出于 dist/DataOG/DataOG.exe）
python -m pip install "pyinstaller>=6.10.0"
python build_exe.py
```
