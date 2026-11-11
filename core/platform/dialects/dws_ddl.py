"""华为云 GaussDB(DWS) 建表 DDL 解析、存储参数与分区子句保序处理工具."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from core.platform.dialects.identifiers import (
    clean_identifier_input,
    quote_ident_str,
    quote_literal_str,
)
from core.platform.dialects.types import normalize_dws_type


def format_reloptions_with_clause(raw_reloptions: Any) -> str:
    """将 pg_class.reloptions 或 WITH 子句规范化，自动为含空格的参数值（如 period='1 week'）补充单引号."""
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


def find_matching_paren(text: str, open_idx: int) -> int:
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
    """解析华为 DWS pg_catalog.pg_get_tabledef(oid) 返回的完整建表 DDL."""
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

    close_paren_idx = find_matching_paren(tabledef, open_paren_idx)
    if close_paren_idx == -1:
        return info

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

    m_with = re.search(r"\bWITH\s*\(", table_tail, flags=re.IGNORECASE)
    if m_with:
        w_open = table_tail.find("(", m_with.start())
        w_close = find_matching_paren(table_tail, w_open) if w_open != -1 else -1
        if w_close != -1:
            raw_with = table_tail[m_with.start() : w_close + 1]
            info["with_clause"] = format_reloptions_with_clause(raw_with)

    m_dist = re.search(r"\bDISTRIBUTE\s+BY\s+(\w+)", table_tail, flags=re.IGNORECASE)
    if m_dist:
        dist_mode = m_dist.group(1).upper()
        if dist_mode in ("HASH", "MODULO"):
            d_open = table_tail.find("(", m_dist.end() - len(dist_mode))
            d_close = find_matching_paren(table_tail, d_open) if d_open != -1 else -1
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


def partition_clause_has_child_defs(partition_clause: str) -> bool:
    """检查 PARTITION BY 子句中是否已包含具体的子分区定义块."""
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


def format_partition_boundary_val(raw_bound: str, col_data_type: str = "") -> str:
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
    """若分区键字段在重建表时被重命名，同步更新 PARTITION BY (...) 中的分区键列名."""
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
