"""DataOG 全局平台统一数据模型定义."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote


_SECRET_PROP_TOKENS = ("password", "passwd", "pwd", "secret", "token", "apikey", "api_key", "credential")
_JDBC_USER_PARAM_NAMES = frozenset({"user", "username", "uid"})
_JDBC_ORACLE_INLINE_CRED_RE = re.compile(
    r"^(jdbc:oracle:[^:@]+:)([^/@\s]+)/([^@\s]+)(@.*)$",
    re.IGNORECASE,
)
_JDBC_AUTHORITY_CRED_RE = re.compile(
    r"^(jdbc:[^:/\s]+(?::[^:/\s]+)*://)([^/@\s:]+):([^/@\s]*)@(.+)$",
    re.IGNORECASE,
)


def _is_secret_key(key: str) -> bool:
    low = (key or "").strip().lower()
    return bool(low) and any(tok in low for tok in _SECRET_PROP_TOKENS)


def sanitize_jdbc_url(jdbc_url: str) -> Tuple[str, str, str]:
    """剥离 JDBC URL 中可能内嵌的明文账号密码与敏感参数，返回 `(clean_jdbc_url, extracted_user, extracted_password)`.

    保障 Layer 3 共享工作区 `.dataog.db` 与连接指纹中彻底零凭据泄露，兼容 Oracle / MySQL / PG / DM8 / SQLServer / DB2 格式。
    """
    raw = (jdbc_url or "").strip()
    if not raw:
        return "", "", ""

    url = raw
    extracted_user = ""
    extracted_pwd = ""

    # 1. Oracle thin/oci 内联凭据: jdbc:oracle:thin:user/pass@...
    m_ora = _JDBC_ORACLE_INLINE_CRED_RE.match(url)
    if m_ora:
        extracted_user = unquote(m_ora.group(2))
        extracted_pwd = unquote(m_ora.group(3))
        url = f"{m_ora.group(1)}{m_ora.group(4)}"

    # 2. 标准 authority 内联凭据: jdbc:<proto>://user:pass@host...
    m_auth = _JDBC_AUTHORITY_CRED_RE.match(url)
    if m_auth:
        if not extracted_user:
            extracted_user = unquote(m_auth.group(2))
        if not extracted_pwd:
            extracted_pwd = unquote(m_auth.group(3))
        url = f"{m_auth.group(1)}{m_auth.group(4)}"

    # 3. Query 参数 (?a=1&password=xxx&user=yyy)
    if "?" in url:
        base, qs = url.split("?", 1)
        kept_pairs: List[str] = []
        for part in qs.split("&"):
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                kl = k.strip().lower()
                if _is_secret_key(kl):
                    if not extracted_pwd and kl in ("password", "passwd", "pwd"):
                        extracted_pwd = unquote(v)
                    continue
                if kl in _JDBC_USER_PARAM_NAMES:
                    if not extracted_user:
                        extracted_user = unquote(v)
                    continue
            kept_pairs.append(part)
        url = f"{base}?{'&'.join(kept_pairs)}" if kept_pairs else base

    # 4. 分号参数格式 (SQLServer / DB2 / Sybase: ;user=sa;password=xxx; 或 DB2 :user=x;password=y;)
    if ";" in url:
        segments = url.split(";")
        kept_segs: List[str] = []
        for idx, seg in enumerate(segments):
            if idx == 0:
                # 兼容 DB2 Type-4 尾部 `:user=xxx` / `:password=xxx` 紧接首段
                m_db2 = re.match(r"^(jdbc:db2://[^:\s]+(?::\d+)?/[^:;\s]+:)(.+)$", seg, re.IGNORECASE)
                if m_db2 and "=" in m_db2.group(2):
                    k, v = m_db2.group(2).split("=", 1)
                    kl = k.strip().lower()
                    if _is_secret_key(kl):
                        if not extracted_pwd and kl in ("password", "passwd", "pwd"):
                            extracted_pwd = v
                        kept_segs.append(m_db2.group(1))
                        continue
                    if kl in _JDBC_USER_PARAM_NAMES:
                        if not extracted_user:
                            extracted_user = v
                        kept_segs.append(m_db2.group(1))
                        continue
                kept_segs.append(seg)
                continue
            if "=" in seg:
                k, v = seg.split("=", 1)
                kl = k.strip().lower()
                if _is_secret_key(kl):
                    if not extracted_pwd and kl in ("password", "passwd", "pwd"):
                        extracted_pwd = v
                    continue
                if kl in _JDBC_USER_PARAM_NAMES:
                    if not extracted_user:
                        extracted_user = v
                    continue
            kept_segs.append(seg)
        # 处理 DB2 前缀以 `:` 结尾后跟非空分号项的拼接
        if kept_segs and kept_segs[0].endswith(":") and len(kept_segs) > 1:
            rest = [s for s in kept_segs[1:] if s != ""]
            if rest:
                trailing_semi = ";" if raw.endswith(";") else ""
                url = kept_segs[0] + ";".join(rest) + trailing_semi
            else:
                url = kept_segs[0].rstrip(":")
        else:
            url = ";".join(kept_segs)

    return url, extracted_user, extracted_pwd


def split_extra_props_secrets(extra_props: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """将扩展连接属性拆分为公开拓扑属性与私密凭据属性，防止敏感键值落入共享工作区."""
    public_props: Dict[str, Any] = {}
    secret_props: Dict[str, Any] = {}
    if not isinstance(extra_props, dict):
        return public_props, secret_props
    for k, v in extra_props.items():
        ks = str(k or "").strip()
        if not ks:
            continue
        low = ks.lower()
        if any(tok in low for tok in _SECRET_PROP_TOKENS):
            secret_props[ks] = v
        else:
            public_props[ks] = v
    return public_props, secret_props


def compute_connection_fingerprint(
    *,
    driver_track: str = "NATIVE",
    db_type: str = "",
    host: str = "",
    port: int = 0,
    dbname: str = "",
    jdbc_url: str = "",
    sqlite_path: str = "",
) -> str:
    """计算数据源端点的无密码标准化指纹，用于跨工作区自动匹配本机个人凭据及合并连接映射."""
    dt = (db_type or "").strip().upper()
    track = (driver_track or "NATIVE").strip().upper()
    if dt == "SQLITE":
        sp = (sqlite_path or dbname or "").strip()
        if not sp:
            return ""
        return f"SQLITE|{os.path.normcase(sp)}"
    clean_jdbc, _, _ = sanitize_jdbc_url(jdbc_url)
    if track == "JDBC" and clean_jdbc:
        return f"JDBC|{dt}|{clean_jdbc.lower()}"
    h = (host or "").strip().lower()
    if not h:
        if clean_jdbc:
            return f"JDBC|{dt}|{clean_jdbc.lower()}"
        return ""
    p = int(port or 0)
    dbn = (dbname or "").strip().lower()
    return f"{dt}|{h}|{p}|{dbn}"


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
    standard_format: str = "AUTO"
    created_at: str = ""
    updated_at: str = ""
    credential_status: str = "BOUND"  # BOUND | AUTO_MATCHED | MISSING

    def endpoint_fingerprint(self) -> str:
        return compute_connection_fingerprint(
            driver_track=self.driver_track,
            db_type=self.db_type,
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            jdbc_url=self.jdbc_url,
            sqlite_path=self.sqlite_path,
        )

    def effective_default_schema(self) -> str:
        """解析当前数据库方言下的真实默认 Schema（兼容 SQLite main、MySQL dbname 与 Inceptor default）."""
        dt = (self.db_type or "").strip().upper()
        raw = (self.default_schema or "").strip()
        dbn = (self.dbname or "").strip()
        if dt == "SQLITE":
            return "main" if raw.lower() in ("", "public") else raw
        if dt in ("MYSQL", "MARIADB", "OCEANBASE", "DORIS", "STARROCKS", "TIDB"):
            if raw and (raw.lower() != "public" or not dbn):
                return raw
            return dbn or raw or "public"
        if dt == "INCEPTOR":
            if raw and raw.lower() != "public":
                return raw
            return dbn or "default"
        return raw or "public"

    def to_public_dict(self) -> Dict[str, Any]:
        """输出脱敏后的前端安全字典（绝不直接暴露明文密码）."""
        public_props, _ = split_extra_props_secrets(self.extra_props)
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
            "credential_status": self.credential_status or "BOUND",
            "sslmode": self.sslmode,
            "auth_mode": self.auth_mode,
            "sqlite_path": self.sqlite_path,
            "jdbc_driver_id": self.jdbc_driver_id,
            "jdbc_url": sanitize_jdbc_url(self.jdbc_url)[0],
            "jdbc_driver_class": self.jdbc_driver_class,
            "extra_props": public_props,
            "standard_format": self.standard_format or "AUTO",
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


def mask_secret_key(secret: str) -> str:
    """对敏感 API 密钥进行不可逆前端脱敏显示."""
    raw = (secret or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "••••••••"
    return f"{raw[:4]}••••••••{raw[-4:]}"


@dataclass
class LLMProviderProfile:
    """全局 AI 大模型服务商配置档案（OpenAI 兼容协议统一管理）."""

    provider_id: str
    name: str
    provider_type: str = "OPENAI_COMPATIBLE"  # DEEPSEEK | GEMINI | OPENAI_COMPATIBLE
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    models: List[str] = field(default_factory=list)
    is_default: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_public_dict(self) -> Dict[str, Any]:
        """输出严格脱敏后的前端安全字典（绝不明文回传 api_key）."""
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "provider_type": self.provider_type,
            "enabled": bool(self.enabled),
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key),
            "api_key_masked": mask_secret_key(self.api_key),
            "default_model": self.default_model,
            "models": list(self.models or []),
            "is_default": bool(self.is_default),
            "extra": dict(self.extra or {}),
            "updated_at": self.updated_at,
        }

