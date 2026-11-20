"""跨数据库方言类型归一化与智能映射矩阵."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from core.platform.dialects.identifiers import clean_identifier_input

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
    t = re.sub(r"^nvarchar\b", "varchar", t)
    t = re.sub(r"^character\b", "char", t)
    t = re.sub(r"^bpchar\b", "char", t)
    t = re.sub(r"^nchar\b", "char", t)
    t = re.sub(r"^decimal\b", "numeric", t)
    t = re.sub(r"^dec\b", "numeric", t)
    t = re.sub(r"^number\b", "numeric", t)
    t = re.sub(r"^int4\b", "integer", t)
    t = re.sub(r"^int\b", "integer", t)
    t = re.sub(r"^int8\b", "bigint", t)
    t = re.sub(r"^int2\b", "smallint", t)
    t = re.sub(r"^int1\b", "tinyint", t)
    t = re.sub(r"^float8\b", "double precision", t)
    t = re.sub(r"^double$", "double precision", t)
    t = re.sub(r"^float4\b", "real", t)
    t = re.sub(r"^bool\b", "boolean", t)
    t = re.sub(r"^datetime$", "timestamp", t)
    t = re.sub(r"^string$", "varchar", t)
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
    t = re.sub(r"^nvarchar(\(\d+\))?$", r"varchar\1", t)
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


_MYSQL_FAMILY_DIALECTS = frozenset(
    {"MYSQL", "MARIADB", "OCEANBASE", "DORIS", "STARROCKS", "TIDB", "GOLDENDB"}
)

_INT_SPEC_TABLE: Dict[str, Tuple[int, int, int]] = {
    "tinyint": (8, 2, 3),
    "int1": (8, 2, 3),
    "byteint": (8, 2, 3),
    "smallint": (16, 4, 5),
    "int2": (16, 4, 5),
    "mediumint": (24, 6, 7),
    "int": (32, 9, 10),
    "integer": (32, 9, 10),
    "int4": (32, 9, 10),
    "serial": (32, 9, 10),
    "binary_integer": (32, 9, 10),
    "pls_integer": (32, 9, 10),
    "bigint": (64, 18, 19),
    "int8": (64, 18, 19),
    "bigserial": (64, 18, 19),
    "largeint": (128, 38, 39),
    "hugeint": (128, 38, 39),
    "int16": (128, 38, 39),
}


def parse_sql_type_spec(raw_type: str, db_type: str = "") -> Dict[str, Any]:
    """解析 SQL 物理数据类型为结构化语义规格（族/子类型/位长/精度/小数位/单位）."""
    cleaned = clean_sql_type_input(raw_type)
    low = cleaned.lower()
    if not low or low in ("unknown", "any", "void"):
        return {
            "empty": True,
            "raw": cleaned,
            "base": "",
            "family": "UNKNOWN",
            "sub": "",
            "family_cn": "未知",
            "unbounded": False,
            "unsigned": False,
            "args": [],
            "unit": "",
        }

    unsigned = bool(re.search(r"\bunsigned\b", low))
    unit = ""
    args: List[int] = []

    m_paren = re.search(r"\(([^)]*)\)", low)
    if m_paren:
        inner = m_paren.group(1).strip()
        if re.search(r"\bbyte\b", inner):
            unit = "BYTE"
        elif re.search(r"\bchar\b", inner):
            unit = "CHAR"
        for part in inner.split(","):
            tok = re.sub(r"\b(?:byte|char)\b", "", part).strip()
            if tok == "*":
                args.append(38)
            elif re.fullmatch(r"[+-]?\d+", tok):
                args.append(int(tok))

    base = re.sub(r"\([^)]*\)", "", low)
    base = re.sub(r"\b(?:unsigned|signed|zerofill)\b", "", base)
    base = " ".join(base.split()).strip()
    if not unit:
        if re.search(r"\bbyte\b", base):
            unit = "BYTE"
            base = re.sub(r"\bbyte\b", "", base).strip()
        elif re.search(r"\bchar\b", base) and base not in ("char", "nchar", "bpchar"):
            unit = "CHAR"
            base = re.sub(r"\bchar\b", "", base).strip()

    arg1 = args[0] if len(args) >= 1 else None
    arg2 = args[1] if len(args) >= 2 else None
    dt = (db_type or "").strip().upper()

    spec: Dict[str, Any] = {
        "empty": False,
        "raw": cleaned,
        "base": base,
        "family": "UNKNOWN",
        "sub": "",
        "family_cn": "未知",
        "unbounded": False,
        "unsigned": unsigned,
        "args": args,
        "unit": unit,
        "char_len": None,
        "byte_len": None,
        "bits": None,
        "precision": None,
        "scale": None,
        "int_digits": None,
        "safe_dec_digits": None,
        "max_dec_digits": None,
        "frac": None,
    }

    # 1. 字符族
    if base in ("varchar", "varchar2", "nvarchar", "nvarchar2", "character varying"):
        spec["family"] = "STRING"
        spec["sub"] = "VARCHAR"
        spec["family_cn"] = "字符"
        if arg1 is not None:
            spec["char_len"] = max(0, arg1)
            spec["unbounded"] = False
        else:
            spec["unbounded"] = True
        return spec
    if base in ("char", "nchar", "character", "bpchar"):
        spec["family"] = "STRING"
        spec["sub"] = "CHAR"
        spec["family_cn"] = "字符"
        spec["char_len"] = max(0, arg1) if arg1 is not None else 1
        spec["unbounded"] = False
        return spec
    if base == "tinytext":
        spec["family"] = "STRING"
        spec["sub"] = "TEXT"
        spec["family_cn"] = "字符"
        spec["char_len"] = 255
        spec["unbounded"] = False
        return spec
    if base in ("text", "mediumtext", "longtext", "string", "clob", "nclob", "long", "longvarchar", "ntext"):
        spec["family"] = "STRING"
        spec["sub"] = "TEXT"
        spec["family_cn"] = "字符"
        spec["unbounded"] = True
        return spec

    # 2. 布尔族（含 bit(1) / bit）
    if base in ("bool", "boolean") or (base == "bit" and (arg1 is None or arg1 == 1)):
        spec["family"] = "BOOL"
        spec["sub"] = "BOOL"
        spec["family_cn"] = "布尔"
        spec["bits"] = 1
        return spec

    # 3. 数值族
    if base in _INT_SPEC_TABLE:
        bits, safe_d, max_d = _INT_SPEC_TABLE[base]
        if unsigned and base in ("mediumint", "bigint", "int8", "bigserial"):
            max_d += 1
        spec["family"] = "NUMERIC"
        spec["sub"] = "INT"
        spec["family_cn"] = "整型"
        spec["bits"] = bits
        spec["safe_dec_digits"] = safe_d
        spec["max_dec_digits"] = max_d
        return spec

    if base in ("decimal", "numeric", "number", "dec", "fixed"):
        spec["family"] = "NUMERIC"
        spec["sub"] = "DECIMAL"
        spec["family_cn"] = "定点数值"
        if arg1 is not None:
            p = max(1, arg1)
            raw_s = arg2 if arg2 is not None else 0
            if raw_s < 0:
                int_d = p + abs(raw_s)
                sc = 0
                p = int_d
            else:
                sc = raw_s
                int_d = max(0, p - sc)
            spec["precision"] = p
            spec["scale"] = sc
            spec["int_digits"] = int_d
            spec["unbounded"] = False
        elif dt in _MYSQL_FAMILY_DIALECTS:
            spec["precision"] = 10
            spec["scale"] = 0
            spec["int_digits"] = 10
            spec["unbounded"] = False
        else:
            spec["unbounded"] = True
        return spec

    if base in ("money", "smallmoney"):
        spec["family"] = "NUMERIC"
        spec["sub"] = "DECIMAL"
        spec["family_cn"] = "定点数值"
        p = 19 if base == "money" else 10
        spec["precision"] = p
        spec["scale"] = 4
        spec["int_digits"] = p - 4
        spec["unbounded"] = False
        return spec

    if base in ("real", "float4", "binary_float"):
        spec["family"] = "NUMERIC"
        spec["sub"] = "FLOAT"
        spec["family_cn"] = "浮点数值"
        spec["bits"] = 32
        return spec
    if base in ("double", "double precision", "float8", "binary_double"):
        spec["family"] = "NUMERIC"
        spec["sub"] = "FLOAT"
        spec["family_cn"] = "浮点数值"
        spec["bits"] = 64
        return spec
    if base == "float":
        spec["family"] = "NUMERIC"
        spec["sub"] = "FLOAT"
        spec["family_cn"] = "浮点数值"
        spec["bits"] = 64 if (arg1 is not None and arg1 > 24) else 32
        return spec

    # 4. 时间日期族
    if base == "date":
        spec["family"] = "TEMPORAL"
        spec["sub"] = "DATE"
        spec["family_cn"] = "日期"
        return spec
    if base in ("time", "timetz", "time without time zone", "time with time zone"):
        spec["family"] = "TEMPORAL"
        spec["sub"] = "TIME"
        spec["family_cn"] = "时间"
        spec["frac"] = arg1
        return spec
    if base in (
        "timestamp",
        "timestamptz",
        "timestamp without time zone",
        "timestamp with time zone",
        "datetime",
        "smalldatetime",
    ):
        spec["family"] = "TEMPORAL"
        spec["sub"] = "TIMESTAMP"
        spec["family_cn"] = "时间戳"
        spec["frac"] = 0 if base == "smalldatetime" else arg1
        return spec
    if base == "year":
        spec["family"] = "TEMPORAL"
        spec["sub"] = "YEAR"
        spec["family_cn"] = "年份"
        return spec

    # 5. 二进制族
    if base in ("binary", "varbinary", "raw"):
        spec["family"] = "BINARY"
        spec["sub"] = "VARBINARY"
        spec["family_cn"] = "二进制"
        if arg1 is not None:
            spec["byte_len"] = max(0, arg1)
            spec["unbounded"] = False
        else:
            spec["unbounded"] = True
        return spec
    if base == "bit" and arg1 is not None and arg1 > 1:
        spec["family"] = "BINARY"
        spec["sub"] = "VARBINARY"
        spec["family_cn"] = "二进制"
        spec["byte_len"] = (arg1 + 7) // 8
        spec["unbounded"] = False
        return spec
    if base == "tinyblob":
        spec["family"] = "BINARY"
        spec["sub"] = "BLOB"
        spec["family_cn"] = "二进制"
        spec["byte_len"] = 255
        spec["unbounded"] = False
        return spec
    if base in ("blob", "mediumblob", "longblob", "bytea"):
        spec["family"] = "BINARY"
        spec["sub"] = "BLOB"
        spec["family_cn"] = "二进制"
        spec["unbounded"] = True
        return spec

    # 6. JSON 族
    if base in ("json", "jsonb"):
        spec["family"] = "JSON"
        spec["sub"] = "JSON"
        spec["family_cn"] = "JSON"
        spec["unbounded"] = True
        return spec

    return spec


def _compat_result(
    *,
    applicable: bool,
    cap_status: str,
    cap_label: str,
    cap_short: str,
    cap_detail: str,
    conv_status: str,
    conv_label: str,
    conv_short: str,
    conv_detail: str,
) -> Dict[str, Any]:
    cap_ok = True if cap_status == "CONTAINED" else (False if cap_status == "INSUFFICIENT" else None)
    return {
        "applicable": applicable,
        "capacity_status": cap_status,
        "capacity_ok": cap_ok,
        "capacity_label": cap_label,
        "capacity_short": cap_short,
        "capacity_detail": cap_detail,
        "has_type_conversion": conv_status in ("SAFE_CAST", "CAST_REQUIRED"),
        "type_conv_status": conv_status,
        "type_conv_label": conv_label,
        "type_conv_short": conv_short,
        "type_conv_detail": conv_detail,
    }


def _string_capacity(src: Dict[str, Any], tgt: Dict[str, Any]) -> Tuple[str, str, str, str]:
    if tgt["unbounded"]:
        return ("CONTAINED", "可包住", "可包住", f"目标类型 {tgt['raw']} 无固定位长限制，可包住来源 {src['raw']}")
    t_len = int(tgt["char_len"] or 0)
    if src["unbounded"]:
        return (
            "INSUFFICIENT",
            f"无法包住 (无界→{t_len})",
            f"位长不足 无界→{t_len}",
            f"来源无界字符 ({src['raw']}) 转目标定长 ({tgt['raw']}) 可能截断",
        )
    s_len = int(src["char_len"] or 0)
    if t_len < s_len:
        return (
            "INSUFFICIENT",
            f"无法包住 (位长 {s_len}→{t_len})",
            f"位长不足 {s_len}→{t_len}",
            f"目标位长 {t_len} 小于来源位长 {s_len}",
        )
    if src["unit"] == "CHAR" and tgt["unit"] == "BYTE" and t_len < s_len * 3:
        return (
            "RISK",
            f"需核验 ({s_len} CHAR→{t_len} BYTE)",
            "需核验 CHAR→BYTE",
            f"来源为字符长度 {s_len} CHAR，目标为字节长度 {t_len} BYTE（多字节中文约需 3×{s_len} 字节）",
        )
    detail = f"双侧位长一致 ({s_len})" if t_len == s_len else f"目标位长 {t_len} 大于来源 {s_len}"
    return ("CONTAINED", "可包住", "可包住", detail)


def _decimal_capacity(src: Dict[str, Any], tgt: Dict[str, Any]) -> Tuple[str, str, str, str]:
    if tgt["unbounded"]:
        return ("CONTAINED", "可包住", "可包住", f"目标 {tgt['raw']} 为无界定点数值，可包住来源 {src['raw']}")
    if src["unbounded"]:
        return (
            "INSUFFICIENT",
            f"无法包住 (无界→{tgt['precision']},{tgt['scale']})",
            "位长/精度不足",
            f"来源 {src['raw']} 未限定精度，转入定长 {tgt['raw']} 存在溢出或截断风险",
        )
    s_i, s_s = int(src["int_digits"] or 0), int(src["scale"] or 0)
    t_i, t_s = int(tgt["int_digits"] or 0), int(tgt["scale"] or 0)
    int_short = t_i < s_i
    scale_short = t_s < s_s
    if int_short and scale_short:
        return (
            "INSUFFICIENT",
            f"无法包住 (整数位 {s_i}→{t_i}位, 小数 {s_s}→{t_s}位)",
            "位长/精度不足",
            f"目标整数位 ({t_i}位) 与小数精度 ({t_s}位) 均小于来源 ({s_i}位, {s_s}位)",
        )
    if int_short:
        return (
            "INSUFFICIENT",
            f"无法包住 (整数位 {s_i}→{t_i}位)",
            f"整数位不足 {s_i}→{t_i}",
            f"目标整数位 {t_i} 位小于来源 {s_i} 位（总长-小数位: {tgt['precision']}-{t_s} < {src['precision']}-{s_s}）",
        )
    if scale_short:
        return (
            "INSUFFICIENT",
            f"无法包住 (小数精度 {s_s}→{t_s}位)",
            f"精度不足 {s_s}→{t_s}",
            f"目标小数精度 {t_s} 位小于来源 {s_s} 位，存在四舍五入截断",
        )
    return (
        "CONTAINED",
        "可包住",
        "可包住",
        f"目标整数位 {t_i} ≥ {s_i} 且小数位 {t_s} ≥ {s_s}",
    )


def analyze_cross_column_compatibility(
    source_type: str,
    target_type: str,
    source_db_type: str = "",
    target_db_type: str = "",
) -> Dict[str, Any]:
    """判定目标库物理字段长度/精度是否包住来源库字段，以及是否涉及类型转换."""
    src = parse_sql_type_spec(source_type, db_type=source_db_type)
    tgt = parse_sql_type_spec(target_type, db_type=target_db_type)
    if src["empty"] or tgt["empty"]:
        return _compat_result(
            applicable=False,
            cap_status="N/A",
            cap_label="-",
            cap_short="-",
            cap_detail="",
            conv_status="N/A",
            conv_label="-",
            conv_short="-",
            conv_detail="",
        )

    sf, tf = src["family"], tgt["family"]
    ss, ts = src["sub"], tgt["sub"]

    # 未知扩展类型兜底
    if sf == "UNKNOWN" or tf == "UNKNOWN":
        if src["base"] == tgt["base"] and src["args"] == tgt["args"]:
            return _compat_result(
                applicable=True,
                cap_status="CONTAINED",
                cap_label="可包住",
                cap_short="可包住",
                cap_detail=f"类型定义一致 ({src['raw']})",
                conv_status="NONE",
                conv_label="否",
                conv_short="无转换",
                conv_detail="同类型无需转换",
            )
        if src["base"] == tgt["base"]:
            return _compat_result(
                applicable=True,
                cap_status="RISK",
                cap_label="需核验 (参数差异)",
                cap_short="需核验",
                cap_detail=f"{src['raw']} → {tgt['raw']}",
                conv_status="NONE",
                conv_label="否",
                conv_short="无转换",
                conv_detail="同类型参数差异，无需类型转换",
            )
        return _compat_result(
            applicable=True,
            cap_status="RISK",
            cap_label=f"需核验 ({src['raw']}→{tgt['raw']})",
            cap_short="需核验",
            cap_detail=f"异构类型映射: {src['raw']} → {tgt['raw']}",
            conv_status="CAST_REQUIRED",
            conv_label=f"是 ({src['raw']}→{tgt['raw']})",
            conv_short=f"转: {src['raw']}→{tgt['raw']}",
            conv_detail=f"需要从 {src['raw']} 转换为 {tgt['raw']}",
        )

    # 1. 字符 ↔ 字符
    if sf == "STRING" and tf == "STRING":
        cs, cl, csh, cd = _string_capacity(src, tgt)
        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="NONE",
            conv_label="否",
            conv_short="无转换",
            conv_detail="同属字符类型，无需类型转换",
        )

    # 2. 定点 ↔ 定点
    if sf == "NUMERIC" and tf == "NUMERIC" and ss == "DECIMAL" and ts == "DECIMAL":
        cs, cl, csh, cd = _decimal_capacity(src, tgt)
        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="NONE",
            conv_label="否",
            conv_short="无转换",
            conv_detail="同属定点数值类型，无需类型转换",
        )

    # 3. 整型 ↔ 整型
    if sf == "NUMERIC" and tf == "NUMERIC" and ss == "INT" and ts == "INT":
        s_bits, t_bits = int(src["bits"] or 32), int(tgt["bits"] or 32)
        if t_bits > s_bits:
            cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_bits} 位整型可包住来源 {s_bits} 位整型")
        elif t_bits == s_bits:
            if src["unsigned"] and not tgt["unsigned"]:
                cs, cl, csh, cd = (
                    "INSUFFICIENT",
                    f"无法包住 (无符号{s_bits}位→有符号{t_bits}位)",
                    "无符号溢出风险",
                    f"无符号 {s_bits} 位上限超出有符号 {t_bits} 位整型正数上限",
                )
            else:
                cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"同为 {t_bits} 位整型，容量一致")
        else:
            cs, cl, csh, cd = (
                "INSUFFICIENT",
                f"无法包住 ({s_bits}位→{t_bits}位整型)",
                f"位宽不足 {s_bits}→{t_bits}位",
                f"目标 {t_bits} 位整型无法包住来源 {s_bits} 位整型",
            )
        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="NONE",
            conv_label="否",
            conv_short="无转换",
            conv_detail="同属整型（已忽略显示宽度差异），无需类型转换",
        )

    # 4. 整型 → 定点
    if sf == "NUMERIC" and tf == "NUMERIC" and ss == "INT" and ts == "DECIMAL":
        s_max = int(src["max_dec_digits"] or 10)
        if tgt["unbounded"] or int(tgt["int_digits"] or 0) >= s_max:
            cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标定点整数位可完全容纳 {src['bits']} 位整型（最大 {s_max} 位）")
        else:
            t_i = int(tgt["int_digits"] or 0)
            cs, cl, csh, cd = (
                "INSUFFICIENT",
                f"无法包住 ({src['bits']}位整型→整数位{t_i}位)",
                f"整数位不足 {s_max}→{t_i}",
                f"来源 {src['bits']} 位整型最大需 {s_max} 位整数，目标仅支持 {t_i} 位整数",
            )
        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="SAFE_CAST",
            conv_label="是 (兼容: 整型→定点)",
            conv_short="兼容: 整型→定点",
            conv_detail="整型升格为定点数值，属于同域兼容转换",
        )

    # 5. 定点 → 整型
    if sf == "NUMERIC" and tf == "NUMERIC" and ss == "DECIMAL" and ts == "INT":
        if src["unbounded"] or int(src["scale"] or 0) > 0:
            reason = "来源无界定点数值可能带小数" if src["unbounded"] else f"来源含 {src['scale']} 位小数，转整型将丢失小数精度"
            return _compat_result(
                applicable=True,
                cap_status="INSUFFICIENT",
                cap_label="无法包住 (小数精度丢失)",
                cap_short="小数精度丢失",
                cap_detail=reason,
                conv_status="CAST_REQUIRED",
                conv_label="是 (定点→整型)",
                conv_short="转: 定点→整型",
                conv_detail="定点数值转整型将截断小数，涉及显式类型转换",
            )
        s_i = int(src["int_digits"] or 0)
        t_safe = int(tgt["safe_dec_digits"] or 9)
        t_max = int(tgt["max_dec_digits"] or 10)
        if s_i <= t_safe:
            cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"来源 {s_i} 位零小数定点可安全落入 {tgt['bits']} 位整型")
        elif s_i == t_max:
            cs, cl, csh, cd = (
                "RISK",
                f"需核验 ({s_i}位→{tgt['bits']}位整型极值)",
                "极值待核验",
                f"来源 {s_i} 位十进制整数可能超过 {tgt['bits']} 位二进制整型极值上限",
            )
        else:
            cs, cl, csh, cd = (
                "INSUFFICIENT",
                f"无法包住 (整数位 {s_i}→{tgt['bits']}位整型)",
                f"位宽不足 {s_i}位",
                f"来源 {s_i} 位整数超出 {tgt['bits']} 位整型最大容限 ({t_max} 位)",
            )
        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="SAFE_CAST",
            conv_label="是 (兼容: 零小数→整型)",
            conv_short="兼容: 零小数→整型",
            conv_detail="零小数定点数值与整型属于同域兼容转换",
        )

    # 6. 浮点内部及数值 ↔ 浮点
    if sf == "NUMERIC" and tf == "NUMERIC" and (ss == "FLOAT" or ts == "FLOAT"):
        if ss == "FLOAT" and ts == "FLOAT":
            s_b, t_b = int(src["bits"] or 32), int(tgt["bits"] or 32)
            if t_b >= s_b:
                cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_b} 位浮点可包住来源 {s_b} 位浮点")
            else:
                cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 ({s_b}位→{t_b}位浮点)", "浮点精度不足", f"双精度 {s_b} 位浮点降至 {t_b} 位存在精度损失")
            return _compat_result(
                applicable=True,
                cap_status=cs,
                cap_label=cl,
                cap_short=csh,
                cap_detail=cd,
                conv_status="NONE",
                conv_label="否",
                conv_short="无转换",
                conv_detail="同属浮点类型，无需类型转换",
            )
        if ss == "INT" and ts == "FLOAT":
            s_b, t_b = int(src["bits"] or 32), int(tgt["bits"] or 32)
            if (s_b <= 16 and t_b >= 32) or (s_b <= 32 and t_b >= 64):
                cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"{t_b} 位浮点尾数位可精确表示 {s_b} 位整型")
            else:
                cs, cl, csh, cd = ("RISK", f"需核验 ({s_b}位整型→{t_b}位浮点精度)", "浮点有效位风险", f"{s_b} 位大整数转 {t_b} 位浮点可能发生有效位舍入")
            return _compat_result(
                applicable=True,
                cap_status=cs,
                cap_label=cl,
                cap_short=csh,
                cap_detail=cd,
                conv_status="SAFE_CAST",
                conv_label="是 (兼容: 整型→浮点)",
                conv_short="兼容: 整型→浮点",
                conv_detail="整型转浮点属于数值域转换",
            )
        if ss == "DECIMAL" and ts == "FLOAT":
            t_b = int(tgt["bits"] or 32)
            max_sig = 15 if t_b >= 64 else 7
            if src["unbounded"] or int(src["precision"] or 0) > max_sig:
                cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (定点精确位>{max_sig}→{t_b}位浮点)", "浮点有效位不足", f"超过 {t_b} 位浮点有效十进制位上限 ({max_sig} 位)")
            else:
                cs, cl, csh, cd = ("RISK", f"需核验 (十进制→二进制浮点舍入)", "浮点舍入风险", "定点十进制转二进制浮点可能产生微小尾数误差")
            return _compat_result(
                applicable=True,
                cap_status=cs,
                cap_label=cl,
                cap_short=csh,
                cap_detail=cd,
                conv_status="CAST_REQUIRED",
                conv_label="是 (定点→浮点)",
                conv_short="转: 定点→浮点",
                conv_detail="精确十进制转二进制浮点涉及类型转换",
            )
        if ss == "FLOAT" and ts == "INT":
            return _compat_result(
                applicable=True,
                cap_status="INSUFFICIENT",
                cap_label="无法包住 (浮点小数精度丢失)",
                cap_short="小数精度丢失",
                cap_detail="浮点类型转整型会截断小数部分且可能溢出",
                conv_status="CAST_REQUIRED",
                conv_label="是 (浮点→整型)",
                conv_short="转: 浮点→整型",
                conv_detail="需要从浮点数值显式转换为整型",
            )
        if ss == "FLOAT" and ts == "DECIMAL":
            if tgt["unbounded"]:
                cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标无界 {tgt['raw']} 可容纳浮点值")
            elif int(tgt["int_digits"] or 0) >= 15 and int(tgt["scale"] or 0) >= 4:
                cs, cl, csh, cd = ("RISK", "需核验 (浮点→定点值域)", "浮点范围核验", f"需确认浮点值域及小数位未超出 {tgt['raw']}")
            else:
                cs, cl, csh, cd = (
                    "INSUFFICIENT",
                    f"无法包住 (浮点→{tgt['raw']})",
                    "定点范围可能不足",
                    f"目标定长 {tgt['raw']} 可能无法包住浮点极值或尾数小数",
                )
            return _compat_result(
                applicable=True,
                cap_status=cs,
                cap_label=cl,
                cap_short=csh,
                cap_detail=cd,
                conv_status="CAST_REQUIRED",
                conv_label="是 (浮点→定点)",
                conv_short="转: 浮点→定点",
                conv_detail="二进制浮点转定点十进制涉及类型转换",
            )

    # 7. 时间日期内部
    if sf == "TEMPORAL" and tf == "TEMPORAL":
        if ss == ts:
            s_f, t_f = src["frac"], tgt["frac"]
            if s_f is not None and t_f is not None and t_f < s_f:
                cs, cl, csh, cd = (
                    "INSUFFICIENT",
                    f"无法包住 (秒精度 {s_f}→{t_f}位)",
                    f"精度不足 {s_f}→{t_f}",
                    f"目标秒小数精度 ({t_f}) 小于来源 ({s_f})",
                )
            else:
                cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"同属{src['family_cn']}类型，容量可覆盖")
            return _compat_result(
                applicable=True,
                cap_status=cs,
                cap_label=cl,
                cap_short=csh,
                cap_detail=cd,
                conv_status="NONE",
                conv_label="否",
                conv_short="无转换",
                conv_detail=f"同属{src['family_cn']}类型，无需类型转换",
            )
        if ss == "DATE" and ts == "TIMESTAMP":
            return _compat_result(
                applicable=True,
                cap_status="CONTAINED",
                cap_label="可包住",
                cap_short="可包住",
                cap_detail="TIMESTAMP 可完整包含 DATE 日期值",
                conv_status="SAFE_CAST",
                conv_label="是 (兼容: 日期→时间戳)",
                conv_short="兼容: 日期→时间戳",
                conv_detail="日期升格为时间戳，补零时分秒，属于兼容转换",
            )
        if ss == "TIMESTAMP" and ts == "DATE":
            return _compat_result(
                applicable=True,
                cap_status="INSUFFICIENT",
                cap_label="无法包住 (时分秒精度丢失)",
                cap_short="时分秒丢失",
                cap_detail="TIMESTAMP 降级为 DATE 将丢失时分秒及毫秒精度",
                conv_status="CAST_REQUIRED",
                conv_label="是 (时间戳→日期)",
                conv_short="转: 时间戳→日期",
                conv_detail="需要从时间戳显式转换为日期",
            )
        return _compat_result(
            applicable=True,
            cap_status="INSUFFICIENT",
            cap_label=f"无法包住 ({src['family_cn']}→{tgt['family_cn']})",
            cap_short="时间分量丢失",
            cap_detail=f"{src['raw']} 转为 {tgt['raw']} 无法完整保留原时间信息",
            conv_status="CAST_REQUIRED",
            conv_label=f"是 ({src['family_cn']}→{tgt['family_cn']})",
            conv_short=f"转: {src['family_cn']}→{tgt['family_cn']}",
            conv_detail=f"涉及时间子类型转换 ({src['raw']} → {tgt['raw']})",
        )

    # 8. 布尔内部及与 1 位数值兼容
    if sf == "BOOL" and tf == "BOOL":
        return _compat_result(
            applicable=True,
            cap_status="CONTAINED",
            cap_label="可包住",
            cap_short="可包住",
            cap_detail="同属布尔类型",
            conv_status="NONE",
            conv_label="否",
            conv_short="无转换",
            conv_detail="同属布尔类型，无需类型转换",
        )
    if sf == "BOOL" and tf == "NUMERIC" and ts in ("INT", "DECIMAL"):
        if ts == "DECIMAL" and not tgt["unbounded"] and int(tgt["int_digits"] or 0) < 1:
            return _compat_result(
                applicable=True,
                cap_status="INSUFFICIENT",
                cap_label="无法包住 (整数位不足1位)",
                cap_short="整数位不足",
                cap_detail=f"目标 {tgt['raw']} 无整数位存储布尔标志 0/1",
                conv_status="CAST_REQUIRED",
                conv_label=f"是 (布尔→{tgt['family_cn']})",
                conv_short=f"转: 布尔→{tgt['family_cn']}",
                conv_detail=f"布尔值需转换为 {tgt['raw']}",
            )
        return _compat_result(
            applicable=True,
            cap_status="CONTAINED",
            cap_label="可包住",
            cap_short="可包住",
            cap_detail=f"目标 {tgt['raw']} 可容纳布尔 0/1 标志",
            conv_status="SAFE_CAST",
            conv_label=f"是 (兼容: 布尔→{tgt['family_cn']})",
            conv_short=f"兼容: 布尔→{tgt['family_cn']}",
            conv_detail=f"布尔值与数值 0/1 兼容转换",
        )
    if sf == "NUMERIC" and tf == "BOOL":
        is_one_bit_flag = (
            (ss == "INT" and src["args"] and src["args"][0] == 1)
            or (ss == "DECIMAL" and src["precision"] == 1 and src["scale"] == 0)
        )
        if is_one_bit_flag:
            return _compat_result(
                applicable=True,
                cap_status="CONTAINED",
                cap_label="可包住",
                cap_short="可包住",
                cap_detail=f"1 位数值标志 ({src['raw']}) 可映射为布尔值",
                conv_status="SAFE_CAST",
                conv_label="是 (兼容: 1位标志→布尔)",
                conv_short="兼容: 标志→布尔",
                conv_detail="1 位数值标志与布尔类型兼容转换",
            )
        return _compat_result(
            applicable=True,
            cap_status="INSUFFICIENT",
            cap_label="无法包住 (多值数值→布尔)",
            cap_short="值域缩窄",
            cap_detail=f"{src['raw']} 可能包含非 0/1 数值，转布尔会缩窄值域",
            conv_status="CAST_REQUIRED",
            conv_label=f"是 ({src['family_cn']}→布尔)",
            conv_short=f"转: {src['family_cn']}→布尔",
            conv_detail=f"需要从 {src['raw']} 显式转换为布尔类型",
        )

    # 9. 二进制 ↔ 二进制
    if sf == "BINARY" and tf == "BINARY":
        if tgt["unbounded"]:
            cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", "目标无界二进制可包住来源")
        elif src["unbounded"]:
            t_b = int(tgt["byte_len"] or 0)
            cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (无界→{t_b}字节)", f"位长不足 无界→{t_b}", "来源无界二进制转定长可能截断")
        else:
            s_b, t_b = int(src["byte_len"] or 0), int(tgt["byte_len"] or 0)
            if t_b >= s_b:
                cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_b} 字节 ≥ 来源 {s_b} 字节")
            else:
                cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (位长 {s_b}→{t_b}字节)", f"位长不足 {s_b}→{t_b}", f"目标字节长 {t_b} 小于来源 {s_b}")
        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="NONE",
            conv_label="否",
            conv_short="无转换",
            conv_detail="同属二进制类型，无需类型转换",
        )

    # 10. JSON ↔ JSON
    if sf == "JSON" and tf == "JSON":
        return _compat_result(
            applicable=True,
            cap_status="CONTAINED",
            cap_label="可包住",
            cap_short="可包住",
            cap_detail="同属 JSON 类型",
            conv_status="NONE",
            conv_label="否",
            conv_short="无转换",
            conv_detail="同属 JSON 类型，无需类型转换",
        )

    # 11. 字符 → 非字符（显式类型转换）
    if sf == "STRING":
        conv_lbl = f"是 (字符→{tgt['family_cn']})"
        conv_sht = f"转: 字符→{tgt['family_cn']}"
        conv_dtl = f"来源为字符 ({src['raw']})，目标为{tgt['family_cn']} ({tgt['raw']})，需要显式类型转换"
        if tf == "NUMERIC" and ts == "INT":
            t_max = int(tgt["max_dec_digits"] or 10)
            if src["unbounded"] or int(src["char_len"] or 0) > t_max:
                cs, cl, csh, cd = (
                    "INSUFFICIENT",
                    f"无法包住 (字符位长{src['char_len'] or '无界'}超{tgt['bits']}位整型上限)",
                    "字符转整型溢出风险",
                    f"来源字符位长可能超过 {tgt['bits']} 位整型最大数字位 ({t_max} 位)",
                )
            else:
                cs, cl, csh, cd = (
                    "RISK",
                    f"需核验 (字符→{tgt['bits']}位整型格式)",
                    "需核验格式",
                    "位长在目标整型范围内，仍需校验源字符全部为合法整数文本",
                )
        elif tf == "NUMERIC" and ts == "DECIMAL":
            if tgt["unbounded"]:
                cs, cl, csh, cd = ("RISK", "需核验 (字符→定点格式)", "需核验格式", "需校验源字符全部为合法数值文本")
            else:
                max_fmt_len = int(tgt["precision"] or 0) + (1 if int(tgt["scale"] or 0) > 0 else 0) + 1
                if src["unbounded"] or int(src["char_len"] or 0) > max_fmt_len:
                    cs, cl, csh, cd = (
                        "INSUFFICIENT",
                        f"无法包住 (字符位长{src['char_len'] or '无界'}→{tgt['raw']})",
                        "字符超定点精度",
                        f"来源字符位长超过目标 {tgt['raw']} 最大文本表示长度 ({max_fmt_len} 位)",
                    )
                else:
                    cs, cl, csh, cd = (
                        "RISK",
                        f"需核验 (字符→{tgt['raw']}格式与位数)",
                        "需核验格式",
                        "需校验源字符为合法数字且整数/小数位未超目标限制",
                    )
        elif tf == "NUMERIC" and ts == "FLOAT":
            cs, cl, csh, cd = ("RISK", "需核验 (字符→浮点格式)", "需核验格式", "需校验源字符均为合法数值文本")
        elif tf == "TEMPORAL":
            cs, cl, csh, cd = (
                "RISK",
                f"需核验 (字符→{tgt['family_cn']}格式)",
                "需核验格式",
                f"需确保来源字符数据均符合目标 {tgt['raw']} 格式规范（如 YYYY-MM-DD）",
            )
        elif tf == "BINARY":
            if tgt["unbounded"]:
                cs, cl, csh, cd = ("RISK", "需核验 (字符→二进制编码)", "需核验编码", "需确认字符转二进制编码方式")
            else:
                t_b = int(tgt["byte_len"] or 0)
                if src["unbounded"] or t_b < int(src["char_len"] or 0):
                    cs, cl, csh, cd = (
                        "INSUFFICIENT",
                        f"无法包住 (字符位长{src['char_len'] or '无界'}→{t_b}字节)",
                        "字节长不足",
                        f"目标 {t_b} 字节可能无法容纳 UTF-8 编码后的来源字符",
                    )
                else:
                    cs, cl, csh, cd = ("RISK", "需核验 (字符→二进制编码)", "需核验编码", "单字节编码可容纳，多字节字符需核验字节数")
        else:
            cs, cl, csh, cd = ("RISK", f"需核验 (字符→{tgt['family_cn']})", "需核验格式", f"需确认源字符串满足目标 {tgt['raw']} 合法性")

        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="CAST_REQUIRED",
            conv_label=conv_lbl,
            conv_short=conv_sht,
            conv_detail=conv_dtl,
        )

    # 12. 非字符 → 字符（显式类型转换，检查目标字符长度是否足够包住序列化输出）
    if tf == "STRING":
        conv_lbl = f"是 ({src['family_cn']}→字符)"
        conv_sht = f"转: {src['family_cn']}→字符"
        conv_dtl = f"来源为{src['family_cn']} ({src['raw']})，目标为字符 ({tgt['raw']})，需要显式类型转换"
        if tgt["unbounded"]:
            cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {tgt['raw']} 无固定长度限制，可包住格式化输出")
        else:
            t_len = int(tgt["char_len"] or 0)
            if sf == "TEMPORAL":
                if ss == "DATE":
                    if t_len >= 10:
                        cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_len} 位可包住标准日期 YYYY-MM-DD (10位)")
                    elif t_len >= 8:
                        cs, cl, csh, cd = ("RISK", f"需核验 (位长 {t_len} 仅支持紧凑日期 YYYYMMDD)", f"位长边界 {t_len}<10", "目标仅 8~9 位，无法存储含横杠的标准 10 位日期")
                    else:
                        cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (日期需8~10位→{t_len})", f"位长不足 10→{t_len}", f"目标字符长度 {t_len} 不足以表示完整日期")
                elif ss == "TIMESTAMP":
                    req = 19 + (1 + int(src["frac"]) if src["frac"] and int(src["frac"]) > 0 else 0)
                    if t_len >= req:
                        cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_len} 位可包住完整时间戳 ({req} 位)")
                    else:
                        cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (时间戳需{req}位→{t_len})", f"位长不足 {req}→{t_len}", f"标准时间戳输出需 {req} 位，目标仅 {t_len} 位")
                elif ss == "TIME":
                    req = 8 + (1 + int(src["frac"]) if src["frac"] and int(src["frac"]) > 0 else 0)
                    if t_len >= req:
                        cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_len} 位可包住时间输出 ({req} 位)")
                    else:
                        cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (时间需{req}位→{t_len})", f"位长不足 {req}→{t_len}", f"时间格式输出需 {req} 位，目标仅 {t_len} 位")
                else:  # YEAR
                    if t_len >= 4:
                        cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", "目标可包住 4 位年份")
                    else:
                        cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (年份需4位→{t_len})", f"位长不足 4→{t_len}", "4 位年份转字符至少需 4 位")
            elif sf == "NUMERIC" and ss == "INT":
                req = int(src["max_dec_digits"] or 10) + (0 if src["unsigned"] else 1)
                safe_req = int(src["safe_dec_digits"] or 9)
                if t_len >= req:
                    cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_len} 位可包住 {src['bits']} 位整型文本（含符号最大 {req} 位）")
                elif t_len >= safe_req:
                    cs, cl, csh, cd = ("RISK", f"需核验 (整型文本最宽{req}位→{t_len})", f"位长边界 {req}→{t_len}", f"不含极值负号时通常够用，极值最大需 {req} 位")
                else:
                    cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (整型需{req}位→{t_len})", f"位长不足 {req}→{t_len}", f"{src['bits']} 位整型文本表示需至多 {req} 位，目标仅 {t_len} 位")
            elif sf == "NUMERIC" and ss == "DECIMAL":
                if src["unbounded"]:
                    cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (无界数值→定长{t_len})", "位长不足", "无界定点数值转定长字符无法保证不截断")
                else:
                    p = int(src["precision"] or 0)
                    sc = int(src["scale"] or 0)
                    base_req = p + (1 if sc > 0 else 0)
                    req_signed = base_req + 1
                    if t_len >= req_signed:
                        cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_len} 位可包住 {src['raw']}（含小数点与符号需 {req_signed} 位）")
                    elif t_len >= base_req:
                        cs, cl, csh, cd = ("RISK", f"需核验 (负值含符号需{req_signed}位→{t_len})", f"位长边界 {req_signed}→{t_len}", f"正数需 {base_req} 位，带负号时需 {req_signed} 位")
                    else:
                        cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (定点需{req_signed}位→{t_len})", f"位长不足 {req_signed}→{t_len}", f"{src['raw']} 转字符需 {base_req}~{req_signed} 位，目标仅 {t_len} 位")
            elif sf == "NUMERIC" and ss == "FLOAT":
                req = 24 if int(src["bits"] or 32) >= 64 else 15
                if t_len >= req:
                    cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", f"目标 {t_len} 位可包住浮点科学计数文本")
                elif t_len >= 10:
                    cs, cl, csh, cd = ("RISK", f"需核验 (浮点文本最宽{req}位→{t_len})", f"位长边界 {req}→{t_len}", "长小数/科学计数输出可能超出长度")
                else:
                    cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (浮点需{req}位→{t_len})", f"位长不足 {req}→{t_len}", f"目标 {t_len} 位不足以输出浮点完整文本")
            elif sf == "BOOL":
                if t_len >= 5:
                    cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", "可包住 'true'/'false' 布尔文本")
                elif t_len >= 1:
                    cs, cl, csh, cd = ("RISK", f"需核验 (位长 {t_len} 仅支持 '1'/'0' 或 'T'/'F')", f"位长边界 {t_len}<5", "小于 5 位仅支持单字符布尔编码")
                else:
                    cs, cl, csh, cd = ("INSUFFICIENT", "无法包住 (目标位长0)", "位长不足", "目标长度为 0")
            elif sf == "JSON":
                cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (JSON→定长{t_len})", "JSON转定长截断", "JSON 长度不固定，转定长字符存在截断风险")
            elif sf == "BINARY":
                if src["unbounded"] or t_len < int(src["byte_len"] or 0):
                    cs, cl, csh, cd = ("INSUFFICIENT", f"无法包住 (二进制→定长{t_len})", "位长不足", "二进制转定长字符长度不足")
                elif t_len < int(src["byte_len"] or 0) * 2:
                    cs, cl, csh, cd = ("RISK", f"需核验 (Hex编码需{int(src['byte_len'] or 0)*2}位→{t_len})", "编码位长核验", "十六进制编码需要 2 倍字节长度")
                else:
                    cs, cl, csh, cd = ("CONTAINED", "可包住", "可包住", "目标长度可包住 Hex/Base64 文本")
            else:
                cs, cl, csh, cd = ("RISK", "需核验", "需核验", f"{src['raw']} → {tgt['raw']}")

        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="CAST_REQUIRED",
            conv_label=conv_lbl,
            conv_short=conv_sht,
            conv_detail=conv_dtl,
        )

    # 13. DATE ↔ 数值 (数仓常用 8 位日期键 YYYYMMDD)
    if sf == "TEMPORAL" and ss == "DATE" and tf == "NUMERIC":
        if ts == "INT":
            ok = int(tgt["safe_dec_digits"] or 0) >= 8
            cs = "CONTAINED" if ok else "INSUFFICIENT"
            cl = "可包住" if ok else f"无法包住 (8位日期键→{tgt['bits']}位整型)"
            csh = "可包住" if ok else f"位宽不足"
            cd = "8 位 YYYYMMDD 日期整数可落入 32/64 位整型" if ok else "目标整型位宽无法容纳 8 位 YYYYMMDD 整数"
        elif ts == "DECIMAL":
            ok = tgt["unbounded"] or int(tgt["int_digits"] or 0) >= 8
            cs = "CONTAINED" if ok else "INSUFFICIENT"
            cl = "可包住" if ok else f"无法包住 (8位日期→整数位{tgt['int_digits']}位)"
            csh = "可包住" if ok else f"整数位不足 8→{tgt['int_digits']}"
            cd = "目标整数位 ≥ 8，可容纳 YYYYMMDD 日期键" if ok else "目标整数位不足 8 位"
        else:
            cs, cl, csh, cd = ("RISK", "需核验 (日期→浮点精度)", "需核验", "浮点不推荐存储 8 位日期键")
        return _compat_result(
            applicable=True,
            cap_status=cs,
            cap_label=cl,
            cap_short=csh,
            cap_detail=cd,
            conv_status="CAST_REQUIRED",
            conv_label=f"是 (日期→{tgt['family_cn']})",
            conv_short=f"转: 日期→{tgt['family_cn']}",
            conv_detail="日期类型转为数值编码（如 YYYYMMDD），涉及类型转换",
        )
    if sf == "NUMERIC" and tf == "TEMPORAL" and ts == "DATE":
        return _compat_result(
            applicable=True,
            cap_status="RISK",
            cap_label="需核验 (数值→日期 YYYYMMDD 语义)",
            cap_short="需核验日期语义",
            cap_detail=f"需确认 {src['raw']} 数值满足合法 YYYYMMDD 日期语义",
            conv_status="CAST_REQUIRED",
            conv_label=f"是 ({src['family_cn']}→日期)",
            conv_short=f"转: {src['family_cn']}→日期",
            conv_detail="数值转日期涉及显式格式解析与类型转换",
        )

    # 14. 其余异构类型不兼容映射
    return _compat_result(
        applicable=True,
        cap_status="INSUFFICIENT",
        cap_label=f"无法包住 (异构不可直接映射: {src['raw']}→{tgt['raw']})",
        cap_short="异构类型不兼容",
        cap_detail=f"来源 {src['raw']} 与目标 {tgt['raw']} 语义域不兼容，无法直接包住",
        conv_status="CAST_REQUIRED",
        conv_label=f"是 ({src['family_cn']}→{tgt['family_cn']})",
        conv_short=f"转: {src['family_cn']}→{tgt['family_cn']}",
        conv_detail=f"跨语义族映射 ({src['raw']} → {tgt['raw']})，涉及强制类型转换",
    )


def analyze_item_changes(
    item: Dict[str, Any],
    matched_col: Optional[Any] = None,
    table_meta: Optional[Any] = None,
) -> Dict[str, Any]:
    """统一判定单个贯标任务在列名、物理类型与注释三维度上的真实差异."""
    del table_meta  # 保留参数签名一致性
    if matched_col is not None:
        cur_name = str(getattr(matched_col, "column_name", "") or "")
        cur_type = str(getattr(matched_col, "data_type", "") or "")
        cur_comment = str(getattr(matched_col, "column_comment", "") or "").strip()
    else:
        has_db_snapshot = bool(
            str(item.get("db_current_col_name") or item.get("current_db_col_name") or "").strip()
            or str(item.get("db_current_type") or item.get("current_db_col_type") or "").strip()
        )
        cur_name = str(
            item.get("db_current_col_name")
            or item.get("current_db_col_name")
            or clean_identifier_input(str(item.get("old_col_name") or ""))
        )
        cur_type = str(
            item.get("db_current_type")
            or item.get("current_db_col_type")
            or item.get("old_col_type")
            or ""
        )
        cur_comment = (
            str(item.get("db_current_comment") or "").strip()
            if has_db_snapshot
            else str(item.get("old_col_comment") or "").strip()
        )

    new_name = (
        clean_identifier_input(str(item.get("new_col_name") or ""))
        or clean_identifier_input(cur_name)
        or cur_name
    )
    new_type = clean_sql_type_input(str(item.get("new_col_type") or "") or cur_type) or cur_type
    new_comment = str(item.get("new_col_comment") or "").strip()

    cur_name_clean = clean_identifier_input(cur_name)
    name_changed = (cur_name.lower() != new_name.lower()) or (cur_name != cur_name_clean)
    type_changed = bool(new_type) and (
        normalize_dws_type(cur_type) != normalize_dws_type(new_type)
    )
    comment_changed = (new_comment != "") and (cur_comment != new_comment)
    is_comment_only = (not name_changed) and (not type_changed) and comment_changed
    has_any_change = name_changed or type_changed or comment_changed

    return {
        "cur_name": cur_name,
        "cur_type": cur_type,
        "cur_comment": cur_comment,
        "new_name": new_name,
        "new_type": new_type,
        "new_comment": new_comment,
        "name_changed": name_changed,
        "type_changed": type_changed,
        "comment_changed": comment_changed,
        "is_comment_only": is_comment_only,
        "has_any_change": has_any_change,
    }


def item_requires_table_rebuild(
    item: Dict[str, Any],
    matched_col: Optional[Any] = None,
    table_meta: Optional[Any] = None,
) -> bool:
    """严格判定字段贯标是否必须采用整表保序重建 (TABLE_REBUILD).

    铁律：若仅差注释（列名与物理类型均未变更）或无任何结构变化，绝不触发整表重建。
    """
    changes = analyze_item_changes(item, matched_col=matched_col, table_meta=table_meta)
    if not changes["name_changed"] and not changes["type_changed"]:
        return False

    dist_keys_lower: set[str] = set()
    part_keys_lower: set[str] = set()
    orientation = str(item.get("table_orientation") or "").upper()
    if table_meta is not None:
        dist_keys_lower = {
            clean_identifier_input(str(k)).lower()
            for k in (getattr(table_meta, "distribute_keys", None) or [])
            if clean_identifier_input(str(k))
        }
        part_keys_lower = {
            clean_identifier_input(str(k)).lower()
            for k in (getattr(table_meta, "partition_keys", None) or [])
            if clean_identifier_input(str(k))
        }
        orientation = str(getattr(table_meta, "orientation", "") or orientation).upper()

    key_set = dist_keys_lower | part_keys_lower
    cur_lower = clean_identifier_input(changes["cur_name"]).lower()
    old_lower = clean_identifier_input(str(item.get("old_col_name") or "")).lower()
    new_lower = clean_identifier_input(changes["new_name"]).lower()

    if table_meta is not None:
        is_dist_or_part = bool(key_set) and (
            cur_lower in key_set or old_lower in key_set or new_lower in key_set
        )
        if is_dist_or_part:
            return True
        if orientation == "COLUMN" and changes["type_changed"]:
            return True
        return False

    if bool(item.get("is_distribute_key")):
        return True
    if orientation == "COLUMN" and changes["type_changed"]:
        return True
    if str(item.get("strategy") or "").upper() == "TABLE_REBUILD":
        return True
    return False
