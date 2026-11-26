"""Data.O.G v3.6.0 落标危险四级分级、结构化变更日志、Checkpoint 与 LIFO 一键回滚测试."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import openpyxl

from app import create_app
from core.dws_engine import ColumnMeta, DWSMigratorEngine, TableMeta
from core.platform.context import PlatformContext
from core.platform.dialects import classify_migration_risk
from modules.standard_migrator.service import StandardMigratorService
from tests.test_modules_and_api import TestClient


class _FakeDWSState:
    """内存模拟 DWS 物理表结构、备份表与原子事务，用于真实测试执行与逆向回滚链路."""

    def __init__(self) -> None:
        self.tables: Dict[Tuple[str, str], TableMeta] = {}
        self.fail_on_sql_substr: Optional[str] = None
        self.executed_sqls: List[str] = []

    def add_table(self, meta: TableMeta) -> None:
        self.tables[(meta.schema_name.lower(), meta.table_name.lower())] = meta

    def get_table(self, schema_name: str, table_name: str) -> TableMeta:
        key = (schema_name.lower(), table_name.lower())
        if key not in self.tables:
            return TableMeta(schema_name=schema_name, table_name=table_name, exists=False)
        m = self.tables[key]
        return TableMeta(
            schema_name=m.schema_name,
            table_name=m.table_name,
            exists=True,
            table_comment=m.table_comment,
            orientation=m.orientation,
            with_clause=m.with_clause,
            distribute_clause=m.distribute_clause,
            distribute_keys=list(m.distribute_keys),
            partition_clause=m.partition_clause,
            partition_keys=list(m.partition_keys),
            columns=[
                ColumnMeta(
                    ordinal_position=c.ordinal_position,
                    column_name=c.column_name,
                    data_type=c.data_type,
                    is_not_null=c.is_not_null,
                    column_default=c.column_default,
                    column_comment=c.column_comment,
                )
                for c in m.columns
            ],
        )


class _FakeDWSCursor:
    def __init__(self, conn: "_FakeDWSConn") -> None:
        self.conn = conn
        self._rows: List[Tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeDWSCursor":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        state = self.conn.state
        if state.fail_on_sql_substr and state.fail_on_sql_substr.lower() in sql.lower():
            raise RuntimeError(f"模拟物理执行失败: {state.fail_on_sql_substr}")
        state.executed_sqls.append(sql)
        sql_s = sql.strip()

        if "FROM pg_catalog.pg_class c" in sql_s and "pg_attribute" in sql_s:
            sch, tbl = params[0], params[1]
            meta = self.conn.working_tables.get((sch.lower(), tbl.lower()))
            cols = sorted(meta.columns, key=lambda x: x.ordinal_position) if meta else []
            self._rows = [
                (c.column_name, c.data_type, c.column_comment)
                for c in cols
            ]
            return

        if "FROM pg_catalog.pg_class c" in sql_s and "c.relkind IN ('r', 'p')" in sql_s:
            sch, tbl = params[0], params[1]
            meta = state.get_table(sch, tbl)
            self._rows = [(1,)] if meta.exists else []
            return

        if sql_s.upper().startswith("SELECT COUNT(*) FROM "):
            self._rows = [(10,)]
            return

        m_rename_tbl = re.match(
            r'^\s*ALTER\s+TABLE\s+"?([^"\s.]+)"?\."?([^"\s]+)"?\s+RENAME\s+TO\s+"?([^"\s;]+)"?\s*;?\s*$',
            sql_s,
            re.IGNORECASE,
        )
        if m_rename_tbl:
            sch, old_t, new_t = m_rename_tbl.groups()
            key = (sch.lower(), old_t.lower())
            tbl_obj = self.conn.working_tables.pop(key)
            tbl_obj.table_name = new_t
            self.conn.working_tables[(sch.lower(), new_t.lower())] = tbl_obj
            return

        m_drop_tbl = re.match(
            r'^\s*DROP\s+TABLE\s+"?([^"\s.]+)"?\."?([^"\s;]+)"?\s*;?\s*$',
            sql_s,
            re.IGNORECASE,
        )
        if m_drop_tbl:
            sch, tbl = m_drop_tbl.groups()
            self.conn.working_tables.pop((sch.lower(), tbl.lower()), None)
            return

        m_create = re.match(
            r'^\s*CREATE\s+TABLE\s+"?([^"\s.]+)"?\."?([^"\s(]+)"?\s*\((.+)\)(?:[^;]*);?\s*$',
            sql_s,
            re.IGNORECASE | re.DOTALL,
        )
        if m_create:
            sch, tbl, cols_raw = m_create.groups()
            cols: List[ColumnMeta] = []
            for idx, part in enumerate(cols_raw.split(","), start=1):
                p = part.strip()
                m_c = re.match(r'^"?([^"\s]+)"?\s+(.+)$', p)
                if m_c:
                    cname, rest = m_c.groups()
                    ctype = rest.split(" DEFAULT ")[0].split(" NOT NULL")[0].strip()
                    cols.append(ColumnMeta(ordinal_position=idx, column_name=cname, data_type=ctype))
            self.conn.working_tables[(sch.lower(), tbl.lower())] = TableMeta(
                schema_name=sch,
                table_name=tbl,
                exists=True,
                columns=cols,
            )
            return

        if sql_s.upper().startswith("INSERT INTO "):
            return

        m_ren_col = re.match(
            r'^\s*ALTER\s+TABLE\s+"?([^"\s.]+)"?\."?([^"\s]+)"?\s+RENAME\s+COLUMN\s+"?([^"\s]+)"?\s+TO\s+"?([^"\s;]+)"?\s*;?\s*$',
            sql_s,
            re.IGNORECASE,
        )
        if m_ren_col:
            sch, tbl, old_c, new_c = m_ren_col.groups()
            meta = self.conn.working_tables[(sch.lower(), tbl.lower())]
            for c in meta.columns:
                if c.column_name.lower() == old_c.lower():
                    c.column_name = new_c
                    break
            return

        m_type = re.match(
            r'^\s*ALTER\s+TABLE\s+"?([^"\s.]+)"?\."?([^"\s]+)"?\s+ALTER\s+COLUMN\s+"?([^"\s]+)"?\s+TYPE\s+(.+?)\s+USING\s+.+;?\s*$',
            sql_s,
            re.IGNORECASE,
        )
        if m_type:
            sch, tbl, col_n, new_t = m_type.groups()
            meta = self.conn.working_tables[(sch.lower(), tbl.lower())]
            for c in meta.columns:
                if c.column_name.lower() == col_n.lower():
                    c.data_type = new_t.strip()
                    break
            return

        m_comm_null = re.match(
            r'^\s*COMMENT\s+ON\s+COLUMN\s+"?([^"\s.]+)"?\."?([^"\s.]+)"?\."?([^"\s]+)"?\s+IS\s+NULL\s*;?\s*$',
            sql_s,
            re.IGNORECASE,
        )
        if m_comm_null:
            sch, tbl, col_n = m_comm_null.groups()
            meta = self.conn.working_tables[(sch.lower(), tbl.lower())]
            for c in meta.columns:
                if c.column_name.lower() == col_n.lower():
                    c.column_comment = ""
                    break
            return

        m_comm = re.match(
            r'^\s*COMMENT\s+ON\s+COLUMN\s+"?([^"\s.]+)"?\."?([^"\s.]+)"?\."?([^"\s]+)"?\s+IS\s+%s\s*;?\s*$',
            sql_s,
            re.IGNORECASE,
        )
        if m_comm:
            sch, tbl, col_n = m_comm.groups()
            meta = self.conn.working_tables[(sch.lower(), tbl.lower())]
            for c in meta.columns:
                if c.column_name.lower() == col_n.lower():
                    c.column_comment = str(params[0] or "")
                    break
            return

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._rows)


class _FakeDWSConn:
    def __init__(self, state: _FakeDWSState) -> None:
        self.state = state
        self.autocommit = True
        self.working_tables: Dict[Tuple[str, str], TableMeta] = {
            k: state.get_table(v.schema_name, v.table_name)
            for k, v in state.tables.items()
        }

    def cursor(self) -> _FakeDWSCursor:
        return _FakeDWSCursor(self)

    def commit(self) -> None:
        self.state.tables = {
            k: TableMeta(
                schema_name=v.schema_name,
                table_name=v.table_name,
                exists=True,
                table_comment=v.table_comment,
                orientation=v.orientation,
                with_clause=v.with_clause,
                distribute_clause=v.distribute_clause,
                distribute_keys=list(v.distribute_keys),
                partition_clause=v.partition_clause,
                partition_keys=list(v.partition_keys),
                columns=[
                    ColumnMeta(
                        ordinal_position=c.ordinal_position,
                        column_name=c.column_name,
                        data_type=c.data_type,
                        is_not_null=c.is_not_null,
                        column_default=c.column_default,
                        column_comment=c.column_comment,
                    )
                    for c in v.columns
                ],
            )
            for k, v in self.working_tables.items()
        }

    def rollback(self) -> None:
        self.working_tables = {
            k: self.state.get_table(v.schema_name, v.table_name)
            for k, v in self.state.tables.items()
        }

    def close(self) -> None:
        return None


class TestMigrationRiskAndRollback(unittest.TestCase):
    def test_classify_migration_risk_four_levels_matrix(self) -> None:
        """验证 L1~L4 四级危险程度分级规则与组合取高危逻辑."""
        # L1: 字段改名（优先级最高，即使同时包含类型转换与精度缩减也定为 L1）
        r_rename = classify_migration_risk(
            {
                "old_col_name": "cust_id_old",
                "old_col_type": "varchar(64)",
                "new_col_name": "cust_id",
                "new_col_type": "varchar(64)",
            }
        )
        self.assertEqual(r_rename["risk_level"], "L1")
        self.assertEqual(r_rename["risk_rank"], 1)
        self.assertTrue(r_rename["has_rename"])

        r_combo = classify_migration_risk(
            {
                "old_col_name": "amt_str",
                "old_col_type": "varchar(100)",
                "new_col_name": "tx_amt",
                "new_col_type": "numeric(12,2)",
            }
        )
        self.assertEqual(r_combo["risk_level"], "L1")
        self.assertTrue(r_combo["has_rename"])
        self.assertTrue(r_combo["has_type_conversion"])
        self.assertTrue(r_combo["is_lossy"])

        # L2: 字段类型转换（不改名）
        for old_t, new_t in (
            ("varchar(32)", "integer"),
            ("integer", "numeric(18,2)"),
            ("date", "timestamp"),
            ("numeric(10,2)", "integer"),
        ):
            r_conv = classify_migration_risk(
                {
                    "old_col_name": "f_col",
                    "old_col_type": old_t,
                    "new_col_name": "f_col",
                    "new_col_type": new_t,
                }
            )
            self.assertEqual(r_conv["risk_level"], "L2", f"{old_t}->{new_t} 应判为 L2")
            self.assertEqual(r_conv["risk_rank"], 2)
            self.assertTrue(r_conv["is_lossy"])

        # L3: 精度缩略或长度缩减（同语义族内缩容）
        for old_t, new_t in (
            ("varchar(100)", "varchar(50)"),
            ("text", "varchar(200)"),
            ("numeric(18,4)", "numeric(16,2)"),
            ("numeric(18,4)", "numeric(18,2)"),
            ("bigint", "integer"),
        ):
            r_shrink = classify_migration_risk(
                {
                    "old_col_name": "f_col",
                    "old_col_type": old_t,
                    "new_col_name": "f_col",
                    "new_col_type": new_t,
                }
            )
            self.assertEqual(r_shrink["risk_level"], "L3", f"{old_t}->{new_t} 应判为 L3")
            self.assertEqual(r_shrink["risk_rank"], 3)
            self.assertTrue(r_shrink["has_precision_shrink"])
            self.assertTrue(r_shrink["is_lossy"])

        # L4: 同族无损扩容与仅注释更新
        for old_t, new_t, old_c, new_c in (
            ("varchar(32)", "varchar(64)", "", ""),
            ("integer", "bigint", "", ""),
            ("numeric(10,2)", "numeric(18,4)", "", ""),
            ("varchar(32)", "varchar(32)", "旧注释", "新注释"),
            ("varchar(32)", "varchar(32)", "一致", "一致"),
        ):
            r_safe = classify_migration_risk(
                {
                    "old_col_name": "f_col",
                    "old_col_type": old_t,
                    "old_col_comment": old_c,
                    "new_col_name": "f_col",
                    "new_col_type": new_t,
                    "new_col_comment": new_c,
                }
            )
            self.assertEqual(r_safe["risk_level"], "L4", f"{old_t}->{new_t} 应判为 L4")
            self.assertEqual(r_safe["risk_rank"], 4)
            self.assertFalse(r_safe["is_lossy"])

    def test_workspace_export_import_preserves_changelog_and_checkpoints(self) -> None:
        """验证变更日志与 Checkpoint 随 .dataog.db 工作区无损导出导入且零密码泄露."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = PlatformContext(private_root=tmp, legacy_data_dir="")
            try:
                svc = StandardMigratorService(ctx)
                cp = svc.create_checkpoint("基准还原点", description="导入测试", connection_id="conn-1")
                self.assertTrue(cp["ok"])
                rec = svc._record_committed_change(
                    {
                        "schema_name": "dwd",
                        "table_name": "t_acct",
                        "ordinal_position": 2,
                        "before_col_name": "old_acct",
                        "before_col_type": "varchar(32)",
                        "before_col_comment": "",
                        "after_col_name": "acct_no",
                        "after_col_type": "varchar(64)",
                        "after_col_comment": "账号",
                        "strategy": "DIRECT_ALTER",
                        "forward_sql": "ALTER TABLE ...",
                        "rollback_sql": "ALTER TABLE ...",
                    },
                    conn_id="conn-1",
                    conn_name="数仓DWS",
                    db_type="DWS",
                )
                self.assertEqual(rec["risk_level"], "L1")

                exported_bytes, _fname = ctx.workspace_hub.export_workspace_bytes()
                export_file = os.path.join(tmp, "exported_cp.dataog.db")
                with open(export_file, "wb") as fh:
                    fh.write(exported_bytes)
                with contextlib.closing(sqlite3.connect(export_file)) as econn:
                    tables = {
                        str(r[0])
                        for r in econn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    self.assertIn("migration_change_logs", tables)
                    self.assertIn("migration_checkpoints", tables)
                    self.assertNotIn("connection_credentials", tables)

                ctx.workspace_hub.import_workspace_as_new(
                    filename="exported_cp.dataog.db",
                    raw_bytes=exported_bytes,
                    name="协作工作区2",
                    switch_immediately=True,
                )
                state = svc.list_changelog_state()
                self.assertEqual(state["summary"]["applied_count"], 1)
                self.assertEqual(len(state["checkpoints"]), 1)
                self.assertEqual(state["checkpoints"][0]["changes_since_count"], 1)
            finally:
                ctx.close()

    def test_execution_changelog_rollback_and_breakpoint_resume(self) -> None:
        """端到端测试单条/批量/重建日志落盘、冲突拦截、Checkpoint LIFO 回滚、断点续滚及绑标联动还原."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(private_root=tmp)
            ctx: PlatformContext = app.state.platform_ctx
            try:
                client = TestClient(app)
                prof = ctx.connection_hub.save_profile(
                    {
                        "connection_id": "dws-test-1",
                        "name": "DWS测试库",
                        "driver_track": "NATIVE",
                        "db_type": "DWS",
                        "host": "127.0.0.1",
                        "port": 8000,
                        "dbname": "dw_test",
                        "user": "tester",
                        "password": "secret",
                    },
                    connection_id="dws-test-1",
                )
                ctx.store.save_meta("migrator_connection_id", prof.connection_id)

                # 初始化标准字典与字段绑定，验证改名执行及回滚后绑定自动迁移/还原
                ctx.standard_catalog.upsert_item(
                    {
                        "std_code": "STD_CUST",
                        "cn_name": "客户编号",
                        "en_abbr": "cust_id",
                        "type_dws": "varchar(64)",
                    }
                )
                ctx.standard_catalog.upsert_field_bindings(
                    prof.connection_id,
                    [
                        {
                            "schema_name": "dwd",
                            "table_name": "t_cust",
                            "column_name": "old_cust",
                            "std_code": "STD_CUST",
                        }
                    ],
                )

                fake_state = _FakeDWSState()
                fake_state.add_table(
                    TableMeta(
                        schema_name="dwd",
                        table_name="t_cust",
                        exists=True,
                        columns=[
                            ColumnMeta(1, "old_cust", "varchar(32)", column_comment=""),
                            ColumnMeta(2, "tx_amt", "numeric(18,4)", column_comment="原金额"),
                        ],
                    )
                )
                fake_state.add_table(
                    TableMeta(
                        schema_name="dwd",
                        table_name="t_dist",
                        exists=True,
                        distribute_keys=["dist_key"],
                        distribute_clause="DISTRIBUTE BY HASH(dist_key)",
                        columns=[
                            ColumnMeta(1, "dist_key", "varchar(32)", column_comment="分布键"),
                            ColumnMeta(2, "remark", "varchar(100)", column_comment="备注"),
                        ],
                    )
                )

                ctx.store.replace_all_items(
                    [
                        {
                            "connection_id": prof.connection_id,
                            "schema_name": "dwd",
                            "table_name": "t_cust",
                            "old_col_name": "old_cust",
                            "old_col_type": "varchar(32)",
                            "old_col_comment": "",
                            "new_col_name": "cust_id",
                            "new_col_type": "varchar(64)",
                            "new_col_comment": "客户编号",
                            "status": "PENDING",
                        },
                        {
                            "connection_id": prof.connection_id,
                            "schema_name": "dwd",
                            "table_name": "t_cust",
                            "old_col_name": "tx_amt",
                            "old_col_type": "numeric(18,4)",
                            "old_col_comment": "原金额",
                            "new_col_name": "tx_amt",
                            "new_col_type": "numeric(16,2)",
                            "new_col_comment": "交易金额",
                            "status": "PENDING",
                        },
                        {
                            "connection_id": prof.connection_id,
                            "schema_name": "dwd",
                            "table_name": "t_dist",
                            "old_col_name": "dist_key",
                            "old_col_type": "varchar(32)",
                            "old_col_comment": "分布键",
                            "new_col_name": "acct_key",
                            "new_col_type": "varchar(64)",
                            "new_col_comment": "账户键",
                            "is_distribute_key": 1,
                            "status": "PENDING",
                        },
                    ],
                    "test_mig.xlsx",
                    preserve_history=False,
                )

                svc = StandardMigratorService(ctx)

                def _open_conn(self_eng: DWSMigratorEngine):
                    return _FakeDWSConn(fake_state)

                def _inspect(self_eng: DWSMigratorEngine, sch: str, tbl: str) -> TableMeta:
                    return fake_state.get_table(sch, tbl)

                def _fetch_batch(self_eng: DWSMigratorEngine, pairs: List[Tuple[str, str]]) -> Dict[Tuple[str, str], TableMeta]:
                    out: Dict[Tuple[str, str], TableMeta] = {}
                    for s, t in pairs:
                        m = fake_state.get_table(s, t)
                        if m.exists:
                            out[(s.lower(), t.lower())] = m
                    return out

                with (
                    mock.patch.object(DWSMigratorEngine, "_open_dws_connection", _open_conn),
                    mock.patch.object(DWSMigratorEngine, "inspect_table_metadata", _inspect),
                    mock.patch.object(DWSMigratorEngine, "_fetch_real_tables_metadata_batch", _fetch_batch),
                    mock.patch.object(
                        svc,
                        "get_table_compliance",
                        return_value={"ok": True, "columns": []},
                    ),
                ):
                    ws0 = svc.get_workspace_state()
                    self.assertEqual(ws0["summary"]["risk_counts"]["L1"], 2)
                    self.assertEqual(ws0["summary"]["risk_counts"]["L3"], 1)
                    items_by_col = {it["old_col_name"]: it for it in ws0["items"]}

                    # 1. 创建初始手动 Checkpoint
                    cp0 = client.post(
                        "/api/modules/standard_migrator/checkpoints",
                        json={"name": "初始安全点", "connection_id": prof.connection_id},
                    ).json()["checkpoint"]

                    # 2. 单条执行 L1 改名（old_cust -> cust_id）
                    res_s = svc.execute_single(items_by_col["old_cust"]["id"])
                    self.assertEqual(res_s["item"]["status"], "COMPLETED")
                    self.assertEqual(fake_state.get_table("dwd", "t_cust").columns[0].column_name, "cust_id")

                    # 绑标应同步迁移到 cust_id
                    with ctx.store.module_db(scope="workspace") as conn:
                        b_cols = [
                            r["column_name"]
                            for r in conn.execute(
                                "SELECT column_name FROM field_standard_bindings WHERE std_code = 'STD_CUST'"
                            ).fetchall()
                        ]
                    self.assertEqual(b_cols, ["cust_id"])

                    # 3. 批量执行剩余（含 L3 tx_amt + 分布键重建 t_dist + auto_checkpoint）
                    res_b = svc.execute_batch(
                        [items_by_col["tx_amt"]["id"], items_by_col["dist_key"]["id"]],
                        max_workers=1,
                        keep_backup=True,
                        auto_checkpoint=True,
                        checkpoint_name="批前自动点",
                    )
                    self.assertEqual(res_b["batch_result"]["success_count"], 2)
                    self.assertIn("created_checkpoint", res_b)
                    self.assertEqual(fake_state.get_table("dwd", "t_dist").columns[0].column_name, "acct_key")

                    # 校验备份表名与日志完全一致且物理存在
                    cl_state = client.get("/api/modules/standard_migrator/changelog").json()
                    self.assertEqual(cl_state["summary"]["applied_count"], 3)
                    logs_desc = cl_state["logs"]
                    reb_log = next(x for x in logs_desc if x["strategy"] == "TABLE_REBUILD")
                    self.assertTrue(reb_log["backup_table_name"])
                    self.assertTrue(fake_state.get_table("dwd", reb_log["backup_table_name"]).exists)

                    # 4. 测试单条同列或链式冲突拦截：先在同一表追加第 2 次对 cust_id 的变更
                    rec_chain = svc._record_committed_change(
                        {
                            "schema_name": "dwd",
                            "table_name": "t_cust",
                            "ordinal_position": 1,
                            "before_col_name": "cust_id",
                            "before_col_type": "varchar(64)",
                            "before_col_comment": "客户编号",
                            "after_col_name": "cust_no",
                            "after_col_type": "varchar(64)",
                            "after_col_comment": "客户号",
                            "strategy": "DIRECT_ALTER",
                        },
                        conn_id=prof.connection_id,
                        conn_name=prof.name,
                        db_type="DWS",
                    )
                    # 同时更新假表列名到 cust_no
                    fake_state.tables[("dwd", "t_cust")].columns[0].column_name = "cust_no"

                    first_cust_log = next(
                        x for x in logs_desc if x["before_col_name"] == "old_cust"
                    )
                    pv_conflict = client.post(
                        "/api/modules/standard_migrator/rollback/preview",
                        json={"change_ids": [first_cust_log["id"]]},
                    ).json()
                    self.assertTrue(pv_conflict["has_conflict"])
                    self.assertIn("存在更新的生效变更", pv_conflict["conflict_message"] or pv_conflict["conflicts"][0]["reason"])

                    # 5. 测试 JSON 底座导出（链式 old_cust -> cust_id -> cust_no 应自动折叠为 old_cust -> cust_no）
                    json_resp = client.get("/api/modules/standard_migrator/changelog/export.json")
                    self.assertEqual(json_resp.status_code, 200)
                    exported_json = json.loads(json_resp.text)
                    cust_map = [
                        m
                        for m in exported_json["effective_column_mappings"]
                        if m["table_name"] == "t_cust" and m["original_col_name"] == "old_cust"
                    ]
                    self.assertEqual(len(cust_map), 1)
                    self.assertEqual(cust_map[0]["current_col_name"], "cust_no")

                    # 测试 Excel 导出双 Sheet
                    xlsx_resp = client.get("/api/modules/standard_migrator/changelog/export.xlsx")
                    self.assertEqual(xlsx_resp.status_code, 200)
                    wb = openpyxl.load_workbook(io.BytesIO(xlsx_resp.content))
                    self.assertEqual(wb.sheetnames, ["变更日志台账", "Checkpoint快照"])

                    # 6. 预览 Checkpoint #cp0 回滚计划（应按 LIFO：先 rec_chain，再重建组/tx_amt，最后 old_cust）
                    pv_cp0 = client.post(
                        "/api/modules/standard_migrator/rollback/preview",
                        json={"checkpoint_id": cp0["id"], "prefer_backup_table": True},
                    ).json()
                    self.assertFalse(pv_cp0["has_conflict"])
                    self.assertEqual(pv_cp0["target_change_count"], 4)
                    self.assertTrue(any(s["mode"] == "BACKUP_SWAP" for s in pv_cp0["steps"]))
                    self.assertIn("IS NULL", pv_cp0["combined_rollback_sql"])

                    # 无备份表偏好时，L3 变更步骤应提示无法还原旧精度数值
                    pv_no_bak = client.post(
                        "/api/modules/standard_migrator/rollback/preview",
                        json={"checkpoint_id": cp0["id"], "prefer_backup_table": False},
                    ).json()
                    self.assertTrue(any("无法还原已截断" in w for w in pv_no_bak["warnings"]))

                    # 7. 测试遇错即停保留断点：在回滚 tx_amt (numeric(18,4)) 时注入失败
                    fake_state.fail_on_sql_substr = "numeric(18,4)"
                    rb_partial = svc.execute_rollback(checkpoint_id=cp0["id"], prefer_backup_table=True)
                    self.assertFalse(rb_partial["ok"])
                    self.assertTrue(rb_partial["partial_rolled_back"])
                    self.assertGreaterEqual(rb_partial["completed_steps"], 1)
                    self.assertIn("numeric(18,4)", rb_partial["error"])

                    # 清除故障，再次一键回滚同一 Checkpoint，自动续滚剩余步骤直到全部恢复
                    fake_state.fail_on_sql_substr = None
                    rb_done = svc.execute_rollback(checkpoint_id=cp0["id"], prefer_backup_table=True)
                    self.assertTrue(rb_done["ok"])
                    self.assertFalse(rb_done["partial_rolled_back"])

                    # 物理表字段、注释与备份表应完全回滚到位
                    t_cust_final = fake_state.get_table("dwd", "t_cust")
                    self.assertEqual(t_cust_final.columns[0].column_name, "old_cust")
                    self.assertEqual(t_cust_final.columns[0].data_type, "varchar(32)")
                    self.assertEqual(t_cust_final.columns[0].column_comment, "")
                    self.assertEqual(t_cust_final.columns[1].data_type, "numeric(18,4)")
                    self.assertEqual(fake_state.get_table("dwd", "t_dist").columns[0].column_name, "dist_key")

                    # 绑标也应自动逆向还原回 old_cust
                    with ctx.store.module_db(scope="workspace") as conn:
                        b_cols_after = [
                            r["column_name"]
                            for r in conn.execute(
                                "SELECT column_name FROM field_standard_bindings WHERE std_code = 'STD_CUST'"
                            ).fetchall()
                        ]
                    self.assertEqual(b_cols_after, ["old_cust"])

                    # 队列状态应已同步复位为非 COMPLETED
                    ws_after = svc.get_workspace_state()
                    self.assertEqual(ws_after["summary"]["completed"], 0)

                    # 删除 Checkpoint 不影响审计日志留存
                    del_cp = client.delete(
                        f"/api/modules/standard_migrator/checkpoints/{cp0['id']}"
                    ).json()
                    self.assertTrue(del_cp["ok"])
                    cl_final = client.get("/api/modules/standard_migrator/changelog").json()
                    self.assertEqual(cl_final["summary"]["applied_count"], 0)
                    self.assertGreaterEqual(cl_final["summary"]["rolled_back_count"], 4)
                    self.assertGreaterEqual(cl_final["summary"]["rollback_event_count"], 3)
            finally:
                ctx.close()

    def test_frontend_migrator_risk_and_changelog_modules_syntax_and_security(self) -> None:
        """校验新增与修改的前端 ES Modules 严格零 XSS、零阻塞弹窗且语法通过 Node.js 检查."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_files = [
            os.path.join(root_dir, "static", "js", "api", "migrator_api.js"),
            os.path.join(root_dir, "static", "js", "views", "standard_migrator", "changelog_panel.js"),
            os.path.join(root_dir, "static", "js", "views", "standard_migrator", "modals.js"),
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
