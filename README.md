# Data.O.G v3.7.0 — macOS 桌面级多功能数据工程与治理一体化系统

**Data.O.G** 是一款基于 Python Web（`FastAPI` + `Pydantic` + 零构建原生 ES Modules 模块化前端）打造的 **macOS 桌面级数据工程与数据治理“瑞士军刀”系统**。

系统采用 **「无状态程序包 × 本地私有保险箱 × 团队可共享工作区 (`.dataog.db`)」三层分离架构**：
- **升级覆盖零丢失**：程序目录完全无状态，后续版本升级直接覆盖安装目录绝不会抹掉个人授权、AI 密钥、数据库密码或业务进度；
- **个人私密与团队协同彻底解耦**：激活码授权、AI 大模型 API 密钥、个人数据库账号密码、自定义壁纸图标、JDBC 驱动与 AI 探查历史严格隔离在操作系统当前用户私有隐藏存储中；企业数据标准字典、字段绑标、跨环境表/字段映射、贯标队列与无密码数据源拓扑保存在纯净的 `.dataog.db` 工作区中，一键导出分享或引导式增量合并同事进度；
- **零延迟平台共享与离线安全授权**：底层一次配置数据库连接（支持按端点指纹自动匹配本机个人凭据）、JDBC 驱动与 AI 大模型（内置 **DeepSeek** 与 **Google Gemini** 接入），内建 **离线非对称激活码授权与 C 原生防反编译护盾**，并可一键打包为独立免安装 Windows `DataOG.exe` 程序。

- **GitHub 仓库**：https://github.com/artxom/DataOG
- **初始化离线知识库下载**：详见 [knowledge_packs/README.md](./knowledge_packs/README.md)（[DWS 9.1](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_DWS_9.1.json.gz) · [Inceptor 9.x](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_INCEPTOR_9.x.json.gz) · [PostgreSQL 19](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_PGSQL_19.json.gz) · [全量合集包](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_ALL_STARTER.json.gz)）
- **项目架构记忆与工程规范**：详见 [AGENTS.md](./AGENTS.md)
- **3.0 插件模块化开发指南**：详见 [docs/MODULE_SDK_3_0.md](./docs/MODULE_SDK_3_0.md)
- **AI 助手弱模型增幅、动态窗口探查与 `/goal` 模式演进方案**：详见 [docs/archive/AI_ASSISTANT_LONG_SQL_GOAL_PLAN.md](./docs/archive/AI_ASSISTANT_LONG_SQL_GOAL_PLAN.md)（已归档 · 已于 v3.4.0–v3.4.1 落地）
- **版本变更记录**：详见 [CHANGELOG.md](./CHANGELOG.md)

---

## 一、macOS 桌面级交互与五大核心模块

### 1. 离线登录授权、桌面 Dock 与窗口控制语义（红黄绿三键）
- **离线非对称激活码登录门禁（默认开启）**：未激活、到期、跨机拷贝或手动锁定时全屏毛玻璃门禁拦截，默认选中「激活码模式」（「账号密码登录」在离线试用期标注暂未开放），自动采集展示本机 IP 与机器码并支持一键复制设备申请码 (`REQ1-...`)；激活时弹出 `<dialog>` 提示 **`当前激活码可用日期至 YYYY-MM-DD`**，有效期内再次打开直接进入工作台，支持顶部菜单栏查看授权、在线续期或一键锁定退回登录页。
- **顶部 28px 毛玻璃全局菜单栏**：提供 `Data.O.G` 快速显示桌面、当前活动应用指示、当前团队工作区快速入口胶囊、`授权至 YYYY-MM-DD` 授权胶囊、AI 模型状态胶囊、`⌘K` Spotlight 秒级库表字段检索、SQL 执行审计日志弹窗、一键无缓存「重载」、自动/浅色/深色外观切换与实时时钟。
- **底部居中悬浮毛玻璃 macOS Dock 栏**：纯净桌面壁纸留白，底部居中 Dock 承载五大应用图标（悬停放大上浮、名称气泡与运行圆点），常规窗口态常驻底部，全屏最大化时自动下沉隐藏、鼠标贴底即可唤出。
- 🔴 **关闭 (`✕`)**：关闭当前窗口返回桌面，并**彻底销毁重置当前工具的运行时界面状态**（已落盘的 SQLite 配置与数据标准不受影响）。
- 🟡 **最小化 (`−`)**：将当前工作窗口收纳至 **Dock 栏竖直分隔线右侧缩略窗口区**，**100% 保留内存 DOM、未提交的编辑内容与滚动位置**；点击 Dock 缩略窗口即可瞬间恢复回离开前的工作现场。
- 🟢 **最大化 (`+`)**（或双击窗口标题栏）：在常规留白桌面窗口与全屏沉浸工作区之间平滑切换。

### 2. 桌面五大独立功能模块
1. **系统配置 (`settings`)**（含八大独立侧栏分区）
   - **外观与图标**：内置多款亮暗自适应壁纸预设，支持上传本地自定义桌面壁纸（支持 `铺满` / `适应`）、上传自定义 `Data.O.G` 软件主图标（同步更新浏览器 Favicon）以及五大模块自定义图标，全部安全持久化于本地私有存储。
   - **工作区协作**：支持多工作区新建（空白/克隆）、重命名、热切换、删除与业务统计看板；一键导出零密钥、自包含的 `.dataog.db` 团队共享工作区（可选是否携带元数据缓存），一键导入同事分享的工作区为独立工作区，或启动 **三步引导式「合并同事进度」向导**（同时兼容 `.dataog.db` 与旧版 `dataog_state.db`，严格白名单只读过滤私密表、按端点指纹自动对齐数据源、支持勾选模块与 `仅补充缺失/未绑定` / `按更新时间保留较新` / `覆盖冲突项` 三大策略，并在落盘前提供跨环境映射表对差异预览）。
   - **数据库连接**：双轨驱动连接管理——无密码连接拓扑随工作区协同共享，个人账号与密码严格落盘于本机私有凭据库并支持按 `(db_type, host, port, dbname)` / `jdbc_url` 端点指纹跨工作区自动匹配；免 Java 原生支持 **华为云 GaussDB(`DWS`)**、**PostgreSQL**、**星环 `Inceptor`/Hive**、**MySQL / StarRocks / Doris / OceanBase / TiDB**、**SQLite**，并支持通过已挂载 JDBC 驱动建立连接。
   - **JDBC 驱动**：独立驱动管理分区，支持上传 `.jar` 驱动包并通过按需懒加载 JVM 连接达梦 `DM8`、人大金仓 `KingbaseES`、`Oracle`、`DB2`、`GBase` 等任意 JDBC 数据源（无 JRE 时绝不影响原生驱动运行）。
   - **参考知识库 (`KnowledgeHub`)**：零新增依赖离线知识库管理中心，内置 11 篇主流数据库方言速查卡并支持上传 `.md` / `.txt` / `.sql` / `.html` / `.json` / `.csv` / `.docx` / `.xlsx` 参考资料，借助 SQLite FTS5 + BM25 实现中英文与报错码毫秒级离线检索，支持按方言导出/导入轻量 `.json.gz` 知识包与团队分享，并可选接入 OpenAI 兼容 `/embeddings` 接口做混合排序（离线自动退回纯 FTS5）。
   - **AI 模型**：内置 **DeepSeek**（`https://api.deepseek.com`）、**Google Gemini**（`https://generativelanguage.googleapis.com/v1beta/openai/`）及自定义 OpenAI 兼容网关配置，支持密钥脱敏存储、一键连通检测、远端可用模型拉取，以及四级动态模型能力探查（`POST /api/platform/llm/probe-model`）、推理档位分级 (`flagship` / `standard` / `lightweight`) 与 24h `model_probes` 缓存。
   - **版本与升级 (`UpdateHub`)**：提供基于 RFC 8032 Ed25519 双层签名、HMAC-SHA256 认证与 BLAKE2b-512-CTR 加密混淆 (`DGP2`) 的 **同二级版本累积增量升级**（约 `0.95 MB` 的 `DataOG-Update-vX.Y.Z.zip` 适用 `vX.Y.0+` 全部同系列客户端）。包内不含任何明文代码、脚本或嵌套 ZIP 头，可安全通过银行/企业邮件网关；支持 **系统目录 `updates/` 覆盖解压** 与 **应用内上传 `.zip`/`.dgp`/`.dat`** 双渠道升级，并内置启动自愈自动回退、一键回退上一补丁与恢复内置基础版本。
   - **性能诊断 (`DiagnosticsHub`)**：零写盘全内存链路追踪、端到端环回与磁盘/SQLite/DNS/鉴权/DB/AI 全栈基准压测 (`Benchmark`)、自动瓶颈研判（识别企业代理未豁免环回、EDR/慢盘 `fsync`、锁竞争、事件循环阻塞等）以及一键导出全量脱敏 `.json` 诊断日志包（顶部「执行日志」弹窗亦可快捷一键导出）。
2. **数据探查 (`explorer`)**
   - 顶栏分段切换 **`SQL 查询` | `AI 探查`**，左侧统一维持 **DataGrip 风格四级已连接数据库目录树**（`连接` → `Schema` → `表/视图` → `字段`）。
   - **`SQL 查询`**：双击表快速抽样预览 + 沉浸式自由 SQL 编辑器（`⌘+Enter` 执行）+ `结果` / `结构` / `DDL` 三合一面板，支持一键导出 CSV / Excel `.xlsx` 或送入「多邻国」。
   - **`AI 探查`**：在左侧数据库清单直接勾选焦点表（支持 Schema 级全选/半选），结合 `MetadataHub` 与 `LLMHub` 自动规划执行多步只读采样查询（覆盖分布画像、空值/重复/脏数据诊断、主外键与跨表关联推断、业务指标核对），透明展开每一步采样 SQL 与行集依据，支持会话历史持久化与一键将探查 SQL 填入主编辑器；底层由**双模态词法白名单 + 数据库会话硬件只读事务 + 退出无条件回滚 + 行数/超时/步数熔断**四道硬防线 100% 阻断任何写操作。
3. **数据标准 (`standard`)**
   - 顶栏分段切换三步递进工作流：**`1. 标准字典` | `2. 落标绑定与贯标` | `3. 跨环境比对`**：
     - **1. 标准字典**：以「数据项编号」为主键统一维护全行最新数据标准，完整支持 46 列多库物理格式数据字典（含 `仓库DWS数据格式`、`数据仓库 集市`、`ArgoDB`、`MySQL`、`Oracle`、`OceanBase`、`达梦` 等），支持先下载系统模板后按编号增量合并导入或在线增删改查。
     - **2. 落标绑定与贯标**：左侧常驻完整数据库目录树（实时呈现各表绑标/达标状态徽标，彻底释放左上角空间），顶部标签栏右侧提供可折叠的 **「对标设置」** 抽屉（默认收起、模块化可扩展：支持按数据库粒度设定并持久化各自的落标标准格式，配备「忽略部分落标」「忽略未绑标字段」统计口径双开关，以及一键 **「全 Schema 刷新并比对落标」** 同步最新目录结构、表列缓存并重算全量落标结果），右侧读取库表字段绑定标准，支持按当前表、当前选中 Schema（`scope=schema`）或全库（`scope=all`）导出预填字段对照模板批量上传及智能预匹配，一键执行三维分级“检查是否落标”（字段名 + 归一化类型 + 中文注释），支持免勾表直接导出**库级离线交互网页报告 (`.html`)** 与**单 Sheet 库级 Excel (`.xlsx`)**（空目录先自动采集再生成，离线网页支持全库/单表交互筛选与一键导出单 Sheet Excel），并可通过统一「纳入贯标」按钮将未达标字段累积纳入**全局持久化贯标队列**（跨表、跨库不覆盖，支持按单字段预览准确的多方言/仅注释 DDL）执行原地 `ALTER` 或 DWS 物理列保序重建（执行完毕自动核验并 3 秒渐隐清出已达标任务）。
     - **3. 跨环境比对**：采用 macOS Sequoia 桌面级**五虚拟列防重叠图形化工作台**（`来源表待选字段` | `来源表已绑定数标字段` | `数据标准` | `目标表已绑定数标字段` | `目标表待选字段`），自动沿用第 2 步按库设定的标准格式与两侧字段绑定；顶部配备原生感 `NSPopUpButton` 双端配置甲板与支持关键字检索/输入法保护/矮窗自适应翻转的缓存表浮层，中栏内置吸顶毛玻璃三列表头、平静分段状态筛选栏（`全部`/`一致`/`差异`/`单侧`/`待贯标`/`位长/精度不足`/`类型转换`）与实时搜索；支持拖拽或点选待选字段至中栏标准卡片完成绑标配对，中性卡片与桥接线清晰呈现一致（`UNIFIED`）/ 异标对比（`SPLIT_DIFF`）/ 单侧绑定（`SPLIT_PARTIAL`），按需紧凑展示**目标长度/精度包络**与**是否涉及类型转换**（如 `VARCHAR -> DATE`）；左上角 `.mac-subbar` 集成支持按**来源/目标表名及字段名**秒级检索的 `.cross-pair-picker` 映射表对选择器；支持一键导出含 24 列明细及包络/转换诊断的跨环境**库级离线交互网页报告 (`.html`)** 与**单 Sheet 库级 Excel (`.xlsx`)**（空目录先自动采集，同时输出已保存映射表对五栏看板与「推荐同名未配对表」清单，严格仅推荐不自动触发预比对）。
   - **全流程模板先导与智能公式解析**：全系统所有表格导入均采用统一的「先获取系统模板（含按已选库表导出预填模板）→ 再上传解析」两步导入向导，内置双阶段实时进度条（字节上传进度 + 服务端行级/表级解析核验进度与计时），自动提取 Excel 公式最终计算结果（支持跨表 `VLOOKUP` / `XLOOKUP` / `INDEX+MATCH` / 共享公式 / 超级表引用）并将 `#N/A` 等公式错误标记识别为空值。
4. **多邻国 (`polyglot`)**
   - **智能命名**：输入中文表名或字段名清单，结合系统配置中的大模型 API（未配密钥时自动回退内置 147 条金融与通用数仓标准词根引擎），按命名规范（`snake_case` / `UPPER_SNAKE` / `camelCase`、数仓分层前缀 `ods_/dwd_/dws_/ads_/dim_`、标准词根缩写）生成标准英文名、推荐 SQL 类型与建表草稿。
   - **方言翻译**：支持从已连数据库表/视图、数据标准或直接粘贴 `CREATE TABLE` / `CREATE VIEW` / 异构查询 SQL，转换为目标数据库方言（`DWS` / `PGSQL` / `INCEPTOR` / `MYSQL` / `DM8` / `ORACLE` / `SQLITE`），融合确定性四层递归视图血缘穿透 AST 引擎与大模型 `AI 语法适配`。
5. **Data.O.G 智能数据助手 (`ai_assistant`)**
   - **全局研发运维双模式调度**：可绑定任意已配置数据库连接（实时调取目录元数据、视图血缘、执行计划 `EXPLAIN` 与只读采样验证），也可切换至「离线纯方言模式」指定目标数据库方言（DWS / PGSQL / MySQL / Oracle / DM8 / Kingbase / Inceptor / DB2 / GBase / SQLite / 通用）结合本机参考知识库完成语法确认、报错排障与调优建议。
   - **循序渐进多轮探索**：支持场景芯片路由（`自动识别` / `数据查询` / `报错排障` / `性能优化` / `语法确认` / `DDL 助手`）、歧义场景交互式「澄清确认卡」、只读 SQL 报错自主查表自我修正、`EXPLAIN` 实测诊断、知识库出处角标定位（右侧按需滑出「库表速览 / 参考知识库」抽屉）以及末尾「下一步推进建议」。
   - **DDL / 写操作分级二次确认门禁**：自主探索阶段物理仅开放只读通道；所有建表/改表与数据写入语句均在对话提示框以分级风险卡片展示（常规 DDL/INSERT 一键确认，高危 `DROP`/`TRUNCATE`/`DELETE`/`UPDATE` 预估影响行数并强制输入 `CONFIRM` 解锁），支持卡片内微调 SQL（服务端独立重新定级）、多条有序 DDL 批执行与失败一键转 AI 诊断修复。
   - **现代模型感知长上下文、Thinking 深度思考、SSE 实时流式输出与持久化 🎯 `/goal` 闭环**：支持剪贴板粘贴文件或超长文本、面板拖拽与按钮上传 13 种文本/代码格式（`.txt`/`.sql`/`.ddl`/`.md`/`.log`/`.json`/`.yaml`/`.yml`/`.xml`/`.ini`/`.conf`/`.py`/`.sh`，`<= 5 MB`）及 `.csv`/`.tsv`（`<= 2 MB`），配套 `<dialog>` 结构化大纲/画像/分片预览与 CSV 内存只读 SQLite `csv_data` 沙箱；配备模型能力实时探查胶囊、`sql_static_analyzer.py` 确定性静态血缘 DAG/隐患雷达与 `compact_sql_for_llm` 保行号语义压缩、`ExecutionPlanner` 五策略（`full_context` / `lineage_sliced` / `goal_driven_staged` / `agentic_explore` / `stepped_batch`）按推理甜蜜区（`flagship 12K` / `standard 8K` / `lightweight 5.5K`）智能调度、14 个工具（含 3 个手术刀工具）与轻量裁剪/防打转护栏、瞬态退避重试+减载降级+`length` 自动续写，以及后台持久化 🎯 `/goal` 模式（战前规划卡 + 实时 Checklist + 三道硬验收补救 + `Last-Event-ID` 断点续流 + 一键「从断点继续执行」`/resume`）；全面适配主流 Thinking 模型与 `<think>`/`<thought>` 流式分离，通过原生 SSE 实现思考过程实时展开/自动收起、自然 Markdown 边生成边排版与随时「停止生成」中断。

---

## 二、项目目录结构速览

```text
DataOG/
├── update_bootstrap.py           # 纯标准库增量更新引导器（DGP2 解密认证、Ed25519 双域验签、SHA-256 核验、sys.meta_path zipimport 挂载与崩溃自动回退）
├── app.py                        # 冻结态极薄启动壳（BASE_APP_VERSION = 3.7.0，引导增量加载并重导出内核公开入口）
├── core/
│   ├── app_kernel.py             # FastAPI 3.7.0 可热更新微内核（安全/防缓存头、版本校验、Gate 1 快速通道、Server-Timing 与 --smoke-test）
│   ├── platform/                 # 全局共享底座（挂载于 /api/platform）
│   │   ├── module_registry.py    # 后端插件声明式清单 (ModuleSpec) 与自动发现挂载中心（零改 app.py 接入 3.0 模块）
│   │   ├── http_utils.py         # 平台与插件共享 HTTP / 文件下载 / task_progress & import_progress 任务追踪 SDK
│   │   ├── diagnostics_hub.py    # 全内存零写盘链路追踪、全栈环境 Benchmark、自动瓶颈研判与严格脱敏诊断日志导出中枢
│   │   ├── update_hub.py         # Layer 2 增量补丁版本管理、兼容检查、双渠道安装与回退中枢（零物理路径泄露）
│   │   ├── routes/               # 平台领域子路由包（auth_and_system / data_sources / appearance_and_llm / knowledge / standards / workspaces / diagnostics / updates）
│   │   ├── storage/              # 双库分离 WAL 引擎包（schema / base / legacy_migration / platform_repos / governance_repos，支持 3.0 插件表自动随工作区流转）
│   │   ├── storage_paths.py      # 三层分离混淆私有路径解析器（OS 用户隐藏目录、时钟锚点混淆编码与 DATAOG_HOME 隔离）
│   │   ├── workspace_hub.py      # 团队共享工作区生命周期管理、零密钥 .dataog.db 导出与三步引导式增量合并引擎
│   │   ├── license_hub.py        # RFC 8032 Ed25519 离线激活码中枢、Gate 2 能力校验、双位置时钟防回拨与 C 原生护盾桥接
│   │   ├── _license_native_src/  # 纯标准 C 密码学核心与 _license_shield 原生动态库源码（64 位挑战-应答防补丁）
│   │   ├── asset_store.py        # 本地桌面壁纸与软件/模块自定义图标安全持久化中心（支持动态注册 3.0 应用图标槽位）
│   │   ├── knowledge_hub.py      # 零新增依赖离线知识库中枢（多格式解析、FTS5+BM25、方言硬隔离、.json.gz 分享包与可选 Embedding RRF）
│   │   ├── builtin_dialect_kb.py # 内置 11 篇主流数据库方言语法与排障速查卡（~20 KB）
│   │   ├── sql_risk_guard.py     # SQL 词法多语句安全切分器与 WRITE/DDL 分级风险定级守卫
│   │   ├── standard_catalog.py   # 全局 46 列标准字典中枢、多库物理格式解析、单事务批量绑标与分级落标核验
│   │   ├── offline_report/       # 通用离线交互网页报告 (runtime.py) 与单库/跨环境单 Sheet Excel 生成器包
│   │   ├── import_progress.py    # 线程安全全平台任务/导入实时阶段进度、节流缩略微日志与 SSE 追踪中枢
│   │   ├── llm_hub.py            # 零额外依赖标准库 DeepSeek / Gemini / OpenAI 兼容协议大模型中枢
│   │   ├── readonly_guard.py     # 双模态词法/语法级只读 SQL 白名单沙箱校验器
│   │   ├── driver_manager.py     # 双轨驱动引擎（Python 原生驱动 + 自定义 JDBC .jar 懒加载 + 硬件只读模式）
│   │   ├── connection_hub.py     # 全局数据源档案池与连接会话上下文（无密码拓扑 × 私有凭据自动匹配、readonly_session 与 60s 温探查池）
│   │   ├── metadata_hub.py       # 双层单会话批量元数据探查器（含 120s 负缓存、单连接视图穿透与锁超时熔断）与本地 SQLite 缓存检索中枢
│   │   ├── query_engine.py       # 跨驱动安全截断查询、硬件级只读执行通道与 CSV/Excel 导出引擎
│   │   └── dialects/             # 标识符清洗、类型映射矩阵、跨库长度/精度包络与类型转换诊断、DDL 生成与四层视图血缘穿透
│   ├── dws_engine.py             # 单库保序重建、Kahn 拓扑改名与分区继承核心引擎（去重直接复用 dialects 底座）
│   ├── excel_formula.py          # Excel 公式最终结果兜底求值（VLOOKUP/XLOOKUP/INDEX+MATCH）与 #N/A 错误值置空引擎
│   └── excel_handler.py          # Excel 安全解析（防 XXE / Zip-Slip）、模板生成与结果导出
├── modules/                      # 相互独立的插件化业务模块（导出 MODULE_SPEC 自动发现挂载，严格零跨模块私有导入）
│   ├── standard_migrator/        # /api/modules/standard_migrator 落标绑定核验、单库批量贯标与库级离线报告（按 Mixin 拆分）
│   ├── cross_comparator/         # /api/modules/cross_comparator  跨环境五虚拟列图形化比对、检索与库级报告（按 Mixin 拆分）
│   ├── ddl_translator/           # /api/modules/ddl_translator    多邻国（智能命名 + 跨方言翻译）
│   ├── data_explorer/            # /api/modules/data_explorer     AI 只读智能数据探查多轮会话引擎
│   └── ai_assistant/             # /api/modules/ai_assistant      Data.O.G 3.0 旗舰多轮渐进式数据库智能助手（含写确认门禁）
├── static/                       # 零 Node 构建链原生 ES Modules macOS 桌面工作台
│   ├── css/                      # tokens.css（明暗双主题与壁纸令牌）与 workbench.css（macOS 桌面与工作台全局样式表）
│   └── js/                       # main.js（零业务视图静态依赖）、core/（http / perf_monitor / platform_api / app_registry / dom / state / type_compat）、apps/（3.0 独立懒加载应用含 ai_assistant/）、api/（模块独立 API 客户端）、ui/（底座共享组件）、views/（拆解后的高内聚业务视图含 knowledge_view.js、updates_view.js 与 diagnostics_view.js）
├── docs/                         # 架构与演进文档 (MODULE_SDK_3_0.md / archive/AI_ASSISTANT_LONG_SQL_GOAL_PLAN.md [已归档])
├── knowledge_packs/              # 离线初始化知识库下载中心（DWS 9.1 / Inceptor 9.x / PGSQL 19 的 .md 源码与 .json.gz 包；绝不打入 .exe）
├── tools/                        # 独立本地管理员可视化总控台 (`admin_console/`，配合根目录 `DataOG_Console.command` 一键启动) 与零依赖 C 激活码生成器构建器 (`build_keygen.py`；私钥与产物严格隔离不入库)
├── demo_sandbox/                 # 独立「启明商业银行」信创数仓治理演示沙箱（Docker 三引擎同源矩阵 + 164 条标准 + 156×162 表工作区 + 演示剧本；绝不打入 .exe）
├── tests/                        # 129 项全量自动化测试（基于变更历史的 SQL 脚本确定性改写与轻量 AI 五道反幻觉门禁、四级危险分级标色拦截与共享工作区变更日志/Checkpoint 一键回滚、DGP2 加密混淆增量包与银行邮件网关零明文防线/防降级/信封防篡改、标准字典性能优化与AN-AS栏位对齐、落标绑定侧栏缩放/统计口径双开关/原位防跳顶/跳已落标/执行核验3秒渐隐/Schema模板、确定性 SQL 静态血缘/隐患剖析与语义压缩、四级动态模型能力探查与超时重试减载续写、ExecutionPlanner 与持久化 🎯 /goal 硬验收补救断点续跑闭环、AI 助手长上下文重构/Thinking 深度思考与 SSE 实时流式输出、Ed25519 签名同系列累积增量升级与崩溃自愈回退、部分落标跨标准核验与离线报告展示、全链路性能诊断与全栈 Benchmark、SQLite 长连接复用/Gate1 快速通道/大规模绑标统计回归与严格脱敏导出 / 聊天附件与大纲按行探索 / 防缓存响应头与版本同步校验 / 智能数据助手与离线知识库及写确认门禁 / 模块化插件注册与 3.0 零耦合扩展 / 三层分离存储、混淆私有库、零密钥导出与引导式增量合并 / 核心算法与兼容矩阵 / 离线非对称授权与跨平台 C 原生护盾 / 平台底座与任务进度流 / 按库标准与五虚拟列防重叠比对 / 库级离线报告与 Schema 及表级落标范围筛选 / 只读红队矩阵 / 演示沙箱资产闭环 / 管理员总控台安全与功能回归 / 极速 --smoke-test 冒烟与纯净无测试客户包防线）
├── build_update_pack.py          # 同二级版本累积增量包 (DataOG-Update-vX.Y.Z.zip) DGP2 加密混淆 + Ed25519 签名构建脚本
├── build_exe.py                  # Windows DataOG.exe PyInstaller 纯净打包脚本（支持 --skip-tests、内置 static/dataog.ico 图标并严格剔除 tests/）
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
| **`v2.8.0`** | `feat/admin-console` → `main` | **本地可视化管理员总控台 (`tools/admin_console/` & `DataOG_Console.command`)**：新增带 PBKDF2 加盐登录认证、`HttpOnly`+`SameSite=Strict` 会话、429 暴力破解限流与严格 CSP 零 XSS 前端的总控台，支持一键全套拉起三引擎演示沙箱并自动激活启动 `Data.O.G`、可视化解析 `REQ1` 申请码与签发/一键写入离线激活码、双工作台启停日志监控，以及三库 DBeaver JDBC URL 与 5 大暗雷 AI 提问一键复制 |
| **`v2.8.1`** | `fix/llm-tls-ca-fallback` → `main` | **跨平台 AI HTTPS TLS 根证书多级自动解析修复**：修复 macOS Python.org 等默认空白 CA 仓环境直连 DeepSeek 等官方 HTTPS AI 接口报 `self-signed certificate in certificate chain` 问题，在严格保留全量 TLS 校验的前提下实现 `SSL_CERT_FILE` / `truststore` / `/etc/ssl/cert.pem` / `certifi` / 钥匙串多级 CA 自动加载与清晰代理提示 |
| **`v2.8.2`** | `fix/binding-stats-and-rename-sync` → `main` | **改名贯标字段绑定自动迁移与落标统计 `6/8` 僵尸绑标修复**：在执行单条/批量/保序重建后单事务原子迁移旧字段绑标并强制重刷元数据；核验期间自动裁剪无效物理列残留绑定；`get_connection_binding_stats()` 关联 `meta_columns` 过滤历史僵尸行并附带真实表总列数；前端执行完毕即时同步左树徽标并自动刷新绑标清单 |
| **`v2.9.0`** | `refactor/v2.9.0-modular-slimming` → `main` | **2.X 终结规整瘦身与 3.0 模块化基石**：落地后端 `ModuleSpec` 插件自动发现与 `http_utils` 公共任务/下载 SDK、3.0 插件共享工作区表自动流转与动态图标槽位、前端 `app_registry.js` 懒加载注册中心与 HTTP/平台/模块 API 彻底分层；拆解 `storage/`、`routes/`、`offline_report/` 及巨型业务 Service/View 单体，去重 `dws_engine.py` 并清理 392 行死 CSS，配套 `docs/MODULE_SDK_3_0.md` 与 6 项模块化回归测试 |
| **`v2.9.1`** | `fix/v2.9.1-ci-frozen-packaging` → `main` | **CI 零 `httpx` 依赖与 Windows `DataOG.exe` 冻结态自检打包修复**：移除 `test_modular_architecture.py` 对 `httpx` 的隐式依赖并补齐零 `httpx` 静态防线；`build_exe.py` 新增客户运行时测试纯净暂存与构建后泄露校验，剔除 `test_admin_console.py`/`test_demo_sandbox.py` 恢复云端 Windows `DataOG.exe` 编译与 GitHub Release 自动发布；同步加固冻结态 `__spec__.loader` 插件发现与私有表越权注册拦截 |
| **`v2.9.2`** | `feat/v2.9.2-report-schema-bound-scope` → `main` | **单库与跨环境报告支持 Schema 筛选及表级落标范围收敛**：导出离线 HTML / Excel 报告与预填模板新增 Schema 多选过滤，默认启用「仅包含已绑定落标需求的表（表级落标范围）」在探查前剪枝零绑标表与无关 Schema（含探查后僵尸绑标兜底剔除与推荐/单侧清单同步收敛），并在导出弹窗与离线 HTML 顶栏实时标注筛选范围 |
| **`v3.0.0`** | `feat/v3.0.0-ai-assistant-knowledge-hub` → `main` | **独立 Dock 旗舰应用 `Data.O.G`（智能数据助手）与零依赖离线知识库 (`KnowledgeHub`)**：新增第五个独立懒加载 Dock 应用，支持在线连库与离线纯方言双模式、场景模式芯片与歧义澄清卡、7 工具渐进自主探索（含只读 SQL 报错自修正、`EXPLAIN` 诊断与引用溯源）、DDL/写操作分级二次确认卡片（常规一键确认 / 高危影响行数估算 + `CONFIRM` 门禁 + 服务端重定级防篡改 + 批执行闭环），配套 0 KB 新增依赖 FTS5 离线知识库（9 格式解析、11 篇内置方言速查卡、`.json.gz` 分享包与可选 Embedding 混合排序）（86 项测试全绿） |
| **`v3.0.1`** | `fix/v3.0.1-no-cache-and-reload` → `main` | **前端静态资源强制 `no-cache`、桌面顶部「一键重载」按钮与静态目录版本校验**：为非 `/api/*` 的 `.html`/`.js`/`.css` 下发 `Cache-Control: no-store, no-cache, must-revalidate`、`Pragma: no-cache` 与 `Expires: 0` 彻底解决升级后浏览器锁死旧模块问题；顶部菜单栏新增 **「重载」** 按钮支持清理 Cache Storage 并无缓存刷新页面；启动时校验 `_internal/static` 内 `dataog-ui-version` 与 `APP_VERSION` 一致性并在仅替换 `.exe` 时给出醒目提示；同步在 `knowledge_packs/` 提供 DWS 9.1 / Inceptor 9.x / PostgreSQL 19 初始化离线知识库自助下载（87 项测试全绿） |
| **`v3.0.2`** | `fix/v3.0.2-ai-assistant-activitybar-and-e2e-tests` → `main` | **修复 AI 助手与知识库 `activityBar.track` 调用错误、交互体验补全与前端端到端交互测试升级**：将 `AiAssistantView.sendUserMessage()` 与 `KnowledgeSettingsView` 重建向量索引误用的 `this.activityBar.run(...)` 修正为 `this.activityBar.track(...)` 根治发消息阻断崩溃；优先自动选中已就绪 AI 模型、补齐发送中用户气泡占位并修复抽屉库表刷新响应结构读取；在 `test_ai_assistant_and_kb.py` 新增 `.activityBar.run(` 全库静态防线及覆盖发送/提案确认/抽屉/知识库全交互的 Node.js 端到端验证（87 项测试全绿） |
| **`v3.1.0`** | `feat/v3.1.0-chat-attachments-and-chunking` → `main` | **AI 助手聊天窗口纯文本/SQL 粘贴、受限 CSV 上传与超长内容防爆窗分步拆解引擎**：聊天输入框支持剪贴板粘贴文件或超长文本（自动转为 `.txt`/`.sql` 附件胶囊）、拖拽与按钮上传（文本/SQL `<=5MB`、CSV/TSV `<=2MB`、二进制零字节拦截及编码自适应）；落地语义智能分片（`CHUNK_HARD_MAX_TOKENS=2800`）、CSV 零 Token 画像 + `:memory:` 只读 SQLite `csv_data` 内存查询沙箱、按需检索与 `stepped_batch` Map-Reduce 滚动摘要接续拆解闭环，配套 `<dialog>` 分片/画像预览弹窗与后端/Node 端到端回归测试（88 项测试全绿） |
| **`v3.2.0`** | `feat/diagnostics-benchmark-v3.2` → `main` | **全链路性能剖析、全栈基准压测 (`Benchmark`) 与脱敏诊断日志包导出中枢**：新增 `DiagnosticsHub` 全内存零写盘 Span 链路追踪、`_TimedRLock` 与 SQLite 开关库计时、全底座热点埋点及 `Server-Timing` / `X-DataOG-*` 响应头；新增「系统配置 → 性能诊断」与「执行日志」快捷导出入口，支持浏览器 10 串行 + 6 并发环回 RTT 测试、事件循环/线程池/设备探针/DNS/4KB `fsync`/SQLite/Bootstrap/可选 DB 与 AI 端点逐段基准压测、自动瓶颈研判以及 100% 零路径零密钥脱敏 `.json` 诊断包导出（92 项测试全绿） |
| **`v3.2.1`** | `fix/v3.2.1-windows-ci-diagnostics-tests` → `main` | **Windows 打包单测 SQLite 句柄泄露与时序竞态修复**：使用 `contextlib.closing` 彻底关闭诊断测试临时 SQLite 句柄避免 Windows `[WinError 32]` 清理失败、消除跨线程锁争用定时器竞态，并加固 `GetDriveTypeW` ctypes 签名、临时压测库 WAL 回收与 `socketserver` 冻结态导入（92 项测试全绿） |
| **`v3.2.2`** | `chore/v3.2.2-fast-ci-smoke-test` → `main` | **CI 纯打包瘦身与 2 秒冻结态 `--smoke-test` 启动冒烟闭环**：移除 GitHub Actions 与 CI 打包阶段重复 4 次的冗余全量单元测试（回归「发布前本地一次测试」定位），为 `app.py` 新增 2 秒极速 `--smoke-test` 冒烟校验（覆盖冻结依赖 / 原生护盾 / 静态资源与版本同步 / 5 插件挂载 / 4 路内存 ASGI 探活），`build_exe.py` 支持 `--skip-tests` 并彻底将 `tests/` 目录从客户版安装包移除（93 项测试全绿） |
| **`v3.2.3`** | `perf/v3.2.3-intranet-sqlite-gate1-binding-stats` → `main` | **内网性能核爆级治理（消灭 SQLite O(N×M) 平方锁死 / 开关库 EDR 风暴 / Gate 1 事件循环阻塞 / N+1 核验比对）**：重构 `get_connection_binding_stats()` 为线性索引查询 + 锁外内存聚合压降至 10ms 级；`PlatformStateStore` 持久复用 SQLite 长连接 + `temp_store = MEMORY` + 16MB 缓存告别每次 214ms 开关句柄开销；Gate 1 新增 10 秒纯内存热缓存快速通道并卸载至线程池；落标核验与跨环境报告全面批处理消除冗余写盘与逐表查询（95 项测试全绿） |
| **`v3.2.4`** | `feat/v3.2.4-partial-compliance-alt-standard` → `main` | **数据标准对标「部分落标 (`PARTIAL_COMPLIANT`)」状态区分与满足标准展示**：字段名符合但类型不满足当前选择标准、却满足同标准下其他物理格式档时精确区分为「部分落标」并高亮展示命中的具体标准名称及类型，全面透传单库绑标核验页、左侧目录树徽标、单库离线 HTML/Excel 报告与跨环境比对视图（97 项测试全绿） |
| **`v3.3.0`** | `feat/v3.3.0-signed-incremental-updates` → `main` | **Ed25519 签名同系列累积增量升级体系与 GitHub Release 双产物自动化发版**：拆分 `app.py` 冻结外壳与 `core/app_kernel.py`、`update_bootstrap.py` 纯标准库 `sys.meta_path` `zipimport` 挂载器；每个正式版同步发布完整客户端 (`DataOG-Windows-x64-vX.Y.Z.zip`) 与 ~0.9 MB 同二级版本累积增量包 (`DataOG-Update-vX.Y.Z.zip`，适用 `vX.Y.0+`)，支持 `updates/` 目录覆盖解压与「系统配置 → 版本与升级」双渠道导入、独立域 Ed25519 + SHA-256 验签、崩溃/篡改自愈回退与一键版本回滚（103 项测试全绿） |
| **`v3.3.1`** | `feat/ai-long-context-thinking-stream` → `main` | **AI 助手现代模型感知长上下文策略重构、Thinking 深度思考适配与 SSE 实时流式输出**：`LLMHub` 推断现代模型上下文窗口与思考能力并支持自定义 `context_window`；提升分片阈值至 `12K/16K` 避免数千行存储过程碎片化，新增零 Token 结构化大纲与紧凑行号标记，按模型预算在「全量直通」「大纲 + 按行/正则探索 (`read_attachment_lines`)」与「`stepped_batch` 兜底拆解」间自适应切换并支持超限透明降级；全面打通推理模型字段与 `<think>`/`<thought>` 流式解析，新增 `/chat/stream` SSE、前端折叠思考卡、自然 Markdown 边生成边渲染及「停止生成」中断（105 项测试全绿） |
| **`v3.4.0`** | `feat/ai-long-sql-weak-model-goal` → `main` | **AI 助手超长 SQL 弱模型增幅、四级动态模型能力探查、超时韧性重试与持久化 🎯 `/goal` 闭环引擎**：新增确定性 SQL 静态血缘 DAG/控制流/隐患雷达预解剖与保行号语义压缩 (`sql_static_analyzer.py`) 及 3 个手术刀工具；落地 L1/L2/L3 四级模型能力实测与 `POST /api/platform/llm/probe-model`、`lightweight` 有效推理甜蜜区预算收敛、瞬态超时/5xx 指数退避重试+减载降级+`length` 自动续写；新增战前 `ExecutionPlanner` 最优拆解规划、弱模型工具裁剪与防原地打转护栏；新增 Layer 2 私有表 `ai_assistant_runs`/`ai_assistant_run_events`、后台持久化 `GoalRunner` 状态机（覆盖率与 HIGH/MEDIUM 隐患硬验收 + 自动补救 + `Last-Event-ID` 无损断点续流 + 一键 `/resume`）及前端探查胶囊、Goal 切换与实时 Checklist UI（110 项测试全绿） |
| **`v3.4.1`** | `fix/ai-goal-and-static-analyzer-hardening` → `main` | **AI 助手静态血缘解析、模型探查校准、孤儿 Goal 断点恢复与前端续流深度加固**：修复 `$$` 闭合标签、嵌入式 CTE、子查询逗号连接列名泄露、Oracle 无 `DECLARE` 变量及字符串内注释符号导致的静态解析与 `DIALECT_COMPAT` 边界缺陷；修复千分位 400 超限解析、`model_tier` 粘滞与草稿流式探查；补齐重启孤儿 Goal 任务自动对账转 `paused_recoverable`、并发 `/resume` 原子防重、SSE 分页无截断续流、多附件 `A1/A2` 隔离、无附件/在线库 Goal 工具探索、`dataog_meta` 剥离与前端切换模型胶囊/断点游标/配置草稿保护（110 项测试全绿） |
| **`v3.4.2`** | `feat/standard-migrator-ux-refinements` → `main` | **「2. 落标绑定与贯标」交互与执行体验精细化升级**：新增左侧栏拖拽调宽（180px–560px 并持久化记忆、双击复位与键盘微调）；点击「纳入贯标」原位刷新防跳回顶部并保持表格滚动与按钮焦点；未勾选全表纳入及混选自动跳过「已落标」字段；单条/批量/保序重建执行完毕自动实时核验并在 3 秒内通过绿光扫掠渐隐塌缩动画平滑清出已达标任务；按钮更名为「上传字段落标映射关系」并新增「当前选中 Schema 全量模板」导出（112 项测试全绿） |
| **`v3.5.0`** | `feat/v3.5.0-encrypted-update-pack` → `main` | **DGP2 加密混淆增量更新包架构重构（兼容银行邮件网关传输）**：将增量更新核心载荷由明文嵌套 ZIP 重构为纯标准库 `BLAKE2b-512-CTR + HMAC-SHA256 + Ed25519` 双域签名高熵密文信封 (`DGP2`) 与紧凑二进制载荷 (`DGPB`)；外层标准无密码 `DataOG-Update-vX.Y.Z.zip` 零明文源码/脚本/可执行特征并内置构建期网关安全扫描与 `>=3.5.0` 防降级保护，客户端透明支持 `.zip`/`.dgp`/`.dat` 双渠道验签解密安装（114 项测试全绿） |
| **`v3.5.1`** | `fix/v3.5.1-excel-illegal-char-sanitize` → `main` | **全量 Schema 模板及全平台 Excel 导出非法控制字符清洗修复**：在底座 `core/excel_handler.py` 与 `core/platform/excel_io.py` 统一提供 `sanitize_excel_cell`/`sanitize_excel_row`/`sanitize_excel_sheet_title`，彻底清除数据库元数据中夹带的 `PK\x02`/`PK\x0b` 等 XML 1.0 非法控制字符并先清后转义公式注入前缀；全覆盖 Schema 全量映射模板、贯标执行报告、SQL 通用导出、标准字典、单库/跨环境离线 Excel 及跨环境比对导出全链路（116 项测试全绿） |
| **`v3.5.2`** | `feat/v3.5.2-sidebar-stat-toggles` → `main` | **「2. 落标绑定与贯标」左侧统计口径 macOS 开关**：`get_cached_catalog` 补齐物理表 `col_count`；左侧「当前库落标标准」卡片下方新增默认开启且持久化记忆的 **「忽略部分落标」** 与 **「忽略未绑标字段」** macOS HIG 胶囊开关，支持实时切换左侧表级徽标统计口径（含部分落标归并、0 绑标表 `0/N` 全量物理列展示与未缓存列数提示）（117 项测试全绿） |
| **`v3.5.3`** | `fix/v3.5.3-standard-dict-perf-and-an-as-fields` → `main` | **「1. 标准字典」卡顿根治与模板 AN–AS 物理格式栏位对齐**：`search_standards` 无关键词直走主键索引并支持 `include_summary=false` 按需汇总；移除 `dialog::backdrop` 全屏模糊与 `tr` `contain`，重构 `StandardDictView` 常驻 DOM + `DataGrid.setData()` + IME 防抖 + `AbortController` + `destroy()` 及 `StandardView` 惰性实例化；新增/编辑弹窗物理格式严格对齐模板 `AN`–`AS` 六列（`ocenbase-oracle` 至 `仓库DWS数据格式`）并完整保留历史非表单字段（118 项测试全绿） |
| **`v3.5.4`** | `chore/v3.5.4-gc-and-docs-reorg` → `main` | **全仓垃圾回收、死代码清理、文档重整与演示沙箱闭环修复**：系统性核查并清理底座/模块/前端/沙箱经确认死代码、旧 `static/js/core/api.js` 兼容层、冗余 CSS 及过期字节码；修复 `demo_sandbox/workspace_builder.py` 导出工作区名称/描述覆盖与 `dwd_acct_swap_demo` 保序重建缓存提示；归档 `docs/archive/AI_ASSISTANT_LONG_SQL_GOAL_PLAN.md` 并全仓同步 `MODULE_SDK_3_0.md`、`knowledge_packs/README.md`、`DEMO_PLAYBOOK.md` 与根文档（118 项测试全绿） |
| **`v3.5.5`** | `feat/v3.5.5-migrator-drawer-schema-refresh` → `main` | **「2. 落标绑定与贯标」可折叠对标设置抽屉 + 全 Schema 刷新与落标比对**：释放左上角侧栏空间，将「当前库落标标准」与目录树统计口径开关迁入默认收起的右侧模块化可扩展「对标设置」抽屉；`check-compliance` 与抽屉新增 `scope=schema`/`all_schemas` + `refresh_catalog` 一键全 Schema 刷新并重算全量落标统计与 `col_count`（119 项测试全绿） |
| **`v3.6.0`** | `feat/migration-risk-changelog-rollback` → `main` | **贯标四级危险分级标色拦截、共享工作区结构化变更日志与 Checkpoint 一键回滚**：建立 L1 改名 / L2 转类型 / L3 缩精度 / L4 安全四级分类与队列标色、高危快捷取消/移出及执行前汇总确认；在 Layer 3 共享工作区持久化 `migration_change_logs` 与 `migration_checkpoints`，提供时间线打点、冲突拦截、`_bak_` 备份表无损切回与轻量逆向 DDL 断点续滚，自动逆向还原字段绑标并导出折叠净映射 JSON 底座（123 项测试全绿） |
| **`v3.7.0`**（当前正式版） | `feat/sql-script-rewriter` → `main` | **基于字段变更历史的 SQL 脚本自动调整（确定性词法作用域底座 + 轻量 AI 手术刀增强）**：在「2. 落标绑定与贯标」新增「SQL 脚本改写」工作台，消费共享工作区变更日志 / Checkpoint 增量 / 离线 `changelog.json`，零 Token 精准 Span 改写 CTE/子查询/DML/DDL/PL/pgSQL 过程体并输出 L2/L3 类型影响雷达；支持按需调用廉价轻量模型（如 `DeepSeek-v4-flash`）对歧义列、跨类型表达式、动态 SQL 与 `JOIN USING` 微切片输出经五道反幻觉门禁核验的局部补丁，支持交互式逐条勾选 Diff 预览与多文件 `.sql`/`.zip` 审计包导出（129 项测试全绿） |

---

## 四、本地运行与打包指引

### 1. 开发环境启动与全量自动化测试门禁
```bash
# 安装依赖
python3 -m pip install -r requirements.txt

# 运行全量自动化单元测试（129 项，发布前本地执行）
python3 -m unittest discover -s tests -p "test_*.py" -v

# 运行 2 秒极速启动冒烟自检（验证依赖、原生护盾、静态资源、插件挂载与 ASGI 路由）
python3 app.py --smoke-test

# 启动本地 Data.O.G 桌面工作台（严格监听 http://127.0.0.1:18080）
python3 app.py
```

### 2. 本地银行信创演示沙箱（无甲方集群高保真全链路演练）
需要向管理层或客户现场演示**华为 DWS / 星环 Inceptor / 核心 MySQL** 全链路治理与 AI 探查能力时，可使用内置独立演示套件 [demo_sandbox/README.md](./demo_sandbox/README.md) 与剧本 [DEMO_PLAYBOOK.md](./demo_sandbox/DEMO_PLAYBOOK.md)：
```bash
# 一键启动三引擎同源容器（PG16 演 DWS + Spark Thrift 演 Inceptor + MySQL8）并灌入 65,000+ 行三引擎（其中 DWS 3.3 万+ 行）含 5 大业务暗雷的金融样本
./demo_sandbox/sandbox.sh up

# 向隔离演示目录预装双工作区（活库工作区 + 150 表大中台压测工作区）与连接凭据后启动演示桌面
./demo_sandbox/sandbox.sh bootstrap-demo-home
DATAOG_HOME="./demo_sandbox/.demo_home" python3 app.py

# 演练改表后 10 秒不重启容器极速复原现场
./demo_sandbox/sandbox.sh reset
```

### 3. 在 Windows 下打包为独立免安装程序 (`DataOG.exe`) 与签名增量包
```bash
# 安装打包器后直接执行完整客户端打包脚本（产出于 dist/DataOG/DataOG.exe；若本地已跑完单测可附加 --skip-tests）
python -m pip install "pyinstaller>=6.10.0"
python build_exe.py --skip-tests

# 构建轻量加密混淆签名同系列累积增量更新包 DataOG-Update-vX.Y.Z.zip（约 0.95 MB，自动完成零明文与 Ed25519/HMAC 闭环自检）
python build_update_pack.py --output-dir dist
```
> **客户升级双渠道**（同二级版本 `v3.5.0+` 均适用后续 `3.5.x` 增量包；跨二级版本请下载完整客户端）：
> - **方式一（外部解压覆盖）**：将 `DataOG-Update-vX.Y.Z.zip` 解压覆盖到 `DataOG` 客户端目录（掉落 `updates/*.dgp` 高熵 `DGP2` 密文载荷，零明文源码/脚本、可安全通过银行邮件网关），下次双击启动 `DataOG.exe` 自动验签解密生效；
> - **方式二（应用内上传）**：在 **「系统配置 → 版本与升级」** 上传同一 `.zip`/`.dgp`/`.dat`，重启 `DataOG.exe` 即刻切换，支持随时一键回退或恢复内置基础版本。

---

## 五、初始化离线知识库下载（DWS 9.1 / Inceptor 9.x / PostgreSQL 19）

为保障离线/内网环境中 **Data.O.G 智能数据助手** 与 **参考知识库 (`KnowledgeHub`)** 开箱即用，同时严格保持安装包零体积膨胀，仓库在 [`knowledge_packs/`](./knowledge_packs/README.md) 独立提供面向最新主流数据库版本的离线初始化知识包（`.json.gz`）及对应 Markdown 原文档，您和团队成员可根据实际连接的数据库按需下载：

| 目标数据库与适用版本 | 知识包文件名 | 内容与分块 | 下载与在线查看超链接 |
| :--- | :--- | :--- | :--- |
| **华为云 GaussDB(DWS)** (`9.1 / 8.3`) | `DataOG_KB_DWS_9.1.json.gz` | 3 篇 / 24 块（`~8.2 KB`） | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_DWS_9.1.json.gz) · [Markdown 源码](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/dws) |
| **星环 Inceptor / ArgoDB** (`9.3 / 9.4`) | `DataOG_KB_INCEPTOR_9.x.json.gz` | 3 篇 / 20 块（`~6.5 KB`） | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_INCEPTOR_9.x.json.gz) · [Markdown 源码](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/inceptor) |
| **官方 PostgreSQL** (`19 / 18 / 17`) | `DataOG_KB_PGSQL_19.json.gz` | 3 篇 / 15 块（`~7.1 KB`） | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_PGSQL_19.json.gz) · [Markdown 源码](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/pgsql) |
| **全量合集包（三库一键导入）** | `DataOG_KB_ALL_STARTER.json.gz` | 9 篇 / 59 块（`~19.4 KB`） | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_ALL_STARTER.json.gz) · [完整下载说明](./knowledge_packs/README.md) |

**导入方式**：打开 Data.O.G → **「系统配置」→「参考知识库」→「导入知识包」** 选择下载好的 `.json.gz` 即可；如需单独微调某份手册，也可下载 `sources/` 下对应 `.md` 文件后点击 **「上传资料」** 并指定方言导入。

