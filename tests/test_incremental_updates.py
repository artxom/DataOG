"""Data.O.G Ed25519 签名同系列累积增量升级体系与双渠道端到端自动化测试."""

from __future__ import annotations

import hashlib
import io
import json
import math
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
import zlib

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


def _rewrite_dgp2_inside_zip(outer_zip_bytes: bytes, seed: bytes, mutator) -> bytes:
    """在测试内解封并重建外层 ZIP / 内层 DGP2 密文信封以构造精确内部篡改攻击样本."""
    pk = build_update_pack.ed25519_derive_pubkey(seed)
    with zipfile.ZipFile(io.BytesIO(outer_zip_bytes), "r") as ozf:
        outer_items = {zi.filename: ozf.read(zi.filename) for zi in ozf.infolist() if not zi.is_dir()}

    dgp_key = next(k for k in outer_items if k.endswith(".dgp"))
    entries = update_bootstrap.unseal_dgp2_bytes(outer_items[dgp_key], pubkey=pk)

    mutator(entries)

    raw_bundle = update_bootstrap.pack_dgp2_bundle(list(entries.items()))
    outer_items[dgp_key] = build_update_pack.seal_dgp2_bundle(raw_bundle, seed=seed)

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w") as nozf:
        for name, data in outer_items.items():
            nozf.writestr(name, data)
    return out_buf.getvalue()


def _build_legacy_v1_outer_zip(source_dir: str, version: str, seed: bytes) -> bytes:
    """构造历史 v1 明文嵌套 ZIP 增量包（用于测试防降级保护与旧版本兼容诊断）."""
    ver_norm = update_bootstrap.normalize_semver(version)
    series = update_bootstrap.format_series(ver_norm)
    min_base = update_bootstrap.compute_min_base_version(ver_norm)
    code_entries = build_update_pack._collect_code_entries(source_dir)
    static_entries = build_update_pack._collect_static_entries(source_dir)

    code_buf = io.BytesIO()
    with zipfile.ZipFile(code_buf, "w") as czf:
        for arcname, raw in code_entries:
            build_update_pack._zip_writestr(czf, arcname, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    code_zip_bytes = code_buf.getvalue()

    manifest = {
        "format_version": update_bootstrap.UPDATE_FORMAT_VERSION_V1,
        "version": ver_norm,
        "series": series,
        "min_base_version": min_base,
        "built_at": "2026-11-24T00:00:00Z",
        "summary": f"Legacy v1 pack {ver_norm}",
        "notes": "",
        "code_zip": {
            "sha256": hashlib.sha256(code_zip_bytes).hexdigest(),
            "size": len(code_zip_bytes),
            "file_count": len(code_entries),
        },
        "static_files": {rel: hashlib.sha256(raw).hexdigest() for rel, raw in static_entries},
    }
    manifest_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig_raw = build_update_pack.ed25519_sign_deterministic(
        update_bootstrap.UPDATE_SIG_DOMAIN_V1 + manifest_raw,
        seed,
    )

    dgp_buf = io.BytesIO()
    with zipfile.ZipFile(dgp_buf, "w") as dzf:
        build_update_pack._zip_writestr(dzf, "manifest.json", manifest_raw)
        build_update_pack._zip_writestr(dzf, "manifest.sig", sig_raw)
        build_update_pack._zip_writestr(dzf, "code.zip", code_zip_bytes, compress_type=zipfile.ZIP_STORED, compresslevel=None)
        for rel, raw in static_entries:
            build_update_pack._zip_writestr(dzf, f"static/{rel}", raw)
    dgp_bytes = dgp_buf.getvalue()

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w") as ozf:
        build_update_pack._zip_writestr(
            ozf,
            f"updates/DataOG-Update-v{ver_norm}.dgp",
            dgp_bytes,
            compress_type=zipfile.ZIP_STORED,
            compresslevel=None,
        )
        build_update_pack._zip_writestr(ozf, "UPDATE_README.txt", b"legacy v1")
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
        """验证 build_update_pack DGP2 加密构建、双层签名域隔离、防篡改、防未签名多余文件与防路径穿越."""
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
            self.assertEqual(res["format_version"], 2)
            self.assertTrue(res["encrypted"])
            self.assertLess(res["zip_size"], 2_500_000, "增量包体积应控制在轻量范围 (<2.5MB)")
            with open(res["zip_path"], "rb") as fh:
                outer_bytes = fh.read()

            # 校验不包含开发/测试/秘密路径，外层仅含 DGP2 .dgp + UPDATE_README.txt，且 .dgp 不再是 ZIP
            with zipfile.ZipFile(io.BytesIO(outer_bytes), "r") as ozf:
                outer_names = set(ozf.namelist())
                self.assertEqual(
                    outer_names,
                    {f"updates/DataOG-Update-v{APP_VERSION}.dgp", "UPDATE_README.txt"},
                )
                dgp_bytes = ozf.read(f"updates/DataOG-Update-v{APP_VERSION}.dgp")
            self.assertFalse(zipfile.is_zipfile(io.BytesIO(dgp_bytes)), "DGP2 载荷严禁保持 ZIP 格式")

            entries = update_bootstrap.unseal_dgp2_bytes(dgp_bytes, pubkey=pk)
            with zipfile.ZipFile(io.BytesIO(entries["code.zip"]), "r") as czf:
                for entry in czf.namelist():
                    self.assertTrue(entry.endswith(".py"), entry)
                    for forbidden in ("tests/", "tools/", "demo_sandbox/", ".keygen_secrets/", "__pycache__/"):
                        self.assertNotIn(forbidden, entry)

            # 同时支持外层 .zip 与直接 .dgp 验签
            verified_outer = update_bootstrap.verify_update_package_bytes(outer_bytes, verifier_pubkey=pk)
            verified_dgp = update_bootstrap.verify_update_package_bytes(dgp_bytes, verifier_pubkey=pk)
            self.assertEqual(verified_outer["manifest"]["version"], APP_VERSION)
            self.assertEqual(verified_outer["format_version"], 2)
            self.assertTrue(verified_outer["encrypted"])
            self.assertEqual(verified_dgp["container_type"], "dgp")
            self.assertEqual(verified_dgp["format_version"], 2)

            # 错公钥拒绝
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_pk:
                update_bootstrap.verify_update_package_bytes(outer_bytes, verifier_pubkey=wrong_pk)
            self.assertEqual(cm_pk.exception.code, "BAD_SIGNATURE")

            # 签名域隔离验证：用 License / V1 / 外壳V2 签名域签出的清单签名绝不能通过 DGP2 清单验签
            for wrong_domain in (
                b"DataOG-License-v1:",
                update_bootstrap.UPDATE_SIG_DOMAIN_V1,
                update_bootstrap.UPDATE_SIG_DOMAIN_V2,
            ):
                def _swap_domain_sig(items: dict, dom: bytes = wrong_domain) -> None:
                    m_raw = items["manifest.json"]
                    items["manifest.sig"] = build_update_pack.ed25519_sign_deterministic(dom + m_raw, seed)

                wrong_domain_zip = _rewrite_dgp2_inside_zip(outer_bytes, seed, _swap_domain_sig)
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
                tampered = _rewrite_dgp2_inside_zip(
                    outer_bytes,
                    seed,
                    lambda items, k=key: items.__setitem__(k, _flip_byte(items[k])),
                )
                with self.assertRaises(update_bootstrap.UpdateVerificationError, msg=key) as cm:
                    update_bootstrap.verify_update_package_bytes(tampered, verifier_pubkey=pk)
                self.assertEqual(cm.exception.code, expected_code)

            # 添加未签名多余 static 文件必须拒绝
            extra_static_zip = _rewrite_dgp2_inside_zip(
                outer_bytes,
                seed,
                lambda items: items.__setitem__("static/evil.js", b"console.log('x');"),
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
                m["code_zip"]["sha256"] = hashlib.sha256(new_code).hexdigest()
                m["code_zip"]["size"] = len(new_code)
                m_raw = json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                items["manifest.json"] = m_raw
                items["manifest.sig"] = build_update_pack.ed25519_sign_deterministic(
                    update_bootstrap.UPDATE_SIG_DOMAIN_V2_MANIFEST + m_raw, seed
                )

            forbidden_zip = _rewrite_dgp2_inside_zip(outer_bytes, seed, _inject_forbidden_code)
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

    def test_dgp2_mail_gateway_opacity_encryption_and_downgrade_guard(self) -> None:
        """验证邮件网关零明文代码/脚本防线、高熵加密、随机 Nonce、.dat 兼容及 >=3.5.0 防降级拒收."""
        project_root = os.path.dirname(resolve_static_dir())
        seed = secrets.token_bytes(32)
        pk = build_update_pack.ed25519_derive_pubkey(seed)

        with tempfile.TemporaryDirectory() as tmp:
            out1 = os.path.join(tmp, "p1")
            out2 = os.path.join(tmp, "p2")
            res1 = build_update_pack.build_update_pack(output_dir=out1, seed=seed, allow_custom_seed=True)
            res2 = build_update_pack.build_update_pack(output_dir=out2, seed=seed, allow_custom_seed=True)

            with open(res1["zip_path"], "rb") as fh:
                outer1 = fh.read()
            with open(res2["zip_path"], "rb") as fh:
                outer2 = fh.read()

            # 1. 外层 ZIP 中央目录绝不含可执行或脚本扩展名
            forbidden_exts = (
                ".py", ".pyc", ".pyo", ".js", ".mjs", ".html", ".htm", ".css",
                ".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".vbs", ".jar", ".zip",
            )
            with zipfile.ZipFile(io.BytesIO(outer1), "r") as ozf:
                names = ozf.namelist()
                self.assertEqual(set(names), {f"updates/DataOG-Update-v{APP_VERSION}.dgp", "UPDATE_README.txt"})
                for name in names:
                    self.assertFalse(name.lower().endswith(forbidden_exts), f"发现敏感扩展名: {name}")
                dgp1 = ozf.read(f"updates/DataOG-Update-v{APP_VERSION}.dgp")
            with zipfile.ZipFile(io.BytesIO(outer2), "r") as ozf2:
                dgp2 = ozf2.read(f"updates/DataOG-Update-v{APP_VERSION}.dgp")

            # 2. DGP2 非 ZIP / 非 PE / 非 ELF / 非裸 zlib / 高香农熵 / 零明文源码特征
            self.assertEqual(dgp1[:4], update_bootstrap.DGP2_MAGIC)
            self.assertFalse(zipfile.is_zipfile(io.BytesIO(dgp1)))
            for magic_prefix in (b"PK", b"MZ", b"\x7fELF", b"\x1f\x8b", b"\x78\x9c", b"\x78\xda", b"\x78\x01"):
                self.assertFalse(dgp1.startswith(magic_prefix))

            body = dgp1[12:]
            counts = [0] * 256
            for b in body:
                counts[b] += 1
            total = len(body)
            entropy = -sum((c / total) * math.log2(c / total) for c in counts if c > 0)
            self.assertGreater(entropy, 7.9, f"DGP2 密文信息熵过低: {entropy:.4f}")

            for marker in (
                b"manifest.json",
                b"code.zip",
                b"core/app_kernel.py",
                b"static/index.html",
                b"js/main.js",
                b"bootstrapWorkbench",
                b"<!DOCTYPE",
                b"import ",
                b"function ",
                b"DataOG",
            ):
                self.assertNotIn(marker, dgp1, f"DGP2 密文中泄露明文标记: {marker!r}")

            # 3. 连续构建两次生成不同 Nonce 与完全不同的密文
            self.assertNotEqual(dgp1[12:36], dgp2[12:36])
            self.assertNotEqual(dgp1, dgp2)

            # 4. 改名为 .dat 的直接文件及外层 ZIP 内 .dat 均可正常验签并通过 scan 吸纳
            v_dat_raw = update_bootstrap.verify_update_package_bytes(dgp1, verifier_pubkey=pk)
            self.assertEqual(v_dat_raw["format_version"], 2)

            dat_outer_buf = io.BytesIO()
            with zipfile.ZipFile(dat_outer_buf, "w") as dz:
                dz.writestr(f"updates/DataOG-Update-v{APP_VERSION}.dat", dgp1)
                dz.writestr("UPDATE_README.txt", b"renamed dat")
            v_dat_outer = update_bootstrap.verify_update_package_bytes(dat_outer_buf.getvalue(), verifier_pubkey=pk)
            self.assertEqual(v_dat_outer["container_type"], "outer_zip")
            self.assertEqual(v_dat_outer["format_version"], 2)

            install_dir = os.path.join(tmp, "inst")
            upd_dir = os.path.join(tmp, "vault_upd")
            os.makedirs(os.path.join(install_dir, "updates"), exist_ok=True)
            with open(os.path.join(install_dir, "updates", "gateway_safe.dat"), "wb") as fh:
                fh.write(dgp1)
            scan_res = update_bootstrap.scan_and_ingest_install_dir_updates(
                updates_dir=upd_dir,
                install_dir=install_dir,
                base_version=APP_VERSION,
                verifier_pubkey=pk,
            )
            self.assertEqual(scan_res["installed_version"], APP_VERSION)

            # 5. 防降级保护：v1 明文包声称 >=3.5.0（即使拥有合法签名）也必须被拒绝；声称 3.4.2 跨版本则拦截
            clone_src = os.path.join(tmp, "src_clone")
            _clone_minimal_source(project_root, clone_src)

            _stamp_cloned_version(clone_src, "3.5.1")
            v1_forged_351 = _build_legacy_v1_outer_zip(clone_src, "3.5.1", seed)
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_dg:
                update_bootstrap.verify_update_package_bytes(v1_forged_351, verifier_pubkey=pk)
            self.assertEqual(cm_dg.exception.code, "INVALID_ARCHIVE")

            _stamp_cloned_version(clone_src, "3.4.2")
            v1_legacy_342 = _build_legacy_v1_outer_zip(clone_src, "3.4.2", seed)
            ctx = PlatformContext(
                db_path=os.path.join(tmp, "hub_vault", "idx.dat"),
                verifier_pubkey=pk,
                install_dir=install_dir,
                base_version="3.5.0",
            )
            try:
                ins = ctx.update_hub.inspect_update_bytes(v1_legacy_342, "DataOG-Update-v3.4.2.zip")
                self.assertTrue(ins["ok"])
                self.assertEqual(ins["format_version"], 1)
                self.assertFalse(ins["encrypted"])
                self.assertFalse(ins["compatible"])
                self.assertIn("跨二级版本", ins["compatibility_message"])

                with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_incompat:
                    ctx.update_hub.install_update_bytes(v1_legacy_342, "DataOG-Update-v3.4.2.zip")
                self.assertEqual(cm_incompat.exception.code, "INCOMPATIBLE_VERSION")
            finally:
                ctx.close()

    def test_dgp2_envelope_tamper_and_zlib_bomb_defenses(self) -> None:
        """验证 DGP2 140 字节信封逐字段篡改、截断、尾字节、错误签名域及 Signed Zlib-Bomb 防御."""
        self.assertEqual(len(update_bootstrap.reconstruct_envelope_pepper()), 32)
        seed = secrets.token_bytes(32)
        pk = build_update_pack.ed25519_derive_pubkey(seed)

        with tempfile.TemporaryDirectory() as tmp:
            res = build_update_pack.build_update_pack(output_dir=tmp, seed=seed, allow_custom_seed=True)
            with zipfile.ZipFile(res["zip_path"], "r") as ozf:
                dgp_bytes = ozf.read(f"updates/DataOG-Update-v{APP_VERSION}.dgp")

        # 1. 逐字段 1-bit 翻转检查
        field_offsets = (
            ("magic", 0, "INVALID_ARCHIVE"),
            ("format_version", 5, "INVALID_ARCHIVE"),
            ("flags", 7, "INVALID_ARCHIVE"),
            ("cipher_id", 8, "INVALID_ARCHIVE"),
            ("reserved", 9, "INVALID_ARCHIVE"),
            ("nonce", 12, "BAD_SIGNATURE"),
            ("plaintext_len_msb", 36, "BAD_SIGNATURE"),
            ("plaintext_len_lsb", 39, "BAD_SIGNATURE"),
            ("ciphertext_len", 43, "INVALID_ARCHIVE"),
            ("envelope_mac", 44, "BAD_SIGNATURE"),
            ("envelope_sig", 76, "BAD_SIGNATURE"),
            ("ciphertext_head", update_bootstrap.DGP2_HEADER_SIZE, "BAD_SIGNATURE"),
            ("ciphertext_tail", len(dgp_bytes) - 1, "BAD_SIGNATURE"),
        )
        for label, offset, expected_code in field_offsets:
            buf = bytearray(dgp_bytes)
            buf[offset] ^= 0x01
            with self.assertRaises(update_bootstrap.UpdateVerificationError, msg=label) as cm:
                update_bootstrap.unseal_dgp2_bytes(bytes(buf), pubkey=pk)
            self.assertEqual(cm.exception.code, expected_code, label)

        # 截断与尾部多余字节
        for truncated in (dgp_bytes[:50], dgp_bytes[:-1]):
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_tr:
                update_bootstrap.unseal_dgp2_bytes(truncated, pubkey=pk)
            self.assertEqual(cm_tr.exception.code, "INVALID_ARCHIVE")

        with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_ext:
            update_bootstrap.unseal_dgp2_bytes(dgp_bytes + b"\x00", pubkey=pk)
        self.assertEqual(cm_ext.exception.code, "INVALID_ARCHIVE")

        # 2. 外壳签名使用了非 DataOG-Update-v2: 签名域（即使签名有效）必须拒绝
        header_core = dgp_bytes[: update_bootstrap.DGP2_HEADER_CORE_SIZE]
        mac_bytes = dgp_bytes[update_bootstrap.DGP2_HEADER_CORE_SIZE : update_bootstrap.DGP2_HEADER_CORE_SIZE + 32]
        ciphertext = dgp_bytes[update_bootstrap.DGP2_HEADER_SIZE :]
        for wrong_dom in (
            update_bootstrap.UPDATE_SIG_DOMAIN_V1,
            update_bootstrap.UPDATE_SIG_DOMAIN_V2_MANIFEST,
            b"DataOG-License-v1:",
        ):
            bad_sig = build_update_pack.ed25519_sign_deterministic(
                wrong_dom + header_core + hashlib.sha256(ciphertext).digest(),
                seed,
            )
            forged = header_core + mac_bytes + bad_sig + ciphertext
            with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_ws:
                update_bootstrap.unseal_dgp2_bytes(forged, pubkey=pk)
            self.assertEqual(cm_ws.exception.code, "BAD_SIGNATURE")

        # 3. 合法签名 + 合法 HMAC 的超大声明长度与真实 Zlib-Bomb 防线
        def _craft_signed_envelope(compressed_payload: bytes, declared_plain_len: int) -> bytes:
            n = secrets.token_bytes(24)
            ek, mk = update_bootstrap.derive_dgp2_keys(pk, n)
            ct = update_bootstrap.dgp2_ctr_xor(compressed_payload, ek, n)
            hc = update_bootstrap.DGP2_HEADER_CORE_STRUCT.pack(
                update_bootstrap.DGP2_MAGIC,
                update_bootstrap.UPDATE_FORMAT_VERSION,
                update_bootstrap.DGP2_FLAG_ZLIB | update_bootstrap.DGP2_FLAG_ENCRYPTED,
                update_bootstrap.DGP2_CIPHER_BLAKE2B_CTR_HMAC,
                b"\x00\x00\x00",
                n,
                declared_plain_len,
                len(ct),
            )
            mac = update_bootstrap.dgp2_compute_mac(mk, hc, ct)
            sig = build_update_pack.ed25519_sign_deterministic(
                update_bootstrap.dgp2_envelope_sign_message(hc, ct),
                seed,
            )
            return hc + mac + sig + ct

        signed_huge_decl = _craft_signed_envelope(
            zlib.compress(b"small", level=9),
            update_bootstrap.MAX_UNCOMPRESSED_BYTES + 1,
        )
        with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_decl:
            update_bootstrap.unseal_dgp2_bytes(signed_huge_decl, pubkey=pk)
        self.assertEqual(cm_decl.exception.code, "SIZE_LIMIT")

        bomb_plain = b"\x00" * (update_bootstrap.MAX_UNCOMPRESSED_BYTES + 4096)
        signed_bomb = _craft_signed_envelope(zlib.compress(bomb_plain, level=9), 1024)
        with self.assertRaises(update_bootstrap.UpdateVerificationError) as cm_bomb:
            update_bootstrap.unseal_dgp2_bytes(signed_bomb, pubkey=pk)
        self.assertEqual(cm_bomb.exception.code, "SIZE_LIMIT")

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
          running_version: "3.5.0",
          base_version: "3.5.0",
          series: "3.5",
          min_compatible_base: "3.5.0",
          patch_active: false,
          running_patch_version: null,
          active_patch_version: "3.5.1",
          previous_version: null,
          restart_required: true,
          pending_target_version: "3.5.1",
          can_rollback: false,
          can_reset_to_base: true,
          install_dir_pending_count: 1,
          installed_releases: [
            {
              version: "3.5.1",
              series: "3.5",
              min_base_version: "3.5.0",
              format_version: 2,
              encrypted: true,
              built_at: "2026-11-25T08:00:00Z",
              summary: "DGP2 加密混淆同系列累积更新测试",
              code_file_count: 86,
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
for (const expected of ["版本与升级", "v3.5.0", "v3.5.1", "加密混淆包", "上传增量更新包", "扫描安装目录", "同系列"]) {
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
