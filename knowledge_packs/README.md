# Data.O.G 离线初始化参考知识库下载中心

> 适用版本：**Data.O.G v3.0.0+** | **零程序体积膨胀**（知识库独立存放于仓库 `knowledge_packs/`，不打包进 `DataOG.exe`，按需自助下载）

本目录为 **华为云 GaussDB(DWS) 9.1 / 8.3**、**星环 Inceptor 9.3 / 9.4（TDH 9.x & ArgoDB）** 与 **官方 PostgreSQL 19 / 18 / 17** 整理了离线初始化参考知识库。所有材料均已按 Data.O.G `KnowledgeHub` 的 Markdown 章节分块、BM25 / SQLite FTS5 中英与错误码分词规则完成优化，并提供 **一键导入知识包（`.json.gz`）** 与 **单篇 `.md` 源码** 两种获取方式。

---

## 一、快速下载（推荐下载 `.json.gz` 离线知识包）

点击下表中的 **「直接下载（Raw）」** 即可保存 `.json.gz` 文件（单库仅 `6.5 ~ 8.2 KB`，全量合并包仅 `19.4 KB`）；如需先在线浏览内容，可点击「源码目录」。

| 目标数据库与适用版本 | 知识包文件 (`packs/`) | 文档数 / 分块数 | 压缩体积 | 下载与在线查看超链接 |
| :--- | :--- | :--- | :--- | :--- |
| **华为云 GaussDB(DWS)**<br>`9.1 / 8.3` | `DataOG_KB_DWS_9.1.json.gz` | 3 篇 / 24 块 | `~8.2 KB` | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_DWS_9.1.json.gz) · [详情页](https://github.com/artxom/DataOG/blob/main/knowledge_packs/packs/DataOG_KB_DWS_9.1.json.gz) · [MD 源码](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/dws) |
| **星环 Inceptor / ArgoDB**<br>`9.3 / 9.4 (TDH 9.x)` | `DataOG_KB_INCEPTOR_9.x.json.gz` | 3 篇 / 20 块 | `~6.5 KB` | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_INCEPTOR_9.x.json.gz) · [详情页](https://github.com/artxom/DataOG/blob/main/knowledge_packs/packs/DataOG_KB_INCEPTOR_9.x.json.gz) · [MD 源码](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/inceptor) |
| **官方 PostgreSQL**<br>`19 / 18 / 17` | `DataOG_KB_PGSQL_19.json.gz` | 3 篇 / 15 块 | `~7.1 KB` | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_PGSQL_19.json.gz) · [详情页](https://github.com/artxom/DataOG/blob/main/knowledge_packs/packs/DataOG_KB_PGSQL_19.json.gz) · [MD 源码](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/pgsql) |
| **三库合集包（全量初始化）**<br>`DWS + Inceptor + PGSQL` | `DataOG_KB_ALL_STARTER.json.gz` | 9 篇 / 59 块 | `~19.4 KB` | [直接下载 (.json.gz)](https://raw.githubusercontent.com/artxom/DataOG/main/knowledge_packs/packs/DataOG_KB_ALL_STARTER.json.gz) · [详情页](https://github.com/artxom/DataOG/blob/main/knowledge_packs/packs/DataOG_KB_ALL_STARTER.json.gz) · [全部源码](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources) |

---

## 二、如何导入到 Data.O.G 中使用

### 方式 A：导入 `.json.gz` 知识包（推荐，100% 离线保留标签与方言绑定）
1. 从上方表格点击下载目标数据库对应的 `.json.gz` 文件到本地（或拷入内网离线工作机）。
2. 启动 **Data.O.G**，点击桌面右上角或 Dock 中的 **「系统配置」→「参考知识库」** 标签页。
3. 点击 **「导入知识包」**，选择刚下载的 `DataOG_KB_*.json.gz` 文件即可立即生效；重复导入同 ID 文档会自动安全跳过。
4. 打开 Dock 中的 **「Data.O.G（智能数据助手）」** 应用，将目标方言切换为 `DWS`、`INCEPTOR` 或 `PGSQL`，提问时助手会通过 `search_knowledge_base` 按目标方言硬隔离检索并引用这些规范与报错排障方案。

### 方式 B：单独上传 `.md` 参考文档
如果您只想补充其中某一份手册（或在内网自行微调编辑后上传），可在 `sources/` 下点击对应 `.md` 文件的 **Raw** 下载后：
1. 进入 **「系统配置 → 参考知识库 → 上传参考文档」**；
2. 选定正确的 **目标方言**（`DWS` / `INCEPTOR` / `PGSQL`）并上传 `.md` 文件，系统将自动完成本地章节切片与 SQLite FTS5 全文索引。

---

## 三、收录内容覆盖范围与参考资料来源

### 1. 华为云 GaussDB(DWS) `9.1` / `8.3`（[`sources/dws/`](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/dws)）
- [`01_dws_9.1_sql_ddl_syntax.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/dws/01_dws_9.1_sql_ddl_syntax.md)：`ROW`/`COLUMN`/`HSTORE_OPT` 存储引擎选型、DWS 9.1 `ENABLE_BINLOG` 与 `ENABLE_DELTA` 互斥规则、分布键与主键/唯一约束铁律、自动分区 `time_format`、OBS 冷热表与 Disk Cache / `EXPLAIN WARMUP`、`ENABLE_RECYCLEBIN` / `PURGE` 与 `sql_dialect` 扩展。
- [`02_dws_9.1_distribution_tuning.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/dws/02_dws_9.1_distribution_tuning.md)：万表集群优先用 `pg_catalog.table_distribution('schema.table')` 替代全库 `pgxc_get_table_skewness`、`pgxc_thread_wait_status` 等待事件定位、`EXPLAIN ANALYZE` 算子/下盘排查、`NOT IN` 改写与 CU 膨胀治理。
- [`03_dws_9.1_sysviews_errors.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/dws/03_dws_9.1_sysviews_errors.md)：只读会话与锁阻塞排查 SQL、高频典型建表/内存/分布键/只读沙箱报错排障方案。
- 官方参考：[华为云 GaussDB(DWS) 帮助文档中心](https://support.huaweicloud.com/dws/index.html)

### 2. 星环 Inceptor `9.3` / `9.4`（TDH 9.x & ArgoDB）（[`sources/inceptor/`](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/inceptor)）
- [`01_inceptor_9.x_sql_storage.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/inceptor/01_inceptor_9.x_sql_storage.md)：Quark SQL 引擎与 Oracle/DB2/TD 兼容（`CONNECT BY` / PL/SQL）、`HOLODESK` / 事务 `TORC` (`transactional=true`) / `ORC` / `PARQUET` 选型、元数据探查命令与反引号规则。
- [`02_inceptor_9.x_partition_tuning.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/inceptor/02_inceptor_9.x_partition_tuning.md)：单值分区与范围分区、分桶设计、`ALTER TABLE ... COMPACT 'major' AND WAIT;`、`hive.exec.reducers.max` / `inceptor.exec.reducers.bytes.per.reducer` / `holodesk.default.storage.format` 调优与 `MAPJOIN` Hint。
- [`03_inceptor_9.x_merge_errors.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/inceptor/03_inceptor_9.x_merge_errors.md)：`MERGE INTO` 严格规范（`USING` 必带别名、源端去重、`EXPLAIN MERGE INTO`）、`SemanticException [Error 10284]`、多行匹配冲突、`Column not found`、`ParseException` 与 Thrift `10000` 连接排障。
- 官方参考：[星环科技文档中心 (Transwarp Docs)](https://docs.transwarp.cn/)

### 3. 官方 PostgreSQL `19` / `18` / `17`（[`sources/pgsql/`](https://github.com/artxom/DataOG/tree/main/knowledge_packs/sources/pgsql)）
- [`01_pgsql_19_sql_features.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/pgsql/01_pgsql_19_sql_features.md)：PG 19（`REPACK CONCURRENTLY`、`INSERT ... ON CONFLICT DO SELECT`、窗口 `IGNORE NULLS`、`COPY ... FORMAT json`、`WAIT FOR LSN`、`wal_level=logical` 动态激活、`pg_plan_advice`）、PG 18（`uuidv7()`、`VIRTUAL` 生成列、`OLD/NEW in RETURNING`）与 PG 17（`JSON_TABLE()`、`MERGE ... RETURNING merge_action()`）。
- [`02_pgsql_19_index_explain_tuning.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/pgsql/02_pgsql_19_index_explain_tuning.md)：PG 18 Async I/O (`io_method`)、B-Tree Skip Scans、`EXPLAIN (ANALYZE)` 默认 `BUFFERS`、`pg_upgrade` 保留统计信息、PG 17 `VACUUM` TidStore 与原生增量备份（`summarize_wal` / `pg_combinebackup`）、索引选型与深分页/索引失效改写。
- [`03_pgsql_19_catalogs_sqlstate.md`](https://github.com/artxom/DataOG/blob/main/knowledge_packs/sources/pgsql/03_pgsql_19_catalogs_sqlstate.md)：`pg_blocking_pids` 锁链、`pg_stat_user_tables` / `pg_stat_user_indexes`、`pg_stat_io` 观测模板与 PostgreSQL Official Appendix A `SQLSTATE` 错因与修复速查。
- 官方参考：[PostgreSQL Official Documentation](https://www.postgresql.org/docs/)

---

## 四、本地定制扩展与重新打包脚本

如果您希望在团队内增加或修订 `sources/` 下的 Markdown 手册并重新生成 `.json.gz` 知识包，可直接运行仓库内置的零外部依赖构建与自检脚本：

```bash
python3 knowledge_packs/build_packs.py
```

脚本会自动调用 `PlatformContext` 与 `KnowledgeHub` 完成所有 `.md` 切块索引、导出 4 份 `.json.gz` 到 `knowledge_packs/packs/`，并在独立临时沙箱中自动校验反序列化导入、重复文档跳过与方言硬隔离 FTS5 检索。
