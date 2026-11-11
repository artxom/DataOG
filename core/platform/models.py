"""DataOG 全局平台统一数据模型定义."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ColumnMeta:
    """标准化字段元数据模型（兼容单库贯标、三栏比对、DDL翻译与全局目录树）."""

    ordinal_position: int
    column_name: str
    data_type: str
    is_not_null: bool = False
    column_default: Optional[str] = None
    column_comment: str = ""
    lineage_source: str = ""
    normalized_type: str = ""
    is_primary_key: bool = False
    index_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TableMeta:
    """标准化表/视图/物化视图元数据模型."""

    schema_name: str
    table_name: str
    exists: bool = True
    table_comment: str = ""
    orientation: str = "ROW"
    with_clause: str = ""
    distribute_clause: str = ""
    distribute_keys: List[str] = field(default_factory=list)
    partition_clause: str = ""
    partition_keys: List[str] = field(default_factory=list)
    columns: List[ColumnMeta] = field(default_factory=list)
    row_count: int = 0
    raw_ddl: str = ""
    object_type: str = "TABLE"  # TABLE | VIEW | MATVIEW
    view_definition: str = ""
    penetrated_base_tables: List[str] = field(default_factory=list)
    primary_keys: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["column_count"] = len(self.columns)
        return data


@dataclass
class SchemaMeta:
    """标准化 Schema / 数据库目录节点."""

    schema_name: str
    table_count: int = 0
    view_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DriverPackage:
    """用户上传的自定义 JDBC .jar 驱动包档案."""

    driver_id: str
    name: str
    file_name: str
    stored_rel_path: str
    sha256: str
    file_size: int
    driver_class_hint: str = ""
    db_type_hint: str = "CUSTOM_JDBC"
    uploaded_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectionProfile:
    """全局命名数据源连接档案（ConnectionHub 管理，模块通过 connection_id 引用）."""

    connection_id: str
    name: str
    driver_track: str = "NATIVE"  # NATIVE | JDBC
    db_type: str = "DWS"  # DWS | PGSQL | INCEPTOR | MYSQL | SQLITE | DM8 | KINGBASE | ORACLE | DB2 | GBASE | CUSTOM_JDBC
    host: str = ""
    port: int = 8000
    dbname: str = ""
    default_schema: str = "public"
    user: str = ""
    password: str = ""
    sslmode: str = "prefer"
    auth_mode: str = "LDAP"
    sqlite_path: str = ""
    jdbc_driver_id: str = ""
    jdbc_url: str = ""
    jdbc_driver_class: str = ""
    extra_props: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_public_dict(self) -> Dict[str, Any]:
        """输出脱敏后的前端安全字典（绝不直接暴露明文密码）."""
        return {
            "connection_id": self.connection_id,
            "name": self.name,
            "driver_track": self.driver_track,
            "db_type": self.db_type,
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "default_schema": self.default_schema,
            "user": self.user,
            "has_saved_password": bool(self.password),
            "password_masked": "******" if self.password else "",
            "sslmode": self.sslmode,
            "auth_mode": self.auth_mode,
            "sqlite_path": self.sqlite_path,
            "jdbc_driver_id": self.jdbc_driver_id,
            "jdbc_url": self.jdbc_url,
            "jdbc_driver_class": self.jdbc_driver_class,
            "extra_props": dict(self.extra_props or {}),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class QueryColumn:
    """查询结果集列头描述."""

    name: str
    data_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QueryResult:
    """跨驱动统一 SQL 查询返回结构."""

    connection_id: str
    sql: str
    columns: List[QueryColumn] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    limit_applied: int = 500
    elapsed_ms: float = 0.0
    affected_rows: int = 0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "sql": self.sql,
            "columns": [c.to_dict() for c in self.columns],
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "limit_applied": self.limit_applied,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "affected_rows": self.affected_rows,
            "message": self.message,
        }
