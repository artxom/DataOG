"""Data.O.G 基于字段变更历史的 SQL 脚本自动调整（确定性引擎 + 轻量模型手术刀增强）全面测试."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from typing import Any, Dict, List
from unittest import mock

from app import create_app
from core.platform.context import PlatformContext
from modules.standard_migrator.service import StandardMigratorService
from modules.standard_migrator.sql_rewrite_llm import enhance_sql_rewrite_with_llm
from modules.standard_migrator.sql_rewriter import (
    ChangeRegistry,
    RewriteOptions,
    apply_patches,
    rewrite_sql_deterministic,
)
from tests.test_modules_and_api import TestClient


def _sample_registry(table_metadata_cols=None) -> ChangeRegistry:
    return ChangeRegistry.from_effective_mappings(
        [
            {
                "connection_id": "conn-dws",
                "connection_name": "数仓DWS",
                "db_type": "DWS",
                "schema_name": "dwd",
                "table_name": "t_cust",
                "original_col_name": "old_cust",
                "current_col_name": "cust_no",
                "historical_names": ["old_cust", "mid_cust"],
                "original_col_type": "varchar(32)",
                "current_col_type": "varchar(64)",
                "original_col_comment": "旧客户号",
                "current_col_comment": "标准客户编号",
                "name_changed": True,
                "type_changed": True,
                "comment_changed": True,
                "highest_risk_level": "L1",
                "highest_risk_rank": 1,
                "std_code": "STD_CUST_NO",
                "change_ids": [1, 2],
            },
            {
                "connection_id": "conn-dws",
                "connection_name": "数仓DWS",
                "db_type": "DWS",
                "schema_name": "dwd",
                "table_name": "t_cust",
                "original_col_name": "amt_str",
                "current_col_name": "tx_amt",
                "original_col_type": "varchar(50)",
                "current_col_type": "numeric(18,2)",
                "name_changed": True,
                "type_changed": True,
                "comment_changed": False,
                "highest_risk_level": "L1",
                "highest_risk_rank": 1,
                "std_code": "STD_TX_AMT",
                "change_ids": [3],
            },
            {
                "connection_id": "conn-dws",
                "connection_name": "数仓DWS",
                "db_type": "DWS",
                "schema_name": "dwd",
                "table_name": "t_cust",
                "original_col_name": "rate_val",
                "current_col_name": "rate_val",
                "original_col_type": "numeric(18,4)",
                "current_col_type": "numeric(16,2)",
                "name_changed": False,
                "type_changed": True,
                "comment_changed": False,
                "highest_risk_level": "L3",
                "highest_risk_rank": 3,
                "std_code": "STD_RATE",
                "change_ids": [4],
            },
        ],
        table_metadata_cols=table_metadata_cols,
    )


class TestSqlScriptRewriter(unittest.TestCase):
    def test_deterministic_scope_precision_cte_dml_ddl_and_idempotency(self) -> None:
        """验证别名隔离、字符串/注释免疫、引号保留、CTE 透传与保留别名、UNION、DML/DDL、PL/SQL 变量保护、列互换及幂等性."""
        reg = _sample_registry()

        # 1. 复杂查询：同列名未改表免疫、注释与字面量免疫、引号风格保留、显式别名保护、多层 CTE 透传
        sql = """-- old_cust 注释绝不能改写
/* amt_str 块注释也不改写 */
WITH c1 AS (
  SELECT
    a.old_cust,
    a."amt_str",
    a.old_cust AS keep_alias,
    b.old_cust AS other_cust,
    'old_cust' AS lit_val
  FROM dwd.t_cust a
  LEFT JOIN dwd.t_other b ON a.old_cust = b.old_cust
),
c2 AS (
  SELECT c1.old_cust, c1."amt_str"
  FROM c1
)
SELECT c2.old_cust, c2."amt_str", c2.keep_alias
FROM c2
WHERE c2.old_cust IS NOT NULL;
"""
        res = rewrite_sql_deterministic(sql, reg, RewriteOptions(cte_alias_policy="propagate"))
        out = res["rewritten_sql"]
        self.assertIn("-- old_cust 注释绝不能改写", out)
        self.assertIn("/* amt_str 块注释也不改写 */", out)
        self.assertIn("'old_cust' AS lit_val", out)
        self.assertIn("b.old_cust AS other_cust", out)
        self.assertIn("ON a.cust_no = b.old_cust", out)
        self.assertIn('a."tx_amt"', out)
        self.assertIn("a.cust_no AS keep_alias", out)
        self.assertIn("SELECT c1.cust_no, c1.\"tx_amt\"", out)
        self.assertIn("SELECT c2.cust_no, c2.\"tx_amt\", c2.keep_alias", out)
        self.assertIn("WHERE c2.cust_no IS NOT NULL", out)

        # 幂等性校验：二次运行必须 0 改动
        res2 = rewrite_sql_deterministic(out, reg, RewriteOptions(cte_alias_policy="propagate"))
        self.assertEqual(res2["rewritten_sql"], out)
        self.assertEqual(len(res2["patches"]), 0)

        # 2. CTE 保留旧列别名模式 (preserve_alias)
        res_keep = rewrite_sql_deterministic(sql, reg, RewriteOptions(cte_alias_policy="preserve_alias"))
        out_keep = res_keep["rewritten_sql"]
        self.assertIn("a.cust_no AS old_cust", out_keep)
        self.assertIn('a."tx_amt" AS "amt_str"', out_keep)
        # 下游 c1.old_cust 应保持不动
        self.assertIn("SELECT c1.old_cust", out_keep)

        # 3. 历史中间名兼容 (old_cust -> mid_cust -> cust_no)
        mid_sql = "SELECT t.mid_cust FROM dwd.t_cust t WHERE t.mid_cust = 'A';"
        mid_res = rewrite_sql_deterministic(mid_sql, reg)
        self.assertEqual(
            mid_res["rewritten_sql"],
            "SELECT t.cust_no FROM dwd.t_cust t WHERE t.cust_no = 'A';",
        )

        # 4. UNION ALL 同名别名跨分支隔离 + 显式 CTE 列清单保护
        union_sql = """WITH u(x_col) AS (
  SELECT a.old_cust FROM dwd.t_cust a
  UNION ALL
  SELECT a.old_cust FROM dwd.t_unrelated a
)
SELECT u.x_col FROM u;"""
        u_res = rewrite_sql_deterministic(union_sql, reg)
        self.assertIn("SELECT a.cust_no FROM dwd.t_cust a", u_res["rewritten_sql"])
        self.assertIn("SELECT a.old_cust FROM dwd.t_unrelated a", u_res["rewritten_sql"])
        self.assertIn("SELECT u.x_col FROM u;", u_res["rewritten_sql"])

        # 5. INSERT / UPDATE / DELETE / MERGE / CREATE TABLE DDL / INDEX / COMMENT
        dml_ddl = """INSERT INTO dwd.t_cust (old_cust, amt_str, rate_val)
SELECT s.cid, s.amt, s.rt FROM dwd.t_stage s;

UPDATE dwd.t_cust SET old_cust = 'X', rate_val = 1.23 WHERE old_cust = 'Y';

DELETE FROM dwd.t_cust WHERE old_cust = 'Z';

MERGE INTO dwd.t_cust t
USING dwd.t_stage s ON t.old_cust = s.cid
WHEN MATCHED THEN UPDATE SET t.amt_str = s.amt
WHEN NOT MATCHED THEN INSERT (old_cust, amt_str) VALUES (s.cid, s.amt);

CREATE TABLE dwd.t_cust (
  old_cust varchar(32) NOT NULL,
  amt_str varchar(50),
  rate_val numeric(18,4)
) DISTRIBUTE BY HASH(old_cust) PARTITION BY RANGE(rate_val);

CREATE INDEX idx_cust ON dwd.t_cust (old_cust);
COMMENT ON COLUMN dwd.t_cust.old_cust IS '旧客户号';
"""
        d_res = rewrite_sql_deterministic(
            dml_ddl, reg, RewriteOptions(rewrite_ddl_types=True, rewrite_comments=True)
        )
        d_out = d_res["rewritten_sql"]
        self.assertIn("INSERT INTO dwd.t_cust (cust_no, tx_amt, rate_val)", d_out)
        self.assertIn("UPDATE dwd.t_cust SET cust_no = 'X', rate_val = 1.23 WHERE cust_no = 'Y';", d_out)
        self.assertIn("DELETE FROM dwd.t_cust WHERE cust_no = 'Z';", d_out)
        self.assertIn("ON t.cust_no = s.cid", d_out)
        self.assertIn("UPDATE SET t.tx_amt = s.amt", d_out)
        self.assertIn("INSERT (cust_no, tx_amt) VALUES (s.cid, s.amt);", d_out)
        self.assertIn("cust_no varchar(64) NOT NULL,", d_out)
        self.assertIn("tx_amt numeric(18,2),", d_out)
        self.assertIn("rate_val numeric(16,2)", d_out)
        self.assertIn("DISTRIBUTE BY HASH(cust_no)", d_out)
        self.assertIn("CREATE INDEX idx_cust ON dwd.t_cust (cust_no);", d_out)
        self.assertIn("COMMENT ON COLUMN dwd.t_cust.cust_no IS '标准客户编号';", d_out)

        # 6. PL/pgSQL 存储过程 $$...$$ 解包 + 局部变量保护 + %TYPE 同步
        proc_sql = """CREATE OR REPLACE PROCEDURE dwd.sp_demo()
LANGUAGE plpgsql AS $$
DECLARE
  old_cust varchar(32);
  v_ref dwd.t_cust.old_cust%TYPE;
BEGIN
  SELECT a.old_cust INTO old_cust FROM dwd.t_cust a WHERE a.old_cust = old_cust;
END;
$$;"""
        p_res = rewrite_sql_deterministic(proc_sql, reg)
        p_out = p_res["rewritten_sql"]
        self.assertIn("v_ref dwd.t_cust.cust_no%TYPE;", p_out)
        self.assertIn("SELECT a.cust_no INTO old_cust FROM dwd.t_cust a WHERE a.cust_no = old_cust;", p_out)

        # 7. 列互换（Swap: col_a <-> col_b）同时无冲突替换
        swap_reg = ChangeRegistry.from_effective_mappings(
            [
                {
                    "schema_name": "dwd",
                    "table_name": "t_swap",
                    "original_col_name": "col_a",
                    "current_col_name": "col_b",
                    "name_changed": True,
                    "highest_risk_level": "L1",
                },
                {
                    "schema_name": "dwd",
                    "table_name": "t_swap",
                    "original_col_name": "col_b",
                    "current_col_name": "col_a",
                    "name_changed": True,
                    "highest_risk_level": "L1",
                },
            ]
        )
        swap_res = rewrite_sql_deterministic(
            "SELECT t.col_a, t.col_b FROM dwd.t_swap t WHERE t.col_a > t.col_b;", swap_reg
        )
        self.assertEqual(
            swap_res["rewritten_sql"],
            "SELECT t.col_b, t.col_a FROM dwd.t_swap t WHERE t.col_b > t.col_a;",
        )

        # 8. 元数据缓存辅助多表未限定字段消歧
        meta_clean = _sample_registry(
            table_metadata_cols={
                ("dwd", "t_cust"): {"cust_no", "tx_amt", "branch_id"},
                ("dwd", "t_branch"): {"branch_id", "branch_name"},
            }
        )
        join_unq = "SELECT old_cust, branch_name FROM dwd.t_cust a JOIN dwd.t_branch b ON a.branch_id = b.branch_id;"
        r_clean = rewrite_sql_deterministic(join_unq, meta_clean)
        self.assertEqual(len(r_clean["patches"]), 1)
        self.assertEqual(r_clean["patches"][0].confidence, "HIGH")
        self.assertTrue(r_clean["patches"][0].selected)

        meta_ambig = _sample_registry(
            table_metadata_cols={
                ("dwd", "t_cust"): {"cust_no", "tx_amt", "branch_id"},
                ("dwd", "t_branch"): {"branch_id", "old_cust"},
            }
        )
        r_ambig = rewrite_sql_deterministic(join_unq, meta_ambig)
        self.assertEqual(len(r_ambig["patches"]), 0)
        self.assertTrue(any(a.category == "AMBIGUOUS_COLUMN" for a in r_ambig["advisories"]))

        # 9. 多 Schema 同名表歧义拦截 vs SET search_path 动态消歧 + timestamp without time zone DDL 类型替换 + 相关子查询
        multi_sch_reg = ChangeRegistry.from_effective_mappings(
            [
                {
                    "schema_name": "dwd",
                    "table_name": "t_dup",
                    "original_col_name": "c_ts",
                    "current_col_name": "biz_dt",
                    "original_col_type": "timestamp without time zone",
                    "current_col_type": "date",
                    "name_changed": True,
                    "type_changed": True,
                    "highest_risk_level": "L1",
                },
                {
                    "schema_name": "ods",
                    "table_name": "t_dup",
                    "original_col_name": "c_ts",
                    "current_col_name": "ods_dt",
                    "name_changed": True,
                    "highest_risk_level": "L1",
                },
            ]
        )
        amb_tbl_res = rewrite_sql_deterministic("SELECT t.c_ts FROM t_dup t;", multi_sch_reg)
        self.assertEqual(len(amb_tbl_res["patches"]), 0)
        self.assertTrue(any(a.category == "AMBIGUOUS_TABLE" for a in amb_tbl_res["advisories"]))

        sp_sql = """SET search_path TO dwd;
CREATE TABLE t_dup (c_ts timestamp without time zone NOT NULL);
SELECT a.c_ts FROM t_dup a WHERE EXISTS (SELECT 1 FROM dwd.t_log l WHERE l.dt = a.c_ts);"""
        sp_res = rewrite_sql_deterministic(sp_sql, multi_sch_reg)
        self.assertIn("CREATE TABLE t_dup (biz_dt date NOT NULL);", sp_res["rewritten_sql"])
        self.assertIn("WHERE l.dt = a.biz_dt", sp_res["rewritten_sql"])

    def test_type_impact_radar_using_conflict_and_dynamic_sql(self) -> None:
        """验证 L2 类型冲突雷达、L3 缩精度写入风险、JOIN USING 单侧改名冲突及动态 SQL 雷达."""
        reg = _sample_registry()
        sql = """SELECT
  TRIM(a.amt_str) AS t1,
  NVL(a.amt_str, '') AS t2,
  a.amt_str::varchar AS t3
FROM dwd.t_cust a
JOIN dwd.t_other b USING (old_cust)
WHERE a.amt_str = '100'
  AND a.amt_str IN ('1', '2')
  AND a.amt_str LIKE '9%';

INSERT INTO dwd.t_cust (old_cust, rate_val) VALUES ('C1', 123.4567);

EXECUTE IMMEDIATE 'SELECT old_cust FROM dwd.t_cust WHERE amt_str = 1';
"""
        res = rewrite_sql_deterministic(sql, reg)
        cats = {a.category for a in res["advisories"]}
        self.assertIn("TYPE_FUNCTION_MISMATCH", cats)
        self.assertIn("TYPE_COALESCE_MISMATCH", cats)
        self.assertIn("TYPE_LITERAL_MISMATCH", cats)
        self.assertIn("TYPE_CAST_REVIEW", cats)
        self.assertIn("USING_CLAUSE_CONFLICT", cats)
        self.assertIn("PRECISION_SHRINK_WRITE", cats)
        self.assertIn("DYNAMIC_SQL_HIT", cats)
        # 动态 SQL 字符串内部绝不能被规则引擎盲改
        self.assertIn("'SELECT old_cust FROM dwd.t_cust WHERE amt_str = 1'", res["rewritten_sql"])

    def test_hybrid_llm_surgical_enhancer_and_five_anti_hallucination_gates(self) -> None:
        """验证轻量模型（如 DeepSeek-v4-flash）0-Token 短路、合法微切片补丁采纳与 5 道反幻觉拦截."""
        reg = _sample_registry()

        class _FakeLLMHub:
            def __init__(self, response_payload: Dict[str, Any], should_fail: bool = False) -> None:
                self.calls: List[Dict[str, Any]] = []
                self.response_payload = response_payload
                self.should_fail = should_fail

            def resolve_provider_and_model(self, provider_id="", model="", require_ready=True):
                return (
                    type("Prof", (), {"provider_id": provider_id or "deepseek"})(),
                    model or "DeepSeek-v4-flash",
                )

            def chat_completion(self, **kwargs):
                self.calls.append(kwargs)
                if self.should_fail:
                    raise RuntimeError("模拟网络超时")
                return {
                    "content": json.dumps(self.response_payload, ensure_ascii=False),
                    "usage": {"prompt_tokens": 320, "completion_tokens": 110, "total_tokens": 430},
                }

        # 1. 无歧义/无类型冲突脚本：自动短路，0 Token，不调用模型
        clean_sql = "SELECT a.old_cust FROM dwd.t_cust a;"
        det_clean = rewrite_sql_deterministic(clean_sql, reg)
        hub_unused = _FakeLLMHub({"patches": []})
        hyb_clean = enhance_sql_rewrite_with_llm(
            original_sql=clean_sql,
            det_result=det_clean,
            registry=reg,
            llm_hub=hub_unused,
            model="DeepSeek-v4-flash",
        )
        self.assertFalse(hyb_clean["llm_meta"]["used"])
        self.assertEqual(len(hub_unused.calls), 0)
        self.assertIn("0 Token", hyb_clean["llm_meta"]["skipped_reason"])

        # 2. 含 L2 类型冲突 + 动态 SQL + 恶意幻觉测试
        risky_sql = (
            "SELECT\n"
            "  a.old_cust,\n"
            "  NVL(a.amt_str, '') AS safe_amt\n"
            "FROM dwd.t_cust a\n"
            "WHERE a.amt_str = '0';\n"
            "EXECUTE IMMEDIATE 'SELECT old_cust FROM dwd.t_cust';\n"
        )
        det_risky = rewrite_sql_deterministic(risky_sql, reg)
        # 构造包含 2 条合法手术刀补丁 + 4 条恶意幻觉补丁的响应
        mock_resp = {
            "patches": [
                # 合法 #1: TYPE_ADAPT（在 Phase 1 已将 amt_str -> tx_amt 的基础上修复 '' -> 0）
                {
                    "line": 3,
                    "original_snippet": "NVL(a.tx_amt, '')",
                    "rewritten_snippet": "NVL(a.tx_amt, 0)",
                    "category": "TYPE_ADAPT",
                    "confidence": "HIGH",
                    "reason": "tx_amt 已由 varchar 改为 numeric(18,2)，空字符串默认值需改为 0",
                    "mapping_ref": "dwd.t_cust.tx_amt",
                },
                # 合法 #2: DYNAMIC_SQL（使用台账白名单内真实目标列 cust_no）
                {
                    "line": 6,
                    "original_snippet": "'SELECT old_cust FROM dwd.t_cust'",
                    "rewritten_snippet": "'SELECT cust_no FROM dwd.t_cust'",
                    "category": "DYNAMIC_SQL",
                    "confidence": "HIGH",
                    "reason": "同步更新动态 SQL 字符串内的已改名字段",
                    "mapping_ref": "dwd.t_cust.cust_no",
                },
                # 拦截 #1: 幻觉列名（不在变更台账中）
                {
                    "line": 6,
                    "original_snippet": "FROM dwd.t_cust",
                    "rewritten_snippet": "FROM dwd.t_cust_fake_col",
                    "category": "DYNAMIC_SQL",
                    "confidence": "HIGH",
                    "reason": "幻觉列测试",
                },
                # 拦截 #2: 试图撤回规则引擎已改好的新列名回到旧列名 old_cust
                {
                    "line": 2,
                    "original_snippet": "a.cust_no",
                    "rewritten_snippet": "a.old_cust",
                    "category": "TYPE_ADAPT",
                    "confidence": "HIGH",
                    "reason": "错误回退测试",
                },
                # 拦截 #3: 语法不平衡（缺右括号）
                {
                    "line": 5,
                    "original_snippet": "a.tx_amt = '0'",
                    "rewritten_snippet": "(a.tx_amt = 0",
                    "category": "TYPE_ADAPT",
                    "confidence": "HIGH",
                    "reason": "括号不平衡测试",
                },
                # 拦截 #4: 原片段在对应行根本不存在
                {
                    "line": 5,
                    "original_snippet": "NON_EXISTENT_SNIPPET",
                    "rewritten_snippet": "SOMETHING_ELSE",
                    "category": "TYPE_ADAPT",
                    "confidence": "HIGH",
                    "reason": "不存在片段测试",
                },
            ],
            "notes": ["请核查下游报表是否依赖 safe_amt 为数值型。"],
        }
        hub_ok = _FakeLLMHub(mock_resp)
        hyb_res = enhance_sql_rewrite_with_llm(
            original_sql=risky_sql,
            det_result=det_risky,
            registry=reg,
            llm_hub=hub_ok,
            provider_id="deepseek",
            model="DeepSeek-v4-flash",
        )
        meta = hyb_res["llm_meta"]
        self.assertTrue(meta["used"])
        self.assertEqual(meta["model"], "DeepSeek-v4-flash")
        self.assertEqual(meta["total_tokens"], 430)
        self.assertEqual(meta["accepted_patches"], 2)
        self.assertEqual(meta["rejected_patches"], 4)
        self.assertIn("NVL(a.tx_amt, 0)", hyb_res["rewritten_sql"])
        self.assertIn("'SELECT cust_no FROM dwd.t_cust'", hyb_res["rewritten_sql"])
        self.assertIn("a.cust_no,", hyb_res["rewritten_sql"])

        # 3. 验证反选某条 AI 补丁后 apply_patches 可精准还原 Phase 1 输出
        only_det_ids = {p.patch_id for p in hyb_res["patches"] if p.source == "DETERMINISTIC"}
        reverted_to_det = apply_patches(risky_sql, hyb_res["patches"], only_det_ids)
        self.assertIn("NVL(a.tx_amt, '')", reverted_to_det)
        self.assertIn("'SELECT old_cust FROM dwd.t_cust'", reverted_to_det)

        # 4. 大模型网络超时异常时必须优雅降级，保留 100% 确定性改写结果
        hub_fail = _FakeLLMHub({}, should_fail=True)
        hyb_fail = enhance_sql_rewrite_with_llm(
            original_sql=risky_sql,
            det_result=det_risky,
            registry=reg,
            llm_hub=hub_fail,
            model="DeepSeek-v4-flash",
        )
        self.assertFalse(hyb_fail["llm_meta"]["used"])
        self.assertIn("模拟网络超时", hyb_fail["llm_meta"]["error"])
        self.assertIn("a.cust_no", hyb_fail["rewritten_sql"])
        self.assertIn("NVL(a.tx_amt, '')", hyb_fail["rewritten_sql"])

    def test_end_to_end_api_workspace_checkpoint_upload_and_zip_export(self) -> None:
        """端到端测试工作区变更日志消费、Checkpoint 起点过滤、离线 JSON 台账、多文件上传及 ZIP 审计包导出."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(private_root=tmp)
            ctx: PlatformContext = app.state.platform_ctx
            try:
                client = TestClient(app)
                svc = StandardMigratorService(ctx)

                # 记录变更 #1（早于 Checkpoint）
                rec1 = svc._record_committed_change(
                    {
                        "schema_name": "dwd",
                        "table_name": "t_acct",
                        "ordinal_position": 1,
                        "before_col_name": "legacy_acct",
                        "before_col_type": "varchar(32)",
                        "before_col_comment": "",
                        "after_col_name": "acct_no",
                        "after_col_type": "varchar(64)",
                        "after_col_comment": "账号",
                        "strategy": "DIRECT_ALTER",
                    },
                    conn_id="c-1",
                    conn_name="DWS生产",
                    db_type="DWS",
                )
                # 创建 Checkpoint
                cp = svc.create_checkpoint("阶段一完成点", connection_id="c-1")["checkpoint"]
                # 记录变更 #2（晚于 Checkpoint，且链式修改 acct_no -> std_acct_no）
                svc._record_committed_change(
                    {
                        "schema_name": "dwd",
                        "table_name": "t_acct",
                        "ordinal_position": 1,
                        "before_col_name": "acct_no",
                        "before_col_type": "varchar(64)",
                        "before_col_comment": "账号",
                        "after_col_name": "std_acct_no",
                        "after_col_type": "varchar(64)",
                        "after_col_comment": "标准账号",
                        "strategy": "DIRECT_ALTER",
                    },
                    conn_id="c-1",
                    conn_name="DWS生产",
                    db_type="DWS",
                )
                # 记录一条已回滚变更（必须被 SQL 改写引擎自动忽略！）
                rb_rec = svc._record_committed_change(
                    {
                        "schema_name": "dwd",
                        "table_name": "t_acct",
                        "ordinal_position": 2,
                        "before_col_name": "keep_me",
                        "before_col_type": "varchar(32)",
                        "before_col_comment": "",
                        "after_col_name": "should_not_apply",
                        "after_col_type": "varchar(32)",
                        "after_col_comment": "",
                        "strategy": "DIRECT_ALTER",
                    },
                    conn_id="c-1",
                    conn_name="DWS生产",
                    db_type="DWS",
                )
                with ctx.store.module_db(scope="workspace") as conn:
                    conn.execute(
                        "UPDATE migration_change_logs SET status = 'ROLLED_BACK' WHERE id = ?",
                        (rb_rec["id"],),
                    )

                # 1. GET /sql-rewrite/context：全量折叠应为 legacy_acct -> std_acct_no，且不含已回滚列
                ctx_resp = client.get("/api/modules/standard_migrator/sql-rewrite/context")
                self.assertEqual(ctx_resp.status_code, 200)
                ctx_body = ctx_resp.json()
                self.assertTrue(ctx_body["ok"])
                self.assertEqual(ctx_body["summary"]["mapping_count"], 1)
                m0 = ctx_body["effective_mappings"][0]
                self.assertEqual(m0["original_col_name"], "legacy_acct")
                self.assertEqual(m0["current_col_name"], "std_acct_no")

                # 按 since_checkpoint_id 过滤，仅应包含 acct_no -> std_acct_no
                ctx_cp = client.get(
                    f"/api/modules/standard_migrator/sql-rewrite/context?since_checkpoint_id={cp['id']}"
                ).json()
                self.assertEqual(ctx_cp["effective_mappings"][0]["original_col_name"], "acct_no")

                # 2. POST /sql-rewrite/analyze（同时测试遗留名 legacy_acct 与已回滚列 keep_me 保持不动）
                analyze_resp = client.post(
                    "/api/modules/standard_migrator/sql-rewrite/analyze",
                    json={
                        "filename": "job_acct.sql",
                        "sql_text": "SELECT a.legacy_acct, a.keep_me FROM dwd.t_acct a WHERE a.acct_no IS NOT NULL;",
                        "mode": "deterministic",
                    },
                )
                self.assertEqual(analyze_resp.status_code, 200)
                an_body = analyze_resp.json()
                self.assertTrue(an_body["ok"])
                f0 = an_body["files"][0]
                self.assertEqual(
                    f0["rewritten_sql"],
                    "SELECT a.std_acct_no, a.keep_me FROM dwd.t_acct a WHERE a.std_acct_no IS NOT NULL;",
                )
                self.assertTrue(f0["changed"])
                self.assertIn("-SELECT a.legacy_acct", f0["unified_diff"])
                self.assertIn("+SELECT a.std_acct_no", f0["unified_diff"])

                # 3. 导出 JSON 底座后再作为离线 JSON 传回 /sql-rewrite/upload 单文件上传 & /sql-rewrite/analyze 批量改写
                exported_json = client.get("/api/modules/standard_migrator/changelog/export.json").text
                up_resp = client.post(
                    "/api/modules/standard_migrator/sql-rewrite/upload",
                    files={
                        "files": ("s1.sql", b"SELECT t.legacy_acct FROM dwd.t_acct t;", "text/plain"),
                        "changelog_file": ("base.json", exported_json.encode("utf-8"), "application/json"),
                    },
                    data={"mode": "deterministic"},
                )
                self.assertEqual(up_resp.status_code, 200)
                up_body = up_resp.json()
                self.assertEqual(up_body["source"]["source_type"], "EXTERNAL_JSON")
                self.assertEqual(up_body["aggregate"]["total_files"], 1)
                self.assertEqual(up_body["aggregate"]["changed_files"], 1)
                self.assertIn("t.std_acct_no", up_body["files"][0]["rewritten_sql"])

                # 多文件批量分析改写
                batch_resp = client.post(
                    "/api/modules/standard_migrator/sql-rewrite/analyze",
                    json={
                        "scripts": [
                            {"filename": "s1.sql", "content": "SELECT t.legacy_acct FROM dwd.t_acct t;"},
                            {"filename": "s2.sql", "content": "SELECT t.other_col FROM dwd.t_other t;"},
                        ],
                        "external_changelog_json": exported_json,
                        "mode": "deterministic",
                    },
                )
                self.assertEqual(batch_resp.status_code, 200)
                batch_body = batch_resp.json()
                self.assertEqual(batch_body["aggregate"]["total_files"], 2)
                self.assertEqual(batch_body["aggregate"]["changed_files"], 1)

                # 4. POST /sql-rewrite/export-zip：验证 ZIP 包含改写脚本、diff 与双审计报告
                zip_resp = client.post(
                    "/api/modules/standard_migrator/sql-rewrite/export-zip",
                    json={
                        "files": [
                            {
                                "filename": f["filename"],
                                "rewritten_sql": f["rewritten_sql"],
                                "unified_diff": f["unified_diff"],
                                "changed": f["changed"],
                                "applied_patch_count": f["stats"]["selected_patches"],
                                "advisory_count": f["stats"]["advisory_count"],
                            }
                            for f in batch_body["files"]
                        ]
                    },
                )
                self.assertEqual(zip_resp.status_code, 200)
                self.assertEqual(zip_resp.headers.get("x-content-type-options"), "nosniff")
                with zipfile.ZipFile(io.BytesIO(zip_resp.content), "r") as zf:
                    names = set(zf.namelist())
                    self.assertIn("rewritten/s1.sql", names)
                    self.assertIn("rewritten/s2.sql", names)
                    self.assertIn("diffs/s1.sql.diff", names)
                    self.assertIn("REWRITE_REPORT.md", names)
                    self.assertIn("REWRITE_AUDIT.json", names)
                    self.assertIn("t.std_acct_no", zf.read("rewritten/s1.sql").decode("utf-8"))
            finally:
                ctx.close()

    def test_frontend_sql_rewrite_panel_security_syntax_and_node_runtime(self) -> None:
        """校验前端 SQL 改写模块零 XSS、语法合规，并在 Node.js 中真实执行前端补丁合成、Diff 计算与视图切换."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_files = [
            os.path.join(root_dir, "static", "js", "api", "migrator_api.js"),
            os.path.join(root_dir, "static", "js", "views", "standard_migrator", "sql_rewrite_panel.js"),
            os.path.join(root_dir, "static", "js", "views", "standard_migrator_view.js"),
        ]
        node_bin = shutil.which("node")
        for jf in js_files:
            with open(jf, "r", encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("innerHTML", src)
            self.assertNotIn("outerHTML", src)
            self.assertNotIn("insertAdjacentHTML", src)
            self.assertNotRegex(src, r"\bwindow\.(?:alert|confirm|prompt)\s*\(")
            if node_bin:
                proc = subprocess.run(
                    [node_bin, "--check", jf],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, f"{jf} 语法校验失败: {proc.stderr}")

        if not node_bin:
            return

        node_script = r"""
import path from "node:path";
import { pathToFileURL } from "node:url";

class MockNode {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = {};
    this.dataset = {};
    this.className = "";
    this.value = "";
    this.hidden = false;
    const self = this;
    this.classList = {
      add(...cs) { self.className = [...new Set([...(self.className||"").split(/\s+/).filter(Boolean), ...cs])].join(" "); },
      remove(...cs) { const s = new Set(cs); self.className = (self.className||"").split(/\s+/).filter(x => x && !s.has(x)).join(" "); },
      toggle(c, f) { const has = (self.className||"").split(/\s+/).includes(c); const on = f === undefined ? !has : Boolean(f); if (on) this.add(c); else this.remove(c); },
      contains(c) { return (self.className||"").split(/\s+/).includes(c); },
    };
  }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...ns) { this.children = ns.filter(Boolean); }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k) ?? null; }
  addEventListener(e, fn) { (this.listeners.get(e) || this.listeners.set(e, []).get(e)).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  remove() {}
}

globalThis.Node = MockNode;
globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, innerWidth: 1440, innerHeight: 900 };
globalThis.document = {
  head: new MockNode("head"),
  body: new MockNode("body"),
  createElement: (tag) => new MockNode(tag),
  createElementNS: (_ns, tag) => new MockNode(tag),
  createTextNode: (t) => Object.assign(new MockNode("#text"), { textContent: String(t) }),
  getElementById: () => null,
  querySelector: () => null,
  addEventListener() {},
  removeEventListener() {},
};

const root = process.argv[1];
const imp = (rel) => import(pathToFileURL(path.join(root, rel)).href);

const { applySelectedPatchesClient, applySelectedPatchesDetailed, computeLineDiffRows, decodeFileWithFallback } = await imp(
  "static/js/views/standard_migrator/sql_rewrite_panel.js"
);
const { StandardMigratorView } = await imp("static/js/views/standard_migrator_view.js");

const orig = "SELECT a.old_cust, NVL(a.amt_str, '') FROM dwd.t_cust a;";
const patches = [
  { patch_id: "p_1", phase: 1, line: 1, col: 10, start: 9, end: 17, original_text: "old_cust", replacement_text: "cust_no" },
  { patch_id: "p_2", phase: 1, line: 1, col: 26, start: 25, end: 32, original_text: "amt_str", replacement_text: "tx_amt" },
  { patch_id: "p_llm_3", phase: 2, line: 1, col: 20, start: 19, end: 37, original_text: "NVL(a.tx_amt, '')", replacement_text: "NVL(a.tx_amt, 0)" },
];

const outDetOnly = applySelectedPatchesClient(orig, patches, new Set(["p_1", "p_2"]));
if (outDetOnly !== "SELECT a.cust_no, NVL(a.tx_amt, '') FROM dwd.t_cust a;") {
  throw new Error(`Unexpected Phase-1 output: ${outDetOnly}`);
}

const outHybrid = applySelectedPatchesClient(orig, patches, new Set(["p_1", "p_2", "p_llm_3"]));
if (outHybrid !== "SELECT a.cust_no, NVL(a.tx_amt, 0) FROM dwd.t_cust a;") {
  throw new Error(`Unexpected Phase-2 output: ${outHybrid}`);
}

// Verify skippedPhase2Ids when prerequisite Phase-1 rename is unchecked
const detSkipped = applySelectedPatchesDetailed(orig, patches, new Set(["p_1", "p_llm_3"]));
if (!detSkipped.skippedPhase2Ids.has("p_llm_3") || detSkipped.appliedIds.size !== 1) {
  throw new Error(`Expected skippedPhase2Ids to track p_llm_3: ${JSON.stringify([...detSkipped.skippedPhase2Ids])}`);
}

// Verify Windows CRLF multi-line Phase-2 patching & clean diff + same-line column tie-breaking
const crlfOrig = "SELECT a.old_cust\r\nFROM dwd.t_cust a\r\nJOIN dwd.t_other b\r\nUSING (old_cust);\r\n";
const crlfPatches = [
  { patch_id: "p_1", phase: 1, line: 1, col: 10, start: 9, end: 17, original_text: "old_cust", replacement_text: "cust_no" },
  { patch_id: "p_llm_2", phase: 2, line: 3, col: 1, start: 38, end: 74, original_text: "JOIN dwd.t_other b\nUSING (old_cust)", replacement_text: "JOIN dwd.t_other b\nON a.cust_no = b.old_cust" },
];
const crlfOut = applySelectedPatchesClient(crlfOrig, crlfPatches, new Set(["p_1", "p_llm_2"]));
if (!crlfOut.includes("JOIN dwd.t_other b\r\nON a.cust_no = b.old_cust;")) {
  throw new Error(`CRLF Phase-2 patch failed: ${JSON.stringify(crlfOut)}`);
}

const sameLineOrig = "SELECT NVL(a.tx_amt, ''), NVL(a.tx_amt, '') FROM dwd.t_cust a;";
const sameLinePatches = [
  { patch_id: "l1", phase: 2, line: 1, col: 8, start: 7, end: 24, original_text: "NVL(a.tx_amt, '')", replacement_text: "NVL(a.tx_amt, 0)" },
  { patch_id: "l2", phase: 2, line: 1, col: 27, start: 26, end: 43, original_text: "NVL(a.tx_amt, '')", replacement_text: "NVL(a.tx_amt, 0)" },
];
const onlySecond = applySelectedPatchesClient(sameLineOrig, sameLinePatches, new Set(["l2"]));
if (onlySecond !== "SELECT NVL(a.tx_amt, ''), NVL(a.tx_amt, 0) FROM dwd.t_cust a;") {
  throw new Error(`Same-line col tie-breaker failed: ${onlySecond}`);
}

const diff = computeLineDiffRows(orig, outHybrid);
if (diff.changedLines !== 2 || !diff.unifiedText.includes("+SELECT a.cust_no, NVL(a.tx_amt, 0)")) {
  throw new Error(`Unexpected diff result: ${JSON.stringify(diff)}`);
}

const mv = new StandardMigratorView();
mv.subMode = "sql_rewrite";
// Verify raw changes-only offline JSON populates active mappings list
mv.sqlRewritePanel.externalJsonText = JSON.stringify({
  changes: [{ event_type: "MIGRATION", status: "APPLIED", schema_name: "dwd", table_name: "t_cust", before_col_name: "old_cust", after_col_name: "cust_no", risk_level: "L1" }]
});
if (mv.sqlRewritePanel._getActiveMappingsList().length !== 1) {
  throw new Error("Expected raw changes-only offline JSON to populate active mappings list");
}
mv.sqlRewritePanel.externalJsonText = "";
mv.render();
const hdr = mv.renderHeaderActions();
if (!hdr || mv.gridMount.children.length !== 1) {
  throw new Error("SqlRewritePanel failed to render inside StandardMigratorView");
}

// Verify GBK/GB18030 fallback decoding & UTF-8 BOM stripping
const utf8BomBytes = new Uint8Array([0xEF, 0xBB, 0xBF, ...Buffer.from("SELECT 1; -- 客户号", "utf-8")]);
const decodedUtf8 = await decodeFileWithFallback({ arrayBuffer: async () => utf8BomBytes.buffer });
if (decodedUtf8 !== "SELECT 1; -- 客户号") {
  throw new Error(`UTF-8 BOM decode failed: ${JSON.stringify(decodedUtf8)}`);
}
// GBK bytes for "-- 客户\nSELECT 1;" (客=0xBF 0xCD, 户=0xBB 0xA7)
const gbkBytes = new Uint8Array([0x2D, 0x2D, 0x20, 0xBF, 0xCD, 0xBB, 0xA7, 0x0A, 0x53, 0x45, 0x4C, 0x45, 0x43, 0x54, 0x20, 0x31, 0x3B]);
const decodedGbk = await decodeFileWithFallback({ arrayBuffer: async () => gbkBytes.buffer });
if (!decodedGbk.includes("客户") || !decodedGbk.includes("SELECT 1;")) {
  throw new Error(`GBK/GB18030 decode failed: ${JSON.stringify(decodedGbk)}`);
}

// Verify re-uploading non-first tab preserves active tab on that script & un-analyzed new tab returns null result
const panel = mv.sqlRewritePanel;
panel.scripts = [
  { id: "s1", filename: "a.sql", content: "SELECT 1;" },
  { id: "s2", filename: "b.sql", content: "SELECT 2;" },
];
panel.activeScriptId = "s1";
await panel._handleSqlFilesSelected([{ name: "B.sql", text: async () => "SELECT 22;" }]);
if (panel.activeScriptId !== "s2" || panel.scripts[1].content !== "SELECT 22;") {
  throw new Error(`Re-upload existing non-first tab failed: active=${panel.activeScriptId}, content=${panel.scripts[1].content}`);
}

panel.result = {
  files: [
    { filename: "a.sql", original_filename: "a.sql", original_sql: "SELECT 1;", rewritten_sql: "SELECT 1;", patches: [], advisories: [] },
  ],
};
panel.activeScriptId = "s2";
if (panel._getActiveResultFile() !== null) {
  throw new Error("Expected un-analyzed tab s2 NOT to inherit result from a.sql");
}

mv.destroy();
console.log("SQL_REWRITE_FRONTEND_OK");
"""
        proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", node_script, root_dir],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node 前端 SQL 改写面板运行验证失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("SQL_REWRITE_FRONTEND_OK", proc.stdout)

    def test_adversarial_sql_dialects_crlf_and_strict_llm_guardrails(self) -> None:
        """深度回归：PL/SQL FOR LOOP、局部作用域隔离、软关键字列、WITH RECURSIVE、UNION ORDER BY、UPSERT、CHECK/GENERATED、DESC JSON、多库冲突、CRLF 与 Gate 5 全类别反幻觉."""
        reg = ChangeRegistry.from_effective_mappings(
            [
                {
                    "schema_name": "dwd",
                    "table_name": "t_cust",
                    "original_col_name": "old_cust",
                    "current_col_name": "cust_no",
                    "historical_names": ["old_cust", "mid_cust"],
                    "original_col_type": "varchar(32)",
                    "current_col_type": "varchar(64)",
                    "name_changed": True,
                    "type_changed": True,
                    "highest_risk_level": "L1",
                    "highest_risk_rank": 1,
                },
                {
                    "schema_name": "dwd",
                    "table_name": "t_cust",
                    "original_col_name": "amt_str",
                    "current_col_name": "tx_amt",
                    "original_col_type": "varchar(50)",
                    "current_col_type": "numeric(18,2)",
                    "name_changed": True,
                    "type_changed": True,
                    "highest_risk_level": "L1",
                    "highest_risk_rank": 1,
                },
                {
                    "schema_name": "dwd",
                    "table_name": "t_cust",
                    "original_col_name": "type",
                    "current_col_name": "biz_type_cd",
                    "original_col_type": "varchar(10)",
                    "current_col_type": "varchar(10)",
                    "name_changed": True,
                    "type_changed": False,
                    "highest_risk_level": "L1",
                    "highest_risk_rank": 1,
                },
                {
                    "schema_name": "dwd",
                    "table_name": "t_cust",
                    "original_col_name": "date",
                    "current_col_name": "biz_dt",
                    "original_col_type": "varchar(10)",
                    "current_col_type": "date",
                    "name_changed": True,
                    "type_changed": True,
                    "highest_risk_level": "L1",
                    "highest_risk_rank": 1,
                },
            ]
        )

        # 1. FOR rec IN (SELECT ...) LOOP 不崩溃且正确改写 + 局部变量不跨语句泄漏
        proc_and_select = (
            "CREATE OR REPLACE PROCEDURE p(old_cust varchar) AS $$\n"
            "BEGIN\n"
            "  FOR rec IN (SELECT a.old_cust FROM dwd.t_cust a WHERE a.old_cust = old_cust) LOOP\n"
            "    NULL;\n"
            "  END LOOP;\n"
            "END;\n"
            "$$;\n"
            "SELECT old_cust FROM dwd.t_cust;\n"
        )
        r1 = rewrite_sql_deterministic(proc_and_select, reg)["rewritten_sql"]
        self.assertIn("SELECT a.cust_no FROM dwd.t_cust a WHERE a.cust_no = old_cust", r1)
        self.assertIn("SELECT cust_no FROM dwd.t_cust;", r1)

        # 2. 软关键字列名 (type, date) 未限定改写，同时保护 DATE '2025-01-01' 字面量
        r2 = rewrite_sql_deterministic(
            "SELECT type, date, DATE '2025-01-01' FROM dwd.t_cust WHERE type = 'A';", reg
        )["rewritten_sql"]
        self.assertEqual(
            r2,
            "SELECT biz_type_cd, biz_dt, DATE '2025-01-01' FROM dwd.t_cust WHERE biz_type_cd = 'A';",
        )

        # 3. WITH RECURSIVE 自引用 + UNION 外层 ORDER BY + 括号 CTAS + UPDATE 逗号多表
        r3 = rewrite_sql_deterministic(
            "WITH RECURSIVE tree AS (\n"
            "  SELECT a.old_cust, a.pid FROM dwd.t_cust a WHERE a.pid IS NULL\n"
            "  UNION ALL\n"
            "  SELECT c.old_cust, c.pid FROM dwd.t_cust c JOIN tree t ON c.pid = t.old_cust\n"
            ")\nSELECT t.old_cust FROM tree t;\n"
            "SELECT a.old_cust FROM dwd.t_cust a UNION ALL SELECT b.cid FROM dwd.t_other b ORDER BY old_cust;\n"
            "CREATE TABLE dwd.t_snap AS (SELECT a.old_cust FROM dwd.t_cust a);\n"
            "UPDATE dwd.t_other b, dwd.t_cust a SET a.old_cust = b.val WHERE a.old_cust = b.cid;\n",
            reg,
        )["rewritten_sql"]
        self.assertIn("ON c.pid = t.cust_no", r3)
        self.assertIn("ORDER BY cust_no;", r3)
        self.assertIn("CREATE TABLE dwd.t_snap AS (SELECT a.cust_no FROM dwd.t_cust a);", r3)
        self.assertIn("UPDATE dwd.t_other b, dwd.t_cust a SET a.cust_no = b.val WHERE a.cust_no = b.cid;", r3)

        # 4. INSERT ON CONFLICT / RETURNING + CREATE TABLE CHECK / GENERATED ALWAYS AS
        r4 = rewrite_sql_deterministic(
            "INSERT INTO dwd.t_cust (old_cust, amt_str) VALUES ('A', '100')\n"
            "ON CONFLICT (old_cust) DO UPDATE SET amt_str = EXCLUDED.amt_str\n"
            "RETURNING old_cust;\n"
            "CREATE TABLE dwd.t_cust (\n"
            "  old_cust varchar(32) CHECK (LENGTH(old_cust) > 0),\n"
            "  amt_str varchar(50),\n"
            "  amt2 numeric(18,2) GENERATED ALWAYS AS (amt_str * 2) STORED,\n"
            "  CONSTRAINT chk_c CHECK (old_cust <> '')\n"
            ");",
            reg,
        )["rewritten_sql"]
        self.assertIn("ON CONFLICT (cust_no) DO UPDATE SET tx_amt = EXCLUDED.tx_amt", r4)
        self.assertIn("RETURNING cust_no;", r4)
        self.assertIn("CHECK (LENGTH(cust_no) > 0)", r4)
        self.assertIn("GENERATED ALWAYS AS (tx_amt * 2) STORED", r4)
        self.assertIn("CONSTRAINT chk_c CHECK (cust_no <> '')", r4)

        # 5. 反向字面量比较 / NOT IN / NOT LIKE / BETWEEN / 多参 COALESCE / CTE 别名类型透传 / 自关联消噪 / Dollar 字面量保护
        r5 = rewrite_sql_deterministic(
            "WITH c1 AS (SELECT a.amt_str AS my_amt FROM dwd.t_cust a)\n"
            "SELECT COALESCE(c1.my_amt, '0'), TRIM(c1.my_amt) FROM c1\n"
            "WHERE '100' = c1.my_amt AND c1.my_amt NOT IN ('1', '2') AND c1.my_amt NOT LIKE '9%' AND c1.my_amt BETWEEN '10' AND '20';\n"
            "SELECT a.id FROM dwd.t_cust a JOIN dwd.t_cust b ON a.amt_str = b.amt_str WHERE a.memo = $$SELECT a.old_cust FROM dwd.t_cust a$$;",
            reg,
        )
        adv_cats = {a.category for a in r5["advisories"]}
        self.assertIn("TYPE_COALESCE_MISMATCH", adv_cats)
        self.assertIn("TYPE_LITERAL_MISMATCH", adv_cats)
        self.assertIn("TYPE_FUNCTION_MISMATCH", adv_cats)
        self.assertIn("DYNAMIC_SQL_HIT", adv_cats)
        self.assertNotIn("TYPE_JOIN_MISMATCH", adv_cats)
        self.assertIn("$$SELECT a.old_cust FROM dwd.t_cust a$$", r5["rewritten_sql"])

        # 6. DESC 倒序 JSON 正确按 ID 升序折叠 + 多数据源同表冲突拦截
        desc_reg = ChangeRegistry.from_changelog_json(
            json.dumps(
                {
                    "changes": [
                        {"id": 2, "event_type": "MIGRATION", "status": "APPLIED", "connection_id": "c1", "schema_name": "dwd", "table_name": "t_cust", "before_col_name": "mid_cust", "after_col_name": "cust_no", "risk_level": "L1", "risk_rank": 1},
                        {"id": 1, "event_type": "MIGRATION", "status": "APPLIED", "connection_id": "c1", "schema_name": "dwd", "table_name": "t_cust", "before_col_name": "old_cust", "after_col_name": "mid_cust", "risk_level": "L1", "risk_rank": 1},
                    ]
                }
            )
        )
        self.assertEqual(len(desc_reg.mappings), 1)
        self.assertEqual(desc_reg.mappings[0].original_col_name, "old_cust")
        self.assertEqual(desc_reg.mappings[0].current_col_name, "cust_no")

        conflict_reg = ChangeRegistry.from_effective_mappings(
            [
                {"connection_id": "c1", "connection_name": "A库", "schema_name": "dwd", "table_name": "t_cust", "original_col_name": "old_cust", "current_col_name": "cust_a", "name_changed": True},
                {"connection_id": "c2", "connection_name": "B库", "schema_name": "dwd", "table_name": "t_cust", "original_col_name": "old_cust", "current_col_name": "cust_b", "name_changed": True},
            ]
        )
        r_conf = rewrite_sql_deterministic("SELECT a.old_cust FROM dwd.t_cust a;", conflict_reg)
        self.assertEqual(r_conf["rewritten_sql"], "SELECT a.old_cust FROM dwd.t_cust a;")
        self.assertTrue(any(a.category == "AMBIGUOUS_COLUMN" for a in r_conf["advisories"]))

        # 7. Windows CRLF 多行 AI 补丁 + Gate 5 跨类别幻觉与历史中间名回退拦截 + 同行重复片段精准分列切换
        crlf_sql = "SELECT a.old_cust\r\nFROM dwd.t_cust a\r\nJOIN dwd.t_other b\r\nUSING (old_cust);\r\n"
        det_crlf = rewrite_sql_deterministic(crlf_sql, reg)

        class _StrictHub:
            def __init__(self, patches_resp):
                self.patches_resp = patches_resp

            def resolve_provider_and_model(self, **kw):
                return type("P", (), {"provider_id": "ds"})(), "DeepSeek-v4-flash"

            def chat_completion(self, **kw):
                return {"content": json.dumps({"patches": self.patches_resp})}

        hyb_crlf = enhance_sql_rewrite_with_llm(
            original_sql=crlf_sql,
            det_result=det_crlf,
            registry=reg,
            llm_hub=_StrictHub(
                [
                    {
                        "line": 3,
                        "original_snippet": "JOIN dwd.t_other b\nUSING (old_cust)",
                        "rewritten_snippet": "JOIN dwd.t_other b\nON a.cust_no = b.old_cust",
                        "category": "USING_REWRITE",
                        "confidence": "HIGH",
                        "reason": "USING 改写为显式 ON",
                    }
                ]
            ),
        )
        self.assertEqual(hyb_crlf["llm_meta"]["accepted_patches"], 1)
        self.assertIn("JOIN dwd.t_other b\r\nON a.cust_no = b.old_cust;", hyb_crlf["rewritten_sql"])

        evil_sql = "SELECT NVL(a.amt_str, ''), NVL(a.amt_str, ''), a.old_cust, a.date FROM dwd.t_cust a WHERE a.date = '';\n"
        det_evil = rewrite_sql_deterministic(evil_sql, reg)
        hyb_evil = enhance_sql_rewrite_with_llm(
            original_sql=evil_sql,
            det_result=det_evil,
            registry=reg,
            llm_hub=_StrictHub(
                [
                    {
                        "line": 1,
                        "original_snippet": "NVL(a.tx_amt, '')",
                        "rewritten_snippet": "NVL(a.fake_hallucinated_col, 0)",
                        "category": "TYPE_ADAPT",
                        "confidence": "HIGH",
                        "reason": "伪装成 TYPE_ADAPT 的幻觉列",
                    },
                    {
                        "line": 1,
                        "original_snippet": "a.cust_no",
                        "rewritten_snippet": "a.mid_cust",
                        "category": "TYPE_ADAPT",
                        "confidence": "HIGH",
                        "reason": "回退到中间历史名",
                    },
                    {
                        "line": 1,
                        "original_snippet": "NVL(a.tx_amt, '')",
                        "rewritten_snippet": "NVL(a.tx_amt, 0)",
                        "category": "TYPE_ADAPT",
                        "confidence": "HIGH",
                        "reason": "修复第 1 处",
                    },
                    {
                        "line": 1,
                        "original_snippet": "NVL(a.tx_amt, '')",
                        "rewritten_snippet": "NVL(a.tx_amt, 0)",
                        "category": "TYPE_ADAPT",
                        "confidence": "HIGH",
                        "reason": "修复第 2 处",
                    },
                    {
                        "line": 1,
                        "original_snippet": "a.biz_dt = ''",
                        "rewritten_snippet": "TO_CHAR(a.biz_dt, 'YYYY-MM-DD') = ''",
                        "category": "TYPE_ADAPT",
                        "confidence": "HIGH",
                        "reason": "合法带日期格式字面量的强转",
                    },
                ]
            ),
        )
        self.assertEqual(hyb_evil["llm_meta"]["rejected_patches"], 2)
        self.assertEqual(hyb_evil["llm_meta"]["accepted_patches"], 3)
        self.assertNotIn("fake_hallucinated_col", hyb_evil["rewritten_sql"])
        self.assertNotIn("mid_cust", hyb_evil["rewritten_sql"])
        self.assertIn("TO_CHAR(a.biz_dt, 'YYYY-MM-DD') = ''", hyb_evil["rewritten_sql"])
        # 验证同行两处相同片段可按列号单独反选第 1 处而不影响第 2 处
        nvl_llm = [p for p in hyb_evil["patches"] if p.source == "LLM" and p.original_text == "NVL(a.tx_amt, '')"]
        self.assertEqual(len(nvl_llm), 2)
        det_ids = {p.patch_id for p in hyb_evil["patches"] if p.source == "DETERMINISTIC"}
        only_second_nvl = apply_patches(evil_sql, hyb_evil["patches"], det_ids | {nvl_llm[1].patch_id})
        self.assertIn("SELECT NVL(a.tx_amt, ''), NVL(a.tx_amt, 0),", only_second_nvl)

    def test_hardening_mysql_oracle_ddl_alias_shadow_and_llm_injection_defense(self) -> None:
        """安全与方言加固回归：MySQL # 注释、Oracle / 块结束符、SELECT 别名遮蔽保护、函数/部分索引、跨表外键 REFERENCES、MySQL 行内注释及 LLM 语句/注释/高危关键字注入拦截."""
        reg = _sample_registry()
        opts = RewriteOptions(rewrite_ddl_types=True, rewrite_comments=True)

        # 1. MySQL # 单行注释不吞噬后续语句，且 PG JSONB #> / #>> 保持完整
        mysql_pg_sql = (
            "# old_cust 顶部注释\n"
            "SELECT old_cust FROM dwd.t_cust;\n"
            "# 第二条语句注释\n"
            "SELECT amt_str, payload #>> '{a,b}' FROM dwd.t_cust WHERE payload #> '{x}' IS NOT NULL;\n"
        )
        r_hash = rewrite_sql_deterministic(mysql_pg_sql, reg, opts)
        self.assertIn("# old_cust 顶部注释\nSELECT cust_no FROM dwd.t_cust;", r_hash["rewritten_sql"])
        self.assertIn("# 第二条语句注释\nSELECT tx_amt, payload #>> '{a,b}' FROM dwd.t_cust WHERE payload #> '{x}' IS NOT NULL;", r_hash["rewritten_sql"])

        # 2. Oracle / DWS 独立成行的 `/` 块结束符不吞噬后续语句
        oracle_sql = (
            "CREATE OR REPLACE PROCEDURE sp_test AS\n"
            "  v_x VARCHAR2(32);\n"
            "BEGIN\n"
            "  SELECT old_cust INTO v_x FROM dwd.t_cust;\n"
            "END;\n"
            "/\n"
            "SELECT old_cust FROM dwd.t_cust;\n"
        )
        r_ora = rewrite_sql_deterministic(oracle_sql, reg, opts)
        self.assertIn("SELECT cust_no INTO v_x FROM dwd.t_cust;", r_ora["rewritten_sql"])
        self.assertIn("/\nSELECT cust_no FROM dwd.t_cust;", r_ora["rewritten_sql"])

        # 3. SELECT 显式表达式别名遮蔽：WHERE 正常改名，GROUP BY / ORDER BY 保持引用显式别名不被破坏
        alias_shadow_sql = (
            "SELECT TRIM(a.old_cust) AS old_cust, COUNT(*) "
            "FROM dwd.t_cust a "
            "WHERE old_cust IS NOT NULL "
            "GROUP BY old_cust "
            "ORDER BY old_cust;"
        )
        r_alias = rewrite_sql_deterministic(alias_shadow_sql, reg, opts)
        self.assertEqual(
            r_alias["rewritten_sql"],
            "SELECT TRIM(a.cust_no) AS old_cust, COUNT(*) "
            "FROM dwd.t_cust a "
            "WHERE cust_no IS NOT NULL "
            "GROUP BY old_cust "
            "ORDER BY old_cust;",
        )

        # 4. 函数索引 / INCLUDE / 部分索引 WHERE 谓词 + 跨表 FOREIGN KEY REFERENCES + MySQL 行内 COMMENT
        ddl_sql = (
            "CREATE INDEX idx_cust_expr ON dwd.t_cust (LOWER(old_cust), old_cust DESC NULLS LAST) "
            "INCLUDE (amt_str) WHERE amt_str IS NOT NULL;\n"
            "CREATE TABLE dwd.t_child (\n"
            "  id int,\n"
            "  c_ref varchar(32),\n"
            "  FOREIGN KEY (c_ref) REFERENCES dwd.t_cust (old_cust)\n"
            ");\n"
            "CREATE TABLE dwd.t_cust (\n"
            "  old_cust varchar(32) COMMENT '旧客户号',\n"
            "  amt_str varchar(50)\n"
            ");\n"
        )
        r_ddl = rewrite_sql_deterministic(ddl_sql, reg, opts)
        self.assertIn(
            "CREATE INDEX idx_cust_expr ON dwd.t_cust (LOWER(cust_no), cust_no DESC NULLS LAST) "
            "INCLUDE (tx_amt) WHERE tx_amt IS NOT NULL;",
            r_ddl["rewritten_sql"],
        )
        self.assertIn("FOREIGN KEY (c_ref) REFERENCES dwd.t_cust (cust_no)", r_ddl["rewritten_sql"])
        self.assertIn("cust_no varchar(64) COMMENT '标准客户编号',", r_ddl["rewritten_sql"])
        self.assertIn("tx_amt numeric(18,2)", r_ddl["rewritten_sql"])

        # 5. LLM 门禁反注入加固：阻断分号语句注入、高危 DDL/DML 关键字、-- / /* 注释注入与 UNION 注入
        inj_sql = "SELECT NVL(a.amt_str, '') FROM dwd.t_cust a WHERE a.amt_str = '100';"
        det_inj = rewrite_sql_deterministic(inj_sql, reg)

        class _InjectionHub:
            def resolve_provider_and_model(self, **kw):
                return type("P", (), {"provider_id": "ds"})(), "DeepSeek-v4-flash"

            def chat_completion(self, **kw):
                return {
                    "content": json.dumps(
                        {
                            "patches": [
                                {
                                    "line": 1,
                                    "original_snippet": "a.tx_amt = '100'",
                                    "rewritten_snippet": "a.tx_amt = 100; DROP TABLE dwd.t_cust --",
                                    "category": "TYPE_ADAPT",
                                    "confidence": "HIGH",
                                },
                                {
                                    "line": 1,
                                    "original_snippet": "a.tx_amt = '100'",
                                    "rewritten_snippet": "a.tx_amt = 100 -- bypass",
                                    "category": "TYPE_ADAPT",
                                    "confidence": "HIGH",
                                },
                                {
                                    "line": 1,
                                    "original_snippet": "a.tx_amt = '100'",
                                    "rewritten_snippet": "a.tx_amt = 100 /* SELECT",
                                    "category": "TYPE_ADAPT",
                                    "confidence": "HIGH",
                                },
                                {
                                    "line": 1,
                                    "original_snippet": "a.tx_amt = '100'",
                                    "rewritten_snippet": "a.tx_amt = 100 UNION SELECT 1",
                                    "category": "TYPE_ADAPT",
                                    "confidence": "HIGH",
                                },
                                {
                                    "line": 1,
                                    "original_snippet": "NVL(a.tx_amt, '')",
                                    "rewritten_snippet": "NVL(a.tx_amt, 0)",
                                    "category": "TYPE_ADAPT",
                                    "confidence": "HIGH",
                                },
                            ]
                        }
                    )
                }

        res_inj = enhance_sql_rewrite_with_llm(
            original_sql=inj_sql,
            det_result=det_inj,
            registry=reg,
            llm_hub=_InjectionHub(),
        )
        self.assertEqual(res_inj["llm_meta"]["accepted_patches"], 1)
        self.assertEqual(res_inj["llm_meta"]["rejected_patches"], 4)
        self.assertIn("NVL(a.tx_amt, 0)", res_inj["rewritten_sql"])
        self.assertNotIn("DROP", res_inj["rewritten_sql"])
        self.assertNotIn("UNION", res_inj["rewritten_sql"])
        self.assertNotIn("--", res_inj["rewritten_sql"])
        self.assertNotIn("/*", res_inj["rewritten_sql"])


if __name__ == "__main__":
    unittest.main()
