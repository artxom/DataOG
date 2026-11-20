# Changelog

本项目的所有重要版本演进与工程变更均记录于此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号严格遵循 [语义化版本 (SemVer 2.0.0)](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- **「启明商业银行」信创数仓治理混合演示沙箱套件 (`demo_sandbox/`)**：
  - 提供独立容器编排与运维脚本 (`docker-compose.yml`、`sandbox_ctl.py`、`sandbox.sh`)：以 PostgreSQL 16 (`15432`) 仿真华为 GaussDB(DWS)、Spark 3.5.5 HiveThriftServer2 (`11000`) 仿真星环 Inceptor、MySQL 8.0 (`13306`) 扮演核心交易源库，并支持 `--registry-prefix` 镜像加速、`reset` 8 秒不重启容器极速复原及 `bootstrap-demo-home` 隔离预装。
  - 构建 164 条金融数据标准资产 (`banking_catalog.py`)，支持导出 46 列多库物理格式标准字典 Excel 与批量绑标示范模板；构建确定性造数引擎 (`live_seeder.py`)，灌入约 3 万行客户/账户/合同/借据/流水/押品/授信样本，精准注入 5 大金融业务暗雷（7 笔负余额不良 `-1,680万`、12 笔逾期 >180 天错标正常 `1.48亿`、48 笔 `4.95~4.99万` 反洗钱拆单 `238.92万`、核心 MySQL vs DWS `3,650万` 授信对账差额、5 笔 `>=1000万` 零利率无押品放款 `9,000万`）及 `DIRECT_ALTER` / `TABLE_REBUILD` 现场真改表靶表、四层级联视图。
  - 构建双零密钥 `.dataog.db` 工作区生成器 (`workspace_builder.py`)，产出活库现场工作区与 156 星环表×162 DWS 表超大规模压测兜底工作区，配套高层演示剧本 (`DEMO_PLAYBOOK.md`)；同步更新 `build_exe.py` 与 `.gitignore` 严格隔离生成产物与安装包。
- **全量自动化测试扩充至 67 项 (`tests/test_demo_sandbox.py`, `tests/test_core.py`, `tests/test_platform.py`, `tests/test_modules_and_api.py`)**：新增标准 PostgreSQL 无 DWS 系统函数安全降级单测、Spark/新版 Inceptor `DESCRIBE FORMATTED` 解析单测、零 Docker 银行沙箱全链路闭环单测、仅注释差异 SQL 生成与多方言预览单测（`test_comment_only_migration_sql_and_multi_dialect_preview`）以及全局跨表/跨库持久化贯标队列与前端防重绘收敛端到端单测（`test_global_persistent_migration_queue_perf_and_sql_preview`）。

### Changed
- **全局持久化贯标执行队列与操作精简 (`core/platform/storage.py`, `core/platform/workspace_hub.py`, `modules/standard_migrator/`, `modules/cross_comparator/service.py`, `static/js/views/standard_migrator_view.js`, `static/js/core/api.js`)**：
  - `migration_items` 新增 `connection_id` 字段并支持增量 Upsert（`upsert_migration_items`）与精准批量移除（`POST /api/modules/standard_migrator/checklist/delete-items`），同步适配工作区合并重映射；切表、切换数据源或从「3. 跨环境比对」推送均不再清空既有队列任务，纳入队列后保持停留在绑定页并实时点亮 `已入队` 状态。
  - 彻底移除「字段落标绑定与核验」行内冗余的「重建贯标 / 生成贯标」双按钮分支，收敛为数据标准绑定（`换绑`/`选标`/`解绑`）与统一的 `纳入贯标`（批量按钮为 `纳入贯标任务` / `纳入本表待改字段`）。
  - 贯标队列按任务所属 `connection_id` 自动路由 `sync_items`、`execute_single`、`execute_batch` 及整表保序重建，并新增 `GET /api/modules/standard_migrator/items/{item_id}/sql` 按目标库方言预览单条 SQL。

### Fixed
- **全链路卡顿与响应慢系统性优化（零大模型 API 调用瓶颈消除） (`core/platform/license_hub.py`, `core/platform/storage.py`, `core/platform/standard_catalog.py`, `modules/standard_migrator/service.py`, `static/js/ui/data_grid.js`, `static/js/ui/database_tree.js`, `static/css/workbench.css`)**：
  - 明确单库落标绑定、入队生成与贯标 SQL 生成 100% 由本地确定性规则/方言引擎完成（未调用大模型 API）；精准定位真实慢因并完成四层优化：① 消除 `stage-migration` 每次同步全队列触发远端数据库连接与全库统计扫描，改为利用本地元数据与标准规范全内存预计算 DDL 与执行策略；② `LicenseHub` 新增 60 秒热验签缓存、`get_meta_batch` 单次读库、60 秒真实时钟水印落盘节流与 Gate 2 30 秒 HMAC 能力令牌快速复核（消除每请求重复 Ed25519 + 双倍 IO）；③ `StandardCatalog.purge_invalid_error_bindings` 按 `(workspace_db_path, connection_id)` 会话级幂等缓存去重（导入场景 `force=True`）；④ `DataGrid` / `DatabaseTree` 勾选与点选改为 `DocumentFragment` + 原位 DOM 类名同步（彻底消除全表重建重绘），搜索输入防抖 `120ms`，收敛 CSS 重度 `backdrop-filter` 模糊半径并为表格/目录树补齐 `contain: layout style` 与纯合成器 `taskPulse`。
- **修复「仅注释差异」场景生成错误重构/冗余 DDL 问题 (`core/platform/dialects/types.py`, `core/dws_engine.py`, `modules/standard_migrator/service.py`, `static/js/views/standard_migrator_view.js`)**：
  - 新增 `analyze_item_changes` 与 `item_requires_table_rebuild`，严格识别并保留仅注释差异列（兼容大小写与 `VARCHAR2`/`DEC`/`DOUBLE`/`DATETIME`/`STRING` 同义词）：即使处于 DWS 列存表或分布键/分区键上也绝不误触发整表重建（`TABLE_REBUILD`），`DIRECT_ALTER` 原地仅输出干净的 `COMMENT ON COLUMN`（不改原字段名大小写、零差异时不生成空 `BEGIN; COMMIT;`），整表重建顺带透传时跳过多余 `CAST`/`AS`；队列 `SQL` 按钮改为按任务直达多方言单条 SQL 预览弹窗（`openItemSqlPreviewModal`），对真实需重建项再引导打开保序重建脚本预览。

### Fixed
- **修复 `DWSMigratorEngine` 在标准 PostgreSQL 上探查 DWS 专有函数失败触发 `InFailedSqlTransaction` 问题 (`core/dws_engine.py`)**：将只读探查与执行路径中的 `with self._open_dws_connection() as conn:` 统一改为 `contextlib.closing(...)` 以确保 `conn.autocommit = True` 真正生效并在退出时释放连接，同时在各降级 `except` 分支补充 `_rollback_if_needed(conn)` 保护。
- **增强 `MetadataHub._probe_inceptor_table` 对 Spark Thrift / 新版 Inceptor `DESCRIBE FORMATTED` 的兼容解析 (`core/platform/metadata_hub.py`)**：支持 `Type=VIEW`、`View Text` 与 `Comment` 在首两列的详情提取。

---

## [2.7.0] - 2026-11-19

### Added
- **三层分离存储架构（无状态程序包 × 混淆本地私有保险箱 × 团队可共享工作区 `*.dataog.db`） (`core/platform/storage_paths.py`, `core/platform/storage.py`, `core/platform/workspace_hub.py`, `core/platform/context.py`, `core/platform/models.py`)**：
  - **Layer 1 无状态程序包**：应用安装目录（`DataOG.exe`、`_internal/`、`static/`）彻底不再创建或写入 `data/` 目录，`build_exe.py` 与 `.github/workflows/ci.yml` 打包产物严禁携带 `data/`，后续版本全量覆盖解压升级绝不会覆盖用户的个人配置与业务进度。
  - **Layer 2 混淆本地私有保险箱**：默认存储于操作系统当前用户私有隐藏目录（Windows `%LOCALAPPDATA%` + `FILE_ATTRIBUTE_HIDDEN`、macOS `~/Library/Application Support` 隐藏路径、Linux `$XDG_DATA_HOME`/`~/.local/share` 隐藏路径 + `0o700` 权限，支持 `DATAOG_HOME` 环境变量用于测试隔离），目录层级与数据库/单调时钟锚点文件名均经确定性混淆派生且对锚点内容进行混淆编码；统一隔离存放离线激活码、锁定状态、单调时钟密封、`llm_providers`（AI API Key）、个人数据库凭据库（`connection_credentials`，支持 `connection_id` 与 `(db_type, host, port, dbname)` / `jdbc_url` 端点指纹双通道自动匹配）、壁纸与图标资产、JDBC `.jar` 驱动包、本地 `execution_logs`、个人 AI 探查会话（`explorer_sessions` / `explorer_messages`）及本地工作区注册表（`workspaces`）。
  - **Layer 3 纯净团队共享工作区 (`*.dataog.db`)**：仅包含协同业务数据，结构上物理剔除 `user`/`password` 及所有私有表（`workspace_meta`、无密码 `connection_topology`、`standard_dictionary`、`field_standard_bindings`、`cross_table_bindings`、`cross_column_bindings`、`migration_items` 及可选 `meta_schemas`/`meta_tables`/`meta_columns` 缓存）。
- **团队工作区管理与三步引导式增量合并向导 (`core/platform/workspace_hub.py`, `core/platform/router.py`, `static/js/views/workspace_view.js`, `static/js/views/settings_view.js`, `static/js/views/connections_view.js`, `static/index.html`, `static/js/main.js`, `static/css/workbench.css`)**：
  - 新增全套 `/api/platform/workspaces*` 路由（受 Gate 1 + Gate 2 双重激活码授权保护），在「系统配置 → 工作区协作」及顶部菜单栏工作区胶囊中提供多工作区新建（空白/克隆）、重命名、热切换、删除、零密钥纯净导出（`.dataog.db`，可选是否含元数据缓存）与安全导入为独立工作区。
  - 新增三步引导式 **「合并同事进度」向导**：支持上传 `.dataog.db` 或旧版 `dataog_state.db`，严格以 `mode=ro&immutable=1` + `PRAGMA query_only=ON` 仅读取白名单业务表，自动按端点指纹映射数据源并预览本机个人凭据匹配状态，提供跨环境映射表对差异预览（`新增`/`冲突`/`一致`），支持按模块自由勾选与三种冲突策略（`supplement` 仅补充缺失与未绑定 / `newer` 按 `updated_at` 保留较新 / `overwrite` 覆盖冲突项但保留本地独有项），单事务原子落盘。
  - 在「系统配置 → 数据库连接」列表中新增个人凭据自动匹配徽标（`已自动匹配个人凭据` / `待填个人账号密码`），方便团队导入共享工作区后按需补充个人数据库凭据。
- **启动自动无损拆分迁移 (`PlatformStateStore.migrate_legacy_install_dir`)**：升级至 `v2.7.0` 首次启动时自动检测旧版 `<exe_dir>/data/dataog_state.db`（或 `dws_migrator_state.db`）、`assets/`、`drivers/` 及 `.sys_clock_anchor`，一次性幂等无损拆分迁移至私有保险箱与默认共享工作区，保留原目录文件作为安全备份。
- **全量自动化测试扩充至 61 项 (`tests/test_workspace_separation.py`)**：新增 9 项深度端到端测试，全面覆盖三端混淆私有路径（无品牌名且无 `.db` 后缀）与全平台错误中间件零路径暴露守卫、物理双库表级隔离与导出原始字节（含 JDBC URL 内嵌凭据剥离）防泄密校验、多工作区生命周期/跨工作区删连接保凭据/同 ID 不同端点凭据防互毁与无冲突合并派生/重启零幽灵连接/端点指纹凭据自动匹配、旧版状态库及对抗边界增量合并与三大策略、只读目录 WAL 无损迁移幂等性以及 Node.js 真实 ES Module 前端三步合并向导 DOM 交互契约。

### Changed
- **正式升级至重大版本 `Data.O.G v2.7.0`**：同步升级 `app.py` 与 `PlatformContext` 版本号至 `2.7.0`；将 `modules/data_explorer/service.py` 的 `explorer_sessions` 与 `explorer_messages` 显式路由至私有库 (`scope="private"`)，避免个人探查会话混入团队共享工作区。

### Fixed
- **无头浏览器全场景视觉与布局审校修复 (`static/css/workbench.css`, `static/js/views/workspace_view.js`, `static/js/main.js`)**：
  - 补齐全局 `[hidden] { display: none !important; }` 守卫，彻底修复未激活登录门禁页上因 `display: inline-flex/flex` 覆盖 `hidden` 属性导致顶部工作区胶囊、激活状态、日志 `0` 徽标与底部 Dock 意外显露的问题。
  - 修复窄视口（`768px`~`960px`）或长工作区名称下顶部 28px 菜单栏文本折行与竖向裁切，为工作区胶囊与 AI 胶囊补齐单行省略截断与悬停完整标题提示。
  - 修复三步合并向导 Step 2 多表差异预览下 `.dialog-header` 因缺少 `flex-shrink: 0` 被压缩至 `26px`、步骤条序号重复显示以及第 5 列「差异说明」横向裁切与数据源预览卡片挤压问题，并统一 `:focus-visible` macOS 蓝光焦点环。
- **三层分离存储安全与增量合并深度加固 (`core/platform/storage.py`, `core/platform/workspace_hub.py`, `core/platform/models.py`, `core/platform/storage_paths.py`, `app.py`, `static/js/views/workspace_view.js`, `static/js/main.js`)**：
  - 修复跨工作区或同事分享文件中出现同 `connection_id`（如 `legacy-dws-workbench`）指向不同数据库端点时引发的凭据串绑、覆写旧工作区密码及合并映射错乱问题：将 `connection_credentials` 主键升级为 `(connection_id, endpoint_fingerprint)` 独立隔离键，凭据匹配严格校验端点指纹兼容性，并在合并 ID 冲突时确定性派生无冲突新 `connection_id`。
  - 新增全格式 `sanitize_jdbc_url` 清洗器（覆盖 Oracle thin/oci、MySQL/PG authority 与 query、SQLServer/DB2 分号参数），自动剥离 JDBC URL 内嵌凭据至 Layer 2 私有凭据库，杜绝密码随 `jdbc_url` 泄露入共享 `.dataog.db`。
  - 统一 `redact_private_storage_paths`（覆盖 `abspath`/`realpath` 及 JSON 转义反斜杠）并接入 `app.py` 全平台 `/api/*` 错误响应兜底脱敏与旧库迁移日志脱敏，彻底封堵 `/drivers/upload`、`/appearance/upload` 等非工作区接口及 macOS `/private/var` 软链路径泄露；同步加固 Windows `GetFileAttributesW`/`SetFileAttributesW` `ctypes` 参数签名，以及只读旧库迁移改用 `shutil.copyfile` + `0o600` 权限修复只读文件 WAL 恢复与临时目录清理隐患，并修复旧库迁移切至空白工作区后 `/api/bootstrap` 残留旧连接问题。
  - 修复增量合并多项边界一致性：标准字典预检升级为全列比对与执行逻辑 1:1 对齐；`newer` 策略不再将空 `updated_at` 误填为当前时间覆写本地较新绑定或跨环境表对；混合大小写重复 `std_table_key` 不再触发唯一约束崩溃；本地已缓存完整列结构（`columns_cached=1`）不被目录概要（`columns_cached=0`）降级覆盖；`list_workspaces` 补齐线程锁消除与删除竞争；前端修复「导入为新工作区」取消勾选立即切换时误触发 `workspace_switched` 销毁已打开窗口、步骤切换滚动复位、表对变更提示及超 300 表截断计数标注。

### Security
- **私有存储路径零暴露红线**：移除终端启动横幅中的状态库物理路径打印，严禁在任何 API 响应、前端页面或日志中暴露私有存储根目录与混淆文件名；导出 `.dataog.db` 与导入/合并同事文件严格实行白名单表与白名单列（物理隔离 `user`/`password`/`api_key`/`license_*`）及 SQLite 魔数头与 `realpath` 边界校验。

---

## [2.6.1] - 2026-11-19

### Changed
- **正式发布 `Data.O.G v2.6.1`**：同步升级 `app.py` 与 `PlatformContext` 版本号至 `2.6.1`，整合离线非对称激活码防反编译护盾、「3. 跨环境比对」macOS Sequoia 防重叠重构与 Windows 云端独立 `.exe` 自动化发布流水线，全量自动化单元测试扩展至 **52 项**。
- **整体重新设计「3. 跨环境比对」为原生 macOS Sequoia 视觉与五虚拟列工作台 (`static/js/views/cross_comparator_view.js`, `static/css/workbench.css`, `static/css/tokens.css`, `static/js/core/dom.js`)**：
  - 重构顶部控制栏与对称双端数据源配置甲板（`来源库` ⇄ `标准对齐` ⇄ `目标库`，`< 920px` 窄窗自动纵向叠放）：将所有 `<select>` 升级为明暗双主题原生感 macOS `NSPopUpButton` 上下箭头弹出按钮，将双端 `Schema` / `表名` 输入框升级为持久化防失焦节点（支持粘贴 `schema.table` 自动拆分覆盖旧模式名），并将 `选表 (N) ▾` 升级为支持关键字实时检索、中文输入法组合态 (`isComposing`) 防误触、键盘上下选表、`150` 项截断保护、一键目录刷新及矮屏自适应翻转钳高 (`.is-flip-up`) 的 macOS 毛玻璃浮层。
  - 在中栏吸顶毛玻璃表头（`position: sticky` + `backdrop-filter`，含三列标题徽标聚拢与竖向列分隔线）下集成单行定高对齐的平静中性 macOS 状态筛选栏（`全部` / `一致` / `差异` / `单侧` / `待贯标` / `位长/精度不足` / `类型转换`，消除未选中红橙边框噪音、支持滚轮横向滑动与柔化边缘渐隐，并配合响应式五栏轨道保障 `1024px`/`840px` 五列同屏完整可见且零生硬截字）与 IME 安全的实时搜索框，支持在筛选态下基于真实行索引 (`realIdx`) 精准增删改配对、绑定新标自动揭示隐藏行且全量保存绝不丢失隐藏行。
  - 将中栏对标卡片升级为纯粹中性的明暗双主题 macOS 卡片 + 水平连接桥线与左侧风险色条（彻底移除一致态整卡绿框与红绿撞色噪音，异标三键统一为无歧义的 `采用源标` / `采用目标` / `拆为两行`）；正常兼容行自动收敛为紧凑双行卡片（省去冗余 `可包住` / `无转换` 底栏，`1440×900` 同屏清晰容纳 9+ 行），修复长物理类型胶囊（如 `TIMESTAMP(6) WITHOUT TIME ZONE`）块级省略号截断，并补齐四套明暗主题细滚动条令牌避免经典白色滚动条突兀展示。

### Fixed
- **修复 Windows 云端打包 `_license_shield.dll` 与独立生成器编译兼容问题 (`core/platform/_license_native_src/license_core.h`, `core/platform/license_hub.py`, `tools/build_keygen.py`, `tools/keygen_src/keygen.c`, `build_exe.py`, `.github/workflows/ci.yml`, `tests/test_license_auth.py`)**：
  - 移除 `license_core.h` 在 `_WIN32_WINNT < 0x0A00` 时未声明的 Windows 10 专属宏 `RRF_SUBKEY_WOW6464KEY`，改为全版本 Windows SDK / MinGW-w64 / MSVC 通用的 `RegOpenKeyExA(..., KEY_READ | KEY_WOW64_64KEY, ...)` + `RegQueryValueExA`（与 Python `winreg` 100% 对齐），接入 `GetComputerNameExW + WideCharToMultiByte(CP_UTF8)` 与 RFC 3629 多字节 UTF-8 码点边界安全截断消除中文计算机名 GBK/半字符截断编码崩溃，补齐 DLL 内部 `WSAStartup` 自初始化，并优化网卡排序策略（单次 `GetAdaptersInfo` + 物理网卡优先同时保留 Hyper-V / Azure / VMware 云主机主网卡与无物理网卡时的 UDP 默认路由自动提权回退，防止云端与企业虚拟机被误判为 `127.0.0.1`）。
  - 升级 `compile_native_license_shield` 与 `build_keygen` 支持 Windows 多编译器静态运行时构建矩阵（MinGW `gcc` `-static -static-libgcc -fno-stack-protector` 零外部 MinGW DLL 依赖、LLVM `clang` `--dependent-lib=libcmt`、MSVC `cl` `/MT`），编译至临时目录并通过 `ctypes` 加载自检 + `_ctypes.FreeLibrary` 立即释放句柄 + 旧版已映射 DLL 改名腾位与无崩溃降级；调整 `build_exe.py` 在运行单测前先行编译原生护盾并校验打包产物完整性，并在 CI 压缩发布包前清理 `--test` 遗留运行日志，新增第 52 项跨平台 `_WIN32` C11 严格语法与中文 UTF-8 边界回归单测。
- **彻底根除「3. 跨环境比对」多字段行纵向挤压互相叠加、滚动位置丢失与表头裁切错位问题 (`static/css/workbench.css`, `static/js/views/cross_comparator_view.js`, `tests/test_modules_and_api.py`)**：
  - 修复原 `.cross-matrix-pane` 采用 `display: flex; flex-direction: column` 配合 `.cross-matrix-row { min-height: 52px }`（未声明 `flex-shrink: 0`）导致字段数较多时每行被强制压缩至 `52px`、内部 3 行卡片内容向下溢出并互相叠加覆盖的严重布局缺陷；改用纯块级滚动容器 + `display: grid; grid-auto-rows: max-content; align-content: start` 与 `flex-shrink: 0` 双重防挤压机制，确保任意行数与内容高度自适应零重叠。
  - 将五栏滚动容器改为持久化 DOM 实例挂载，彻底解决点击待选卡片、解绑、绑标或统一异标时重渲染导致三栏 `scrollTop` 跳回顶部（`0`）的问题，支持 `Escape` 取消已选待选字段、长表拖拽上下边缘自动滚屏（`dragover` auto-scroll）及点选/拖拽态下吸底悬浮 `+ 绑定新标准` 投放栏（任意滚动位置零翻页直达绑标）。
  - 修复矮屏向上翻转选表浮层（`.cross-table-popover.is-flip-up`）受父级层叠上下文限制被 `.mac-subbar` 遮挡拦截、`.cross-pair-picker` 按 `Escape`/点击外部关闭时因 `pointerdown` 早于 `blur` 丢失当前表对回显标题、原生 `search` 重复清除叉号、单侧视图血缘（`视图穿透:`）导致左右配置卡基线错位及 Spotlight 上下文切表未触发二次拉取等边界缺陷。
  - 将中央三列表头移入与 `.cross-matrix-row` 共享同一滚动容器与 `--cross-matrix-cols: minmax(0, ...)` 弹性轨道变量的吸顶层（移除多余 `scrollbar-gutter: stable` 空白槽并统一三栏表头检索区 `40px` 行高），彻底消除悬浮/经典 `16px` 滚动条偏移与中等分辨率下右侧徽标裁切问题，新增第 51 项自动化回归测试 `test_cross_comparator_macos_layout_anti_overlap_and_filter_integrity`。

---

## [2.6.0] - 2026-11-19

### Added
- **离线非对称激活码登录拦截与 C 原生防反编译护盾 (`core/platform/license_hub.py`, `core/platform/_license_native_src/`)**：
  - 全屏 macOS 毛玻璃登录拦截页（`static/js/ui/auth_gate.js` `#os-login-gate`）：支持「激活码模式」（默认开启）与「账号密码登录」（标注离线试用期且不联网暂未开放）；输入框默认留空，展示自动识别的本机 IP、本机机器码及一键「复制设备申请码 (`REQ1-...`)」按钮与管理员联系方式（莫政辉 / `131-6111-4561` / `artxom@outlook.com`）。
  - 确定性唯一激活码（`DOG1-...`）：基于 RFC 8032 Ed25519 非对称数字签名，对 `(版本, 授权范围, 天数, 截止日期, 机器码摘要, 本机物理 IPv4 摘要)` 签发严格唯一的离线激活码；同时支持 **设备专属激活码**（严格绑定机器码 + IP，跨机拷贝状态库或换 IP 立即失效）与 **全局通用激活码**（无视一切机器与 IP 限制、仅受有效期与防回拨约束）。
  - 激活成功通过原生 `<dialog>` 明确提醒 `当前激活码可用日期至 YYYY-MM-DD`，有效期内再次进入直接打开桌面；顶部 28px 菜单栏常驻展示 `授权至 YYYY-MM-DD` 状态胶囊，支持在线更换/续期或一键锁定退回登录页。
  - 新增 `GET /api/platform/auth/status`、`POST /api/platform/auth/activate` 与 `POST /api/platform/auth/lock` 认证接口，以及 **Gate 1 (ASGI 全量 `/api/*` 中间件) + Gate 2 (`get_platform_context` 动态 HMAC 请求能力令牌)** 双重强制拦截门禁。
- **零环境依赖独立激活码生成器构建器 (`tools/keygen_src/keygen.c`, `tools/build_keygen.py`)**：
  - 使用纯标准 C 编译产出独立零依赖原生可执行程序 `keygen/DataOG_Keygen`（或 Windows `DataOG_Keygen.exe`），支持交互式向导与 `--info` / `--local` / `--request REQ1-...` / `--machine --ip` / `--global` / `--days` / `--until` 全参数模式，自动探测本机 IP、机器码与申请码并支持多档或自定义时效选择。
- **全量自动化测试扩展至 50 项 (`tests/test_license_auth.py`)**：新增 6 项认证安全测试，全面验证 RFC 8032 测试向量、C 原生护盾 64 位挑战-应答防补丁一致性、Gate 1/Gate 2 双层 401 拦截、多网卡匹配、防拷贝/防篡改、防时钟回拨水印、手动锁定及独立 C 生成器编译与 CLI 全链路闭环。

### Changed
- **升级至 `Data.O.G v2.6.0`**：同步升级 `app.py` 与 `PlatformContext` 版本号至 `2.6.0`，`build_exe.py` 打包阶段自动编译并随客户端嵌入 `_license_shield` 原生动态库，同时排除 `tools` 等非运行期构建工具。

### Security
- **四重防反编译与私钥隔离机制**：客户端仅嵌有 4 组异或+置换拆分的 Ed25519 公钥，彻底不含私钥；核心验签与硬件探测运行于编译态 C 原生机器码动态库并通过 64 位随机挑战-应答 MAC 校验防函数返回值篡改；双位置硬件密封单调时钟水印（`app_meta['license_clock_seal']` + `data/.sys_clock_anchor`）拦截离线回拨系统时间；`.gitignore` 严格隔离 `.keygen_secrets/`、`keygen/` 及原生动态库，确保私钥绝不进入仓库或客户发行包。

---

## [2.5.2] - 2026-11-19

### Added
- **跨库字段长度/精度包络与类型转换双维诊断引擎**：
  - 在平台方言层（`core/platform/dialects/types.py`）新增 `parse_sql_type_spec` 与 `analyze_cross_column_compatibility`，并在前端新增零依赖实时镜像 `static/js/core/type_compat.js` (`analyzeCrossColumnCompat`)，覆盖字符宽度（含无界 `STRING`/`TEXT` 转有界 `VARCHAR`、UTF-8 跨库边界）、定点数精度与小数位（`DECIMAL(p, s)` 独立校验整数位 `p-s` 与小数位 `s`）、整数位宽（自动忽略 MySQL `INT(11)` 显示宽度，识别 `BIGINT`/`INT`/`SMALLINT`/`TINYINT` 真实字节位宽）、浮点数与时间小数秒精度，输出 `capacity_status`（`CONTAINED` / `INSUFFICIENT` / `RISK` / `N/A`）与 `type_conv_status`（`NONE` / `SAFE_CAST` / `CAST_REQUIRED` / `N/A`，例如 `VARCHAR(20) → DATE` 标记为需显式类型转换）。
  - 跨环境比对全链路（`modules/cross_comparator/service.py` `/overview`、`/compare-pair`、`bookmark_summary`、五虚拟列 `CrossComparatorView` 拖拽/配对实时重算与目标字段胶囊徽标）全面透传与实时渲染包络及类型转换诊断标签。
  - 升级跨环境单表对照 Excel 导出（`/bindings/export.xlsx`）、库级单 Sheet Excel 报告（`/report.xlsx`）、离线交互 HTML 报告（`/report.html`）及其内嵌纯 JS `.xlsx` 导出器：扩展为 24 列体系，新增「目标长度/精度包络」「是否涉及类型转换」两列，并在离线 HTML 报告中新增 KPI 汇总、字段差异摘要计数及「存在包络不足/风险 (`HAS_CAP_ISSUE`)」「涉及类型转换 (`HAS_TYPE_CONV`)」快捷筛选与胶囊。
- **「3. 跨环境比对」左上角表名与字段级可搜索映射表对选择器 (`.cross-pair-picker`)**：
  - 优化 `PlatformStateStore.list_cross_tables()` SQL，通过 `GROUP_CONCAT(DISTINCT ...)` 单次聚合来源/目标/标准字段与注释文本，并在 `modules/cross_comparator/router.py` 新增 `GET /api/modules/cross_comparator/pairs/search` 接口（联合检索 `cross_table_bindings`、`cross_column_bindings`、`meta_columns` 与 `field_standard_bindings`）。
  - 在 `CrossComparatorView` `.mac-subbar` 左上角落地自定义可搜索选择器 `.cross-pair-picker`，支持本地 0ms 即时过滤 + 120ms 防抖后端精确/字段检索、键盘上下切换及高亮匹配字段上下文，彻底不丢失焦点与中文输入法合成状态。
- **自动化测试覆盖增强**：新增 `test_cross_column_capacity_and_type_conversion_matrix` 与 `test_cross_compat_pair_search_and_report_columns`（含离线 HTML 内嵌 JS `.xlsx` 24 列 Node.js 往返验证），全量自动化测试扩展至 44 项。

### Changed
- **正式发布 `Data.O.G v2.5.2`**：同步升级 `app.py` 与 `PlatformContext` 版本号至 `2.5.2`，并触发 GitHub Actions 自动构建发布 `DataOG-Windows-x64-v2.5.2.zip`（内含 `DataOG.exe`）。

---

## [2.5.1] - 2026-11-18

### Changed
- **更换 Windows 独立可执行程序 (`DataOG.exe`) 品牌图标 (`static/dataog.ico`, `build_exe.py`)**：
  - 新增包含 `256/128/64/48/32/24/16` 七档标准尺寸的多分辨率图标资源 `static/dataog.ico`（`256px` 高清 PNG 帧 + `16~128px` 32-bit BGRA DIB 与 1-bit AND 掩码帧），并在 `build_exe.py` 中接入 PyInstaller `--icon` 参数写入 `DataOG.exe` PE 图标资源（Windows 构建免额外第三方图像库依赖）。
  - 升级 `app.py` 与 `PlatformContext` 版本号至 `2.5.1`，并在自动化测试中补充 `.ico` 魔数头与打包脚本 `--icon` 契约校验。

### Fixed
- **修复 Windows 打包前单测 `node -e` 命令行长度超限导致 Release 构建中断 (`tests/test_modules_and_api.py`)**：将离线报告内嵌 JS 与 `.xlsx` 往返校验探针改为写入临时 `.cjs` UTF-8 脚本文件执行，规避 Windows `CreateProcessW` `32,767` 字符命令行硬上限（`WinError 206`）。

---

## [2.5.0] - 2026-11-18

### Added
- **库级离线交互网页报告 (`.html`) 与单 Sheet 库级 Excel (`.xlsx`) 导出引擎 (`core/platform/offline_report.py`, `static/js/ui/export_report_modal.js`, `static/css/workbench.css`, `static/js/core/api.js`)**：
  - 平台底座新增 `core/platform/offline_report.py`，统一生成单 Sheet 库级 Excel（`build_single_db_excel_bytes` / `build_cross_env_excel_bytes`，单 Sheet 汇总全库表及字段明细、首行冻结与自动筛选、公式注入转义与 `-` 占位符豁免）以及零外部网络依赖的离线交互网页（`build_single_db_html_bytes` / `build_cross_env_html_bytes`，安全转义嵌入 `<script type="application/json">`、内置纯 JS 单 Sheet OOXML `.xlsx` 构建器 `buildSingleSheetXlsxBlob` 支持网页内一键导出全库或当前筛选明细、明暗自适应与安全 DOM 渲染）。
  - 公共 UI 新增通用导出弹窗 `ExportReportModal`（`openExportReportModal`），默认推荐离线交互网页报告 (`.html`) 并并列提供单 Sheet 库级 Excel (`.xlsx`) 与可重导入铺底模板/配置包入口；`downloadApiFile` 同步支持 `X-Task-Id` 与 `AbortSignal` 接入 `TaskActivityBar` 实时进度与取消控制。
- **单库落标与贯标库级报告与全库模板 (`modules/standard_migrator/`, `static/js/views/standard_migrator_view.js`)**：
  - 新增 `GET /api/modules/standard_migrator/bindings/report.html` 并升级 `GET /api/modules/standard_migrator/bindings/report.xlsx`：无需逐表勾选即按库级全量范围聚合全库表清单、绑标/落标状态、贯标改造队列与 SQL 折叠查阅（严格按 `migrator_connection_id` 隔离队列）；若当前数据源目录缓存为空，导出前先自动触发 `refresh_catalog` 采集全库目录后再生成报告；`GET /bindings/template.xlsx` 同步支持 `scope=all` 全库预填对照模板导出。
- **跨环境库级比对离线报告与推荐同名未保存表 (`modules/cross_comparator/`, `static/js/views/cross_comparator_view.js`)**：
  - 新增 `GET /api/modules/cross_comparator/report.html` 与 `GET /api/modules/cross_comparator/report.xlsx`：自动沿用来源库与目标库按库设定的落标标准格式，空缓存时先并发采集两侧目录（同库自比对仅刷新一次），导出全部已保存映射表对的五虚拟列看板与字段明细，同时附上未配对同名表推荐清单（`_compute_recommended_pairings` 区分同模式/跨模式同名，严格仅推荐不自动触发字段预比对）及单侧独有表清单。
- **全平台实时任务进度条与间断缩略微日志底座 (`core/platform/import_progress.py`, `core/platform/router.py`, `static/js/ui/task_activity_bar.js`, `static/css/workbench.css`)**：
  - 将 `ImportProgressHub` 升级为通用 `TaskProgressHub`（向下兼容 `/import-progress/*`），新增 `threading.Condition` 实时变更唤醒、单调 `seq` 增量追踪、凭据脱敏与 `<=64` 字截断的 `0.35s` 节流 `recent_logs`（最多保留 8 条）及 `cancel()`/`is_cancelled()` 协作取消支持；新增 `/api/platform/tasks/{task_id}`、`/api/platform/tasks/{task_id}/cancel` 与原生 `StreamingResponse` SSE 流 `/api/platform/tasks/{task_id}/stream`。
  - 新增 macOS 毛玻璃内联活动条公共组件 `TaskActivityBar`（`180ms` 防闪烁静默宽限期、`2px` 顶部渐变进度条、`120ms` 渐进计时与平滑爬升、`EventSource` 主通道 + `250ms` 轮询自动兜底、单行翻页缩略微日志及可折叠 `步骤 (N) ▾` 气泡），覆盖跨环境比对切表/预览/保存/推送、左树刷新/表展开、第 2 步单表核验/批量检查/贯标执行、SQL 执行/预览、AI 多步只读探查与多邻国智能命名/方言翻译，并全面支持 `AbortController` 抢占取消防竞态。
- **跨环境比对轻量切表、双库并发探查与后台预热 (`modules/cross_comparator/`, `static/js/views/cross_comparator_view.js`)**：
  - 新增 `GET /api/modules/cross_comparator/compare-pair` 轻量单表对接口（剥离全库目录树与全量表结构扫描，配合 `count_cross_columns()`）与 `POST /api/modules/cross_comparator/prefetch-pairs` 后台单会话批量预热接口；`compare_single_table` 支持源/目标双库 `ThreadPoolExecutor` 并发探查。

### Changed
- **元数据探查 60 秒温连接池、120 秒负缓存与单连接四层视图穿透 (`core/platform/connection_hub.py`, `core/platform/metadata_hub.py`)**：
  - 新增 `ConnectionHub.probe_session`（非 SQLite 数据源空闲 `60s` 温连接复用、退出自动 `rollback()`、异常自动剔除并销毁、`save_profile`/`delete_profile` 失效清理，严格隔离 `readonly_session` 零复用；SQLite 默认直连免锁文件句柄）。
  - `MetadataHub` 新增 `120s` 不存在表内存负缓存（强制刷新时立即失效）与 `_shared_conn` 单连接四层递归视图血缘穿透复用，消除连续切表与多层视图的重复握手耗时。
- 升级 `app.py` 与 `PlatformContext` 版本号至 `2.5.0`，在 `app.py` 访问日志过滤器中同步静默 `/api/platform/tasks/*` 正常 `200` 轮询，并扩充单库/跨环境库级离线报告与 Node.js 内嵌 `.xlsx` 构建端到端测试至 **42 项**。

### Fixed
- **修复 `ExportReportModal` 弹窗主题令牌缺失与离线报告页签筛选导出错位 (`static/css/workbench.css`, `static/js/ui/export_report_modal.js`, `core/platform/offline_report.py`, `modules/standard_migrator/`)**：
  - 修正 `.export-report-*` 样式中未定义的 `--os-panel*` / `--os-text-2` 变量为 `tokens.css` 标准令牌（`--os-win-bg` / `--os-subtle-bg` / `--os-text-secondary`），并将 `badgeTone` 由不存在的 `"ok"` 修正为 `.badge-success`。
  - 修复离线 HTML 报告中 `hasActiveFilter()` / `exportFilteredExcel()` 未按当前活动页签隔离导致在贯标队列或告警页签误显/误导上一页签筛选明细的问题，并优化仅存在单侧独有表时的跨环境默认落点页签。
  - 为 `GET /api/modules/standard_migrator/bindings/template.xlsx`（`scope=all`）补齐 `X-Task-Id` 异步任务进度追踪与 `public` 绑定回退匹配。

---

## [2.4.0] - 2026-11-17

### Added
- **按数据库粒度设定与持久化落标标准格式 (`core/platform/`, `modules/standard_migrator/`, `modules/cross_comparator/`, `static/js/views/standard_migrator_view.js`, `static/js/ui/database_tree.js`)**：
  - 数据源档案 `connection_profiles` 新增幂等迁移列 `standard_format`，支持通过 `PUT /api/platform/connections/{connection_id}/standard-format` 持久化保存各库落标标准（覆盖 `AUTO`、`仓库DWS数据格式`、`数据仓库 集市`、`argodb数据格式` 等，带白名单校验与密码严格脱敏）。
  - 「2. 落标绑定与贯标」左侧边栏支持为每个已配置数据库独立设定落标标准并展示徽标，切换数据库时自动应用该库设定，并在「3. 跨环境比对」选择对应来源库或目标库时自动沿用。

### Changed
- **「3. 跨环境比对」macOS 五虚拟列图形化工作台重构 (`static/js/views/cross_comparator_view.js`, `static/css/workbench.css`, `modules/cross_comparator/service.py`)**：
  - 将字段配对工作区重构为从左到右五虚拟列：`来源表待选字段` | `来源表已绑定数标字段` | `数据标准` | `目标表已绑定数标字段` | `目标表待选字段`，精简大段冗余文案并采用 macOS 桌面级图形卡片交互。
  - 第 2 步已绑定标准字段自动归入两侧「已绑定数标字段」列并按中栏对齐，未绑标字段保留在两侧「待选字段」列；支持拖拽或点选待选字段至中栏标准卡片或新标准落点完成绑标配对，中栏清晰呈现一致合并单卡（`UNIFIED`）、异标双胶囊对比（`SPLIT_DIFF`）与单侧绑定（`SPLIT_PARTIAL`）。
- 升级 `app.py` 与 `PlatformContext` 版本号至 `2.4.0`，补充按库标准格式持久化及五虚拟列/防遮蔽聚焦单测至 **38 项**。

### Fixed
- **彻底解决顶部「已保存映射表对」遮蔽中间选择框问题 (`static/js/views/cross_comparator_view.js`, `static/css/workbench.css`)**：将 `已保存映射表对`（`cross-saved-pair-select`）从窗口顶栏右区迁移至页面内部 `.mac-subbar` 工具栏，并为 `.win-titlebar-center`（`z-index: 2; min-width: 0`）与 `.win-titlebar-right`（`overflow: hidden; min-width: 0; z-index: 1`）补充溢出保护与层级隔离。
- **修复跨 Schema 同名表冲突与单侧解绑回显边界缺陷 (`modules/cross_comparator/service.py`, `static/js/views/cross_comparator_view.js`)**：隔离 Schema 限定与带点表键解析，消除目标表重名幽灵列泄露，并保证第 2 步解绑后与第 3 步预览/保存状态同步一致。

---

## [2.3.3] - 2026-11-17

### Fixed
- **彻底消除数千行字段对照关系导入在「正在持久化字段标准绑定」与批量核验阶段的卡死风暴 (`core/platform/standard_catalog.py`, `core/platform/metadata_hub.py`, `core/platform/storage.py`, `modules/standard_migrator/service.py`, `modules/cross_comparator/service.py`)**：
  - 新增 `bulk_save_field_bindings` 单事务分块（`500` 行/批）`executemany` 持久化与 `make_table_key_bounds` B-Tree 半开区间精确删除（防 `LIKE '_'` 通配符误伤，辅以 `COLLATE NOCASE` 大小写无关索引清理），彻底消除 `replace_table` / `merge` 多表逐次开闭 SQLite 连接与磁盘 `commit` 风暴，实时推进持久化进度（`60% -> 70%`）并在事务落盘后即刻切换至核验阶段；跨环境批量铺底（`upload_pairings_excel`）同步升级为单事务分块写入。
  - 新增 `MetadataHub.get_tables_metadata_batch` 单会话多表批量探查与单事务批量缓存落库（支持 PG/DWS 与 MySQL 家族系统目录分块批量查询、显式非默认 Schema 防跨库误匹配、非 `autocommit` 事务异常自动回滚重配及 `match_standard_candidates_batch` 单连接 `COLLATE NOCASE` 索引候选匹配），消除多表核验与无编号候选匹配时的 $O(N)$ 数据库连接风暴。
  - 为 PG / GaussDB(DWS) 元数据探查注入会话级 `lock_timeout` / `lockwait_timeout`（`3s`）与 `statement_timeout`（`8s`）保护、`pg_get_expr` 锁等待 MVCC 无锁列查询自动降级、`pg_get_tabledef` `15`-OID 子分批单调进度上报与慢超时熔断，并在 JDBC / Inceptor 等单连接逐表回退路径（含 Inceptor 套接字超时透传）引入连续 `3` 次异常自动熔断跳过剩余未缓存表，防止远端 ETL 排他锁或断连导致百张表无限期阻塞。
  - 修复未填「模式名」工作簿在 DWS/MySQL/SQLite/Inceptor 下的默认 Schema 解析与真实 Schema 自动回写对齐（确保导入后左树徽标、点击预览及二次热缓存零重连一致生效），拦截无标准列的纯表结构工作簿在 `replace_table` 下误删既有绑定，修复跨环境铺底表首行为单侧目标字段时来源表名覆盖错位问题。
- **优化导入进度轮询日志降噪、禁缓存与终态停轮询 (`app.py`, `core/platform/import_progress.py`, `core/platform/router.py`, `static/js/core/api.js`, `static/js/ui/upload_wizard_modal.js`)**：过滤正常 `200` `/api/platform/import-progress/*` 控制台访问日志刷屏（保留非 `200` 异常日志），补充 `Cache-Control: no-store` 防缓存头与运行态导入任务延长 TTL 保护，并在前端检测到服务端终态（`completed`/`failed`）时立即停止轮询且保留错误态样式。

---

## [2.3.2] - 2026-11-16

### Added
- **纯 Python 公式求值器扩展支持经典业务查找范式 (`core/excel_formula.py`)**：新增支持数组字面量 `{...}`、整行引用（`2:2` / `$2:$2`）、`LOOKUP`（含 `LOOKUP(1, 0/(...), ...)` 多条件逆向查找）、`CHOOSE`（含 `VLOOKUP(..., CHOOSE({1,2}, ...), 2, 0)` 数组拼列查找）、`INDIRECT` 与 `OFFSET`。

### Fixed
- **修复共享公式平移误改结构化表引用列名**：`_shift_shared_formula` 在拖拽平移坐标时完整跳过 `[...]` 平衡方括号，防止形如 `[@ID1]`、`Table1[[#This Row],[C2]]` 的列名被误识别为单元格坐标而偏移。
- **修复流式导出 `.xlsx` 固定 `<dimension ref="A1:A1"/>` 导致读取进度首行跳顶冻结**：在工作簿包扫描阶段从工作表 XML 尾部毫秒级提取真实最大行号，保障 EasyExcel / POI `SXSSFWorkbook` / `write_only` 工作簿行级进度平滑单调递增。
- **修复文本态公式双工作簿句柄泄漏与异常公式 `IFERROR` 捕获失效**：`ResolvedWorkbook.close()` 确保同时关闭主工作簿与懒加载公式求值工作簿；未缓存异常公式求值失败时返回 `#VALUE!` 以支持外层 `IFERROR` 兜底（顶层读取仍自动置空）。
- **消除跨环境多表铺底上传 $O(N)$ 冗余数据库探查 (`modules/cross_comparator/service.py`)**：`upload_pairings_excel` 批量保存循环内跳过逐表 `get_overview_state()` 重算，仅在末尾汇总拉取一次当前激活表概览（离线数据源场景下消除 `2*N` 次超时等待）。
- **修复前端 `<dialog>` 重入异步 `close` 事件与重试轮询竞态 (`static/js/ui/modal.js` & `static/js/ui/upload_wizard_modal.js`)**：`Modal.open` 引入会话代数守卫与原地顶层复用，防止已打开弹窗切换时异步 `close` 事件误关闭新弹窗状态；`UploadWizardModal` 引入 `attemptSeq` 轮次隔离与 `kickPollTimer` 清理，消除快速点击“重新导入”时上一轮失败状态残留覆盖当前进度的竞态。

---

## [2.3.1] - 2026-11-16

### Added
- **Excel 导入双阶段实时进度条与服务端进度追踪中枢 (`core/platform/import_progress.py` & `static/js/ui/upload_wizard_modal.js`)**：
  - 平台底座新增线程安全 `ImportProgressHub` 与轮询接口 `GET /api/platform/import-progress/{progress_id}`，将标准字典、表字段对照关系、单库贯标清单、跨环境映射铺底与标准包上传全部切换至线程池非阻塞执行（释放 FastAPI 主事件循环），同时覆盖大工作簿与小批量（`<400` 行）首行及中间阶段进度上报。
  - 前端 `UploadWizardModal`、`Modal` 与 `uploadFormData` 升级为 `XMLHttpRequest` 字节流上传进度 + 服务端实时行级/表级解析核验进度条、动态计时面板与导入期间弹窗防误关锁定（`closedby="none"` / `Escape` 与背景点击拦截）。
- **纯 Python Excel 公式最终值求值兜底引擎与全谱系错误值置空清洗 (`core/excel_formula.py`)**：
  - 优先采用 Excel/WPS 缓存的公式最终计算结果（`data_only=True`，含显式空串 `<c t="str"><v/></c>` 识别与外部工作簿引用防劫持）；针对缺失 `<v>` 缓存、拖拽共享公式或文本态公式工作簿采用只读流式值加载 + 稀疏 XML 公式提取自动求值（支持跨 Sheet 与整列引用、名称管理器 `<definedNames>`、超级表结构化引用 `Table[列]`/`[@列]`、多条件数组匹配 `(A:A=x)*(B:B=y)`、`VLOOKUP`、`HLOOKUP`、`XLOOKUP`、`INDEX`+`MATCH`、`IF`/`IFS`/`IFERROR`/`IFNA`、字符串拼接与文本函数，并对查找列构建哈希索引）。
  - 全系统数据标准字典、字段对照关系、单库贯标清单与跨环境比对统一将 `#N/A`、`#VALUE!`、`#REF!`、`#DIV/0!`、`#NUM!`、`#NAME?`、`#NULL!`、`#SPILL!`、`#CALC!` 等公式错误标记识别为空值，并支持历史残留 `#N/A` 脏绑定自动自愈清理。

### Changed
- 升级 `app.py` 与 `PlatformContext` 版本号至 `2.3.1`，扩充全量自动化单元测试与端到端闭环测试至 **34 项**。

### Fixed
- 修复多工作表（如同时携带数万行「数据字典」参考 Sheet）上传对照关系时全表非只读加载与离线数据源逐表超时导致的长时间“正在解析导入”卡顿问题。
- 兼容第三方工具生成的 `.xlsx` 在 `.rels` 中写入无前导 `/` 的包根路径（如 `Target="xl/worksheets/..."`、`Target="xl/tables/..."`）或反斜杠路径，防止拼接为 `xl/xl/...` 导致工作表解析丢失。
- 修复 `ReadOnlyWorksheet.reset_dimensions()` 清空 `<dimension>` 最大行元数据导致先探查表头后全量读取时总行数估计退化的问题。

---

## [2.3.0] - 2026-11-16

### Added
- **平台统一数据标准字典中枢 (`core/platform/standard_catalog.py` — `/api/platform/standards*`)**：
  - 以 `数据项编号` (`std_code`) 为唯一主键维护全行一套最新数据标准（SQLite 表 `standard_dictionary` 与字段绑定表 `field_standard_bindings`），完整兼容 46 列多库物理格式（含 `仓库DWS数据格式`、`数据仓库 集市`、`ArgoDB`、`MySQL`、`Oracle`、`OceanBase`、`达梦` 等，并精准处理同名 `仓库DWS数据格式` 列右侧主格式优先解析）。
  - 支持流式只读高速解析大型标准字典工作簿、按 `数据项编号` 增量合并 Upsert（保留既有库表字段绑定）或全量覆盖导入、在线增删改查（修改编号自动级联更新所有绑定）、多维分页搜索以及 46 列脱敏系统模板下载与完整导出。
- **第 2 步「落标绑定与贯标」全链路升级 (`modules/standard_migrator/` & `standard_migrator_view.js`)**：
  - 采用左树（`DatabaseTree` 实时显示各表已绑标/已达标徽标）+ 右工作台，按数据源连接持久化字段标准绑定（自动共享给第 3 步跨环境比对）。
  - 新增 `GET/POST /bindings/*` 接口：支持一键导出带当前已选表真实物理字段的待填对照模板、批量上传「表字段↔数据标准」对照表、智能预匹配、三维分级“检查是否落标”（字段名 + 归一化物理类型 + 中文注释），并支持勾选未达标字段一键生成贯标改造任务衔接原地 `ALTER` 或 DWS 物理列保序重建。
- **第 3 步「跨环境比对」双列拟物书签工作台 (`modules/cross_comparator/` & `cross_comparator_view.js`)**：
  - 重构为「来源库 | 目标库」双列对照布局 + 内侧中缝拟物书签桥 (`dual_rows`)：自动继承第 2 步在来源库与目标库维护的字段数标；左右已配对且数标编号一致时合拢为不惹眼的**淡绿色贯通拟物书签**（目标物理未达标时附“待贯标”角标），不一致或单侧未绑时在中缝断开为左右燕尾独立书签。
  - 支持直接拖拽字段卡片调整/交换/配对与虚线空槽承载、点击书签实时选绑或更换数据标准、一键拆解配对、保存后双向同步回第 2 步数据源绑定（`/pairings/save`），以及导出当前两表预填映射模板与批量铺底上传（`/pairings/template.xlsx`、`/pairings/upload`）。
- **全系统两步式模板导入向导 (`static/js/ui/upload_wizard_modal.js`)**：
  - 统一全平台所有 Excel 上传交互为「第 1 步下载空白/预填系统模板（附带 `填写说明` Sheet）→ 第 2 步拖放/选择文件与策略导入」。
- **原生 macOS 底部居中悬浮毛玻璃 Dock 栏 (`static/index.html` & `static/js/main.js`)**：
  - 桌面中央保持纯净壁纸留白，底部居中悬浮毛玻璃 Dock 承载四大模块（悬停平滑放大、邻近抬起、名称气泡、运行圆点），最小化窗口收纳于 Dock 竖直分隔线右侧缩略区，全屏最大化时 Dock 自动下沉隐藏、鼠标贴底即可丝滑唤出。

### Changed
- `core/platform/dialects/types.py` 与 `core/dws_engine.py` 补充 `NVARCHAR` 至 `varchar` 归一化；`core/excel_handler.py` 将 Excel 安全上传上限提升至 `50MB` 并在解析清单时自动跳过 `填写说明` 等辅助工作表。
- 升级 `app.py` 至 `2.3.0`，扩充自动化单测与端到端闭环测试至 **32 项**。

### Security
- 将本地参考样本 `Book1.xlsx` 加入 `.gitignore` 严格隔离防止敏感字典数据提交，系统内置模板均采用脱敏通用金融示例数据，全量前端新增视图与组件保持零 `innerHTML` 与零阻塞弹窗。

---

## [2.2.0] - 2026-11-12

### Added
- **AI 只读智能数据探查模块 (`modules/data_explorer/` — `/api/modules/data_explorer`)**：
  - 新增目标驱动的多轮大模型自动数据探查引擎（`POST /explore`），零新增第三方依赖直接复用 `LLMHub`（DeepSeek / Google Gemini / OpenAI 兼容网关）与 `MetadataHub` 库表字段上下文，支持数据分布画像、空值/重复/异常脏数据诊断、主外键与跨表关联推断、业务指标核对。
  - 新增探查会话与采样轨迹持久化管理接口（`GET /sessions`、`POST /sessions`、`GET /sessions/{session_id}`、`PATCH /sessions/{session_id}`、`DELETE /sessions/{session_id}`），通过 `PlatformStateStore.register_table_schema` 幂等注册 `explorer_sessions` 与 `explorer_messages` 表。
- **平台底座只读 SQL 沙箱与跨驱动硬件只读通道 (`core/platform/`)**：
  - 新增 `core/platform/readonly_guard.py`（`validate_readonly_sql`）与平台只读执行接口 `POST /api/platform/query/execute-readonly`。
  - 新增 `ConnectionHub.readonly_session` 与 `QueryEngine.execute_readonly_sql`，统一施加单步采样行数上限（默认 `100` 行，硬上限 `200` 行）与短超时熔断（默认 `12s`）。
- **macOS 风格「数据探查」双模工作台 (`static/js/ui/database_tree.js` & `static/js/views/`)**：
  - 顶栏新增 `SQL 查询` | `AI 探查` 分段切换，完整保留目录树浏览、双击表预览、自由 SQL 执行与 CSV/Excel 导出。
  - 左侧继续沿用 DataGrip 风格四级已连接数据库目录树（`连接` → `Schema` → `表/视图` → `字段`），在 `AI 探查` 模式下支持直接在树节点内联勾选焦点表（含 Schema 级全选与 `indeterminate` 半选态）。
  - 新增 `explorer_ai_pane.js`，采用原生 `<details>` 折叠面板透明展示每一步执行的采样 SQL、耗时、行集预览与结构化结论卡片，支持一键将探查 SQL 填入主编辑器复核导出，并兼容中文输入法（IME）安全回车发送。

### Changed
- 升级微内核与应用清单版本至 `2.2.0`，扩充全量自动化单元与红队集成测试至 **30 项**。

### Security
- **四道引擎级绝对只读硬隔离防线（绝不依赖 Prompt 软约束）**：
  1. **双模态词法/语法白名单（`ReadOnlySqlGuard`）**：同时在标准 SQL 与反斜杠转义双模式下确定性分词以消除转义歧义偷渡，阻断 MySQL 条件可执行注释 `/*! ... */` 与未闭合注释/Dollar-Quoting，强制单语句（禁 `;` 堆叠），顶层仅放行 `SELECT`、`WITH ... SELECT`（递归校验全部 CTE 子句严禁数据修改型 CTE）、元数据 `SHOW`、`DESCRIBE`/`DESC` 与非 `ANALYZE` `EXPLAIN`，全语句拦截一切写/DDL/DCL/TCL/锁/`INTO` 关键字及跨方言危险控制/文件函数（含双引号标识符包裹调用）。
  2. **数据库驱动会话级硬件只读锁**：PostgreSQL/DWS 强制 `set_session(readonly=True, autocommit=False)` + `SET TRANSACTION READ ONLY`；MySQL 系强制 `SET SESSION TRANSACTION READ ONLY` + `START TRANSACTION READ ONLY`；SQLite 强制 URI `?mode=ro` + `PRAGMA query_only = ON`；JDBC 强制 `setAutoCommit(False)` + `setReadOnly(True)` 并禁用 `commit()`。
  3. **零提交与退出无条件回滚**：只读执行路径零 `commit()` 调用，`readonly_session` 在 `finally:` 中无条件调用 `rollback()`。
  4. **输出二次清洗与步数熔断**：单轮自动探查硬上限 `8` 步熔断，并对 AI 返回的 `recommended_sql` 再次执行只读白名单校验，防止恶意写语句被诱导填入主编辑器。

---

## [2.1.1] - 2026-11-12

### Changed
- **全工程代码与文件深度瘦身**：
  - 清理移除已被 v2.x 模块化架构完全替代的历史冗余代码与一次性脚本：`core/cross_env_engine.py`（169 KB 历史副本）、`core/state_store.py`、`pytest.bat`、`run_tests.bat`、`static/js/ui/split_pane.js`、`static/styles.css`、`示例_DWS字段贯标清单.xlsx`，并精简 `app.py` 中 450+ 行未使用的 v1.0 遗留路由。
  - `static/index.html` 直接加载 `/css/tokens.css` 与 `/css/workbench.css`，消除 `@import` 二次串行请求。
- **核心依赖精简与零 SDK 大模型通信 (`requirements.txt` & `core/platform/llm_hub.py`)**：
  - `LLMHub` 改用 Python 标准库（`urllib.request` / `ssl` / `json` + 安全重定向协议校验）直连 DeepSeek、Google Gemini 及 OpenAI 兼容端点；`tests/test_modules_and_api.py` 改用轻量标准库内存 ASGI 客户端。
  - `requirements.txt` 从 15 项精简为 **8 项纯运行时核心依赖**，彻底移除 `openai`（含 `jiter` / `tqdm` / `distro`）、`httpx`（含 `httpcore` / `certifi`）、`pytest`，并将 `pyinstaller` 隔离至打包构建阶段按需安装。
- **Windows `DataOG.exe` 打包体积优化 (`build_exe.py`)**：
  - 消除 `static/` 目录在 `_internal/static` 与外层目录的重复拷贝；精细化收敛 `pydantic_core` / `psycopg2` 二进制收集规则，显式排除 `openai`、`httpx`、`lxml`、`cryptography`、`pytest`、`psycopg2.tests`、`uvicorn.protocols.websockets` 等非运行时模块，并在打包后自动剥离 `.pyi` / `.pxd` / `__pycache__` 冗余文件。

### Fixed
- 修复 `core/platform/dialects/view_lineage.py` 中上游视图投影列同时带有显式 `CAST(...)` 与泛化原类型（如 Hive/Inceptor `STRING`）时未优先升级为 `CAST` 推导精度类型的问题。
- 修复标准库内存 ASGI 测试客户端在 `BaseHTTPMiddleware` 流式文件响应期间过早返回 `http.disconnect` 导致响应体被提前取消的生命周期竞态。

---

## [2.1.0] - 2026-11-12

### Added
- **macOS 桌面窗口体系与四大独立应用模块 (`static/`)**：
  - 桌面主画布与顶部 28px 毛玻璃全局菜单栏（支持 `⌘K` Spotlight 库表字段秒级搜索、AI 状态指示、运行日志弹窗、自动/浅色/深色模式切换与实时时钟）。
  - 真实红黄绿窗口生命周期：🔴 **关闭 (`✕`)** 返回桌面并彻底重置当前工具界面状态；🟡 **最小化 (`−`)** 将当前工作缩略进屏幕右下角毛玻璃小抽屉（完整保留内存 DOM 与未保存编辑内容，点击抽屉卡片即刻恢复工作现场）；🟢 **最大化 (`+`)** 切换留白桌面窗口与全屏沉浸画布。
  - 桌面拆解为四大高内聚模块：**「系统配置」**（外观与图标 / 数据库连接 / JDBC 驱动 / AI 模型）、**「数据探查」**（目录树 + SQL 查询与导出）、**「数据标准」**（整合单库贯标与三栏跨环境比对）、**「多邻国」**（整合智能命名与跨方言翻译）。
- **本地桌面壁纸与软件/模块图标个性化中心 (`core/platform/asset_store.py`)**：
  - 支持在「系统配置 → 外观与图标」上传自定义桌面壁纸（支持 `铺满` / `适应` 切换）、自定义 `Data.O.G` 软件主图标（动态覆盖 `/favicon.ico`）及四大模块专属图标，本地持久化保存于 `data/assets/` 与 `data/dataog_state.db`，支持单槽位或一键恢复默认并自动清理旧文件。
- **多服务商 AI 大模型统一接入中枢 (`core/platform/llm_hub.py`)**：
  - 采用官方 `openai` SDK 兼容协议，内置 **DeepSeek**（`https://api.deepseek.com`）、**Google Gemini**（`https://generativelanguage.googleapis.com/v1beta/openai/`）与自定义 OpenAI 兼容网关预设，支持连通性毫秒级检测与远端模型列表拉取（自动兼容剥离 Gemini `models/` 前缀）。
- **「多邻国」AI 智能数仓命名与异构方言深度适配 (`modules/ddl_translator/`)**：
  - 新增 `POST /api/modules/ddl_translator/naming`：将中文表名/字段名按数仓分层前缀、词根缩写与命名风格（`snake_case` / `UPPER_SNAKE` / `camelCase`）转换为标准英文标识符与推荐 SQL 类型（支持大模型生成与内置 170+ 金融数仓词根引擎双驱动），一键生成建表 DDL 草稿。
  - 增强 `POST /api/modules/ddl_translator/translate`：新增直接粘贴 `CREATE TABLE` SQL 解析与 `ai_assist` 大模型跨方言语法/函数深度适配（`AST+AI` 融合模式）。

### Changed
- 正式升级产品官方品牌展示名为 **`Data.O.G`**，全面移除旧界面中冗长解释性副标题、提示段落与版本号徽标噪音，采用极简无框 macOS 视觉语言（`tokens.css` 升级 `color-scheme` 与 `light-dark()` 双色令牌）。
- 扩充全量自动化单测与集成测试至 **27 项**（新增 `AssetStore`、`LLMHub` 与 `Polyglot` 端到端测试），同步升级 `build_exe.py` 以收集 `openai` 依赖并初始化 `data/assets` 目录。

### Security
- 自定义壁纸与图标上传严格执行扩展名与光栅二进制魔数头双重白名单校验（仅允许 PNG / JPEG / WebP / GIF，彻底阻断 SVG / HTML 脚本注入），统一重命名为随机 UUID 隔离存储于非静态目录 `data/assets/`，读取接口强制附加 `X-Content-Type-Options: nosniff` 与 `Content-Security-Policy: default-src 'none'; sandbox` 响应头，并将 `data/assets/` 纳入 `.gitignore`。
- AI 大模型 API Key 严格加密脱敏回显（`mask_secret_key`），严禁在任何公开 API 或运行日志中泄露明文密钥，并拦截非 `http/https` 或内嵌凭据的异常 Base URL。

---

## [2.0.0] - 2026-11-11

### Added
- **FastAPI 微内核与四大全局共享底座 (`core/platform/`)**：
  - **双轨驱动引擎 (`DriverManager`) 与全局连接池 (`ConnectionHub`)**：内置免 Java 原生 Python 驱动通道（覆盖华为云 `DWS`、`PostgreSQL`、星环 `Inceptor`/Hive、`MySQL`/OceanBase/Doris/StarRocks/TiDB、`SQLite`），同时支持界面上传标准 JDBC `.jar` 驱动包（内置达梦 `DM8`、人大金仓 `KingbaseES`、`Oracle`、`DB2`、`GBase` 模板），按需懒加载 `JPype1` + 独立 `URLClassLoader`，无 Java 运行时绝不影响原生驱动启动。
  - **全局持久化元数据共享中枢 (`MetadataHub`)**：支持深度适配库高性能探查与任意自定义 JDBC 驱动 `java.sql.DatabaseMetaData` 通用回退探查，本地 SQLite 结构化缓存库表字段、主键、分布键、分区键与视图血缘，全模块零延迟共享与关键词秒级检索。
  - **通用简单查询引擎 (`QueryEngine`) 与方言血缘库 (`core/platform/dialects/`)**：提供跨驱动 SQL 执行与双击表节点快速预览（默认 `LIMIT 500`、最高 `5000` 行安全下推截断、耗时毫秒统计、CSV/Excel `.xlsx` 导出），抽离跨方言类型归一化矩阵与四层递归视图血缘穿透器。
- **插件化独立业务模块 (`modules/`)**：
  - `modules/standard_migrator`：单库批量落标贯标执行台（完整保留 `DIRECT_ALTER` / `TABLE_REBUILD` 物理列保序迁移、Kahn 拓扑改名排序、分区边界继承与多线程并发能力）。
  - `modules/cross_comparator`：16:9 跨环境三栏数标比对中心（来源库 × 数据标准 × 目标库 三方双向桥接绑定、Excel/JSON 导入导出、实时重算差异并一键推送待改造项至贯标台）。
  - `modules/ddl_translator`：提拔为独立一级模块「跨方言建表 DDL 翻译与四层视图穿透器」。
- **类 DataGrip / DBeaver 原生 ES Modules 统一 IDE 工作台 (`static/`)**：
  - 零 Node 构建链，支持浅色/深色/跟随系统主题切换，统一封装 `DatabaseTree`、`DataGrid`、`ObjectPicker`、`SqlCodeBox`、`Modal`（原生 `<dialog>`）、`Toast`、`SplitPane` 公共组件库，新增全局数据源驱动管理视图与轻量 SQL 查询浏览台。
- **自动化测试与 Windows `DataOG.exe` 持续交付流水线**：
  - 扩充至 24 项单元与集成红线测试（`tests/test_core.py`、`tests/test_platform.py`、`tests/test_modules_and_api.py`），升级 `build_exe.py` 与 `.github/workflows/ci.yml` 实现 Ubuntu + Windows 双平台自动测试、PyInstaller 独立打包、`DataOG.exe --test` 冻结态自检及 GitHub Release 自动发布。

### Changed
- 运行时状态库统一升级为 `data/dataog_state.db`（WAL 模式），启动时自动检测历史 `data/dws_migrator_state.db` 及旧版 `app_meta` 连接配置并无损迁移为全局命名数据源档案，支持模块表幂等列演进。

### Fixed
- 修复 Windows CI 控制台 `cp1252`/`gbk` 编码导致的中文输出 `UnicodeEncodeError` 及 PyInstaller 冻结态模块零互引字节码自检兼容问题。
- 修复多线程并发环境下 `JPype1` 跨线程调用 JDBC 缺少 `attachThreadToJVM()` 的潜在故障。
- 修复左侧数据库目录树单击重绘阻断双击表预览、单 Schema 无法折叠、贯标搜索框重绘失焦及跨环境新建手动字段桥接保存丢失等问题。

### Security
- 严格绑定 `127.0.0.1` 本地回环地址，注入 CSP 与 `Sec-Fetch-Site` 跨站请求拦截响应头，全接口密码脱敏返回。
- JDBC `.jar` 上传严格限制 64MB 体积、校验 ZIP/JAR 魔数头、拦截 POSIX 与反斜杠 Zip-Slip 路径穿越，并在加载前后双重校验 SHA-256 指纹。
- 前端全量采用 `createElement` / `textContent` 安全渲染 DOM（零 `innerHTML`），通过自动化静态扫描测试强制守卫。

---

## [1.0.0] - 2026-11-11

### Added
- 初始里程碑基线：完成华为云 DWS 单库物理列保序批量贯标引擎、16:9 跨环境（Inceptor/PG/DWS）三栏数标比对工作台、跨方言建表 DDL 翻译及上游四层视图血缘递归穿透解析核心能力。
