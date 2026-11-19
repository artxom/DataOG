"""Data.O.G 离线非对称激活码登录、C 原生护盾防反编译与独立生成器闭环测试."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app import create_app
from core.platform.context import get_platform_context
from core.platform.license_hub import (
    CONTACT_INFO,
    SCOPE_GLOBAL,
    SCOPE_MACHINE,
    LicenseHub,
    NativeLicenseShield,
    date_to_epoch_day,
    decode_request_code,
    ed25519_derive_pubkey,
    ed25519_sign_deterministic,
    ed25519_verify,
    encode_request_code,
    epoch_day_to_date,
    get_machine_code_py,
    issue_activation_code_for_seed,
)
from core.platform.storage import PlatformStateStore
from tests.test_modules_and_api import TestClient

# 由生产私钥对固定过去日期 (2025-12-31) 签发的样本向量（纯公钥验签，绝不含任何私钥）
_PROD_SIGNED_EXPIRED_VECTOR = (
    "DOG1-AEAQAHQB-NQILUBFR-SI2IYBVP-MJ3E2R2W-J6WIFMFU-4MBL4J5F-QB7V63EV-SBUYHRSV-"
    "AXW2KAIP-RUP3SONV-JK6UUZIM-F5366EFA-H3UPG5WR-H33VBIJM-NPEKBDA3-ABSE3XQO"
)


class TestOfflineLicenseAuth(unittest.TestCase):
    def test_rfc8032_vectors_and_native_c_shield_parity(self) -> None:
        """验证 RFC 8032 标准向量、C 原生护盾 64 位挑战-应答 MAC 及公历闰年边界一致性."""
        # RFC 8032 Section 7.1 Test Vector 1 (empty message)
        seed1 = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        )
        pk1 = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        )
        sig1 = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        )
        self.assertEqual(ed25519_derive_pubkey(seed1), pk1)
        self.assertEqual(ed25519_sign_deterministic(b"", seed1), sig1)
        self.assertTrue(ed25519_verify(b"", sig1, pk1))
        self.assertFalse(ed25519_verify(b"tampered", sig1, pk1))

        # 公历闰年对拍 (2025-01-01 ~ 2028-02-29 / 2028-03-01)
        for dt in (
            datetime.date(2025, 1, 1),
            datetime.date(2026, 11, 19),
            datetime.date(2028, 2, 28),
            datetime.date(2028, 2, 29),
            datetime.date(2028, 3, 1),
            datetime.date(2030, 12, 31),
        ):
            self.assertEqual(epoch_day_to_date(date_to_epoch_day(dt)), dt)

        # C 原生护盾一致性与机器码校验
        import core.platform.license_hub as _lh_mod

        shield = NativeLicenseShield()
        if not getattr(sys, "frozen", False):
            self.assertTrue(
                shield.is_loaded,
                f"开发环境必须成功编译并加载 C 原生防反编译护盾: {_lh_mod.LAST_SHIELD_COMPILE_ERROR}",
            )
        elif any(os.path.isfile(p) for p in _lh_mod._candidate_shield_paths()):
            self.assertTrue(shield.is_loaded, "冻结态安装包内已包含 _license_shield 但加载失败")

        if shield.is_loaded:
            probed = shield.probe_device()
            self.assertIsNotNone(probed)
            self.assertEqual(probed[0], get_machine_code_py())

            code = issue_activation_code_for_seed(
                seed1,
                scope=SCOPE_MACHINE,
                duration_days=30,
                expiry_date=datetime.date(2028, 2, 29),
                machine_code=probed[0],
                ip_str=probed[1],
            )
            st = shield.verify_with_challenge(
                code_str=code,
                machine_code=probed[0],
                ips=probed[2],
                today_day=date_to_epoch_day(datetime.date(2028, 2, 20)),
                effective_pubkey=pk1,
                using_custom_pubkey=True,
            )
            self.assertEqual(st[0], 0)
            self.assertEqual(st[1], SCOPE_MACHINE)
            self.assertEqual(st[2], 30)
            self.assertEqual(epoch_day_to_date(st[3]), datetime.date(2028, 2, 29))

            # 验证若 C 返回篡改 MAC（模拟二进制返回值补丁攻击）必须立即拦截
            with mock.patch("hashlib.sha512", return_value=mock.Mock(digest=lambda: b"\xff" * 64)):
                patched = shield.verify_with_challenge(
                    code_str=code,
                    machine_code=probed[0],
                    ips=probed[2],
                    today_day=date_to_epoch_day(datetime.date(2028, 2, 20)),
                    effective_pubkey=pk1,
                    using_custom_pubkey=True,
                )
            self.assertEqual(patched[0], -99)

    def test_production_public_key_pre_signed_vector(self) -> None:
        """验证默认内置混淆生产公钥真实核验预签发向量，且任意单字符篡改均被签名拦截."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(db_path=os.path.join(tmp, "state.db"))
            hub = app.state.platform_ctx.license_hub
            # 绑定匹配机器码与 IP，但日期处于已过期状态 -> 必须通过 Ed25519 签名校验并精确识别为 EXPIRED (而非 INVALID)
            st, scope, dur, exp_day, msg = hub._verify_token_raw(
                _PROD_SIGNED_EXPIRED_VECTOR,
                {"machine_code": "MCH-TEST-0001-0002-0003", "all_ips": ["10.20.30.40"]},
                datetime.date(2026, 6, 1),
            )
            self.assertEqual(st, "EXPIRED")
            self.assertEqual(scope, SCOPE_MACHINE)
            self.assertEqual(dur, 30)
            self.assertEqual(epoch_day_to_date(exp_day), datetime.date(2025, 12, 31))
            self.assertIn("2025-12-31", msg)

            # 篡改最后 1 字符 -> 必须判定为签名无效
            bad_vec = _PROD_SIGNED_EXPIRED_VECTOR[:-1] + (
                "A" if _PROD_SIGNED_EXPIRED_VECTOR[-1] != "A" else "B"
            )
            st_bad, _, _, _, _ = hub._verify_token_raw(
                bad_vec,
                {"machine_code": "MCH-TEST-0001-0002-0003", "all_ips": ["10.20.30.40"]},
                datetime.date(2025, 12, 20),
            )
            self.assertEqual(st_bad, "INVALID")

    def test_unactivated_dual_gate_blocks_all_platform_and_module_apis(self) -> None:
        """验证未激活时全量业务/模块 API 返回 401 LICENSE_REQUIRED，且 Gate 2 独立阻断绕过中间件的调用."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(db_path=os.path.join(tmp, "state.db"))
            client = TestClient(app, auto_activate=False)

            # 静态外壳与登录状态探测正常放行
            self.assertEqual(client.get("/").status_code, 200)
            st_resp = client.get("/api/platform/auth/status")
            self.assertEqual(st_resp.status_code, 200)
            st_json = st_resp.json()
            self.assertFalse(st_json["authorized"])
            self.assertEqual(st_json["login_mode_default"], "activation_code")
            self.assertEqual(st_json["contact"]["owner"], "莫政辉")
            self.assertEqual(st_json["contact"]["phone"], "131-6111-4561")
            self.assertEqual(st_json["contact"]["email"], "artxom@outlook.com")

            # Gate 1: 全量业务 API 全部拦截为 401
            blocked_endpoints = [
                ("GET", "/api/bootstrap"),
                ("GET", "/api/platform/bootstrap"),
                ("GET", "/api/platform/connections"),
                ("POST", "/api/platform/query/execute"),
                ("GET", "/api/modules/standard_migrator/state"),
                ("GET", "/api/modules/cross_comparator/overview"),
                ("GET", "/api/modules/ddl_translator/options"),
                ("GET", "/api/modules/data_explorer/sessions"),
            ]
            for method, path in blocked_endpoints:
                r = client.request(method, path)
                self.assertEqual(r.status_code, 401, f"{method} {path} 应在未激活时返回 401")
                self.assertEqual(r.json().get("code"), "LICENSE_REQUIRED")

            # Gate 2: 模拟直接绕过中间件伪造请求调用 get_platform_context 仍被拒绝
            fake_req = SimpleNamespace(
                app=app,
                url=SimpleNamespace(path="/api/platform/connections"),
                state=SimpleNamespace(),
            )
            with self.assertRaises(HTTPException) as ctx_err:
                get_platform_context(fake_req)  # type: ignore[arg-type]
            self.assertEqual(ctx_err.exception.status_code, 401)

    def test_deterministic_machine_bound_multi_nic_and_anti_copy(self) -> None:
        """验证确定性唯一激活码、多网卡匹配、重启持久化及防状态库拷贝/篡改."""
        seed = b"Test-Machine-Bound-Seed-32Bytes!"
        pub = ed25519_derive_pubkey(seed)

        today = datetime.date(2026, 11, 19)
        exp_date = today + datetime.timedelta(days=29)  # 30天
        now_ts = datetime.datetime(2026, 11, 19, 10, 0, 0).timestamp()

        mc_a = "MCH-AAAA-BBBB-CCCC-DDDD"
        ip_a = "192.168.10.25"

        # 同一设备 + IP + 时长 + 日期 多次签发严格唯一相同
        tok1 = issue_activation_code_for_seed(
            seed, scope=SCOPE_MACHINE, duration_days=30, expiry_date=exp_date, machine_code=mc_a, ip_str=ip_a
        )
        tok2 = issue_activation_code_for_seed(
            seed, scope=SCOPE_MACHINE, duration_days=30, expiry_date=exp_date, machine_code=mc_a, ip_str=ip_a
        )
        self.assertEqual(tok1, tok2)
        self.assertTrue(tok1.startswith("DOG1-"))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "state.db")
            app = create_app(db_path=db_path)
            ctx = app.state.platform_ctx
            ctx.license_hub = LicenseHub(
                ctx.store,
                verifier_pubkey=pub,
                clock_fn=lambda: now_ts,
                device_probe_fn=lambda: {
                    "machine_code": mc_a,
                    "primary_ip": ip_a,
                    "all_ips": [ip_a, "10.0.5.88"],
                    "hostname": "DEV-NODE-A",
                },
            )
            client = TestClient(app, auto_activate=False)

            # 错误激活码拦截
            bad_act = client.post("/api/platform/auth/activate", json={"activation_code": "DOG1-BAD-TOKEN"})
            self.assertEqual(bad_act.status_code, 400)

            # 正确激活并返回可用日期至 XXX
            ok_act = client.post("/api/platform/auth/activate", json={"activation_code": tok1})
            self.assertEqual(ok_act.status_code, 200)
            body = ok_act.json()
            self.assertIn(f"当前激活码可用日期至 {exp_date.isoformat()}", body["message"])
            self.assertTrue(body["license"]["authorized"])
            self.assertEqual(body["license"]["expires_date"], exp_date.isoformat())

            # 已激活状态下放行全部接口
            self.assertEqual(client.get("/api/platform/bootstrap").status_code, 200)
            self.assertEqual(client.get("/api/bootstrap").status_code, 200)

            # 验证多网卡环境：即使主 IP 切换到另一块物理网卡 10.0.5.88，只要本机网卡列表中仍包含绑定的 192.168.10.25 依然放行
            ctx2_store = PlatformStateStore(db_path)
            hub_multi = LicenseHub(
                ctx2_store,
                verifier_pubkey=pub,
                clock_fn=lambda: now_ts + 3600,
                device_probe_fn=lambda: {
                    "machine_code": mc_a,
                    "primary_ip": "10.0.5.88",
                    "all_ips": ["10.0.5.88", ip_a],
                    "hostname": "DEV-NODE-A",
                },
            )
            self.assertTrue(hub_multi.get_auth_status()["authorized"])

            # 模拟拷贝 dataog_state.db 到另一台机器（不同机器码） -> 立即拦截
            hub_other_mc = LicenseHub(
                ctx2_store,
                verifier_pubkey=pub,
                clock_fn=lambda: now_ts + 3600,
                device_probe_fn=lambda: {
                    "machine_code": "MCH-9999-8888-7777-6666",
                    "primary_ip": ip_a,
                    "all_ips": [ip_a],
                    "hostname": "OTHER-PC",
                },
            )
            st_mc = hub_other_mc.get_auth_status()
            self.assertFalse(st_mc["authorized"])
            self.assertEqual(st_mc["status"], "DEVICE_MISMATCH")

            # 模拟更换局域网 IP（所有网卡均无绑定 IP） -> 立即拦截
            hub_other_ip = LicenseHub(
                ctx2_store,
                verifier_pubkey=pub,
                clock_fn=lambda: now_ts + 3600,
                device_probe_fn=lambda: {
                    "machine_code": mc_a,
                    "primary_ip": "172.16.88.99",
                    "all_ips": ["172.16.88.99"],
                    "hostname": "DEV-NODE-A",
                },
            )
            st_ip = hub_other_ip.get_auth_status()
            self.assertFalse(st_ip["authorized"])
            self.assertEqual(st_ip["status"], "DEVICE_MISMATCH")

            # 模拟攻击者直接修改 SQLite 中存储的激活码或时钟密封值 -> 立即失效
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE app_meta SET value = ? WHERE key = 'license_activation_code'",
                    (tok1[:-1] + ("Z" if tok1[-1] != "Z" else "Y"),),
                )
                conn.commit()
            self.assertFalse(hub_multi.get_auth_status()["authorized"])

    def test_global_universal_code_clock_rollback_lock_and_account_mode(self) -> None:
        """验证全局通用码、到期拦截、防时钟回拨、手动锁定与账号密码禁用提示."""
        seed = b"Test-Global-Code-Seed-32-Bytes!!"
        pub = ed25519_derive_pubkey(seed)

        cur_ts = [datetime.datetime(2026, 11, 19, 12, 0, 0).timestamp()]
        cur_mc = ["MCH-NODE-0001-0001-0001"]
        cur_ip = ["192.168.1.50"]

        exp_date = datetime.date(2026, 12, 18)  # 30天
        global_tok = issue_activation_code_for_seed(
            seed, scope=SCOPE_GLOBAL, duration_days=30, expiry_date=exp_date
        )

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(db_path=os.path.join(tmp, "state.db"))
            ctx = app.state.platform_ctx
            ctx.license_hub = LicenseHub(
                ctx.store,
                verifier_pubkey=pub,
                clock_fn=lambda: cur_ts[0],
                device_probe_fn=lambda: {
                    "machine_code": cur_mc[0],
                    "primary_ip": cur_ip[0],
                    "all_ips": [cur_ip[0]],
                    "hostname": "ANY-HOST",
                },
            )
            client = TestClient(app, auto_activate=False)

            # 1. 账号密码模式在试用期必须拒绝
            acc_res = client.post(
                "/api/platform/auth/activate",
                json={"login_mode": "account", "username": "admin", "password": "123"},
            )
            self.assertEqual(acc_res.status_code, 400)
            self.assertIn("试用期", acc_res.json()["error"])

            # 2. 全局通用码激活，且更换任意机器码与 IP 后依然保持授权
            self.assertEqual(
                client.post("/api/platform/auth/activate", json={"activation_code": global_tok}).status_code,
                200,
            )
            cur_mc[0] = "MCH-COMPLETELY-DIFF-NODE"
            cur_ip[0] = "10.99.88.77"
            self.assertTrue(client.get("/api/platform/auth/status").json()["authorized"])

            # 3. 推进时钟至截止日次日 (2026-12-19) -> 自动过期拦截 401
            cur_ts[0] = datetime.datetime(2026, 12, 19, 0, 1, 0).timestamp()
            exp_st = client.get("/api/platform/auth/status").json()
            self.assertFalse(exp_st["authorized"])
            self.assertEqual(exp_st["status"], "EXPIRED")
            self.assertEqual(client.get("/api/platform/connections").status_code, 401)

            # 4. 恶意把系统时钟倒拨回 2026-11-25 试图绕过过期 -> 触发单调水位线 CLOCK_ROLLBACK 拦截
            cur_ts[0] = datetime.datetime(2026, 11, 25, 10, 0, 0).timestamp()
            rb_st = client.get("/api/platform/auth/status").json()
            self.assertFalse(rb_st["authorized"])
            self.assertEqual(rb_st["status"], "CLOCK_ROLLBACK")
            self.assertEqual(client.get("/api/platform/connections").status_code, 401)

            # 5. 校准回正常有效日期并重新签发长期码后验证手动锁定与解锁
            cur_ts[0] = datetime.datetime(2026, 12, 20, 10, 0, 0).timestamp()
            new_tok = issue_activation_code_for_seed(
                seed,
                scope=SCOPE_GLOBAL,
                duration_days=90,
                expiry_date=datetime.date(2027, 3, 19),
            )
            self.assertEqual(
                client.post("/api/platform/auth/activate", json={"activation_code": new_tok}).status_code,
                200,
            )
            self.assertEqual(client.get("/api/platform/connections").status_code, 200)

            lock_res = client.post("/api/platform/auth/lock")
            self.assertEqual(lock_res.status_code, 200)
            self.assertFalse(lock_res.json()["license"]["authorized"])
            self.assertEqual(lock_res.json()["license"]["status"], "LOCKED")
            self.assertEqual(client.get("/api/platform/connections").status_code, 401)

            # 重新激活后恢复放行
            self.assertEqual(
                client.post("/api/platform/auth/activate", json={"activation_code": new_tok}).status_code,
                200,
            )
            self.assertEqual(client.get("/api/platform/connections").status_code, 200)

    def test_standalone_c_keygen_binary_compile_and_cli_roundtrip(self) -> None:
        """端到端验证独立 C 激活码生成器构建、零依赖信息采集及各模式签发与客户端验签闭环."""
        if getattr(sys, "frozen", False):
            return
        has_cc = any(shutil.which(c) for c in ("clang", "gcc", "cc", "cl"))
        if not has_cc:
            return

        from tools.build_keygen import build_keygen

        seed = b"Test-Keygen-Binary-Seed-32Bytes!"
        pub = ed25519_derive_pubkey(seed)

        with tempfile.TemporaryDirectory() as tmp:
            seed_path = os.path.join(tmp, "test_seed.key")
            with open(seed_path, "w", encoding="utf-8") as fh:
                fh.write(seed.hex())

            exe_name = "Test_Keygen.exe" if os.name == "nt" else "Test_Keygen"
            bin_path = build_keygen(
                output_path=os.path.join(tmp, exe_name),
                seed_file=seed_path,
                allow_custom_seed=True,
            )
            self.assertTrue(os.path.isfile(bin_path))

            # 1. --info --json 自动采集本机信息
            info_out = subprocess.check_output([bin_path, "--info", "--json"], text=True, timeout=10)
            info = json.loads(info_out)
            self.assertEqual(info["machine_code"], get_machine_code_py())
            req_decoded = decode_request_code(info["request_code"])
            self.assertEqual(req_decoded["machine_code"], info["machine_code"])

            # 2. 为本机签发 30 天专属码 & 确定性唯一校验
            loc_code1 = subprocess.check_output([bin_path, "--local", "--days", "30", "--quiet"], text=True, timeout=10).strip()
            loc_code2 = subprocess.check_output([bin_path, "--local", "--days", "30", "--quiet"], text=True, timeout=10).strip()
            self.assertEqual(loc_code1, loc_code2)

            # 3. 粘贴设备申请码远程签发 & 全局通用码签发
            remote_req = encode_request_code("MCH-REMT-1111-2222-3333", "10.88.1.9", "REMOTE-HOST")
            rem_code = subprocess.check_output(
                [bin_path, "--request", remote_req, "--days", "60", "--quiet"],
                text=True,
                timeout=10,
            ).strip()
            glob_code = subprocess.check_output(
                [bin_path, "--global", "--days", "90", "--quiet"],
                text=True,
                timeout=10,
            ).strip()

            # 4. 将生成器产出的本机专属码与全局码送入 FastAPI /api/platform/auth/activate 验证端到端闭环
            app = create_app(db_path=os.path.join(tmp, "state.db"))
            ctx = app.state.platform_ctx
            ctx.license_hub = LicenseHub(
                ctx.store,
                verifier_pubkey=pub,
                device_probe_fn=lambda: {
                    "machine_code": info["machine_code"],
                    "primary_ip": info["primary_ip"],
                    "all_ips": [x.strip() for x in info["all_ips"].split(",") if x.strip()],
                    "hostname": info["hostname"],
                },
            )
            client = TestClient(app, auto_activate=False)

            r_loc = client.post("/api/platform/auth/activate", json={"activation_code": loc_code1})
            self.assertEqual(r_loc.status_code, 200)
            exp_30 = (datetime.date.today() + datetime.timedelta(days=29)).isoformat()
            self.assertIn(f"当前激活码可用日期至 {exp_30}", r_loc.json()["message"])

            # 远程专属码在其他机器上必须被拒
            r_rem_bad = client.post("/api/platform/auth/activate", json={"activation_code": rem_code})
            self.assertEqual(r_rem_bad.status_code, 400)

            # 全局码在本机与任意异机均可激活，且默认 CONTACT_INFO 文案完整
            r_glob = client.post("/api/platform/auth/activate", json={"activation_code": glob_code})
            self.assertEqual(r_glob.status_code, 200)
            self.assertEqual(r_glob.json()["license"]["scope"], "GLOBAL")
            self.assertIn("莫政辉", CONTACT_INFO["notice"])

    def test_windows_shield_compiler_matrix_and_win32_c_header_parity(self) -> None:
        """验证 Windows 多编译器静态运行时命令矩阵、重复强制编译防文件锁与 _WIN32 C 头文件兼容性."""
        import core.platform.license_hub as _lh_mod

        with mock.patch.object(_lh_mod.os, "name", "nt"), mock.patch.object(_lh_mod.sys, "platform", "win32"):
            gcc_cmds = _lh_mod._build_shield_compiler_commands("gcc", "gcc.exe", "src.c", "out.dll", "tmp")
            self.assertIn("-static", gcc_cmds[0])
            self.assertIn("-static-libgcc", gcc_cmds[0])
            self.assertIn("-fno-stack-protector", gcc_cmds[0])

            clang_cmds = _lh_mod._build_shield_compiler_commands("clang", "clang.exe", "src.c", "out.dll", "tmp")
            self.assertNotIn("-static-libgcc", clang_cmds[0])
            self.assertIn("--dependent-lib=libcmt", clang_cmds[0])

            cl_cmds = _lh_mod._build_shield_compiler_commands("cl", "cl.exe", "src.c", "out.dll", "tmp")
            self.assertIn("/MT", cl_cmds[0])
            self.assertTrue(any(arg.startswith("/Fo:") for arg in cl_cmds[0]))
            self.assertTrue(any(arg.startswith("/IMPLIB:") for arg in cl_cmds[0]))

        if getattr(sys, "frozen", False):
            return

        # 验证已加载 NativeLicenseShield 后再次调用 compile_native_license_shield(force=True) 不抛锁冲突
        _ = NativeLicenseShield()
        rebuilt = _lh_mod.compile_native_license_shield(force=True)
        if any(shutil.which(c) for c in ("clang", "gcc", "cl", "cc")):
            self.assertIsNotNone(rebuilt)
            self.assertTrue(os.path.isfile(rebuilt))

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        native_dir = os.path.join(project_root, "core", "platform", "_license_native_src")
        core_h_path = os.path.join(native_dir, "license_core.h")
        with open(core_h_path, "r", encoding="utf-8") as fh:
            core_h_src = fh.read()

        # 严禁依赖 Windows 10 专属宏 RRF_SUBKEY_WOW6464KEY，确保兼容全系列 MinGW/MSVC SDK 与 Win7+
        self.assertNotIn("RRF_SUBKEY_WOW6464KEY", core_h_src)
        self.assertIn("RegOpenKeyExA", core_h_src)
        self.assertIn("KEY_WOW64_64KEY", core_h_src)

        clang_bin = shutil.which("clang")
        if os.name != "nt" and clang_bin:
            stub_headers = {
                "winsock2.h": (
                    "#ifndef _WINSOCK2_H\n#define _WINSOCK2_H\n#include <stdint.h>\n"
                    "typedef uintptr_t SOCKET;\n#define INVALID_SOCKET ((SOCKET)(~0))\n"
                    "#define AF_INET 2\n#define SOCK_DGRAM 2\n#define IPPROTO_UDP 17\n"
                    "#define INADDR_NONE 0xffffffffUL\ntypedef unsigned short WORD;\n"
                    "#define MAKEWORD(a,b) ((WORD)(((a)&0xff)|(((b)&0xff)<<8)))\n"
                    "#define h_addr h_addr_list[0]\n"
                    "typedef struct WSAData { char d[32]; } WSADATA;\n"
                    "struct in_addr { uint32_t s_addr; };\n"
                    "struct sockaddr { unsigned short sa_family; char sa_data[14]; };\n"
                    "struct sockaddr_in { short sin_family; unsigned short sin_port; struct in_addr sin_addr; char z[8]; };\n"
                    "int WSAStartup(WORD, WSADATA *); int WSACleanup(void); int gethostname(char *, int);\n"
                    "unsigned long inet_addr(const char *); uint32_t ntohl(uint32_t); uint32_t htonl(uint32_t);\n"
                    "uint16_t htons(uint16_t); SOCKET socket(int, int, int);\n"
                    "int connect(SOCKET, const struct sockaddr *, int);\n"
                    "int getsockname(SOCKET, struct sockaddr *, int *); int closesocket(SOCKET);\n#endif\n"
                ),
                "ws2tcpip.h": "#ifndef _WS2TCPIP_H\n#define _WS2TCPIP_H\n#endif\n",
                "windows.h": (
                    "#ifndef _WINDOWS_H\n#define _WINDOWS_H\n#include <stdint.h>\n"
                    "typedef unsigned long DWORD; typedef unsigned long ULONG; typedef long LONG;\n"
                    "typedef unsigned char BYTE, *LPBYTE; typedef void *HKEY; typedef DWORD REGSAM;\n"
                    "#define HKEY_LOCAL_MACHINE ((HKEY)(uintptr_t)0x80000002)\n"
                    "#define ERROR_SUCCESS 0L\n#define ERROR_BUFFER_OVERFLOW 111L\n#define NO_ERROR 0L\n"
                    "#define KEY_READ 0x20019u\n#define KEY_WOW64_64KEY 0x0100u\n"
                    "#define REG_SZ 1u\n#define REG_EXPAND_SZ 2u\n"
                    "int SetConsoleOutputCP(unsigned int); int SetConsoleCP(unsigned int);\n#endif\n"
                ),
                "winreg.h": (
                    "#ifndef _WINREG_H\n#define _WINREG_H\n#include <windows.h>\n"
                    "LONG RegOpenKeyExA(HKEY, const char *, DWORD, REGSAM, HKEY *);\n"
                    "LONG RegQueryValueExA(HKEY, const char *, DWORD *, DWORD *, LPBYTE, DWORD *);\n"
                    "LONG RegCloseKey(HKEY);\n#endif\n"
                ),
                "iphlpapi.h": (
                    "#ifndef _IPHLPAPI_H\n#define _IPHLPAPI_H\n#include <windows.h>\n"
                    "#define MIB_IF_TYPE_LOOPBACK 24u\n"
                    "typedef struct { char String[16]; } IP_ADDRESS_STRING;\n"
                    "typedef struct _IP_ADDR_STRING { struct _IP_ADDR_STRING *Next; IP_ADDRESS_STRING IpAddress; } IP_ADDR_STRING, *PIP_ADDR_STRING;\n"
                    "typedef struct _IP_ADAPTER_INFO { struct _IP_ADAPTER_INFO *Next; char AdapterName[260]; char Description[132]; unsigned int Type; IP_ADDR_STRING IpAddressList; } IP_ADAPTER_INFO, *PIP_ADAPTER_INFO;\n"
                    "ULONG GetAdaptersInfo(PIP_ADAPTER_INFO, ULONG *);\n#endif\n"
                ),
            }
            with tempfile.TemporaryDirectory() as tmp:
                for fname, content in stub_headers.items():
                    with open(os.path.join(tmp, fname), "w", encoding="utf-8") as fh:
                        fh.write(content)
                with open(os.path.join(tmp, "keygen_secret_tables.h"), "w", encoding="utf-8") as fh:
                    perm_csv = ",".join(str(i) for i in range(32))
                    fh.write(
                        f"#include <stdint.h>\nstatic const uint8_t DOG_SK_PERM[32]={{{perm_csv}}};\n"
                        "static const uint64_t DOG_SK_T0[4]={0};\nstatic const uint64_t DOG_SK_T1[4]={0};\n"
                        "static const uint64_t DOG_SK_T2[4]={0};\nstatic const uint64_t DOG_SK_T3[4]={0};\n"
                    )
                shim = os.path.join(tmp, "win32_shim.h")
                with open(shim, "w", encoding="utf-8") as fh:
                    fh.write(
                        "#include <stdio.h>\n#include <time.h>\n#undef __APPLE__\n#define _WIN32 1\n"
                        "static inline int localtime_s(struct tm *t, const time_t *n){*t=*gmtime(n);return 0;}\n"
                        "static inline FILE *_popen(const char *c, const char *m){(void)c;(void)m;return NULL;}\n"
                        "static inline int _pclose(FILE *f){(void)f;return 0;}\n"
                    )
                for rel_c in (
                    os.path.join("core", "platform", "_license_native_src", "license_shield.c"),
                    os.path.join("tools", "keygen_src", "keygen.c"),
                ):
                    c_path = os.path.join(project_root, rel_c)
                    proc = subprocess.run(
                        [
                            clang_bin,
                            "-fsyntax-only",
                            "-std=c11",
                            "-fdeclspec",
                            "-Wall",
                            "-Wextra",
                            "-Werror",
                            "-Wno-unused-function",
                            "-Wno-ignored-attributes",
                            "-include",
                            shim,
                            f"-I{tmp}",
                            f"-I{native_dir}",
                            c_path,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 0, f"_WIN32 语法校验未通过 ({rel_c}): {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
