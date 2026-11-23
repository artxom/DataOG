# PostgreSQL 19 / 18 / 17 新特性与现代 SQL 语法参考

> 适用版本：PostgreSQL 19（前沿特性）/ 18 / 17 | 方言标识：`PGSQL` | 官方文档基准：`postgresql.org/docs`

## 1. PostgreSQL 19 核心新特性

1. **原生无锁在线重组 `REPACK CONCURRENTLY`**：
   - 将长期依赖外部插件 `pg_repack` 的在线瘦身能力内置入核心语法，可在不长时间阻塞读写业务的前提下回收表与索引碎片膨胀空间。
2. **`INSERT ... ON CONFLICT DO SELECT`**：
   - 解决了此前 `Upsert` 想在冲突时“只查不改”只能写无意义 `DO UPDATE SET id = EXCLUDED.id` 造成行锁放大与触发器误触的痛点，直接返回已有冲突行（并支持搭配行锁子句）。
3. **窗口函数 `IGNORE NULLS` / `RESPECT NULLS`**：
   - 原生支持 SQL 标准空值忽略修饰，一行 SQL 实现金融/时序常见的“前向填充（Forward Fill）”：

```sql
-- PostgreSQL 19+：按时间窗口向后查找最近一个非空报价
SELECT
    trade_ts,
    symbol,
    quote_price,
    LAST_VALUE(quote_price) IGNORE NULLS OVER (
        PARTITION BY symbol
        ORDER BY trade_ts
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS filled_price
FROM market_ticks;

-- PostgreSQL 19+：幂等插入并在冲突时直接返回现有记录
INSERT INTO dim_tags (tag_name)
VALUES ('risk_high')
ON CONFLICT (tag_name) DO SELECT
RETURNING tag_id, tag_name;
```

4. **流式与复制增强**：
   - **`COPY ... FORMAT json`**：原生高速导出换行分隔 JSON 流。
   - **`WAIT FOR LSN`**：读写分离架构下可在会话中显式等待备库 WAL 回放到指定 LSN，避免读到旧数据。
   - **自动化 `wal_level = logical` 切换** 与 **`pg_plan_advice`**（执行计划控制建议）。

## 2. PostgreSQL 18 重要新能力

- **原生时间有序主键 `uuidv7()`**：基于 Unix 时间戳前缀生成单调递增 UUID，相比 `gen_random_uuid()` (v4) 大幅减少 B-Tree 索引随机写页分裂与缓冲池抖动。
- **`VIRTUAL` 生成列**：支持不占用磁盘空间的虚拟生成列（动态求值），适合派生字段格式化或计算表达式索引来源。
- **`RETURNING` 同时引用 `OLD` 与 `NEW`**：单条 `UPDATE` / `DELETE` / `MERGE` 可直接审计旧值与新值：

```sql
-- PostgreSQL 18+：在同一 RETURNING 中审计变更前后数值
UPDATE acct_ledger
SET balance = balance + 100.00
WHERE acct_id = 10001
RETURNING acct_id, old.balance AS before_bal, new.balance AS after_bal;
```

- **默认启用数据页校验和**：`initdb` 默认启用 checksum，增强静默坏块早期感知。

## 3. PostgreSQL 17 现代 SQL 与分区基准

- **`JSON_TABLE()` 标准行列映射**：将半结构化 JSON/JSONB 直接按列定义展开为关系结果集，取代多层级联 `jsonb_array_elements`：

```sql
-- PostgreSQL 17+：标准 JSON_TABLE 提取嵌套订单明细
SELECT jt.*
FROM raw_events e,
JSON_TABLE(
    e.payload,
    '$.items[*]' COLUMNS (
        sku      TEXT          PATH '$.sku',
        qty      INT           PATH '$.qty',
        price    NUMERIC(12,2) PATH '$.price'
    )
) AS jt;
```

- **`MERGE ... RETURNING merge_action()`**：配合 `MERGE` 返回 `'INSERT'` / `'UPDATE'` / `'DELETE'` 动作标签，便于精确统计同步行数。
- **声明式分区铁律（`PARTITION BY RANGE / LIST / HASH`）**：
  - 主键或唯一索引 **必须包含全部 `PARTITION BY` 列**；
  - 新建范围分区时务必预留默认分区或确保未来时间窗提前建好分区，防止插入触发 `no partition of relation found for row`。
