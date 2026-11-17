"""DataOG v2.0.0 平台底座核心单元测试 (core/platform/*).

全面验证：
1. 旧版 `dws_migrator_state.db` 与旧版连接键值 (`dws_conn_saved`, `cross_source_conn`, `cross_target_conn`) 自动无损迁移至 `dataog_state.db`；
2. 模块级 SQLite 表幂等列自检演进 (`register_table_schema`)；
3. 双轨驱动引擎 (`DriverManager`)：SQLite 原生连接、自定义 JDBC `.jar` 安全校验（魔数头、路径穿越拦截、SHA-256、SPI 主类探测）、无 Java 环境降级隔离与通用 `DatabaseMetaData` 探查适配；
4. 全局持久化元数据共享中枢 (`MetadataHub`)：目录探查、SQLite 持久化缓存命中、关键词全文检索与四层递归视图血缘穿透 (`v_l4 -> v_l3 -> v_l2 -> v_l1 -> base_table`)；
5. 通用简单查询引擎 (`QueryEngine`)：SQL 执行、`LIMIT` 安全截断、表快速预览、异常路径与 CSV / Excel 导出。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from typing import Any, Dict, List
from unittest import mock

import openpyxl

from core.platform.context import PlatformContext
from core.platform.driver_manager import JdbcConnectionAdapter
from core.platform.llm_hub import LLMHub, extract_json_payload
from core.platform.readonly_guard import ReadOnlyViolationError, validate_readonly_sql
from core.platform.storage import PlatformStateStore


def _make_valid_driver_jar(driver_class: str = "dm.jdbc.driver.DmDriver") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        zf.writestr("META-INF/services/java.sql.Driver", f"{driver_class}\n")
        zf.writestr("dm/jdbc/driver/DmDriver.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x34")
    return buf.getvalue()


class _FakeResultSet:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self._idx = -1
        self.closed = False

    def next(self) -> bool:
        self._idx += 1
        return self._idx < len(self._rows)

    def getString(self, col: Any) -> Any:
        row = self._rows[self._idx]
        if isinstance(col, int):
            keys = list(row.keys())
            return row[keys[col - 1]] if 1 <= col <= len(keys) else None
        return row.get(str(col))

    def getInt(self, col: Any) -> int:
        val = self.getString(col)
        return int(val) if val is not None else 0

    def close(self) -> None:
        self.closed = True


class _FakeDatabaseMetaData:
    def getSchemas(self) -> _FakeResultSet:
        return _FakeResultSet([{"TABLE_SCHEM": "SYSDBA"}, {"TABLE_SCHEM": "INFORMATION_SCHEMA"}])

    def getCatalogs(self) -> _FakeResultSet:
        return _FakeResultSet([])

    def getTables(self, _cat: Any, schema_pat: Any, table_pat: Any, _types: Any) -> _FakeResultSet:
        all_rows = [
            {
                "TABLE_CAT": "",
                "TABLE_SCHEM": "SYSDBA",
                "TABLE_NAME": "DM_CUSTOMER",
                "TABLE_TYPE": "TABLE",
                "REMARKS": "达梦客户主表",
            },
            {
                "TABLE_CAT": "",
                "TABLE_SCHEM": "SYSDBA",
                "TABLE_NAME": "V_DM_VIP",
                "TABLE_TYPE": "VIEW",
                "REMARKS": "达梦高净值客户视图",
            },
        ]
        filtered = [
            r
            for r in all_rows
            if (not schema_pat or r["TABLE_SCHEM"] == schema_pat)
            and (table_pat in (None, "%") or r["TABLE_NAME"] == table_pat)
        ]
        return _FakeResultSet(filtered)

    def getPrimaryKeys(self, _cat: Any, _sch: Any, _tbl: Any) -> _FakeResultSet:
        return _FakeResultSet([{"COLUMN_NAME": "CUST_ID"}])

    def getColumns(self, _cat: Any, _sch: Any, _tbl: Any, _col_pat: Any) -> _FakeResultSet:
        return _FakeResultSet(
            [
                {
                    "COLUMN_NAME": "CUST_ID",
                    "TYPE_NAME": "VARCHAR",
                    "COLUMN_SIZE": 32,
                    "DECIMAL_DIGITS": 0,
                    "NULLABLE": 0,
                    "COLUMN_DEF": None,
                    "REMARKS": "客户主键编号",
                    "ORDINAL_POSITION": 1,
                },
                {
                    "COLUMN_NAME": "BAL_AMT",
                    "TYPE_NAME": "DECIMAL",
                    "COLUMN_SIZE": 18,
                    "DECIMAL_DIGITS": 2,
                    "NULLABLE": 1,
                    "COLUMN_DEF": "0.00",
                    "REMARKS": "账户余额",
                    "ORDINAL_POSITION": 2,
                },
            ]
        )


class _FakeRawJdbcConnection:
    def getMetaData(self) -> _FakeDatabaseMetaData:
        return _FakeDatabaseMetaData()

    def close(self) -> None:
        pass


class TestDataOGPlatformFoundation(unittest.TestCase):
    def test_legacy_state_db_and_connection_keys_lossless_migration(self) -> None:
        """验证从历史 v1.0.0 dws_migrator_state.db 无损迁移表数据与连接档案至 dataog_state.db."""
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_path = os.path.join(tmpdir, "dws_migrator_state.db")
            legacy_store = PlatformStateStore(legacy_path)
            legacy_store.replace_all_items(
                [
                    {
                        "row_index": 1,
                        "schema_name": "public",
                        "table_name": "t_legacy_order",
                        "old_col_name": "ord_id",
                        "old_col_type": "varchar(20)",
                        "old_col_comment": "旧单号",
                        "new_col_name": "order_id",
                        "new_col_type": "varchar(64)",
                        "new_col_comment": "标准订单号",
                    }
                ],
                "legacy_checklist.xlsx",
            )
            legacy_store.save_meta(
                "dws_conn_saved",
                {
                    "configured": True,
                    "host": "10.20.30.40",
                    "port": 8000,
                    "dbname": "gaussdb",
                    "user": "dws_admin",
                    "password": "LegacySecretPwd!99",
                    "sslmode": "prefer",
                },
            )
            legacy_store.save_meta(
                "cross_source_conn",
                {
                    "configured": True,
                    "db_type": "INCEPTOR",
                    "host": "10.20.30.50",
                    "port": 10000,
                    "dbname": "ods_hive",
                    "schema_name": "ods_hive",
                    "user": "hive_reader",
                    "password": "HiveSecret!88",
                    "auth_mode": "LDAP",
                },
            )

            # 在同目录初始化新版 dataog_state.db，触发自动检测与无损升级
            new_db_path = os.path.join(tmpdir, "dataog_state.db")
            ctx = PlatformContext(db_path=new_db_path)

            items = ctx.store.list_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["table_name"], "t_legacy_order")
            self.assertEqual(items[0]["new_col_name"], "order_id")

            profiles = ctx.store.list_connection_profiles()
            self.assertGreaterEqual(len(profiles), 2)
            by_host = {p.host: p for p in profiles}
            self.assertIn("10.20.30.40", by_host)
            self.assertEqual(by_host["10.20.30.40"].password, "LegacySecretPwd!99")
            # 公开字典必须对密码脱敏，严禁明文暴露
            pub_dict = by_host["10.20.30.40"].to_public_dict()
            self.assertNotIn("LegacySecretPwd!99", json.dumps(pub_dict))
            self.assertTrue(pub_dict["has_saved_password"])

            # 二次重启必须保持幂等，不重复生成连接档案
            count_before = len(profiles)
            ctx_reopen = PlatformContext(db_path=new_db_path)
            self.assertEqual(len(ctx_reopen.store.list_connection_profiles()), count_before)

    def test_module_table_schema_idempotent_evolution(self) -> None:
        """验证模块表结构注册与新增列幂等演进 (register_table_schema)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PlatformStateStore(os.path.join(tmpdir, "dataog_state.db"))
            store.register_table_schema(
                "mod_custom_audit",
                "CREATE TABLE IF NOT EXISTS mod_custom_audit (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
                [("id", "INTEGER"), ("name", "TEXT NOT NULL DEFAULT ''")],
            )
            with store._get_conn() as conn:
                conn.execute("INSERT INTO mod_custom_audit (name) VALUES ('rule_alpha')")
                conn.commit()

            # 模拟新版本为该模块表追加两个带默认值的新字段
            store.register_table_schema(
                "mod_custom_audit",
                "CREATE TABLE IF NOT EXISTS mod_custom_audit (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
                [
                    ("id", "INTEGER"),
                    ("name", "TEXT NOT NULL DEFAULT ''"),
                    ("severity", "TEXT NOT NULL DEFAULT 'MEDIUM'"),
                    ("retry_count", "INTEGER NOT NULL DEFAULT 3"),
                ],
            )
            with store._get_conn() as conn:
                row = dict(conn.execute("SELECT * FROM mod_custom_audit WHERE name = 'rule_alpha'").fetchone())
                self.assertEqual(row["severity"], "MEDIUM")
                self.assertEqual(row["retry_count"], 3)

    def test_driver_manager_jar_security_no_java_fallback_and_jdbc_metadata(self) -> None:
        """验证自定义 JDBC .jar 安全上传拦截、无 Java 优雅报错及通用 DatabaseMetaData 回退探查."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = PlatformContext(db_path=os.path.join(tmpdir, "dataog_state.db"))
            dm = ctx.driver_manager

            # 1. 非 .jar 扩展名拦截
            with self.assertRaises(ValueError):
                dm.register_uploaded_jar(filename="evil.exe", content=b"PK\x03\x04dummy")

            # 2. 空文件与错误魔数头拦截
            with self.assertRaises(ValueError):
                dm.register_uploaded_jar(filename="empty.jar", content=b"")
            with self.assertRaises(ValueError):
                dm.register_uploaded_jar(filename="fake.jar", content=b"NOT_A_VALID_JAR_ARCHIVE")

            # 3. Zip-Slip 路径穿越攻击包拦截
            slip_buf = io.BytesIO()
            with zipfile.ZipFile(slip_buf, "w") as zf:
                zf.writestr("../../outside.class", b"malicious")
            with self.assertRaises(ValueError):
                dm.register_uploaded_jar(filename="slip.jar", content=slip_buf.getvalue())

            # 4. 合法 JDBC .jar 上传、SHA-256 指纹校验与 SPI Driver 类自动识别
            valid_jar = _make_valid_driver_jar("dm.jdbc.driver.DmDriver")
            expected_sha = hashlib.sha256(valid_jar).hexdigest()
            pkg = dm.register_uploaded_jar(filename="DmJdbcDriver18.jar", content=valid_jar, name="达梦DM8驱动")
            self.assertEqual(pkg.sha256, expected_sha)
            self.assertEqual(pkg.driver_class_hint, "dm.jdbc.driver.DmDriver")

            # 5. 模拟无 JRE 环境时：JDBC 连接优雅返回可读提示，同时内置 SQLite 原生连接完全不受影响
            jdbc_prof = ctx.connection_hub.save_profile(
                {
                    "name": "测试达梦库",
                    "driver_track": "JDBC",
                    "db_type": "DM8",
                    "jdbc_driver_id": pkg.driver_id,
                    "jdbc_url": "jdbc:dm://127.0.0.1:5236/DAMENG",
                    "user": "SYSDBA",
                    "password": "SYSDBA",
                }
            )
            sqlite_db_file = os.path.join(tmpdir, "biz.sqlite")
            sqlite_prof = ctx.connection_hub.save_profile(
                {
                    "name": "本地SQLite业务库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": sqlite_db_file,
                }
            )
            with mock.patch.object(
                dm, "_ensure_jvm_started", side_effect=RuntimeError("当前系统未安装 Java 运行环境 (JRE)")
            ):
                with self.assertRaises(RuntimeError) as cm:
                    with ctx.connection_hub.session(jdbc_prof.connection_id):
                        pass
                self.assertIn("JRE", str(cm.exception))

                # 原生通道依然正常连接并查询
                res = ctx.query_engine.execute_sql(sqlite_prof.connection_id, "SELECT 42 AS answer")
                self.assertEqual(res.rows, [[42]])

            # 6. 验证通用 JDBC DatabaseMetaData 回退探查器可完整提取模式、表/视图与主键精度
            fake_adapter = JdbcConnectionAdapter(_FakeRawJdbcConnection())
            schemas, tables = ctx.metadata_hub._probe_jdbc_catalog(fake_adapter, jdbc_prof, None)
            self.assertEqual([s.schema_name for s in schemas], ["SYSDBA"])
            self.assertEqual(len(tables), 2)

            tbl_meta = ctx.metadata_hub._probe_jdbc_table(fake_adapter, jdbc_prof, "sysdba", "dm_customer")
            self.assertTrue(tbl_meta.exists)
            self.assertEqual(tbl_meta.primary_keys, ["CUST_ID"])
            self.assertEqual(tbl_meta.columns[0].data_type, "VARCHAR(32)")
            self.assertTrue(tbl_meta.columns[0].is_not_null)
            self.assertEqual(tbl_meta.columns[1].data_type, "DECIMAL(18,2)")

    def test_metadata_hub_cache_search_and_four_layer_view_penetration(self) -> None:
        """验证 MetadataHub 持久化索引缓存、全局秒级检索以及真实四层嵌套视图血缘穿透."""
        with tempfile.TemporaryDirectory() as tmpdir:
            biz_sqlite = os.path.join(tmpdir, "warehouse.sqlite")
            with contextlib.closing(sqlite3.connect(biz_sqlite)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE dwd_trade_order (
                        order_id VARCHAR(64) PRIMARY KEY,
                        cust_code VARCHAR(32) NOT NULL,
                        pay_amt DECIMAL(18,2),
                        created_dt DATE
                    );
                    CREATE VIEW v_order_l1 AS
                        SELECT order_id, cust_code, pay_amt, created_dt FROM dwd_trade_order;
                    CREATE VIEW v_order_l2 AS
                        SELECT order_id, cust_code AS customer_code, pay_amt, created_dt FROM v_order_l1;
                    CREATE VIEW v_order_l3 AS
                        SELECT order_id, customer_code, pay_amt AS order_amount, created_dt FROM v_order_l2;
                    CREATE VIEW v_order_l4 AS
                        SELECT order_id, customer_code AS vip_cust_code, order_amount, created_dt FROM v_order_l3;
                    """
                )

            ctx = PlatformContext(db_path=os.path.join(tmpdir, "dataog_state.db"))
            prof = ctx.connection_hub.save_profile(
                {
                    "name": "SQLite数仓",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": biz_sqlite,
                }
            )

            tree = ctx.metadata_hub.refresh_catalog(prof.connection_id)
            self.assertEqual(len(tree["tables"]), 5)

            # 穿透第 4 层视图 v_order_l4，验证其能够跨越 v_order_l3 -> v_order_l2 -> v_order_l1 还原到底表 dwd_trade_order 的真实列类型！
            v4_meta = ctx.metadata_hub.get_table_metadata(
                prof.connection_id, "main", "v_order_l4", force_refresh=True, penetrate_views=True
            )
            self.assertTrue(v4_meta.exists)
            self.assertEqual(v4_meta.object_type, "VIEW")
            col_by_name = {c.column_name: c for c in v4_meta.columns}
            self.assertEqual(col_by_name["vip_cust_code"].data_type.upper(), "VARCHAR(32)")
            self.assertEqual(col_by_name["order_amount"].data_type.upper(), "DECIMAL(18,2)")
            self.assertTrue(any("dwd_trade_order" in b for b in v4_meta.penetrated_base_tables))

            # 验证缓存命中：即使屏蔽底层连接打开，不带 force_refresh 时仍可从本地 SQLite 元数据索引零延迟读取
            with mock.patch.object(
                ctx.connection_hub, "session", side_effect=AssertionError("不应触发实时数据库连接")
            ):
                cached_v4 = ctx.metadata_hub.get_table_metadata(
                    prof.connection_id, "main", "v_order_l4", force_refresh=False
                )
                self.assertEqual(len(cached_v4.columns), 4)

            # 验证全局关键词检索同时命中表级与字段级记录
            hits = ctx.metadata_hub.search_objects("vip_cust_code", connection_id=prof.connection_id)
            self.assertTrue(any(h["hit_kind"] == "COLUMN" and h["column_name"] == "vip_cust_code" for h in hits))

    def test_query_engine_truncation_preview_errors_and_exports(self) -> None:
        """验证 QueryEngine 安全行数截断、表快速预览、空/错误 SQL 拦截与 CSV/Excel 导出."""
        with tempfile.TemporaryDirectory() as tmpdir:
            biz_sqlite = os.path.join(tmpdir, "query_demo.sqlite")
            with contextlib.closing(sqlite3.connect(biz_sqlite)) as conn:
                conn.execute("CREATE TABLE t_metrics (id INTEGER PRIMARY KEY, label TEXT, score REAL)")
                conn.executemany(
                    "INSERT INTO t_metrics (id, label, score) VALUES (?, ?, ?)",
                    [(i, f"metric_{i},special", i * 1.5) for i in range(1, 26)],
                )
                conn.commit()

            ctx = PlatformContext(db_path=os.path.join(tmpdir, "dataog_state.db"))
            prof = ctx.connection_hub.save_profile(
                {
                    "name": "查询测试库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": biz_sqlite,
                }
            )

            # 1. LIMIT 10 截断检测 (25 行数据取前 10 行，truncated 必须为 True)
            q_trunc = ctx.query_engine.execute_sql(
                prof.connection_id, "SELECT id, label, score FROM t_metrics ORDER BY id", limit=10
            )
            self.assertEqual(q_trunc.row_count, 10)
            self.assertTrue(q_trunc.truncated)

            # 2. LIMIT 50 完整返回 (truncated 必须为 False)
            q_full = ctx.query_engine.execute_sql(
                prof.connection_id, "SELECT id, label, score FROM t_metrics ORDER BY id;", limit=50
            )
            self.assertEqual(q_full.row_count, 25)
            self.assertFalse(q_full.truncated)

            # 3. 表数据快速预览接口
            preview = ctx.query_engine.preview_table(prof.connection_id, "main", "t_metrics", limit=5)
            self.assertEqual(preview.row_count, 5)
            self.assertTrue(preview.truncated)

            # 4. 边界与错误 SQL 验证
            with self.assertRaises(ValueError):
                ctx.query_engine.execute_sql(prof.connection_id, "   ")
            with self.assertRaises(Exception):
                ctx.query_engine.execute_sql(prof.connection_id, "SELECT * FROM non_existent_xyz_table")

            # 5. CSV 与 Excel 导出验证
            col_names = [c.name for c in q_trunc.columns]
            csv_bytes, csv_mime = ctx.query_engine.export_result_bytes(
                columns=col_names, rows=q_trunc.rows, fmt="csv", title="指标导出"
            )
            self.assertIn("text/csv", csv_mime)
            self.assertIn("metric_1,special", csv_bytes.decode("utf-8-sig"))

            xlsx_bytes, xlsx_mime = ctx.query_engine.export_result_bytes(
                columns=col_names, rows=q_trunc.rows, fmt="xlsx", title="指标导出"
            )
            self.assertIn("spreadsheetml", xlsx_mime)
            wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
            ws = wb.active
            self.assertEqual(ws.max_row, 11)  # 1 表头 + 10 数据行

    def test_jar_backslash_zip_slip_jvm_thread_attach_and_bounded_query_pushdown(self) -> None:
        """回归验证：反斜杠路径穿越拦截、JRE 状态双字段契约、跨线程 attachThreadToJVM 与无界 SELECT 保护."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = PlatformContext(
                db_path=os.path.join(tmpdir, "dataog_state.db"),
                drivers_dir=os.path.join(tmpdir, "drivers"),
            )
            dm = ctx.driver_manager

            # 1. Windows 风格反斜杠路径穿越与盘符注入在任意 OS 上均被拦截
            for evil_entry in ("foo\\..\\", "foo\\..\\..\\evil.class", "C:/evil.class"):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
                    zf.writestr(evil_entry, b"\xca\xfe\xba\xbe")
                with self.assertRaises(ValueError):
                    dm.validate_and_inspect_jar_bytes("evil.jar", buf.getvalue())

            # 2. JRE 状态同时暴露 jdbc_extension_ready/note 与 available/message
            jre_status = dm.get_java_runtime_status()
            self.assertIn("jdbc_extension_ready", jre_status)
            self.assertIn("available", jre_status)
            self.assertEqual(jre_status["available"], jre_status["jdbc_extension_ready"])
            self.assertEqual(jre_status["message"], jre_status["note"])

            # 3. 多线程调用 _ensure_jvm_started 时自动为未挂载工作线程执行 attachThreadToJVM
            fake_jpype = mock.MagicMock()
            fake_jpype.isJVMStarted.return_value = True
            fake_jpype.isThreadAttachedToJVM.return_value = False
            with mock.patch.dict("sys.modules", {"jpype": fake_jpype}):
                ret_jpype = dm._ensure_jvm_started()
                self.assertIs(ret_jpype, fake_jpype)
                fake_jpype.attachThreadToJVM.assert_called_once()

            # 4. QueryEngine 无界 SELECT 自动追加 LIMIT 保护，且不破坏已有 LIMIT / EXPLAIN
            qe = ctx.query_engine
            bounded = qe._build_bounded_select_sql("SELECT * FROM big_tbl;", "PGSQL", 501)
            self.assertTrue(bounded.endswith("LIMIT 501"))
            unchanged = qe._build_bounded_select_sql("SELECT * FROM big_tbl LIMIT 10;", "PGSQL", 501)
            self.assertEqual(unchanged, "SELECT * FROM big_tbl LIMIT 10;")
            explain_sql = qe._build_bounded_select_sql("EXPLAIN SELECT * FROM big_tbl", "PGSQL", 501)
            self.assertEqual(explain_sql, "EXPLAIN SELECT * FROM big_tbl")

            # 5. JdbcCursorAdapter.execute 支持 max_rows 向下传递至 Statement.setMaxRows
            fake_jdbc_conn = mock.MagicMock()
            fake_stmt = mock.MagicMock()
            fake_stmt.execute.return_value = False
            fake_stmt.getUpdateCount.return_value = 0
            fake_jdbc_conn.createStatement.return_value = fake_stmt
            jdbc_adapter = JdbcConnectionAdapter(fake_jdbc_conn)
            with jdbc_adapter.cursor() as cur:
                cur.execute("SELECT 1", timeout_sec=15, max_rows=201)
            fake_stmt.setQueryTimeout.assert_called_once_with(15)
            fake_stmt.setMaxRows.assert_called_once_with(201)

    def test_asset_store_wallpaper_and_icons_magic_bytes_security_and_persistence(self) -> None:
        """验证 AssetStore 光栅魔数头校验、SVG/路径穿越阻断、旧文件清理与跨重启持久化."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        jpg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 32
        webp_bytes = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 24
        gif_bytes = b"GIF89a" + b"\x00" * 24

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "dataog_state.db")
            assets_dir = os.path.join(tmpdir, "assets")
            ctx = PlatformContext(db_path=db_path, assets_dir=assets_dir)
            ast_store = ctx.asset_store

            # 1. 阻断 SVG / HTML 脚本伪装与非法槽位
            with self.assertRaises(ValueError):
                ast_store.save_uploaded_asset(
                    slot="wallpaper",
                    filename="xss.svg",
                    content=b"<svg onload='alert(1)'></svg>",
                )
            with self.assertRaises(ValueError):
                ast_store.save_uploaded_asset(
                    slot="brand",
                    filename="disguised.png",
                    content=b"<html><script>alert(1)</script></html>",
                )
            with self.assertRaises(ValueError):
                ast_store.save_uploaded_asset(
                    slot="app:unknown_module",
                    filename="ok.png",
                    content=png_bytes,
                )

            # 2. 上传壁纸、主品牌图标与模块图标（覆盖 PNG / JPG / WebP / GIF）
            s1 = ast_store.save_uploaded_asset(slot="wallpaper", filename="desk.png", content=png_bytes)
            self.assertEqual(s1["wallpaper_id"], "custom")
            first_wp_name = s1["custom_wallpaper_url"].rsplit("/", 1)[-1]
            self.assertTrue(os.path.isfile(os.path.join(assets_dir, first_wp_name)))

            # 替换壁纸时自动清理旧壁纸文件
            s2 = ast_store.save_uploaded_asset(slot="wallpaper", filename="desk2.jpg", content=jpg_bytes)
            second_wp_name = s2["custom_wallpaper_url"].rsplit("/", 1)[-1]
            self.assertNotEqual(first_wp_name, second_wp_name)
            self.assertFalse(os.path.isfile(os.path.join(assets_dir, first_wp_name)))
            self.assertTrue(os.path.isfile(os.path.join(assets_dir, second_wp_name)))

            ast_store.save_uploaded_asset(slot="brand", filename="logo.webp", content=webp_bytes)
            s_icons = ast_store.save_uploaded_asset(slot="app:polyglot", filename="poly.gif", content=gif_bytes)
            self.assertTrue(s_icons["brand_icon_url"].endswith(".webp"))
            self.assertTrue(s_icons["app_icons"]["polyglot"].endswith(".gif"))

            # 3. 路径穿越读取必须返回 None
            self.assertIsNone(ast_store.read_asset_bytes("../dataog_state.db"))
            self.assertIsNone(ast_store.read_asset_bytes("..\\dataog_state.db"))
            self.assertIsNone(ast_store.read_asset_bytes("not_a_uuid.png"))

            # 4. 重新打开 PlatformContext 验证持久化，随后执行重置确认磁盘清理
            ctx_reopen = PlatformContext(db_path=db_path, assets_dir=assets_dir)
            re_state = ctx_reopen.asset_store.get_public_appearance()
            self.assertEqual(re_state["wallpaper_id"], "custom")
            self.assertIn("polyglot", re_state["app_icons"])
            fav = ctx_reopen.asset_store.read_brand_favicon()
            self.assertIsNotNone(fav)
            self.assertEqual(fav[1], "image/webp")

            cleared = ctx_reopen.asset_store.reset_slot("all")
            self.assertEqual(cleared["wallpaper_id"], "default")
            self.assertEqual(cleared["custom_wallpaper_url"], "")
            self.assertEqual(cleared["brand_icon_url"], "")
            self.assertEqual(cleared["app_icons"], {})
            self.assertEqual(os.listdir(assets_dir), [])

    def test_llm_hub_presets_secret_masking_and_mock_openai_client(self) -> None:
        """验证 LLMHub 内置 DeepSeek/Gemini 预设、API Key 严格脱敏与 OpenAI 兼容协议客户端."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PlatformStateStore(os.path.join(tmpdir, "dataog_state.db"))

            class _FakeCompletions:
                def create(self, **kwargs: Any) -> Any:
                    msg = mock.MagicMock()
                    msg.content = f"OK:{kwargs.get('model')}"
                    choice = mock.MagicMock()
                    choice.message = msg
                    resp = mock.MagicMock()
                    resp.choices = [choice]
                    return resp

            class _FakeModels:
                def list(self) -> Any:
                    m1 = mock.MagicMock()
                    m1.id = "models/gemini-2.5-flash"
                    m2 = mock.MagicMock()
                    m2.id = "models/gemini-2.5-pro"
                    res = mock.MagicMock()
                    res.data = [m1, m2]
                    return res

            class _FakeOpenAIClient:
                def __init__(self) -> None:
                    self.chat = mock.MagicMock()
                    self.chat.completions = _FakeCompletions()
                    self.models = _FakeModels()

            hub = LLMHub(store, client_factory=lambda _prof, _timeout: _FakeOpenAIClient())

            # 1. 验证内置 DeepSeek 与 Google Gemini 预设及禁止删除保护
            providers = {p["provider_id"]: p for p in hub.list_providers()}
            self.assertIn("deepseek", providers)
            self.assertIn("gemini", providers)
            self.assertEqual(providers["deepseek"]["base_url"], "https://api.deepseek.com")
            self.assertIn("generativelanguage.googleapis.com/v1beta/openai/", providers["gemini"]["base_url"])
            with self.assertRaises(ValueError):
                hub.delete_provider("deepseek")

            # 2. 非法协议与内嵌凭据 URL 拦截
            with self.assertRaises(ValueError):
                hub.save_provider({"provider_id": "deepseek", "base_url": "file:///etc/passwd"})
            with self.assertRaises(ValueError):
                hub.save_provider({"provider_id": "deepseek", "base_url": "https://u:p@api.deepseek.com"})

            # 3. 保存密钥后公开接口严格脱敏，且留空或回传掩码时原密钥不丢失
            raw_secret = "sk-deepseek-super-secret-987654"
            hub.save_provider(
                {
                    "provider_id": "deepseek",
                    "name": "DeepSeek",
                    "base_url": "https://api.deepseek.com",
                    "api_key": raw_secret,
                    "default_model": "deepseek-chat",
                    "enabled": True,
                    "is_default": True,
                }
            )
            pub_list = hub.list_providers()
            self.assertNotIn(raw_secret, json.dumps(pub_list))
            ds_pub = next(p for p in pub_list if p["provider_id"] == "deepseek")
            self.assertTrue(ds_pub["has_api_key"])
            self.assertIn("••••", ds_pub["api_key_masked"])

            # 回传掩码或空字符串保持原密钥不变
            hub.save_provider({"provider_id": "deepseek", "api_key": ds_pub["api_key_masked"]})
            self.assertEqual(store.get_llm_provider("deepseek").api_key, raw_secret)
            hub.save_provider({"provider_id": "deepseek", "api_key": ""})
            self.assertEqual(store.get_llm_provider("deepseek").api_key, raw_secret)

            # 4. 连通测试、Gemini 模型前缀自动剥离与 JSON 提取器验证
            ping = hub.test_provider({"provider_id": "deepseek"})
            self.assertTrue(ping["ok"])
            self.assertEqual(ping["reply_preview"], "OK:deepseek-chat")

            hub.save_provider({"provider_id": "gemini", "api_key": "AIzaSyDummyGeminiKey123456"})
            fetched = hub.fetch_models({"provider_id": "gemini"})
            self.assertEqual(fetched["models"], ["gemini-2.5-flash", "gemini-2.5-pro"])

            parsed_json = extract_json_payload("输出结果如下：\n```json\n[{\"cn\":\"客户号\",\"en\":\"cust_no\"}]\n```")
            self.assertEqual(parsed_json[0]["en"], "cust_no")

            # 5. 验证零第三方 SDK 的原生标准库 HTTP 客户端真实回环通信（含 Bearer 鉴权、模型拉取与 HTTP 错误提取）
            import http.server
            import threading

            received_auth: List[str] = []

            class _MockOpenAIHandler(http.server.BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                    return

                def do_GET(self) -> None:  # noqa: N802
                    received_auth.append(self.headers.get("Authorization", ""))
                    if self.path.endswith("/models"):
                        payload = {"data": [{"id": "models/gemini-2.5-flash"}, {"id": "deepseek-chat"}]}
                        raw = json.dumps(payload).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    self.send_response(404)
                    self.end_headers()

                def do_POST(self) -> None:  # noqa: N802
                    received_auth.append(self.headers.get("Authorization", ""))
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    req_json = json.loads(self.rfile.read(length).decode("utf-8"))
                    if req_json.get("model") == "bad-model":
                        err_bytes = json.dumps({"error": {"message": "Model not found"}}).encode("utf-8")
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err_bytes)))
                        self.end_headers()
                        self.wfile.write(err_bytes)
                        return
                    resp_bytes = json.dumps(
                        {"choices": [{"message": {"content": f"STDLIB_OK:{req_json.get('model')}"}}]}
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp_bytes)))
                    self.end_headers()
                    self.wfile.write(resp_bytes)

            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
            port = httpd.server_address[1]
            srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            srv_thread.start()
            try:
                stdlib_hub = LLMHub(store)
                stdlib_hub.save_provider(
                    {
                        "provider_id": "local_gw",
                        "name": "本地测试网关",
                        "provider_type": "GEMINI",
                        "base_url": f"http://127.0.0.1:{port}/v1",
                        "api_key": "sk-stdlib-token-777",
                        "default_model": "gemini-2.5-flash",
                        "enabled": True,
                    }
                )
                ping_std = stdlib_hub.test_provider({"provider_id": "local_gw"})
                self.assertEqual(ping_std["reply_preview"], "STDLIB_OK:gemini-2.5-flash")
                models_std = stdlib_hub.fetch_models({"provider_id": "local_gw"})
                self.assertEqual(models_std["models"], ["gemini-2.5-flash", "deepseek-chat"])
                self.assertTrue(all(a == "Bearer sk-stdlib-token-777" for a in received_auth))
                with self.assertRaises(RuntimeError) as err_ctx:
                    stdlib_hub.chat_completion(
                        provider_id="local_gw",
                        model="bad-model",
                        messages=[{"role": "user", "content": "hi"}],
                    )
                self.assertIn("Model not found", str(err_ctx.exception))
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_readonly_sql_guard_redteam_matrix_and_false_positive_immunity(self) -> None:
        """红队验证：双模态词法/语法只读沙箱拦截一切写/DDL/锁/多语句/转义偷渡/危险函数，且合法查询零误伤."""
        blocked_cases = [
            # 1. 多语句堆叠与注释伪装
            "SELECT 1; DROP TABLE t_users",
            "SELECT 1; SELECT 2",
            "SELECT 1; /* comment */ DELETE FROM t_users",
            # 2. 反斜杠转义歧义偷渡（ANSI vs MySQL/PG 非标转义）
            r"SELECT 'abc\'; DROP TABLE t_users; --'",
            # 3. MySQL 条件可执行注释与未闭合注释/Dollar-Quoting
            "SELECT /*!50000 1 */",
            "SELECT /*! DROP TABLE t_users */ 1",
            "SELECT /* outer /* inner */ 1",
            "SELECT $$unclosed dollar literal",
            # 4. 数据修改型 CTE (Data-Modifying CTE)
            "WITH del AS (DELETE FROM t_users RETURNING *) SELECT * FROM del",
            "WITH s AS (SELECT 1), u AS (UPDATE t_users SET a = 1 RETURNING *) SELECT * FROM u",
            # 5. SELECT INTO 隐式建表与文件导出
            "SELECT * INTO new_backup_tbl FROM t_users",
            "SELECT id, name FROM t_users INTO OUTFILE '/tmp/leak.csv'",
            "SELECT id FROM t_users INTO DUMPFILE '/tmp/leak.bin'",
            # 6. 行级锁与表锁
            "SELECT * FROM t_users FOR UPDATE",
            "SELECT * FROM t_users FOR SHARE",
            "SELECT * FROM t_users LOCK IN SHARE MODE",
            # 7. EXPLAIN ANALYZE 真实执行副作用
            "EXPLAIN ANALYZE SELECT * FROM t_users",
            "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM t_users",
            # 8. 顶层写/DDL/DCL/TCL/会话修改语句
            "DELETE FROM t_users WHERE id = 1",
            "UPDATE t_users SET status = 'X'",
            "INSERT INTO t_users(id) VALUES (1)",
            "MERGE INTO t_users t USING src s ON (t.id = s.id) WHEN MATCHED THEN DELETE",
            "ALTER TABLE t_users ADD COLUMN hacked INT",
            "TRUNCATE TABLE t_users",
            "DROP VIEW v_users",
            "GRANT SELECT ON t_users TO public",
            "SET work_mem = '2GB'",
            "PRAGMA writable_schema = ON",
            "VACUUM",
            "CALL sp_refresh_data()",
            "SHOW GRANTS FOR root",
            # 9. 跨方言危险控制/系统文件/外部扩展函数（含双引号包裹绕过尝试）
            "SELECT pg_terminate_backend(1024)",
            'SELECT "pg_sleep"(5)',
            "SELECT pg_catalog.pg_read_file('/etc/passwd')",
            "SELECT dblink_exec('host=10.0.0.1', 'DROP TABLE t')",
            "SELECT lo_import('/etc/passwd')",
            "SELECT load_file('/etc/passwd')",
            "SELECT load_extension('evil_ext')",
            "SELECT DBMS_LOCK.SLEEP(3) FROM dual",
            "SELECT UTL_HTTP.REQUEST('http://127.0.0.1') FROM dual",
        ]
        for sql in blocked_cases:
            with self.subTest(blocked_sql=sql):
                with self.assertRaises(ReadOnlyViolationError, msg=f"未能拦截危险 SQL: {sql}"):
                    validate_readonly_sql(sql, "PGSQL")

        allowed_cases = [
            "SELECT REPLACE(phone, '-', '') AS clean_phone, TRUNCATE(pay_amt, 2) AS amt FROM dwd_order;",
            "SELECT update_tm, create_dt, \"delete\", 'DROP TABLE fake; --' AS note FROM dwd_order; -- end ; note",
            "SELECT CAST(cust_name AS CHAR CHARACTER SET utf8mb4) FROM dwd_order",
            (
                "WITH base AS (SELECT cust_id, pay_amt FROM dwd_order WHERE pay_amt IS NOT NULL), "
                "ranked AS (SELECT cust_id, pay_amt, ROW_NUMBER() OVER (PARTITION BY cust_id ORDER BY pay_amt DESC) AS rn FROM base) "
                "SELECT cust_id, pay_amt FROM ranked WHERE rn = 1;"
            ),
            "SHOW TABLES;",
            "SHOW CREATE TABLE dwd_order;",
            "SHOW FULL COLUMNS FROM dwd_order",
            "DESCRIBE dwd_order",
            "DESC dwd_order",
            "EXPLAIN SELECT * FROM dwd_order WHERE cust_id = 'C01'",
            "SELECT payload #>> '{profile,tier}' AS tier FROM ods_event_log",
        ]
        for sql in allowed_cases:
            with self.subTest(allowed_sql=sql):
                cleaned = validate_readonly_sql(sql, "PGSQL")
                self.assertTrue(len(cleaned) > 0)
                self.assertFalse(cleaned.rstrip().endswith(";"))

    def test_hardware_readonly_session_unconditional_rollback_and_bounded_sampling(self) -> None:
        """验证四道防线联动：SQLite URI mode=ro + query_only 硬件拒写、JDBC/PG/MySQL 只读模式与退出无条件回滚."""
        with tempfile.TemporaryDirectory() as tmpdir:
            biz_sqlite = os.path.join(tmpdir, "ro_guard.sqlite")
            with contextlib.closing(sqlite3.connect(biz_sqlite)) as conn:
                conn.execute("CREATE TABLE t_ledger (id INTEGER PRIMARY KEY, amt NUMERIC)")
                conn.executemany("INSERT INTO t_ledger (id, amt) VALUES (?, ?)", [(i, i * 10) for i in range(1, 251)])
                conn.commit()

            ctx = PlatformContext(db_path=os.path.join(tmpdir, "dataog_state.db"))
            prof = ctx.connection_hub.save_profile(
                {
                    "name": "只读账本库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": biz_sqlite,
                }
            )

            # 1. 即使绕过 SQL 校验器直接在 readonly_session 底层连接执行写语句，SQLite 引擎级 mode=ro 也会直接抛错阻断
            with ctx.connection_hub.readonly_session(prof.connection_id, timeout_sec=8) as ro_conn:
                q_only = ro_conn.execute("PRAGMA query_only").fetchone()[0]
                self.assertEqual(int(q_only), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    ro_conn.execute("DELETE FROM t_ledger WHERE id = 1")
                with self.assertRaises(sqlite3.OperationalError):
                    ro_conn.execute("CREATE TABLE hacked_tbl (x INT)")

            # 核对底层 250 行数据 100% 完好无损
            with contextlib.closing(sqlite3.connect(biz_sqlite)) as check_conn:
                cnt = check_conn.execute("SELECT COUNT(*) FROM t_ledger").fetchone()[0]
                self.assertEqual(cnt, 250)

            # 2. QueryEngine.execute_readonly_sql 行数熔断（limit=999 自动钳制到 MAX_EXPLORER_SAMPLE_LIMIT=200）
            sampled = ctx.query_engine.execute_readonly_sql(
                prof.connection_id,
                "SELECT id, amt FROM t_ledger ORDER BY id;",
                limit=999,
                timeout_sec=8,
            )
            self.assertEqual(sampled.row_count, 200)
            self.assertTrue(sampled.truncated)

            # 拦截写 SQL
            with self.assertRaises(ReadOnlyViolationError):
                ctx.query_engine.execute_readonly_sql(prof.connection_id, "DELETE FROM t_ledger")

            # 3. 验证 JDBC 只读会话：开启 setAutoCommit(False) + setReadOnly(True)，退出时无条件 rollback 且零 commit
            raw_jdbc = mock.MagicMock()
            jdbc_adapter = JdbcConnectionAdapter(raw_jdbc)
            jdbc_adapter.set_readonly_mode(True)
            raw_jdbc.setAutoCommit.assert_called_once_with(False)
            raw_jdbc.setReadOnly.assert_called_once_with(True)

            jdbc_prof = ctx.connection_hub.save_profile(
                {
                    "name": "模拟JDBC只读库",
                    "driver_track": "JDBC",
                    "db_type": "ORACLE",
                    "jdbc_url": "jdbc:oracle:thin:@127.0.0.1:1521:orcl",
                }
            )
            with mock.patch.object(ctx.driver_manager, "open_connection", return_value=jdbc_adapter) as m_open:
                with ctx.connection_hub.readonly_session(jdbc_prof.connection_id, timeout_sec=10):
                    pass
                m_open.assert_called_once()
                self.assertTrue(m_open.call_args.kwargs.get("readonly"))
            raw_jdbc.rollback.assert_called_once()
            raw_jdbc.commit.assert_not_called()

            # 4. 验证 PostgreSQL / MySQL 驱动打开 readonly=True 连接时下发会话级硬件只读指令
            pg_prof = ctx.connection_hub.save_profile(
                {
                    "name": "模拟DWS库",
                    "driver_track": "NATIVE",
                    "db_type": "DWS",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "dbname": "dws",
                    "user": "ro_user",
                }
            )
            fake_pg_conn = mock.MagicMock()
            with mock.patch("psycopg2.connect", return_value=fake_pg_conn):
                ctx.driver_manager.open_connection(pg_prof, timeout_sec=9, readonly=True)
            fake_pg_conn.set_session.assert_called_once_with(readonly=True, autocommit=False)

            mysql_prof = ctx.connection_hub.save_profile(
                {
                    "name": "模拟MySQL库",
                    "driver_track": "NATIVE",
                    "db_type": "MYSQL",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "dbname": "app",
                    "user": "ro_user",
                }
            )
            fake_my_conn = mock.MagicMock()
            fake_my_cur = mock.MagicMock()
            fake_my_conn.cursor.return_value.__enter__.return_value = fake_my_cur
            with mock.patch("pymysql.connect", return_value=fake_my_conn):
                ctx.driver_manager.open_connection(mysql_prof, timeout_sec=9, readonly=True)
            executed_sqls = [c.args[0] for c in fake_my_cur.execute.call_args_list]
            self.assertIn("SET SESSION TRANSACTION READ ONLY", executed_sqls)
            self.assertIn("START TRANSACTION READ ONLY", executed_sqls)

    def test_standard_catalog_bohai_46col_import_upsert_formats_and_compliance(self) -> None:
        """验证 StandardCatalog 46 列渤海模板导入（右侧 DWS 列优先）、Upsert/Replace、多库格式解析与分级落标核验."""
        from core.platform.standard_catalog import BOHAI_DICT_HEADERS, StandardCatalog

        def _make_46col_xlsx(rows_data: List[List[str]]) -> bytes:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "数据字典"
            ws.append(list(BOHAI_DICT_HEADERS))
            for r in rows_data:
                ws.append(r)
            out = io.BytesIO()
            wb.save(out)
            return out.getvalue()

        r1 = [""] * 46
        r1[0] = "XTPO105525"
        r1[1] = "业务数据"
        r1[5] = "新凭证冠字号码"
        r1[7] = "NEW_PREFIX"
        r1[8] = "NEW_PREFIX"
        r1[19] = "文本类10(nvarchar)"
        r1[21] = "VARCHAR(10)"
        r1[22] = "VARCHAR(40)"
        r1[23] = "/"  # 同名旧 DWS 格式列为 '/'，必须取右侧第 45 列
        r1[24] = "VARCHAR2(40)"
        r1[29] = "1"
        r1[36] = "文本类"
        r1[41] = "VARCHAR(10)"  # argodb
        r1[43] = "VARCHAR2(50)"  # 数据仓库 集市
        r1[44] = "NVARCHAR2(50)"  # 第 45 列 仓库DWS数据格式

        r2 = [""] * 46
        r2[0] = "RDAM000426"
        r2[1] = "业务数据"
        r2[5] = "零售客户折人民币存款年日均金额"
        r2[7] = "Retail Customer Year Avg Amt"
        r2[8] = "RTL_CUST_CNY_DPST_Y_AVG_AMT"
        r2[21] = "DECIMAL(18,2)"
        r2[22] = "DECIMAL(18,2)"
        r2[29] = "1"
        r2[36] = "金额类"
        r2[41] = "DECIMAL(18,2)"
        r2[43] = "DECIMAL(18,2)"
        r2[44] = "DECIMAL(18,2)"

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = PlatformContext(db_path=os.path.join(tmpdir, "dataog_state.db"))
            cat = ctx.standard_catalog

            # 1. 导入 46 列标准字典（同时在文件中间混入重复表头行测试去噪）
            dup_hdr = list(BOHAI_DICT_HEADERS)
            xlsx_bytes = _make_46col_xlsx([r1, dup_hdr, r2])
            res_up = cat.import_dictionary_excel("bohai_dict.xlsx", xlsx_bytes, mode="upsert")
            self.assertEqual(res_up["imported_count"], 2)

            it1 = cat.get_item("XTPO105525")
            self.assertIsNotNone(it1)
            self.assertEqual(it1["type_dws"], "NVARCHAR2(50)")
            self.assertEqual(it1["type_dw_mart"], "VARCHAR2(50)")
            self.assertEqual(it1["type_argodb"], "VARCHAR(10)")

            # 2. 多库物理格式自动路由与手动切换
            self.assertEqual(cat.resolve_physical_type(it1, db_type="DWS"), "NVARCHAR2(50)")
            self.assertEqual(cat.resolve_physical_type(it1, db_type="INCEPTOR"), "VARCHAR(10)")
            self.assertEqual(cat.resolve_physical_type(it1, db_type="MYSQL"), "VARCHAR(10)")
            self.assertEqual(cat.resolve_physical_type(it1, db_type="ORACLE"), "VARCHAR(40)")
            self.assertEqual(cat.resolve_physical_type(it1, db_type="DM8"), "VARCHAR2(40)")
            self.assertEqual(
                cat.resolve_physical_type(it1, db_type="DWS", format_profile="type_dw_mart"),
                "VARCHAR2(50)",
            )

            # 3. 三维分级落标核验 (COMPLIANT / COMMENT_ONLY / NON_COMPLIANT / UNBOUND / STD_MISSING)
            ev_ok = StandardCatalog.evaluate_column_compliance(
                col_name="new_prefix",
                col_type="VARCHAR(50)",  # 与 NVARCHAR2(50) 归一化相等
                col_comment="新凭证冠字号码（含补充说明）",
                std_item=it1,
                std_code="XTPO105525",
                db_type="DWS",
            )
            self.assertEqual(ev_ok["status"], "COMPLIANT")

            ev_comm = StandardCatalog.evaluate_column_compliance(
                col_name="NEW_PREFIX",
                col_type="NVARCHAR2(50)",
                col_comment="冠字号",  # 缺少标准中文名
                std_item=it1,
                std_code="XTPO105525",
                db_type="DWS",
            )
            self.assertEqual(ev_comm["status"], "COMMENT_ONLY")

            ev_bad = StandardCatalog.evaluate_column_compliance(
                col_name="old_prefix",
                col_type="VARCHAR(20)",
                col_comment="新凭证冠字号码",
                std_item=it1,
                std_code="XTPO105525",
                db_type="DWS",
            )
            self.assertEqual(ev_bad["status"], "NON_COMPLIANT")

            self.assertEqual(
                StandardCatalog.evaluate_column_compliance(
                    col_name="x", col_type="INT", col_comment="", std_item=None, std_code=""
                )["status"],
                "UNBOUND",
            )
            self.assertEqual(
                StandardCatalog.evaluate_column_compliance(
                    col_name="x", col_type="INT", col_comment="", std_item=None, std_code="NO_SUCH"
                )["status"],
                "STD_MISSING",
            )

            # 4. 绑定字段后执行 replace 模式重新导入与修改 std_code 级联更新
            cat.upsert_field_bindings(
                "conn_demo",
                [{"schema_name": "public", "table_name": "t_vchr", "column_name": "new_prefix", "std_code": "XTPO105525"}],
            )
            cat.import_dictionary_excel("bohai_v2.xlsx", xlsx_bytes, mode="replace")
            binds_after_replace = cat.get_table_field_bindings("conn_demo", "public", "t_vchr")
            self.assertEqual(binds_after_replace["new_prefix"]["std_code"], "XTPO105525")

            cat.update_item("XTPO105525", {**it1, "std_code": "XTPO105525_V2"})
            binds_after_rename = cat.get_table_field_bindings("conn_demo", "public", "t_vchr")
            self.assertEqual(binds_after_rename["new_prefix"]["std_code"], "XTPO105525_V2")

            # 5. 验证 46 列系统模板生成与导出往返无损
            tpl_bytes = cat.generate_dictionary_template_bytes()
            tpl_parsed = cat.parse_standard_dictionary_excel("tpl.xlsx", tpl_bytes)
            self.assertGreaterEqual(len(tpl_parsed), 4)

            exp_bytes = cat.export_dictionary_excel_bytes()
            exp_parsed = cat.parse_standard_dictionary_excel("exp.xlsx", exp_bytes)
            self.assertEqual({x["std_code"] for x in exp_parsed}, {"XTPO105525_V2", "RDAM000426"})

    def test_excel_formula_resolution_na_cleaning_and_import_progress_hub(self) -> None:
        """验证 Excel 公式最终结果优先（含缓存与未缓存求值）、#N/A 全量错误值识别置空与 ImportProgressHub."""
        from core.excel_formula import (
            ResolvedWorkbook,
            clean_excel_cell_str,
            is_excel_error_value,
        )
        from core.platform.import_progress import ImportProgressHub
        from core.platform.standard_catalog import BOHAI_DICT_HEADERS

        # 1. 全谱系 Excel 错误值与空占位符清洗
        for err_tok in (
            "#N/A",
            "#n/a",
            "  #N/A  ",
            "'#N/A",
            "＃Ｎ／Ａ",
            "#NA",
            "#N/A!",
            "#VALUE!",
            "#REF!",
            "#DIV/0!",
            "#NUM!",
            "#NAME?",
            "#NULL!",
            "#SPILL!",
            "#CALC!",
            "#GETTING_DATA",
        ):
            self.assertTrue(is_excel_error_value(err_tok), f"未识别错误标记: {err_tok!r}")
            self.assertEqual(clean_excel_cell_str(err_tok), "")
            self.assertEqual(clean_excel_cell_str(err_tok, treat_placeholder_empty=True), "")

        self.assertEqual(clean_excel_cell_str(100101.0), "100101")
        self.assertEqual(clean_excel_cell_str(float("nan")), "")
        self.assertEqual(clean_excel_cell_str("0"), "0")
        self.assertEqual(clean_excel_cell_str("=1:有效"), "=1:有效")
        self.assertEqual(clean_excel_cell_str("/", treat_placeholder_empty=True), "")
        self.assertEqual(clean_excel_cell_str("N/A", treat_placeholder_empty=True), "")
        self.assertEqual(clean_excel_cell_str("/", treat_placeholder_empty=False), "/")

        # 2. 验证 OOXML 既有缓存值 (<f>+<v>) 优先采用最终计算值，且 t="e" 的 #N/A 正确转为空
        wb_base = openpyxl.Workbook()
        buf_base = io.BytesIO()
        wb_base.save(buf_base)
        patched_buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(buf_base.getvalue()), "r") as zf_in, zipfile.ZipFile(
            patched_buf, "w"
        ) as zf_out:
            for info in zf_in.infolist():
                raw = zf_in.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    raw = (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                        "<sheetData>"
                        '<row r="1">'
                        '<c r="A1" t="str"><f>CUSTOM_EXCEL_UDF(Z99)</f><v>CACHED_FINAL_CODE</v></c>'
                        '<c r="B1" t="e"><f>VLOOKUP("x",D:E,2,0)</f><v>#N/A</v></c>'
                        '<c r="C1" t="str"><f>IF(1=1,"","x")</f><v/></c>'
                        "</row>"
                        "</sheetData></worksheet>"
                    ).encode("utf-8")
                zf_out.writestr(info, raw)

        with ResolvedWorkbook(patched_buf.getvalue()) as rwb_cached:
            row0 = list(rwb_cached.iter_sheet_rows(rwb_cached.sheetnames[0]))[0]
            self.assertEqual(clean_excel_cell_str(row0[0]), "CACHED_FINAL_CODE")
            self.assertEqual(clean_excel_cell_str(row0[1]), "")
            self.assertEqual(clean_excel_cell_str(row0[2]), "")

        # 3. 验证无缓存公式工作簿（VLOOKUP / INDEX+MATCH / _xlfn.XLOOKUP / IFERROR / @整列交集 / 循环引用保护）
        wb_f = openpyxl.Workbook()
        ws_dict = wb_f.active
        ws_dict.title = "数据字典"
        ws_dict.append(list(BOHAI_DICT_HEADERS))

        row_valid = [""] * 46
        row_valid[0] = '=CONCATENATE("STD", TEXT(101, "0000"))'  # -> STD0101
        row_valid[1] = "#N/A"  # 监管分类为 #N/A 应置空
        row_valid[5] = '=VLOOKUP("STD0101", 参数表!A:B, 2, FALSE())'  # -> 客户风险等级
        row_valid[8] = '=UPPER(TRIM("  cust_risk_lvl  "))'  # -> CUST_RISK_LVL
        row_valid[12] = "=16+16"  # 最大录入长度 -> 32
        row_valid[23] = "VARCHAR2(32)"  # 第 24 列遗留 DWS 格式
        row_valid[44] = '=VLOOKUP("NO_MATCH", 参数表!A:B, 2, 0)'  # 第 45 列为 #N/A -> 自动回退第 24 列 VARCHAR2(32)
        row_valid[45] = '=IFERROR(1/0, "#N/A")'  # 备注为 #N/A -> 置空
        ws_dict.append(row_valid)

        # 编号本身求值为 #N/A 的无效行应被自动忽略跳过
        row_na_code = [""] * 46
        row_na_code[0] = '=VLOOKUP("MISSING", 参数表!A:B, 2, 0)'
        row_na_code[5] = "无效行"
        row_na_code[8] = "INVALID_COL"
        ws_dict.append(row_na_code)

        # 自引用循环公式保护（不崩溃且置空）
        row_cycle = [""] * 46
        row_cycle[0] = "STD0202"
        row_cycle[5] = "循环测试标准"
        row_cycle[8] = '=@I:I'  # 自引用循环 -> #REF! -> 置空后回退 en_name
        row_cycle[7] = "CYCLE_SAFE_COL"
        row_cycle[44] = '="VARCHAR(" & 64 & ")"'
        ws_dict.append(row_cycle)

        ws_ref = wb_f.create_sheet("参数表")
        ws_ref.append(["键", "值"])
        ws_ref.append(["STD0101", "客户风险等级"])

        buf_f = io.BytesIO()
        wb_f.save(buf_f)

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = PlatformContext(db_path=os.path.join(tmpdir, "dataog_state.db"))
            hub = ImportProgressHub(max_entries=16, ttl_seconds=60.0)
            pid = "imp_unit_dict_01"
            hub.start(pid, "导入字典测试")
            res = ctx.standard_catalog.import_dictionary_excel(
                "formula_dict.xlsx",
                buf_f.getvalue(),
                mode="upsert",
                progress_cb=hub.make_reporter(pid),
            )
            hub.finish(pid, detail="完成", processed=res["imported_count"], total=res["imported_count"])
            snap = hub.get(pid)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["status"], "completed")
            self.assertEqual(snap["percent"], 100.0)
            self.assertIsNone(hub.get("../bad_id"))

            self.assertEqual(res["imported_count"], 2)
            s1 = ctx.standard_catalog.get_item("STD0101")
            self.assertIsNotNone(s1)
            self.assertEqual(s1["reg_category"], "")
            self.assertEqual(s1["cn_name"], "客户风险等级")
            self.assertEqual(s1["en_abbr"], "CUST_RISK_LVL")
            self.assertEqual(s1["max_length"], "32")
            self.assertEqual(s1["type_dws"], "VARCHAR2(32)")
            self.assertEqual(s1["remark"], "")

            s2 = ctx.standard_catalog.get_item("STD0202")
            self.assertIsNotNone(s2)
            self.assertEqual(s2["en_abbr"], "CYCLE_SAFE_COL")
            self.assertEqual(s2["type_dws"], "VARCHAR(64)")

        # 4. 验证单库贯标改造清单 parse_excel_checklist 同样支持公式计算最终值且 #N/A 自动回退原字段
        from core.excel_handler import parse_excel_checklist

        wb_cl = openpyxl.Workbook()
        ws_cl = wb_cl.active
        ws_cl.append(["模式名", "表名", "修改前字段名", "修改前类型", "修改后字段名", "修改后类型", "修改后注释"])
        ws_cl.append(
            [
                "public",
                "dwd_loan",
                "loan_no",
                "varchar(32)",
                '=UPPER("party_loan_id")',
                '="VARCHAR(" & (32*2) & ")"',
                "#N/A",
            ]
        )
        ws_cl.append(
            [
                "public",
                "dwd_loan",
                "keep_col",
                "date",
                "#N/A",  # 修改后字段名为 #N/A 时应识别为空并自动回退 keep_col
                "#N/A",
                '=IF(1=1, "贷款起息日", "ERR")',
            ]
        )
        buf_cl = io.BytesIO()
        wb_cl.save(buf_cl)
        cl_items = parse_excel_checklist(buf_cl.getvalue(), "cl_formula.xlsx")
        self.assertEqual(len(cl_items), 2)
        self.assertEqual(cl_items[0]["new_col_name"], "PARTY_LOAN_ID")
        self.assertEqual(cl_items[0]["new_col_type"], "VARCHAR(64)")
        self.assertEqual(cl_items[0]["new_col_comment"], "")
        self.assertEqual(cl_items[1]["new_col_name"], "keep_col")
        self.assertEqual(cl_items[1]["new_col_type"], "date")
        self.assertEqual(cl_items[1]["new_col_comment"], "贷款起息日")

        # 5. 回归验证：
        #    a) OOXML 显式缓存空串 <c t="str"><f>...</f><v/></c> 绝不误判为未缓存，且外部工作簿 [1]Sheet1! 绝不误劫持当前同名 Sheet1；
        #    b) 拖拽共享公式 <f t="shared" si="0" ref="C2:C4">、DataTableFormula 不泄漏对象 repr；
        #    c) 名称管理器 <definedNames>、超级表 Table[列]/[@列] 与多条件数组 INDEX+MATCH(1,(A=x)*(B=y),0) 精准求值。
        import xml.etree.ElementTree as ET
        from openpyxl.workbook.defined_name import DefinedName
        from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula
        from openpyxl.worksheet.table import Table
        from core.excel_formula import inspect_xlsx_formula_state

        self.assertEqual(clean_excel_cell_str(DataTableFormula(ref="C3")), "")
        self.assertEqual(clean_excel_cell_str(ArrayFormula(ref="C2:C3", text="")), "")

        wb_ext = openpyxl.Workbook()
        ws_ext = wb_ext.active
        ws_ext.title = "Sheet1"
        ws_ext.append(["表名", "字段名", "数据项编号"])
        ws_ext.append(["cust_id", "cust_id", "placeholder"])
        ws_ext.append(["unmapped_col", "cust_id", "placeholder"])
        buf_ext_raw = io.BytesIO()
        wb_ext.save(buf_ext_raw)

        z_ext_out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(buf_ext_raw.getvalue()), "r") as zin, zipfile.ZipFile(
            z_ext_out, "w"
        ) as zout:
            for zinfo in zin.infolist():
                zdata = zin.read(zinfo.filename)
                if zinfo.filename == "xl/worksheets/sheet1.xml":
                    ET.register_namespace("", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
                    xroot = ET.fromstring(zdata)
                    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                    for c_el in xroot.iter(f"{ns}c"):
                        r_coord = c_el.attrib.get("r")
                        if r_coord == "C2":
                            c_el.attrib["t"] = "str"
                            for ch in list(c_el):
                                c_el.remove(ch)
                            ET.SubElement(c_el, f"{ns}f").text = 'IFNA(VLOOKUP(B2,[1]Sheet1!$A:$B,2,0),"")'
                            ET.SubElement(c_el, f"{ns}v").text = "STD_CACHED_EXT"
                        elif r_coord == "C3":
                            c_el.attrib["t"] = "str"
                            for ch in list(c_el):
                                c_el.remove(ch)
                            ET.SubElement(c_el, f"{ns}f").text = 'IFNA(VLOOKUP(B3,[1]Sheet1!$A:$B,2,0),"")'
                            ET.SubElement(c_el, f"{ns}v").text = ""
                    zdata = ET.tostring(xroot, encoding="utf-8", xml_declaration=True)
                zout.writestr(zinfo, zdata)

        ext_xlsx_bytes = z_ext_out.getvalue()
        has_uncached_flag, empty_map = inspect_xlsx_formula_state(ext_xlsx_bytes)
        self.assertFalse(has_uncached_flag)
        self.assertIn((3, 3), empty_map.get("Sheet1", set()))
        with ResolvedWorkbook(ext_xlsx_bytes) as rwb_ext:
            ext_rows = list(rwb_ext.iter_sheet_rows("Sheet1"))
            self.assertEqual(ext_rows[1][2], "STD_CACHED_EXT")
            self.assertEqual(ext_rows[2][2], "")

        # 未缓存外部工作簿公式返回 #REF!（可被 IFERROR 捕获或置空，绝不劫持当前 Sheet1）
        wb_unext = openpyxl.Workbook()
        ws_u = wb_unext.active
        ws_u.title = "Sheet1"
        ws_u.append(["cust_id", "WRONG_LOCAL_VAL", "=VLOOKUP(A1,[1]Sheet1!$A:$B,2,0)", '=IFERROR(VLOOKUP(A1,[1]Sheet1!$A:$B,2,0),"SAFE_FB")'])
        buf_unext = io.BytesIO()
        wb_unext.save(buf_unext)
        with ResolvedWorkbook(buf_unext.getvalue()) as rwb_u:
            u_row = list(rwb_u.iter_sheet_rows("Sheet1"))[0]
            self.assertEqual(clean_excel_cell_str(u_row[2]), "")
            self.assertEqual(u_row[3], "SAFE_FB")

        # 验证拖拽共享公式 + 名称管理器 + 超级表结构化引用 + 多条件 INDEX+MATCH
        wb_adv = openpyxl.Workbook()
        ws_map = wb_adv.active
        ws_map.title = "对照"
        ws_map.append(["字段", "中文", "命名区结果", "双条件编号", "超级表编号", "共享公式列"])
        ws_map.append(
            [
                "amt",
                "交易金额",
                "=VLOOKUP(A2, DictNamedRange, 2, 0)",
                "=INDEX(字典!$A$2:$A$4, MATCH(1, (字典!$B$2:$B$4=A2)*(字典!$C$2:$C$4=B2), 0))",
                "=XLOOKUP([@字段], TblDict[英文], TblDict[编号])",
                "placeholder",
            ]
        )
        ws_map.append(["open_dt", "开户日期", "", "", "=VLOOKUP([@字段], TblDict, 1, 0)", "placeholder"])
        ws_map.add_table(Table(displayName="TblMap", ref="A1:F3"))

        ws_d2 = wb_adv.create_sheet("字典")
        ws_d2.append(["编号", "英文", "中文"])
        ws_d2.append(["STD_BAL", "amt", "账户余额"])
        ws_d2.append(["STD_TXN", "amt", "交易金额"])
        ws_d2.append(["STD_DT", "open_dt", "开户日期"])
        ws_d2.add_table(Table(displayName="TblDict", ref="A1:C4"))
        wb_adv.defined_names["DictNamedRange"] = DefinedName("DictNamedRange", attr_text="字典!$B$2:$C$4")

        buf_adv_raw = io.BytesIO()
        wb_adv.save(buf_adv_raw)
        z_adv_out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(buf_adv_raw.getvalue()), "r") as zin, zipfile.ZipFile(
            z_adv_out, "w"
        ) as zout:
            for zinfo in zin.infolist():
                zdata = zin.read(zinfo.filename)
                if zinfo.filename == "xl/worksheets/sheet1.xml":
                    ET.register_namespace("", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
                    xroot = ET.fromstring(zdata)
                    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                    for c_el in xroot.iter(f"{ns}c"):
                        rc_str = c_el.attrib.get("r")
                        if rc_str in ("F2", "F3"):
                            c_el.attrib.pop("t", None)
                            for ch in list(c_el):
                                c_el.remove(ch)
                            f_el = ET.SubElement(c_el, f"{ns}f", {"t": "shared", "si": "0"})
                            if rc_str == "F2":
                                f_el.attrib["ref"] = "F2:F3"
                                f_el.text = "INDEX(字典!$A:$A, MATCH(A2, 字典!$B:$B, 0))"
                    zdata = ET.tostring(xroot, encoding="utf-8", xml_declaration=True)
                zout.writestr(zinfo, zdata)

        with ResolvedWorkbook(z_adv_out.getvalue()) as rwb_adv:
            adv_rows = list(rwb_adv.iter_sheet_rows("对照"))
            self.assertEqual(adv_rows[1][2], "账户余额")
            self.assertEqual(adv_rows[1][3], "STD_TXN")
            self.assertEqual(adv_rows[1][4], "STD_BAL")
            self.assertEqual(adv_rows[1][5], "STD_BAL")
            self.assertEqual(adv_rows[2][5], "STD_DT")

        # 验证第三方工具写入无前导 '/' 的 Target="xl/worksheets/..." / Target="xl/tables/..." 与反斜杠路径兼容，
        # 以及先 probe 后全量读取时 <dimension> 总行数保留不退化
        from core.excel_formula import _resolve_zip_rel_target

        self.assertEqual(
            _resolve_zip_rel_target("xl/workbook.xml", "xl/worksheets/sheet1.xml"),
            "xl/worksheets/sheet1.xml",
        )
        self.assertEqual(
            _resolve_zip_rel_target("xl/worksheets/sheet1.xml", "xl/tables/table1.xml"),
            "xl/tables/table1.xml",
        )
        self.assertEqual(
            _resolve_zip_rel_target("xl/workbook.xml", "worksheets\\sheet2.xml"),
            "xl/worksheets/sheet2.xml",
        )

        z_norels_slash = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(z_adv_out.getvalue()), "r") as zin, zipfile.ZipFile(
            z_norels_slash, "w"
        ) as zout:
            for zinfo in zin.infolist():
                zdata = zin.read(zinfo.filename)
                if zinfo.filename == "xl/_rels/workbook.xml.rels":
                    zdata = (
                        zdata.replace(b'Target="/xl/worksheets/sheet1.xml"', b'Target="xl/worksheets/sheet1.xml"')
                        .replace(b'Target="worksheets/sheet1.xml"', b'Target="xl/worksheets/sheet1.xml"')
                        .replace(b'Target="/xl/worksheets/sheet2.xml"', b'Target="xl\\worksheets\\sheet2.xml"')
                        .replace(b'Target="worksheets/sheet2.xml"', b'Target="xl\\worksheets\\sheet2.xml"')
                    )
                elif zinfo.filename.startswith("xl/worksheets/_rels/") and zinfo.filename.endswith(".rels"):
                    zdata = (
                        zdata.replace(b'Target="../tables/', b'Target="xl/tables/')
                        .replace(b'Target="/xl/tables/', b'Target="xl/tables/')
                    )
                zout.writestr(zinfo, zdata)

        with ResolvedWorkbook(z_norels_slash.getvalue()) as rwb_ns:
            ns_rows = list(rwb_ns.iter_sheet_rows("对照"))
            self.assertEqual(ns_rows[1][3], "STD_TXN")
            self.assertEqual(ns_rows[1][4], "STD_BAL")
            self.assertEqual(ns_rows[2][5], "STD_DT")

        wb_prog = openpyxl.Workbook()
        ws_prog = wb_prog.active
        ws_prog.title = "ProgSheet"
        for i in range(1, 86):
            ws_prog.append([f"col_{i}", i])
        buf_prog = io.BytesIO()
        wb_prog.save(buf_prog)
        cb_records: List[Tuple[int, int]] = []
        with ResolvedWorkbook(buf_prog.getvalue(), read_only_preferred=True) as rwb_p:
            _ = list(rwb_p.iter_sheet_rows("ProgSheet", max_rows=10))
            _ = rwb_p.extract_sheet_rows_with_merged(
                "ProgSheet",
                row_progress_cb=lambda d, t: cb_records.append((d, t)),
            )
        self.assertGreaterEqual(len(cb_records), 2)
        self.assertEqual(cb_records[0], (1, 85))
        self.assertEqual(cb_records[-1], (85, 85))

        # 6. 回归验证：
        #    a) 共享公式平移保护结构化表引用列名（如 [@ID1]、Table1[[#This Row],[C2]] 不被当作单元格坐标平移）及整行引用平移；
        #    b) LOOKUP(1,0/(...))、VLOOKUP+CHOOSE({1,2},...)、INDIRECT、OFFSET、$3:$3 整行引用及异常公式外包 IFERROR；
        #    c) 文本态公式触发懒求值器后 close() 彻底关闭双工作簿句柄；
        #    d) EasyExcel / write_only 写死 <dimension ref="A1:A1"/> 时准确还原真实行数。
        import re
        from core.excel_formula import _shift_shared_formula

        self.assertEqual(
            _shift_shared_formula("VLOOKUP([@ID1], Dict!$A$2:$B$10, 2, 0)", 3, 0),
            "VLOOKUP([@ID1], Dict!$A$2:$B$10, 2, 0)",
        )
        self.assertEqual(
            _shift_shared_formula("Table1[[#This Row],[C2]]", 2, 1),
            "Table1[[#This Row],[C2]]",
        )
        self.assertEqual(
            _shift_shared_formula("INDEX(2:2, 1, B1)", 3, 0),
            "INDEX(5:5, 1, B4)",
        )

        wb_idiom = openpyxl.Workbook()
        ws_id1 = wb_idiom.active
        ws_id1.title = "Sheet1"
        ws_id1.append(["tbl", "col", "f_lookup", "f_choose", "f_indirect", "f_offset", "f_rowref", "f_bad", "f_iferr"])
        ws_id1.append(
            [
                "t_cust",
                "cust_no",
                "=LOOKUP(1, 0/((Sheet2!A2:A4=A2)*(Sheet2!B2:B4=B2)), Sheet2!C2:C4)",
                '=VLOOKUP(A2&"|"&B2, CHOOSE({1,2}, Sheet2!A2:A4&"|"&Sheet2!B2:B4, Sheet2!C2:C4), 2, 0)',
                '=INDIRECT("Sheet2!C3")',
                "=OFFSET(Sheet2!A1, 2, 2)",
                "=INDEX(Sheet2!$3:$3, 1, 3)",
                "=1 @@@ 2",
                '=IFERROR(H2, "SAFE_FALLBACK")',
            ]
        )
        ws_id2 = wb_idiom.create_sheet("Sheet2")
        ws_id2.append(["tbl", "col", "std_code"])
        ws_id2.append(["t_acct", "cust_no", "WRONG_1"])
        ws_id2.append(["t_cust", "cust_no", "STD_CUST_01"])
        ws_id2.append(["t_loan", "cust_no", "WRONG_2"])
        buf_idiom = io.BytesIO()
        wb_idiom.save(buf_idiom)
        with ResolvedWorkbook(buf_idiom.getvalue()) as rwb_id:
            id_rows = list(rwb_id.iter_sheet_rows("Sheet1"))
            self.assertEqual(id_rows[1][2:7], ["STD_CUST_01"] * 5)
            self.assertEqual(clean_excel_cell_str(id_rows[1][7]), "")
            self.assertEqual(id_rows[1][8], "SAFE_FALLBACK")

        wb_txt = openpyxl.Workbook()
        ws_txt = wb_txt.active
        ws_txt.title = "TextFml"
        ws_txt.append(["raw", "fml"])
        ws_txt.cell(row=2, column=1, value="abc")
        c_tf = ws_txt.cell(row=2, column=2, value="=UPPER(A2)")
        c_tf.data_type = "s"
        buf_txt = io.BytesIO()
        wb_txt.save(buf_txt)
        rwb_tf = ResolvedWorkbook(buf_txt.getvalue())
        tf_rows = list(rwb_tf.iter_sheet_rows("TextFml"))
        self.assertEqual(tf_rows[1], ["abc", "ABC"])
        primary_wb = rwb_tf._wb
        eval_wb = rwb_tf._evaluator._wb_val
        rwb_tf.close()
        self.assertIsNone(primary_wb._archive.fp)
        self.assertIsNone(eval_wb._archive.fp)

        wb_stream = openpyxl.Workbook(write_only=True)
        ws_st = wb_stream.create_sheet("EasySheet")
        for r_i in range(1, 211):
            ws_st.append([f"k_{r_i}", r_i])
        buf_st = io.BytesIO()
        wb_stream.save(buf_st)
        buf_st_fixed_dim = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(buf_st.getvalue()), "r") as zin, zipfile.ZipFile(
            buf_st_fixed_dim, "w"
        ) as zout:
            for zinfo in zin.infolist():
                zdata = zin.read(zinfo.filename)
                if zinfo.filename == "xl/worksheets/sheet1.xml":
                    zdata = re.sub(rb'<dimension\s+ref="[^"]*"\s*/>', b'<dimension ref="A1:A1"/>', zdata)
                    if b"<dimension" not in zdata:
                        zdata = zdata.replace(b"<sheetData>", b'<dimension ref="A1:A1"/><sheetData>', 1)
                zout.writestr(zinfo, zdata)
        with ResolvedWorkbook(buf_st_fixed_dim.getvalue()) as rwb_st:
            self.assertEqual(rwb_st.estimate_sheet_rows("EasySheet"), 210)

    def test_bulk_binding_btree_bounds_batch_metadata_probe_and_running_progress_ttl(self) -> None:
        """验证字段绑定 B-Tree 精确前缀隔离（防 '_' 通配符误伤）、PG/DWS/MySQL 单会话批量元数据探查及运行态进度 TTL 保护."""
        import logging
        from app import _QuietImportProgressAccessFilter
        from core.platform.import_progress import ImportProgressHub

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = PlatformContext(db_path=os.path.join(tmpdir, "dataog_state.db"))
            cat = ctx.standard_catalog
            cat.upsert_item(
                {
                    "std_code": "STD_W1",
                    "cn_name": "客户号",
                    "en_abbr": "CUST_ID",
                    "type_dws": "VARCHAR(32)",
                }
            )
            cat.upsert_item(
                {
                    "std_code": "STD_W2",
                    "cn_name": "账户号",
                    "en_abbr": "ACCT_ID",
                    "type_dws": "VARCHAR(32)",
                }
            )

            # 1. 验证下划线表名 dwd_a_1 在按表覆盖或单表查询时绝不误伤 dwdxa_1，且重复字段以最后一行准
            cat.bulk_save_field_bindings(
                "conn_wild",
                [
                    {"schema_name": "public", "table_name": "dwdxa_1", "column_name": "c1", "std_code": "STD_W2"},
                    {"schema_name": "public", "table_name": "dwd_a_1", "column_name": "c1", "std_code": "STD_W1"},
                    {"schema_name": "public", "table_name": "dwd_a_1", "column_name": "c1", "std_code": "STD_W2"},
                ],
                mode="merge",
            )
            self.assertEqual(cat.get_table_field_bindings("conn_wild", "public", "dwd_a_1")["c1"]["std_code"], "STD_W2")
            self.assertEqual(cat.get_table_field_bindings("conn_wild", "public", "dwdxa_1")["c1"]["std_code"], "STD_W2")

            cat.replace_table_field_bindings(
                "conn_wild",
                "public",
                "dwd_a_1",
                [{"schema_name": "public", "table_name": "dwd_a_1", "column_name": "c_new", "std_code": "STD_W1"}],
            )
            self.assertEqual(set(cat.get_table_field_bindings("conn_wild", "public", "dwd_a_1").keys()), {"c_new"})
            self.assertEqual(cat.get_table_field_bindings("conn_wild", "public", "dwdxa_1")["c1"]["std_code"], "STD_W2")

            # 按表覆盖传入空绑定列表时应清空目标表旧绑定而不影响其他表
            cat.bulk_save_field_bindings(
                "conn_wild",
                [],
                mode="replace_table",
                replace_tables=[("public", "dwd_a_1")],
            )
            self.assertEqual(cat.get_table_field_bindings("conn_wild", "public", "dwd_a_1"), {})
            self.assertEqual(cat.get_table_field_bindings("conn_wild", "public", "dwdxa_1")["c1"]["std_code"], "STD_W2")
            self.assertEqual(cat.bulk_save_field_bindings("", []), 0)
            self.assertEqual(cat.get_tables_field_bindings_batch("", [("public", "dwdxa_1")]), {})

            # 大小写不一致的标准编号批量查询回退验证
            ci_items = cat.get_items_by_codes(["std_w1", "NO_SUCH_CODE"])
            self.assertIn("std_w1", ci_items)
            self.assertEqual(ci_items["std_w1"]["std_code"], "STD_W1")

            # 2. 验证模拟 DWS 与 MySQL 单连接批量探查路径（含同名跨 Schema 表、不存在表及缓存落盘）
            dws_prof = ctx.connection_hub.save_profile(
                {
                    "name": "mock_dws",
                    "driver_track": "NATIVE",
                    "db_type": "DWS",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "dbname": "dw",
                    "user": "u",
                    "password": "p",
                }
            )
            mysql_prof = ctx.connection_hub.save_profile(
                {
                    "name": "mock_mysql",
                    "driver_track": "NATIVE",
                    "db_type": "MYSQL",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "dbname": "ods",
                    "user": "u",
                    "password": "p",
                }
            )

            class _FakeDwsCursor:
                def __init__(self) -> None:
                    self._rows: list = []

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def execute(self, sql: str, params=None) -> None:
                    s = " ".join(sql.split())
                    if "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n" in s:
                        self._rows = [
                            (101, "ods", "cust_info", "ODS客户表", "orientation=column", "r"),
                            (202, "dwd", "cust_info", "DWD客户表", "orientation=row", "r"),
                        ]
                    elif "FROM pg_catalog.pg_constraint con" in s:
                        self._rows = [(101, "ods_id"), (202, "cust_id")]
                    elif "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid" in s:
                        self._rows = [
                            (101, "ods_id", "character varying(32)", True, None, "源客户号"),
                            (202, "cust_id", "character varying(32)", True, None, "统一客户号"),
                        ]
                    elif "pg_get_tabledef" in s:
                        self._rows = [
                            (101, "CREATE TABLE ods.cust_info (ods_id varchar(32)) WITH (orientation=column) DISTRIBUTE BY HASH(ods_id);"),
                            (202, "CREATE TABLE dwd.cust_info (cust_id varchar(32)) DISTRIBUTE BY HASH(cust_id);"),
                        ]
                    else:
                        self._rows = []

                def fetchall(self):
                    return list(self._rows)

                def fetchone(self):
                    return self._rows[0] if self._rows else None

            class _FakeDwsConn:
                autocommit = False

                def cursor(self):
                    return _FakeDwsCursor()

                def close(self):
                    pass

            class _FakeMysqlCursor:
                def __init__(self) -> None:
                    self._rows: list = []

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def execute(self, sql: str, params=None) -> None:
                    s = " ".join(sql.split())
                    p_list = [str(x).lower() for x in (params or ())]
                    if "FROM information_schema.TABLES" in s:
                        if "ods" in p_list:
                            self._rows = [("ods", "t_order", "BASE TABLE", "订单表")]
                        else:
                            self._rows = []
                    elif "FROM information_schema.COLUMNS" in s:
                        if "SELECT TABLE_SCHEMA," in s:
                            self._rows = [(
                                "ods", "t_order", 1, "order_id", "varchar(40)", "NO", None, "订单号", "PRI"
                            )]
                        else:
                            self._rows = [(
                                1, "order_id", "varchar(40)", "NO", None, "订单号", "PRI"
                            )]
                    else:
                        self._rows = []

                def fetchall(self):
                    return list(self._rows)

                def fetchone(self):
                    return self._rows[0] if self._rows else None

            class _FakeMysqlConn:
                def cursor(self):
                    return _FakeMysqlCursor()

                def close(self):
                    pass

            self.assertEqual(mysql_prof.default_schema, "ods")

            open_calls = {"dws": 0, "mysql": 0}
            orig_open = ctx.driver_manager.open_connection

            def _mock_open(profile, timeout_sec=10, **kw):
                if profile.connection_id == dws_prof.connection_id:
                    open_calls["dws"] += 1
                    return _FakeDwsConn()
                if profile.connection_id == mysql_prof.connection_id:
                    open_calls["mysql"] += 1
                    return _FakeMysqlConn()
                return orig_open(profile, timeout_sec=timeout_sec, **kw)

            ctx.driver_manager.open_connection = _mock_open
            try:
                dws_metas, dws_warn, dws_skip = ctx.metadata_hub.get_tables_metadata_batch(
                    dws_prof.connection_id,
                    [
                        ("ods", "cust_info"),
                        ("dwd", "cust_info"),
                        ("ads", "cust_info"),
                        ("dwd", "missing_tbl"),
                    ],
                    force_refresh=True,
                )
                self.assertEqual(open_calls["dws"], 1)
                self.assertEqual(dws_warn, "")
                self.assertEqual(len(dws_skip), 0)
                self.assertEqual(dws_metas[("ods", "cust_info")].columns[0].column_name, "ods_id")
                self.assertEqual(dws_metas[("ods", "cust_info")].orientation, "COLUMN")
                self.assertEqual(dws_metas[("dwd", "cust_info")].columns[0].column_name, "cust_id")
                # 显式指定不存在的非默认 Schema (ads.cust_info) 绝不可跨 Schema 误匹配到 ods/dwd
                self.assertFalse(dws_metas[("ads", "cust_info")].exists)
                self.assertFalse(dws_metas[("dwd", "missing_tbl")].exists)

                # 二次非强制刷新（含默认 public 回退匹配已缓存异构 Schema 表）均零远程连库
                dws_cached, _, _ = ctx.metadata_hub.get_tables_metadata_batch(
                    dws_prof.connection_id,
                    [("ods", "cust_info"), ("dwd", "cust_info"), ("public", "cust_info")],
                    force_refresh=False,
                )
                self.assertEqual(open_calls["dws"], 1)
                self.assertEqual(dws_cached[("dwd", "cust_info")].columns[0].column_name, "cust_id")
                self.assertTrue(dws_cached[("public", "cust_info")].exists)

                # MySQL 传入空白模式名或历史遗留 'public' 均自动解析至 dbname ('ods')
                my_metas, _, _ = ctx.metadata_hub.get_tables_metadata_batch(
                    mysql_prof.connection_id,
                    [("", "t_order"), ("public", "t_absent")],
                    force_refresh=True,
                )
                self.assertEqual(open_calls["mysql"], 1)
                self.assertTrue(my_metas[("", "t_order")].exists)
                self.assertEqual(my_metas[("", "t_order")].primary_keys, ["order_id"])
                self.assertFalse(my_metas[("public", "t_absent")].exists)
            finally:
                ctx.driver_manager.open_connection = orig_open

            # 2b. 验证 DWS 批量探查在非 autocommit 包装器且单表 pg_get_tabledef 报错时的事务回滚隔离恢复
            class _StrictTxDwsConn:
                def __init__(self) -> None:
                    self._in_tx = True
                    self._aborted = False

                @property
                def autocommit(self) -> bool:
                    return False

                @autocommit.setter
                def autocommit(self, _v: bool) -> None:
                    if self._in_tx:
                        raise RuntimeError("set_session cannot be used inside a transaction")

                def rollback(self) -> None:
                    self._in_tx = False
                    self._aborted = False

                def close(self) -> None:
                    pass

                def cursor(self):
                    conn = self

                    class _Cur:
                        def __enter__(s):
                            return s

                        def __exit__(s, *a):
                            return False

                        def execute(s, sql: str, params=None) -> None:
                            if conn._aborted:
                                raise RuntimeError("current transaction is aborted")
                            conn._in_tx = True
                            q = " ".join(sql.split())
                            if "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n" in q:
                                s.rows = [
                                    (2, "public", "t_ext", "", "", "f"),
                                    (1, "public", "t_hash", "", "", "r"),
                                ]
                            elif "JOIN pg_catalog.pg_attribute" in q and "c.oid," in q:
                                s.rows = [
                                    (2, "fid", "varchar(32)", False, None, ""),
                                    (1, "id", "varchar(32)", True, None, ""),
                                ]
                            elif "pg_get_tabledef(oid) FROM" in q:
                                conn._aborted = True
                                raise RuntimeError("foreign table unsupported")
                            elif "pg_get_tabledef(" in q:
                                if params and params[0] == 2:
                                    conn._aborted = True
                                    raise RuntimeError("foreign table unsupported on oid 2")
                                s.rows = [
                                    ("CREATE TABLE public.t_hash (id varchar(32)) DISTRIBUTE BY HASH(id);",)
                                ]
                            else:
                                s.rows = []

                        def fetchall(s):
                            return list(s.rows)

                        def fetchone(s):
                            return s.rows[0] if s.rows else None

                    return _Cur()

            ctx.driver_manager.open_connection = lambda p, **kw: _StrictTxDwsConn()
            try:
                tx_metas, _, _ = ctx.metadata_hub.get_tables_metadata_batch(
                    dws_prof.connection_id,
                    [("public", "t_ext"), ("public", "t_hash")],
                    force_refresh=True,
                )
                self.assertEqual(tx_metas[("public", "t_hash")].distribute_keys, ["id"])
            finally:
                ctx.driver_manager.open_connection = orig_open

            # 2c. 验证历史手工大写 col_key 在 replace_table 模式下完整清除，且候选匹配命中 COLLATE NOCASE 索引
            with ctx.store.module_db() as sconn:
                sconn.execute(
                    "INSERT INTO field_standard_bindings (connection_id, schema_name, table_name, column_name, col_key, std_code) "
                    "VALUES ('conn_wild', 'PUBLIC', 'DWD_UPPER', 'OLD_C', 'PUBLIC::DWD_UPPER::OLD_C', 'STD_W1')"
                )
                plan_en = sconn.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM standard_dictionary WHERE en_abbr = ? COLLATE NOCASE",
                    ("cust_id",),
                ).fetchall()
                plan_code = sconn.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM standard_dictionary WHERE std_code = ? COLLATE NOCASE",
                    ("std_w1",),
                ).fetchall()
            self.assertTrue(any("USING INDEX" in str(tuple(r)) for r in plan_en))
            self.assertTrue(any("USING INDEX" in str(tuple(r)) for r in plan_code))
            cat.replace_table_field_bindings(
                "conn_wild",
                "public",
                "dwd_upper",
                [{"schema_name": "public", "table_name": "dwd_upper", "column_name": "new_c", "std_code": "STD_W2"}],
            )
            self.assertEqual(set(cat.get_table_field_bindings("conn_wild", "public", "dwd_upper").keys()), {"new_c"})
            batch_cands = cat.match_standard_candidates_batch([("cust_id", "客户号"), ("acct_id", "")])
            self.assertEqual(batch_cands[("cust_id", "客户号")][0]["std_code"], "STD_W1")
            self.assertEqual(batch_cands[("acct_id", "")][0]["std_code"], "STD_W2")

            # 2d. 验证 DWS 批量探查会话级 lock_timeout/lockwait_timeout/statement_timeout、pg_get_expr 锁等待回退、15-OID 子分批单调进度及慢超时熔断
            executed_dws_sqls: List[str] = []
            tdef_sub_batches: List[int] = []
            per_oid_tdef_calls = {"cnt": 0}

            class _LockedEtlDwsConn:
                autocommit = True

                def rollback(self) -> None:
                    pass

                def close(self) -> None:
                    pass

                def cursor(self):
                    class _Cur:
                        def __enter__(s):
                            return s

                        def __exit__(s, *a):
                            return False

                        def execute(s, sql: str, params=None) -> None:
                            q = " ".join(sql.split())
                            executed_dws_sqls.append(q)
                            if q == "SET lock_timeout = 3000;":
                                raise RuntimeError('unrecognized configuration parameter "lock_timeout"')
                            if "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n" in q:
                                s.rows = [
                                    (1000 + i, "dwd", f"etl_tbl_{i:02d}", f"表{i}", "orientation=row", "r")
                                    for i in range(1, 36)
                                ]
                            elif "pg_get_expr(" in q and "JOIN pg_catalog.pg_attribute" in q:
                                raise RuntimeError("canceling statement due to lock timeout")
                            elif "NULL AS column_default" in q and "JOIN pg_catalog.pg_attribute" in q:
                                s.rows = [
                                    (1000 + i, "cust_id", "character varying(32)", True, None, "客户号")
                                    for i in range(1, 36)
                                ]
                            elif "pg_get_tabledef(oid) FROM" in q:
                                sub_oids = params[0]
                                tdef_sub_batches.append(len(sub_oids))
                                if 1016 in sub_oids:
                                    raise TimeoutError("lockwait_timeout on second 15-OID sub-batch")
                                s.rows = [
                                    (oid_x, f"CREATE TABLE dwd.etl_tbl_{oid_x - 1000:02d} (cust_id varchar(32)) DISTRIBUTE BY HASH(cust_id);")
                                    for oid_x in sub_oids
                                ]
                            elif "pg_get_tabledef(" in q:
                                per_oid_tdef_calls["cnt"] += 1
                                raise TimeoutError("ETL exclusive lock timeout on table")
                            else:
                                s.rows = []

                        def fetchall(s):
                            return list(s.rows)

                        def fetchone(s):
                            return s.rows[0] if s.rows else None

                    return _Cur()

            dws_lock_prof = ctx.connection_hub.save_profile(
                {
                    "name": "mock_dws_etl_lock",
                    "driver_track": "NATIVE",
                    "db_type": "DWS",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "dbname": "dw",
                    "user": "u",
                    "password": "p",
                }
            )
            etl_progress: List[int] = []
            ctx.driver_manager.open_connection = lambda p, **kw: _LockedEtlDwsConn()
            try:
                etl_metas, _, _ = ctx.metadata_hub.get_tables_metadata_batch(
                    dws_lock_prof.connection_id,
                    [("dwd", f"etl_tbl_{i:02d}") for i in range(1, 36)],
                    force_refresh=True,
                    on_progress=lambda d_c, _t_c, _cs, _ct: etl_progress.append(d_c),
                )
            finally:
                ctx.driver_manager.open_connection = orig_open

            self.assertIn("SET lock_timeout = 3000;", executed_dws_sqls)
            self.assertIn("SET lockwait_timeout = 3000;", executed_dws_sqls)
            self.assertIn("SET statement_timeout = 8000;", executed_dws_sqls)
            # 第一批 15 个 OID 成功，第二批 15 个 OID 触发逐表回退并在连续 3 次 TimeoutError 后熔断跳过第 3 批与 getdistributekey
            self.assertEqual(tdef_sub_batches, [15, 15])
            self.assertEqual(per_oid_tdef_calls["cnt"], 3)
            self.assertEqual(etl_progress, sorted(etl_progress))
            self.assertEqual(etl_progress[-1], 35)
            self.assertGreaterEqual(len(set(etl_progress)), 4)
            # 即使存在 ETL 排他锁，全部 35 张表依然通过 MVCC 系统表降级拿到完整字段与注释
            self.assertEqual(len(etl_metas), 35)
            self.assertEqual(etl_metas[("dwd", "etl_tbl_01")].distribute_keys, ["cust_id"])
            self.assertEqual(etl_metas[("dwd", "etl_tbl_25")].columns[0].column_name, "cust_id")
            self.assertEqual(etl_metas[("dwd", "etl_tbl_35")].columns[0].column_comment, "客户号")

            # 2e. 验证逐表回退分支（如 Inceptor / JDBC）连续 3 次超时/断连自动触发熔断，避免百张表逐一超时卡死
            from core.platform.inceptor_client import InceptorThriftClient

            inc_prof = ctx.connection_hub.save_profile(
                {
                    "name": "mock_inceptor_flaky",
                    "driver_track": "NATIVE",
                    "db_type": "INCEPTOR",
                    "host": "127.0.0.1",
                    "port": 10000,
                    "dbname": "ods",
                    "user": "hive",
                }
            )
            inc_query_calls = {"cnt": 0}

            class _FakeFlakyInceptor(InceptorThriftClient):
                def __init__(self) -> None:
                    pass

                def execute_query(self, sql: str, max_rows: int = 1000):
                    inc_query_calls["cnt"] += 1
                    if "inc_tbl_01" in sql:
                        if sql.startswith("DESCRIBE FORMATTED"):
                            return [["cust_id", "varchar(32)", "客户号"]]
                        return [["CREATE TABLE ods.inc_tbl_01 (cust_id varchar(32))"]]
                    raise TimeoutError("Inceptor Thrift socket timed out")

                def close(self) -> None:
                    pass

            inc_progress: List[int] = []
            ctx.driver_manager.open_connection = lambda p, **kw: _FakeFlakyInceptor()
            try:
                inc_targets = [("ods", f"inc_tbl_{i:02d}") for i in range(1, 51)]
                inc_metas, inc_warn, inc_skipped = ctx.metadata_hub.get_tables_metadata_batch(
                    inc_prof.connection_id,
                    inc_targets,
                    force_refresh=True,
                    tolerate_errors=True,
                    on_progress=lambda d_c, _t_c, _cs, _ct: inc_progress.append(d_c),
                )
                self.assertTrue(inc_metas[("ods", "inc_tbl_01")].exists)
                self.assertIn("timed out", inc_warn)
                # 第 1 张表成功 (2 次查询)，第 2/3/4 张表各触发 1 次 TimeoutError 后立即熔断跳过剩余 46 张表
                self.assertEqual(inc_query_calls["cnt"], 5)
                self.assertEqual(len([k for k in inc_targets if (k[0], k[1]) in inc_skipped]), 49)
                self.assertEqual(inc_progress, sorted(inc_progress))
                self.assertEqual(inc_progress[-1], 50)

                # 当 tolerate_errors=False 时必须原样抛出 TimeoutError 绝不静默吞没
                with self.assertRaises(TimeoutError):
                    ctx.metadata_hub.get_tables_metadata_batch(
                        inc_prof.connection_id,
                        [("ods", "inc_tbl_02")],
                        force_refresh=True,
                        tolerate_errors=False,
                    )
            finally:
                ctx.driver_manager.open_connection = orig_open

            # 3. 验证运行态进度条目延长 TTL 保护与轮询 200 访问日志降噪过滤器
            hub = ImportProgressHub(max_entries=4, ttl_seconds=60.0)
            hub.start("imp_running_long", "长耗时探查")
            hub.start("imp_done_short", "短耗时完成")
            hub.finish("imp_done_short", detail="ok")
            hub._items["imp_running_long"]["updated_ts"] -= 120.0
            hub._items["imp_done_short"]["updated_ts"] -= 120.0
            self.assertIsNone(hub.get("imp_done_short"))
            self.assertIsNotNone(hub.get("imp_running_long"))

            flt = _QuietImportProgressAccessFilter()
            rec_poll_200 = logging.LogRecord(
                "uvicorn.access", logging.INFO, "", 1,
                '127.0.0.1:51234 - "GET /api/platform/import-progress/imp_abc123 HTTP/1.1" 200',
                None, None,
            )
            rec_poll_500 = logging.LogRecord(
                "uvicorn.access", logging.INFO, "", 1,
                '127.0.0.1:51234 - "GET /api/platform/import-progress/imp_abc123 HTTP/1.1" 500',
                None, None,
            )
            rec_other = logging.LogRecord(
                "uvicorn.access", logging.INFO, "", 1,
                '127.0.0.1:51234 - "POST /api/modules/standard_migrator/bindings/upload HTTP/1.1" 200',
                None, None,
            )
            self.assertFalse(flt.filter(rec_poll_200))
            self.assertTrue(flt.filter(rec_poll_500))
            self.assertTrue(flt.filter(rec_other))

    def test_connection_profile_standard_format_persistence_and_migration(self) -> None:
        """验证 connection_profiles.standard_format 幂等迁移、重启持久化、校验与密码脱敏."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "legacy_conn.db")
            legacy = sqlite3.connect(db_path)
            legacy.execute(
                """
                CREATE TABLE connection_profiles (
                    connection_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    driver_track TEXT NOT NULL DEFAULT 'NATIVE',
                    db_type TEXT NOT NULL DEFAULT 'DWS',
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER NOT NULL DEFAULT 8000,
                    dbname TEXT NOT NULL DEFAULT '',
                    default_schema TEXT NOT NULL DEFAULT 'public',
                    user TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    sslmode TEXT NOT NULL DEFAULT 'prefer',
                    auth_mode TEXT NOT NULL DEFAULT 'LDAP',
                    sqlite_path TEXT NOT NULL DEFAULT '',
                    jdbc_driver_id TEXT NOT NULL DEFAULT '',
                    jdbc_url TEXT NOT NULL DEFAULT '',
                    jdbc_driver_class TEXT NOT NULL DEFAULT '',
                    extra_props_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            legacy.execute(
                """
                INSERT INTO connection_profiles (
                    connection_id, name, db_type, host, port, dbname, user, password
                ) VALUES ('conn_legacy_1', '历史DWS库', 'DWS', '127.0.0.1', 8000, 'dw', 'admin', 'secret123')
                """
            )
            legacy.commit()
            legacy.close()

            ctx = PlatformContext(
                db_path=db_path,
                drivers_dir=os.path.join(tmp, "drivers"),
                assets_dir=os.path.join(tmp, "assets"),
            )
            legacy_prof = ctx.connection_hub.get_profile("conn_legacy_1")
            self.assertIsNotNone(legacy_prof)
            assert legacy_prof is not None
            self.assertEqual(legacy_prof.standard_format, "AUTO")

            updated = ctx.connection_hub.update_standard_format("conn_legacy_1", "type_dw_mart")
            self.assertEqual(updated.standard_format, "type_dw_mart")

            created = ctx.connection_hub.save_profile(
                {
                    "connection_id": "conn_argo_1",
                    "name": "星环Argo库",
                    "db_type": "INCEPTOR",
                    "host": "10.0.0.8",
                    "port": 10000,
                    "user": "hive",
                    "password": "pw_argo",
                    "standard_format": "type_argodb",
                }
            )
            self.assertEqual(created.standard_format, "type_argodb")

            with self.assertRaises(ValueError):
                ctx.connection_hub.update_standard_format("conn_legacy_1", "invalid_format")
            with self.assertRaises(ValueError):
                ctx.connection_hub.update_standard_format("conn_legacy_1", "")
            with self.assertRaises(ValueError):
                ctx.connection_hub.update_standard_format("conn_legacy_1", "   ")
            with self.assertRaises(ValueError):
                ctx.connection_hub.save_profile(
                    {
                        "connection_id": "conn_legacy_1",
                        "standard_format": "",
                    },
                    connection_id="conn_legacy_1",
                )
            with self.assertRaises(LookupError):
                ctx.connection_hub.update_standard_format("conn_not_exists", "type_dws")

            reopened = PlatformContext(
                db_path=db_path,
                drivers_dir=os.path.join(tmp, "drivers"),
                assets_dir=os.path.join(tmp, "assets"),
            )
            p1 = reopened.connection_hub.require_profile("conn_legacy_1").to_public_dict()
            p2 = reopened.connection_hub.require_profile("conn_argo_1").to_public_dict()
            self.assertEqual(p1["standard_format"], "type_dw_mart")
            self.assertTrue(p1["has_saved_password"])
            self.assertEqual(p1["password_masked"], "******")
            self.assertNotIn("password", p1)
            self.assertEqual(p2["standard_format"], "type_argodb")


if __name__ == "__main__":
    unittest.main()
