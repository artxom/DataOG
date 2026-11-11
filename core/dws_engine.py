"""华为云 GaussDB(DWS) 元数据联动核验与保序字段转换引擎.

核心能力：
1. 本地持久化保存华为 DWS 数据库连接配置（含账号密码，重启免重输）
2. 单连接批量高速查询 DWS 系统目录 (pg_class / pg_namespace / pg_attribute / pg_description / getdistributekey)
3. 智能比对 Excel 贯标清单与数据库内当前表结构（支持忽略大小写与跨 Schema 自动定位目标表），识别「已贯标」「待贯标」「部分完成」「缺失」
4. 双模式保序转换引擎：
   - 模式 A (DIRECT_ALTER)：原生 RENAME COLUMN + ALTER COLUMN TYPE USING + COMMENT ON COLUMN，并在事务内校验 pg_attribute.attnum 顺序完全一致
   - 模式 B (TABLE_REBUILD)：针对 DWS 分布列 (DISTRIBUTE BY)、列存表 (ORIENTATION=COLUMN) 或跨类型限制，严格按照原表 attnum 物理顺序创建影子表、转换迁移数据、校验行数并原子切换表名
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import datetime
import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2 import sql as pg_sql

from core.state_store import StateStore

logger = logging.getLogger("dws_migrator")

# 华为 DWS / GaussDB 系统内置 Schema 排除列表
SYSTEM_SCHEMAS = (
    "pg_catalog",
    "information_schema",
    "cstore",
    "dbms_om",
    "dbe_perf",
    "snapshot",
    "blockchain",
    "pkg_service",
    "dbe_pldeveloper",
    "sqladvisor",
    "pg_auditor",
    "dbe_pldebugger",
    "db4ai",
)


_IDENTIFIER_QUOTE_CHARS = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'


def clean_identifier_input(ident: str) -> str:
    """清理标识符首尾空白、零宽字符、中英文单双引号及括号附注."""
    s = (
        (ident or "")
        .replace("\u200b", "")
        .replace("\u3000", " ")
        .replace("\ufeff", "")
        .strip(_IDENTIFIER_QUOTE_CHARS)
    )
    m = re.match(r"^([^\s(（]+)\s*[(（].*[)）]$", s)
    if m:
        s = m.group(1).strip(_IDENTIFIER_QUOTE_CHARS)
    return s.strip(_IDENTIFIER_QUOTE_CHARS)


def validate_identifier(ident: str, label: str = "标识符") -> str:
    cleaned = clean_identifier_input(ident)
    if not cleaned:
        raise ValueError(f"{label}不能为空")
    return cleaned


def clean_sql_type_input(sql_type: str) -> str:
    """将全角括号/逗号及常见数据类型写法规范化."""
    s = (sql_type or "").replace("\u200b", "").replace("\u3000", " ").strip()
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    s = " ".join(s.split())
    return s


def validate_sql_type(sql_type: str) -> str:
    cleaned = clean_sql_type_input(sql_type)
    if not cleaned:
        raise ValueError("数据类型不能为空")
    return cleaned


def quote_ident_str(ident: str) -> str:
    """格式化目标数据库标识符：标准标识符保持自然无引号格式 (如 loan_status)，避免生成带引号的字段名."""
    validated = validate_identifier(ident)
    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_$]*$", validated):
        return validated
    return '"' + validated.replace('"', '""') + '"'


def format_existing_db_ident(raw_ident: str) -> str:
    """格式化数据库中已存在的原字段名：标准字段名保持无引号，若原字段名已误带引号等特殊字符则用双引号精确匹配以支持修复."""
    s = (raw_ident or "").strip()
    if not s:
        raise ValueError("原字段名不能为空")
    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_$]*$", s):
        return s
    return '"' + s.replace('"', '""') + '"'


def quote_literal_str(text: str) -> str:
    """将文本转义为单引号字符串."""
    val = (text or "").replace("'", "''")
    return f"'{val}'"


def normalize_dws_type(raw_type: str) -> str:
    """将华为 DWS / PostgreSQL / Oracle 兼容模式的各种数据类型同义写法归一化，确保比对准确."""
    if not raw_type:
        return ""
    t = clean_sql_type_input(raw_type).lower()
    t = re.sub(r"\(\s*", "(", t)
    t = re.sub(r"\s*,\s*", ",", t)
    t = re.sub(r"\s+(?:byte|char)\s*\)", ")", t)
    t = re.sub(r"\s*\)", ")", t)

    t = re.sub(r"^character varying\b", "varchar", t)
    t = re.sub(r"^varchar2\b", "varchar", t)
    t = re.sub(r"^nvarchar2\b", "varchar", t)
    t = re.sub(r"^character\b", "char", t)
    t = re.sub(r"^bpchar\b", "char", t)
    t = re.sub(r"^nchar\b", "char", t)
    t = re.sub(r"^decimal\b", "numeric", t)
    t = re.sub(r"^number\b", "numeric", t)
    t = re.sub(r"^int4\b", "integer", t)
    t = re.sub(r"^int\b", "integer", t)
    t = re.sub(r"^int8\b", "bigint", t)
    t = re.sub(r"^int2\b", "smallint", t)
    t = re.sub(r"^int1\b", "tinyint", t)
    t = re.sub(r"^float8\b", "double precision", t)
    t = re.sub(r"^float4\b", "real", t)
    t = re.sub(r"^bool\b", "boolean", t)
    t = re.sub(r"^timestamp without time zone\b", "timestamp", t)
    t = re.sub(r"^timestamp with time zone\b", "timestamptz", t)
    t = re.sub(r"^timestamp\(\d+\) without time zone\b", "timestamp", t)
    t = re.sub(r"^timestamp\(\d+\) with time zone\b", "timestamptz", t)
    t = re.sub(r"^timestamp\(\d+\)$", "timestamp", t)
    t = re.sub(r"^time without time zone\b", "time", t)
    t = re.sub(r"^time with time zone\b", "timetz", t)
    t = re.sub(r"^time\(\d+\) without time zone\b", "time", t)
    t = re.sub(r"^time\(\d+\) with time zone\b", "timetz", t)
    t = re.sub(r"^time\(\d+\)$", "time", t)
    return t


def format_reloptions_with_clause(raw_reloptions: Any) -> str:
    """将 pg_class.reloptions 或 WITH 子句规范化，自动为含空格的参数值（如 period=1 week, ttl=30 days）补充单引号."""
    if isinstance(raw_reloptions, (list, tuple)):
        s = ", ".join(str(x).strip() for x in raw_reloptions if str(x).strip())
    else:
        s = str(raw_reloptions or "").strip()
    if not s:
        return ""
    if s.upper().startswith("WITH"):
        m = re.match(r"^WITH\s*\((.*)\)\s*$", s, flags=re.IGNORECASE | re.DOTALL)
        if m:
            s = m.group(1).strip()
    if not s:
        return ""

    parts: List[str] = []
    for token in s.split(","):
        tok = token.strip()
        if not tok:
            continue
        if "=" in tok:
            k, v = tok.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v and not (v.startswith("'") and v.endswith("'")):
                if not re.match(r"^[a-zA-Z0-9_.-]+$", v):
                    v = "'" + v.strip("'\"") + "'"
            parts.append(f"{k}={v}")
        else:
            parts.append(tok)
    if not parts:
        return ""
    return f"WITH ({', '.join(parts)})"


def _find_matching_paren(text: str, open_idx: int) -> int:
    """从 open_idx ('(') 开始寻找匹配的 ')'，自动跳过单双引号字符串及 SQL 注释."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            i += 1
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
        elif ch == '"':
            i += 1
            while i < n:
                if text[i] == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            nxt_nl = text.find("\n", i + 2)
            if nxt_nl == -1:
                return -1
            i = nxt_nl
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            end_c = text.find("*/", i + 2)
            if end_c == -1:
                return -1
            i = end_c + 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def parse_dws_tabledef(tabledef: str) -> Dict[str, Any]:
    """解析华为 DWS pg_catalog.pg_get_tabledef(oid) 返回的完整建表 DDL，
    提取 WITH (...)、DISTRIBUTE BY ... 以及完整的 PARTITION BY ...（含起始分区边界与周期定义）.
    """
    info: Dict[str, Any] = {
        "with_clause": "",
        "distribute_clause": "",
        "distribute_keys": [],
        "partition_clause": "",
        "partition_keys": [],
    }
    if not tabledef or not tabledef.strip():
        return info

    m_create = re.search(
        r"\bCREATE\s+(?:(?:GLOBAL|LOCAL|TEMP|TEMPORARY|UNLOGGED|ROW|COLUMN)\s+)*TABLE\b",
        tabledef,
        flags=re.IGNORECASE,
    )
    if not m_create:
        return info

    # 定位 CREATE TABLE 表名后的字段定义列表左括号 '('
    n = len(tabledef)
    i = m_create.end()
    open_paren_idx = -1
    while i < n:
        ch = tabledef[i]
        if ch == "(":
            open_paren_idx = i
            break
        if ch == ";":
            break
        if ch == '"':
            i += 1
            while i < n and tabledef[i] != '"':
                i += 1
        i += 1

    if open_paren_idx == -1:
        return info

    close_paren_idx = _find_matching_paren(tabledef, open_paren_idx)
    if close_paren_idx == -1:
        return info

    # 从字段列表右括号 ')' 扫描到 CREATE TABLE 的结束分号 ';'
    j = close_paren_idx + 1
    depth = 0
    semi_idx = n
    while j < n:
        ch = tabledef[j]
        if ch == "'":
            j += 1
            while j < n:
                if tabledef[j] == "'":
                    if j + 1 < n and tabledef[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
        elif ch == '"':
            j += 1
            while j < n:
                if tabledef[j] == '"':
                    if j + 1 < n and tabledef[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth > 0:
                depth -= 1
        elif ch == ";" and depth == 0:
            semi_idx = j
            break
        j += 1

    table_tail = tabledef[close_paren_idx + 1 : semi_idx].strip()
    if not table_tail:
        return info

    # 1. 提取 WITH (...)
    m_with = re.search(r"\bWITH\s*\(", table_tail, flags=re.IGNORECASE)
    if m_with:
        w_open = table_tail.find("(", m_with.start())
        w_close = _find_matching_paren(table_tail, w_open) if w_open != -1 else -1
        if w_close != -1:
            raw_with = table_tail[m_with.start() : w_close + 1]
            info["with_clause"] = format_reloptions_with_clause(raw_with)

    # 2. 提取 DISTRIBUTE BY ...
    m_dist = re.search(r"\bDISTRIBUTE\s+BY\s+(\w+)", table_tail, flags=re.IGNORECASE)
    if m_dist:
        dist_mode = m_dist.group(1).upper()
        if dist_mode in ("HASH", "MODULO"):
            d_open = table_tail.find("(", m_dist.end() - len(dist_mode))
            d_close = _find_matching_paren(table_tail, d_open) if d_open != -1 else -1
            if d_close != -1:
                raw_keys = table_tail[d_open + 1 : d_close]
                keys = [
                    clean_identifier_input(k)
                    for k in raw_keys.split(",")
                    if clean_identifier_input(k)
                ]
                if dist_mode == "HASH":
                    info["distribute_keys"] = keys
                quoted_keys = ", ".join(quote_ident_str(k) for k in keys)
                info["distribute_clause"] = f"DISTRIBUTE BY {dist_mode}({quoted_keys})"
        elif dist_mode in ("REPLICATION", "ROUNDROBIN"):
            info["distribute_clause"] = f"DISTRIBUTE BY {dist_mode}"

    # 3. 提取完整的 PARTITION BY ... 子句（包含 INTERVAL / 起始分区 START..END..EVERY / VALUES LESS THAN / ENABLE ROW MOVEMENT）
    m_part = re.search(r"\bPARTITION\s+BY\b", table_tail, flags=re.IGNORECASE)
    if m_part:
        part_clause = table_tail[m_part.start() :].strip()
        info["partition_clause"] = part_clause
        m_pkeys = re.match(
            r"^PARTITION\s+BY\s+(?:RANGE|LIST|HASH|VALUES)\s*\(([^)]+)\)",
            part_clause,
            flags=re.IGNORECASE,
        )
        if m_pkeys:
            info["partition_keys"] = [
                clean_identifier_input(k)
                for k in m_pkeys.group(1).split(",")
                if clean_identifier_input(k)
            ]

    return info


def _partition_clause_has_child_defs(partition_clause: str) -> bool:
    """检查 PARTITION BY 子句中是否已包含具体的子分区定义括号块 (PARTITION p1 ...)."""
    if not partition_clause:
        return False
    m = re.match(
        r"^PARTITION\s+BY\s+(?:RANGE|LIST|HASH|VALUES)\s*\([^)]+\)(.*)$",
        partition_clause.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return False
    rest = m.group(1).strip()
    return bool(re.search(r"\b(?:PARTITION|START)\b", rest, flags=re.IGNORECASE))


def _format_partition_boundary_val(raw_bound: str, col_data_type: str = "") -> str:
    """格式化从 pg_partition.boundaries 读取的起始/历史分区边界值."""
    b = (raw_bound or "").strip()
    if not b:
        return "MAXVALUE"
    if b.upper() in ("MAXVALUE", "MINVALUE", "DEFAULT"):
        return b.upper()
    if (b.startswith("'") and b.endswith("'")) or b.startswith("("):
        return b
    if re.match(r"^(?:DATE|TIMESTAMP|TIMESTAMPTZ|TO_DATE)\b", b, flags=re.IGNORECASE):
        return b
    norm_t = normalize_dws_type(col_data_type)
    is_num_col = any(
        norm_t.startswith(prefix)
        for prefix in (
            "integer",
            "bigint",
            "smallint",
            "tinyint",
            "numeric",
            "real",
            "double precision",
            "float",
        )
    )
    if is_num_col and re.match(r"^-?\d+(?:\.\d+)?$", b):
        return b
    return quote_literal_str(b)


def update_partition_clause_columns(
    partition_clause: str, col_rename_map: Dict[str, str]
) -> str:
    """若分区键字段在重建表时被重命名，同步更新 PARTITION BY RANGE/LIST/HASH(...) 中的分区键列名，并完整保留原起始分区边界."""
    if not partition_clause or not partition_clause.strip():
        return ""
    if not col_rename_map:
        return partition_clause.strip()

    def _replace_keys(match: re.Match) -> str:
        prefix = match.group(1)
        raw_keys = match.group(2)
        suffix = match.group(3)
        updated_keys: List[str] = []
        seen: set[str] = set()
        for k in raw_keys.split(","):
            clean_k = clean_identifier_input(k)
            if not clean_k:
                continue
            target_k = col_rename_map.get(clean_k.lower(), clean_k)
            if target_k.lower() not in seen:
                seen.add(target_k.lower())
                updated_keys.append(quote_ident_str(target_k))
        return f"{prefix}{', '.join(updated_keys)}{suffix}"

    return re.sub(
        r"^(PARTITION\s+BY\s+(?:RANGE|LIST|HASH|VALUES)\s*\()([^)]+)(\))",
        _replace_keys,
        partition_clause.strip(),
        count=1,
        flags=re.IGNORECASE,
    )


@dataclass
class ColumnMeta:
    ordinal_position: int
    column_name: str
    data_type: str
    is_not_null: bool = False
    column_default: Optional[str] = None
    column_comment: str = ""
    lineage_source: str = ""


@dataclass
class TableMeta:
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
    object_type: str = "TABLE"
    view_definition: str = ""
    penetrated_base_tables: List[str] = field(default_factory=list)


class DWSMigratorEngine:
    """华为 DWS 数据库连接、元数据批量比对与保序字段转换核心引擎."""

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._conn_lock = threading.Lock()
        self._conn_config: Dict[str, Any] = {
            "configured": False,
            "host": "",
            "port": 8000,
            "dbname": "",
            "user": "",
            "password": "",
            "sslmode": "prefer",
        }
        self._catalog_cache: Dict[str, Any] = {
            "current_schema": "public",
            "schemas": [],
            "tables": [],
        }

        # 从本地 SQLite 直接加载已保存的完整连接信息（含全局 ConnectionHub 命名连接档案回填）
        saved_cfg = self.store.get_meta("dws_conn_saved")
        if isinstance(saved_cfg, dict) and saved_cfg.get("host"):
            for k in ("configured", "host", "port", "dbname", "user", "password", "sslmode"):
                if k in saved_cfg:
                    self._conn_config[k] = saved_cfg[k]
            self._conn_config["configured"] = True
        else:
            active_cid = str(self.store.get_meta("migrator_connection_id", "") or "")
            if active_cid and hasattr(self.store, "get_connection_profile"):
                prof = self.store.get_connection_profile(active_cid)
                if prof and prof.host:
                    self._apply_profile_to_conn_config(prof)

        saved_cat = self.store.get_meta("dws_catalog_cache")
        if isinstance(saved_cat, dict):
            self._catalog_cache = saved_cat

    def _apply_profile_to_conn_config(self, prof: Any) -> None:
        with self._conn_lock:
            self._conn_config = {
                "configured": bool(prof.host and prof.dbname and prof.user),
                "host": prof.host,
                "port": int(prof.port or (5432 if prof.db_type == "PGSQL" else 8000)),
                "dbname": prof.dbname,
                "user": prof.user,
                "password": prof.password or "",
                "sslmode": prof.sslmode or "prefer",
            }

    def is_configured(self) -> bool:
        return bool(
            self._conn_config.get("configured")
            and str(self._conn_config.get("host", "")).strip()
            and str(self._conn_config.get("dbname", "")).strip()
            and str(self._conn_config.get("user", "")).strip()
        )

    def get_public_conn_info(self) -> Dict[str, Any]:
        """返回本地保存的完整连接配置（含密码回填）及已探测到的目标库 Schema/表清单."""
        return {
            "configured": self.is_configured(),
            "host": self._conn_config.get("host", ""),
            "port": self._conn_config.get("port", 8000),
            "dbname": self._conn_config.get("dbname", ""),
            "user": self._conn_config.get("user", ""),
            "password": self._conn_config.get("password", ""),
            "sslmode": self._conn_config.get("sslmode", "prefer"),
            "current_schema": self._catalog_cache.get("current_schema", "public"),
            "schemas": self._catalog_cache.get("schemas", []),
            "discovered_table_count": len(self._catalog_cache.get("tables", [])),
            "discovered_tables": self._catalog_cache.get("tables", [])[:300],
        }

    def configure_connection(
        self,
        *,
        host: str = "",
        port: int = 8000,
        dbname: str = "",
        user: str = "",
        password: str = "",
        sslmode: str = "prefer",
    ) -> Dict[str, Any]:
        """保存数据库连接配置到本地（含账号密码），测试连接并读取目标库全部模式与表清单."""
        clean_host = host.strip()
        clean_port = int(port or 8000)
        clean_db = dbname.strip()
        clean_user = user.strip()
        clean_ssl = sslmode.strip() or "prefer"

        if not clean_host or not clean_db or not clean_user:
            raise ValueError("请完整填写主机地址 (Host)、数据库名称 (Database) 和用户名 (User)")

        with self._conn_lock:
            self._conn_config = {
                "configured": True,
                "host": clean_host,
                "port": clean_port,
                "dbname": clean_db,
                "user": clean_user,
                "password": password,
                "sslmode": clean_ssl,
            }
            # 直接在本地持久化保存账号密码，下次打开无需重新输入
            self.store.save_meta("dws_conn_saved", self._conn_config)

        try:
            logger.info(
                "正在连接华为 DWS 数据库 [%s:%s/%s] (用户: %s)...",
                clean_host,
                clean_port,
                clean_db,
                clean_user,
            )
            with self._open_dws_connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SELECT version();")
                    ver_row = cur.fetchone()
                    ver_str = ver_row[0] if ver_row else "Huawei GaussDB(DWS)"
                    catalog = self._query_dws_catalog_on_cursor(cur)

            self._catalog_cache = catalog
            self.store.save_meta("dws_catalog_cache", catalog)

            schema_cnt = len(catalog.get("schemas", []))
            tbl_cnt = len(catalog.get("tables", []))
            msg = (
                f"成功连接华为 DWS [{clean_host}:{clean_port}/{clean_db}]（账号密码已保存在本地），"
                f"共读取到 {schema_cnt} 个模式、{tbl_cnt} 张数据表"
            )
            logger.info("%s | %s", msg, ver_str)
            self.store.add_log(
                "SUCCESS",
                "数据库连接",
                f"{msg} | 默认模式: {catalog.get('current_schema')} | 内核: {ver_str}",
            )
            return {
                "connected": True,
                "server_version": ver_str,
                "message": msg,
                "catalog": catalog,
            }
        except Exception as exc:
            err_msg = f"连接华为 DWS 失败: {exc}"
            logger.error(err_msg)
            self.store.add_log("ERROR", "数据库连接", err_msg)
            raise RuntimeError(err_msg) from exc

    def _open_dws_connection(self):
        if not self.is_configured():
            raise RuntimeError("尚未配置华为 DWS 数据库连接，请先点击顶部「数据库连接设置」")
        cfg = self._conn_config
        conn = psycopg2.connect(
            host=cfg["host"],
            port=int(cfg["port"]),
            dbname=cfg["dbname"],
            user=cfg["user"],
            password=cfg["password"],
            sslmode=cfg.get("sslmode", "prefer"),
            connect_timeout=8,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 20000;")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        return conn

    def _query_dws_catalog_on_cursor(self, cur: Any) -> Dict[str, Any]:
        """在已打开的 DWS 游标上快速读取当前库的默认 Schema、所有业务 Schema 与表清单."""
        current_schema = "public"
        try:
            cur.execute("SELECT COALESCE(current_schema(), 'public');")
            r = cur.fetchone()
            if r and r[0]:
                current_schema = str(r[0])
        except Exception:
            pass

        cur.execute(
            """
            SELECT nspname
            FROM pg_catalog.pg_namespace
            WHERE nspname NOT IN %s
              AND nspname NOT LIKE 'pg_toast%%'
              AND nspname NOT LIKE 'pg_temp%%'
            ORDER BY
                CASE WHEN nspname = %s THEN 0
                     WHEN nspname = 'public' THEN 1
                     ELSE 2
                END,
                nspname ASC;
            """,
            (SYSTEM_SCHEMAS, current_schema),
        )
        schemas = [str(row[0]) for row in cur.fetchall() if row and row[0]]
        if not schemas:
            schemas = [current_schema]

        cur.execute(
            """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                COALESCE(pg_catalog.obj_description(c.oid, 'pg_class'), '') AS table_comment,
                COALESCE(pg_catalog.array_to_string(c.reloptions, ', '), '') AS reloptions,
                (
                    SELECT COUNT(*)
                    FROM pg_catalog.pg_attribute a
                    WHERE a.attrelid = c.oid
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                ) AS col_count
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN %s
              AND n.nspname NOT LIKE 'pg_toast%%'
              AND n.nspname NOT LIKE 'pg_temp%%'
              AND c.relkind NOT IN ('i', 'I', 'S', 't', 'c')
              AND c.relname NOT LIKE '__mig_%%'
              AND c.relname NOT LIKE '%%__bak_%%'
            ORDER BY
                CASE WHEN n.nspname = %s THEN 0
                     WHEN n.nspname = 'public' THEN 1
                     ELSE 2
                END,
                n.nspname ASC,
                c.relname ASC
            LIMIT 1000;
            """,
            (SYSTEM_SCHEMAS, current_schema),
        )
        tables = []
        for row in cur.fetchall():
            relopts = str(row[3] or "").lower()
            tables.append(
                {
                    "schema_name": str(row[0]),
                    "table_name": str(row[1]),
                    "table_comment": str(row[2] or ""),
                    "orientation": "COLUMN" if "orientation=column" in relopts else "ROW",
                    "column_count": int(row[4] or 0),
                }
            )

        return {
            "current_schema": current_schema,
            "schemas": schemas,
            "tables": tables,
        }

    def discover_database_catalog(self) -> Dict[str, Any]:
        """主动刷新并返回当前连接华为 DWS 数据库中的 Schema 与表结构列表."""
        with self._open_dws_connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                self._catalog_cache = self._query_dws_catalog_on_cursor(cur)
                self.store.save_meta("dws_catalog_cache", self._catalog_cache)
        return self._catalog_cache

    def load_tables_from_database(
        self,
        schema_name: str = "",
        table_name: str = "",
        max_tables: int = 30,
    ) -> int:
        """直接从华为 DWS 数据库中读取真实表结构并加载到工作台清单中."""
        with self._open_dws_connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                params: List[Any] = [SYSTEM_SCHEMAS]
                where_clauses = [
                    "n.nspname NOT IN %s",
                    "n.nspname NOT LIKE 'pg_toast%%'",
                    "n.nspname NOT LIKE 'pg_temp%%'",
                    "c.relkind NOT IN ('i', 'I', 'S', 't', 'c')",
                ]
                if schema_name.strip():
                    where_clauses.append("LOWER(n.nspname) = LOWER(%s)")
                    params.append(schema_name.strip())
                if table_name.strip():
                    where_clauses.append("LOWER(c.relname) = LOWER(%s)")
                    params.append(table_name.strip())

                params.append(int(max_tables))
                sql_tables = f"""
                    SELECT c.oid, n.nspname, c.relname
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY n.nspname ASC, c.relname ASC
                    LIMIT %s;
                """
                cur.execute(sql_tables, tuple(params))
                tbl_rows = cur.fetchall()
                if not tbl_rows:
                    return 0

                oids = [r[0] for r in tbl_rows]
                tbl_by_oid = {r[0]: (str(r[1]), str(r[2])) for r in tbl_rows}

                cur.execute(
                    """
                    SELECT
                        a.attrelid,
                        a.attnum,
                        a.attname,
                        pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                        COALESCE(pg_catalog.col_description(a.attrelid, a.attnum), '') AS column_comment
                    FROM pg_catalog.pg_attribute a
                    WHERE a.attrelid = ANY(%s)
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attrelid, a.attnum ASC;
                    """,
                    (oids,),
                )
                col_rows = cur.fetchall()

        items = []
        for r in col_rows:
            oid, _attnum, col_name, col_type, col_comment = r
            s_name, t_name = tbl_by_oid[oid]
            items.append(
                {
                    "row_index": len(items) + 1,
                    "schema_name": s_name,
                    "table_name": t_name,
                    "old_col_name": str(col_name),
                    "old_col_type": str(col_type),
                    "old_col_comment": str(col_comment or ""),
                    "new_col_name": str(col_name),
                    "new_col_type": str(col_type),
                    "new_col_comment": str(col_comment or ""),
                }
            )

        if items:
            target_desc = (
                f"DWS目标库表 [{schema_name + '.' if schema_name else ''}{table_name or '共' + str(len(tbl_rows)) + '张表'}]"
            )
            self.store.replace_all_items(items, target_desc, preserve_history=False)
            self.sync_all_items_with_db()
            logger.info("已从华为 DWS 目标库读取 %d 张表共 %d 个字段结构。", len(tbl_rows), len(items))
        return len(items)

    def inspect_table_metadata(self, schema_name: str, table_name: str) -> TableMeta:
        """获取单张表在 DWS 中的完整字段顺序 (attnum)、类型、注释、存储属性及分布列信息."""
        validate_identifier(schema_name, "模式名")
        validate_identifier(table_name, "表名")

        batch_map = self._fetch_real_tables_metadata_batch([(schema_name, table_name)])
        key = (schema_name.lower(), table_name.lower())
        return batch_map.get(
            key, TableMeta(schema_name=schema_name, table_name=table_name, exists=False)
        )

    def _fetch_real_tables_metadata_batch(
        self, requested_pairs: List[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], TableMeta]:
        """使用单条 DWS 连接一次性批量查询多张表的完整表结构（含大小写不敏感匹配与跨 Schema 自动定位）."""
        result: Dict[Tuple[str, str], TableMeta] = {}
        if not requested_pairs:
            return result

        lower_table_names = list({t.strip().lower() for _, t in requested_pairs if t.strip()})
        if not lower_table_names:
            return result

        with self._open_dws_connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                current_schema = "public"
                try:
                    cur.execute("SELECT COALESCE(current_schema(), 'public');")
                    cs_row = cur.fetchone()
                    if cs_row and cs_row[0]:
                        current_schema = str(cs_row[0])
                except Exception:
                    pass

                # 1. 一次性查出所有同名候选表（忽略大小写）
                cur.execute(
                    """
                    SELECT
                        c.oid,
                        n.nspname AS schema_name,
                        c.relname AS table_name,
                        COALESCE(pg_catalog.obj_description(c.oid, 'pg_class'), '') AS table_comment,
                        COALESCE(pg_catalog.array_to_string(c.reloptions, ', '), '') AS reloptions
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE LOWER(c.relname) = ANY(%s)
                      AND c.relkind NOT IN ('i', 'I', 'S', 't', 'c')
                      AND n.nspname NOT IN %s
                      AND n.nspname NOT LIKE 'pg_toast%%'
                      AND n.nspname NOT LIKE 'pg_temp%%';
                    """,
                    (lower_table_names, SYSTEM_SCHEMAS),
                )
                candidate_rows = cur.fetchall()

                exact_map: Dict[Tuple[str, str], Any] = {}
                by_table_lower: Dict[str, List[Any]] = {}
                for row in candidate_rows:
                    nsp_l = str(row[1]).lower()
                    rel_l = str(row[2]).lower()
                    exact_map[(nsp_l, rel_l)] = row
                    by_table_lower.setdefault(rel_l, []).append(row)

                login_user_lower = str(self._conn_config.get("user", "")).lower()
                cur_schema_lower = current_schema.lower()

                matched_oid_to_row: Dict[Any, Any] = {}
                pair_to_oid: Dict[Tuple[str, str], Any] = {}

                for req_schema, req_table in requested_pairs:
                    s_l = req_schema.strip().lower()
                    t_l = req_table.strip().lower()
                    chosen_row = exact_map.get((s_l, t_l))

                    if chosen_row is None:
                        # 若按指定的 schema 未找到，自动在数据库实际存在的同名候选表中智能匹配
                        candidates = by_table_lower.get(t_l, [])
                        if len(candidates) == 1:
                            chosen_row = candidates[0]
                        elif len(candidates) > 1:
                            def _schema_priority(r: Any) -> int:
                                cand_s = str(r[1]).lower()
                                if cand_s == cur_schema_lower:
                                    return 0
                                if cand_s == login_user_lower:
                                    return 1
                                if cand_s == "public":
                                    return 2
                                return 3

                            candidates_sorted = sorted(candidates, key=_schema_priority)
                            chosen_row = candidates_sorted[0]

                    if chosen_row is not None:
                        oid = chosen_row[0]
                        matched_oid_to_row[oid] = chosen_row
                        pair_to_oid[(s_l, t_l)] = oid
                    else:
                        result[(s_l, t_l)] = TableMeta(
                            schema_name=req_schema, table_name=req_table, exists=False
                        )

                if not matched_oid_to_row:
                    return result

                matched_oids = list(matched_oid_to_row.keys())

                # 2. 批量查询华为 DWS 分布列 (getdistributekey)
                dist_by_oid: Dict[Any, List[str]] = {}
                try:
                    cur.execute(
                        """
                        SELECT c.oid, pg_catalog.getdistributekey(c.oid)
                        FROM pg_catalog.pg_class c
                        WHERE c.oid = ANY(%s) AND c.relkind = 'r';
                        """,
                        (matched_oids,),
                    )
                    for oid_val, raw_dist in cur.fetchall():
                        if raw_dist:
                            keys = [
                                k.strip().strip('"')
                                for k in str(raw_dist).split(",")
                                if k.strip()
                            ]
                            dist_by_oid[oid_val] = keys
                except Exception:
                    pass

                # 3. 批量查询所有匹配表的字段列表（严格按 attnum 升序）
                cur.execute(
                    """
                    SELECT
                        a.attrelid,
                        a.attnum,
                        a.attname AS column_name,
                        pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                        a.attnotnull AS is_not_null,
                        pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default,
                        COALESCE(pg_catalog.col_description(a.attrelid, a.attnum), '') AS column_comment
                    FROM pg_catalog.pg_attribute a
                    LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE a.attrelid = ANY(%s)
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attrelid, a.attnum ASC;
                    """,
                    (matched_oids,),
                )
                cols_by_oid: Dict[Any, List[ColumnMeta]] = {oid: [] for oid in matched_oids}
                attnum_col_map: Dict[Tuple[Any, int], Tuple[str, str]] = {}
                for r in cur.fetchall():
                    oid_val = r[0]
                    attnum_val = int(r[1])
                    c_name = str(r[2])
                    c_type = str(r[3])
                    attnum_col_map[(oid_val, attnum_val)] = (c_name, c_type)
                    col_list = cols_by_oid.setdefault(oid_val, [])
                    col_list.append(
                        ColumnMeta(
                            ordinal_position=len(col_list) + 1,
                            column_name=c_name,
                            data_type=c_type,
                            is_not_null=bool(r[4]),
                            column_default=str(r[5]) if r[5] is not None else None,
                            column_comment=str(r[6] or ""),
                        )
                    )

                # 4. 批量查询华为 DWS 完整建表定义 pg_get_tabledef(c.oid) 以继承分区表起始分区与周期定义
                tabledef_by_oid: Dict[Any, str] = {}
                try:
                    cur.execute(
                        """
                        SELECT c.oid, pg_catalog.pg_get_tabledef(c.oid)
                        FROM pg_catalog.pg_class c
                        WHERE c.oid = ANY(%s) AND c.relkind = 'r';
                        """,
                        (matched_oids,),
                    )
                    for oid_val, raw_def in cur.fetchall():
                        if raw_def:
                            tabledef_by_oid[oid_val] = str(raw_def)
                except Exception:
                    for oid_val in matched_oids:
                        try:
                            cur.execute(
                                "SELECT pg_catalog.pg_get_tabledef(%s);",
                                (oid_val,),
                            )
                            one_def = cur.fetchone()
                            if one_def and one_def[0]:
                                tabledef_by_oid[oid_val] = str(one_def[0])
                        except Exception:
                            pass

                # 5. 兜底查询 pg_catalog.pg_partition，防止 pg_get_tabledef 不可用或仅输出 period 未输出起始分区边界
                part_catalog_by_oid: Dict[Any, Tuple[str, List[str]]] = {}
                need_part_catalog_oids = [
                    oid_val
                    for oid_val in matched_oids
                    if not _partition_clause_has_child_defs(
                        parse_dws_tabledef(tabledef_by_oid.get(oid_val, "")).get(
                            "partition_clause", ""
                        )
                    )
                ]
                if need_part_catalog_oids:
                    try:
                        cur.execute(
                            """
                            SELECT
                                p.parentid,
                                p.parttype,
                                p.partstrategy,
                                p.relname,
                                CAST(p.partkey AS text) AS partkey_str,
                                COALESCE(pg_catalog.array_to_string(p.interval, ', '), '') AS interval_str,
                                COALESCE(pg_catalog.array_to_string(p.boundaries, ', '), '') AS boundary_str
                            FROM pg_catalog.pg_partition p
                            WHERE p.parentid = ANY(%s)
                              AND p.parttype IN ('r', 'p')
                            ORDER BY p.parentid, p.parttype DESC, p.oid ASC;
                            """,
                            (need_part_catalog_oids,),
                        )
                        part_table_info: Dict[Any, Tuple[str, List[Tuple[str, str]], str]] = {}
                        part_children: Dict[Any, List[Tuple[str, str]]] = {}
                        for p_row in cur.fetchall():
                            p_parent = p_row[0]
                            p_type = str(p_row[1] or "")
                            p_strat = str(p_row[2] or "r").lower()
                            p_relname = str(p_row[3] or "")
                            p_key_str = str(p_row[4] or "").strip()
                            p_interval = str(p_row[5] or "").strip()
                            p_bound = str(p_row[6] or "").strip()
                            if p_type == "r":
                                key_cols: List[Tuple[str, str]] = []
                                for tok in p_key_str.split():
                                    if tok.lstrip("-").isdigit():
                                        col_pair = attnum_col_map.get((p_parent, int(tok)))
                                        if col_pair:
                                            key_cols.append(col_pair)
                                part_table_info[p_parent] = (p_strat, key_cols, p_interval)
                            elif p_type == "p" and p_relname:
                                part_children.setdefault(p_parent, []).append((p_relname, p_bound))

                        for p_parent, (p_strat, key_cols, p_interval) in part_table_info.items():
                            children = part_children.get(p_parent, [])
                            if not key_cols or not children:
                                continue
                            strat_kw = "LIST" if p_strat == "l" else ("HASH" if p_strat == "h" else "RANGE")
                            key_names = [c_name for c_name, _ in key_cols]
                            primary_col_type = key_cols[0][1] if key_cols else ""
                            quoted_pkeys = ", ".join(quote_ident_str(k) for k in key_names)
                            child_defs: List[str] = []
                            for c_relname, c_bound in children:
                                p_name_q = quote_ident_str(c_relname)
                                if strat_kw == "HASH":
                                    child_defs.append(f"PARTITION {p_name_q}")
                                elif strat_kw == "LIST":
                                    vals = ", ".join(
                                        _format_partition_boundary_val(v, primary_col_type)
                                        for v in c_bound.split(",")
                                        if v.strip()
                                    )
                                    child_defs.append(f"PARTITION {p_name_q} VALUES ({vals})")
                                else:
                                    vals = ", ".join(
                                        _format_partition_boundary_val(v, primary_col_type)
                                        for v in c_bound.split(",")
                                        if v.strip()
                                    )
                                    child_defs.append(
                                        f"PARTITION {p_name_q} VALUES LESS THAN ({vals or 'MAXVALUE'})"
                                    )
                            interval_sql = (
                                f" INTERVAL ({quote_literal_str(p_interval.strip(chr(39)))})"
                                if p_interval and p_strat == "i"
                                else ""
                            )
                            reconstructed_clause = (
                                f"PARTITION BY {strat_kw} ({quoted_pkeys}){interval_sql} (\n"
                                + ",\n".join(f"    {cd}" for cd in child_defs)
                                + "\n)"
                            )
                            part_catalog_by_oid[p_parent] = (reconstructed_clause, key_names)
                    except Exception:
                        pass

                meta_by_oid: Dict[Any, TableMeta] = {}
                for oid_val, tbl_row in matched_oid_to_row.items():
                    actual_schema = str(tbl_row[1])
                    actual_table = str(tbl_row[2])
                    table_comment = str(tbl_row[3] or "")
                    reloptions = str(tbl_row[4] or "")
                    parsed_def = parse_dws_tabledef(tabledef_by_oid.get(oid_val, ""))

                    orientation = (
                        "COLUMN"
                        if "orientation=column" in reloptions.lower()
                        or "orientation=column" in parsed_def["with_clause"].lower()
                        else "ROW"
                    )
                    with_clause = (
                        parsed_def["with_clause"]
                        or format_reloptions_with_clause(reloptions)
                    )
                    d_keys = dist_by_oid.get(oid_val) or parsed_def["distribute_keys"] or []
                    distribute_clause = ""
                    if d_keys:
                        quoted = ", ".join(quote_ident_str(k) for k in d_keys)
                        distribute_clause = f"DISTRIBUTE BY HASH({quoted})"
                    elif parsed_def["distribute_clause"]:
                        distribute_clause = parsed_def["distribute_clause"]

                    partition_clause = parsed_def["partition_clause"]
                    partition_keys = list(parsed_def["partition_keys"] or [])
                    if not _partition_clause_has_child_defs(partition_clause) and oid_val in part_catalog_by_oid:
                        partition_clause, cat_pkeys = part_catalog_by_oid[oid_val]
                        if not partition_keys:
                            partition_keys = cat_pkeys

                    meta_by_oid[oid_val] = TableMeta(
                        schema_name=actual_schema,
                        table_name=actual_table,
                        exists=True,
                        table_comment=table_comment,
                        orientation=orientation,
                        with_clause=with_clause,
                        distribute_clause=distribute_clause,
                        distribute_keys=d_keys,
                        partition_clause=partition_clause,
                        partition_keys=partition_keys,
                        columns=cols_by_oid.get(oid_val, []),
                    )

                for pair_key, oid_val in pair_to_oid.items():
                    result[pair_key] = copy.deepcopy(meta_by_oid[oid_val])

        return result

    def sync_all_items_with_db(self) -> Dict[str, Any]:
        """将当前导入的清单与华为 DWS 数据库元数据做全量批量实时比对."""
        items = self.store.list_items()
        if not items:
            return {"summary": self.store.get_summary(), "items": []}

        if not self.is_configured():
            self.store.add_log(
                "INFO",
                "等待连接数据库",
                f"已载入 {len(items)} 条清单记录，请先配置华为 DWS 数据库连接以核验表结构。",
            )
            return {"summary": self.store.get_summary(), "items": items}

        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for item in items:
            key = (item["schema_name"].strip().lower(), item["table_name"].strip().lower())
            grouped.setdefault(key, []).append(item)

        req_pairs = [
            (grp[0]["schema_name"], grp[0]["table_name"]) for grp in grouped.values()
        ]
        table_meta_map = self._fetch_real_tables_metadata_batch(req_pairs)

        batch_updates: List[Dict[str, Any]] = []
        for pair_key, group_items in grouped.items():
            table_meta = table_meta_map.get(
                pair_key,
                TableMeta(
                    schema_name=group_items[0]["schema_name"],
                    table_name=group_items[0]["table_name"],
                    exists=False,
                ),
            )
            claimed_old_names = {
                clean_identifier_input(x["old_col_name"]).lower()
                for x in group_items
                if clean_identifier_input(x["old_col_name"])
            }
            for item in group_items:
                upd = self._build_single_item_sync_update(
                    item, table_meta, claimed_old_names=claimed_old_names
                )
                batch_updates.append(upd)

        self.store.batch_update_items_sync(batch_updates)
        self.store.add_log(
            "INFO",
            "元数据联动核验",
            f"已完成 {len(items)} 条字段规则（涉及 {len(grouped)} 张表）与华为 DWS 数据库的实时比对。",
        )
        return {
            "summary": self.store.get_summary(),
            "items": self.store.list_items(),
        }

    def _build_single_item_sync_update(
        self,
        item: Dict[str, Any],
        table_meta: TableMeta,
        claimed_old_names: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        item_id = item["id"]
        if not table_meta.exists:
            avail_schemas = self._catalog_cache.get("schemas", [])
            schema_hint = (
                f"（当前库可用模式: {', '.join(avail_schemas[:8])}）" if avail_schemas else ""
            )
            return {
                "item_id": item_id,
                "status": "MISSING_TABLE",
                "status_detail": f"数据库中未找到表 {table_meta.schema_name}.{table_meta.table_name} {schema_hint}".strip(),
                "ordinal_position": None,
                "total_columns": 0,
            }

        actual_schema = table_meta.schema_name
        actual_table = table_meta.table_name

        old_name = clean_identifier_input(item["old_col_name"])
        new_name = clean_identifier_input(item["new_col_name"]) or old_name
        new_type = item["new_col_type"].strip() or item["old_col_type"].strip()
        new_comment = item["new_col_comment"].strip()

        col_by_lower: Dict[str, ColumnMeta] = {}
        for c in table_meta.columns:
            col_by_lower.setdefault(clean_identifier_input(c.column_name).lower(), c)
            col_by_lower[c.column_name.lower()] = c

        total_cols = len(table_meta.columns)
        dist_keys_lower = {
            clean_identifier_input(k).lower() for k in table_meta.distribute_keys
        } | {clean_identifier_input(k).lower() for k in table_meta.partition_keys}

        old_col = col_by_lower.get(old_name.lower())
        new_col = col_by_lower.get(new_name.lower())

        matched_col: Optional[ColumnMeta] = None
        if old_name.lower() != new_name.lower():
            if old_col is not None:
                matched_col = old_col
            elif new_col is not None and new_name.lower() not in (claimed_old_names or set()):
                matched_col = new_col
        else:
            matched_col = old_col or new_col

        if matched_col is None:
            sample_cols = ", ".join(c.column_name for c in table_meta.columns[:6])
            more_hint = "..." if total_cols > 6 else ""
            return {
                "item_id": item_id,
                "schema_name": actual_schema,
                "table_name": actual_table,
                "status": "MISSING_COLUMN",
                "status_detail": (
                    f"表 {actual_schema}.{actual_table} 共 {total_cols} 列 ({sample_cols}{more_hint})，"
                    f"未找到原字段 [{old_name}] 或新字段 [{new_name}]"
                ),
                "ordinal_position": None,
                "total_columns": total_cols,
                "table_orientation": table_meta.orientation,
            }

        is_dist = clean_identifier_input(matched_col.column_name).lower() in dist_keys_lower
        effective_new_type = new_type or matched_col.data_type
        type_matches_new = normalize_dws_type(matched_col.data_type) == normalize_dws_type(
            effective_new_type
        )
        comment_matches_new = (
            True if new_comment == "" else (matched_col.column_comment == new_comment)
        )
        # 确保库内字段名不仅是同名，且本身不包含多余引号包裹 (matched_col.column_name == clean_identifier_input(...))
        name_matches_new = (
            matched_col.column_name.lower() == new_name.lower()
            and matched_col.column_name == clean_identifier_input(matched_col.column_name)
        )

        item_with_actual_tbl = dict(item)
        item_with_actual_tbl["schema_name"] = actual_schema
        item_with_actual_tbl["table_name"] = actual_table
        sql_preview = self.build_direct_alter_sql_preview(
            item=item_with_actual_tbl, matched_col=matched_col, table_meta=table_meta
        )

        if name_matches_new and type_matches_new and comment_matches_new:
            return {
                "item_id": item_id,
                "schema_name": actual_schema,
                "table_name": actual_table,
                "status": "COMPLETED",
                "status_detail": (
                    f"已贯标完成（固定在第 {matched_col.ordinal_position}/{total_cols} 列，顺序一致）"
                ),
                "ordinal_position": matched_col.ordinal_position,
                "total_columns": total_cols,
                "db_current_col_name": matched_col.column_name,
                "db_current_type": matched_col.data_type,
                "db_current_comment": matched_col.column_comment,
                "is_distribute_key": is_dist,
                "table_orientation": table_meta.orientation,
                "generated_sql": sql_preview,
                "clear_error": True,
            }

        prev_status = item.get("status")
        prev_error = item.get("error_message", "")

        if name_matches_new and (not type_matches_new or not comment_matches_new):
            diffs = []
            if not type_matches_new:
                diffs.append(f"当前类型 [{matched_col.data_type}] -> 目标 [{effective_new_type}]")
            if not comment_matches_new:
                diffs.append(
                    f"当前注释 [{matched_col.column_comment or '空'}] -> 目标 [{new_comment}]"
                )
            detail_msg = f"字段名已是 [{matched_col.column_name}]，待更新: " + "；".join(diffs)
            new_status = "FAILED" if (prev_status == "FAILED" and prev_error) else "PARTIAL"
        else:
            dist_hint = (
                "（注意：该列为分布列，直接修改类型受限，建议使用原序重建表）" if is_dist else ""
            )
            detail_msg = (
                f"待贯标：位于表内第 {matched_col.ordinal_position}/{total_cols} 列{dist_hint}"
            )
            new_status = "FAILED" if (prev_status == "FAILED" and prev_error) else "PENDING"

        return {
            "item_id": item_id,
            "schema_name": actual_schema,
            "table_name": actual_table,
            "status": new_status,
            "status_detail": detail_msg,
            "ordinal_position": matched_col.ordinal_position,
            "total_columns": total_cols,
            "db_current_col_name": matched_col.column_name,
            "db_current_type": matched_col.data_type,
            "db_current_comment": matched_col.column_comment,
            "is_distribute_key": is_dist,
            "table_orientation": table_meta.orientation,
            "generated_sql": sql_preview,
            "clear_error": False,
        }

    def build_direct_alter_sql_preview(
        self,
        item: Dict[str, Any],
        matched_col: Optional[ColumnMeta] = None,
        table_meta: Optional[TableMeta] = None,
    ) -> str:
        """生成保证字段顺序不变的原生 DWS ALTER TABLE SQL 预览语句."""
        schema_q = quote_ident_str(item["schema_name"])
        table_q = quote_ident_str(item["table_name"])
        full_table = f"{schema_q}.{table_q}"

        current_name = (
            matched_col.column_name
            if matched_col is not None
            else (item.get("db_current_col_name") or clean_identifier_input(item["old_col_name"]))
        )
        current_type = (
            matched_col.data_type
            if matched_col is not None
            else (item.get("db_current_type") or item["old_col_type"] or "varchar(64)")
        )
        current_comment = (
            matched_col.column_comment
            if matched_col is not None
            else (item.get("db_current_comment") or item["old_col_comment"])
        )

        new_name = clean_identifier_input(item["new_col_name"]) or clean_identifier_input(current_name)
        new_type = clean_sql_type_input(item["new_col_type"] or current_type) or current_type
        new_comment = item["new_col_comment"].strip()

        cur_col_q = format_existing_db_ident(current_name)
        new_col_q = quote_ident_str(new_name)

        pos_info = (
            f"第 {matched_col.ordinal_position}/{len(table_meta.columns)} 列"
            if (matched_col and table_meta)
            else "原位置"
        )
        lines = [
            f"-- [模式1: 原生无损 ALTER] 目标表: {full_table} | 字段固定位置: {pos_info} (attnum 保持不变)",
            "BEGIN;",
        ]

        if (
            current_name.lower() != new_name.lower()
            or current_name != clean_identifier_input(current_name)
        ):
            lines.append(
                f"ALTER TABLE {full_table} RENAME COLUMN {cur_col_q} TO {new_col_q};"
            )

        if normalize_dws_type(current_type) != normalize_dws_type(new_type):
            lines.append(
                f"ALTER TABLE {full_table} ALTER COLUMN {new_col_q} TYPE {new_type} USING {new_col_q}::{new_type};"
            )

        if new_comment != "" and current_comment != new_comment:
            lines.append(
                f"COMMENT ON COLUMN {full_table}.{new_col_q} IS {quote_literal_str(new_comment)};"
            )

        lines.append("COMMIT;")
        return "\n".join(lines)

    def _plan_table_items_for_columns(
        self,
        table_meta: TableMeta,
        table_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """对单张表下的多个贯标字段任务进行精确物理列绑定、重命名依赖拓扑排序与冲突去重编排，
        彻底杜绝单表多字段并发/同批次执行时出现：
        - ERROR: column "xxx" specified more than once (重建表或分布键中同一列名重复出现)
        - ERROR: column "xxx" of relation "yyy" already exists (多字段改名链 A->B, B->C 执行顺序颠倒或两列改名为同一目标名)
        """
        col_by_lower: Dict[str, ColumnMeta] = {}
        col_by_ordinal: Dict[int, ColumnMeta] = {}
        for col in table_meta.columns:
            col_by_ordinal[col.ordinal_position] = col
            col_by_lower.setdefault(clean_identifier_input(col.column_name).lower(), col)
            col_by_lower[col.column_name.lower()] = col

        claimed_old_names = {
            clean_identifier_input(it["old_col_name"]).lower()
            for it in table_items
            if it.get("status") != "COMPLETED"
            and clean_identifier_input(it["old_col_name"]).lower() in col_by_lower
        }

        mod_by_ordinal: Dict[int, Dict[str, Any]] = {}
        bound_items_by_ordinal: Dict[int, List[Dict[str, Any]]] = {}
        missing_items: List[Dict[str, Any]] = []
        conflict_items: List[Tuple[Dict[str, Any], str]] = []

        for it in table_items:
            if it.get("status") == "COMPLETED":
                continue
            old_clean = clean_identifier_input(it["old_col_name"])
            new_clean = clean_identifier_input(it["new_col_name"]) or old_clean
            cur_clean = clean_identifier_input(it.get("db_current_col_name") or "")

            matched_col = col_by_lower.get(old_clean.lower())
            if matched_col is None and cur_clean and cur_clean.lower() not in claimed_old_names:
                matched_col = col_by_lower.get(cur_clean.lower())
            if matched_col is None and new_clean.lower() not in claimed_old_names:
                matched_col = col_by_lower.get(new_clean.lower())

            if matched_col is None:
                missing_items.append(it)
                continue

            ord_pos = matched_col.ordinal_position
            if ord_pos in mod_by_ordinal:
                existing_it = mod_by_ordinal[ord_pos]
                existing_new = (
                    clean_identifier_input(existing_it["new_col_name"])
                    or clean_identifier_input(existing_it["old_col_name"])
                )
                if existing_new.lower() == new_clean.lower():
                    merged = dict(existing_it)
                    if (it.get("new_col_type") or "").strip():
                        merged["new_col_type"] = it["new_col_type"]
                    if (it.get("new_col_comment") or "").strip():
                        merged["new_col_comment"] = it["new_col_comment"]
                    mod_by_ordinal[ord_pos] = merged
                    bound_items_by_ordinal[ord_pos].append(it)
                else:
                    conflict_items.append(
                        (
                            it,
                            f"字段编排冲突：同一原字段 [{matched_col.column_name}] 存在多个不同的目标字段名 ({existing_new} vs {new_clean})",
                        )
                    )
            else:
                mod_by_ordinal[ord_pos] = dict(it)
                bound_items_by_ordinal[ord_pos] = [it]

        # Fixed-point 检查：确保所有物理列改造后的最终列名两两唯一，不与未改名现有列或其他改名目标列撞名
        while True:
            conflict_ord = None
            conflict_reason = ""
            seen_targets: Dict[str, Tuple[int, str]] = {}

            # 1) 未被纳入改造的物理列固定占用其自身列名
            for col in sorted(table_meta.columns, key=lambda c: c.ordinal_position):
                if col.ordinal_position not in mod_by_ordinal:
                    nat_name = clean_identifier_input(col.column_name) or col.column_name
                    seen_targets[nat_name.lower()] = (
                        col.ordinal_position,
                        f"表中现有未改名字段 [{nat_name}](第{col.ordinal_position}列)",
                    )

            # 2) 检查所有纳入改造的物理列的目标列名是否与未改名列或其他改造列冲突
            for ord_pos in sorted(mod_by_ordinal.keys()):
                col = col_by_ordinal[ord_pos]
                it = mod_by_ordinal[ord_pos]
                target_name = (
                    clean_identifier_input(it["new_col_name"])
                    or clean_identifier_input(col.column_name)
                    or col.column_name
                )
                t_lower = target_name.lower()
                if t_lower in seen_targets:
                    other_ord, other_desc = seen_targets[t_lower]
                    if other_ord != ord_pos:
                        conflict_ord = ord_pos
                        conflict_reason = (
                            f"目标字段名 [{target_name}] 冲突：原字段 [{col.column_name}](第{ord_pos}列) "
                            f"拟改名为 [{target_name}]，但与 {other_desc} 重名，会导致 column already exists / specified more than once"
                        )
                        break
                else:
                    seen_targets[t_lower] = (
                        ord_pos,
                        f"原字段 [{col.column_name}](第{ord_pos}列 -> {target_name})",
                    )

            if conflict_ord is not None:
                bad_items = bound_items_by_ordinal.pop(conflict_ord, [])
                mod_by_ordinal.pop(conflict_ord, None)
                for b_it in bad_items:
                    conflict_items.append((b_it, conflict_reason))
            else:
                break

        # 对 DIRECT_ALTER 执行顺序进行拓扑排序：若列 P 的目标名等于列 Q 的当前名（如 A->B 且 B->C），必须先执行 Q (B->C) 再执行 P (A->B)
        current_name_to_ord: Dict[str, int] = {}
        for ord_pos in mod_by_ordinal:
            col = col_by_ordinal[ord_pos]
            current_name_to_ord[clean_identifier_input(col.column_name).lower()] = ord_pos
            current_name_to_ord[col.column_name.lower()] = ord_pos

        deps: Dict[int, List[int]] = {ord_pos: [] for ord_pos in mod_by_ordinal}
        in_degree: Dict[int, int] = {ord_pos: 0 for ord_pos in mod_by_ordinal}

        for ord_pos, it in mod_by_ordinal.items():
            col = col_by_ordinal[ord_pos]
            target_lower = (
                clean_identifier_input(it["new_col_name"])
                or clean_identifier_input(col.column_name)
                or col.column_name
            ).lower()
            blocking_ord = current_name_to_ord.get(target_lower)
            if blocking_ord is not None and blocking_ord != ord_pos:
                # blocking_ord 必须先改名腾出位置，ord_pos 才能改名为 target_lower
                deps[blocking_ord].append(ord_pos)
                in_degree[ord_pos] += 1

        ready = sorted([o for o, deg in in_degree.items() if deg == 0])
        ordered_ordinals: List[int] = []
        while ready:
            curr = ready.pop(0)
            ordered_ordinals.append(curr)
            for nxt in sorted(deps[curr]):
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    ready.append(nxt)
                    ready.sort()

        has_rename_cycle = len(ordered_ordinals) < len(mod_by_ordinal)
        if has_rename_cycle:
            for ord_pos in sorted(mod_by_ordinal.keys()):
                if ord_pos not in ordered_ordinals:
                    ordered_ordinals.append(ord_pos)

        return {
            "mod_by_ordinal": mod_by_ordinal,
            "bound_items_by_ordinal": bound_items_by_ordinal,
            "ordered_ordinals": ordered_ordinals,
            "has_rename_cycle": has_rename_cycle,
            "missing_items": missing_items,
            "conflict_items": conflict_items,
        }

    def build_table_rebuild_sql_preview(
        self,
        schema_name: str,
        table_name: str,
        items_for_table: List[Dict[str, Any]],
        keep_backup: bool = True,
        table_meta_override: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """生成严格按照原表 pg_attribute.attnum 顺序重建表并完成类型转换与数据迁移的完整事务 SQL."""
        table_meta = (
            table_meta_override
            if table_meta_override is not None
            else self.inspect_table_metadata(schema_name, table_name)
        )
        if not table_meta.exists:
            raise ValueError(f"数据库中不存在表 {schema_name}.{table_name}，无法生成重建 SQL")

        actual_schema = table_meta.schema_name
        actual_table = table_meta.table_name

        col_plan = self._plan_table_items_for_columns(table_meta, items_for_table)
        mod_by_ordinal: Dict[int, Dict[str, Any]] = col_plan["mod_by_ordinal"]

        ts_suffix = datetime.datetime.now().strftime("%m%d%H%M%S")
        tmp_table_name = f"{actual_table[:42]}__mig_{ts_suffix}"
        bak_table_name = f"{actual_table[:42]}__bak_{ts_suffix}"

        schema_q = quote_ident_str(actual_schema)
        orig_table_q = quote_ident_str(actual_table)
        tmp_table_q = quote_ident_str(tmp_table_name)
        bak_table_q = quote_ident_str(bak_table_name)

        full_orig = f"{schema_q}.{orig_table_q}"
        full_tmp = f"{schema_q}.{tmp_table_q}"

        col_defs: List[str] = []
        insert_cols: List[str] = []
        select_exprs: List[str] = []
        comment_stmts: List[Tuple[str, str]] = []
        column_order_comparison: List[Dict[str, Any]] = []
        col_rename_map: Dict[str, str] = {}

        for col in sorted(table_meta.columns, key=lambda c: c.ordinal_position):
            matched_item = mod_by_ordinal.get(col.ordinal_position)
            src_col_q = format_existing_db_ident(col.column_name)
            orig_clean = clean_identifier_input(col.column_name) or col.column_name
            if matched_item:
                target_name = (
                    clean_identifier_input(matched_item["new_col_name"])
                    or orig_clean
                )
                target_type = validate_sql_type(
                    matched_item["new_col_type"].strip() or col.data_type
                )
                target_comment = (
                    matched_item["new_col_comment"].strip()
                    if matched_item.get("new_col_comment", "").strip() != ""
                    else col.column_comment
                )
                not_null_sql = " NOT NULL" if col.is_not_null else ""
                default_sql = f" DEFAULT {col.column_default}" if col.column_default else ""
                col_defs.append(
                    f"    {quote_ident_str(target_name)} {target_type}{default_sql}{not_null_sql} /* 第{col.ordinal_position}列 (已转换) */"
                )
                insert_cols.append(quote_ident_str(target_name))
                if normalize_dws_type(col.data_type) != normalize_dws_type(target_type):
                    select_exprs.append(
                        f"CAST({src_col_q} AS {target_type}) AS {quote_ident_str(target_name)}"
                    )
                else:
                    select_exprs.append(
                        f"{src_col_q} AS {quote_ident_str(target_name)}"
                    )
                if target_comment:
                    comment_stmts.append((target_name, target_comment))

                col_rename_map[col.column_name.lower()] = target_name
                col_rename_map[orig_clean.lower()] = target_name
                column_order_comparison.append(
                    {
                        "ordinal_position": col.ordinal_position,
                        "changed": True,
                        "before_name": col.column_name,
                        "before_type": col.data_type,
                        "before_comment": col.column_comment,
                        "after_name": target_name,
                        "after_type": target_type,
                        "after_comment": target_comment,
                    }
                )
            else:
                natural_col_name = orig_clean
                not_null_sql = " NOT NULL" if col.is_not_null else ""
                default_sql = f" DEFAULT {col.column_default}" if col.column_default else ""
                col_defs.append(
                    f"    {quote_ident_str(natural_col_name)} {col.data_type}{default_sql}{not_null_sql} /* 第{col.ordinal_position}列 (原样保留) */"
                )
                insert_cols.append(quote_ident_str(natural_col_name))
                select_exprs.append(src_col_q)
                if col.column_comment:
                    comment_stmts.append((natural_col_name, col.column_comment))
                col_rename_map[col.column_name.lower()] = natural_col_name
                col_rename_map[natural_col_name.lower()] = natural_col_name
                column_order_comparison.append(
                    {
                        "ordinal_position": col.ordinal_position,
                        "changed": False,
                        "before_name": col.column_name,
                        "before_type": col.data_type,
                        "before_comment": col.column_comment,
                        "after_name": natural_col_name,
                        "after_type": col.data_type,
                        "after_comment": col.column_comment,
                    }
                )

        # 一次性映射分布键并去重，防止链式改名（A->B, B->C）导致分布键被二次覆盖或重复
        updated_dist_keys: List[str] = []
        for raw_k in table_meta.distribute_keys:
            clean_k = clean_identifier_input(raw_k)
            mapped_k = col_rename_map.get(clean_k.lower(), clean_k)
            if mapped_k.lower() not in {x.lower() for x in updated_dist_keys}:
                updated_dist_keys.append(mapped_k)

        with_sql = f"\n{table_meta.with_clause}" if table_meta.with_clause else ""
        if updated_dist_keys:
            quoted_dist = ", ".join(quote_ident_str(k) for k in updated_dist_keys)
            dist_sql = f"\nDISTRIBUTE BY HASH({quoted_dist})"
        elif table_meta.distribute_clause:
            dist_sql = f"\n{table_meta.distribute_clause}"
        else:
            dist_sql = ""

        updated_partition_clause = update_partition_clause_columns(
            table_meta.partition_clause, col_rename_map
        )
        part_sql = f"\n{updated_partition_clause}" if updated_partition_clause else ""

        sql_lines = [
            "-- ============================================================================",
            f"-- [模式2: 华为 DWS 按原表物理顺序 (attnum 1..{len(table_meta.columns)}) 重建表与数据迁移脚本]",
            "-- 适用场景: 分布列修改类型、列存表跨类型转换、保证全表字段顺序与原表 100% 一致",
            "-- ============================================================================",
            "BEGIN;",
            "",
            "-- Step 1: 严格按照原表 attnum (1..N) 字段顺序创建新结构临时表",
            f"CREATE TABLE {full_tmp} (",
            ",\n".join(col_defs),
            f"){with_sql}{dist_sql}{part_sql};",
            "",
            "-- Step 2: 按精确列顺序执行类型转换并灌入全量数据",
            f"INSERT INTO {full_tmp} ({', '.join(insert_cols)})",
            "SELECT\n    " + ",\n    ".join(select_exprs),
            f"FROM {full_orig};",
            "",
            "-- Step 3: 恢复表级注释与全部字段注释",
        ]

        if table_meta.table_comment:
            sql_lines.append(
                f"COMMENT ON TABLE {full_tmp} IS {quote_literal_str(table_meta.table_comment)};"
            )
        for col_n, col_c in comment_stmts:
            sql_lines.append(
                f"COMMENT ON COLUMN {full_tmp}.{quote_ident_str(col_n)} IS {quote_literal_str(col_c)};"
            )

        sql_lines.append("")
        sql_lines.append("-- Step 4: 原子切换表名（保持原表名对外服务，全部字段顺序与原表完全一致）")
        if keep_backup:
            sql_lines.append(
                f"ALTER TABLE {full_orig} RENAME TO {bak_table_q}; -- 原表保留为备份表"
            )
        else:
            sql_lines.append(f"DROP TABLE {full_orig};")
        sql_lines.append(f"ALTER TABLE {full_tmp} RENAME TO {orig_table_q};")
        sql_lines.append("COMMIT;")

        return {
            "schema_name": actual_schema,
            "table_name": actual_table,
            "tmp_table_name": tmp_table_name,
            "bak_table_name": bak_table_name if keep_backup else "",
            "total_columns": len(table_meta.columns),
            "sql_preview": "\n".join(sql_lines),
            "column_order_comparison": column_order_comparison,
            "col_plan": col_plan,
            "table_meta": {
                "orientation": table_meta.orientation,
                "with_clause": table_meta.with_clause,
                "distribute_clause": table_meta.distribute_clause,
                "distribute_keys": table_meta.distribute_keys,
                "partition_clause": updated_partition_clause,
                "partition_keys": table_meta.partition_keys,
            },
        }

    def execute_single_item_direct(self, item_id: int) -> Dict[str, Any]:
        """使用模式 1 (原生 ALTER TABLE) 执行单条字段贯标，并严格核验前后字段顺序 attnum 一致."""
        item = self.store.get_item(item_id)
        if not item:
            raise ValueError(f"未找到 ID={item_id} 的贯标记录")

        schema_name = validate_identifier(item["schema_name"], "模式名")
        table_name = validate_identifier(item["table_name"], "表名")
        old_name = validate_identifier(item["old_col_name"], "原字段名")
        new_name = validate_identifier(item["new_col_name"] or old_name, "新字段名")

        table_meta = self.inspect_table_metadata(schema_name, table_name)
        if not table_meta.exists:
            err = f"目标表 {schema_name}.{table_name} 在数据库中不存在"
            self.store.update_item_execution(
                item_id,
                status="MISSING_TABLE",
                strategy="DIRECT_ALTER",
                status_detail=err,
                error_message=err,
            )
            self.store.add_log("ERROR", f"{schema_name}.{table_name}", err)
            return self.store.get_item(item_id)

        actual_schema = table_meta.schema_name
        actual_table = table_meta.table_name
        target_label = f"{actual_schema}.{actual_table}.{old_name} -> {new_name}"

        col_map: Dict[str, ColumnMeta] = {}
        for c in table_meta.columns:
            col_map.setdefault(clean_identifier_input(c.column_name).lower(), c)
            col_map[c.column_name.lower()] = c

        old_col = col_map.get(old_name.lower())
        new_col = col_map.get(new_name.lower())

        # 若原字段与目标字段在表中同时存在且是不同的两列，直接拦截并提示重名冲突
        if (
            old_col is not None
            and new_col is not None
            and old_col.ordinal_position != new_col.ordinal_position
        ):
            err = (
                f"字段名冲突：表 {actual_schema}.{actual_table} 中原字段 [{old_col.column_name}](第{old_col.ordinal_position}列) "
                f"拟改名为 [{new_name}]，但表中第 {new_col.ordinal_position} 列已存在同名字段 [{new_col.column_name}]"
            )
            self.store.update_item_execution(
                item_id,
                status="FAILED",
                strategy="DIRECT_ALTER",
                status_detail="目标字段名在表中已存在（请同时选中被占用的同名字段按序改名，或检查贯标清单是否把两列配成了同一个新字段名）",
                error_message=err,
            )
            self.store.add_log("ERROR", target_label, err)
            return self.store.get_item(item_id)

        matched_col = old_col or new_col
        if not matched_col:
            err = f"表 {actual_schema}.{actual_table} 中未找到字段 {old_name} 或 {new_name}"
            self.store.update_item_execution(
                item_id,
                status="MISSING_COLUMN",
                strategy="DIRECT_ALTER",
                status_detail=err,
                error_message=err,
            )
            self.store.add_log("ERROR", target_label, err)
            return self.store.get_item(item_id)

        new_type = validate_sql_type(
            item["new_col_type"] or item["old_col_type"] or matched_col.data_type
        )
        new_comment = item["new_col_comment"].strip()

        before_ordinal = matched_col.ordinal_position
        sql_preview = self.build_direct_alter_sql_preview(item, matched_col, table_meta)

        start_msg = f"开始执行 [原生 ALTER] {target_label} (位于第 {before_ordinal}/{len(table_meta.columns)} 列)..."
        logger.info(start_msg)
        self.store.add_log("INFO", target_label, start_msg)
        try:
            after_ordinal, after_type, after_comment = self._run_real_direct_alter(
                schema_name=actual_schema,
                table_name=actual_table,
                current_col_name=matched_col.column_name,
                current_col_type=matched_col.data_type,
                current_col_comment=matched_col.column_comment,
                new_col_name=new_name,
                new_col_type=new_type,
                new_col_comment=new_comment,
                expected_ordinal=before_ordinal,
            )

            success_msg = (
                f"执行成功！字段已转换为 [{new_name} {new_type}]，"
                f"物理顺序保持在第 {after_ordinal}/{len(table_meta.columns)} 列不变"
            )
            logger.info(success_msg)
            self.store.update_item_execution(
                item_id,
                status="COMPLETED",
                strategy="DIRECT_ALTER",
                status_detail=success_msg,
                error_message="",
                generated_sql=sql_preview,
                ordinal_position=after_ordinal,
                db_current_col_name=new_name,
                db_current_type=after_type,
                db_current_comment=after_comment,
            )
            self.store.add_log("SUCCESS", target_label, success_msg, sql_preview)
        except Exception as exc:
            err_str = str(exc).strip()
            logger.error("执行失败 [%s]: %s", target_label, err_str)
            self.store.update_item_execution(
                item_id,
                status="FAILED",
                strategy="DIRECT_ALTER",
                status_detail="原生 ALTER 执行报错（可点击「原序重建表」突破限制）",
                error_message=err_str,
                generated_sql=sql_preview,
            )
            self.store.add_log("ERROR", target_label, err_str, sql_preview)

        return self.store.get_item(item_id)

    def _run_real_direct_alter(
        self,
        *,
        schema_name: str,
        table_name: str,
        current_col_name: str,
        current_col_type: str,
        current_col_comment: str,
        new_col_name: str,
        new_col_type: str,
        new_col_comment: str,
        expected_ordinal: int,
    ) -> Tuple[int, str, str]:
        """在真实华为 DWS 事务内执行 RENAME / ALTER TYPE / COMMENT 并校验字段顺序."""
        with self._open_dws_connection() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    full_table = f"{quote_ident_str(schema_name)}.{quote_ident_str(table_name)}"
                    natural_new_col = quote_ident_str(new_col_name)

                    if (
                        current_col_name.lower() != new_col_name.lower()
                        or current_col_name != clean_identifier_input(current_col_name)
                    ):
                        cur_col_ident = format_existing_db_ident(current_col_name)
                        cur.execute(
                            f"ALTER TABLE {full_table} RENAME COLUMN {cur_col_ident} TO {natural_new_col};"
                        )

                    if normalize_dws_type(current_col_type) != normalize_dws_type(new_col_type):
                        safe_type = validate_sql_type(new_col_type)
                        cur.execute(
                            f"ALTER TABLE {full_table} ALTER COLUMN {natural_new_col} TYPE {safe_type} USING {natural_new_col}::{safe_type};"
                        )

                    if new_col_comment != "" and current_col_comment != new_col_comment:
                        cur.execute(
                            f"COMMENT ON COLUMN {full_table}.{natural_new_col} IS %s;",
                            (new_col_comment,),
                        )

                    cur.execute(
                        """
                        SELECT
                            a.attname,
                            pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                            COALESCE(col_description(c.oid, a.attnum), '') AS column_comment
                        FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                        WHERE n.nspname = %s
                          AND c.relname = %s
                          AND a.attnum > 0
                          AND NOT a.attisdropped
                        ORDER BY a.attnum ASC;
                        """,
                        (schema_name, table_name),
                    )
                    rows = cur.fetchall()
                    actual_ordinal = -1
                    actual_type = new_col_type
                    actual_comment = new_col_comment
                    for idx, r in enumerate(rows, start=1):
                        if clean_identifier_input(r[0]).lower() == clean_identifier_input(new_col_name).lower():
                            actual_ordinal = idx
                            actual_type = r[1]
                            actual_comment = r[2] or ""
                            break

                    if actual_ordinal != expected_ordinal:
                        raise RuntimeError(
                            f"字段顺序核验异常：期望位于第 {expected_ordinal} 列，实际位于第 {actual_ordinal} 列，已自动回滚事务"
                        )

                conn.commit()
                return actual_ordinal, actual_type, actual_comment
            except Exception:
                conn.rollback()
                raise

    def execute_table_rebuild_migration(
        self,
        schema_name: str,
        table_name: str,
        item_ids: Optional[List[int]] = None,
        keep_backup: bool = True,
    ) -> Dict[str, Any]:
        """使用模式 2 (按原表 attnum 顺序重建表并迁移数据) 批量完成指定表下的字段贯标."""
        validate_identifier(schema_name, "模式名")
        validate_identifier(table_name, "表名")

        all_items = self.store.list_items()
        if item_ids is not None:
            id_set = {int(i) for i in item_ids}
            table_items = [
                it
                for it in all_items
                if it["id"] in id_set and it["status"] != "COMPLETED"
            ]
        else:
            table_items = [
                it
                for it in all_items
                if clean_identifier_input(it["schema_name"]).lower() == clean_identifier_input(schema_name).lower()
                and clean_identifier_input(it["table_name"]).lower() == clean_identifier_input(table_name).lower()
                and it["status"] != "COMPLETED"
            ]
        if not table_items:
            raise ValueError(f"表 {schema_name}.{table_name} 下没有需要执行的待贯标或报错字段")

        plan = self.build_table_rebuild_sql_preview(
            schema_name, table_name, table_items, keep_backup=keep_backup
        )
        actual_schema = plan["schema_name"]
        actual_table = plan["table_name"]
        sql_preview = plan["sql_preview"]
        col_plan = plan["col_plan"]
        bound_items_by_ordinal: Dict[int, List[Dict[str, Any]]] = col_plan["bound_items_by_ordinal"]
        conflict_items: List[Tuple[Dict[str, Any], str]] = col_plan["conflict_items"]
        missing_items: List[Dict[str, Any]] = col_plan["missing_items"]
        target_label = f"{actual_schema}.{actual_table} (整表保序重建迁移)"

        # 先记录缺失列与重名冲突列
        for m_it in missing_items:
            err = f"表 {actual_schema}.{actual_table} 中未找到字段 {m_it['old_col_name']} 或 {m_it['new_col_name']}"
            self.store.update_item_execution(
                m_it["id"],
                status="MISSING_COLUMN",
                strategy="TABLE_REBUILD",
                status_detail=err,
                error_message=err,
            )
            self.store.add_log("ERROR", target_label, err)

        for c_it, c_reason in conflict_items:
            self.store.update_item_execution(
                c_it["id"],
                status="FAILED",
                strategy="TABLE_REBUILD",
                status_detail="字段编排存在重名冲突，已自动跳过该冲突项以防建表报错",
                error_message=c_reason,
                generated_sql=sql_preview,
            )
            self.store.add_log("ERROR", target_label, c_reason)

        valid_items = [
            it for grp in bound_items_by_ordinal.values() for it in grp
        ]
        if not valid_items:
            raise ValueError(
                f"表 {actual_schema}.{actual_table} 所选字段均存在重名冲突或字段缺失，无法执行重建迁移"
            )

        start_msg = f"开始执行 [按原表字段顺序重建表迁移] {target_label}，涉及 {len(valid_items)} 个有效改造字段..."
        logger.info(start_msg)
        self.store.add_log("INFO", target_label, start_msg)

        try:
            self._run_real_table_rebuild(actual_schema, actual_table, valid_items, keep_backup)

            updated_meta = self.inspect_table_metadata(actual_schema, actual_table)
            col_by_lower = {
                clean_identifier_input(c.column_name).lower(): c
                for c in updated_meta.columns
            }
            for ord_pos, grp in bound_items_by_ordinal.items():
                for it in grp:
                    clean_new = (
                        clean_identifier_input(it["new_col_name"])
                        or clean_identifier_input(it["old_col_name"])
                    )
                    new_col_meta = col_by_lower.get(clean_new.lower())
                    final_ord = (
                        new_col_meta.ordinal_position if new_col_meta else ord_pos
                    )
                    msg = (
                        f"原序重建表迁移成功！字段固定在第 {final_ord}/{len(updated_meta.columns)} 列"
                        + (f"（原表已备份为 {plan['bak_table_name']}）" if keep_backup else "")
                    )
                    self.store.update_item_execution(
                        it["id"],
                        status="COMPLETED",
                        strategy="TABLE_REBUILD",
                        status_detail=msg,
                        error_message="",
                        generated_sql=sql_preview,
                        ordinal_position=final_ord,
                        db_current_col_name=new_col_meta.column_name if new_col_meta else clean_new,
                        db_current_type=new_col_meta.data_type if new_col_meta else it["new_col_type"],
                        db_current_comment=new_col_meta.column_comment if new_col_meta else it["new_col_comment"],
                    )

            log_msg = (
                f"成功完成表 {actual_schema}.{actual_table} 的按原序重建迁移，"
                f"共转换 {len(valid_items)} 个字段，全表 {len(updated_meta.columns)} 列顺序 100% 保持一致。"
            )
            logger.info(log_msg)
            self.store.add_log("SUCCESS", target_label, log_msg, sql_preview)
            return {
                "success": True,
                "message": log_msg,
                "valid_count": len(valid_items),
                "failed_count": len(missing_items) + len(conflict_items),
                "items": self.store.list_items(),
                "summary": self.store.get_summary(),
            }
        except Exception as exc:
            err_str = f"原序重建表迁移失败（事务已安全回滚，原表未受影响）: {exc}"
            logger.error(err_str)
            for it in valid_items:
                self.store.update_item_execution(
                    it["id"],
                    status="FAILED",
                    strategy="TABLE_REBUILD",
                    status_detail="原序重建表执行失败（事务已回滚）",
                    error_message=err_str,
                    generated_sql=sql_preview,
                )
            self.store.add_log("ERROR", target_label, err_str, sql_preview)
            raise RuntimeError(err_str) from exc

    def execute_batch_items(
        self,
        item_ids: List[int],
        max_workers: int = 5,
        keep_backup: bool = True,
    ) -> Dict[str, Any]:
        """以表为单位智能判断执行策略，并通过多线程并发处理不同的表（默认 5 线程）：
        - 若某张表的选中任务中存在分布键/分区键调整要求，或存在字段互换改名环路（A<->B），则该表全部选中任务统一采用「按原表顺序重建表 (TABLE_REBUILD)」执行；
        - 若某张表的选中任务中无任何分布键/分区键调整要求，则按拓扑顺序采用「原生 ALTER COLUMN (DIRECT_ALTER)」逐列执行；
        - 每个工作线程独立负责不同的数据表，互不争抢同表锁。
        """
        id_set = {int(x) for x in (item_ids or [])}
        all_items = self.store.list_items()
        target_items = [
            it for it in all_items if it["id"] in id_set and it["status"] != "COMPLETED"
        ]
        if not target_items:
            return {
                "executed_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "rebuild_table_count": 0,
                "direct_table_count": 0,
                "message": "所选记录均已贯标完成，无需重复执行",
            }

        table_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for it in target_items:
            key = (
                clean_identifier_input(it["schema_name"]).lower(),
                clean_identifier_input(it["table_name"]).lower(),
            )
            table_groups.setdefault(key, []).append(it)

        total_tables = len(table_groups)
        worker_count = max(1, min(int(max_workers or 5), total_tables))
        batch_start_msg = (
            f"已提交批量贯标任务：共 {len(target_items)} 个字段（涉及 {total_tables} 张数据表），"
            f"启动 {worker_count} 个工作线程按表并发执行..."
        )
        logger.info(batch_start_msg)
        self.store.add_log("INFO", "多线程并发调度", batch_start_msg)

        def _process_single_table_group(
            group_items: List[Dict[str, Any]],
        ) -> Tuple[str, int, int]:
            t_name_tag = threading.current_thread().name
            req_schema = validate_identifier(group_items[0]["schema_name"], "模式名")
            req_table = validate_identifier(group_items[0]["table_name"], "表名")

            table_meta = self.inspect_table_metadata(req_schema, req_table)
            if not table_meta.exists:
                err = f"目标表 {req_schema}.{req_table} 在数据库中不存在"
                for it in group_items:
                    self.store.update_item_execution(
                        it["id"],
                        status="MISSING_TABLE",
                        strategy="DIRECT_ALTER",
                        status_detail=err,
                        error_message=err,
                    )
                self.store.add_log("ERROR", f"{req_schema}.{req_table}", f"[{t_name_tag}] {err}")
                return ("MISSING", 0, len(group_items))

            actual_schema = table_meta.schema_name
            actual_table = table_meta.table_name
            tbl_label = f"{actual_schema}.{actual_table}"

            col_plan = self._plan_table_items_for_columns(table_meta, group_items)
            dist_keys_lower = {
                clean_identifier_input(k).lower() for k in table_meta.distribute_keys
            }
            part_keys_lower = {
                clean_identifier_input(k).lower() for k in table_meta.partition_keys
            }
            col_map: Dict[str, ColumnMeta] = {}
            for c in table_meta.columns:
                col_map.setdefault(clean_identifier_input(c.column_name).lower(), c)
                col_map[c.column_name.lower()] = c

            rebuild_reasons: List[str] = []
            for it in group_items:
                old_c = clean_identifier_input(it["old_col_name"])
                new_c = clean_identifier_input(it["new_col_name"]) or old_c
                cur_c = clean_identifier_input(it.get("db_current_col_name") or "")
                matched_col = (
                    col_map.get(old_c.lower())
                    or col_map.get(new_c.lower())
                    or (col_map.get(cur_c.lower()) if cur_c else None)
                )
                is_dist_or_part = (
                    bool(it.get("is_distribute_key"))
                    or old_c.lower() in dist_keys_lower
                    or new_c.lower() in dist_keys_lower
                    or (cur_c and cur_c.lower() in dist_keys_lower)
                    or old_c.lower() in part_keys_lower
                    or new_c.lower() in part_keys_lower
                    or (
                        matched_col is not None
                        and clean_identifier_input(matched_col.column_name).lower()
                        in (dist_keys_lower | part_keys_lower)
                    )
                    or ("distribute" in (it.get("error_message") or "").lower())
                    or ("partition" in (it.get("error_message") or "").lower())
                )
                if is_dist_or_part:
                    if matched_col is None:
                        needs_col_alter = True
                    else:
                        eff_new_type = (
                            it["new_col_type"].strip()
                            or it["old_col_type"].strip()
                            or matched_col.data_type
                        )
                        name_changed = (
                            matched_col.column_name.lower() != new_c.lower()
                            or matched_col.column_name
                            != clean_identifier_input(matched_col.column_name)
                        )
                        type_changed = normalize_dws_type(
                            matched_col.data_type
                        ) != normalize_dws_type(eff_new_type)
                        needs_col_alter = (
                            name_changed
                            or type_changed
                            or it.get("status") == "FAILED"
                        )
                    if needs_col_alter:
                        rebuild_reasons.append(f"分布键/分区键字段 {old_c}")

            if col_plan["has_rename_cycle"]:
                rebuild_reasons.append("字段互换改名环路")

            # 策略 A：只要当前表选中的任务里存在分布键/分区键被调整或字段互换改名，整张表统一采用「按原表顺序重建表」修改
            if rebuild_reasons:
                route_msg = (
                    f"[{t_name_tag}] 表 {tbl_label} 检测到 {', '.join(rebuild_reasons)}，"
                    f"整表共 {len(group_items)} 个任务自动采用「按原表顺序重建表」模式执行..."
                )
                logger.info(route_msg)
                self.store.add_log("INFO", tbl_label, route_msg)
                try:
                    reb_res = self.execute_table_rebuild_migration(
                        schema_name=actual_schema,
                        table_name=actual_table,
                        item_ids=[it["id"] for it in group_items],
                        keep_backup=keep_backup,
                    )
                    return (
                        "REBUILD",
                        int(reb_res.get("valid_count", len(group_items))),
                        int(reb_res.get("failed_count", 0)),
                    )
                except Exception:
                    return ("REBUILD", 0, len(group_items))

            # 策略 B：整张表的选中任务中无任何分布键被调整，按拓扑顺序采用原生 ALTER COLUMN 逐列执行
            route_msg = (
                f"[{t_name_tag}] 表 {tbl_label} 无分布键调整要求，"
                f"共 {len(group_items)} 个任务采用「原生 ALTER COLUMN」模式按依赖顺序执行..."
            )
            logger.info(route_msg)
            self.store.add_log("INFO", tbl_label, route_msg)

            succ_cnt = 0
            fail_cnt = 0

            # 先处理未找到字段或重名冲突字段
            for m_it in col_plan["missing_items"]:
                err = f"表 {actual_schema}.{actual_table} 中未找到字段 {m_it['old_col_name']} 或 {m_it['new_col_name']}"
                self.store.update_item_execution(
                    m_it["id"],
                    status="MISSING_COLUMN",
                    strategy="DIRECT_ALTER",
                    status_detail=err,
                    error_message=err,
                )
                self.store.add_log("ERROR", tbl_label, err)
                fail_cnt += 1

            for c_it, c_reason in col_plan["conflict_items"]:
                self.store.update_item_execution(
                    c_it["id"],
                    status="FAILED",
                    strategy="DIRECT_ALTER",
                    status_detail="字段编排存在重名冲突，已自动拦截以防 column already exists 报错",
                    error_message=c_reason,
                )
                self.store.add_log("ERROR", tbl_label, c_reason)
                fail_cnt += 1

            hit_dist_error = False
            for ord_pos in col_plan["ordered_ordinals"]:
                bound_list = col_plan["bound_items_by_ordinal"][ord_pos]
                primary_item = bound_list[0]
                res = self.execute_single_item_direct(primary_item["id"])
                if res and res["status"] == "COMPLETED":
                    succ_cnt += 1
                    # 若贯标清单中同一物理列有重复行，同步更新为已完成
                    for dup_it in bound_list[1:]:
                        self.store.update_item_execution(
                            dup_it["id"],
                            status="COMPLETED",
                            strategy="DIRECT_ALTER",
                            status_detail=res["status_detail"],
                            error_message="",
                            generated_sql=res["generated_sql"],
                            ordinal_position=res["ordinal_position"],
                            db_current_col_name=res["db_current_col_name"],
                            db_current_type=res["db_current_type"],
                            db_current_comment=res["db_current_comment"],
                        )
                        succ_cnt += 1
                else:
                    err_txt = (res.get("error_message") if res else "") or ""
                    if (
                        "distribute" in err_txt.lower()
                        or "partition" in err_txt.lower()
                        or "0a000" in err_txt.lower()
                    ):
                        hit_dist_error = True
                        break
                    fail_cnt += len(bound_list)

            # 若执行原生 ALTER 过程中意外触发 DWS 分布键/分区键限制，自动将该表剩余任务升级为重建表
            if hit_dist_error:
                remaining_ids = [
                    it["id"]
                    for ord_pos in col_plan["ordered_ordinals"]
                    for it in col_plan["bound_items_by_ordinal"][ord_pos]
                    if (self.store.get_item(it["id"]) or {}).get("status") != "COMPLETED"
                ]
                fallback_msg = (
                    f"[{t_name_tag}] 表 {tbl_label} 在执行中触发分布键/分区键限制，"
                    f"自动切换为「按原表顺序重建表」模式处理剩余 {len(remaining_ids)} 个字段..."
                )
                logger.info(fallback_msg)
                self.store.add_log("INFO", tbl_label, fallback_msg)
                try:
                    reb_res = self.execute_table_rebuild_migration(
                        schema_name=actual_schema,
                        table_name=actual_table,
                        item_ids=remaining_ids,
                        keep_backup=keep_backup,
                    )
                    return (
                        "REBUILD",
                        succ_cnt + int(reb_res.get("valid_count", len(remaining_ids))),
                        fail_cnt + int(reb_res.get("failed_count", 0)),
                    )
                except Exception:
                    return ("REBUILD", succ_cnt, fail_cnt + len(remaining_ids))

            return ("DIRECT", succ_cnt, fail_cnt)

        executed_count = len(target_items)
        success_count = 0
        failed_count = 0
        rebuild_table_count = 0
        direct_table_count = 0

        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="DWS表线程"
        ) as executor:
            futures = [
                executor.submit(_process_single_table_group, grp)
                for grp in table_groups.values()
            ]
            for fut in as_completed(futures):
                mode, s_cnt, f_cnt = fut.result()
                success_count += s_cnt
                failed_count += f_cnt
                if mode == "REBUILD":
                    rebuild_table_count += 1
                elif mode == "DIRECT":
                    direct_table_count += 1

        finish_msg = (
            f"批量并发执行完毕：共处理 {total_tables} 张表（原序重建 {rebuild_table_count} 张、原生 ALTER {direct_table_count} 张），"
            f"成功 {success_count} 个字段，失败 {failed_count} 个字段。"
        )
        logger.info(finish_msg)
        self.store.add_log(
            "SUCCESS" if failed_count == 0 else "ERROR",
            "多线程并发调度",
            finish_msg,
        )
        return {
            "executed_count": executed_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "rebuild_table_count": rebuild_table_count,
            "direct_table_count": direct_table_count,
            "message": finish_msg,
        }

    def _run_real_table_rebuild(
        self,
        schema_name: str,
        table_name: str,
        table_items: List[Dict[str, Any]],
        keep_backup: bool,
    ) -> None:
        """在真实华为 DWS 事务内执行临时表创建、按原序 CAST 灌数、行数核验、注释还原与表名原子切换."""
        table_meta = self.inspect_table_metadata(schema_name, table_name)
        col_plan = self._plan_table_items_for_columns(table_meta, table_items)
        mod_by_ordinal: Dict[int, Dict[str, Any]] = col_plan["mod_by_ordinal"]

        ts_suffix = datetime.datetime.now().strftime("%m%d%H%M%S")
        tmp_table_name = f"{table_name[:42]}__mig_{ts_suffix}"
        bak_table_name = f"{table_name[:42]}__bak_{ts_suffix}"

        schema_q = quote_ident_str(schema_name)
        orig_table_q = quote_ident_str(table_name)
        tmp_table_q = quote_ident_str(tmp_table_name)
        bak_table_q = quote_ident_str(bak_table_name)

        full_orig = f"{schema_q}.{orig_table_q}"
        full_tmp = f"{schema_q}.{tmp_table_q}"

        col_defs: List[str] = []
        insert_cols: List[str] = []
        select_exprs: List[str] = []
        comment_pairs: List[Tuple[str, str]] = []
        col_rename_map: Dict[str, str] = {}

        for col in sorted(table_meta.columns, key=lambda c: c.ordinal_position):
            m = mod_by_ordinal.get(col.ordinal_position)
            src_col_q = format_existing_db_ident(col.column_name)
            orig_clean = clean_identifier_input(col.column_name) or col.column_name
            if m:
                t_name = validate_identifier(m["new_col_name"] or orig_clean, "新字段名")
                t_type = validate_sql_type(m["new_col_type"] or col.data_type)
                t_comment = (
                    m["new_col_comment"].strip()
                    if m.get("new_col_comment", "").strip() != ""
                    else col.column_comment
                )
                not_null_sql = " NOT NULL" if col.is_not_null else ""
                default_sql = f" DEFAULT {col.column_default}" if col.column_default else ""
                col_defs.append(f"{quote_ident_str(t_name)} {t_type}{default_sql}{not_null_sql}")
                insert_cols.append(quote_ident_str(t_name))
                if normalize_dws_type(col.data_type) != normalize_dws_type(t_type):
                    select_exprs.append(
                        f"CAST({src_col_q} AS {t_type})"
                    )
                else:
                    select_exprs.append(src_col_q)
                if t_comment:
                    comment_pairs.append((t_name, t_comment))
                col_rename_map[col.column_name.lower()] = t_name
                col_rename_map[orig_clean.lower()] = t_name
            else:
                natural_col_name = orig_clean
                not_null_sql = " NOT NULL" if col.is_not_null else ""
                default_sql = f" DEFAULT {col.column_default}" if col.column_default else ""
                col_defs.append(
                    f"{quote_ident_str(natural_col_name)} {col.data_type}{default_sql}{not_null_sql}"
                )
                insert_cols.append(quote_ident_str(natural_col_name))
                select_exprs.append(src_col_q)
                if col.column_comment:
                    comment_pairs.append((natural_col_name, col.column_comment))
                col_rename_map[col.column_name.lower()] = natural_col_name
                col_rename_map[natural_col_name.lower()] = natural_col_name

        updated_dist_keys: List[str] = []
        for raw_k in table_meta.distribute_keys:
            clean_k = clean_identifier_input(raw_k)
            mapped_k = col_rename_map.get(clean_k.lower(), clean_k)
            if mapped_k.lower() not in {x.lower() for x in updated_dist_keys}:
                updated_dist_keys.append(mapped_k)

        with_sql = f" {table_meta.with_clause}" if table_meta.with_clause else ""
        if updated_dist_keys:
            quoted_dist = ", ".join(quote_ident_str(k) for k in updated_dist_keys)
            dist_sql = f" DISTRIBUTE BY HASH({quoted_dist})"
        elif table_meta.distribute_clause:
            dist_sql = f" {table_meta.distribute_clause}"
        else:
            dist_sql = ""

        updated_partition_clause = update_partition_clause_columns(
            table_meta.partition_clause, col_rename_map
        )
        part_sql = f" {updated_partition_clause}" if updated_partition_clause else ""

        create_sql = (
            f"CREATE TABLE {full_tmp} ({', '.join(col_defs)}){with_sql}{dist_sql}{part_sql};"
        )
        insert_sql = (
            f"INSERT INTO {full_tmp} ({', '.join(insert_cols)}) "
            f"SELECT {', '.join(select_exprs)} FROM {full_orig};"
        )

        with self._open_dws_connection() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {full_orig};")
                    orig_cnt = cur.fetchone()[0]

                    cur.execute(create_sql)
                    cur.execute(insert_sql)

                    cur.execute(f"SELECT COUNT(*) FROM {full_tmp};")
                    new_cnt = cur.fetchone()[0]
                    if orig_cnt != new_cnt:
                        raise RuntimeError(
                            f"重建表前后数据行数不一致 (原表={orig_cnt}, 新表={new_cnt})，已中止并回滚"
                        )

                    if table_meta.table_comment:
                        cur.execute(
                            f"COMMENT ON TABLE {full_tmp} IS %s;",
                            (table_meta.table_comment,),
                        )
                    for col_n, col_c in comment_pairs:
                        cur.execute(
                            f"COMMENT ON COLUMN {full_tmp}.{quote_ident_str(col_n)} IS %s;",
                            (col_c,),
                        )

                    if keep_backup:
                        cur.execute(
                            f"ALTER TABLE {full_orig} RENAME TO {bak_table_q};"
                        )
                    else:
                        cur.execute(f"DROP TABLE {full_orig};")

                    cur.execute(
                        f"ALTER TABLE {full_tmp} RENAME TO {orig_table_q};"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
