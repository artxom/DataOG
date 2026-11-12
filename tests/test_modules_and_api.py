"""DataOG v2.0.0 FastAPI 微内核路由、三大独立插件模块及架构安全红线集成测试."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dis
import glob
import importlib
import importlib.util
import io
import json
import os
import pkgutil
import re
import sqlite3
import sys
import tempfile
import types
import unittest
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock
from urllib.parse import urlencode, urlsplit

import openpyxl

from app import create_app, resolve_static_dir
from core.platform.context import PlatformContext


class _CaseInsensitiveHeaders:
    def __init__(self, pairs: List[Tuple[bytes, bytes]]) -> None:
        self._map: Dict[str, str] = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in pairs
        }

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._map.get(key.lower(), default)

    def __contains__(self, key: str) -> bool:
        return key.lower() in self._map


class _AsgiResponse:
    def __init__(self, status_code: int, raw_headers: List[Tuple[bytes, bytes]], content: bytes) -> None:
        self.status_code = status_code
        self.headers = _CaseInsensitiveHeaders(raw_headers)
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class TestClient:
    """零第三方 HTTP 依赖的标准库内存 ASGI 测试客户端（免打包 httpx / httpcore / certifi）."""

    __test__ = False

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _encode_multipart(
        data: Optional[Dict[str, Any]],
        files: Dict[str, Tuple[str, bytes, str]],
    ) -> Tuple[bytes, str]:
        boundary = f"----DataOGBoundary{uuid.uuid4().hex}"
        chunks: List[bytes] = []
        for k, v in (data or {}).items():
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
            chunks.append(str(v).encode("utf-8") + b"\r\n")
        for field_name, file_spec in files.items():
            filename, file_bytes, content_type = file_spec
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8")
            )
            chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            chunks.append(bytes(file_bytes) + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Tuple[str, bytes, str]]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> _AsgiResponse:
        hdrs: Dict[str, str] = {"host": "127.0.0.1:18080"}
        if headers:
            for hk, hv in headers.items():
                hdrs[hk.lower()] = hv

        body = b""
        if files:
            body, ctype = self._encode_multipart(data, files)
            hdrs["content-type"] = ctype
        elif json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("content-type", "application/json")
        elif data is not None:
            body = urlencode(data).encode("utf-8")
            hdrs.setdefault("content-type", "application/x-www-form-urlencoded")

        if body:
            hdrs["content-length"] = str(len(body))

        parsed = urlsplit(url)
        path = parsed.path or "/"
        query_bytes = parsed.query.encode("ascii")
        raw_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in hdrs.items()
        ]

        async def _invoke() -> _AsgiResponse:
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method.upper(),
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("utf-8"),
                "query_string": query_bytes,
                "headers": raw_headers,
                "client": ("127.0.0.1", 54321),
                "server": ("127.0.0.1", 18080),
            }
            sent = False
            status_code = 500
            resp_headers: List[Tuple[bytes, bytes]] = []
            resp_chunks: List[bytes] = []

            async def receive() -> Dict[str, Any]:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(msg: Dict[str, Any]) -> None:
                nonlocal status_code, resp_headers
                if msg["type"] == "http.response.start":
                    status_code = int(msg["status"])
                    resp_headers = list(msg.get("headers") or [])
                elif msg["type"] == "http.response.body":
                    resp_chunks.append(msg.get("body") or b"")

            await self.app(scope, receive, send)
            return _AsgiResponse(status_code, resp_headers, b"".join(resp_chunks))

        return asyncio.run(_invoke())

    def get(self, url: str, *, headers: Optional[Dict[str, str]] = None) -> _AsgiResponse:
        return self.request("GET", url, headers=headers)

    def post(
        self,
        url: str,
        *,
        json: Any = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Tuple[str, bytes, str]]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> _AsgiResponse:
        return self.request("POST", url, json_body=json, data=data, files=files, headers=headers)

    def put(
        self,
        url: str,
        *,
        json: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> _AsgiResponse:
        return self.request("PUT", url, json_body=json, headers=headers)

    def delete(self, url: str, *, headers: Optional[Dict[str, str]] = None) -> _AsgiResponse:
        return self.request("DELETE", url, headers=headers)


def _build_mini_jar() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        zf.writestr("META-INF/services/java.sql.Driver", "com.kingbase8.Driver\n")
    return buf.getvalue()


def _build_standard_excel_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "数据标准清单"
    ws.append(
        [
            "模式名",
            "表名",
            "修改前字段名",
            "修改前类型",
            "修改前注释",
            "修改后字段名",
            "修改后类型",
            "修改后注释",
        ]
    )
    ws.append(
        [
            "main",
            "t_acct_info",
            "acct_no",
            "VARCHAR(20)",
            "旧账号",
            "account_no",
            "VARCHAR(64)",
            "标准客户账号",
        ]
    )
    ws.append(
        [
            "main",
            "t_acct_info",
            "open_dt",
            "DATE",
            "开户日期",
            "open_dt",
            "DATE",
            "开户日期",
        ]
    )
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class TestModulesAndFastApiWorkbench(unittest.TestCase):
    def test_security_headers_csrf_defense_and_static_es_modules(self) -> None:
        """验证安全响应头、Sec-Fetch-Site 跨站写拦截及模块化静态资源加载."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_app(db_path=os.path.join(tmpdir, "dataog_state.db"))
            client = TestClient(app)

            r_home = client.get("/")
            self.assertEqual(r_home.status_code, 200)
            self.assertEqual(r_home.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(r_home.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("script-src 'self'", r_home.headers.get("Content-Security-Policy", ""))
            self.assertIn('type="module" src="/js/main.js"', r_home.text)

            r_js = client.get("/js/main.js")
            self.assertEqual(r_js.status_code, 200)
            self.assertIn("bootstrapWorkbench", r_js.text)

            # 模拟恶意跨站页面发起 POST 调用：必须返回 403 阻断
            r_csrf = client.post(
                "/api/platform/logs/clear",
                headers={"Sec-Fetch-Site": "cross-site"},
            )
            self.assertEqual(r_csrf.status_code, 403)

            # 同源请求正常放行
            r_same = client.post(
                "/api/platform/logs/clear",
                headers={"Sec-Fetch-Site": "same-origin"},
            )
            self.assertEqual(r_same.status_code, 200)
            self.assertTrue(r_same.json()["ok"])

    def test_platform_endpoints_and_three_plugin_modules_closed_loop(self) -> None:
        """端到端验证：平台连接/驱动/查询 API + DDL翻译四层穿透 + 跨环境三栏比对 + 推送至单库贯标台闭环."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_sqlite = os.path.join(tmpdir, "source_ods.sqlite")
            tgt_sqlite = os.path.join(tmpdir, "target_dwd.sqlite")

            with contextlib.closing(sqlite3.connect(src_sqlite)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE raw_acct_base (
                        acct_no VARCHAR(64) PRIMARY KEY,
                        open_dt DATE NOT NULL,
                        branch_cd VARCHAR(16)
                    );
                    CREATE VIEW v_acct_l1 AS SELECT acct_no, open_dt, branch_cd FROM raw_acct_base;
                    CREATE VIEW v_acct_l2 AS SELECT acct_no, open_dt, branch_cd FROM v_acct_l1;
                    CREATE VIEW v_acct_l3 AS SELECT acct_no, open_dt, branch_cd FROM v_acct_l2;
                    CREATE VIEW t_acct_info AS SELECT acct_no, open_dt, branch_cd FROM v_acct_l3;
                    INSERT INTO raw_acct_base VALUES ('A1001', '2026-01-01', 'B01');
                    INSERT INTO raw_acct_base VALUES ('A1002', '2026-01-02', 'B02');
                    """
                )

            with contextlib.closing(sqlite3.connect(tgt_sqlite)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE t_acct_info (
                        acct_no VARCHAR(20), -- 旧账号
                        open_dt DATE -- 开户日期
                    );
                    INSERT INTO t_acct_info VALUES ('A1001', '2026-01-01');
                    """
                )

            app = create_app(db_path=os.path.join(tmpdir, "dataog_state.db"))
            client = TestClient(app)

            # 1. Bootstrap 检查三大插件清单注册
            boot = client.get("/api/platform/bootstrap").json()
            self.assertTrue(boot["ok"])
            mod_ids = {m["module_id"] for m in boot["modules"]}
            self.assertEqual(mod_ids, {"standard_migrator", "cross_comparator", "ddl_translator"})

            # 2. 上传并删除自定义 JDBC .jar 驱动
            up_drv = client.post(
                "/api/platform/drivers/upload",
                files={"file": ("kingbase8.jar", _build_mini_jar(), "application/java-archive")},
                data={"name": "KingbaseES驱动", "db_type_hint": "KINGBASE"},
            ).json()
            self.assertTrue(up_drv["ok"])
            drv_id = up_drv["driver"]["driver_id"]
            self.assertEqual(up_drv["driver"]["driver_class_hint"], "com.kingbase8.Driver")

            del_drv = client.delete(f"/api/platform/drivers/{drv_id}").json()
            self.assertTrue(del_drv["ok"])

            # 3. 创建来源库与目标库两个全局共享数据源连接档案并自动刷新 MetadataHub
            c_src = client.post(
                "/api/platform/connections",
                json={
                    "name": "ODS来源库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": src_sqlite,
                    "auto_refresh_metadata": True,
                },
            ).json()
            self.assertTrue(c_src["ok"])
            src_cid = c_src["connection"]["connection_id"]

            c_tgt = client.post(
                "/api/platform/connections",
                json={
                    "name": "DWD目标库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": tgt_sqlite,
                    "auto_refresh_metadata": True,
                },
            ).json()
            self.assertTrue(c_tgt["ok"])
            tgt_cid = c_tgt["connection"]["connection_id"]

            # 4. 平台统一查询、表预览与导出测试
            q_res = client.post(
                "/api/platform/query/execute",
                json={"connection_id": src_cid, "sql": "SELECT * FROM t_acct_info ORDER BY acct_no", "limit": 1},
            ).json()
            self.assertTrue(q_res["ok"])
            self.assertEqual(q_res["result"]["row_count"], 1)
            self.assertTrue(q_res["result"]["truncated"])

            exp_csv = client.post(
                "/api/platform/query/export",
                json={
                    "columns": ["acct_no", "open_dt"],
                    "rows": [["A1001", "2026-01-01"]],
                    "format": "csv",
                    "title": "acct_export",
                },
            )
            self.assertEqual(exp_csv.status_code, 200)
            self.assertIn("A1001", exp_csv.content.decode("utf-8-sig"))

            # 5. 独立模块 ddl_translator：将四层嵌套视图 t_acct_info 一键穿透并翻译为 DWS / INCEPTOR / MYSQL / DM8 / ORACLE 建表语句
            for dialect in ("DWS", "PGSQL", "INCEPTOR", "MYSQL", "DM8", "ORACLE", "SQLITE"):
                tr = client.post(
                    "/api/modules/ddl_translator/translate",
                    json={
                        "source_mode": "connection",
                        "connection_id": src_cid,
                        "source_schema": "main",
                        "source_table": "t_acct_info",
                        "target_dialect": dialect,
                        "storage_mode": "INCEPTOR_ORC_TXN" if dialect == "INCEPTOR" else "AUTO",
                    },
                ).json()
                self.assertTrue(tr["ok"], f"Dialect {dialect} translation failed: {tr}")
                self.assertIn("CREATE TABLE", tr["target_ddl"].upper())
                self.assertTrue(any("raw_acct_base" in b for b in tr["penetrated_base_tables"]))

            # 6. 独立模块 cross_comparator：绑定左右库 -> 导入 Excel 数标 -> 三栏比对 -> 导出 -> 推送待改造项至贯标台
            client.post(
                "/api/modules/cross_comparator/bind-role",
                json={"role": "source", "connection_id": src_cid, "auto_refresh": True},
            )
            client.post(
                "/api/modules/cross_comparator/bind-role",
                json={"role": "target", "connection_id": tgt_cid, "auto_refresh": True},
            )
            up_std = client.post(
                "/api/modules/cross_comparator/standard/upload",
                files={
                    "file": (
                        "acct_standard.xlsx",
                        _build_standard_excel_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            ).json()
            self.assertTrue(up_std["ok"])
            comp = up_std["active_comparison"]
            self.assertIsNotNone(comp)
            # acct_no 在目标库为 VARCHAR(20) 待改名为 account_no VARCHAR(64) -> PENDING_MIGRATION；open_dt 已合规 -> COMPLIANT
            self.assertEqual(comp["summary"]["compliant"], 1)
            self.assertEqual(comp["summary"]["target_pending"], 1)

            exp_json = client.get("/api/modules/cross_comparator/bindings/export.json")
            self.assertEqual(exp_json.status_code, 200)
            exp_xlsx = client.get("/api/modules/cross_comparator/bindings/export.xlsx")
            self.assertEqual(exp_xlsx.status_code, 200)

            push_res = client.post(
                "/api/modules/cross_comparator/push-to-migrator",
                json={"std_table_key": up_std["active_table_key"]},
            ).json()
            self.assertTrue(push_res["ok"])
            self.assertEqual(push_res["pushed_count"], 2)

            # 7. 独立模块 standard_migrator：绑定目标连接、核验由三栏比对推送过来的规则（1条 PENDING + 1条 COMPLETED）并导出报告
            mig_bind = client.post(
                "/api/modules/standard_migrator/bind-connection",
                json={"connection_id": tgt_cid, "auto_sync": True},
            ).json()
            self.assertTrue(mig_bind["ok"])
            self.assertEqual(len(mig_bind["items"]), 2)
            by_new_col = {it["new_col_name"]: it for it in mig_bind["items"]}
            self.assertEqual(by_new_col["account_no"]["status"], "PENDING")
            self.assertEqual(by_new_col["open_dt"]["status"], "COMPLETED")

            tpl_resp = client.get("/api/modules/standard_migrator/checklist/template.xlsx")
            self.assertEqual(tpl_resp.status_code, 200)
            rep_resp = client.get("/api/modules/standard_migrator/checklist/report.xlsx")
            self.assertEqual(rep_resp.status_code, 200)

    def test_plugin_modules_strict_zero_cross_import_isolation(self) -> None:
        """架构铁律校验：扫描 modules/* 下全部 Python AST（或打包态字节码），确保模块之间零相互私有导入."""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_root = os.path.join(project_root, "modules")
        py_files = glob.glob(os.path.join(modules_root, "**", "*.py"), recursive=True)

        violations = []
        if py_files:
            self.assertGreaterEqual(len(py_files), 9)
            for path in py_files:
                rel = os.path.relpath(path, modules_root)
                parts = rel.split(os.sep)
                if len(parts) < 2:
                    continue
                own_module = parts[0]
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
                for node in ast.walk(tree):
                    imported_names = []
                    if isinstance(node, ast.Import):
                        imported_names.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_names.append(node.module)
                    for imp in imported_names:
                        if imp.startswith("modules."):
                            target_mod = imp.split(".")[1]
                            if target_mod != own_module:
                                violations.append(f"{rel} 违规导入了其他插件模块 {imp}")
        else:
            # PyInstaller 冻结态下 .py 已编译进入 PYZ，递归扫描全部模块与函数字节码指令
            import modules as modules_pkg

            def _iter_code_imports(code_obj: types.CodeType):
                for instr in dis.get_instructions(code_obj):
                    if instr.opname == "IMPORT_NAME" and isinstance(instr.argval, str):
                        yield instr.argval
                for const in code_obj.co_consts:
                    if isinstance(const, types.CodeType):
                        yield from _iter_code_imports(const)

            inspected_count = 0
            for mod_info in pkgutil.walk_packages(modules_pkg.__path__, "modules."):
                parts = mod_info.name.split(".")
                if len(parts) < 3:
                    continue
                own_module = parts[1]
                mod_obj = importlib.import_module(mod_info.name)
                inspected_count += 1
                code_roots: list[types.CodeType] = []
                spec = importlib.util.find_spec(mod_info.name)
                if spec and spec.loader and hasattr(spec.loader, "get_code"):
                    top_code = spec.loader.get_code(mod_info.name)
                    if isinstance(top_code, types.CodeType):
                        code_roots.append(top_code)
                if not code_roots:
                    for obj in vars(mod_obj).values():
                        code_obj = getattr(obj, "__code__", None)
                        if isinstance(code_obj, types.CodeType):
                            code_roots.append(code_obj)
                for root_code in code_roots:
                    for imp in _iter_code_imports(root_code):
                        if imp.startswith("modules.") and imp.split(".")[1] != own_module:
                            violations.append(f"{mod_info.name} 违规导入了其他插件模块 {imp}")
            self.assertGreaterEqual(inspected_count, 9)

        self.assertEqual(violations, [], f"发现违反插件隔离铁律的跨模块导入: {violations}")

    def test_rebuild_preview_schema_inheritance_live_cross_save_and_password_masking(self) -> None:
        """回归验证：重建预览无 AttributeError、清单上传继承连接默认 Schema、在线三栏桥接无 ID 保存推送与旧接口密码脱敏."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_sqlite = os.path.join(tmpdir, "live_src.sqlite")
            tgt_sqlite = os.path.join(tmpdir, "live_tgt.sqlite")
            with contextlib.closing(sqlite3.connect(src_sqlite)) as conn:
                conn.execute("CREATE TABLE t_loan (loan_id VARCHAR(64), amt DECIMAL(18,2))")
                conn.commit()
            with contextlib.closing(sqlite3.connect(tgt_sqlite)) as conn:
                conn.execute("CREATE TABLE t_loan (old_loan_id VARCHAR(20), amt DECIMAL(18,2))")
                conn.commit()

            ctx = PlatformContext(
                db_path=os.path.join(tmpdir, "dataog_state.db"),
                drivers_dir=os.path.join(tmpdir, "drivers"),
            )
            ctx.store.save_meta(
                "dws_conn_saved",
                {
                    "configured": True,
                    "host": "10.0.0.9",
                    "port": 8000,
                    "dbname": "dwsdb",
                    "user": "dbadmin",
                    "password": "TopSecretValue!2026",
                },
            )
            app = create_app(ctx=ctx)
            client = TestClient(app)

            # 1. 旧版 /api/bootstrap 绝不明文回传密码
            legacy_boot = client.get("/api/bootstrap").json()
            self.assertNotIn("password", legacy_boot["connection"])
            self.assertTrue(legacy_boot["connection"]["has_saved_password"])

            src_cid = client.post(
                "/api/platform/connections",
                json={"name": "直连来源库", "db_type": "SQLITE", "sqlite_path": src_sqlite},
            ).json()["connection"]["connection_id"]
            tgt_cid = client.post(
                "/api/platform/connections",
                json={
                    "name": "直连目标库",
                    "db_type": "SQLITE",
                    "sqlite_path": tgt_sqlite,
                    "default_schema": "main",
                },
            ).json()["connection"]["connection_id"]

            # 2. 未导入 Excel 时，直接在三栏比对中保存无正数 ID 的草稿桥接并推送至贯标台
            client.post(
                "/api/modules/cross_comparator/bind-role",
                json={"role": "source", "connection_id": src_cid, "auto_refresh": True},
            )
            client.post(
                "/api/modules/cross_comparator/bind-role",
                json={"role": "target", "connection_id": tgt_cid, "auto_refresh": True},
            )
            save_res = client.post(
                "/api/modules/cross_comparator/bindings/save",
                json={
                    "std_table_key": "t_loan",
                    "source_schema": "main",
                    "source_table": "t_loan",
                    "target_schema": "main",
                    "target_table": "t_loan",
                    "column_bindings": [
                        {
                            "std_col_name": "loan_id",
                            "orig_col_name": "loan_id",
                            "std_col_type": "VARCHAR(64)",
                            "bound_source_col": "loan_id",
                            "bound_target_col": "old_loan_id",
                        },
                        {
                            "std_col_name": "amt",
                            "orig_col_name": "amt",
                            "std_col_type": "DECIMAL(18,2)",
                            "bound_source_col": "amt",
                            "bound_target_col": "amt",
                        },
                    ],
                    "force_refresh": True,
                },
            ).json()
            self.assertTrue(save_res["ok"])
            saved_cols = {
                c["std_col_name"]: c
                for c in save_res["active_comparison"]["standard_panel"]["columns"]
            }
            self.assertEqual(saved_cols["loan_id"]["bound_target_col"], "old_loan_id")

            push_res = client.post(
                "/api/modules/cross_comparator/push-to-migrator",
                json={"std_table_key": "t_loan"},
            ).json()
            self.assertTrue(push_res["ok"])
            self.assertEqual(push_res["pushed_count"], 2)

            # 3. 贯标台同步并生成整表保序重建 SQL 预览（无 AttributeError，包含 rebuild_sql 与 column_count）
            client.post(
                "/api/modules/standard_migrator/bind-connection",
                json={"connection_id": tgt_cid, "auto_sync": True},
            )
            preview_res = client.post(
                "/api/modules/standard_migrator/rebuild/preview",
                json={"schema_name": "main", "table_name": "t_loan", "keep_backup": True},
            ).json()
            self.assertTrue(preview_res["ok"])
            self.assertEqual(preview_res["column_count"], 2)
            self.assertIn("CREATE TABLE", preview_res["rebuild_sql"])
            self.assertIn("loan_id", preview_res["sql_preview"])

            # 4. 上传不含模式名列的 Excel 清单时自动继承当前绑定连接的 default_schema
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["表名", "原字段名", "标准字段名", "目标类型"])
            ws.append(["t_loan", "old_loan_id", "loan_id", "VARCHAR(64)"])
            buf = io.BytesIO()
            wb.save(buf)
            up_no_schema = client.post(
                "/api/modules/standard_migrator/checklist/upload",
                files={
                    "file": (
                        "no_schema.xlsx",
                        buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            ).json()
            self.assertTrue(up_no_schema["ok"])
            self.assertEqual(up_no_schema["items"][0]["schema_name"], "main")

    def test_frontend_static_files_zero_xss_sinks_and_zero_blocking_dialogs(self) -> None:
        """前端安全红线校验：确保所有原生 ES Module 与 HTML 零 innerHTML/outerHTML/阻塞弹窗/内联脚本."""
        static_root = resolve_static_dir()
        project_root = os.path.dirname(static_root)

        with open(os.path.join(static_root, "index.html"), encoding="utf-8") as fh:
            index_html = fh.read()
        # 仅允许外部 <script type="module" src="/js/main.js"></script>，严禁任何内联脚本代码
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", index_html, flags=re.DOTALL | re.IGNORECASE)
        for body in scripts:
            self.assertEqual(body.strip(), "", "index.html 中严禁包含内联 JavaScript 代码块")

        unsafe_dom_re = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b")
        blocking_dialog_re = re.compile(r"(?<!\.)\b(alert|confirm|prompt)\s*\(")

        js_files = glob.glob(os.path.join(static_root, "**", "*.js"), recursive=True)
        self.assertGreaterEqual(len(js_files), 12)

        bad_hits = []
        for jf in js_files:
            rel_jf = os.path.relpath(jf, project_root)
            with open(jf, encoding="utf-8") as jfh:
                lines = jfh.read().splitlines()
            for lineno, line in enumerate(lines, start=1):
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                    continue
                if unsafe_dom_re.search(line):
                    bad_hits.append(f"{rel_jf}:{lineno} 命中不安全 DOM API: {stripped}")
                # 排除类方法声明如 `confirm({ title = ... }) {`
                if blocking_dialog_re.search(line) and not re.match(r"^confirm\s*\(", stripped):
                    bad_hits.append(f"{rel_jf}:{lineno} 命中阻塞式弹窗调用: {stripped}")

        self.assertEqual(bad_hits, [], f"前端静态安全扫描未通过: {bad_hits}")

    def test_appearance_llm_and_polyglot_endpoints_end_to_end(self) -> None:
        """端到端验证 v2.1.0：Data.O.G 外观与自定义图标持久化、LLM 路由及多邻国智能命名与 AI 方言适配."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = PlatformContext(
                db_path=os.path.join(tmpdir, "dataog_state.db"),
                drivers_dir=os.path.join(tmpdir, "drivers"),
                assets_dir=os.path.join(tmpdir, "assets"),
            )
            app = create_app(ctx=ctx)
            client = TestClient(app)

            # 1. Bootstrap 验证官方品牌名 Data.O.G 与四大桌面架构支撑数据
            boot = client.get("/api/platform/bootstrap").json()
            self.assertEqual(boot["brand_name"], "Data.O.G")
            self.assertIn("appearance", boot)
            self.assertIn("llm_providers", boot)
            self.assertIn("llm_models", boot)

            # 2. 外观壁纸与软件/模块图标上传、安全响应头与 /favicon.ico 动态覆盖
            r_bad_svg = client.post(
                "/api/platform/appearance/upload",
                files={"file": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
                data={"slot": "wallpaper"},
            )
            self.assertEqual(r_bad_svg.status_code, 400)

            up_brand = client.post(
                "/api/platform/appearance/upload",
                files={"file": ("brand.png", png_bytes, "image/png")},
                data={"slot": "brand"},
            ).json()
            self.assertTrue(up_brand["ok"])
            brand_url = up_brand["appearance"]["brand_icon_url"]
            self.assertTrue(brand_url.startswith("/api/platform/assets/"))

            r_asset = client.get(brand_url)
            self.assertEqual(r_asset.status_code, 200)
            self.assertEqual(r_asset.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIn("sandbox", r_asset.headers.get("Content-Security-Policy", ""))

            # 路径穿越资产请求必须返回 404
            self.assertEqual(client.get("/api/platform/assets/not_valid_name.png").status_code, 404)

            # /favicon.ico 应动态返回刚上传的 PNG 品牌图标，重置后恢复默认 SVG
            r_fav_png = client.get("/favicon.ico")
            self.assertEqual(r_fav_png.headers.get("content-type"), "image/png")
            client.post("/api/platform/appearance/reset", json={"slot": "brand"})
            r_fav_svg = client.get("/favicon.ico")
            self.assertIn("image/svg+xml", r_fav_svg.headers.get("content-type", ""))

            # 3. 多邻国智能命名：未配密钥时自动回退本地 170+ 数仓标准词根并输出建表草稿
            r_local_naming = client.post(
                "/api/modules/ddl_translator/naming",
                json={
                    "text": "客户唯一标识\n累计交易金额\n是否实名认证",
                    "granularity": "column",
                    "naming_style": "snake_case",
                    "table_prefix": "dwd",
                    "use_abbrev": True,
                    "target_dialect": "DWS",
                    "use_ai": True,
                },
            ).json()
            self.assertTrue(r_local_naming["ok"])
            self.assertFalse(r_local_naming["ai_used"])
            by_cn = {it["cn"]: it for it in r_local_naming["items"]}
            self.assertEqual(by_cn["客户唯一标识"]["en"], "cust_uniq_id")
            self.assertEqual(by_cn["累计交易金额"]["en"], "cum_txn_amt")
            self.assertEqual(by_cn["累计交易金额"]["type"], "NUMERIC(18,2)")
            self.assertIn("CREATE TABLE", r_local_naming["ddl_preview"])

            # 4. 配置 DeepSeek API Key（验证密钥脱敏）并测试 AI 驱动智能命名与 AI 跨方言翻译
            secret_key = "sk-live-deepseek-test-key-999999"
            r_save_llm = client.put(
                "/api/platform/llm/providers/deepseek",
                json={
                    "name": "DeepSeek",
                    "base_url": "https://api.deepseek.com",
                    "api_key": secret_key,
                    "default_model": "deepseek-chat",
                    "models": ["deepseek-chat", "deepseek-reasoner"],
                    "enabled": True,
                    "is_default": True,
                },
            )
            self.assertEqual(r_save_llm.status_code, 200)
            self.assertNotIn(secret_key, r_save_llm.text)
            self.assertTrue(r_save_llm.json()["provider"]["has_api_key"])

            with mock.patch.object(
                ctx.llm_hub,
                "chat_completion",
                return_value={
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "content": json.dumps(
                        [
                            {
                                "cn": "个人信贷合同明细表",
                                "en": "rtl_loan_ctrct_dtl",
                                "type": "TABLE",
                                "note": "AI信贷词根",
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 42.5,
                },
            ):
                r_ai_naming = client.post(
                    "/api/modules/ddl_translator/naming",
                    json={
                        "text": "个人信贷合同明细表",
                        "granularity": "table",
                        "naming_style": "snake_case",
                        "table_prefix": "dwd",
                        "use_ai": True,
                        "provider_id": "deepseek",
                        "model": "deepseek-chat",
                    },
                ).json()
                self.assertTrue(r_ai_naming["ok"])
                self.assertTrue(r_ai_naming["ai_used"])
                self.assertEqual(r_ai_naming["items"][0]["en"], "dwd_rtl_loan_ctrct_dtl")

            # 5. 多邻国方言翻译：支持直接粘贴 CREATE TABLE SQL 翻译 + AI 深度语法适配 (AST+AI)
            sample_ct = """
            CREATE TABLE ods.t_cust_bal (
                cust_id VARCHAR(64) PRIMARY KEY COMMENT '客户编号',
                bal_amt DECIMAL(18,2) COMMENT '账户余额'
            );
            """
            with mock.patch.object(
                ctx.llm_hub,
                "chat_completion",
                return_value={
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "content": json.dumps(
                        {
                            "target_sql": "CREATE TABLE ods.t_cust_bal (\n    cust_id VARCHAR(64) NOT NULL,\n    bal_amt NUMERIC(18,2)\n) WITH (ORIENTATION = COLUMN) DISTRIBUTE BY HASH(cust_id);",
                            "notes": ["已切换为 DWS 列存并按主键 cust_id 哈希分布"],
                        },
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 68.0,
                },
            ):
                r_ai_tr = client.post(
                    "/api/modules/ddl_translator/translate",
                    json={
                        "source_mode": "sql_text",
                        "sql_text": sample_ct,
                        "target_dialect": "DWS",
                        "storage_mode": "DWS_COLUMN",
                        "ai_assist": True,
                        "provider_id": "deepseek",
                        "model": "deepseek-chat",
                    },
                ).json()
                self.assertTrue(r_ai_tr["ok"])
                self.assertEqual(r_ai_tr["engine_mode"], "AST+AI")
                self.assertTrue(r_ai_tr["ai_used"])
                self.assertIn("ORIENTATION = COLUMN", r_ai_tr["target_ddl"])
                self.assertEqual(len(r_ai_tr["column_mappings"]), 2)


if __name__ == "__main__":
    unittest.main()
