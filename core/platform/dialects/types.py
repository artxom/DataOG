"""跨数据库方言类型归一化与智能映射矩阵."""

from __future__ import annotations

import re
from typing import Tuple

SUPPORTED_DIALECTS = (
    "DWS",
    "PGSQL",
    "INCEPTOR",
    "MYSQL",
    "SQLITE",
    "DM8",
    "KINGBASE",
    "ORACLE",
    "DB2",
    "GBASE",
    "CUSTOM_JDBC",
)


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


def normalize_dws_type(raw_type: str) -> str:
    """将华为 DWS / PostgreSQL / Oracle 兼容模式的各种数据类型同义写法归一化."""
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


def normalize_cross_type(raw_type: str) -> str:
    """跨异构库 (PGSQL / DWS / Inceptor / MySQL / DM8 / Oracle / SQLite) 的通用数据类型归一化."""
    t = normalize_dws_type(raw_type)
    if not t:
        return ""
    t = re.sub(r"^varchar2(\(\d+\))?$", r"varchar\1", t)
    t = re.sub(r"^nvarchar2(\(\d+\))?$", r"varchar\1", t)
    t = re.sub(r"^number(\(\d+(?:,\d+)?\))?$", r"numeric\1", t)
    if t == "string":
        return "varchar"
    if t == "datetime":
        return "timestamp"
    if t == "double":
        return "float8"
    if t == "float":
        return "float4"
    return t


def is_cross_type_compatible(actual_type: str, std_type: str) -> Tuple[bool, bool]:
    """判断库内实际类型与数据标准类型是否一致或兼容.

    返回: (exact_match, base_compatible)
    """
    norm_actual = normalize_cross_type(actual_type)
    norm_std = normalize_cross_type(std_type)
    if not norm_std:
        return True, True
    if not norm_actual:
        return False, False
    if norm_actual == norm_std:
        return True, True
    base_actual = re.sub(r"\(.*\)$", "", norm_actual).strip()
    base_std = re.sub(r"\(.*\)$", "", norm_std).strip()
    if base_actual == base_std:
        return False, True
    if {base_actual, base_std} <= {"varchar", "text", "char"}:
        return False, True
    return False, False


def is_generic_or_unbounded_type(sql_type: str) -> bool:
    """判断某个字段类型是否为视图常见的无长度/泛化类型."""
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


def translate_column_type(
    raw_type: str, target_dialect: str, is_key_col: bool = False
) -> Tuple[str, str]:
    """根据目标数据库方言转换字段数据类型，返回 (目标类型声明, 转换规则说明)."""
    dialect = (target_dialect or "DWS").strip().upper()
    if dialect == "KINGBASE":
        dialect = "PGSQL"
    s = re.sub(r"\s+", " ", (raw_type or "").strip())
    if not s:
        s = "varchar(255)"
    low = s.lower()
    low_nospace = re.sub(r"\s*\(\s*", "(", low)
    low_nospace = re.sub(r"\s*,\s*", ",", low_nospace)
    low_nospace = re.sub(r"\s*\)\s*", ")", low_nospace)

    paren_match = re.search(r"\((\d+(?:,\d+)?)\)", low_nospace)
    paren_arg = f"({paren_match.group(1)})" if paren_match else ""
    base_name = re.sub(r"\(.*\)", "", low_nospace).strip()

    # 1. 华为云 GaussDB(DWS)
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

    # 2. PostgreSQL / KingbaseES
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

    # 3. 星环 Transwarp Inceptor / Hive
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

    # 4. MySQL / OceanBase / Doris / StarRocks / TiDB
    if dialect in ("MYSQL", "MARIADB", "OCEANBASE", "DORIS", "STARROCKS", "TIDB"):
        if base_name in ("json", "jsonb"):
            return ("JSON", "")
        if base_name in ("string", "text", "clob", "longvarchar"):
            return ("VARCHAR(255)" if is_key_col else "TEXT", "")
        if base_name in ("varchar", "character varying", "varchar2", "nvarchar", "nvarchar2"):
            return (f"VARCHAR{paren_arg}" if paren_arg else "VARCHAR(255)", "")
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
            return (f"DECIMAL{paren_arg}" if paren_arg else "DECIMAL(38,10)", "")
        if base_name in ("real", "float4", "float"):
            return ("FLOAT", "")
        if base_name in ("double", "double precision", "float8"):
            return ("DOUBLE", "")
        if "timestamp" in base_name or base_name in ("datetime", "smalldatetime"):
            return ("DATETIME", "")
        if "time" in base_name and "timestamp" not in base_name:
            return ("TIME", "")
        if base_name == "date":
            return ("DATE", "")
        if base_name in ("bool", "boolean"):
            return ("TINYINT(1)", "布尔类型规范化为 MySQL TINYINT(1)")
        if base_name in ("bytea", "binary", "varbinary", "blob", "raw"):
            return ("BLOB", "")
        return (s.upper(), "")

    # 5. 达梦 DM8 / Oracle
    if dialect in ("DM8", "ORACLE"):
        if base_name in ("string", "text", "clob", "longvarchar", "json", "jsonb"):
            return ("CLOB", f"原类型 {s} 映射为 {dialect} CLOB")
        if base_name in ("varchar", "character varying", "varchar2", "nvarchar", "nvarchar2"):
            v_kw = "VARCHAR2" if dialect == "ORACLE" else "VARCHAR"
            return (f"{v_kw}{paren_arg}" if paren_arg else f"{v_kw}(255)", "")
        if base_name in ("char", "character", "bpchar", "nchar"):
            return (f"CHAR{paren_arg}" if paren_arg else "CHAR(1)", "")
        if base_name in ("tinyint", "smallint", "int", "integer", "int1", "int2", "int4", "serial"):
            return ("NUMBER(10)" if dialect == "ORACLE" else "INT", "")
        if base_name in ("bigint", "int8", "bigserial"):
            return ("NUMBER(19)" if dialect == "ORACLE" else "BIGINT", "")
        if base_name in ("numeric", "decimal", "number", "dec"):
            n_kw = "NUMBER" if dialect == "ORACLE" else "DECIMAL"
            return (f"{n_kw}{paren_arg}" if paren_arg else f"{n_kw}(38,10)", "")
        if base_name in ("real", "float4", "float", "double", "double precision", "float8"):
            return ("BINARY_DOUBLE" if dialect == "ORACLE" else "DOUBLE", "")
        if "timestamp" in base_name or base_name in ("datetime", "smalldatetime"):
            return ("TIMESTAMP", "")
        if base_name == "date":
            return ("DATE", "")
        if base_name in ("bool", "boolean"):
            return ("NUMBER(1)" if dialect == "ORACLE" else "BIT", "")
        if base_name in ("bytea", "binary", "varbinary", "blob", "raw"):
            return ("BLOB", "")
        return (s.upper(), "")

    # 6. SQLite
    if dialect == "SQLITE":
        if base_name in ("tinyint", "smallint", "int", "integer", "bigint", "int2", "int4", "int8", "serial", "bigserial", "bool", "boolean"):
            return ("INTEGER", "")
        if base_name in ("real", "float", "float4", "float8", "double", "double precision"):
            return ("REAL", "")
        if base_name in ("bytea", "binary", "varbinary", "blob", "raw"):
            return ("BLOB", "")
        if base_name in ("numeric", "decimal", "number", "dec"):
            return (f"NUMERIC{paren_arg}" if paren_arg else "NUMERIC", "")
        return ("TEXT", "")

    return (s.upper(), "")
