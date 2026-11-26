"""Data.O.G 三层分离 SQLite 表结构定义、表白名单与幂等列演进工具 (core/platform/storage/schema.py)."""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, List, Tuple

_SAFE_SQL_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

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
    "migration_change_logs",
    "migration_checkpoints",
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

CREATE TABLE IF NOT EXISTS migration_change_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_uid TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL DEFAULT '',
    execution_group_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'MIGRATION',
    status TEXT NOT NULL DEFAULT 'APPLIED',
    connection_id TEXT NOT NULL DEFAULT '',
    connection_name TEXT NOT NULL DEFAULT '',
    db_type TEXT NOT NULL DEFAULT '',
    schema_name TEXT NOT NULL DEFAULT 'public',
    table_name TEXT NOT NULL DEFAULT '',
    source_item_id INTEGER NOT NULL DEFAULT 0,
    ordinal_position INTEGER NOT NULL DEFAULT 0,
    std_code TEXT NOT NULL DEFAULT '',
    before_col_name TEXT NOT NULL DEFAULT '',
    before_col_type TEXT NOT NULL DEFAULT '',
    before_col_comment TEXT NOT NULL DEFAULT '',
    after_col_name TEXT NOT NULL DEFAULT '',
    after_col_type TEXT NOT NULL DEFAULT '',
    after_col_comment TEXT NOT NULL DEFAULT '',
    name_changed INTEGER NOT NULL DEFAULT 0,
    type_changed INTEGER NOT NULL DEFAULT 0,
    comment_changed INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT 'L4',
    risk_rank INTEGER NOT NULL DEFAULT 4,
    risk_code TEXT NOT NULL DEFAULT 'SAFE_CHANGE',
    risk_label TEXT NOT NULL DEFAULT '',
    risk_factors_json TEXT NOT NULL DEFAULT '[]',
    is_lossy INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL DEFAULT 'DIRECT_ALTER',
    backup_table_name TEXT NOT NULL DEFAULT '',
    forward_sql TEXT NOT NULL DEFAULT '',
    rollback_sql TEXT NOT NULL DEFAULT '',
    rollback_of_change_id INTEGER NOT NULL DEFAULT 0,
    rolled_back_by_id INTEGER NOT NULL DEFAULT 0,
    rolled_back_at TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcl_conn_table
    ON migration_change_logs (connection_id, schema_name, table_name, id DESC);
CREATE INDEX IF NOT EXISTS idx_mcl_status
    ON migration_change_logs (event_type, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_mcl_group
    ON migration_change_logs (execution_group_id);

CREATE TABLE IF NOT EXISTS migration_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    connection_id TEXT NOT NULL DEFAULT '',
    connection_name TEXT NOT NULL DEFAULT '',
    anchor_log_id INTEGER NOT NULL DEFAULT 0,
    auto_created INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcp_conn
    ON migration_checkpoints (connection_id, id DESC);
"""



def ensure_columns_for_table(
    conn: sqlite3.Connection,
    table_name: str,
    col_defs: List[Tuple[str, str]],
) -> None:
    if not _SAFE_SQL_IDENT_RE.match(table_name or ""):
        raise ValueError(f"非法的 SQLite 表名: {table_name!r}")
    existing = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1]).lower()
        for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }
    if not existing:
        return
    for col_name, col_ddl in col_defs:
        if not _SAFE_SQL_IDENT_RE.match(col_name or ""):
            raise ValueError(f"非法的 SQLite 列名: {col_name!r}")
        if col_name.lower() not in existing:
            conn.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col_ddl}'
            )


class _SchemaColumnMixin:
    @staticmethod
    def _ensure_columns_for_table(
        conn: sqlite3.Connection,
        table_name: str,
        col_defs: List[Tuple[str, str]],
    ) -> None:
        ensure_columns_for_table(conn, table_name, col_defs)

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
            "migration_change_logs": [
                ("change_uid", "TEXT NOT NULL DEFAULT ''"),
                ("batch_id", "TEXT NOT NULL DEFAULT ''"),
                ("execution_group_id", "TEXT NOT NULL DEFAULT ''"),
                ("event_type", "TEXT NOT NULL DEFAULT 'MIGRATION'"),
                ("status", "TEXT NOT NULL DEFAULT 'APPLIED'"),
                ("connection_id", "TEXT NOT NULL DEFAULT ''"),
                ("connection_name", "TEXT NOT NULL DEFAULT ''"),
                ("db_type", "TEXT NOT NULL DEFAULT ''"),
                ("schema_name", "TEXT NOT NULL DEFAULT 'public'"),
                ("table_name", "TEXT NOT NULL DEFAULT ''"),
                ("source_item_id", "INTEGER NOT NULL DEFAULT 0"),
                ("ordinal_position", "INTEGER NOT NULL DEFAULT 0"),
                ("std_code", "TEXT NOT NULL DEFAULT ''"),
                ("before_col_name", "TEXT NOT NULL DEFAULT ''"),
                ("before_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("before_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("after_col_name", "TEXT NOT NULL DEFAULT ''"),
                ("after_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("after_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("name_changed", "INTEGER NOT NULL DEFAULT 0"),
                ("type_changed", "INTEGER NOT NULL DEFAULT 0"),
                ("comment_changed", "INTEGER NOT NULL DEFAULT 0"),
                ("risk_level", "TEXT NOT NULL DEFAULT 'L4'"),
                ("risk_rank", "INTEGER NOT NULL DEFAULT 4"),
                ("risk_code", "TEXT NOT NULL DEFAULT 'SAFE_CHANGE'"),
                ("risk_label", "TEXT NOT NULL DEFAULT ''"),
                ("risk_factors_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("is_lossy", "INTEGER NOT NULL DEFAULT 0"),
                ("strategy", "TEXT NOT NULL DEFAULT 'DIRECT_ALTER'"),
                ("backup_table_name", "TEXT NOT NULL DEFAULT ''"),
                ("forward_sql", "TEXT NOT NULL DEFAULT ''"),
                ("rollback_sql", "TEXT NOT NULL DEFAULT ''"),
                ("rollback_of_change_id", "INTEGER NOT NULL DEFAULT 0"),
                ("rolled_back_by_id", "INTEGER NOT NULL DEFAULT 0"),
                ("rolled_back_at", "TEXT NOT NULL DEFAULT ''"),
                ("note", "TEXT NOT NULL DEFAULT ''"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "migration_checkpoints": [
                ("checkpoint_uid", "TEXT NOT NULL DEFAULT ''"),
                ("name", "TEXT NOT NULL DEFAULT ''"),
                ("description", "TEXT NOT NULL DEFAULT ''"),
                ("connection_id", "TEXT NOT NULL DEFAULT ''"),
                ("connection_name", "TEXT NOT NULL DEFAULT ''"),
                ("anchor_log_id", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_created", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
        }
        for table_name, col_defs in expected_columns.items():
            cls._ensure_columns_for_table(conn, table_name, col_defs)


def ensure_private_schema_columns(conn: sqlite3.Connection) -> None:
    _SchemaColumnMixin._ensure_private_schema_columns(conn)


def ensure_workspace_schema_columns(conn: sqlite3.Connection) -> None:
    _SchemaColumnMixin._ensure_workspace_schema_columns(conn)


def init_workspace_schema_on_conn(conn: sqlite3.Connection) -> None:
    """在指定 SQLite 连接上幂等初始化 Layer 3 共享工作区纯净表结构（零私有/凭据表）."""
    conn.executescript(WORKSPACE_SCHEMA_SQL)
    ensure_workspace_schema_columns(conn)
