"""跨环境三栏数标比对引擎 (Source DB × Data Standard × Target DB).

支持的数据源类型：
1. 华为云 GaussDB(DWS) (`DWS`) - 基于 psycopg2 驱动
2. PostgreSQL (`PGSQL`) - 基于 psycopg2 驱动
3. 星环科技 Transwarp Inceptor (`INCEPTOR`) - 支持标准 HiveServer2 Thrift 协议 (LDAP / NONE / NOSASL)
"""

from __future__ import annotations

import io
import logging
import re
import socket
import struct
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.dws_engine import (
    ColumnMeta,
    DWSMigratorEngine,
    TableMeta,
    clean_identifier_input,
    format_reloptions_with_clause,
    normalize_dws_type,
    parse_dws_tabledef,
    quote_ident_str,
    quote_literal_str,
    update_partition_clause_columns,
    validate_identifier,
)
from core.state_store import StateStore

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

logger = logging.getLogger("dws_migrator")

SUPPORTED_DB_TYPES = ("DWS", "PGSQL", "INCEPTOR")


def normalize_cross_type(raw_type: str) -> str:
    """跨异构库 (PGSQL / 华为 DWS / 星环 Inceptor) 的通用数据类型归一化."""
    t = normalize_dws_type(raw_type)
    if not t:
        return ""
    # 星环 Inceptor (Oracle/Hive 兼容模式) 常见等价类型映射
    t = re.sub(r"^varchar2(\(\d+\))?$", r"varchar\1", t)
    t = re.sub(r"^nvarchar2(\(\d+\))?$", r"varchar\1", t)
    t = re.sub(r"^number(\(\d+(?:,\d+)?\))?$", r"numeric\1", t)
    if t == "string":
        return "varchar"
    if t == "double":
        return "float8"
    if t == "float":
        return "float4"
    return t


def is_cross_type_compatible(actual_type: str, std_type: str) -> Tuple[bool, bool]:
    """判断库内实际类型与数据标准类型是否一致或兼容.

    返回: (exact_match, base_compatible)
    - exact_match: 类型与精度完全一致 (如 varchar(100) == character varying(100))
    - base_compatible: 基础类型兼容但精度/表现不同 (如 ইনceptor string vs varchar(100))
    """
    norm_actual = normalize_cross_type(actual_type)
    norm_std = normalize_cross_type(std_type)
    if not norm_std:
        return True, True
    if not norm_actual:
        return False, False
    if norm_actual == norm_std:
        return True, True
    # 处理 Inceptor STRING 与标准 VARCHAR(N)/TEXT 的兼容识别
    base_actual = re.sub(r"\(.*\)$", "", norm_actual).strip()
    base_std = re.sub(r"\(.*\)$", "", norm_std).strip()
    if base_actual == base_std:
        return False, True
    if {base_actual, base_std} <= {"varchar", "text", "char"}:
        return False, True
    return False, False


def translate_column_type(
    raw_type: str, target_dialect: str, is_key_col: bool = False
) -> Tuple[str, str]:
    """根据目标数据库方言 (PGSQL / DWS / INCEPTOR) 的最新引擎规范转换字段数据类型.

    返回: (目标方言类型声明, 转换规则说明)
    """
    dialect = (target_dialect or "DWS").strip().upper()
    s = re.sub(r"\s+", " ", (raw_type or "").strip())
    if not s:
        s = "varchar(255)"
    low = s.lower()
    low_nospace = re.sub(r"\s*\(\s*", "(", low)
    low_nospace = re.sub(r"\s*,\s*", ",", low_nospace)
    low_nospace = re.sub(r"\s*\)\s*", ")", low_nospace)

    # 提取括号内精度或长度参数，例如 (100) 或 (18,2)
    paren_match = re.search(r"\((\d+(?:,\d+)?)\)", low_nospace)
    paren_arg = f"({paren_match.group(1)})" if paren_match else ""
    base_name = re.sub(r"\(.*\)", "", low_nospace).strip()

    # -------------------- 目标方言 1：华为云 GaussDB(DWS) --------------------
    if dialect == "DWS":
        if base_name in ("string", "clob", "longvarchar", "text", "json", "jsonb"):
            if is_key_col:
                return (
                    "VARCHAR(255)",
                    f"DWS 分布键/分区键不支持无界 {s.upper()}，自动映射为 VARCHAR(255)",
                )
            return ("TEXT", f"原类型 {s} 映射为 DWS TEXT" if base_name != "text" else "")
        if base_name in ("varchar", "character varying", "varchar2", "nvarchar", "nvarchar2"):
            if paren_arg:
                return (f"VARCHAR{paren_arg}", "")
            return ("VARCHAR(255)" if is_key_col else "VARCHAR", "")
        if base_name in ("char", "character", "bpchar", "nchar"):
            return (f"CHAR{paren_arg}" if paren_arg else "CHAR(1)", "")
        if base_name in ("tinyint", "int1", "byteint"):
            return ("TINYINT", "")
        if base_name in ("smallint", "int2"):
            return ("SMALLINT", "")
        if base_name in ("int", "integer", "int4", "binary_integer", "serial"):
            return ("INTEGER", "")
        if base_name in ("bigint", "int8", "bigserial"):
            return ("BIGINT", "")
        if base_name in ("numeric", "decimal", "number", "dec"):
            return (
                f"NUMERIC{paren_arg}" if paren_arg else "NUMERIC(38,10)",
                "" if paren_arg else f"无精度 {s} 规范化为 DWS NUMERIC(38,10)",
            )
        if base_name in ("real", "float4", "float"):
            return ("REAL", "")
        if base_name in ("double", "double precision", "float8"):
            return ("DOUBLE PRECISION", "")
        if "timestamp" in base_name or base_name in ("datetime", "smalldatetime"):
            if "with time zone" in base_name or base_name == "timestamptz":
                return ("TIMESTAMP(0) WITH TIME ZONE", "")
            return ("TIMESTAMP(0) WITHOUT TIME ZONE", "")
        if "time" in base_name and "timestamp" not in base_name:
            if "with time zone" in base_name or base_name == "timetz":
                return ("TIME(0) WITH TIME ZONE", "")
            return ("TIME(0) WITHOUT TIME ZONE", "")
        if base_name == "date":
            return ("DATE", "")
        if base_name in ("bool", "boolean"):
            return ("BOOLEAN", "")
        if base_name in ("bytea", "binary", "varbinary", "blob", "raw"):
            return ("BYTEA", "")
        return (s.upper(), "")

    # -------------------- 目标方言 2：PostgreSQL (PGSQL) --------------------
    if dialect == "PGSQL":
        if base_name in ("string", "clob", "longvarchar", "text"):
            return ("TEXT", f"原类型 {s} 映射为 PGSQL TEXT" if base_name != "text" else "")
        if base_name in ("varchar", "character varying", "varchar2", "nvarchar", "nvarchar2"):
            return (f"VARCHAR{paren_arg}" if paren_arg else "VARCHAR", "")
        if base_name in ("char", "character", "bpchar", "nchar"):
            return (f"CHAR{paren_arg}" if paren_arg else "CHAR(1)", "")
        if base_name in ("tinyint", "int1", "byteint"):
            return (
                "SMALLINT",
                f"PostgreSQL 无原生 {s.upper()} 类型，自动映射为 SMALLINT",
            )
        if base_name in ("smallint", "int2"):
            return ("SMALLINT", "")
        if base_name in ("int", "integer", "int4", "binary_integer"):
            return ("INTEGER", "")
        if base_name in ("bigint", "int8"):
            return ("BIGINT", "")
        if base_name in ("serial", "bigserial"):
            return (base_name.upper(), "")
        if base_name in ("numeric", "decimal", "number", "dec"):
            return (f"NUMERIC{paren_arg}" if paren_arg else "NUMERIC", "")
        if base_name in ("real", "float4", "float"):
            return ("REAL", "")
        if base_name in ("double", "double precision", "float8"):
            return ("DOUBLE PRECISION", "")
        if "timestamp" in base_name or base_name in ("datetime", "smalldatetime"):
            if "with time zone" in base_name or base_name == "timestamptz":
                return ("TIMESTAMP WITH TIME ZONE", "")
            return ("TIMESTAMP WITHOUT TIME ZONE", "")
        if "time" in base_name and "timestamp" not in base_name:
            if "with time zone" in base_name or base_name == "timetz":
                return ("TIME WITH TIME ZONE", "")
            return ("TIME WITHOUT TIME ZONE", "")
        if base_name == "date":
            return ("DATE", "")
        if base_name in ("bool", "boolean"):
            return ("BOOLEAN", "")
        if base_name in ("bytea", "binary", "varbinary", "blob", "raw"):
            return ("BYTEA", "")
        return (s.upper(), "")

    # -------------------- 目标方言 3：星环 Transwarp Inceptor --------------------
    if dialect == "INCEPTOR":
        if base_name in ("text", "clob", "longvarchar", "json", "jsonb", "string"):
            return ("STRING", f"原类型 {s} 映射为 Inceptor STRING" if base_name != "string" else "")
        if base_name in ("varchar", "character varying", "varchar2", "nvarchar", "nvarchar2"):
            if paren_arg:
                return (f"VARCHAR{paren_arg}", "")
            return ("STRING", "无长度限制 VARCHAR 在 Inceptor 中映射为 STRING")
        if base_name in ("char", "character", "bpchar", "nchar"):
            return (f"CHAR{paren_arg}" if paren_arg else "CHAR(1)", "")
        if base_name in ("tinyint", "int1", "byteint"):
            return ("TINYINT", "")
        if base_name in ("smallint", "int2"):
            return ("SMALLINT", "")
        if base_name in ("int", "integer", "int4", "binary_integer", "serial"):
            return ("INT", "")
        if base_name in ("bigint", "int8", "bigserial"):
            return ("BIGINT", "")
        if base_name in ("numeric", "decimal", "number", "dec"):
            return (
                f"DECIMAL{paren_arg}" if paren_arg else "DECIMAL(38,10)",
                "" if paren_arg else f"无精度 {s} 规范化为 Inceptor DECIMAL(38,10)",
            )
        if base_name in ("real", "float4", "float"):
            return ("FLOAT", "")
        if base_name in ("double", "double precision", "float8"):
            return ("DOUBLE", "")
        if "timestamp" in base_name or base_name in ("datetime", "smalldatetime"):
            return (
                "TIMESTAMP",
                f"原类型 {s} 映射为 Inceptor TIMESTAMP" if base_name != "timestamp" else "",
            )
        if "time" in base_name and "timestamp" not in base_name:
            return (
                "STRING",
                f"星环 Inceptor 无独立 {s.upper()} 类型，自动映射为 STRING 存储时间文本",
            )
        if base_name == "date":
            return ("DATE", "")
        if base_name in ("bool", "boolean"):
            return ("BOOLEAN", "")
        if base_name in ("bytea", "binary", "varbinary", "blob", "raw"):
            return ("BINARY", "")
        return (s.upper(), "")

    return (s.upper(), "")


_SQL_KEYWORDS_STOP_ALIAS = {
    "FROM",
    "WHERE",
    "GROUP",
    "ORDER",
    "HAVING",
    "LIMIT",
    "OFFSET",
    "UNION",
    "INTERSECT",
    "EXCEPT",
    "WINDOW",
    "QUALIFY",
    "DISTRIBUTE",
    "CLUSTER",
    "SORT",
    "INTO",
    "ON",
    "USING",
    "JOIN",
    "LEFT",
    "RIGHT",
    "FULL",
    "INNER",
    "OUTER",
    "CROSS",
    "END",
    "NULL",
    "ASC",
    "DESC",
}


def is_generic_or_unbounded_type(sql_type: str) -> bool:
    """判断某个字段类型是否为视图常见的无长度/泛化类型（如无长度的 VARCHAR、TEXT、STRING、无精度的 NUMERIC 等）.

    当视图列为此类泛化类型，而穿透找到的底层物理基表具有精确长度/精度（如 VARCHAR(64)、DECIMAL(18,2)）时，
    可自动用底层基表的精确类型补全。
    """
    s = (sql_type or "").strip().lower()
    if not s:
        return True
    s = " ".join(s.split())
    return s in (
        "string",
        "text",
        "varchar",
        "character varying",
        "nvarchar",
        "nvarchar2",
        "varchar2",
        "char",
        "character",
        "bpchar",
        "numeric",
        "decimal",
        "number",
        "unknown",
        "any",
        "void",
    )


def _split_top_level_select_items_with_comments(select_body: str) -> List[Tuple[str, str]]:
    """切分顶层 SELECT 投影项，并精准保留同行逗号前后的行尾注释 (-- ...) 或块注释 (/* ... */)."""
    items: List[Tuple[str, str]] = []
    n = len(select_body)
    i = 0
    depth = 0
    buf: List[str] = []
    inline_comments: List[str] = []

    while i < n:
        ch = select_body[i]
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(select_body[i])
                if select_body[i] == "'":
                    if i + 1 < n and select_body[i + 1] == "'":
                        buf.append(select_body[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch in ('"', "`"):
            quote_ch = ch
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(select_body[i])
                if select_body[i] == quote_ch:
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" and i + 1 < n and select_body[i + 1] == "-":
            end_line = select_body.find("\n", i + 2)
            if end_line == -1:
                comm_txt = select_body[i + 2 :].strip()
                i = n
            else:
                comm_txt = select_body[i + 2 : end_line].strip()
                i = end_line + 1
            if comm_txt:
                inline_comments.append(comm_txt)
            buf.append(" ")
            continue
        if ch == "/" and i + 1 < n and select_body[i + 1] == "*":
            end_blk = select_body.find("*/", i + 2)
            if end_blk == -1:
                comm_txt = select_body[i + 2 :].strip()
                i = n
            else:
                comm_txt = select_body[i + 2 : end_blk].strip()
                i = end_blk + 2
            if comm_txt and not comm_txt.startswith("+"):
                inline_comments.append(comm_txt)
            buf.append(" ")
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if ch == "," and depth == 0:
            i += 1
            while i < n and select_body[i] in (" ", "\t", "\r"):
                i += 1
            if i + 1 < n and select_body[i] == "-" and select_body[i + 1] == "-":
                end_line = select_body.find("\n", i + 2)
                if end_line == -1:
                    comm_txt = select_body[i + 2 :].strip()
                    i = n
                else:
                    comm_txt = select_body[i + 2 : end_line].strip()
                    i = end_line + 1
                if comm_txt:
                    inline_comments.append(comm_txt)
            elif i + 1 < n and select_body[i] == "/" and select_body[i + 1] == "*":
                end_blk = select_body.find("*/", i + 2)
                if end_blk == -1:
                    comm_txt = select_body[i + 2 :].strip()
                    i = n
                else:
                    comm_txt = select_body[i + 2 : end_blk].strip()
                    i = end_blk + 2
                if comm_txt and not comm_txt.startswith("+"):
                    inline_comments.append(comm_txt)

            expr_str = "".join(buf).strip()
            if expr_str:
                items.append((expr_str, "；".join(inline_comments)))
            buf = []
            inline_comments = []
            continue

        buf.append(ch)
        i += 1

    tail_expr = "".join(buf).strip()
    if tail_expr:
        items.append((tail_expr, "；".join(inline_comments)))
    return items


def _strip_sql_comments_preserve_literals(sql_text: str) -> str:
    """移除 SQL 注释但保留字符串常量，便于语法定位."""
    out: List[str] = []
    n = len(sql_text)
    i = 0
    while i < n:
        ch = sql_text[i]
        if ch in ("'", '"', "`"):
            q = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql_text[i])
                if sql_text[i] == q:
                    if q == "'" and i + 1 < n and sql_text[i + 1] == "'":
                        out.append(sql_text[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            end_line = sql_text.find("\n", i + 2)
            if end_line == -1:
                break
            out.append("\n")
            i = end_line + 1
            continue
        if ch == "/" and i + 1 < n and sql_text[i + 1] == "*":
            end_blk = sql_text.find("*/", i + 2)
            if end_blk == -1:
                break
            out.append(" ")
            i = end_blk + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _infer_sql_expr_type(expr: str, col_name: str) -> Tuple[str, bool]:
    """根据视图 SELECT 表达式或字段命名规范智能推导数据类型.

    返回: (inferred_type, has_explicit_cast)
    """
    clean_e = " ".join((expr or "").strip().split())
    upper_e = clean_e.upper()

    # 1. 显式 CAST(x AS TYPE) 或 TRY_CAST(x AS TYPE)
    m_cast = re.search(
        r"\b(?:TRY_)?CAST\s*\(.+?\bAS\s+([A-Za-z0-9_]+(?:\s+VARYING|\s+PRECISION|\s+WITHOUT\s+TIME\s+ZONE|\s+WITH\s+TIME\s+ZONE)?(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)\s*\)$",
        clean_e,
        flags=re.IGNORECASE,
    )
    if m_cast:
        return m_cast.group(1).strip().upper(), True

    # 2. PostgreSQL / DWS ::TYPE 强转语法
    m_pg_cast = re.search(
        r"::\s*([A-Za-z0-9_]+(?:\s+VARYING|\s+PRECISION)?(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)\s*$",
        clean_e,
        flags=re.IGNORECASE,
    )
    if m_pg_cast:
        return m_pg_cast.group(1).strip().upper(), True

    # 3. 聚合与窗口函数识别
    if re.match(r"^(?:COUNT|ROW_NUMBER|RANK|DENSE_RANK|NTILE)\s*\(", upper_e):
        return "BIGINT", True
    m_round = re.match(r"^(?:ROUND|TRUNC|TRUNCATE)\s*\(.+,\s*(\d+)\s*\)$", upper_e)
    if m_round:
        scale = int(m_round.group(1))
        return (f"NUMERIC(18,{scale})" if scale > 0 else "BIGINT"), True
    if re.match(r"^(?:SUM|AVG|TO_NUMBER|TO_DEC|TO_DECIMAL)\s*\(", upper_e):
        return "NUMERIC(18,2)", False
    if re.match(r"^(?:TO_DATE|DATE_ADD|DATE_SUB|ADD_MONTHS|LAST_DAY|CURRENT_DATE)\b", upper_e):
        return "DATE", True
    if re.match(r"^(?:TO_TIMESTAMP|CURRENT_TIMESTAMP|NOW|SYSDATE|SYSTIMESTAMP|FROM_UNIXTIME)\b", upper_e):
        return "TIMESTAMP", True
    if re.match(
        r"^(?:CONCAT|CONCAT_WS|SUBSTR|SUBSTRING|TRIM|LTRIM|RTRIM|UPPER|LOWER|REPLACE|REGEXP_REPLACE|TO_CHAR|DATE_FORMAT|LPAD|RPAD)\s*\(",
        upper_e,
    ):
        return "VARCHAR(255)", False

    # 4. 常量字面量识别
    if re.match(r"^'.*'$", clean_e):
        str_len = max(16, min(255, len(clean_e) * 2))
        return f"VARCHAR({str_len})", False
    if re.match(r"^-?\d+$", clean_e):
        return "INTEGER", False
    if re.match(r"^-?\d+\.\d+$", clean_e):
        return "NUMERIC(18,2)", False

    # 5. 根据字段名语义后缀推导（适用于离线解析无基表元数据的视图）
    cn = (col_name or "").lower()
    if any(cn.endswith(suf) for suf in ("_amt", "_bal", "_amount", "_balance", "_price", "_fee", "_cost")):
        return "NUMERIC(18,2)", False
    if any(cn.endswith(suf) for suf in ("_rate", "_ratio", "_pct")):
        return "NUMERIC(12,6)", False
    if any(cn.endswith(suf) for suf in ("_dt", "_date")) or cn in ("stat_dt", "data_dt", "etl_dt", "biz_date"):
        return "DATE", False
    if any(cn.endswith(suf) for suf in ("_tm", "_time", "_ts")) or cn in ("created_at", "updated_at", "etl_time"):
        return "TIMESTAMP", False
    if any(cn.endswith(suf) for suf in ("_cnt", "_count", "_num", "_seq", "_qty")):
        return "BIGINT", False
    if any(cn.endswith(suf) for suf in ("_id", "_no", "_nbr", "_key", "_acct", "_card")):
        return "VARCHAR(64)", False
    if any(cn.endswith(suf) for suf in ("_cd", "_code", "_status", "_st", "_type", "_tp", "_flag", "_flg", "_ind")):
        return "VARCHAR(32)", False
    return "VARCHAR(255)", False


def parse_view_sql_lineage(view_sql: str, default_schema: str = "public") -> Dict[str, Any]:
    """解析任意数据库（星环 Inceptor / 华为 DWS / PostgreSQL / Oracle / MySQL 等）的视图定义或 SELECT 语句.

    核心能力：
    1. 支持 CREATE [OR REPLACE] [MATERIALIZED] VIEW [db.]v_name [(col1 COMMENT '...', ...)] [COMMENT '...'] AS SELECT ...
    2. 支持 WITH cte AS (SELECT ...) 多级 CTE 递归穿透溯源，将 CTE 引用还原到底层真实物理基表
    3. 支持解析 FROM / JOIN 子句中的底层物理基表与别名映射
    4. 提取每个视图输出列的名称、显式 CAST 类型、SQL 行内注释，以及对应的底层基表列 (source_schema, source_table, source_column)
    """
    raw = (view_sql or "").strip().rstrip(";").strip()
    eff_default_schema = clean_identifier_input(default_schema) or "public"
    result: Dict[str, Any] = {
        "is_view_sql": False,
        "view_schema": eff_default_schema,
        "view_name": "",
        "view_comment": "",
        "select_sql": raw,
        "base_tables": [],
        "columns": [],
    }
    if not raw:
        return result

    header_col_names: List[str] = []
    header_col_comments: Dict[str, str] = {}
    body_sql = raw

    # 1. 识别 CREATE [OR REPLACE] ... VIEW <name> [(col_list)] [COMMENT '...'] AS ...
    m_view = re.match(
        r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:FORCE|NOFORCE|GLOBAL|LOCAL|TEMP|TEMPORARY|MATERIALIZED)\s+)*VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m_view:
        result["is_view_sql"] = True
        raw_vname = m_view.group(1).strip()
        if "." in raw_vname:
            parts = raw_vname.split(".", 1)
            result["view_schema"] = clean_identifier_input(parts[0]) or eff_default_schema
            result["view_name"] = clean_identifier_input(parts[1])
        else:
            result["view_name"] = clean_identifier_input(raw_vname)

        pos = m_view.end()
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos < len(raw) and raw[pos] == "(":
            depth = 0
            close_idx = -1
            for k in range(pos, len(raw)):
                if raw[k] == "(":
                    depth += 1
                elif raw[k] == ")":
                    depth -= 1
                    if depth == 0:
                        close_idx = k
                        break
            if close_idx != -1:
                hdr_inside = raw[pos + 1 : close_idx]
                for item_txt, _ in _split_top_level_select_items_with_comments(hdr_inside):
                    m_hc = re.match(
                        r"^([^\s]+?)(?:\s+COMMENT\s+'([^']*)')?\s*$",
                        item_txt.strip(),
                        flags=re.IGNORECASE,
                    )
                    if m_hc:
                        c_nm = clean_identifier_input(m_hc.group(1))
                        if c_nm:
                            header_col_names.append(c_nm)
                            if m_hc.group(2):
                                header_col_comments[c_nm.lower()] = m_hc.group(2).strip()
                pos = close_idx + 1

        rest_after_cols = raw[pos:]
        m_vcomm = re.search(r"\bCOMMENT\s+'([^']*)'", rest_after_cols, flags=re.IGNORECASE)
        if m_vcomm:
            result["view_comment"] = m_vcomm.group(1).strip()

        m_as = re.search(
            r"\bAS\s+(?=(?:\(\s*)*(?:WITH|SELECT)\b)",
            rest_after_cols,
            flags=re.IGNORECASE,
        )
        if m_as:
            body_sql = rest_after_cols[m_as.end() :].strip()

    while body_sql.startswith("(") and body_sql.endswith(")"):
        depth = 0
        matched_end = False
        for idx_c, ch in enumerate(body_sql):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    if idx_c == len(body_sql) - 1:
                        matched_end = True
                    break
        if matched_end:
            body_sql = body_sql[1:-1].strip()
        else:
            break

    result["select_sql"] = body_sql

    # 2. 解析 WITH cte_name AS (...) 块
    cte_map: Dict[str, Dict[str, Any]] = {}
    main_query_sql = body_sql
    clean_for_cte = _strip_sql_comments_preserve_literals(body_sql).lstrip()

    if re.match(r"^WITH\b", clean_for_cte, flags=re.IGNORECASE):
        m_with = re.match(r"^\s*WITH\s+(?:RECURSIVE\s+)?", body_sql, flags=re.IGNORECASE)
        idx_scan = m_with.end() if m_with else 4
        n_len = len(body_sql)
        while idx_scan < n_len:
            m_cte_head = re.match(
                r"\s*([A-Za-z0-9_\"`]+)\s+AS\s*\(",
                body_sql[idx_scan:],
                flags=re.IGNORECASE,
            )
            if not m_cte_head:
                break
            cte_name = clean_identifier_input(m_cte_head.group(1)).lower()
            open_p = idx_scan + m_cte_head.end() - 1
            depth = 0
            close_p = -1
            k = open_p
            while k < n_len:
                c_ch = body_sql[k]
                if c_ch == "'":
                    k += 1
                    while k < n_len and body_sql[k] != "'":
                        k += 1
                elif c_ch == "(":
                    depth += 1
                elif c_ch == ")":
                    depth -= 1
                    if depth == 0:
                        close_p = k
                        break
                k += 1
            if close_p == -1:
                break
            cte_inner_sql = body_sql[open_p + 1 : close_p].strip()
            cte_parsed = parse_view_sql_lineage(cte_inner_sql, default_schema=eff_default_schema)
            cte_map[cte_name] = cte_parsed
            idx_scan = close_p + 1
            while idx_scan < n_len and body_sql[idx_scan].isspace():
                idx_scan += 1
            if idx_scan < n_len and body_sql[idx_scan] == ",":
                idx_scan += 1
                continue
            main_query_sql = body_sql[idx_scan:].strip()
            break

    # 3. 定位顶层 SELECT 与顶层 FROM
    m_sel = re.match(
        r"^\s*SELECT\s+(?:(?:DISTINCT|ALL)\s+)?(?:/\*\+.*?\*/\s*)?",
        main_query_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m_sel:
        return result
    result["is_view_sql"] = True

    sel_start = m_sel.end()
    from_pos = -1
    from_end = -1
    depth = 0
    k = sel_start
    n_main = len(main_query_sql)
    while k < n_main:
        ch = main_query_sql[k]
        if ch in ("'", '"', "`"):
            q = ch
            k += 1
            while k < n_main:
                if main_query_sql[k] == q:
                    if q == "'" and k + 1 < n_main and main_query_sql[k + 1] == "'":
                        k += 2
                        continue
                    k += 1
                    break
                k += 1
            continue
        if ch == "-" and k + 1 < n_main and main_query_sql[k + 1] == "-":
            nl = main_query_sql.find("\n", k + 2)
            k = n_main if nl == -1 else nl + 1
            continue
        if ch == "/" and k + 1 < n_main and main_query_sql[k + 1] == "*":
            eb = main_query_sql.find("*/", k + 2)
            k = n_main if eb == -1 else eb + 2
            continue
        if ch == "(":
            depth += 1
            k += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            k += 1
            continue
        if depth == 0 and (k == 0 or not (main_query_sql[k - 1].isalnum() or main_query_sql[k - 1] == "_")):
            m_from_kw = re.match(r"^FROM(?=\s|\()", main_query_sql[k:], flags=re.IGNORECASE)
            if m_from_kw:
                from_pos = k
                from_end = k + m_from_kw.end()
                break
        k += 1

    select_list_str = main_query_sql[sel_start:from_pos] if from_pos != -1 else main_query_sql[sel_start:]
    from_clause_str = main_query_sql[from_end:] if from_end != -1 else ""

    # 4. 解析 FROM / JOIN 子句中的基表及别名
    alias_to_table: Dict[str, Tuple[str, str]] = {}
    base_tables_list: List[Dict[str, str]] = []
    seen_base: set = set()

    def _add_base_table(sch: str, tbl: str, alias_nm: str) -> None:
        c_sch = clean_identifier_input(sch) or eff_default_schema
        c_tbl = clean_identifier_input(tbl)
        if not c_tbl or c_tbl.upper() in ("DUAL", "SYSIBM.SYSDUMMY1"):
            return
        if c_tbl.lower() in cte_map:
            cte_info = cte_map[c_tbl.lower()]
            for bt in cte_info.get("base_tables", []):
                key_bt = (bt["schema_name"].lower(), bt["table_name"].lower())
                if key_bt not in seen_base:
                    seen_base.add(key_bt)
                    base_tables_list.append(bt)
            alias_to_table[(clean_identifier_input(alias_nm) or c_tbl).lower()] = ("__CTE__", c_tbl.lower())
            alias_to_table[c_tbl.lower()] = ("__CTE__", c_tbl.lower())
            return

        key = (c_sch.lower(), c_tbl.lower())
        if key not in seen_base:
            seen_base.add(key)
            base_tables_list.append(
                {
                    "schema_name": c_sch,
                    "table_name": c_tbl,
                    "alias": clean_identifier_input(alias_nm) or c_tbl,
                }
            )
        eff_alias = (clean_identifier_input(alias_nm) or c_tbl).lower()
        alias_to_table[eff_alias] = (c_sch, c_tbl)
        alias_to_table[c_tbl.lower()] = (c_sch, c_tbl)

    if from_clause_str:
        clean_from = _strip_sql_comments_preserve_literals(from_clause_str)
        idx_f = 0
        while idx_f < len(clean_from):
            if clean_from[idx_f] == "(":
                d_sub = 0
                close_sub = -1
                for m_i in range(idx_f, len(clean_from)):
                    if clean_from[m_i] == "(":
                        d_sub += 1
                    elif clean_from[m_i] == ")":
                        d_sub -= 1
                        if d_sub == 0:
                            close_sub = m_i
                            break
                if close_sub != -1:
                    inner_sub = clean_from[idx_f + 1 : close_sub].strip()
                    m_sub_alias = re.match(
                        r"^\s*(?:AS\s+)?([A-Za-z0-9_\"`]+)",
                        clean_from[close_sub + 1 :],
                        flags=re.IGNORECASE,
                    )
                    if re.match(r"^(?:WITH|SELECT)\b", inner_sub, flags=re.IGNORECASE) and m_sub_alias:
                        sub_alias = clean_identifier_input(m_sub_alias.group(1)).lower()
                        if sub_alias and sub_alias.upper() not in _SQL_KEYWORDS_STOP_ALIAS:
                            sub_parsed = parse_view_sql_lineage(inner_sub, default_schema=eff_default_schema)
                            cte_map[sub_alias] = sub_parsed
                            for bt in sub_parsed.get("base_tables", []):
                                k_bt = (bt["schema_name"].lower(), bt["table_name"].lower())
                                if k_bt not in seen_base:
                                    seen_base.add(k_bt)
                                    base_tables_list.append(bt)
                            alias_to_table[sub_alias] = ("__CTE__", sub_alias)
                    idx_f = close_sub + 1
                    continue
            idx_f += 1

        top_from_chars: List[str] = []
        d_lvl = 0
        for ch_f in clean_from:
            if ch_f == "(":
                d_lvl += 1
                top_from_chars.append(" ")
            elif ch_f == ")":
                d_lvl = max(0, d_lvl - 1)
                top_from_chars.append(" ")
            elif d_lvl == 0:
                top_from_chars.append(ch_f)
            else:
                top_from_chars.append(" ")
        top_from_only = "".join(top_from_chars)
        m_stop = re.search(
            r"\b(?:WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|INTERSECT|EXCEPT|WINDOW|DISTRIBUTE\s+BY|CLUSTER\s+BY)\b",
            top_from_only,
            flags=re.IGNORECASE,
        )
        if m_stop:
            top_from_only = top_from_only[: m_stop.start()]

        for m_tbl in re.finditer(
            r"(?:^|,|\bJOIN)\s*([A-Za-z0-9_\"`]+(?:\.[A-Za-z0-9_\"`]+)?)(?:\s+(?:AS\s+)?([A-Za-z0-9_\"`]+))?",
            top_from_only.strip(),
            flags=re.IGNORECASE,
        ):
            raw_tref = m_tbl.group(1) or ""
            raw_alias = m_tbl.group(2) or ""
            if raw_alias.upper() in _SQL_KEYWORDS_STOP_ALIAS:
                raw_alias = ""
            if "." in raw_tref:
                sp = raw_tref.split(".", 1)
                _add_base_table(sp[0], sp[1], raw_alias)
            else:
                _add_base_table(eff_default_schema, raw_tref, raw_alias)

    result["base_tables"] = base_tables_list

    # 5. 解析 SELECT 投影列并映射到底层基表字段
    raw_items = _split_top_level_select_items_with_comments(select_list_str)
    parsed_columns: List[Dict[str, Any]] = []

    for idx, (item_expr, item_comm) in enumerate(raw_items, start=1):
        clean_item = " ".join(item_expr.strip().split())
        if not clean_item:
            continue

        col_alias = ""
        core_expr = clean_item
        m_as_alias = re.match(
            r"^(.+?)\s+AS\s+([A-Za-z0-9_\"`\[\]]+)\s*$",
            clean_item,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m_as_alias:
            core_expr = m_as_alias.group(1).strip()
            col_alias = clean_identifier_input(m_as_alias.group(2))
        else:
            m_imp_alias = re.match(
                r"^(.+?)\s+([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\"|`[^`]+`)\s*$",
                clean_item,
                flags=re.DOTALL,
            )
            if m_imp_alias:
                cand_expr = m_imp_alias.group(1).strip()
                cand_alias = clean_identifier_input(m_imp_alias.group(2))
                if (
                    cand_alias.upper() not in _SQL_KEYWORDS_STOP_ALIAS
                    and not re.search(
                        r"(?:\bAND|\bOR|\bIS|\bNOT|\bIN|\bCASE|\bWHEN|\bTHEN|\bELSE|[+\-*/=<>])$",
                        cand_expr,
                        flags=re.IGNORECASE,
                    )
                ):
                    core_expr = cand_expr
                    col_alias = cand_alias

        src_qualifier = ""
        src_col_name = ""
        m_direct_col = re.match(
            r"^(?:([A-Za-z0-9_\"`]+)\.)?([A-Za-z0-9_\"`\*]+)$",
            core_expr.strip(),
        )
        if m_direct_col:
            src_qualifier = clean_identifier_input(m_direct_col.group(1) or "")
            src_col_name = clean_identifier_input(m_direct_col.group(2) or "")
        else:
            m_inner_qcol = re.search(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                core_expr,
            )
            if m_inner_qcol:
                src_qualifier = clean_identifier_input(m_inner_qcol.group(1))
                src_col_name = clean_identifier_input(m_inner_qcol.group(2))
            else:
                m_func_col = re.match(
                    r"^[A-Za-z0-9_]+\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\b",
                    core_expr.strip(),
                )
                if m_func_col and m_func_col.group(1).upper() not in _SQL_KEYWORDS_STOP_ALIAS:
                    src_col_name = clean_identifier_input(m_func_col.group(1))

        if idx <= len(header_col_names) and header_col_names[idx - 1]:
            final_col_name = header_col_names[idx - 1]
        elif col_alias:
            final_col_name = col_alias
        elif src_col_name and src_col_name != "*":
            final_col_name = src_col_name
        else:
            final_col_name = f"col_{idx}"

        resolved_src_schema = ""
        resolved_src_table = ""
        resolved_src_col = src_col_name if src_col_name != "*" else ""
        cte_inferred_type = ""
        cte_comment = ""

        target_lookup_alias = src_qualifier.lower() if src_qualifier else ""
        if not target_lookup_alias and len(base_tables_list) == 1:
            target_lookup_alias = base_tables_list[0]["alias"].lower()
        elif not target_lookup_alias and len(alias_to_table) == 1:
            target_lookup_alias = next(iter(alias_to_table.keys()))

        if target_lookup_alias and target_lookup_alias in alias_to_table:
            sch_or_tag, tbl_or_cte = alias_to_table[target_lookup_alias]
            if sch_or_tag == "__CTE__" and tbl_or_cte in cte_map:
                cte_cols = cte_map[tbl_or_cte].get("columns", [])
                lookup_c = (resolved_src_col or final_col_name).lower()
                matched_cte_col = next(
                    (cc for cc in cte_cols if cc["column_name"].lower() == lookup_c),
                    None,
                )
                if matched_cte_col:
                    resolved_src_schema = matched_cte_col.get("source_schema", "")
                    resolved_src_table = matched_cte_col.get("source_table", "")
                    resolved_src_col = matched_cte_col.get("source_column") or resolved_src_col
                    cte_inferred_type = matched_cte_col.get("inferred_type", "")
                    cte_comment = matched_cte_col.get("inline_comment", "")
            else:
                resolved_src_schema = sch_or_tag
                resolved_src_table = tbl_or_cte
        elif not src_qualifier and base_tables_list:
            resolved_src_schema = base_tables_list[0]["schema_name"]
            resolved_src_table = base_tables_list[0]["table_name"]

        inferred_tp, has_cast = _infer_sql_expr_type(core_expr, final_col_name)
        if not has_cast and cte_inferred_type:
            inferred_tp = cte_inferred_type

        final_comment = (
            header_col_comments.get(final_col_name.lower(), "")
            or item_comm
            or cte_comment
        )

        parsed_columns.append(
            {
                "ordinal_position": len(parsed_columns) + 1,
                "column_name": final_col_name,
                "inferred_type": inferred_tp,
                "has_explicit_cast": has_cast,
                "inline_comment": final_comment,
                "source_schema": resolved_src_schema,
                "source_table": resolved_src_table,
                "source_column": resolved_src_col,
                "expression": core_expr,
            }
        )

    result["columns"] = parsed_columns
    return result


def build_dialect_ddl(
    meta: TableMeta,
    *,
    source_dialect: str,
    target_dialect: str,
    target_schema: str = "",
    target_table: str = "",
    storage_mode: str = "AUTO",
) -> Dict[str, Any]:
    """将 TableMeta 翻译生成指定目标方言 (PGSQL / 华为 DWS / 星环 Inceptor) 的可执行建表 DDL."""
    src_d = (source_dialect or "DWS").strip().upper()
    tgt_d = (target_dialect or "DWS").strip().upper()
    s_mode = (storage_mode or "AUTO").strip().upper()

    eff_schema = clean_identifier_input(target_schema) or clean_identifier_input(meta.schema_name) or (
        "default" if tgt_d == "INCEPTOR" else "public"
    )
    eff_table = clean_identifier_input(target_table) or clean_identifier_input(meta.table_name) or "unnamed_table"

    dist_keys = [clean_identifier_input(k) for k in meta.distribute_keys if clean_identifier_input(k)]
    part_keys = [clean_identifier_input(k) for k in meta.partition_keys if clean_identifier_input(k)]
    key_lower_set = {k.lower() for k in (dist_keys + part_keys)}
    part_lower_set = {k.lower() for k in part_keys}

    column_mappings: List[Dict[str, Any]] = []
    sorted_cols = sorted(meta.columns, key=lambda c: c.ordinal_position)

    for col in sorted_cols:
        c_name = clean_identifier_input(col.column_name) or col.column_name
        is_key = c_name.lower() in key_lower_set
        is_part = c_name.lower() in part_lower_set
        t_type, note = translate_column_type(col.data_type, tgt_d, is_key_col=is_key)
        lineage_src = getattr(col, "lineage_source", "") or ""
        if lineage_src:
            note = f"视图穿透自 {lineage_src}" + (f"；{note}" if note else "")
        column_mappings.append(
            {
                "ordinal_position": col.ordinal_position,
                "column_name": c_name,
                "source_type": col.data_type,
                "target_type": t_type,
                "is_not_null": bool(col.is_not_null),
                "column_default": col.column_default,
                "column_comment": col.column_comment or "",
                "comment": col.column_comment or "",
                "is_key": is_key,
                "is_distribute_key": c_name.lower() in {k.lower() for k in dist_keys},
                "is_partition_key": is_part,
                "lineage_source": lineage_src,
                "translation_note": note,
                "note": note,
            }
        )

    obj_type = getattr(meta, "object_type", "TABLE") or "TABLE"
    base_tbls = getattr(meta, "penetrated_base_tables", []) or []
    view_header_notes: List[str] = []
    if obj_type in ("VIEW", "MATVIEW"):
        view_header_notes.append(
            f"-- [视图破壁转换] 来源对象为视图 ({obj_type})，已自动提取输出列结构并逆向生成目标环境物理建表语句"
        )
    if base_tbls:
        view_header_notes.append(
            f"-- [底层基表穿透] 已自动穿透关联基表 ({', '.join(base_tbls)}) 补全字段精度、中文注释与分布/分区键"
        )

    # -------------------- 生成星环 Inceptor 方言 DDL --------------------
    if tgt_d == "INCEPTOR":
        full_tbl = f"{quote_ident_str(eff_schema)}.{quote_ident_str(eff_table)}"
        non_part_cols = [m for m in column_mappings if not m["is_partition_key"]]
        part_col_List = [m for m in column_mappings if m["is_partition_key"]]
        # 若整张表只有分区列（极端情况），保留全部列在主表体
        if not non_part_cols:
            non_part_cols = column_mappings
            part_col_List = []

        col_lines: List[str] = []
        for idx, m in enumerate(non_part_cols):
            comma = "," if idx < len(non_part_cols) - 1 else ""
            comm_sql = (
                f" COMMENT {quote_literal_str(m['column_comment'])}"
                if m["column_comment"]
                else ""
            )
            note_comment = f" /* {m['translation_note']} */" if m["translation_note"] else ""
            col_lines.append(
                f"    {quote_ident_str(m['column_name'])} {m['target_type']}{comm_sql}{comma}{note_comment}"
            )

        ddl_lines = [
            "-- ============================================================================",
            f"-- [跨环境方言建表 DDL] 来源方言: {src_d} -> 目标方言: 星环 Transwarp Inceptor",
            f"-- 目标表: {eff_schema}.{eff_table} (共 {len(column_mappings)} 列)",
            *view_header_notes,
            "-- ============================================================================",
            f"DROP TABLE IF EXISTS {full_tbl};",
            f"CREATE TABLE IF NOT EXISTS {full_tbl} (",
            "\n".join(col_lines),
            ")",
        ]
        if meta.table_comment:
            ddl_lines.append(f"COMMENT {quote_literal_str(meta.table_comment)}")

        if part_col_List:
            p_defs = []
            for pm in part_col_List:
                p_comm = (
                    f" COMMENT {quote_literal_str(pm['column_comment'])}"
                    if pm["column_comment"]
                    else ""
                )
                p_defs.append(f"{quote_ident_str(pm['column_name'])} {pm['target_type']}{p_comm}")
            ddl_lines.append(f"PARTITIONED BY ({', '.join(p_defs)})")

        # 分桶键：将 DWS 分布键或首列映射为 Inceptor CLUSTERED BY 分桶键（排除已作为分区键的列）
        cluster_keys = [
            k for k in dist_keys if k.lower() not in part_lower_set
        ]
        if not cluster_keys and non_part_cols and s_mode in ("AUTO", "INCEPTOR_ORC_TXN"):
            cluster_keys = [non_part_cols[0]["column_name"]]

        if s_mode == "INCEPTOR_HOLODESK":
            ddl_lines.append("STORED AS HOLODESK;")
        elif s_mode == "INCEPTOR_TEXTFILE":
            ddl_lines.append("ROW FORMAT DELIMITED FIELDS TERMINATED BY ','")
            ddl_lines.append("STORED AS TEXTFILE;")
        elif s_mode == "INCEPTOR_ORC":
            if cluster_keys:
                q_ck = ", ".join(quote_ident_str(k) for k in cluster_keys)
                ddl_lines.append(f"CLUSTERED BY ({q_ck}) INTO 7 BUCKETS")
            ddl_lines.append("STORED AS ORC;")
        else:
            # 默认推荐：Inceptor ORC 事务表 (支持 CRUD / UPDATE / DELETE)
            if cluster_keys:
                q_ck = ", ".join(quote_ident_str(k) for k in cluster_keys)
                ddl_lines.append(f"CLUSTERED BY ({q_ck}) INTO 7 BUCKETS")
            ddl_lines.append("STORED AS ORC")
            ddl_lines.append("TBLPROPERTIES ('transactional'='true');")

        return {
            "target_dialect": tgt_d,
            "target_schema": eff_schema,
            "target_table": eff_table,
            "storage_mode": s_mode,
            "column_count": len(column_mappings),
            "ddl": "\n".join(ddl_lines),
            "column_mappings": column_mappings,
        }

    # -------------------- 生成华为云 GaussDB(DWS) 或 PostgreSQL 方言 DDL --------------------
    full_tbl = f"{quote_ident_str(eff_schema)}.{quote_ident_str(eff_table)}"
    col_lines = []
    comment_lines: List[str] = []
    if meta.table_comment:
        comment_lines.append(
            f"COMMENT ON TABLE {full_tbl} IS {quote_literal_str(meta.table_comment)};"
        )

    for idx, m in enumerate(column_mappings):
        comma = "," if idx < len(column_mappings) - 1 else ""
        not_null_sql = " NOT NULL" if m["is_not_null"] else ""
        default_sql = ""
        if m["column_default"]:
            raw_def = str(m["column_default"]).strip()
            # 跨库翻译时剥离特定库内部类型强转后缀（如 ::character varying）以提升 PGSQL/DWS 跨版本兼容性
            clean_def = re.sub(r"::[a-zA-Z0-9_ ]+(\(\d+(?:,\d+)?\))?", "", raw_def)
            if clean_def and "nextval(" not in clean_def.lower():
                default_sql = f" DEFAULT {clean_def}"
        note_comment = f" /* {m['translation_note']} */" if m["translation_note"] else ""
        col_lines.append(
            f"    {quote_ident_str(m['column_name'])} {m['target_type']}{default_sql}{not_null_sql}{comma}{note_comment}"
        )
        if m["column_comment"]:
            comment_lines.append(
                f"COMMENT ON COLUMN {full_tbl}.{quote_ident_str(m['column_name'])} IS {quote_literal_str(m['column_comment'])};"
            )

    tail_lines: List[str] = []
    if tgt_d == "DWS":
        if s_mode == "DWS_COLUMN":
            tail_lines.append("WITH (orientation=column, compression=middle)")
        elif s_mode == "DWS_ROW":
            tail_lines.append("WITH (orientation=row, compression=no)")
        elif meta.with_clause:
            tail_lines.append(meta.with_clause)
        else:
            tail_lines.append("WITH (orientation=row, compression=no)")

        eff_dist_keys = dist_keys or (
            [column_mappings[0]["column_name"]] if column_mappings else []
        )
        if eff_dist_keys:
            q_dk = ", ".join(quote_ident_str(k) for k in eff_dist_keys)
            tail_lines.append(f"DISTRIBUTE BY HASH({q_dk})")
        elif meta.distribute_clause:
            tail_lines.append(meta.distribute_clause)

        if meta.partition_clause:
            tail_lines.append(meta.partition_clause)
        elif part_keys:
            q_pk = ", ".join(quote_ident_str(k) for k in part_keys)
            tail_lines.append(
                f"PARTITION BY RANGE ({q_pk})"
            )
    else:
        # 标准 PostgreSQL (PGSQL)：不输出 DWS 专有的 WITH(orientation=...) 和 DISTRIBUTE BY
        if part_keys:
            q_pk = ", ".join(quote_ident_str(k) for k in part_keys)
            tail_lines.append(f"PARTITION BY RANGE ({q_pk})")

    close_paren = ")" + ("\n" + "\n".join(tail_lines) if tail_lines else "") + ";"
    dialect_title = "华为云 GaussDB(DWS)" if tgt_d == "DWS" else "PostgreSQL (PGSQL)"
    ddl_lines = [
        "-- ============================================================================",
        f"-- [跨环境方言建表 DDL] 来源方言: {src_d} -> 目标方言: {dialect_title}",
        f"-- 目标表: {eff_schema}.{eff_table} (共 {len(column_mappings)} 列，物理列序 100% 保持一致)",
        *view_header_notes,
        "-- ============================================================================",
        f"DROP TABLE IF EXISTS {full_tbl};",
        f"CREATE TABLE {full_tbl} (",
        "\n".join(col_lines),
        close_paren,
    ]
    if comment_lines:
        ddl_lines.append("")
        ddl_lines.append("-- 表级注释与字段中文注释")
        ddl_lines.extend(comment_lines)

    return {
        "target_dialect": tgt_d,
        "target_schema": eff_schema,
        "target_table": eff_table,
        "storage_mode": s_mode,
        "column_count": len(column_mappings),
        "ddl": "\n".join(ddl_lines),
        "column_mappings": column_mappings,
    }


def _format_readable_sql_ddl(sql_text: str) -> str:
    """将单行超长的 CREATE VIEW / SELECT 语句自动换行美化，防止预览框出现单行千字无换行."""
    raw = (sql_text or "").strip()
    if not raw:
        return ""
    lines = raw.splitlines()
    if all(len(ln) <= 110 for ln in lines):
        return raw

    # 对超过 110 字符的超长行在顶层关键字与逗号处插入换行
    formatted_lines: List[str] = []
    for ln in lines:
        if len(ln) <= 110 or ln.lstrip().startswith("--"):
            formatted_lines.append(ln)
            continue
        # 先在主 SQL 子句关键字前换行
        s = re.sub(
            r"\s+(?=(?:AS\s+SELECT|SELECT|FROM|LEFT\s+OUTER\s+JOIN|LEFT\s+JOIN|RIGHT\s+OUTER\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|FULL\s+OUTER\s+JOIN|FULL\s+JOIN|CROSS\s+JOIN|JOIN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|UNION\s+ALL|UNION)\b)",
            "\n",
            ln,
            flags=re.IGNORECASE,
        )
        for sub_ln in s.splitlines():
            if len(sub_ln) <= 110:
                formatted_lines.append(sub_ln)
                continue
            # 在括号深度为 0 的逗号后换行
            buf: List[str] = []
            depth = 0
            in_q: Optional[str] = None
            for ch in sub_ln:
                buf.append(ch)
                if in_q:
                    if ch == in_q:
                        in_q = None
                else:
                    if ch in ("'", '"', "`"):
                        in_q = ch
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth = max(0, depth - 1)
                    elif ch == "," and depth == 0:
                        buf.append("\n    ")
            formatted_lines.append("".join(buf))
    return "\n".join(formatted_lines)


class InceptorThriftClient:
    """高兼容纯 Python HiveServer2 Thrift 客户端（全面适配星环 Inceptor 各版本默认 10000 端口）.

    核心特性与修复：
    1. 严格遵循 TCLIService.thrift 规范提取 TOpenSessionResp.sessionHandle (field 3)，解决因读取错位导致后续查询报连接断开的问题；
    2. 支持协议版本自动协商 (V6 -> V5 -> V4 -> V3 -> V1)，兼容基于 Hive 0.12/0.13/1.2/2.x 的全系列星环 Inceptor 版本；
    3. 支持传输层自动协商回退：优先按用户所选认证模式 (LDAP/NONE SASL PLAIN 或 NOSASL) 连接，若遇服务端传输模式不一致导致握手断开，自动尝试 SASL_PLAIN / NOSASL_UNFRAMED / NOSASL_FRAMED；
    4. 支持 GetResultSetMetadata (probe_query_schema)，可针对视图或任意 SELECT 语句执行零行元数据探测 (SELECT * FROM v WHERE 1=0)，直接获取编译期列结构与精确类型。
    """

    # Thrift Type Constants
    T_STOP = 0
    T_BOOL = 2
    T_BYTE = 3
    T_DOUBLE = 4
    T_I16 = 6
    T_I32 = 8
    T_I64 = 10
    T_STRING = 11
    T_STRUCT = 12
    T_MAP = 13
    T_SET = 14
    T_LIST = 15

    # SASL negotiation status bytes
    SASL_START = 1
    SASL_OK = 2
    SASL_BAD = 3
    SASL_ERROR = 4
    SASL_COMPLETE = 5

    # TCLIService TTypeId Constants
    HIVE_TYPE_MAP = {
        0: "BOOLEAN",
        1: "TINYINT",
        2: "SMALLINT",
        3: "INT",
        4: "BIGINT",
        5: "FLOAT",
        6: "DOUBLE",
        7: "STRING",
        8: "TIMESTAMP",
        9: "BINARY",
        10: "ARRAY",
        11: "MAP",
        12: "STRUCT",
        13: "UNIONTYPE",
        15: "DECIMAL",
        16: "NULL",
        17: "DATE",
        18: "VARCHAR",
        19: "CHAR",
        20: "INTERVAL_YEAR_MONTH",
        21: "INTERVAL_DAY_TIME",
    }

    def __init__(
        self,
        host: str,
        port: int = 10000,
        dbname: str = "default",
        user: str = "hive",
        password: str = "",
        auth_mode: str = "LDAP",
        timeout: float = 12.0,
    ) -> None:
        self.host = host.strip()
        self.port = int(port or 10000)
        self.dbname = (dbname or "default").strip() or "default"
        self.user = (user or "anonymous").strip() or "anonymous"
        # LDAP 模式下保留真实密码；NONE 免密模式下若密码为空则填充 anonymous 以满足 SASL PLAIN 非空要求
        self.raw_password = password or ""
        self.auth_mode = (auth_mode or "LDAP").strip().upper()
        if self.raw_password:
            self.password = self.raw_password
        elif self.auth_mode == "LDAP":
            self.password = ""
        else:
            self.password = "anonymous"
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._seq_id = 0
        self._session_handle: Optional[Tuple[bytes, bytes]] = None
        self._protocol_version = 4  # 默认从兼容性最广的 HIVE_CLI_SERVICE_PROTOCOL_V5 (4) 开始协商
        self._transport_framing = "SASL"  # "SASL" | "FRAMED" | "UNFRAMED"

    def __enter__(self) -> "InceptorThriftClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("星环 Inceptor HiveServer2 连接已断开")
            buf.extend(chunk)
        return bytes(buf)

    def _send_sasl_msg(self, status: int, payload: bytes) -> None:
        assert self._sock is not None
        header = struct.pack(">BI", status, len(payload))
        self._sock.sendall(header + payload)

    def _recv_sasl_msg(self) -> Tuple[int, bytes]:
        header = self._recv_exact(5)
        status, length = struct.unpack(">BI", header)
        payload = self._recv_exact(length) if length > 0 else b""
        return status, payload

    def _perform_sasl_plain_handshake(self, pwd_to_use: str) -> None:
        self._send_sasl_msg(self.SASL_START, b"PLAIN")
        auth_bytes = (
            b"\x00"
            + self.user.encode("utf-8")
            + b"\x00"
            + pwd_to_use.encode("utf-8")
        )
        self._send_sasl_msg(self.SASL_OK, auth_bytes)
        while True:
            status, payload = self._recv_sasl_msg()
            if status == self.SASL_COMPLETE:
                break
            if status in (self.SASL_BAD, self.SASL_ERROR):
                err_txt = payload.decode("utf-8", errors="replace")
                raise PermissionError(f"星环 Inceptor 账号或密码认证失败 (SASL): {err_txt}")
            self._send_sasl_msg(self.SASL_OK, b"")

    def connect(self) -> None:
        # 构建候选传输与协议组合，自动克服不同版本星环 Inceptor 的 SASL / NOSASL 与协议版本差异
        if self.auth_mode == "NOSASL":
            framing_candidates = ["UNFRAMED", "FRAMED", "SASL"]
        else:
            framing_candidates = ["SASL", "UNFRAMED", "FRAMED"]

        # 候选协议版本：V5(4), V6(5), V4(3), V3(2), V1(0)
        proto_candidates = [4, 5, 3, 2, 0]
        last_err: Optional[Exception] = None

        for framing in framing_candidates:
            for proto_ver in proto_candidates:
                self._close_socket_only()
                try:
                    self._sock = socket.create_connection(
                        (self.host, self.port), timeout=self.timeout
                    )
                    self._transport_framing = framing
                    self._protocol_version = proto_ver
                    self._seq_id = 0

                    if framing == "SASL":
                        pwd_try = self.password if self.password != "" else "anonymous"
                        self._perform_sasl_plain_handshake(pwd_try)

                    self._open_session(proto_ver)

                    # 会话建立成功！尝试切换默认库（若填入的是实例名而非库名，捕获异常不中断连接）
                    if self.dbname and self.dbname.lower() != "default":
                        try:
                            safe_db = validate_identifier(self.dbname, "Inceptor数据库名")
                            self.execute_query(f"USE {safe_db}")
                        except Exception as use_exc:
                            logger.warning(
                                "星环 Inceptor 连接已建立，但切换默认库 USE %s 未生效 (%s)，将使用服务端默认库。",
                                self.dbname,
                                use_exc,
                            )
                    return
                except PermissionError:
                    # 若为明确的账号密码错误，且密码原本为空但尝试了 anonymous，再试一次空密码，否则直接抛出
                    self._close_socket_only()
                    raise
                except Exception as exc:
                    last_err = exc
                    self._close_socket_only()
                    # 若在 SASL 握手阶段就断开，说明服务端可能不是 SASL 模式，直接跳出内层协议循环切换下一种 framing
                    if self._session_handle is None and "OpenSession" not in str(exc) and framing == "SASL" and proto_ver == proto_candidates[0]:
                        # 尝试下一个 proto_ver 一次（排除因 OpenSession 协议版本过高导致的断开）
                        continue

        raise RuntimeError(
            f"无法建立星环 Inceptor ({self.host}:{self.port}) 会话: {last_err}"
        )

    def _send_framed_rpc(self, method_name: str, body_bytes: bytes) -> bytes:
        assert self._sock is not None
        self._seq_id += 1
        name_b = method_name.encode("utf-8")
        # TBinaryProtocol VERSION_1 | CALL (1) = 0x80010001
        msg = (
            struct.pack(">II", 0x80010001, len(name_b))
            + name_b
            + struct.pack(">i", self._seq_id)
            + body_bytes
            + b"\x00"  # T_STOP of args struct
        )
        if self._transport_framing in ("SASL", "FRAMED"):
            self._sock.sendall(struct.pack(">I", len(msg)) + msg)
            frame_len = struct.unpack(">I", self._recv_exact(4))[0]
            return self._recv_exact(frame_len)
        else:
            # UNFRAMED (原始 TSocket + TBinaryProtocol)：循环接收直至拼出完整的 Thrift 响应包
            self._sock.sendall(msg)
            buf = bytearray()
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    if not buf:
                        raise ConnectionError(f"星环 Inceptor 在执行 {method_name} 时断开连接")
                    break
                buf.extend(chunk)
                try:
                    # 尝试试探解析是否已收到完整 Thrift Struct
                    self._probe_complete_thrift_message(bytes(buf))
                    return bytes(buf)
                except (struct.error, IndexError):
                    continue

    @classmethod
    def _probe_complete_thrift_message(cls, data: bytes) -> None:
        if len(data) < 12:
            raise IndexError("incomplete header")
        offset = 4
        name_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4 + name_len + 4
        if offset >= len(data):
            raise IndexError("incomplete body")
        cls._read_struct(data, offset)

    @staticmethod
    def _pack_string(val: str) -> bytes:
        b = val.encode("utf-8")
        return struct.pack(">I", len(b)) + b

    @staticmethod
    def _pack_binary(val: bytes) -> bytes:
        return struct.pack(">I", len(val)) + val

    def _pack_handle_id(self, guid: bytes, secret: bytes) -> bytes:
        # THandleIdentifier: 1: binary guid, 2: binary secret
        return (
            struct.pack(">Bh", self.T_STRING, 1)
            + self._pack_binary(guid)
            + struct.pack(">Bh", self.T_STRING, 2)
            + self._pack_binary(secret)
            + b"\x00"
        )

    def _open_session(self, proto_ver: Optional[int] = None) -> None:
        eff_proto = self._protocol_version if proto_ver is None else int(proto_ver)
        # TOpenSessionReq (field 1 of OpenSession args):
        # 1: required TProtocolVersion client_protocol
        # 2: optional string username
        # 3: optional string password
        req_body = (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + struct.pack(">Bh", self.T_I32, 1)
            + struct.pack(">i", eff_proto)
            + struct.pack(">Bh", self.T_STRING, 2)
            + self._pack_string(self.user)
            + struct.pack(">Bh", self.T_STRING, 3)
            + self._pack_string(self.password)
            + b"\x00"
        )
        resp = self._send_framed_rpc("OpenSession", req_body)
        parsed = self._parse_thrift_message(resp)
        # TCLIService.thrift -> TOpenSessionResp:
        # 1: required TStatus status
        # 2: required TProtocolVersion serverProtocolVersion
        # 3: optional TSessionHandle sessionHandle
        # 4: optional map<string, string> configuration
        res_struct = parsed.get(0, {})
        self._check_status(res_struct.get(1, {}))
        if 2 in res_struct and isinstance(res_struct[2], int):
            self._protocol_version = res_struct[2]

        # 关键修复：Field 3 才是 sessionHandle (Field 4 是 configuration map)
        sess_struct = res_struct.get(3)
        if not isinstance(sess_struct, dict) or 1 not in sess_struct:
            sess_struct = res_struct.get(4, {}) if isinstance(res_struct.get(4), dict) else {}
        handle_id = sess_struct.get(1, {}) if isinstance(sess_struct, dict) else {}
        guid = handle_id.get(1, b"") if isinstance(handle_id, dict) else b""
        secret = handle_id.get(2, b"") if isinstance(handle_id, dict) else b""
        if isinstance(guid, str):
            guid = guid.encode("latin-1")
        if isinstance(secret, str):
            secret = secret.encode("latin-1")
        if not guid:
            raise RuntimeError("OpenSession 未返回有效的 SessionHandle GUID")
        self._session_handle = (guid, secret)

    @staticmethod
    def _check_status(status_struct: Dict[int, Any]) -> None:
        if not isinstance(status_struct, dict):
            return
        # TStatus: 1: statusCode (0=SUCCESS, 1=SUCCESS_WITH_INFO, 2=STILL_EXECUTING, 3=ERROR, 4=INVALID_HANDLE)
        code = status_struct.get(1, 0)
        if code in (3, 4):
            err_msg = status_struct.get(3, b"Inceptor Thrift RPC Error")
            if isinstance(err_msg, bytes):
                err_msg = err_msg.decode("utf-8", errors="replace")
            raise RuntimeError(f"星环 Inceptor 执行异常: {err_msg}")

    def _encode_op_handle(
        self, op_guid: bytes, op_secret: bytes, op_type: int, has_result_set: bool
    ) -> bytes:
        return (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + self._pack_handle_id(op_guid, op_secret)
            + struct.pack(">Bh", self.T_I32, 2)
            + struct.pack(">i", op_type)
            + struct.pack(">Bh", self.T_BOOL, 3)
            + struct.pack(">B", 1 if has_result_set else 0)
            + b"\x00"
        )

    def _close_operation(self, op_handle_bytes: bytes) -> None:
        try:
            req = (
                struct.pack(">Bh", self.T_STRUCT, 1)
                + struct.pack(">Bh", self.T_STRUCT, 1)
                + op_handle_bytes
                + b"\x00"
            )
            self._send_framed_rpc("CloseOperation", req)
        except Exception:
            pass

    def _execute_statement_raw(self, sql: str) -> Tuple[bytes, bool]:
        if not self._session_handle:
            raise RuntimeError("星环 Inceptor 会话尚未初始化")
        guid, secret = self._session_handle
        sess_bytes = (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + self._pack_handle_id(guid, secret)
            + b"\x00"
        )
        # TExecuteStatementReq: 1: sessionHandle, 2: statement, 4: runAsync=false
        req_body = (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + struct.pack(">Bh", self.T_STRUCT, 1)
            + sess_bytes
            + struct.pack(">Bh", self.T_STRING, 2)
            + self._pack_string(sql)
            + struct.pack(">BhB", self.T_BOOL, 4, 0)
            + b"\x00"
        )
        resp = self._send_framed_rpc("ExecuteStatement", req_body)
        parsed = self._parse_thrift_message(resp)
        res_struct = parsed.get(0, {})
        self._check_status(res_struct.get(1, {}))
        op_handle = res_struct.get(2, {})
        has_result_set = bool(op_handle.get(3, False))
        op_id = op_handle.get(1, {})
        op_guid = op_id.get(1, b"")
        op_secret = op_id.get(2, b"")
        if isinstance(op_guid, str):
            op_guid = op_guid.encode("latin-1")
        if isinstance(op_secret, str):
            op_secret = op_secret.encode("latin-1")
        op_type = int(op_handle.get(2, 0))
        op_handle_bytes = self._encode_op_handle(
            op_guid, op_secret, op_type, has_result_set
        )
        return op_handle_bytes, has_result_set

    def execute_query(self, sql: str) -> List[List[Any]]:
        op_handle_bytes, has_result_set = self._execute_statement_raw(sql)
        if not has_result_set:
            self._close_operation(op_handle_bytes)
            return []

        try:
            # TFetchResultsReq: 1: operationHandle, 2: orientation (0=FETCH_NEXT), 3: maxRows (5000)
            fetch_req = (
                struct.pack(">Bh", self.T_STRUCT, 1)
                + struct.pack(">Bh", self.T_STRUCT, 1)
                + op_handle_bytes
                + struct.pack(">Bh", self.T_I32, 2)
                + struct.pack(">i", 0)
                + struct.pack(">Bh", self.T_I64, 3)
                + struct.pack(">q", 5000)
                + b"\x00"
            )
            fetch_resp = self._send_framed_rpc("FetchResults", fetch_req)
            fetch_parsed = self._parse_thrift_message(fetch_resp)
            fetch_res = fetch_parsed.get(0, {})
            self._check_status(fetch_res.get(1, {}))
            rowset = fetch_res.get(3, {})
            return self._extract_rows_from_rowset(rowset)
        finally:
            self._close_operation(op_handle_bytes)

    def probe_query_schema(self, sql: str) -> List[ColumnMeta]:
        """利用 HiveServer2 GetResultSetMetadata 对视图或 SELECT 语句执行零行结果集元数据探测."""
        op_handle_bytes, _ = self._execute_statement_raw(sql)
        try:
            meta_req = (
                struct.pack(">Bh", self.T_STRUCT, 1)
                + struct.pack(">Bh", self.T_STRUCT, 1)
                + op_handle_bytes
                + b"\x00"
            )
            meta_resp = self._send_framed_rpc("GetResultSetMetadata", meta_req)
            meta_parsed = self._parse_thrift_message(meta_resp)
            meta_res = meta_parsed.get(0, {})
            self._check_status(meta_res.get(1, {}))
            schema_struct = meta_res.get(2, {})
            col_descs = schema_struct.get(1, []) if isinstance(schema_struct, dict) else []
            cols: List[ColumnMeta] = []

            for idx, cd in enumerate(col_descs, start=1):
                if not isinstance(cd, dict):
                    continue
                raw_name = cd.get(1, b"")
                if isinstance(raw_name, bytes):
                    raw_name = raw_name.decode("utf-8", errors="replace")
                col_name = str(raw_name).strip()
                # 若 Hive 返回 alias.col_name 格式，剥离表名前缀
                if "." in col_name:
                    col_name = col_name.split(".")[-1].strip()

                raw_comm = cd.get(4, b"")
                if isinstance(raw_comm, bytes):
                    raw_comm = raw_comm.decode("utf-8", errors="replace")
                col_comm = str(raw_comm or "").strip()

                # 解析 TTypeDesc (field 2) -> types (field 1: list<TTypeEntry>) -> primitiveEntry (field 1)
                col_type = "STRING"
                type_desc = cd.get(2, {})
                type_entries = type_desc.get(1, []) if isinstance(type_desc, dict) else []
                if type_entries and isinstance(type_entries[0], dict):
                    prim = type_entries[0].get(1, {})
                    if isinstance(prim, dict):
                        tid = int(prim.get(1, 7))
                        base_t = self.HIVE_TYPE_MAP.get(tid, "STRING")
                        quals_struct = prim.get(2, {})
                        q_map_raw = quals_struct.get(1, {}) if isinstance(quals_struct, dict) else {}
                        q_map: Dict[str, int] = {}
                        if isinstance(q_map_raw, dict):
                            for qk, qv in q_map_raw.items():
                                k_str = qk.decode("utf-8", errors="replace") if isinstance(qk, bytes) else str(qk)
                                if isinstance(qv, dict) and 1 in qv and isinstance(qv[1], int):
                                    q_map[k_str] = qv[1]
                        if base_t in ("VARCHAR", "CHAR") and "characterMaximumLength" in q_map:
                            col_type = f"{base_t}({q_map['characterMaximumLength']})"
                        elif base_t == "DECIMAL" and "precision" in q_map:
                            sc = q_map.get("scale", 0)
                            col_type = f"DECIMAL({q_map['precision']},{sc})"
                        else:
                            col_type = base_t

                if col_name:
                    cols.append(
                        ColumnMeta(
                            ordinal_position=len(cols) + 1,
                            column_name=col_name,
                            data_type=col_type,
                            is_not_null=False,
                            column_default=None,
                            column_comment=col_comm,
                        )
                    )
            return cols
        finally:
            self._close_operation(op_handle_bytes)

    @classmethod
    def _extract_rows_from_rowset(cls, rowset: Dict[int, Any]) -> List[List[Any]]:
        # 1) Column-based TRowSet (field 3: list<TColumn>)
        cols = rowset.get(3)
        if isinstance(cols, list) and cols:
            col_arrays: List[List[Any]] = []
            for c_struct in cols:
                if not isinstance(c_struct, dict):
                    continue
                # TColumn union: 1:boolVal, 2:byteVal, 3:i16Val, 4:i32Val, 5:i64Val, 6:doubleVal, 7:stringVal
                vals: List[Any] = []
                for field_id in (7, 4, 5, 3, 6, 1, 2):
                    if field_id in c_struct and isinstance(c_struct[field_id], dict):
                        raw_list = c_struct[field_id].get(1, [])
                        vals = [
                            v.decode("utf-8", errors="replace")
                            if isinstance(v, bytes)
                            else v
                            for v in raw_list
                        ]
                        break
                col_arrays.append(vals)
            if not col_arrays:
                return []
            num_rows = max(len(arr) for arr in col_arrays)
            rows: List[List[Any]] = []
            for r_idx in range(num_rows):
                rows.append(
                    [
                        arr[r_idx] if r_idx < len(arr) else None
                        for arr in col_arrays
                    ]
                )
            return rows

        # 2) Row-based TRowSet (field 2: list<TRow>)
        raw_rows = rowset.get(2)
        if isinstance(raw_rows, list) and raw_rows:
            out: List[List[Any]] = []
            for r_struct in raw_rows:
                col_vals = r_struct.get(1, []) if isinstance(r_struct, dict) else []
                row_items: List[Any] = []
                for cv in col_vals:
                    val = None
                    if isinstance(cv, dict):
                        for fid in (7, 4, 5, 3, 6, 1, 2):
                            if fid in cv and isinstance(cv[fid], dict):
                                v = cv[fid].get(1)
                                if isinstance(v, bytes):
                                    v = v.decode("utf-8", errors="replace")
                                val = v
                                break
                    row_items.append(val)
                out.append(row_items)
            return out
        return []

    @classmethod
    def _parse_thrift_message(cls, data: bytes) -> Dict[int, Any]:
        # Header: 4 bytes version+type, 4 bytes method name len + method name, 4 bytes seqid
        offset = 0
        ver_type = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        name_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4 + name_len
        _seq_id = struct.unpack_from(">i", data, offset)[0]
        offset += 4
        msg_type = ver_type & 0xFF
        struct_val, _ = cls._read_struct(data, offset)
        if msg_type == 3:  # T_EXCEPTION
            err_msg = struct_val.get(1, b"Thrift Application Exception")
            if isinstance(err_msg, bytes):
                err_msg = err_msg.decode("utf-8", errors="replace")
            raise RuntimeError(f"Thrift RPC 异常: {err_msg}")
        return struct_val

    @classmethod
    def _read_struct(cls, data: bytes, offset: int) -> Tuple[Dict[int, Any], int]:
        fields: Dict[int, Any] = {}
        while offset < len(data):
            ftype = data[offset]
            offset += 1
            if ftype == cls.T_STOP:
                break
            fid = struct.unpack_from(">h", data, offset)[0]
            offset += 2
            val, offset = cls._read_value(data, offset, ftype)
            fields[fid] = val
        return fields, offset

    @classmethod
    def _read_value(cls, data: bytes, offset: int, ftype: int) -> Tuple[Any, int]:
        if ftype == cls.T_BOOL:
            return bool(data[offset]), offset + 1
        if ftype == cls.T_BYTE:
            return struct.unpack_from(">b", data, offset)[0], offset + 1
        if ftype == cls.T_I16:
            return struct.unpack_from(">h", data, offset)[0], offset + 2
        if ftype == cls.T_I32:
            return struct.unpack_from(">i", data, offset)[0], offset + 4
        if ftype == cls.T_I64:
            return struct.unpack_from(">q", data, offset)[0], offset + 8
        if ftype == cls.T_DOUBLE:
            return struct.unpack_from(">d", data, offset)[0], offset + 8
        if ftype == cls.T_STRING:
            length = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            return data[offset : offset + length], offset + length
        if ftype == cls.T_STRUCT:
            return cls._read_struct(data, offset)
        if ftype == cls.T_LIST or ftype == cls.T_SET:
            elem_type = data[offset]
            count = struct.unpack_from(">I", data, offset + 1)[0]
            offset += 5
            items = []
            for _ in range(count):
                v, offset = cls._read_value(data, offset, elem_type)
                items.append(v)
            return items, offset
        if ftype == cls.T_MAP:
            ktype = data[offset]
            vtype = data[offset + 1]
            count = struct.unpack_from(">I", data, offset + 2)[0]
            offset += 6
            mp = {}
            for _ in range(count):
                k, offset = cls._read_value(data, offset, ktype)
                v, offset = cls._read_value(data, offset, vtype)
                mp[k] = v
            return mp, offset
        raise ValueError(f"未知的 Thrift 字段类型: {ftype}")

    def _close_socket_only(self) -> None:
        self._session_handle = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def close(self) -> None:
        if self._sock and self._session_handle:
            try:
                guid, secret = self._session_handle
                sess_bytes = (
                    struct.pack(">Bh", self.T_STRUCT, 1)
                    + self._pack_handle_id(guid, secret)
                    + b"\x00"
                )
                req = (
                    struct.pack(">Bh", self.T_STRUCT, 1)
                    + struct.pack(">Bh", self.T_STRUCT, 1)
                    + sess_bytes
                    + b"\x00"
                )
                self._send_framed_rpc("CloseSession", req)
            except Exception:
                pass
        self._close_socket_only()


class CrossEnvCompareEngine:
    """跨环境三栏数标比对中心引擎（管理来源库、数据标准、目标库的三方连接与比对）."""

    def __init__(self, store: StateStore, dws_engine: DWSMigratorEngine) -> None:
        self.store = store
        self.dws_engine = dws_engine
        self._catalog_cache: Dict[str, Dict[str, Any]] = {
            "source": {"schemas": [], "tables": []},
            "target": {"schemas": [], "tables": []},
        }

    def _meta_key_for_role(self, role: str) -> str:
        r = role.strip().lower()
        if r not in ("source", "target"):
            raise ValueError("数据库角色必须为 source (来源库) 或 target (目标库)")
        return f"cross_{r}_conn"

    def get_role_conn_config(self, role: str, include_password: bool = False) -> Dict[str, Any]:
        meta_key = self._meta_key_for_role(role)
        saved = dict(self.store.get_meta(meta_key, {}) or {})

        # 若目标库尚未单独配置，但单库执行台已配置了 DWS，自动继承执行台连接参数
        if role == "target" and not saved.get("host"):
            dws_cfg = self.store.get_meta("dws_connection_config", {}) or {}
            if dws_cfg.get("host"):
                saved = {
                    "configured": bool(dws_cfg.get("configured", True)),
                    "db_type": "DWS",
                    "host": dws_cfg.get("host", ""),
                    "port": int(dws_cfg.get("port", 8000)),
                    "dbname": dws_cfg.get("dbname", ""),
                    "schema_name": "public",
                    "user": dws_cfg.get("user", ""),
                    "password": dws_cfg.get("password", ""),
                    "sslmode": dws_cfg.get("sslmode", "prefer"),
                    "auth_mode": "LDAP",
                }

        db_type = str(saved.get("db_type") or "DWS").upper()
        default_port = 10000 if db_type == "INCEPTOR" else (5432 if db_type == "PGSQL" else 8000)
        cache = self._catalog_cache.get(role, {"schemas": [], "tables": []})

        res = {
            "role": role,
            "configured": bool(saved.get("configured") and saved.get("host")),
            "db_type": db_type,
            "host": str(saved.get("host") or ""),
            "port": int(saved.get("port") or default_port),
            "dbname": str(saved.get("dbname") or ""),
            "schema_name": str(saved.get("schema_name") or ("default" if db_type == "INCEPTOR" else "public")),
            "user": str(saved.get("user") or ""),
            "sslmode": str(saved.get("sslmode") or "prefer"),
            "auth_mode": str(saved.get("auth_mode") or "LDAP"),
            "has_saved_password": bool(saved.get("password")),
            "schemas": cache.get("schemas", []),
            "tables": cache.get("tables", []),
        }
        if include_password:
            res["password"] = str(saved.get("password") or "")
        return res

    def configure_role_connection(
        self,
        role: str,
        *,
        db_type: str,
        host: str,
        port: int,
        dbname: str,
        user: str,
        password: str = "",
        schema_name: str = "",
        sslmode: str = "prefer",
        auth_mode: str = "LDAP",
    ) -> Dict[str, Any]:
        """配置并测试来源库 (source) 或目标库 (target) 连接（支持 DWS / PGSQL / INCEPTOR）."""
        meta_key = self._meta_key_for_role(role)
        clean_type = (db_type or "DWS").strip().upper()
        if clean_type not in SUPPORTED_DB_TYPES:
            raise ValueError(f"不支持的数据库类型: {clean_type}（仅支持 PGSQL / DWS / INCEPTOR）")

        clean_host = (host or "").strip()
        clean_db = (dbname or "").strip() or ("default" if clean_type == "INCEPTOR" else "")
        clean_user = (user or "").strip()
        if not clean_host or not clean_db:
            raise ValueError("主机地址 (Host) 与数据库名 (Database) 不能为空")

        existing = self.store.get_meta(meta_key, {}) or {}
        effective_pwd = password if password != "" else str(existing.get("password") or "")
        effective_schema = (schema_name or "").strip() or (
            clean_db if clean_type == "INCEPTOR" else "public"
        )

        cfg = {
            "configured": True,
            "db_type": clean_type,
            "host": clean_host,
            "port": int(port or (10000 if clean_type == "INCEPTOR" else 8000)),
            "dbname": clean_db,
            "schema_name": effective_schema,
            "user": clean_user,
            "password": effective_pwd,
            "sslmode": (sslmode or "prefer").strip(),
            "auth_mode": (auth_mode or "LDAP").strip().upper(),
        }

        catalog = self._probe_connection_and_catalog(cfg)
        self._catalog_cache[role] = catalog
        if catalog["schemas"] and cfg["schema_name"] not in catalog["schemas"]:
            cfg["schema_name"] = catalog["schemas"][0]
        self.store.save_meta(meta_key, cfg)

        role_cn = "来源库" if role == "source" else "目标库"
        self.store.add_log(
            "SUCCESS",
            f"跨环境{role_cn}连接",
            f"成功连接 {role_cn} [{clean_type}] {clean_host}:{cfg['port']}/{clean_db}，探测到 {len(catalog['schemas'])} 个模式、{len(catalog['tables'])} 张表/视图。",
        )
        return self.get_role_conn_config(role)

    def _probe_connection_and_catalog(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        db_type = cfg["db_type"]
        if db_type in ("DWS", "PGSQL"):
            if psycopg2 is None:
                raise RuntimeError("当前环境未安装 psycopg2 驱动")
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
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT nspname
                        FROM pg_catalog.pg_namespace
                        WHERE nspname NOT LIKE 'pg_%'
                          AND nspname NOT IN ('information_schema', 'cstore', 'db4ai', 'dbe_perf', 'dbe_pldeveloper', 'pkg_service', 'snapshot', 'blockchain')
                        ORDER BY CASE WHEN nspname = 'public' THEN 0 ELSE 1 END, nspname ASC;
                        """
                    )
                    schemas = [r[0] for r in cur.fetchall()]
                    # 同时扫描物理表 ('r', 'p')、视图 ('v')、物化视图 ('m') 与外表 ('f')
                    cur.execute(
                        """
                        SELECT
                            n.nspname AS schema_name,
                            c.relname AS table_name,
                            COALESCE(obj_description(c.oid, 'pg_class'), '') AS table_comment,
                            c.relkind AS relkind
                        FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                          AND n.nspname NOT LIKE 'pg_%'
                          AND n.nspname NOT IN ('information_schema', 'cstore', 'db4ai', 'dbe_perf', 'dbe_pldeveloper', 'pkg_service', 'snapshot', 'blockchain')
                          AND c.relname NOT LIKE '%\\_\\_mig\\_%'
                          AND c.relname NOT LIKE '%\\_\\_bak\\_%'
                        ORDER BY n.nspname ASC, c.relname ASC
                        LIMIT 1500;
                        """
                    )
                    tables = []
                    for r in cur.fetchall():
                        rk = str(r[3] or "r")
                        obj_t = "VIEW" if rk == "v" else ("MATVIEW" if rk == "m" else "TABLE")
                        comm_str = r[2] or ""
                        if obj_t in ("VIEW", "MATVIEW") and "[视图]" not in comm_str:
                            comm_str = f"[视图] {comm_str}".strip()
                        tables.append(
                            {
                                "schema_name": r[0],
                                "table_name": r[1],
                                "table_comment": comm_str,
                                "object_type": obj_t,
                            }
                        )
                return {"schemas": schemas or ["public"], "tables": tables}
            finally:
                conn.close()

        # INCEPTOR (HiveServer2 Thrift)
        with InceptorThriftClient(
            host=cfg["host"],
            port=int(cfg["port"]),
            dbname=cfg["dbname"],
            user=cfg["user"],
            password=cfg["password"],
            auth_mode=cfg.get("auth_mode", "LDAP"),
        ) as client:
            db_rows = client.execute_query("SHOW DATABASES")
            schemas = [str(r[0]).strip() for r in db_rows if r and r[0]]
            if not schemas:
                schemas = [cfg["dbname"] or "default"]

            lower_map = {s.lower(): s for s in schemas}
            cand_schema = (cfg.get("schema_name") or "").strip().lower()
            cand_db = (cfg.get("dbname") or "").strip().lower()
            if cand_schema in lower_map:
                target_db = lower_map[cand_schema]
            elif cand_db in lower_map:
                target_db = lower_map[cand_db]
            else:
                target_db = schemas[0]

            safe_target_db = validate_identifier(target_db, "Inceptor库名")
            tbl_rows = client.execute_query(f"SHOW TABLES IN {safe_target_db}")
            view_names_lower: set = set()
            try:
                view_rows = client.execute_query(f"SHOW VIEWS IN {safe_target_db}")
                for vr in view_rows:
                    if not vr:
                        continue
                    v_nm = str(
                        vr[1]
                        if len(vr) >= 2 and str(vr[0]).lower() == safe_target_db.lower()
                        else vr[0]
                    ).strip()
                    if v_nm:
                        view_names_lower.add(v_nm.lower())
            except Exception:
                pass

            tables = []
            seen_names: set = set()
            for r in tbl_rows:
                if not r:
                    continue
                t_name = str(
                    r[1]
                    if len(r) >= 2 and str(r[0]).lower() == safe_target_db.lower()
                    else r[0]
                ).strip()
                if t_name and t_name.lower() not in seen_names:
                    seen_names.add(t_name.lower())
                    is_v = t_name.lower() in view_names_lower or t_name.lower().startswith("v_")
                    tables.append(
                        {
                            "schema_name": safe_target_db,
                            "table_name": t_name,
                            "table_comment": "[视图]" if is_v else "",
                            "object_type": "VIEW" if is_v else "TABLE",
                        }
                    )
            for v_low in view_names_lower:
                if v_low not in seen_names:
                    seen_names.add(v_low)
                    tables.append(
                        {
                            "schema_name": safe_target_db,
                            "table_name": v_low,
                            "table_comment": "[视图]",
                            "object_type": "VIEW",
                        }
                    )
            return {"schemas": schemas, "tables": tables}

    def inspect_role_table_metadata(
        self,
        role: str,
        schema_name: str,
        table_name: str,
        *,
        _penetrate_views: bool = True,
    ) -> TableMeta:
        """读取指定环境 (source 或 target) 下某张表或视图的完整字段结构（若为视图则自动穿透底层基表补全类型、注释与分布键）."""
        cfg = self.get_role_conn_config(role, include_password=True)
        raw_t = (table_name or "").strip()
        raw_s = (schema_name or "").strip()
        if "." in raw_t:
            parts = raw_t.split(".", 1)
            if not raw_s:
                raw_s = parts[0]
            raw_t = parts[1]
        clean_schema = clean_identifier_input(raw_s) or cfg.get("schema_name") or "public"
        clean_table = clean_identifier_input(raw_t)
        if not clean_table or not cfg.get("configured"):
            return TableMeta(schema_name=clean_schema, table_name=clean_table, exists=False)

        db_type = cfg["db_type"]
        if db_type in ("DWS", "PGSQL"):
            return self._inspect_pg_or_dws_table(
                cfg, clean_schema, clean_table, _penetrate_views=_penetrate_views
            )
        return self._inspect_inceptor_table(
            cfg, clean_schema, clean_table, _penetrate_views=_penetrate_views
        )

    def _inspect_pg_or_dws_table(
        self,
        cfg: Dict[str, Any],
        schema_name: str,
        table_name: str,
        *,
        _penetrate_views: bool = True,
    ) -> TableMeta:
        if psycopg2 is None:
            return TableMeta(schema_name=schema_name, table_name=table_name, exists=False)
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
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        c.oid,
                        n.nspname,
                        c.relname,
                        COALESCE(obj_description(c.oid, 'pg_class'), ''),
                        COALESCE(array_to_string(c.reloptions, ', '), ''),
                        c.relkind
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND LOWER(c.relname) = LOWER(%s)
                      AND n.nspname NOT LIKE 'pg_%'
                    ORDER BY CASE WHEN LOWER(n.nspname) = LOWER(%s) THEN 0 ELSE 1 END, n.nspname ASC
                    LIMIT 1;
                    """,
                    (table_name, schema_name),
                )
                row = cur.fetchone()
                if not row:
                    return TableMeta(schema_name=schema_name, table_name=table_name, exists=False)
                rel_oid, actual_schema, actual_table, tbl_comment, reloptions_str, relkind = row
                rk = str(relkind or "r")
                obj_type = "VIEW" if rk == "v" else ("MATVIEW" if rk == "m" else "TABLE")

                with_clause = format_reloptions_with_clause(reloptions_str)
                dist_clause = ""
                dist_keys: List[str] = []
                part_clause = ""
                part_keys: List[str] = []
                raw_ddl = ""
                view_def_sql = ""

                if obj_type in ("VIEW", "MATVIEW"):
                    try:
                        cur.execute("SELECT pg_catalog.pg_get_viewdef(%s, true);", (rel_oid,))
                        vrow = cur.fetchone()
                        if vrow and vrow[0]:
                            view_def_sql = str(vrow[0]).strip()
                            raw_ddl = (
                                f"-- [{cfg['db_type']} 视图原始定义 ({obj_type})]\n"
                                f"CREATE OR REPLACE VIEW {quote_ident_str(actual_schema)}.{quote_ident_str(actual_table)} AS\n"
                                f"{view_def_sql}"
                            )
                    except Exception:
                        pass
                elif cfg["db_type"] == "DWS":
                    try:
                        cur.execute("SELECT pg_catalog.pg_get_tabledef(%s);", (rel_oid,))
                        tdef_row = cur.fetchone()
                        if tdef_row and tdef_row[0]:
                            raw_ddl = str(tdef_row[0]).strip()
                            parsed_def = parse_dws_tabledef(raw_ddl)
                            if parsed_def["with_clause"]:
                                with_clause = parsed_def["with_clause"]
                            if parsed_def["distribute_clause"]:
                                dist_clause = parsed_def["distribute_clause"]
                            if parsed_def["distribute_keys"]:
                                dist_keys = parsed_def["distribute_keys"]
                            if parsed_def["partition_clause"]:
                                part_clause = parsed_def["partition_clause"]
                            if parsed_def["partition_keys"]:
                                part_keys = parsed_def["partition_keys"]
                    except Exception:
                        pass

                    if not dist_keys:
                        try:
                            cur.execute(
                                """
                                SELECT a.attname
                                FROM pg_catalog.pgxc_class x
                                JOIN pg_catalog.pg_attribute a
                                  ON a.attrelid = x.pcrelid AND a.attnum = ANY(x.pchashattnum)
                                WHERE x.pcrelid = %s;
                                """,
                                (rel_oid,),
                            )
                            dist_keys = [r[0] for r in cur.fetchall() if r and r[0]]
                        except Exception:
                            dist_keys = []
                else:
                    # PostgreSQL: 提取主键列作为跨库转换时的默认分布/分桶键参考，并提取 PG10+ 分区键
                    try:
                        cur.execute(
                            """
                            SELECT a.attname
                            FROM pg_catalog.pg_constraint con
                            JOIN pg_catalog.pg_attribute a
                              ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey)
                            WHERE con.conrelid = %s AND con.contype = 'p';
                            """,
                            (rel_oid,),
                        )
                        dist_keys = [r[0] for r in cur.fetchall() if r and r[0]]
                    except Exception:
                        dist_keys = []
                    try:
                        cur.execute("SELECT pg_catalog.pg_get_partkeydef(%s);", (rel_oid,))
                        pk_row = cur.fetchone()
                        if pk_row and pk_row[0]:
                            part_clause = f"PARTITION BY {pk_row[0]}"
                            m_pk = re.search(r"\(([^)]+)\)", pk_row[0])
                            if m_pk:
                                part_keys = [
                                    clean_identifier_input(x)
                                    for x in m_pk.group(1).split(",")
                                    if clean_identifier_input(x)
                                ]
                    except Exception:
                        pass

                cur.execute(
                    """
                    SELECT
                        a.attname AS column_name,
                        pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                        a.attnotnull AS is_not_null,
                        pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS column_default,
                        COALESCE(col_description(c.oid, a.attnum), '') AS column_comment
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                    LEFT JOIN pg_catalog.pg_attrdef ad
                      ON ad.adrelid = c.oid AND ad.adnum = a.attnum
                    WHERE c.oid = %s
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attnum ASC;
                    """,
                    (rel_oid,),
                )
                cols = [
                    ColumnMeta(
                        ordinal_position=idx,
                        column_name=r[0],
                        data_type=r[1],
                        is_not_null=bool(r[2]),
                        column_default=r[3],
                        column_comment=r[4] or "",
                    )
                    for idx, r in enumerate(cur.fetchall(), start=1)
                ]

                meta = TableMeta(
                    schema_name=actual_schema,
                    table_name=actual_table,
                    exists=True,
                    table_comment=tbl_comment or "",
                    with_clause=with_clause,
                    distribute_clause=dist_clause,
                    distribute_keys=dist_keys,
                    partition_clause=part_clause,
                    partition_keys=part_keys,
                    columns=cols,
                    raw_ddl=raw_ddl,
                    object_type=obj_type,
                    view_definition=view_def_sql,
                )
        finally:
            conn.close()

        if _penetrate_views and meta.object_type in ("VIEW", "MATVIEW") and meta.view_definition:
            meta = self._enrich_view_metadata_from_lineage(meta, role_cfg=cfg)
        return meta

    def _inspect_inceptor_table(
        self,
        cfg: Dict[str, Any],
        schema_name: str,
        table_name: str,
        *,
        _penetrate_views: bool = True,
    ) -> TableMeta:
        safe_db = validate_identifier(schema_name or cfg["dbname"] or "default", "Inceptor库名")
        safe_tbl = validate_identifier(table_name, "Inceptor表名")
        with InceptorThriftClient(
            host=cfg["host"],
            port=int(cfg["port"]),
            dbname=safe_db,
            user=cfg["user"],
            password=cfg["password"],
            auth_mode=cfg.get("auth_mode", "LDAP"),
        ) as client:
            # 1) 优先执行 DESCRIBE FORMATTED，同时获取输出列结构、分区列、Table Type (VIRTUAL_VIEW) 与视图 SQL
            rows: List[List[Any]] = []
            used_formatted = False
            try:
                rows = client.execute_query(f"DESCRIBE FORMATTED {safe_db}.{safe_tbl}")
                if rows:
                    used_formatted = True
            except Exception:
                rows = []
            if not rows:
                try:
                    rows = client.execute_query(f"DESCRIBE {safe_db}.{safe_tbl}")
                except Exception:
                    rows = []

            cols: List[ColumnMeta] = []
            part_keys: List[str] = []
            obj_type = "TABLE"
            tbl_comment = ""
            view_def_lines: List[str] = []
            section = "COLUMNS"  # "COLUMNS" | "PARTITION" | "DETAILED"
            capturing_view_text = False

            for r in rows:
                c0 = str(r[0] if len(r) > 0 and r[0] is not None else "").strip()
                c1 = str(r[1] if len(r) > 1 and r[1] is not None else "").strip()
                c2 = str(r[2] if len(r) > 2 and r[2] is not None else "").strip()

                if c0.startswith("#"):
                    low_hdr = c0.lower()
                    capturing_view_text = False
                    if "partition" in low_hdr and "not partitioned" not in low_hdr:
                        section = "PARTITION"
                    elif "detailed" in low_hdr or "storage" in low_hdr or "view" in low_hdr:
                        section = "DETAILED"
                    elif "col_name" in low_hdr:
                        if section == "PARTITION":
                            pass
                        else:
                            section = "COLUMNS"
                    continue

                if section == "DETAILED":
                    low_c0 = c0.lower().rstrip(":")
                    if low_c0 == "table type":
                        if "view" in c1.lower():
                            obj_type = "VIEW"
                        capturing_view_text = False
                    elif low_c0 in ("view expanded text", "view original text"):
                        obj_type = "VIEW"
                        capturing_view_text = True
                        if c1:
                            view_def_lines.append(c1)
                    elif low_c0 in (
                        "table parameters",
                        "storage information",
                        "serde library",
                        "inputformat",
                        "outputformat",
                        "compressed",
                        "num buckets",
                        "bucket columns",
                        "sort columns",
                        "retention",
                        "create time",
                        "last access",
                        "owner",
                        "location",
                    ):
                        capturing_view_text = False
                    elif capturing_view_text:
                        line_piece = c0 or c1 or c2
                        if line_piece:
                            view_def_lines.append(line_piece)

                    if c1.lower() == "comment" and c2:
                        tbl_comment = c2
                    continue

                if not c0:
                    continue

                if section == "PARTITION":
                    if c0 not in part_keys:
                        part_keys.append(c0)
                    # 若 DESCRIBE FORMATTED 的主列区未包含该分区列，补充至 cols
                    if not any(existing_c.column_name.lower() == c0.lower() for existing_c in cols):
                        cols.append(
                            ColumnMeta(
                                ordinal_position=len(cols) + 1,
                                column_name=c0,
                                data_type=c1 or "STRING",
                                is_not_null=False,
                                column_default=None,
                                column_comment=c2,
                            )
                        )
                    continue

                cols.append(
                    ColumnMeta(
                        ordinal_position=len(cols) + 1,
                        column_name=c0,
                        data_type=c1,
                        is_not_null=False,
                        column_default=None,
                        column_comment=c2 if c2.lower() != "from deserializer" else "",
                    )
                )

            # 2) 尝试执行 SHOW CREATE TABLE 获取完整原始 DDL（对于星环视图，返回 CREATE VIEW ... AS SELECT ...）
            raw_ddl = ""
            try:
                ddl_rows = client.execute_query(f"SHOW CREATE TABLE {safe_db}.{safe_tbl}")
                ddl_chunks = [
                    str(dr[0])
                    for dr in ddl_rows
                    if dr and dr[0] is not None
                ]
                raw_ddl = "\n".join(ddl_chunks).strip()
                if re.search(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\b", raw_ddl, flags=re.IGNORECASE):
                    obj_type = "VIEW"
            except Exception:
                raw_ddl = ""

            view_def_sql = raw_ddl if obj_type == "VIEW" and raw_ddl else "\n".join(view_def_lines).strip()

            # 3) 关键破壁：若为视图且 DESCRIBE 未返回列，或者存在列类型为空/unknown，执行零行结果集探测 (SELECT * FROM v WHERE 1=0)
            if not cols or any(not c.data_type or c.data_type.lower() == "unknown" for c in cols):
                try:
                    probed_cols = client.probe_query_schema(
                        f"SELECT * FROM {safe_db}.{safe_tbl} WHERE 1=0"
                    )
                    if not cols and probed_cols:
                        cols = probed_cols
                    elif probed_cols:
                        probed_map = {pc.column_name.lower(): pc for pc in probed_cols}
                        for c in cols:
                            if (not c.data_type or c.data_type.lower() == "unknown") and c.column_name.lower() in probed_map:
                                c.data_type = probed_map[c.column_name.lower()].data_type
                except Exception as probe_exc:
                    logger.warning("星环视图/表 %s.%s 零行元数据探测未生效: %s", safe_db, safe_tbl, probe_exc)

            # 4) 若连 DESCRIBE 與零行探测都未拿到列，但拿到了视图 CREATE VIEW SQL，直接从视图 SQL AST 解析列结构！
            if not cols and view_def_sql:
                parsed_v = parse_view_sql_lineage(view_def_sql, default_schema=safe_db)
                for vc in parsed_v.get("columns", []):
                    cols.append(
                        ColumnMeta(
                            ordinal_position=vc["ordinal_position"],
                            column_name=vc["column_name"],
                            data_type=vc["inferred_type"],
                            is_not_null=False,
                            column_default=None,
                            column_comment=vc["inline_comment"],
                        )
                    )

            meta = TableMeta(
                schema_name=safe_db,
                table_name=safe_tbl,
                exists=bool(cols),
                table_comment=tbl_comment,
                partition_keys=part_keys,
                columns=cols,
                raw_ddl=raw_ddl,
                object_type=obj_type,
                view_definition=view_def_sql,
            )

        if _penetrate_views and (meta.object_type == "VIEW" or meta.view_definition):
            meta = self._enrich_view_metadata_from_lineage(meta, role_cfg=cfg)
        return meta

    def _enrich_view_metadata_from_lineage(
        self,
        meta: TableMeta,
        role_cfg: Optional[Dict[str, Any]] = None,
        base_table_fetcher: Optional[ Any ] = None,
    ) -> TableMeta:
        """视图底层基表穿透引擎：解析视图 SQL 中的底层物理基表与表达式，自动补全视图缺失的字段注释、精确长度及分布/分区键."""
        view_sql = (meta.view_definition or meta.raw_ddl or "").strip()
        if not view_sql:
            return meta

        lineage = parse_view_sql_lineage(view_sql, default_schema=meta.schema_name or "public")
        if not lineage.get("columns") and not lineage.get("base_tables"):
            return meta

        if not meta.table_comment and lineage.get("view_comment"):
            meta.table_comment = lineage["view_comment"]

        # 1. 穿透查询视图引用的底层物理基表元数据（最多穿透前 6 张关联基表，并设置 _penetrate_views=False 防止循环）
        base_meta_map: Dict[Tuple[str, str], TableMeta] = {}
        penetrated_names: List[str] = []

        for bt in (lineage.get("base_tables") or [])[:6]:
            b_sch = bt.get("schema_name") or meta.schema_name or "public"
            b_tbl = bt.get("table_name") or ""
            if not b_tbl or b_tbl.lower() == meta.table_name.lower():
                continue
            key_bt = (b_sch.lower(), b_tbl.lower())
            if key_bt in base_meta_map:
                continue

            b_meta: Optional[TableMeta] = None
            if callable(base_table_fetcher):
                try:
                    b_meta = base_table_fetcher(b_sch, b_tbl)
                except Exception:
                    b_meta = None
            elif role_cfg and role_cfg.get("configured"):
                role_name = str(role_cfg.get("role") or "source")
                try:
                    # 优先尝试调用 inspect_role_table_metadata（兼容单元测试 mock 与真实环境）
                    try:
                        b_meta = self.inspect_role_table_metadata(
                            role_name, b_sch, b_tbl, _penetrate_views=False
                        )
                    except TypeError:
                        b_meta = self.inspect_role_table_metadata(role_name, b_sch, b_tbl)
                except Exception:
                    b_meta = None

            if b_meta and b_meta.exists and b_meta.columns:
                base_meta_map[key_bt] = b_meta
                penetrated_names.append(f"{b_meta.schema_name}.{b_meta.table_name}")

        # 同时加载本地已保存的数据标准字段作为离线补充知识库
        std_col_lookup: Dict[str, Dict[str, Any]] = {}
        for sc in self.store.list_cross_columns():
            for cand_k in (
                sc.get("bound_source_col", ""),
                sc.get("orig_col_name", ""),
                sc.get("std_col_name", ""),
            ):
                if cand_k:
                    std_col_lookup[cand_k.lower()] = sc

        lineage_col_by_name: Dict[str, Dict[str, Any]] = {}
        lineage_cols = lineage.get("columns") or []
        for lc in lineage_cols:
            lineage_col_by_name[lc["column_name"].lower()] = lc

        inherited_dist_keys: List[str] = list(meta.distribute_keys or [])
        inherited_part_keys: List[str] = list(meta.partition_keys or [])

        # 若视图原本没有列（例如离线解析视图 SQL），直接用 lineage_cols 构建列
        if not meta.columns and lineage_cols:
            meta.columns = [
                ColumnMeta(
                    ordinal_position=lc["ordinal_position"],
                    column_name=lc["column_name"],
                    data_type=lc["inferred_type"],
                    is_not_null=False,
                    column_default=None,
                    column_comment=lc["inline_comment"],
                )
                for lc in lineage_cols
            ]

        for idx, col in enumerate(meta.columns):
            lc = lineage_col_by_name.get(col.column_name.lower())
            if lc is None and idx < len(lineage_cols):
                lc = lineage_cols[idx]

            src_sch = (lc.get("source_schema") if lc else "") or meta.schema_name
            src_tbl = (lc.get("source_table") if lc else "") or ""
            src_col_nm = (lc.get("source_column") if lc else "") or col.column_name

            # 尝试在已穿透的底层物理基表中定位对应源字段
            matched_base_col: Optional[ColumnMeta] = None
            matched_base_meta: Optional[TableMeta] = None

            if src_tbl:
                cand_bm = base_meta_map.get((src_sch.lower(), src_tbl.lower()))
                if not cand_bm:
                    cand_bm = next(
                        (bm for (s_k, t_k), bm in base_meta_map.items() if t_k == src_tbl.lower()),
                        None,
                    )
                if cand_bm:
                    matched_base_meta = cand_bm
                    matched_base_col = next(
                        (
                            bc
                            for bc in cand_bm.columns
                            if bc.column_name.lower() == src_col_nm.lower()
                        ),
                        None,
                    )
            if matched_base_col is None and base_meta_map:
                # 在所有关联基表中按同名字段搜索
                for bm in base_meta_map.values():
                    bc_found = next(
                        (
                            bc
                            for bc in bm.columns
                            if bc.column_name.lower() in (src_col_nm.lower(), col.column_name.lower())
                        ),
                        None,
                    )
                    if bc_found:
                        matched_base_meta = bm
                        matched_base_col = bc_found
                        break

            # 1) 补全视图列的精确数据类型：
            #    - 若视图 SQL 中有显式 CAST(x AS VARCHAR(64))，且当前列是无长度泛化类型，优先采用显式 CAST 类型；
            #    - 若穿透找到了底层物理基表字段，且当前视图列是泛化类型（如 Inceptor STRING / PG 无长度 varchar/text），用基表精确类型升级！
            if lc and lc.get("has_explicit_cast") and is_generic_or_unbounded_type(col.data_type):
                col.data_type = lc["inferred_type"]
                col.lineage_source = f"视图CAST({lc['inferred_type']})"
            elif matched_base_col and matched_base_meta:
                base_ref_str = f"{matched_base_meta.schema_name}.{matched_base_meta.table_name}.{matched_base_col.column_name}"
                col.lineage_source = base_ref_str
                if is_generic_or_unbounded_type(col.data_type) and not is_generic_or_unbounded_type(matched_base_col.data_type):
                    col.data_type = matched_base_col.data_type
                elif not col.data_type:
                    col.data_type = matched_base_col.data_type

                if matched_base_col.is_not_null and not col.is_not_null:
                    col.is_not_null = True

                # 继承底层基表的分布键与分区键
                base_dist_lower = {k.lower() for k in (matched_base_meta.distribute_keys or [])}
                base_part_lower = {k.lower() for k in (matched_base_meta.partition_keys or [])}
                if matched_base_col.column_name.lower() in base_dist_lower:
                    if col.column_name not in inherited_dist_keys:
                        inherited_dist_keys.append(col.column_name)
                if matched_base_col.column_name.lower() in base_part_lower:
                    if col.column_name not in inherited_part_keys:
                        inherited_part_keys.append(col.column_name)
            elif is_generic_or_unbounded_type(col.data_type):
                # 若无法连接底层基表，检查本地数标知识库或 SQL 表达式推导类型
                std_match = std_col_lookup.get(col.column_name.lower()) or std_col_lookup.get(src_col_nm.lower())
                if std_match and std_match.get("orig_col_type") and not is_generic_or_unbounded_type(std_match["orig_col_type"]):
                    col.data_type = std_match["orig_col_type"]
                    col.lineage_source = "数标原字段类型"
                elif lc and lc.get("inferred_type") and not col.data_type:
                    col.data_type = lc["inferred_type"]

            # 2) 补全视图缺失的字段中文注释（优先级：视图自身注释 -> 视图SQL行内注释 -> 穿透的底层物理基表注释 -> 数标清单注释）
            if not col.column_comment:
                if lc and lc.get("inline_comment"):
                    col.column_comment = lc["inline_comment"]
                elif matched_base_col and matched_base_col.column_comment:
                    col.column_comment = matched_base_col.column_comment
                else:
                    std_match = std_col_lookup.get(col.column_name.lower()) or std_col_lookup.get(src_col_nm.lower())
                    if std_match:
                        col.column_comment = (
                            std_match.get("orig_col_comment")
                            or std_match.get("std_col_comment")
                            or ""
                        )

        meta.distribute_keys = inherited_dist_keys or (
            [meta.columns[0].column_name] if meta.columns else []
        )
        meta.partition_keys = inherited_part_keys
        meta.penetrated_base_tables = penetrated_names
        return meta

    def build_table_meta_from_view_sql(
        self,
        view_sql: str,
        *,
        default_schema: str = "public",
        default_table: str = "",
        probe_role: str = "source",
    ) -> TableMeta:
        """从用户粘贴的任意视图定义 (CREATE VIEW ... AS SELECT ...) 或 SELECT 语句直接构建完整的 TableMeta."""
        raw = (view_sql or "").strip()
        if not raw:
            raise ValueError("粘贴的视图 DDL 或 SELECT 语句不能为空")

        lineage = parse_view_sql_lineage(raw, default_schema=default_schema)
        if not lineage.get("is_view_sql"):
            raise ValueError("未能识别出有效的 CREATE VIEW ... AS SELECT 或 SELECT 查询语句，请检查 SQL 语法")

        v_schema = clean_identifier_input(default_schema) or lineage.get("view_schema") or "public"
        v_table = (
            clean_identifier_input(default_table)
            or lineage.get("view_name")
            or "v_extracted_table"
        )
        select_query = lineage.get("select_sql") or raw

        role_cfg: Optional[Dict[str, Any]] = None
        if probe_role in ("source", "target"):
            cand_cfg = self.get_role_conn_config(probe_role, include_password=True)
            if cand_cfg.get("configured"):
                role_cfg = cand_cfg

        # 1. 当 SQL 为 SELECT * 等未能从 AST 静态提取出显式列清单且所选环境已连接数据库时，通过零行子查询 SELECT * FROM (<select_sql>) _v_probe WHERE 1=0 获取数据库引擎解析的真实列清单
        probed_cols: List[ColumnMeta] = []
        if role_cfg and not lineage.get("columns"):
            probe_sql = f"SELECT * FROM ({select_query.rstrip(';')}) _v_probe WHERE 1=0"
            try:
                if role_cfg["db_type"] == "INCEPTOR":
                    with InceptorThriftClient(
                        host=role_cfg["host"],
                        port=int(role_cfg["port"]),
                        dbname=v_schema or role_cfg["dbname"],
                        user=role_cfg["user"],
                        password=role_cfg["password"],
                        auth_mode=role_cfg.get("auth_mode", "LDAP"),
                        timeout=4,
                    ) as client:
                        probed_cols = client.probe_query_schema(probe_sql)
                elif psycopg2 is not None:
                    conn = psycopg2.connect(
                        host=role_cfg["host"],
                        port=int(role_cfg["port"]),
                        dbname=role_cfg["dbname"],
                        user=role_cfg["user"],
                        password=role_cfg["password"],
                        sslmode=role_cfg.get("sslmode", "prefer"),
                        connect_timeout=4,
                    )
                    try:
                        conn.autocommit = True
                        with conn.cursor() as cur:
                            cur.execute(probe_sql)
                            if cur.description:
                                type_oids = [d.type_code for d in cur.description]
                                oid_name_map: Dict[int, str] = {}
                                if type_oids:
                                    cur.execute(
                                        "SELECT oid, pg_catalog.format_type(oid, -1) FROM pg_catalog.pg_type WHERE oid = ANY(%s);",
                                        (list(set(type_oids)),),
                                    )
                                    oid_name_map = { int(r[0]): str(r[1]) for r in cur.fetchall() }
                                for idx, d in enumerate(cur.description, start=1):
                                    probed_cols.append(
                                        ColumnMeta(
                                            ordinal_position=idx,
                                            column_name=str(d.name),
                                            data_type=oid_name_map.get(int(d.type_code), "VARCHAR(255)"),
                                            is_not_null=False,
                                            column_default=None,
                                            column_comment="",
                                        )
                                    )
                    finally:
                        conn.close()
            except Exception as exc:
                logger.info("粘贴视图 SQL 实时零行探测未命中 (%s)，将使用 AST 语法树与底层基表穿透推导。", exc)

        # 2. 用 AST 解析出的列结构或实时探测列构建初始 TableMeta，并调用 _enrich_view_metadata_from_lineage 穿透补全
        init_cols = probed_cols or [
            ColumnMeta(
                ordinal_position=lc["ordinal_position"],
                column_name=lc["column_name"],
                data_type=lc["inferred_type"],
                is_not_null=False,
                column_default=None,
                column_comment=lc["inline_comment"],
            )
            for lc in (lineage.get("columns") or [])
        ]

        meta = TableMeta(
            schema_name=v_schema,
            table_name=v_table,
            exists=bool(init_cols),
            table_comment=lineage.get("view_comment", ""),
            columns=init_cols,
            raw_ddl=raw,
            object_type="VIEW",
            view_definition=raw,
        )
        return self._enrich_view_metadata_from_lineage(meta, role_cfg=role_cfg)

    def sync_standards_from_workbench(self) -> Dict[str, Any]:
        """从单库『批量贯标执行台』已有清单一键同步数据标准至三栏比对中心."""
        wb_items = self.store.list_items()
        if not wb_items:
            raise ValueError("当前『批量贯标执行台』中尚无清单数据，请先上传落标清单或数标文件")
        fn = self.store.get_meta("current_excel_name", "执行台同步清单.xlsx")
        self.store.replace_cross_standards(wb_items, fn, preserve_bindings=True)
        return self.get_overview_state()

    def get_overview_state(self, selected_table_key: Optional[str] = None) -> Dict[str, Any]:
        """获取跨环境三栏比对中心的全局状态及当前选中表的三纵列详情."""
        # 若三栏中心尚无数据标准，但执行台已有清单，自动初始化同步一次
        tables = self.store.list_cross_tables()
        if not tables and self.store.list_items():
            fn = self.store.get_meta("current_excel_name", "执行台同步清单.xlsx")
            tables = self.store.replace_cross_standards(
                self.store.list_items(), fn, preserve_bindings=True
            )

        active_key = (selected_table_key or "").strip().lower()
        if not active_key and tables:
            active_key = tables[0]["std_table_key"]

        table_detail = (
            self.compare_single_table(active_key) if active_key else None
        )

        return {
            "source_connection": self.get_role_conn_config("source"),
            "target_connection": self.get_role_conn_config("target"),
            "standard_meta": {
                "filename": self.store.get_meta("cross_standard_filename", "未导入"),
                "imported_at": self.store.get_meta("cross_standard_imported_at", ""),
                "table_count": len(tables),
                "column_count": len(self.store.list_cross_columns()),
            },
            "tables": tables,
            "active_table_key": active_key,
            "active_comparison": table_detail,
        }

    def compare_single_table(
        self,
        std_table_key: str,
        *,
        override_source_schema: Optional[str] = None,
        override_source_table: Optional[str] = None,
        override_target_schema: Optional[str] = None,
        override_target_table: Optional[str] = None,
    ) -> Dict[str, Any]:
        """以表为单位构建左（来源库）、中（数据标准）、右（目标库）三纵列深度比对视图."""
        t_bind = self.store.get_cross_table_binding(std_table_key)
        if not t_bind:
            t_bind = self.store.upsert_cross_table_binding(
                std_table_key,
                source_schema=override_source_schema or "public",
                source_table=override_source_table or std_table_key,
                target_schema=override_target_schema or "public",
                target_table=override_target_table or std_table_key,
            )

        src_conn = self.get_role_conn_config("source")
        tgt_conn = self.get_role_conn_config("target")

        src_schema = (
            override_source_schema
            or t_bind.get("source_schema")
            or src_conn.get("schema_name")
            or "public"
        )
        src_table = (
            override_source_table
            or t_bind.get("source_table")
            or t_bind["std_table_name"]
        )
        tgt_schema = (
            override_target_schema
            or t_bind.get("target_schema")
            or tgt_conn.get("schema_name")
            or "public"
        )
        tgt_table = (
            override_target_table
            or t_bind.get("target_table")
            or t_bind["std_table_name"]
        )

        src_meta = TableMeta(schema_name=src_schema, table_name=src_table, exists=False)
        tgt_meta = TableMeta(schema_name=tgt_schema, table_name=tgt_table, exists=False)
        src_error = ""
        tgt_error = ""

        if src_conn.get("configured"):
            try:
                src_meta = self.inspect_role_table_metadata("source", src_schema, src_table)
            except Exception as exc:
                src_error = str(exc)

        if tgt_conn.get("configured"):
            try:
                tgt_meta = self.inspect_role_table_metadata("target", tgt_schema, tgt_table)
            except Exception as exc:
                tgt_error = str(exc)

        src_col_map: Dict[str, ColumnMeta] = {}
        for c in src_meta.columns:
            src_col_map[clean_identifier_input(c.column_name).lower()] = c
            src_col_map[c.column_name.lower()] = c

        tgt_col_map: Dict[str, ColumnMeta] = {}
        for c in tgt_meta.columns:
            tgt_col_map[clean_identifier_input(c.column_name).lower()] = c
            tgt_col_map[c.column_name.lower()] = c

        std_cols = self.store.list_cross_columns(t_bind["std_table_key"])
        # 若用户直接选择了某张视图或表（尚未为该表上传 Excel 数标清单），自动以来源库列（或目标库列）作为基准列构建三栏比对视图
        if not std_cols and (src_meta.columns or tgt_meta.columns):
            seed_cols = src_meta.columns if src_meta.columns else tgt_meta.columns
            std_cols = [
                {
                    "id": -idx,
                    "row_index": idx,
                    "std_table_key": t_bind["std_table_key"],
                    "std_schema_name": t_bind.get("std_schema_name") or src_schema or "public",
                    "std_table_name": t_bind.get("std_table_name") or src_table or tgt_table,
                    "orig_col_name": c.column_name,
                    "orig_col_type": c.data_type,
                    "orig_col_comment": c.column_comment or "",
                    "std_col_name": c.column_name,
                    "std_col_type": c.data_type,
                    "std_col_comment": c.column_comment or "",
                    "bound_source_col": c.column_name,
                    "bound_target_col": c.column_name,
                }
                for idx, c in enumerate(seed_cols, start=1)
            ]

        src_bound_by_col: Dict[str, List[str]] = {}
        tgt_bound_by_col: Dict[str, List[str]] = {}

        enriched_std_cols: List[Dict[str, Any]] = []
        summary = {
            "total": len(std_cols),
            "compliant": 0,
            "target_pending": 0,
            "source_diff": 0,
            "missing": 0,
        }

        for sc in std_cols:
            orig_name = clean_identifier_input(sc["orig_col_name"])
            std_name = clean_identifier_input(sc["std_col_name"]) or orig_name
            std_type = sc["std_col_type"].strip()
            std_comm = sc["std_col_comment"].strip()

            # 自动匹配或按保存的绑定名定位来源库字段
            bound_src_name = clean_identifier_input(sc.get("bound_source_col") or "")
            src_col = (
                src_col_map.get(bound_src_name.lower())
                or src_col_map.get(orig_name.lower())
                or src_col_map.get(std_name.lower())
            )
            effective_src_col_name = (
                clean_identifier_input(src_col.column_name)
                if src_col
                else (bound_src_name or orig_name or std_name)
            )

            # 自动匹配或按保存的绑定名定位目标库字段（优先匹配新字段名 std_name，其次匹配原字段名 orig_name）
            bound_tgt_name = clean_identifier_input(sc.get("bound_target_col") or "")
            tgt_col = (
                tgt_col_map.get(bound_tgt_name.lower())
                or tgt_col_map.get(std_name.lower())
                or tgt_col_map.get(orig_name.lower())
            )
            effective_tgt_col_name = (
                clean_identifier_input(tgt_col.column_name)
                if tgt_col
                else (bound_tgt_name or std_name or orig_name)
            )

            if src_col:
                src_bound_by_col.setdefault(src_col.column_name.lower(), []).append(std_name)
            if tgt_col:
                tgt_bound_by_col.setdefault(tgt_col.column_name.lower(), []).append(std_name)

            # 诊断来源库与数据标准的对应关系
            src_status = "UNCONNECTED"
            src_detail = "来源库未连接"
            if src_conn.get("configured"):
                if not src_meta.exists:
                    src_status = "MISSING_TABLE"
                    src_detail = f"来源库不存在表 {src_schema}.{src_table}"
                elif not src_col:
                    src_status = "MISSING_COLUMN"
                    src_detail = f"来源库未找到字段 {effective_src_col_name}"
                else:
                    s_exact, s_compat = is_cross_type_compatible(src_col.data_type, std_type)
                    if clean_identifier_input(src_col.column_name).lower() == std_name.lower() and s_exact:
                        src_status = "COMPLIANT"
                        src_detail = f"来源列 [{src_col.column_name}] 已符合标准"
                    elif clean_identifier_input(src_col.column_name).lower() == orig_name.lower():
                        src_status = "MATCHED_ORIG"
                        src_detail = f"匹配原字段 [{src_col.column_name} {src_col.data_type}]"
                    else:
                        src_status = "BOUND_DIFF"
                        src_detail = f"绑定来源列 [{src_col.column_name} {src_col.data_type}]"

            # 诊断目标库落标合规状态
            tgt_status = "UNCONNECTED"
            tgt_detail = "目标库未连接"
            diffs: List[str] = []
            if tgt_conn.get("configured"):
                if not tgt_meta.exists:
                    tgt_status = "MISSING_TABLE"
                    tgt_detail = f"目标库不存在表 {tgt_schema}.{tgt_table}"
                    summary["missing"] += 1
                elif not tgt_col:
                    tgt_status = "MISSING_COLUMN"
                    tgt_detail = f"目标库未找到字段 {std_name} / {orig_name}"
                    summary["missing"] += 1
                else:
                    name_ok = clean_identifier_input(tgt_col.column_name).lower() == std_name.lower()
                    type_exact, type_compat = is_cross_type_compatible(tgt_col.data_type, std_type)
                    comm_ok = (not std_comm) or ((tgt_col.column_comment or "").strip() == std_comm)

                    if not name_ok:
                        diffs.append(f"字段名待改 ({tgt_col.column_name} -> {std_name})")
                    if not type_exact:
                        if type_compat:
                            diffs.append(f"类型精度差异 ({tgt_col.data_type} vs {std_type})")
                        else:
                            diffs.append(f"类型不符 ({tgt_col.data_type} -> {std_type})")
                    if not comm_ok:
                        diffs.append("注释待更新")

                    if name_ok and type_exact and comm_ok:
                        tgt_status = "COMPLIANT"
                        tgt_detail = "目标库字段名、类型、注释均符合数据标准"
                        summary["compliant"] += 1
                    else:
                        tgt_status = "PENDING_MIGRATION"
                        tgt_detail = "；".join(diffs)
                        summary["target_pending"] += 1

            # 对比来源库与目标库两库之间的一致性
            env_diff_text = ""
            if src_col and tgt_col:
                same_name = clean_identifier_input(src_col.column_name).lower() == clean_identifier_input(tgt_col.column_name).lower()
                same_type = normalize_cross_type(src_col.data_type) == normalize_cross_type(tgt_col.data_type)
                if same_name and same_type:
                    env_diff_text = "两库结构一致"
                else:
                    env_diff_text = f"两库差异: {src_col.column_name}({src_col.data_type}) ↔ {tgt_col.column_name}({tgt_col.data_type})"
                    summary["source_diff"] += 1

            enriched_std_cols.append(
                {
                    **sc,
                    "effective_source_col": effective_src_col_name,
                    "effective_target_col": effective_tgt_col_name,
                    "source_col_meta": (
                        {
                            "ordinal_position": src_col.ordinal_position,
                            "column_name": src_col.column_name,
                            "data_type": src_col.data_type,
                            "column_comment": src_col.column_comment,
                        }
                        if src_col
                        else None
                    ),
                    "target_col_meta": (
                        {
                            "ordinal_position": tgt_col.ordinal_position,
                            "column_name": tgt_col.column_name,
                            "data_type": tgt_col.data_type,
                            "column_comment": tgt_col.column_comment,
                        }
                        if tgt_col
                        else None
                    ),
                    "source_status": src_status,
                    "source_detail": src_detail,
                    "target_status": tgt_status,
                    "target_detail": tgt_detail,
                    "env_diff_text": env_diff_text,
                }
            )

        # 构建左列（来源库全表列清单）与右列（目标库全表列清单）
        src_dist_set = {k.lower() for k in (src_meta.distribute_keys + src_meta.partition_keys)}
        tgt_dist_set = {k.lower() for k in (tgt_meta.distribute_keys + tgt_meta.partition_keys)}

        source_columns_view = [
            {
                "ordinal_position": c.ordinal_position,
                "column_name": clean_identifier_input(c.column_name) or c.column_name,
                "raw_column_name": c.column_name,
                "data_type": c.data_type,
                "column_comment": c.column_comment,
                "lineage_source": getattr(c, "lineage_source", ""),
                "is_key": clean_identifier_input(c.column_name).lower() in src_dist_set,
                "bound_std_cols": src_bound_by_col.get(c.column_name.lower(), []),
            }
            for c in src_meta.columns
        ]

        # 为目标库每一列标注其对应的标准达标状态
        tgt_status_by_col: Dict[str, Tuple[str, str]] = {}
        for esc in enriched_std_cols:
            t_meta = esc.get("target_col_meta")
            if t_meta:
                tgt_status_by_col[t_meta["column_name"].lower()] = (
                    esc["target_status"],
                    esc["target_detail"],
                )

        target_columns_view = [
            {
                "ordinal_position": c.ordinal_position,
                "column_name": clean_identifier_input(c.column_name) or c.column_name,
                "raw_column_name": c.column_name,
                "data_type": c.data_type,
                "column_comment": c.column_comment,
                "lineage_source": getattr(c, "lineage_source", ""),
                "is_key": clean_identifier_input(c.column_name).lower() in tgt_dist_set,
                "bound_std_cols": tgt_bound_by_col.get(c.column_name.lower(), []),
                "compliance_status": tgt_status_by_col.get(c.column_name.lower(), ("UNBOUND", "未绑定数标"))[0],
                "compliance_detail": tgt_status_by_col.get(c.column_name.lower(), ("UNBOUND", "未在当前数标清单中声明"))[1],
            }
            for c in tgt_meta.columns
        ]

        return {
            "table_binding": {
                **t_bind,
                "source_schema": src_meta.schema_name if src_meta.exists else src_schema,
                "source_table": src_meta.table_name if src_meta.exists else src_table,
                "target_schema": tgt_meta.schema_name if tgt_meta.exists else tgt_schema,
                "target_table": tgt_meta.table_name if tgt_meta.exists else tgt_table,
            },
            "summary": summary,
            "source_panel": {
                "configured": bool(src_conn.get("configured")),
                "db_type": src_conn.get("db_type", "DWS"),
                "schema_name": src_meta.schema_name if src_meta.exists else src_schema,
                "table_name": src_meta.table_name if src_meta.exists else src_table,
                "exists": src_meta.exists,
                "object_type": getattr(src_meta, "object_type", "TABLE") or "TABLE",
                "view_definition": getattr(src_meta, "view_definition", "") or "",
                "penetrated_base_tables": list(getattr(src_meta, "penetrated_base_tables", []) or []),
                "table_comment": src_meta.table_comment,
                "distribute_keys": src_meta.distribute_keys,
                "partition_keys": src_meta.partition_keys,
                "error": src_error,
                "columns": source_columns_view,
            },
            "standard_panel": {
                "std_table_key": t_bind["std_table_key"],
                "std_schema_name": t_bind["std_schema_name"],
                "std_table_name": t_bind["std_table_name"],
                "columns": enriched_std_cols,
            },
            "target_panel": {
                "configured": bool(tgt_conn.get("configured")),
                "db_type": tgt_conn.get("db_type", "DWS"),
                "schema_name": tgt_meta.schema_name if tgt_meta.exists else tgt_schema,
                "table_name": tgt_meta.table_name if tgt_meta.exists else tgt_table,
                "exists": tgt_meta.exists,
                "object_type": getattr(tgt_meta, "object_type", "TABLE") or "TABLE",
                "view_definition": getattr(tgt_meta, "view_definition", "") or "",
                "penetrated_base_tables": list(getattr(tgt_meta, "penetrated_base_tables", []) or []),
                "table_comment": tgt_meta.table_comment,
                "distribute_keys": tgt_meta.distribute_keys,
                "partition_keys": tgt_meta.partition_keys,
                "error": tgt_error,
                "columns": target_columns_view,
            },
        }

    def save_table_and_column_bindings(
        self,
        std_table_key: str,
        *,
        source_schema: str,
        source_table: str,
        target_schema: str,
        target_table: str,
        column_bindings: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """持久保存单张表（或视图）的左右库表级映射及字段级数标绑定关系."""
        s_sch = (source_schema or "").strip()
        s_tbl = (source_table or "").strip()
        if "." in s_tbl:
            parts = s_tbl.split(".", 1)
            if not s_sch:
                s_sch = parts[0]
            s_tbl = parts[1]

        t_sch = (target_schema or "").strip()
        t_tbl = (target_table or "").strip()
        if "." in t_tbl:
            parts = t_tbl.split(".", 1)
            if not t_sch:
                t_sch = parts[0]
            t_tbl = parts[1]

        eff_key = (std_table_key or "").strip()
        if "." in eff_key:
            eff_key = eff_key.split(".", 1)[1].strip()
        if not eff_key:
            eff_key = s_tbl or t_tbl
        if not eff_key:
            raise ValueError("请先在来源库或目标库中选择/输入要读取的表名或视图名")

        self.store.upsert_cross_table_binding(
            eff_key,
            source_schema=s_sch,
            source_table=s_tbl or eff_key,
            target_schema=t_sch,
            target_table=t_tbl or eff_key,
        )
        if column_bindings:
            valid_bindings = [
                b for b in column_bindings if int(b.get("id", 0) or 0) > 0
            ]
            if valid_bindings:
                self.store.batch_update_cross_column_bindings(valid_bindings)

        self.store.add_log(
            "INFO",
            f"数标绑定保存 [{eff_key}]",
            f"已持久保存表/视图 [{eff_key}] 的三方绑定关系：来源({s_sch}.{s_tbl or eff_key}) ↔ 数标 ↔ 目标({t_sch}.{t_tbl or eff_key})。",
        )
        return self.get_overview_state(selected_table_key=eff_key)

    def push_uncompliant_to_workbench(
        self, std_table_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """将三栏比对中心的目标库待贯标项一键推送至『批量贯标执行台』."""
        tables = (
            [self.store.get_cross_table_binding(std_table_key)]
            if std_table_key
            else self.store.list_cross_tables()
        )
        tables = [t for t in tables if t]
        if not tables:
            raise ValueError("没有可推送的标准绑定表")

        items_to_push: List[Dict[str, Any]] = []
        row_idx = 1
        for t in tables:
            t_key = t["std_table_key"]
            tgt_schema = t.get("target_schema") or t.get("std_schema_name") or "public"
            tgt_table = t.get("target_table") or t["std_table_name"]
            cols = self.store.list_cross_columns(t_key)
            for c in cols:
                old_c = clean_identifier_input(
                    c.get("orig_col_name") or c.get("bound_source_col") or c["std_col_name"]
                )
                new_c = clean_identifier_input(
                    c.get("std_col_name") or c.get("bound_target_col") or old_c
                )
                items_to_push.append(
                    {
                        "row_index": row_idx,
                        "schema_name": tgt_schema,
                        "table_name": tgt_table,
                        "old_col_name": old_c,
                        "old_col_type": c.get("orig_col_type") or c.get("std_col_type") or "",
                        "old_col_comment": c.get("orig_col_comment") or "",
                        "new_col_name": new_c,
                        "new_col_type": c.get("std_col_type") or c.get("orig_col_type") or "",
                        "new_col_comment": c.get("std_col_comment") or "",
                    }
                )
                row_idx += 1

        fn = self.store.get_meta("cross_standard_filename", "跨环境数标推送清单.xlsx")
        self.store.replace_all_items(items_to_push, fn, preserve_history=True)
        if self.dws_engine.is_configured():
            try:
                self.dws_engine.sync_all_items_with_db()
            except Exception:
                pass
        return {
            "pushed_count": len(items_to_push),
            "table_count": len(tables),
            "message": f"已将 {len(tables)} 张表共 {len(items_to_push)} 条数标规则推送至『批量贯标执行台』！",
        }

    def export_comparison_excel(self, std_table_key: Optional[str] = None) -> bytes:
        """导出三方跨环境（来源库 × 数据标准 × 目标库）比对与绑定关系 Excel 报告（同时支持作为绑定包再次导入）."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "跨环境三方数标比对与绑定表"

        headers = [
            "序号",
            "标准模式名",
            "标准表名",
            "来源库模式",
            "来源库表名",
            "来源库绑定字段",
            "来源库当前类型",
            "来源库当前注释",
            "清单原字段名",
            "清单原类型",
            "清单原注释",
            "数据标准字段名",
            "数据标准类型",
            "数据标准注释",
            "目标库模式",
            "目标库表名",
            "目标库绑定字段",
            "目标库当前类型",
            "目标库当前注释",
            "目标库落标状态",
            "三方比对诊断详情",
        ]
        ws.append(headers)

        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True, size=10)
        thin_border = Border(
            left=Side(style="thin", color="CBD5E1"),
            right=Side(style="thin", color="CBD5E1"),
            top=Side(style="thin", color="CBD5E1"),
            bottom=Side(style="thin", color="CBD5E1"),
        )
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

        tables = (
            [self.store.get_cross_table_binding(std_table_key)]
            if std_table_key
            else self.store.list_cross_tables()
        )
        tables = [t for t in tables if t]

        row_num = 1
        for t in tables:
            comp = self.compare_single_table(t["std_table_key"])
            t_bind = comp["table_binding"]
            for sc in comp["standard_panel"]["columns"]:
                src_m = sc.get("source_col_meta") or {}
                tgt_m = sc.get("target_col_meta") or {}
                row_vals = [
                    row_num,
                    t_bind.get("std_schema_name", "public"),
                    t_bind.get("std_table_name", ""),
                    t_bind.get("source_schema", ""),
                    t_bind.get("source_table", ""),
                    sc.get("effective_source_col", ""),
                    src_m.get("data_type", ""),
                    src_m.get("column_comment", ""),
                    sc.get("orig_col_name", ""),
                    sc.get("orig_col_type", ""),
                    sc.get("orig_col_comment", ""),
                    sc.get("std_col_name", ""),
                    sc.get("std_col_type", ""),
                    sc.get("std_col_comment", ""),
                    t_bind.get("target_schema", ""),
                    t_bind.get("target_table", ""),
                    sc.get("effective_target_col", ""),
                    tgt_m.get("data_type", ""),
                    tgt_m.get("column_comment", ""),
                    sc.get("target_status", ""),
                    sc.get("target_detail", ""),
                ]
                ws.append(row_vals)
                row_num += 1

        widths = [6, 14, 22, 14, 22, 20, 18, 22, 20, 16, 20, 22, 18, 24, 14, 22, 20, 18, 22, 16, 36]
        for idx, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def import_bindings_from_file(self, content: bytes, filename: str) -> Dict[str, Any]:
        """支持批量导入 JSON 绑定配置包或 Excel 落标清单/三方绑定表."""
        lower_fn = (filename or "").lower()
        if lower_fn.endswith(".json"):
            import json

            bundle = json.loads(content.decode("utf-8"))
            res = self.store.import_cross_bindings_bundle(bundle, filename=filename)
            self.store.add_log(
                "SUCCESS",
                "导入数标绑定包",
                f"成功从 JSON 绑定包 [{filename}] 恢复 {res['table_count']} 张表、{res['column_count']} 条字段绑定关系。",
            )
            return self.get_overview_state()

        # 解析 Excel (.xlsx)：首先尝试识别是否包含完整三方绑定列，否则走通用 parse_excel_checklist
        from core.excel_handler import parse_excel_checklist

        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) >= 2:
            header_cells = [str(c or "").strip() for c in rows[0]]
            if "数据标准字段名" in header_cells and "来源库绑定字段" in header_cells:
                idx_map = {h: i for i, h in enumerate(header_cells)}
                items: List[Dict[str, Any]] = []
                for r in rows[1:]:
                    if not r or all(x is None or str(x).strip() == "" for x in r):
                        continue
                    std_tbl = str(r[idx_map.get("标准表名", 2)] or "").strip()
                    std_col = str(r[idx_map.get("数据标准字段名", 11)] or "").strip()
                    if not std_tbl or not std_col:
                        continue
                    items.append(
                        {
                            "schema_name": str(r[idx_map.get("标准模式名", 1)] or "public").strip(),
                            "table_name": std_tbl,
                            "source_schema": str(r[idx_map.get("来源库模式", 3)] or "").strip(),
                            "source_table": str(r[idx_map.get("来源库表名", 4)] or std_tbl).strip(),
                            "bound_source_col": str(r[idx_map.get("来源库绑定字段", 5)] or "").strip(),
                            "old_col_name": str(r[idx_map.get("清单原字段名", 8)] or "").strip(),
                            "old_col_type": str(r[idx_map.get("清单原类型", 9)] or "").strip(),
                            "old_col_comment": str(r[idx_map.get("清单原注释", 10)] or "").strip(),
                            "new_col_name": std_col,
                            "new_col_type": str(r[idx_map.get("数据标准类型", 12)] or "").strip(),
                            "new_col_comment": str(r[idx_map.get("数据标准注释", 13)] or "").strip(),
                            "target_schema": str(r[idx_map.get("目标库模式", 14)] or "").strip(),
                            "target_table": str(r[idx_map.get("目标库表名", 15)] or std_tbl).strip(),
                            "bound_target_col": str(r[idx_map.get("目标库绑定字段", 16)] or std_col).strip(),
                        }
                    )
                if items:
                    self.store.replace_cross_standards(items, filename, preserve_bindings=False)
                    self.store.add_log(
                        "SUCCESS",
                        "导入三方绑定Excel",
                        f"成功从 [{filename}] 导入 {len(items)} 条三方数标绑定记录。",
                    )
                    return self.get_overview_state()

        parsed_items = parse_excel_checklist(
            content=content,
            filename=filename,
            default_schema="public",
            default_table="",
        )
        self.store.replace_cross_standards(parsed_items, filename, preserve_bindings=True)
        # 同时同步至单库贯标工作台，实现双模式数据互通
        self.store.replace_all_items(parsed_items, filename, preserve_history=True)
        self.store.add_log(
            "SUCCESS",
            "上传数标清单",
            f"成功导入数据标准/落标清单 [{filename}] 共 {len(parsed_items)} 条规则，并已自动绑定同名表字段。",
        )
        return self.get_overview_state()

    def translate_table_ddl(
        self,
        *,
        source_role: str = "source",
        source_schema: str = "",
        source_table: str = "",
        target_dialect: str = "DWS",
        target_schema: str = "",
        target_table: str = "",
        storage_mode: str = "AUTO",
        apply_standard_mapping: bool = False,
        std_table_key: str = "",
        view_sql: str = "",
    ) -> Dict[str, Any]:
        """提取指定环境（来源库/目标库/中间数据标准/粘贴的视图SQL）的表结构，并翻译为目标方言（PGSQL / DWS / INCEPTOR）的可执行建表 DDL."""
        role = (source_role or "source").strip().lower()
        if role not in ("source", "target", "standard", "view_sql"):
            raise ValueError("source_role 必须为 source、target、standard 或 view_sql")

        tgt_dialect = (target_dialect or "DWS").strip().upper()
        if tgt_dialect not in ("PGSQL", "DWS", "INCEPTOR"):
            raise ValueError("target_dialect 必须为 PGSQL、DWS 或 INCEPTOR")

        conns = {
            "source": self.get_role_conn_config("source"),
            "target": self.get_role_conn_config("target"),
        }
        all_tables = self.store.list_cross_tables()
        t_bindings = {tb["std_table_key"]: tb for tb in all_tables}
        c_bindings = self.store.list_cross_columns()

        # 解析 std_table_key 及默认 schema/table
        resolved_key = (std_table_key or "").strip().lower()
        s_schema = (source_schema or "").strip()
        s_table = (source_table or "").strip()
        if "." in s_table:
            parts = s_table.split(".", 1)
            if not s_schema:
                s_schema = parts[0].strip()
            s_table = parts[1].strip()
        raw_view_sql = (view_sql or "").strip()

        if (role == "view_sql" or raw_view_sql) and raw_view_sql:
            probe_role = role if role in ("source", "target") else "source"
            live_meta = self.build_table_meta_from_view_sql(
                raw_view_sql,
                default_schema=s_schema or "public",
                default_table=s_table,
                probe_role=probe_role,
            )
            s_schema = live_meta.schema_name
            s_table = live_meta.table_name
            from_live_db = bool(conns.get(probe_role, {}).get("configured"))
            src_dialect = str(conns.get(probe_role, {}).get("db_type", "INCEPTOR") or "INCEPTOR").upper()
        else:
            if not resolved_key and s_table:
                if s_table.lower() in t_bindings:
                    resolved_key = s_table.lower()
                else:
                    for k, tb in t_bindings.items():
                        if role == "source" and (tb.get("source_table") or "").lower() == s_table.lower():
                            resolved_key = k
                            if not s_schema:
                                s_schema = tb.get("source_schema", "public")
                            break
                        if role == "target" and (tb.get("target_table") or "").lower() == s_table.lower():
                            resolved_key = k
                            if not s_schema:
                                s_schema = tb.get("target_schema", "public")
                            break
                        if (tb.get("std_table_name") or "").lower() == s_table.lower():
                            resolved_key = k
                            if not s_schema:
                                s_schema = tb.get("std_schema", "public")
                            break

            if resolved_key and resolved_key in t_bindings:
                tb_info = t_bindings[resolved_key]
                if not s_table:
                    if role == "source":
                        s_schema = s_schema or tb_info.get("source_schema") or tb_info.get("std_schema") or "public"
                        s_table = tb_info.get("source_table") or tb_info.get("std_table_name") or ""
                    elif role == "target":
                        s_schema = s_schema or tb_info.get("target_schema") or tb_info.get("std_schema") or "public"
                        s_table = tb_info.get("target_table") or tb_info.get("std_table_name") or ""
                    else:
                        s_schema = s_schema or tb_info.get("std_schema") or "public"
                        s_table = tb_info.get("std_table_name") or ""

            if not s_table:
                raise ValueError("请指定要导出或转换的来源表/视图名，或粘贴视图 SQL 语句")
            if not s_schema:
                if role in ("source", "target"):
                    s_schema = conns.get(role, {}).get("schema_name", "public") or "public"
                else:
                    s_schema = "public"

            from_live_db = False
            live_meta = None
            if role in ("source", "target"):
                src_dialect = str(conns.get(role, {}).get("db_type", "DWS") or "DWS").upper()
                if conns.get(role, {}).get("configured"):
                    try:
                        inspected = self.inspect_role_table_metadata(role, s_schema, s_table)
                        if inspected.exists and inspected.columns:
                            live_meta = inspected
                            from_live_db = True
                    except Exception:
                        live_meta = None
            else:
                src_dialect = "DWS"

        # 若未从实时库获取到元数据，则使用已绑定的数据标准/落标清单回退构建 TableMeta
        matched_std_cols = [
            c
            for c in c_bindings
            if (resolved_key and (c.get("std_table_key") or "").lower() == resolved_key.lower())
            or (not resolved_key and (c.get("std_table_name") or "").lower() == s_table.lower())
        ]

        if live_meta is None:
            if not matched_std_cols:
                raise ValueError(
                    f"未能从实时数据库读取表 [{s_schema}.{s_table}]，且当前未找到该表对应的数据标准清单字段。请先连接对应数据库或上传数标清单。"
                )
            fallback_cols: List[ColumnMeta] = []
            for idx, sc in enumerate(matched_std_cols, start=1):
                if role == "source":
                    col_nm = (
                        sc.get("bound_source_col")
                        or sc.get("orig_col_name")
                        or sc.get("std_col_name")
                        or f"col_{idx}"
                    )
                    col_tp = sc.get("orig_col_type") or sc.get("std_col_type") or "VARCHAR(255)"
                    col_cm = sc.get("orig_col_comment") or sc.get("std_col_comment") or ""
                elif role == "target":
                    col_nm = (
                        sc.get("bound_target_col")
                        or sc.get("std_col_name")
                        or sc.get("orig_col_name")
                        or f"col_{idx}"
                    )
                    col_tp = sc.get("std_col_type") or sc.get("orig_col_type") or "VARCHAR(255)"
                    col_cm = sc.get("std_col_comment") or sc.get("orig_col_comment") or ""
                else:
                    col_nm = sc.get("std_col_name") or sc.get("orig_col_name") or f"col_{idx}"
                    col_tp = sc.get("std_col_type") or sc.get("orig_col_type") or "VARCHAR(255)"
                    col_cm = sc.get("std_col_comment") or sc.get("orig_col_comment") or ""

                fallback_cols.append(
                    ColumnMeta(
                        ordinal_position=idx,
                        column_name=col_nm,
                        data_type=col_tp,
                        is_not_null=False,
                        column_default=None,
                        column_comment=col_cm,
                    )
                )
            live_meta = TableMeta(
                schema_name=s_schema,
                table_name=s_table,
                table_comment="",
                exists=True,
                columns=fallback_cols,
                distribute_keys=[fallback_cols[0].column_name] if fallback_cols else [],
            )

        # 构建原始方言参考 DDL
        source_ddl_preview = build_dialect_ddl(
            live_meta,
            source_dialect=src_dialect,
            target_dialect=src_dialect if src_dialect in ("PGSQL", "DWS", "INCEPTOR") else "DWS",
            target_schema=live_meta.schema_name,
            target_table=live_meta.table_name,
            storage_mode="AUTO",
        )
        raw_source_ddl = (getattr(live_meta, "raw_ddl", "") or "").strip()
        source_ddl_text = (
            _format_readable_sql_ddl(raw_source_ddl)
            if raw_source_ddl
            else source_ddl_preview["ddl"]
        )

        # 若勾选了 apply_standard_mapping，则套用中间数据标准的字段名、类型与注释
        working_columns: List[ColumnMeta] = []
        rename_map: Dict[str, str] = {}
        std_lookup_by_src: Dict[str, Dict[str, Any]] = {}
        for sc in matched_std_cols:
            for candidate_name in (
                sc.get("bound_source_col", ""),
                sc.get("orig_col_name", ""),
                sc.get("bound_target_col", ""),
                sc.get("std_col_name", ""),
            ):
                if candidate_name:
                    std_lookup_by_src[candidate_name.lower()] = sc

        for col in live_meta.columns:
            if apply_standard_mapping and std_lookup_by_src:
                matched_sc = std_lookup_by_src.get(col.column_name.lower())
                if matched_sc:
                    new_nm = matched_sc.get("std_col_name") or col.column_name
                    new_tp = matched_sc.get("std_col_type") or col.data_type
                    new_cm = matched_sc.get("std_col_comment") or col.column_comment
                    rename_map[col.column_name.lower()] = new_nm
                    working_columns.append(
                        ColumnMeta(
                            ordinal_position=col.ordinal_position,
                            column_name=new_nm,
                            data_type=new_tp,
                            is_not_null=col.is_not_null,
                            column_default=col.column_default,
                            column_comment=new_cm,
                            lineage_source=getattr(col, "lineage_source", ""),
                        )
                    )
                    continue
            working_columns.append(col)

        updated_dist_keys = [
            rename_map.get(k.lower(), k) for k in (live_meta.distribute_keys or [])
        ]
        updated_part_keys = [
            rename_map.get(k.lower(), k) for k in (live_meta.partition_keys or [])
        ]

        working_meta = TableMeta(
            schema_name=live_meta.schema_name,
            table_name=live_meta.table_name,
            table_comment=live_meta.table_comment,
            exists=True,
            orientation=live_meta.orientation,
            columns=working_columns,
            raw_ddl=getattr(live_meta, "raw_ddl", ""),
            with_clause=live_meta.with_clause,
            distribute_clause=live_meta.distribute_clause if not rename_map else "",
            distribute_keys=updated_dist_keys,
            partition_clause=live_meta.partition_clause if not rename_map else "",
            partition_keys=updated_part_keys,
            object_type=getattr(live_meta, "object_type", "TABLE") or "TABLE",
            view_definition=getattr(live_meta, "view_definition", "") or "",
            penetrated_base_tables=list(getattr(live_meta, "penetrated_base_tables", []) or []),
        )

        eff_target_schema = (target_schema or "").strip() or live_meta.schema_name or "public"
        eff_target_table = (target_table or "").strip() or live_meta.table_name

        translated = build_dialect_ddl(
            working_meta,
            source_dialect=src_dialect,
            target_dialect=tgt_dialect,
            target_schema=eff_target_schema,
            target_table=eff_target_table,
            storage_mode=storage_mode or "AUTO",
        )

        self.store.add_log(
            "INFO",
            "跨环境DDL翻译",
            f"已将 [{role}:{s_schema}.{s_table}] ({src_dialect}, 对象类型={working_meta.object_type}) 翻译为 {tgt_dialect} 方言建表语句 [{eff_target_schema}.{eff_target_table}]（共 {len(working_columns)} 个字段）。",
        )

        return {
            "source_role": role,
            "source_dialect": src_dialect,
            "source_schema": live_meta.schema_name,
            "source_table": live_meta.table_name,
            "source_object_type": working_meta.object_type,
            "penetrated_base_tables": working_meta.penetrated_base_tables,
            "source_ddl": source_ddl_text,
            "target_dialect": tgt_dialect,
            "target_schema": eff_target_schema,
            "target_table": eff_target_table,
            "target_ddl": translated["ddl"],
            "storage_mode": translated["storage_mode"],
            "apply_standard_mapping": bool(apply_standard_mapping),
            "from_live_db": from_live_db,
            "column_count": translated["column_count"],
            "column_mappings": translated["column_mappings"],
        }


