"""跨方言建表 DDL 代码生成与 SQL 可读性格式化工具."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.platform.dialects.identifiers import (
    clean_identifier_input,
    quote_backtick_str,
    quote_ident_str,
    quote_literal_str,
)
from core.platform.dialects.types import translate_column_type
from core.platform.models import TableMeta


def format_readable_sql_ddl(sql_text: str) -> str:
    """将单行超长的 CREATE VIEW / SELECT 语句自动换行美化."""
    raw = (sql_text or "").strip()
    if not raw:
        return ""
    lines = raw.splitlines()
    if all(len(ln) <= 110 for ln in lines):
        return raw

    formatted_lines: List[str] = []
    for ln in lines:
        if len(ln) <= 110 or ln.lstrip().startswith("--"):
            formatted_lines.append(ln)
            continue
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


def build_dialect_ddl(
    meta: TableMeta,
    *,
    source_dialect: str,
    target_dialect: str,
    target_schema: str = "",
    target_table: str = "",
    storage_mode: str = "AUTO",
) -> Dict[str, Any]:
    """将 TableMeta 翻译生成指定目标方言 (DWS / PGSQL / INCEPTOR / MYSQL / DM8 / ORACLE / SQLITE) 的可执行建表 DDL."""
    src_d = (source_dialect or "DWS").strip().upper()
    tgt_d = (target_dialect or "DWS").strip().upper()
    s_mode = (storage_mode or "AUTO").strip().upper()

    default_sch = (
        "default"
        if tgt_d == "INCEPTOR"
        else ("main" if tgt_d == "SQLITE" else "public")
    )
    eff_schema = (
        clean_identifier_input(target_schema)
        or clean_identifier_input(meta.schema_name)
        or default_sch
    )
    eff_table = (
        clean_identifier_input(target_table)
        or clean_identifier_input(meta.table_name)
        or "unnamed_table"
    )

    dist_keys = [
        clean_identifier_input(k)
        for k in (meta.distribute_keys or [])
        if clean_identifier_input(k)
    ]
    part_keys = [
        clean_identifier_input(k)
        for k in (meta.partition_keys or [])
        if clean_identifier_input(k)
    ]
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

    # 1. 星环 Transwarp Inceptor
    if tgt_d == "INCEPTOR":
        full_tbl = f"{quote_ident_str(eff_schema)}.{quote_ident_str(eff_table)}"
        non_part_cols = [m for m in column_mappings if not m["is_partition_key"]]
        part_col_list = [m for m in column_mappings if m["is_partition_key"]]
        if not non_part_cols:
            non_part_cols = column_mappings
            part_col_list = []

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

        if part_col_list:
            p_defs = []
            for pm in part_col_list:
                p_comm = (
                    f" COMMENT {quote_literal_str(pm['column_comment'])}"
                    if pm["column_comment"]
                    else ""
                )
                p_defs.append(f"{quote_ident_str(pm['column_name'])} {pm['target_type']}{p_comm}")
            ddl_lines.append(f"PARTITIONED BY ({', '.join(p_defs)})")

        cluster_keys = [k for k in dist_keys if k.lower() not in part_lower_set]
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

    # 2. MySQL / OceanBase / Doris / StarRocks / TiDB
    if tgt_d in ("MYSQL", "OCEANBASE", "DORIS", "STARROCKS", "TIDB"):
        full_tbl = f"{quote_backtick_str(eff_schema)}.{quote_backtick_str(eff_table)}"
        col_lines = []
        for idx, m in enumerate(column_mappings):
            comma = "," if idx < len(column_mappings) - 1 else ""
            nn_sql = " NOT NULL" if m["is_not_null"] else ""
            comm_sql = (
                f" COMMENT {quote_literal_str(m['column_comment'])}"
                if m["column_comment"]
                else ""
            )
            note_comment = f" /* {m['translation_note']} */" if m["translation_note"] else ""
            col_lines.append(
                f"    {quote_backtick_str(m['column_name'])} {m['target_type']}{nn_sql}{comm_sql}{comma}{note_comment}"
            )
        tbl_comm = (
            f" COMMENT={quote_literal_str(meta.table_comment)}"
            if meta.table_comment
            else ""
        )
        ddl_lines = [
            "-- ============================================================================",
            f"-- [跨环境方言建表 DDL] 来源方言: {src_d} -> 目标方言: {tgt_d}",
            f"-- 目标表: {eff_schema}.{eff_table} (共 {len(column_mappings)} 列)",
            *view_header_notes,
            "-- ============================================================================",
            f"DROP TABLE IF EXISTS {full_tbl};",
            f"CREATE TABLE {full_tbl} (",
            "\n".join(col_lines),
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4{tbl_comm};",
        ]
        return {
            "target_dialect": tgt_d,
            "target_schema": eff_schema,
            "target_table": eff_table,
            "storage_mode": s_mode,
            "column_count": len(column_mappings),
            "ddl": "\n".join(ddl_lines),
            "column_mappings": column_mappings,
        }

    # 3. 华为云 GaussDB(DWS) / PostgreSQL / 达梦 DM8 / Oracle / SQLite
    full_tbl = (
        quote_ident_str(eff_table)
        if tgt_d == "SQLITE"
        else f"{quote_ident_str(eff_schema)}.{quote_ident_str(eff_table)}"
    )
    col_lines = []
    comment_lines: List[str] = []
    if meta.table_comment and tgt_d != "SQLITE":
        comment_lines.append(
            f"COMMENT ON TABLE {full_tbl} IS {quote_literal_str(meta.table_comment)};"
        )

    for idx, m in enumerate(column_mappings):
        comma = "," if idx < len(column_mappings) - 1 else ""
        not_null_sql = " NOT NULL" if m["is_not_null"] else ""
        default_sql = ""
        if m["column_default"]:
            raw_def = str(m["column_default"]).strip()
            clean_def = re.sub(r"::[a-zA-Z0-9_ ]+(\(\d+(?:,\d+)?\))?", "", raw_def)
            if clean_def and "nextval(" not in clean_def.lower():
                default_sql = f" DEFAULT {clean_def}"
        note_comment = f" /* {m['translation_note']} */" if m["translation_note"] else ""
        col_lines.append(
            f"    {quote_ident_str(m['column_name'])} {m['target_type']}{default_sql}{not_null_sql}{comma}{note_comment}"
        )
        if m["column_comment"] and tgt_d != "SQLITE":
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
            tail_lines.append(f"PARTITION BY RANGE ({q_pk})")
    elif tgt_d in ("PGSQL", "KINGBASE"):
        if part_keys:
            q_pk = ", ".join(quote_ident_str(k) for k in part_keys)
            tail_lines.append(f"PARTITION BY RANGE ({q_pk})")

    close_paren = ")" + ("\n" + "\n".join(tail_lines) if tail_lines else "") + ";"
    dialect_titles = {
        "DWS": "华为云 GaussDB(DWS)",
        "PGSQL": "PostgreSQL (PGSQL)",
        "KINGBASE": "人大金仓 KingbaseES",
        "DM8": "达梦数据库 DM8",
        "ORACLE": "Oracle Database",
        "SQLITE": "SQLite 3",
    }
    dialect_title = dialect_titles.get(tgt_d, tgt_d)
    drop_stmt = (
        f"DROP TABLE IF EXISTS {full_tbl};"
        if tgt_d != "ORACLE"
        else f"-- Oracle 如需重建请先手工执行 DROP TABLE {full_tbl};"
    )
    ddl_lines = [
        "-- ============================================================================",
        f"-- [跨环境方言建表 DDL] 来源方言: {src_d} -> 目标方言: {dialect_title}",
        f"-- 目标表: {eff_schema}.{eff_table} (共 {len(column_mappings)} 列，物理列序 100% 保持一致)",
        *view_header_notes,
        "-- ============================================================================",
        drop_stmt,
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
