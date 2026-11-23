# 星环 Inceptor 9.3 / 9.4 (TDH 9.x & ArgoDB) SQL 语法与存储引擎指南

> 适用版本：Transwarp Inceptor 9.3 / 9.4（TDH 9.x & ArgoDB） | 方言标识：`INCEPTOR` | 协议通道：HiveServer2 Thrift (`10000`) / Quark SQL 引擎

## 1. Quark SQL 编译器与多方言兼容特性

Transwarp Inceptor 9.x 内置 **Quark** SQL 编译与分布式执行引擎：

- 全面兼容 **ANSI SQL-99 / SQL-2003** 标准，提供高兼容性分析函数、窗口函数与 CTE (`WITH ...`)。
- 原生支持 **Oracle / DB2 / Teradata** 常用语法扩展：
  - `PL/SQL` 存储过程、匿名块与自定义函数；
  - `CONNECT BY ... START WITH ...` 树形层次递归查询；
  - `NVL()`、`DECODE()`、`ADD_MONTHS()`、`TO_CHAR()`、`TO_DATE()` 等常用表达。
- **兼容性提示**：尽量使用标准 `ROW_NUMBER() OVER (ORDER BY ...)` 进行排序分页，避免在分布式环境下依赖单机 `ROWNUM` 隐式顺序。

## 2. 四类主流表存储格式选型（Holodesk / TORC / ORC / Parquet）

| 存储引擎 / 格式 | 建表核心语法 | 核心适用场景与注意事项 |
| :--- | :--- | :--- |
| **HOLODESK** | `STORED AS HOLODESK` | ArgoDB/Inceptor 主力高性能列存分析引擎，向量化高并发查询（受 `holodesk.default.storage.format` 控制） |
| **事务 ORC (TORC)** | `STORED AS ORC TBLPROPERTIES ('transactional'='true')` | 支持 ACID 行级 `INSERT` / `UPDATE` / `DELETE` / `MERGE INTO`；通常需配合 `CLUSTERED BY (...) INTO N BUCKETS` |
| **普通 ORC** | `STORED AS ORC` | 大容量只追加（Append-Only）离线数仓表，压缩率高但不支持 `UPDATE`/`DELETE` |
| **Parquet / TextFile** | `STORED AS PARQUET` / `TEXTFILE` | 跨生态数据交换、外部表挂载与原始数据落盘接入 |

```sql
-- 示例 1：支持 ACID 事务的 TORC 分桶分区表
CREATE TABLE dwd.acct_balance_torc (
    acct_id     STRING,
    cust_id     STRING,
    balance     DECIMAL(18, 2),
    updated_at  TIMESTAMP
)
PARTITIONED BY (dt STRING)
CLUSTERED BY (acct_id) INTO 16 BUCKETS
STORED AS ORC
TBLPROPERTIES ('transactional' = 'true');

-- 示例 2：ArgoDB / Inceptor 高性能 Holodesk 分析表
CREATE TABLE dm.sales_summary_holo (
    sale_dt     DATE,
    org_code    STRING,
    amt         DECIMAL(18, 2),
    order_cnt   BIGINT
)
PARTITIONED BY RANGE (sale_dt) (
    PARTITION p202601 VALUES LESS THAN (DATE '2026-02-01'),
    PARTITION pmax    VALUES LESS THAN (MAXVALUE)
)
STORED AS HOLODESK;
```

## 3. 库表元数据探查与常用方言细节

在 Data.O.G AI 助手或客户端执行探查时，优先使用轻量元数据命令：

```sql
-- 切换库与查看表结构（只读）
USE dwd;
SHOW TABLES LIKE '*acct*';
DESCRIBE FORMATTED dwd.acct_balance_torc;
SHOW PARTITIONS dwd.acct_balance_torc;
```

**常用 SQL 编写注意**：
1. 保留字列名（如 `date`、`level`、`comment`、`timestamp`、`key`）务必使用反引号 `` `date` `` 包裹，避免触发 `ParseException`。
2. 金额与精确统计字段优先使用 `DECIMAL(p, s)` 而非 `DOUBLE`，避免浮点舍入误差。
3. 事务表在非事务会话或非 `transactional=true` 表上执行 `UPDATE`/`DELETE`/`MERGE` 会直接被语义检查拦截。
