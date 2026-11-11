"""SQL 标识符清洗、合法性校验与多方言安全转义工具."""

from __future__ import annotations

import re

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
    "performance_schema",
    "mysql",
    "sys",
)

_IDENTIFIER_QUOTE_CHARS = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'

BACKTICK_DIALECTS = {
    "INCEPTOR",
    "MYSQL",
    "MARIADB",
    "OCEANBASE",
    "DORIS",
    "STARROCKS",
    "TIDB",
}


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


def quote_ident_str(ident: str) -> str:
    """格式化目标数据库标识符：标准标识符保持自然无引号格式，特殊字符使用双引号安全包裹."""
    validated = validate_identifier(ident)
    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_$]*$", validated):
        return validated
    return '"' + validated.replace('"', '""') + '"'


def quote_backtick_str(ident: str) -> str:
    """格式化反引号方言（MySQL / Inceptor / Doris 等）标识符."""
    validated = validate_identifier(ident)
    return "`" + validated.replace("`", "``") + "`"


def quote_dialect_ident(ident: str, dialect: str = "DWS") -> str:
    """根据数据库方言选择反引号或双引号安全包裹标识符（始终加引号用于 SELECT 预览防关键字冲突）."""
    validated = validate_identifier(ident)
    d = (dialect or "DWS").strip().upper()
    if d in BACKTICK_DIALECTS:
        return "`" + validated.replace("`", "``") + "`"
    return '"' + validated.replace('"', '""') + '"'


def format_existing_db_ident(raw_ident: str) -> str:
    """格式化数据库中已存在的原字段名：标准字段名保持无引号，特殊字符用双引号精确匹配."""
    s = (raw_ident or "").strip()
    if not s:
        raise ValueError("原字段名不能为空")
    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_$]*$", s):
        return s
    return '"' + s.replace('"', '""') + '"'


def quote_literal_str(text: str) -> str:
    """将文本安全转义为 SQL 单引号字符串字面量."""
    val = (text or "").replace("'", "''")
    return f"'{val}'"
