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
            response_complete = asyncio.Event()
            status_code = 500
            resp_headers: List[Tuple[bytes, bytes]] = []
            resp_chunks: List[bytes] = []

            async def receive() -> Dict[str, Any]:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await response_complete.wait()
                return {"type": "http.disconnect"}

            async def send(msg: Dict[str, Any]) -> None:
                nonlocal status_code, resp_headers
                if msg["type"] == "http.response.start":
                    status_code = int(msg["status"])
                    resp_headers = list(msg.get("headers") or [])
                elif msg["type"] == "http.response.body":
                    resp_chunks.append(msg.get("body") or b"")
                    if not msg.get("more_body", False):
                        response_complete.set()

            try:
                await self.app(scope, receive, send)
            finally:
                response_complete.set()
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

    def patch(
        self,
        url: str,
        *,
        json: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> _AsgiResponse:
        return self.request("PATCH", url, json_body=json, headers=headers)

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

            # 1. Bootstrap 检查四大插件清单注册
            boot = client.get("/api/platform/bootstrap").json()
            self.assertTrue(boot["ok"])
            mod_ids = {m["module_id"] for m in boot["modules"]}
            self.assertEqual(mod_ids, {"standard_migrator", "cross_comparator", "ddl_translator", "data_explorer"})

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

    def test_data_explorer_agentic_loop_readonly_block_and_session_lifecycle(self) -> None:
        """端到端验证 v2.2.0：AI 多轮只读探查会话、幻觉写语句实时拦截保护、恶意推荐 SQL 清洗与会话 CRUD."""
        with tempfile.TemporaryDirectory() as tmpdir:
            biz_sqlite = os.path.join(tmpdir, "trade_quality.sqlite")
            with contextlib.closing(sqlite3.connect(biz_sqlite)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE dwd_trade_order (
                        order_id VARCHAR(64),
                        cust_id VARCHAR(32),
                        pay_amt DECIMAL(18,2),
                        order_status VARCHAR(16)
                    );
                    INSERT INTO dwd_trade_order VALUES ('O1001', 'C01', 128.50, 'PAID');
                    INSERT INTO dwd_trade_order VALUES ('O1001', 'C01', 128.50, 'PAID');
                    INSERT INTO dwd_trade_order VALUES ('O1002', NULL, -45.00, 'ABNORMAL');
                    INSERT INTO dwd_trade_order VALUES ('O1003', 'C02', 360.00, 'PAID');
                    """
                )

            ctx = PlatformContext(
                db_path=os.path.join(tmpdir, "dataog_state.db"),
                drivers_dir=os.path.join(tmpdir, "drivers"),
                assets_dir=os.path.join(tmpdir, "assets"),
            )
            app = create_app(ctx=ctx)
            client = TestClient(app)

            conn_resp = client.post(
                "/api/platform/connections",
                json={
                    "name": "交易质量待检库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": biz_sqlite,
                    "auto_refresh_metadata": True,
                },
            ).json()
            self.assertTrue(conn_resp["ok"])
            cid = conn_resp["connection"]["connection_id"]

            # 1. 平台级只读执行端点 /api/platform/query/execute-readonly 放行 SELECT、阻断 DELETE
            ro_ok = client.post(
                "/api/platform/query/execute-readonly",
                json={
                    "connection_id": cid,
                    "sql": "SELECT COUNT(*) AS cnt FROM dwd_trade_order;",
                    "limit": 50,
                },
            )
            self.assertEqual(ro_ok.status_code, 200)
            self.assertEqual(ro_ok.json()["result"]["rows"], [[4]])

            ro_bad = client.post(
                "/api/platform/query/execute-readonly",
                json={
                    "connection_id": cid,
                    "sql": "DELETE FROM dwd_trade_order WHERE pay_amt < 0",
                    "limit": 50,
                },
            )
            self.assertEqual(ro_bad.status_code, 400)

            # 2. 配置默认 AI 服务商
            client.put(
                "/api/platform/llm/providers/deepseek",
                json={
                    "name": "DeepSeek",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "sk-explorer-mock-key-8888",
                    "default_model": "deepseek-chat",
                    "models": ["deepseek-chat"],
                    "enabled": True,
                    "is_default": True,
                },
            )

            # 3. 模拟 3 步 ReAct 探查循环：
            #    Step 1: 正常统计总行数、空值与负数金额
            #    Step 2: 大模型幻觉试图执行 DELETE 清理脏数据 -> 被只读沙箱硬拦截 (status='blocked')
            #    Step 3: 大模型收到拦截错误后输出结论（同时故意夹带非只读 recommended_sql 测试二次清洗）
            llm_step_replies = [
                {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "content": json.dumps(
                        {
                            "action": "query",
                            "thought": "核查订单表总行数、客户号空值数与负向金额记录数",
                            "sql": (
                                "SELECT COUNT(*) AS total_rows, "
                                "SUM(CASE WHEN cust_id IS NULL THEN 1 ELSE 0 END) AS null_cust_cnt, "
                                "SUM(CASE WHEN pay_amt < 0 THEN 1 ELSE 0 END) AS neg_amt_cnt "
                                "FROM dwd_trade_order"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 30.0,
                },
                {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "content": json.dumps(
                        {
                            "action": "query",
                            "thought": "尝试删除负数脏数据（越权幻觉测试）",
                            "sql": "DELETE FROM dwd_trade_order WHERE pay_amt < 0",
                        },
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 25.0,
                },
                {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "content": json.dumps(
                        {
                            "action": "conclude",
                            "summary": "订单表共 4 行记录，发现 1 条客户号缺失且金额为负数的异常记录。",
                            "findings": [
                                {
                                    "severity": "warn",
                                    "title": "存在空客户号与负金额脏数据",
                                    "detail": "O1002 记录 cust_id 为 NULL 且 pay_amt 为 -45.00。",
                                    "evidence_sql": "SELECT * FROM dwd_trade_order WHERE cust_id IS NULL OR pay_amt < 0",
                                }
                            ],
                            "recommended_sql": "UPDATE dwd_trade_order SET pay_amt = 0 WHERE pay_amt < 0",
                        },
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 35.0,
                },
            ]

            with mock.patch.object(ctx.llm_hub, "chat_completion", side_effect=llm_step_replies):
                r_exp = client.post(
                    "/api/modules/data_explorer/explore",
                    json={
                        "connection_id": cid,
                        "schema_name": "main",
                        "focus_tables": ["main.dwd_trade_order"],
                        "prompt": "排查订单表空值与异常负金额脏数据",
                        "goal_type": "dirty",
                        "max_steps": 5,
                        "sample_limit": 100,
                    },
                )
            self.assertEqual(r_exp.status_code, 200, r_exp.text)
            exp_body = r_exp.json()
            self.assertTrue(exp_body["ok"])
            session_id = exp_body["session"]["session_id"]
            assistant_msg = exp_body["assistant_message"]
            steps = assistant_msg["steps"]
            self.assertEqual(len(steps), 2)
            self.assertEqual(steps[0]["status"], "ok")
            self.assertEqual(steps[0]["rows"], [[4, 1, 1]])
            self.assertEqual(steps[1]["status"], "blocked")
            self.assertIn("DELETE", steps[1]["error"].upper())

            # 夹带的恶意 UPDATE recommended_sql 必须被剥离并回退至第 1 步合法的 SELECT SQL
            self.assertTrue(assistant_msg["recommended_sql"].upper().startswith("SELECT"))
            self.assertNotIn("UPDATE", assistant_msg["recommended_sql"].upper())
            self.assertEqual(len(assistant_msg["findings"]), 1)

            # 底层 SQLite 业务表必须保持 4 行分毫无损
            with contextlib.closing(sqlite3.connect(biz_sqlite)) as check_conn:
                remain_cnt = check_conn.execute("SELECT COUNT(*) FROM dwd_trade_order").fetchone()[0]
                self.assertEqual(remain_cnt, 4)

            # 4. 同一 session_id 多轮追问（测试主键重复排查）
            followup_replies = [
                {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "content": json.dumps(
                        {
                            "action": "query",
                            "thought": "检查 order_id 是否存在重复值",
                            "sql": (
                                "SELECT order_id, COUNT(*) AS dup_cnt "
                                "FROM dwd_trade_order GROUP BY order_id HAVING COUNT(*) > 1"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 20.0,
                },
                {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "content": json.dumps(
                        {
                            "action": "conclude",
                            "summary": "订单号 O1001 存在 2 条重复记录。",
                            "findings": [
                                {
                                    "severity": "error",
                                    "title": "主键候选列 order_id 重复",
                                    "detail": "order_id = 'O1001' 重复出现 2 次。",
                                    "evidence_sql": "SELECT * FROM dwd_trade_order WHERE order_id = 'O1001'",
                                }
                            ],
                            "recommended_sql": "SELECT * FROM dwd_trade_order WHERE order_id = 'O1001'",
                        },
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 22.0,
                },
            ]
            with mock.patch.object(ctx.llm_hub, "chat_completion", side_effect=followup_replies):
                r_follow = client.post(
                    "/api/modules/data_explorer/explore",
                    json={
                        "session_id": session_id,
                        "connection_id": cid,
                        "schema_name": "main",
                        "focus_tables": ["main.dwd_trade_order"],
                        "prompt": "继续检查 order_id 是否有重复单号",
                        "goal_type": "dirty",
                    },
                ).json()
            self.assertTrue(r_follow["ok"])
            self.assertEqual(r_follow["assistant_message"]["steps"][0]["rows"], [["O1001", 2]])

            # 5. 会话列表、会话详情（共 4 条消息：2 user + 2 assistant）、重命名与删除闭环
            s_list = client.get(f"/api/modules/data_explorer/sessions?connection_id={cid}").json()
            self.assertEqual(len(s_list["sessions"]), 1)

            r_patch = client.patch(
                f"/api/modules/data_explorer/sessions/{session_id}",
                json={"title": "订单脏数据与重复单号核查"},
            ).json()
            self.assertTrue(r_patch["ok"])
            self.assertEqual(r_patch["session"]["title"], "订单脏数据与重复单号核查")

            s_detail = client.get(f"/api/modules/data_explorer/sessions/{session_id}").json()
            self.assertTrue(s_detail["ok"])
            self.assertEqual(len(s_detail["session"]["messages"]), 4)

            r_del = client.delete(f"/api/modules/data_explorer/sessions/{session_id}").json()
            self.assertTrue(r_del["ok"])
            self.assertEqual(
                client.get(f"/api/modules/data_explorer/sessions/{session_id}").status_code,
                404,
            )

    def test_three_step_standard_governance_bookmarks_templates_and_dock(self) -> None:
        """端到端验证 v2.3.0：macOS 底部 Dock、全流程模板下载、标准字典 -> 字段绑定与落标检查 -> 跨环境拟物书签闭环."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_sqlite = os.path.join(tmpdir, "src_ods.sqlite")
            tgt_sqlite = os.path.join(tmpdir, "tgt_dwd.sqlite")

            with contextlib.closing(sqlite3.connect(src_sqlite)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE ods_cust_acct (
                        cust_no VARCHAR(32),
                        open_dt DATE,
                        bal_amt DECIMAL(18,2),
                        ext_note VARCHAR(100)
                    );
                    """
                )
            with contextlib.closing(sqlite3.connect(tgt_sqlite)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE dwd_cust_acct (
                        CUST_ID VARCHAR(32),
                        open_date VARCHAR(20),
                        bal_amt DECIMAL(10,2),
                        extra_tgt_col VARCHAR(20)
                    );
                    """
                )

            ctx = PlatformContext(
                db_path=os.path.join(tmpdir, "dataog_state.db"),
                drivers_dir=os.path.join(tmpdir, "drivers"),
                assets_dir=os.path.join(tmpdir, "assets"),
            )
            app = create_app(ctx=ctx)
            client = TestClient(app)

            # 1. 验证桌面重构为底部 macOS Dock，不再使用中央图标网格与右下角抽屉
            home_html = client.get("/").text
            self.assertIn('id="os-dock"', home_html)
            self.assertIn('id="dock-hover-zone"', home_html)
            self.assertNotIn('id="desktop-icon-grid"', home_html)
            self.assertNotIn('id="minimized-drawer"', home_html)

            # 2. 创建来源库与目标库连接
            src_cid = client.post(
                "/api/platform/connections",
                json={
                    "name": "来源ODS库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": src_sqlite,
                    "auto_refresh_metadata": True,
                },
            ).json()["connection"]["connection_id"]
            tgt_cid = client.post(
                "/api/platform/connections",
                json={
                    "name": "目标DWD库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": tgt_sqlite,
                    "auto_refresh_metadata": True,
                },
            ).json()["connection"]["connection_id"]

            # 3. 验证全系统全部 Excel 模板接口均可下载且附带「填写说明」工作表
            for tpl_url in (
                "/api/platform/standards/template.xlsx",
                "/api/modules/standard_migrator/bindings/template.xlsx",
                f"/api/modules/standard_migrator/bindings/template.xlsx?connection_id={tgt_cid}&schema_name=main&tables=main.dwd_cust_acct",
                "/api/modules/cross_comparator/pairings/template.xlsx",
                "/api/modules/standard_migrator/checklist/template.xlsx",
            ):
                r_tpl = client.get(tpl_url)
                self.assertEqual(r_tpl.status_code, 200, f"模板下载失败: {tpl_url}")
                wb_t = openpyxl.load_workbook(io.BytesIO(r_tpl.content), data_only=True)
                self.assertIn("填写说明", wb_t.sheetnames, f"{tpl_url} 缺少填写说明工作表")
                wb_t.close()

            # 4. 第 1 步：先下载系统标准字典模板（含 CUST000101 / ACCT000205 / TXN000309 / COM000402）并直接上传导入
            dict_tpl_bytes = client.get("/api/platform/standards/template.xlsx").content
            r_up_dict = client.post(
                "/api/platform/standards/upload",
                files={
                    "file": (
                        "std_dict.xlsx",
                        dict_tpl_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
                data={"mode": "upsert"},
            ).json()
            self.assertTrue(r_up_dict["ok"])
            self.assertGreaterEqual(r_up_dict["imported_count"], 4)

            # 5. 第 2 步：分别给来源库表与目标库表上传字段对照关系，并验证「检查是否落标」与「生成贯标任务」
            # 5a) 来源库绑定：cust_no -> CUST000101, open_dt -> ACCT000205, bal_amt -> TXN000309
            wb_src_bind = openpyxl.Workbook()
            ws_sb = wb_src_bind.active
            ws_sb.append(["模式名", "表名", "物理字段名", "数据项编号"])
            ws_sb.append(["main", "ods_cust_acct", "cust_no", "CUST000101"])
            ws_sb.append(["main", "ods_cust_acct", "open_dt", "ACCT000205"])
            ws_sb.append(["main", "ods_cust_acct", "bal_amt", "TXN000309"])
            buf_sb = io.BytesIO()
            wb_src_bind.save(buf_sb)
            r_sb = client.post(
                "/api/modules/standard_migrator/bindings/upload",
                files={"file": ("src_binds.xlsx", buf_sb.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"connection_id": src_cid, "mode": "merge"},
            ).json()
            self.assertTrue(r_sb["ok"])
            self.assertEqual(r_sb["aggregate"]["bound_cols"], 3)

            # 5b) 目标库绑定：CUST_ID -> CUST000101 (仅缺注释 -> COMMENT_ONLY), open_date -> ACCT000205 (NON_COMPLIANT), bal_amt -> COM000402 (故意绑错数标制造两侧不一致)
            wb_tgt_bind = openpyxl.Workbook()
            ws_tb = wb_tgt_bind.active
            ws_tb.append(["模式名", "表名", "物理字段名", "数据项编号"])
            ws_tb.append(["main", "dwd_cust_acct", "CUST_ID", "CUST000101"])
            ws_tb.append(["main", "dwd_cust_acct", "open_date", "ACCT000205"])
            ws_tb.append(["main", "dwd_cust_acct", "bal_amt", "COM000402"])
            buf_tb = io.BytesIO()
            wb_tgt_bind.save(buf_tb)
            r_tb = client.post(
                "/api/modules/standard_migrator/bindings/upload",
                files={"file": ("tgt_binds.xlsx", buf_tb.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"connection_id": tgt_cid, "mode": "merge", "format_profile": "type_mysql"},
            ).json()
            self.assertTrue(r_tb["ok"])
            cols_by_name = {c["column_name"]: c for c in r_tb["active_table"]["columns"]}
            self.assertEqual(cols_by_name["CUST_ID"]["compliance_status"], "COMMENT_ONLY")
            self.assertEqual(cols_by_name["open_date"]["compliance_status"], "NON_COMPLIANT")
            self.assertEqual(cols_by_name["extra_tgt_col"]["compliance_status"], "UNBOUND")

            # 生成待贯标队列
            r_stage = client.post(
                "/api/modules/standard_migrator/bindings/stage-migration",
                json={
                    "connection_id": tgt_cid,
                    "schema_name": "main",
                    "table_name": "dwd_cust_acct",
                    "column_names": ["open_date"],
                    "format_profile": "type_mysql",
                },
            ).json()
            self.assertTrue(r_stage["ok"])
            self.assertEqual(r_stage["staged_count"], 1)
            self.assertEqual(r_stage["items"][0]["new_col_name"], "OPEN_DT")
            self.assertEqual(r_stage["items"][0]["new_col_type"], "DATE")

            # 6. 第 3 步：跨环境双列比对自动继承第 2 步绑定、同标合拢为淡绿贯通书签、异标断开、拖拽保存与铺底闭环
            client.post("/api/modules/cross_comparator/bind-role", json={"role": "source", "connection_id": src_cid, "auto_refresh": True})
            client.post("/api/modules/cross_comparator/bind-role", json={"role": "target", "connection_id": tgt_cid, "auto_refresh": True})

            r_ov = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_schema=main&source_table=ods_cust_acct"
                "&target_schema=main&target_table=dwd_cust_acct&format_profile=type_mysql"
            ).json()
            self.assertTrue(r_ov["ok"])
            dual_rows = r_ov["active_comparison"]["dual_rows"]
            # 自动按共同 std_code (CUST000101, ACCT000205) 和同名 (bal_amt) 对齐
            by_src = {(r["source_col"]["column_name"] if r["source_col"] else ""): r for r in dual_rows}
            self.assertEqual(by_src["cust_no"]["target_col"]["column_name"], "CUST_ID")
            self.assertEqual(by_src["cust_no"]["bookmark_state"], "UNIFIED")
            self.assertEqual(by_src["open_dt"]["target_col"]["column_name"], "open_date")
            self.assertEqual(by_src["open_dt"]["bookmark_state"], "UNIFIED")
            self.assertTrue(by_src["open_dt"]["target_needs_migration"])
            self.assertEqual(by_src["bal_amt"]["bookmark_state"], "SPLIT_DIFF")

            # 模拟界面拖拽调整与修改书签后保存，验证同步回写第 2 步数据源绑定
            r_save_pair = client.post(
                "/api/modules/cross_comparator/pairings/save",
                json={
                    "std_table_key": "dwd_cust_acct",
                    "source_schema": "main",
                    "source_table": "ods_cust_acct",
                    "target_schema": "main",
                    "target_table": "dwd_cust_acct",
                    "format_profile": "type_mysql",
                    "pairings": [
                        {"source_col": "cust_no", "target_col": "CUST_ID", "source_std_code": "CUST000101", "target_std_code": "CUST000101"},
                        {"source_col": "open_dt", "target_col": "open_date", "source_std_code": "ACCT000205", "target_std_code": "ACCT000205"},
                        {"source_col": "bal_amt", "target_col": "bal_amt", "source_std_code": "TXN000309", "target_std_code": "TXN000309"},
                    ],
                },
            ).json()
            self.assertTrue(r_save_pair["ok"])
            self.assertEqual(r_save_pair["active_comparison"]["bookmark_summary"]["unified"], 3)

            # 第 2 步目标库字段绑定应已同步更新为 TXN000309
            tgt_binds_after = ctx.standard_catalog.get_table_field_bindings(tgt_cid, "main", "dwd_cust_acct")
            self.assertEqual(tgt_binds_after["bal_amt"]["std_code"], "TXN000309")

            # 验证导出跨环境预填映射模板与批量铺底上传
            r_ptpl = client.get("/api/modules/cross_comparator/pairings/template.xlsx?table_key=dwd_cust_acct")
            self.assertEqual(r_ptpl.status_code, 200)
            r_pup = client.post(
                "/api/modules/cross_comparator/pairings/upload",
                files={"file": ("pair_up.xlsx", r_ptpl.content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"format_profile": "type_mysql"},
            ).json()
            self.assertTrue(r_pup["ok"])
            self.assertEqual(r_pup["imported_count"], 5)

            # 从第 3 步一键推送回第 2 步贯标队列（应按标准字典生成 OPEN_DT / TXN_AMT 等目标规则）
            r_push = client.post(
                "/api/modules/cross_comparator/push-to-migrator",
                json={"std_table_key": "dwd_cust_acct", "format_profile": "type_mysql"},
            ).json()
            self.assertTrue(r_push["ok"])
            self.assertEqual(r_push["pushed_count"], 3)

    def test_upload_bindings_progress_tracking_na_empty_and_formula_evaluation(self) -> None:
        """端到端验证：上传对照关系实时进度轮询、Excel 公式取最终结果、#N/A 自动识别为空及历史脏绑定自愈."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_sqlite = os.path.join(tmpdir, "biz_target.sqlite")
            with contextlib.closing(sqlite3.connect(db_sqlite)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE dwd_party_acct (
                        cust_id VARCHAR(32),
                        open_dt DATE,
                        txn_amt DECIMAL(18,2),
                        unmapped_memo VARCHAR(100),
                        legacy_na_col VARCHAR(50)
                    );
                    """
                )

            ctx = PlatformContext(
                db_path=os.path.join(tmpdir, "dataog_state.db"),
                drivers_dir=os.path.join(tmpdir, "drivers"),
                assets_dir=os.path.join(tmpdir, "assets"),
            )
            app = create_app(ctx=ctx)
            client = TestClient(app)

            cid = client.post(
                "/api/platform/connections",
                json={
                    "name": "业务目标库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": db_sqlite,
                    "auto_refresh_metadata": True,
                },
            ).json()["connection"]["connection_id"]

            # 1. 导入系统标准字典模板，并校验服务端进度查询接口
            dict_bytes = client.get("/api/platform/standards/template.xlsx").content
            r_dict = client.post(
                "/api/platform/standards/upload",
                files={"file": ("dict.xlsx", dict_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"mode": "upsert", "progress_id": "imp_std_dict_001"},
            ).json()
            self.assertTrue(r_dict["ok"])
            prog_dict = client.get("/api/platform/import-progress/imp_std_dict_001").json()
            self.assertTrue(prog_dict["found"])
            self.assertEqual(prog_dict["progress"]["status"], "completed")
            self.assertEqual(prog_dict["progress"]["percent"], 100.0)

            # 模拟旧版历史残留：手动在库内注入一条 std_code='#N/A' 的脏绑定记录，验证重新上传 merge 时自动自愈清除
            with ctx.store.module_db() as sconn:
                sconn.execute(
                    """
                    INSERT INTO field_standard_bindings (
                        connection_id, schema_name, table_name, column_name, col_key,
                        std_code, bind_source, compliance_status, compliance_detail, updated_at
                    ) VALUES (?, 'main', 'dwd_party_acct', 'legacy_na_col', 'main::dwd_party_acct::legacy_na_col',
                              '#N/A', 'EXCEL', 'STD_MISSING', '旧版脏数据', '2026-01-01 00:00:00')
                    """,
                    (cid,),
                )

            # 2. 构造真实业务场景的「多 Sheet 公式 + 合并单元格 + #N/A」对照关系工作簿：
            #    Sheet1: 字段与数据标准对照（含 INDEX+MATCH、_xlfn.XLOOKUP、VLOOKUP 返回 #N/A、编号 #N/A 回退英文简称匹配）
            #    Sheet2: 数据字典（模拟银行大表同工作簿挂字典反查场景）
            wb_bind = openpyxl.Workbook()
            ws_b = wb_bind.active
            ws_b.title = "字段与数据标准对照"
            ws_b.append(["模式名", "表名", "物理字段名", "数据项编号", "标准英文简称(选填)", "标准中文名称(选填)"])
            ws_b.append(
                [
                    "main",
                    "dwd_party_acct",
                    "cust_id",
                    "=INDEX('数据字典'!A:A, MATCH(UPPER(C2), '数据字典'!C:C, 0))",
                    "=VLOOKUP(D2, '数据字典'!$A$2:$C$10, 3, FALSE)",
                    "#N/A",
                ]
            )
            ws_b.append(
                [
                    None,  # 合并单元格向下填充
                    None,
                    "open_dt",
                    "#N/A",  # 编号为 #N/A，但提供了标准英文简称 OPEN_DT，应识别 #N/A 为空并自动按 OPEN_DT 匹配 ACCT000205
                    "OPEN_DT",
                    "#VALUE!",
                ]
            )
            ws_b.append(
                [
                    None,
                    None,
                    "txn_amt",
                    "=_xlfn.XLOOKUP(UPPER(C4), '数据字典'!C:C, '数据字典'!A:A)",
                    "#N/A",
                    "#N/A",
                ]
            )
            ws_b.append(
                [
                    None,
                    None,
                    "unmapped_memo",
                    "=VLOOKUP(C5, '数据字典'!C:A, 1, 0)",  # 查找不到返回 #N/A -> 应按未绑标跳过
                    "#N/A",
                    "#REF!",
                ]
            )
            ws_b.append(
                [
                    None,
                    None,
                    "legacy_na_col",
                    "  #N/A  ",  # 仍为 #N/A -> 历史脏绑定也应已被清除，呈现 UNBOUND
                    "",
                    "",
                ]
            )
            ws_b.merge_cells("A2:A6")
            ws_b.merge_cells("B2:B6")

            ws_ref = wb_bind.create_sheet("数据字典")
            ws_ref.append(["数据项编号", "中文名称", "英文简称"])
            ws_ref.append(["CUST000101", "客户统一编号", "CUST_ID"])
            ws_ref.append(["ACCT000205", "账户开户日期", "OPEN_DT"])
            ws_ref.append(["TXN000309", "交易发生金额", "TXN_AMT"])

            buf_b = io.BytesIO()
            wb_bind.save(buf_b)

            r_up = client.post(
                "/api/modules/standard_migrator/bindings/upload",
                files={"file": ("formula_binds.xlsx", buf_b.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={
                    "connection_id": cid,
                    "mode": "merge",
                    "format_profile": "type_mysql",
                    "progress_id": "imp_bind_002",
                },
            ).json()
            self.assertTrue(r_up["ok"], f"上传失败: {r_up}")
            self.assertEqual(r_up["imported_count"], 3)
            self.assertEqual(r_up["unresolved_count"], 2)

            prog_bind = client.get("/api/platform/import-progress/imp_bind_002").json()
            self.assertTrue(prog_bind["found"])
            self.assertEqual(prog_bind["progress"]["status"], "completed")
            self.assertEqual(prog_bind["progress"]["percent"], 100.0)
            self.assertEqual(prog_bind["progress"]["processed"], 3)

            cols = {c["column_name"]: c for c in r_up["active_table"]["columns"]}
            self.assertEqual(cols["cust_id"]["std_code"], "CUST000101")
            self.assertEqual(cols["open_dt"]["std_code"], "ACCT000205")
            self.assertEqual(cols["txn_amt"]["std_code"], "TXN000309")
            # 两个 #N/A 字段必须为 UNBOUND（未绑标），绝不能变成 STD_MISSING（标准缺失 [#N/A]）
            self.assertEqual(cols["unmapped_memo"]["std_code"], "")
            self.assertEqual(cols["unmapped_memo"]["compliance_status"], "UNBOUND")
            self.assertEqual(cols["legacy_na_col"]["std_code"], "")
            self.assertEqual(cols["legacy_na_col"]["compliance_status"], "UNBOUND")

            # 3. 验证全表均为 #N/A 时返回清晰错误提示且进度状态置为 failed
            wb_all_na = openpyxl.Workbook()
            ws_na = wb_all_na.active
            ws_na.append(["模式名", "表名", "物理字段名", "数据项编号"])
            ws_na.append(["main", "dwd_party_acct", "unmapped_memo", "#N/A"])
            buf_na = io.BytesIO()
            wb_all_na.save(buf_na)
            r_fail = client.post(
                "/api/modules/standard_migrator/bindings/upload",
                files={"file": ("all_na.xlsx", buf_na.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"connection_id": cid, "mode": "merge", "progress_id": "imp_fail_003"},
            )
            self.assertEqual(r_fail.status_code, 400)
            self.assertIn("#N/A", r_fail.json()["error"])
            prog_fail = client.get("/api/platform/import-progress/imp_fail_003").json()
            self.assertEqual(prog_fail["progress"]["status"], "failed")

            # 4. 验证第 3 步跨环境铺底上传同样支持公式最终结果与 #N/A 置空
            client.post("/api/modules/cross_comparator/bind-role", json={"role": "source", "connection_id": cid})
            client.post("/api/modules/cross_comparator/bind-role", json={"role": "target", "connection_id": cid})
            wb_pair = openpyxl.Workbook()
            ws_p = wb_pair.active
            ws_p.append(["来源模式名", "来源表名", "来源字段名", "来源数据项编号", "目标模式名", "目标表名", "目标字段名", "目标数据项编号"])
            ws_p.append(["main", "dwd_party_acct", "cust_id", '=UPPER("cust000101")', "main", "dwd_party_acct", "cust_id", "=D2"])
            ws_p.append(["main", "dwd_party_acct", "unmapped_memo", "#N/A", "main", "dwd_party_acct", "#N/A", "#N/A"])
            buf_p = io.BytesIO()
            wb_pair.save(buf_p)
            r_pair = client.post(
                "/api/modules/cross_comparator/pairings/upload",
                files={"file": ("pair_formula.xlsx", buf_p.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"format_profile": "type_mysql", "progress_id": "imp_pair_004"},
            ).json()
            self.assertTrue(r_pair["ok"])
            self.assertEqual(r_pair["imported_count"], 2)
            p_rows = {
                (r["source_col"]["column_name"] if r["source_col"] else ""): r
                for r in r_pair["active_comparison"]["dual_rows"]
            }
            self.assertEqual(p_rows["cust_id"]["source_std_code"], "CUST000101")
            self.assertEqual(p_rows["cust_id"]["target_std_code"], "CUST000101")
            self.assertEqual(p_rows["unmapped_memo"]["source_std_code"], "")
            self.assertIsNone(p_rows["unmapped_memo"]["target_col"])

            # 5. 验证目标数据库离线/未缓存探查异常熔断保护：对照绑定依然成功入库并返回 warning 而非卡死报错
            offline_cid = client.post(
                "/api/platform/connections",
                json={
                    "name": "离线模拟库",
                    "driver_track": "NATIVE",
                    "db_type": "PGSQL",
                    "host": "127.0.0.1",
                    "port": 1,
                    "dbname": "unreachable_db",
                    "user": "u",
                    "password": "p",
                    "auto_refresh_metadata": False,
                },
            ).json()["connection"]["connection_id"]
            wb_off = openpyxl.Workbook()
            ws_off = wb_off.active
            ws_off.append(["模式名", "表名", "物理字段名", "数据项编号"])
            ws_off.append(["public", "t_off_1", "cust_id", "CUST000101"])
            ws_off.append(["public", "t_off_2", "open_dt", "ACCT000205"])
            buf_off = io.BytesIO()
            wb_off.save(buf_off)
            r_off = client.post(
                "/api/modules/standard_migrator/bindings/upload",
                files={"file": ("off.xlsx", buf_off.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"connection_id": offline_cid, "mode": "merge"},
            ).json()
            self.assertTrue(r_off["ok"])
            self.assertEqual(r_off["imported_count"], 2)
            self.assertIn("warning", r_off)
            saved_off = ctx.standard_catalog.get_table_field_bindings(offline_cid, "public", "t_off_1")
            self.assertEqual(saved_off["cust_id"]["std_code"], "CUST000101")

            # 6. 回归验证：
            #    a) 当数据项编号与英文简称均为 #N/A、仅填了不存在的标准中文名称时，绝不因物理列名同名而误绑其他标准；
            #    b) 跨环境比对历史残留的全角/引号 #N/A 脏记录自动自愈置空，不误报「字典缺失」。
            wb_guard = openpyxl.Workbook()
            ws_g = wb_guard.active
            ws_g.append(["模式名", "表名", "物理字段名", "数据项编号", "标准英文简称(选填)", "标准中文名称(选填)"])
            ws_g.append(["main", "dwd_party_acct", "open_dt", "ACCT000205", "#N/A", "#N/A"])
            ws_g.append(["main", "dwd_party_acct", "cust_id", "#N/A", "#N/A", "不存在的虚构标准中文名"])
            buf_g = io.BytesIO()
            wb_guard.save(buf_g)
            r_guard = client.post(
                "/api/modules/standard_migrator/bindings/upload",
                files={"file": ("guard.xlsx", buf_g.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"connection_id": cid, "mode": "replace_table"},
            ).json()
            self.assertTrue(r_guard["ok"])
            self.assertEqual(r_guard["imported_count"], 1)
            self.assertEqual(r_guard["unresolved_count"], 1)
            g_cols = {c["column_name"]: c for c in r_guard["active_table"]["columns"]}
            self.assertEqual(g_cols["cust_id"]["std_code"], "")
            self.assertEqual(g_cols["cust_id"]["compliance_status"], "UNBOUND")
            self.assertEqual(g_cols["open_dt"]["std_code"], "ACCT000205")

            # 注入跨环境全角/引号 #N/A 脏数据并验证读取与自愈清洗
            with ctx.store.module_db() as sconn:
                sconn.execute(
                    "UPDATE cross_column_bindings SET source_std_code = '＃Ｎ／Ａ', target_std_code = '''#N/A' WHERE std_table_key = 'dwd_party_acct'"
                )
            ov_clean = client.get("/api/modules/cross_comparator/overview?table_key=dwd_party_acct").json()
            for dr in ov_clean["active_comparison"]["dual_rows"]:
                self.assertNotIn("#", dr["source_std_code"])
                self.assertNotIn("＃", dr["source_std_code"])
                self.assertNotIn("#", dr["target_std_code"])

            # 7. 验证小批量（<400 行）工作簿在标准字典解析与跨环境标准上传中均上报首行与中间阶段进度，且错误路径同步标记进度失败
            dict_stages: List[Tuple[float, str]] = []
            ctx.standard_catalog.import_dictionary_excel(
                "small_dict.xlsx",
                dict_bytes,
                mode="upsert",
                progress_cb=lambda pct, stg, _dtl, _proc, _tot: dict_stages.append((pct, stg)),
            )
            dict_stage_names = [s for _, s in dict_stages]
            self.assertIn("解析数据标准条目", dict_stage_names)
            self.assertEqual(dict_stages, sorted(dict_stages, key=lambda x: x[0]))

            wb_cross_std = openpyxl.Workbook()
            ws_cs = wb_cross_std.active
            ws_cs.title = "贯标清单"
            ws_cs.append(["模式名", "表名", "修改前字段名", "修改前类型", "修改前注释", "修改后字段名", "修改后类型", "修改后注释"])
            ws_cs.append(["main", "dwd_party_acct", "cust_id", "varchar(32)", "#N/A", '=LOWER("CUST_ID")', "VARCHAR(32)", "客户统一编号"])
            ws_cs.append(["main", "dwd_party_acct", "open_dt", "date", "开户日", "open_dt", "DATE", "#N/A"])
            buf_cs = io.BytesIO()
            wb_cross_std.save(buf_cs)

            cross_stages: List[Tuple[float, str]] = []
            from modules.cross_comparator.service import CrossComparatorService

            CrossComparatorService(ctx).import_standard_or_bundle(
                "small_cross_std.xlsx",
                buf_cs.getvalue(),
                progress_cb=lambda pct, stg, _dtl, _proc, _tot: cross_stages.append((pct, stg)),
            )
            cross_stage_names = [s for _, s in cross_stages]
            self.assertIn("解析跨环境标准清单与公式结果", cross_stage_names)
            self.assertIn("保存标准清单", cross_stage_names)

            exported_cross_xlsx = client.get("/api/modules/cross_comparator/bindings/export.xlsx").content
            bundle_stages: List[Tuple[float, str]] = []
            CrossComparatorService(ctx).import_standard_or_bundle(
                "exported_bundle.xlsx",
                exported_cross_xlsx,
                progress_cb=lambda pct, stg, _dtl, _proc, _tot: bundle_stages.append((pct, stg)),
            )
            bundle_stage_names = [s for _, s in bundle_stages]
            self.assertIn("解析跨环境标准映射", bundle_stage_names)

            wb_all_na = openpyxl.Workbook()
            ws_na = wb_all_na.active
            ws_na.append(["模式名", "表名", "物理字段名", "数据项编号"])
            ws_na.append(["main", "dwd_party_acct", "cust_id", "#N/A"])
            buf_na = io.BytesIO()
            wb_all_na.save(buf_na)
            r_all_na = client.post(
                "/api/modules/standard_migrator/bindings/upload",
                files={"file": ("all_na.xlsx", buf_na.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"connection_id": cid, "mode": "merge", "progress_id": "imp_fail_all_na"},
            ).json()
            self.assertFalse(r_all_na["ok"])
            prog_fail = client.get("/api/platform/import-progress/imp_fail_all_na").json()
            self.assertTrue(prog_fail["found"])
            self.assertEqual(prog_fail["progress"]["status"], "failed")

            # 8. 回归验证：
            #    a) 第三方流式导出的 <dimension ref="A1:A1"/> 对照表读取阶段进度平滑递增而非首行跳顶冻结；
            #    b) 跨环境多表批量铺底上传仅在末尾拉取一次当前激活表概览，避免 N 张表触发 2*N 次冗余元数据探查。
            wb_dim = openpyxl.Workbook(write_only=True)
            ws_dim = wb_dim.create_sheet("对照")
            ws_dim.append(["模式名", "表名", "物理字段名", "数据项编号"])
            for i_col in range(1, 221):
                ws_dim.append(["main", "dwd_party_acct", f"col_{i_col}", "CUST000101"])
            buf_dim = io.BytesIO()
            wb_dim.save(buf_dim)
            buf_dim_a1 = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(buf_dim.getvalue()), "r") as zin, zipfile.ZipFile(
                buf_dim_a1, "w"
            ) as zout:
                for zinfo in zin.infolist():
                    zdata = zin.read(zinfo.filename)
                    if zinfo.filename == "xl/worksheets/sheet1.xml":
                        zdata = re.sub(rb'<dimension\s+ref="[^"]*"\s*/>', b'<dimension ref="A1:A1"/>', zdata)
                        if b"<dimension" not in zdata:
                            zdata = zdata.replace(b"<sheetData>", b'<dimension ref="A1:A1"/><sheetData>', 1)
                    zout.writestr(zinfo, zdata)

            from modules.standard_migrator.service import StandardMigratorService

            dim_pcts: List[float] = []
            StandardMigratorService(ctx).upload_field_bindings_excel(
                filename="easyexcel_a1.xlsx",
                content=buf_dim_a1.getvalue(),
                connection_id=cid,
                progress_cb=lambda pct, stg, _dtl, _pr, _tot: dim_pcts.append(round(pct, 1))
                if "读取工作表" in stg
                else None,
            )
            self.assertGreaterEqual(len(dim_pcts), 3)
            self.assertLess(dim_pcts[0], 18.0)
            self.assertEqual(dim_pcts[-1], 36.0)
            self.assertEqual(dim_pcts, sorted(dim_pcts))

            wb_many = openpyxl.Workbook()
            ws_many = wb_many.active
            ws_many.append(["来源模式名", "来源表名", "来源字段名", "来源数据项编号", "目标模式名", "目标表名", "目标字段名", "目标数据项编号"])
            for t_idx in range(1, 13):
                ws_many.append(["public", f"src_tbl_{t_idx}", "c1", "CUST000101", "public", f"tgt_tbl_{t_idx}", "c1", "CUST000101"])
            buf_many = io.BytesIO()
            wb_many.save(buf_many)

            orig_get_meta = ctx.metadata_hub.get_table_metadata
            meta_probe_calls = 0

            def _counting_get_meta(*args, **kwargs):
                nonlocal meta_probe_calls
                meta_probe_calls += 1
                return orig_get_meta(*args, **kwargs)

            ctx.metadata_hub.get_table_metadata = _counting_get_meta
            try:
                res_many = CrossComparatorService(ctx).upload_pairings_excel(
                    filename="many_pairs.xlsx",
                    content=buf_many.getvalue(),
                )
            finally:
                ctx.metadata_hub.get_table_metadata = orig_get_meta
            self.assertEqual(res_many["imported_count"], 12)
            self.assertLessEqual(meta_probe_calls, 2)

            # 9. 回归验证 4631 行 × 160 张表大规模对照关系导入：
            #    - 「保存对照关系」阶段单事务分块上报实时进度（绝不卡死在 62%）；
            #    - replace_table 模式下同名大小写混合表（TBL_001 / tbl_001）不互相覆盖丢失；
            #    - 模式名留空 + SQLite 默认 public 连接仍自动对齐 main 表并完成落标核验；
            #    - 冷缓存仅打开 <= 1 次目标库连接，热缓存 0 次目标库连接，本地状态库连接数 <= 25；
            #    - 跨环境 4631 行铺底导入同样走单事务批量写入。
            prog_resp_hdr = client.get("/api/platform/import-progress/imp_bind_002")
            self.assertIn("no-store", prog_resp_hdr.headers.get("cache-control", ""))

            big_sq_path = os.path.join(tmpdir, "big_160_tables.db")
            with contextlib.closing(sqlite3.connect(big_sq_path)) as bconn:
                for t_i in range(1, 161):
                    col_defs = ", ".join(f"c_{c_i} VARCHAR(32)" for c_i in range(1, 30))
                    bconn.execute(f"CREATE TABLE tbl_{t_i:03d} ({col_defs})")
                bconn.commit()

            big_cid = client.post(
                "/api/platform/connections",
                json={
                    "name": "大规模4631行压测库",
                    "driver_track": "NATIVE",
                    "db_type": "SQLITE",
                    "sqlite_path": big_sq_path,
                    "auto_refresh_metadata": False,
                },
            ).json()["connection"]["connection_id"]

            wb_4631 = openpyxl.Workbook(write_only=True)
            ws_4631 = wb_4631.create_sheet("字段对照")
            ws_4631.append(["模式名", "表名", "物理字段名", "数据项编号"])
            written_4631 = 0
            # 前 2 行测试同表大小写混合（TBL_001 与 tbl_001）在 replace_table 下不互相抹除
            ws_4631.append(["", "TBL_001", "c_1", "CUST000101"])
            ws_4631.append(["", "tbl_001", "c_2", "cust000101"])
            # 第 3 行测试带引号 schema.table 拆分清洗
            ws_4631.append(["", '"main"."tbl_002"', "c_1", "ACCT000205"])
            written_4631 += 3

            for t_i in range(1, 161):
                start_c = 3 if t_i == 1 else (2 if t_i == 2 else 1)
                for c_i in range(start_c, 30):
                    if written_4631 >= 4631:
                        break
                    if written_4631 % 500 == 0:
                        code_val = "#N/A"
                    elif written_4631 % 333 == 0:
                        code_val = "MISSING_STD_999"
                    else:
                        code_val = "CUST000101"
                    ws_4631.append(["", f"tbl_{t_i:03d}", f"c_{c_i}", code_val])
                    written_4631 += 1
                if written_4631 >= 4631:
                    break
            self.assertEqual(written_4631, 4631)

            buf_4631 = io.BytesIO()
            wb_4631.save(buf_4631)

            orig_store_conn = ctx.store._get_conn
            orig_drv_open = ctx.driver_manager.open_connection
            conn_counters = {"state_db": 0, "target_db": 0}

            def _count_state_conn():
                conn_counters["state_db"] += 1
                return orig_store_conn()

            def _count_target_open(*a, **kw):
                conn_counters["target_db"] += 1
                return orig_drv_open(*a, **kw)

            persist_progress: List[Tuple[float, int, int]] = []
            all_pcts_4631: List[float] = []

            def _track_4631(pct: float, stg: str, _dtl: str, proc: int, tot: int) -> None:
                all_pcts_4631.append(round(pct, 2))
                if stg == "保存对照关系":
                    persist_progress.append((round(pct, 2), proc, tot))

            ctx.store._get_conn = _count_state_conn
            ctx.driver_manager.open_connection = _count_target_open
            try:
                res_4631 = StandardMigratorService(ctx).upload_field_bindings_excel(
                    filename="bindings_4631.xlsx",
                    content=buf_4631.getvalue(),
                    connection_id=big_cid,
                    mode="replace_table",
                    format_profile="type_mysql",
                    progress_cb=_track_4631,
                )
                cold_state_conns = conn_counters["state_db"]
                cold_target_conns = conn_counters["target_db"]

                conn_counters["state_db"] = 0
                conn_counters["target_db"] = 0
                res_4631_warm = StandardMigratorService(ctx).upload_field_bindings_excel(
                    filename="bindings_4631.xlsx",
                    content=buf_4631.getvalue(),
                    connection_id=big_cid,
                    mode="merge",
                    format_profile="type_mysql",
                )
                warm_state_conns = conn_counters["state_db"]
                warm_target_conns = conn_counters["target_db"]
            finally:
                ctx.store._get_conn = orig_store_conn
                ctx.driver_manager.open_connection = orig_drv_open

            self.assertTrue(res_4631["ok"])
            self.assertEqual(res_4631["imported_count"] + res_4631["unresolved_count"], 4631)
            self.assertEqual(res_4631["aggregate"]["checked_tables"], 160)
            self.assertEqual(res_4631["aggregate"]["bound_cols"], res_4631["imported_count"])
            self.assertGreaterEqual(len(persist_progress), 8)
            self.assertEqual(all_pcts_4631, sorted(all_pcts_4631))
            self.assertLessEqual(cold_target_conns, 1)
            self.assertLessEqual(cold_state_conns, 25)
            self.assertEqual(warm_target_conns, 0)
            self.assertLessEqual(warm_state_conns, 25)
            self.assertEqual(res_4631_warm["imported_count"], res_4631["imported_count"])

            # 验证 TBL_001 与 tbl_001 在 replace_table 下均完整保留，且带引号拆分的 tbl_002 正常入库
            t1_binds = ctx.standard_catalog.get_table_field_bindings(big_cid, "main", "tbl_001")
            self.assertIn("c_1", t1_binds)
            self.assertIn("c_2", t1_binds)
            t2_binds = ctx.standard_catalog.get_table_field_bindings(big_cid, "main", "tbl_002")
            self.assertEqual(t2_binds["c_1"]["std_code"], "ACCT000205")

            # 验证跨环境 4631 行铺底导入同样使用单事务批量写入
            client.post("/api/modules/cross_comparator/bind-role", json={"role": "source", "connection_id": big_cid})
            client.post("/api/modules/cross_comparator/bind-role", json={"role": "target", "connection_id": big_cid})
            wb_p4631 = openpyxl.Workbook(write_only=True)
            ws_p4631 = wb_p4631.create_sheet("跨环境映射")
            ws_p4631.append(["来源模式名", "来源表名", "来源字段名", "来源数据项编号", "目标模式名", "目标表名", "目标字段名", "目标数据项编号"])
            p_cnt = 0
            for t_i in range(1, 161):
                for c_i in range(1, 30):
                    if p_cnt >= 4631:
                        break
                    ws_p4631.append(["main", f"tbl_{t_i:03d}", f"c_{c_i}", "CUST000101", "main", f"tbl_{t_i:03d}", f"c_{c_i}", "CUST000101"])
                    p_cnt += 1
                if p_cnt >= 4631:
                    break
            buf_p4631 = io.BytesIO()
            wb_p4631.save(buf_p4631)

            conn_counters["state_db"] = 0
            pair_pcts: List[float] = []
            ctx.store._get_conn = _count_state_conn
            try:
                res_p4631 = CrossComparatorService(ctx).upload_pairings_excel(
                    filename="pairings_4631.xlsx",
                    content=buf_p4631.getvalue(),
                    format_profile="type_mysql",
                    progress_cb=lambda pct, _s, _d, _pr, _tot: pair_pcts.append(round(pct, 2)),
                )
                pair_state_conns = conn_counters["state_db"]
            finally:
                ctx.store._get_conn = orig_store_conn

            self.assertEqual(res_p4631["imported_count"], 4631)
            self.assertEqual(pair_pcts, sorted(pair_pcts))
            # 160 张表批量写入 + 1 次末尾概览渲染，状态库总连接数恒定 O(1)（旧版逐表循环高达 1400+ 次）
            self.assertLessEqual(pair_state_conns, 50)

            # 10. 深度回归验证：
            #     a) 未填模式名的 DWS 对照表上传自动对齐真实 Schema (dwd)，左树徽标、点击预览与热缓存零重连全通；
            #     b) 误传纯字段清单（无数据项编号/标准列）在 replace_table 模式下拒绝导入，绝不误删既有绑定；
            #     c) 大批量无编号行依赖英文简称/中文名称匹配保持单连接 O(1) 索引查询；
            #     d) 跨环境铺底表首行为仅目标单侧字段时，后续配对行的真实来源表名不被覆盖错绑；
            #     e) 单库贯标清单多表上传核验同样复用单连接批量元数据探查。
            dws_unqual_cid = client.post(
                "/api/platform/connections",
                json={
                    "name": "模拟DWS无模式名上传库",
                    "driver_track": "NATIVE",
                    "db_type": "DWS",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "dbname": "dw",
                    "user": "u",
                    "password": "p",
                    "auto_refresh_metadata": False,
                },
            ).json()["connection"]["connection_id"]

            dws_unqual_opens = {"cnt": 0}

            cust_std = ctx.standard_catalog.get_item("CUST000101") or {}
            cust_dws_type = cust_std.get("type_dws") or "VARCHAR(32)"
            cust_cn_name = cust_std.get("cn_name") or "客户统一编号"

            class _MockDwsUnqualCur:
                def __enter__(s):
                    return s

                def __exit__(s, *a):
                    return False

                def execute(s, sql: str, params=None) -> None:
                    q = " ".join(sql.split())
                    if "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n" in q:
                        s.rows = [
                            (301, "dwd", "cust_info", "客户表", "orientation=row", "r"),
                            (302, "dwd", "acct_info", "账户表", "orientation=row", "r"),
                        ]
                    elif "JOIN pg_catalog.pg_attribute" in q:
                        s.rows = (
                            [
                                (301, "cust_id", cust_dws_type, True, None, cust_cn_name),
                                (302, "open_dt", "date", False, None, "账户开立日期"),
                            ]
                            if "c.oid," in q
                            else [("cust_id", cust_dws_type, True, None, cust_cn_name)]
                        )
                    else:
                        s.rows = []

                def fetchall(s):
                    return list(s.rows)

                def fetchone(s):
                    return s.rows[0] if s.rows else None

            class _MockDwsUnqualConn:
                autocommit = False

                def cursor(s):
                    return _MockDwsUnqualCur()

                def close(s):
                    pass

            def _open_dws_unqual(prof_obj, *a, **kw):
                if prof_obj.connection_id == dws_unqual_cid:
                    dws_unqual_opens["cnt"] += 1
                    return _MockDwsUnqualConn()
                return orig_drv_open(prof_obj, *a, **kw)

            wb_unqual = openpyxl.Workbook()
            ws_uq = wb_unqual.active
            ws_uq.append(["表名", "物理字段名", "数据项编号"])
            ws_uq.append(["cust_info", "cust_id", "CUST000101"])
            ws_uq.append(["acct_info", "open_dt", "ACCT000205"])
            buf_uq = io.BytesIO()
            wb_unqual.save(buf_uq)

            ctx.driver_manager.open_connection = _open_dws_unqual
            try:
                r_uq1 = StandardMigratorService(ctx).upload_field_bindings_excel(
                    filename="unqual_dws.xlsx",
                    content=buf_uq.getvalue(),
                    connection_id=dws_unqual_cid,
                )
                self.assertIn("dwd.cust_info", r_uq1["table_stats"])
                self.assertIn("dwd.acct_info", r_uq1["table_stats"])
                tc_dwd = StandardMigratorService(ctx).get_table_compliance(
                    dws_unqual_cid, "dwd", "cust_info"
                )
                self.assertEqual(tc_dwd["columns"][0]["std_code"], "CUST000101")
                self.assertEqual(tc_dwd["columns"][0]["compliance_status"], "COMPLIANT")

                dws_unqual_opens["cnt"] = 0
                StandardMigratorService(ctx).upload_field_bindings_excel(
                    filename="unqual_dws.xlsx",
                    content=buf_uq.getvalue(),
                    connection_id=dws_unqual_cid,
                )
                self.assertEqual(dws_unqual_opens["cnt"], 0)
            finally:
                ctx.driver_manager.open_connection = orig_drv_open

            # 10b) 误传无标准列的普通表结构工作簿在 replace_table 下抛出明确错误且原绑定零丢失
            wb_no_std = openpyxl.Workbook()
            ws_ns = wb_no_std.active
            ws_ns.append(["模式名", "表名", "物理字段名", "数据类型"])
            ws_ns.append(["dwd", "cust_info", "cust_id", "varchar(32)"])
            buf_ns = io.BytesIO()
            wb_no_std.save(buf_ns)
            with self.assertRaises(ValueError):
                StandardMigratorService(ctx).upload_field_bindings_excel(
                    filename="no_std_cols.xlsx",
                    content=buf_ns.getvalue(),
                    connection_id=dws_unqual_cid,
                    mode="replace_table",
                )
            self.assertEqual(
                ctx.standard_catalog.get_table_field_bindings(dws_unqual_cid, "dwd", "cust_info")["cust_id"]["std_code"],
                "CUST000101",
            )

            # 10c) 600 行无编号不同候选标准名称匹配保持单连接 O(1)
            with ctx.store.module_db() as sconn:
                sconn.executemany(
                    "INSERT OR REPLACE INTO standard_dictionary (std_code, cn_name, en_abbr, is_standard_dict, updated_at) VALUES (?, ?, ?, '1', '2026-01-01')",
                    [(f"FZ_{i:04d}", f"候选中文_{i}", f"FZ_COL_{i}") for i in range(600)],
                )
            wb_fz = openpyxl.Workbook(write_only=True)
            ws_fz = wb_fz.create_sheet("候选匹配")
            ws_fz.append(["表名", "物理字段名", "数据项编号", "标准英文简称(选填)", "标准中文名称(选填)"])
            for i in range(600):
                ws_fz.append([f"tbl_{(i // 20) + 1:03d}", f"c_{(i % 20) + 1}", "#N/A", f"FZ_COL_{i}", f"候选中文_{i}"])
            buf_fz = io.BytesIO()
            wb_fz.save(buf_fz)

            conn_counters["state_db"] = 0
            ctx.store._get_conn = _count_state_conn
            try:
                res_fz = StandardMigratorService(ctx).upload_field_bindings_excel(
                    filename="fuzzy_600.xlsx",
                    content=buf_fz.getvalue(),
                    connection_id=big_cid,
                )
                fz_conns = conn_counters["state_db"]
            finally:
                ctx.store._get_conn = orig_store_conn
            self.assertEqual(res_fz["imported_count"], 600)
            self.assertLessEqual(fz_conns, 25)

            # 10d) 跨环境铺底表首行为单侧目标字段时，后续配对行的真实来源表名正确同步
            wb_unp = openpyxl.Workbook()
            ws_unp = wb_unp.active
            ws_unp.append(["来源模式名", "来源表名", "来源字段名", "来源数据项编号", "目标模式名", "目标表名", "目标字段名", "目标数据项编号"])
            ws_unp.append(["", "", "", "", "dwd", "dwd_unp_test", "etl_ts", ""])
            ws_unp.append(["ods", "ods_unp_real", "src_cust", "CUST000101", "dwd", "dwd_unp_test", "cust_id", "CUST000101"])
            buf_unp = io.BytesIO()
            wb_unp.save(buf_unp)
            ctx.store.save_meta("cross_source_connection_id", dws_unqual_cid)
            CrossComparatorService(ctx).upload_pairings_excel(
                filename="unp.xlsx",
                content=buf_unp.getvalue(),
            )
            self.assertEqual(
                ctx.standard_catalog.get_table_field_bindings(dws_unqual_cid, "ods", "ods_unp_real")["src_cust"]["std_code"],
                "CUST000101",
            )

            # 10e) 单库贯标清单多表上传核验同样保持单次目标库连接
            StandardMigratorService(ctx).bind_connection(big_cid, auto_sync=False)
            wb_chk = openpyxl.Workbook()
            ws_chk = wb_chk.active
            ws_chk.append(["模式名", "表名", "修改前字段名", "修改后字段名", "修改后类型", "修改后注释"])
            for t_i in range(1, 21):
                ws_chk.append(["main", f"tbl_{t_i:03d}", "c_1", "cust_id", "VARCHAR(32)", "客户号"])
            buf_chk = io.BytesIO()
            wb_chk.save(buf_chk)
            conn_counters["target_db"] = 0
            ctx.driver_manager.open_connection = _count_target_open
            try:
                res_chk = StandardMigratorService(ctx).upload_excel_checklist(
                    "multi_chk.xlsx",
                    buf_chk.getvalue(),
                )
                chk_target_conns = conn_counters["target_db"]
            finally:
                ctx.driver_manager.open_connection = orig_drv_open
            self.assertTrue(res_chk["ok"])
            self.assertLessEqual(chk_target_conns, 1)


if __name__ == "__main__":
    unittest.main()
