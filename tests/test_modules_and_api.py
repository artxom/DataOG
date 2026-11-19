"""DataOG v2.0.0 FastAPI 微内核路由、三大独立插件模块及架构安全红线集成测试."""

from __future__ import annotations

import ast
import asyncio
import base64
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
import shutil
import sqlite3
import subprocess
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
        self._pairs = list(pairs)
        self._map: Dict[str, str] = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in pairs
        }

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._map.get(key.lower(), default)

    def get_all(self, key: str) -> List[str]:
        lk = key.lower()
        return [v.decode("latin-1") for k, v in self._pairs if k.decode("latin-1").lower() == lk]

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


_TEST_LICENSE_SEED = b"DataOG-UnitTest-Ed25519-Seed-001"


class TestClient:
    """零第三方 HTTP 依赖的标准库内存 ASGI 测试客户端（免打包 httpx / httpcore / certifi）."""

    __test__ = False

    def __init__(self, app: Any, *, auto_activate: bool = True) -> None:
        self.app = app
        self._cookies: Dict[str, str] = {}
        if auto_activate:
            self._ensure_activated()

    def _ensure_activated(self) -> None:
        ctx = getattr(getattr(self.app, "state", None), "platform_ctx", None)
        if ctx is None:
            return
        lic_hub = getattr(ctx, "license_hub", None)
        if lic_hub is None:
            return
        if lic_hub.get_auth_status()["authorized"]:
            return
        import datetime
        from core.platform.license_hub import (
            SCOPE_GLOBAL,
            ed25519_derive_pubkey,
            issue_activation_code_for_seed,
        )

        test_pub = ed25519_derive_pubkey(_TEST_LICENSE_SEED)
        lic_hub._bind_test_pubkey(test_pub)
        code = issue_activation_code_for_seed(
            _TEST_LICENSE_SEED,
            scope=SCOPE_GLOBAL,
            duration_days=90,
            expiry_date=datetime.date.today() + datetime.timedelta(days=89),
        )
        res = self.post("/api/platform/auth/activate", json={"activation_code": code})
        if res.status_code != 200:
            raise RuntimeError(f"TestClient 自动激活失败: {res.status_code} {res.text}")

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
        if self._cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
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
            resp = _AsgiResponse(status_code, resp_headers, b"".join(resp_chunks))
            for sc in resp.headers.get_all("set-cookie"):
                first_part = sc.split(";", 1)[0].strip()
                if "=" not in first_part:
                    continue
                ck_name, ck_val = first_part.split("=", 1)
                if "max-age=0" in sc.lower() or not ck_val:
                    self._cookies.pop(ck_name.strip(), None)
                else:
                    self._cookies[ck_name.strip()] = ck_val.strip()
            return resp

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

            r_ico = client.get("/dataog.ico")
            self.assertEqual(r_ico.status_code, 200)
            self.assertEqual(r_ico.content[:6], b"\x00\x00\x01\x00\x07\x00")
            if not getattr(sys, "frozen", False):
                build_script_path = os.path.join(os.path.dirname(resolve_static_dir()), "build_exe.py")
                with open(build_script_path, encoding="utf-8") as bf:
                    build_src = bf.read()
                self.assertIn("dataog.ico", build_src)
                self.assertIn("--icon=", build_src)

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

    def test_per_db_standard_format_reuse_and_five_col_pairing_flow(self) -> None:
        """验证 R3 按库标准格式持久化与跨模块沿用，以及 R1 五虚拟列分列绑标配对闭环."""
        with tempfile.TemporaryDirectory() as tmp:
            state_db = os.path.join(tmp, "state.db")
            src_db = os.path.join(tmp, "src.sqlite")
            tgt_db = os.path.join(tmp, "tgt.sqlite")

            with contextlib.closing(sqlite3.connect(src_db)) as sc:
                sc.execute(
                    "CREATE TABLE ods_cust (cust_id TEXT, raw_extra TEXT)"
                )
                sc.execute(
                    "CREATE TABLE ods_cust_v2 (new_col_a TEXT, new_col_b TEXT)"
                )
                sc.commit()
            with contextlib.closing(sqlite3.connect(tgt_db)) as tc:
                tc.execute(
                    "CREATE TABLE dwd_cust (cust_no TEXT, tgt_extra TEXT)"
                )
                tc.execute(
                    "CREATE TABLE dwd_cust_v2 (tgt_new_a TEXT)"
                )
                tc.commit()

            app = create_app(
                db_path=state_db,
            )
            ctx: PlatformContext = app.state.platform_ctx
            client = TestClient(app)

            ctx.standard_catalog.upsert_item(
                {
                    "std_code": "STD001",
                    "cn_name": "客户编号",
                    "en_abbr": "cust_id",
                    "type_dws": "VARCHAR(32)",
                    "type_dw_mart": "VARCHAR(64)",
                    "type_argodb": "STRING",
                }
            )

            r_src = client.post(
                "/api/platform/connections",
                json={
                    "name": "源Argo库",
                    "db_type": "SQLITE",
                    "sqlite_path": src_db,
                    "auto_refresh_metadata": True,
                },
            )
            self.assertEqual(r_src.status_code, 200)
            src_cid = r_src.json()["connection"]["connection_id"]

            r_tgt = client.post(
                "/api/platform/connections",
                json={
                    "name": "目标集市库",
                    "db_type": "SQLITE",
                    "sqlite_path": tgt_db,
                    "auto_refresh_metadata": True,
                },
            )
            self.assertEqual(r_tgt.status_code, 200)
            tgt_cid = r_tgt.json()["connection"]["connection_id"]

            # 1) 设定并持久化各库标准格式（含错误码检查）
            self.assertEqual(
                client.put(
                    f"/api/platform/connections/{src_cid}/standard-format",
                    json={"standard_format": "bad_fmt"},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.put(
                    f"/api/platform/connections/{src_cid}/standard-format",
                    json={"standard_format": ""},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.put(
                    f"/api/platform/connections/{src_cid}/standard-format",
                    json={"standard_format": "   "},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.put(
                    "/api/platform/connections/not_found/standard-format",
                    json={"standard_format": "type_dws"},
                ).status_code,
                404,
            )
            self.assertEqual(
                client.put(
                    f"/api/platform/connections/{src_cid}/standard-format",
                    json={"standard_format": "type_argodb"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.put(
                    f"/api/platform/connections/{src_cid}",
                    json={"name": "源Argo库", "standard_format": ""},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.put(
                    f"/api/platform/connections/{tgt_cid}/standard-format",
                    json={"standard_format": "type_dw_mart"},
                ).status_code,
                200,
            )

            # 2) Step 2 切换库自动应用该库标准格式；来源字段 cust_id 先在 Step 2 绑标
            ov2 = client.get(
                f"/api/modules/standard_migrator/bindings/overview?connection_id={tgt_cid}"
            )
            self.assertEqual(ov2.status_code, 200)
            self.assertEqual(ov2.json()["format_profile"], "type_dw_mart")

            save2 = client.post(
                "/api/modules/standard_migrator/bindings/save",
                json={
                    "connection_id": src_cid,
                    "active_schema": "main",
                    "active_table": "ods_cust",
                    "bindings": [
                        {
                            "schema_name": "main",
                            "table_name": "ods_cust",
                            "column_name": "cust_id",
                            "std_code": "STD001",
                        }
                    ],
                },
            )
            self.assertEqual(save2.status_code, 200)
            self.assertEqual(save2.json()["format_profile"], "type_argodb")
            bound_col = next(
                c for c in save2.json()["columns"] if c["column_name"] == "cust_id"
            )
            self.assertEqual(bound_col["std_target_type"], "STRING")

            # 3) Step 3 绑定双库并加载对比：两侧各自沿用 type_argodb 与 type_dw_mart
            client.post(
                "/api/modules/cross_comparator/bind-role",
                json={"role": "source", "connection_id": src_cid},
            )
            client.post(
                "/api/modules/cross_comparator/bind-role",
                json={"role": "target", "connection_id": tgt_cid},
            )
            ov3 = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_schema=main&source_table=ods_cust&target_schema=main&target_table=dwd_cust"
            )
            self.assertEqual(ov3.status_code, 200)
            ov3_data = ov3.json()
            self.assertEqual(ov3_data["source_format_profile"], "type_argodb")
            self.assertEqual(ov3_data["target_format_profile"], "type_dw_mart")
            comp = ov3_data["active_comparison"]
            self.assertEqual(comp["source_format_profile"], "type_argodb")
            self.assertEqual(comp["target_format_profile"], "type_dw_mart")

            # 4) 在五虚拟列模型中将未绑标候选 cust_no 绑至 STD001 并与 cust_id 配对保存
            save3 = client.post(
                "/api/modules/cross_comparator/pairings/save",
                json={
                    "std_table_key": "dwd_cust",
                    "source_schema": "main",
                    "source_table": "ods_cust",
                    "target_schema": "main",
                    "target_table": "dwd_cust",
                    "source_format_profile": "type_argodb",
                    "target_format_profile": "type_dw_mart",
                    "sync_to_field_bindings": True,
                    "pairings": [
                        {
                            "source_col": "cust_id",
                            "target_col": "cust_no",
                            "source_std_code": "STD001",
                            "target_std_code": "STD001",
                        },
                        {
                            "source_col": "raw_extra",
                            "target_col": "",
                            "source_std_code": "",
                            "target_std_code": "",
                        },
                        {
                            "source_col": "",
                            "target_col": "tgt_extra",
                            "source_std_code": "",
                            "target_std_code": "",
                        },
                    ],
                },
            )
            self.assertEqual(save3.status_code, 200)
            dual_rows = save3.json()["active_comparison"]["dual_rows"]
            unified_row = next(r for r in dual_rows if r["bookmark_state"] == "UNIFIED")
            self.assertEqual(unified_row["source_std"]["resolved_type"], "STRING")
            self.assertEqual(unified_row["target_std"]["resolved_type"], "VARCHAR(64)")

            tgt_binds = ctx.standard_catalog.get_table_field_bindings(tgt_cid, "main", "dwd_cust")
            self.assertEqual(tgt_binds["cust_no"]["std_code"], "STD001")
            self.assertNotIn("tgt_extra", tgt_binds)

            # 5) 推送贯标时沿用目标库 type_dw_mart 的 VARCHAR(64)
            push_res = client.post(
                "/api/modules/cross_comparator/push-to-migrator",
                json={"std_table_key": "dwd_cust"},
            )
            self.assertEqual(push_res.status_code, 200)
            pushed_items = ctx.store.list_items()
            self.assertEqual(pushed_items[0]["new_col_type"], "VARCHAR(64)")

            # 6) 按 (source_table, target_table) 不传 table_key 读取概览可命中已保存表对 dwd_cust
            ov_by_pair = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_schema=main&source_table=ods_cust&target_schema=main&target_table=dwd_cust"
            )
            self.assertEqual(ov_by_pair.status_code, 200)
            ov_pair_data = ov_by_pair.json()
            self.assertEqual(ov_pair_data["active_table_key"], "dwd_cust")
            self.assertTrue(
                any(
                    r["bookmark_state"] == "UNIFIED" and r["source_std_code"] == "STD001"
                    for r in ov_pair_data["active_comparison"]["dual_rows"]
                )
            )

            # 7) Step 3 空 Schema 解绑与 Step 2 单独解绑均不会在 Step 3 概览幽灵复活
            unbind_s3 = client.post(
                "/api/modules/cross_comparator/pairings/save",
                json={
                    "std_table_key": "dwd_cust",
                    "source_schema": "",
                    "source_table": "ods_cust",
                    "target_schema": "",
                    "target_table": "dwd_cust",
                    "sync_to_field_bindings": True,
                    "pairings": [
                        {
                            "source_col": "cust_id",
                            "target_col": "",
                            "source_std_code": "",
                            "target_std_code": "",
                        },
                        {
                            "source_col": "",
                            "target_col": "cust_no",
                            "source_std_code": "",
                            "target_std_code": "STD001",
                        },
                    ],
                },
            )
            self.assertEqual(unbind_s3.status_code, 200)
            self.assertEqual(
                ctx.standard_catalog.get_table_field_bindings(src_cid, "main", "ods_cust"),
                {},
            )
            ov_after_s3_unbind = client.get(
                "/api/modules/cross_comparator/overview?table_key=dwd_cust"
            ).json()
            src_cust_row = next(
                r
                for r in ov_after_s3_unbind["active_comparison"]["dual_rows"]
                if (r.get("source_col") or {}).get("column_name") == "cust_id"
            )
            self.assertEqual(src_cust_row["source_std_code"], "")

            unbind_s2 = client.post(
                "/api/modules/standard_migrator/bindings/save",
                json={
                    "connection_id": tgt_cid,
                    "active_schema": "main",
                    "active_table": "dwd_cust",
                    "bindings": [
                        {
                            "schema_name": "main",
                            "table_name": "dwd_cust",
                            "column_name": "cust_no",
                            "std_code": "",
                        }
                    ],
                },
            )
            self.assertEqual(unbind_s2.status_code, 200)
            ov_after_s2_unbind = client.get(
                "/api/modules/cross_comparator/overview?table_key=dwd_cust"
            ).json()
            tgt_cust_row = next(
                r
                for r in ov_after_s2_unbind["active_comparison"]["dual_rows"]
                if (r.get("target_col") or {}).get("column_name") == "cust_no"
            )
            self.assertEqual(tgt_cust_row["target_std_code"], "")

            # 8) Step 2 全量解绑后推送贯标不得从旧 cross_column_bindings 复活未绑定字段
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/push-to-migrator",
                    json={"std_table_key": "dwd_cust"},
                ).status_code,
                400,
            )
            client.post(
                "/api/modules/cross_comparator/pairings/save",
                json={
                    "std_table_key": "dwd_cust",
                    "source_schema": "main",
                    "source_table": "ods_cust",
                    "target_schema": "main",
                    "target_table": "dwd_cust",
                    "sync_to_field_bindings": True,
                    "pairings": [
                        {
                            "source_col": "cust_id",
                            "target_col": "cust_no",
                            "source_std_code": "STD001",
                            "target_std_code": "STD001",
                        }
                    ],
                },
            )
            client.post(
                "/api/modules/standard_migrator/bindings/save",
                json={
                    "connection_id": tgt_cid,
                    "active_schema": "main",
                    "active_table": "dwd_cust",
                    "bindings": [
                        {
                            "schema_name": "main",
                            "table_name": "dwd_cust",
                            "column_name": "cust_no",
                            "std_code": "",
                        }
                    ],
                },
            )
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/push-to-migrator",
                    json={"std_table_key": "dwd_cust"},
                ).status_code,
                400,
            )

            # 9) 用新的来源表 ods_cust_v2 预览已保存过的目标表 dwd_cust（不传 table_key）不得混入旧表幽灵字段
            ov_new_src = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_schema=main&source_table=ods_cust_v2&target_schema=main&target_table=dwd_cust"
            )
            self.assertEqual(ov_new_src.status_code, 200)
            ov_new_data = ov_new_src.json()
            self.assertEqual(ov_new_data["active_table_key"], "")
            new_src_cols = {
                (r.get("source_col") or {}).get("column_name")
                for r in ov_new_data["active_comparison"]["dual_rows"]
                if r.get("source_col")
            }
            self.assertEqual(new_src_cols, {"new_col_a", "new_col_b"})

            # 10) 跨模式同名来源表 other_sch.ods_cust -> main.dwd_cust 预览不得跨模式匹配 main.ods_cust 混入幽灵字段
            from core.platform.models import ColumnMeta, TableMeta

            ctx.store.save_table_meta(
                src_cid,
                TableMeta(
                    schema_name="other_sch",
                    table_name="ods_cust",
                    columns=[
                        ColumnMeta(1, "other_x", "TEXT"),
                        ColumnMeta(2, "other_y", "TEXT"),
                    ],
                ),
            )
            ov_cross_sch = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_schema=other_sch&source_table=ods_cust&target_schema=main&target_table=dwd_cust"
            )
            self.assertEqual(ov_cross_sch.status_code, 200)
            ov_cross_data = ov_cross_sch.json()
            self.assertEqual(ov_cross_data["active_table_key"], "")
            cross_src_cols = {
                (r.get("source_col") or {}).get("column_name")
                for r in ov_cross_data["active_comparison"]["dual_rows"]
                if r.get("source_col")
            }
            self.assertEqual(cross_src_cols, {"other_x", "other_y"})

            # 11) 下载未保存表对预填模板时不得泄漏冲突 std_table_key 的幽灵字段
            save_collide = client.post(
                "/api/modules/cross_comparator/pairings/save",
                json={
                    "std_table_key": "ods_cust",
                    "source_schema": "main",
                    "source_table": "ods_cust_v2",
                    "target_schema": "main",
                    "target_table": "ods_cust",
                    "sync_to_field_bindings": False,
                    "pairings": [
                        {
                            "source_col": "new_col_a",
                            "target_col": "cust_id",
                            "source_std_code": "STD001",
                            "target_std_code": "STD001",
                        }
                    ],
                },
            )
            self.assertEqual(save_collide.status_code, 200)
            tpl_res = client.get(
                "/api/modules/cross_comparator/pairings/template.xlsx"
                "?source_schema=main&source_table=ods_cust&target_schema=main&target_table=dwd_cust_v2"
            )
            self.assertEqual(tpl_res.status_code, 200)
            wb_tpl = openpyxl.load_workbook(io.BytesIO(tpl_res.content), data_only=True)
            ws_tpl = wb_tpl["跨环境字段映射"]
            tpl_rows = list(ws_tpl.iter_rows(min_row=2, values_only=True))
            tpl_src_cols = {str(r[2] or "").strip() for r in tpl_rows if r[2]}
            tpl_tgt_cols = {str(r[6] or "").strip() for r in tpl_rows if r[6]}
            self.assertNotIn("new_col_a", tpl_src_cols)
            self.assertEqual(tpl_src_cols, {"cust_id", "raw_extra"})
            self.assertEqual(tpl_tgt_cols, {"tgt_new_a"})

            # 12) 单侧 source_table 查询概览与下载模板不得被同名的 std_table_key 劫持
            ov_single_src = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_schema=main&source_table=ods_cust&target_schema=&target_table="
            )
            self.assertEqual(ov_single_src.status_code, 200)
            ov_single_data = ov_single_src.json()
            self.assertNotEqual(ov_single_data["active_table_key"], "ods_cust")
            single_src_cols = {
                c["column_name"]
                for c in ov_single_data["active_comparison"]["source_panel"]["columns"]
            }
            self.assertNotIn("new_col_a", single_src_cols)
            self.assertEqual(single_src_cols, {"cust_id", "raw_extra"})

            tpl_single_res = client.get(
                "/api/modules/cross_comparator/pairings/template.xlsx"
                "?source_schema=main&source_table=ods_cust&target_schema=&target_table="
            )
            self.assertEqual(tpl_single_res.status_code, 200)
            wb_single = openpyxl.load_workbook(io.BytesIO(tpl_single_res.content), data_only=True)
            ws_single = wb_single["跨环境字段映射"]
            single_tpl_rows = list(ws_single.iter_rows(min_row=2, values_only=True))
            single_tpl_src_cols = {str(r[2] or "").strip() for r in single_tpl_rows if r[2]}
            self.assertNotIn("new_col_a", single_tpl_src_cols)
            self.assertEqual(single_tpl_src_cols, {"cust_id", "raw_extra"})

            ctx.store.save_table_meta(
                src_cid,
                TableMeta(
                    schema_name="main",
                    table_name="ods_cust_solo",
                    columns=[ColumnMeta(1, "solo_col", "TEXT")],
                ),
            )
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/pairings/save",
                    json={
                        "std_table_key": "ods_cust_solo",
                        "source_schema": "main",
                        "source_table": "ods_cust_v2",
                        "target_schema": "main",
                        "target_table": "ods_cust_solo",
                        "sync_to_field_bindings": False,
                        "pairings": [
                            {
                                "source_col": "new_col_a",
                                "target_col": "solo_col",
                                "source_std_code": "STD001",
                                "target_std_code": "STD001",
                            }
                        ],
                    },
                ).status_code,
                200,
            )
            ov_solo = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_schema=main&source_table=ods_cust_solo&target_schema=&target_table="
            )
            self.assertEqual(ov_solo.status_code, 200)
            ov_solo_data = ov_solo.json()
            self.assertEqual(ov_solo_data["active_table_key"], "")
            solo_src_cols = {
                c["column_name"]
                for c in ov_solo_data["active_comparison"]["source_panel"]["columns"]
            }
            self.assertEqual(solo_src_cols, {"solo_col"})

            tpl_solo_res = client.get(
                "/api/modules/cross_comparator/pairings/template.xlsx"
                "?source_schema=main&source_table=ods_cust_solo&target_schema=&target_table="
            )
            self.assertEqual(tpl_solo_res.status_code, 200)
            wb_solo = openpyxl.load_workbook(io.BytesIO(tpl_solo_res.content), data_only=True)
            ws_solo = wb_solo["跨环境字段映射"]
            solo_tpl_rows = list(ws_solo.iter_rows(min_row=2, values_only=True))
            solo_tpl_src_cols = {str(r[2] or "").strip() for r in solo_tpl_rows if r[2]}
            self.assertEqual(solo_tpl_src_cols, {"solo_col"})

            # 13) 不传独立 schema 时，带点号表名 OTHER_SCH.ODS_CUST -> MAIN.DWD_CUST 仍隔离并正确加载 other_sch.ods_cust 字段
            ov_dotted = client.get(
                "/api/modules/cross_comparator/overview"
                "?source_table=OTHER_SCH.ODS_CUST&target_table=MAIN.DWD_CUST"
            )
            self.assertEqual(ov_dotted.status_code, 200)
            ov_dot_data = ov_dotted.json()
            self.assertEqual(ov_dot_data["active_table_key"], "")
            dot_src_panel = ov_dot_data["active_comparison"]["source_panel"]
            self.assertEqual(dot_src_panel["schema_name"].lower(), "other_sch")
            self.assertEqual(
                {c["column_name"] for c in dot_src_panel["columns"]},
                {"other_x", "other_y"},
            )

    def test_cross_comparator_five_col_and_header_non_overlap_structure(self) -> None:
        """静态校验 R1 五虚拟列、R2 顶部防遮蔽与 R3 侧边栏标准选择器结构."""
        static_dir = resolve_static_dir()
        with open(os.path.join(static_dir, "css", "workbench.css"), "r", encoding="utf-8") as f:
            css_text = f.read()
        with open(os.path.join(static_dir, "js", "views", "cross_comparator_view.js"), "r", encoding="utf-8") as f:
            cross_js = f.read()
        with open(os.path.join(static_dir, "js", "views", "standard_migrator_view.js"), "r", encoding="utf-8") as f:
            mig_js = f.read()

        with open(os.path.join(static_dir, "css", "tokens.css"), "r", encoding="utf-8") as f:
            tokens_css = f.read()
        with open(os.path.join(static_dir, "js", "ui", "export_report_modal.js"), "r", encoding="utf-8") as f:
            export_modal_js = f.read()

        # 导出弹窗样式变量必须全部在 tokens.css 中定义，且仅使用 workbench.css 支持的 badge 语义色
        defined_vars = set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", tokens_css + "\n" + css_text))
        for raw_call in re.findall(r"var\(([^)]+)\)", css_text):
            if "," not in raw_call:
                var_name = raw_call.strip()
                self.assertIn(var_name, defined_vars, f"undefined CSS var without fallback: {var_name}")
        self.assertNotIn('badgeTone: "ok"', mig_js)
        self.assertNotIn('badgeTone: "ok"', cross_js)
        self.assertNotIn('"ok"', export_modal_js)

        # R2: .win-titlebar-center 受 z-index 保护，.win-titlebar-right 受 overflow: hidden 约束
        self.assertRegex(css_text, r"\.win-titlebar-center\s*\{[^}]*z-index:\s*2")
        self.assertRegex(css_text, r"\.win-titlebar-right\s*\{[^}]*overflow:\s*hidden")

        # R2: renderHeaderActions 不得包含已保存映射表对或上传按钮，已保存映射表对迁移至 .mac-subbar
        header_match = re.search(
            r"renderHeaderActions\(\)\s*\{(?P<body>.*?)^\s{2}\}",
            cross_js,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(header_match)
        header_body = header_match.group("body")
        self.assertNotIn("已保存映射表对", header_body)
        self.assertNotIn("tableSelect", header_body)
        self.assertNotIn("上传映射铺底", header_body)
        self.assertIn("已保存映射表对", cross_js)

        # R1: 五虚拟列完整呈现
        for col_label in (
            "来源表待选字段",
            "来源表已绑定数标字段",
            "数据标准",
            "目标表已绑定数标字段",
            "目标表待选字段",
        ):
            self.assertIn(col_label, cross_js)

        # R3: Step 2 侧边栏包含按库标准选择器且顶部不再放置 formatSelect
        self.assertIn("当前库落标标准", mig_js)
        mig_header = re.search(
            r"renderHeaderActions\(\)\s*\{(?P<body>.*?)^\s{2}\}",
            mig_js,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(mig_header)
        self.assertNotIn("formatSelect", mig_header.group("body"))

        # Iter2 回归防线：dirty 状态通知、不复用旧 activeTableKey、跨页签状态同步
        with open(os.path.join(static_dir, "js", "views", "standard_view.js"), "r", encoding="utf-8") as f:
            std_view_js = f.read()
        self.assertIn("_markDirty()", cross_js)
        self.assertIn("this.onStateUpdate?.()", cross_js)
        self.assertNotIn("this.activeTableKey || target_table", cross_js)
        self.assertNotIn("this.activeTableKey = nextKey", cross_js)
        self.assertIn("_reloadCurrentComparison", cross_js)
        self.assertIn("_deriveSaveTableKey", cross_js)
        self.assertIn("refreshOnActivate", std_view_js)
        self.assertIn("store.subscribe", cross_js)
        self.assertIn("comparatorPane?.destroy", std_view_js)

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(db_path=os.path.join(tmp, "state.db"))
            client = TestClient(app)
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/js/views/cross_comparator_view.js").status_code, 200)

    def test_cross_compare_pair_lightweight_prefetch_and_task_activity_contracts(self) -> None:
        """验证 /compare-pair 轻量切表、/prefetch-pairs 批量预热、跨模块 X-Task-Id 缩略日志与前端 TaskActivityBar 契约."""
        from unittest.mock import patch

        static_dir = resolve_static_dir()
        with open(os.path.join(static_dir, "js", "ui", "task_activity_bar.js"), "r", encoding="utf-8") as f:
            bar_js = f.read()
        with open(os.path.join(static_dir, "js", "core", "api.js"), "r", encoding="utf-8") as f:
            api_js = f.read()
        for tok in ("class TaskActivityBar", "createTaskId", "graceMs", "EventSource", "cancel"):
            self.assertIn(tok, bar_js)
        for tok in ("isAbortError", "tasksApi", "comparePair", "prefetchPairs", "X-Task-Id"):
            self.assertIn(tok, api_js)

        with tempfile.TemporaryDirectory() as tmp:
            src_db = os.path.join(tmp, "src.db")
            tgt_db = os.path.join(tmp, "tgt.db")
            for db_f, ddl in (
                (src_db, "CREATE TABLE t_ord_s (id INTEGER PRIMARY KEY, amt TEXT); CREATE TABLE t_usr_s (uid TEXT);"),
                (tgt_db, "CREATE TABLE t_ord_t (id INTEGER PRIMARY KEY, amt VARCHAR(32)); CREATE TABLE t_usr_t (uid VARCHAR(64));"),
            ):
                c = sqlite3.connect(db_f)
                c.executescript(ddl)
                c.commit()
                c.close()

            app = create_app(db_path=os.path.join(tmp, "state.db"))
            ctx = app.state.platform_ctx
            client = TestClient(app)

            ctx.connection_hub.save_profile({"connection_id": "src_c", "name": "SrcDB", "db_type": "SQLITE", "sqlite_path": src_db})
            ctx.connection_hub.save_profile({"connection_id": "tgt_c", "name": "TgtDB", "db_type": "SQLITE", "sqlite_path": tgt_db})
            ctx.standard_catalog.upsert_item({"std_code": "S_ID", "cn_name": "编号", "en_abbr": "ID", "type_dws": "VARCHAR(32)"})

            self.assertEqual(client.post("/api/modules/cross_comparator/bind-role", json={"role": "source", "connection_id": "src_c"}).status_code, 200)
            self.assertEqual(client.post("/api/modules/cross_comparator/bind-role", json={"role": "target", "connection_id": "tgt_c"}).status_code, 200)
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/pairings/save",
                    json={
                        "std_table_key": "ord_pair",
                        "source_schema": "main",
                        "source_table": "t_ord_s",
                        "target_schema": "main",
                        "target_table": "t_ord_t",
                        "pairings": [{"source_col": "id", "target_col": "id", "source_std_code": "S_ID", "target_std_code": "S_ID"}],
                    },
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/pairings/save",
                    json={
                        "std_table_key": "usr_pair",
                        "source_schema": "main",
                        "source_table": "t_usr_s",
                        "target_schema": "main",
                        "target_table": "t_usr_t",
                        "pairings": [{"source_col": "uid", "target_col": "uid", "source_std_code": "S_ID", "target_std_code": "S_ID"}],
                    },
                ).status_code,
                200,
            )

            # /compare-pair 绝不触发全库目录扫描或全量 cross_column_bindings 拉取，且正常上报 X-Task-Id 缩略微日志
            orig_list_cross_cols = ctx.store.list_cross_columns

            def _guarded_list_cross_cols(std_table_key=None):
                if not std_table_key:
                    raise AssertionError("unexpected full cross_column_bindings scan")
                return orig_list_cross_cols(std_table_key)

            with patch.object(ctx.metadata_hub, "get_catalog_tree", side_effect=AssertionError("unexpected catalog scan")), \
                 patch.object(ctx.store, "list_cross_columns", side_effect=_guarded_list_cross_cols):
                cmp_res = client.get(
                    "/api/modules/cross_comparator/compare-pair?table_key=ord_pair",
                    headers={"X-Task-Id": "cmp_light_01"},
                )
            self.assertEqual(cmp_res.status_code, 200)
            cmp_data = cmp_res.json()
            self.assertTrue(cmp_data["ok"])
            self.assertEqual(cmp_data["active_table_key"], "ord_pair")
            self.assertGreaterEqual(len(cmp_data["active_comparison"]["source_panel"]["columns"]), 1)

            t_cmp = client.get("/api/platform/tasks/cmp_light_01").json()["task"]
            self.assertEqual(t_cmp["status"], "completed")
            self.assertGreaterEqual(len(t_cmp["recent_logs"]), 2)

            # /prefetch-pairs 批量预热全部映射表对到本地元数据缓存
            pf_res = client.post(
                "/api/modules/cross_comparator/prefetch-pairs",
                json={"max_pairs": 10},
                headers={"X-Task-Id": "cmp_pf_01"},
            )
            self.assertEqual(pf_res.status_code, 200)
            self.assertGreaterEqual(pf_res.json()["prefetched_source"], 1)
            self.assertGreaterEqual(pf_res.json()["prefetched_target"], 1)
            self.assertIsNotNone(ctx.store.get_cached_table_meta("src_c", "main", "t_usr_s"))
            self.assertIsNotNone(ctx.store.get_cached_table_meta("tgt_c", "main", "t_usr_t"))
            self.assertEqual(client.get("/api/platform/tasks/cmp_pf_01").json()["task"]["status"], "completed")

            # 第 2 步 check-compliance 与第 4 步 AI 只读探查均携带 X-Task-Id 产生缩略微日志
            chk_res = client.post(
                "/api/modules/standard_migrator/bindings/check-compliance",
                json={"connection_id": "src_c", "tables": [{"schema_name": "main", "table_name": "t_ord_s"}]},
                headers={"X-Task-Id": "mig_chk_01"},
            )
            self.assertEqual(chk_res.status_code, 200)
            t_mig = client.get("/api/platform/tasks/mig_chk_01").json()["task"]
            self.assertEqual(t_mig["status"], "completed")
            self.assertTrue(t_mig["recent_logs"])

            ctx.llm_hub.save_provider(
                {
                    "provider_id": "p_mock_tsk",
                    "name": "Mock",
                    "provider_type": "CUSTOM",
                    "base_url": "https://mock.local/v1",
                    "api_key": "sk-mock-secret",
                    "default_model": "gpt-mock",
                    "enabled": True,
                    "is_default": True,
                }
            )
            llm_replies = [
                {
                    "provider_id": "p_mock_tsk",
                    "provider_name": "Mock",
                    "model": "gpt-mock",
                    "content": json.dumps(
                        {"action": "query", "thought": "统计行数", "sql": "SELECT COUNT(*) AS cnt FROM main.t_ord_s"},
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 15.0,
                },
                {
                    "provider_id": "p_mock_tsk",
                    "provider_name": "Mock",
                    "model": "gpt-mock",
                    "content": json.dumps(
                        {"action": "conclude", "summary": "共 0 行", "findings": []},
                        ensure_ascii=False,
                    ),
                    "elapsed_ms": 18.0,
                },
            ]
            with patch.object(ctx.llm_hub, "chat_completion", side_effect=llm_replies):
                exp_res = client.post(
                    "/api/modules/data_explorer/explore",
                    json={
                        "connection_id": "src_c",
                        "schema_name": "main",
                        "focus_tables": ["main.t_ord_s"],
                        "prompt": "统计行数",
                        "max_steps": 3,
                    },
                    headers={"X-Task-Id": "exp_ai_01"},
                )
            self.assertEqual(exp_res.status_code, 200)
            t_exp = client.get("/api/platform/tasks/exp_ai_01").json()["task"]
            self.assertEqual(t_exp["status"], "completed")
            stages = [x["stage"] for x in t_exp["recent_logs"]]
            self.assertTrue(any("加载元数据" in s or "AI 规划" in s or "只读采样" in s for s in stages))

    @staticmethod
    def _run_node_probe(node_bin: str, tmp_dir: str, filename: str, js_code: str) -> str:
        script_path = os.path.join(tmp_dir, filename)
        with open(script_path, "w", encoding="utf-8") as sf:
            sf.write(js_code)
        proc = subprocess.run(
            [node_bin, script_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if proc.returncode != 0:
            raise AssertionError(f"Node probe {filename} failed (rc={proc.returncode}):\n{proc.stderr}")
        return proc.stdout

    def test_single_db_offline_html_and_single_sheet_excel_report(self):
        """验证单库库级离线 HTML / 单 Sheet Excel 报告：空缓存先采目录再导出、全库范围、公式防注入与队列隔离."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "single_rep.db")
            conn_raw = sqlite3.connect(db_path)
            conn_raw.execute("CREATE TABLE t_cust (cust_id TEXT, bal NUMERIC)")
            conn_raw.execute("CREATE TABLE t_ord (ord_id TEXT, amt NUMERIC)")
            conn_raw.execute("CREATE TABLE t_edge (safe_col TEXT)")
            conn_raw.commit()
            conn_raw.close()

            other_db = os.path.join(tmp, "other.db")
            sqlite3.connect(other_db).close()

            app = create_app(db_path=os.path.join(tmp, "state.db"))
            ctx = app.state.platform_ctx
            client = TestClient(app)

            p1 = ctx.connection_hub.save_profile(
                {
                    "connection_id": "c_single",
                    "name": "单库测试库",
                    "db_type": "SQLITE",
                    "sqlite_path": db_path,
                    "standard_format": "type_mysql",
                }
            )
            ctx.connection_hub.save_profile(
                {
                    "connection_id": "c_other",
                    "name": "其他库",
                    "db_type": "SQLITE",
                    "sqlite_path": other_db,
                }
            )
            ctx.standard_catalog.upsert_item(
                {
                    "std_code": "S_CUST_01",
                    "cn_name": "客户号</script><img onerror=alert(1)>",
                    "en_abbr": "cust_id",
                    "data_format": "C32",
                    "type_mysql": "=SUM(A1)",
                }
            )
            ctx.standard_catalog.bulk_save_field_bindings(
                "c_single",
                [{"schema_name": "main", "table_name": "t_cust", "column_name": "cust_id", "std_code": "S_CUST_01"}],
                mode="merge",
            )
            # 插入属于 c_other 的队列任务，确认不会串入 c_single 报告
            ctx.store.save_meta("migrator_connection_id", "c_other")
            ctx.store.replace_all_items(
                [
                    {
                        "row_index": 1,
                        "schema_name": "main",
                        "table_name": "t_cust",
                        "old_col_name": "cust_id",
                        "old_col_type": "TEXT",
                        "old_col_comment": "",
                        "new_col_name": "cust_id",
                        "new_col_type": "VARCHAR(32)",
                        "new_col_comment": "客户号",
                    }
                ],
                "other_queue.xlsx",
                preserve_history=False,
            )

            # 清空 c_single 的目录与绑标缓存以触发 Q4「先触发采集目录，再生成报告」
            with ctx.store.module_db() as mdb:
                mdb.execute("DELETE FROM meta_tables WHERE connection_id = ?", ("c_single",))
                mdb.execute("DELETE FROM meta_schemas WHERE connection_id = ?", ("c_single",))
                mdb.execute("DELETE FROM field_standard_bindings WHERE connection_id = ?", ("c_single",))

            self.assertEqual(ctx.metadata_hub.get_catalog_tree("c_single", auto_refresh=False)["tables"], [])

            with mock.patch.object(
                ctx.metadata_hub, "refresh_catalog", wraps=ctx.metadata_hub.refresh_catalog
            ) as spy_refresh:
                html_res = client.get(
                    f"/api/modules/standard_migrator/bindings/report.html?connection_id={p1.connection_id}",
                    headers={"X-Task-Id": "rep_html_1"},
                )
            self.assertEqual(html_res.status_code, 200, html_res.text)
            self.assertEqual(spy_refresh.call_count, 1)
            self.assertIn("text/html", html_res.headers.get("content-type", ""))
            self.assertIn("attachment;", html_res.headers.get("content-disposition", ""))
            self.assertGreaterEqual(
                len(ctx.metadata_hub.get_catalog_tree("c_single", auto_refresh=False)["tables"]), 3
            )

            # 恢复绑定（含一条陈旧 schema 'public' 与一条库内缺表绑定），再清空目录但保留绑定以回归测试「有绑标无目录仍自触刷新」
            ctx.standard_catalog.bulk_save_field_bindings(
                "c_single",
                [
                    {"schema_name": "main", "table_name": "t_cust", "column_name": "cust_id", "std_code": "S_CUST_01"},
                    {"schema_name": "public", "table_name": "t_cust", "column_name": "cust_id", "std_code": "S_CUST_01"},
                    {"schema_name": "public", "table_name": "t_missing_only", "column_name": "ghost_col", "std_code": "S_CUST_01"},
                ],
                mode="merge",
            )
            with ctx.store.module_db() as mdb:
                mdb.execute("DELETE FROM meta_tables WHERE connection_id = ?", ("c_single",))
                mdb.execute("DELETE FROM meta_schemas WHERE connection_id = ?", ("c_single",))

            with mock.patch.object(
                ctx.metadata_hub, "refresh_catalog", wraps=ctx.metadata_hub.refresh_catalog
            ) as spy_refresh2:
                html_res2 = client.get(
                    "/api/modules/standard_migrator/bindings/report.html?connection_id=c_single",
                    headers={"X-Task-Id": "rep_html_2"},
                )
            self.assertEqual(html_res2.status_code, 200)
            self.assertEqual(spy_refresh2.call_count, 1)
            html_txt = html_res2.text
            self.assertIn("t_cust", html_txt)
            self.assertIn("t_ord", html_txt)
            self.assertIn("t_edge", html_txt)
            self.assertIn("t_missing_only", html_txt)
            self.assertNotIn("</script><img", html_txt)
            self.assertIn("\\u003c/script\\u003e", html_txt)
            self.assertEqual(client.get("/api/platform/tasks/rep_html_2").json()["task"]["status"], "completed")

            m_json = re.search(
                r'<script id="report-data" type="application/json">(.*?)</script>',
                html_txt,
                re.DOTALL,
            )
            self.assertIsNotNone(m_json)
            payload = json.loads(m_json.group(1))
            self.assertEqual(payload["migration_items"], [])
            # public.t_cust 应与 main.t_cust 归并去重；外加 t_ord、t_edge、t_missing_only 共 4 张表
            self.assertEqual([t["table"]["table_name"] for t in payload["tables"]].count("t_cust"), 1)
            self.assertEqual(
                {t["table"]["table_name"] for t in payload["tables"]},
                {"t_cust", "t_ord", "t_edge", "t_missing_only"},
            )

            xlsx_res = client.get("/api/modules/standard_migrator/bindings/report.xlsx?connection_id=c_single")
            self.assertEqual(xlsx_res.status_code, 200)
            wb = openpyxl.load_workbook(io.BytesIO(xlsx_res.content))
            self.assertEqual(wb.sheetnames, ["全库落标与贯标报告"])
            ws = wb.active
            self.assertEqual(ws.freeze_panes, "A2")
            self.assertTrue(ws.auto_filter.ref)
            rows_vals = list(ws.iter_rows(min_row=2, values_only=True))
            table_names_in_sheet = {r[2] for r in rows_vals}
            self.assertEqual(table_names_in_sheet, {"t_cust", "t_ord", "t_edge", "t_missing_only"})
            # 单横杠占位符 '-' 不得被误前缀为 "'-"
            self.assertFalse(any(v == "'-" for row in rows_vals for v in row))
            missing_rows = [r for r in rows_vals if r[2] == "t_missing_only"]
            self.assertEqual(len(missing_rows), 1)
            self.assertEqual(missing_rows[0][14], "库内缺表")
            cust_row = [r for r in rows_vals if r[2] == "t_cust" and r[6] == "cust_id"][0]
            self.assertEqual(cust_row[13], "'=SUM(A1)")

            # 若本机具备 node，验证单库离线 HTML 搜索框输入不重建节点（防失焦/断输入法）
            node_bin = shutil.which("node")
            if node_bin:
                scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_txt, re.DOTALL)
                self.assertEqual(len(scripts), 2)
                dom_probe = (
                    "class Node{}\n"
                    "function makeNode(tag){const n=new Node();Object.assign(n,{tagName:String(tag).toUpperCase(),children:[],style:{},dataset:{},_ev:{},className:'',type:'',placeholder:'',value:'',hidden:false,appendChild(c){this.children.push(c);return c;},removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);return c;},remove(){},click(){this.dispatchEvent({type:'click'});},get firstChild(){return this.children[0]||null;},replaceChildren(...a){this.children=a;},addEventListener(e,f){(this._ev[e]||(this._ev[e]=[])).push(f);},dispatchEvent(ev){if(!ev.target)ev.target=this;ev.currentTarget=this;(this._ev[ev.type]||[]).forEach(f=>f(ev));},setAttribute(k,v){this[k]=v;},getAttribute(k){return this[k];},querySelector(sel){const c=sel[0]==='.'?sel.slice(1):null;let f=null;const walk=x=>{if(f)return;if(c&&(x.className||'').split(/\\s+/).includes(c)){f=x;return;}(x.children||[]).forEach(walk);};walk(this);return f;},closest(){return null;},focus(){}});return n;}\n"
                    "const txtOf=n=>(n.textContent||'')+(n.children||[]).map(txtOf).join(' ');\n"
                    "const findAll=(n,p,o=[])=>{if(p(n))o.push(n);(n.children||[]).forEach(c=>findAll(c,p,o));return o;};\n"
                    "global.Node=Node;\n"
                    "const _els={};const root=makeNode('div');root.id='app-root';_els['app-root']=root;\n"
                    "const dataEl=makeNode('script');dataEl.id='report-data';dataEl.textContent=" + json.dumps(m_json.group(1)) + ";_els['report-data']=dataEl;\n"
                    "global.window={matchMedia:()=>({matches:false})};\n"
                    "global.document={documentElement:makeNode('html'),body:makeNode('body'),createElement:makeNode,createTextNode:t=>Object.assign(new Node(),{nodeType:3,textContent:t}),getElementById:id=>_els[id]||null};\n"
                    "const dlBlobs=[];global.URL={createObjectURL:b=>{dlBlobs.push(b);return 'blob:0';},revokeObjectURL(){}};global.Blob=class{constructor(p){this._b=Buffer.concat(p.map(x=>Buffer.from(x)));}};\n"
                    + scripts[1]
                    + "\nconst inputs=[];const findInputs=x=>{if(x.tagName==='INPUT')inputs.push(x);(x.children||[]).forEach(findInputs);};findInputs(root);\n"
                    + "if(!inputs.length) throw new Error('missing search input');\n"
                    + "const inp=inputs[0];inp.value='cust';inp.dispatchEvent({type:'input'});\n"
                    + "const inputs2=[];const findInputs2=x=>{if(x.tagName==='INPUT')inputs2.push(x);(x.children||[]).forEach(findInputs2);};findInputs2(root);\n"
                    + "if(inputs2[0]!==inp) throw new Error('input recreated on search');\n"
                    + "const filterBtn=findAll(root,n=>n.tagName==='BUTTON'&&txtOf(n).includes('导出当前筛选'))[0];\n"
                    + "if(!filterBtn||filterBtn.hidden) throw new Error('overview filter btn should be visible');\n"
                    + "const qTabBtn=findAll(root,n=>n.tagName==='BUTTON'&&txtOf(n).includes('贯标改造执行清单'))[0];\n"
                    + "qTabBtn.click();\n"
                    + "if(!filterBtn.hidden) throw new Error('stale overview filter leaked onto queue tab');\n"
                )
                self._run_node_probe(node_bin, tmp, "single_dom_probe.cjs", dom_probe)

            # 全库预填对照模板下载（含 X-Task-Id 进度追踪与 public->main 陈旧绑定回退预填）
            tpl_res = client.get(
                "/api/modules/standard_migrator/bindings/template.xlsx?connection_id=c_single&scope=all",
                headers={"X-Task-Id": "tpl_all_01"},
            )
            self.assertEqual(tpl_res.status_code, 200)
            t_tpl = client.get("/api/platform/tasks/tpl_all_01").json()["task"]
            self.assertEqual(t_tpl["status"], "completed")
            wb_tpl = openpyxl.load_workbook(io.BytesIO(tpl_res.content))
            tpl_rows = list(wb_tpl["字段与数据标准对照"].iter_rows(min_row=2, values_only=True))
            tpl_tbls = {r[1] for r in tpl_rows}
            self.assertTrue({"t_cust", "t_ord", "t_edge"}.issubset(tpl_tbls))
            cust_tpl = next(r for r in tpl_rows if r[1] == "t_cust" and r[2] == "cust_id")
            self.assertEqual(cust_tpl[5], "S_CUST_01")

    def test_cross_env_offline_html_single_sheet_excel_and_node_xlsx_roundtrip(self):
        """验证跨环境离线网页/单 Sheet Excel、推荐同名表不触发预比对及网页内嵌 JS 生成合法 .xlsx."""
        with tempfile.TemporaryDirectory() as tmp:
            src_db = os.path.join(tmp, "src.db")
            tgt_db = os.path.join(tmp, "tgt.db")
            sc = sqlite3.connect(src_db)
            sc.execute("CREATE TABLE t_saved_s (cust_no TEXT, extra_s TEXT)")
            sc.execute("CREATE TABLE t_same_unpaired (x1 TEXT, x2 INTEGER)")
            sc.execute("CREATE TABLE t_src_only (s_only TEXT)")
            sc.commit()
            sc.close()

            tc = sqlite3.connect(tgt_db)
            tc.execute("CREATE TABLE t_saved_t (cust_no TEXT, extra_t TEXT)")
            tc.execute("CREATE TABLE t_same_unpaired (x1 TEXT, x2 INTEGER)")
            tc.execute("CREATE TABLE t_tgt_only (t_only TEXT)")
            tc.commit()
            tc.close()

            app = create_app(db_path=os.path.join(tmp, "state.db"))
            ctx = app.state.platform_ctx
            client = TestClient(app)

            ctx.connection_hub.save_profile(
                {
                    "connection_id": "src_db",
                    "name": "上游Oracle",
                    "db_type": "SQLITE",
                    "sqlite_path": src_db,
                    "standard_format": "type_oracle",
                }
            )
            ctx.connection_hub.save_profile(
                {
                    "connection_id": "tgt_db",
                    "name": "下游DWS",
                    "db_type": "SQLITE",
                    "sqlite_path": tgt_db,
                    "standard_format": "type_dws",
                }
            )
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/bind-role",
                    json={"role": "source", "connection_id": "src_db", "auto_refresh": False},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/bind-role",
                    json={"role": "target", "connection_id": "tgt_db", "auto_refresh": False},
                ).status_code,
                200,
            )

            ctx.standard_catalog.upsert_item(
                {
                    "std_code": "STD_01",
                    "cn_name": "客户编码",
                    "en_abbr": "cust_no",
                    "type_oracle": "VARCHAR2(32)",
                    "type_dws": "VARCHAR(32)",
                }
            )
            save_res = client.post(
                "/api/modules/cross_comparator/pairings/save",
                json={
                    "std_table_key": "t_saved",
                    "source_schema": "main",
                    "source_table": "t_saved_s",
                    "target_schema": "main",
                    "target_table": "t_saved_t",
                    "pairings": [
                        {
                            "source_col": "cust_no",
                            "target_col": "cust_no",
                            "source_std_code": "STD_01",
                            "target_std_code": "STD_01",
                        }
                    ],
                },
            )
            self.assertEqual(save_res.status_code, 200)

            # 仅清空目录缓存但保留已存绑标：仍应自动触发目录刷新发现全部表，且不触发未配对表字段探查
            with ctx.store.module_db() as mdb:
                mdb.execute("DELETE FROM meta_tables")
                mdb.execute("DELETE FROM meta_schemas")

            probed_tables_log: List[Tuple[str, str]] = []
            orig_batch = ctx.metadata_hub.get_tables_metadata_batch
            orig_single = ctx.metadata_hub.get_table_metadata

            def _spy_batch(cid, targets, *args, **kwargs):
                for _s, _t in targets:
                    probed_tables_log.append((cid, str(_t).lower()))
                return orig_batch(cid, targets, *args, **kwargs)

            def _spy_single(cid, sch, tbl, *args, **kwargs):
                probed_tables_log.append((cid, str(tbl).lower()))
                return orig_single(cid, sch, tbl, *args, **kwargs)

            with (
                mock.patch.object(ctx.metadata_hub, "get_tables_metadata_batch", side_effect=_spy_batch),
                mock.patch.object(ctx.metadata_hub, "get_table_metadata", side_effect=_spy_single),
            ):
                html_res = client.get(
                    "/api/modules/cross_comparator/report.html",
                    headers={"X-Task-Id": "cross_rep_1"},
                )

            self.assertEqual(html_res.status_code, 200, html_res.text)
            self.assertEqual(client.get("/api/platform/tasks/cross_rep_1").json()["task"]["status"], "completed")
            # Q2 核心约束：未配对的同名表与单侧表严禁触发字段探查 / 自动预比对
            probed_names = {t_name for _c, t_name in probed_tables_log}
            self.assertNotIn("t_same_unpaired", probed_names)
            self.assertNotIn("t_src_only", probed_names)
            self.assertNotIn("t_tgt_only", probed_names)

            m_json = re.search(
                r'<script id="report-data" type="application/json">(.*?)</script>',
                html_res.text,
                re.DOTALL,
            )
            self.assertIsNotNone(m_json)
            data_obj = json.loads(m_json.group(1))
            self.assertEqual(data_obj["source_format_profile"], "type_oracle")
            self.assertEqual(data_obj["target_format_profile"], "type_dws")
            self.assertEqual(len(data_obj["pairs"]), 1)
            rec_names = {r["table_name"] for r in data_obj["recommended_pairings"]}
            self.assertEqual(rec_names, {"t_same_unpaired"})
            self.assertEqual([t["table_name"] for t in data_obj["source_only_tables"]], ["t_src_only"])
            self.assertEqual([t["table_name"] for t in data_obj["target_only_tables"]], ["t_tgt_only"])

            xlsx_res = client.get("/api/modules/cross_comparator/report.xlsx")
            self.assertEqual(xlsx_res.status_code, 200)
            wb = openpyxl.load_workbook(io.BytesIO(xlsx_res.content))
            self.assertEqual(wb.sheetnames, ["跨环境库级比对报告"])
            ws = wb.active
            self.assertEqual(ws.freeze_panes, "A2")
            self.assertTrue(ws.auto_filter.ref)
            sheet_rows = list(ws.iter_rows(min_row=2, values_only=True))
            self.assertFalse(any(v == "'-" for row in sheet_rows for v in row))
            self.assertTrue(any(r[1] == "推荐同名(未保存)" and r[4] == "t_same_unpaired" for r in sheet_rows))
            self.assertTrue(any(r[1] == "已保存映射" and r[4] == "t_saved_s" for r in sheet_rows))
            self.assertTrue(any(r[1] == "仅来源库存在" and r[4] == "t_src_only" for r in sheet_rows))
            self.assertTrue(any(r[1] == "仅目标库存在" and r[12] == "t_tgt_only" for r in sheet_rows))

            # 同库自比对（src_cid == tgt_cid）清空目录后仅应刷新一次，不得并发重复采集
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/bind-role",
                    json={"role": "target", "connection_id": "src_db", "auto_refresh": False},
                ).status_code,
                200,
            )
            with ctx.store.module_db() as mdb:
                mdb.execute("DELETE FROM meta_tables WHERE connection_id = ?", ("src_db",))
                mdb.execute("DELETE FROM meta_schemas WHERE connection_id = ?", ("src_db",))
            with mock.patch.object(
                ctx.metadata_hub, "refresh_catalog", wraps=ctx.metadata_hub.refresh_catalog
            ) as spy_self_ref:
                self_rep = client.get("/api/modules/cross_comparator/report.xlsx")
            self.assertEqual(self_rep.status_code, 200)
            self.assertEqual(spy_self_ref.call_count, 1)

            # 保留旧版可重导入绑定导出契约
            legacy_res = client.get("/api/modules/cross_comparator/bindings/export.xlsx")
            self.assertEqual(legacy_res.status_code, 200)
            wb_legacy = openpyxl.load_workbook(io.BytesIO(legacy_res.content))
            self.assertIn("数据标准字段名", list(wb_legacy.active.iter_rows(min_row=1, max_row=1, values_only=True))[0])

            # 若本机具备 node，端到端执行离线 HTML 内嵌的零依赖 .xlsx ZIP/OOXML 生成器及 DOM 搜索态稳定性
            node_bin = shutil.which("node")
            if node_bin:
                scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_res.text, re.DOTALL)
                self.assertEqual(len(scripts), 2)
                app_js = scripts[1]
                # 抽取 _PURE_JS_XLSX_RUNTIME（在 _OFFLINE_DOM_HELPERS_JS 之前）并喂入构造的 rows 测试
                runtime_part = app_js.split("function el(", 1)[0]
                harness = (
                    "class Blob { constructor(parts) { this._buf = Buffer.concat(parts.map(p => Buffer.from(p))); } }\n"
                    + runtime_part
                    + "\nconst blob = buildSingleSheetXlsxBlob('跨环境库级比对报告', ['序号','模式','表名'], [{ tone: 'GREEN', values: [1, 'main', 't_saved_s'] }, { tone: 'LAVENDER', values: [2, 'main', 't_same_unpaired'] }]);\n"
                    + "process.stdout.write(blob._buf.toString('base64'));\n"
                )
                xlsx_b64 = self._run_node_probe(node_bin, tmp, "xlsx_harness.cjs", harness)
                js_xlsx_bytes = base64.b64decode(xlsx_b64.strip())
                with zipfile.ZipFile(io.BytesIO(js_xlsx_bytes), "r") as zf_js:
                    self.assertIsNone(zf_js.testzip())
                wb_js = openpyxl.load_workbook(io.BytesIO(js_xlsx_bytes))
                self.assertEqual(wb_js.sheetnames, ["跨环境库级比对报告"])
                js_vals = list(wb_js.active.iter_rows(min_row=1, values_only=True))
                self.assertEqual(js_vals[0], ("序号", "模式", "表名"))
                self.assertEqual(js_vals[1], (1, "main", "t_saved_s"))
                self.assertEqual(js_vals[2], (2, "main", "t_same_unpaired"))

                cross_dom_probe = (
                    "class Node{}\n"
                    "function makeNode(tag){const n=new Node();Object.assign(n,{tagName:String(tag).toUpperCase(),children:[],style:{},dataset:{},_ev:{},className:'',type:'',placeholder:'',value:'',hidden:false,appendChild(c){this.children.push(c);return c;},removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);return c;},remove(){},click(){this.dispatchEvent({type:'click'});},get firstChild(){return this.children[0]||null;},replaceChildren(...a){this.children=a;},addEventListener(e,f){(this._ev[e]||(this._ev[e]=[])).push(f);},dispatchEvent(ev){if(!ev.target)ev.target=this;ev.currentTarget=this;(this._ev[ev.type]||[]).forEach(f=>f(ev));},setAttribute(k,v){this[k]=v;},getAttribute(k){return this[k];},querySelector(sel){const c=sel[0]==='.'?sel.slice(1):null;let f=null;const walk=x=>{if(f)return;if(c&&(x.className||'').split(/\\s+/).includes(c)){f=x;return;}(x.children||[]).forEach(walk);};walk(this);return f;},closest(){return null;},focus(){}});return n;}\n"
                    "const txtOf=n=>(n.textContent||'')+(n.children||[]).map(txtOf).join(' ');\n"
                    "const findAll=(n,p,o=[])=>{if(p(n))o.push(n);(n.children||[]).forEach(c=>findAll(c,p,o));return o;};\n"
                    "global.Node=Node;\n"
                    "const _els={};const root=makeNode('div');root.id='app-root';_els['app-root']=root;\n"
                    "const dataEl=makeNode('script');dataEl.id='report-data';dataEl.textContent=" + json.dumps(m_json.group(1)) + ";_els['report-data']=dataEl;\n"
                    "global.window={matchMedia:()=>({matches:false})};\n"
                    "global.document={documentElement:makeNode('html'),body:makeNode('body'),createElement:makeNode,createTextNode:t=>Object.assign(new Node(),{nodeType:3,textContent:t}),getElementById:id=>_els[id]||null};\n"
                    "global.URL={createObjectURL:()=>'blob:0',revokeObjectURL(){}};global.Blob=class{constructor(p){this._b=Buffer.concat(p.map(x=>Buffer.from(x)));}};\n"
                    + app_js
                    + "\nconst inputs=[];const findInputs=x=>{if(x.tagName==='INPUT')inputs.push(x);(x.children||[]).forEach(findInputs);};findInputs(root);\n"
                    + "if(!inputs.length) throw new Error('missing cross-env search input');\n"
                    + "const inp=inputs[0];inp.value='saved';inp.dispatchEvent({type:'input'});\n"
                    + "const inputs2=[];const findInputs2=x=>{if(x.tagName==='INPUT')inputs2.push(x);(x.children||[]).forEach(findInputs2);};findInputs2(root);\n"
                    + "if(inputs2[0]!==inp) throw new Error('cross-env input recreated on search');\n"
                    + "const filterBtn=findAll(root,n=>n.tagName==='BUTTON'&&txtOf(n).includes('导出当前筛选'))[0];\n"
                    + "if(!filterBtn||filterBtn.hidden) throw new Error('cross pairs filter btn should be visible');\n"
                    + "const soloTab=findAll(root,n=>n.tagName==='BUTTON'&&txtOf(n).includes('单侧独有表'))[0];\n"
                    + "soloTab.click();\n"
                    + "if(!filterBtn.hidden) throw new Error('stale pairs filter leaked onto solo tab');\n"
                    + "const soloOnlyPayload=JSON.parse(dataEl.textContent);\n"
                    + "soloOnlyPayload.pairs=[];soloOnlyPayload.recommended_pairings=[];soloOnlyPayload.warnings=['w1'];\n"
                    + "const root2=makeNode('div');_els['app-root']=root2;dataEl.textContent=JSON.stringify(soloOnlyPayload);\n"
                )
                cross_dom_probe += (
                    app_js
                    + "\nconst activeTabs=findAll(root2,n=>n.tagName==='BUTTON'&&(n.className||'').includes('is-active'));\n"
                    + "if(!activeTabs.length||!txtOf(activeTabs[0]).includes('单侧独有表')) throw new Error('expected solo initial tab');\n"
                    + "const warnTab=findAll(root2,n=>n.tagName==='BUTTON'&&txtOf(n).includes('采集告警'))[0];\n"
                    + "warnTab.click();\n"
                    + "const fBtn2=findAll(root2,n=>n.tagName==='BUTTON'&&txtOf(n).includes('导出当前筛选'))[0];\n"
                    + "if(!fBtn2.hidden) throw new Error('filter btn should be hidden on warnings tab');\n"
                )
                self._run_node_probe(node_bin, tmp, "cross_dom_probe.cjs", cross_dom_probe)

    def test_cross_compat_pair_search_and_report_columns(self) -> None:
        """端到端验证：跨库长度/精度包络、类型转换判定、/pairs/search 字段级检索及报告列展示."""
        with tempfile.TemporaryDirectory() as tmp:
            src_db = os.path.join(tmp, "src_compat.db")
            tgt_db = os.path.join(tmp, "tgt_compat.db")

            with contextlib.closing(sqlite3.connect(src_db)) as sc:
                sc.executescript(
                    """
                    CREATE TABLE ods_acct (
                        cust_name VARCHAR(100),
                        open_dt VARCHAR(20),
                        bal_amt DECIMAL(18,2),
                        unmapped_src_phone VARCHAR(30)
                    );
                    CREATE TABLE ods_txn (
                        txn_id VARCHAR(32),
                        txn_amt DECIMAL(12,2)
                    );
                    """
                )
            with contextlib.closing(sqlite3.connect(tgt_db)) as tc:
                tc.executescript(
                    """
                    CREATE TABLE dwd_acct (
                        cust_name VARCHAR(50),
                        open_dt DATE,
                        bal_amt DECIMAL(10,2),
                        unmapped_tgt_email VARCHAR(80)
                    );
                    CREATE TABLE dwd_txn (
                        txn_id VARCHAR(64),
                        txn_amt DECIMAL(18,4)
                    );
                    """
                )

            app = create_app(db_path=os.path.join(tmp, "state.db"))
            ctx = app.state.platform_ctx
            client = TestClient(app)

            src_cid = client.post(
                "/api/platform/connections",
                json={"name": "源兼容库", "db_type": "SQLITE", "sqlite_path": src_db, "auto_refresh_metadata": True},
            ).json()["connection"]["connection_id"]
            tgt_cid = client.post(
                "/api/platform/connections",
                json={"name": "目标兼容库", "db_type": "SQLITE", "sqlite_path": tgt_db, "auto_refresh_metadata": True},
            ).json()["connection"]["connection_id"]

            # 预热物理字段缓存，确保 meta_columns 内包含未保存映射的物理字段 unmapped_src_phone / unmapped_tgt_email
            ctx.metadata_hub.get_table_metadata(src_cid, "main", "ods_acct")
            ctx.metadata_hub.get_table_metadata(tgt_cid, "main", "dwd_acct")

            for code, cn, abbr, dws_t in (
                ("S_NAME", "客户名称", "cust_name", "VARCHAR(100)"),
                ("S_DATE", "开户日期", "open_dt", "DATE"),
                ("S_AMT", "账户余额", "bal_amt", "DECIMAL(18,2)"),
                ("S_TXN", "交易流水号", "txn_id", "VARCHAR(32)"),
            ):
                ctx.standard_catalog.upsert_item(
                    {"std_code": code, "cn_name": cn, "en_abbr": abbr, "type_dws": dws_t}
                )

            client.post("/api/modules/cross_comparator/bind-role", json={"role": "source", "connection_id": src_cid})
            client.post("/api/modules/cross_comparator/bind-role", json={"role": "target", "connection_id": tgt_cid})

            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/pairings/save",
                    json={
                        "std_table_key": "acct_pair",
                        "source_schema": "main",
                        "source_table": "ods_acct",
                        "target_schema": "main",
                        "target_table": "dwd_acct",
                        "pairings": [
                            {"source_col": "cust_name", "target_col": "cust_name", "source_std_code": "S_NAME", "target_std_code": "S_NAME"},
                            {"source_col": "open_dt", "target_col": "open_dt", "source_std_code": "S_DATE", "target_std_code": "S_DATE"},
                            {"source_col": "bal_amt", "target_col": "bal_amt", "source_std_code": "S_AMT", "target_std_code": "S_AMT"},
                        ],
                    },
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/modules/cross_comparator/pairings/save",
                    json={
                        "std_table_key": "txn_pair",
                        "source_schema": "main",
                        "source_table": "ods_txn",
                        "target_schema": "main",
                        "target_table": "dwd_txn",
                        "pairings": [
                            {"source_col": "txn_id", "target_col": "txn_id", "source_std_code": "S_TXN", "target_std_code": "S_TXN"},
                        ],
                    },
                ).status_code,
                200,
            )

            # 1. 验证 /compare-pair 返回精确的双维兼容性字段与汇总统计
            cmp_res = client.get("/api/modules/cross_comparator/compare-pair?table_key=acct_pair").json()
            self.assertTrue(cmp_res["ok"])
            comp = cmp_res["active_comparison"]
            self.assertEqual(comp["bookmark_summary"]["capacity_insufficient"], 2)
            self.assertEqual(comp["bookmark_summary"]["capacity_risk"], 1)
            self.assertEqual(comp["bookmark_summary"]["type_conversion"], 1)

            rows_by_col = {
                (r["source_col"]["column_name"] if r.get("source_col") else ""): r
                for r in comp["dual_rows"]
            }
            self.assertEqual(rows_by_col["cust_name"]["cross_compat"]["capacity_status"], "INSUFFICIENT")
            self.assertIn("100→50", rows_by_col["cust_name"]["cross_compat"]["capacity_label"])
            self.assertFalse(rows_by_col["cust_name"]["cross_compat"]["has_type_conversion"])

            self.assertTrue(rows_by_col["open_dt"]["cross_compat"]["has_type_conversion"])
            self.assertEqual(rows_by_col["open_dt"]["cross_compat"]["type_conv_status"], "CAST_REQUIRED")
            self.assertIn("字符→日期", rows_by_col["open_dt"]["cross_compat"]["type_conv_label"])
            self.assertEqual(rows_by_col["open_dt"]["cross_compat"]["capacity_status"], "RISK")

            self.assertEqual(rows_by_col["bal_amt"]["cross_compat"]["capacity_status"], "INSUFFICIENT")
            self.assertIn("整数位 16→8位", rows_by_col["bal_amt"]["cross_compat"]["capacity_label"])

            # 2. 验证 list_cross_tables 聚合字段与 /pairs/search 接口（按表名、已保存字段、缓存物理字段检索）
            ov_tables = cmp_res["tables"]
            acct_tbl = next(t for t in ov_tables if t["std_table_key"] == "acct_pair")
            self.assertIn("cust_name", acct_tbl["source_cols_text"])
            self.assertIn("bal_amt", acct_tbl["target_cols_text"])

            s_by_src_col = client.get("/api/modules/cross_comparator/pairs/search?keyword=open_dt").json()
            self.assertEqual(s_by_src_col["total"], 1)
            self.assertEqual(s_by_src_col["items"][0]["std_table_key"], "acct_pair")
            self.assertIn("open_dt", s_by_src_col["items"][0]["matched_source_cols"])
            self.assertIn("open_dt", s_by_src_col["items"][0]["matched_target_cols"])

            s_by_meta_col = client.get("/api/modules/cross_comparator/pairs/search?keyword=unmapped_src_phone").json()
            self.assertEqual(s_by_meta_col["total"], 1)
            self.assertEqual(s_by_meta_col["items"][0]["std_table_key"], "acct_pair")
            self.assertIn("unmapped_src_phone", s_by_meta_col["items"][0]["matched_source_cols"])

            s_by_tgt_meta = client.get("/api/modules/cross_comparator/pairs/search?keyword=unmapped_tgt_email").json()
            self.assertEqual(s_by_tgt_meta["total"], 1)
            self.assertIn("unmapped_tgt_email", s_by_tgt_meta["items"][0]["matched_target_cols"])

            s_by_tbl = client.get("/api/modules/cross_comparator/pairs/search?keyword=dwd_txn").json()
            self.assertEqual(s_by_tbl["total"], 1)
            self.assertEqual(s_by_tbl["items"][0]["std_table_key"], "txn_pair")

            # 3. 验证单 Sheet 库级 Excel、三方绑定 Excel 与离线 HTML 均包含两个新增字段及统计
            rep_xlsx = client.get("/api/modules/cross_comparator/report.xlsx").content
            wb = openpyxl.load_workbook(io.BytesIO(rep_xlsx))
            ws = wb.active
            hdrs = list(ws.iter_rows(min_row=1, max_row=1, values_only=True))[0]
            self.assertEqual(hdrs[21], "目标长度/精度包络")
            self.assertEqual(hdrs[22], "是否涉及类型转换")
            self.assertEqual(hdrs[23], "差异与诊断详情")
            data_rows = list(ws.iter_rows(min_row=2, values_only=True))
            open_dt_row = next(r for r in data_rows if r[4] == "ods_acct" and r[5] == "open_dt")
            self.assertIn("需核验", open_dt_row[21])
            self.assertIn("字符→日期", open_dt_row[22])
            cust_row = next(r for r in data_rows if r[4] == "ods_acct" and r[5] == "cust_name")
            self.assertIn("100→50", cust_row[21])
            self.assertEqual(cust_row[22], "否")

            legacy_xlsx = client.get("/api/modules/cross_comparator/bindings/export.xlsx?table_key=acct_pair").content
            wb_l = openpyxl.load_workbook(io.BytesIO(legacy_xlsx))
            hdrs_l = list(wb_l.active.iter_rows(min_row=1, max_row=1, values_only=True))[0]
            self.assertIn("目标长度/精度包络", hdrs_l)
            self.assertIn("是否涉及类型转换", hdrs_l)

            rep_html = client.get("/api/modules/cross_comparator/report.html").text
            m_json = re.search(
                r'<script id="report-data" type="application/json">(.*?)</script>',
                rep_html,
                re.DOTALL,
            )
            self.assertIsNotNone(m_json)
            payload = json.loads(m_json.group(1))
            self.assertEqual(payload["aggregate"]["capacity_insufficient_cols"], 2)
            self.assertEqual(payload["aggregate"]["capacity_risk_cols"], 1)
            self.assertEqual(payload["aggregate"]["type_conversion_cols"], 1)
            self.assertEqual(payload["excel_headers"][21], "目标长度/精度包络")
            self.assertEqual(payload["excel_headers"][22], "是否涉及类型转换")

            # 4. 前端静态与 Node 校验：type_compat.js 与后端判定一致、可搜索选择器保留持久化节点且零 XSS
            static_dir = resolve_static_dir()
            with open(os.path.join(static_dir, "js", "views", "cross_comparator_view.js"), "r", encoding="utf-8") as f:
                cross_js = f.read()
            self.assertIn("cross-pair-picker", cross_js)
            self.assertIn("analyzeCrossColumnCompat", cross_js)
            self.assertIn("searchPairs", cross_js)

            node_bin = shutil.which("node")
            if node_bin:
                with open(os.path.join(static_dir, "js", "core", "type_compat.js"), "r", encoding="utf-8") as f:
                    tc_js = f.read()
                tc_cjs = tc_js.replace("export function ", "function ") + "\n" + (
                    "const r1 = analyzeCrossColumnCompat('VARCHAR(20)', 'DATE');\n"
                    "if (!r1.has_type_conversion || r1.type_conv_status !== 'CAST_REQUIRED' || !r1.type_conv_label.includes('字符→日期')) throw new Error('bad r1');\n"
                    "const r2 = analyzeCrossColumnCompat('DECIMAL(18,2)', 'DECIMAL(10,2)');\n"
                    "if (r2.capacity_status !== 'INSUFFICIENT' || r2.has_type_conversion) throw new Error('bad r2');\n"
                    "const r3 = analyzeCrossColumnCompat('INT(11)', 'INTEGER', 'MYSQL', 'DWS');\n"
                    "if (r3.capacity_status !== 'CONTAINED' || r3.has_type_conversion) throw new Error('bad r3');\n"
                )
                self._run_node_probe(node_bin, tmp, "type_compat_probe.cjs", tc_cjs)

                scripts = re.findall(r"<script[^>]*>(.*?)</script>", rep_html, re.DOTALL)
                self.assertEqual(len(scripts), 2)
                off_app_js = scripts[1]
                off_probe = (
                    "class Node{}\n"
                    "function makeNode(tag){const n=new Node();Object.assign(n,{tagName:String(tag).toUpperCase(),children:[],style:{},dataset:{},_ev:{},className:'',type:'',placeholder:'',value:'',hidden:false,appendChild(c){this.children.push(c);return c;},removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);return c;},remove(){},click(){this.dispatchEvent({type:'click'});},get firstChild(){return this.children[0]||null;},replaceChildren(...a){this.children=a;},addEventListener(e,f){(this._ev[e]||(this._ev[e]=[])).push(f);},dispatchEvent(ev){if(!ev.target)ev.target=this;ev.currentTarget=this;(this._ev[ev.type]||[]).forEach(f=>f(ev));},setAttribute(k,v){this[k]=v;},getAttribute(k){return this[k];},querySelector(sel){const c=sel[0]==='.'?sel.slice(1):null;let f=null;const walk=x=>{if(f)return;if(c&&(x.className||'').split(/\\s+/).includes(c)){f=x;return;}(x.children||[]).forEach(walk);};walk(this);return f;},closest(){return null;},focus(){}});return n;}\n"
                    "const txtOf=n=>(n.textContent||'')+(n.children||[]).map(txtOf).join(' ');\n"
                    "const findAll=(n,p,o=[])=>{if(p(n))o.push(n);(n.children||[]).forEach(c=>findAll(c,p,o));return o;};\n"
                    "global.Node=Node;\n"
                    "const _els={};const root=makeNode('div');root.id='app-root';_els['app-root']=root;\n"
                    "const dataEl=makeNode('script');dataEl.id='report-data';dataEl.textContent=" + json.dumps(m_json.group(1)) + ";_els['report-data']=dataEl;\n"
                    "global.window={matchMedia:()=>({matches:false})};\n"
                    "global.document={documentElement:makeNode('html'),body:makeNode('body'),createElement:makeNode,createTextNode:t=>Object.assign(new Node(),{nodeType:3,textContent:t}),getElementById:id=>_els[id]||null};\n"
                    "const dlBlobs=[];global.URL={createObjectURL:b=>{dlBlobs.push(b);return 'blob:0';},revokeObjectURL(){}};global.Blob=class{constructor(p){this._buf=Buffer.concat(p.map(x=>Buffer.from(x)));}};\n"
                    + off_app_js
                    + "\nconst expBtn=findAll(root,n=>n.tagName==='BUTTON'&&txtOf(n).includes('导出全库 Excel'))[0];\n"
                    + "expBtn.click();\n"
                    + "if(!dlBlobs.length) throw new Error('missing js xlsx download');\n"
                    + "const inp=findAll(root,n=>n.tagName==='INPUT')[0];\n"
                    + "inp.value='open_dt';inp.dispatchEvent({type:'input'});\n"
                    + "const navItems=findAll(root,n=>n.tagName==='BUTTON'&&(n.className||'').includes('nav-item'));\n"
                    + "if(!navItems.some(b=>txtOf(b).includes('ods_acct'))) throw new Error('sidebar search did not match column open_dt');\n"
                    + "process.stdout.write(dlBlobs[0]._buf.toString('base64'));\n"
                )
                js_xlsx_b64 = self._run_node_probe(node_bin, tmp, "cross_compat_off_probe.cjs", off_probe)
                wb_off = openpyxl.load_workbook(io.BytesIO(base64.b64decode(js_xlsx_b64.strip())))
                off_vals = list(wb_off.active.iter_rows(min_row=1, values_only=True))
                self.assertEqual(len(off_vals[0]), 24)
                self.assertEqual(off_vals[0][21], "目标长度/精度包络")
                self.assertEqual(off_vals[0][22], "是否涉及类型转换")
                off_open_dt = next(r for r in off_vals[1:] if r[4] == "ods_acct" and r[5] == "open_dt")
                self.assertIn("字符→日期", off_open_dt[22])

    def test_cross_comparator_macos_layout_anti_overlap_and_filter_integrity(self) -> None:
        """验证跨环境比对 macOS 重设计防重叠网格契约、四套主题令牌同步及筛选态真实索引(realIdx)与保存完整性."""
        static_dir = resolve_static_dir()
        with open(os.path.join(static_dir, "css", "tokens.css"), "r", encoding="utf-8") as f:
            tokens_css = f.read()
        with open(os.path.join(static_dir, "css", "workbench.css"), "r", encoding="utf-8") as f:
            css_text = f.read()

        # 1. 四套主题块 (:root / prefers-color-scheme:dark / meta dark / meta light) 均须定义跨环境表面、滚动条与 macOS 弹出箭头令牌
        for tok_name in (
            "--cross-stage-bg",
            "--cross-track-bg",
            "--cross-card-bg",
            "--cross-card-shadow",
            "--scrollbar-thumb",
            "--scrollbar-track",
            "--mac-select-arrow",
        ):
            occurrences = re.findall(rf"{re.escape(tok_name)}\s*:", tokens_css)
            self.assertGreaterEqual(
                len(occurrences),
                4,
                f"{tok_name} must be defined across all 4 light/dark theme blocks in tokens.css",
            )

        # 2. CSS 防纵向挤压重叠、防横向裁切与表头像素级对齐契约
        ws_m = re.search(r"\.cross-five-workspace\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(ws_m)
        self.assertIn("minmax(0,", ws_m.group(1))

        pane_m = re.search(r"\.cross-matrix-pane\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(pane_m)
        self.assertNotIn("flex-direction: column", pane_m.group(1))
        self.assertNotIn("scrollbar-gutter: stable", pane_m.group(1))
        self.assertIn("overflow-y: auto", pane_m.group(1))

        cand_s_m = re.search(r"\.cross-cand-search\s*\{([^}]*)\}", css_text)
        filt_b_m = re.search(r"\.cross-center-filterbar\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(cand_s_m)
        self.assertIsNotNone(filt_b_m)
        self.assertIn("height: 40px", cand_s_m.group(1))
        self.assertIn("height: 40px", filt_b_m.group(1))
        self.assertIn("flex-wrap: nowrap", filt_b_m.group(1))
        self.assertIn("@media (max-width: 920px)", css_text)

        for grid_cls in (r"\.cross-matrix-list", r"\.cross-cand-list"):
            gm = re.search(rf"{grid_cls}\s*\{{([^}}]*)\}}", css_text)
            self.assertIsNotNone(gm)
            self.assertIn("grid-auto-rows: max-content", gm.group(1))
            self.assertIn("align-content: start", gm.group(1))

        row_m = re.search(r"\.cross-matrix-row\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(row_m)
        self.assertIn("var(--cross-matrix-cols)", row_m.group(1))
        self.assertIn("flex-shrink: 0", row_m.group(1))
        self.assertNotIn("min-height: 52px", row_m.group(1))

        hdr_m = re.search(r"\.cross-center-col-headers\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(hdr_m)
        self.assertIn("var(--cross-matrix-cols)", hdr_m.group(1))

        sticky_m = re.search(r"\.cross-center-sticky-top\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(sticky_m)
        self.assertIn("position: sticky", sticky_m.group(1))

        # macOS 弹出按钮样式、中性一致卡片边框、三列表头聚拢分隔与去除未选中红橙胶囊边框契约
        sel_m = re.search(r"select\.mac-input,\s*select\.input\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(sel_m)
        self.assertIn("appearance: none", sel_m.group(1))
        pop_sel_m = re.search(r"\.cross-popup-select-wrap\s*>\s*select\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(pop_sel_m)
        self.assertIn("appearance: none", pop_sel_m.group(1))
        self.assertNotIn(".cross-ep-table-select", css_text)
        self.assertNotIn("is-danger-hint", css_text)
        self.assertNotIn("is-warn-hint", css_text)

        unif_m = re.search(r"\.cross-std-card\.is-unified\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(unif_m)
        self.assertIn("border-color: var(--os-border)", unif_m.group(1))
        self.assertNotIn("--status-success", unif_m.group(1))

        center_hdr_cell_m = re.search(r"\.cross-center-col-headers\s+\.cross-col-head\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(center_hdr_cell_m)
        self.assertIn("justify-content: flex-start", center_hdr_cell_m.group(1))
        self.assertIn(".cross-center-col-headers .cross-col-head.is-center::before", css_text)

        # 类型胶囊使用块级容器确保长 SQL 类型真实省略号、五栏窄屏无第 5 列横向截断、选表浮层支持向上翻转防裁切及现代细滚动条
        type_pill_m = re.search(r"\.cross-type-pill\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(type_pill_m)
        self.assertIn("display: block", type_pill_m.group(1))
        self.assertNotIn("inline-flex", type_pill_m.group(1))
        self.assertIn(".cross-type-pill:only-child", css_text)
        self.assertIn(".cross-table-popover.is-flip-up", css_text)
        self.assertIn("@supports (scrollbar-color: auto)", css_text)
        five_inner_m = re.search(r"\.cross-five-inner\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(five_inner_m)
        self.assertNotIn("min-width: 980px", five_inner_m.group(1))

        # 3. Node.js 真实加载 CrossComparatorView：验证筛选胶囊、IME 安全搜索输入与筛选态下 realIdx 操作及全量保存不丢行
        import pathlib

        node_bin = shutil.which("node")
        if node_bin:
            with tempfile.TemporaryDirectory() as tmp:
                view_url = pathlib.Path(static_dir, "js", "views", "cross_comparator_view.js").as_uri()
                probe_mjs = f"""
class Node {{}}
class MockElement extends Node {{
  constructor(tag) {{
    super();
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.style = {{}};
    this.dataset = {{}};
    this._ev = {{}};
    this._txt = "";
    this.className = "";
    this.value = "";
    this.hidden = false;
    const self = this;
    this.classList = {{
      add(...cs) {{
        const cur = new Set((self.className || "").split(/\\s+/).filter(Boolean));
        cs.forEach(c => cur.add(c));
        self.className = [...cur].join(" ");
      }},
      remove(...cs) {{
        const cur = new Set((self.className || "").split(/\\s+/).filter(Boolean));
        cs.forEach(c => cur.delete(c));
        self.className = [...cur].join(" ");
      }},
      contains(c) {{
        return (self.className || "").split(/\\s+/).includes(c);
      }},
      toggle(c, force) {{
        const cur = new Set((self.className || "").split(/\\s+/).filter(Boolean));
        const shouldAdd = force !== undefined ? Boolean(force) : !cur.has(c);
        if (shouldAdd) cur.add(c);
        else cur.delete(c);
        self.className = [...cur].join(" ");
        return shouldAdd;
      }},
    }};
  }}
  get textContent() {{
    return this._txt + (this.children || []).map(c => c.textContent || "").join("");
  }}
  set textContent(v) {{
    this._txt = String(v ?? "");
    this.children = [];
  }}
  appendChild(c) {{ this.children.push(c); return c; }}
  replaceChildren(...kids) {{ this.children = kids.filter(Boolean); }}
  addEventListener(e, fn) {{ (this._ev[e] || (this._ev[e] = [])).push(fn); }}
  removeEventListener(e, fn) {{ this._ev[e] = (this._ev[e] || []).filter(x => x !== fn); }}
  dispatchEvent(ev) {{
    if (!ev.target) ev.target = this;
    ev.currentTarget = this;
    if (!ev.stopPropagation) ev.stopPropagation = () => {{}};
    if (!ev.preventDefault) ev.preventDefault = () => {{}};
    (this._ev[ev.type] || []).forEach(fn => fn(ev));
  }}
  click() {{ this.dispatchEvent({{ type: "click" }}); }}
  setAttribute(k, v) {{ this[k] = String(v); }}
  contains(other) {{
    if (this === other) return true;
    return (this.children || []).some(c => c && c.contains && c.contains(other));
  }}
  focus() {{ globalThis.document.activeElement = this; }}
  blur() {{ if (globalThis.document.activeElement === this) globalThis.document.activeElement = null; }}
  remove() {{}}
}}
const makeNode = (tag) => new MockElement(tag);
const rawSetTimeout = globalThis.setTimeout;
const unrefTimeout = (fn, ms) => {{ const t = rawSetTimeout(fn, ms); t?.unref?.(); return t; }};
globalThis.setTimeout = unrefTimeout;
const toastRoot = makeNode("div");
globalThis.Node = Node;
globalThis.window = {{ matchMedia: () => ({{ matches: false }}), setTimeout: unrefTimeout, clearTimeout }};
const _docEv = {{}};
globalThis.document = {{
  activeElement: null,
  documentElement: makeNode("html"),
  head: makeNode("head"),
  body: makeNode("body"),
  createElement: makeNode,
  createElementNS: (_ns, tag) => makeNode(tag),
  createTextNode: (t) => {{ const tn = new MockElement("#text"); tn._txt = String(t ?? ""); return tn; }},
  addEventListener(e, fn) {{ (_docEv[e] || (_docEv[e] = [])).push(fn); }},
  removeEventListener(e, fn) {{ _docEv[e] = (_docEv[e] || []).filter(x => x !== fn); }},
  dispatchEvent(ev) {{ (_docEv[ev.type] || []).forEach(fn => fn(ev)); }},
  querySelector: () => null,
  getElementById: (id) => id === "toast-root" ? toastRoot : null,
}};

let savedPayload = null;
let lastCompareUrl = "";
globalThis.fetch = async (url, opts = {{}}) => {{
  if (String(url).includes("/compare-pair")) {{
    lastCompareUrl = String(url);
    const bodyObj = {{ ok: true, active_table_key: "", tables: [], active_comparison: mockComp("ods_ledger_121", "t_d", "ods") }};
    return {{
      ok: true,
      status: 200,
      headers: {{ get: () => "application/json" }},
      json: async () => bodyObj,
      text: async () => JSON.stringify(bodyObj),
    }};
  }}
  if (String(url).includes("/pairings/save")) {{
    savedPayload = JSON.parse(opts.body);
    const bodyObj = {{ ok: true, message: "saved", active_table_key: savedPayload.std_table_key, tables: [], active_comparison: mockComp("t_a", "t_b") }};
    return {{
      ok: true,
      status: 200,
      headers: {{ get: () => "application/json" }},
      json: async () => bodyObj,
      text: async () => JSON.stringify(bodyObj),
    }};
  }}
  const defObj = {{ ok: true, items: [] }};
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => "application/json" }},
    json: async () => defObj,
    text: async () => JSON.stringify(defObj),
  }};
}};

function mockComp(sTbl, tTbl, sSch = "main") {{
  return {{
    table_binding: {{ source_schema: sSch, source_table: sTbl, target_schema: "main", target_table: tTbl }},
    source_panel: {{ schema_name: sSch, table_name: sTbl, penetrated_base_tables: [sSch + ".base_" + sTbl], columns: [{{ column_name: "cand_s1", data_type: "VARCHAR(20)", ordinal_position: 9 }}] }},
    target_panel: {{ schema_name: "main", table_name: tTbl, penetrated_base_tables: [], columns: [{{ column_name: "cand_t1", data_type: "VARCHAR(20)", ordinal_position: 9 }}] }},
    dual_rows: [
      {{
        row_id: "r0",
        source_col: {{ column_name: "ok_col", data_type: "VARCHAR(20)", column_comment: "正常列" }},
        target_col: {{ column_name: "ok_col", data_type: "VARCHAR(50)", column_comment: "正常列" }},
        source_std_code: "S_OK",
        target_std_code: "S_OK",
        source_std: {{ std_code: "S_OK", cn_name: "正常标准", en_abbr: "OK_COL", resolved_type: "VARCHAR(50)" }},
        target_std: {{ std_code: "S_OK", cn_name: "正常标准", en_abbr: "OK_COL", resolved_type: "VARCHAR(50)" }},
        target_compliance: {{ status: "COMPLIANT" }},
        target_needs_migration: false,
      }},
      {{
        row_id: "r1",
        source_col: {{ column_name: "diff_col", data_type: "VARCHAR(30)" }},
        target_col: {{ column_name: "diff_col", data_type: "VARCHAR(30)" }},
        source_std_code: "S_D1",
        target_std_code: "S_D2",
        source_std: {{ std_code: "S_D1", cn_name: "源标准", en_abbr: "D1" }},
        target_std: {{ std_code: "S_D2", cn_name: "目标标准", en_abbr: "D2" }},
        target_compliance: {{ status: "NON_COMPLIANT" }},
        target_needs_migration: true,
      }},
      {{
        row_id: "r2",
        source_col: {{ column_name: "risk_col", data_type: "VARCHAR(100)", column_comment: "超长源列" }},
        target_col: {{ column_name: "risk_col", data_type: "VARCHAR(20)", column_comment: "超短目标列" }},
        source_std_code: "S_RISK",
        target_std_code: "S_RISK",
        source_std: {{ std_code: "S_RISK", cn_name: "风险标准", en_abbr: "RISK_COL", resolved_type: "VARCHAR(100)" }},
        target_std: {{ std_code: "S_RISK", cn_name: "风险标准", en_abbr: "RISK_COL", resolved_type: "VARCHAR(100)" }},
        target_compliance: {{ status: "NON_COMPLIANT" }},
        target_needs_migration: true,
      }},
    ],
  }};
}}

const {{ CrossComparatorView }} = await import({json.dumps(view_url)});
const view = new CrossComparatorView();
view._applyComparisonResponse({{
  ok: true,
  active_table_key: "pair_ab",
  tables: [{{ std_table_key: "pair_ab", source_table: "t_a", target_table: "t_b", column_count: 3 }}],
  source_connection: {{ connection_id: "c1", name: "Src", db_type: "DWS", configured: true, tables: [] }},
  target_connection: {{ connection_id: "c2", name: "Tgt", db_type: "DWS", configured: true, tables: [] }},
  active_comparison: mockComp("t_a", "t_b"),
}});

const findAll = (n, pred, out = []) => {{
  if (pred(n)) out.push(n);
  (n.children || []).forEach(c => findAll(c, pred, out));
  return out;
}};

// 1) IME 安全：中栏搜索框输入时不得重建 input 节点
const searchBefore = view.matrixSearchInput;
searchBefore.value = "risk_col";
searchBefore.dispatchEvent({{ type: "input" }});
if (view.matrixSearchInput !== searchBefore) throw new Error("matrixSearchInput recreated");
const rowsAfterSearch = findAll(view.matrixListMount, x => (x.className || "").includes("cross-matrix-row") && !(x.className || "").includes("is-dropzone-row"));
if (rowsAfterSearch.length !== 1) throw new Error("expected 1 row on search, got " + rowsAfterSearch.length);
searchBefore.value = "";
searchBefore.dispatchEvent({{ type: "input" }});

// 2) 筛选「位长/精度不足」时仅展示 r2 (realIdx=2)，此时保存必须仍保留全部 3 行 + 2 个待选字段，绝不丢失隐藏行
const capPill = findAll(view.root, x => x.tagName === "BUTTON" && x.textContent.includes("位长/精度不足"))[0];
if (!capPill) throw new Error("missing capacity filter pill");
capPill.click();
if (view.matrixFilter !== "CAPACITY_RISK") throw new Error("filter state mismatch");
const filteredRows = findAll(view.matrixListMount, x => (x.className || "").includes("cross-matrix-row") && !(x.className || "").includes("is-dropzone-row"));
if (filteredRows.length !== 1) throw new Error("expected 1 capacity risk row");

await view.saveCurrentPairings(false);
if (!savedPayload || savedPayload.pairings.length !== 5) {{
  throw new Error("save under active filter dropped rows: " + JSON.stringify(savedPayload?.pairings) + " | toast=" + toastRoot.textContent);
}}

// 3) 在筛选态下解绑唯一可见行的目标列，必须精确解绑 centerRows[2] (risk_col) 而非 centerRows[0] (ok_col)
view.matrixFilter = "CAPACITY_RISK";
view.render();
const visRows2 = findAll(view.matrixListMount, x => (x.className || "").includes("cross-matrix-row") && !(x.className || "").includes("is-dropzone-row"));
const removeBtns = findAll(visRows2[0], x => x.tagName === "BUTTON" && (x.className || "").includes("cross-chip-remove"));
// removeBtns: [source ×, center split ×, target ×]
removeBtns[removeBtns.length - 1].click();
if (!view.centerRows[0].target_col || view.centerRows[0].target_col.column_name !== "ok_col") {{
  throw new Error("wrong row mutated under filter! row 0 was touched");
}}
if (view.centerRows[2].target_col !== null) {{
  throw new Error("expected centerRows[2].target_col to be unbound");
}}

// 4) 滚动容器节点恒定与 scrollTop 保持 + Escape 取消点选待选字段
const paneRef = view.matrixPaneEl;
const wsRef = view.workspaceEl;
paneRef.scrollTop = 360;
view.srcCandidateListMount.scrollTop = 140;
const candCard0 = findAll(view.srcCandidateListMount, x => (x.className || "").includes("cross-cand-card"))[0];
candCard0.click();
if (!view.selectedCandidate || view.matrixPaneEl !== paneRef || view.workspaceEl !== wsRef) {{
  throw new Error("scroll container recreated on candidate click");
}}
if (paneRef.scrollTop !== 360 || view.srcCandidateListMount.scrollTop !== 140) {{
  throw new Error("scrollTop lost on candidate click");
}}
globalThis.document.dispatchEvent({{ type: "keydown", key: "Escape" }});
if (view.selectedCandidate !== null) {{
  throw new Error("Escape did not clear selectedCandidate");
}}

// 5) 筛选态下绑定新标准若被当前筛选隐藏，自动切回 ALL 展示新行，绝不让字段凭空消失
view.matrixFilter = "SPLIT_DIFF";
view.matrixKeyword = "diff_col";
view.matrixSearchInput.value = "diff_col";
view.render();
const takenCand = view._takeCandidate("source", "cand_s1");
view._commitNewStandardBinding("source", takenCand, {{ std_code: "S_NEW", cn_name: "新绑标准", en_abbr: "CAND_S1", type_dws: "VARCHAR(20)" }});
if (view.matrixFilter !== "ALL" || view.matrixKeyword !== "") {{
  throw new Error("active filter was not auto-reset to reveal newly bound row");
}}

// 6) 拖拽已绑字段跨行换绑（removeFromRowIdx < targetRowIdx 触发 splice 时仍精准落位且重算残留行）
// 当前最后一行为仅含 source_col=cand_s1 的单侧行，而 r2 (idx=2) 为仅含 source_col=risk_col 的单侧行
// 将 idx=2 的 source_col 拖入最后一行的 target 侧不匹配，改为测试同侧从单侧行 r2 拖入另一单侧行
const tgtTaken = view._takeCandidate("target", "cand_t1");
view._commitNewStandardBinding("target", tgtTaken, {{ std_code: "S_TGT_SOLO", cn_name: "目标单侧", en_abbr: "CAND_T1", type_dws: "VARCHAR(20)" }});
const srcSoloIdx = view.centerRows.findIndex(r => r.source_col?.column_name === "risk_col" && !r.target_col);
const tgtSoloIdx = view.centerRows.findIndex(r => !r.source_col && r.target_col?.column_name === "cand_t1");
if (srcSoloIdx === -1 || tgtSoloIdx === -1 || !(srcSoloIdx < tgtSoloIdx)) {{
  throw new Error("unexpected solo row indices: " + srcSoloIdx + ", " + tgtSoloIdx);
}}
view.dragPayload = {{ kind: "bound", side: "source", colName: "risk_col", rowIdx: srcSoloIdx }};
view._handleDropOnCenterTarget(tgtSoloIdx, "source");
const mergedRow = view.centerRows.find(r => r.target_col?.column_name === "cand_t1");
if (!mergedRow || mergedRow.source_col?.column_name !== "risk_col" || mergedRow.bookmark_state !== "UNIFIED") {{
  throw new Error("cross-row bound drop splice corrupted target row: " + JSON.stringify(mergedRow));
}}

// 7) 切换到另一对表时自动重置筛选器与滚动位置，避免新表空白困惑
const manySrcTables = Array.from({{ length: 180 }}, (_, i) => ({{
  schema_name: "ods",
  table_name: "ods_ledger_" + String(i).padStart(3, "0"),
  object_type: i % 5 === 0 ? "VIEW" : "TABLE",
  table_comment: "台账表_" + i,
}}));
paneRef.scrollTop = 280;
view._applyComparisonResponse({{
  ok: true,
  active_table_key: "pair_cd",
  tables: [],
  source_connection: {{ connection_id: "c1", name: "Src", db_type: "DWS", configured: true, tables: manySrcTables }},
  target_connection: {{ connection_id: "c2", name: "Tgt", db_type: "DWS", configured: true, tables: [] }},
  active_comparison: mockComp("t_c", "t_d"),
}}, {{ mergeBrief: false }});
if (view.matrixFilter !== "ALL" || view.matrixKeyword !== "" || paneRef.scrollTop !== 0) {{
  throw new Error("switching table pair did not reset matrixFilter and scrollTop");
}}

// 8) 正常兼容行 (ok_col) 不得渲染冗余三层 .cross-compat-tags 底栏，而风险行 (risk_col) 仅渲染有效风险徽标且不含「无转换」噪音
const allVisRows = findAll(view.matrixListMount, x => (x.className || "").includes("cross-matrix-row") && !(x.className || "").includes("is-dropzone-row"));
const okCompatTags = findAll(allVisRows[0], x => (x.className || "").includes("cross-compat-tags"));
if (okCompatTags.length !== 0) {{
  throw new Error("benign row should not render noisy 3rd-tier .cross-compat-tags");
}}
const riskCompatTags = findAll(allVisRows[2], x => (x.className || "").includes("cross-compat-tags"));
if (riskCompatTags.length !== 1 || !riskCompatTags[0].textContent.includes("100→20") || riskCompatTags[0].textContent.includes("无转换")) {{
  throw new Error("unexpected risk compat tags: " + (riskCompatTags[0]?.textContent || "missing"));
}}

// 9) 双端 Schema/Table <input> 与 <select> 节点持久化，聚焦输入期间触发 render / Escape 绝不重建节点或丢失输入焦点
const srcSchRef = view.srcSchemaInput;
const srcTblRef = view.srcTableInput;
const tgtSchRef = view.tgtSchemaInput;
const tgtTblRef = view.tgtTableInput;
const srcConnRef = view.srcConnSelect;
const candForEsc = findAll(view.srcCandidateListMount, x => (x.className || "").includes("cross-cand-card"))[0];
candForEsc.click();
srcTblRef.focus();
srcTblRef.value = "t_c_composing_ime";
srcTblRef.dispatchEvent({{ type: "input" }});
globalThis.document.dispatchEvent({{ type: "keydown", key: "Escape" }});
if (
  view.srcSchemaInput !== srcSchRef ||
  view.srcTableInput !== srcTblRef ||
  view.tgtSchemaInput !== tgtSchRef ||
  view.tgtTableInput !== tgtTblRef ||
  view.srcConnSelect !== srcConnRef
) {{
  throw new Error("endpoint inputs/selects were recreated on render()");
}}
if (globalThis.document.activeElement !== srcTblRef || srcTblRef.value !== "t_c_composing_ime") {{
  throw new Error("focused endpoint input lost focus or IME value on render()");
}}
srcTblRef.blur();

// 10) 可搜索 macOS 选表浮层 (180 张表截断保护 + 关键字实时过滤 + Escape 仅关浮层不误清待选字段 + 键盘选表触发比对)
candForEsc.click();
if (!view.selectedCandidate) throw new Error("expected selectedCandidate before opening table picker");
const srcQuickBtn = view.epControls.source.quickBtn;
if (!srcQuickBtn.textContent.includes("180")) {{
  throw new Error("expected quickBtn count 180, got " + srcQuickBtn.textContent);
}}
srcQuickBtn.click();
if (!view.epControls.source.pickerOpen || view.epControls.source.popoverEl.hidden) {{
  throw new Error("source table popover did not open");
}}
const initialOpts = findAll(view.epControls.source.pickerListEl, x => x.tagName === "BUTTON" && x.classList.contains("cross-table-option"));
if (initialOpts.length !== 150 || view.epControls.source.pickerFootEl.hidden) {{
  throw new Error("expected 150 capped options with footer notice for 180-table schema, got " + initialOpts.length);
}}
// Escape 关闭选表浮层并归还焦点，且保留之前选中的待选字段卡片
globalThis.document.dispatchEvent({{ type: "keydown", key: "Escape" }});
if (view.epControls.source.pickerOpen || !view.selectedCandidate) {{
  throw new Error("Escape while table picker open failed or cleared selectedCandidate");
}}
// 重新打开并搜索 "ledger_12" -> 先模拟中文输入法组合态 (isComposing / keyCode=229) 不误关浮层不误触发选表 -> ArrowDown -> Enter 选中 ods.ods_ledger_121
srcQuickBtn.click();
const tblSearchInp = view.epControls.source.pickerSearchInput;
tblSearchInp.value = "ledger_12";
tblSearchInp.dispatchEvent({{ type: "input" }});
const searchedOpts = findAll(view.epControls.source.pickerListEl, x => x.tagName === "BUTTON" && x.classList.contains("cross-table-option"));
if (searchedOpts.length !== 10) {{
  throw new Error("expected 10 filtered tables for ledger_12, got " + searchedOpts.length);
}}
lastCompareUrl = "";
tblSearchInp.dispatchEvent({{ type: "keydown", key: "Enter", isComposing: true, keyCode: 229 }});
tblSearchInp.dispatchEvent({{ type: "keydown", key: "Escape", isComposing: true, keyCode: 229 }});
tblSearchInp.dispatchEvent({{ type: "keydown", key: "ArrowDown", isComposing: true, keyCode: 229 }});
if (!view.epControls.source.pickerOpen || lastCompareUrl !== "" || view.epControls.source.pickerHighlightIdx !== 0) {{
  throw new Error("IME composing keydown prematurely hijacked table picker");
}}
tblSearchInp.dispatchEvent({{ type: "keydown", key: "ArrowDown" }});
tblSearchInp.dispatchEvent({{ type: "keydown", key: "Enter" }});
await new Promise(r => rawSetTimeout(r, 20));
if (!lastCompareUrl.includes("ods_ledger_121") || view.srcSchemaInput.value !== "ods" || view.srcTableInput.value !== "ods_ledger_121") {{
  throw new Error("keyboard selection in table popover did not load ods.ods_ledger_121: " + lastCompareUrl);
}}

// 11) 当表名框粘贴或输入 schema.table (如 mart_core.dws_acct_sum) 且 Schema 框已有旧值 ("ods") 时，必须自动覆盖为新 Schema
lastCompareUrl = "";
view.srcSchemaInput.value = "ods";
view.srcSchemaInput.dispatchEvent({{ type: "input" }});
view.srcTableInput.value = "mart_core.dws_acct_sum";
view.srcTableInput.dispatchEvent({{ type: "input" }});
view.srcTableInput.dispatchEvent({{ type: "keydown", key: "Enter" }});
await new Promise(r => rawSetTimeout(r, 20));
if (!lastCompareUrl.includes("source_schema=mart_core") || !lastCompareUrl.includes("source_table=dws_acct_sum")) {{
  throw new Error("explicit schema.table in tableInput kept stale schema: " + lastCompareUrl);
}}

// 12) 超矮窗口堆叠态下打开底部目标库选表浮层：当下方空间不足而上方空间充足时自动向上翻转 (.is-flip-up) 并钳制 maxHeight
view.root.getBoundingClientRect = () => ({{ top: 70, bottom: 430, left: 0, right: 840 }});
view.epControls.target.bodyEl.getBoundingClientRect = () => ({{ top: 295, bottom: 320, left: 10, right: 820 }});
view.epControls.target.quickBtn.click();
if (!view.epControls.target.popoverEl.classList.contains("is-flip-up") || !view.epControls.target.popoverEl.style.maxHeight) {{
  throw new Error("expected target table popover to flip upward with clamped maxHeight on short viewport");
}}
view._closeAllTablePickers();

// 13) 已保存表对浮层 (.cross-pair-popover) 打开时按 Escape（无论焦点在输入框还是失焦到文档）：仅关闭浮层且不误清空已选待选字段
const candForPairEsc = findAll(view.srcCandidateListMount, x => (x.className || "").includes("cross-cand-card"))[0];
candForPairEsc.click();
if (!view.selectedCandidate) throw new Error("expected selectedCandidate before pairPicker Escape test");
view._openPairPicker();
view.pairSearchInput.focus();
view.pairSearchInput.dispatchEvent({{ type: "keydown", key: "Escape" }});
if (view.pairPickerOpen || !view.selectedCandidate) {{
  throw new Error("Escape on focused pairSearchInput failed or cleared selectedCandidate");
}}
view._openPairPicker();
view.pairSearchInput.blur();
globalThis.document.dispatchEvent({{ type: "keydown", key: "Escape" }});
if (view.pairPickerOpen || !view.selectedCandidate) {{
  throw new Error("Document Escape while pairPickerOpen failed or cleared selectedCandidate");
}}

view.destroy();
"""
                self._run_node_probe(node_bin, tmp, "cross_macos_probe.mjs", probe_mjs)


if __name__ == "__main__":
    unittest.main()
