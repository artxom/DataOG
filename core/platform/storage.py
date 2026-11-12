"""DataOG 全局状态存储与无损迁移引擎 (data/dataog_state.db).

核心能力：
1. 运行时状态库统一为 `data/dataog_state.db`（WAL 模式 + 批量事务优化）；
2. 启动时自动检测旧版 `data/dws_migrator_state.db` 并无损迁移所有历史表记录；
3. 自动扫描旧版 `app_meta` 连接键值 (`dws_conn_saved`, `dws_connection_config`, `cross_source_conn`, `cross_target_conn`)，
   无损升级为全局命名数据源档案 (`connection_profiles`)；
4. 提供平台与模块级 SQLite 表幂等列自检接口 (`register_table_schema`)，保障跨分支/跨版本切换零报错；
5. 提供全局连接档案、JDBC 驱动包索引与持久化元数据缓存表 (`meta_schemas`, `meta_tables`, `meta_columns`) 存取能力。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from core.platform.models import (
    ColumnMeta,
    ConnectionProfile,
    DriverPackage,
    LLMProviderProfile,
    SchemaMeta,
    TableMeta,
)

logger = logging.getLogger("dataog")


class PlatformStateStore:
    """线程安全的 DataOG 全局本地 SQLite 状态库."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self._migrate_legacy_db_file_if_needed()
        self._migrate_legacy_connections_if_needed()

    @contextlib.contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 10000;")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _now_str() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _init_db(self) -> None:
        with self._lock, self._get_conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    target TEXT NOT NULL,
                    message TEXT NOT NULL,
                    sql_text TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS connection_profiles (
                    connection_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    driver_track TEXT NOT NULL DEFAULT 'NATIVE',
                    db_type TEXT NOT NULL DEFAULT 'DWS',
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER NOT NULL DEFAULT 8000,
                    dbname TEXT NOT NULL DEFAULT '',
                    default_schema TEXT NOT NULL DEFAULT 'public',
                    user TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    sslmode TEXT NOT NULL DEFAULT 'prefer',
                    auth_mode TEXT NOT NULL DEFAULT 'LDAP',
                    sqlite_path TEXT NOT NULL DEFAULT '',
                    jdbc_driver_id TEXT NOT NULL DEFAULT '',
                    jdbc_url TEXT NOT NULL DEFAULT '',
                    jdbc_driver_class TEXT NOT NULL DEFAULT '',
                    extra_props_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS jdbc_drivers (
                    driver_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    stored_rel_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    driver_class_hint TEXT NOT NULL DEFAULT '',
                    db_type_hint TEXT NOT NULL DEFAULT 'CUSTOM_JDBC',
                    uploaded_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS meta_schemas (
                    connection_id TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_count INTEGER NOT NULL DEFAULT 0,
                    view_count INTEGER NOT NULL DEFAULT 0,
                    refreshed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (connection_id, schema_name)
                );

                CREATE TABLE IF NOT EXISTS meta_tables (
                    connection_id TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    object_type TEXT NOT NULL DEFAULT 'TABLE',
                    table_comment TEXT NOT NULL DEFAULT '',
                    orientation TEXT NOT NULL DEFAULT 'ROW',
                    with_clause TEXT NOT NULL DEFAULT '',
                    distribute_clause TEXT NOT NULL DEFAULT '',
                    distribute_keys_json TEXT NOT NULL DEFAULT '[]',
                    partition_clause TEXT NOT NULL DEFAULT '',
                    partition_keys_json TEXT NOT NULL DEFAULT '[]',
                    primary_keys_json TEXT NOT NULL DEFAULT '[]',
                    penetrated_base_tables_json TEXT NOT NULL DEFAULT '[]',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    raw_ddl TEXT NOT NULL DEFAULT '',
                    view_definition TEXT NOT NULL DEFAULT '',
                    columns_cached INTEGER NOT NULL DEFAULT 0,
                    refreshed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (connection_id, schema_name, table_name)
                );
                CREATE INDEX IF NOT EXISTS idx_meta_tables_search
                    ON meta_tables (connection_id, table_name);

                CREATE TABLE IF NOT EXISTS meta_columns (
                    connection_id TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    ordinal_position INTEGER NOT NULL,
                    column_name TEXT NOT NULL,
                    data_type TEXT NOT NULL DEFAULT '',
                    normalized_type TEXT NOT NULL DEFAULT '',
                    is_not_null INTEGER NOT NULL DEFAULT 0,
                    column_default TEXT,
                    column_comment TEXT NOT NULL DEFAULT '',
                    is_primary_key INTEGER NOT NULL DEFAULT 0,
                    index_names_json TEXT NOT NULL DEFAULT '[]',
                    lineage_source TEXT NOT NULL DEFAULT '',
                    refreshed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (connection_id, schema_name, table_name, column_name)
                );
                CREATE INDEX IF NOT EXISTS idx_meta_columns_table
                    ON meta_columns (connection_id, schema_name, table_name, ordinal_position);

                CREATE TABLE IF NOT EXISTS migration_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    row_index INTEGER NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    old_col_name TEXT NOT NULL,
                    old_col_type TEXT NOT NULL DEFAULT '',
                    old_col_comment TEXT NOT NULL DEFAULT '',
                    new_col_name TEXT NOT NULL,
                    new_col_type TEXT NOT NULL,
                    new_col_comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'UNCHECKED',
                    strategy TEXT NOT NULL DEFAULT 'DIRECT_ALTER',
                    ordinal_position INTEGER,
                    total_columns INTEGER,
                    db_current_col_name TEXT DEFAULT '',
                    db_current_type TEXT DEFAULT '',
                    db_current_comment TEXT DEFAULT '',
                    is_distribute_key INTEGER DEFAULT 0,
                    table_orientation TEXT DEFAULT '',
                    status_detail TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    generated_sql TEXT DEFAULT '',
                    executed_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cross_table_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    std_table_key TEXT NOT NULL UNIQUE,
                    std_schema_name TEXT NOT NULL DEFAULT 'public',
                    std_table_name TEXT NOT NULL,
                    std_table_comment TEXT NOT NULL DEFAULT '',
                    source_schema TEXT NOT NULL DEFAULT '',
                    source_table TEXT NOT NULL DEFAULT '',
                    target_schema TEXT NOT NULL DEFAULT '',
                    target_table TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cross_column_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    std_table_key TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    orig_col_name TEXT NOT NULL DEFAULT '',
                    orig_col_type TEXT NOT NULL DEFAULT '',
                    orig_col_comment TEXT NOT NULL DEFAULT '',
                    std_col_name TEXT NOT NULL,
                    std_col_type TEXT NOT NULL DEFAULT '',
                    std_col_comment TEXT NOT NULL DEFAULT '',
                    bound_source_col TEXT NOT NULL DEFAULT '',
                    bound_target_col TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_providers (
                    provider_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider_type TEXT NOT NULL DEFAULT 'OPENAI_COMPATIBLE',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    base_url TEXT NOT NULL DEFAULT '',
                    api_key TEXT NOT NULL DEFAULT '',
                    default_model TEXT NOT NULL DEFAULT '',
                    models_json TEXT NOT NULL DEFAULT '[]',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            self._ensure_schema_columns(conn)
            conn.commit()

    def register_table_schema(
        self,
        table_name: str,
        create_sql: str,
        expected_columns: List[Tuple[str, str]],
    ) -> None:
        """供业务插件模块幂等注册私有 SQLite 表结构并自动演进补齐新增列."""
        with self._lock, self._get_conn() as conn:
            if create_sql.strip():
                conn.executescript(create_sql)
            self._ensure_columns_for_table(conn, table_name, expected_columns)
            conn.commit()

    @contextlib.contextmanager
    def module_db(self):
        """供业务插件模块执行参数化 SQLite 读写事务的线程安全上下文."""
        with self._lock, self._get_conn() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _ensure_columns_for_table(
        conn: sqlite3.Connection,
        table_name: str,
        col_defs: List[Tuple[str, str]],
    ) -> None:
        existing = {
            row["name"].lower()
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if not existing:
            return
        for col_name, col_ddl in col_defs:
            if col_name.lower() not in existing:
                conn.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_ddl}"
                )

    @classmethod
    def _ensure_schema_columns(cls, conn: sqlite3.Connection) -> None:
        """幂等补齐平台表与模块表列定义，保障跨分支切换与版本回退前后向无损兼容."""
        expected_columns: Dict[str, List[Tuple[str, str]]] = {
            "execution_logs": [
                ("sql_text", "TEXT DEFAULT ''"),
            ],
            "connection_profiles": [
                ("driver_track", "TEXT NOT NULL DEFAULT 'NATIVE'"),
                ("db_type", "TEXT NOT NULL DEFAULT 'DWS'"),
                ("default_schema", "TEXT NOT NULL DEFAULT 'public'"),
                ("sslmode", "TEXT NOT NULL DEFAULT 'prefer'"),
                ("auth_mode", "TEXT NOT NULL DEFAULT 'LDAP'"),
                ("sqlite_path", "TEXT NOT NULL DEFAULT ''"),
                ("jdbc_driver_id", "TEXT NOT NULL DEFAULT ''"),
                ("jdbc_url", "TEXT NOT NULL DEFAULT ''"),
                ("jdbc_driver_class", "TEXT NOT NULL DEFAULT ''"),
                ("extra_props_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "jdbc_drivers": [
                ("driver_class_hint", "TEXT NOT NULL DEFAULT ''"),
                ("db_type_hint", "TEXT NOT NULL DEFAULT 'CUSTOM_JDBC'"),
                ("uploaded_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "meta_tables": [
                ("object_type", "TEXT NOT NULL DEFAULT 'TABLE'"),
                ("table_comment", "TEXT NOT NULL DEFAULT ''"),
                ("orientation", "TEXT NOT NULL DEFAULT 'ROW'"),
                ("with_clause", "TEXT NOT NULL DEFAULT ''"),
                ("distribute_clause", "TEXT NOT NULL DEFAULT ''"),
                ("distribute_keys_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("partition_clause", "TEXT NOT NULL DEFAULT ''"),
                ("partition_keys_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("primary_keys_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("penetrated_base_tables_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("row_count", "INTEGER NOT NULL DEFAULT 0"),
                ("raw_ddl", "TEXT NOT NULL DEFAULT ''"),
                ("view_definition", "TEXT NOT NULL DEFAULT ''"),
                ("columns_cached", "INTEGER NOT NULL DEFAULT 0"),
            ],
            "meta_columns": [
                ("normalized_type", "TEXT NOT NULL DEFAULT ''"),
                ("is_not_null", "INTEGER NOT NULL DEFAULT 0"),
                ("column_default", "TEXT"),
                ("column_comment", "TEXT NOT NULL DEFAULT ''"),
                ("is_primary_key", "INTEGER NOT NULL DEFAULT 0"),
                ("index_names_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("lineage_source", "TEXT NOT NULL DEFAULT ''"),
            ],
            "migration_items": [
                ("old_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("old_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("new_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("status", "TEXT NOT NULL DEFAULT 'UNCHECKED'"),
                ("strategy", "TEXT NOT NULL DEFAULT 'DIRECT_ALTER'"),
                ("ordinal_position", "INTEGER"),
                ("total_columns", "INTEGER"),
                ("db_current_col_name", "TEXT DEFAULT ''"),
                ("db_current_type", "TEXT DEFAULT ''"),
                ("db_current_comment", "TEXT DEFAULT ''"),
                ("is_distribute_key", "INTEGER DEFAULT 0"),
                ("table_orientation", "TEXT DEFAULT ''"),
                ("status_detail", "TEXT DEFAULT ''"),
                ("error_message", "TEXT DEFAULT ''"),
                ("generated_sql", "TEXT DEFAULT ''"),
                ("executed_at", "TEXT DEFAULT ''"),
            ],
            "cross_table_bindings": [
                ("std_schema_name", "TEXT NOT NULL DEFAULT 'public'"),
                ("std_table_comment", "TEXT NOT NULL DEFAULT ''"),
                ("source_schema", "TEXT NOT NULL DEFAULT ''"),
                ("source_table", "TEXT NOT NULL DEFAULT ''"),
                ("target_schema", "TEXT NOT NULL DEFAULT ''"),
                ("target_table", "TEXT NOT NULL DEFAULT ''"),
            ],
            "cross_column_bindings": [
                ("orig_col_name", "TEXT NOT NULL DEFAULT ''"),
                ("orig_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("orig_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("std_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("std_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("bound_source_col", "TEXT NOT NULL DEFAULT ''"),
                ("bound_target_col", "TEXT NOT NULL DEFAULT ''"),
            ],
            "llm_providers": [
                ("provider_type", "TEXT NOT NULL DEFAULT 'OPENAI_COMPATIBLE'"),
                ("enabled", "INTEGER NOT NULL DEFAULT 0"),
                ("base_url", "TEXT NOT NULL DEFAULT ''"),
                ("api_key", "TEXT NOT NULL DEFAULT ''"),
                ("default_model", "TEXT NOT NULL DEFAULT ''"),
                ("models_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("is_default", "INTEGER NOT NULL DEFAULT 0"),
                ("extra_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
        }
        for table_name, col_defs in expected_columns.items():
            cls._ensure_columns_for_table(conn, table_name, col_defs)

    # =========================================================================
    # 旧版状态库 (dws_migrator_state.db) 与旧版连接配置无损自动迁移
    # =========================================================================

    def _migrate_legacy_db_file_if_needed(self) -> None:
        """若当前状态库为 dataog_state.db 且同目录存在旧版 dws_migrator_state.db，自动无损迁移全部历史数据."""
        if os.path.basename(self.db_path) != "dataog_state.db":
            return
        legacy_path = os.path.join(os.path.dirname(self.db_path), "dws_migrator_state.db")
        if not os.path.isfile(legacy_path):
            return
        if self.get_meta("legacy_db_migrated_v2", False):
            return

        # 仅在新库尚未写入业务数据时迁移，防止覆盖新库已更新的状态
        with self._lock, self._get_conn() as dst_conn:
            has_new_data = (
                dst_conn.execute("SELECT COUNT(*) FROM migration_items").fetchone()[0] > 0
                or dst_conn.execute("SELECT COUNT(*) FROM cross_table_bindings").fetchone()[0] > 0
                or dst_conn.execute("SELECT COUNT(*) FROM connection_profiles").fetchone()[0] > 0
            )
            if has_new_data:
                dst_conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("legacy_db_migrated_v2", "true"),
                )
                dst_conn.commit()
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
                tables_to_copy = (
                    "app_meta",
                    "migration_items",
                    "execution_logs",
                    "cross_table_bindings",
                    "cross_column_bindings",
                )
                for tbl in tables_to_copy:
                    if tbl not in src_tables:
                        continue
                    dst_cols = {
                        r["name"]
                        for r in dst_conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                    }
                    rows = src_conn.execute(f"SELECT * FROM {tbl}").fetchall()
                    if not rows:
                        continue
                    common_cols = [c for c in rows[0].keys() if c in dst_cols]
                    if not common_cols:
                        continue
                    placeholders = ", ".join("?" for _ in common_cols)
                    col_list_sql = ", ".join(common_cols)
                    dst_conn.executemany(
                        f"INSERT OR REPLACE INTO {tbl} ({col_list_sql}) VALUES ({placeholders})",
                        [[r[c] for c in common_cols] for r in rows],
                    )
                dst_conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("legacy_db_migrated_v2", "true"),
                )
                dst_conn.commit()
                logger.info("已自动从历史状态库 [%s] 无损迁移数据至 [%s]", legacy_path, self.db_path)
            except Exception as exc:
                logger.warning("迁移旧版状态库 %s 时遇到非致命异常: %s", legacy_path, exc)
            finally:
                src_conn.close()

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
        now = self._now_str()
        for meta_key, profile_id, default_label, binding_meta_key in legacy_specs:
            raw_cfg = self.get_meta(meta_key, None)
            if not isinstance(raw_cfg, dict):
                continue
            host = str(raw_cfg.get("host") or "").strip()
            if not host:
                continue
            existing = self.get_connection_profile(profile_id)
            if existing is None:
                db_type = str(raw_cfg.get("db_type") or "DWS").strip().upper()
                default_port = (
                    10000
                    if db_type == "INCEPTOR"
                    else (5432 if db_type == "PGSQL" else 8000)
                )
                dbname = str(raw_cfg.get("dbname") or "").strip()
                profile = ConnectionProfile(
                    connection_id=profile_id,
                    name=f"{default_label} [{db_type} {host}/{dbname}]" if dbname else f"{default_label} [{db_type} {host}]",
                    driver_track="NATIVE",
                    db_type=db_type,
                    host=host,
                    port=int(raw_cfg.get("port") or default_port),
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
            if not self.get_meta(binding_meta_key, ""):
                self.save_meta(binding_meta_key, profile_id)

    # =========================================================================
    # 基础键值配置与日志中心
    # =========================================================================

    def save_meta(self, key: str, value: Any) -> None:
        val_str = json.dumps(value, ensure_ascii=False)
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, val_str),
            )
            conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def add_log(self, level: str, target: str, message: str, sql_text: str = "") -> None:
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO execution_logs (created_at, level, target, message, sql_text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now, level.upper(), target, message, sql_text),
            )
            conn.execute(
                """
                DELETE FROM execution_logs
                WHERE id NOT IN (
                    SELECT id FROM execution_logs ORDER BY id DESC LIMIT 1000
                )
                """
            )
            conn.commit()

    def list_logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_logs(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM execution_logs")
            conn.commit()

    # =========================================================================
    # 全局连接档案 (ConnectionProfile) 与自定义 JDBC 驱动包索引
    # =========================================================================

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> ConnectionProfile:
        try:
            extra_props = json.loads(row["extra_props_json"] or "{}")
            if not isinstance(extra_props, dict):
                extra_props = {}
        except Exception:
            extra_props = {}
        return ConnectionProfile(
            connection_id=row["connection_id"],
            name=row["name"],
            driver_track=row["driver_track"] or "NATIVE",
            db_type=row["db_type"] or "DWS",
            host=row["host"] or "",
            port=int(row["port"] or 0),
            dbname=row["dbname"] or "",
            default_schema=row["default_schema"] or "public",
            user=row["user"] or "",
            password=row["password"] or "",
            sslmode=row["sslmode"] or "prefer",
            auth_mode=row["auth_mode"] or "LDAP",
            sqlite_path=row["sqlite_path"] or "",
            jdbc_driver_id=row["jdbc_driver_id"] or "",
            jdbc_url=row["jdbc_url"] or "",
            jdbc_driver_class=row["jdbc_driver_class"] or "",
            extra_props=extra_props,
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def list_connection_profiles(self) -> List[ConnectionProfile]:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM connection_profiles ORDER BY updated_at DESC, name ASC"
            ).fetchall()
            return [self._row_to_profile(r) for r in rows]

    def get_connection_profile(self, connection_id: str) -> Optional[ConnectionProfile]:
        if not connection_id:
            return None
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM connection_profiles WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            return self._row_to_profile(row) if row else None

    def upsert_connection_profile(self, profile: ConnectionProfile) -> ConnectionProfile:
        now = self._now_str()
        created_at = profile.created_at or now
        updated_at = now
        extra_json = json.dumps(profile.extra_props or {}, ensure_ascii=False)
        with self._lock, self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO connection_profiles (
                    connection_id, name, driver_track, db_type,
                    host, port, dbname, default_schema, user, password,
                    sslmode, auth_mode, sqlite_path,
                    jdbc_driver_id, jdbc_url, jdbc_driver_class,
                    extra_props_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    name = excluded.name,
                    driver_track = excluded.driver_track,
                    db_type = excluded.db_type,
                    host = excluded.host,
                    port = excluded.port,
                    dbname = excluded.dbname,
                    default_schema = excluded.default_schema,
                    user = excluded.user,
                    password = excluded.password,
                    sslmode = excluded.sslmode,
                    auth_mode = excluded.auth_mode,
                    sqlite_path = excluded.sqlite_path,
                    jdbc_driver_id = excluded.jdbc_driver_id,
                    jdbc_url = excluded.jdbc_url,
                    jdbc_driver_class = excluded.jdbc_driver_class,
                    extra_props_json = excluded.extra_props_json,
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
                    profile.user,
                    profile.password,
                    profile.sslmode,
                    profile.auth_mode,
                    profile.sqlite_path,
                    profile.jdbc_driver_id,
                    profile.jdbc_url,
                    profile.jdbc_driver_class,
                    extra_json,
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()
        return self.get_connection_profile(profile.connection_id) or profile

    def delete_connection_profile(self, connection_id: str) -> bool:
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM connection_profiles WHERE connection_id = ?",
                (connection_id,),
            )
            conn.execute("DELETE FROM meta_schemas WHERE connection_id = ?", (connection_id,))
            conn.execute("DELETE FROM meta_tables WHERE connection_id = ?", (connection_id,))
            conn.execute("DELETE FROM meta_columns WHERE connection_id = ?", (connection_id,))
            conn.commit()
            return cur.rowcount > 0

    def list_jdbc_drivers(self) -> List[DriverPackage]:
        with self._lock, self._get_conn() as conn:
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
        with self._lock, self._get_conn() as conn:
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
        with self._lock, self._get_conn() as conn:
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
        with self._lock, self._get_conn() as conn:
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
            return {
                "connection_id": connection_id,
                "schemas": [dict(r) for r in s_rows],
                "tables": [dict(r) for r in t_rows],
            }

    def get_cached_table_meta(
        self, connection_id: str, schema_name: str, table_name: str
    ) -> Optional[TableMeta]:
        with self._lock, self._get_conn() as conn:
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
    # 单库批量贯标模块 (standard_migrator) 状态接口
    # =========================================================================

    def replace_all_items(
        self,
        items: List[Dict[str, Any]],
        source_filename: str,
        preserve_history: bool = True,
    ) -> List[Dict[str, Any]]:
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            history_map: Dict[str, Dict[str, Any]] = {}
            if preserve_history:
                existing_rows = conn.execute("SELECT * FROM migration_items").fetchall()
                for r in existing_rows:
                    key = f"{r['schema_name']}.{r['table_name']}.{r['old_col_name']}->{r['new_col_name']}".lower()
                    history_map[key] = dict(r)

            conn.execute("DELETE FROM migration_items")
            rows_to_insert = []
            quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
            for idx, item in enumerate(items, start=1):
                schema_name = str(item.get("schema_name", "")).strip(quote_chars) or "public"
                table_name = str(item.get("table_name", "")).strip(quote_chars)
                old_col_name = str(item.get("old_col_name", "")).strip(quote_chars)
                old_col_type = str(item.get("old_col_type", "")).strip()
                old_col_comment = str(item.get("old_col_comment", "")).strip()
                new_col_name = str(item.get("new_col_name", "")).strip(quote_chars) or old_col_name
                new_col_type = str(item.get("new_col_type", "")).strip() or old_col_type
                new_col_comment = str(item.get("new_col_comment", "")).strip()

                key = f"{schema_name}.{table_name}.{old_col_name}->{new_col_name}".lower()
                prev = history_map.get(key)

                status = item.get("status") or (prev["status"] if prev else "UNCHECKED")
                status_detail = item.get("status_detail") or (
                    prev["status_detail"]
                    if prev
                    else "已导入清单，等待连接数据库同步元数据状态"
                )
                error_message = prev["error_message"] if prev else ""
                executed_at = prev["executed_at"] if prev else ""
                ordinal_pos = item.get("ordinal_position") or (
                    prev["ordinal_position"] if prev else None
                )
                total_cols = item.get("total_columns") or (
                    prev["total_columns"] if prev else None
                )
                db_col = item.get("db_current_col_name") or (
                    prev["db_current_col_name"] if prev else ""
                )
                db_type = item.get("db_current_type") or (
                    prev["db_current_type"] if prev else ""
                )
                db_comment = item.get("db_current_comment") or (
                    prev["db_current_comment"] if prev else ""
                )
                is_dist = (
                    1
                    if item.get("is_distribute_key")
                    else (prev["is_distribute_key"] if prev else 0)
                )
                orientation = item.get("table_orientation") or (
                    prev["table_orientation"] if prev else ""
                )

                rows_to_insert.append(
                    (
                        item.get("row_index", idx),
                        schema_name,
                        table_name,
                        old_col_name,
                        old_col_type,
                        old_col_comment,
                        new_col_name,
                        new_col_type,
                        new_col_comment,
                        status,
                        "DIRECT_ALTER",
                        ordinal_pos,
                        total_cols,
                        db_col,
                        db_type,
                        db_comment,
                        is_dist,
                        orientation,
                        status_detail,
                        error_message,
                        executed_at,
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO migration_items (
                    row_index, schema_name, table_name,
                    old_col_name, old_col_type, old_col_comment,
                    new_col_name, new_col_type, new_col_comment,
                    status, strategy, ordinal_position, total_columns,
                    db_current_col_name, db_current_type, db_current_comment,
                    is_distribute_key, table_orientation,
                    status_detail, error_message, executed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            conn.commit()
        self.save_meta("current_excel_name", source_filename)
        self.save_meta("imported_at", now)
        self.add_log(
            "INFO",
            "清单载入",
            f"成功载入清单 [{source_filename}]，共 {len(items)} 条字段规则。",
        )
        return self.list_items()

    def list_items(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM migration_items ORDER BY row_index ASC, id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM migration_items WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_item_sync(
        self,
        item_id: int,
        *,
        status: str,
        status_detail: str,
        schema_name: Optional[str] = None,
        table_name: Optional[str] = None,
        ordinal_position: Optional[int] = None,
        total_columns: Optional[int] = None,
        db_current_col_name: str = "",
        db_current_type: str = "",
        db_current_comment: str = "",
        is_distribute_key: bool = False,
        table_orientation: str = "",
        generated_sql: str = "",
        clear_error: bool = False,
    ) -> None:
        self.batch_update_items_sync(
            [
                {
                    "item_id": item_id,
                    "status": status,
                    "status_detail": status_detail,
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "ordinal_position": ordinal_position,
                    "total_columns": total_columns,
                    "db_current_col_name": db_current_col_name,
                    "db_current_type": db_current_type,
                    "db_current_comment": db_current_comment,
                    "is_distribute_key": is_distribute_key,
                    "table_orientation": table_orientation,
                    "generated_sql": generated_sql,
                    "clear_error": clear_error,
                }
            ]
        )

    def batch_update_items_sync(self, updates: List[Dict[str, Any]]) -> None:
        if not updates:
            return
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            for u in updates:
                item_id = u["item_id"]
                status = u["status"]
                status_detail = u["status_detail"]
                ordinal_position = u.get("ordinal_position")
                total_columns = u.get("total_columns")
                db_current_col_name = u.get("db_current_col_name", "")
                db_current_type = u.get("db_current_type", "")
                db_current_comment = u.get("db_current_comment", "")
                is_dist = 1 if u.get("is_distribute_key") else 0
                table_orientation = u.get("table_orientation", "")
                generated_sql = u.get("generated_sql", "")
                clear_error = bool(u.get("clear_error", False))
                schema_name = u.get("schema_name")
                table_name = u.get("schema_table_override") or u.get("table_name")

                if schema_name and table_name:
                    conn.execute(
                        """
                        UPDATE migration_items
                        SET schema_name = ?,
                            table_name = ?,
                            status = ?,
                            status_detail = ?,
                            ordinal_position = ?,
                            total_columns = ?,
                            db_current_col_name = ?,
                            db_current_type = ?,
                            db_current_comment = ?,
                            is_distribute_key = ?,
                            table_orientation = ?,
                            generated_sql = ?,
                            error_message = CASE WHEN ? THEN '' ELSE error_message END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            schema_name,
                            table_name,
                            status,
                            status_detail,
                            ordinal_position,
                            total_columns,
                            db_current_col_name,
                            db_current_type,
                            db_current_comment,
                            is_dist,
                            table_orientation,
                            generated_sql,
                            1 if clear_error else 0,
                            now,
                            item_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE migration_items
                        SET status = ?,
                            status_detail = ?,
                            ordinal_position = ?,
                            total_columns = ?,
                            db_current_col_name = ?,
                            db_current_type = ?,
                            db_current_comment = ?,
                            is_distribute_key = ?,
                            table_orientation = ?,
                            generated_sql = ?,
                            error_message = CASE WHEN ? THEN '' ELSE error_message END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            status_detail,
                            ordinal_position,
                            total_columns,
                            db_current_col_name,
                            db_current_type,
                            db_current_comment,
                            is_dist,
                            table_orientation,
                            generated_sql,
                            1 if clear_error else 0,
                            now,
                            item_id,
                        ),
                    )
            conn.commit()

    def update_item_execution(
        self,
        item_id: int,
        *,
        status: str,
        strategy: str,
        status_detail: str,
        error_message: str = "",
        generated_sql: str = "",
        ordinal_position: Optional[int] = None,
        db_current_col_name: Optional[str] = None,
        db_current_type: Optional[str] = None,
        db_current_comment: Optional[str] = None,
    ) -> None:
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            current = conn.execute(
                "SELECT * FROM migration_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not current:
                return
            conn.execute(
                """
                UPDATE migration_items
                SET status = ?,
                    strategy = ?,
                    status_detail = ?,
                    error_message = ?,
                    generated_sql = ?,
                    ordinal_position = ?,
                    db_current_col_name = ?,
                    db_current_type = ?,
                    db_current_comment = ?,
                    executed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    strategy,
                    status_detail,
                    error_message,
                    generated_sql or current["generated_sql"],
                    ordinal_position if ordinal_position is not None else current["ordinal_position"],
                    db_current_col_name if db_current_col_name is not None else current["db_current_col_name"],
                    db_current_type if db_current_type is not None else current["db_current_type"],
                    db_current_comment if db_current_comment is not None else current["db_current_comment"],
                    now,
                    now,
                    item_id,
                ),
            )
            conn.commit()

    def clear_items(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM migration_items")
            conn.commit()
        self.save_meta("current_excel_name", "未导入")
        self.save_meta("imported_at", "")

    def get_summary(self) -> Dict[str, int]:
        items = self.list_items()
        summary = {
            "total": len(items),
            "completed": 0,
            "pending": 0,
            "partial": 0,
            "failed": 0,
            "missing": 0,
            "unchecked": 0,
            "rebuild_required": 0,
        }
        for item in items:
            st = item["status"]
            if st == "COMPLETED":
                summary["completed"] += 1
            elif st == "PENDING":
                summary["pending"] += 1
            elif st == "PARTIAL":
                summary["partial"] += 1
            elif st == "FAILED":
                summary["failed"] += 1
            elif st in ("MISSING_TABLE", "MISSING_COLUMN"):
                summary["missing"] += 1
            else:
                summary["unchecked"] += 1
            if st != "COMPLETED" and (
                item.get("strategy") == "TABLE_REBUILD"
                or bool(item.get("is_distribute_key"))
                or str(item.get("table_orientation") or "").upper() == "COLUMN"
            ):
                summary["rebuild_required"] += 1
        return summary

    # =========================================================================
    # 跨环境三栏数标比对模块 (cross_comparator) 状态接口
    # =========================================================================

    def replace_cross_standards(
        self,
        items: List[Dict[str, Any]],
        source_filename: str,
        preserve_bindings: bool = True,
    ) -> List[Dict[str, Any]]:
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        with self._lock, self._get_conn() as conn:
            prev_tables: Dict[str, Dict[str, Any]] = {}
            prev_cols: Dict[str, Dict[str, Any]] = {}
            if preserve_bindings:
                for r in conn.execute("SELECT * FROM cross_table_bindings").fetchall():
                    prev_tables[r["std_table_key"].lower()] = dict(r)
                for r in conn.execute("SELECT * FROM cross_column_bindings").fetchall():
                    k = f"{r['std_table_key']}::{r['orig_col_name']}->{r['std_col_name']}".lower()
                    prev_cols[k] = dict(r)
                    k2 = f"{r['std_table_key']}::{r['std_col_name']}".lower()
                    prev_cols.setdefault(k2, dict(r))

            conn.execute("DELETE FROM cross_table_bindings")
            conn.execute("DELETE FROM cross_column_bindings")

            table_rows_map: Dict[str, Tuple[str, str, str, str, str, str, str, str]] = {}
            col_rows_to_insert = []
            per_table_idx: Dict[str, int] = {}

            for item in items:
                schema_name = str(item.get("schema_name") or item.get("std_schema_name") or "").strip(quote_chars) or "public"
                table_name = str(item.get("table_name") or item.get("std_table_name") or "").strip(quote_chars)
                if not table_name:
                    continue
                t_key = table_name.lower()

                old_col_name = str(item.get("old_col_name") or item.get("orig_col_name") or "").strip(quote_chars)
                old_col_type = str(item.get("old_col_type") or item.get("orig_col_type") or "").strip()
                old_col_comment = str(item.get("old_col_comment") or item.get("orig_col_comment") or "").strip()
                std_col_name = str(item.get("new_col_name") or item.get("std_col_name") or "").strip(quote_chars) or old_col_name
                std_col_type = str(item.get("new_col_type") or item.get("std_col_type") or "").strip() or old_col_type
                std_col_comment = str(item.get("new_col_comment") or item.get("std_col_comment") or "").strip() or old_col_comment

                if t_key not in table_rows_map:
                    prev_t = prev_tables.get(t_key)
                    src_schema = str(item.get("source_schema") or (prev_t["source_schema"] if prev_t else schema_name)).strip(quote_chars)
                    src_table = str(item.get("source_table") or (prev_t["source_table"] if prev_t else table_name)).strip(quote_chars)
                    tgt_schema = str(item.get("target_schema") or (prev_t["target_schema"] if prev_t else schema_name)).strip(quote_chars)
                    tgt_table = str(item.get("target_table") or (prev_t["target_table"] if prev_t else table_name)).strip(quote_chars)
                    t_comment = str(item.get("std_table_comment") or (prev_t["std_table_comment"] if prev_t else "")).strip()
                    table_rows_map[t_key] = (
                        t_key,
                        schema_name,
                        table_name,
                        t_comment,
                        src_schema,
                        src_table,
                        tgt_schema,
                        tgt_table,
                    )

                per_table_idx[t_key] = per_table_idx.get(t_key, 0) + 1
                row_idx = int(item.get("row_index") or per_table_idx[t_key])

                c_key1 = f"{t_key}::{old_col_name}->{std_col_name}".lower()
                c_key2 = f"{t_key}::{std_col_name}".lower()
                prev_c = prev_cols.get(c_key1) or prev_cols.get(c_key2)

                bound_src = str(
                    item.get("bound_source_col")
                    or (prev_c["bound_source_col"] if prev_c else "")
                    or old_col_name
                    or std_col_name
                ).strip(quote_chars)
                bound_tgt = str(
                    item.get("bound_target_col")
                    or (prev_c["bound_target_col"] if prev_c else "")
                    or std_col_name
                    or old_col_name
                ).strip(quote_chars)

                col_rows_to_insert.append(
                    (
                        t_key,
                        row_idx,
                        old_col_name,
                        old_col_type,
                        old_col_comment,
                        std_col_name,
                        std_col_type,
                        std_col_comment,
                        bound_src,
                        bound_tgt,
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO cross_table_bindings (
                    std_table_key, std_schema_name, std_table_name, std_table_comment,
                    source_schema, source_table, target_schema, target_table, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(*vals, now) for vals in table_rows_map.values()],
            )
            conn.executemany(
                """
                INSERT INTO cross_column_bindings (
                    std_table_key, row_index,
                    orig_col_name, orig_col_type, orig_col_comment,
                    std_col_name, std_col_type, std_col_comment,
                    bound_source_col, bound_target_col, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                col_rows_to_insert,
            )
            conn.commit()

        self.save_meta("cross_standard_filename", source_filename)
        self.save_meta("cross_standard_imported_at", now)
        return self.list_cross_tables()

    def list_cross_tables(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            t_rows = conn.execute(
                """
                SELECT
                    t.*,
                    COUNT(c.id) AS column_count
                FROM cross_table_bindings t
                LEFT JOIN cross_column_bindings c ON c.std_table_key = t.std_table_key
                GROUP BY t.id
                ORDER BY t.id ASC
                """
            ).fetchall()
            return [dict(r) for r in t_rows]

    def get_cross_table_binding(self, std_table_key: str) -> Optional[Dict[str, Any]]:
        key_lower = (std_table_key or "").strip().lower()
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM cross_table_bindings WHERE std_table_key = ?",
                (key_lower,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_cross_table_binding(
        self,
        std_table_name: str,
        *,
        std_schema_name: str = "public",
        source_schema: str = "",
        source_table: str = "",
        target_schema: str = "",
        target_table: str = "",
        std_table_comment: str = "",
    ) -> Dict[str, Any]:
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        clean_std_table = str(std_table_name or "").strip(quote_chars)
        if not clean_std_table:
            raise ValueError("标准表名不能为空")
        t_key = clean_std_table.lower()
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM cross_table_bindings WHERE std_table_key = ?",
                (t_key,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE cross_table_bindings
                    SET source_schema = ?,
                        source_table = ?,
                        target_schema = ?,
                        target_table = ?,
                        std_table_comment = CASE WHEN ? != '' THEN ? ELSE std_table_comment END,
                        updated_at = ?
                    WHERE std_table_key = ?
                    """,
                    (
                        str(source_schema).strip(quote_chars),
                        str(source_table).strip(quote_chars),
                        str(target_schema).strip(quote_chars),
                        str(target_table).strip(quote_chars),
                        std_table_comment,
                        std_table_comment,
                        now,
                        t_key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO cross_table_bindings (
                        std_table_key, std_schema_name, std_table_name, std_table_comment,
                        source_schema, source_table, target_schema, target_table, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        t_key,
                        str(std_schema_name or "public").strip(quote_chars),
                        clean_std_table,
                        std_table_comment,
                        str(source_schema).strip(quote_chars) or str(std_schema_name or "public").strip(quote_chars),
                        str(source_table).strip(quote_chars) or clean_std_table,
                        str(target_schema).strip(quote_chars) or str(std_schema_name or "public").strip(quote_chars),
                        str(target_table).strip(quote_chars) or clean_std_table,
                        now,
                    ),
                )
            conn.commit()
        return self.get_cross_table_binding(t_key) or {}

    def list_cross_columns(self, std_table_key: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            if std_table_key:
                rows = conn.execute(
                    """
                    SELECT * FROM cross_column_bindings
                    WHERE std_table_key = ?
                    ORDER BY row_index ASC, id ASC
                    """,
                    (std_table_key.strip().lower(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cross_column_bindings ORDER BY std_table_key ASC, row_index ASC, id ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def batch_update_cross_column_bindings(
        self, updates: List[Dict[str, Any]]
    ) -> None:
        if not updates:
            return
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        with self._lock, self._get_conn() as conn:
            for u in updates:
                cid = int(u["id"])
                bound_src = str(u.get("bound_source_col", "")).strip(quote_chars)
                bound_tgt = str(u.get("bound_target_col", "")).strip(quote_chars)
                conn.execute(
                    """
                    UPDATE cross_column_bindings
                    SET bound_source_col = ?,
                        bound_target_col = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (bound_src, bound_tgt, now, cid),
                )
            conn.commit()

    def upsert_cross_table_column_bindings(
        self, std_table_key: str, column_bindings: List[Dict[str, Any]]
    ) -> None:
        """按 id 或 (std_table_key, std_col_name) 幂等更新或插入三方字段桥接关系（兼容导入数标与在线直连表模式）."""
        t_key = (std_table_key or "").strip().lower()
        if not t_key or not column_bindings:
            return
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        with self._lock, self._get_conn() as conn:
            existing_rows = conn.execute(
                "SELECT id, std_col_name FROM cross_column_bindings WHERE std_table_key = ?",
                (t_key,),
            ).fetchall()
            by_std_col = {
                str(r["std_col_name"] or "").strip(quote_chars).lower(): int(r["id"])
                for r in existing_rows
            }
            next_row_idx = len(existing_rows) + 1
            for item in column_bindings:
                raw_id = int(item.get("id", 0) or 0)
                std_col = str(item.get("std_col_name") or "").strip(quote_chars)
                bound_src = str(item.get("bound_source_col") or "").strip(quote_chars)
                bound_tgt = str(item.get("bound_target_col") or "").strip(quote_chars)
                target_id = raw_id if raw_id > 0 else by_std_col.get(std_col.lower(), 0)
                if target_id > 0:
                    conn.execute(
                        """
                        UPDATE cross_column_bindings
                        SET bound_source_col = ?,
                            bound_target_col = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (bound_src, bound_tgt, now, target_id),
                    )
                elif std_col:
                    orig_col = str(item.get("orig_col_name") or std_col).strip(quote_chars)
                    std_type = str(item.get("std_col_type") or item.get("orig_col_type") or "").strip()
                    std_comm = str(item.get("std_col_comment") or item.get("orig_col_comment") or "").strip()
                    cur = conn.execute(
                        """
                        INSERT INTO cross_column_bindings (
                            std_table_key, row_index,
                            orig_col_name, orig_col_type, orig_col_comment,
                            std_col_name, std_col_type, std_col_comment,
                            bound_source_col, bound_target_col, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            t_key,
                            next_row_idx,
                            orig_col,
                            std_type,
                            std_comm,
                            std_col,
                            std_type,
                            std_comm,
                            bound_src,
                            bound_tgt,
                            now,
                        ),
                    )
                    by_std_col[std_col.lower()] = int(cur.lastrowid or 0)
                    next_row_idx += 1
            conn.commit()

    def clear_cross_standards(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM cross_table_bindings")
            conn.execute("DELETE FROM cross_column_bindings")
            conn.commit()
        self.save_meta("cross_standard_filename", "未导入")
        self.save_meta("cross_standard_imported_at", "")

    def export_cross_bindings_bundle(self) -> Dict[str, Any]:
        src_conn = dict(self.get_meta("cross_source_conn", {}) or {})
        tgt_conn = dict(self.get_meta("cross_target_conn", {}) or {})
        src_conn.pop("password", None)
        tgt_conn.pop("password", None)

        return {
            "bundle_version": "2.0",
            "exported_at": self._now_str(),
            "standard_filename": self.get_meta("cross_standard_filename", "未导入"),
            "source_connection_id": self.get_meta("cross_source_connection_id", ""),
            "target_connection_id": self.get_meta("cross_target_connection_id", ""),
            "source_connection": src_conn,
            "target_connection": tgt_conn,
            "table_bindings": self.list_cross_tables(),
            "column_bindings": self.list_cross_columns(),
        }

    def import_cross_bindings_bundle(
        self, bundle: Dict[str, Any], filename: str = "bindings_bundle.json"
    ) -> Dict[str, Any]:
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        table_bindings = bundle.get("table_bindings") or []
        column_bindings = bundle.get("column_bindings") or []
        if not table_bindings and not column_bindings:
            raise ValueError("导入的绑定配置包中未包含有效的 table_bindings 或 column_bindings 数据")

        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM cross_table_bindings")
            conn.execute("DELETE FROM cross_column_bindings")

            t_rows = []
            seen_keys = set()
            for t in table_bindings:
                t_name = str(t.get("std_table_name") or t.get("table_name") or "").strip(quote_chars)
                if not t_name:
                    continue
                t_key = str(t.get("std_table_key") or t_name).strip(quote_chars).lower()
                if t_key in seen_keys:
                    continue
                seen_keys.add(t_key)
                s_schema = str(t.get("std_schema_name") or "public").strip(quote_chars)
                t_rows.append(
                    (
                        t_key,
                        s_schema,
                        t_name,
                        str(t.get("std_table_comment") or "").strip(),
                        str(t.get("source_schema") or s_schema).strip(quote_chars),
                        str(t.get("source_table") or t_name).strip(quote_chars),
                        str(t.get("target_schema") or s_schema).strip(quote_chars),
                        str(t.get("target_table") or t_name).strip(quote_chars),
                        now,
                    )
                )

            c_rows = []
            for idx, c in enumerate(column_bindings, start=1):
                t_key = str(c.get("std_table_key") or c.get("table_name") or "").strip(quote_chars).lower()
                if not t_key:
                    continue
                if t_key not in seen_keys:
                    seen_keys.add(t_key)
                    t_rows.append(
                        (t_key, "public", t_key, "", "public", t_key, "public", t_key, now)
                    )
                orig_name = str(c.get("orig_col_name") or c.get("old_col_name") or "").strip(quote_chars)
                std_name = str(c.get("std_col_name") or c.get("new_col_name") or orig_name).strip(quote_chars)
                c_rows.append(
                    (
                        t_key,
                        int(c.get("row_index") or idx),
                        orig_name,
                        str(c.get("orig_col_type") or c.get("old_col_type") or "").strip(),
                        str(c.get("orig_col_comment") or c.get("old_col_comment") or "").strip(),
                        std_name,
                        str(c.get("std_col_type") or c.get("new_col_type") or "").strip(),
                        str(c.get("std_col_comment") or c.get("new_col_comment") or "").strip(),
                        str(c.get("bound_source_col") or orig_name or std_name).strip(quote_chars),
                        str(c.get("bound_target_col") or std_name or orig_name).strip(quote_chars),
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO cross_table_bindings (
                    std_table_key, std_schema_name, std_table_name, std_table_comment,
                    source_schema, source_table, target_schema, target_table, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                t_rows,
            )
            conn.executemany(
                """
                INSERT INTO cross_column_bindings (
                    std_table_key, row_index,
                    orig_col_name, orig_col_type, orig_col_comment,
                    std_col_name, std_col_type, std_col_comment,
                    bound_source_col, bound_target_col, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                c_rows,
            )
            conn.commit()

        std_fn = str(bundle.get("standard_filename") or filename)
        self.save_meta("cross_standard_filename", std_fn)
        self.save_meta("cross_standard_imported_at", now)
        return {
            "table_count": len(t_rows),
            "column_count": len(c_rows),
            "standard_filename": std_fn,
        }

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
        with self._lock, self._get_conn() as conn:
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
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM llm_providers WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return self._row_to_llm_provider(row) if row else None

    def upsert_llm_provider(self, profile: LLMProviderProfile) -> LLMProviderProfile:
        now = self._now_str()
        models_json = json.dumps(list(profile.models or []), ensure_ascii=False)
        extra_json = json.dumps(dict(profile.extra or {}), ensure_ascii=False)
        with self._lock, self._get_conn() as conn:
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
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM llm_providers WHERE provider_id = ?",
                (provider_id,),
            )
            conn.commit()
            return cur.rowcount > 0

