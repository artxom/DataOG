"""Data.O.G 内置主流数据库方言速查知识卡 (builtin_dialect_kb).

设计原则：
1. 体积精简（~20 KB 纯文本），开箱零网络、零外部文件即可为大模型提供准确的方言语法基线；
2. 覆盖 10 大内置与 JDBC 方言（DWS / PGSQL / MYSQL / ORACLE / DM8 / KINGBASE / INCEPTOR / DB2 / GBASE / SQLITE）及通用排障调优 (GENERIC)；
3. 用户上传的官方文档、内部规范在检索排序中自动享有更高权重，形成“开箱速查卡 + 私有参考资料”双层离线智库。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

BUILTIN_KB_VERSION = "3.0.0"

SUPPORTED_KB_DIALECTS: Tuple[Tuple[str, str], ...] = (
    ("DWS", "华为云 GaussDB(DWS)"),
    ("PGSQL", "PostgreSQL / openGauss"),
    ("MYSQL", "MySQL / OceanBase / Doris / StarRocks / TiDB"),
    ("ORACLE", "Oracle Database"),
    ("DM8", "达梦数据库 DM8"),
    ("KINGBASE", "人大金仓 KingbaseES"),
    ("INCEPTOR", "星环 Transwarp Inceptor / Hive"),
    ("DB2", "IBM Db2"),
    ("GBASE", "南大通用 GBase 8a/8s"),
    ("SQLITE", "SQLite 3"),
    ("GENERIC", "通用 SQL 调优与规范"),
)

DIALECT_LABEL_MAP: Dict[str, str] = dict(SUPPORTED_KB_DIALECTS)


def normalize_kb_dialect(raw: str) -> str:
    val = (raw or "").strip().upper()
    if val in DIALECT_LABEL_MAP:
        return val
    aliases = {
        "POSTGRES": "PGSQL",
        "POSTGRESQL": "PGSQL",
        "OPENGAUSS": "PGSQL",
        "GAUSSDB": "DWS",
        "MARIADB": "MYSQL",
        "OCEANBASE": "MYSQL",
        "DORIS": "MYSQL",
        "STARROCKS": "MYSQL",
        "TIDB": "MYSQL",
        "HIVE": "INCEPTOR",
        "DAMENG": "DM8",
        "DM": "DM8",
        "SQLITE3": "SQLITE",
        "ALL": "GENERIC",
    }
    return aliases.get(val, "GENERIC")


BUILTIN_DIALECT_DOCS: List[Dict[str, str]] = [
    {
        "doc_id": "builtin_dws",
        "dialect": "DWS",
        "title": "GaussDB(DWS) 核心语法、分布键与调优速查卡",
        "summary": "涵盖 DWS 分布键 DISTRIBUTE BY HASH/REPLICATION、行列存 ORIENTATION、分区表 DDL、EXPLAIN 计划识别与常见报错处理。",
        "content": """# GaussDB(DWS) 核心语法与运维速查

## 标识符、分页与常用函数
- 标识符使用双引号 `"schema"."table"`，默认未加引号折叠为小写。
- 分页支持标准 `LIMIT n OFFSET m`，Top-N 推荐配合 `ORDER BY` 使用避免 MPP 节点返回序抖动。
- 字符串拼接使用 `||` 或 `CONCAT(a, b)`；空值替换使用 `COALESCE(col, default)` 或 `NVL(col, default)`。
- 日期截断与加减：`DATE_TRUNC('month', ts)`、`TO_CHAR(ts, 'YYYY-MM-DD')`、`ts + INTERVAL '7 days'`。
- Upsert 语法：DWS 支持 `MERGE INTO tgt USING src ON (...) WHEN MATCHED THEN UPDATE SET ... WHEN NOT MATCHED THEN INSERT ...`，部分兼容 `INSERT ... ON DUPLICATE KEY UPDATE`。

## 分布键、存储引擎与 DDL 规范
- 建表必须评估分布策略：`DISTRIBUTE BY HASH(join_or_pk_col)`（大事实表按高频等值关联键或高基数主键散列，严禁选性别/状态等低基数字段导致数据倾斜）或 `DISTRIBUTE BY REPLICATION`（小维表广播）。
- 存储方向：`WITH (ORIENTATION=COLUMN, COMPRESSION=LOW)` 适合 OLAP 宽表聚合分析；`ORIENTATION=ROW` 适合高并发点查更新。
- 范围分区示例：`PARTITION BY RANGE (dt) (PARTITION p202601 VALUES LESS THAN ('2026-02-01'))`。
- 列与表注释必须使用独立语句：`COMMENT ON TABLE schema.tbl IS '表注释';`、`COMMENT ON COLUMN schema.tbl.col IS '字段注释';`。

## 系统目录与元数据探查
- 表与注释：`pg_class`、`pg_namespace`、`pg_description`；DWS 分布信息在 `pgxc_class` (`pclocatortype`, `pcattnum`)。
- 字段类型与长度：`pg_attribute` + `format_type(atttypid, atttypmod)`。
- 执行计划：使用 `EXPLAIN (COSTS TRUE, VERBOSE FALSE) SELECT ...` 查看算子；重点关注 `Streaming (type: REDISTRIBUTE)`（重分布网络开销）与 `Streaming (type: BROADCAST)`，若大表被 Broadcast 或出现 `Seq Scan` 过滤百万行，应检查统计信息 (`ANALYZE`) 与分布键对齐。

## 高频报错排查
- `ERROR: distribution key cannot be updated`：DWS 分布列不允许直接 `UPDATE`，需确认业务是否真需修改分布键或采用删除重建策略。
- `ERROR: relation "xxx" does not exist`：确认当前 `search_path` 或是否遗漏 `schema.` 前缀及大小写双引号。
- 数据倾斜诊断：只读执行 `SELECT xc_node_id, COUNT(*) FROM schema.tbl GROUP BY 1 ORDER BY 2 DESC;` 检查各 DN 节点行数是否均衡。
""",
    },
    {
        "doc_id": "builtin_pgsql",
        "dialect": "PGSQL",
        "title": "PostgreSQL / openGauss 语法与性能调优速查卡",
        "summary": "涵盖 PG/openGauss 标识符规则、JSONB/CTE 语法、ON CONFLICT、EXPLAIN 计划分析与高频锁/类型报错。",
        "content": """# PostgreSQL / openGauss 语法与性能调优速查

## 语法与类型要点
- 标识符使用双引号 `"col"`，字符串单引号 `'val'`；类型转换支持 `col::bigint` 或 `CAST(col AS BIGINT)`。
- 分页：`LIMIT n OFFSET m` 或 `FETCH FIRST n ROWS ONLY`。
- 字符串聚合：`STRING_AGG(col, ',') WITHIN GROUP (ORDER BY col)`；正则匹配：`~` / `~*` / `REGEXP_REPLACE`。
- 冲突更新：`INSERT INTO tbl(id, val) VALUES (...) ON CONFLICT (id) DO UPDATE SET val = EXCLUDED.val`。
- 注释：`COMMENT ON TABLE` / `COMMENT ON COLUMN`。

## 元数据与执行计划
- 系统目录：`information_schema.columns`、`pg_stat_user_tables`、`pg_indexes`。
- 只读执行计划：`EXPLAIN SELECT ...`（切勿盲目在生产大表或写语句直接运行 `EXPLAIN ANALYZE`）。
- 关注 `Seq Scan` vs `Index Scan` / `Index Only Scan` / `Bitmap Heap Scan`；若索引未命中，排查隐式类型转换（如 `varchar` 列与整数条件比较）或函数包裹列（如 `DATE(create_time)` 应改写为闭开区间范围条件）。

## 常见报错排查
- `ERROR: operator does not exist: character varying = integer`：显式对齐参数类型或加引号。
- `ERROR: column "xxx" must appear in the GROUP BY clause`：补齐 `GROUP BY` 非聚合列或使用聚合/窗口函数。
""",
    },
    {
        "doc_id": "builtin_mysql",
        "dialect": "MYSQL",
        "title": "MySQL / OceanBase / Doris / StarRocks / TiDB 语法速查卡",
        "summary": "涵盖 MySQL 系反引号标识、ON DUPLICATE KEY UPDATE、EXPLAIN 解读、索引失效与字符集排序规则排查。",
        "content": """# MySQL / OceanBase / Doris / StarRocks / TiDB 速查

## 核心语法差异
- 标识符使用反引号 `` `schema`.`table` ``；字符串拼接使用 `CONCAT(a, b)`（注意 `||` 在默认 SQL_MODE 下是逻辑或而非字符串拼接！）。
- 分页：`LIMIT offset, count` 或 `LIMIT count OFFSET offset`。
- 空值处理：`IFNULL(a, b)` / `COALESCE(a, b)`；日期格式化：`DATE_FORMAT(dt, '%Y-%m-%d')`、`DATE_ADD(dt, INTERVAL 7 DAY)`。
- 冲突更新：`INSERT INTO tbl (id, c1) VALUES (...) ON DUPLICATE KEY UPDATE c1 = VALUES(c1)`（MySQL 8.0+ 推荐行别名 `AS new_row`）。
- DDL 注释内联在建表/改表语句中：`COMMENT '字段说明'`，不支持 `COMMENT ON COLUMN`。

## 索引调优与 EXPLAIN 解读
- 查看执行计划：`EXPLAIN SELECT ...`；重点检查 `type` 列（`const` > `eq_ref` > `ref` > `range` > `index` > `ALL` 全表扫描）、`key`（实际命中索引）、`rows` 与 `Extra`（出现 `Using filesort` / `Using temporary` 需重点优化排序或联合索引顺序）。
- 索引失效常见原因：违背最左前缀法则、`LIKE '%abc'` 前导通配符、对索引列使用函数/隐式类型转换、`!=` / `NOT IN` 或 `OR` 未分别建索引。
- Doris / StarRocks 注意区分 Olap 表模型（Duplicate / Unique / Aggregate）及分区裁剪条件。

## 高频报错排查
- `ERROR 1055 (42000)`: `ONLY_FULL_GROUP_BY` 模式下 SELECT 含非 GROUP BY 列，需使用 `ANY_VALUE(col)` 或补齐分组。
- `ERROR 1064 (42000)`: SQL 语法错误，检查保留字未加反引号（如 `order`, `key`, `desc`）或末尾多余逗号。
- `ERROR 1267`: `Illegal mix of collations` 字符集排序规则不一致，确认表/字段 `utf8mb4_general_ci` 与 `utf8mb4_unicode_ci` 对齐。
""",
    },
    {
        "doc_id": "builtin_oracle",
        "dialect": "ORACLE",
        "title": "Oracle Database 语法、ROWNUM 分页与 ORA 排障速查卡",
        "summary": "涵盖 Oracle VARCHAR2/NUMBER 类型、ROWNUM/FETCH 分页、DUAL 伪表、MERGE INTO 及高频 ORA 报错定位。",
        "content": """# Oracle Database 语法与排障速查

## 语法与方言要点
- 标识符默认大写存储，未加双引号自动折叠大写；双引号 `"col"` 区分大小写。
- 无表常量查询必须带 `FROM DUAL`。
- 分页：12c+ 支持 `OFFSET m ROWS FETCH NEXT n ROWS ONLY`；11g 及通用写法用子查询 `ROWNUM <= n`（注意 `ORDER BY` 必须在内层子查询完成排序后再在外层限制 `ROWNUM`）。
- 类型特征：字符推荐 `VARCHAR2(n)` / `CLOB`，数值 `NUMBER(p, s)`，时间 `DATE` / `TIMESTAMP`；空值判断留意 Oracle 中空串 `''` 等同于 `NULL`。
- 日期转换：`TO_DATE('2026-01-01', 'YYYY-MM-DD')`、`TO_CHAR(SYSDATE, 'YYYYMMDD')`。
- Upsert 使用 `MERGE INTO target t USING source s ON (...) WHEN MATCHED THEN ... WHEN NOT MATCHED THEN ...`。

## 元数据与执行计划
- 数据字典：`ALL_TABLES`、`ALL_TAB_COMMENTS`、`ALL_TAB_COLUMNS`、`ALL_COL_COMMENTS`、`ALL_IND_COLUMNS`。
- 注释语法：`COMMENT ON TABLE owner.tbl IS '...'`、`COMMENT ON COLUMN owner.tbl.col IS '...'`。

## 高频 ORA 错误定位
- `ORA-00942`: 表或视图不存在 — 检查 Owner Schema 前缀、同义词或当前用户 `SELECT` 权限。
- `ORA-00904`: 无效标识符 — 核对字段名拼写、双引号大小写或派生别名作用域。
- `ORA-01722`: 无效数字 — 发生隐式字符转数字失败，用正则或安全过滤排查脏字符数据。
- `ORA-01843` / `ORA-01861`: 日期格式不匹配 — 显式使用 `TO_DATE(..., fmt)` 指定掩码。
""",
    },
    {
        "doc_id": "builtin_dm8",
        "dialect": "DM8",
        "title": "达梦数据库 DM8 语法特性与 Oracle/PG 迁移速查卡",
        "summary": "涵盖 DM8 大小写敏感机制、模式对象规范、EXPLAIN 计划及建表/改表注意事项。",
        "content": """# 达梦数据库 DM8 语法与兼容速查

## 核心方言规则
- 达梦 DM8 高度兼容 Oracle 与 ANSI SQL，但**默认大小写敏感参数** (`CASE_SENSITIVE`) 取决于初始化建库设置；建议表名与字段名使用双引号或统一大写/小写约定。
- 支持 `LIMIT n OFFSET m` 以及 `TOP n`、`ROWNUM` 分页写法，推荐明确 `ORDER BY` 后使用 `LIMIT n OFFSET m`。
- 字符类型支持 `VARCHAR(n)` / `VARCHAR2(n)`，数值支持 `NUMBER(p, s)` / `DECIMAL(p, s)` / `BIGINT` / `INT`。
- 注意达梦中修改列语法通常为 `ALTER TABLE schema.tbl MODIFY col_name new_type;`，重命名列为 `ALTER TABLE schema.tbl RENAME COLUMN old_col TO new_col;`。
- 注释：`COMMENT ON TABLE` 与 `COMMENT ON COLUMN`。

## 元数据与执行计划
- 系统视图：`ALL_TABLES`、`ALL_TAB_COLUMNS`、`ALL_COL_COMMENTS`、`SYSOBJECTS`、`SYSCOLUMNS`。
- 执行计划：使用 `EXPLAIN SELECT ...` 观察 `CSCN2`（全表扫描）与 `SSEK2` / `BLKUP2`（索引扫描及回表），尽量为高选择性过滤与关联键建立二级索引。

## 常见报错提示
- `对象名无效` / `无效的表或视图名`：排查模式名 (`SYSDBA` 等) 与对象大小写匹配情况。
- `字符串截断` (`-6169`)：检查汉字多字节长度（UTF-8 下 1 个汉字占 3 字节，若达梦字符集按字节计长需适当放宽 VARCHAR 长度）。
""",
    },
    {
        "doc_id": "builtin_kingbase",
        "dialect": "KINGBASE",
        "title": "人大金仓 KingbaseES V8 语法与兼容模式速查卡",
        "summary": "涵盖 KingbaseES Oracle/PG 双兼容模式注意点、系统目录视图、分页与执行计划分析。",
        "content": """# 人大金仓 KingbaseES V8 速查

## 方言与兼容特性
- KingbaseES 基于 PG 内核扩展，支持 PostgreSQL 与 Oracle 两种兼容模式（常见默认 Schema `public` 或业务 Schema）。
- 标识符使用双引号 `"col"`，字符串单引号 `'val'`。
- 分页：`LIMIT n OFFSET m` 与 `ROWNUM` 在 Oracle 兼容模式下均可工作，但 `LIMIT n OFFSET m` 跨模式最稳妥。
- 常用函数：`COALESCE` / `NVL`、`TO_CHAR`、`TO_DATE`、`STRING_AGG`、`ROW_NUMBER() OVER (...)`。
- 冲突更新：PG 模式下支持 `ON CONFLICT (...) DO UPDATE SET ...`，Oracle 兼容模式推荐 `MERGE INTO`。

## 调优与排障
- 执行计划：`EXPLAIN SELECT ...`；检查统计信息 `sys_stat_user_tables` 与索引定义 `sys_indexes`（或 `pg_indexes` 兼容视图）。
- 注释与 DDL 遵循 PG 风格：`ALTER TABLE schema.tbl ALTER COLUMN col TYPE ...`、`COMMENT ON COLUMN`。
""",
    },
    {
        "doc_id": "builtin_inceptor",
        "dialect": "INCEPTOR",
        "title": "星环 Transwarp Inceptor / Hive SQL 语法与分桶优化速查卡",
        "summary": "涵盖 Inceptor/Hive 分区表 PARTITIONED BY、分桶 CLUSTERED BY、ORC/Hyperbase 存储、不支持更新与倾斜优化。",
        "content": """# 星环 Inceptor / Hive SQL 语法与优化速查

## 方言与类型要点
- 标识符使用反引号 `` `db`.`table` ``。
- 常用类型：`STRING`、`BIGINT`、`INT`、`DOUBLE`、`DECIMAL(p, s)`、`BOOLEAN`、`DATE`、`TIMESTAMP`（注意常规 Hive/Inceptor 表默认无 `VARCHAR2` 长度强截断语义，用 `STRING` 最稳妥）。
- 仅支持 `LIMIT n`（部分版本支持 `LIMIT offset, count`），注意排序全局有序用 `ORDER BY`，局部节点有序用 `SORT BY`。
- 字符串拼接：`CONCAT(a, b)` 或 `CONCAT_WS(',', a, b)`；日期函数：`TO_DATE`、`DATE_FORMAT`、`DATE_ADD`、`DATEDIFF`。

## 建表、分区与存储规范
- 建表格式：`CREATE TABLE db.tbl (col1 STRING COMMENT '说明') PARTITIONED BY (pt_dt STRING) STORED AS ORC;`。
- Hyperbase / ArgoDB 事务表支持主键与 `INSERT`/`UPDATE`/`DELETE`，普通 HDFS ORC 表不适合高频单行更新。
- 严格分区裁剪：查询分区表务必在 `WHERE` 中显式过滤分区列（如 `pt_dt = '20260101'`），避免全量扫描海量历史文件！

## 常见性能与报错排查
- 数据倾斜（某个 Reduce 卡在 99%）：检查 `GROUP BY` 或 `JOIN` 键中是否存在大量 `NULL` 或默认值（如 `'-1'` / `'未知'`），先过滤空值或使用两阶段随机前缀打散聚合。
- `SemanticException`: 核对 `GROUP BY` 列表、别名引用及分区字段不要重复写入普通列定义中。
""",
    },
    {
        "doc_id": "builtin_db2",
        "dialect": "DB2",
        "title": "IBM Db2 SQL 语法与 FETCH FIRST 分页速查卡",
        "summary": "涵盖 Db2 FETCH FIRST ROWS ONLY、SYSIBM/SYSCAT 目录视图、类型规范与 SQLCODE 报错排查。",
        "content": """# IBM Db2 语法与排障速查

## 语法特性
- 标识符使用双引号 `"SCHEMA"."TABLE"`，默认折叠为大写。
- 常量查询使用 `FROM SYSIBM.SYSDUMMY1`（或 `VALUES (...)`）。
- 分页：`FETCH FIRST n ROWS ONLY` 或 11.1+ 的 `LIMIT n OFFSET m`。
- 常用类型：`VARCHAR(n)`、`DECIMAL(p, s)`、`INTEGER`、`BIGINT`、`DATE`、`TIMESTAMP`、`CLOB`。
- 修改字段类型：`ALTER TABLE schema.tbl ALTER COLUMN col SET DATA TYPE VARCHAR(128)`（注意可能需要 `REORG TABLE schema.tbl` 使表脱离 REORG PENDING 状态）。

## 系统目录与报错码
- 元数据目录：`SYSCAT.TABLES`、`SYSCAT.COLUMNS`、`SYSCAT.INDEXES`。
- `SQLCODE=-204`: 表或对象未定义；`SQLCODE=-206`: 字段名在上下文无效；`SQLCODE=-668`: 表处于 Check/Reorg Pending 状态（通常由批量加载或特定 ALTER 引起）。
""",
    },
    {
        "doc_id": "builtin_gbase",
        "dialect": "GBASE",
        "title": "南大通用 GBase 8a/8s 语法与分布式键值速查卡",
        "summary": "涵盖 GBase 8a 列存/分布列 DISTRIBUTED BY 与 MySQL 风格语法要点、8s 兼容特性及排障参考。",
        "content": """# 南大通用 GBase 8a / 8s 语法速查

## 核心特点
- GBase 8a MPP 集群高度兼容 MySQL SQL 语法，标识符常用反引号 `` `tbl` ``；支持 `LIMIT n OFFSET m`。
- 分布式建表可指定 `DISTRIBUTED BY ('hash_col')` 以保证大表 Join 同分布本地化执行，避免跨节点重分布。
- GBase 8s 偏向 Informix/Oracle 事务语义，支持 `FIRST n` / `SKIP m FIRST n` 分页及 `SYSTABLES`/`SYSCOLUMNS`。
- 调优建议：避免在大表关联键上做隐式类型转换或函数运算；确认字符串拼接使用 `CONCAT` 避免跨方言 `||` 语义差异。
""",
    },
    {
        "doc_id": "builtin_sqlite",
        "dialect": "SQLITE",
        "title": "SQLite 3 本地语法、PRAGMA 与 EXPLAIN QUERY PLAN 速查卡",
        "summary": "涵盖 SQLite 3 动态类型亲和、UPSERT、sqlite_master 目录与 EXPLAIN QUERY PLAN 索引诊断。",
        "content": """# SQLite 3 语法与诊断速查

## 语法要点
- 默认单库 Schema 名为 `main`，可直接写表名 `tbl` 或 `"main"."tbl"`；标识符支持双引号或反引号。
- 分页：`LIMIT n OFFSET m`。
- 字符串拼接使用 `||`；日期时间函数：`DATE('now')`、`DATETIME('now', 'localtime')`、`STRFTIME('%Y-%m-%d', col)`。
- Upsert：`INSERT INTO tbl (id, val) VALUES (...) ON CONFLICT(id) DO UPDATE SET val = excluded.val`。
- DDL 限制：SQLite 不支持 `COMMENT ON`；修改列类型或删外键通常需“建新表 -> 导数据 -> 删旧表 -> 重命名”重建表。

## 元数据与执行计划
- 目录查询：`SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view');`、`SELECT * FROM pragma_table_info('tbl');`。
- 执行计划：`EXPLAIN QUERY PLAN SELECT ...`；出现 `SCAN tbl` 表示全表扫描，`SEARCH tbl USING INDEX ...` 表示命中索引。
""",
    },
    {
        "doc_id": "builtin_generic",
        "dialect": "GENERIC",
        "title": "通用 SQL 性能优化、防踩坑与排障方法论",
        "summary": "涵盖索引 SARGable 原则、JOIN 膨胀预防、子查询改写、执行计划排查与安全写操作核对清单。",
        "content": """# 通用 SQL 性能优化与排障方法论

## 十大高频性能反模式与优化建议
1. **避免对索引列做函数/算术计算 (Non-SARGable)**：`WHERE YEAR(dt) = 2026` 或 `WHERE amt + 10 > 100` 会导致索引失效，应改写为范围比较 `dt >= '2026-01-01' AND dt < '2027-01-01'`。
2. **杜绝隐式类型转换**：字符串列 `cust_id = 1001` 会触发全列强转，务必写成 `cust_id = '1001'`。
3. **慎用前导通配符**：`LIKE '%keyword'` 无法利用 B-Tree 索引，尽量使用前缀匹配 `'keyword%'` 或全文检索。
4. **先过滤聚合再关联**：先对明细大表按关联键做 `WHERE` 过滤与聚合，再与维表 `JOIN`，能显著减小中间哈希表内存占用。
5. **警惕多表 1:N / M:N 关联导致行数翻倍**：关联后指标翻倍时，先用 `COUNT(*) vs COUNT(DISTINCT pk)` 验证连接键唯一性。
6. **避免 SELECT * 搬运宽表**：只选需要字段以提升列存库 I/O 裁剪与索引覆盖 (`Index Only Scan`) 命中率。
7. **`NOT IN` 遇 `NULL` 陷阱**：子查询结果包含 `NULL` 时 `NOT IN` 永远返回空集！改用 `NOT EXISTS` 或 `LEFT JOIN ... WHERE right.key IS NULL`。
8. **深度分页优化**：避免 `OFFSET 1000000`，改用基于上页最大主键的游标分页 (`WHERE id > last_id ORDER BY id LIMIT 50`)。
9. **合并多次全表扫描**：用 `COUNT(CASE WHEN cond1 THEN 1 END)` 替代多个独立 `SELECT COUNT(*)` 子查询。
10. **写入与结构变更先验证再确认**：执行 `UPDATE`/`DELETE` 前必须先用 `SELECT COUNT(*)` 与带条件抽样验证受影响范围。
""",
    },
]
