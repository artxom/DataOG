# 星环 Inceptor 9.3 / 9.4 `MERGE INTO` 规范与高频报错排障

> 适用版本：Transwarp Inceptor 9.3 / 9.4（TDH 9.x & ArgoDB） | 方言标识：`INCEPTOR`

## 1. `MERGE INTO` 严格语法约束与最佳实践

Inceptor 9.x 对 `MERGE INTO`（常用于数仓拉链、增量 Upsert 与 T+1 对账回写）有严格语义检查，编写时必须遵守以下 4 条规范：

1. **必须为 `USING` 子查询/源表指定别名**：例如 `USING (...) s1`，缺少源别名会触发语法错误。
2. **源端关联键必须严格唯一去重**：同一条目标行不允许匹配到多条源记录，否则运行时直接抛出确定性冲突异常。
3. **仅允许在事务表上作为目标表**：目标表需启用 `TBLPROPERTIES ('transactional'='true')`（通常带 `CLUSTERED BY ... INTO N BUCKETS`）。
4. **先检查执行计划，善用 `MAPJOIN` 提示**：通过 `EXPLAIN MERGE INTO ...` 查看 Shuffle 路径，若增量批次较小，推荐添加 `/*+ MAPJOIN(s1) */` 消除大表重分布。

```sql
-- 标准安全的 MERGE INTO 写法（源端去重 + USING 别名 + MAPJOIN Hint）
EXPLAIN MERGE INTO dwd.acct_balance_torc AS t
USING (
    SELECT acct_id, cust_id, balance, updated_at, dt
    FROM (
        SELECT
            acct_id,
            cust_id,
            balance,
            updated_at,
            dt,
            ROW_NUMBER() OVER (PARTITION BY acct_id, dt ORDER BY updated_at DESC) AS rn
        FROM ods.inc_acct_balance
        WHERE dt = '20260401'
    ) dedup
    WHERE rn = 1
) s1
ON (t.acct_id = s1.acct_id AND t.dt = s1.dt)
WHEN MATCHED THEN
    UPDATE SET t.balance = s1.balance, t.updated_at = s1.updated_at
WHEN NOT MATCHED THEN
    INSERT (acct_id, cust_id, balance, updated_at, dt)
    VALUES (s1.acct_id, s1.cust_id, s1.balance, s1.updated_at, s1.dt);
```

> 安全提醒：在 Data.O.G AI 助手中，`EXPLAIN MERGE INTO ...` 属于只读分析可安全执行；真正提交 `MERGE INTO` 数据变更必须通过**二次确认弹窗**由用户授权执行。

## 2. 高频报错原因与精准排障

### 错误 1：`SemanticException [Error 10284]`（表事务/语义属性校验异常）
- **常见原因**：
  - 在未开启 `'transactional'='true'` 的普通 ORC / Parquet / 外部表上尝试执行 `UPDATE`、`DELETE` 或 `MERGE INTO`；
  - 修改分区列或分桶列的值（事务更新通常禁止直接修改目标表的分桶键/分区键）；
  - 目标表缺少必要的分桶定义或事务权限未放开。
- **修复步骤**：
  1. 执行 `DESCRIBE FORMATTED target_table;` 核对 `transactional=true` 与分桶配置；
  2. 确保 `UPDATE SET` 中不包含 `PARTITIONED BY` 列及 `CLUSTERED BY` 列；
  3. 若需更新分区/分桶键，改用 `INSERT OVERWRITE` 重算目标分区或删除后重建记录。

### 错误 2：`More than one source row matched for the same target row`（MERGE 多行匹配冲突）
- **根因**：`USING` 数据源存在按 `ON (...)` 关联键重复的多条记录，触发 ANSI SQL / Inceptor 运行时唯一性保护。
- **修复步骤**：在 `USING (...)` 内部先执行 `GROUP BY` 聚合或 `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ... DESC) = 1` 严格去重，并确认去重键与 `ON` 条件完全一致。

### 错误 3：`Column not found` / 字段未找到或作用域不可见
- **常见原因**：
  - 表别名在子查询内外混用或 `ON`/`WHERE` 引用了未投影到外层的列名；
  - `MERGE` 语句的 `VALUES (...)` 或 `SET` 中引用了不存在的列名；
  - 保留字字段未用反引号转义导致解析错位。
- **修复步骤**：核对 `DESCRIBE FORMATTED` 实际物理列名，子查询层显式列出所有透传字段并统一使用小写+下划线及反引号。

### 错误 4：`ParseException`（SQL 词法或语法解析失败）
- **排查要点**：
  1. 检查 `USING (...)` 是否遗漏表别名；
  2. 检查是否使用了保留字（如 `date`、`level`、`comment`、`user`）未加 `` `...` ``；
  3. 检查单次提交是否包含多余分号或 Oracle 风格匿名块与普通查询混写；
  4. `ORDER BY` / `LIMIT` 出现位置是否符合 Quark 语法要求。

### 错误 5：HiveServer2 Thrift (`10000`) 连接或认证报错
- **典型现象**：连接超时、TTransportException、SASL/认证握手失败。
- **排障核对清单**：
  1. 确认目标服务端监听的是 HiveServer2 Thrift 端口（通常 `10000`）而非 HTTP WebUI 端口；
  2. 核对集群开启的认证模式（`NOSASL`、`PLAIN`、`LDAP` 或 `Kerberos`）；Data.O.G 通过内置无依赖 `InceptorThriftClient` 连接，需在连接档案中选择与服务端匹配的认证方式并配置正确用户名/密码及库名。
