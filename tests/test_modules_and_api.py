"""DataOG v2.0.0 FastAPI 微内核路由、三大独立插件模块及架构安全红线集成测试."""

from __future__ import annotations

import ast
import contextlib
import glob
import io
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile

import openpyxl
from fastapi.testclient import TestClient

from app import create_app


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
        """架构铁律校验：扫描 modules/* 下全部 Python AST，确保模块之间零相互私有导入."""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_root = os.path.join(project_root, "modules")
        py_files = glob.glob(os.path.join(modules_root, "**", "*.py"), recursive=True)
        self.assertGreaterEqual(len(py_files), 9)

        violations = []
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

        self.assertEqual(violations, [], f"发现违反插件隔离铁律的跨模块导入: {violations}")

    def test_frontend_static_files_zero_xss_sinks_and_zero_blocking_dialogs(self) -> None:
        """前端安全红线校验：确保所有原生 ES Module 与 HTML 零 innerHTML/outerHTML/阻塞弹窗/内联脚本."""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        static_root = os.path.join(project_root, "static")

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


if __name__ == "__main__":
    unittest.main()
