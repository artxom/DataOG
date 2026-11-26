"""Data.O.G 平台级核心实体持久化仓库：连接与凭据 / JDBC 驱动 / 元数据缓存 / AI 服务商 (core/platform/storage/platform_repos.py)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from core.platform.models import (
    ColumnMeta,
    ConnectionProfile,
    DriverPackage,
    LLMProviderProfile,
    SchemaMeta,
    TableMeta,
    compute_connection_fingerprint,
    sanitize_jdbc_url,
    split_extra_props_secrets,
)


class _PlatformReposMixin:
    """平台底座共享数据的 CRUD 持久化仓库."""

    # =========================================================================
    # 全局连接档案：工作区无密码拓扑 (connection_topology) × 私有个人凭据库 (connection_credentials)
    # =========================================================================

    @staticmethod
    def _credential_row_id(connection_id: str, endpoint_fingerprint: str) -> str:
        """为 (connection_id, endpoint_fingerprint) 派生独立凭据主键，彻底防止不同工作区同 ID 指向不同库时互相覆盖凭据."""
        cid = (connection_id or "").strip()
        fp = (endpoint_fingerprint or "").strip()
        if fp:
            fp_tag = hashlib.sha256(fp.encode("utf-8")).hexdigest()[:12]
            return f"cid:{cid}|fp:{fp_tag}"
        return f"cid:{cid}"

    @staticmethod
    def _resolve_credential_for_topology(
        cred_by_cid_rows: Dict[str, List[Dict[str, Any]]],
        cred_by_fp: Dict[str, Dict[str, Any]],
        *,
        connection_id: str,
        endpoint_fingerprint: str,
        db_type: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """按严格端点兼容语义解析本机个人凭据，杜绝同 connection_id 指向不同数据库时误绑旧凭据."""
        cid = (connection_id or "").strip()
        fp = (endpoint_fingerprint or "").strip()
        candidates = cred_by_cid_rows.get(cid, [])

        exact_cred: Optional[Dict[str, Any]] = None
        if fp:
            # 1. 优先匹配 (connection_id, endpoint_fingerprint) 完全一致且含有效凭据的记录
            for row in candidates:
                rfp = str(row.get("endpoint_fingerprint") or "").strip()
                if rfp == fp and (row.get("user") or row.get("password")):
                    exact_cred = row
                    break
            # 2. 次选同 connection_id + 同指纹（或历史未记录指纹的兼容记录），绝不匹配指向另一端点的凭据
            if exact_cred is None:
                for row in candidates:
                    rfp = str(row.get("endpoint_fingerprint") or "").strip()
                    if rfp == fp or not rfp:
                        exact_cred = row
                        break
        elif candidates:
            exact_cred = candidates[0]

        fp_cred = cred_by_fp.get(fp) if fp else None
        if (db_type or "").strip().upper() == "SQLITE":
            return (exact_cred or fp_cred), "BOUND"
        if exact_cred and (exact_cred.get("user") or exact_cred.get("password")):
            return exact_cred, "BOUND"
        if fp_cred and (fp_cred.get("user") or fp_cred.get("password")):
            return fp_cred, "AUTO_MATCHED"
        if exact_cred:
            return exact_cred, "MISSING"
        return None, "MISSING"

    def _load_credential_maps(
        self,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, Any]], set[str]]:
        """读取私有库中的个人 DB 凭据映射（按 connection_id 列表与 endpoint_fingerprint 双索引）与本地驱动 ID 集合."""
        by_cid_rows: Dict[str, List[Dict[str, Any]]] = {}
        by_fp: Dict[str, Dict[str, Any]] = {}
        driver_ids: set[str] = set()
        with self._lock, self._get_private_conn() as pconn:
            cred_rows = pconn.execute(
                "SELECT * FROM connection_credentials ORDER BY updated_at DESC"
            ).fetchall()
            for r in cred_rows:
                d = dict(r)
                cid = str(d.get("connection_id") or "").strip()
                if cid:
                    by_cid_rows.setdefault(cid, []).append(d)
                fp = str(d.get("endpoint_fingerprint") or "").strip()
                if fp and fp not in by_fp and (d.get("user") or d.get("password")):
                    by_fp[fp] = d
            for dr in pconn.execute("SELECT driver_id FROM jdbc_drivers").fetchall():
                driver_ids.add(str(dr["driver_id"]))
        return by_cid_rows, by_fp, driver_ids

    @staticmethod
    def _assemble_profile(
        row: sqlite3.Row,
        cred_by_cid_rows: Dict[str, List[Dict[str, Any]]],
        cred_by_fp: Dict[str, Dict[str, Any]],
        local_driver_ids: set[str],
    ) -> ConnectionProfile:
        try:
            extra_props = json.loads(row["extra_props_json"] or "{}")
            if not isinstance(extra_props, dict):
                extra_props = {}
        except Exception:
            extra_props = {}
        row_keys = set(row.keys()) if hasattr(row, "keys") else set()
        raw_std_fmt = (row["standard_format"] if "standard_format" in row_keys else "AUTO") or "AUTO"
        cid = str(row["connection_id"])
        driver_track = str(row["driver_track"] or "NATIVE")
        db_type = str(row["db_type"] or "DWS")
        host = str(row["host"] or "")
        port = int(row["port"] or 0)
        dbname = str(row["dbname"] or "")
        jdbc_url = sanitize_jdbc_url(str(row["jdbc_url"] or ""))[0]
        sqlite_path = str(row["sqlite_path"] or "")
        jdbc_driver_id = str(row["jdbc_driver_id"] or "")

        fp = compute_connection_fingerprint(
            driver_track=driver_track,
            db_type=db_type,
            host=host,
            port=port,
            dbname=dbname,
            jdbc_url=jdbc_url,
            sqlite_path=sqlite_path,
        )

        chosen_cred, cred_status = _PlatformReposMixin._resolve_credential_for_topology(
            cred_by_cid_rows,
            cred_by_fp,
            connection_id=cid,
            endpoint_fingerprint=fp,
            db_type=db_type,
        )

        user_val = str(chosen_cred.get("user") or "") if chosen_cred else ""
        pwd_val = str(chosen_cred.get("password") or "") if chosen_cred else ""
        if chosen_cred:
            try:
                sec_extra = json.loads(chosen_cred.get("extra_secrets_json") or "{}")
                if isinstance(sec_extra, dict) and sec_extra:
                    extra_props = {**extra_props, **sec_extra}
            except Exception:
                pass
            # 若工作区引用的 JDBC 驱动在本机不存在，则尝试沿用本机凭据绑定的驱动 ID
            cred_drv = str(chosen_cred.get("jdbc_driver_id") or "").strip()
            if cred_drv and (not jdbc_driver_id or jdbc_driver_id not in local_driver_ids):
                if cred_drv in local_driver_ids:
                    jdbc_driver_id = cred_drv

        return ConnectionProfile(
            connection_id=cid,
            name=row["name"],
            driver_track=driver_track,
            db_type=db_type,
            host=host,
            port=port,
            dbname=dbname,
            default_schema=row["default_schema"] or "public",
            user=user_val,
            password=pwd_val,
            sslmode=row["sslmode"] or "prefer",
            auth_mode=row["auth_mode"] or "LDAP",
            sqlite_path=sqlite_path,
            jdbc_driver_id=jdbc_driver_id,
            jdbc_url=jdbc_url,
            jdbc_driver_class=row["jdbc_driver_class"] or "",
            extra_props=extra_props,
            standard_format=str(raw_std_fmt).strip() or "AUTO",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            credential_status=cred_status,
        )

    def list_connection_credentials(self) -> List[Dict[str, Any]]:
        """列出私有库个人凭据元数据（脱敏，绝不返回明文密码）."""
        with self._lock, self._get_private_conn() as pconn:
            rows = pconn.execute(
                "SELECT cred_id, connection_id, endpoint_fingerprint, db_type, host, port, dbname, "
                "jdbc_url, user, (CASE WHEN password != '' THEN 1 ELSE 0 END) AS has_password, updated_at "
                "FROM connection_credentials ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_connection_profiles(self) -> List[ConnectionProfile]:
        with self._lock:
            cred_by_cid, cred_by_fp, local_driver_ids = self._load_credential_maps()
            with self._get_workspace_conn() as wconn:
                rows = wconn.execute(
                    "SELECT * FROM connection_topology ORDER BY updated_at DESC, name ASC"
                ).fetchall()
            return [
                self._assemble_profile(r, cred_by_cid, cred_by_fp, local_driver_ids)
                for r in rows
            ]

    def get_connection_profile(self, connection_id: str) -> Optional[ConnectionProfile]:
        if not connection_id:
            return None
        with self._lock:
            cred_by_cid, cred_by_fp, local_driver_ids = self._load_credential_maps()
            with self._get_workspace_conn() as wconn:
                row = wconn.execute(
                    "SELECT * FROM connection_topology WHERE connection_id = ?",
                    (connection_id,),
                ).fetchone()
            return (
                self._assemble_profile(row, cred_by_cid, cred_by_fp, local_driver_ids)
                if row
                else None
            )

    def upsert_connection_profile(self, profile: ConnectionProfile) -> ConnectionProfile:
        now = self._now_str()
        created_at = profile.created_at or now
        updated_at = now
        pub_extra, sec_extra = split_extra_props_secrets(profile.extra_props or {})
        extra_json = json.dumps(pub_extra, ensure_ascii=False)
        sec_extra_json = json.dumps(sec_extra, ensure_ascii=False)
        standard_format = (profile.standard_format or "AUTO").strip() or "AUTO"
        clean_jdbc_url, jdbc_user, jdbc_pwd = sanitize_jdbc_url(profile.jdbc_url or "")
        eff_user = (profile.user or "").strip() or jdbc_user
        eff_password = profile.password if profile.password else jdbc_pwd
        fp = compute_connection_fingerprint(
            driver_track=profile.driver_track,
            db_type=profile.db_type,
            host=profile.host,
            port=int(profile.port or 0),
            dbname=profile.dbname,
            jdbc_url=clean_jdbc_url,
            sqlite_path=profile.sqlite_path,
        )
        cred_id = self._credential_row_id(profile.connection_id, fp)
        with self._lock:
            # 1. 写入共享工作区：严格不含 user / password / 私密扩展属性及 JDBC URL 内嵌凭据
            with self._get_workspace_conn() as wconn:
                wconn.execute(
                    """
                    INSERT INTO connection_topology (
                        connection_id, name, driver_track, db_type,
                        host, port, dbname, default_schema,
                        sslmode, auth_mode, sqlite_path,
                        jdbc_driver_id, jdbc_url, jdbc_driver_class,
                        extra_props_json, standard_format, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(connection_id) DO UPDATE SET
                        name = excluded.name,
                        driver_track = excluded.driver_track,
                        db_type = excluded.db_type,
                        host = excluded.host,
                        port = excluded.port,
                        dbname = excluded.dbname,
                        default_schema = excluded.default_schema,
                        sslmode = excluded.sslmode,
                        auth_mode = excluded.auth_mode,
                        sqlite_path = excluded.sqlite_path,
                        jdbc_driver_id = excluded.jdbc_driver_id,
                        jdbc_url = excluded.jdbc_url,
                        jdbc_driver_class = excluded.jdbc_driver_class,
                        extra_props_json = excluded.extra_props_json,
                        standard_format = excluded.standard_format,
                        updated_at = excluded.updated_at
                    """,
                    (
                        profile.connection_id,
                        profile.name,
                        profile.driver_track,
                        profile.db_type,
                        profile.host,
                        int(profile.port or 0),
                        profile.dbname,
                        profile.default_schema,
                        profile.sslmode,
                        profile.auth_mode,
                        profile.sqlite_path,
                        profile.jdbc_driver_id,
                        clean_jdbc_url,
                        profile.jdbc_driver_class,
                        extra_json,
                        standard_format,
                        created_at,
                        updated_at,
                    ),
                )
                wconn.commit()

            # 2. 写入本地私有保险箱凭据库（按 (connection_id, endpoint_fingerprint) 独立主键隔离，跨工作区同 ID 绝不互相覆盖）
            with self._get_private_conn() as pconn:
                legacy_cred_id = f"cid:{profile.connection_id}"
                if cred_id != legacy_cred_id:
                    pconn.execute(
                        "DELETE FROM connection_credentials WHERE cred_id = ? AND (endpoint_fingerprint = ? OR endpoint_fingerprint = '')",
                        (legacy_cred_id, fp),
                    )
                pconn.execute(
                    """
                    INSERT INTO connection_credentials (
                        cred_id, connection_id, endpoint_fingerprint,
                        db_type, host, port, dbname, jdbc_url,
                        user, password, sslmode, auth_mode, sqlite_path,
                        jdbc_driver_id, extra_secrets_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cred_id) DO UPDATE SET
                        connection_id = excluded.connection_id,
                        endpoint_fingerprint = excluded.endpoint_fingerprint,
                        db_type = excluded.db_type,
                        host = excluded.host,
                        port = excluded.port,
                        dbname = excluded.dbname,
                        jdbc_url = excluded.jdbc_url,
                        user = excluded.user,
                        password = excluded.password,
                        sslmode = excluded.sslmode,
                        auth_mode = excluded.auth_mode,
                        sqlite_path = excluded.sqlite_path,
                        jdbc_driver_id = excluded.jdbc_driver_id,
                        extra_secrets_json = excluded.extra_secrets_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        cred_id,
                        profile.connection_id,
                        fp,
                        profile.db_type,
                        profile.host,
                        int(profile.port or 0),
                        profile.dbname,
                        clean_jdbc_url,
                        eff_user,
                        eff_password,
                        profile.sslmode,
                        profile.auth_mode,
                        profile.sqlite_path,
                        profile.jdbc_driver_id,
                        sec_extra_json,
                        updated_at,
                    ),
                )
                pconn.commit()
        return self.get_connection_profile(profile.connection_id) or profile

    def delete_connection_profile(self, connection_id: str) -> bool:
        with self._lock:
            with self._get_workspace_conn() as wconn:
                cur = wconn.execute(
                    "DELETE FROM connection_topology WHERE connection_id = ?",
                    (connection_id,),
                )
                wconn.execute("DELETE FROM meta_schemas WHERE connection_id = ?", (connection_id,))
                wconn.execute("DELETE FROM meta_tables WHERE connection_id = ?", (connection_id,))
                wconn.execute("DELETE FROM meta_columns WHERE connection_id = ?", (connection_id,))
                wconn.commit()
                deleted = cur.rowcount > 0
            # 三层分离模式下，Layer 2 个人凭据库跨工作区共享：从某一工作区删除拓扑时不得抹除其他工作区复用的个人密码；
            # 仅在单库兼容模式下同步清理历史表记录
            if not self.is_split_storage:
                with self._get_private_conn() as pconn:
                    pconn.execute(
                        "DELETE FROM connection_credentials WHERE connection_id = ?",
                        (connection_id,),
                    )
                    has_legacy = pconn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connection_profiles'"
                    ).fetchone()
                    if has_legacy:
                        pconn.execute(
                            "DELETE FROM connection_profiles WHERE connection_id = ?",
                            (connection_id,),
                        )
                    pconn.commit()
            return deleted

    def list_jdbc_drivers(self) -> List[DriverPackage]:
        with self._lock, self._get_private_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jdbc_drivers ORDER BY uploaded_at DESC, name ASC"
            ).fetchall()
            return [
                DriverPackage(
                    driver_id=r["driver_id"],
                    name=r["name"],
                    file_name=r["file_name"],
                    stored_rel_path=r["stored_rel_path"],
                    sha256=r["sha256"],
                    file_size=int(r["file_size"] or 0),
                    driver_class_hint=r["driver_class_hint"] or "",
                    db_type_hint=r["db_type_hint"] or "CUSTOM_JDBC",
                    uploaded_at=r["uploaded_at"] or "",
                )
                for r in rows
            ]

    def get_jdbc_driver(self, driver_id: str) -> Optional[DriverPackage]:
        with self._lock, self._get_private_conn() as conn:
            r = conn.execute(
                "SELECT * FROM jdbc_drivers WHERE driver_id = ?",
                (driver_id,),
            ).fetchone()
            if not r:
                return None
            return DriverPackage(
                driver_id=r["driver_id"],
                name=r["name"],
                file_name=r["file_name"],
                stored_rel_path=r["stored_rel_path"],
                sha256=r["sha256"],
                file_size=int(r["file_size"] or 0),
                driver_class_hint=r["driver_class_hint"] or "",
                db_type_hint=r["db_type_hint"] or "CUSTOM_JDBC",
                uploaded_at=r["uploaded_at"] or "",
            )

    def upsert_jdbc_driver(self, pkg: DriverPackage) -> DriverPackage:
        now = pkg.uploaded_at or self._now_str()
        with self._lock, self._get_private_conn() as conn:
            conn.execute(
                """
                INSERT INTO jdbc_drivers (
                    driver_id, name, file_name, stored_rel_path,
                    sha256, file_size, driver_class_hint, db_type_hint, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(driver_id) DO UPDATE SET
                    name = excluded.name,
                    file_name = excluded.file_name,
                    stored_rel_path = excluded.stored_rel_path,
                    sha256 = excluded.sha256,
                    file_size = excluded.file_size,
                    driver_class_hint = excluded.driver_class_hint,
                    db_type_hint = excluded.db_type_hint,
                    uploaded_at = excluded.uploaded_at
                """,
                (
                    pkg.driver_id,
                    pkg.name,
                    pkg.file_name,
                    pkg.stored_rel_path,
                    pkg.sha256,
                    pkg.file_size,
                    pkg.driver_class_hint,
                    pkg.db_type_hint,
                    now,
                ),
            )
            conn.commit()
        return self.get_jdbc_driver(pkg.driver_id) or pkg

    def delete_jdbc_driver(self, driver_id: str) -> Optional[DriverPackage]:
        pkg = self.get_jdbc_driver(driver_id)
        if not pkg:
            return None
        with self._lock, self._get_private_conn() as conn:
            conn.execute("DELETE FROM jdbc_drivers WHERE driver_id = ?", (driver_id,))
            conn.commit()
        return pkg

    # =========================================================================
    # 全局持久化元数据索引表 (MetadataHub 本地缓存)
    # =========================================================================

    def save_catalog_snapshot(
        self,
        connection_id: str,
        schemas: List[SchemaMeta],
        tables: List[Dict[str, Any]],
        target_schema: Optional[str] = None,
    ) -> None:
        """增量或全量持久化指定连接的 Schema 与库表目录."""
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            if target_schema:
                conn.execute(
                    "DELETE FROM meta_schemas WHERE connection_id = ? AND schema_name = ?",
                    (connection_id, target_schema),
                )
            else:
                conn.execute("DELETE FROM meta_schemas WHERE connection_id = ?", (connection_id,))

            if schemas:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO meta_schemas (
                        connection_id, schema_name, table_count, view_count, refreshed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            connection_id,
                            s.schema_name,
                            int(s.table_count),
                            int(s.view_count),
                            now,
                        )
                        for s in schemas
                    ],
                )

            # 保留已缓存列详情的表属性，仅同步表目录并清理已删除表
            existing_map: Dict[Tuple[str, str], sqlite3.Row] = {}
            for r in conn.execute(
                "SELECT * FROM meta_tables WHERE connection_id = ?", (connection_id,)
            ).fetchall():
                existing_map[(r["schema_name"], r["table_name"])] = r

            incoming_keys = set()
            table_rows = []
            for t in tables:
                sch = str(t.get("schema_name") or "").strip()
                tbl = str(t.get("table_name") or "").strip()
                if not sch or not tbl:
                    continue
                incoming_keys.add((sch, tbl))
                prev = existing_map.get((sch, tbl))
                obj_type = str(t.get("object_type") or (prev["object_type"] if prev else "TABLE")).upper()
                t_comm = str(t.get("table_comment") or (prev["table_comment"] if prev else ""))
                orient = str(t.get("orientation") or (prev["orientation"] if prev else "ROW"))
                cols_cached = int(prev["columns_cached"]) if prev else 0
                table_rows.append(
                    (
                        connection_id,
                        sch,
                        tbl,
                        obj_type,
                        t_comm,
                        orient,
                        prev["with_clause"] if prev else "",
                        prev["distribute_clause"] if prev else "",
                        prev["distribute_keys_json"] if prev else "[]",
                        prev["partition_clause"] if prev else "",
                        prev["partition_keys_json"] if prev else "[]",
                        prev["primary_keys_json"] if prev else "[]",
                        prev["penetrated_base_tables_json"] if prev else "[]",
                        int(t.get("row_count") or (prev["row_count"] if prev else 0)),
                        prev["raw_ddl"] if prev else "",
                        prev["view_definition"] if prev else "",
                        cols_cached,
                        now,
                    )
                )

            if target_schema:
                for sch, tbl in list(existing_map.keys()):
                    if sch == target_schema and (sch, tbl) not in incoming_keys:
                        conn.execute(
                            "DELETE FROM meta_tables WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                            (connection_id, sch, tbl),
                        )
                        conn.execute(
                            "DELETE FROM meta_columns WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                            (connection_id, sch, tbl),
                        )
            else:
                for sch, tbl in list(existing_map.keys()):
                    if (sch, tbl) not in incoming_keys:
                        conn.execute(
                            "DELETE FROM meta_tables WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                            (connection_id, sch, tbl),
                        )
                        conn.execute(
                            "DELETE FROM meta_columns WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                            (connection_id, sch, tbl),
                        )

            if table_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO meta_tables (
                        connection_id, schema_name, table_name, object_type, table_comment,
                        orientation, with_clause, distribute_clause, distribute_keys_json,
                        partition_clause, partition_keys_json, primary_keys_json,
                        penetrated_base_tables_json, row_count, raw_ddl, view_definition,
                        columns_cached, refreshed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    table_rows,
                )
            conn.commit()

    def save_table_meta(self, connection_id: str, meta: TableMeta) -> None:
        """将单表/视图完整元数据（含全部列、主键、分布键、分区键、视图穿透血缘）写入本地持久化缓存."""
        if not meta.exists:
            return
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO meta_schemas (
                    connection_id, schema_name, table_count, view_count, refreshed_at
                ) VALUES (
                    ?, ?,
                    COALESCE((SELECT table_count FROM meta_schemas WHERE connection_id = ? AND schema_name = ?), 0),
                    COALESCE((SELECT view_count FROM meta_schemas WHERE connection_id = ? AND schema_name = ?), 0),
                    ?
                )
                """,
                (
                    connection_id,
                    meta.schema_name,
                    connection_id,
                    meta.schema_name,
                    connection_id,
                    meta.schema_name,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO meta_tables (
                    connection_id, schema_name, table_name, object_type, table_comment,
                    orientation, with_clause, distribute_clause, distribute_keys_json,
                    partition_clause, partition_keys_json, primary_keys_json,
                    penetrated_base_tables_json, row_count, raw_ddl, view_definition,
                    columns_cached, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    connection_id,
                    meta.schema_name,
                    meta.table_name,
                    meta.object_type or "TABLE",
                    meta.table_comment or "",
                    meta.orientation or "ROW",
                    meta.with_clause or "",
                    meta.distribute_clause or "",
                    json.dumps(meta.distribute_keys or [], ensure_ascii=False),
                    meta.partition_clause or "",
                    json.dumps(meta.partition_keys or [], ensure_ascii=False),
                    json.dumps(meta.primary_keys or [], ensure_ascii=False),
                    json.dumps(meta.penetrated_base_tables or [], ensure_ascii=False),
                    int(meta.row_count or 0),
                    meta.raw_ddl or "",
                    meta.view_definition or "",
                    1,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM meta_columns WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                (connection_id, meta.schema_name, meta.table_name),
            )
            if meta.columns:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO meta_columns (
                        connection_id, schema_name, table_name, ordinal_position,
                        column_name, data_type, normalized_type, is_not_null,
                        column_default, column_comment, is_primary_key,
                        index_names_json, lineage_source, refreshed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            connection_id,
                            meta.schema_name,
                            meta.table_name,
                            int(c.ordinal_position),
                            c.column_name,
                            c.data_type or "",
                            c.normalized_type or "",
                            1 if c.is_not_null else 0,
                            c.column_default,
                            c.column_comment or "",
                            1 if c.is_primary_key else 0,
                            json.dumps(c.index_names or [], ensure_ascii=False),
                            c.lineage_source or "",
                            now,
                        )
                        for c in meta.columns
                    ],
                )
            conn.commit()

    def get_cached_catalog(self, connection_id: str) -> Dict[str, Any]:
        with self._lock, self._get_conn() as conn:
            s_rows = conn.execute(
                "SELECT * FROM meta_schemas WHERE connection_id = ? ORDER BY schema_name ASC",
                (connection_id,),
            ).fetchall()
            t_rows = conn.execute(
                """
                SELECT connection_id, schema_name, table_name, object_type,
                       table_comment, orientation, row_count, columns_cached, refreshed_at
                FROM meta_tables
                WHERE connection_id = ?
                ORDER BY schema_name ASC, object_type ASC, table_name ASC
                """,
                (connection_id,),
            ).fetchall()
            c_rows = conn.execute(
                """
                SELECT LOWER(schema_name) AS s_low, LOWER(table_name) AS t_low, COUNT(*) AS col_count
                FROM meta_columns
                WHERE connection_id = ?
                GROUP BY LOWER(schema_name), LOWER(table_name)
                """,
                (connection_id,),
            ).fetchall()
            col_count_map = {
                (str(r["s_low"] or ""), str(r["t_low"] or "")): int(r["col_count"] or 0)
                for r in c_rows
            }
            tables_out = []
            for r in t_rows:
                d = dict(r)
                d["col_count"] = col_count_map.get(
                    (
                        str(d.get("schema_name") or "").lower(),
                        str(d.get("table_name") or "").lower(),
                    ),
                    0,
                )
                tables_out.append(d)
            return {
                "connection_id": connection_id,
                "schemas": [dict(r) for r in s_rows],
                "tables": tables_out,
            }

    def get_cached_table_meta(
        self, connection_id: str, schema_name: str, table_name: str
    ) -> Optional[TableMeta]:
        with self._lock, self._get_conn() as conn:
            t_row = conn.execute(
                """
                SELECT * FROM meta_tables
                WHERE connection_id = ?
                  AND schema_name = ?
                  AND table_name = ?
                LIMIT 1
                """,
                (connection_id, schema_name, table_name),
            ).fetchone()
            if not t_row:
                t_row = conn.execute(
                    """
                    SELECT * FROM meta_tables
                    WHERE connection_id = ?
                      AND LOWER(schema_name) = LOWER(?)
                      AND LOWER(table_name) = LOWER(?)
                    LIMIT 1
                    """,
                    (connection_id, schema_name, table_name),
                ).fetchone()
            if not t_row or not int(t_row["columns_cached"] or 0):
                return None
            c_rows = conn.execute(
                """
                SELECT * FROM meta_columns
                WHERE connection_id = ? AND schema_name = ? AND table_name = ?
                ORDER BY ordinal_position ASC
                """,
                (connection_id, t_row["schema_name"], t_row["table_name"]),
            ).fetchall()
            cols = [
                ColumnMeta(
                    ordinal_position=int(cr["ordinal_position"]),
                    column_name=cr["column_name"],
                    data_type=cr["data_type"],
                    normalized_type=cr["normalized_type"] or "",
                    is_not_null=bool(cr["is_not_null"]),
                    column_default=cr["column_default"],
                    column_comment=cr["column_comment"] or "",
                    is_primary_key=bool(cr["is_primary_key"]),
                    index_names=json.loads(cr["index_names_json"] or "[]"),
                    lineage_source=cr["lineage_source"] or "",
                )
                for cr in c_rows
            ]
            return TableMeta(
                schema_name=t_row["schema_name"],
                table_name=t_row["table_name"],
                exists=True,
                table_comment=t_row["table_comment"] or "",
                orientation=t_row["orientation"] or "ROW",
                with_clause=t_row["with_clause"] or "",
                distribute_clause=t_row["distribute_clause"] or "",
                distribute_keys=json.loads(t_row["distribute_keys_json"] or "[]"),
                partition_clause=t_row["partition_clause"] or "",
                partition_keys=json.loads(t_row["partition_keys_json"] or "[]"),
                primary_keys=json.loads(t_row["primary_keys_json"] or "[]"),
                penetrated_base_tables=json.loads(
                    t_row["penetrated_base_tables_json"] or "[]"
                ),
                columns=cols,
                row_count=int(t_row["row_count"] or 0),
                raw_ddl=t_row["raw_ddl"] or "",
                object_type=t_row["object_type"] or "TABLE",
                view_definition=t_row["view_definition"] or "",
            )

    def get_cached_tables_meta_batch(
        self,
        connection_id: str,
        targets: List[Tuple[str, str]],
        *,
        fallback_default_schema: str = "",
    ) -> Dict[Tuple[str, str], TableMeta]:
        """单连接批量读取多张已缓存列结构的表元数据，避免批量核验时产生 N 次 SQLite 连接开销."""
        if not connection_id or not targets:
            return {}
        wanted_keys = {
            (str(s or "").strip().lower(), str(t or "").strip().lower())
            for s, t in targets
            if str(t or "").strip()
        }
        if not wanted_keys:
            return {}
        fb_low = str(fallback_default_schema or "").strip().lower()
        fb_candidates = {x for x in (fb_low, "public") if x} if fb_low else set()
        lower_tbls = list({t for _, t in wanted_keys})
        matched_t_rows: Dict[Tuple[str, str], sqlite3.Row] = {}
        rows_by_tbl: Dict[str, List[sqlite3.Row]] = {}

        with self._lock, self._get_conn() as conn:
            chunk_size = 400
            for i in range(0, len(lower_tbls), chunk_size):
                chunk = lower_tbls[i : i + chunk_size]
                ph = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT * FROM meta_tables
                    WHERE connection_id = ?
                      AND columns_cached != 0
                      AND LOWER(table_name) IN ({ph})
                    ORDER BY schema_name ASC
                    """,
                    [connection_id, *chunk],
                ).fetchall()
                for r in rows:
                    s_l = str(r["schema_name"]).lower()
                    t_l = str(r["table_name"]).lower()
                    rows_by_tbl.setdefault(t_l, []).append(r)
                    k = (s_l, t_l)
                    if k in wanted_keys and k not in matched_t_rows:
                        matched_t_rows[k] = r

            if fb_candidates:
                for req_s, req_t in wanted_keys:
                    if (req_s, req_t) not in matched_t_rows and req_s in fb_candidates:
                        cands = rows_by_tbl.get(req_t)
                        if cands:
                            matched_t_rows[(req_s, req_t)] = cands[0]

            if not matched_t_rows:
                return {}

            actual_tbls = list({r["table_name"] for r in matched_t_rows.values()})
            cols_by_exact: Dict[Tuple[str, str], List[ColumnMeta]] = {}
            for i in range(0, len(actual_tbls), chunk_size):
                chunk = actual_tbls[i : i + chunk_size]
                ph = ",".join("?" for _ in chunk)
                c_rows = conn.execute(
                    f"""
                    SELECT * FROM meta_columns
                    WHERE connection_id = ? AND table_name IN ({ph})
                    ORDER BY schema_name ASC, table_name ASC, ordinal_position ASC
                    """,
                    [connection_id, *chunk],
                ).fetchall()
                for cr in c_rows:
                    ek = (cr["schema_name"], cr["table_name"])
                    cols_by_exact.setdefault(ek, []).append(
                        ColumnMeta(
                            ordinal_position=int(cr["ordinal_position"]),
                            column_name=cr["column_name"],
                            data_type=cr["data_type"],
                            normalized_type=cr["normalized_type"] or "",
                            is_not_null=bool(cr["is_not_null"]),
                            column_default=cr["column_default"],
                            column_comment=cr["column_comment"] or "",
                            is_primary_key=bool(cr["is_primary_key"]),
                            index_names=json.loads(cr["index_names_json"] or "[]"),
                            lineage_source=cr["lineage_source"] or "",
                        )
                    )

        out: Dict[Tuple[str, str], TableMeta] = {}
        for k, t_row in matched_t_rows.items():
            cols = cols_by_exact.get((t_row["schema_name"], t_row["table_name"]), [])
            out[k] = TableMeta(
                schema_name=t_row["schema_name"],
                table_name=t_row["table_name"],
                exists=True,
                table_comment=t_row["table_comment"] or "",
                orientation=t_row["orientation"] or "ROW",
                with_clause=t_row["with_clause"] or "",
                distribute_clause=t_row["distribute_clause"] or "",
                distribute_keys=json.loads(t_row["distribute_keys_json"] or "[]"),
                partition_clause=t_row["partition_clause"] or "",
                partition_keys=json.loads(t_row["partition_keys_json"] or "[]"),
                primary_keys=json.loads(t_row["primary_keys_json"] or "[]"),
                penetrated_base_tables=json.loads(
                    t_row["penetrated_base_tables_json"] or "[]"
                ),
                columns=cols,
                row_count=int(t_row["row_count"] or 0),
                raw_ddl=t_row["raw_ddl"] or "",
                object_type=t_row["object_type"] or "TABLE",
                view_definition=t_row["view_definition"] or "",
            )
        return out

    def save_tables_meta_batch(self, connection_id: str, metas: List[TableMeta]) -> None:
        """单事务批量写入多张表/视图完整元数据缓存，消除逐表 commit 磁盘同步风暴."""
        valid = [m for m in (metas or []) if m and m.exists and m.table_name]
        if not connection_id or not valid:
            return
        now = self._now_str()
        dedup: Dict[Tuple[str, str], TableMeta] = {}
        for m in valid:
            dedup[(m.schema_name, m.table_name)] = m
        unique_metas = list(dedup.values())
        unique_schemas = list(dict.fromkeys(m.schema_name for m in unique_metas))

        schema_params = [
            (connection_id, sch, connection_id, sch, connection_id, sch, now)
            for sch in unique_schemas
        ]
        table_params = [
            (
                connection_id,
                m.schema_name,
                m.table_name,
                m.object_type or "TABLE",
                m.table_comment or "",
                m.orientation or "ROW",
                m.with_clause or "",
                m.distribute_clause or "",
                json.dumps(m.distribute_keys or [], ensure_ascii=False),
                m.partition_clause or "",
                json.dumps(m.partition_keys or [], ensure_ascii=False),
                json.dumps(m.primary_keys or [], ensure_ascii=False),
                json.dumps(m.penetrated_base_tables or [], ensure_ascii=False),
                int(m.row_count or 0),
                m.raw_ddl or "",
                m.view_definition or "",
                1,
                now,
            )
            for m in unique_metas
        ]
        del_col_params = [(connection_id, m.schema_name, m.table_name) for m in unique_metas]
        col_params = [
            (
                connection_id,
                m.schema_name,
                m.table_name,
                int(c.ordinal_position),
                c.column_name,
                c.data_type or "",
                c.normalized_type or "",
                1 if c.is_not_null else 0,
                c.column_default,
                c.column_comment or "",
                1 if c.is_primary_key else 0,
                json.dumps(c.index_names or [], ensure_ascii=False),
                c.lineage_source or "",
                now,
            )
            for m in unique_metas
            for c in (m.columns or [])
        ]

        with self._lock, self._get_conn() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO meta_schemas (
                    connection_id, schema_name, table_count, view_count, refreshed_at
                ) VALUES (
                    ?, ?,
                    COALESCE((SELECT table_count FROM meta_schemas WHERE connection_id = ? AND schema_name = ?), 0),
                    COALESCE((SELECT view_count FROM meta_schemas WHERE connection_id = ? AND schema_name = ?), 0),
                    ?
                )
                """,
                schema_params,
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO meta_tables (
                    connection_id, schema_name, table_name, object_type, table_comment,
                    orientation, with_clause, distribute_clause, distribute_keys_json,
                    partition_clause, partition_keys_json, primary_keys_json,
                    penetrated_base_tables_json, row_count, raw_ddl, view_definition,
                    columns_cached, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                table_params,
            )
            conn.executemany(
                "DELETE FROM meta_columns WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                del_col_params,
            )
            if col_params:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO meta_columns (
                        connection_id, schema_name, table_name, ordinal_position,
                        column_name, data_type, normalized_type, is_not_null,
                        column_default, column_comment, is_primary_key,
                        index_names_json, lineage_source, refreshed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    col_params,
                )
            conn.commit()

    def search_cached_metadata(
        self, keyword: str, connection_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """在本地持久化元数据缓存中秒级全文检索表名、表注释、字段名或字段注释."""
        kw = f"%{(keyword or '').strip()}%"
        params: List[Any] = []
        conn_filter_t = ""
        conn_filter_c = ""
        if connection_id:
            conn_filter_t = "AND connection_id = ?"
            conn_filter_c = "AND connection_id = ?"

        with self._lock, self._get_conn() as conn:
            t_params = ([connection_id] if connection_id else []) + [kw, kw, limit]
            t_hits = conn.execute(
                f"""
                SELECT 'TABLE' AS hit_kind, connection_id, schema_name, table_name,
                       object_type, '' AS column_name, '' AS data_type, table_comment AS comment
                FROM meta_tables
                WHERE 1=1 {conn_filter_t}
                  AND (table_name LIKE ? OR table_comment LIKE ?)
                LIMIT ?
                """,
                t_params,
            ).fetchall()

            c_params = ([connection_id] if connection_id else []) + [kw, kw, limit]
            c_hits = conn.execute(
                f"""
                SELECT 'COLUMN' AS hit_kind, connection_id, schema_name, table_name,
                       'COLUMN' AS object_type, column_name, data_type, column_comment AS comment
                FROM meta_columns
                WHERE 1=1 {conn_filter_c}
                  AND (column_name LIKE ? OR column_comment LIKE ?)
                LIMIT ?
                """,
                c_params,
            ).fetchall()

            merged = [dict(r) for r in t_hits] + [dict(r) for r in c_hits]
            return merged[:limit]


    # =========================================================================
    # 全局 AI 大模型服务商档案 (LLMProviderProfile) 持久化
    # =========================================================================

    @staticmethod
    def _row_to_llm_provider(row: sqlite3.Row) -> LLMProviderProfile:
        try:
            models = json.loads(row["models_json"] or "[]")
            if not isinstance(models, list):
                models = []
        except Exception:
            models = []
        try:
            extra = json.loads(row["extra_json"] or "{}")
            if not isinstance(extra, dict):
                extra = {}
        except Exception:
            extra = {}
        return LLMProviderProfile(
            provider_id=row["provider_id"],
            name=row["name"],
            provider_type=row["provider_type"] or "OPENAI_COMPATIBLE",
            enabled=bool(row["enabled"]),
            base_url=row["base_url"] or "",
            api_key=row["api_key"] or "",
            default_model=row["default_model"] or "",
            models=[str(m).strip() for m in models if str(m).strip()],
            is_default=bool(row["is_default"]),
            extra=extra,
            updated_at=row["updated_at"] or "",
        )

    def list_llm_providers(self) -> List[LLMProviderProfile]:
        with self._lock, self._get_private_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM llm_providers
                ORDER BY
                    CASE provider_id
                        WHEN 'deepseek' THEN 1
                        WHEN 'gemini' THEN 2
                        WHEN 'custom' THEN 3
                        ELSE 10
                    END ASC,
                    name ASC
                """
            ).fetchall()
            return [self._row_to_llm_provider(r) for r in rows]

    def get_llm_provider(self, provider_id: str) -> Optional[LLMProviderProfile]:
        if not provider_id:
            return None
        with self._lock, self._get_private_conn() as conn:
            row = conn.execute(
                "SELECT * FROM llm_providers WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return self._row_to_llm_provider(row) if row else None

    def upsert_llm_provider(self, profile: LLMProviderProfile) -> LLMProviderProfile:
        now = self._now_str()
        models_json = json.dumps(list(profile.models or []), ensure_ascii=False)
        extra_json = json.dumps(dict(profile.extra or {}), ensure_ascii=False)
        with self._lock, self._get_private_conn() as conn:
            if profile.is_default:
                conn.execute(
                    "UPDATE llm_providers SET is_default = 0 WHERE provider_id != ?",
                    (profile.provider_id,),
                )
            conn.execute(
                """
                INSERT INTO llm_providers (
                    provider_id, name, provider_type, enabled,
                    base_url, api_key, default_model, models_json,
                    is_default, extra_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    name = excluded.name,
                    provider_type = excluded.provider_type,
                    enabled = excluded.enabled,
                    base_url = excluded.base_url,
                    api_key = excluded.api_key,
                    default_model = excluded.default_model,
                    models_json = excluded.models_json,
                    is_default = excluded.is_default,
                    extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.provider_id,
                    profile.name,
                    profile.provider_type,
                    1 if profile.enabled else 0,
                    profile.base_url,
                    profile.api_key,
                    profile.default_model,
                    models_json,
                    1 if profile.is_default else 0,
                    extra_json,
                    now,
                ),
            )
            conn.commit()
        return self.get_llm_provider(profile.provider_id) or profile

    def delete_llm_provider(self, provider_id: str) -> bool:
        with self._lock, self._get_private_conn() as conn:
            cur = conn.execute(
                "DELETE FROM llm_providers WHERE provider_id = ?",
                (provider_id,),
            )
            conn.commit()
            return cur.rowcount > 0

