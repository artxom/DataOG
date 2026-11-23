# PostgreSQL 19 / 18 / 17 系统目录排障与 SQLSTATE 错误码手册

> 适用版本：PostgreSQL 19 / 18 / 17 | 方言标识：`PGSQL` | 错误码参考：PostgreSQL Official Appendix A

## 1. 高频只读系统视图诊断模板

以下查询均为纯只读语句，可用于在线会话分析、锁阻塞排查、空间膨胀评估与 I/O 热点诊断：

### 1.1 活跃会话、等待事件与锁阻塞链（`pg_blocking_pids`）

```sql
-- 一键定位阻塞源头与被阻塞 SQL（只读）
SELECT
    blocked.pid                               AS blocked_pid,
    blocked.usename                           AS blocked_user,
    NOW() - blocked.query_start               AS wait_duration,
    blocked.wait_event_type || ':' || blocked.wait_event AS wait_event,
    pg_blocking_pids(blocked.pid)             AS blocker_pids,
    LEFT(blocked.query, 150)                  AS blocked_sql
FROM pg_stat_activity blocked
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
ORDER BY wait_duration DESC;
```

### 1.2 表膨胀（死元组）、高顺序扫描大表与零命中冗余索引

```sql
-- 排查高顺序扫描大表与死元组清理需求
SELECT
    schemaname,
    relname,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
    seq_scan,
    idx_scan,
    n_live_tup,
    n_dead_tup,
    last_autovacuum,
    last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC, seq_scan DESC
LIMIT 25;

-- 排查长期未使用的冗余大索引（评估清理前需结合统计复位时间）
SELECT
    schemaname,
    relname AS table_name,
    indexrelname AS index_name,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
    idx_scan
FROM pg_stat_user_indexes
WHERE idx_scan = 0
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 20;
```

### 1.3 I/O 子系统精准观测（PG 17+ `pg_stat_io`）

```sql
-- 分析各后台角色与存储对象的真实 I/O 读写、fsync 与淘汰压力
SELECT backend_type, object, context, reads, writes, extends, fsyncs, evictions
FROM pg_stat_io
WHERE (reads + writes + extends + fsyncs) > 0
ORDER BY reads + writes DESC
LIMIT 30;
```

## 2. 官方 `SQLSTATE` 常见错误码与修复对照表

| `SQLSTATE` | 官方名称 | 常见触发场景 | 精准排障与修复策略 |
| :--- | :--- | :--- | :--- |
| **`23505`** | `unique_violation` | 插入或更新违反唯一约束/主键 | 改用 `INSERT ... ON CONFLICT DO UPDATE` 或 PG 19 **`ON CONFLICT DO SELECT`**；核对序列值 `setval` 是否落后于表最大 ID |
| **`23503`** | `foreign_key_violation` | 插入子表值在父表不存在，或删除仍被引用的父记录 | 检查父表主键值及写入顺序，或根据业务评估外键级联策略 |
| **`22P02`** | `invalid_text_representation` | 将空串 `''` 或非法字符强转为 `INT`/`NUMERIC`/`UUID`/`TIMESTAMP` | 使用 **`NULLIF(TRIM(col), '')::NUMERIC`** 安全过滤空串再强转 |
| **`40P01`** | `deadlock_detected` | 并发事务以相反顺序申请互斥锁 | 统一多行更新的锁获取排序（如按主键 `ORDER BY id` 批量锁）；缩短单事务长度；应用层加退避重试 |
| **`40001`** | `serialization_failure` | `REPEATABLE READ` / `SERIALIZABLE` 隔离级别下并发写冲突 | 属于正常 MVCC 冲突保护，应用层应捕获 `40001` 自动重试整个事务 |
| **`42P01`** | `undefined_table` | 表不存在或当前 `search_path` 未包含目标 Schema | 显式使用 `schema_name.table_name` 全限定名，并检查表名创建时是否使用了带大写字母的双引号 `"MyTable"` |
| **`42703`** | `undefined_column` | 列名拼写错误、别名作用域不可见或双引号大小写不匹配 | PG 未加双引号的标识符会自动折叠为**小写**；排查 `CREATE TABLE` 是否带双引号保留了大写列名 |
| **`53200` / `53100`** | `out_of_memory` / `disk_full` | 复杂哈希/排序内存爆表或临时目录写满 | 拆解超大 SQL 或添加分批过滤条件；查 `work_mem` 与 `EXPLAIN (ANALYZE)` 中 `temp written` 溢出量 |
| **`25006`** | `read_only_sql_transaction` | 在只读副本或只读沙箱会话中执行了 DDL/DML | 切换主库可写会话；在 Data.O.G AI 助手中需生成可执行提案并由用户**二次确认**后执行 |
