"""Data.O.G v2.7.0 三层分离架构（无状态应用 + 混淆私有库 + 团队共享工作区与引导式增量合并）端到端测试."""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from app import create_app
from core.platform.context import PlatformContext
from core.platform.license_hub import (
    SCOPE_GLOBAL,
    ed25519_derive_pubkey,
    issue_activation_code_for_seed,
)
from core.platform.models import ConnectionProfile
from core.platform.storage import (
    PRIVATE_ONLY_TABLES,
    WORKSPACE_BUSINESS_TABLES,
    PlatformStateStore,
)
from core.platform.storage_paths import (
    decode_obfuscated_anchor,
    encode_obfuscated_anchor,
    resolve_private_storage_layout,
)
from modules.data_explorer.service import DataExplorerService
from modules.standard_migrator.service import StandardMigratorService
from tests.test_modules_and_api import TestClient


def _sqlite_table_names(db_file: str) -> set[str]:
    with contextlib.closing(sqlite3.connect(db_file)) as conn:
        return {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not str(r[0]).startswith("sqlite_")
        }


def _sqlite_column_names(db_file: str, table_name: str) -> set[str]:
    with contextlib.closing(sqlite3.connect(db_file)) as conn:
        return {
            str(r[1])
            for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }


def _make_valid_jar_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/services/java.sql.Driver", "dm.jdbc.driver.DmDriver\n")
        zf.writestr("dm/jdbc/driver/DmDriver.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x34")
    return buf.getvalue()


class TestWorkspaceThreeTierSeparation(unittest.TestCase):
    def test_obfuscated_private_storage_paths_and_zero_path_exposure(self) -> None:
        """验证 OS 私有路径混淆无品牌明文、确定性解析、锚点编码与全量 API 零路径泄露."""
        # 1. 验证 Windows / macOS / Linux 三端混淆路径均不含敏感字样及 .db 后缀且确定性一致
        forbidden_tokens = ("dataog", "license", "private", "vault", "secret", ".db")
        with mock.patch.dict(os.environ, {"DATAOG_HOME": ""}, clear=False):
            with mock.patch.dict(
                os.environ, {"LOCALAPPDATA": "/mock_win_localappdata"}, clear=False
            ):
                win_a = resolve_private_storage_layout(os_name="nt", platform_name="win32")
                win_b = resolve_private_storage_layout(os_name="nt", platform_name="win32")
                self.assertEqual(win_a.private_root, win_b.private_root)
                win_base = os.path.abspath("/mock_win_localappdata")
                self.assertTrue(win_a.private_root.startswith(win_base))
                for p in (
                    win_a.private_db_path,
                    win_a.default_workspace_path,
                    win_a.clock_anchor_path,
                    win_a.service_log_path,
                ):
                    rel_win = os.path.relpath(p, win_base).lower()
                    for tok in forbidden_tokens:
                        self.assertNotIn(tok, rel_win, f"Windows 私有路径不得包含明文特征 {tok!r}: {rel_win}")
                self.assertTrue(win_a.private_db_path.endswith(".dat"))
                self.assertTrue(win_a.default_workspace_path.endswith(".dat"))
                self.assertTrue(win_a.clock_anchor_path.endswith(".bin"))

            mac_layout = resolve_private_storage_layout(os_name="posix", platform_name="darwin")
            self.assertIn("Application Support", mac_layout.private_root)
            for p in (
                mac_layout.private_db_path,
                mac_layout.default_workspace_path,
                mac_layout.clock_anchor_path,
            ):
                mac_tail = p.split("Application Support", 1)[-1].lower()
                for tok in forbidden_tokens:
                    self.assertNotIn(tok, mac_tail)

            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/mock_xdg_share"}, clear=False):
                lin_layout = resolve_private_storage_layout(os_name="posix", platform_name="linux")
                lin_base = os.path.abspath("/mock_xdg_share")
                self.assertTrue(lin_layout.private_root.startswith(lin_base))
                for p in (
                    lin_layout.private_db_path,
                    lin_layout.default_workspace_path,
                    lin_layout.clock_anchor_path,
                ):
                    lin_rel = os.path.relpath(p, lin_base).lower()
                    for tok in forbidden_tokens:
                        self.assertNotIn(tok, lin_rel)

        # 2. 验证混淆时钟锚点编解码往返及旧版明文 JSON 兼容
        sample_seal = {"ts": 1795000000, "sig": "abcdef1234567890"}
        encoded = encode_obfuscated_anchor(sample_seal)
        self.assertTrue(encoded.startswith("V2:"))
        self.assertNotIn("1795000000", encoded)
        self.assertEqual(decode_obfuscated_anchor(encoded), sample_seal)
        self.assertEqual(decode_obfuscated_anchor(json.dumps(sample_seal)), sample_seal)

        # 3. 验证 DATAOG_HOME 隔离启动且全量 API 绝不泄露任何私有存储路径或内部文件名
        with tempfile.TemporaryDirectory() as tmp:
            priv_root = os.path.join(tmp, "secret_priv_home_xyz")
            app = create_app(private_root=priv_root)
            client = TestClient(app)
            ctx: PlatformContext = app.state.platform_ctx
            self.assertTrue(ctx.store.is_split_storage)
            # 确认私有目录内所有落盘文件名均不含 dataog 明文
            for dirpath, _dirnames, filenames in os.walk(priv_root):
                for fn in filenames:
                    self.assertNotIn("dataog", fn.lower(), f"私有库内部文件名不得含品牌明文: {fn}")

            leaked_markers = [
                priv_root,
                os.path.basename(ctx.store.private_db_path),
                os.path.basename(ctx.store.workspace_db_path),
                "ws_store",
                "res_obj",
                "ext_drv",
                "rt_log",
                ".chk",
            ]
            endpoints = [
                "/api/platform/auth/status",
                "/api/platform/bootstrap",
                "/api/platform/workspaces",
                "/api/platform/connections",
                "/api/platform/logs",
            ]
            for ep in endpoints:
                resp = client.get(ep)
                self.assertEqual(resp.status_code, 200, ep)
                raw_text = resp.text
                for marker in leaked_markers:
                    self.assertNotIn(
                        marker,
                        raw_text,
                        f"接口 {ep} 严禁暴露私有存储路径或内部文件名 ({marker})",
                    )

            boot = client.get("/api/platform/bootstrap").json()
            self.assertEqual(boot["version"], "2.7.0")
            self.assertEqual(boot["storage_mode"], "SEPARATED")
            self.assertEqual(boot["active_workspace"]["workspace_id"], "ws-default")

        # 4. 源码态验证 build_exe.py 与 app.py 不再创建程序目录 data/ 或打印私有路径（冻结态跳过源码文件读取）
        if not getattr(sys, "frozen", False):
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(project_root, "build_exe.py"), encoding="utf-8") as fh:
                build_src = fh.read()
            self.assertNotIn('os.makedirs(os.path.join(dist_bundle_dir, "data"', build_src)
            with open(os.path.join(project_root, "app.py"), encoding="utf-8") as fh:
                app_src = fh.read()
            self.assertNotIn("本地状态库存储路径", app_src)

    def test_physical_three_tier_table_separation_and_zero_secret_export(self) -> None:
        """验证物理双库表级完全隔离及导出 .dataog.db 零密钥、零私有表保障."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
        with tempfile.TemporaryDirectory() as tmp:
            priv_root = os.path.join(tmp, "vault_root")
            app = create_app(private_root=priv_root)
            client = TestClient(app)
            ctx: PlatformContext = app.state.platform_ctx

            # 写入全谱系 Layer 2 私有数据
            ctx.llm_hub.save_provider(
                {
                    "provider_id": "deepseek",
                    "enabled": True,
                    "api_key": "sk-top-secret-deepseek-99999",
                    "default_model": "deepseek-chat",
                }
            )
            ctx.asset_store.save_uploaded_asset(slot="wallpaper", filename="bg.png", content=png_bytes)
            ctx.driver_manager.register_uploaded_jar(filename="dm.jar", content=_make_valid_jar_bytes(), name="达梦驱动")

            # 写入带密码与私密扩展属性的数据源
            prof = ctx.connection_hub.save_profile(
                {
                    "connection_id": "conn-dws-prod",
                    "name": "生产 DWS",
                    "db_type": "DWS",
                    "host": "10.66.1.10",
                    "port": 8000,
                    "dbname": "prod_dw",
                    "default_schema": "dwd",
                    "user": "alice_secret_user",
                    "password": "AliceSuperPassword!2026",
                    "standard_format": "type_dws",
                    "extra_props": {"application_name": "dataog", "secret_token": "tok-ultra-secret-88"},
                }
            )
            self.assertEqual(prof.password, "AliceSuperPassword!2026")
            self.assertEqual(prof.credential_status, "BOUND")
            pub = prof.to_public_dict()
            self.assertNotIn("secret_token", pub["extra_props"])
            self.assertEqual(pub["extra_props"].get("application_name"), "dataog")

            # 写入个人 AI 探查会话（必须进入私有库而非工作区）
            exp_svc = DataExplorerService(ctx)
            sess = exp_svc.create_session(connection_id="conn-dws-prod", title="我的私人探查记录")
            self.assertTrue(sess["session_id"].startswith("exp-"))

            # 写入 Layer 3 业务数据
            ctx.standard_catalog.upsert_item(
                {
                    "std_code": "STD_CUST_ID",
                    "cn_name": "客户编号",
                    "en_abbr": "cust_id",
                    "type_dws": "VARCHAR(64)",
                }
            )
            ctx.standard_catalog.upsert_field_bindings(
                "conn-dws-prod",
                [
                    {
                        "schema_name": "dwd",
                        "table_name": "t_cust",
                        "column_name": "cust_id",
                        "std_code": "STD_CUST_ID",
                    }
                ],
            )
            ctx.store.upsert_cross_table_binding(
                "t_cust",
                source_schema="ods",
                source_table="t_cust_raw",
                target_schema="dwd",
                target_table="t_cust",
            )
            ctx.store.upsert_cross_table_column_bindings(
                "t_cust",
                [
                    {
                        "std_col_name": "cust_id",
                        "bound_source_col": "raw_cid",
                        "bound_target_col": "cust_id",
                        "source_std_code": "STD_CUST_ID",
                        "target_std_code": "STD_CUST_ID",
                    }
                ],
            )
            ctx.store.replace_all_items(
                [
                    {
                        "schema_name": "dwd",
                        "table_name": "t_cust",
                        "old_col_name": "cid",
                        "new_col_name": "cust_id",
                        "new_col_type": "VARCHAR(64)",
                    }
                ],
                "plan.xlsx",
            )
            ctx.store.save_catalog_snapshot(
                "conn-dws-prod",
                schemas=[],
                tables=[{"schema_name": "dwd", "table_name": "t_cust", "object_type": "TABLE"}],
            )

            # 1. 物理校验：私有库与工作区库表集合彻底分离
            priv_tables = _sqlite_table_names(ctx.store.private_db_path)
            ws_tables = _sqlite_table_names(ctx.store.workspace_db_path)
            for t in ("app_meta", "workspaces", "llm_providers", "connection_credentials", "jdbc_drivers", "execution_logs", "explorer_sessions", "explorer_messages"):
                self.assertIn(t, priv_tables)
                self.assertNotIn(t, ws_tables, f"私有表 {t} 严禁出现在工作区 DB 中")
            for t in WORKSPACE_BUSINESS_TABLES:
                self.assertIn(t, ws_tables)
                self.assertNotIn(t, priv_tables, f"业务表 {t} 不应出现在私有库 DB 中")

            # connection_topology 结构上物理不存在 user/password 列
            topo_cols = _sqlite_column_names(ctx.store.workspace_db_path, "connection_topology")
            self.assertNotIn("user", topo_cols)
            self.assertNotIn("password", topo_cols)

            # 2. 导出共享工作区（含元数据缓存 & 不含元数据缓存）并做原始字节级扫描防泄密
            exp_full = client.get("/api/platform/workspaces/export?include_metadata_cache=true")
            self.assertEqual(exp_full.status_code, 200)
            self.assertTrue(exp_full.content.startswith(b"SQLite format 3\x00"))

            forbidden_secrets = [
                b"sk-top-secret-deepseek-99999",
                b"AliceSuperPassword!2026",
                b"alice_secret_user",
                b"tok-ultra-secret-88",
                b"DOG1-",
                "我的私人探查记录".encode("utf-8"),
            ]
            for sec in forbidden_secrets:
                self.assertNotIn(sec, exp_full.content, f"导出的 .dataog.db 中绝不允许残留敏感内容: {sec!r}")

            exp_file = os.path.join(tmp, "exported_full.dataog.db")
            with open(exp_file, "wb") as fh:
                fh.write(exp_full.content)
            exp_tbls = _sqlite_table_names(exp_file)
            self.assertEqual(exp_tbls, set(WORKSPACE_BUSINESS_TABLES))
            for pt in PRIVATE_ONLY_TABLES:
                self.assertNotIn(pt, exp_tbls)
            with contextlib.closing(sqlite3.connect(exp_file)) as econn:
                self.assertEqual(econn.execute("SELECT COUNT(*) FROM meta_tables").fetchone()[0], 1)
                self.assertEqual(econn.execute("SELECT COUNT(*) FROM standard_dictionary").fetchone()[0], 1)

            exp_slim = client.get("/api/platform/workspaces/export?include_metadata_cache=false")
            self.assertEqual(exp_slim.status_code, 200)
            slim_file = os.path.join(tmp, "exported_slim.dataog.db")
            with open(slim_file, "wb") as fh:
                fh.write(exp_slim.content)
            with contextlib.closing(sqlite3.connect(slim_file)) as sconn:
                self.assertEqual(sconn.execute("SELECT COUNT(*) FROM meta_tables").fetchone()[0], 0)
                self.assertEqual(sconn.execute("SELECT COUNT(*) FROM standard_dictionary").fetchone()[0], 1)

    def test_workspace_lifecycle_and_credential_fingerprint_auto_match(self) -> None:
        """验证多工作区新建/克隆/重命名/切换/删除、私有配置跨切换留存及端点指纹个人凭据自动匹配."""
        with tempfile.TemporaryDirectory() as tmp:
            # --- User A: 创建数据源并导出工作区 ---
            app_a = create_app(private_root=os.path.join(tmp, "user_a_vault"))
            client_a = TestClient(app_a)
            ctx_a: PlatformContext = app_a.state.platform_ctx
            ctx_a.connection_hub.save_profile(
                {
                    "connection_id": "conn-a-dws",
                    "name": "团队DWS主库",
                    "db_type": "DWS",
                    "host": "10.20.30.100",
                    "port": 8000,
                    "dbname": "team_dw",
                    "user": "alice",
                    "password": "AlicePassword!",
                    "standard_format": "type_dws",
                }
            )
            ctx_a.connection_hub.save_profile(
                {
                    "connection_id": "conn-a-mysql",
                    "name": "团队MySQL业务库",
                    "db_type": "MYSQL",
                    "host": "10.20.30.200",
                    "port": 3306,
                    "dbname": "biz_app",
                    "user": "alice_my",
                    "password": "AliceMysqlPwd!",
                }
            )
            ctx_a.standard_catalog.upsert_item(
                {"std_code": "STD_A1", "cn_name": "账户号", "en_abbr": "acct_no", "type_dws": "VARCHAR(32)"}
            )
            shared_bytes = client_a.get("/api/platform/workspaces/export").content

            # --- User B: 已有自己独立的 DWS 个人凭据 + AI 密钥，导入 User A 的工作区 ---
            app_b = create_app(private_root=os.path.join(tmp, "user_b_vault"))
            client_b = TestClient(app_b)
            ctx_b: PlatformContext = app_b.state.platform_ctx
            ctx_b.llm_hub.save_provider(
                {"provider_id": "deepseek", "enabled": True, "api_key": "sk-bob-personal-key-777"}
            )
            # User B 预先保存了自己对 10.20.30.100:8000/team_dw 的个人账号密码（不同的 connection_id!）
            ctx_b.connection_hub.save_profile(
                {
                    "connection_id": "conn-bob-own-dws",
                    "name": "Bob个人DWS",
                    "db_type": "DWS",
                    "host": "10.20.30.100",
                    "port": 8000,
                    "dbname": "team_dw",
                    "user": "bob_personal",
                    "password": "BobPersonalPassword!88",
                }
            )

            imp_res = client_b.post(
                "/api/platform/workspaces/import",
                data={"name": "Alice分享的工作区", "switch_immediately": "true"},
                files={"file": ("alice_team.dataog.db", shared_bytes, "application/octet-stream")},
            )
            self.assertEqual(imp_res.status_code, 200)
            imp_json = imp_res.json()
            self.assertTrue(imp_json["ok"])
            self.assertEqual(imp_json["credential_summary"]["auto_matched_count"], 1)
            self.assertEqual(imp_json["credential_summary"]["needs_credentials_count"], 1)

            # 校验端点指纹自动匹配：DWS 自动绑定 Bob 个人凭据，MySQL 标记 MISSING
            conns_b = {c["connection_id"]: c for c in client_b.get("/api/platform/connections").json()["connections"]}
            self.assertEqual(conns_b["conn-a-dws"]["credential_status"], "AUTO_MATCHED")
            self.assertEqual(conns_b["conn-a-dws"]["user"], "bob_personal")
            self.assertTrue(conns_b["conn-a-dws"]["has_saved_password"])
            self.assertEqual(ctx_b.connection_hub.require_profile("conn-a-dws").password, "BobPersonalPassword!88")

            self.assertEqual(conns_b["conn-a-mysql"]["credential_status"], "MISSING")
            self.assertEqual(conns_b["conn-a-mysql"]["user"], "")
            self.assertFalse(conns_b["conn-a-mysql"]["has_saved_password"])

            # 校验私有 AI 密钥跨工作区切换完全保留
            ds_b = ctx_b.store.get_llm_provider("deepseek")
            self.assertIsNotNone(ds_b)
            assert ds_b is not None
            self.assertEqual(ds_b.api_key, "sk-bob-personal-key-777")

            # 创建空白工作区、克隆工作区、重命名、切回、禁止删活动工作区、删除非活动工作区
            imported_ws_id = imp_json["workspace"]["workspace_id"]
            blank_res = client_b.post(
                "/api/platform/workspaces",
                json={"name": "空白沙箱区", "description": "独立测试", "switch_immediately": True},
            ).json()
            self.assertTrue(blank_res["ok"])
            blank_ws_id = blank_res["workspace"]["workspace_id"]
            self.assertEqual(blank_res["bootstrap"]["standard_dict_summary"]["total_count"], 0)

            clone_res = client_b.post(
                "/api/platform/workspaces",
                json={"name": "克隆分支", "clone_from_workspace_id": imported_ws_id, "switch_immediately": True},
            ).json()
            self.assertTrue(clone_res["ok"])
            clone_ws_id = clone_res["workspace"]["workspace_id"]
            self.assertEqual(clone_res["bootstrap"]["standard_dict_summary"]["total_count"], 1)

            ren_res = client_b.put(
                f"/api/platform/workspaces/{clone_ws_id}",
                json={"name": "克隆分支_已改名", "description": "备注已更新"},
            ).json()
            self.assertEqual(ren_res["workspace"]["name"], "克隆分支_已改名")

            # 禁止删除当前活动工作区
            del_active = client_b.delete(f"/api/platform/workspaces/{clone_ws_id}")
            self.assertEqual(del_active.status_code, 400)

            # 验证重启后活动工作区自动恢复为上次切换的克隆分支，且私有库未受影响
            app_b_reopen = create_app(private_root=os.path.join(tmp, "user_b_vault"))
            ctx_b_reopen: PlatformContext = app_b_reopen.state.platform_ctx
            self.assertEqual(
                ctx_b_reopen.workspace_hub.get_active_workspace_summary()["workspace_id"],
                clone_ws_id,
            )
            self.assertEqual(ctx_b_reopen.standard_catalog.get_summary()["total_count"], 1)
            self.assertEqual(ctx_b_reopen.store.get_llm_provider("deepseek").api_key, "sk-bob-personal-key-777")

            # 切换回默认工作区后再删除克隆分支与空白沙箱区
            sw_res = client_b.post("/api/platform/workspaces/ws-default/switch").json()
            self.assertEqual(sw_res["active_workspace"]["workspace_id"], "ws-default")
            self.assertEqual(client_b.delete(f"/api/platform/workspaces/{clone_ws_id}").status_code, 200)
            self.assertEqual(client_b.delete(f"/api/platform/workspaces/{blank_ws_id}").status_code, 200)

            # 未激活状态下全部工作区接口必须被 Gate 1 / Gate 2 拦截为 401 LICENSE_REQUIRED
            unact_app = create_app(private_root=os.path.join(tmp, "unactivated_vault"))
            unact_client = TestClient(unact_app, auto_activate=False)
            for method, path in (
                ("GET", "/api/platform/workspaces"),
                ("GET", "/api/platform/workspaces/export"),
                ("POST", "/api/platform/workspaces"),
            ):
                r401 = unact_client.request(method, path)
                self.assertEqual(r401.status_code, 401, f"{method} {path} 未激活应返回 401")
                self.assertEqual(r401.json().get("code"), "LICENSE_REQUIRED")

            # 伪造魔数头损坏文件与无关 SQLite 文件必须优雅拦截为 400
            corrupt_bytes = b"SQLite format 3\x00" + b"\xff" * 200
            self.assertEqual(
                client_b.post(
                    "/api/platform/workspaces/merge/inspect",
                    files={"file": ("corrupt.dataog.db", corrupt_bytes, "application/octet-stream")},
                ).status_code,
                400,
            )
            unrelated_path = os.path.join(tmp, "unrelated.db")
            with contextlib.closing(sqlite3.connect(unrelated_path)) as uconn:
                uconn.execute("CREATE TABLE foo (id INT)")
                uconn.commit()
            with open(unrelated_path, "rb") as ufh:
                unrelated_bytes = ufh.read()
            self.assertEqual(
                client_b.post(
                    "/api/platform/workspaces/import",
                    files={"file": ("unrelated.db", unrelated_bytes, "application/octet-stream")},
                ).status_code,
                400,
            )

    def test_guided_incremental_merge_strategies_legacy_db_and_table_diffs(self) -> None:
        """端到端验证增量合并向导：兼容旧版 dataog_state.db、白名单防污染、端点重映射、表对差异预览与三大策略."""
        with tempfile.TemporaryDirectory() as tmp:
            # 1. 构造同事的旧版 v2.6.1 dataog_state.db（内含私密激活码/AI密钥/DB密码/探查会话 + 业务数据）
            mate_db = os.path.join(tmp, "mate_legacy_dataog_state.db")
            mate_ctx = PlatformContext(db_path=mate_db)
            mate_ctx.store.save_meta("license_activation_code", "DOG1-MATE-EVIL-FAKE-TOKEN")
            mate_ctx.llm_hub.save_provider(
                {"provider_id": "deepseek", "enabled": True, "api_key": "sk-mate-secret-must-ignore"}
            )
            # 模拟旧版 connection_profiles 表（含明文账号密码），测试合并时绝不读取明文凭据
            with contextlib.closing(sqlite3.connect(mate_db)) as mconn:
                mconn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS connection_profiles (
                        connection_id TEXT PRIMARY KEY, name TEXT NOT NULL, driver_track TEXT DEFAULT 'NATIVE',
                        db_type TEXT DEFAULT 'DWS', host TEXT DEFAULT '', port INTEGER DEFAULT 8000,
                        dbname TEXT DEFAULT '', default_schema TEXT DEFAULT 'public',
                        user TEXT DEFAULT '', password TEXT DEFAULT '', sslmode TEXT DEFAULT 'prefer',
                        auth_mode TEXT DEFAULT 'LDAP', sqlite_path TEXT DEFAULT '',
                        jdbc_driver_id TEXT DEFAULT '', jdbc_url TEXT DEFAULT '',
                        jdbc_driver_class TEXT DEFAULT '', extra_props_json TEXT DEFAULT '{}',
                        standard_format TEXT DEFAULT 'AUTO', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
                    );
                    INSERT INTO connection_profiles (
                        connection_id, name, db_type, host, port, dbname, default_schema,
                        user, password, standard_format, updated_at
                    ) VALUES
                        ('conn-mate-dws', '同事DWS库', 'DWS', '10.88.1.1', 8000, 'prod_dw', 'public',
                         'mate_user', 'MateSecretPwd!99', 'type_dws', '2026-11-15 10:00:00'),
                        ('conn-mate-mysql', '同事新MySQL', 'MYSQL', '10.88.2.2', 3306, 'crm', 'crm',
                         'mate_my', 'MateMyPwd!88', 'type_mysql', '2026-11-15 10:00:00');
                    DELETE FROM connection_topology;
                    """
                )
                mconn.execute(
                    """
                    INSERT INTO standard_dictionary (std_code, cn_name, en_abbr, type_dws, updated_at) VALUES
                        ('STD_1', '客户号_同事新版', 'cust_no', 'VARCHAR(64)', '2026-11-15 10:00:00'),
                        ('STD_2', '余额_同事旧版', 'bal_amt', 'DECIMAL(16,2)', '2026-11-12 10:00:00'),
                        ('STD_3', '新增币种标准', 'ccy_cd', 'VARCHAR(8)', '2026-11-15 10:00:00')
                    """
                )
                mconn.execute(
                    """
                    INSERT INTO field_standard_bindings (
                        connection_id, schema_name, table_name, column_name, col_key, std_code, updated_at
                    ) VALUES
                        ('conn-mate-dws', 'public', 't_ord', 'col_a', 'public::t_ord::col_a', 'STD_3', '2026-11-15 10:00:00'),
                        ('conn-mate-dws', 'public', 't_ord', 'col_b', 'public::t_ord::col_b', 'STD_3', '2026-11-12 10:00:00'),
                        ('conn-mate-dws', 'public', 't_ord', 'col_empty', 'public::t_ord::col_empty', 'STD_3', '2026-11-15 10:00:00'),
                        ('conn-mate-dws', 'public', 't_ord', 'col_new', 'public::t_ord::col_new', 'STD_3', '2026-11-15 10:00:00')
                    """
                )
                mconn.execute(
                    """
                    INSERT INTO cross_table_bindings (
                        std_table_key, std_schema_name, std_table_name,
                        source_schema, source_table, target_schema, target_table, updated_at
                    ) VALUES
                        ('t_orders', 'public', 't_orders', 'ods', 't_orders', 'dwd', 't_orders', '2026-11-15 10:00:00'),
                        ('t_customers', 'public', 't_customers', 'ods', 't_customers', 'dwd', 't_customers', '2026-11-15 10:00:00')
                    """
                )
                mconn.execute(
                    """
                    INSERT INTO cross_column_bindings (
                        std_table_key, row_index, orig_col_name, std_col_name,
                        bound_source_col, bound_target_col, source_std_code, target_std_code, updated_at
                    ) VALUES
                        ('t_orders', 1, 'ord_id', 'ord_id', 'ord_id', 'ord_id', 'STD_3', 'STD_3', '2026-11-15 10:00:00'),
                        ('t_orders', 2, 'cust_id', 'cust_id', 'cust_id', 'cust_id', 'STD_1', 'STD_1', '2026-11-15 10:00:00'),
                        ('t_customers', 1, 'cid', 'cust_id', 'cid', 'cust_id', 'STD_1', 'STD_1', '2026-11-15 10:00:00')
                    """
                )
                mconn.execute(
                    """
                    INSERT INTO migration_items (
                        row_index, schema_name, table_name, old_col_name, new_col_name, new_col_type, updated_at
                    ) VALUES (1, 'public', 't_ord', 'c_old', 'c_new', 'VARCHAR(32)', '2026-11-15 10:00:00')
                    """
                )
                mconn.execute(
                    """
                    INSERT INTO meta_tables (connection_id, schema_name, table_name, columns_cached, refreshed_at)
                    VALUES ('conn-mate-dws', 'public', 't_ord', 1, '2026-11-15 10:00:00')
                    """
                )
                mconn.commit()
            with open(mate_db, "rb") as fh:
                mate_bytes = fh.read()

            # 2. 构造本地用户基准工作区，并分别测试 supplement / newer / overwrite 三种策略
            def _make_local_env(tag: str):
                app = create_app(private_root=os.path.join(tmp, f"local_{tag}"))
                client = TestClient(app)
                ctx: PlatformContext = app.state.platform_ctx
                ctx.llm_hub.save_provider(
                    {"provider_id": "deepseek", "enabled": True, "api_key": "sk-local-private-key-111"}
                )
                ctx.connection_hub.save_profile(
                    {
                        "connection_id": "conn-local-dws",
                        "name": "本地DWS生产",
                        "db_type": "DWS",
                        "host": "10.88.1.1",
                        "port": 8000,
                        "dbname": "prod_dw",
                        "user": "local_user",
                        "password": "LocalOwnPassword!11",
                        "standard_format": "AUTO",
                    }
                )
                with ctx.store.module_db("workspace") as wconn:
                    wconn.execute(
                        """
                        INSERT INTO standard_dictionary (std_code, cn_name, en_abbr, type_dws, updated_at) VALUES
                            ('STD_1', '客户号_本地旧版', 'cust_no', 'VARCHAR(32)', '2026-11-10 10:00:00'),
                            ('STD_2', '余额_本地新版', 'bal_amt', 'DECIMAL(18,2)', '2026-11-18 10:00:00')
                        """
                    )
                    wconn.execute(
                        """
                        INSERT INTO field_standard_bindings (
                            connection_id, schema_name, table_name, column_name, col_key, std_code, updated_at
                        ) VALUES
                            ('conn-local-dws', 'public', 't_ord', 'col_a', 'public::t_ord::col_a', 'STD_1', '2026-11-10 10:00:00'),
                            ('conn-local-dws', 'public', 't_ord', 'col_b', 'public::t_ord::col_b', 'STD_2', '2026-11-18 10:00:00'),
                            ('conn-local-dws', 'public', 't_ord', 'col_empty', 'public::t_ord::col_empty', '', '2026-11-10 10:00:00')
                        """
                    )
                    wconn.execute(
                        """
                        INSERT INTO cross_table_bindings (
                            std_table_key, std_schema_name, std_table_name,
                            source_schema, source_table, target_schema, target_table, updated_at
                        ) VALUES
                            ('t_orders', 'public', 't_orders', 'ods', 't_orders', 'dwd', 't_orders', '2026-11-10 10:00:00'),
                            ('t_local_only', 'public', 't_local_only', 'ods', 't_local_only', 'dwd', 't_local_only', '2026-11-10 10:00:00')
                        """
                    )
                    wconn.execute(
                        """
                        INSERT INTO cross_column_bindings (
                            std_table_key, row_index, orig_col_name, std_col_name,
                            bound_source_col, bound_target_col, source_std_code, target_std_code, updated_at
                        ) VALUES
                            ('t_orders', 1, 'ord_id', 'ord_id', 'ord_id', 'ord_id', 'STD_1', 'STD_1', '2026-11-10 10:00:00'),
                            ('t_local_only', 1, 'loc_id', 'loc_id', 'loc_id', 'loc_id', 'STD_1', 'STD_1', '2026-11-10 10:00:00')
                        """
                    )
                return app, client, ctx

            all_mods = [
                "standard_dictionary",
                "field_standard_bindings",
                "cross_mappings",
                "migration_items",
                "connection_topology",
                "metadata_cache",
            ]

            # --- 策略 A: supplement（仅补充缺失/未绑定） + 校验预检结果与边界拦截 ---
            _, client_sup, ctx_sup = _make_local_env("supplement")
            # 非法非 SQLite 文件必须拦截
            bad_up = client_sup.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("fake.dataog.db", b"NOT_A_SQLITE_FILE" * 20, "application/octet-stream")},
            )
            self.assertEqual(bad_up.status_code, 400)

            insp = client_sup.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("dataog_state.db", mate_bytes, "application/octet-stream")},
            ).json()
            self.assertTrue(insp["ok"])
            self.assertEqual(insp["source_info"]["source_kind"], "LEGACY_STATE_DB")
            # 端点指纹映射校验：conn-mate-dws 自动映射到 conn-local-dws
            cprev = {c["source_connection_id"]: c for c in insp["connections_preview"]}
            self.assertEqual(cprev["conn-mate-dws"]["action"], "MAP_EXISTING")
            self.assertEqual(cprev["conn-mate-dws"]["mapped_connection_id"], "conn-local-dws")
            self.assertEqual(cprev["conn-mate-mysql"]["action"], "ADD")
            self.assertEqual(cprev["conn-mate-mysql"]["credential_match"], "NEEDS_INPUT")
            # 跨环境表对差异预览校验
            tdiffs = {d["std_table_key"]: d for d in insp["table_diffs"]}
            self.assertEqual(tdiffs["t_orders"]["status"], "CONFLICT")
            self.assertEqual(tdiffs["t_orders"]["added_cols_count"], 1)
            self.assertTrue(tdiffs["t_orders"]["source_is_newer"])
            self.assertEqual(tdiffs["t_customers"]["status"], "NEW")

            res_sup = client_sup.post(
                "/api/platform/workspaces/merge/execute",
                json={"merge_token": insp["merge_token"], "selected_modules": all_mods, "strategy": "supplement"},
            ).json()
            self.assertTrue(res_sup["ok"])
            # 重复使用同一 merge_token 必须拒绝
            self.assertEqual(
                client_sup.post(
                    "/api/platform/workspaces/merge/execute",
                    json={"merge_token": insp["merge_token"], "selected_modules": all_mods, "strategy": "supplement"},
                ).status_code,
                400,
            )

            with ctx_sup.store.module_db("workspace") as wc:
                stds = {r["std_code"]: r["cn_name"] for r in wc.execute("SELECT std_code, cn_name FROM standard_dictionary").fetchall()}
                self.assertEqual(stds["STD_1"], "客户号_本地旧版")  # 不覆盖
                self.assertEqual(stds["STD_2"], "余额_本地新版")
                self.assertEqual(stds["STD_3"], "新增币种标准")
                fsb = {
                    (r["connection_id"], r["column_name"]): r["std_code"]
                    for r in wc.execute("SELECT connection_id, column_name, std_code FROM field_standard_bindings").fetchall()
                }
                self.assertEqual(fsb[("conn-local-dws", "col_a")], "STD_1")
                self.assertEqual(fsb[("conn-local-dws", "col_b")], "STD_2")
                self.assertEqual(fsb[("conn-local-dws", "col_empty")], "STD_3")  # 补充未绑定
                self.assertEqual(fsb[("conn-local-dws", "col_new")], "STD_3")  # 补充缺失且重映射到 conn-local-dws
                # cross_mappings: t_orders 保留原 ord_id 并补充新列 cust_id，t_customers 新增，t_local_only 保留
                ord_cols = {
                    r["std_col_name"]: r["source_std_code"]
                    for r in wc.execute("SELECT std_col_name, source_std_code FROM cross_column_bindings WHERE std_table_key='t_orders'").fetchall()
                }
                self.assertEqual(ord_cols, {"ord_id": "STD_1", "cust_id": "STD_1"})
                tbl_keys = {r["std_table_key"] for r in wc.execute("SELECT std_table_key FROM cross_table_bindings").fetchall()}
                self.assertEqual(tbl_keys, {"t_orders", "t_customers", "t_local_only"})

            # 绝对安全校验：本地私有库激活码、AI 密钥、DB 密码丝毫未被同事旧库污染
            self.assertNotEqual(ctx_sup.store.get_meta("license_activation_code"), "DOG1-MATE-EVIL-FAKE-TOKEN")
            self.assertEqual(ctx_sup.store.get_llm_provider("deepseek").api_key, "sk-local-private-key-111")
            self.assertEqual(ctx_sup.connection_hub.require_profile("conn-local-dws").password, "LocalOwnPassword!11")
            self.assertEqual(ctx_sup.connection_hub.require_profile("conn-mate-mysql").password, "")

            # --- 策略 B: newer（按 updated_at 保留较新） ---
            _, client_new, ctx_new = _make_local_env("newer")
            insp_new = client_new.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("dataog_state.db", mate_bytes, "application/octet-stream")},
            ).json()
            res_new = client_new.post(
                "/api/platform/workspaces/merge/execute",
                json={"merge_token": insp_new["merge_token"], "selected_modules": all_mods, "strategy": "newer"},
            ).json()
            self.assertTrue(res_new["ok"])
            with ctx_new.store.module_db("workspace") as wc:
                stds = {r["std_code"]: r["cn_name"] for r in wc.execute("SELECT std_code, cn_name FROM standard_dictionary").fetchall()}
                self.assertEqual(stds["STD_1"], "客户号_同事新版")  # 对方较新 -> 更新
                self.assertEqual(stds["STD_2"], "余额_本地新版")  # 本地较新 -> 保留
                self.assertEqual(stds["STD_3"], "新增币种标准")
                fsb = {
                    (r["connection_id"], r["column_name"]): r["std_code"]
                    for r in wc.execute("SELECT connection_id, column_name, std_code FROM field_standard_bindings").fetchall()
                }
                self.assertEqual(fsb[("conn-local-dws", "col_a")], "STD_3")  # 对方较新
                self.assertEqual(fsb[("conn-local-dws", "col_b")], "STD_2")  # 本地较新
                ord_cols = {
                    r["std_col_name"]: r["source_std_code"]
                    for r in wc.execute("SELECT std_col_name, source_std_code FROM cross_column_bindings WHERE std_table_key='t_orders'").fetchall()
                }
                self.assertEqual(ord_cols["ord_id"], "STD_3")  # t_orders 对方较新，整体替换为对方版本

            # --- 策略 C: overwrite（覆盖冲突，保留本地独有） + 选择性单模块合并 ---
            _, client_ov, ctx_ov = _make_local_env("overwrite")
            insp_ov = client_ov.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("dataog_state.db", mate_bytes, "application/octet-stream")},
            ).json()
            # 仅勾选 standard_dictionary，不勾选 cross_mappings
            res_ov = client_ov.post(
                "/api/platform/workspaces/merge/execute",
                json={
                    "merge_token": insp_ov["merge_token"],
                    "selected_modules": ["standard_dictionary"],
                    "strategy": "overwrite",
                },
            ).json()
            self.assertTrue(res_ov["ok"])
            with ctx_ov.store.module_db("workspace") as wc:
                stds = {r["std_code"]: r["cn_name"] for r in wc.execute("SELECT std_code, cn_name FROM standard_dictionary").fetchall()}
                self.assertEqual(stds["STD_1"], "客户号_同事新版")
                self.assertEqual(stds["STD_2"], "余额_同事旧版")  # 强制覆盖
                self.assertEqual(stds["STD_3"], "新增币种标准")
                # 未勾选的 cross_mappings 必须原封不动
                tbl_keys = {r["std_table_key"] for r in wc.execute("SELECT std_table_key FROM cross_table_bindings").fetchall()}
                self.assertEqual(tbl_keys, {"t_orders", "t_local_only"})

    def test_automatic_lossless_legacy_install_dir_migration_and_idempotency(self) -> None:
        """端到端验证旧版 <exe_dir>/data/ 全量无损拆分迁移至三层分离架构及二次重启幂等性."""
        seed = b"Legacy-Migration-Test-Seed-32B!!"
        pub = ed25519_derive_pubkey(seed)
        valid_code = issue_activation_code_for_seed(
            seed,
            scope=SCOPE_GLOBAL,
            duration_days=90,
            expiry_date=datetime.date.today() + datetime.timedelta(days=89),
        )
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x01" * 40
        jar_bytes = _make_valid_jar_bytes()

        with tempfile.TemporaryDirectory() as tmp:
            # 1. 构造完整的旧版 v2.6.1 <exe_dir>/data/ 目录
            legacy_data_dir = os.path.join(tmp, "old_exe_dir", "data")
            os.makedirs(os.path.join(legacy_data_dir, "assets"), exist_ok=True)
            os.makedirs(os.path.join(legacy_data_dir, "drivers"), exist_ok=True)
            legacy_db = os.path.join(legacy_data_dir, "dataog_state.db")

            old_store = PlatformStateStore(legacy_db)
            old_store.save_meta("license_activation_code", valid_code)
            old_store.save_meta("license_locked", "0")
            asset_fname = "a" * 32 + ".png"
            with open(os.path.join(legacy_data_dir, "assets", asset_fname), "wb") as fh:
                fh.write(png_bytes)
            old_store.save_meta(
                "desktop_appearance_v1",
                {
                    "wallpaper_id": "custom",
                    "wallpaper_fit": "cover",
                    "custom_wallpaper_asset": asset_fname,
                    "brand_icon_asset": "",
                    "app_icons": {},
                },
            )
            jar_fname = "b" * 32 + ".jar"
            with open(os.path.join(legacy_data_dir, "drivers", jar_fname), "wb") as fh:
                fh.write(jar_bytes)
            with contextlib.closing(sqlite3.connect(legacy_db)) as oconn:
                oconn.executescript(
                    f"""
                    CREATE TABLE IF NOT EXISTS connection_profiles (
                        connection_id TEXT PRIMARY KEY, name TEXT NOT NULL, driver_track TEXT DEFAULT 'NATIVE',
                        db_type TEXT DEFAULT 'DWS', host TEXT DEFAULT '', port INTEGER DEFAULT 8000,
                        dbname TEXT DEFAULT '', default_schema TEXT DEFAULT 'public',
                        user TEXT DEFAULT '', password TEXT DEFAULT '', sslmode TEXT DEFAULT 'prefer',
                        auth_mode TEXT DEFAULT 'LDAP', sqlite_path TEXT DEFAULT '',
                        jdbc_driver_id TEXT DEFAULT '', jdbc_url TEXT DEFAULT '',
                        jdbc_driver_class TEXT DEFAULT '', extra_props_json TEXT DEFAULT '{{}}',
                        standard_format TEXT DEFAULT 'AUTO', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
                    );
                    INSERT INTO connection_profiles (
                        connection_id, name, db_type, host, port, dbname, user, password, standard_format
                    ) VALUES ('conn-old-1', '旧版生产DWS', 'DWS', '10.1.2.3', 8000, 'old_dw', 'old_u', 'OldSecretPwd!22', 'type_dws');
                    INSERT INTO llm_providers (provider_id, name, provider_type, enabled, base_url, api_key, default_model, is_default)
                    VALUES ('deepseek', 'DeepSeek', 'DEEPSEEK', 1, 'https://api.deepseek.com', 'sk-old-migrated-key', 'deepseek-chat', 1);
                    INSERT INTO jdbc_drivers (driver_id, name, file_name, stored_rel_path, sha256, file_size)
                    VALUES ('drv-1', '旧达梦驱动', 'dm.jar', '{jar_fname}', 'deadbeef', {len(jar_bytes)});
                    INSERT INTO standard_dictionary (std_code, cn_name, en_abbr, type_dws)
                    VALUES ('STD_OLD_1', '旧版标准字段', 'old_std_col', 'VARCHAR(64)');
                    INSERT INTO field_standard_bindings (connection_id, schema_name, table_name, column_name, col_key, std_code)
                    VALUES ('conn-old-1', 'public', 't_old', 'old_std_col', 'public::t_old::old_std_col', 'STD_OLD_1');
                    INSERT INTO cross_table_bindings (std_table_key, std_table_name, source_schema, source_table, target_schema, target_table, updated_at)
                    VALUES ('t_old', 't_old', 'ods', 't_old', 'dwd', 't_old', '2026-11-10 10:00:00');
                    INSERT INTO migration_items (row_index, schema_name, table_name, old_col_name, new_col_name, new_col_type, updated_at)
                    VALUES (1, 'public', 't_old', 'old_c', 'new_c', 'VARCHAR(64)', '2026-11-10 10:00:00');
                    CREATE TABLE IF NOT EXISTS explorer_sessions (
                        session_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', connection_id TEXT NOT NULL DEFAULT '',
                        schema_name TEXT NOT NULL DEFAULT '', focus_tables_json TEXT NOT NULL DEFAULT '[]',
                        provider_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', turn_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO explorer_sessions (session_id, title, connection_id) VALUES ('exp-old-1', '旧版探查会话', 'conn-old-1');
                    """
                )
                oconn.commit()

            with open(os.path.join(legacy_data_dir, ".sys_clock_anchor"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": 1700000000, "sig": "legacy_sig"}))

            # 2. 首次以三层分离模式启动，自动无损迁移
            new_priv_root = os.path.join(tmp, "v27_private_vault")
            ctx = PlatformContext(
                private_root=new_priv_root,
                legacy_data_dir=legacy_data_dir,
                verifier_pubkey=pub,
            )
            self.assertTrue(ctx.store.is_split_storage)
            # 私有数据均已无损迁移至私有库
            self.assertTrue(ctx.license_hub.get_auth_status()["authorized"])
            self.assertEqual(ctx.store.get_llm_provider("deepseek").api_key, "sk-old-migrated-key")
            self.assertEqual(ctx.asset_store.get_public_appearance()["wallpaper_id"], "custom")
            self.assertIsNotNone(ctx.asset_store.read_asset_bytes(asset_fname))
            self.assertEqual(len(ctx.store.list_jdbc_drivers()), 1)
            self.assertTrue(os.path.isfile(os.path.join(ctx.driver_manager.drivers_dir, jar_fname)))
            exp_svc = DataExplorerService(ctx)
            self.assertEqual(len(exp_svc.list_sessions()), 1)
            self.assertEqual(exp_svc.list_sessions()[0]["title"], "旧版探查会话")

            # 连接档案拆分为无密码拓扑 + 私有凭据
            migrated_prof = ctx.connection_hub.require_profile("conn-old-1")
            self.assertEqual(migrated_prof.user, "old_u")
            self.assertEqual(migrated_prof.password, "OldSecretPwd!22")
            self.assertEqual(migrated_prof.standard_format, "type_dws")

            # 业务数据均已无损迁移至默认共享工作区，且工作区不含密码/密钥
            ws_summary = ctx.workspace_hub.get_active_workspace_summary()
            self.assertEqual(ws_summary["stats"]["standards_count"], 1)
            self.assertEqual(ws_summary["stats"]["field_bindings_count"], 1)
            self.assertEqual(ws_summary["stats"]["cross_tables_count"], 1)
            self.assertEqual(ws_summary["stats"]["migration_items_count"], 1)
            with open(ctx.store.workspace_db_path, "rb") as wfh:
                ws_raw = wfh.read()
            self.assertNotIn(b"OldSecretPwd!22", ws_raw)
            self.assertNotIn(b"sk-old-migrated-key", ws_raw)

            # 3. 修改新版本数据后再次重启，验证迁移幂等绝不覆盖新版本改动
            ctx.standard_catalog.upsert_item(
                {"std_code": "STD_OLD_1", "cn_name": "v2.7新版修改名", "en_abbr": "old_std_col", "type_dws": "VARCHAR(128)"}
            )
            ctx_reopen = PlatformContext(
                private_root=new_priv_root,
                legacy_data_dir=legacy_data_dir,
                verifier_pubkey=pub,
            )
            item_after = ctx_reopen.standard_catalog.get_item("STD_OLD_1")
            self.assertIsNotNone(item_after)
            assert item_after is not None
            self.assertEqual(item_after["cn_name"], "v2.7新版修改名")

    def test_regressions_ghost_conn_cross_ws_creds_ro_wal_and_path_redaction(self) -> None:
        """回归验证：重启零幽灵连接、跨工作区删连接保凭据、只读目录 WAL 迁移、错误零路径泄露与单库兼容重启恢复."""
        with tempfile.TemporaryDirectory() as tmp:
            # 1. StandardMigrator bind_connection + 重启不产生幽灵 legacy-dws-workbench 连接，切至空白工作区清空引擎连接态
            priv = os.path.join(tmp, "vault_ghost")
            app = create_app(private_root=priv)
            client = TestClient(app)
            ctx: PlatformContext = app.state.platform_ctx
            ctx.connection_hub.save_profile(
                {
                    "connection_id": "conn-real-dws",
                    "name": "生产DWS",
                    "db_type": "DWS",
                    "host": "10.1.1.1",
                    "port": 8000,
                    "dbname": "dw_prod",
                    "user": "alice",
                    "password": "AliceSecret!99",
                }
            )
            mig_svc = StandardMigratorService(ctx)
            mig_svc.bind_connection("conn-real-dws", auto_sync=False)
            self.assertEqual([c["connection_id"] for c in ctx.connection_hub.list_profiles()], ["conn-real-dws"])

            # 克隆工作区并在克隆区内删除 conn-real-dws：原工作区个人密码必须完好无损！
            clone_res = client.post(
                "/api/platform/workspaces",
                json={"name": "克隆沙箱", "clone_from_workspace_id": "ws-default", "switch_immediately": True},
            ).json()
            self.assertTrue(clone_res["ok"])
            self.assertTrue(ctx.connection_hub.delete_profile("conn-real-dws"))
            self.assertEqual(ctx.connection_hub.list_profiles(), [])
            # 此时在无连接的空白工作区获取 migrator 状态，引擎不得残留旧工作区连接
            mig_blank = StandardMigratorService(ctx)
            mig_blank.get_workspace_state()
            self.assertFalse(mig_blank.engine.is_configured())

            # 切回默认工作区并重启：conn-real-dws 个人凭据仍为 BOUND，且绝无幽灵 legacy-dws-workbench
            client.post("/api/platform/workspaces/ws-default/switch")
            ctx_reopen = PlatformContext(private_root=priv)
            reopen_ids = [c["connection_id"] for c in ctx_reopen.connection_hub.list_profiles()]
            self.assertEqual(reopen_ids, ["conn-real-dws"])
            reopen_prof = ctx_reopen.connection_hub.require_profile("conn-real-dws")
            self.assertEqual(reopen_prof.password, "AliceSecret!99")
            self.assertEqual(reopen_prof.credential_status, "BOUND")

            # 2. 错误响应脱敏：底层 OSError / sqlite3.DatabaseError 绝不泄露 private_root
            with mock.patch.object(
                ctx.workspace_hub,
                "create_workspace",
                side_effect=OSError(f"Permission denied: '{priv}/ws_store/s_123.dat'"),
            ):
                err_resp = client.post("/api/platform/workspaces", json={"name": "触发OS异常"})
                self.assertEqual(err_resp.status_code, 400)
                self.assertNotIn(priv, err_resp.text)
                self.assertNotIn("ws_store", err_resp.text)

            with mock.patch.object(
                ctx.workspace_hub,
                "switch_workspace",
                side_effect=ValueError(f"boom at {ctx.store.private_db_path}"),
            ):
                err_resp2 = client.post("/api/platform/workspaces/ws-default/switch")
                self.assertEqual(err_resp2.status_code, 400)
                self.assertNotIn(priv, err_resp2.text)
                self.assertNotIn(os.path.basename(ctx.store.private_db_path), err_resp2.text)

            # 3. 只读旧版 <exe_dir>/data/ 且含未 checkpoint 的 -wal（无 -shm）仍能 100% 无损迁移
            if os.name != "nt":
                live_src_dir = os.path.join(tmp, "wal_live_src")
                os.makedirs(live_src_dir)
                ldb = os.path.join(live_src_dir, "dataog_state.db")
                c_live = sqlite3.connect(ldb)
                c_live.execute("PRAGMA journal_mode = WAL;")
                c_live.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                c_live.execute("INSERT INTO app_meta VALUES (?, ?)", ("custom_wal_key", json.dumps("from_uncheckpointed_wal")))
                c_live.commit()
                ro_legacy = os.path.join(tmp, "ro_legacy_data")
                os.makedirs(ro_legacy)
                for fn in ("dataog_state.db", "dataog_state.db-wal"):
                    src_f = os.path.join(live_src_dir, fn)
                    if os.path.isfile(src_f):
                        shutil.copy2(src_f, os.path.join(ro_legacy, fn))
                c_live.close()
                for fn in os.listdir(ro_legacy):
                    os.chmod(os.path.join(ro_legacy, fn), 0o444)
                os.chmod(ro_legacy, 0o555)
                try:
                    ctx_ro = PlatformContext(
                        private_root=os.path.join(tmp, "vault_from_ro"),
                        legacy_data_dir=ro_legacy,
                    )
                    self.assertEqual(ctx_ro.store.get_meta("custom_wal_key"), "from_uncheckpointed_wal")
                    self.assertTrue(ctx_ro.store.get_meta("legacy_v27_migrated"))
                finally:
                    os.chmod(ro_legacy, 0o755)
                    for fn in os.listdir(ro_legacy):
                        os.chmod(os.path.join(ro_legacy, fn), 0o644)

            # 4. 单库兼容模式创建并切换至非默认工作区后，重启必须正确恢复该活动工作区
            compat_db = os.path.join(tmp, "compat_dir", "dataog_state.db")
            ctx_c1 = PlatformContext(db_path=compat_db)
            ws2 = ctx_c1.workspace_hub.create_workspace(name="兼容模式二号区", switch_immediately=True)
            ctx_c1.standard_catalog.upsert_item(
                {"std_code": "STD_COMPAT_2", "cn_name": "二号区字段", "en_abbr": "c2_col", "type_dws": "VARCHAR(16)"}
            )
            ctx_c2 = PlatformContext(db_path=compat_db)
            self.assertEqual(ctx_c2.workspace_hub.get_active_workspace_summary()["workspace_id"], ws2["workspace_id"])
            self.assertIsNotNone(ctx_c2.standard_catalog.get_item("STD_COMPAT_2"))

    def test_adversarial_merge_edge_cases_and_selective_meta_isolation(self) -> None:
        """对抗边界验证：勾选单模块元信息隔离、混合大小写表键/元数据冲突、newer 防回灌、空工作区导入合并."""
        with tempfile.TemporaryDirectory() as tmp:
            # 1. 导出一个完全空白的 0 行 .dataog.db 并测试导入与合并预检
            empty_app = create_app(private_root=os.path.join(tmp, "empty_src"))
            empty_client = TestClient(empty_app)
            empty_bytes = empty_client.get("/api/platform/workspaces/export").content

            # 2. 构造含混合大小写表键、缺失单侧、空 std_code、元信息与大小写元数据差异的同事来源工作区
            src_app = create_app(private_root=os.path.join(tmp, "adv_src"))
            src_client = TestClient(src_app)
            src_ctx: PlatformContext = src_app.state.platform_ctx
            src_ctx.connection_hub.save_profile(
                {
                    "connection_id": "conn-src-1",
                    "name": "源库DWS",
                    "db_type": "DWS",
                    "host": "10.9.9.9",
                    "port": 8000,
                    "dbname": "adv_dw",
                    "user": "u1",
                    "password": "p1",
                }
            )
            with src_ctx.store.module_db("workspace") as sc:
                sc.execute(
                    "INSERT INTO workspace_meta (key, value) VALUES (?, ?), (?, ?), (?, ?)",
                    (
                        "cross_standard_filename",
                        json.dumps("teammate_cross.xlsx"),
                        "migrator_connection_id",
                        json.dumps("conn-src-1"),
                        "std_dict_filename",
                        json.dumps("teammate_std.xlsx"),
                    ),
                )
                sc.execute(
                    "INSERT INTO standard_dictionary (std_code, cn_name, en_abbr, type_dws, updated_at) "
                    "VALUES (' STD_TRIM ', '去空格标准', 'trim_col', 'VARCHAR(32)', '2026-11-15 10:00:00')"
                )
                sc.execute(
                    "INSERT INTO field_standard_bindings (connection_id, schema_name, table_name, column_name, col_key, std_code, updated_at) "
                    "VALUES ('conn-src-1', 'public', 't_a', 'col_keep', 'public::t_a::col_keep', '', '2026-11-20 10:00:00')"
                )
                sc.execute(
                    "INSERT INTO cross_table_bindings (std_table_key, std_schema_name, std_table_name, source_schema, source_table, target_schema, target_table, updated_at) VALUES "
                    "('Public.T_Mixed', 'public', 'T_Mixed', 'ods', 't_mixed_src', 'dwd', 't_mixed_tgt', '2026-11-15 10:00:00'), "
                    "('t_newer_local', 'public', 't_newer_local', 'ods', 't_nl', 'dwd', 't_nl', '2026-11-10 10:00:00')"
                )
                sc.execute(
                    "INSERT INTO cross_column_bindings (std_table_key, row_index, std_col_name, bound_source_col, bound_target_col, source_std_code, target_std_code, updated_at) VALUES "
                    "('Public.T_Mixed', 1, 'id_col', 'id_col', 'id_col', 'STD_NEW', 'STD_NEW', '2026-11-15 10:00:00'), "
                    "('t_newer_local', 1, 'deleted_in_local', 'deleted_in_local', 'deleted_in_local', 'STD_OLD', 'STD_OLD', '2026-11-10 10:00:00')"
                )
                sc.execute(
                    "INSERT INTO meta_tables (connection_id, schema_name, table_name, columns_cached, refreshed_at) "
                    "VALUES ('conn-src-1', 'PUBLIC', 't_case', 1, '2026-11-18 10:00:00')"
                )
                sc.execute(
                    "INSERT INTO meta_columns (connection_id, schema_name, table_name, ordinal_position, column_name, data_type, refreshed_at) "
                    "VALUES ('conn-src-1', 'PUBLIC', 't_case', 1, 'c1', 'VARCHAR(32)', '2026-11-18 10:00:00')"
                )
            src_bytes = src_client.get("/api/platform/workspaces/export").content

            # A. 验证空白 .dataog.db 导入 + 预检不报错，且仅勾选 standard_dictionary 时绝不污染未勾选模块的 workspace_meta
            dst_app = create_app(private_root=os.path.join(tmp, "adv_dst_sel"))
            dst_client = TestClient(dst_app)
            dst_ctx: PlatformContext = dst_app.state.platform_ctx
            imp_empty = dst_client.post(
                "/api/platform/workspaces/import",
                data={"name": "空工作区导入", "switch_immediately": "false"},
                files={"file": ("empty.dataog.db", empty_bytes, "application/octet-stream")},
            )
            self.assertEqual(imp_empty.status_code, 200)
            insp_empty = dst_client.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("empty.dataog.db", empty_bytes, "application/octet-stream")},
            )
            self.assertEqual(insp_empty.status_code, 200)

            insp_sel = dst_client.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("src.dataog.db", src_bytes, "application/octet-stream")},
            ).json()
            res_sel = dst_client.post(
                "/api/platform/workspaces/merge/execute",
                json={
                    "merge_token": insp_sel["merge_token"],
                    "selected_modules": ["standard_dictionary"],
                    "strategy": "supplement",
                },
            ).json()
            self.assertTrue(res_sel["ok"])
            self.assertIsNotNone(dst_ctx.standard_catalog.get_item("STD_TRIM"))
            self.assertEqual(dst_ctx.store.get_meta("std_dict_filename"), "teammate_std.xlsx")
            # 未勾选 cross_mappings / connection_topology 时不得写入对方跨环境文件名或不存在的连接 ID
            self.assertEqual(dst_ctx.store.get_meta("cross_standard_filename", ""), "")
            self.assertEqual(dst_ctx.store.get_meta("migrator_connection_id", ""), "")

            # B. 验证 mixed-case std_table_key、newer 保留本地较新表、empty std_code 防误清、meta_tables 大小写去重
            dst2_app = create_app(private_root=os.path.join(tmp, "adv_dst_newer"))
            dst2_client = TestClient(dst2_app)
            dst2_ctx: PlatformContext = dst2_app.state.platform_ctx
            dst2_ctx.connection_hub.save_profile(
                {
                    "connection_id": "conn-dst-1",
                    "name": "本机DWS",
                    "db_type": "DWS",
                    "host": "10.9.9.9",
                    "port": 8000,
                    "dbname": "adv_dw",
                    "user": "u2",
                    "password": "p2",
                }
            )
            with dst2_ctx.store.module_db("workspace") as lc:
                lc.execute(
                    "INSERT INTO field_standard_bindings (connection_id, schema_name, table_name, column_name, col_key, std_code, updated_at) "
                    "VALUES ('conn-dst-1', 'public', 't_a', 'col_keep', 'public::t_a::col_keep', 'STD_KEEP', '2026-11-10 10:00:00')"
                )
                lc.execute(
                    "INSERT INTO cross_table_bindings (std_table_key, std_schema_name, std_table_name, source_schema, source_table, target_schema, target_table, updated_at) VALUES "
                    "('Public.T_Mixed', 'public', 'T_Mixed', '', '', 'dwd', 't_mixed_tgt', '2026-11-10 10:00:00'), "
                    "('t_newer_local', 'public', 't_newer_local', 'ods', 't_nl', 'dwd', 't_nl', '2026-11-19 10:00:00')"
                )
                lc.execute(
                    "INSERT INTO cross_column_bindings (std_table_key, row_index, std_col_name, bound_source_col, bound_target_col, source_std_code, target_std_code, updated_at) VALUES "
                    "('Public.T_Mixed', 1, 'id_col', 'id_col', 'id_col', 'STD_OLD', 'STD_OLD', '2026-11-10 10:00:00'), "
                    "('t_newer_local', 1, 'kept_col', 'kept_col', 'kept_col', 'STD_KEEP', 'STD_KEEP', '2026-11-19 10:00:00')"
                )
                lc.execute(
                    "INSERT INTO meta_tables (connection_id, schema_name, table_name, columns_cached, refreshed_at) "
                    "VALUES ('conn-dst-1', 'public', 'T_Case', 1, '2026-11-10 10:00:00')"
                )

            insp2 = dst2_client.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("src.dataog.db", src_bytes, "application/octet-stream")},
            ).json()
            res2 = dst2_client.post(
                "/api/platform/workspaces/merge/execute",
                json={
                    "merge_token": insp2["merge_token"],
                    "selected_modules": ["field_standard_bindings", "cross_mappings", "metadata_cache"],
                    "strategy": "newer",
                },
            ).json()
            self.assertTrue(res2["ok"])
            with dst2_ctx.store.module_db("workspace") as lc:
                # 1) newer 模式不被对方空 std_code 抹掉已有绑定
                kept_std = lc.execute(
                    "SELECT std_code FROM field_standard_bindings WHERE connection_id='conn-dst-1' AND column_name='col_keep'"
                ).fetchone()["std_code"]
                self.assertEqual(kept_std, "STD_KEEP")
                # 2) Public.T_Mixed 混合大小写正确更新且无重复列
                mixed_row = lc.execute(
                    "SELECT source_table FROM cross_table_bindings WHERE std_table_key='Public.T_Mixed'"
                ).fetchone()
                self.assertEqual(mixed_row["source_table"], "t_mixed_src")
                mixed_cols = lc.execute(
                    "SELECT std_col_name, source_std_code FROM cross_column_bindings WHERE LOWER(std_table_key)='public.t_mixed'"
                ).fetchall()
                self.assertEqual(len(mixed_cols), 1)
                self.assertEqual(mixed_cols[0]["source_std_code"], "STD_NEW")
                # 3) newer 策略保留本地较新表，不将本地已删除的旧列重新塞回
                nl_cols = [
                    r["std_col_name"]
                    for r in lc.execute(
                        "SELECT std_col_name FROM cross_column_bindings WHERE std_table_key='t_newer_local'"
                    ).fetchall()
                ]
                self.assertEqual(nl_cols, ["kept_col"])
                # 4) meta_tables 大小写碰撞不产生重复行
                mt_rows = lc.execute(
                    "SELECT schema_name, table_name FROM meta_tables WHERE connection_id='conn-dst-1'"
                ).fetchall()
                self.assertEqual(len(mt_rows), 1)

            # C. 验证 supplement 模式对单侧缺失的 cross_table_bindings 自动补齐 source_table
            dst3_app = create_app(private_root=os.path.join(tmp, "adv_dst_sup"))
            dst3_client = TestClient(dst3_app)
            dst3_ctx: PlatformContext = dst3_app.state.platform_ctx
            with dst3_ctx.store.module_db("workspace") as lc:
                lc.execute(
                    "INSERT INTO cross_table_bindings (std_table_key, std_schema_name, std_table_name, source_schema, source_table, target_schema, target_table, updated_at) "
                    "VALUES ('Public.T_Mixed', 'public', 'T_Mixed', '', '', 'dwd', 't_mixed_tgt', '2026-11-10 10:00:00')"
                )
            insp3 = dst3_client.post(
                "/api/platform/workspaces/merge/inspect",
                files={"file": ("src.dataog.db", src_bytes, "application/octet-stream")},
            ).json()
            res3 = dst3_client.post(
                "/api/platform/workspaces/merge/execute",
                json={
                    "merge_token": insp3["merge_token"],
                    "selected_modules": ["cross_mappings"],
                    "strategy": "supplement",
                },
            ).json()
            self.assertTrue(res3["ok"])
            with dst3_ctx.store.module_db("workspace") as lc:
                mrow = lc.execute(
                    "SELECT source_schema, source_table, target_table FROM cross_table_bindings WHERE std_table_key='Public.T_Mixed'"
                ).fetchone()
                self.assertEqual(mrow["source_schema"], "ods")
                self.assertEqual(mrow["source_table"], "t_mixed_src")
                self.assertEqual(mrow["target_table"], "t_mixed_tgt")

    def test_frontend_workspace_and_settings_dom_interaction_via_node(self) -> None:
        """通过 Node.js 真实执行 ES Modules 验证 WorkspaceView、SettingsView、ConnectionsView 与三步合并向导交互契约."""
        if getattr(sys, "frozen", False) or not shutil.which("node"):
            return
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = r"""
import path from "node:path";
import { pathToFileURL } from "node:url";

class MockNode {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.id = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.open = false;
    this.returnValue = "";
    this.scrollTop = 0;
    this._text = "";
  }
  get textContent() {
    if (this.children.length === 0) return this._text;
    return this._text + this.children.map((c) => c.textContent || "").join("");
  }
  set textContent(v) {
    this._text = String(v ?? "");
    this.children = [];
  }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...nodes) { this.children = []; this._text = ""; for (const n of nodes) if (n) this.children.push(n); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === "id") this.id = String(v);
    if (k === "placeholder") this.placeholder = String(v);
    if (k === "type") this.type = String(v);
  }
  remove() {}
  getAttribute(k) { return this.attributes.get(k) ?? null; }
  addEventListener(ev, fn) {
    if (!this.listeners.has(ev)) this.listeners.set(ev, []);
    this.listeners.get(ev).push(fn);
  }
  removeEventListener(ev, fn) {
    const arr = this.listeners.get(ev) || [];
    this.listeners.set(ev, arr.filter((f) => f !== fn));
  }
  dispatch(ev, payload = {}) {
    const arr = this.listeners.get(ev) || [];
    for (const fn of arr) fn({ target: this, currentTarget: this, preventDefault() {}, ...payload });
  }
  showModal() { this.open = true; }
  close(v = "") { this.open = false; this.returnValue = v; this.dispatch("close"); }
  focus() {}
  select() {}
  click() { this.dispatch("click"); }
}

const byId = new Map();
for (const id of ["global-modal", "global-modal-title", "global-modal-body", "global-modal-footer", "global-modal-close", "toast-root"]) {
  const n = new MockNode(id === "global-modal" ? "dialog" : "div");
  n.id = id;
  byId.set(id, n);
}

globalThis.Node = MockNode;
globalThis.HTMLDialogElement = class HTMLDialogElement {};
globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class CustomEvent { constructor(t, d) { this.type = t; this.detail = d?.detail; } };
globalThis.document = {
  body: new MockNode("body"),
  createElement(tag) { return new MockNode(tag); },
  createElementNS(_ns, tag) { return new MockNode(tag); },
  createTextNode(t) { const n = new MockNode("#text"); n._text = String(t); return n; },
  getElementById(id) { return byId.get(id) || null; },
};

const fetchCalls = [];
globalThis.fetch = async (url, opts = {}) => {
  fetchCalls.push({ url, method: opts.method || "GET", body: opts.body });
  if (url === "/api/platform/workspaces") {
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      async json() {
        return {
          ok: true,
          active_workspace: { workspace_id: "ws-default", name: "默认工作区", is_active: true, is_default: true, stats: { standards_count: 5, field_bindings_count: 3, cross_tables_count: 2, migration_items_count: 1, connections_count: 1, meta_tables_count: 4 } },
          workspaces: [
            { workspace_id: "ws-default", name: "默认工作区", is_active: true, is_default: true, size_bytes: 4096, stats: { standards_count: 5, field_bindings_count: 3, cross_tables_count: 2, migration_items_count: 1, connections_count: 1, meta_tables_count: 4 } },
          ],
        };
      },
    };
  }
  return { ok: true, status: 200, headers: { get: () => "application/json" }, async json() { return { ok: true }; } };
};

const root = process.argv[1];
const imp = (rel) => import(pathToFileURL(path.join(root, rel)).href);
const { store } = await imp("static/js/core/state.js");
const { SettingsView } = await imp("static/js/views/settings_view.js");
const { openWorkspaceMergeWizard } = await imp("static/js/views/workspace_view.js");

function findNodes(rootNode, pred, acc = []) {
  if (!rootNode) return acc;
  if (pred(rootNode)) acc.push(rootNode);
  for (const c of rootNode.children || []) findNodes(c, pred, acc);
  return acc;
}

// 1. SettingsView mount with section='workspaces' triggers workspaceApi.list() refresh
const sv = new SettingsView();
const host = new MockNode("div");
sv.mount(host, { section: "workspaces" });
await new Promise((r) => setTimeout(r, 20));
if (!fetchCalls.some((c) => c.url === "/api/platform/workspaces")) {
  throw new Error("Expected SettingsView.mount({section:'workspaces'}) to call /api/platform/workspaces");
}

// 2. ConnectionsView credential placeholders & resetToNew on workspace_switched
sv.connectionsPane.loadProfile({
  connection_id: "conn-1",
  name: "同事DWS",
  db_type: "DWS",
  driver_track: "NATIVE",
  host: "10.0.0.1",
  port: 8000,
  dbname: "dw",
  has_saved_password: false,
  credential_status: "MISSING",
});
let pwdInputs = findNodes(sv.connectionsPane.root, (n) => n.tagName === "INPUT" && n.getAttribute("type") === "password");
if (pwdInputs.length !== 1 || !String(pwdInputs[0].placeholder || "").includes("请输入您的个人数据库密码")) {
  throw new Error("Expected MISSING credential password placeholder, got: " + pwdInputs[0]?.placeholder);
}

sv.connectionsPane.loadProfile({
  connection_id: "conn-2",
  name: "自动匹配DWS",
  db_type: "DWS",
  driver_track: "NATIVE",
  host: "10.0.0.2",
  port: 8000,
  dbname: "dw",
  has_saved_password: true,
  credential_status: "AUTO_MATCHED",
});
pwdInputs = findNodes(sv.connectionsPane.root, (n) => n.tagName === "INPUT" && n.getAttribute("type") === "password");
if (pwdInputs.length !== 1 || !String(pwdInputs[0].placeholder || "").includes("已自动匹配本机凭据")) {
  throw new Error("Expected AUTO_MATCHED credential password placeholder, got: " + pwdInputs[0]?.placeholder);
}

// Switching workspace must reset stale ConnectionsView editingId
store.updateWorkspaces(
  { workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} },
  [{ workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} }],
  { brand_name: "Data.O.G", connections: [], catalog_trees: {}, drivers: [], driver_templates: [], llm_providers: [], llm_models: [], logs: [], active_workspace: { workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} }, workspaces: [{ workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} }] }
);
if (sv.connectionsPane.editingId !== "") {
  throw new Error("Expected workspace_switched to reset connectionsPane.editingId, got: " + sv.connectionsPane.editingId);
}

// 3. openWorkspaceMergeWizard full Step 1 -> Step 2 (filter/strategy/checkbox) -> Step 3 execution
const origCreateEl = globalThis.document.createElement.bind(globalThis.document);
globalThis.document.createElement = (tag) => {
  const n = origCreateEl(tag);
  if (String(tag).toLowerCase() === "input") {
    const origClick = n.click.bind(n);
    n.click = () => {
      if (n.type === "file") {
        n.files = [{ name: "teammate.dataog.db", size: 2048 }];
        n.dispatch("change");
        return;
      }
      origClick();
    };
  }
  return n;
};
globalThis.FormData = class FormData {
  constructor() { this.entries = []; }
  append(k, v) { this.entries.push([k, v]); }
};
const origFetch = globalThis.fetch;
globalThis.fetch = async (url, opts = {}) => {
  fetchCalls.push({ url, method: opts.method || "GET", body: opts.body });
  if (url === "/api/platform/workspaces/merge/inspect") {
    return {
      ok: true, status: 200, headers: { get: () => "application/json" },
      async json() {
        return {
          ok: true,
          merge_token: "a".repeat(32),
          source_info: { filename: "teammate.dataog.db", source_kind: "WORKSPACE", source_kind_label: "团队共享工作区 (.dataog.db)", file_size: 2048 },
          modules: {
            standard_dictionary: { source_count: 3, local_count: 1, new_count: 2, conflict_count: 1, same_count: 0 },
            field_standard_bindings: { source_count: 0, local_count: 0, new_count: 0, conflict_count: 0, same_count: 0 },
            cross_mappings: { source_tables_count: 2, local_tables_count: 1, new_count: 1, conflict_count: 1, same_count: 0 },
            migration_items: { source_count: 0, local_count: 0, new_count: 0, conflict_count: 0, same_count: 0 },
            connection_topology: { source_count: 1, local_count: 1, new_count: 0, conflict_count: 1, same_count: 1, auto_cred_matched_count: 0, needs_cred_count: 0 },
            metadata_cache: { source_count: 0, local_count: 0, new_count: 0, conflict_count: 0, same_count: 0 },
          },
          table_diffs: [
            { std_table_key: "t_conflict", source_table_ref: "ods.t_c", target_table_ref: "dwd.t_c", status: "CONFLICT", source_col_count: 4, local_col_count: 3, added_cols_count: 1, changed_cols_count: 1, source_is_newer: true },
            { std_table_key: "t_new", source_table_ref: "ods.t_n", target_table_ref: "dwd.t_n", status: "NEW", source_col_count: 2, local_col_count: 0, added_cols_count: 2, changed_cols_count: 0, source_is_newer: true },
          ],
          table_diffs_total: 2,
          connections_preview: [
            { source_connection_id: "c1", mapped_connection_id: "c1", name: "生产DWS", db_type: "DWS", endpoint: "10.0.0.1:8000/dw", action: "MATCH_ID", credential_match: "EXISTING" },
          ],
        };
      },
    };
  }
  if (url === "/api/platform/workspaces/merge/execute") {
    const parsed = JSON.parse(opts.body || "{}");
    if (parsed.strategy !== "newer") throw new Error("Expected selected strategy 'newer', got: " + parsed.strategy);
    return {
      ok: true, status: 200, headers: { get: () => "application/json" },
      async json() {
        return {
          ok: true,
          strategy: parsed.strategy,
          selected_modules: parsed.selected_modules,
          summary: { added: 3, updated: 1, skipped: 0 },
          details: { standard_dictionary: { added: 2, updated: 1, skipped: 0 }, cross_mappings: { added: 1, updated: 0, skipped: 0 } },
          active_workspace: { workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} },
          workspaces: [{ workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} }],
          bootstrap: { brand_name: "Data.O.G", connections: [], catalog_trees: {}, drivers: [], driver_templates: [], llm_providers: [], llm_models: [], logs: [], active_workspace: { workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} }, workspaces: [{ workspace_id: "ws-2", name: "二号工作区", is_active: true, stats: {} }] },
          message: "合并完成",
        };
      },
    };
  }
  return origFetch(url, opts);
};

openWorkspaceMergeWizard();
const modalEl = byId.get("global-modal");
const bodyEl = byId.get("global-modal-body");
const footerEl = byId.get("global-modal-footer");
if (!modalEl.open) throw new Error("Expected merge wizard modal to open");
if (!bodyEl.textContent.includes("白名单业务表隔离解析")) {
  throw new Error("Expected merge wizard Step 1 security banner");
}
// Trigger Step 1 file pick -> Step 2
const pickBtn = findNodes(footerEl, (n) => n.tagName === "BUTTON" && n.textContent.includes("选择文件"))[0];
pickBtn.click();
await new Promise((r) => setTimeout(r, 30));
if (!bodyEl.textContent.includes("teammate.dataog.db") || !bodyEl.textContent.includes("t_conflict")) {
  throw new Error("Expected Step 2 diff preview with teammate.dataog.db and t_conflict");
}
// Select 'newer' strategy and filter table diffs to 'NEW'
const newerBtn = findNodes(bodyEl, (n) => n.tagName === "BUTTON" && n.textContent.includes("按更新时间保留较新记录"))[0];
newerBtn.click();
const filterNewBtn = findNodes(bodyEl, (n) => n.tagName === "BUTTON" && n.textContent.includes("新增 1"))[0];
filterNewBtn.click();
if (!bodyEl.textContent.includes("t_new") || bodyEl.textContent.includes("t_conflict")) {
  throw new Error("Expected NEW table diff filter to show only t_new");
}
// Execute merge -> Step 3
const execBtn = findNodes(footerEl, (n) => n.tagName === "BUTTON" && n.textContent.includes("确认合并"))[0];
execBtn.click();
await new Promise((r) => setTimeout(r, 30));
if (!bodyEl.textContent.includes("增量合并成功：新增 3 项 · 更新 1 项 · 跳过 0 项")) {
  throw new Error("Expected Step 3 merge summary, got: " + bodyEl.textContent);
}
sv.destroy();
console.log("NODE_WORKSPACE_UI_OK");
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script, project_root],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node.js WorkspaceView/SettingsView DOM 交互验证失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("NODE_WORKSPACE_UI_OK", proc.stdout)

        # 守卫无头浏览器视觉审校修复的关键 CSS 布局契约（全局 [hidden]、窄屏菜单栏防折行、弹窗头防挤压）
        css_path = os.path.join(project_root, "static", "css", "workbench.css")
        with open(css_path, "r", encoding="utf-8") as cf:
            css_text = cf.read()
        self.assertIn("[hidden] {\n  display: none !important;\n}", css_text)
        self.assertIn("#btn-menubar-workspace {", css_text)
        self.assertIn("#menubar-workspace-label {", css_text)
        self.assertIn(".ws-diff-col-key {", css_text)
        self.assertIn(".ws-diff-col-ref {", css_text)
        dialog_hdr_idx = css_text.index(".dialog-header {")
        dialog_hdr_block = css_text[dialog_hdr_idx : css_text.index("}", dialog_hdr_idx)]
        self.assertIn("flex-shrink: 0;", dialog_hdr_block)


if __name__ == "__main__":
    unittest.main()
