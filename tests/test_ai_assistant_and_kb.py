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

    def test_chat_attachments_csv_query_and_stepped_map_reduce(self) -> None:
        """端到端验证现代长上下文存储过程全量直通/大纲按行探索、ContextLengthExceeded 自动降级、CSV 沙箱及 Map-Reduce 拆解."""
        from modules.ai_assistant.attachments import (
            CHUNK_HARD_MAX_TOKENS,
            CSV_INLINE_TOKEN_BUDGET,
            INLINE_TOKEN_BUDGET,
            MAX_CSV_BYTES,
            MAX_TEXT_BYTES,
            estimate_tokens,
            query_csv_in_memory,
        )
        from core.platform.llm_hub import ContextLengthExceededError
        from core.platform.readonly_guard import ReadOnlyViolationError

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(private_root=os.path.join(tmp, "vault"))
            ctx: PlatformContext = app.state.platform_ctx
            _activate_ctx(ctx)
            ctx.llm_hub.save_provider(
                {
                    "provider_id": "deepseek",
                    "name": "DeepSeek Cloud",
                    "provider_type": "DEEPSEEK",
                    "enabled": True,
                    "api_key": "sk-ds-mock-key-123456",
                    "default_model": "deepseek-chat",
                    "models": ["deepseek-chat", "deepseek-reasoner"],
                    "is_default": True,
                }
            )
            ctx.llm_hub.save_provider(
                {
                    "provider_id": "local_32k",
                    "name": "Local 32K LLM",
                    "provider_type": "OPENAI_COMPATIBLE",
                    "enabled": True,
                    "base_url": "http://127.0.0.1:11434/v1",
                    "default_model": "small-proc-32k",
                    "models": ["small-proc-32k"],
                    "context_window": 32000,
                    "is_default": False,
                }
            )
            client = TestClient(app)

            # 0. /dialects 下发 attachment_limits（默认 inline budget = 32000）与模型上下文容量
            dl_res = client.get("/api/modules/ai_assistant/dialects").json()
            self.assertTrue(dl_res["ok"])
            self.assertEqual(dl_res["attachment_limits"]["max_csv_bytes"], MAX_CSV_BYTES)
            self.assertEqual(dl_res["attachment_limits"]["max_text_bytes"], MAX_TEXT_BYTES)
            self.assertEqual(dl_res["attachment_limits"]["inline_token_budget"], INLINE_TOKEN_BUDGET)
            models_map = {f"{m['provider_id']}::{m['model']}": m for m in dl_res["llm_models"]}
            self.assertEqual(models_map["deepseek::deepseek-chat"]["context_window"], 128000)
            self.assertEqual(models_map["local_32k::small-proc-32k"]["context_window"], 32000)

            # 1. 安全拦截：不允许后缀 / 空字节二进制伪装 / CSV>2MB / TXT>5MB
            bad_ext = client.post(
                "/api/modules/ai_assistant/attachments/upload",
                files={"file": ("malware.exe", b"MZ...", "application/octet-stream")},
            )
            self.assertEqual(bad_ext.status_code, 400)

            bad_bin = client.post(
                "/api/modules/ai_assistant/attachments/upload",
                files={"file": ("fake.txt", b"hello\x00\x01binary", "text/plain")},
            )
            self.assertEqual(bad_bin.status_code, 400)
            self.assertIn("空字节", bad_bin.json()["error"])

            big_csv = client.post(
                "/api/modules/ai_assistant/attachments/upload",
                files={"file": ("huge.csv", b"a,b\n" + b"1,2\n" * (MAX_CSV_BYTES // 3), "text/csv")},
            )
            self.assertEqual(big_csv.status_code, 400)

            big_txt = client.post(
                "/api/modules/ai_assistant/attachments/upload",
                files={"file": ("huge.txt", b"x" * (MAX_TEXT_BYTES + 100), "text/plain")},
            )
            self.assertEqual(big_txt.status_code, 400)

            # 2. GBK 编码 CSV 解析、前导零保留 TEXT、类型推断与 :memory: 只读 SQLite 查询 + 写阻断
            gbk_csv_text = "网点号,部门,交易笔数,金额\n001024,零售部,10,120.5\n001024,零售部,5,79.5\n002048,对公部,3,500.0\n"
            up_gbk = client.post(
                "/api/modules/ai_assistant/attachments/upload",
                files={"file": ("txns_gbk.csv", gbk_csv_text.encode("gbk"), "text/csv")},
            )
            self.assertEqual(up_gbk.status_code, 200)
            gbk_att = up_gbk.json()["attachment"]
            self.assertEqual(gbk_att["file_kind"], "csv")
            cols_by_name = {c["name"]: c for c in gbk_att["csv_profile"]["columns"]}
            self.assertEqual(cols_by_name["网点号"]["inferred_type"], "TEXT")
            self.assertEqual(cols_by_name["交易笔数"]["inferred_type"], "INTEGER")
            self.assertEqual(cols_by_name["金额"]["inferred_type"], "REAL")

            mem_res = query_csv_in_memory(
                gbk_csv_text,
                ".csv",
                'SELECT 网点号, 部门, SUM(交易笔数) AS total_cnt, ROUND(SUM(金额), 2) AS total_amt FROM csv_data GROUP BY 网点号, 部门 ORDER BY total_amt DESC',
            )
            self.assertTrue(mem_res["ok"])
            self.assertEqual(mem_res["rows"][0], ["002048", "对公部", 3, 500.0])

            for evil_sql in ("DROP TABLE csv_data", "ATTACH DATABASE '/tmp/x.db' AS x"):
                with self.assertRaises(ReadOnlyViolationError):
                    query_csv_in_memory(gbk_csv_text, ".csv", evil_sql)

            # 3. 超大单行粘贴 (>50K tokens)：大粒度硬切 <= CHUNK_HARD_MAX_TOKENS (16K)
            giant_single_line = "SELECT col_a FROM dwd.t WHERE note = '" + ("中文业务说明描述规则" * 10500) + "';"
            paste_giant = client.post(
                "/api/modules/ai_assistant/attachments/text",
                json={"content": giant_single_line},
            )
            self.assertEqual(paste_giant.status_code, 200)
            giant_att = paste_giant.json()["attachment"]
            self.assertTrue(giant_att["oversized"])
            self.assertGreaterEqual(giant_att["chunk_count"], 3)
            for ch in giant_att["chunks"]:
                self.assertLessEqual(ch["estimated_tokens"], CHUNK_HARD_MAX_TOKENS)
            del_giant = client.delete(f"/api/modules/ai_assistant/attachments/{giant_att['attachment_id']}")
            self.assertEqual(del_giant.status_code, 200)

            # 4. 3,200+ 行复杂存储过程：结构化大纲提取 + 大粒度分块（仅 2~6 片，杜绝 30+ 碎片）
            sp_lines = [
                "CREATE OR REPLACE PROCEDURE dwd.sp_daily_settlement(p_biz_date VARCHAR(8))",
                "LANGUAGE PLPGSQL AS $$",
                "DECLARE",
                "    v_row_cnt BIGINT := 0;",
                "BEGIN",
            ]
            for sec_i in range(1, 13):
                sp_lines.append(f"    -- [Section {sec_i:02d}] 日终对账_{sec_i:02d}")
                sp_lines.append(f"    -- STEP_{sec_i:02d}_MARKER 重算")
                for row_k in range(1, 265):
                    if row_k == 1:
                        sp_lines.append(f"    INSERT INTO dwd.agg_{sec_i:02d} SELECT acct_id, 1 FROM ods.txns_{sec_i:02d};")
                    else:
                        sp_lines.append(f"    INSERT INTO dwd.agg_{sec_i:02d} VALUES ({row_k});")
            sp_lines.extend(["    COMMIT;", "END;", "$$;"])
            big_sp_bytes = "\n".join(sp_lines).encode("utf-8")

            up_sp = client.post(
                "/api/modules/ai_assistant/attachments/upload",
                files={"file": ("sp_daily_settlement.sql", big_sp_bytes, "text/plain")},
            ).json()
            sp_att = up_sp["attachment"]
            self.assertGreater(sp_att["line_count"], 3100)
            self.assertTrue(sp_att["outline"]["is_stored_procedure"])
            self.assertTrue(any("sp_daily_settlement" in o["name"] for o in sp_att["outline"]["objects"]))
            self.assertGreaterEqual(len(sp_att["outline"]["sections"]), 10)
            self.assertIn("ods.txns_01", sp_att["outline"]["referenced_tables"])
            # 3200+ 行存储过程在现代 12K/16K 大分片下仅占 3~8 片，绝不拆成 30 多片
            self.assertGreaterEqual(sp_att["chunk_count"], 2)
            self.assertLessEqual(sp_att["chunk_count"], 12)

            seen_prompts: list[str] = []
            seen_models: list[str] = []
            force_ctx_exceed_once = {"armed": False}

            def _mock_chat_completion(**kwargs):
                msgs = kwargs.get("messages") or []
                mdl = str(kwargs.get("model") or "")
                seen_models.append(mdl)
                sys_msg = msgs[0]["content"] if msgs else ""
                user_msg = msgs[-1]["content"] if msgs else ""
                seen_prompts.append(user_msg)

                if force_ctx_exceed_once["armed"]:
                    force_ctx_exceed_once["armed"] = False
                    raise ContextLengthExceededError("simulated upstream 32k context overflow")

                if "Map 阶段" in sys_msg:
                    return {
                        "provider_name": "Local 32K LLM",
                        "model": mdl,
                        "content": json.dumps(
                            {
                                "summary": "该存储过程分段逻辑清晰，无死锁风险",
                                "findings": ["发现批量 INSERT INTO dwd.settlement_agg 语句"],
                                "sql_or_action": "继续检查下一段",
                            },
                            ensure_ascii=False,
                        ),
                    }

                # 32K 小窗口 agentic_explore：模型自主调用 read_attachment_lines 与正则 search_attachment
                if mdl == "small-proc-32k" and "【工具 `" not in user_msg:
                    return {
                        "provider_name": "Local 32K LLM",
                        "model": mdl,
                        "content": json.dumps(
                            {
                                "action": "tool",
                                "tool": "read_attachment_lines",
                                "args": {
                                    "attachment_id": sp_att["attachment_id"],
                                    "start_line": 1500,
                                    "end_line": 1525,
                                },
                                "purpose": "精读第 1500~1525 行存储过程核心逻辑",
                            },
                            ensure_ascii=False,
                        ),
                    }
                if "【工具 `read_attachment_lines` 返回结果】" in user_msg:
                    return {
                        "provider_name": "Local 32K LLM",
                        "model": mdl,
                        "content": json.dumps(
                            {
                                "action": "tool",
                                "tool": "search_attachment",
                                "args": {
                                    "attachment_id": sp_att["attachment_id"],
                                    "keyword": r"STEP_07_\w+",
                                    "regex": True,
                                    "context_lines": 1,
                                },
                                "purpose": "正则检索第 7 阶段标记及其上下文行",
                            },
                            ensure_ascii=False,
                        ),
                    }

                # CSV 沙箱三步探索
                if "csv_large.csv" in user_msg and "【工具 `" not in user_msg:
                    return {
                        "provider_name": "DeepSeek Cloud",
                        "model": mdl,
                        "content": json.dumps(
                            {
                                "action": "tool",
                                "tool": "query_csv_attachment",
                                "args": {
                                    "sql": "SELECT dept, SUM(amt) AS sum_amt FROM csv_data GROUP BY dept ORDER BY sum_amt DESC"
                                },
                                "purpose": "在内存只读 SQLite 中按部门聚合 CSV 金额",
                            },
                            ensure_ascii=False,
                        ),
                    }
                if "【工具 `query_csv_attachment` 返回结果】" in user_msg:
                    return {
                        "provider_name": "DeepSeek Cloud",
                        "model": mdl,
                        "content": json.dumps(
                            {
                                "action": "tool",
                                "tool": "search_attachment",
                                "args": {"keyword": "DEPT_A"},
                                "purpose": "在 CSV 原文中检索 DEPT_A 行号定位",
                            },
                            ensure_ascii=False,
                        ),
                    }
                if "【工具 `search_attachment` 返回结果】" in user_msg:
                    return {
                        "provider_name": "DeepSeek Cloud",
                        "model": mdl,
                        "content": json.dumps(
                            {
                                "action": "tool",
                                "tool": "inspect_attachment_chunk",
                                "args": {"chunk_index": 0},
                                "purpose": "抽查首个分片确认明细",
                            },
                            ensure_ascii=False,
                        ),
                    }

                return {
                    "provider_name": "DeepSeek Cloud",
                    "model": mdl,
                    "content": json.dumps(
                        {
                            "action": "answer",
                            "summary": "已完成长存储过程/CSV 分析结论",
                            "content": "汇总结论：存储过程清算逻辑与表关联关系已核查完毕。",
                            "citations": [],
                            "next_steps": [{"label": "生成性能优化建议", "prompt": "优化批量插入性能"}],
                        },
                        ensure_ascii=False,
                    ),
                }

            with unittest.mock.patch.object(ctx.llm_hub, "chat_completion", side_effect=_mock_chat_completion):
                # 4a. 128K 大窗口模型 (deepseek-chat)：auto 策略直接全量带行号直通，零分片等待！
                r_full = client.post(
                    "/api/modules/ai_assistant/chat",
                    json={
                        "prompt": "梳理这个存储过程的整体逻辑",
                        "dialect": "DWS",
                        "provider_id": "deepseek",
                        "model": "deepseek-chat",
                        "attachment_ids": [sp_att["attachment_id"]],
                    },
                ).json()
                self.assertTrue(r_full["ok"])
                self.assertEqual(len(r_full["assistant_message"]["steps"]), 0)
                self.assertIn("【用户本轮附带文件全文】", seen_prompts[-1])
                self.assertIn("1| CREATE OR REPLACE PROCEDURE", seen_prompts[-1])
                self.assertIn("3100| ", seen_prompts[-1])

                # 4b. 32K 小窗口模型 + 巨大附件 (giant_att_2 ~45K tok)：auto 切换为 agentic_explore，大纲 + 按行精读 + 正则检索
                sp_32k_huge = client.post(
                    "/api/modules/ai_assistant/attachments/upload",
                    files={
                        "file": (
                            "giant_sp.sql",
                            "\n".join(sp_lines * 2).encode("utf-8"),
                            "text/plain",
                        )
                    },
                ).json()["attachment"]
                # 绑定到 32k 会话先复用现有 sp_att 也可以：如果 sp_att ~38K > 20.8K (32K*0.65)，也会触发 agentic_explore!
                r_32k = client.post(
                    "/api/modules/ai_assistant/chat",
                    json={
                        "prompt": "排查第 7 阶段和 1500 行附近的逻辑",
                        "dialect": "DWS",
                        "provider_id": "local_32k",
                        "model": "small-proc-32k",
                        "attachment_ids": [sp_att["attachment_id"], sp_32k_huge["attachment_id"]],
                    },
                ).json()
                self.assertTrue(r_32k["ok"])
                asst_32k_steps = r_32k["assistant_message"]["steps"]
                self.assertEqual(
                    [s["tool"] for s in asst_32k_steps],
                    ["read_attachment_lines", "search_attachment", "inspect_attachment_chunk"],
                )
                tool_1_out = [p for p in seen_prompts if "【工具 `read_attachment_lines` 返回结果】" in p][-1]
                self.assertIn("1500| ", tool_1_out)
                self.assertIn("1525| ", tool_1_out)
                tool_2_out = [p for p in seen_prompts if "【工具 `search_attachment` 返回结果】" in p][-1]
                self.assertIn("STEP_07_MARKER", tool_2_out)
                self.assertIn('"context":', tool_2_out)
                self.assertIn("1602| ", tool_2_out)
                self.assertIn("1604| ", tool_2_out)

                # 4c. 上游网关报 ContextLengthExceededError 时自动无缝降级到 agentic_explore 重试成功
                force_ctx_exceed_once["armed"] = True
                r_ctx_fb = client.post(
                    "/api/modules/ai_assistant/chat",
                    json={
                        "prompt": "强制全量尝试超限降级",
                        "dialect": "DWS",
                        "provider_id": "deepseek",
                        "model": "deepseek-chat",
                        "attachment_ids": [sp_att["attachment_id"]],
                        "large_payload_strategy": "full_context",
                    },
                ).json()
                self.assertTrue(r_ctx_fb["ok"])
                self.assertIn("【长上下文智能探索指引】", seen_prompts[-1])

                # 4d. 显式指定 stepped_batch：验证大块 Map-Reduce 拆解与接续芯片
                batch_sp_att = client.post(
                    "/api/modules/ai_assistant/attachments/upload",
                    files={"file": ("batch_sp.sql", big_sp_bytes, "text/plain")},
                ).json()["attachment"]
                seen_prompts.clear()
                r_batch1 = client.post(
                    "/api/modules/ai_assistant/chat",
                    json={
                        "prompt": "按分步拆解模式逐段审计",
                        "dialect": "DWS",
                        "provider_id": "local_32k",
                        "model": "small-proc-32k",
                        "attachment_ids": [batch_sp_att["attachment_id"]],
                        "large_payload_strategy": "stepped_batch",
                        "batch_start_chunk": 0,
                        "batch_max_chunks": 2,
                    },
                ).json()
                self.assertTrue(r_batch1["ok"])
                sid_sql = r_batch1["session"]["session_id"]
                asst1 = r_batch1["assistant_message"]
                map_steps = [s for s in asst1["steps"] if s["tool"] == "analyze_attachment_chunk"]
                self.assertEqual(len(map_steps), 2)
                for sp_txt in seen_prompts:
                    self.assertLessEqual(estimate_tokens(sp_txt), CHUNK_HARD_MAX_TOKENS + 3000)

                if batch_sp_att["chunk_count"] > 2:
                    cont_chip = asst1["next_steps"][0]
                    self.assertEqual(cont_chip["batch_start_chunk"], 2)
                    r_batch2 = client.post(
                        "/api/modules/ai_assistant/chat",
                        json={
                            "session_id": sid_sql,
                            "prompt": cont_chip["prompt"],
                            "dialect": "DWS",
                            "provider_id": "local_32k",
                            "model": "small-proc-32k",
                            "attachment_ids": cont_chip["attachment_ids"],
                            "large_payload_strategy": "stepped_batch",
                            "batch_start_chunk": 2,
                            "batch_max_chunks": 12,
                        },
                    ).json()
                    self.assertTrue(r_batch2["ok"])
                    map_steps2 = [s for s in r_batch2["assistant_message"]["steps"] if s["tool"] == "analyze_attachment_chunk"]
                    self.assertEqual(len(map_steps2), batch_sp_att["chunk_count"] - 2)

                # 5. 超大 CSV：默认 agentic_explore 策略 -> query_csv_attachment + search_attachment + inspect_attachment_chunk
                csv_rows = ["id,dept,amt,remark"] + [
                    f"{i},{'DEPT_A' if i % 2 == 0 else 'DEPT_B'},{100.0 if i % 2 == 0 else 50.0},大体积财务流水明细对比备注_{i:04d}_" + ("X" * 40)
                    for i in range(1, 260)
                ]
                big_csv_bytes = "\n".join(csv_rows).encode("utf-8")
                up_big_csv = client.post(
                    "/api/modules/ai_assistant/attachments/upload",
                    files={"file": ("csv_large.csv", big_csv_bytes, "text/csv")},
                ).json()
                big_csv_att = up_big_csv["attachment"]
                self.assertGreater(big_csv_att["estimated_tokens"], CSV_INLINE_TOKEN_BUDGET)
                self.assertTrue(big_csv_att["oversized"])
                self.assertEqual(big_csv_att["recommended_strategy"], "agentic_explore")

                r_csv = client.post(
                    "/api/modules/ai_assistant/chat",
                    json={
                        "prompt": "",
                        "dialect": "DWS",
                        "attachment_ids": [big_csv_att["attachment_id"]],
                    },
                ).json()
                self.assertTrue(r_csv["ok"])
                sid_csv = r_csv["session"]["session_id"]
                csv_asst = r_csv["assistant_message"]
                csv_tool_names = [s["tool"] for s in csv_asst["steps"]]
                self.assertEqual(
                    csv_tool_names,
                    ["query_csv_attachment", "search_attachment", "inspect_attachment_chunk"],
                )
                self.assertEqual(csv_asst["result_table"]["row_count"], 2)
                self.assertEqual(csv_asst["result_table"]["rows"][0][0], "DEPT_A")

                # 5b. 小体积 SQL full_context 直通单次调用 + 历史消息不重复内嵌全文正文
                up_small = client.post(
                    "/api/modules/ai_assistant/attachments/upload",
                    files={
                        "file": (
                            "small.sql",
                            b"SELECT id, amt FROM dwd.orders WHERE status = 'PAID';\n",
                            "text/plain",
                        )
                    },
                ).json()
                small_att = up_small["attachment"]
                self.assertFalse(small_att["oversized"])
                self.assertEqual(small_att["recommended_strategy"], "full_context")

                r_inline = client.post(
                    "/api/modules/ai_assistant/chat",
                    json={
                        "prompt": "检查这条小查询",
                        "dialect": "DWS",
                        "attachment_ids": [small_att["attachment_id"]],
                    },
                ).json()
                self.assertTrue(r_inline["ok"])
                self.assertIn("【用户本轮附带文件全文】", seen_prompts[-1])
                self.assertIn("SELECT id, amt FROM dwd.orders WHERE status = 'PAID';", seen_prompts[-1])

                detail_inline = client.get(
                    f"/api/modules/ai_assistant/sessions/{r_inline['session']['session_id']}"
                ).json()
                user_msgs = [m for m in detail_inline["session"]["messages"] if m["role"] == "user"]
                self.assertEqual(len(user_msgs), 1)
                self.assertEqual(user_msgs[0]["content"], "检查这条小查询")
                self.assertNotIn("【用户本轮附带文件全文】", user_msgs[0]["content"])
                self.assertEqual(user_msgs[0]["attachments"][0]["attachment_id"], small_att["attachment_id"])

                # 5c. AssistantToolbox.tool_query_csv_attachment 写入语句只读沙箱拦截
                from modules.ai_assistant.tools import AssistantToolbox
                from modules.ai_assistant.service import AiAssistantService

                tb = AssistantToolbox(
                    ctx=ctx,
                    work_mode="dialect",
                    profile=None,
                    dialect="DWS",
                    default_schema="public",
                    attachments=[big_csv_att],
                    storage=AiAssistantService(ctx),
                )
                blk = tb.tool_query_csv_attachment(
                    {
                        "attachment_id": big_csv_att["attachment_id"],
                        "sql": "DELETE FROM csv_data",
                    }
                )
                self.assertFalse(blk["ok"])
                self.assertTrue(blk.get("blocked_by_sandbox"))

            # 6. 工作区导出隔离（严禁包含 ai_assistant_attachments / chunks）与级联删除清理
            ws_bytes, _ = ctx.workspace_hub.export_workspace_bytes()
            ws_file = os.path.join(tmp, "att_ws_check.dataog.db")
            with open(ws_file, "wb") as fh:
                fh.write(ws_bytes)
            with contextlib.closing(sqlite3.connect(ws_file)) as wconn:
                tbls = {
                    r[0]
                    for r in wconn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertFalse(any("ai_assistant_" in t for t in tbls))

            del_sess = client.delete(f"/api/modules/ai_assistant/sessions/{sid_sql}")
            self.assertEqual(del_sess.status_code, 200)
            with ctx.store.module_db(scope="private") as pconn:
                rem_att = pconn.execute(
                    "SELECT COUNT(*) FROM ai_assistant_attachments WHERE attachment_id = ?",
                    (batch_sp_att["attachment_id"],),
                ).fetchone()[0]
                rem_chk = pconn.execute(
                    "SELECT COUNT(*) FROM ai_assistant_attachment_chunks WHERE attachment_id = ?",
                    (batch_sp_att["attachment_id"],),
                ).fetchone()[0]
                rem_csv_keep = pconn.execute(
                    "SELECT COUNT(*) FROM ai_assistant_attachments WHERE session_id = ?",
                    (sid_csv,),
                ).fetchone()[0]
            self.assertEqual(rem_att, 0)
            self.assertEqual(rem_chk, 0)
            self.assertEqual(rem_csv_keep, 1)

    def test_ai_assistant_sse_streaming_thinking_and_natural_markdown(self) -> None:
        """验证 AI 助手 /chat/stream 真实 SSE 帧、Thinking 推理聚合、自然 Markdown+dataog_meta 剥离及错误脱敏."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(private_root=os.path.join(tmp, "vault"))
            ctx: PlatformContext = app.state.platform_ctx
            _activate_ctx(ctx)
            ctx.llm_hub.save_provider(
                {
                    "provider_id": "deepseek",
                    "name": "DeepSeek",
                    "provider_type": "DEEPSEEK",
                    "enabled": True,
                    "api_key": "sk-ds-stream-123456",
                    "default_model": "deepseek-reasoner",
                    "models": ["deepseek-reasoner", "deepseek-chat"],
                    "is_default": True,
                }
            )
            client = TestClient(app)

            stream_call_no = {"n": 0}

            def _mock_stream(**kwargs):
                stream_call_no["n"] += 1
                self.assertEqual(kwargs.get("thinking_mode"), "enabled")
                if stream_call_no["n"] == 1:
                    yield {"type": "reasoning", "delta": "首先核查可用知识库与表结构"}
                    yield {"type": "reasoning", "delta": " -> 调用 search_knowledge 工具"}
                    act_text = (
                        "我先为您检索一次语法知识库：\n\n"
                        "```dataog_action\n"
                        '{"action":"tool","tool":"search_knowledge","args":{"query":"DISTRIBUTE"},"purpose":"检索 DWS 分布键规范"}\n'
                        "```"
                    )
                    yield {"type": "content", "delta": act_text}
                    yield {
                        "type": "done",
                        "provider_name": "DeepSeek",
                        "model": "deepseek-reasoner",
                        "reasoning": "首先核查可用知识库与表结构 -> 调用 search_knowledge 工具",
                        "content": act_text,
                        "cancelled": False,
                    }
                    return

                yield {"type": "reasoning", "delta": "已获得知识库线索，组织 Markdown 结论"}
                md_parts = [
                    "### 分布键选型分析\n\n",
                    "建议优先选择**高基数等值连接键**作为 `DISTRIBUTE BY HASH`。\n\n",
                    "```dataog_meta\n",
                    '{"summary":"完成分布键分析","citations":[],"next_steps":[{"label":"检查倾斜率","prompt":"查看数据倾斜"}]}\n',
                    "```",
                ]
                for p in md_parts:
                    yield {"type": "content", "delta": p}
                yield {
                    "type": "done",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-reasoner",
                    "reasoning": "已获得知识库线索，组织 Markdown 结论",
                    "content": "".join(md_parts),
                    "cancelled": False,
                }

            def _parse_sse(raw_text: str):
                events = []
                for frame in raw_text.split("\n\n"):
                    frame = frame.strip()
                    if not frame:
                        continue
                    ev_name = "message"
                    data_lines = []
                    for ln in frame.splitlines():
                        if ln.startswith(":"):
                            continue
                        if ln.startswith("event:"):
                            ev_name = ln.split(":", 1)[1].strip()
                        elif ln.startswith("data:"):
                            data_lines.append(ln.split(":", 1)[1].strip())
                    if data_lines:
                        events.append((ev_name, json.loads("\n".join(data_lines))))
                return events

            with unittest.mock.patch.object(ctx.llm_hub, "chat_completion_stream", side_effect=_mock_stream):
                resp = client.post(
                    "/api/modules/ai_assistant/chat/stream",
                    json={
                        "prompt": "DWS 分布键怎么选？",
                        "work_mode": "dialect",
                        "dialect": "DWS",
                        "provider_id": "deepseek",
                        "model": "deepseek-reasoner",
                        "thinking_mode": "enabled",
                    },
                )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
            sse_events = _parse_sse(resp.text)
            ev_types = [t for t, _ in sse_events]
            for expected_ev in (
                "session_init",
                "thinking_start",
                "thinking_delta",
                "thinking_end",
                "content_reset",
                "tool_start",
                "tool_end",
                "content_delta",
                "done",
            ):
                self.assertIn(expected_ev, ev_types)

            done_data = [d for t, d in sse_events if t == "done"][-1]
            asst_msg = done_data["assistant_message"]
            self.assertIn("分布键选型分析", asst_msg["content"])
            self.assertNotIn("dataog_action", asst_msg["content"])
            self.assertNotIn("dataog_meta", asst_msg["content"])
            self.assertIn("首先核查可用知识库与表结构", asst_msg["reasoning"])
            self.assertIn("已获得知识库线索", asst_msg["reasoning"])
            self.assertGreaterEqual(float(asst_msg["reasoning_ms"]), 0.0)
            self.assertEqual(asst_msg["next_steps"][0]["label"], "检查倾斜率")

            sid = done_data["session"]["session_id"]
            sess_detail = client.get(f"/api/modules/ai_assistant/sessions/{sid}").json()["session"]
            self.assertEqual(sess_detail["thinking_mode"], "enabled")
            saved_asst = [m for m in sess_detail["messages"] if m["role"] == "assistant"][-1]
            self.assertIn("首先核查可用知识库与表结构", saved_asst["reasoning"])

            # 验证 SSE 异常事件严格脱敏私有保险箱物理路径
            vault_path = os.path.join(tmp, "vault")
            with unittest.mock.patch.object(
                ctx.llm_hub,
                "chat_completion_stream",
                side_effect=RuntimeError(f"boom while reading {vault_path}/secret.db"),
            ):
                err_resp = client.post(
                    "/api/modules/ai_assistant/chat/stream",
                    json={"prompt": "触发错误脱敏测试", "dialect": "DWS", "session_id": sid},
                )
            self.assertEqual(err_resp.status_code, 200)
            err_events = _parse_sse(err_resp.text)
            err_payload = [d for t, d in err_events if t == "error"][-1]
            self.assertNotIn(vault_path, err_payload["error"])

    def test_frontend_ai_assistant_and_knowledge_modules_via_node(self) -> None:
        """全静态防呆扫描 + Node.js 运行时真实交互端到端验证 v3.1.0 AI 助手（含粘贴/上传/防爆窗拆解）、提案卡片、抽屉及参考知识库."""
        static_root = resolve_static_dir()
        js_root = os.path.join(static_root, "js")
        for dirpath, _dirnames, filenames in os.walk(js_root):
            for fname in filenames:
                if not fname.endswith(".js"):
                    continue
                fpath = os.path.join(dirpath, fname)
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
                self.assertNotIn(
                    ".activityBar.run(",
                    src,
                    f"{os.path.relpath(fpath, static_root)} 禁止调用不存在的 activityBar.run()，必须使用 TaskActivityBar.track()",
                )

        if getattr(sys, "frozen", False) or not shutil.which("node"):
            return

        project_root = os.path.dirname(static_root)
        node_script = r"""
import path from "node:path";
import { pathToFileURL } from "node:url";

class MockNode {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.id = "";
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = {};
    this.dataset = {};
    this.className = "";
    this.value = "";
    this.textContent = "";
    this.title = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.selected = false;
    this.parentNode = null;
    this.scrollTop = 0;
    this.scrollHeight = 120;
    const self = this;
    this.classList = {
      add(...cs) { self.className = [...new Set([...(self.className||"").split(/\s+/).filter(Boolean), ...cs])].join(" "); },
      remove(...cs) { const s = new Set(cs); self.className = (self.className||"").split(/\s+/).filter(x => x && !s.has(x)).join(" "); },
      toggle(c, f) { const has = (self.className||"").split(/\s+/).includes(c); const on = f === undefined ? !has : Boolean(f); if (on) this.add(c); else this.remove(c); },
      contains(c) { return (self.className||"").split(/\s+/).includes(c); },
    };
  }
  append(...ns) { for (const n of ns) if (n) this.appendChild(n); }
  appendChild(c) { if (c) { c.parentNode = this; this.children.push(c); } return c; }
  replaceChildren(...ns) { this.children = ns.filter(Boolean); this.children.forEach(c => c.parentNode = this); }
  setAttribute(k, v) {
    const s = String(v);
    this.attributes.set(k, s);
    if (k === "id") this.id = s;
    if (k === "title") this.title = s;
  }
  getAttribute(k) {
    if (k === "id" && this.id) return this.id;
    if (k === "title" && this.title) return this.title;
    return this.attributes.get(k) ?? null;
  }
  addEventListener(e, fn) { (this.listeners.get(e) || this.listeners.set(e, []).get(e)).push(fn); }
  removeEventListener(e, fn) {
    const arr = this.listeners.get(e);
    if (arr) this.listeners.set(e, arr.filter((f) => f !== fn));
  }
  contains(target) {
    if (!target) return false;
    if (target === this) return true;
    return this.children.some((c) => typeof c.contains === "function" && c.contains(target));
  }
  click() {
    if (this.disabled) return;
    const fns = this.listeners.get("click") || [];
    for (const fn of fns) fn({ target: this, stopPropagation() {}, preventDefault() {} });
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const match = (n) => {
      if (!n || !sel) return false;
      if (sel.startsWith("#")) return n.id === sel.slice(1);
      if (sel.startsWith(".")) return (n.className || "").split(/\s+/).includes(sel.slice(1));
      return String(n.tagName || "").toLowerCase() === sel.toLowerCase();
    };
    const walk = (n) => {
      for (const c of (n.children || [])) {
        if (match(c)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  closest() { return null; }
  focus() {}
  remove() {
    if (this.parentNode && Array.isArray(this.parentNode.children)) {
      this.parentNode.children = this.parentNode.children.filter((x) => x !== this);
      this.parentNode = null;
    }
  }
}

function getNodeText(node) {
  if (!node) return "";
  let s = node.textContent || "";
  for (const c of (node.children || [])) s += getNodeText(c);
  return s;
}

function findNodes(root, pred) {
  const res = [];
  const walk = (n) => {
    if (!n) return;
    if (pred(n)) res.push(n);
    for (const c of (n.children || [])) walk(c);
  };
  walk(root);
  return res;
}

function triggerInput(node, value) {
  node.value = value;
  for (const fn of (node.listeners.get("input") || [])) fn({ target: node });
}

async function flushAsync(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((r) => setTimeout(r, 8));
  }
}

const toastRoot = new MockNode("div");
toastRoot.id = "toast-root";

globalThis.Node = MockNode;
globalThis.HTMLDialogElement = class HTMLDialogElement {};
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.window = {
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
  innerWidth: 1440,
  innerHeight: 900,
};
globalThis.document = {
  head: new MockNode("head"),
  body: new MockNode("body"),
  createElement: (tag) => new MockNode(tag),
  createElementNS: (_ns, tag) => new MockNode(tag),
  createTextNode: (t) => Object.assign(new MockNode("#text"), { textContent: String(t) }),
  createDocumentFragment: () => new MockNode("#frag"),
  getElementById: (id) => (id === "toast-root" ? toastRoot : null),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
};

const fetchCalls = [];
const mockState = { sessions: [], sseMode: "none", sseStage: "" };

const stdProposal = {
  proposal_id: "prop_std",
  connection_id: "conn_dws_1",
  dialect: "DWS",
  schema_name: "dwd",
  title: "创建订单汇总表",
  rationale: "按客户分布存储",
  risk_level: "STANDARD_WRITE",
  status: "pending",
  original_sql: "CREATE TABLE dwd.agg_orders (cust_id BIGINT, amt NUMERIC(18,2));",
  statements: [{ index: 1 }],
  warnings: [],
};

const hiProposal = {
  proposal_id: "prop_hi",
  connection_id: "conn_dws_1",
  dialect: "DWS",
  schema_name: "dwd",
  title: "清理测试订单",
  rationale: "清理脏数据",
  risk_level: "HIGH_RISK",
  status: "pending",
  original_sql: "DELETE FROM dwd.orders WHERE status = 'BAD';",
  statements: [{ index: 1 }],
  warnings: ["高危删除"],
  estimated_rows: 2,
};

globalThis.fetch = async (rawUrl, opts = {}) => {
  const url = String(rawUrl || "");
  const method = String(opts.method || "GET").toUpperCase();
  const headers = opts.headers || {};
  let bodyObj = null;
  if (typeof opts.body === "string") {
    try { bodyObj = JSON.parse(opts.body); } catch (_e) { bodyObj = opts.body; }
  }
  fetchCalls.push({ url, method, headers, body: bodyObj });

  let payload = { ok: true };
  if (url.includes("/api/modules/ai_assistant/dialects")) {
    payload = {
      ok: true,
      dialects: [
        { code: "DWS", label: "华为云 GaussDB(DWS)" },
        { code: "PGSQL", label: "PostgreSQL" },
      ],
      llm_models: [
        { provider_id: "deepseek", provider_name: "DeepSeek", model: "deepseek-reasoner", ready: false, has_api_key: false, is_default: true, context_window: 128000, attachment_budget: 80000, supports_thinking: true },
        { provider_id: "custom", provider_name: "内网Qwen", model: "qwen-coder", ready: true, has_api_key: true, is_default: false, context_window: 128000, attachment_budget: 80000, supports_thinking: false },
        { provider_id: "local32", provider_name: "本地轻量32K", model: "small-32k", ready: true, has_api_key: true, is_default: false, context_window: 32000, attachment_budget: 20800, supports_thinking: false },
      ],
      attachment_limits: {
        max_csv_bytes: 2 * 1024 * 1024,
        max_text_bytes: 5 * 1024 * 1024,
        inline_token_budget: 32000,
        max_attachments_per_turn: 5,
        csv_extensions: [".csv", ".tsv"],
        allowed_extensions: [".txt", ".sql", ".ddl", ".md", ".log", ".json", ".csv", ".tsv"],
      },
      knowledge_summary: {},
    };
  } else if (url === "/api/modules/ai_assistant/attachments/upload") {
    payload = {
      ok: true,
      attachment: {
        attachment_id: "att_file_1",
        filename: "tiny.sql",
        file_ext: ".sql",
        file_kind: "sql",
        size_bytes: 120,
        line_count: 3,
        row_count: 0,
        estimated_tokens: 45,
        chunk_count: 1,
        oversized: false,
        recommended_strategy: "inline",
        summary: "SQL 脚本：3 行 | 估算 ~45 tokens | 切分为 1 个语义块",
        csv_profile: {},
        chunks: [{ chunk_index: 0, title: "语句 #1", range_label: "L1-L3", estimated_tokens: 45, content: "SELECT 1;" }],
      },
    };
  } else if (url === "/api/modules/ai_assistant/attachments/text") {
    payload = {
      ok: true,
      attachment: {
        attachment_id: "att_paste_1",
        filename: "pasted_sql_120000.sql",
        file_ext: ".sql",
        file_kind: "sql",
        size_bytes: 18000,
        line_count: 220,
        row_count: 0,
        estimated_tokens: 9200,
        chunk_count: 10,
        oversized: true,
        recommended_strategy: "stepped_batch",
        summary: "SQL 脚本：220 行 | 估算 ~9200 tokens | 切分为 10 个语义块",
        csv_profile: {},
        chunks: [{ chunk_index: 0, title: "语句 #1~#3", range_label: "L1-L40", estimated_tokens: 2100, content: "CREATE TABLE t1 (id INT);" }],
      },
    };
  } else if (url.startsWith("/api/modules/ai_assistant/attachments/") && method === "DELETE") {
    payload = { ok: true, message: "附件已移除" };
  } else if (url.startsWith("/api/modules/ai_assistant/attachments/")) {
    payload = {
      ok: true,
      attachment: {
        attachment_id: "att_paste_1",
        filename: "pasted_sql_120000.sql",
        file_ext: ".sql",
        file_kind: "sql",
        size_bytes: 18000,
        line_count: 220,
        row_count: 0,
        estimated_tokens: 9200,
        chunk_count: 10,
        oversized: true,
        recommended_strategy: "stepped_batch",
        summary: "SQL 脚本：220 行 | 估算 ~9200 tokens | 切分为 10 个语义块",
        csv_profile: {},
        chunks: [{ chunk_index: 0, title: "语句 #1~#3", range_label: "L1-L40", estimated_tokens: 2100, content: "CREATE TABLE t1 (id INT);" }],
      },
    };
  } else if (url.endsWith("/api/modules/ai_assistant/sessions") || url.includes("/api/modules/ai_assistant/sessions?")) {
    payload = { ok: true, sessions: mockState.sessions };
  } else if (url.startsWith("/api/modules/ai_assistant/chat")) {
    if (mockState.sseMode === "stream" || mockState.sseMode === "abortable") {
      const encoder = new TextEncoder();
      const asstStreamMsg = {
        id: `msg_stream_${fetchCalls.length}`,
        role: "assistant",
        response_type: "answer",
        reasoning: "深度思考推导中：检查分布键与表关联",
        reasoning_ms: 420.5,
        content: "### 流式回答\n\n第一段流式正文 + 第二段追加",
        steps: [{ step: 1, tool: "search_knowledge", purpose: "检索方言文档", ok: true, elapsed_ms: 9.0, summary: "命中 1 条" }],
        citations: [],
        next_steps: [],
        provider_name: "DeepSeek",
        model: bodyObj?.model || "deepseek-reasoner",
        created_at: "2026-11-24 12:00:00",
        total_elapsed_ms: 65.0,
      };
      const streamSess = {
        session_id: "sess_stream_1",
        title: bodyObj?.prompt || "流式对话",
        work_mode: "connection",
        connection_id: "conn_dws_1",
        dialect: "DWS",
        schema_name: "dwd",
        focus_tables: [],
        task_mode: "auto",
        thinking_mode: bodyObj?.thinking_mode || "enabled",
        turn_count: 1,
        messages: [
          { id: "msg_stream_u", role: "user", content: bodyObj?.prompt || "", attachments: [] },
          asstStreamMsg,
        ],
      };
      const stream = new ReadableStream({
        start(controller) {
          const pushEv = (evObj) => {
            const evName = evObj.type || "message";
            controller.enqueue(encoder.encode(`event: ${evName}\ndata: ${JSON.stringify(evObj)}\n\n`));
          };
          if (mockState.sseMode === "abortable") {
            pushEv({
              type: "session_init",
              session: { session_id: "sess_abort_1" },
              strategy: "full_context",
              context_window: 128000,
              thinking_mode: bodyObj?.thinking_mode || "auto",
            });
            const onAbort = () => {
              controller.error(Object.assign(new Error("The operation was aborted"), { name: "AbortError" }));
            };
            if (opts.signal?.aborted) {
              onAbort();
            } else {
              opts.signal?.addEventListener("abort", onAbort, { once: true });
            }
            return;
          }
          pushEv({
            type: "session_init",
            session: { session_id: "sess_stream_1" },
            strategy: "full_context",
            context_window: 128000,
            thinking_mode: bodyObj?.thinking_mode || "enabled",
          });
          pushEv({ type: "thinking_start", step: 1 });
          pushEv({ type: "thinking_delta", step: 1, delta: "深度思考推导中：检查分布键与表关联" });
          setTimeout(() => {
            pushEv({ type: "thinking_end", step: 1, elapsed_ms: 420.5 });
            pushEv({ type: "tool_start", step: 1, tool: "search_knowledge", purpose: "检索方言文档" });
            pushEv({ type: "tool_end", step: 1, tool: "search_knowledge", ok: true, elapsed_ms: 9.0, summary: "命中 1 条" });
            pushEv({ type: "content_delta", delta: "### 流式回答\n\n第一段流式正文" });
            pushEv({ type: "content_delta", delta: " + 第二段追加" });
          }, 50);
          setTimeout(() => {
            mockState.sessions = [streamSess];
            pushEv({
              type: "done",
              ok: true,
              session: streamSess,
              user_message: streamSess.messages[0],
              assistant_message: asstStreamMsg,
            });
            controller.close();
          }, 130);
        },
      });
      return {
        ok: true,
        status: 200,
        headers: new Map([["content-type", "text/event-stream"]]),
        body: stream,
      };
    }
    const reqAtts = Array.isArray(bodyObj?.attachment_ids) ? bodyObj.attachment_ids : [];
    const reqBatchStart = Number(bodyObj?.batch_start_chunk || 0);
    const nextStepsList = reqAtts.length && reqBatchStart === 0
      ? [
          {
            label: "继续拆解第 9~10 片 (共 10 片)",
            prompt: "请继续拆解第 9~10 片",
            batch_start_chunk: 8,
            batch_max_chunks: 8,
            attachment_ids: reqAtts,
            large_payload_strategy: "stepped_batch",
          },
        ]
      : [{ label: "查看分布倾斜", prompt: "查看 dwd.orders 分布倾斜情况" }];
    const attMeta = reqAtts.map((aid) => ({
      attachment_id: aid,
      filename: aid === "att_file_1" ? "tiny.sql" : "pasted_sql_120000.sql",
      file_kind: "sql",
      estimated_tokens: aid === "att_file_1" ? 45 : 9200,
      chunk_count: aid === "att_file_1" ? 1 : 10,
    }));
    const asstMsg = {
      id: `msg_asst_${fetchCalls.length}`,
      role: "assistant",
      response_type: "proposal",
      content: "# 分析结论\n\n已查询表结构与知识库：\n\n```sql\nSELECT COUNT(*) FROM dwd.orders;\n```",
      steps: [{ step: 1, tool: "search_knowledge", purpose: "检索 DWS 建表规范", ok: true, elapsed_ms: 11.2, summary: "命中 1 条知识" }],
      citations: [{ chunk_id: "doc1::c0000", doc_id: "doc1", doc_title: "DWS 9.1 规范", dialect: "DWS", heading_path: "分布键规则", snippet: "DISTRIBUTE BY HASH" }],
      result_table: { columns: [{ name: "cnt" }], rows: [[42]], row_count: 1, truncated: false, elapsed_ms: 4.5 },
      proposal: { ...stdProposal },
      next_steps: nextStepsList,
      provider_name: "内网Qwen",
      model: "qwen-coder",
      created_at: "2026-11-24 11:00:00",
      total_elapsed_ms: 28.0,
    };
    const sess = {
      session_id: "sess_1",
      title: "统计 dwd.orders 总额",
      work_mode: "connection",
      connection_id: "conn_dws_1",
      dialect: "DWS",
      schema_name: "dwd",
      focus_tables: [],
      task_mode: "auto",
      turn_count: 1,
      messages: [
        { id: "msg_u_1", role: "user", content: bodyObj?.prompt || "结合附件分析", attachments: attMeta },
        asstMsg,
      ],
    };
    mockState.sessions = [sess];
    payload = { ok: true, session: sess, user_message: sess.messages[0], assistant_message: asstMsg };
  } else if (url.includes("/api/modules/ai_assistant/proposals/prop_std/confirm")) {
    payload = {
      ok: true,
      execution_ok: true,
      catalog_refreshed: true,
      proposal: {
        ...stdProposal,
        status: "executed",
        execution_result: { statements: [{ index: 1, primary_verb: "CREATE", ok: true, affected_rows: 0, elapsed_ms: 3.1 }] },
      },
      session: mockState.sessions[0],
    };
  } else if (url.includes("/api/modules/ai_assistant/proposals/prop_hi/confirm")) {
    payload = {
      ok: true,
      execution_ok: true,
      catalog_refreshed: false,
      proposal: {
        ...hiProposal,
        status: "executed",
        execution_result: { statements: [{ index: 1, primary_verb: "DELETE", ok: true, affected_rows: 2, elapsed_ms: 2.5 }] },
      },
      session: mockState.sessions[0],
    };
  } else if (url.includes("/proposals/") && url.endsWith("/reject")) {
    payload = { ok: true, proposal: { ...hiProposal, status: "rejected" }, session: mockState.sessions[0], message: "已取消执行该语句" };
  } else if (url.includes("/api/platform/knowledge/chunks/")) {
    payload = {
      ok: true,
      document: { doc_id: "doc1", title: "DWS 9.1 规范", dialect: "DWS" },
      sections: [{ chunk_id: "doc1::c0000", heading_path: "分布键规则", content: "优先选择高基数列作为 DISTRIBUTE BY HASH。" }],
    };
  } else if (url === "/api/platform/knowledge/search") {
    payload = {
      ok: true,
      retrieval_mode: "fts5",
      hits: [{ chunk_id: "doc1::c0000", doc_title: "DWS 9.1 规范", dialect: "DWS", is_builtin: false, heading_path: "分布键规则", snippet: "DISTRIBUTE BY HASH", score: 0.96 }],
    };
  } else if (url === "/api/platform/metadata/conn_dws_1/refresh") {
    payload = {
      ok: true,
      schemas: ["dwd", "dm"],
      tables: [
        { schema_name: "dwd", table_name: "orders", table_comment: "订单" },
        { schema_name: "dm", table_name: "agg_orders", table_comment: "订单汇总" },
      ],
      message: "成功刷新元数据缓存（共 2 个模式、2 个对象）",
    };
  } else if (url.startsWith("/api/platform/metadata/conn_dws_1/tree")) {
    payload = {
      ok: true,
      schemas: ["dwd", "dm"],
      tables: [{ schema_name: "dwd", table_name: "orders" }, { schema_name: "dm", table_name: "agg_orders" }],
    };
  } else if (url === "/api/platform/knowledge/summary") {
    payload = {
      ok: true,
      summary: { fts5_available: true, user_docs: 1, builtin_docs: 11, total_chunks: 12, total_bytes: 2048 },
      embedding_config: { enabled: true, provider_id: "", base_url: "http://127.0.0.1:11434/v1", model: "bge-m3", has_api_key: false },
    };
  } else if (url.startsWith("/api/platform/knowledge/documents")) {
    payload = { ok: true, documents: [], summary: { fts5_available: true } };
  } else if (url === "/api/platform/knowledge/embedding-config" && method === "PUT") {
    payload = { ok: true, embedding_config: { enabled: true, base_url: "http://127.0.0.1:11434/v1", model: "bge-m3", has_api_key: false }, message: "已保存" };
  } else if (url.startsWith("/api/platform/knowledge/embeddings/reindex")) {
    payload = { ok: true, embedded_chunks: 12, message: "向量索引重建完成" };
  } else if (url.startsWith("/api/platform/tasks/")) {
    payload = { ok: true, status: "running", stage: "执行中", percent: 50, logs: [] };
  }

  return {
    ok: true,
    status: 200,
    headers: new Map([["content-type", "application/json"]]),
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
};

const root = process.argv[1];
const imp = (rel) => import(pathToFileURL(path.join(root, rel)).href);

const { store } = await imp("static/js/core/state.js");
store.state.connections = [
  { connection_id: "conn_dws_1", name: "DWS生产集群", db_type: "DWS", default_schema: "dwd" },
];
store.state.catalogTrees = {
  conn_dws_1: { schemas: ["dwd"], tables: [{ schema_name: "dwd", table_name: "orders" }] },
};

const { getDesktopApp, loadAppInstance } = await imp("static/js/core/app_registry.js");
await imp("static/js/apps/index.js");

const manifest = getDesktopApp("ai_assistant");
if (!manifest || manifest.title !== "Data.O.G") {
  throw new Error("ai_assistant not registered with expected title Data.O.G");
}

// 场景 1：真实挂载 AiAssistantView、验证自动选中已就绪模型 + sendUserMessage 全链路闭环
const view = await loadAppInstance("ai_assistant");
const host = new MockNode("div");
await view.mount(host);
const hdr = view.getHeaderParts();
if (!hdr?.center || !hdr?.right) throw new Error("AiAssistantView header parts missing");

const parsedSel = view._parseSelectedModel();
if (parsedSel.providerId !== "custom" || parsedSel.model !== "qwen-coder") {
  throw new Error(`Expected ready model custom::qwen-coder, got ${JSON.stringify(parsedSel)}`);
}

await view.sendUserMessage("统计 dwd.orders 总额");
const errToasts1 = toastRoot.children.filter((c) => c.classList.contains("toast-error"));
if (errToasts1.length > 0) {
  throw new Error(`Unexpected toast-error after sendUserMessage: ${errToasts1.map(getNodeText).join("; ")}`);
}

const chatCall = fetchCalls.find((c) => c.url.startsWith("/api/modules/ai_assistant/chat"));
if (!chatCall) throw new Error("sendUserMessage did not call /api/modules/ai_assistant/chat");
if (!String(chatCall.headers["X-Task-Id"] || "").startsWith("asst_")) {
  throw new Error(`Missing asst_ X-Task-Id header on chat request: ${JSON.stringify(chatCall.headers)}`);
}
if (chatCall.body?.provider_id !== "custom" || chatCall.body?.model !== "qwen-coder") {
  throw new Error(`Unexpected model payload in chat request: ${JSON.stringify(chatCall.body)}`);
}
if (!fetchCalls.some((c) => c.url.includes("/api/modules/ai_assistant/sessions"))) {
  throw new Error("Sessions list not refreshed after chat turn");
}

const renderedText = getNodeText(view.root);
if (!renderedText.includes("创建订单汇总表") || !renderedText.includes("DWS 9.1 规范") || !renderedText.includes("查看分布倾斜")) {
  throw new Error("Assistant response sections (proposal / citations / next_steps) not rendered");
}
if (!view.root.querySelectorAll(".ai-asst-result-box").length) {
  throw new Error("Result DataGrid not rendered for result_table");
}

// 场景 2：提案卡片真实点击（常规写确认 + 高危 CONFIRM 拦截与放行 + 取消提案）
const confirmBtn1 = findNodes(view.root, (n) => n.tagName === "BUTTON" && getNodeText(n).includes("确认执行"))[0];
if (!confirmBtn1) throw new Error("Standard proposal 确认执行 button missing");
confirmBtn1.click();
await flushAsync();

const stdConfirmCall = fetchCalls.find((c) => c.url.includes("/proposals/prop_std/confirm"));
if (!stdConfirmCall) throw new Error("Standard proposal confirm request not triggered on click");
if (!fetchCalls.some((c) => c.url.includes("/api/platform/metadata/conn_dws_1/tree"))) {
  throw new Error("Metadata catalog tree refresh not triggered after catalog_refreshed DDL");
}

const { renderProposalCard } = await imp("static/js/apps/ai_assistant/proposal_card.js");
let rejectedCalled = false;
let hiConfirmed = false;
const hiCard = renderProposalCard(hiProposal, {
  activeConnectionId: "conn_dws_1",
  onProposalUpdated: (res) => {
    if (res?.proposal?.status === "executed") hiConfirmed = true;
    if (res?.proposal?.status === "rejected") rejectedCalled = true;
  },
});
const hiExecBtn = findNodes(hiCard, (n) => n.tagName === "BUTTON" && getNodeText(n).includes("确认执行"))[0];
const hiRejectBtn = findNodes(hiCard, (n) => n.tagName === "BUTTON" && getNodeText(n) === "取消不执行")[0];
const hiInput = findNodes(hiCard, (n) => n.tagName === "INPUT")[0];
if (!hiExecBtn || !hiRejectBtn || !hiInput) throw new Error("High-risk proposal controls missing");

const beforeHiCount = fetchCalls.filter((c) => c.url.includes("/proposals/prop_hi/confirm")).length;
hiExecBtn.click();
await flushAsync(3);
const afterBlockedCount = fetchCalls.filter((c) => c.url.includes("/proposals/prop_hi/confirm")).length;
if (afterBlockedCount !== beforeHiCount) {
  throw new Error("High-risk proposal should be blocked cleanly before entering CONFIRM");
}
toastRoot.replaceChildren();

triggerInput(hiInput, "CONFIRM");
hiExecBtn.click();
await flushAsync();
const hiConfirmCall = fetchCalls.find((c) => c.url.includes("/proposals/prop_hi/confirm"));
if (!hiConfirmCall || hiConfirmCall.body?.confirm_token !== "CONFIRM" || !hiConfirmed) {
  throw new Error("High-risk proposal did not submit CONFIRM token properly");
}
const rejCard = renderProposalCard(
  { ...hiProposal, proposal_id: "prop_rej" },
  {
    activeConnectionId: "conn_dws_1",
    onProposalUpdated: (res) => {
      if (res?.proposal?.status === "rejected") rejectedCalled = true;
    },
  }
);
const rejBtn = findNodes(rejCard, (n) => n.tagName === "BUTTON" && getNodeText(n) === "取消不执行")[0];
if (!rejBtn) throw new Error("Proposal reject button missing on fresh card");
rejBtn.click();
await flushAsync();
if (!rejectedCalled) {
  throw new Error("Proposal reject action did not trigger callback");
}

// 场景 3：右侧抽屉（知识引用定位 + 知识搜索 + 库表目录顶层 schemas/tables 刷新）
await view.drawer.openCitation("doc1::c0000");
if (!fetchCalls.some((c) => c.url.includes("/api/platform/knowledge/chunks/doc1%3A%3Ac0000?window=1"))) {
  throw new Error("openCitation did not request knowledge chunk section");
}
if (!getNodeText(view.drawer.root).includes("优先选择高基数列")) {
  throw new Error("openCitation did not render fetched section content");
}

view.drawer.kbQuery = "DISTRIBUTE";
await view.drawer.searchKb();
if (!fetchCalls.some((c) => c.url === "/api/platform/knowledge/search")) {
  throw new Error("drawer.searchKb did not request /api/platform/knowledge/search");
}

view.drawer.activeTab = "tables";
view.drawer.render();
const refreshBtn = findNodes(view.drawer.root, (n) => n.tagName === "BUTTON" && n.title === "刷新库表目录")[0];
if (!refreshBtn) throw new Error("Drawer 刷新库表目录 button missing");
refreshBtn.click();
await flushAsync();
const updatedTree = store.state.catalogTrees.conn_dws_1;
if (!updatedTree || updatedTree.table_count !== 2 || updatedTree.schema_count !== 2) {
  throw new Error(`Drawer metadata refresh failed to handle flat schemas/tables response: ${JSON.stringify(updatedTree)}`);
}

// 场景 4：KnowledgeSettingsView 真实刷新、检索测试与重建向量索引（TaskActivityBar.track）
const { KnowledgeSettingsView } = await imp("static/js/views/knowledge_view.js");
const kbView = new KnowledgeSettingsView();
await kbView.refreshFromServer();
kbView.testQuery = "DISTRIBUTE";
await kbView._runSearchTest();
kbView.embeddingExpanded = true;
kbView.render();

toastRoot.replaceChildren();
const reindexBtn = findNodes(kbView.root, (n) => n.tagName === "BUTTON" && getNodeText(n) === "保存并重建向量索引")[0];
if (!reindexBtn) throw new Error("KnowledgeSettingsView 保存并重建向量索引 button missing");
reindexBtn.click();
await flushAsync();

const reindexCall = fetchCalls.find((c) => c.url.startsWith("/api/platform/knowledge/embeddings/reindex"));
if (!reindexCall || !String(reindexCall.headers["X-Task-Id"] || "").startsWith("kb_emb_")) {
  throw new Error(`Knowledge reindexEmbeddings failed or missing kb_emb_ X-Task-Id: ${JSON.stringify(reindexCall)}`);
}
const finalErrToasts = toastRoot.children.filter((c) => c.classList.contains("toast-error"));
if (finalErrToasts.length > 0) {
  throw new Error(`Unexpected error toasts in v3 frontend E2E: ${finalErrToasts.map(getNodeText).join("; ")}`);
}

const { renderSafeMarkdown } = await imp("static/js/apps/ai_assistant/markdown_dom.js");
const mdNode = renderSafeMarkdown("# 标题\n\n> 引用\n\n```sql\nSELECT 1;\n```\n\n| A | B |\n| --- | --- |\n| 1 | 2 |");
if (!mdNode.children.length) throw new Error("renderSafeMarkdown produced empty DOM");

// 场景 5：粘贴文件 + 超长纯文本自动转分片 + 拒绝超大 CSV + 策略切换 + 接续下一批拆解
toastRoot.replaceChildren();
const fakeSqlFile = new File(["SELECT 1;\n"], "tiny.sql", { type: "text/plain" });
await view.handlePasteEvent({
  preventDefault() {},
  clipboardData: { files: [fakeSqlFile], getData: () => "" },
});
if (view.pendingAttachments.length !== 1 || view.pendingAttachments[0].attachment_id !== "att_file_1") {
  throw new Error(`Paste file upload failed: ${JSON.stringify(view.pendingAttachments)}`);
}

const composerTa = view.root.querySelector(".ai-asst-composer-input");
if (!composerTa || !(composerTa.listeners.get("paste") || []).length) {
  throw new Error("Composer textarea paste handler missing");
}
let pastePrevented = false;
const giantPasteSql = "SELECT col_x FROM dwd.orders WHERE id > 0;\n".repeat(160);
for (const fn of composerTa.listeners.get("paste")) {
  await fn({
    preventDefault() { pastePrevented = true; },
    clipboardData: {
      files: [],
      getData: (t) => (t === "text/plain" ? giantPasteSql : ""),
    },
  });
}
if (!pastePrevented) throw new Error("Oversized text paste did not trigger preventDefault()");
if (view.pendingAttachments.length !== 2 || !view.root.querySelector(".ai-asst-strategy-bar")) {
  throw new Error("Oversized pasted text did not render strategy protection bar");
}

const beforeUploadCount = fetchCalls.filter((c) => c.url === "/api/modules/ai_assistant/attachments/upload").length;
await view.handleUploadFiles([{ name: "too_big.csv", size: 2.5 * 1024 * 1024 }]);
const afterUploadCount = fetchCalls.filter((c) => c.url === "/api/modules/ai_assistant/attachments/upload").length;
if (afterUploadCount !== beforeUploadCount) {
  throw new Error("Oversized 2.5MB CSV should be rejected client-side without hitting upload API");
}
const sizeErrToasts = toastRoot.children.filter((c) => c.classList.contains("toast-error"));
if (!sizeErrToasts.length || !getNodeText(sizeErrToasts[0]).includes("超出")) {
  throw new Error("Oversized CSV rejection did not show clear error toast");
}
toastRoot.replaceChildren();

view.largePayloadStrategy = "stepped_batch";
await view.sendUserMessage("");
const attChatCalls = fetchCalls.filter(
  (c) => c.url.startsWith("/api/modules/ai_assistant/chat") && Array.isArray(c.body?.attachment_ids) && c.body.attachment_ids.length === 2
);
if (!attChatCalls.length || attChatCalls[0].body.large_payload_strategy !== "stepped_batch") {
  throw new Error("Attachment chat payload did not transmit attachment_ids and large_payload_strategy");
}
if (view.pendingAttachments.length !== 0) {
  throw new Error("pendingAttachments were not cleared after sending turn");
}
if (!view.root.querySelectorAll(".ai-asst-msg-attach-pill").length) {
  throw new Error("User message bubble did not render sent attachment pills");
}

const contChipBtn = findNodes(view.root, (n) => n.tagName === "BUTTON" && getNodeText(n).includes("继续拆解第 9~10 片"))[0];
if (!contChipBtn) throw new Error("Stepped-batch continuation chip missing from next_steps");
contChipBtn.click();
await flushAsync();
const contChatCall = fetchCalls.find(
  (c) => c.url.startsWith("/api/modules/ai_assistant/chat") && Number(c.body?.batch_start_chunk) === 8
);
if (!contChatCall) {
  throw new Error("Clicking continuation chip did not trigger batch_start_chunk=8 chat call");
}
const postAttachErrs = toastRoot.children.filter((c) => c.classList.contains("toast-error"));
if (postAttachErrs.length > 0) {
  throw new Error(`Unexpected toast-error in Scene 5: ${postAttachErrs.map(getNodeText).join("; ")}`);
}

// 场景 6：v3.3.1 SSE 实时流式输出 + Thinking 展开/自动收起 + 停止生成中断 + 模型上下文容量条 + 结构化大纲预览
toastRoot.replaceChildren();
mockState.sseMode = "stream";
const thinkSelectEl = view.root.querySelector(".ai-asst-think-select");
if (!thinkSelectEl) throw new Error("Thinking mode selector missing in toolbar");
thinkSelectEl.value = "enabled";
for (const fn of (thinkSelectEl.listeners.get("change") || [])) fn({ target: thinkSelectEl });
if (view.thinkingMode !== "enabled") throw new Error("Changing thinking selector did not update view.thinkingMode");

const streamPromise = view.sendUserMessage("流式验证与深度思考");
await new Promise((r) => setTimeout(r, 15));
const liveCard1 = view.root.querySelector(".ai-asst-live-card");
const livePre1 = liveCard1?.querySelector(".ai-asst-thinking-pre");
if (!liveCard1 || !livePre1 || !getNodeText(livePre1).includes("深度思考推导中")) {
  throw new Error("Live streaming card did not expand Thinking panel during thinking phase");
}
const stopBtnMid = findNodes(view.root, (n) => n.tagName === "BUTTON" && getNodeText(n).includes("停止生成"))[0];
if (!stopBtnMid || !stopBtnMid.classList.contains("btn-danger")) {
  throw new Error("Stop generation danger button missing during active streaming");
}

await new Promise((r) => setTimeout(r, 65));
const liveCard2 = view.root.querySelector(".ai-asst-live-card");
if (!liveCard2 || liveCard2.querySelector(".ai-asst-thinking-body")) {
  throw new Error("Thinking box did not auto-collapse after thinking_end event");
}
if (!getNodeText(liveCard2).includes("已深度思考")) {
  throw new Error("Thinking box header missing elapsed label after thinking_end");
}
if (!liveCard2.querySelector(".ai-asst-stream-cursor") || !getNodeText(liveCard2).includes("流式回答")) {
  throw new Error("Live card did not render incremental streamed Markdown and blinking cursor");
}

await streamPromise;
await flushAsync();
const sseReq = fetchCalls.filter((c) => c.url.endsWith("/api/modules/ai_assistant/chat/stream")).slice(-1)[0];
if (!sseReq || sseReq.body?.thinking_mode !== "enabled") {
  throw new Error(`Expected /chat/stream with thinking_mode=enabled, got ${JSON.stringify(sseReq)}`);
}
const histThinkToggle = view.root.querySelector(".ai-asst-thinking-toggle");
if (!histThinkToggle || view.root.querySelector(".ai-asst-thinking-body")) {
  throw new Error("Persisted message thinking box should start collapsed by default");
}
histThinkToggle.click();
if (!view.root.querySelector(".ai-asst-thinking-pre") || !getNodeText(view.root.querySelector(".ai-asst-thinking-pre")).includes("深度思考推导中")) {
  throw new Error("Clicking persisted thinking toggle did not expand reasoning content");
}
view.root.querySelector(".ai-asst-thinking-toggle").click();
if (view.root.querySelector(".ai-asst-thinking-body")) {
  throw new Error("Clicking persisted thinking toggle again did not collapse reasoning panel");
}

// 验证停止生成中断能力（无错误红框、恢复可发送态）
toastRoot.replaceChildren();
mockState.sseMode = "abortable";
const abortPromise = view.sendUserMessage("触发停止生成中断");
await new Promise((r) => setTimeout(r, 15));
const abortBtn = findNodes(view.root, (n) => n.tagName === "BUTTON" && getNodeText(n).includes("停止生成"))[0];
if (!abortBtn) throw new Error("Stop generation button missing during abortable stream");
abortBtn.click();
await abortPromise;
await flushAsync();
if (view.loading) throw new Error("view.loading remained true after clicking Stop Generation");
const abortErrs = toastRoot.children.filter((c) => c.classList.contains("toast-error"));
if (abortErrs.length > 0) throw new Error(`Abort triggered unexpected toast-error: ${abortErrs.map(getNodeText).join("; ")}`);
if (!toastRoot.children.some((c) => getNodeText(c).includes("已停止生成"))) {
  throw new Error("Clicking Stop Generation did not notify user with 已停止生成");
}
mockState.sseMode = "none";

// 验证模型感知上下文容量进度条（128K ok vs 32K warn）与结构大纲预览弹窗
view.pendingAttachments = [
  {
    attachment_id: "att_sp_25k",
    filename: "sp_settle.sql",
    file_kind: "sql",
    size_bytes: 90000,
    line_count: 2600,
    estimated_tokens: 25000,
    chunk_count: 3,
    oversized: false,
    recommended_strategy: "full_context",
    outline: {
      is_stored_procedure: true,
      summary_line: "PROCEDURE dwd.sp_settle | 1 个结构锚点",
      objects: [{ kind: "PROCEDURE", name: "dwd.sp_settle", line: 1 }],
      sections: [{ line: 20, title: "阶段一 清算" }],
      referenced_tables: ["dwd.orders"],
    },
    chunks: [{ chunk_index: 0, title: "段1", range_label: "L1-L900", estimated_tokens: 9000, content: "CREATE PROCEDURE dwd.sp_settle()" }],
  },
];
view.selectedModelKey = "custom::qwen-coder";
view.render();
const bar128k = view.root.querySelector(".ai-asst-strategy-bar");
if (!bar128k || !bar128k.classList.contains("is-ok") || !view.root.querySelector(".ai-asst-ctx-meter-fill")) {
  throw new Error("Expected green .is-ok context meter bar for 25K attachment under 128K model");
}
view.selectedModelKey = "local32::small-32k";
view.render();
const bar32k = view.root.querySelector(".ai-asst-strategy-bar");
if (!bar32k || !bar32k.classList.contains("is-warn")) {
  throw new Error("Expected warn .is-warn context meter bar for 25K attachment under 32K model");
}

kbView.destroy();
view.destroy();
console.log("V3_AI_ASSISTANT_FRONTEND_E2E_OK");
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, project_root],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node 前端 v3.0.2 AI 助手交互测试失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("V3_AI_ASSISTANT_FRONTEND_E2E_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
