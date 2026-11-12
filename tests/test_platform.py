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


if __name__ == "__main__":
    unittest.main()
