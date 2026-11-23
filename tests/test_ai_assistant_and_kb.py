"""Data.O.G v3.0 离线轻量化知识库、SQL 写操作风险门禁与智能数据助手全链路自动化测试."""

from __future__ import annotations

import contextlib
import datetime
import gzip
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile

import openpyxl

from app import create_app, resolve_static_dir
from core.platform.context import PlatformContext
from core.platform.knowledge_hub import KnowledgeHub
from core.platform.license_hub import (
    SCOPE_GLOBAL,
    ed25519_derive_pubkey,
    issue_activation_code_for_seed,
)
from core.platform.sql_risk_guard import (
    build_affected_rows_probe_sql,
    classify_sql_batch,
    split_sql_statements,
)
from core.platform.storage.schema import WORKSPACE_BUSINESS_TABLES
from tests.test_modules_and_api import TestClient


def _activate_ctx(ctx: PlatformContext) -> None:
    seed = b"dataog-v3-ai-assistant-kb-seed01"
    ctx.license_hub._bind_test_pubkey(ed25519_derive_pubkey(seed))
    code = issue_activation_code_for_seed(
        seed,
        scope=SCOPE_GLOBAL,
        duration_days=90,
        expiry_date=datetime.date.today() + datetime.timedelta(days=89),
    )
    ctx.license_hub.activate(code)


def _make_sample_docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """用纯标准库构造合法 .docx 文档字节流（含 Heading1 与表格）."""
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>',
    ]
    for style, text in paragraphs:
        if style == "Heading1":
            xml_parts.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
            )
        else:
            xml_parts.append(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")
    xml_parts.append(
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>参数名</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>建议值</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>query_mem</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>4GB</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    xml_parts.append("</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>')
        zf.writestr("word/document.xml", "".join(xml_parts).encode("utf-8"))
    return buf.getvalue()


def _make_sample_xlsx(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DWS报错对照表"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _ScriptedLLMClient:
    """确定性脚本化 Mock OpenAI SDK 客户端，用于验证多轮渐进探索、纠错与提案交互."""

    def __init__(self, script_fn) -> None:
        self._script_fn = script_fn
        self.calls: list[list[dict]] = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )
        self.models = types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[]))

    def _create(self, **kwargs):
        msgs = list(kwargs.get("messages") or [])
        self.calls.append(msgs)
        content = self._script_fn(len(self.calls), msgs)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        )


class TestAiAssistantAndKnowledgeBaseV3(unittest.TestCase):
    def test_sql_risk_guard_splitting_and_tiered_classification(self) -> None:
        """验证 SQL 词法多语句切分、分级写门禁 (READONLY / STANDARD_WRITE / HIGH_RISK) 与影响行数探针生成."""
        script = """
        -- 注释里的分号; 不应拆分
        CREATE TABLE dwd.t_demo (
            id INT,
            note VARCHAR(64) DEFAULT 'a;b'
        );
        /* 块注释里的 ; 也不拆分 */
        INSERT INTO dwd.t_demo (id, note) VALUES (1, 'hello;world');
        SELECT 'ok' AS flag;
        """
        stmts = split_sql_statements(script)
        self.assertEqual(len(stmts), 3)
        self.assertIn("CREATE TABLE", stmts[0].upper())
        self.assertIn("'hello;world'", stmts[1])
        script_risk = classify_sql_batch(script)
        self.assertEqual(script_risk.risk_level, "STANDARD_WRITE")
        self.assertEqual(script_risk.statement_risks[0].primary_verb, "CREATE")

        # Dollar-Quoting 内部 ; 保留完整
        dq_stmts = split_sql_statements("CREATE FUNCTION f() RETURNS text AS $$ BEGIN RETURN 'x;y'; END; $$ LANGUAGE plpgsql; SELECT 1;")
        self.assertEqual(len(dq_stmts), 2)

        ro = classify_sql_batch("WITH cte AS (SELECT 1 AS a) SELECT * FROM cte;", db_type="DWS")
        self.assertEqual(ro.risk_level, "READONLY")
        self.assertFalse(ro.requires_confirm_token)

        std_write = classify_sql_batch(
            "CREATE TABLE t1 (id INT); COMMENT ON TABLE t1 IS '测试'; INSERT INTO t1 VALUES (1);",
            db_type="PGSQL",
        )
        self.assertEqual(std_write.risk_level, "STANDARD_WRITE")
        self.assertEqual(std_write.category, "DDL+DML")
        self.assertFalse(std_write.requires_confirm_token)

        # ALTER ... DROP 必须升为高危
        alt_drop = classify_sql_batch("ALTER TABLE t1 DROP COLUMN col_a;", db_type="DWS")
        self.assertEqual(alt_drop.risk_level, "HIGH_RISK")
        self.assertTrue(alt_drop.requires_confirm_token)

        # 无 WHERE DELETE 必须触发高危 + 整表警示
        no_where_del = classify_sql_batch("DELETE FROM t1;", db_type="MYSQL")
        self.assertEqual(no_where_del.risk_level, "HIGH_RISK")
        self.assertTrue(no_where_del.requires_confirm_token)
        self.assertTrue(any("WHERE" in w for w in no_where_del.warnings))

        # 影响行数安全只读探针生成
        probe_del = build_affected_rows_probe_sql("DELETE FROM public.orders WHERE status = 'BAD'", db_type="PGSQL")
        self.assertEqual(probe_del, "SELECT COUNT(*) AS estimated_rows FROM public.orders WHERE status = 'BAD'")
        probe_upd = build_affected_rows_probe_sql("UPDATE orders SET flag = 1 WHERE id > 10", db_type="SQLITE")
        self.assertEqual(probe_upd, "SELECT COUNT(*) AS estimated_rows FROM orders WHERE id > 10")
        self.assertIsNone(build_affected_rows_probe_sql("DELETE t1 FROM t1 JOIN t2 ON t1.id = t2.id"))

    def test_knowledge_hub_zero_dep_parsers_fts5_dialect_filter_and_pack_io(self) -> None:
        """验证全格式零依赖解析、FTS5 中文+报错码检索、方言硬隔离、知识包导出导入、可选 Embedding 与工作区零泄露."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(private_root=os.path.join(tmp, "vault_a"))
            ctx: PlatformContext = app.state.platform_ctx
            _activate_ctx(ctx)
            client = TestClient(app)

            # 1. 默认预置 11 篇主流方言速查卡
            sum_res = client.get("/api/platform/knowledge/summary").json()
            self.assertTrue(sum_res["ok"])
            self.assertEqual(sum_res["summary"]["builtin_docs"], 11)
            self.assertTrue(sum_res["summary"]["fts5_available"])

            # 2. 上传全部零依赖格式 (.md / .sql / .html / .json / .csv / .docx / .xlsx)
            docx_bytes = _make_sample_docx([
                ("Heading1", "DWS 内存与重分布调优手册"),
                ("Normal", "遇到 ERROR_MEM_LIMIT 或 Streaming REDISTRIBUTE 过慢时，应检查分布键倾斜。"),
            ])
            xlsx_bytes = _make_sample_xlsx([
                ["错误码", "方言", "解决建议"],
                ["ERR-9981", "DWS", "调整分布键 DISTRIBUTE BY HASH(cust_id) 并执行 ANALYZE"],
            ])

            uploads = [
                ("dws_guide.md", b"# DWS \xe5\x88\x86\xe5\xb8\x83\xe9\x94\xae\n\xe4\xb8\xa5\xe7\xa6\x81\xe4\xbd\xbf\xe7\x94\xa8\xe4\xbd\x8e\xe5\x9f\xba\xe6\x95\xb0\xe5\xad\x97\xe6\xae\xb5\xe5\x81\x9a DISTRIBUTE BY HASH\xe3\x80\x82", "DWS"),
                ("oracle_err.sql", b"-- ORA-00942 \xe6\x8e\x92\xe6\x9f\xa5\xe6\x8c\x87\xe5\x8d\x97\nSELECT * FROM ALL_TABLES WHERE TABLE_NAME = 'T_CUST';", "ORACLE"),
                ("dm8_ref.html", "<html><body><h1>DM8 字符集</h1><p>达梦 -6169 字符串截断需注意 UTF-8 三字节长度。</p></body></html>".encode("utf-8"), "DM8"),
                ("pg_tips.json", json.dumps({"pg_explain": "使用 EXPLAIN 查看 Seq Scan 与 Index Scan"}, ensure_ascii=False).encode("utf-8"), "PGSQL"),
                ("mysql_codes.csv", "code,solution\nERROR 1064,检查反引号与保留字\n".encode("utf-8"), "MYSQL"),
                ("dws_tuning.docx", docx_bytes, "DWS"),
                ("dws_errors.xlsx", xlsx_bytes, "DWS"),
            ]
            for fname, fbytes, dl in uploads:
                up = client.post(
                    "/api/platform/knowledge/documents/upload",
                    files={"file": (fname, fbytes, "application/octet-stream")},
                    data={"dialect": dl},
                )
                self.assertEqual(up.status_code, 200, up.text)
                self.assertTrue(up.json()["ok"])

            # 非法扩展名与 XXE DOCX 必须拦截
            bad_ext = client.post(
                "/api/platform/knowledge/documents/upload",
                files={"file": ("manual.exe", b"MZ...", "application/octet-stream")},
                data={"dialect": "DWS"},
            )
            self.assertEqual(bad_ext.status_code, 400)

            xxe_buf = io.BytesIO()
            with zipfile.ZipFile(xxe_buf, "w") as zf:
                zf.writestr("word/document.xml", b'<!DOCTYPE foo [<!ENTITY xxe "bad">]><w:document/>')
            bad_xxe = client.post(
                "/api/platform/knowledge/documents/upload",
                files={"file": ("xxe.docx", xxe_buf.getvalue(), "application/octet-stream")},
                data={"dialect": "DWS"},
            )
            self.assertEqual(bad_xxe.status_code, 400)

            # 3. 方言硬隔离 & 错误码检索 & 用户文档优先级
            ora_search = client.post(
                "/api/platform/knowledge/search",
                json={"query": "ORA-00942", "dialect": "ORACLE", "top_k": 5},
            ).json()
            self.assertTrue(ora_search["ok"])
            self.assertGreaterEqual(len(ora_search["hits"]), 1)
            self.assertTrue(all(h["dialect"] in ("ORACLE", "GENERIC") for h in ora_search["hits"]))
            # 用户上传的 oracle_err.sql 应命中且排在前面
            self.assertEqual(ora_search["hits"][0]["doc_title"], "oracle_err")

            # 在 DWS 方言下检索 ORA-00942，不得泄露 ORACLE 专有文档
            dws_leak_check = client.post(
                "/api/platform/knowledge/search",
                json={"query": "ORA-00942", "dialect": "DWS", "top_k": 5},
            ).json()
            self.assertFalse(any(h["dialect"] == "ORACLE" for h in dws_leak_check["hits"]))

            # 检索 XLSX + DOCX 提取内容 + 二阶段章节读取
            xlsx_hit = client.post(
                "/api/platform/knowledge/search",
                json={"query": "ERR-9981 query_mem", "dialect": "DWS", "top_k": 6},
            ).json()
            self.assertGreaterEqual(len(xlsx_hit["hits"]), 1)
            first_cid = xlsx_hit["hits"][0]["chunk_id"]
            sec_res = client.get(f"/api/platform/knowledge/chunks/{first_cid}?window=1").json()
            self.assertTrue(sec_res["ok"])
            self.assertGreaterEqual(len(sec_res["sections"]), 1)

            # 4. 可选 Embedding 语义混排 + 离线断网静默回退纯 FTS5
            def _fake_embed(texts, **kwargs):
                return [[1.0, 0.0, 0.0] if ("倾斜" in t or "REDISTRIBUTE" in t or "query_mem" in t) else [0.0, 1.0, 0.0] for t in texts]

            ctx.knowledge_hub.save_embedding_config({
                "enabled": True,
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "bge-m3",
            })
            with unittest.mock.patch.object(ctx.llm_hub, "create_embeddings", side_effect=_fake_embed):
                reidx = client.post("/api/platform/knowledge/embeddings/reindex").json()
                self.assertTrue(reidx["ok"])
                self.assertGreater(reidx["embedded_chunks"], 0)
                hyb = client.post(
                    "/api/platform/knowledge/search",
                    json={"query": "倾斜 REDISTRIBUTE", "dialect": "DWS", "top_k": 5},
                ).json()
                self.assertEqual(hyb["retrieval_mode"], "hybrid_fts5_embedding")

            # 模拟离线断网，自动无感退回 FTS5
            with unittest.mock.patch.object(ctx.llm_hub, "create_embeddings", side_effect=RuntimeError("Network unreachable")):
                offline_res = client.post(
                    "/api/platform/knowledge/search",
                    json={"query": "倾斜", "dialect": "DWS", "top_k": 5},
                ).json()
                self.assertTrue(offline_res["ok"])
                self.assertEqual(offline_res["retrieval_mode"], "fts5_offline_fallback")
                self.assertGreaterEqual(len(offline_res["hits"]), 1)

            # 5. .json.gz 知识包导出 -> 导入全新隔离实例 + 重复跳过
            exp_resp = client.get("/api/platform/knowledge/export?dialect=DWS")
            self.assertEqual(exp_resp.status_code, 200)
            self.assertEqual(exp_resp.headers.get("x-content-type-options"), "nosniff")
            pack_obj = json.loads(gzip.decompress(exp_resp.content).decode("utf-8"))
            self.assertEqual(pack_obj["format"], "dataog_kb_pack_v1")
            self.assertEqual(pack_obj["doc_count"], 3)  # dws_guide + dws_tuning + dws_errors

            app_b = create_app(private_root=os.path.join(tmp, "vault_b"))
            _activate_ctx(app_b.state.platform_ctx)
            client_b = TestClient(app_b)
            imp1 = client_b.post(
                "/api/platform/knowledge/import",
                files={"file": ("dws_pack.json.gz", exp_resp.content, "application/gzip")},
                data={"overwrite_existing": "false"},
            ).json()
            self.assertTrue(imp1["ok"])
            self.assertEqual(imp1["imported_count"], 3)
            imp2 = client_b.post(
                "/api/platform/knowledge/import",
                files={"file": ("dws_pack.json.gz", exp_resp.content, "application/gzip")},
                data={"overwrite_existing": "false"},
            ).json()
            self.assertEqual(imp2["imported_count"], 0)
            self.assertEqual(imp2["skipped_count"], 3)

            # 6. Layer 2 隔离：导出共享工作区 .dataog.db 不得包含任何 kb_* 或 ai_assistant_* 表
            ws_bytes, _ = ctx.workspace_hub.export_workspace_bytes()
            ws_file = os.path.join(tmp, "ws_check.dataog.db")
            with open(ws_file, "wb") as fh:
                fh.write(ws_bytes)
            with contextlib.closing(sqlite3.connect(ws_file)) as wconn:
                tbls = {
                    r[0]
                    for r in wconn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertEqual(tbls - {"sqlite_sequence"}, set(WORKSPACE_BUSINESS_TABLES))
            self.assertFalse(any(t.startswith(("kb_", "ai_assistant_")) for t in tbls))

    def test_ai_assistant_progressive_clarify_self_heal_explain_and_pure_dialect(self) -> None:
        """验证渐进式澄清门禁、自主工具探索 + 报错自修复 + EXPLAIN 执行计划 + 知识库溯源 + 离线纯方言模式."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = os.path.join(tmp, "ops_demo.db")
            with contextlib.closing(sqlite3.connect(db_file)) as sconn:
                sconn.execute(
                    "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, cust_id TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL)"
                )
                sconn.execute("CREATE INDEX idx_orders_status ON orders(status)")
                sconn.execute("INSERT INTO orders VALUES (1, 'C001', 120.5, 'PAID'), (2, 'C002', 88.0, 'BAD')")
                sconn.execute("CREATE TABLE orders_archive (order_id INTEGER PRIMARY KEY, cust_id TEXT)")
                sconn.commit()

            app = create_app(private_root=os.path.join(tmp, "vault"))
            ctx: PlatformContext = app.state.platform_ctx
            _activate_ctx(ctx)

            # 配置本地免密测试模型 & 录入一篇 SQLite 排障资料
            ctx.llm_hub.save_provider(
                {
                    "provider_id": "local_mock",
                    "name": "Local Mock LLM",
                    "provider_type": "OPENAI_COMPATIBLE",
                    "enabled": True,
                    "base_url": "http://127.0.0.1:11434/v1",
                    "default_model": "mock-coder",
                    "models": ["mock-coder"],
                    "is_default": True,
                }
            )
            kb_added = ctx.knowledge_hub.ingest_text_document(
                title="SQLite 索引执行计划速查",
                dialect="SQLITE",
                content="## EXPLAIN QUERY PLAN 解读\n遇到 SCAN orders 说明全表扫描，SEARCH orders USING INDEX idx_orders_status 说明命中二级索引。",
            )
            target_cid_chunk = f"{kb_added['document']['doc_id']}::c0000"

            prof = ctx.connection_hub.save_profile(
                {
                    "name": "订单SQLite库",
                    "db_type": "SQLITE",
                    "sqlite_path": db_file,
                }
            )
            ctx.metadata_hub.refresh_catalog(prof.connection_id)

            client = TestClient(app)
            scenario_mode = {"phase": "clarify"}

            def _scripted_reply(call_no: int, msgs: list[dict]) -> str:
                last_user = msgs[-1]["content"]
                if scenario_mode["phase"] == "clarify":
                    if "[用户澄清选择]" not in last_user:
                        return json.dumps(
                            {
                                "action": "clarify",
                                "question": "检测到 orders 与 orders_archive 两张订单表，请问要统计哪一张？",
                                "reason": "避免统计口径歧义",
                                "options": [
                                    {"label": "统计实时订单表 orders", "detail": "含金额与状态", "value": "orders"},
                                    {"label": "统计归档表 orders_archive", "detail": "仅归档主键", "value": "orders_archive"},
                                ],
                            },
                            ensure_ascii=False,
                        )
                    return json.dumps(
                        {
                            "action": "answer",
                            "summary": "已按实时订单表 orders 完成确认",
                            "content": "好的，后续分析将锁定 `orders` 表。",
                            "citations": [],
                            "next_steps": [{"label": "统计各状态订单总额", "prompt": "统计 orders 各状态订单数量和总金额，并查看执行计划"}],
                        },
                        ensure_ascii=False,
                    )

                if scenario_mode["phase"] == "self_heal_explain":
                    # 第1步：先查知识库
                    if "【工具 `" not in last_user:
                        return json.dumps(
                            {
                                "action": "tool",
                                "tool": "search_knowledge",
                                "args": {"query": "EXPLAIN QUERY PLAN", "dialect": "SQLITE"},
                                "purpose": "检索 SQLite 执行计划说明",
                            },
                            ensure_ascii=False,
                        )
                    if "【工具 `search_knowledge` 返回结果】" in last_user:
                        # 第2步：故意先写错一个字段名触发只读 SQL 报错，验证自我纠错
                        return json.dumps(
                            {
                                "action": "tool",
                                "tool": "run_readonly_sql",
                                "args": {"sql": "SELECT status, SUM(amt_wrong) FROM orders GROUP BY status"},
                                "purpose": "尝试聚合统计订单状态",
                            },
                            ensure_ascii=False,
                        )
                    if "只读 SQL 执行失败" in last_user:
                        # 第3步：自主调用 inspect_table 查看真实字段
                        return json.dumps(
                            {
                                "action": "tool",
                                "tool": "inspect_table",
                                "args": {"table_ref": "orders"},
                                "purpose": "核对 orders 真实字段结构",
                            },
                            ensure_ascii=False,
                        )
                    if "【工具 `inspect_table` 返回结果】" in last_user:
                        # 第4步：修正字段为 amount 重跑只读 SQL
                        return json.dumps(
                            {
                                "action": "tool",
                                "tool": "run_readonly_sql",
                                "args": {"sql": "SELECT status, COUNT(*) AS cnt, SUM(amount) AS total_amt FROM orders WHERE status = 'PAID' GROUP BY status"},
                                "purpose": "使用正确字段 amount 重新聚合",
                            },
                            ensure_ascii=False,
                        )
                    if "【工具 `run_readonly_sql` 返回结果】" in last_user:
                        # 第5步：查看真实执行计划 EXPLAIN
                        return json.dumps(
                            {
                                "action": "tool",
                                "tool": "explain_sql",
                                "args": {"sql": "SELECT status, amount FROM orders WHERE status = 'PAID'"},
                                "purpose": "验证 status 条件索引命中情况",
                            },
                            ensure_ascii=False,
                        )
                    # 第6步：输出完整溯源结论
                    return json.dumps(
                        {
                            "action": "answer",
                            "summary": "已完成 orders 只读统计与执行计划诊断（自我修正字段后命中索引）",
                            "content": "PAID 状态共 1 笔，总额 120.5；`EXPLAIN QUERY PLAN` 显示命中 `idx_orders_status`。",
                            "citations": [target_cid_chunk],
                            "next_steps": [
                                {"label": "检查异常状态订单", "prompt": "查看 status='BAD' 的明细"},
                            ],
                        },
                        ensure_ascii=False,
                    )

                # pure_dialect
                if "【工具 `search_knowledge` 返回结果】" not in last_user:
                    return json.dumps(
                        {
                            "action": "tool",
                            "tool": "search_knowledge",
                            "args": {"query": "DM8 字符串截断 -6169", "dialect": "DM8"},
                            "purpose": "检索 DM8 方言参考",
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "action": "answer",
                        "summary": "离线 DM8 方言语法诊断完成",
                        "content": "在 DM8 下可通过 `ALTER TABLE ... MODIFY` 放宽 VARCHAR 长度。",
                        "citations": [],
                        "next_steps": [],
                    },
                    ensure_ascii=False,
                )

            ctx.llm_hub._client_factory = lambda prof, timeout: _ScriptedLLMClient(_scripted_reply)

            # A. 澄清 -> 选择澄清项继续
            r1 = client.post(
                "/api/modules/ai_assistant/chat",
                json={
                    "work_mode": "connection",
                    "connection_id": prof.connection_id,
                    "prompt": "帮我统计一下订单表金额",
                },
            ).json()
            self.assertTrue(r1["ok"])
            sid = r1["session"]["session_id"]
            self.assertEqual(r1["assistant_message"]["response_type"], "clarify")
            self.assertEqual(len(r1["assistant_message"]["clarification"]["options"]), 2)

            r2 = client.post(
                "/api/modules/ai_assistant/chat",
                json={
                    "session_id": sid,
                    "prompt": "统计实时订单表 orders",
                    "clarification_reply": {"label": "统计实时订单表 orders", "value": "orders"},
                },
            ).json()
            self.assertTrue(r2["ok"])
            self.assertEqual(r2["assistant_message"]["response_type"], "answer")

            # B. 自我纠错 + EXPLAIN + 知识库引用 + 内联结果表 + 导出
            scenario_mode["phase"] = "self_heal_explain"
            r3 = client.post(
                "/api/modules/ai_assistant/chat",
                json={
                    "session_id": sid,
                    "prompt": "统计 PAID 状态订单总额并分析索引性能",
                    "task_mode": "optimize",
                    "max_steps": 8,
                },
            ).json()
            self.assertTrue(r3["ok"])
            asst3 = r3["assistant_message"]
            tools_used = [s["tool"] for s in asst3["steps"]]
            self.assertEqual(
                tools_used,
                ["search_knowledge", "run_readonly_sql", "inspect_table", "run_readonly_sql", "explain_sql"],
            )
            self.assertFalse(asst3["steps"][1]["ok"])  # 第2步故意失败
            self.assertTrue(asst3["steps"][3]["ok"])   # 第4步自我修正后成功
            self.assertTrue(asst3["steps"][4]["ok"])   # 第5步 EXPLAIN 成功
            self.assertEqual(asst3["result_table"]["rows"][0][0], "PAID")
            self.assertEqual(asst3["result_table"]["rows"][0][2], 120.5)
            self.assertGreaterEqual(len(asst3["citations"]), 1)
            self.assertEqual(asst3["citations"][0]["chunk_id"], target_cid_chunk)

            exp_xlsx = client.post(
                "/api/modules/ai_assistant/export-result",
                json={
                    "columns": [c["name"] for c in asst3["result_table"]["columns"]],
                    "rows": asst3["result_table"]["rows"],
                    "fmt": "xlsx",
                    "title": "orders_paid",
                },
            )
            self.assertEqual(exp_xlsx.status_code, 200)
            self.assertTrue(exp_xlsx.content.startswith(b"PK"))

            # C. 离线纯方言模式
            scenario_mode["phase"] = "pure_dialect"
            r4 = client.post(
                "/api/modules/ai_assistant/chat",
                json={
                    "work_mode": "dialect",
                    "dialect": "DM8",
                    "prompt": "达梦报错 -6169 字符串截断怎么修改字段？",
                },
            ).json()
            self.assertTrue(r4["ok"])
            self.assertEqual(r4["session"]["work_mode"], "dialect")
            self.assertEqual(r4["session"]["dialect"], "DM8")
            self.assertEqual(r4["assistant_message"]["steps"][0]["tool"], "search_knowledge")

    def test_ai_assistant_tiered_write_proposal_security_tamper_and_batch_execution(self) -> None:
        """验证自主只读隔离、DDL/INSERT 分级提案、高危 DELETE 影响行数+CONFIRM 门禁、SQL 篡改防绕过与批执行闭环."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = os.path.join(tmp, "write_gate.db")
            with contextlib.closing(sqlite3.connect(db_file)) as sconn:
                sconn.execute("CREATE TABLE t_logs (id INTEGER PRIMARY KEY, status TEXT)")
                sconn.execute("INSERT INTO t_logs VALUES (1, 'BAD'), (2, 'BAD'), (3, 'OK')")
                sconn.commit()

            app = create_app(private_root=os.path.join(tmp, "vault"))
            ctx: PlatformContext = app.state.platform_ctx
            _activate_ctx(ctx)
            ctx.llm_hub.save_provider(
                {
                    "provider_id": "local_mock",
                    "name": "Local Mock LLM",
                    "provider_type": "OPENAI_COMPATIBLE",
                    "enabled": True,
                    "base_url": "http://127.0.0.1:11434/v1",
                    "default_model": "mock-coder",
                    "models": ["mock-coder"],
                    "is_default": True,
                }
            )
            prof = ctx.connection_hub.save_profile(
                {"name": "写门禁测试库", "db_type": "SQLITE", "sqlite_path": db_file}
            )
            client = TestClient(app)

            mode = {"kind": "ddl_insert"}

            def _write_script(_call_no: int, msgs: list[dict]) -> str:
                last = msgs[-1]["content"]
                if mode["kind"] == "sneaky_ro":
                    if "只读沙箱已拦截" not in last:
                        # 模拟大模型试图直接用 run_readonly_sql 执行 DROP
                        return json.dumps(
                            {
                                "action": "tool",
                                "tool": "run_readonly_sql",
                                "args": {"sql": "DROP TABLE t_logs"},
                                "purpose": "尝试直接执行删表",
                            }
                        )
                    return json.dumps(
                        {
                            "action": "propose_write",
                            "title": "删除 t_logs 表",
                            "rationale": "只读通道已阻止直接执行，改为提交高危确认提案",
                            "sql": "DROP TABLE t_logs;",
                            "summary": "已按规范生成高危删表提案，请在下方确认。",
                        },
                        ensure_ascii=False,
                    )
                if mode["kind"] == "ddl_insert":
                    return json.dumps(
                        {
                            "action": "propose_write",
                            "title": "新建审计配置表并写入初始记录",
                            "rationale": "创建 t_cfg 表并写入一条默认配置",
                            "sql": "CREATE TABLE t_cfg (k TEXT PRIMARY KEY, v TEXT); INSERT INTO t_cfg VALUES ('env', 'prod');",
                            "summary": "已生成建表与初始化写入语句，请核对确认执行。",
                        },
                        ensure_ascii=False,
                    )
                # delete_bad
                return json.dumps(
                    {
                        "action": "propose_write",
                        "title": "清理异常 BAD 状态日志",
                        "rationale": "删除 t_logs 中 status='BAD' 的历史脏数据",
                        "sql": "DELETE FROM t_logs WHERE status = 'BAD';",
                        "summary": "请确认高危删除操作。",
                    },
                    ensure_ascii=False,
                )

            ctx.llm_hub._client_factory = lambda p, t: _write_script_client(_write_script)

            def _write_script_client(fn):
                return _ScriptedLLMClient(fn)

            # 1. 即使大模型企图调用 run_readonly_sql 执行 DROP，也被物理只读沙箱硬阻断，t_logs 完好无损
            mode["kind"] = "sneaky_ro"
            r_sneaky = client.post(
                "/api/modules/ai_assistant/chat",
                json={"connection_id": prof.connection_id, "prompt": "删掉 t_logs 表"},
            ).json()
            self.assertTrue(r_sneaky["ok"])
            self.assertFalse(r_sneaky["assistant_message"]["steps"][0]["ok"])
            prop_drop = r_sneaky["assistant_message"]["proposal"]
            self.assertEqual(prop_drop["risk_level"], "HIGH_RISK")
            self.assertEqual(prop_drop["status"], "pending")
            # 确认取消（Reject）后不能再执行
            rej = client.post(
                f"/api/modules/ai_assistant/proposals/{prop_drop['proposal_id']}/reject",
                json={"reason": "误操作取消"},
            ).json()
            self.assertTrue(rej["ok"])
            self.assertEqual(rej["proposal"]["status"], "rejected")
            re_exec_rej = client.post(
                f"/api/modules/ai_assistant/proposals/{prop_drop['proposal_id']}/confirm",
                json={"confirm_token": "CONFIRM"},
            )
            self.assertEqual(re_exec_rej.status_code, 400)

            # 2. STANDARD_WRITE 多语句 DDL + INSERT：未确认前表不存在；防篡改（改写为 DROP 必须触发 CONFIRM_TOKEN_REQUIRED）；确认后批执行并刷新目录
            mode["kind"] = "ddl_insert"
            r_ddl = client.post(
                "/api/modules/ai_assistant/chat",
                json={"connection_id": prof.connection_id, "prompt": "帮我创建 t_cfg 并插一条 prod"},
            ).json()
            prop_ddl = r_ddl["assistant_message"]["proposal"]
            self.assertEqual(prop_ddl["risk_level"], "STANDARD_WRITE")
            self.assertEqual(len(prop_ddl["statements"]), 2)

            # 确认前数据库里还没有 t_cfg
            with contextlib.closing(sqlite3.connect(db_file)) as sconn:
                exists_before = sconn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='t_cfg'"
                ).fetchone()[0]
            self.assertEqual(exists_before, 0)

            # 篡改防绕过测试：把常规提案 SQL 偷改为 DROP TABLE t_logs 且不传 CONFIRM -> 必须被服务端重新定级拦下
            tamper_resp = client.post(
                f"/api/modules/ai_assistant/proposals/{prop_ddl['proposal_id']}/confirm",
                json={"edited_sql": "DROP TABLE t_logs;", "confirm_token": ""},
            )
            self.assertEqual(tamper_resp.status_code, 400)
            self.assertEqual(tamper_resp.json().get("code"), "CONFIRM_TOKEN_REQUIRED")

            # 正常确认执行原多语句 DDL + INSERT
            exec_ddl = client.post(
                f"/api/modules/ai_assistant/proposals/{prop_ddl['proposal_id']}/confirm",
                json={},
            ).json()
            self.assertTrue(exec_ddl["ok"])
            self.assertEqual(exec_ddl["proposal"]["status"], "executed")
            self.assertEqual(exec_ddl["execution_result"]["succeeded_count"], 2)
            self.assertTrue(exec_ddl["catalog_refreshed"])
            self.assertEqual(exec_ddl["execution_message"]["response_type"], "execution")

            with contextlib.closing(sqlite3.connect(db_file)) as sconn:
                val = sconn.execute("SELECT v FROM t_cfg WHERE k='env'").fetchone()[0]
            self.assertEqual(val, "prod")

            # 3. HIGH_RISK DELETE：自动预估影响行数 = 2；未填 CONFIRM 拦截；填入 CONFIRM 后精准删掉 2 行
            mode["kind"] = "delete_bad"
            r_del = client.post(
                "/api/modules/ai_assistant/chat",
                json={"connection_id": prof.connection_id, "prompt": "清理 BAD 状态数据"},
            ).json()
            prop_del = r_del["assistant_message"]["proposal"]
            self.assertEqual(prop_del["risk_level"], "HIGH_RISK")
            self.assertEqual(prop_del["estimated_rows"], 2)

            fail_no_tok = client.post(
                f"/api/modules/ai_assistant/proposals/{prop_del['proposal_id']}/confirm",
                json={"confirm_token": "wrong"},
            )
            self.assertEqual(fail_no_tok.status_code, 400)
            self.assertEqual(fail_no_tok.json().get("code"), "CONFIRM_TOKEN_REQUIRED")

            ok_del = client.post(
                f"/api/modules/ai_assistant/proposals/{prop_del['proposal_id']}/confirm",
                json={"confirm_token": "CONFIRM"},
            ).json()
            self.assertTrue(ok_del["ok"])
            self.assertTrue(ok_del["execution_ok"])
            self.assertEqual(ok_del["execution_result"]["statements"][0]["affected_rows"], 2)

            with contextlib.closing(sqlite3.connect(db_file)) as sconn:
                rem = sconn.execute("SELECT COUNT(*) FROM t_logs").fetchone()[0]
            self.assertEqual(rem, 1)

            # 4. 失败执行路径：确认执行引用不存在的表 -> 返回 HTTP 200 (ok=True, execution_ok=False, status='failed') 且记录 execution 报错消息
            mode["kind"] = "ddl_insert"
            r_fail_prop = client.post(
                "/api/modules/ai_assistant/chat",
                json={"connection_id": prof.connection_id, "prompt": "插入数据到不存在的表"},
            ).json()
            fail_prop_id = r_fail_prop["assistant_message"]["proposal"]["proposal_id"]
            exec_fail_resp = client.post(
                f"/api/modules/ai_assistant/proposals/{fail_prop_id}/confirm",
                json={"edited_sql": "INSERT INTO nonexistent_tbl_xyz VALUES (1);"},
            )
            self.assertEqual(exec_fail_resp.status_code, 200)
            exec_fail = exec_fail_resp.json()
            self.assertTrue(exec_fail["ok"])
            self.assertFalse(exec_fail["execution_ok"])
            self.assertEqual(exec_fail["proposal"]["status"], "failed")
            self.assertEqual(exec_fail["execution_message"]["response_type"], "execution")
            self.assertIn("nonexistent_tbl_xyz", exec_fail["execution_message"]["content"])

    def test_frontend_ai_assistant_and_knowledge_modules_via_node(self) -> None:
        """Node.js 运行时验证 v3.0 前端应用 `Data.O.G` (ai_assistant) 与 `KnowledgeSettingsView` 渲染及销毁零报错."""
        if getattr(sys, "frozen", False) or not shutil.which("node"):
            return

        project_root = os.path.dirname(resolve_static_dir())
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
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.selected = false;
    this.parentNode = null;
    const self = this;
    this.classList = {
      add(...cs) { self.className = [...new Set([...(self.className||"").split(/\s+/).filter(Boolean), ...cs])].join(" "); },
      remove(...cs) { const s = new Set(cs); self.className = (self.className||"").split(/\s+/).filter(x => x && !s.has(x)).join(" "); },
      toggle(c, f) { const has = (self.className||"").split(/\s+/).includes(c); const on = f === undefined ? !has : Boolean(f); if (on) this.add(c); else this.remove(c); },
      contains(c) { return (self.className||"").split(/\s+/).includes(c); },
    };
  }
  appendChild(c) { if (c) { c.parentNode = this; this.children.push(c); } return c; }
  replaceChildren(...ns) { this.children = ns.filter(Boolean); this.children.forEach(c => c.parentNode = this); }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k) ?? null; }
  addEventListener(e, fn) { (this.listeners.get(e) || this.listeners.set(e, []).get(e)).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {}
  remove() {}
}

globalThis.Node = MockNode;
globalThis.HTMLDialogElement = class HTMLDialogElement {};
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, innerWidth: 1440, innerHeight: 900 };
globalThis.document = {
  head: new MockNode("head"),
  body: new MockNode("body"),
  createElement: (tag) => new MockNode(tag),
  createElementNS: (_ns, tag) => new MockNode(tag),
  createTextNode: (t) => Object.assign(new MockNode("#text"), { textContent: String(t) }),
  createDocumentFragment: () => new MockNode("#frag"),
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
};
globalThis.fetch = async () => ({
  ok: true,
  status: 200,
  headers: new Map([["content-type", "application/json"]]),
  json: async () => ({ ok: true, dialects: [{ code: "DWS", label: "GaussDB(DWS)" }], sessions: [], llm_models: [], documents: [], summary: {} }),
  text: async () => "{}",
});

const root = process.argv[1];
const imp = (rel) => import(pathToFileURL(path.join(root, rel)).href);

const { getDesktopApp, loadAppInstance } = await imp("static/js/core/app_registry.js");
await imp("static/js/apps/index.js");

const manifest = getDesktopApp("ai_assistant");
if (!manifest || manifest.title !== "Data.O.G") {
  throw new Error("ai_assistant not registered with expected title Data.O.G");
}

const view = await loadAppInstance("ai_assistant");
const host = new MockNode("div");
await view.mount(host, { dialect: "DM8" });
const hdr = view.getHeaderParts();
if (!hdr?.center || !hdr?.right) throw new Error("AiAssistantView header parts missing");

const { renderSafeMarkdown } = await imp("static/js/apps/ai_assistant/markdown_dom.js");
const mdNode = renderSafeMarkdown("# 标题\n\n> 引用\n\n```sql\nSELECT 1;\n```\n\n| A | B |\n| --- | --- |\n| 1 | 2 |");
if (!mdNode.children.length) throw new Error("renderSafeMarkdown produced empty DOM");

const { renderProposalCard } = await imp("static/js/apps/ai_assistant/proposal_card.js");
const propNode = renderProposalCard({
  proposal_id: "prop_1",
  title: "高危清理",
  dialect: "DWS",
  risk_level: "HIGH_RISK",
  status: "pending",
  original_sql: "DELETE FROM dwd.t WHERE id = 1;",
  statements: [{ index: 1 }],
  warnings: ["高危删除"],
  estimated_rows: 1,
});
if (!propNode || !propNode.children.length) throw new Error("renderProposalCard failed");

const { KnowledgeSettingsView } = await imp("static/js/views/knowledge_view.js");
const kbView = new KnowledgeSettingsView();
kbView.render();
if (!kbView.root.children.length) throw new Error("KnowledgeSettingsView render failed");
kbView.destroy();
view.destroy();
console.log("V3_AI_ASSISTANT_FRONTEND_OK");
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, project_root],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node 前端 v3.0 AI 助手验证失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("V3_AI_ASSISTANT_FRONTEND_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
