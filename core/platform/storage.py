"""Data.O.G 三层分离状态存储与无损迁移引擎 (Private Vault + Shareable Team Workspace).

核心架构（v2.7.0 三层分离）：
1. Layer 2 本地私有保险箱 (`private_db_path`，位于混淆的 OS 用户私有目录)：
   - 存放仅限本机/个人的敏感与运行态数据：`app_meta`（离线激活码、锁定标记、单调时钟密封、外观配置、工作区索引）、
     `workspaces`（本地托管工作区清单）、`llm_providers`（AI API Key）、`connection_credentials`（个人数据库账号密码库，
     支持 `connection_id` 与 `(db_type, host, port, dbname)` / `jdbc_url` 指纹双通道自动匹配）、
     `jdbc_drivers`（自定义 `.jar` 索引）、`execution_logs`（本地执行日志）以及 `explorer_sessions`/`explorer_messages`（个人 AI 探查历史）；
2. Layer 3 团队可共享工作区 (`workspace_db_path`，标准 `.dataog.db` 文件)：
   - 纯静态/协同业务数据，结构上彻底不含任何密码、API Key、激活码或机器绑定信息：
     `workspace_meta`、`connection_topology`（无 `user`/`password` 列）、`standard_dictionary`、
     `field_standard_bindings`、`cross_table_bindings`、`cross_column_bindings`、`migration_items`、
     `meta_schemas`、`meta_tables`、`meta_columns`；
3. 向下兼容单库路径模式：当调用方（如历史单元测试）显式传入单个 `db_path` 且未指定 `workspace_db_path` 时，
   自动在同一文件内承载全部表并保持 100% API 兼容；同时导出 `.dataog.db` 时始终按白名单生成零密钥纯净工作区文件；
4. 启动无损迁移：自动检测旧版 `<exe_dir>/data/dataog_state.db` / `dws_migrator_state.db`、`assets/`、`drivers/` 及 `.sys_clock_anchor`，
   一次幂等无损拆分迁移至私有保险箱与默认共享工作区。
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.platform.dialects import item_requires_table_rebuild

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
from core.platform.storage_paths import (
    decode_obfuscated_anchor,
    encode_obfuscated_anchor,
    redact_private_storage_paths,
)

logger = logging.getLogger("dataog")

# 明确路由至共享工作区 (workspace_meta) 的纯业务元数据键白名单；其余全部归属本地私有库 (app_meta)
WORKSPACE_META_KEYS = frozenset(
    {
        "workspace_format_version",
        "workspace_name",
        "workspace_description",
        "std_dict_filename",
        "std_dict_imported_at",
        "current_excel_name",
        "imported_at",
        "cross_standard_filename",
        "cross_standard_imported_at",
        "migrator_connection_id",
        "cross_source_connection_id",
        "cross_target_connection_id",
    }
)

# 仅存在于 Layer 2 私有保险箱的表清单（严禁出现在共享工作区或被合并读取）
PRIVATE_ONLY_TABLES: Tuple[str, ...] = (
    "app_meta",
    "workspaces",
    "llm_providers",
    "connection_credentials",
    "jdbc_drivers",
    "execution_logs",
    "explorer_sessions",
    "explorer_messages",
)

# 仅存在于 Layer 3 共享工作区的纯业务表白名单
WORKSPACE_BUSINESS_TABLES: Tuple[str, ...] = (
    "workspace_meta",
    "connection_topology",
    "standard_dictionary",
    "field_standard_bindings",
    "cross_table_bindings",
    "cross_column_bindings",
    "migration_items",
    "meta_schemas",
    "meta_tables",
    "meta_columns",
)

PRIVATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    target TEXT NOT NULL,
    message TEXT NOT NULL,
    sql_text TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS connection_credentials (
    cred_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL DEFAULT '',
    endpoint_fingerprint TEXT NOT NULL DEFAULT '',
    db_type TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 0,
    dbname TEXT NOT NULL DEFAULT '',
    jdbc_url TEXT NOT NULL DEFAULT '',
    user TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    sslmode TEXT NOT NULL DEFAULT 'prefer',
    auth_mode TEXT NOT NULL DEFAULT 'LDAP',
    sqlite_path TEXT NOT NULL DEFAULT '',
    jdbc_driver_id TEXT NOT NULL DEFAULT '',
    extra_secrets_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_conn_cred_cid
    ON connection_credentials (connection_id);
CREATE INDEX IF NOT EXISTS idx_conn_cred_fp
    ON connection_credentials (endpoint_fingerprint);

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

WORKSPACE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspace_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connection_topology (
    connection_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    driver_track TEXT NOT NULL DEFAULT 'NATIVE',
    db_type TEXT NOT NULL DEFAULT 'DWS',
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 8000,
    dbname TEXT NOT NULL DEFAULT '',
    default_schema TEXT NOT NULL DEFAULT 'public',
    sslmode TEXT NOT NULL DEFAULT 'prefer',
    auth_mode TEXT NOT NULL DEFAULT 'LDAP',
    sqlite_path TEXT NOT NULL DEFAULT '',
    jdbc_driver_id TEXT NOT NULL DEFAULT '',
    jdbc_url TEXT NOT NULL DEFAULT '',
    jdbc_driver_class TEXT NOT NULL DEFAULT '',
    extra_props_json TEXT NOT NULL DEFAULT '{}',
    standard_format TEXT NOT NULL DEFAULT 'AUTO',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
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
    connection_id TEXT NOT NULL DEFAULT '',
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
    source_std_code TEXT NOT NULL DEFAULT '',
    target_std_code TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cross_col_tbl_key
    ON cross_column_bindings (std_table_key, row_index);

CREATE TABLE IF NOT EXISTS standard_dictionary (
    std_code TEXT PRIMARY KEY,
    reg_category TEXT NOT NULL DEFAULT '',
    cat_l1 TEXT NOT NULL DEFAULT '',
    cat_l2 TEXT NOT NULL DEFAULT '',
    cat_l3 TEXT NOT NULL DEFAULT '',
    cn_name TEXT NOT NULL DEFAULT '',
    cn_alias TEXT NOT NULL DEFAULT '',
    en_name TEXT NOT NULL DEFAULT '',
    en_abbr TEXT NOT NULL DEFAULT '',
    biz_definition TEXT NOT NULL DEFAULT '',
    scope_desc TEXT NOT NULL DEFAULT '',
    biz_rule TEXT NOT NULL DEFAULT '',
    max_length TEXT NOT NULL DEFAULT '',
    min_scale TEXT NOT NULL DEFAULT '',
    ref_code_no TEXT NOT NULL DEFAULT '',
    ref_code_name TEXT NOT NULL DEFAULT '',
    ref_code_detail TEXT NOT NULL DEFAULT '',
    security_level TEXT NOT NULL DEFAULT '',
    ref_std_no TEXT NOT NULL DEFAULT '',
    ref_std_name TEXT NOT NULL DEFAULT '',
    domain_name TEXT NOT NULL DEFAULT '',
    domain_code TEXT NOT NULL DEFAULT '',
    data_category TEXT NOT NULL DEFAULT '',
    data_format TEXT NOT NULL DEFAULT '',
    value_range TEXT NOT NULL DEFAULT '',
    source_system TEXT NOT NULL DEFAULT '',
    ddm_catalog TEXT NOT NULL DEFAULT '',
    is_standard_dict TEXT NOT NULL DEFAULT '',
    version_no TEXT NOT NULL DEFAULT '',
    remark TEXT NOT NULL DEFAULT '',
    type_dws TEXT NOT NULL DEFAULT '',
    type_dw_mart TEXT NOT NULL DEFAULT '',
    type_mysql TEXT NOT NULL DEFAULT '',
    type_oracle TEXT NOT NULL DEFAULT '',
    type_dameng TEXT NOT NULL DEFAULT '',
    type_goldendb TEXT NOT NULL DEFAULT '',
    type_ob_oracle TEXT NOT NULL DEFAULT '',
    type_ob_mysql TEXT NOT NULL DEFAULT '',
    type_argodb TEXT NOT NULL DEFAULT '',
    type_oceanbase TEXT NOT NULL DEFAULT '',
    extra_attrs_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_std_dict_code_nocase
    ON standard_dictionary (std_code COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_std_dict_en_abbr
    ON standard_dictionary (en_abbr COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_std_dict_cn_name
    ON standard_dictionary (cn_name);
CREATE INDEX IF NOT EXISTS idx_std_dict_cn_alias
    ON standard_dictionary (cn_alias);
CREATE INDEX IF NOT EXISTS idx_std_dict_reg
    ON standard_dictionary (reg_category);
CREATE INDEX IF NOT EXISTS idx_std_dict_data_cat
    ON standard_dictionary (data_category);

CREATE TABLE IF NOT EXISTS field_standard_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    schema_name TEXT NOT NULL DEFAULT 'public',
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    col_key TEXT NOT NULL,
    std_code TEXT NOT NULL DEFAULT '',
    bind_source TEXT NOT NULL DEFAULT 'MANUAL',
    compliance_status TEXT NOT NULL DEFAULT 'UNCHECKED',
    compliance_detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(connection_id, col_key)
);
CREATE INDEX IF NOT EXISTS idx_fsb_conn_table
    ON field_standard_bindings (connection_id, schema_name, table_name);
CREATE INDEX IF NOT EXISTS idx_fsb_conn_table_nocase
    ON field_standard_bindings (connection_id, schema_name COLLATE NOCASE, table_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_fsb_std_code
    ON field_standard_bindings (std_code);
"""


def init_workspace_schema_on_conn(conn: sqlite3.Connection) -> None:
    """在指定 SQLite 连接上幂等初始化 Layer 3 共享工作区纯净表结构（零私有/凭据表）."""
    conn.executescript(WORKSPACE_SCHEMA_SQL)
    PlatformStateStore._ensure_workspace_schema_columns(conn)


class PlatformStateStore:
    """线程安全的 Data.O.G 双库分离状态引擎（本地私有保险箱 + 可共享团队工作区）."""

    def __init__(
        self,
        db_path: str,
        *,
        workspace_db_path: Optional[str] = None,
        clock_anchor_path: Optional[str] = None,
    ) -> None:
        self.private_db_path = os.path.abspath(db_path)
        self.db_path = self.private_db_path
        self.workspace_db_path = (
            os.path.abspath(workspace_db_path) if workspace_db_path else self.private_db_path
        )
        self.is_split_storage = self.private_db_path != self.workspace_db_path
        self.clock_anchor_path = os.path.abspath(clock_anchor_path) if clock_anchor_path else None

        os.makedirs(os.path.dirname(self.private_db_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.workspace_db_path), exist_ok=True)

        self._lock = threading.RLock()
        self._registered_schemas: Dict[str, List[Tuple[str, str, List[Tuple[str, str]]]]] = {
            "private": [],
            "workspace": [],
        }

        self._init_private_db()
        self._init_workspace_db()
        self._migrate_legacy_db_file_if_needed()
        self._migrate_legacy_connection_profiles_in_db()
        if not self.is_split_storage or not self.get_meta("active_workspace_id", ""):
            self._migrate_legacy_connections_if_needed()

    @staticmethod
    def _connect_sqlite(path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000;")
        return conn

    @contextlib.contextmanager
    def _get_private_conn(self):
        conn = self._connect_sqlite(self.private_db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _get_workspace_conn(self):
        conn = self._connect_sqlite(self.workspace_db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _get_conn(self):
        """默认指向当前活动工作区数据库（在单库兼容模式下与私有库为同一文件）."""
        with self._get_workspace_conn() as conn:
            yield conn

    @contextlib.contextmanager
    def _get_both_conns(self):
        """同时获取 (private_conn, workspace_conn)；单库兼容模式下复用同一连接避免同文件双写锁."""
        if self.is_split_storage:
            with self._get_private_conn() as p_conn, self._get_workspace_conn() as w_conn:
                yield p_conn, w_conn
        else:
            with self._get_private_conn() as conn:
                yield conn, conn

    @staticmethod
    def _now_str() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _init_private_db(self) -> None:
        with self._lock, self._get_private_conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            conn.executescript(PRIVATE_SCHEMA_SQL)
            self._ensure_private_schema_columns(conn)
            conn.commit()

    def _init_workspace_db(self) -> None:
        os.makedirs(os.path.dirname(self.workspace_db_path), exist_ok=True)
        with self._lock, self._get_workspace_conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            init_workspace_schema_on_conn(conn)
            for tbl_name, create_sql, exp_cols in self._registered_schemas.get("workspace", []):
                if create_sql.strip():
                    conn.executescript(create_sql)
                self._ensure_columns_for_table(conn, tbl_name, exp_cols)
            conn.commit()

    def switch_workspace_db(self, new_workspace_db_path: str) -> None:
        """原子热切换当前活动的 Layer 3 团队共享工作区 SQLite 文件（不改动私有保险箱）."""
        target = os.path.abspath(new_workspace_db_path)
        with self._lock:
            self.workspace_db_path = target
            self.is_split_storage = self.private_db_path != self.workspace_db_path
            self._init_workspace_db()

    def register_table_schema(
        self,
        table_name: str,
        create_sql: str,
        expected_columns: List[Tuple[str, str]],
        *,
        scope: str = "workspace",
    ) -> None:
        """供业务插件模块幂等注册私有/工作区 SQLite 表结构并自动演进补齐新增列."""
        norm_scope = "private" if str(scope or "").strip().lower() == "private" else "workspace"
        with self._lock:
            bucket = self._registered_schemas[norm_scope]
            if not any(t[0] == table_name for t in bucket):
                bucket.append((table_name, create_sql, list(expected_columns)))
            cm = self._get_private_conn() if norm_scope == "private" else self._get_workspace_conn()
            with cm as conn:
                if create_sql.strip():
                    conn.executescript(create_sql)
                self._ensure_columns_for_table(conn, table_name, expected_columns)
                conn.commit()

    @contextlib.contextmanager
    def module_db(self, scope: str = "workspace"):
        """供业务插件模块执行参数化 SQLite 读写事务的线程安全上下文."""
        norm_scope = "private" if str(scope or "").strip().lower() == "private" else "workspace"
        cm = self._get_private_conn() if norm_scope == "private" else self._get_workspace_conn()
        with self._lock, cm as conn:
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
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1]).lower()
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
    def _ensure_private_schema_columns(cls, conn: sqlite3.Connection) -> None:
        """幂等补齐 Layer 2 私有库表列定义."""
        expected_columns: Dict[str, List[Tuple[str, str]]] = {
            "execution_logs": [
                ("sql_text", "TEXT DEFAULT ''"),
            ],
            "workspaces": [
                ("description", "TEXT NOT NULL DEFAULT ''"),
                ("file_name", "TEXT NOT NULL DEFAULT ''"),
                ("is_default", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "connection_credentials": [
                ("connection_id", "TEXT NOT NULL DEFAULT ''"),
                ("endpoint_fingerprint", "TEXT NOT NULL DEFAULT ''"),
                ("db_type", "TEXT NOT NULL DEFAULT ''"),
                ("host", "TEXT NOT NULL DEFAULT ''"),
                ("port", "INTEGER NOT NULL DEFAULT 0"),
                ("dbname", "TEXT NOT NULL DEFAULT ''"),
                ("jdbc_url", "TEXT NOT NULL DEFAULT ''"),
                ("user", "TEXT NOT NULL DEFAULT ''"),
                ("password", "TEXT NOT NULL DEFAULT ''"),
                ("sslmode", "TEXT NOT NULL DEFAULT 'prefer'"),
                ("auth_mode", "TEXT NOT NULL DEFAULT 'LDAP'"),
                ("sqlite_path", "TEXT NOT NULL DEFAULT ''"),
                ("jdbc_driver_id", "TEXT NOT NULL DEFAULT ''"),
                ("extra_secrets_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "jdbc_drivers": [
                ("driver_class_hint", "TEXT NOT NULL DEFAULT ''"),
                ("db_type_hint", "TEXT NOT NULL DEFAULT 'CUSTOM_JDBC'"),
                ("uploaded_at", "TEXT NOT NULL DEFAULT ''"),
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
            # 若为历史单库兼容文件且残留 connection_profiles，保证列完整供迁移读取
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
                ("standard_format", "TEXT NOT NULL DEFAULT 'AUTO'"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
        }
        for table_name, col_defs in expected_columns.items():
            cls._ensure_columns_for_table(conn, table_name, col_defs)

    @classmethod
    def _ensure_workspace_schema_columns(cls, conn: sqlite3.Connection) -> None:
        """幂等补齐 Layer 3 共享工作区业务表列定义，保障跨分支切换与版本回退前后向无损兼容."""
        expected_columns: Dict[str, List[Tuple[str, str]]] = {
            "connection_topology": [
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
                ("standard_format", "TEXT NOT NULL DEFAULT 'AUTO'"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
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
                ("connection_id", "TEXT NOT NULL DEFAULT ''"),
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
                ("source_std_code", "TEXT NOT NULL DEFAULT ''"),
                ("target_std_code", "TEXT NOT NULL DEFAULT ''"),
            ],
            "standard_dictionary": [
                ("reg_category", "TEXT NOT NULL DEFAULT ''"),
                ("cat_l1", "TEXT NOT NULL DEFAULT ''"),
                ("cat_l2", "TEXT NOT NULL DEFAULT ''"),
                ("cat_l3", "TEXT NOT NULL DEFAULT ''"),
                ("cn_name", "TEXT NOT NULL DEFAULT ''"),
                ("cn_alias", "TEXT NOT NULL DEFAULT ''"),
                ("en_name", "TEXT NOT NULL DEFAULT ''"),
                ("en_abbr", "TEXT NOT NULL DEFAULT ''"),
                ("biz_definition", "TEXT NOT NULL DEFAULT ''"),
                ("scope_desc", "TEXT NOT NULL DEFAULT ''"),
                ("biz_rule", "TEXT NOT NULL DEFAULT ''"),
                ("max_length", "TEXT NOT NULL DEFAULT ''"),
                ("min_scale", "TEXT NOT NULL DEFAULT ''"),
                ("ref_code_no", "TEXT NOT NULL DEFAULT ''"),
                ("ref_code_name", "TEXT NOT NULL DEFAULT ''"),
                ("ref_code_detail", "TEXT NOT NULL DEFAULT ''"),
                ("security_level", "TEXT NOT NULL DEFAULT ''"),
                ("ref_std_no", "TEXT NOT NULL DEFAULT ''"),
                ("ref_std_name", "TEXT NOT NULL DEFAULT ''"),
                ("domain_name", "TEXT NOT NULL DEFAULT ''"),
                ("domain_code", "TEXT NOT NULL DEFAULT ''"),
                ("data_category", "TEXT NOT NULL DEFAULT ''"),
                ("data_format", "TEXT NOT NULL DEFAULT ''"),
                ("value_range", "TEXT NOT NULL DEFAULT ''"),
                ("source_system", "TEXT NOT NULL DEFAULT ''"),
                ("ddm_catalog", "TEXT NOT NULL DEFAULT ''"),
                ("is_standard_dict", "TEXT NOT NULL DEFAULT ''"),
                ("version_no", "TEXT NOT NULL DEFAULT ''"),
                ("remark", "TEXT NOT NULL DEFAULT ''"),
                ("type_dws", "TEXT NOT NULL DEFAULT ''"),
                ("type_dw_mart", "TEXT NOT NULL DEFAULT ''"),
                ("type_mysql", "TEXT NOT NULL DEFAULT ''"),
                ("type_oracle", "TEXT NOT NULL DEFAULT ''"),
                ("type_dameng", "TEXT NOT NULL DEFAULT ''"),
                ("type_goldendb", "TEXT NOT NULL DEFAULT ''"),
                ("type_ob_oracle", "TEXT NOT NULL DEFAULT ''"),
                ("type_ob_mysql", "TEXT NOT NULL DEFAULT ''"),
                ("type_argodb", "TEXT NOT NULL DEFAULT ''"),
                ("type_oceanbase", "TEXT NOT NULL DEFAULT ''"),
                ("extra_attrs_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "field_standard_bindings": [
                ("std_code", "TEXT NOT NULL DEFAULT ''"),
                ("bind_source", "TEXT NOT NULL DEFAULT 'MANUAL'"),
                ("compliance_status", "TEXT NOT NULL DEFAULT 'UNCHECKED'"),
                ("compliance_detail", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
        }
        for table_name, col_defs in expected_columns.items():
            cls._ensure_columns_for_table(conn, table_name, col_defs)

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

    # =========================================================================
    # 基础键值配置（按白名单自动分流至私有 app_meta 或工作区 workspace_meta）与日志中心
    # =========================================================================

    def save_meta(self, key: str, value: Any) -> None:
        val_str = json.dumps(value, ensure_ascii=False)
        if key in WORKSPACE_META_KEYS:
            with self._lock, self._get_workspace_conn() as conn:
                conn.execute(
                    "INSERT INTO workspace_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, val_str),
                )
                conn.commit()
            return
        with self._lock, self._get_private_conn() as conn:
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, val_str),
            )
            conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        if key in WORKSPACE_META_KEYS:
            with self._lock, self._get_workspace_conn() as conn:
                row = conn.execute(
                    "SELECT value FROM workspace_meta WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    try:
                        return json.loads(row["value"])
                    except Exception:
                        return default
            # 单库兼容回退：历史文件中可能存于 app_meta
            if not self.is_split_storage:
                with self._lock, self._get_private_conn() as conn:
                    row2 = conn.execute(
                        "SELECT value FROM app_meta WHERE key = ?", (key,)
                    ).fetchone()
                    if row2:
                        try:
                            return json.loads(row2["value"])
                        except Exception:
                            return default
            return default

        with self._lock, self._get_private_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def get_meta_batch(self, keys: Iterable[str]) -> Dict[str, Any]:
        """单次事务/连接按路由批量读取 app_meta 与 workspace_meta 键值."""
        uniq_keys = list(dict.fromkeys(str(k) for k in keys if str(k)))
        if not uniq_keys:
            return {}
        ws_keys = [k for k in uniq_keys if k in WORKSPACE_META_KEYS]
        priv_keys = [k for k in uniq_keys if k not in WORKSPACE_META_KEYS]
        out: Dict[str, Any] = {}
        with self._lock:
            if priv_keys:
                ph = ",".join("?" for _ in priv_keys)
                with self._get_private_conn() as pconn:
                    for row in pconn.execute(
                        f"SELECT key, value FROM app_meta WHERE key IN ({ph})",
                        priv_keys,
                    ).fetchall():
                        try:
                            out[str(row["key"])] = json.loads(row["value"])
                        except Exception:
                            pass
            if ws_keys:
                ph = ",".join("?" for _ in ws_keys)
                with self._get_workspace_conn() as wconn:
                    for row in wconn.execute(
                        f"SELECT key, value FROM workspace_meta WHERE key IN ({ph})",
                        ws_keys,
                    ).fetchall():
                        try:
                            out[str(row["key"])] = json.loads(row["value"])
                        except Exception:
                            pass
                missing_ws = [k for k in ws_keys if k not in out]
                if missing_ws and not self.is_split_storage:
                    ph2 = ",".join("?" for _ in missing_ws)
                    with self._get_private_conn() as pconn:
                        for row in pconn.execute(
                            f"SELECT key, value FROM app_meta WHERE key IN ({ph2})",
                            missing_ws,
                        ).fetchall():
                            try:
                                out[str(row["key"])] = json.loads(row["value"])
                            except Exception:
                                pass
        return out

    def add_log(self, level: str, target: str, message: str, sql_text: str = "") -> None:
        now = self._now_str()
        with self._lock, self._get_private_conn() as conn:
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
        with self._lock, self._get_private_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_logs(self) -> None:
        with self._lock, self._get_private_conn() as conn:
            conn.execute("DELETE FROM execution_logs")
            conn.commit()

    # =========================================================================
    # 本地托管工作区注册表 (Private Vault `workspaces`)
    # =========================================================================

    def list_workspace_records(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_private_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces ORDER BY is_default DESC, updated_at DESC, created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_workspace_record(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        if not workspace_id:
            return None
        with self._lock, self._get_private_conn() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_workspace_record(
        self,
        *,
        workspace_id: str,
        name: str,
        file_name: str,
        description: str = "",
        is_default: bool = False,
        created_at: str = "",
    ) -> Dict[str, Any]:
        now = self._now_str()
        with self._lock, self._get_private_conn() as conn:
            existing = conn.execute(
                "SELECT created_at FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            eff_created = created_at or (existing["created_at"] if existing else now)
            conn.execute(
                """
                INSERT INTO workspaces (
                    workspace_id, name, description, file_name, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    file_name = excluded.file_name,
                    is_default = excluded.is_default,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    name,
                    description,
                    file_name,
                    1 if is_default else 0,
                    eff_created,
                    now,
                ),
            )
            conn.commit()
        return self.get_workspace_record(workspace_id) or {}

    def delete_workspace_record(self, workspace_id: str) -> bool:
        with self._lock, self._get_private_conn() as conn:
            cur = conn.execute(
                "DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            )
            conn.commit()
            return cur.rowcount > 0

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

        chosen_cred, cred_status = PlatformStateStore._resolve_credential_for_topology(
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
    # 单库批量贯标模块 (standard_migrator) 状态接口
    # =========================================================================

    @staticmethod
    def _normalize_migration_item_payload(
        item: Dict[str, Any],
        prev: Optional[Dict[str, Any]] = None,
        *,
        default_connection_id: str = "",
        preserve_prev_errors: bool = True,
    ) -> Dict[str, Any]:
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        schema_name = str(item.get("schema_name", "")).strip(quote_chars) or "public"
        table_name = str(item.get("table_name", "")).strip(quote_chars)
        old_col_name = str(item.get("old_col_name", "")).strip(quote_chars)
        old_col_type = str(item.get("old_col_type", "")).strip()
        old_col_comment = str(item.get("old_col_comment", "")).strip()
        new_col_name = str(item.get("new_col_name", "")).strip(quote_chars) or old_col_name
        new_col_type = str(item.get("new_col_type", "")).strip() or old_col_type
        new_col_comment = str(item.get("new_col_comment", "")).strip()
        conn_id = str(
            item.get("connection_id")
            or (prev.get("connection_id") if prev else "")
            or default_connection_id
            or ""
        ).strip()

        status = item.get("status") or (prev["status"] if prev else "UNCHECKED")
        strategy = str(
            item.get("strategy") or (prev.get("strategy") if prev else "DIRECT_ALTER") or "DIRECT_ALTER"
        )
        status_detail = item.get("status_detail") or (
            prev["status_detail"]
            if prev
            else "已导入清单，等待连接数据库同步元数据状态"
        )
        if preserve_prev_errors:
            error_message = (
                str(item.get("error_message"))
                if item.get("error_message") is not None
                else (prev["error_message"] if prev else "")
            )
            executed_at = (
                str(item.get("executed_at"))
                if item.get("executed_at") is not None
                else (prev["executed_at"] if prev else "")
            )
        else:
            error_message = str(item.get("error_message") or "")
            executed_at = str(item.get("executed_at") or "")

        ordinal_pos = item.get("ordinal_position")
        if ordinal_pos is None and prev:
            ordinal_pos = prev["ordinal_position"]
        total_cols = item.get("total_columns")
        if total_cols is None and prev:
            total_cols = prev["total_columns"]
        db_col = item.get("db_current_col_name") or (
            prev["db_current_col_name"] if prev else ""
        )
        db_type = item.get("db_current_type") or (
            prev["db_current_type"] if prev else ""
        )
        db_comment = (
            item.get("db_current_comment")
            if "db_current_comment" in item and item.get("db_current_comment") is not None
            else (prev["db_current_comment"] if prev else "")
        )
        is_dist = (
            1
            if item.get("is_distribute_key")
            else (int(prev["is_distribute_key"] or 0) if (preserve_prev_errors and prev) else 0)
        )
        orientation = item.get("table_orientation") or (
            prev["table_orientation"] if prev else ""
        )
        generated_sql = str(
            item.get("generated_sql") or (prev.get("generated_sql") if prev else "") or ""
        )
        return {
            "connection_id": conn_id,
            "schema_name": schema_name,
            "table_name": table_name,
            "old_col_name": old_col_name,
            "old_col_type": old_col_type,
            "old_col_comment": old_col_comment,
            "new_col_name": new_col_name,
            "new_col_type": new_col_type,
            "new_col_comment": new_col_comment,
            "status": status,
            "strategy": strategy,
            "ordinal_position": ordinal_pos,
            "total_columns": total_cols,
            "db_current_col_name": db_col,
            "db_current_type": db_type,
            "db_current_comment": db_comment,
            "is_distribute_key": is_dist,
            "table_orientation": orientation,
            "status_detail": status_detail,
            "error_message": error_message,
            "generated_sql": generated_sql,
            "executed_at": executed_at,
        }

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
                    rd = dict(r)
                    rcid = str(rd.get("connection_id") or "").strip().lower()
                    base_k = f"{rd['schema_name']}.{rd['table_name']}.{rd['old_col_name']}->{rd['new_col_name']}".lower()
                    history_map[f"{rcid}::{base_k}"] = rd
                    history_map.setdefault(base_k, rd)

            conn.execute("DELETE FROM migration_items")
            rows_to_insert = []
            for idx, item in enumerate(items, start=1):
                quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
                s_n = str(item.get("schema_name", "")).strip(quote_chars) or "public"
                t_n = str(item.get("table_name", "")).strip(quote_chars)
                o_n = str(item.get("old_col_name", "")).strip(quote_chars)
                n_n = str(item.get("new_col_name", "")).strip(quote_chars) or o_n
                c_n = str(item.get("connection_id") or "").strip().lower()
                base_k = f"{s_n}.{t_n}.{o_n}->{n_n}".lower()
                prev = history_map.get(f"{c_n}::{base_k}") or history_map.get(base_k)

                norm = self._normalize_migration_item_payload(
                    item, prev=prev, preserve_prev_errors=True
                )
                rows_to_insert.append(
                    (
                        item.get("row_index", idx),
                        norm["connection_id"],
                        norm["schema_name"],
                        norm["table_name"],
                        norm["old_col_name"],
                        norm["old_col_type"],
                        norm["old_col_comment"],
                        norm["new_col_name"],
                        norm["new_col_type"],
                        norm["new_col_comment"],
                        norm["status"],
                        norm["strategy"],
                        norm["ordinal_position"],
                        norm["total_columns"],
                        norm["db_current_col_name"],
                        norm["db_current_type"],
                        norm["db_current_comment"],
                        norm["is_distribute_key"],
                        norm["table_orientation"],
                        norm["status_detail"],
                        norm["error_message"],
                        norm["generated_sql"],
                        norm["executed_at"],
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO migration_items (
                    row_index, connection_id, schema_name, table_name,
                    old_col_name, old_col_type, old_col_comment,
                    new_col_name, new_col_type, new_col_comment,
                    status, strategy, ordinal_position, total_columns,
                    db_current_col_name, db_current_type, db_current_comment,
                    is_distribute_key, table_orientation,
                    status_detail, error_message, generated_sql, executed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def upsert_migration_items(
        self,
        items: List[Dict[str, Any]],
        source_label: str,
        connection_id: str = "",
    ) -> List[Dict[str, Any]]:
        """将字段贯标任务幂等追加或原位更新至全局队列，不删除已有其他表/字段任务."""
        if not items:
            return self.list_items()
        now = self._now_str()
        def_cid = str(connection_id or "").strip()
        active_cid = str(self.get_meta("migrator_connection_id", "") or "").strip()
        with self._lock, self._get_conn() as conn:
            existing_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM migration_items ORDER BY row_index ASC, id ASC"
                ).fetchall()
            ]
            max_row_idx = max((int(r.get("row_index") or 0) for r in existing_rows), default=0)
            existing_by_key: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
            for r in existing_rows:
                r_cid = str(r.get("connection_id") or active_cid or "").strip().lower()
                k = (
                    r_cid,
                    str(r.get("schema_name") or "").strip().lower(),
                    str(r.get("table_name") or "").strip().lower(),
                    str(r.get("old_col_name") or "").strip().lower(),
                )
                existing_by_key[k] = r
                if not str(r.get("connection_id") or "").strip() and def_cid:
                    # 兼容历史未标记 connection_id 的队列项
                    existing_by_key.setdefault(
                        (
                            def_cid.lower(),
                            k[1],
                            k[2],
                            k[3],
                        ),
                        r,
                    )

            to_insert: List[Tuple[Any, ...]] = []
            to_update: List[Tuple[Any, ...]] = []

            for item in items:
                norm = self._normalize_migration_item_payload(
                    item,
                    prev=None,
                    default_connection_id=def_cid or active_cid,
                    preserve_prev_errors=False,
                )
                eff_cid = str(norm["connection_id"] or def_cid or active_cid).strip().lower()
                k = (
                    eff_cid,
                    norm["schema_name"].lower(),
                    norm["table_name"].lower(),
                    norm["old_col_name"].lower(),
                )
                hit = existing_by_key.get(k)
                if hit is not None:
                    to_update.append(
                        (
                            norm["connection_id"],
                            norm["old_col_type"],
                            norm["old_col_comment"],
                            norm["new_col_name"],
                            norm["new_col_type"],
                            norm["new_col_comment"],
                            norm["status"],
                            norm["strategy"],
                            norm["ordinal_position"],
                            norm["total_columns"],
                            norm["db_current_col_name"],
                            norm["db_current_type"],
                            norm["db_current_comment"],
                            norm["is_distribute_key"],
                            norm["table_orientation"],
                            norm["status_detail"],
                            norm["error_message"],
                            norm["generated_sql"],
                            now,
                            int(hit["id"]),
                        )
                    )
                    existing_by_key[k] = {**hit, **norm, "id": hit["id"]}
                else:
                    max_row_idx += 1
                    to_insert.append(
                        (
                            max_row_idx,
                            norm["connection_id"],
                            norm["schema_name"],
                            norm["table_name"],
                            norm["old_col_name"],
                            norm["old_col_type"],
                            norm["old_col_comment"],
                            norm["new_col_name"],
                            norm["new_col_type"],
                            norm["new_col_comment"],
                            norm["status"],
                            norm["strategy"],
                            norm["ordinal_position"],
                            norm["total_columns"],
                            norm["db_current_col_name"],
                            norm["db_current_type"],
                            norm["db_current_comment"],
                            norm["is_distribute_key"],
                            norm["table_orientation"],
                            norm["status_detail"],
                            norm["error_message"],
                            norm["generated_sql"],
                            norm["executed_at"],
                            now,
                        )
                    )
                    existing_by_key[k] = {**norm, "row_index": max_row_idx}

            if to_update:
                conn.executemany(
                    """
                    UPDATE migration_items
                    SET connection_id = ?,
                        old_col_type = ?,
                        old_col_comment = ?,
                        new_col_name = ?,
                        new_col_type = ?,
                        new_col_comment = ?,
                        status = ?,
                        strategy = ?,
                        ordinal_position = ?,
                        total_columns = ?,
                        db_current_col_name = ?,
                        db_current_type = ?,
                        db_current_comment = ?,
                        is_distribute_key = ?,
                        table_orientation = ?,
                        status_detail = ?,
                        error_message = ?,
                        generated_sql = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    to_update,
                )
            if to_insert:
                conn.executemany(
                    """
                    INSERT INTO migration_items (
                        row_index, connection_id, schema_name, table_name,
                        old_col_name, old_col_type, old_col_comment,
                        new_col_name, new_col_type, new_col_comment,
                        status, strategy, ordinal_position, total_columns,
                        db_current_col_name, db_current_type, db_current_comment,
                        is_distribute_key, table_orientation,
                        status_detail, error_message, generated_sql, executed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    to_insert,
                )
            conn.commit()
        self.save_meta("current_excel_name", source_label)
        self.save_meta("imported_at", now)
        self.add_log(
            "INFO",
            "队列更新",
            f"已将 [{source_label}] 共 {len(items)} 条字段规则纳入全局贯标队列（新增 {len(to_insert)} / 更新 {len(to_update)}）。",
        )
        return self.list_items()

    def delete_migration_items(self, item_ids: List[int]) -> int:
        """按 ID 从全局贯标队列中移除指定任务（不影响已绑定的数据标准）."""
        clean_ids: List[int] = []
        seen = set()
        for raw_id in item_ids or []:
            try:
                iid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if iid > 0 and iid not in seen:
                seen.add(iid)
                clean_ids.append(iid)
        if not clean_ids:
            return 0
        deleted = 0
        with self._lock, self._get_conn() as conn:
            for idx in range(0, len(clean_ids), 500):
                chunk = clean_ids[idx : idx + 500]
                ph = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM migration_items WHERE id IN ({ph})",
                    chunk,
                )
                deleted += max(0, int(cur.rowcount or 0))
            conn.commit()
        return deleted

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
                strategy = str(u.get("strategy") or "").strip()
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
                            strategy = CASE WHEN ? != '' THEN ? ELSE strategy END,
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
                            strategy,
                            strategy,
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
                            strategy = CASE WHEN ? != '' THEN ? ELSE strategy END,
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
                            strategy,
                            strategy,
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

    def get_summary(
        self, items: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, int]:
        if items is None:
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
            if st != "COMPLETED" and item_requires_table_rebuild(item):
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
                src_std_code = str(
                    item.get("source_std_code")
                    or (prev_c.get("source_std_code", "") if prev_c else "")
                    or ""
                ).strip()
                tgt_std_code = str(
                    item.get("target_std_code")
                    or (prev_c.get("target_std_code", "") if prev_c else "")
                    or ""
                ).strip()

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
                        src_std_code,
                        tgt_std_code,
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
                    bound_source_col, bound_target_col, source_std_code, target_std_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    COUNT(c.id) AS column_count,
                    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(c.bound_source_col, '')), '') AS source_cols_text,
                    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(c.bound_target_col, '')), '') AS target_cols_text,
                    COALESCE(
                        GROUP_CONCAT(
                            DISTINCT CASE
                                WHEN SUBSTR(COALESCE(c.std_col_name, ''), 1, 11) = '__unpaired_' THEN NULL
                                ELSE NULLIF(c.std_col_name, '')
                            END
                        ),
                        ''
                    ) AS std_cols_text,
                    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(c.std_col_comment, '')), '') AS std_comments_text
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

    def count_cross_columns(self, std_table_key: Optional[str] = None) -> int:
        with self._lock, self._get_conn() as conn:
            if std_table_key:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cross_column_bindings WHERE std_table_key = ?",
                    (std_table_key.strip().lower(),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cross_column_bindings"
                ).fetchone()
            return int(row[0]) if row else 0

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
                has_src_std = "source_std_code" in item
                has_tgt_std = "target_std_code" in item
                src_std_code = str(item.get("source_std_code") or "").strip()
                tgt_std_code = str(item.get("target_std_code") or "").strip()
                target_id = raw_id if raw_id > 0 else by_std_col.get(std_col.lower(), 0)
                if target_id > 0:
                    conn.execute(
                        """
                        UPDATE cross_column_bindings
                        SET bound_source_col = ?,
                            bound_target_col = ?,
                            source_std_code = CASE WHEN ? THEN ? ELSE source_std_code END,
                            target_std_code = CASE WHEN ? THEN ? ELSE target_std_code END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            bound_src,
                            bound_tgt,
                            1 if has_src_std else 0,
                            src_std_code,
                            1 if has_tgt_std else 0,
                            tgt_std_code,
                            now,
                            target_id,
                        ),
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
                            bound_source_col, bound_target_col,
                            source_std_code, target_std_code, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            src_std_code,
                            tgt_std_code,
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
                        str(c.get("source_std_code") or "").strip(),
                        str(c.get("target_std_code") or "").strip(),
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
                    bound_source_col, bound_target_col,
                    source_std_code, target_std_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

