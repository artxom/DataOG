# PostgreSQL 19 / 18 / 17 索引、执行计划与性能调优指南

> 适用版本：PostgreSQL 19 / 18 / 17 | 方言标识：`PGSQL`

## 1. 执行计划分析与 PG 18/19 关键引擎特性

排查 PostgreSQL 慢查询首选只读 `EXPLAIN`（不实际修改数据可将 DML 放入事务测试或优先仅分析 `SELECT`；Data.O.G 只读沙箱支持无副作用 `EXPLAIN`）：

- **PG 18+ `EXPLAIN (ANALYZE)` 默认输出 `BUFFERS`**：
  - 直接关注 `Buffers: shared hit=... read=... written=...` 与 `temp read=... written=...`；
  - 出现 `temp written` 说明算子发生磁盘溢出，需评估调整 `work_mem` 或优化连接过滤顺序；
  - 若 `Rows Removed by Filter` 远大于返回行数，说明缺少有效索引或过滤条件写法阻碍了索引匹配。
- **PG 18 B-Tree Skip Scans（跳跃扫描）**：
  - 当联合索引为 `(tenant_id, created_at)` 而查询仅过滤 `WHERE created_at >= ...` 时，如果前导列 `tenant_id` 不同值（Distinct）较少，PG 18 可自动执行 Skip Scan 命中联合索引，无需额外维护大量冗余单列索引。
- **PG 18 异步 I/O（Async I/O）与统计信息无缝保留**：
  - 核心参数 **`io_method`**（可选 `worker` / `io_uring` / `sync`），在云盘与高 IOPS NVMe 环境开启异步 I/O 可显著加快大表顺序扫描、`Bitmap Heap Scan` 与 `VACUUM`。
  - PG 18+ 执行 `pg_upgrade` 升级时默认保留优化器统计信息，消除升级后短期索引退化风险。
- **PG 19 执行计划固化建议 (`pg_plan_advice`)**：辅助对关键核心 SQL 输出可追踪的计划建议，避免统计信息轻微波动导致的执行计划突变。

## 2. 四类常用索引精准选型

| 索引类型 | 创建示例 | 最佳适用场景 |
| :--- | :--- | :--- |
| **B-Tree（含覆盖列）** | `CREATE INDEX CONCURRENTLY idx_ord_cust ON orders(cust_id) INCLUDE (amt, status);` | 等值、范围、`ORDER BY`；`INCLUDE` 配合可见映射可走极速 `Index Only Scan` |
| **Partial Index（局部索引）** | `CREATE INDEX CONCURRENTLY idx_todo ON jobs(created_at) WHERE status = 'PENDING';` | 仅查询极少数未处理/有效状态行，索引体积可缩小 90% 以上 |
| **GIN 倒排索引** | `CREATE INDEX CONCURRENTLY idx_meta_gin ON docs USING GIN (meta_jsonb jsonb_path_ops);` | `JSONB` `@>` 包含检索、数组重叠 `&&` 与 `tsvector` 全文检索 |
| **BRIN 块范围索引** | `CREATE INDEX CONCURRENTLY idx_log_ts_brin ON audit_logs USING BRIN (created_at);` | 按时间严格追加（Append-Only）的十亿级流水大表，体积仅为 B-Tree 的 1% |

> **上线铁律**：生产环境新建索引务必附带 **`CONCURRENTLY`**（不阻塞业务 DML 写入），且注意 `CREATE INDEX CONCURRENTLY` 不能在显式 `BEGIN ... COMMIT` 事务块内执行。

## 3. `VACUUM` 维护与高频 SQL 改写反模式

1. **PG 17+ 新型 `VACUUM` 内存结构（TidStore）与增量备份**：
   - PG 17 重构了 `VACUUM` 死元组索引，彻底打破早期 `maintenance_work_mem` 1 GB 上限，超大表 `VACUUM` 可一次性完成索引清理而无需反复多轮扫描。
   - 备份恢复推荐了解 `summarize_wal = on` + `pg_basebackup --incremental` + `pg_combinebackup` 原生增量备份体系，以及逻辑订阅转换工具 `pg_createsubscriber`。
2. **避免索引失效的 SQL 改写清单**：
   - **禁止索引列包裹函数或隐式类型强转**：
     - 慢：`WHERE DATE_TRUNC('day', created_at) = '2026-04-01'` 或 `WHERE varchar_id = 12345`；
     - 快：`WHERE created_at >= '2026-04-01' AND created_at < '2026-04-02'` 与类型精确匹配 `'12345'`。
   - **深分页优化（Keyset Pagination）**：用 `WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT 50` 替代 `OFFSET 500000`。
   - **警惕 `NOT IN (SELECT ...)` 陷阱**：若子查询允许 `NULL` 会返回空集且难走高效 Anti-Join，统一改写为 `NOT EXISTS`。
   - **防 `OR` 条件打碎索引**：同一表跨不同列 `WHERE a = 1 OR b = 2` 可视情况改写为 `UNION` / `UNION ALL` 分别走索引（前提注意去重与互斥）。
