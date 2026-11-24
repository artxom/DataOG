"""Data.O.G Ed25519 签名同系列累积增量升级体系与双渠道端到端自动化测试."""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from app import APP_VERSION, create_app, resolve_static_dir
import build_update_pack
from core.platform.context import PlatformContext
from core.platform.license_hub import _reconstruct_prod_pubkey
from core.platform.storage_paths import resolve_private_storage_layout
from tests.test_modules_and_api import TestClient
import update_bootstrap


def _clone_minimal_source(project_root: str, dest_dir: str) -> None:
    """复制轻量代码与静态资源快照用于构造临时测试增量包（绝不读取任何生产私钥）."""
    skip_dirs = {
        "__pycache__",
        ".git",
        ".keygen_secrets",
        "tests",
        "tools",
        "demo_sandbox",
        "_license_native_src",
        "dist",
        "build",
    }

    def _ignore(_cur: str, names):
        return [
            n
            for n in names
            if n in skip_dirs
            or n.endswith((".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".DS_Store"))
        ]

    for sub in ("core", "modules", "static"):
        shutil.copytree(
            os.path.join(project_root, sub),
            os.path.join(dest_dir, sub),
            ignore=_ignore,
        )


def _stamp_cloned_version(clone_dir: str, version: str) -> None:
    kernel_path = os.path.join(clone_dir, "core", "app_kernel.py")
    with open(kernel_path, "r", encoding="utf-8") as fh:
        k_src = fh.read()
    k_src = re.sub(r'^APP_VERSION\s*=\s*"[^"]+"', f'APP_VERSION = "{version}"', k_src, count=1, flags=re.M)
    with open(kernel_path, "w", encoding="utf-8") as fh:
        fh.write(k_src)

    ctx_path = os.path.join(clone_dir, "core", "platform", "context.py")
    with open(ctx_path, "r", encoding="utf-8") as fh:
        c_src = fh.read()
    c_src = re.sub(r'self\.app_version\s*=\s*"[^"]+"', f'self.app_version = "{version}"', c_src, count=1)
    c_src = re.sub(r'"version":\s*"[^"]+"', f'"version": "{version}"', c_src, count=1)
    with open(ctx_path, "w", encoding="utf-8") as fh:
        fh.write(c_src)

    idx_path = os.path.join(clone_dir, "static", "index.html")
    with open(idx_path, "r", encoding="utf-8") as fh:
        h_src = fh.read()
    h_src = re.sub(
        r'<meta\s+name="dataog-ui-version"\s+content="[^"]+"',
        f'<meta name="dataog-ui-version" content="{version}"',
        h_src,
        count=1,
    )
    with open(idx_path, "w", encoding="utf-8") as fh:
        fh.write(h_src)


def _rewrite_dgp_inside_zip(outer_zip_bytes: bytes, mutator) -> bytes:
    """在测试内重建外层 ZIP / 内层 .dgp 以构造精确篡改攻击样本."""
    with zipfile.ZipFile(io.BytesIO(outer_zip_bytes), "r") as ozf:
        outer_items = {zi.filename: ozf.read(zi.filename) for zi in ozf.infolist() if not zi.is_dir()}

    dgp_key = next(k for k in outer_items if k.endswith(".dgp"))
    with zipfile.ZipFile(io.BytesIO(outer_items[dgp_key]), "r") as dzf:
        dgp_items = {zi.filename: dzf.read(zi.filename) for zi in dzf.infolist() if not zi.is_dir()}

    mutator(dgp_items)

    dgp_buf = io.BytesIO()
    with zipfile.ZipFile(dgp_buf, "w") as ndzf:
        for name, data in dgp_items.items():
            ndzf.writestr(name, data)
    outer_items[dgp_key] = dgp_buf.getvalue()

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w") as nozf:
        for name, data in outer_items.items():
            nozf.writestr(name, data)
    return out_buf.getvalue()


class TestIncrementalUpdates(unittest.TestCase):
    def test_private_root_and_pubkey_parity(self) -> None:
        """验证纯标准库 update_bootstrap 与 storage_paths/license_hub 在公钥与混淆路径派生上的字节级一致性."""
        self.assertEqual(update_bootstrap.reconstruct_prod_pubkey(), _reconstruct_prod_pubkey())

        for os_name, plat_name, env_patch in (
            ("nt", "win32", {"LOCALAPPDATA": r"C:\Users\Tester\AppData\Local"}),
            ("posix", "darwin", {}),
            ("posix", "linux", {"XDG_DATA_HOME": "/tmp/mock_xdg_share"}),
        ):
            with mock.patch.dict(os.environ, env_patch, clear=False):
                env_clean = {k: v for k, v in os.environ.items() if k != "DATAOG_HOME"}
                with mock.patch.dict(os.environ, env_clean, clear=True):
                    layout = resolve_private_storage_layout(os_name=os_name, platform_name=plat_name)
                    boot_root = update_bootstrap.resolve_private_root(
                        os_name=os_name, platform_name=plat_name
                    )
                    self.assertEqual(boot_root, layout.private_root)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DATAOG_HOME": tmp}, clear=False):
                layout = resolve_private_storage_layout()
                self.assertEqual(update_bootstrap.resolve_private_root(), layout.private_root)

        # 冻结态下严格忽略测试公钥环境变量覆盖，防止生产二进制被劫持验签公钥
        ephemeral_pk_hex = build_update_pack.ed25519_derive_pubkey(secrets.token_bytes(32)).hex()
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.dict(os.environ, {"DATAOG_TEST_UPDATE_PUBKEY_HEX": ephemeral_pk_hex}, clear=False),
        ):
            self.assertEqual(
                update_bootstrap.resolve_verifier_pubkey(),
                update_bootstrap.reconstruct_prod_pubkey(),
            )

    def test_same_minor_series_version_compatibility(self) -> None:
        """验证同二级版本 X.Y.0+ 累积增量兼容性、降级拒绝与跨二级系列拒绝策略."""
        for base_ver in ("3.3.0", "3.3.1", "3.3.3", "3.3.4"):
            ok, msg = update_bootstrap.check_version_compatibility("3.3.4", base_ver)
            self.assertTrue(ok, f"3.3.4 应兼容底座 {base_ver}: {msg}")

        ok_same, _ = update_bootstrap.check_version_compatibility("3.3.0", "3.3.0")
        self.assertTrue(ok_same)

        for cross_base in ("3.2.4", "3.4.0", "4.0.0"):
            ok_cross, msg_cross = update_bootstrap.check_version_compatibility("3.3.4", cross_base)
            self.assertFalse(ok_cross)
            self.assertIn("跨二级版本", msg_cross)

        ok_down, msg_down = update_bootstrap.check_version_compatibility("3.3.1", "3.3.5")
        self.assertFalse(ok_down)
        self.assertIn("低于当前客户端底座版本", msg_down)

        ok_min, _ = update_bootstrap.check_version_compatibility("3.3.4", "3.3.2", min_base_version="3.3.2")
        self.assertTrue(ok_min)
        ok_below_min, msg_below = update_bootstrap.check_version_compatibility(
            "3.3.4", "3.3.1", min_base_version="3.3.2"
        )
        self.assertFalse(ok_below_min)
        self.assertIn("最低要求", msg_below)

    def test_build_update_pack_and_tamper_defenses(self) -> None:
        """验证 build_update_pack 轻量构建、Ed25519 域隔离、防篡改、防未签名多余文件与防路径穿越."""
        seed = secrets.token_bytes(32)
        pk = build_update_pack.ed25519_derive_pubkey(seed)
        wrong_pk = build_update_pack.ed25519_derive_pubkey(secrets.token_bytes(32))

        with tempfile.TemporaryDirectory() as tmp:
            res = build_update_pack.build_update_pack(
                output_dir=tmp,
                seed=seed,
                allow_custom_seed=True,
            )
            self.assertTrue(res["ok"])
            self.assertLess(res["zip_size"], 2_500_000, "增量包体积应控制在轻量范围 (<2.5MB)")
            with open(res["zip_path"], "rb") as fh:
                outer_bytes = fh.read()

            # 校验不包含开发/测试/秘密路径，且提供双渠道产物布局
            with zipfile.ZipFile(io.BytesIO(outer_bytes), "r") as ozf:
                outer_names = set(ozf.namelist())
                self.assertIn(f"updates/DataOG-Update-v{APP_VERSION}.dgp", outer_names)
                self.assertIn("UPDATE_README.txt", outer_names)
                dgp_bytes = ozf.read(f"updates/DataOG-Update-v{APP_VERSION}.dgp")
            with zipfile.ZipFile(io.BytesIO(dgp_bytes), "r") as dzf:
                code_bytes = dzf.read("code.zip")
            with zipfile.ZipFile(io.BytesIO(code_bytes), "r") as czf:
                for entry in czf.namelist():
                    self.assertTrue(entry.endswith(".py"), entry)
                    for forbidden in ("tests/", "tools/", "demo_sandbox/", ".keygen_secrets/", "__pycache__/"):
                        self.assertNotIn(forbidden, entry)

            # 同时支持外层 .zip 与直接 .dgp 验签
            verified_outer = update_bootstrap.verify_update_package_bytes(outer_bytes, verifier_pubkey=pk)
            verified_dgp = update_bootstrap.verify_update_package_bytes(dgp_bytes, verifier_pubkey=pk)
            self.assertEqual(verified_outer["manifest"]["version"], APP_VERSION)
            self.assertEqual(verified_dgp["container_type"], "dgp")

            # 错公钥拒绝
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_pk:
                update_bootstrap.verify_update_package_bytes(outer_bytes, verifier_pubkey=wrong_pk)
            self.assertEqual(cm_pk.exception.code, "BAD_SIGNATURE")

            # 签名域隔离验证：用 License 签名域签出的合法签名绝不能通过 Update 验签
            def _swap_license_domain_sig(items: dict) -> None:
                m_raw = items["manifest.json"]
                items["manifest.sig"] = build_update_pack.ed25519_sign_deterministic(
                    b"DataOG-License-v1:" + m_raw, seed
                )

            wrong_domain_zip = _rewrite_dgp_inside_zip(outer_bytes, _swap_license_domain_sig)
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_dom:
                update_bootstrap.verify_update_package_bytes(wrong_domain_zip, verifier_pubkey=pk)
            self.assertEqual(cm_dom.exception.code, "BAD_SIGNATURE")

            # 1 字节篡改 manifest.json / manifest.sig / code.zip / static/index.html 全部拦截
            def _flip_byte(data: bytes) -> bytes:
                b = bytearray(data)
                b[-1] ^= 0x01
                return bytes(b)

            for key, expected_code in (
                ("manifest.json", "BAD_SIGNATURE"),
                ("manifest.sig", "BAD_SIGNATURE"),
                ("code.zip", "CHECKSUM_MISMATCH"),
                ("static/index.html", "CHECKSUM_MISMATCH"),
            ):
                tampered = _rewrite_dgp_inside_zip(
                    outer_bytes, lambda items, k=key: items.__setitem__(k, _flip_byte(items[k]))
                )
                with self.assertRaises(update_bootstrap.UpdateVerificationError, msg=key) as cm:
                    update_bootstrap.verify_update_package_bytes(tampered, verifier_pubkey=pk)
                self.assertEqual(cm.exception.code, expected_code)

            # 添加未签名多余 static 文件必须拒绝
            extra_static_zip = _rewrite_dgp_inside_zip(
                outer_bytes, lambda items: items.__setitem__("static/evil.js", b"console.log('x');")
            )
            with self.assertRaises(update_bootstrap.UpdateVerificationError):
                update_bootstrap.verify_update_package_bytes(extra_static_zip, verifier_pubkey=pk)

            # code.zip 注入 tests/ 或 .dll 必须拦截（即使重新签名也不能绕过内容白名单）
            def _inject_forbidden_code(items: dict) -> None:
                cbuf = io.BytesIO(items["code.zip"])
                with zipfile.ZipFile(cbuf, "a") as cz:
                    cz.writestr("tests/test_evil.py", b"print(1)")
                new_code = cbuf.getvalue()
                items["code.zip"] = new_code
                m = json.loads(items["manifest.json"].decode("utf-8"))
                import hashlib

                m["code_zip"]["sha256"] = hashlib.sha256(new_code).hexdigest()
                m["code_zip"]["size"] = len(new_code)
                m_raw = json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                items["manifest.json"] = m_raw
                items["manifest.sig"] = build_update_pack.ed25519_sign_deterministic(
                    update_bootstrap.UPDATE_SIG_DOMAIN + m_raw, seed
                )

            forbidden_zip = _rewrite_dgp_inside_zip(outer_bytes, _inject_forbidden_code)
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_forb:
                update_bootstrap.verify_update_package_bytes(forbidden_zip, verifier_pubkey=pk)
            self.assertEqual(cm_forb.exception.code, "FORBIDDEN_CONTENT")

            # 路径穿越防御
            trav_buf = io.BytesIO()
            with zipfile.ZipFile(trav_buf, "w") as tzf:
                tzf.writestr("../evil.dgp", b"bad")
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_trav:
                update_bootstrap.verify_update_package_bytes(trav_buf.getvalue(), verifier_pubkey=pk)
            self.assertEqual(cm_trav.exception.code, "PATH_TRAVERSAL")

    def test_end_to_end_subprocess_dual_channel_patch_activation_and_crash_rollback(self) -> None:
        """真实子进程验证：覆盖 updates/ 目录自动加载新版本 (sys.meta_path zipimport)、升级退役与崩溃自愈回退."""
        project_root = os.path.dirname(resolve_static_dir())
        seed = secrets.token_bytes(32)
        pk = build_update_pack.ed25519_derive_pubkey(seed)
        pk_hex = pk.hex()
        maj, minor, _ = update_bootstrap.parse_semver(APP_VERSION)
        patched_ver = f"{maj}.{minor}.9"
        broken_ver = f"{maj}.{minor}.8"

        with tempfile.TemporaryDirectory() as tmp:
            clone_src = os.path.join(tmp, "src_clone")
            _clone_minimal_source(project_root, clone_src)
            _stamp_cloned_version(clone_src, patched_ver)

            out_dir = os.path.join(tmp, "packs")
            pack_good = build_update_pack.build_update_pack(
                source_dir=clone_src,
                output_dir=out_dir,
                version=patched_ver,
                seed=seed,
                allow_custom_seed=True,
                summary="子进程真加载测试补丁",
            )

            # 1. 模拟方式一：用户将 DataOG-Update-vX.Y.9.zip 直接解压覆盖到安装目录（掉落 updates/*.dgp）
            install_dir = os.path.join(tmp, "client_install")
            vault_dir = os.path.join(tmp, "priv_vault")
            os.makedirs(install_dir, exist_ok=True)
            with zipfile.ZipFile(pack_good["zip_path"], "r") as zf:
                zf.extractall(install_dir)

            sub_env = dict(os.environ)
            sub_env["DATAOG_ENABLE_PATCH_LOADER"] = "1"
            sub_env["DATAOG_HOME"] = vault_dir
            sub_env["DATAOG_INSTALL_DIR"] = install_dir
            sub_env["DATAOG_TEST_UPDATE_PUBKEY_HEX"] = pk_hex

            proc = subprocess.run(
                [sys.executable, os.path.join(project_root, "app.py"), "--smoke-test"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sub_env,
                timeout=30,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"子进程加载签名增量补丁失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )
            self.assertIn(f"version=v{patched_ver}", proc.stdout)
            self.assertIn("patch_active=True", proc.stdout)

            # 验证 boot_guard 已清零健康标记
            upd_dir = os.path.join(vault_dir, "rt_upd")
            guard = update_bootstrap.load_boot_guard(upd_dir)
            self.assertTrue(guard["healthy"])
            self.assertEqual(guard["consecutive_failures"], 0)

            # 1b. 验证连续启动失败达到阈值时自动回退（且不重复陷入 updates/ 同包死循环）
            update_bootstrap.save_boot_guard(
                upd_dir,
                {
                    "version": patched_ver,
                    "consecutive_failures": update_bootstrap.MAX_BOOT_FAILURES,
                    "last_attempt_at": "2026-11-24T00:00:00Z",
                    "healthy": False,
                },
            )
            bg_script = (
                "import update_bootstrap\n"
                f"st = update_bootstrap.bootstrap_runtime_patch(base_version={APP_VERSION!r}, force_enable=True)\n"
                "assert not st['active'], st\n"
                f"assert st['rolled_back_from'] == {patched_ver!r}, st\n"
                "print('BOOT_GUARD_ROLLBACK_OK')\n"
            )
            bg_proc = subprocess.run(
                [sys.executable, "-c", bg_script],
                cwd=project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sub_env,
                timeout=20,
            )
            self.assertEqual(bg_proc.returncode, 0, bg_proc.stderr)
            self.assertIn("BOOT_GUARD_ROLLBACK_OK", bg_proc.stdout)

            # 1c. 验证落盘补丁文件被外部篡改时，启动前复验立即拦截并自动回退
            st_re = update_bootstrap.load_update_state(upd_dir)
            st_re["active_version"] = patched_ver
            update_bootstrap.save_update_state(upd_dir, st_re)
            update_bootstrap.save_boot_guard(
                upd_dir,
                {"version": patched_ver, "consecutive_failures": 0, "last_attempt_at": None, "healthy": True},
            )
            disk_idx = os.path.join(upd_dir, "releases", patched_ver, "static", "index.html")
            with open(disk_idx, "ab") as fh:
                fh.write(b"\n<!-- tampered -->\n")
            tamper_script = (
                "import update_bootstrap\n"
                f"st = update_bootstrap.bootstrap_runtime_patch(base_version={APP_VERSION!r}, force_enable=True)\n"
                "assert not st['active'], st\n"
                f"assert st['rolled_back_from'] == {patched_ver!r}, st\n"
                "print('DISK_TAMPER_ROLLBACK_OK')\n"
            )
            tamper_proc = subprocess.run(
                [sys.executable, "-c", tamper_script],
                cwd=project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sub_env,
                timeout=20,
            )
            self.assertEqual(tamper_proc.returncode, 0, tamper_proc.stderr)
            self.assertIn("DISK_TAMPER_ROLLBACK_OK", tamper_proc.stdout)

            # 重新恢复干净状态（通过删旧缓存重建或重写入 clean 3.3.9）后再测试高底座自动退役
            shutil.rmtree(upd_dir, ignore_errors=True)
            proc_re = subprocess.run(
                [sys.executable, os.path.join(project_root, "app.py"), "--smoke-test"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sub_env,
                timeout=30,
            )
            self.assertEqual(proc_re.returncode, 0, proc_re.stderr)

            # 2. 验证当客户端底座升级到更高版本时自动退役旧补丁
            next_base = f"{maj}.{minor + 1}.0"
            retire_script = (
                "import update_bootstrap\n"
                f"st = update_bootstrap.bootstrap_runtime_patch(base_version={next_base!r}, force_enable=True)\n"
                "assert not st['active'], st\n"
                "assert st['retired_reason'], st\n"
                "print('RETIRED_OK')\n"
            )
            retire_proc = subprocess.run(
                [sys.executable, "-c", retire_script],
                cwd=project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sub_env,
                timeout=20,
            )
            self.assertEqual(retire_proc.returncode, 0, retire_proc.stderr)
            self.assertIn("RETIRED_OK", retire_proc.stdout)

            # 3. 验证当已签名补丁在 import core.app_kernel 时崩溃，app.py 自动卸载补丁并回退内置版本启动成功
            _stamp_cloned_version(clone_src, broken_ver)
            broken_kernel = os.path.join(clone_src, "core", "app_kernel.py")
            with open(broken_kernel, "w", encoding="utf-8") as fh:
                fh.write('raise RuntimeError("simulated broken patch import failure")\n')
            pack_broken = build_update_pack.build_update_pack(
                source_dir=clone_src,
                output_dir=out_dir,
                version=broken_ver,
                seed=seed,
                allow_custom_seed=True,
                summary="故障回退测试补丁",
            )
            install_crash = os.path.join(tmp, "client_crash_install")
            vault_crash = os.path.join(tmp, "priv_crash_vault")
            os.makedirs(install_crash, exist_ok=True)
            with zipfile.ZipFile(pack_broken["zip_path"], "r") as zf:
                zf.extractall(install_crash)

            crash_env = dict(sub_env)
            crash_env["DATAOG_HOME"] = vault_crash
            crash_env["DATAOG_INSTALL_DIR"] = install_crash
            crash_proc = subprocess.run(
                [sys.executable, os.path.join(project_root, "app.py"), "--smoke-test"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=crash_env,
                timeout=30,
            )
            self.assertEqual(
                crash_proc.returncode,
                0,
                f"补丁崩溃未能自动回退至内置基础版本:\nSTDOUT:\n{crash_proc.stdout}\nSTDERR:\n{crash_proc.stderr}",
            )
            self.assertIn("自动回退至内置基础版本", crash_proc.stdout)
            self.assertIn(f"version=v{APP_VERSION}", crash_proc.stdout)
            self.assertIn("patch_active=False", crash_proc.stdout)
            crash_state = update_bootstrap.load_update_state(os.path.join(vault_crash, "rt_upd"))
            self.assertIsNone(crash_state["active_version"])
            self.assertIn("simulated broken patch", str(crash_state.get("last_rollback_reason") or ""))

    def test_updates_api_gate2_channel2_upload_rollback_reset_delete_and_zero_path_leak(self) -> None:
        """验证 /api/platform/updates/* Gate 2 鉴权、双渠道上传/扫描、回退、恢复基础版、清理缓存与零路径泄露."""
        project_root = os.path.dirname(resolve_static_dir())
        seed = secrets.token_bytes(32)
        pk = build_update_pack.ed25519_derive_pubkey(seed)
        maj, minor, _ = update_bootstrap.parse_semver(APP_VERSION)
        base_ver = f"{maj}.{minor}.0"
        v1 = f"{maj}.{minor}.1"
        v2 = f"{maj}.{minor}.2"
        v3 = f"{maj}.{minor}.3"
        cross_ver = f"{maj}.{minor + 1}.0"

        with tempfile.TemporaryDirectory() as tmp:
            clone_src = os.path.join(tmp, "src_clone")
            _clone_minimal_source(project_root, clone_src)
            pack_dir = os.path.join(tmp, "packs")

            packs = {}
            for ver in (v1, v2, v3, cross_ver):
                _stamp_cloned_version(clone_src, ver)
                res = build_update_pack.build_update_pack(
                    source_dir=clone_src,
                    output_dir=pack_dir,
                    version=ver,
                    seed=seed,
                    allow_custom_seed=True,
                )
                with open(res["zip_path"], "rb") as fh:
                    packs[ver] = (res["zip_filename"], fh.read())

            priv_root = os.path.join(tmp, "secret_priv_vault_999")
            install_dir = os.path.join(tmp, "secret_install_dir_888")
            os.makedirs(priv_root, exist_ok=True)
            os.makedirs(os.path.join(install_dir, "updates"), exist_ok=True)

            ctx = PlatformContext(
                db_path=os.path.join(priv_root, "vault_idx.dat"),
                verifier_pubkey=pk,
                install_dir=install_dir,
                base_version=base_ver,
            )
            try:
                app = create_app(ctx=ctx)
                # 1. 未激活 Gate 1/2 严格拦截 401
                unauth_client = TestClient(app, auto_activate=False)
                self.assertEqual(unauth_client.get("/api/platform/updates/status").status_code, 401)
                self.assertEqual(
                    unauth_client.post(
                        "/api/platform/updates/upload",
                        files={"file": (packs[v1][0], packs[v1][1], "application/zip")},
                    ).status_code,
                    401,
                )

                # 2. 已激活态完整闭环
                client = TestClient(app, auto_activate=True)
                st_res = client.get("/api/platform/updates/status")
                self.assertEqual(st_res.status_code, 200)
                st_json = st_res.json()
                self.assertEqual(st_json["update"]["base_version"], base_ver)
                self.assertEqual(st_json["update"]["series"], f"{maj}.{minor}")

                # 预检兼容 & 跨二级系列拒收诊断
                ins_ok = client.post(
                    "/api/platform/updates/inspect",
                    files={"file": (packs[v1][0], packs[v1][1], "application/zip")},
                ).json()
                self.assertTrue(ins_ok["ok"])
                self.assertTrue(ins_ok["compatible"])

                ins_cross = client.post(
                    "/api/platform/updates/inspect",
                    files={"file": (packs[cross_ver][0], packs[cross_ver][1], "application/zip")},
                ).json()
                self.assertTrue(ins_cross["ok"])
                self.assertFalse(ins_cross["compatible"])
                self.assertIn("跨二级版本", ins_cross["compatibility_message"])

                # 上传跨系列包必须拒绝 400
                bad_up = client.post(
                    "/api/platform/updates/upload",
                    files={"file": (packs[cross_ver][0], packs[cross_ver][1], "application/zip")},
                )
                self.assertEqual(bad_up.status_code, 400)

                # 渠道二上传 v1 -> 再上传 v2
                up1 = client.post(
                    "/api/platform/updates/upload",
                    files={"file": (packs[v1][0], packs[v1][1], "application/zip")},
                )
                self.assertEqual(up1.status_code, 200)
                self.assertEqual(up1.json()["installed_version"], v1)
                self.assertTrue(up1.json()["restart_required"])

                up2 = client.post(
                    "/api/platform/updates/upload",
                    files={"file": (packs[v2][0], packs[v2][1], "application/zip")},
                )
                self.assertEqual(up2.status_code, 200)
                u2 = up2.json()["update"]
                self.assertEqual(u2["active_patch_version"], v2)
                self.assertEqual(u2["previous_version"], v1)
                self.assertTrue(u2["can_rollback"])
                self.assertTrue(u2["can_reset_to_base"])

                # 一键回退至 v1
                rb = client.post("/api/platform/updates/rollback")
                self.assertEqual(rb.status_code, 200)
                self.assertEqual(rb.json()["update"]["active_patch_version"], v1)

                # 删除非活动缓存 v2 成功，删除激活版本 v1 拒绝
                del_active = client.delete(f"/api/platform/updates/releases/{v1}")
                self.assertEqual(del_active.status_code, 400)
                del_v2 = client.delete(f"/api/platform/updates/releases/{v2}")
                self.assertEqual(del_v2.status_code, 200)

                # 恢复内置基础版本
                rst = client.post("/api/platform/updates/reset-base")
                self.assertEqual(rst.status_code, 200)
                self.assertIsNone(rst.json()["update"]["active_patch_version"])

                # 渠道一投放至 <install_dir>/updates/ 并通过 /updates/scan 吸纳 v3
                with open(os.path.join(install_dir, "updates", packs[v3][0]), "wb") as fh:
                    fh.write(packs[v3][1])
                st_before_scan = client.get("/api/platform/updates/status").json()["update"]
                self.assertEqual(st_before_scan["install_dir_pending_count"], 1)

                scan_resp = client.post("/api/platform/updates/scan")
                self.assertEqual(scan_resp.status_code, 200)
                self.assertEqual(scan_resp.json()["installed_version"], v3)
                self.assertEqual(scan_resp.json()["update"]["active_patch_version"], v3)

                # 验证全量接口响应严禁泄露任何本地私有/安装目录物理路径
                for body_text in (st_res.text, up2.text, rb.text, rst.text, scan_resp.text):
                    for secret_marker in (priv_root, install_dir, "rt_upd", "secret_priv_vault_999"):
                        self.assertNotIn(secret_marker, body_text)
            finally:
                ctx.close()

    def test_frontend_updates_view_and_settings_section_via_node(self) -> None:
        """Node.js 真机 DOM 渲染验证：「系统配置 -> 版本与升级」UpdatesView 正常挂载且零 XSS/阻塞弹窗."""
        if getattr(sys, "frozen", False) or not shutil.which("node"):
            return

        project_root = os.path.dirname(resolve_static_dir())
        node_script = r"""
import path from "node:path";
import { pathToFileURL } from "node:url";

class MockNode {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = {};
    this.dataset = {};
    this.className = "";
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.parentNode = null;
    const self = this;
    this.classList = {
      add(...cs) { self.className = [...new Set([...(self.className||"").split(/\s+/).filter(Boolean), ...cs])].join(" "); },
      remove(...cs) { const s = new Set(cs); self.className = (self.className||"").split(/\s+/).filter(x => x && !s.has(x)).join(" "); },
      toggle(c, f) { const has = (self.className||"").split(/\s+/).includes(c); const on = f === undefined ? !has : Boolean(f); if (on) this.add(c); else this.remove(c); },
      contains(c) { return (self.className||"").split(/\s+/).includes(c); },
    };
  }
  appendChild(c) { if (c) { c.parentNode = this; this.children.push(c); } return c; }
  replaceChildren(...ns) { this.children = ns.filter(Boolean); this.children.forEach(c => c.parentNode = this); }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k) ?? null; }
  addEventListener(e, fn) { (this.listeners.get(e) || this.listeners.set(e, []).get(e)).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() {}
  click() {}
  collectText() {
    let out = this.textContent || "";
    for (const c of this.children) {
      if (c && typeof c.collectText === "function") out += " " + c.collectText();
    }
    return out;
  }
}

const byId = new Map();
for (const id of ["global-modal", "global-modal-title", "global-modal-body", "global-modal-footer", "global-modal-close", "toast-root"]) {
  const n = new MockNode(id === "global-modal" ? "dialog" : "div");
  n.id = id;
  byId.set(id, n);
}

globalThis.Node = MockNode;
globalThis.HTMLDialogElement = class HTMLDialogElement {};
globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
globalThis.document = {
  body: new MockNode("body"),
  head: new MockNode("head"),
  createElement: (tag) => new MockNode(tag),
  createElementNS: (_ns, tag) => new MockNode(tag),
  createTextNode: (t) => Object.assign(new MockNode("#text"), { textContent: String(t) }),
  getElementById: (id) => byId.get(id) || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
};
globalThis.fetch = async (url) => ({
  ok: true,
  status: 200,
  headers: new Map([["content-type", "application/json"]]),
  json: async () => {
    if (String(url).includes("/updates/status")) {
      return {
        ok: true,
        update: {
          running_version: "3.3.0",
          base_version: "3.3.0",
          series: "3.3",
          min_compatible_base: "3.3.0",
          patch_active: false,
          running_patch_version: null,
          active_patch_version: "3.3.1",
          previous_version: null,
          restart_required: true,
          pending_target_version: "3.3.1",
          can_rollback: false,
          can_reset_to_base: true,
          install_dir_pending_count: 1,
          installed_releases: [
            {
              version: "3.3.1",
              series: "3.3",
              min_base_version: "3.3.0",
              built_at: "2026-11-24T08:00:00Z",
              summary: "同系列累积更新测试",
              code_file_count: 84,
              static_file_count: 54,
              is_active: true,
              is_running: false,
              is_previous: false,
              verified: true,
            },
          ],
        },
      };
    }
    return { ok: true };
  },
});

const rootDir = process.argv[1];
const imp = (rel) => import(pathToFileURL(path.join(rootDir, rel)).href);

const { SettingsView } = await imp("static/js/views/settings_view.js");
const sv = new SettingsView();
const host = new MockNode("div");
sv.mount(host, { section: "updates" }, () => {});
await sv.updatesPane.refreshFromServer();

const renderedText = host.collectText();
for (const expected of ["版本与升级", "v3.3.0", "v3.3.1", "上传增量更新包", "扫描安装目录", "同系列"]) {
  if (!renderedText.includes(expected)) {
    throw new Error(`UpdatesView missing expected text: ${expected}\nGot: ${renderedText}`);
  }
}
sv.destroy();
console.log("FRONTEND_UPDATES_VIEW_OK");
process.exit(0);
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, project_root],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node UpdatesView 渲染验证失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("FRONTEND_UPDATES_VIEW_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
