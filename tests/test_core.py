"""核心功能自动化单元测试 (基于标准库 unittest，兼容 pytest)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, Tuple

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from core.dws_engine import (
    ColumnMeta,
    DWSMigratorEngine,
    TableMeta,
    format_reloptions_with_clause,
    normalize_dws_type,
    parse_dws_tabledef,
    update_partition_clause_columns,
    validate_identifier,
    validate_sql_type,
)
from core.excel_handler import (
    export_report_excel,
    generate_template_excel,
    parse_excel_checklist,
)
from core.state_store import StateStore


async def _call_asgi(
    app: Any,
    method: str,
    path: str,
    headers: Dict[str, str] | None = None,
    body: bytes = b"",
) -> Tuple[int, Dict[str, str], bytes]:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 18080),
    }

    sent_request = False
    status_code = 500
    resp_headers: Dict[str, str] = {}
    resp_chunks: list[bytes] = []

    async def receive() -> Dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Dict[str, Any]) -> None:
        nonlocal status_code, resp_headers
        if message["type"] == "http.response.start":
            status_code = message["status"]
            resp_headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            resp_chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status_code, resp_headers, b"".join(resp_chunks)


class TestDWSMigrator(unittest.TestCase):
    def test_type_normalization(self) -> None:
        self.assertEqual(
            normalize_dws_type("character varying(64)"),
            normalize_dws_type("VARCHAR(64)"),
        )
        self.assertEqual(
            normalize_dws_type("NVARCHAR2(100)"),
            normalize_dws_type("varchar(100)"),
        )
        self.assertEqual(
            normalize_dws_type("VARCHAR2（100 CHAR）"),
            normalize_dws_type("varchar(100)"),
        )
        self.assertEqual(
            normalize_dws_type("DECIMAL(18, 2)"),
            normalize_dws_type("numeric(18,2)"),
        )
        self.assertEqual(
            normalize_dws_type("int4"),
            normalize_dws_type("INTEGER"),
        )
        self.assertEqual(
            normalize_dws_type("timestamp without time zone"),
            normalize_dws_type("TIMESTAMP"),
        )

    def test_identifier_and_type_cleaning(self) -> None:
        self.assertEqual(validate_identifier("dwd_cust_info"), "dwd_cust_info")
        self.assertEqual(
            validate_identifier("dwd_cust_info(客户信息表)"), "dwd_cust_info"
        )
        self.assertEqual(validate_identifier('"loan_status"'), "loan_status")
        self.assertEqual(validate_identifier("“loan_status”"), "loan_status")
        self.assertEqual(validate_sql_type("varchar(64)"), "varchar(64)")
        self.assertEqual(validate_sql_type("numeric（18，2）"), "numeric(18,2)")

    def test_excel_template_and_complex_sheets_parsing(self) -> None:
        xlsx_bytes = generate_template_excel()
        items = parse_excel_checklist(xlsx_bytes, "template.xlsx")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["schema_name"], "public")
        self.assertEqual(items[0]["table_name"], "dwd_cust_info")
        self.assertEqual(items[0]["old_col_name"], "cust_no")
        self.assertEqual(items[0]["new_col_name"], "party_id")

        wb = openpyxl.Workbook()
        cover = wb.active
        cover.title = "封面说明"
        cover.append(["XX银行数据标准贯标改造清单", "v2.0", "2026年", "内部资料"])

        ws = wb.create_sheet("客户域贯标")
        ws.append(["", "", "", "", ""])
        ws.append(["表英文名", "原字段英文名", "原数据类型", "贯标后字段英文名", "贯标后数据类型", "贯标后中文名"])
        ws.append(["DWD_CUST_INFO(客户表)", "CUST_NO", "VARCHAR2（32）", "PARTY_ID", "VARCHAR2（64）", "当事人编号"])
        ws.append([None, "CUST_NM", "VARCHAR2(100)", "PARTY_NAME", "VARCHAR2(200)", "当事人姓名"])
        ws.merge_cells("A3:A4")

        buf = io.BytesIO()
        wb.save(buf)
        parsed2 = parse_excel_checklist(buf.getvalue(), "complex.xlsx", default_schema="public")
        self.assertEqual(len(parsed2), 2)
        self.assertEqual(parsed2[0]["table_name"], "DWD_CUST_INFO")
        self.assertEqual(parsed2[1]["table_name"], "DWD_CUST_INFO")
        self.assertEqual(parsed2[0]["new_col_type"], "VARCHAR2(64)")

    def test_clean_init_and_password_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_state.db")
            app = create_app(db_path=db_path)

            # 验证纯净初始化：无任何预置沙盒假数据
            status, _, body = asyncio.run(_call_asgi(app, "GET", "/api/bootstrap"))
            self.assertEqual(status, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data["summary"]["total"], 0)
            self.assertEqual(len(data["items"]), 0)
            self.assertFalse(data["connection"]["configured"])

            # 验证本地持久化保存连接配置（含账号密码）
            store = StateStore(db_path)
            store.save_meta(
                "dws_conn_saved",
                {
                    "configured": True,
                    "host": "10.0.0.88",
                    "port": 8000,
                    "dbname": "fin_dws",
                    "user": "dw_admin",
                    "password": "MySecretPassword123",
                    "sslmode": "prefer",
                },
            )
            engine2 = DWSMigratorEngine(store)
            conn_info = engine2.get_public_conn_info()
            self.assertTrue(conn_info["configured"])
            self.assertEqual(conn_info["host"], "10.0.0.88")
            self.assertEqual(conn_info["user"], "dw_admin")
            self.assertEqual(conn_info["password"], "MySecretPassword123")

    def test_sql_generation_and_report_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_sql.db")
            store = StateStore(db_path)
            engine = DWSMigratorEngine(store)

            table_meta = TableMeta(
                schema_name="public",
                table_name="dwd_cust_info",
                exists=True,
                orientation="COLUMN",
                with_clause="WITH (orientation=column)",
                distribute_clause="DISTRIBUTE BY HASH(cust_no)",
                distribute_keys=["cust_no"],
                columns=[
                    ColumnMeta(1, "etl_dt", "date", True, None, "数据日期"),
                    ColumnMeta(2, "cust_no", "character varying(32)", True, None, "客户号"),
                    ColumnMeta(3, "cust_nm", "character varying(100)", False, None, "客户姓名"),
                ],
            )
            item = {
                "id": 1,
                "row_index": 1,
                "schema_name": "PUBLIC",
                "table_name": "DWD_CUST_INFO",
                "old_col_name": "CUST_NO",
                "old_col_type": "varchar(32)",
                "old_col_comment": "客户号",
                "new_col_name": "party_id",
                "new_col_type": "varchar(64)",
                "new_col_comment": "当事人编号",
            }
            upd = engine._build_single_item_sync_update(item, table_meta)
            self.assertEqual(upd["status"], "PENDING")
            self.assertEqual(upd["schema_name"], "public")
            self.assertEqual(upd["table_name"], "dwd_cust_info")
            self.assertEqual(upd["ordinal_position"], 2)
            self.assertTrue(upd["is_distribute_key"])
            self.assertIn("RENAME COLUMN cust_no TO party_id", upd["generated_sql"])

            report_bytes = export_report_excel([item])
            self.assertTrue(report_bytes.startswith(b"PK\x03\x04"))

    def test_multi_thread_batch_and_distribute_key_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_batch.db")
            store = StateStore(db_path)
            engine = DWSMigratorEngine(store)

            # 准备两张表：tbl_with_dist (包含分布键改造 + 普通列改造) 与 tbl_no_dist (仅普通列改造)
            raw_items = [
                {
                    "row_index": 1,
                    "schema_name": "public",
                    "table_name": "tbl_with_dist",
                    "old_col_name": "cust_id",
                    "old_col_type": "varchar(32)",
                    "old_col_comment": "客户号",
                    "new_col_name": "party_id",
                    "new_col_type": "varchar(64)",
                    "new_col_comment": "当事人ID",
                },
                {
                    "row_index": 2,
                    "schema_name": "public",
                    "table_name": "tbl_with_dist",
                    "old_col_name": "cust_name",
                    "old_col_type": "varchar(50)",
                    "old_col_comment": "姓名",
                    "new_col_name": "party_name",
                    "new_col_type": "varchar(100)",
                    "new_col_comment": "当事人名称",
                },
                {
                    "row_index": 3,
                    "schema_name": "public",
                    "table_name": "tbl_no_dist",
                    "old_col_name": "loan_st",
                    "old_col_type": "varchar(10)",
                    "old_col_comment": "状态",
                    "new_col_name": "loan_status",
                    "new_col_type": "varchar(20)",
                    "new_col_comment": "贷款状态",
                },
            ]
            inserted = store.replace_all_items(raw_items, "batch_test.xlsx")
            all_ids = [it["id"] for it in inserted]

            meta_map = {
                "tbl_with_dist": TableMeta(
                    schema_name="public",
                    table_name="tbl_with_dist",
                    exists=True,
                    distribute_keys=["cust_id"],
                    columns=[
                        ColumnMeta(1, "cust_id", "varchar(32)", True, None, "客户号"),
                        ColumnMeta(2, "cust_name", "varchar(50)", False, None, "姓名"),
                    ],
                ),
                "tbl_no_dist": TableMeta(
                    schema_name="public",
                    table_name="tbl_no_dist",
                    exists=True,
                    distribute_keys=["other_key"],
                    columns=[
                        ColumnMeta(1, "other_key", "varchar(32)", True, None, "主键"),
                        ColumnMeta(2, "loan_st", "varchar(10)", False, None, "状态"),
                    ],
                ),
            }

            def fake_inspect(schema_name: str, table_name: str) -> TableMeta:
                return meta_map[table_name]

            rebuild_calls = []
            direct_calls = []

            def fake_rebuild(
                schema_name: str,
                table_name: str,
                item_ids: list[int] | None = None,
                keep_backup: bool = True,
            ) -> Dict[str, Any]:
                rebuild_calls.append((table_name, list(item_ids or [])))
                for iid in item_ids or []:
                    store.update_item_execution(
                        iid,
                        status="COMPLETED",
                        strategy="TABLE_REBUILD",
                        status_detail="重建完成",
                    )
                return {"success": True, "message": "ok"}

            def fake_direct(item_id: int) -> Dict[str, Any]:
                direct_calls.append(item_id)
                store.update_item_execution(
                    item_id,
                    status="COMPLETED",
                    strategy="DIRECT_ALTER",
                    status_detail="ALTER完成",
                )
                return store.get_item(item_id)

            engine.inspect_table_metadata = fake_inspect  # type: ignore[method-assign]
            engine.execute_table_rebuild_migration = fake_rebuild  # type: ignore[method-assign]
            engine.execute_single_item_direct = fake_direct  # type: ignore[method-assign]

            res = engine.execute_batch_items(all_ids, max_workers=5)
            self.assertEqual(res["executed_count"], 3)
            self.assertEqual(res["success_count"], 3)
            self.assertEqual(res["failed_count"], 0)
            self.assertEqual(res["rebuild_table_count"], 1)
            self.assertEqual(res["direct_table_count"], 1)
            self.assertEqual(len(rebuild_calls), 1)
            self.assertEqual(rebuild_calls[0][0], "tbl_with_dist")
            self.assertEqual(len(rebuild_calls[0][1]), 2)
            self.assertEqual(len(direct_calls), 1)

    def test_time_without_time_zone_normalized_as_time(self) -> None:
        """验证 time without time zone 与 TIME 被识别为同一类型，同步状态应为 COMPLETED 而非 PARTIAL."""
        self.assertEqual(normalize_dws_type("time without time zone"), "time")
        self.assertEqual(normalize_dws_type("TIME"), "time")
        self.assertEqual(normalize_dws_type("time(0) without time zone"), "time")
        self.assertEqual(normalize_dws_type("time with time zone"), "timetz")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(os.path.join(tmpdir, "state.db"))
            engine = DWSMigratorEngine(store)
            store.replace_all_items(
                [
                    {
                        "row_index": 1,
                        "schema_name": "public",
                        "table_name": "tbl_time",
                        "old_col_name": "trd_tm",
                        "old_col_type": "varchar(20)",
                        "old_col_comment": "交易时间",
                        "new_col_name": "trade_time",
                        "new_col_type": "TIME",
                        "new_col_comment": "交易时间",
                    }
                ],
                "time_test.xlsx",
            )
            fake_meta = TableMeta(
                schema_name="public",
                table_name="tbl_time",
                exists=True,
                columns=[
                    ColumnMeta(
                        1,
                        "trade_time",
                        "time without time zone",
                        False,
                        None,
                        "交易时间",
                    )
                ],
            )
            engine.is_configured = lambda: True  # type: ignore[method-assign]
            engine._fetch_real_tables_metadata_batch = lambda pairs: {  # type: ignore[method-assign]
                ("public", "tbl_time"): fake_meta
            }
            engine.sync_all_items_with_db()
            items = store.list_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["status"], "COMPLETED")

    def test_partition_table_period_and_starting_boundaries_preserved(self) -> None:
        """验证带有 period='1 week' 的自动/范围分区表在重建表时完整保留起始分区定义及分区键改名."""
        self.assertEqual(
            format_reloptions_with_clause(["orientation=row", "period=1 week", "compression=no"]),
            "WITH (orientation=row, period='1 week', compression=no)",
        )

        sample_tabledef = """CREATE TABLE public.rpt_weekly_txn (
    stat_dt timestamp(0) without time zone NOT NULL,
    cust_no character varying(32),
    amt numeric(18,2)
)
WITH (orientation=row, compression=no, period='1 week')
DISTRIBUTE BY HASH(cust_no)
PARTITION BY RANGE (stat_dt)
(
    PARTITION p20250101 VALUES LESS THAN ('2025-01-08 00:00:00') TABLESPACE pg_default,
    PARTITION p20250108 VALUES LESS THAN ('2025-01-15 00:00:00') TABLESPACE pg_default
)
ENABLE ROW MOVEMENT;"""
        parsed = parse_dws_tabledef(sample_tabledef)
        self.assertIn("period='1 week'", parsed["with_clause"])
        self.assertEqual(parsed["distribute_keys"], ["cust_no"])
        self.assertEqual(parsed["partition_keys"], ["stat_dt"])
        self.assertIn("VALUES LESS THAN ('2025-01-08 00:00:00')", parsed["partition_clause"])

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(os.path.join(tmpdir, "state.db"))
            engine = DWSMigratorEngine(store)
            items = [
                {
                    "id": 1,
                    "row_index": 1,
                    "schema_name": "public",
                    "table_name": "rpt_weekly_txn",
                    "old_col_name": "stat_dt",
                    "old_col_type": "timestamp",
                    "old_col_comment": "统计日期",
                    "new_col_name": "stat_date",
                    "new_col_type": "timestamp",
                    "new_col_comment": "统计日期",
                    "status": "PENDING",
                }
            ]
            fake_meta = TableMeta(
                schema_name="public",
                table_name="rpt_weekly_txn",
                exists=True,
                with_clause=parsed["with_clause"],
                distribute_clause=parsed["distribute_clause"],
                distribute_keys=parsed["distribute_keys"],
                partition_clause=parsed["partition_clause"],
                partition_keys=parsed["partition_keys"],
                columns=[
                    ColumnMeta(1, "stat_dt", "timestamp without time zone", True, None, "统计日期"),
                    ColumnMeta(2, "cust_no", "character varying(32)", False, None, "客户号"),
                    ColumnMeta(3, "amt", "numeric(18,2)", False, None, "金额"),
                ],
            )
            engine.inspect_table_metadata = lambda s, t: fake_meta  # type: ignore[method-assign]
            plan = engine.build_table_rebuild_sql_preview("public", "rpt_weekly_txn", items)
            sql = plan["sql_preview"]
            self.assertIn("period='1 week'", sql)
            self.assertIn("PARTITION BY RANGE (stat_date)", sql)
            self.assertIn("VALUES LESS THAN ('2025-01-08 00:00:00')", sql)

    def test_multi_column_orchestration_prevents_duplicate_and_conflict_errors(self) -> None:
        """验证单表多字段同时选中时：
        1. 链式改名 (col_a -> col_b, col_b -> col_c) 在重建表中不会产生重复列 (specified more than once)，
           在原生 ALTER 中按拓扑顺序先执行 col_b -> col_c 再执行 col_a -> col_b (避免 column already exists)；
        2. 两个不同原字段改为同一目标字段名时自动拦截冲突，不会生成重复列 SQL。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(os.path.join(tmpdir, "state.db"))
            engine = DWSMigratorEngine(store)

            # 构造链式改名任务：第1列 col_a -> col_b，第2列 col_b -> col_c
            inserted = store.replace_all_items(
                [
                    {
                        "row_index": 1,
                        "schema_name": "public",
                        "table_name": "tbl_chain",
                        "old_col_name": "col_a",
                        "old_col_type": "varchar(20)",
                        "old_col_comment": "A",
                        "new_col_name": "col_b",
                        "new_col_type": "varchar(30)",
                        "new_col_comment": "新B",
                    },
                    {
                        "row_index": 2,
                        "schema_name": "public",
                        "table_name": "tbl_chain",
                        "old_col_name": "col_b",
                        "old_col_type": "varchar(20)",
                        "old_col_comment": "B",
                        "new_col_name": "col_c",
                        "new_col_type": "varchar(40)",
                        "new_col_comment": "新C",
                    },
                ],
                "chain.xlsx",
            )
            fake_meta = TableMeta(
                schema_name="public",
                table_name="tbl_chain",
                exists=True,
                distribute_keys=["col_a", "col_b"],
                columns=[
                    ColumnMeta(1, "col_a", "character varying(20)", False, None, "A"),
                    ColumnMeta(2, "col_b", "character varying(20)", False, None, "B"),
                ],
            )
            engine.inspect_table_metadata = lambda s, t: fake_meta  # type: ignore[method-assign]

            # 1) 测试重建表 SQL 预览：第1列应为 col_b，第2列应为 col_c，分布键应为 HASH(col_b, col_c)
            rebuild_plan = engine.build_table_rebuild_sql_preview(
                "public", "tbl_chain", store.list_items()
            )
            comp = rebuild_plan["column_order_comparison"]
            self.assertEqual(comp[0]["after_name"], "col_b")
            self.assertEqual(comp[1]["after_name"], "col_c")
            self.assertIn("DISTRIBUTE BY HASH(col_b, col_c)", rebuild_plan["sql_preview"])
            self.assertIn("INSERT INTO public.tbl_chain__mig_", rebuild_plan["sql_preview"])
            self.assertIn("(col_b, col_c)", rebuild_plan["sql_preview"])

            # 2) 测试拓扑排序顺序：必须先执行第2列 (col_b -> col_c)，再执行第1列 (col_a -> col_b)
            col_plan = rebuild_plan["col_plan"]
            self.assertEqual(col_plan["ordered_ordinals"], [2, 1])
            self.assertFalse(col_plan["has_rename_cycle"])

            # 3) 测试两个不同字段改为同一个目标名时的冲突拦截
            conflict_items = [
                {
                    "id": 10,
                    "schema_name": "public",
                    "table_name": "tbl_chain",
                    "old_col_name": "col_a",
                    "new_col_name": "dup_target",
                    "new_col_type": "varchar(30)",
                    "new_col_comment": "",
                    "status": "PENDING",
                },
                {
                    "id": 11,
                    "schema_name": "public",
                    "table_name": "tbl_chain",
                    "old_col_name": "col_b",
                    "new_col_name": "dup_target",
                    "new_col_type": "varchar(30)",
                    "new_col_comment": "",
                    "status": "PENDING",
                },
            ]
            conflict_plan = engine.build_table_rebuild_sql_preview(
                "public", "tbl_chain", conflict_items
            )
            after_names = [c["after_name"] for c in conflict_plan["column_order_comparison"]]
            self.assertEqual(len(after_names), len(set(after_names)))
            self.assertEqual(len(conflict_plan["col_plan"]["conflict_items"]), 1)

    def test_cross_env_tri_column_compare_and_bindings_import_export(self) -> None:
        """验证跨环境三栏数标比对中心：来源库(Inceptor/PGSQL) × 数据标准 × 目标库(DWS) 三方绑定、持久化与批量导入导出."""
        from core.cross_env_engine import CrossEnvCompareEngine, is_cross_type_compatible

        self.assertEqual(is_cross_type_compatible("varchar2(50)", "VARCHAR(50)"), (True, True))
        self.assertEqual(is_cross_type_compatible("string", "varchar(100)"), (False, True))

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(os.path.join(tmpdir, "state.db"))
            dws_engine = DWSMigratorEngine(store)
            cross_engine = CrossEnvCompareEngine(store, dws_engine)

            # 1) 导入数据标准
            std_items = [
                {
                    "row_index": 1,
                    "schema_name": "public",
                    "table_name": "t_loan_acct",
                    "old_col_name": "cst_id",
                    "old_col_type": "string",
                    "old_col_comment": "客户号",
                    "new_col_name": "cust_id",
                    "new_col_type": "varchar(32)",
                    "new_col_comment": "客户编号",
                },
                {
                    "row_index": 2,
                    "schema_name": "public",
                    "table_name": "t_loan_acct",
                    "old_col_name": "ln_st",
                    "old_col_type": "string",
                    "old_col_comment": "状态",
                    "new_col_name": "loan_status",
                    "new_col_type": "varchar(10)",
                    "new_col_comment": "贷款状态",
                },
            ]
            store.replace_cross_standards(std_items, "数标清单.xlsx")

            # 2) 模拟来源库 (INCEPTOR) 与目标库 (DWS) 已连接并返回各自表结构
            store.save_meta(
                "cross_source_conn",
                {
                    "configured": True,
                    "db_type": "INCEPTOR",
                    "host": "10.0.0.11",
                    "port": 10000,
                    "dbname": "ods_db",
                    "schema_name": "ods_db",
                    "user": "hive",
                },
            )
            store.save_meta(
                "cross_target_conn",
                {
                    "configured": True,
                    "db_type": "DWS",
                    "host": "10.0.0.22",
                    "port": 8000,
                    "dbname": "dws_db",
                    "schema_name": "public",
                    "user": "dbadmin",
                },
            )

            def fake_inspect_role(role: str, schema_name: str, table_name: str) -> TableMeta:
                if role == "source":
                    return TableMeta(
                        schema_name=schema_name or "ods_db",
                        table_name=table_name or "t_loan_acct",
                        exists=True,
                        columns=[
                            ColumnMeta(1, "cst_id", "string", False, None, "客户号"),
                            ColumnMeta(2, "ln_st", "string", False, None, "状态"),
                        ],
                    )
                return TableMeta(
                    schema_name=schema_name or "public",
                    table_name=table_name or "t_loan_acct",
                    exists=True,
                    columns=[
                        ColumnMeta(1, "cust_id", "character varying(32)", True, None, "客户编号"),
                        ColumnMeta(2, "ln_st", "character varying(10)", False, None, "状态"),
                    ],
                )

            cross_engine.inspect_role_table_metadata = fake_inspect_role  # type: ignore[method-assign]

            comp = cross_engine.compare_single_table("t_loan_acct")
            self.assertEqual(comp["summary"]["total"], 2)
            self.assertEqual(comp["summary"]["compliant"], 1)  # cust_id 已落标
            self.assertEqual(comp["summary"]["target_pending"], 1)  # ln_st 待改名为 loan_status

            # 3) 修改绑定关系并持久保存
            col_list = comp["standard_panel"]["columns"]
            cross_engine.save_table_and_column_bindings(
                "t_loan_acct",
                source_schema="ods_db",
                source_table="ods_loan_acct",
                target_schema="public",
                target_table="dws_loan_acct",
                column_bindings=[
                    {
                        "id": col_list[0]["id"],
                        "bound_source_col": "cst_id",
                        "bound_target_col": "cust_id",
                    }
                ],
            )
            saved_tb = store.get_cross_table_binding("t_loan_acct")
            self.assertIsNotNone(saved_tb)
            assert saved_tb is not None
            self.assertEqual(saved_tb["source_table"], "ods_loan_acct")
            self.assertEqual(saved_tb["target_table"], "dws_loan_acct")

            # 4) 导出 JSON 绑定包 & Excel 三方比对表，并重新导入验证
            bundle = store.export_cross_bindings_bundle()
            self.assertEqual(len(bundle["table_bindings"]), 1)
            self.assertEqual(len(bundle["column_bindings"]), 2)

            excel_bytes = cross_engine.export_comparison_excel("t_loan_acct")
            self.assertTrue(len(excel_bytes) > 500)

            store.clear_cross_standards()
            self.assertEqual(len(store.list_cross_tables()), 0)

            cross_engine.import_bindings_from_file(excel_bytes, "exported_3way.xlsx")
            restored_tb = store.get_cross_table_binding("t_loan_acct")
            self.assertIsNotNone(restored_tb)
            assert restored_tb is not None
            self.assertEqual(restored_tb["source_table"], "ods_loan_acct")
            self.assertEqual(restored_tb["target_table"], "dws_loan_acct")

            # 5) 推送至单库批量贯标执行台
            push_res = cross_engine.push_uncompliant_to_workbench("t_loan_acct")
            self.assertEqual(push_res["pushed_count"], 2)
            wb_items = store.list_items()
            self.assertEqual(len(wb_items), 2)
            self.assertEqual(wb_items[0]["table_name"], "dws_loan_acct")

    def test_cross_dialect_ddl_translation_pgsql_dws_inceptor(self) -> None:
        """验证跨环境建表语句导出与多方言翻译器 (PGSQL ⟷ 华为 DWS ⟷ 星环 Inceptor)."""
        from core.cross_env_engine import (
            CrossEnvCompareEngine,
            build_dialect_ddl,
            translate_column_type,
        )

        # 1) 验证三方核心类型映射
        # Inceptor STRING 作 DWS 分布键时必须转为定长 VARCHAR(255)
        self.assertEqual(translate_column_type("STRING", "DWS", is_key_col=True)[0], "VARCHAR(255)")
        self.assertEqual(translate_column_type("STRING", "DWS", is_key_col=False)[0], "TEXT")
        # TINYINT 在 PGSQL 中自动映射为 SMALLINT，在 DWS / INCEPTOR 中保留 TINYINT
        self.assertEqual(translate_column_type("TINYINT", "PGSQL")[0], "SMALLINT")
        self.assertEqual(translate_column_type("TINYINT", "DWS")[0], "TINYINT")
        self.assertEqual(translate_column_type("TINYINT", "INCEPTOR")[0], "TINYINT")
        # NUMERIC(18,2) 與 TIME 在 Inceptor 中分别映射为 DECIMAL(18,2) 與 STRING
        self.assertEqual(translate_column_type("numeric(18,2)", "INCEPTOR")[0], "DECIMAL(18,2)")
        self.assertEqual(translate_column_type("time without time zone", "INCEPTOR")[0], "STRING")

        # 2) 验证 build_dialect_ddl 在 DWS / PGSQL / INCEPTOR 下的语法特征（含分区键分离与分桶事务表）
        sample_meta = TableMeta(
            schema_name="ods_db",
            table_name="t_acct_txn",
            table_comment="账户交易流水表",
            exists=True,
            distribute_keys=["acct_no"],
            partition_keys=["stat_dt"],
            columns=[
                ColumnMeta(1, "acct_no", "STRING", True, None, "账号"),
                ColumnMeta(2, "txn_amt", " DECIMAL(18,4) ", False, "0.00", "交易金额"),
                ColumnMeta(3, "txn_time", "time without time zone", False, None, "交易时间"),
                ColumnMeta(4, "status_flag", "tinyint", True, "1", "状态标志"),
                ColumnMeta(5, "stat_dt", "DATE", True, None, "统计日期"),
            ],
        )

        # 翻译为华为 DWS 列存高压缩表
        dws_res = build_dialect_ddl(
            sample_meta,
            source_dialect="INCEPTOR",
            target_dialect="DWS",
            target_schema="dws_fin",
            target_table="dws_acct_txn",
            storage_mode="DWS_COLUMN",
        )
        self.assertIn("CREATE TABLE dws_fin.dws_acct_txn", dws_res["ddl"])
        self.assertIn("acct_no VARCHAR(255) NOT NULL", dws_res["ddl"])
        self.assertIn("WITH (orientation=column, compression=middle)", dws_res["ddl"])
        self.assertIn("DISTRIBUTE BY HASH(acct_no)", dws_res["ddl"])
        self.assertIn("PARTITION BY RANGE (stat_dt)", dws_res["ddl"])
        self.assertIn("COMMENT ON TABLE dws_fin.dws_acct_txn IS '账户交易流水表';", dws_res["ddl"])

        # 翻译为 PostgreSQL 标准表
        pg_res = build_dialect_ddl(
            sample_meta,
            source_dialect="INCEPTOR",
            target_dialect="PGSQL",
            target_schema="public",
            target_table="pg_acct_txn",
        )
        self.assertIn("CREATE TABLE public.pg_acct_txn", pg_res["ddl"])
        self.assertIn("status_flag SMALLINT DEFAULT 1 NOT NULL", pg_res["ddl"])
        self.assertNotIn("DISTRIBUTE BY", pg_res["ddl"])
        self.assertIn("PARTITION BY RANGE (stat_dt)", pg_res["ddl"])

        # 翻译为星环 Inceptor ORC 事务表（验证分区列自动从主列清单移出至 PARTITIONED BY，防止重复列报错）
        inc_res = build_dialect_ddl(
            sample_meta,
            source_dialect="DWS",
            target_dialect="INCEPTOR",
            target_schema="dw_hive",
            target_table="inc_acct_txn",
            storage_mode="INCEPTOR_ORC_TXN",
        )
        self.assertIn("CREATE TABLE IF NOT EXISTS dw_hive.inc_acct_txn", inc_res["ddl"])
        self.assertIn("COMMENT '账户交易流水表'", inc_res["ddl"])
        self.assertIn("PARTITIONED BY (stat_dt DATE COMMENT '统计日期')", inc_res["ddl"])
        self.assertIn("CLUSTERED BY (acct_no) INTO 7 BUCKETS", inc_res["ddl"])
        self.assertIn("STORED AS ORC", inc_res["ddl"])
        self.assertIn("'transactional'='true'", inc_res["ddl"])
        # 主字段定义区不能重复出现 stat_dt，仅在 PARTITIONED BY 中出现一次
        self.assertEqual(inc_res["ddl"].count("stat_dt"), 1)

        # 3) 验证 CrossEnvCompareEngine.translate_table_ddl 结合中间数据标准套用转换
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(os.path.join(tmpdir, "state.db"))
            dws_engine = DWSMigratorEngine(store)
            cross_engine = CrossEnvCompareEngine(store, dws_engine)

            store.replace_cross_standards(
                [
                    {
                        "row_index": 1,
                        "schema_name": "public",
                        "table_name": "t_acct_txn",
                        "old_col_name": "acct_no",
                        "old_col_type": "STRING",
                        "old_col_comment": "账号",
                        "new_col_name": "std_acct_id",
                        "new_col_type": "VARCHAR(64)",
                        "new_col_comment": "标准账户编号",
                    },
                    {
                        "row_index": 2,
                        "schema_name": "public",
                        "table_name": "t_acct_txn",
                        "old_col_name": "txn_amt",
                        "old_col_type": "DECIMAL(18,4)",
                        "old_col_comment": "交易金额",
                        "new_col_name": "std_txn_amt",
                        "new_col_type": "NUMERIC(20,2)",
                        "new_col_comment": "标准交易金额",
                    },
                ],
                "std.xlsx",
            )

            translated = cross_engine.translate_table_ddl(
                source_role="source",
                source_schema="ods_db",
                source_table="t_acct_txn",
                target_dialect="DWS",
                target_schema="public",
                target_table="dws_acct_std",
                storage_mode="DWS_ROW",
                apply_standard_mapping=True,
            )
            self.assertIn("CREATE TABLE public.dws_acct_std", translated["target_ddl"])
            self.assertIn("std_acct_id VARCHAR(64)", translated["target_ddl"])
            self.assertIn("std_txn_amt NUMERIC(20,2)", translated["target_ddl"])
            self.assertIn("DISTRIBUTE BY HASH(std_acct_id)", translated["target_ddl"])
            self.assertIn("COMMENT ON COLUMN public.dws_acct_std.std_acct_id IS '标准账户编号';", translated["target_ddl"])

    def test_view_lineage_penetration_and_inceptor_thrift_session_handle(self) -> None:
        """验证：
        1. 星环 Inceptor HiveServer2 Thrift TOpenSessionResp 正确读取 field 3 (sessionHandle) 而非 field 4 (configuration)，解决连接立即断开问题；
        2. 针对上游星环 Inceptor / DWS / PGSQL 视图 (CREATE VIEW ... AS SELECT ...)，自动解析视图 SQL 血缘并穿透底层物理基表，补全字段精确类型、长度、中文注释、分布键与分区键，逆向生成可执行物理表 CREATE TABLE 语句；
        3. 支持直接粘贴 CREATE VIEW / SELECT 语句逆向推导目标库物理建表 DDL。
        """
        from core.cross_env_engine import (
            CrossEnvCompareEngine,
            InceptorThriftClient,
            parse_view_sql_lineage,
        )

        # 1) 验证 InceptorThriftClient._open_session 正确提取 field 3 的 sessionHandle (guid + secret)
        client = InceptorThriftClient("127.0.0.1", 10000, "hive", "secret_pwd", "ods", "LDAP")
        expected_guid = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10"
        expected_secret = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99"

        # 构造模拟的 TOpenSessionResp 字典结构:
        # field 1: status (TStatus: statusCode=0)
        # field 2: serverProtocolVersion=4 (V5)
        # field 3: sessionHandle (TSessionHandle -> field 1: THandleIdentifier(1: guid, 2: secret))
        # field 4: configuration (map<string,string> 干扰项)
        fake_parsed_thrift_resp = {
            0: {
                1: {1: 0},
                2: 4,
                3: {1: {1: expected_guid, 2: expected_secret}},
                4: {b"hive.server2.idl.version": b"1.2"},
            }
        }
        client._send_framed_rpc = lambda method, body: b"MOCK_RESP"  # type: ignore[method-assign]
        client._parse_thrift_message = lambda data: fake_parsed_thrift_resp  # type: ignore[method-assign]
        client._open_session(proto_ver=5)
        self.assertEqual(client._session_handle, (expected_guid, expected_secret))
        self.assertEqual(client._protocol_version, 4)

        # 2) 验证复杂视图 SQL 的 AST 血缘解析（含 CTE、JOIN、CAST、函数与行内注释）
        view_sql = """
        CREATE OR REPLACE VIEW ods_db.v_cust_loan_summary
        COMMENT '客户贷款汇总上游视图'
        AS
        WITH active_cust AS (
            SELECT c.cust_id, c.cust_name, c.cert_no
            FROM ods_db.t_cust_base c
            WHERE c.status = '1'
        )
        SELECT
            a.cust_id AS customer_id, -- 客户编号
            a.cust_name,
            CAST(b.loan_bal AS DECIMAL(18,2)) AS loan_balance, /* 贷款余额 */
            COUNT(b.contract_no) AS contract_cnt,
            b.stat_dt
        FROM active_cust a
        LEFT JOIN ods_db.t_loan_contract b ON a.cust_id = b.cust_id
        """
        lineage = parse_view_sql_lineage(view_sql, default_schema="ods_db")
        self.assertTrue(lineage["is_view_sql"])
        self.assertEqual(lineage["view_schema"], "ods_db")
        self.assertEqual(lineage["view_name"], "v_cust_loan_summary")
        self.assertEqual(lineage["view_comment"], "客户贷款汇总上游视图")
        cols_by_name = {c["column_name"]: c for c in lineage["columns"]}
        self.assertEqual(cols_by_name["customer_id"]["source_table"], "t_cust_base")
        self.assertEqual(cols_by_name["customer_id"]["source_column"], "cust_id")
        self.assertEqual(cols_by_name["cust_name"]["source_table"], "t_cust_base")
        self.assertEqual(cols_by_name["loan_balance"]["inferred_type"], "DECIMAL(18,2)")
        self.assertEqual(cols_by_name["contract_cnt"]["inferred_type"], "BIGINT")
        self.assertEqual(cols_by_name["stat_dt"]["source_table"], "t_loan_contract")

        # 3) 验证视图底层基表穿透补全 (_enrich_view_metadata_from_lineage + translate_table_ddl)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(os.path.join(tmpdir, "state.db"))
            dws_engine = DWSMigratorEngine(store)
            cross_engine = CrossEnvCompareEngine(store, dws_engine)

            store.save_meta(
                "cross_source_conn",
                {
                    "configured": True,
                    "db_type": "INCEPTOR",
                    "host": "10.0.0.11",
                    "port": 10000,
                    "dbname": "ods_db",
                    "schema_name": "ods_db",
                    "user": "hive",
                },
            )

            # 模拟上游返回的是星环视图（视图自身列类型仅有泛化的 STRING 且无注释，但底层基表有精确 VARCHAR(64)、注释、分布键与分区键）
            raw_view_meta = TableMeta(
                schema_name="ods_db",
                table_name="v_cust_loan_summary",
                table_comment="",
                exists=True,
                object_type="VIEW",
                view_definition=view_sql,
                raw_ddl=view_sql.strip(),
                columns=[
                    ColumnMeta(1, "customer_id", "STRING", False, None, ""),
                    ColumnMeta(2, "cust_name", "STRING", False, None, ""),
                    ColumnMeta(3, "loan_balance", "STRING", False, None, ""),
                    ColumnMeta(4, "contract_cnt", "BIGINT", False, None, ""),
                    ColumnMeta(5, "stat_dt", "STRING", False, None, ""),
                ],
            )
            base_cust_meta = TableMeta(
                schema_name="ods_db",
                table_name="t_cust_base",
                table_comment="客户基础信息物理表",
                exists=True,
                distribute_keys=["cust_id"],
                columns=[
                    ColumnMeta(1, "cust_id", "VARCHAR(64)", True, None, "核心客户号"),
                    ColumnMeta(2, "cust_name", "VARCHAR(120)", False, None, "客户中文姓名"),
                ],
            )
            base_loan_meta = TableMeta(
                schema_name="ods_db",
                table_name="t_loan_contract",
                table_comment="贷款合同物理表",
                exists=True,
                distribute_keys=["cust_id"],
                partition_keys=["stat_dt"],
                columns=[
                    ColumnMeta(1, "contract_no", "VARCHAR(64)", True, None, "合同号"),
                    ColumnMeta(2, "cust_id", "VARCHAR(64)", True, None, "核心客户号"),
                    ColumnMeta(3, "loan_bal", "DECIMAL(18,2)", False, None, "合同贷款余额"),
                    ColumnMeta(4, "stat_dt", "DATE", True, None, "数据统计日期"),
                ],
            )

            def fake_inspect(role: str, s_name: str, t_name: str) -> TableMeta:
                if t_name == "v_cust_loan_summary":
                    return cross_engine._enrich_view_metadata_from_lineage(
                        raw_view_meta,
                        base_table_fetcher=lambda bs, bt: (
                            base_cust_meta
                            if bt == "t_cust_base"
                            else base_loan_meta
                            if bt == "t_loan_contract"
                            else TableMeta(schema_name=bs, table_name=bt, exists=False)
                        ),
                    )
                if t_name == "t_cust_base":
                    return base_cust_meta
                if t_name == "t_loan_contract":
                    return base_loan_meta
                return TableMeta(schema_name=s_name, table_name=t_name, exists=False)

            cross_engine.inspect_role_table_metadata = fake_inspect  # type: ignore[method-assign]

            res_dws = cross_engine.translate_table_ddl(
                source_role="source",
                source_schema="ods_db",
                source_table="v_cust_loan_summary",
                target_dialect="DWS",
                target_schema="dws_fin",
                target_table="dws_cust_loan_summary",
            )
            self.assertEqual(res_dws["source_object_type"], "VIEW")
            self.assertIn("ods_db.t_cust_base", res_dws["penetrated_base_tables"])
            self.assertIn("ods_db.t_loan_contract", res_dws["penetrated_base_tables"])
            # 验证泛化的 STRING 已被底层基表的精确类型 VARCHAR(64) / VARCHAR(120) / NUMERIC(18,2) / DATE 升级替代！
            self.assertIn("customer_id VARCHAR(64)", res_dws["target_ddl"])
            self.assertIn("cust_name VARCHAR(120)", res_dws["target_ddl"])
            self.assertIn("loan_balance NUMERIC(18,2)", res_dws["target_ddl"])
            self.assertIn("stat_dt DATE", res_dws["target_ddl"])
            # 验证底层基表的分布键 cust_id（对应视图别名列 customer_id）与分区键 stat_dt 已被自动继承！
            self.assertIn("DISTRIBUTE BY HASH(customer_id)", res_dws["target_ddl"])
            self.assertIn("PARTITION BY RANGE (stat_dt)", res_dws["target_ddl"])
            self.assertIn("COMMENT ON COLUMN dws_fin.dws_cust_loan_summary.cust_name IS '客户中文姓名';", res_dws["target_ddl"])

            # 4) 验证直接粘贴视图 SQL 逆向建表 (source_role="view_sql")
            res_pasted = cross_engine.translate_table_ddl(
                source_role="view_sql",
                target_dialect="INCEPTOR",
                target_schema="dw_ods",
                target_table="inc_cust_loan_phys",
                view_sql=view_sql,
            )
            self.assertEqual(res_pasted["source_object_type"], "VIEW")
            self.assertIn("DROP TABLE IF EXISTS dw_ods.inc_cust_loan_phys;", res_pasted["target_ddl"])
            self.assertIn("CREATE TABLE IF NOT EXISTS dw_ods.inc_cust_loan_phys", res_pasted["target_ddl"])
            self.assertIn("PARTITIONED BY (stat_dt DATE COMMENT '数据统计日期')", res_pasted["target_ddl"])
            self.assertIn("CLUSTERED BY (customer_id) INTO 7 BUCKETS", res_pasted["target_ddl"])
            self.assertIn("DROP TABLE IF EXISTS dws_fin.dws_cust_loan_summary;", res_dws["target_ddl"])

            # 5) 验证未上传/未选择标准表时，直接在来源库选择视图并点击「读取表」不会报错，且自动生成三栏比对结果
            overview_after_read = cross_engine.save_table_and_column_bindings(
                "",
                source_schema="ods_db",
                source_table="v_cust_loan_summary",
                target_schema="dws_fin",
                target_table="dws_cust_loan_summary",
            )
            self.assertEqual(overview_after_read["active_table_key"], "v_cust_loan_summary")
            comp_view = overview_after_read["active_comparison"]
            self.assertIsNotNone(comp_view)
            self.assertEqual(comp_view["source_panel"]["object_type"], "VIEW")
            self.assertEqual(len(comp_view["source_panel"]["columns"]), 5)
            self.assertEqual(len(comp_view["standard_panel"]["columns"]), 5)

            # 6) 验证单行超长视图 DDL 自动换行美化 (_format_readable_sql_ddl)
            from core.cross_env_engine import _format_readable_sql_ddl

            single_line_view = (
                "CREATE VIEW ods.v_long AS SELECT a.col1, a.col2, a.col3, a.col4, a.col5, "
                "b.col6, b.col7, b.col8, b.col9 FROM ods.table_alpha a LEFT JOIN ods.table_beta b "
                "ON a.col1 = b.col6 WHERE a.col2 = 'ACTIVE' GROUP BY a.col1"
            )
            formatted_view = _format_readable_sql_ddl(single_line_view)
            self.assertGreater(len(formatted_view.splitlines()), 3)

    def test_state_store_branch_rollback_schema_compatibility(self) -> None:
        """验证 Git 跨分支切换与版本回退场景下，本地持久化 SQLite 状态库的前向与后向列兼容性."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy_or_future_state.db")

            # 1) 模拟旧分支遗留的精简表结构（缺少后续版本新增列），切到新分支初始化时应自动补齐
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE migration_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    row_index INTEGER NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    old_col_name TEXT NOT NULL,
                    new_col_name TEXT NOT NULL,
                    new_col_type TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
            conn.close()

            store = StateStore(db_path)
            # 2) 模拟未来实验分支新增了额外字段 (future_branch_flag)，回退到当前分支后仍能正常读写不报错
            conn2 = sqlite3.connect(db_path)
            conn2.execute(
                "ALTER TABLE migration_items ADD COLUMN future_branch_flag TEXT NOT NULL DEFAULT 'exp'"
            )
            conn2.commit()
            conn2.close()

            items = store.replace_all_items(
                [
                    {
                        "row_index": 1,
                        "schema_name": "public",
                        "table_name": "t_demo",
                        "old_col_name": "old_c",
                        "new_col_name": "new_c",
                        "new_col_type": "VARCHAR(32)",
                    }
                ],
                "rollback_check.xlsx",
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["new_col_name"], "new_c")


if __name__ == "__main__":
    unittest.main()
