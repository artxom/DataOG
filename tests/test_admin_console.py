"""Data.O.G 管理员可视化总控台 (`tools/admin_console`) 安全性与功能回归测试."""

from __future__ import annotations

import datetime
import glob
import os
import re
import tempfile
import unittest
from unittest import mock

from core.platform.license_hub import (
    SCOPE_GLOBAL,
    encode_request_code,
    issue_activation_code_for_seed,
)
from tests.test_modules_and_api import TestClient, _TEST_LICENSE_SEED
from tools.admin_console.auth import AdminAuthManager, SESSION_COOKIE_NAME
from tools.admin_console.server import create_admin_app
from tools.admin_console.service import AdminConsoleService


class TestAdminConsole(unittest.TestCase):
    def _make_isolated_app(self, tmpdir: str, *, keygen_bin: str = ""):
        auth = AdminAuthManager(auth_file=os.path.join(tmpdir, "admin_auth.json"))
        svc = AdminConsoleService(
            history_file=os.path.join(tmpdir, "issue_hist.json"),
            demo_home_dir=os.path.join(tmpdir, "demo_home"),
            main_home_dir=os.path.join(tmpdir, "main_home"),
            keygen_bin=keygen_bin,
        )
        app = create_admin_app(auth_manager=auth, service=svc)
        return app, auth, svc

    def test_auth_setup_login_rate_limit_csrf_headers_and_password_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app, auth, _ = self._make_isolated_app(tmpdir)
            client = TestClient(app, auto_activate=False)

            # 1. 未初始化状态与未登录门禁
            st0 = client.get("/api/auth/status")
            self.assertEqual(st0.status_code, 200)
            self.assertFalse(st0.json()["initialized"])
            self.assertFalse(st0.json()["authenticated"])
            self.assertIn("script-src 'self'", st0.headers.get("content-security-policy", ""))
            self.assertEqual(st0.headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(st0.headers.get("x-frame-options"), "DENY")
            self.assertEqual(st0.headers.get("cache-control"), "no-store")

            for protected_url in (
                "/api/overview",
                "/api/workbenches",
                "/api/sandbox/dbeaver",
                "/api/sandbox/prompts",
                "/api/license/info",
                "/api/license/history",
            ):
                r_unauth = client.get(protected_url)
                self.assertEqual(r_unauth.status_code, 401, protected_url)
                self.assertEqual(r_unauth.json()["code"], "ADMIN_AUTH_REQUIRED")

            # 2. 首次初始化账号
            r_setup = client.post(
                "/api/auth/setup",
                json={"username": "console_admin", "password": "TestPass!2026"},
            )
            self.assertEqual(r_setup.status_code, 200)
            self.assertTrue(r_setup.json()["ok"])
            self.assertIn(SESSION_COOKIE_NAME, client._cookies)

            # 二次调用 setup 必须拦截
            r_setup_again = client.post(
                "/api/auth/setup",
                json={"username": "attacker", "password": "AnotherPass!9"},
            )
            self.assertEqual(r_setup_again.status_code, 403)

            # 3. 跨站 CSRF 写请求拦截
            r_csrf = client.post(
                "/api/auth/logout",
                headers={"sec-fetch-site": "cross-site"},
            )
            self.assertEqual(r_csrf.status_code, 403)

            # 4. 修改密码并轮换会话
            r_bad_pw = client.post(
                "/api/auth/change-password",
                json={"old_password": "wrong", "new_password": "NewPass!2026"},
            )
            self.assertEqual(r_bad_pw.status_code, 400)

            old_tok = client._cookies[SESSION_COOKIE_NAME]
            r_chg = client.post(
                "/api/auth/change-password",
                json={"old_password": "TestPass!2026", "new_password": "NewPass!2026"},
            )
            self.assertEqual(r_chg.status_code, 200)
            new_tok = client._cookies[SESSION_COOKIE_NAME]
            self.assertNotEqual(old_tok, new_tok)

            # 5. 登出并验证旧会话失效
            r_out = client.post("/api/auth/logout")
            self.assertEqual(r_out.status_code, 200)
            self.assertNotIn(SESSION_COOKIE_NAME, client._cookies)
            self.assertEqual(client.get("/api/license/history").status_code, 401)

            # 旧密码无法再登录，新密码成功登录，连续失败 5 次触发 429 限流
            r_login_old = client.post(
                "/api/auth/login",
                json={"username": "console_admin", "password": "TestPass!2026"},
            )
            self.assertEqual(r_login_old.status_code, 401)

            for _ in range(4):
                r_fail = client.post(
                    "/api/auth/login",
                    json={"username": "console_admin", "password": "bad"},
                )
                self.assertEqual(r_fail.status_code, 401)

            r_locked = client.post(
                "/api/auth/login",
                json={"username": "console_admin", "password": "NewPass!2026"},
            )
            self.assertEqual(r_locked.status_code, 429)

            # auth manager 新客户端仍可正常登录
            ok, _msg, tok, st_code = auth.verify_login(
                "console_admin", "NewPass!2026", client_key="clean-ip"
            )
            self.assertTrue(ok)
            self.assertEqual(st_code, 200)
            self.assertIsNotNone(tok)

    def test_license_decode_issue_validation_history_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_keygen = os.path.join(tmpdir, "DataOG_Keygen")
            with open(fake_keygen, "w", encoding="utf-8") as fh:
                fh.write("stub")

            app, _auth, svc = self._make_isolated_app(tmpdir, keygen_bin=fake_keygen)
            client = TestClient(app, auto_activate=False)
            client.post("/api/auth/setup", json={"username": "adm", "password": "Pass!1234"})

            # 1. 解码客户申请码 REQ1-...
            req_code = encode_request_code("MCH-ABCD-1234-EF56-7890", "10.20.30.40", "BANK-HOST")
            r_dec = client.post("/api/license/decode-request", json={"request_code": req_code})
            self.assertEqual(r_dec.status_code, 200)
            self.assertEqual(r_dec.json()["device"]["machine_code"], "MCH-ABCD-1234-EF56-7890")
            self.assertEqual(r_dec.json()["device"]["primary_ip"], "10.20.30.40")

            # 2. 参数非法拦截（无shell注入风险）
            r_bad_ip = client.post(
                "/api/license/issue",
                json={"mode": "manual", "machine_code": "MCH-1111-2222", "ip_addr": "not-an-ip", "days": 30},
            )
            self.assertEqual(r_bad_ip.status_code, 400)

            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            r_bad_date = client.post(
                "/api/license/issue",
                json={"mode": "global", "until_date": yesterday},
            )
            self.assertEqual(r_bad_date.status_code, 400)

            # 3. 签发全局激活码并落入台账、支持查询与删除
            sample_code = issue_activation_code_for_seed(
                _TEST_LICENSE_SEED,
                scope=SCOPE_GLOBAL,
                duration_days=90,
                expiry_date=datetime.date.today() + datetime.timedelta(days=89),
            )

            class _ProcRes:
                returncode = 0
                stdout = sample_code + "\n"
                stderr = ""

            with mock.patch("tools.admin_console.service.subprocess.run", return_value=_ProcRes()) as m_run:
                r_issue = client.post(
                    "/api/license/issue",
                    json={"mode": "request", "request_code": req_code, "days": 180, "note": "启明银行PoC"},
                )
                self.assertEqual(r_issue.status_code, 200)
                rec = r_issue.json()["record"]
                self.assertEqual(rec["activation_code"], sample_code)
                self.assertEqual(rec["note"], "启明银行PoC")
                called_cmd = m_run.call_args[0][0]
                self.assertEqual(called_cmd[0], fake_keygen)
                self.assertIn("--request", called_cmd)
                self.assertIn("--days", called_cmd)
                self.assertIn("180", called_cmd)
                self.assertIn("--quiet", called_cmd)

            r_hist = client.get("/api/license/history")
            self.assertEqual(len(r_hist.json()["items"]), 1)
            item_id = r_hist.json()["items"][0]["id"]

            r_del = client.request("DELETE", f"/api/license/history/{item_id}")
            self.assertEqual(r_del.status_code, 200)
            self.assertEqual(client.get("/api/license/history").json()["items"], [])

            # 4. 一键激活指定目标（demo/main）路由校验
            with mock.patch.object(svc, "_build_ctx_for_target") as m_ctx:
                fake_ctx = mock.MagicMock()
                fake_ctx.license_hub.activate.return_value = {
                    "authorized": True,
                    "status": "ACTIVE",
                    "expires_date": "2027-01-01",
                }
                m_ctx.return_value = fake_ctx
                with mock.patch.object(svc, "_inspect_workbench", return_value={"running": False, "port": None}):
                    r_app = client.post(
                        "/api/license/apply",
                        json={"activation_code": sample_code, "target": "demo"},
                    )
                    self.assertEqual(r_app.status_code, 200)
                    self.assertTrue(r_app.json()["license"]["authorized"])
                    fake_ctx.license_hub.activate.assert_called_once_with(
                        sample_code, login_mode="activation_code"
                    )

    def test_dbeaver_connections_prompts_and_static_security_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _auth, _svc = self._make_isolated_app(tmpdir)
            client = TestClient(app, auto_activate=False)
            client.post("/api/auth/setup", json={"username": "adm", "password": "Pass!1234"})

            # 1. DBeaver 三库连接信息准确性校验
            r_dbv = client.get("/api/sandbox/dbeaver")
            self.assertEqual(r_dbv.status_code, 200)
            conns = {c["id"]: c for c in r_dbv.json()["connections"]}
            self.assertEqual(set(conns.keys()), {"qiming-dws", "qiming-inceptor", "qiming-mysql"})
            self.assertEqual(
                conns["qiming-dws"]["jdbc_url"],
                "jdbc:postgresql://127.0.0.1:15432/qiming_dws?currentSchema=dwd_bank",
            )
            self.assertEqual(
                conns["qiming-inceptor"]["jdbc_url"],
                "jdbc:hive2://127.0.0.1:11000/bank_inceptor",
            )
            self.assertNotIn("auth=noSasl", conns["qiming-inceptor"]["jdbc_url"])
            self.assertIn("allowPublicKeyRetrieval=true", conns["qiming-mysql"]["jdbc_url"])
            self.assertIn("useSSL=false", conns["qiming-mysql"]["jdbc_url"])

            # 2. 演练剧本提问词完整性校验
            r_prm = client.get("/api/sandbox/prompts")
            self.assertEqual(r_prm.status_code, 200)
            prompts = r_prm.json()["prompts"]
            self.assertEqual(len(prompts), 5)
            joined_expected = " ".join(p["expected"] for p in prompts)
            for token in ("16,800,000.00", "148,000,000.00", "2,389,200.00", "90,000,000.00", "36,500,000.00"):
                self.assertIn(token, joined_expected)

            # 3. 静态安全扫描：控制台前端零 innerHTML/阻塞弹窗/内联脚本/内联样式
            admin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            static_dir = os.path.join(admin_root, "tools", "admin_console", "static")
            with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as fh:
                html = fh.read()
            scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE)
            for body in scripts:
                self.assertEqual(body.strip(), "")
            self.assertNotIn("style=", html.lower())

            unsafe_dom = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b")
            blocking_dlg = re.compile(r"(?<!\.)\b(alert|confirm|prompt)\s*\(")
            with open(os.path.join(static_dir, "console.js"), encoding="utf-8") as jfh:
                for lineno, line in enumerate(jfh, start=1):
                    s = line.strip()
                    if s.startswith("//") or s.startswith("*"):
                        continue
                    self.assertIsNone(unsafe_dom.search(line), f"console.js:{lineno} 命中危险 DOM API")
                    self.assertIsNone(blocking_dlg.search(line), f"console.js:{lineno} 命中阻塞弹窗")

            # 4. 客户端与管理员工具严格隔离，禁止 app/core/modules 引入 admin_console
            for sub in ("app.py", "core", "modules"):
                target = os.path.join(admin_root, sub)
                paths = [target] if os.path.isfile(target) else glob.glob(
                    os.path.join(target, "**", "*.py"), recursive=True
                )
                for p in paths:
                    with open(p, encoding="utf-8") as pfh:
                        content = pfh.read()
                    self.assertNotIn("admin_console", content, f"客户端路径不得引用总控台: {p}")

            with open(os.path.join(admin_root, "build_exe.py"), encoding="utf-8") as bfh:
                self.assertIn('"tools"', bfh.read())
