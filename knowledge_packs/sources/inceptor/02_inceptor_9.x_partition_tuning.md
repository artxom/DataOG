# 星环 Inceptor 9.3 / 9.4 分区、分桶、Compaction 与性能调优

> 适用版本：Transwarp Inceptor 9.3 / 9.4（TDH 9.x & ArgoDB） | 方言标识：`INCEPTOR`

## 1. 单值分区 vs 范围分区（Single Value vs Range Partition）

1. **单值分区（Single Value Partition）**：
   - 语法：`PARTITIONED BY (dt STRING, biz_line STRING)`
   - 适用：离散枚举或标准化日期分区（如 `dt='20260401'`），每个分区对应独立存储目录，支持按目录粒度裁剪和直接 `DROP PARTITION`。
   - **风险规避**：避免用高基数列（如 `user_id`、`txn_id`）作单值分区，否则会产生数万级小目录与 MetaStore 内存膨胀。
2. **范围分区（Range Partition）**：
   - 语法：`PARTITIONED BY RANGE (col) (PARTITION p1 VALUES LESS THAN (...), PARTITION pmax VALUES LESS THAN (MAXVALUE))`
   - 适用：连续时间区间或数值区间归档，常见于 `HOLODESK` 与 ArgoDB 宽表。

## 2. 分桶设计原则（Bucketing）

- **作用**：通过 `CLUSTERED BY (join_or_pk_col) INTO N BUCKETS` 将数据 Hash 散列至固定数量的桶文件，既能加速 Bucket Map Join，又是 TORC 事务表开启 `transactional = 'true'` 的典型前提条件。
- **桶数（`N`）规划**：参考历史/预估总数据量，使单个 Bucket 物理文件大小尽量落在 **256 MB ~ 1 GB** 区间，通常取与集群 Executor 核数成倍数的 2 的幂或质数（如 16 / 32 / 64）。
- **切忌随意修改**：分桶数一经建表确定，直接更改容易导致 Hash 路由错乱；需调整时应新建目标表并 `INSERT OVERWRITE` 迁移。

## 3. 事务表碎片合并治理（Compaction）

频繁对 TORC/事务表执行小批量 `INSERT`、`UPDATE`、`DELETE`、`MERGE INTO` 会累积大量 `delta` 增量目录，显著拖慢读取并提高任务调度开销。

- **Major Compaction（全量合并基线与增量文件）**：

```sql
-- 全表同步合并（等待合并完成）
ALTER TABLE dwd.acct_balance_torc COMPACT 'major' AND WAIT;

-- 仅合并指定分区（生产大表推荐按分区粒度执行）
ALTER TABLE dwd.acct_balance_torc PARTITION (dt='20260401') COMPACT 'major' AND WAIT;
```

- **治理建议**：对每日高频 `MERGE` 更新的当日分区或最近 3 天活动分区，在调度链路末尾追加分区级 `COMPACT 'major' AND WAIT;`。

## 4. 核心调优参数与执行计划诊断

Inceptor 9.x 常通过 `SET param=value;` 在会话级调优资源与并行度（以下为高频参考参数，具体参数值结合集群资源规格设置）：

| 参数名 | 核心作用 | 典型调优场景 |
| :--- | :--- | :--- |
| `hive.exec.reducers.max` | 限制单个 SQL 任务的最大 Reducer 并发数 | 防止大分区表自动派生过多 Reducer 引发资源抢占或大量小文件 |
| `inceptor.exec.reducers.bytes.per.reducer` | 控制每个 Reducer 预期处理的数据字节数 | 调大可合并过碎 Reducer；调小可提升巨型聚合任务的并行度 |
| `holodesk.default.storage.format` | 指定 Holodesk 表默认底层存储编码格式 | 统一集群内新建 Holodesk 表的默认存储规范 |

### 4.1 MapJoin 小表广播与倾斜缓解

当大事实表关联小维表发生长尾 Shuffle 时，可通过 Hint 强制触发 Map 端广播关联（MapJoin）：

```sql
SELECT /*+ MAPJOIN(d) */
    f.order_id,
    f.amt,
    d.org_name
FROM dwd.fact_order f
JOIN dim.org_info d
  ON f.org_code = d.org_code
WHERE f.dt = '20260401';
```

### 4.2 查询优化检查清单
1. 先跑 **`EXPLAIN`** 检查分区裁剪是否生效（过滤条件切勿对分区列包裹函数）。
2. 检查倾斜值（如大量的 `NULL`、`'-1'`、`'UNKNOWN'`）：关联前单独过滤或加随机前缀打散热点 Key。
3. 严禁无过滤条件执行全量 `SELECT *` 或无上限跨年大跨度聚合。
