# Data.O.G 项目记忆与架构工程规范 (AGENTS.md)

## 1. 项目定位与核心对标
- **官方品牌名称**：**`Data.O.G`**（GitHub 远程仓库：`https://github.com/artxom/DataOG.git`，Windows 独立打包文件名保持 `DataOG.exe`）。
- **产品形态**：基于 Python Web 技术栈构建的 **macOS 桌面级极简美学 × 多功能数据工程与数据治理“瑞士军刀”一体化系统**（支持 macOS / Windows 本地与内网运行，支持一键打包为独立 Windows `.exe`）。
- **核心设计哲学**：
  1. **强底座共享**：连接数据库、加载自定义 JDBC 驱动、执行基础数据查询、采集缓存库表字段元数据、维护全局统一数据标准字典（`StandardCatalog`）、管理 AI 大模型 API（DeepSeek / Gemini / OpenAI 兼容网关）及持久化本地壁纸与自定义图标，均属于**平台级公共能力**，配置一次、全平台所有子模块零延迟共享。
  2. **macOS 桌面化模块隔离**：界面严禁堆砌冗长解释性副标题与版本号噪音；桌面保持纯净壁纸画布，通过底部居中悬浮毛玻璃 **Dock 栏**启动与切换四大模块，支持真实红黄绿窗口生命周期（关闭重置状态、最小化收纳至 Dock 竖直分隔线右侧保留现场、最大化全屏切换且 Dock 自动下沉贴底唤出），每个业务模块各自独立定制演进，后端模块间严格零私有互引。

## 2. 核心分层与模块化铁律
所有代码设计与功能迭代必须严格遵循 **「平台微内核底座 (`core/platform/`) + macOS 桌面窗口外壳与公共组件 (`static/js/`) + 独立业务功能模块 (`modules/<module_id>/`)」** 架构：

### 2.1 全局共享底座层 (`core/platform/`)
严禁任何业务模块私自管理数据库密码或 AI API Key、或重复编写系统目录查询 SQL，一律通过 `PlatformContext` 依赖注入调用底座七大中枢：
1. **双轨驱动引擎与全局连接中心 (`DriverManager` & `ConnectionHub`)**：
   - **内置原生通道（免 Java 开箱即用）**：内置纯 Python / 主流 DB-API 驱动（`psycopg2` 覆盖 PG/华为 DWS/openGauss/金仓、`PyMySQL` 覆盖 MySQL/OceanBase/Doris/StarRocks/TiDB、标准库 `sqlite3`、纯 Python `InceptorThriftClient` 覆盖星环 Inceptor/Hive）。
   - **自定义 JDBC 扩展通道（用户上传 `.jar` 驱动）**：支持用户在界面上传标准 JDBC `.jar` 驱动包（安全隔离存放于非静态目录 `data/drivers/`，校验文件头与 SHA-256 指纹），通过按需懒加载的 `JPype1` + 独立 `URLClassLoader` 连接达梦 DM8、Oracle、DB2、GBase 等任意数据库（未安装 JRE 时绝不影响内置原生驱动正常启动运行）。
   - **全局连接池 (`ConnectionHub`)**：统一管理命名数据源档案（Connection Profiles），所有模块通过 `connection_id` 引用。
2. **全局元数据共享中枢 (`MetadataHub`)**：
   - 统一标准化元数据模型：`Catalog/Schema` → `Table/View/MatView` → `ColumnMeta`（物理列序 `ordinal_position`、原始类型与归一化类型、非空 `is_not_null`、默认值、注释、主键/索引、分布键/分桶键、分区键）。
   - **双层探查与本地持久化缓存**：针对深度适配库（DWS/PG/Inceptor/MySQL）使用定制高性能探查器；针对任意自定义 JDBC 驱动库自动回退使用标准 `java.sql.DatabaseMetaData` 通用探查器；采集结果持久化缓存至本地 SQLite 元数据索引表，支持增量刷新与 `⌘K` Spotlight 全局秒级检索。
3. **通用数据查询、预览与硬件级只读沙箱中心 (`QueryEngine` & `ReadOnlySqlGuard`)**：
   - 提供跨驱动统一的自由 SQL 查询与表数据快速预览能力，输出标准 `QueryResult`（列头类型描述、行集、耗时、默认 `LIMIT 500`/上限 `5000` 安全截断、查询超时保护、CSV/Excel 导出）。
   - 内置双模态词法/语法级只读白名单校验器（`readonly_guard.py`）与跨驱动会话级硬件只读执行通道（`execute_readonly_sql` + `ConnectionHub.readonly_session` 退出无条件 `rollback()` + 零 `commit()`），供 AI 探查等零副作用场景复用。
4. **AI 大模型统一接入中枢 (`LLMHub`)**：
   - 参考 Cherry Studio 多服务商范式，采用零额外第三方依赖的 Python 标准库（`urllib.request` / `ssl` / `json`）原生实现 OpenAI 兼容 REST 协议管理 `llm_providers` 表，内置 **DeepSeek**（`https://api.deepseek.com`）、**Google Gemini**（`https://generativelanguage.googleapis.com/v1beta/openai/`）及自定义 OpenAI 兼容网关预设，支持连通性检测、远端模型拉取（自动剥离 Gemini `models/` 前缀）与结构化 JSON 输出。
5. **本地壁纸与自定义图标资源中心 (`AssetStore`)**：
   - 支持用户上传自定义桌面壁纸（支持 `铺满`/`适应`）、`Data.O.G` 软件主图标（同步动态覆盖 `/favicon.ico`）及四大桌面模块图标；严格执行光栅图像魔数头白名单（PNG/JPEG/WebP/GIF，阻断 SVG 脚本）、UUID 随机重命名落盘于私有存储资源目录并持久化映射至本地私有保险箱。
6. **三层分离存储、全球数据标准字典与团队工作区协作 (`StoragePaths` / `PlatformStateStore` / `WorkspaceManager` / `StandardCatalog` / `Dialects` / `ExcelIO`)**：
   - **三层分离存储架构（v2.7.0）**：
     - **Layer 1 无状态程序包**：安装目录（`DataOG.exe`、`_internal/`、`static/`）不存放也不创建任何运行期 `data/` 目录，软件版本更新全量覆盖解压绝不覆盖用户配置与进度。
     - **Layer 2 本地私有保险箱（混淆的 OS 用户私有目录，支持 `DATAOG_HOME` 测试覆盖，严禁在 UI/API/日志中暴露路径）**：存放离线激活码/锁/混淆时钟锚点、`llm_providers`（AI API Key）、个人数据库凭据库（`connection_credentials`，支持 `connection_id` 与端点指纹双通道自动匹配）、壁纸与图标资产、JDBC `.jar` 驱动、本地 `execution_logs`、个人 AI 探查会话（`explorer_sessions`/`explorer_messages`）及本地工作区注册表（`workspaces`）。
     - **Layer 3 团队共享工作区（`*.dataog.db`）**：纯业务协同数据，结构上彻底不含任何密码、API Key、激活码或机器绑定信息（`workspace_meta`、无 `user`/`password` 列的 `connection_topology`、`standard_dictionary`、`field_standard_bindings`、`cross_table_bindings`、`cross_column_bindings`、`migration_items` 及可选 `meta_*` 缓存）；通过 `WorkspaceManager` 提供多工作区新建/克隆/重命名/热切换/删除、零密钥导出、安全导入及三步引导式增量合并（兼容 `.dataog.db` 与旧版 `dataog_state.db`，白名单只读解析、端点指纹重映射、`supplement`/`newer`/`overwrite` 三大策略及跨环境表对差异预览）。
   - `StandardCatalog` (`core/platform/standard_catalog.py`) 以 `数据项编号` (`std_code`) 为唯一主键维护当前工作区最新数据标准字典（兼容 46 列多库物理格式 Excel 模板、流式导入与单事务 Upsert），统一提供跨库物理类型智能解析、三维分级落标判定与数据源字段级数标绑定持久化（`field_standard_bindings`）；全系统表格类上传入口统一先提供标准系统模板（含可选按已选库表预填模板）再由用户填写上传。
   - 统一提供 SQL 标识符清洗、跨方言类型归一化与转换矩阵、上游视图四层递归血缘穿透解析、SQLite WAL 模块表幂等注册迁移（支持 `scope="workspace"|"private"`）与 Excel 导入导出基础能力。
7. **离线非对称激活码授权与原生防反编译护盾 (`LicenseHub`)**：
   - 客户端仅嵌有混淆拆分的 RFC 8032 Ed25519 32 字节公钥（零私钥），配合编译态 C 原生动态库护盾 (`_license_shield.{dylib,so,dll}` + 64 位随机挑战-应答 MAC 防补丁)、Gate 1 (ASGI 中间件) + Gate 2 (`get_platform_context` 动态 HMAC 请求能力令牌) 双层强制门禁与双位置硬件密封单调时钟回拨防线（私有库 `app_meta['license_clock_seal']` + 私有库隐藏混淆编码时钟锚点文件）；
   - 支持 **设备专属确定性激活码**（严格绑定唯一 `机器码 + 本机物理 IPv4 + 时长 + 截止日`）与 **全局通用激活码**（无视机器与 IP 限制、仅受有效期与防回拨约束），同一入参确定性签发唯一激活码；独立零环境依赖 C 可执行生成器源码位于 `tools/keygen_src/` 与 `tools/build_keygen.py`。

### 2.2 后端模块化路由与前端 macOS 桌面工作台 (`modules/` & `static/`)
- **后端技术选型**：采用 **`FastAPI` + `Pydantic` + `Uvicorn`**。每个业务模块位于独立的 `modules/<module_id>/` 目录（含 `router.py`、`schemas.py`、`service.py`），通过 `APIRouter` 注册至 `/api/modules/<module_id>`，模块之间严禁相互私有导入，跨模块协作一律经由底座定义的数据契约或状态存储接口完成。
- **前端 macOS 桌面级架构（零 Node 构建链）**：
  - 采用浏览器原生 **ES Modules (`<script type="module">`)** + CSS `light-dark()` / `color-scheme` 明暗双主题令牌，不引入 Webpack/Vite 等编译链，保持静态资源零构建运行与 PyInstaller 直接打包能力。
  - **macOS 桌面、登录拦截与窗口语义**：
    - 全屏毛玻璃登录拦截门禁（`static/js/ui/auth_gate.js` `#os-login-gate`）：未激活、过期、设备/IP 变更、时钟回拨或手动锁定时全屏拦截，默认选中「激活码模式」（「账号密码登录」标注离线试用期暂未开放），展示自动采集的本机 IP、本机机器码与「复制设备申请码 (`REQ1-...`)」及管理员联系方式（莫政辉 / `131-6111-4561` / `artxom@outlook.com`），激活成功后弹出 `<dialog>` 提醒 `当前激活码可用日期至 YYYY-MM-DD`，有效期内再次打开直接进入桌面。
    - 顶部 28px 毛玻璃菜单栏（`Data.O.G` 品牌归位、活动应用名、`授权至 YYYY-MM-DD` 状态胶囊支持查看授权/续期/一键锁定退回登录页、AI 状态指示、`⌘K` Spotlight 搜索、运行日志弹窗、深浅色切换、实时时钟）。
    - 纯净桌面画布 + 底部居中悬浮毛玻璃 **macOS Dock 栏**承载四大独立应用：**`系统配置`** (`settings`)、**`数据探查`** (`explorer`)、**`数据标准`** (`standard`：整合 `1. 标准字典` | `2. 落标绑定与贯标` | `3. 跨环境比对`)、**`多邻国`** (`polyglot`：整合 `智能命名` | `方言翻译`)。
    - 左上角红黄绿三键语义：🔴 **关闭 (`✕`)** 回到桌面并彻底销毁重置当前工具运行时状态；🟡 **最小化 (`−`)** 收纳至 Dock 竖直分隔线右侧缩略窗口区（100% 保留内存 DOM、光标与未保存输入，点击 Dock 缩略窗口即刻恢复工作现场）；🟢 **最大化 (`+`)** 切换常规留白窗口（Dock 常驻底部）与全屏沉浸工作态（Dock 自动下沉隐藏、鼠标贴底唤出）。
  - **公共 UI 组件库 (`static/js/ui/`)**：统一复用 `DatabaseTree`、`DataGrid`、`ObjectPicker`、`SqlCodeBox`、`UploadWizardModal`（两步式模板先导上传向导 + XHR 上传与服务端解析双阶段实时进度条）、`StandardPickerModal`（标准字典实时检索绑定器）、`ExportReportModal`（库级离线交互 `.html` 与单 Sheet `.xlsx` 导出弹窗）、`Modal`（原生 `<dialog>` + 坐标判定轻关闭兜底与导入期防误关锁）、`Toast`，严格使用 `textContent` / `createElement` 等安全 DOM 操作杜绝 XSS。

## 3. 安全、数据兼容与跨平台打包红线
- **安全与本地隔离**：默认仅监听 `127.0.0.1:18080`（启用 CSP 与 `Sec-Fetch-Site` 跨站请求防护）；`data/*.db`、`data/*.log`、驱动目录 `data/drivers/`、个性化图片目录 `data/assets/`、本地私钥目录 `.keygen_secrets/`、独立生成器产物 `keygen/` 及原生编译产物 `*.dylib`/`*.so`/`*.dll` 必须被 `.gitignore` 排除，严禁将任何数据库密码、AI API Key、本地状态或 Ed25519 签发私钥提交至 Git（也不得打入客户版 `DataOG.exe`）；所有接口返回与日志输出必须脱敏密码、密钥与激活码；`.jar` 驱动与壁纸/图标上传均须严格限制体积、校验二进制魔数头并校验 `realpath` 目录边界防路径穿越，资产读取响应强制携带 `X-Content-Type-Options: nosniff` 与独立 `sandbox` CSP。
- **前端零 XSS 与非阻塞交互红线**：前端代码严禁出现 `innerHTML`、`outerHTML`、`insertAdjacentHTML` 及阻塞式 `window.alert`/`confirm`/`prompt`，统一通过 `static/js/core/dom.js` 安全节点工厂与原生 `<dialog>` (`Modal`) 渲染（由单测静态扫描强制守卫）。
- **状态库前后向无损兼容与私有路径隐藏红线**：默认运行期采用三层分离存储（混淆 OS 用户私有库 + 托管共享工作区 `*.dataog.db`），启动时自动检测旧版 `<app_dir>/data/dataog_state.db` / `dws_migrator_state.db`、`assets/`、`drivers/` 及 `.sys_clock_anchor` 并幂等无损拆分迁移至私有保险箱与默认共享工作区；所有 UI、API 与终端日志严禁暴露私有存储物理路径与内部文件名；所有 SQLite 表结构演进必须通过 `register_table_schema` 幂等列自检机制（新增列带安全 `DEFAULT`），保障跨分支切换与版本回退零报错。
- **Windows `.exe` 与冻结态自检保障**：保留 Windows CMD 快速编辑模式禁用（防点击挂起）、标准流 UTF-8 重配置（防 `cp1252`/`gbk` 编码崩溃）、静态目录多路径探测、多线程 JPype `attachThreadToJVM()` 及 PyInstaller 轻量化裁剪保护（排除 `openai` / `httpx` / `lxml` / `cryptography` / `pytest` 等非必要包与 `.pyi` 类型桩）；模块零互引校验兼容源码 AST 与冻结态 `pkgutil` + `dis` 字节码自省，确保 `DataOG.exe --test` 独立自检通过。

## 4. 工程交付闭环：文档同步、GitHub Flow 与最佳实践（强制执行）
每次完成任何开发、重构、缺陷修复或工程变更任务时，**必须完整执行以下「文档同步 + 规范提交 + GitHub 远端闭环」流程**，不得遗漏：
1. **每次收尾强制同步项目文档（供下次会话无缝衔接）**：
   - **`AGENTS.md`**：若模块职责、目录拓扑、数据表、路由契约、版本号或工程红线发生变化，立即更新本文档第 5 节基线快照与对应规范章节，确保下一次新会话启动时零成本获取最新上下文。
   - **`README.md`**：同步更新对外架构说明、功能模块列表、目录速览与运行/打包指引。
   - **`CHANGELOG.md`**：遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，在 `[Unreleased]` 或对应正式版本下按 `Added` / `Changed` / `Fixed` / `Security` 记录核心变更。
2. **分支模型（标准 GitHub Flow）**：
   - `main`：受保护语义的稳定主干分支，随时保持测试全绿、文档同步、可直接运行与打包。
   - 工作分支一律从最新干净的 `main` 切出：`feat/<模块名>`（新特性）、`fix/<问题名>`（缺陷修复）、`refactor/<主题>`（架构重构）、`docs/<主题>`（文档与规范更新）、`chore/<主题>`（构建与工程维护）。
3. **原子提交、双门禁校验与 GitHub 同步规范**：
   - **提交规范**：遵循 Conventional Commits（格式：`<type>(<scope>): <中文简明摘要>`），保持提交原子化；提交前执行 `git status` 与 `git diff` 逐项核查，严禁混入未跟踪的临时文件或本地状态库。
   - **合并门禁**：合并回 `main` 前必须 100% 通过全量自动化单测（`python3 -m unittest discover -s tests -p "test_*.py" -v`）与启动自检（`python3 app.py --test`）。
   - **推送、发版与分支清理**：合并回 `main` 后推送至 `origin`；里程碑发版按 SemVer（如 `v2.0.0`、`v2.1.0`）创建附注标签（`git tag -a vX.Y.Z -m "..."`）并推送标签以触发 `.github/workflows/ci.yml` 自动测试与 Windows `.exe` Release 发布；合并完成后及时删除已使命终结的临时本地与远端工作分支，保持 GitHub 仓库分支树整洁，并确认远端 GitHub Actions CI 状态全绿。

## 5. 当前代码基线快照（供下次会话快速定位）
- **当前主干里程碑版本**：`v2.7.0`（官方品牌名 `Data.O.G`，三层分离存储架构——无状态程序包 + 混淆 OS 本地私有保险箱 + 零密钥团队共享工作区 `*.dataog.db` 与引导式增量合并向导大版本升级）。
- **微内核入口与构建脚本**：
  - `app.py`：FastAPI `2.7.0` 微内核入口（`create_app` 支持默认三层分离模式 / `private_root` / 兼容单库 `db_path`，日志写入私有目录且绝不创建 `<app_dir>/data/` 或打印私有路径、动态 `/favicon.ico`、UTF-8 流保护、Windows 防挂起、高频进度/任务轮询 `200` 日志降噪过滤器、安全头/CSRF 与 Gate 1 全量 `/api/*` 激活码授权拦截中间件、挂载平台路由与四大插件模块路由，保留 `/api/bootstrap` 脱敏兼容探针且受 Gate 2 鉴权）。
  - `requirements.txt`：保持 8 项纯运行时核心依赖（`fastapi`、`pydantic`、`uvicorn`、`psycopg2-binary`、`PyMySQL`、`JPype1`、`openpyxl`、`python-multipart`），零新增第三方依赖。
  - `build_exe.py` / `build_windows.bat` / `.github/workflows/ci.yml`：先预检编译 `_license_shield` 原生护盾（规避 Windows 下 `ctypes` 预先映射旧 DLL 导致文件锁）再执行单测门禁后轻量化打包无状态 `dist/DataOG/DataOG.exe`（严禁在产物目录创建或打包 `data/`、内置 `--icon=static/dataog.ico` 多尺寸 `.exe` 品牌图标、校验产物中 `_license_shield` 存在性、排除 `tools` 与 `demo_sandbox` 等非运行时包，CI 中使用隔离 `DATAOG_HOME` 执行冻结态 `--test` 自检后发布 `v*` GitHub Releases）。
  - `tools/keygen_src/keygen.c` & `tools/build_keygen.py`：本地零环境依赖独立 C 激活码生成器构建器，兼容 MinGW `gcc` 静态运行时、LLVM `clang` (`libcmt`) 与 MSVC `cl` (`/MT`)，支持 Windows `GetComputerNameExW` UTF-8 主机名与多字节码点安全截断，读取本地 `.keygen_secrets/master_ed25519.key` 编译产出 `./keygen/DataOG_Keygen`（或 Windows `.exe`），支持自动采集本机 IP/机器码、粘贴远端设备申请码 (`REQ1-...`) 签发与全局通用码签发（严格 `.gitignore` 隔离不入库）。
  - `demo_sandbox/`（独立银行信创演示沙箱套件，已于打包排除）：`docker-compose.yml`（PG16 仿真华为 DWS `15432` + Spark 3.5.5 HiveThriftServer2 仿真星环 Inceptor `11000` + MySQL 8 `13306`）、`banking_catalog.py`（164 条启明商业银行 8 大域标准字典与 46 列 Excel / 绑标模板导出）、`live_seeder.py`（确定性 `Random(20261119)` 造数与 5 大金融业务暗雷注入及 `dwd_loan_contract`/`dwd_acct_swap_demo` 改表靶表）、`workspace_builder.py`（输出 `qiming_live_sandbox.dataog.db` 与 156 星环表×162 DWS 表 `qiming_enterprise_150tables.dataog.db` 至 `.gitignore` 忽略目录 `demo_sandbox/output/`）、`sandbox_ctl.py` / `sandbox.sh`（`up` / `reset` / `status` / `generate-assets` / `bootstrap-demo-home` / `down`）及 `DEMO_PLAYBOOK.md` 高层演示剧本。
- **全局共享底座 (`core/platform/`) — 挂载前缀 `/api/platform`**：
  - `storage_paths.py`（三层分离混淆路径解析器：默认派生操作系统当前用户私有隐藏混淆目录及 `.dat`/`.bin` 非直观文件名，Windows 应用 `FILE_ATTRIBUTE_HIDDEN`、POSIX 应用 `0o700`，支持 `DATAOG_HOME` 测试隔离，提供单调时钟锚点混淆编解码与旧明文兼容）
  - `workspace_hub.py` (`WorkspaceManager`，挂载 `/workspaces*`：本地托管工作区列表/新建/克隆/重命名/热切换/删除、零密钥纯净 `VACUUM` 导出 `.dataog.db`、安全导入独立工作区、以及引导式三步增量合并 `inspect_merge_source` + `execute_merge`，兼容 `.dataog.db` 与旧版 `dataog_state.db`、严格 `mode=ro&immutable=1` + `query_only=ON` 白名单只读读取、智能端点指纹重映射、`supplement`/`newer`/`overwrite` 三大策略及跨环境表对差异预览，零物理路径暴露)
  - `context.py` (`PlatformContext`，默认启用双库物理分离并自动无损迁移旧版 `<app_dir>/data/`，同时兼容显式 `db_path` 单库模式；暴露 `workspace_hub` 与 `task_progress = import_progress`；`get_platform_context` 内置 Gate 2 请求级动态 HMAC 能力令牌复核) / `router.py`（含 `/auth/*` 免拦截认证路由及 `/workspaces*` 全套工作区协作路由） / `schemas.py` / `models.py`（含 `compute_connection_fingerprint`、`split_extra_props_secrets` 与 `ConnectionProfile.credential_status`）
  - `license_hub.py` & `_license_native_src/`（`license_core.h` + `license_shield.c`）：RFC 8032 Ed25519 确定性非对称签名校验、混淆拆分公钥查找表、C 原生动态库 64 位随机挑战-应答 MAC 防补丁桥接、防爆破限流、双位置硬件密封单调时钟防回拨水印（私有库 `app_meta['license_clock_seal']` + 私有库混淆编码时钟锚点文件）。
  - `storage.py` (`PlatformStateStore`，双库分离 WAL 状态引擎：Layer 2 私有保险箱承载 `app_meta`/`workspaces`/`llm_providers`/`connection_credentials`/`jdbc_drivers`/`execution_logs`/`explorer_sessions`/`explorer_messages`；Layer 3 共享工作区承载 `workspace_meta`/无密码 `connection_topology`/`standard_dictionary`/`field_standard_bindings`/`cross_table_bindings`/`cross_column_bindings`/`migration_items`/`meta_schemas`/`meta_tables`/`meta_columns`；支持 `module_db(scope="workspace"|"private")`、`register_table_schema(..., scope=...)`、`switch_workspace_db()`、端点指纹自动匹配个人凭据及 `migrate_legacy_install_dir()` 一次性幂等无损迁移)
  - `standard_catalog.py` (`StandardCatalog`，管理全局 46 列标准字典 Upsert/Replace 导入与导出、脱敏系统模板生成、多库物理格式智能解析、三级落标判定、`bulk_save_field_bindings` 单事务分块绑定持久化、B-Tree 半开区间精确清理与 `match_standard_candidates_batch` 批量候选匹配，挂载 `/standards*`)
  - `offline_report.py`（库级单 Sheet `.xlsx` 与零外部依赖离线交互 `.html` 生成引擎：`build_single_db_excel_bytes`、`build_cross_env_excel_bytes`、`build_single_db_html_bytes`、`build_cross_env_html_bytes`，跨环境报告输出含「目标长度/精度包络」「是否涉及类型转换」的 24 列体系、KPI/胶囊/下拉筛选、内嵌纯 JS 单 Sheet OOXML `.xlsx` 导出器、XSS 安全 JSON 嵌入与 Excel 公式防注入清洗）
  - `import_progress.py` (`ImportProgressHub` / `TaskProgressHub`，线程安全任务进度与 `0.35s` 节流 `recent_logs` 缩略微日志环形缓冲、`Condition` 唤醒与取消控制，挂载 `/import-progress/{progress_id}`、`/tasks/{task_id}`、`/tasks/{task_id}/cancel` 与 SSE `/tasks/{task_id}/stream`)
  - `excel_io.py` & `core/excel_formula.py`（统一提供 Excel 缓存结果优先读取、纯 Python 跨表公式兜底求值及 `#N/A` 等全谱系公式错误值置空清洗）
  - `readonly_guard.py` (`validate_readonly_sql` / `ReadOnlyViolationError`，双模态 ANSI + 反斜杠转义词法分词器、单语句校验、`SELECT`/`WITH ... SELECT`/`SHOW`/`DESCRIBE`/非 `ANALYZE` `EXPLAIN` 顶层白名单、全语句写/DDL/锁/`INTO` 与跨方言危险系统函数黑名单)
  - `asset_store.py` (`AssetStore`，管理壁纸预设、本地上传壁纸、`Data.O.G` 主图标与四大模块自定义图标持久化至 `data/assets/`，挂载 `/appearance*` 与 `/assets/{asset_name}`)
  - `llm_hub.py` (`LLMHub`，基于 Python 标准库 `urllib.request` 零额外依赖统一接入 DeepSeek、Google Gemini 与自定义 OpenAI 兼容网关，挂载 `/llm/providers*`、`/llm/test`、`/llm/models`)
  - `driver_manager.py` & `inceptor_client.py` (`DriverManager`，双轨驱动：`NATIVE` + 按需懒加载 `JDBC`，支持 `open_connection(..., readonly=True)` 会话级硬件只读锁)
  - `connection_hub.py`（含 `readonly_session` 退出无条件 `rollback()`、`probe_session` 60 秒非 SQLite 温连接复用及按库持久化 `standard_format`：`PUT /connections/{connection_id}/standard-format`） / `metadata_hub.py`（含 `120s` 缺失表负缓存、单连接四层视图穿透、`get_tables_metadata_batch` 单会话系统目录批量探查、DWS/PG `lock_timeout`/`lockwait_timeout`/`statement_timeout` 锁保护降级、Spark/新版 Inceptor `DESCRIBE FORMATTED` 视图/注释兼容与逐表异常熔断） / `query_engine.py`（含 `execute_readonly_sql` 与 `POST /query/execute-readonly`） / `excel_io.py` / `dialects/`（含 `parse_sql_type_spec` 与 `analyze_cross_column_compatibility` 跨库字段长度/精度包络与类型转换双维诊断引擎） / `core/dws_engine.py`（使用 `contextlib.closing` + `_rollback_if_needed` 确保标准 PostgreSQL 上探查 DWS 专有函数缺失安全降级无 `InFailedSqlTransaction`）
- **独立业务插件模块 (`modules/`，严格零跨模块互引)**：
  - `modules/standard_migrator/`（`/api/modules/standard_migrator`）：支撑「2. 落标绑定与贯标」，支持左侧按数据库粒度设定并持久化落标标准格式，覆盖字段数标绑定（`/bindings/*`）、批量对照导入与按表/全库（`scope=all`）预填模板、三维分级落标核验（`analyze_item_changes`/`item_requires_table_rebuild` 精确隔离仅注释差异）、全局持久化跨表/跨库贯标队列（`connection_id` + `upsert_migration_items` + `POST /checklist/delete-items` + 零远端重连全内存生成 `stage-migration`）、单条多方言 SQL 预览（`GET /items/{item_id}/sql`）、单库保序迁移执行台（按 `connection_id` 路由 `DIRECT_ALTER` / `TABLE_REBUILD`、Kahn 拓扑改名、分区继承、并发作业池）及免勾表库级报告导出（`GET /bindings/report.html` 与单 Sheet `GET /bindings/report.xlsx`，空目录先自动采集再生成），全链路接入 `X-Task-Id` 阶段微日志。
  - `modules/cross_comparator/`（`/api/modules/cross_comparator`）：支撑「3. 跨环境比对」，自动沿用第 2 步按库设定的标准格式与两侧字段绑定，输出五虚拟列图形化对比工作台模型（`UNIFIED` 一致单卡 / `SPLIT_DIFF` 异标双胶囊 / `SPLIT_PARTIAL` 单侧绑定）及 Schema 隔离表对解析；全量挂载 `cross_compat` 字段级目标长度/精度包络（`CONTAINED`/`INSUFFICIENT`/`RISK`）与类型转换（`NONE`/`SAFE_CAST`/`CAST_REQUIRED`）诊断；提供 `GET /pairs/search` 按来源/目标表名及字段名快速检索已保存表对、轻量 `/compare-pair` 双库并发探查、`/prefetch-pairs` 批量预热、拖拽/点选配对保存（`/pairings/save` 双向回写）、铺底模板导入、一键推送至第 2 步贯标及库级报告导出（`GET /report.html` 与单 Sheet `GET /report.xlsx`，空目录先并发采集、同库自比对仅刷新一次，输出已保存映射表对五栏模型 + `_compute_recommended_pairings` 推荐同名未保存表清单不自动预比对 + 单侧独有表）。
  - `modules/ddl_translator/`（`/api/modules/ddl_translator`）：支撑「多邻国」模块，含 `POST /naming`（AI 大模型 + 内置 170+ 数仓标准词根双驱动智能命名与建表草稿生成）与 `POST /translate`（数据源库表/粘贴 `CREATE TABLE` 或 `VIEW`/自由异构 SQL 跨方言互译、四层递归视图穿透与 `AST+AI` 语法适配），接入 `X-Task-Id` 进度微日志。
  - `modules/data_explorer/`（`/api/modules/data_explorer`）：支撑「数据探查 · AI 探查」多轮目标驱动只读探查会话，含 `/sessions*` 会话历史管理与 `POST /explore` 多步自动采样推理循环（单步采样上限 `200` 行、`12s` 超时、单轮最多 `8` 步熔断、`recommended_sql` 二次只读清洗与实时步骤微日志推送）。
- **前端原生 ES Modules macOS 桌面工作台 (`static/`)**：
  - `dataog.ico`、`index.html`、`css/tokens.css`（明暗双主题与内置壁纸变量）、`css/workbench.css`、`js/main.js`（纯净桌面画布、底部悬浮毛玻璃 macOS Dock 栏、最小化 Dock 右侧收纳、全屏 Dock 自动下沉贴底唤出、`⌘K` Spotlight、日志控制台）
  - 核心层 `js/core/`：`api.js`（含 `AbortSignal`、`X-Task-Id`、`downloadApiFile` 取消与任务追踪、`tasksApi`、`comparePair`/`prefetchPairs`/`crossApi.searchPairs` 与库级报告/全库模板下载接口）、`dom.js`、`state.js`、`type_compat.js`（零依赖前端实时兼容诊断 `analyzeCrossColumnCompat`）
  - 公共组件 `js/ui/`：`auth_gate.js`（`AuthGateController` 全屏毛玻璃登录拦截页、激活码/账号模式切换、本机 IP/机器码识别与申请码复制、原生 `<dialog>` 过期日期提醒及菜单栏授权管理/锁定弹窗）、`task_activity_bar.js`（`TaskActivityBar`：`180ms` 防闪烁毛玻璃活动条 + SSE/轮询 + 缩略微日志气泡）、`export_report_modal.js`（`openExportReportModal`：统一离线网页 `.html` 与单 Sheet `.xlsx` 导出弹窗）、`database_tree.js`（DataGrip 风格四级数据库目录树，支持 `single`/`multi` 模式、紧凑刷新进度条及表级自定义徽标渲染）、`data_grid.js`、`object_picker.js`、`sql_code_box.js`、`upload_wizard_modal.js`、`standard_picker_modal.js`、`modal.js`、`toast.js`
  - 四大桌面应用视图 `js/views/`：
    - **系统配置**：`settings_view.js` + `workspace_view.js`（团队共享工作区管理与三步引导式增量合并向导） + `connections_view.js`（外观与图标 / 工作区协作 / 数据库连接含个人凭据匹配状态徽标 / JDBC 驱动 / AI 模型）
    - **数据探查**：`sql_console_view.js` + `explorer_ai_pane.js`（`SQL 查询` | `AI 探查`，共用左侧 DataGrip 四级数据库清单、实时任务微日志并支持探查 SQL 一键同步至主编辑器）
    - **数据标准**：`standard_view.js` + `standard_dict_view.js` + `standard_migrator_view.js`（含左侧按库设定标准格式卡片与统一库级报告导出） + `cross_comparator_view.js`（macOS Sequoia 五虚拟列防重叠工作台 + 吸顶毛玻璃三列表头分隔与平静分段筛选栏/IME 安全检索 + 拖拽边缘自动滚屏与吸底 `+ 绑定新标准` 投放区 + 持久化双端 macOS 弹出选单与防遮挡向上翻转缓存表浮层 + `.mac-subbar` 可搜索表名/字段级 `.cross-pair-picker` + 按需紧凑长度包络/类型转换诊断底栏 + `TaskActivityBar` 秒切预热 + 统一库级报告导出）（`1. 标准字典` | `2. 落标绑定与贯标` | `3. 跨环境比对`）
    - **多邻国**：`ddl_translator_view.js`（智能命名 | 方言翻译）
- **全量自动化测试套件 (`tests/`，当前共 67 项测试全绿，内置标准库内存 ASGI 测试客户端免 `httpx` 依赖)**：
  - `tests/test_workspace_separation.py`（9 项）：验证三层分离存储架构——三端混淆 OS 私有路径无品牌明文与全量 API（含跨平台错误中间件）零路径泄露、双库物理表级隔离与零密钥 `.dataog.db` 导出（含 JDBC URL 内嵌凭据剥离）、多工作区生命周期与同 ID 不同端点凭据隔离防互毁/端点指纹自动匹配、引导式增量合并（兼容旧版 `dataog_state.db`、白名单只读防污染、端点重映射与同 ID 不同端点无冲突派生、`table_diffs` 表对预览、全列标准差异预览、空 `updated_at` 不覆写 `newer`、混合大小写去重、已缓存列结构不降级与 `supplement`/`newer`/`overwrite` 三大策略）、旧版 `<exe_dir>/data/` 只读目录 WAL 全量无损拆分迁移与幂等性及 Node.js 真实 ES Module 前端交互契约。
  - `tests/test_core.py`（16 项）：验证单库保序重建、拓扑改名、分区继承、仅注释差异精准识别与多方言单条 DDL 生成（`test_comment_only_migration_sql_and_multi_dialect_preview`）、标准 PostgreSQL 无 DWS 系统函数安全降级、三栏比对、跨库长度/精度包络与类型转换双维判定全矩阵（`test_cross_column_capacity_and_type_conversion_matrix`）、DDL 互译与四层视图穿透核心引擎。
  - `tests/test_platform.py`（16 项）：验证旧版状态库无损迁移、数据源 `standard_format` 幂等迁移与持久化、`TaskProgressHub` 缩略微日志与 SSE、`ConnectionHub.probe_session` 60s 温连接复用、`MetadataHub` 120s 负缓存与单连接视图穿透、Spark Thrift / 新版 Inceptor `DESCRIBE FORMATTED` 解析、双轨驱动 `.jar` 安全校验与无 Java 降级、`MetadataHub` 缓存检索与单会话多表批量探查（含 DWS/PG 会话级锁等待超时降级与逐表异常熔断）、`StandardCatalog` 46 列导入与多库格式解析及 B-Tree 区间绑定清理、Excel 公式最终结果求值与 `#N/A` 置空清洗、`QueryEngine` 安全截断、`AssetStore` 光栅魔数头与路径穿越防护、`LLMHub` 标准库 HTTP 回环与密钥脱敏、`ReadOnlySqlGuard` 红队绕过拦截矩阵及跨驱动硬件只读回滚防线。
  - `tests/test_modules_and_api.py`（17 项）：验证 FastAPI 全路由契约、macOS 底部 Dock 与三步式数据标准（标准字典 -> 按库设标与落标绑定核验 -> 全局持久化跨表/跨库贯标队列零远端重连入队/移出/单条 SQL 预览与前端 `DataGrid` 原位勾选优化 `test_global_persistent_migration_queue_perf_and_sql_preview` -> 跨环境五虚拟列防重叠网格、吸顶表头对齐、筛选态 `realIdx` 完整性、Schema 隔离与 `.mac-subbar` 顶栏防遮蔽闭环）、跨库长度/精度包络与类型转换判定、`/pairs/search` 表及字段级检索与 24 列报告导出（`test_cross_compat_pair_search_and_report_columns`）、`/compare-pair` 轻量切表与 `/prefetch-pairs` 批量预热及跨模块 `X-Task-Id` 缩略微日志、单库与跨环境库级离线交互 `.html` / 单 Sheet `.xlsx` 报告（空目录先采再出、推荐同名未保存表不预比对、Node.js 内嵌纯 JS `.xlsx` 24 列闭环与搜索框 DOM 稳定性校验）、4631 行×160 表批量绑标压测与离线探查熔断保护、全流程系统模板下载、外观与自定义图标端到端流转、多邻国智能命名与 `AST+AI` 方言适配、AI 只读数据探查多步采样与幻觉写语句实时拦截保护、插件模块零互引（AST/字节码双模）及前端零 `innerHTML` 安全红线。
  - `tests/test_license_auth.py`（7 项）：验证 RFC 8032 Ed25519 标准向量、C 原生护盾 64 位挑战-应答 MAC 防补丁桥接与闰年日历对拍、生产混淆公钥预签发向量核验、Gate 1 + Gate 2 双重未激活 `401` 拦截、确定性设备专属码（多网卡适配、异机/异 IP 拷贝状态库拦截、篡改状态库拦截）、全局通用码/过期/防时钟回拨水位线/手动锁定/账号模式禁用、独立零依赖 C 生成器编译与 CLI 闭环，以及 Windows 多编译器静态命令矩阵与 `_WIN32` C11 头文件严格语法回归校验。
  - `tests/test_demo_sandbox.py`（2 项）：零 Docker 验证启明银行金融标准字典（164 条）与 5 大暗雷确定性合成、双零密钥 `.dataog.db` 工作区生成导入及 150 表工作区离线 `.html` / `.xlsx` 报告全谱系检出。

## 6. AI 只读探查硬隔离铁律与后续演进守则
- **绝对只读四道硬防线（后续任何涉及 AI 自动查询的功能均须严格复用 `QueryEngine.execute_readonly_sql`，严禁仅靠 Prompt 软约束）**：
  1. **双模态词法/语法白名单（`core/platform/readonly_guard.py`）**：同时在标准 SQL 与反斜杠转义双模式下分词，强制单语句，顶层仅放行纯只读语句，递归校验 CTE 子句，全语句封禁写/DDL/锁/`INTO` 与危险系统函数。
  2. **数据库会话级硬件只读锁（`DriverManager.open_connection(..., readonly=True)`）**：SQLite `mode=ro` + `PRAGMA query_only=ON`、PostgreSQL/DWS `set_session(readonly=True, autocommit=False)`、PyMySQL `SET SESSION TRANSACTION READ ONLY` + `START TRANSACTION READ ONLY`、JDBC `setAutoCommit(False)` + `setReadOnly(True)`。
  3. **零提交与退出无条件回滚（`ConnectionHub.readonly_session`）**：只读执行路径零 `commit()` 调用，会话 `finally:` 块无条件执行 `rollback()`。
  4. **采样行数、超时与步数熔断**：单步强制采样上限 `200` 行、`12s` 超时，单轮自动探查最多 `8` 步熔断。
