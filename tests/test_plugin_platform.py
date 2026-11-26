"""Data.O.G 4.0 操作系统化外置插件平台与 Antigravity Init SDK 端到端测试."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from app import create_app, resolve_static_dir
from core.platform.context import PlatformContext
from core.platform.license_hub import (
    SCOPE_GLOBAL,
    ed25519_derive_pubkey,
    issue_activation_code_for_seed,
)
from core.platform.plugin_package import (
    PluginVerificationError,
    build_plugin_package,
    extract_inner_dogapp_bytes,
    verify_plugin_package_bytes,
)
from tests.test_modules_and_api import TestClient
from tools.export_plugin_sdk import export_plugin_sdk

_PLUGIN_SIGN_SEED = b"DataOG-40-Plugin-Test-Seed-00001"
_PLUGIN_SIGN_PUBKEY = ed25519_derive_pubkey(_PLUGIN_SIGN_SEED)


def _activate_app_ctx(ctx: PlatformContext) -> None:
    seed = b"dataog-plugin-platform-lic-seed1"
    ctx.license_hub._bind_test_pubkey(ed25519_derive_pubkey(seed))
    code = issue_activation_code_for_seed(
        seed,
        scope=SCOPE_GLOBAL,
        duration_days=90,
        expiry_date=datetime.date.today() + datetime.timedelta(days=89),
    )
    ctx.license_hub.activate(code)


class TestPluginPlatformV4(unittest.TestCase):
    def test_01_init_sdk_export_standalone_validation_unittest_and_gateway_safe_pack(self) -> None:
        """导出独立 Init SDK -> 在脱离主仓 PYTHONPATH 的子进程完成强合约校验、单元测试与签名打包."""
        with tempfile.TemporaryDirectory() as tmp:
            init_dir = os.path.join(tmp, "dq_demo_init")
            zip_out = os.path.join(tmp, "dq_demo_init.zip")
            exp_res = export_plugin_sdk(
                out_dir=init_dir,
                zip_path=zip_out,
                plugin_id="dq_demo",
                plugin_name="质检示范插件",
            )
            self.assertTrue(exp_res["ok"])
            self.assertTrue(os.path.isfile(zip_out))
            self.assertTrue(os.path.isfile(os.path.join(init_dir, ".dataog_sdk", "stubs", "core", "platform", "context.pyi")))
            self.assertTrue(os.path.isfile(os.path.join(init_dir, ".dataog_sdk", "frontend_api.d.ts")))
            self.assertTrue(os.path.isfile(os.path.join(init_dir, ".dataog_sdk", "CONTRACT.md")))
            self.assertTrue(os.path.isfile(os.path.join(init_dir, "AGENTS.md")))

            # 清空 PYTHONPATH 确保完全脱离 DataOG 主仓运行
            clean_env = dict(os.environ)
            clean_env.pop("PYTHONPATH", None)
            clean_env["DATAOG_UPDATE_SIGNING_KEY"] = _PLUGIN_SIGN_SEED.hex()

            # 1. 强合约校验
            val_proc = subprocess.run(
                [sys.executable, os.path.join(".dataog_sdk", "validate_plugin.py")],
                cwd=init_dir,
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(val_proc.returncode, 0, f"validate_plugin 失败:\n{val_proc.stdout}\n{val_proc.stderr}")
            self.assertIn("[VALIDATE] OK", val_proc.stdout)

            # 2. 离线单元测试
            ut_proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
                cwd=init_dir,
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(ut_proc.returncode, 0, f"Init 离线单测失败:\n{ut_proc.stdout}\n{ut_proc.stderr}")

            # 3. 签名打包（无主仓依赖）
            pack_proc = subprocess.run(
                [sys.executable, os.path.join(".dataog_sdk", "pack_plugin.py"), "--output-dir", "dist"],
                cwd=init_dir,
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(pack_proc.returncode, 0, f"pack_plugin 失败:\n{pack_proc.stdout}\n{pack_proc.stderr}")
            built_zip = os.path.join(init_dir, "dist", "DataOG-Plugin-dq_demo-v1.0.0.zip")
            built_dogapp = os.path.join(init_dir, "dist", "DataOG-Plugin-dq_demo-v1.0.0.dogapp")
            self.assertTrue(os.path.isfile(built_zip))
            self.assertTrue(os.path.isfile(built_dogapp))

            with open(built_dogapp, "rb") as fh:
                dogapp_bytes = fh.read()
            self.assertTrue(dogapp_bytes.startswith(b"DGAP"))
            self.assertNotIn(b"plugin.json", dogapp_bytes)
            self.assertNotIn(b"registerDesktopApp", dogapp_bytes)

            with open(built_zip, "rb") as fh:
                verified = verify_plugin_package_bytes(
                    fh.read(),
                    platform_version="4.0.0",
                    verifier_pubkey=_PLUGIN_SIGN_PUBKEY,
                )
            self.assertEqual(verified["manifest"]["plugin_id"], "dq_demo")

    def test_02_install_restart_crud_workspace_transfer_and_frontend_assets(self) -> None:
        """真实 Data.O.G 安装外置插件 -> 重启挂载 -> API CRUD -> .dataog.db 工作区导出导入 -> 前端静态与防穿越."""
        with tempfile.TemporaryDirectory() as tmp:
            init_dir = os.path.join(tmp, "init_plg")
            pkg_dir = os.path.join(tmp, "pkg_out")
            export_plugin_sdk(out_dir=init_dir, plugin_id="dq_demo", plugin_name="质检插件")
            pack_res = build_plugin_package(
                init_dir,
                output_dir=pkg_dir,
                seed=_PLUGIN_SIGN_SEED,
                allow_custom_seed=True,
            )
            with open(pack_res["zip_path"], "rb") as fh:
                zip_bytes = fh.read()

            vault = os.path.join(tmp, "vault")
            with mock.patch("update_bootstrap.resolve_verifier_pubkey", return_value=_PLUGIN_SIGN_PUBKEY):
                app1 = create_app(private_root=vault)
                _activate_app_ctx(app1.state.platform_ctx)
                client1 = TestClient(app1)

                ins = client1.post(
                    "/api/platform/plugins/inspect",
                    files={"file": ("dq_demo.zip", zip_bytes, "application/zip")},
                )
                self.assertEqual(ins.status_code, 200)
                self.assertTrue(ins.json()["compatible"])
                self.assertEqual(ins.json()["plugin_id"], "dq_demo")

                up = client1.post(
                    "/api/platform/plugins/upload",
                    files={"file": ("dq_demo.zip", zip_bytes, "application/zip")},
                )
                self.assertEqual(up.status_code, 200)
                self.assertTrue(up.json()["restart_required"])
                app1.state.platform_ctx.close()

                # 重启应用：自动验签加载 dq_demo
                app2 = create_app(private_root=vault)
                _activate_app_ctx(app2.state.platform_ctx)
                client2 = TestClient(app2)

                boot = client2.get("/api/platform/bootstrap").json()
                self.assertEqual(boot["platform_abi"], "4.0")
                plug_ids = [p["plugin_id"] for p in boot["plugins"]]
                self.assertIn("dq_demo", plug_ids)

                # 调用插件业务 API 创建记录并验证工作区持久化
                c_res = client2.post(
                    "/api/modules/dq_demo/notes",
                    json={"title": "贯标质检单", "content": "已挂载至 Data.O.G 4.0"},
                )
                self.assertEqual(c_res.status_code, 200)
                ov = client2.get("/api/modules/dq_demo/overview").json()
                self.assertEqual(len(ov["notes"]), 1)

                # 验证前端静态资源服务与路径穿越拦截
                js_resp = client2.get("/plugins/dq_demo/register.js")
                self.assertEqual(js_resp.status_code, 200)
                self.assertIn("registerDesktopApp", js_resp.text)
                self.assertIn("no-store", js_resp.headers.get("cache-control", ""))
                self.assertEqual(js_resp.headers.get("x-content-type-options"), "nosniff")

                trav_resp = client2.get("/plugins/dq_demo/../backend/__init__.py")
                self.assertEqual(trav_resp.status_code, 404)

                # 验证工作区 .dataog.db 导出与导入完整携带外置插件表
                exp_bytes, _ = app2.state.platform_ctx.workspace_hub.export_workspace_bytes()
                imp = app2.state.platform_ctx.workspace_hub.import_workspace_as_new(
                    filename="dq_ws.dataog.db",
                    raw_bytes=exp_bytes,
                    name="共享插件工作区",
                    switch_immediately=True,
                )
                self.assertTrue(imp["workspace"]["is_active"])
                ov_after = client2.get("/api/modules/dq_demo/overview").json()
                self.assertEqual(len(ov_after["notes"]), 1)
                self.assertNotIn("rt_plg", json.dumps(client2.get("/api/platform/plugins/status").json()))
                app2.state.platform_ctx.close()

    def test_03_tamper_cross_protocol_replay_binary_forbidden_and_scan_dir(self) -> None:
        """验证篡改拦截、DGP2 系统包跨协议防重放、禁止二进制/保留 ID 以及 plugins/ 目录吸纳."""
        import build_update_pack

        with tempfile.TemporaryDirectory() as tmp:
            init_dir = os.path.join(tmp, "init_sec")
            pkg_dir = os.path.join(tmp, "pkg_sec")
            export_plugin_sdk(out_dir=init_dir, plugin_id="sec_probe", plugin_name="安全探针")
            pack_res = build_plugin_package(
                init_dir,
                output_dir=pkg_dir,
                seed=_PLUGIN_SIGN_SEED,
                allow_custom_seed=True,
            )
            with open(pack_res["dogapp_path"], "rb") as fh:
                dogapp_bytes = bytearray(fh.read())

            # 1. 篡改密文末字节必须触发 BAD_SIGNATURE
            tampered = bytearray(dogapp_bytes)
            tampered[-1] ^= 0xFF
            with self.assertRaises(PluginVerificationError) as ctx_err:
                verify_plugin_package_bytes(bytes(tampered), verifier_pubkey=_PLUGIN_SIGN_PUBKEY)
            self.assertEqual(ctx_err.exception.code, "BAD_SIGNATURE")

            # 2. 跨协议重放：拿系统增量包 DGP2 充当插件上传必须被拒绝
            dgp2_zip_info = build_update_pack.build_update_pack(
                source_dir=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                output_dir=os.path.join(tmp, "upd_out"),
                seed=_PLUGIN_SIGN_SEED,
                allow_custom_seed=True,
            )
            with open(dgp2_zip_info["zip_path"], "rb") as fh:
                dgp2_zip_bytes = fh.read()
            with self.assertRaises(PluginVerificationError) as replay_err:
                extract_inner_dogapp_bytes(dgp2_zip_bytes)
            self.assertEqual(replay_err.exception.code, "WRONG_PACKAGE_TYPE")

            # 3. 禁止包含 .pyd 编译态二进制文件
            bad_bin = os.path.join(init_dir, "backend", "evil.pyd")
            with open(bad_bin, "wb") as fh:
                fh.write(b"MZ_FAKE_PYD")
            with self.assertRaises(PluginVerificationError) as bin_err:
                build_plugin_package(init_dir, output_dir=pkg_dir, seed=_PLUGIN_SIGN_SEED, allow_custom_seed=True)
            self.assertEqual(bin_err.exception.code, "FORBIDDEN_CONTENT")
            os.remove(bad_bin)

            # 4. 禁止冒用保留 plugin_id
            with open(os.path.join(init_dir, "plugin.json"), "r", encoding="utf-8") as fh:
                m_obj = json.load(fh)
            m_obj["plugin_id"] = "ai_assistant"
            with open(os.path.join(init_dir, "plugin.json"), "w", encoding="utf-8") as fh:
                json.dump(m_obj, fh)
            with self.assertRaises(PluginVerificationError) as res_err:
                build_plugin_package(init_dir, output_dir=pkg_dir, seed=_PLUGIN_SIGN_SEED, allow_custom_seed=True)
            self.assertEqual(res_err.exception.code, "RESERVED_ID")

            # 5. 验证 <install_dir>/plugins/ 自动吸纳
            vault = os.path.join(tmp, "vault_scan")
            fake_install = os.path.join(tmp, "install_dir")
            os.makedirs(os.path.join(fake_install, "plugins"), exist_ok=True)
            shutil.copy2(pack_res["zip_path"], os.path.join(fake_install, "plugins", "sec_probe.zip"))

            with mock.patch("update_bootstrap.resolve_verifier_pubkey", return_value=_PLUGIN_SIGN_PUBKEY), mock.patch.dict(
                os.environ, {"DATAOG_INSTALL_DIR": fake_install}
            ):
                app = create_app(private_root=vault)
                _activate_app_ctx(app.state.platform_ctx)
                client = TestClient(app)
                st = client.get("/api/platform/plugins/status").json()["plugin_hub"]
                self.assertEqual(st["active_count"], 1)
                self.assertEqual(st["plugins"][0]["plugin_id"], "sec_probe")
                app.state.platform_ctx.close()

    def test_04_fault_quarantine_rollback_and_safe_uninstall_purge(self) -> None:
        """验证故障插件自动隔离自愈、升级回退、卸载保留表 vs purge清表，且绝对禁止删除系统表."""
        with tempfile.TemporaryDirectory() as tmp:
            # 先构建 v1.0.0 正常插件与 v1.1.0 故障插件（init_hook 故意抛异常）
            plg_dir = os.path.join(tmp, "faulty_plg")
            pkg_out = os.path.join(tmp, "pkg_fault")
            export_plugin_sdk(out_dir=plg_dir, plugin_id="fault_demo", plugin_name="自愈测试插件")
            v1_pack = build_plugin_package(plg_dir, output_dir=pkg_out, seed=_PLUGIN_SIGN_SEED, allow_custom_seed=True)

            # 升级为 v1.1.0 并注入 init 异常
            with open(os.path.join(plg_dir, "plugin.json"), "r", encoding="utf-8") as fh:
                mj = json.load(fh)
            mj["version"] = "1.1.0"
            with open(os.path.join(plg_dir, "plugin.json"), "w", encoding="utf-8") as fh:
                json.dump(mj, fh)
            with open(os.path.join(plg_dir, "backend", "__init__.py"), "r", encoding="utf-8") as fh:
                init_src = fh.read()
            init_src = init_src.replace(
                "PluginService.ensure_schema(ctx.store)",
                'raise RuntimeError("模拟插件初始化崩溃")',
            )
            with open(os.path.join(plg_dir, "backend", "__init__.py"), "w", encoding="utf-8") as fh:
                fh.write(init_src)
            v2_pack = build_plugin_package(plg_dir, output_dir=pkg_out, seed=_PLUGIN_SIGN_SEED, allow_custom_seed=True)

            vault = os.path.join(tmp, "vault_q")
            with mock.patch("update_bootstrap.resolve_verifier_pubkey", return_value=_PLUGIN_SIGN_PUBKEY):
                # 1. 安装 v1.0.0 并重启写一条数据
                app1 = create_app(private_root=vault)
                _activate_app_ctx(app1.state.platform_ctx)
                c1 = TestClient(app1)
                with open(v1_pack["zip_path"], "rb") as fh:
                    c1.post("/api/platform/plugins/upload", files={"file": ("v1.zip", fh.read(), "application/zip")})
                app1.state.platform_ctx.close()

                app2 = create_app(private_root=vault)
                _activate_app_ctx(app2.state.platform_ctx)
                c2 = TestClient(app2)
                c2.post("/api/modules/fault_demo/notes", json={"title": "重要业务记录", "content": "keep"})
                # 升级到故意崩溃的 v1.1.0
                with open(v2_pack["zip_path"], "rb") as fh:
                    c2.post("/api/platform/plugins/upload", files={"file": ("v2.zip", fh.read(), "application/zip")})
                app2.state.platform_ctx.close()

                # 2. 重启后 v1.1.0 必须被自动隔离而不影响平台与 5 大内置模块
                app3 = create_app(private_root=vault)
                _activate_app_ctx(app3.state.platform_ctx)
                c3 = TestClient(app3)
                boot3 = c3.get("/api/platform/bootstrap").json()
                builtin_ids = {m["module_id"] for m in boot3["modules"]}
                self.assertTrue({"standard_migrator", "cross_comparator", "ddl_translator", "data_explorer", "ai_assistant"}.issubset(builtin_ids))
                st3 = c3.get("/api/platform/plugins/status").json()["plugin_hub"]
                self.assertEqual(st3["quarantined_count"], 1)
                p3 = st3["plugins"][0]
                self.assertTrue(p3["quarantined"])
                self.assertTrue(p3["can_rollback"])
                self.assertEqual(p3["previous_version"], "1.0.0")

                # 3. 回退到 v1.0.0 并验证业务数据表依然完好
                rb = c3.post("/api/platform/plugins/fault_demo/rollback")
                self.assertEqual(rb.status_code, 200)
                app3.state.platform_ctx.close()

                app4 = create_app(private_root=vault)
                _activate_app_ctx(app4.state.platform_ctx)
                c4 = TestClient(app4)
                ov4 = c4.get("/api/modules/fault_demo/overview").json()
                self.assertEqual(len(ov4["notes"]), 1)

                # 4. 默认卸载保留表 vs purge_data=True 清表（且 drop_plugin_tables 严禁删系统表）
                with self.assertRaises(ValueError):
                    app4.state.platform_ctx.store.drop_plugin_tables(workspace_tables=["standard_dictionary"])
                with self.assertRaises(ValueError):
                    app4.state.platform_ctx.store.drop_plugin_tables(private_tables=["llm_providers"])

                un1 = c4.delete("/api/platform/plugins/fault_demo?purge_data=true")
                self.assertEqual(un1.status_code, 200)
                app4.state.platform_ctx.close()

                # 重启后完成挂起卸载并确认插件业务表已清理、系统表完好
                app5 = create_app(private_root=vault)
                _activate_app_ctx(app5.state.platform_ctx)
                c5 = TestClient(app5)
                st5 = c5.get("/api/platform/plugins/status").json()["plugin_hub"]
                self.assertEqual(st5["installed_count"], 0)
                with app5.state.platform_ctx.store._get_workspace_conn() as wconn:
                    tbls = {r["name"] for r in wconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                self.assertNotIn("plg_fault_demo_notes", tbls)
                self.assertIn("standard_dictionary", tbls)
                app5.state.platform_ctx.close()

    def test_05_dev_plugin_hot_mount_and_frozen_rejection(self) -> None:
        """验证源码态 --dev-plugin 免签热挂载，以及冻结态严格拒绝未签名开发插件."""
        with tempfile.TemporaryDirectory() as tmp:
            dev_dir = os.path.join(tmp, "dev_hot_plg")
            export_plugin_sdk(out_dir=dev_dir, plugin_id="hot_dev", plugin_name="热挂测试")
            vault = os.path.join(tmp, "vault_dev")

            app = create_app(private_root=vault, dev_plugin_dirs=[dev_dir])
            _activate_app_ctx(app.state.platform_ctx)
            client = TestClient(app)
            ov = client.get("/api/modules/hot_dev/overview")
            self.assertEqual(ov.status_code, 200)
            st = client.get("/api/platform/plugins/status").json()["plugin_hub"]
            self.assertEqual(st["dev_count"], 1)
            self.assertTrue(st["plugins"][0]["dev_mode"])
            app.state.platform_ctx.close()

            # 模拟 sys.frozen=True 冻结态：必须拒绝加载未签名开发目录
            vault_f = os.path.join(tmp, "vault_frozen")
            with mock.patch.object(sys, "frozen", True, create=True):
                app_f = create_app(private_root=vault_f, dev_plugin_dirs=[dev_dir])
                _activate_app_ctx(app_f.state.platform_ctx)
                st_f = TestClient(app_f).get("/api/platform/plugins/status").json()["plugin_hub"]
                self.assertEqual(st_f["dev_count"], 0)
                self.assertEqual(st_f["active_count"], 0)
                app_f.state.platform_ctx.close()

    def test_06_frontend_dynamic_loader_and_plugins_view_xss_guard(self) -> None:
        """静态扫防线 + Node.js 运行时验证 loadExternalPluginApps 路径校验与故障隔离."""
        static_dir = resolve_static_dir()
        plugins_view_path = os.path.join(static_dir, "js", "views", "plugins_view.js")
        with open(plugins_view_path, encoding="utf-8") as fh:
            pv_src = fh.read()
        self.assertIsNone(
            re.search(r"\.\s*(?:innerHTML|outerHTML|insertAdjacentHTML)\b|\bwindow\.(?:alert|confirm|prompt)\s*\(", pv_src)
        )

        if getattr(sys, "frozen", False) or not shutil.which("node"):
            return

        project_root = os.path.dirname(static_dir)
        node_script = r"""
import path from "node:path";
import { pathToFileURL } from "node:url";

globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
globalThis.document = {
  head: { children: [], appendChild() {} },
  createElement: () => ({ setAttribute() {}, style: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, appendChild() {} }),
  createElementNS: () => ({ setAttribute() {}, appendChild() {} }),
  createTextNode: (t) => ({ textContent: String(t) }),
  querySelectorAll: () => [],
};
globalThis.Node = class {};

const root = process.argv[1];
const { loadExternalPluginApps } = await import(pathToFileURL(path.join(root, "static/js/core/app_registry.js")).href);

const res = await loadExternalPluginApps([
  { plugin_id: "evil_path", entry_url: "/plugins/evil/../secret.js" },
  { plugin_id: "missing_plg", entry_url: "/plugins/not_exist_123/register.js" },
]);
if (res.loaded.length !== 0 || res.failed.length !== 2) {
  throw new Error(`Unexpected result: ${JSON.stringify(res)}`);
}
if (!res.failed[0].error.includes("非法")) {
  throw new Error("Path traversal entry_url was not blocked");
}
console.log("EXTERNAL_PLUGIN_LOADER_OK");
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, project_root],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, f"Node 验证失败:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("EXTERNAL_PLUGIN_LOADER_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
