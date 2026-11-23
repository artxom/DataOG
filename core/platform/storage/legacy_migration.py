"""Data.O.G 旧版单库与安装目录 <exe_dir>/data 无损拆分迁移引擎 (core/platform/storage/legacy_migration.py)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from typing import Any, Dict, List, Optional

from core.platform.models import (
    ConnectionProfile,
    compute_connection_fingerprint,
    sanitize_jdbc_url,
    split_extra_props_secrets,
)
from core.platform.storage.schema import (
    WORKSPACE_META_KEYS,
    init_workspace_schema_on_conn,
)
from core.platform.storage_paths import (
    decode_obfuscated_anchor,
    encode_obfuscated_anchor,
    redact_private_storage_paths,
)

logger = logging.getLogger("dataog")


class _LegacyMigrationMixin:
    """隔离历史版本 (v1.x ~ v2.6.x) 状态库与安装目录数据无损迁移逻辑."""

    # =========================================================================
    # 旧版状态库 (dws_migrator_state.db / connection_profiles / <exe_dir>/data) 无损迁移
    # =========================================================================

    def _migrate_legacy_connection_profiles_in_db(self) -> None:
        """单库兼容打开历史状态库时，将旧 `connection_profiles` 幂等拆分为 `connection_topology` + `connection_credentials`."""
        if self.get_meta("legacy_conn_profiles_split_v27", False):
            return
        with self._lock, self._get_private_conn() as pconn:
            has_old_tbl = pconn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connection_profiles'"
            ).fetchone()
            if not has_old_tbl:
                return
            rows = pconn.execute("SELECT * FROM connection_profiles").fetchall()
            if not rows:
                pconn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("legacy_conn_profiles_split_v27", "true"),
                )
                pconn.commit()
                return

        for r in rows:
            cid = str(r["connection_id"] or "").strip()
            if not cid:
                continue
            existing = self.get_connection_profile(cid)
            if existing is not None:
                continue
            rkeys = set(r.keys())
            try:
                extra_raw = json.loads((r["extra_props_json"] if "extra_props_json" in rkeys else "{}") or "{}")
                if not isinstance(extra_raw, dict):
                    extra_raw = {}
            except Exception:
                extra_raw = {}
            prof = ConnectionProfile(
                connection_id=cid,
                name=str(r["name"] or cid),
                driver_track=str(r["driver_track"] if "driver_track" in rkeys else "NATIVE") or "NATIVE",
                db_type=str(r["db_type"] if "db_type" in rkeys else "DWS") or "DWS",
                host=str(r["host"] if "host" in rkeys else "") or "",
                port=int((r["port"] if "port" in rkeys else 0) or 0),
                dbname=str(r["dbname"] if "dbname" in rkeys else "") or "",
                default_schema=str(r["default_schema"] if "default_schema" in rkeys else "public") or "public",
                user=str(r["user"] if "user" in rkeys else "") or "",
                password=str(r["password"] if "password" in rkeys else "") or "",
                sslmode=str(r["sslmode"] if "sslmode" in rkeys else "prefer") or "prefer",
                auth_mode=str(r["auth_mode"] if "auth_mode" in rkeys else "LDAP") or "LDAP",
                sqlite_path=str(r["sqlite_path"] if "sqlite_path" in rkeys else "") or "",
                jdbc_driver_id=str(r["jdbc_driver_id"] if "jdbc_driver_id" in rkeys else "") or "",
                jdbc_url=str(r["jdbc_url"] if "jdbc_url" in rkeys else "") or "",
                jdbc_driver_class=str(r["jdbc_driver_class"] if "jdbc_driver_class" in rkeys else "") or "",
                extra_props=extra_raw,
                standard_format=str((r["standard_format"] if "standard_format" in rkeys else "AUTO") or "AUTO"),
                created_at=str(r["created_at"] if "created_at" in rkeys else "") or "",
                updated_at=str(r["updated_at"] if "updated_at" in rkeys else "") or "",
            )
            self.upsert_connection_profile(prof)

        self.save_meta("legacy_conn_profiles_split_v27", True)

    def _migrate_legacy_db_file_if_needed(self) -> None:
        """若当前状态库为 dataog_state.db 且同目录存在旧版 dws_migrator_state.db，自动无损迁移全部历史数据."""
        if os.path.basename(self.private_db_path) != "dataog_state.db":
            return
        legacy_path = os.path.join(os.path.dirname(self.private_db_path), "dws_migrator_state.db")
        if not os.path.isfile(legacy_path):
            return
        if self.get_meta("legacy_db_migrated_v2", False):
            return

        with self._lock, self._get_both_conns() as (p_conn, w_conn):
            has_new_data = (
                w_conn.execute("SELECT COUNT(*) FROM migration_items").fetchone()[0] > 0
                or w_conn.execute("SELECT COUNT(*) FROM cross_table_bindings").fetchone()[0] > 0
                or w_conn.execute("SELECT COUNT(*) FROM connection_topology").fetchone()[0] > 0
            )
            if has_new_data:
                p_conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("legacy_db_migrated_v2", "true"),
                )
                p_conn.commit()
                return

            src_conn = sqlite3.connect(legacy_path, timeout=10.0)
            src_conn.row_factory = sqlite3.Row
            try:
                src_tables = {
                    r["name"]
                    for r in src_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                private_tables = ("app_meta", "execution_logs")
                workspace_tables = (
                    "migration_items",
                    "cross_table_bindings",
                    "cross_column_bindings",
                )
                for tbl in private_tables:
                    if tbl not in src_tables:
                        continue
                    self._copy_common_rows(src_conn, p_conn, tbl, tbl)
                for tbl in workspace_tables:
                    if tbl not in src_tables:
                        continue
                    self._copy_common_rows(src_conn, w_conn, tbl, tbl)
                if "app_meta" in src_tables:
                    for r in src_conn.execute("SELECT key, value FROM app_meta").fetchall():
                        if r["key"] in WORKSPACE_META_KEYS:
                            w_conn.execute(
                                "INSERT INTO workspace_meta (key, value) VALUES (?, ?) "
                                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                (r["key"], r["value"]),
                            )
                p_conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("legacy_db_migrated_v2", "true"),
                )
                w_conn.commit()
                p_conn.commit()
                logger.info("已自动从历史状态库无损迁移数据")
            except Exception as exc:
                logger.warning(
                    "迁移旧版状态库时遇到非致命异常: %s",
                    redact_private_storage_paths(str(exc), store=self),
                )
            finally:
                src_conn.close()

    @staticmethod
    def _copy_common_rows(
        src_conn: sqlite3.Connection,
        dst_conn: sqlite3.Connection,
        src_tbl: str,
        dst_tbl: str,
    ) -> int:
        dst_cols = {
            r["name"]
            for r in dst_conn.execute(f"PRAGMA table_info({dst_tbl})").fetchall()
        }
        rows = src_conn.execute(f"SELECT * FROM {src_tbl}").fetchall()
        if not rows:
            return 0
        common_cols = [c for c in rows[0].keys() if c in dst_cols]
        if not common_cols:
            return 0
        placeholders = ", ".join("?" for _ in common_cols)
        col_list_sql = ", ".join(common_cols)
        dst_conn.executemany(
            f"INSERT OR REPLACE INTO {dst_tbl} ({col_list_sql}) VALUES ({placeholders})",
            [[r[c] for c in common_cols] for r in rows],
        )
        return len(rows)

    def migrate_legacy_install_dir(
        self,
        legacy_data_dir: str,
        *,
        assets_dir: str,
        drivers_dir: str,
        clock_anchor_path: Optional[str] = None,
    ) -> bool:
        """从旧版程序目录 `<app_dir>/data/` 无损拆分迁移至三层分离架构（私有库 + 默认共享工作区）.

        严格保证：
        1. 幂等单次执行（通过私有库 `legacy_v27_migrated` 标记，且仅当新库尚未写入数据时执行）；
        2. 绝不删除旧目录原文件（保留安全备份）；
        3. 绝不向日志输出混淆后的私有库真实路径。
        """
        if not legacy_data_dir or not os.path.isdir(legacy_data_dir):
            return False
        if self.get_meta("legacy_v27_migrated", False):
            return False

        cand_dbs = [
            os.path.join(legacy_data_dir, "dataog_state.db"),
            os.path.join(legacy_data_dir, "dws_migrator_state.db"),
        ]
        legacy_db = next((p for p in cand_dbs if os.path.isfile(p)), "")
        if not legacy_db:
            return False
        if os.path.realpath(legacy_db) == os.path.realpath(self.private_db_path):
            return False

        # 若新私有库或工作区已有业务/授权数据，仅置迁移完成标志，绝不覆盖新版本已有进度
        with self._lock, self._get_both_conns() as (p_conn, w_conn):
            has_new_private = (
                p_conn.execute(
                    "SELECT COUNT(*) FROM app_meta WHERE key = 'license_activation_code'"
                ).fetchone()[0]
                > 0
                or p_conn.execute(
                    "SELECT COUNT(*) FROM llm_providers WHERE api_key != ''"
                ).fetchone()[0]
                > 0
                or p_conn.execute("SELECT COUNT(*) FROM connection_credentials").fetchone()[0] > 0
            )
            has_new_ws = (
                w_conn.execute("SELECT COUNT(*) FROM standard_dictionary").fetchone()[0] > 0
                or w_conn.execute("SELECT COUNT(*) FROM field_standard_bindings").fetchone()[0] > 0
                or w_conn.execute("SELECT COUNT(*) FROM cross_table_bindings").fetchone()[0] > 0
                or w_conn.execute("SELECT COUNT(*) FROM migration_items").fetchone()[0] > 0
                or w_conn.execute("SELECT COUNT(*) FROM connection_topology").fetchone()[0] > 0
            )
            if has_new_private or has_new_ws:
                p_conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("legacy_v27_migrated", "true"),
                )
                p_conn.commit()
                return False

            # 将旧版库及其可能未 checkpoint 的 WAL/SHM 旁路文件复制到可写临时目录打开：
            # 使用 copyfile + 0o600 确保即使旧目录/旧文件为只读属性（如 Program Files 0o444），临时副本仍可正常恢复 WAL 并安全清理
            with tempfile.TemporaryDirectory(prefix="rt_mig_") as stage_dir:
                staged_legacy_db = os.path.join(stage_dir, "legacy_src.db")
                shutil.copyfile(legacy_db, staged_legacy_db)
                try:
                    os.chmod(staged_legacy_db, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
                for side_suffix in ("-wal", "-shm"):
                    side_src = legacy_db + side_suffix
                    if os.path.isfile(side_src):
                        try:
                            side_dst = staged_legacy_db + side_suffix
                            shutil.copyfile(side_src, side_dst)
                            try:
                                os.chmod(side_dst, stat.S_IRUSR | stat.S_IWUSR)
                            except OSError:
                                pass
                        except OSError:
                            pass

                src_conn = sqlite3.connect(staged_legacy_db, timeout=15.0)
                src_conn.row_factory = sqlite3.Row
                try:
                    src_tables = {
                        r["name"]
                        for r in src_conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }

                    # 1. 拆分 app_meta -> 私有 app_meta + 工作区 workspace_meta
                    if "app_meta" in src_tables:
                        for r in src_conn.execute("SELECT key, value FROM app_meta").fetchall():
                            k = str(r["key"] or "")
                            v = str(r["value"] or "")
                            if k in WORKSPACE_META_KEYS:
                                w_conn.execute(
                                    "INSERT INTO workspace_meta (key, value) VALUES (?, ?) "
                                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                    (k, v),
                                )
                            else:
                                p_conn.execute(
                                    "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                    (k, v),
                                )
                    if "workspace_meta" in src_tables:
                        for r in src_conn.execute("SELECT key, value FROM workspace_meta").fetchall():
                            k = str(r["key"] or "")
                            v = str(r["value"] or "")
                            if k in WORKSPACE_META_KEYS:
                                w_conn.execute(
                                    "INSERT INTO workspace_meta (key, value) VALUES (?, ?) "
                                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                    (k, v),
                                )

                    # 2. 迁移私有表（AI 密钥、JDBC 驱动索引、执行日志、个人 AI 探查会话）
                    for ptbl in ("llm_providers", "jdbc_drivers", "execution_logs"):
                        if ptbl in src_tables:
                            self._copy_common_rows(src_conn, p_conn, ptbl, ptbl)

                    # 个人 AI 探查表由 DataExplorerService 延迟注册；若旧库存在则先建表并迁移进私有库
                    for exp_tbl in ("explorer_sessions", "explorer_messages"):
                        if exp_tbl in src_tables:
                            ddl_row = src_conn.execute(
                                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                                (exp_tbl,),
                            ).fetchone()
                            if ddl_row and ddl_row["sql"]:
                                safe_ddl = re.sub(
                                    r"(?i)^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?",
                                    "CREATE TABLE IF NOT EXISTS ",
                                    str(ddl_row["sql"]).strip(),
                                    count=1,
                                )
                                p_conn.execute(safe_ddl)
                                self._copy_common_rows(src_conn, p_conn, exp_tbl, exp_tbl)

                    # 3. 拆分旧 connection_profiles -> 无密码 connection_topology (工作区) + 个人 connection_credentials (私有库)
                    now = self._now_str()
                    if "connection_profiles" in src_tables:
                        for r in src_conn.execute("SELECT * FROM connection_profiles").fetchall():
                            rkeys = set(r.keys())
                            cid = str(r["connection_id"] or "").strip()
                            if not cid:
                                continue
                            try:
                                raw_extra = json.loads(
                                    (r["extra_props_json"] if "extra_props_json" in rkeys else "{}") or "{}"
                                )
                                if not isinstance(raw_extra, dict):
                                    raw_extra = {}
                            except Exception:
                                raw_extra = {}
                            pub_extra, sec_extra = split_extra_props_secrets(raw_extra)
                            driver_track = str((r["driver_track"] if "driver_track" in rkeys else "NATIVE") or "NATIVE")
                            db_type = str((r["db_type"] if "db_type" in rkeys else "DWS") or "DWS")
                            host = str((r["host"] if "host" in rkeys else "") or "")
                            port = int((r["port"] if "port" in rkeys else 0) or 0)
                            dbname = str((r["dbname"] if "dbname" in rkeys else "") or "")
                            default_schema = str((r["default_schema"] if "default_schema" in rkeys else "public") or "public")
                            sslmode = str((r["sslmode"] if "sslmode" in rkeys else "prefer") or "prefer")
                            auth_mode = str((r["auth_mode"] if "auth_mode" in rkeys else "LDAP") or "LDAP")
                            sqlite_path = str((r["sqlite_path"] if "sqlite_path" in rkeys else "") or "")
                            jdbc_driver_id = str((r["jdbc_driver_id"] if "jdbc_driver_id" in rkeys else "") or "")
                            raw_jdbc_url = str((r["jdbc_url"] if "jdbc_url" in rkeys else "") or "")
                            jdbc_url, jdbc_user, jdbc_pwd = sanitize_jdbc_url(raw_jdbc_url)
                            jdbc_driver_class = str((r["jdbc_driver_class"] if "jdbc_driver_class" in rkeys else "") or "")
                            std_fmt = str((r["standard_format"] if "standard_format" in rkeys else "AUTO") or "AUTO")
                            created_at = str((r["created_at"] if "created_at" in rkeys else now) or now)
                            updated_at = str((r["updated_at"] if "updated_at" in rkeys else now) or now)
                            user_val = str((r["user"] if "user" in rkeys else "") or "") or jdbc_user
                            pwd_val = str((r["password"] if "password" in rkeys else "") or "") or jdbc_pwd

                            w_conn.execute(
                                """
                                INSERT OR REPLACE INTO connection_topology (
                                    connection_id, name, driver_track, db_type,
                                    host, port, dbname, default_schema,
                                    sslmode, auth_mode, sqlite_path,
                                    jdbc_driver_id, jdbc_url, jdbc_driver_class,
                                    extra_props_json, standard_format, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    cid,
                                    str(r["name"] or cid),
                                    driver_track,
                                    db_type,
                                    host,
                                    port,
                                    dbname,
                                    default_schema,
                                    sslmode,
                                    auth_mode,
                                    sqlite_path,
                                    jdbc_driver_id,
                                    jdbc_url,
                                    jdbc_driver_class,
                                    json.dumps(pub_extra, ensure_ascii=False),
                                    std_fmt,
                                    created_at,
                                    updated_at,
                                ),
                            )
                            fp = compute_connection_fingerprint(
                                driver_track=driver_track,
                                db_type=db_type,
                                host=host,
                                port=port,
                                dbname=dbname,
                                jdbc_url=jdbc_url,
                                sqlite_path=sqlite_path,
                            )
                            p_conn.execute(
                                """
                                INSERT OR REPLACE INTO connection_credentials (
                                    cred_id, connection_id, endpoint_fingerprint,
                                    db_type, host, port, dbname, jdbc_url,
                                    user, password, sslmode, auth_mode, sqlite_path,
                                    jdbc_driver_id, extra_secrets_json, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    self._credential_row_id(cid, fp),
                                    cid,
                                    fp,
                                    db_type,
                                    host,
                                    port,
                                    dbname,
                                    jdbc_url,
                                    user_val,
                                    pwd_val,
                                    sslmode,
                                    auth_mode,
                                    sqlite_path,
                                    jdbc_driver_id,
                                    json.dumps(sec_extra, ensure_ascii=False),
                                    updated_at,
                                ),
                            )
                    elif "connection_topology" in src_tables:
                        self._copy_common_rows(src_conn, w_conn, "connection_topology", "connection_topology")
                        if "connection_credentials" in src_tables:
                            self._copy_common_rows(src_conn, p_conn, "connection_credentials", "connection_credentials")

                    # 4. 迁移共享工作区全部纯业务表
                    for wtbl in (
                        "standard_dictionary",
                        "field_standard_bindings",
                        "cross_table_bindings",
                        "cross_column_bindings",
                        "migration_items",
                        "meta_schemas",
                        "meta_tables",
                        "meta_columns",
                    ):
                        if wtbl in src_tables:
                            self._copy_common_rows(src_conn, w_conn, wtbl, wtbl)

                    p_conn.execute(
                        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        ("legacy_v27_migrated", "true"),
                    )
                    w_conn.commit()
                    p_conn.commit()
                except Exception as exc:
                    try:
                        w_conn.rollback()
                    except Exception:
                        pass
                    try:
                        p_conn.rollback()
                    except Exception:
                        pass
                    logger.warning(
                        "迁移旧版程序目录数据时出现非致命异常: %s",
                        redact_private_storage_paths(str(exc), store=self, extra_paths=(assets_dir, drivers_dir)),
                    )
                    return False
                finally:
                    src_conn.close()

        # 5. 无损复制旧版上传的壁纸/图标、JDBC 驱动包及单调时钟锚点至私有库目录
        self._copy_dir_files_if_missing(os.path.join(legacy_data_dir, "assets"), assets_dir)
        self._copy_dir_files_if_missing(os.path.join(legacy_data_dir, "drivers"), drivers_dir)
        legacy_anchor = os.path.join(legacy_data_dir, ".sys_clock_anchor")
        eff_anchor = clock_anchor_path or self.clock_anchor_path
        if eff_anchor and os.path.isfile(legacy_anchor):
            try:
                with open(legacy_anchor, "r", encoding="utf-8") as fh:
                    legacy_obj = decode_obfuscated_anchor(fh.read())
                if legacy_obj and float(legacy_obj.get("ts", 0)) > 0:
                    cur_ts = 0.0
                    if os.path.isfile(eff_anchor):
                        with open(eff_anchor, "r", encoding="utf-8") as cfh:
                            cur_obj = decode_obfuscated_anchor(cfh.read()) or {}
                            cur_ts = float(cur_obj.get("ts", 0))
                    if float(legacy_obj.get("ts", 0)) > cur_ts:
                        os.makedirs(os.path.dirname(eff_anchor), exist_ok=True)
                        with open(eff_anchor, "w", encoding="utf-8") as wfh:
                            wfh.write(encode_obfuscated_anchor(legacy_obj))
            except Exception:
                pass

        # 6. 若旧库中仍有 v1.x app_meta 连接键值，同步升级为拓扑 + 凭据
        self._migrate_legacy_connections_if_needed()
        self.add_log(
            "INFO",
            "存储升级",
            "已自动将历史本地数据无损拆分升级至本地私有保险箱与默认共享工作区",
        )
        return True

    @staticmethod
    def _copy_dir_files_if_missing(src_dir: str, dst_dir: str) -> None:
        if not src_dir or not os.path.isdir(src_dir):
            return
        os.makedirs(dst_dir, exist_ok=True)
        try:
            for name in os.listdir(src_dir):
                src_path = os.path.join(src_dir, name)
                dst_path = os.path.join(dst_dir, name)
                if os.path.isfile(src_path) and not os.path.exists(dst_path):
                    try:
                        shutil.copy2(src_path, dst_path)
                    except OSError:
                        pass
        except OSError:
            pass

    def _migrate_legacy_connections_if_needed(self) -> None:
        """自动扫描 app_meta 中的旧版连接配置并无损迁移为全局命名数据源档案."""
        legacy_specs = [
            (
                "dws_conn_saved",
                "legacy-dws-workbench",
                "单库贯标默认数据源 (迁移)",
                "migrator_connection_id",
            ),
            (
                "dws_connection_config",
                "legacy-dws-config",
                "单库贯标数据源 (迁移)",
                "migrator_connection_id",
            ),
            (
                "cross_source_conn",
                "legacy-cross-source",
                "跨环境来源库 (迁移)",
                "cross_source_connection_id",
            ),
            (
                "cross_target_conn",
                "legacy-cross-target",
                "跨环境目标库 (迁移)",
                "cross_target_connection_id",
            ),
        ]
        existing_profiles = self.list_connection_profiles()
        existing_ids = {p.connection_id for p in existing_profiles}
        existing_fps = {p.endpoint_fingerprint() for p in existing_profiles if p.endpoint_fingerprint()}
        now = self._now_str()
        for meta_key, profile_id, default_label, binding_meta_key in legacy_specs:
            raw_cfg = self.get_meta(meta_key, None)
            if not isinstance(raw_cfg, dict):
                continue
            host = str(raw_cfg.get("host") or "").strip()
            if not host:
                continue
            cur_bound = str(self.get_meta(binding_meta_key, "") or "").strip()
            if cur_bound and cur_bound in existing_ids:
                continue
            db_type = str(raw_cfg.get("db_type") or "DWS").strip().upper()
            default_port = (
                10000
                if db_type == "INCEPTOR"
                else (5432 if db_type == "PGSQL" else 8000)
            )
            port = int(raw_cfg.get("port") or default_port)
            dbname = str(raw_cfg.get("dbname") or "").strip()
            cand_fp = compute_connection_fingerprint(
                driver_track="NATIVE",
                db_type=db_type,
                host=host,
                port=port,
                dbname=dbname,
            )
            if profile_id in existing_ids or (cand_fp and cand_fp in existing_fps):
                continue
            profile = ConnectionProfile(
                connection_id=profile_id,
                name=f"{default_label} [{db_type} {host}/{dbname}]" if dbname else f"{default_label} [{db_type} {host}]",
                driver_track="NATIVE",
                db_type=db_type,
                host=host,
                port=port,
                dbname=dbname,
                default_schema=str(raw_cfg.get("schema_name") or raw_cfg.get("default_schema") or ("default" if db_type == "INCEPTOR" else "public")).strip(),
                user=str(raw_cfg.get("user") or "").strip(),
                password=str(raw_cfg.get("password") or ""),
                sslmode=str(raw_cfg.get("sslmode") or "prefer").strip(),
                auth_mode=str(raw_cfg.get("auth_mode") or "LDAP").strip().upper(),
                created_at=now,
                updated_at=now,
            )
            self.upsert_connection_profile(profile)
            existing_ids.add(profile_id)
            if cand_fp:
                existing_fps.add(cand_fp)
            if not self.get_meta(binding_meta_key, ""):
                self.save_meta(binding_meta_key, profile_id)

