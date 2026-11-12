"""跨方言视图 SQL 解析与四层递归血缘穿透引擎."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.platform.dialects.identifiers import clean_identifier_input
from core.platform.dialects.types import is_generic_or_unbounded_type
from core.platform.models import ColumnMeta, TableMeta

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


def split_top_level_select_items_with_comments(select_body: str) -> List[Tuple[str, str]]:
    """切分顶层 SELECT 投影项，并精准保留同行逗号前后的行尾注释或块注释."""
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


def strip_sql_comments_preserve_literals(sql_text: str) -> str:
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


def infer_sql_expr_type(expr: str, col_name: str) -> Tuple[str, bool]:
    """根据视图 SELECT 表达式或字段命名规范智能推导数据类型."""
    clean_e = " ".join((expr or "").strip().split())
    upper_e = clean_e.upper()

    m_cast = re.search(
        r"\b(?:TRY_)?CAST\s*\(.+?\bAS\s+([A-Za-z0-9_]+(?:\s+VARYING|\s+PRECISION|\s+WITHOUT\s+TIME\s+ZONE|\s+WITH\s+TIME\s+ZONE)?(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)\s*\)$",
        clean_e,
        flags=re.IGNORECASE,
    )
    if m_cast:
        return m_cast.group(1).strip().upper(), True

    m_pg_cast = re.search(
        r"::\s*([A-Za-z0-9_]+(?:\s+VARYING|\s+PRECISION)?(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)\s*$",
        clean_e,
        flags=re.IGNORECASE,
    )
    if m_pg_cast:
        return m_pg_cast.group(1).strip().upper(), True

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
    if re.match(
        r"^(?:TO_TIMESTAMP|CURRENT_TIMESTAMP|NOW|SYSDATE|SYSTIMESTAMP|FROM_UNIXTIME)\b",
        upper_e,
    ):
        return "TIMESTAMP", True
    if re.match(
        r"^(?:CONCAT|CONCAT_WS|SUBSTR|SUBSTRING|TRIM|LTRIM|RTRIM|UPPER|LOWER|REPLACE|REGEXP_REPLACE|TO_CHAR|DATE_FORMAT|LPAD|RPAD)\s*\(",
        upper_e,
    ):
        return "VARCHAR(255)", False

    if re.match(r"^'.*'$", clean_e):
        str_len = max(16, min(255, len(clean_e) * 2))
        return f"VARCHAR({str_len})", False
    if re.match(r"^-?\d+$", clean_e):
        return "INTEGER", False
    if re.match(r"^-?\d+\.\d+$", clean_e):
        return "NUMERIC(18,2)", False

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


def parse_view_sql_lineage(
    view_sql: str,
    default_schema: str = "public",
    outer_cte_map: Optional[Dict[str, Dict[str, Any]]] = None,
    _depth: int = 0,
) -> Dict[str, Any]:
    """解析任意方言的视图定义或 SELECT 语句，支持多级 CTE 与嵌套子查询四层以上递归血缘溯源."""
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
    if not raw or _depth > 8:
        return result

    header_col_names: List[str] = []
    header_col_comments: Dict[str, str] = {}
    body_sql = raw

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
                for item_txt, _ in split_top_level_select_items_with_comments(hdr_inside):
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

    # 继承上层 CTE 上下文，使 cte2 -> cte1 -> base_table 多级串联 CTE 可逐层穿透
    cte_map: Dict[str, Dict[str, Any]] = dict(outer_cte_map or {})
    main_query_sql = body_sql
    clean_for_cte = strip_sql_comments_preserve_literals(body_sql).lstrip()

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
            cte_parsed = parse_view_sql_lineage(
                cte_inner_sql,
                default_schema=eff_default_schema,
                outer_cte_map=cte_map,
                _depth=_depth + 1,
            )
            cte_map[cte_name] = cte_parsed
            idx_scan = close_p + 1
            while idx_scan < n_len and body_sql[idx_scan].isspace():
                idx_scan += 1
            if idx_scan < n_len and body_sql[idx_scan] == ",":
                idx_scan += 1
                continue
            main_query_sql = body_sql[idx_scan:].strip()
            break

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

    alias_to_table: Dict[str, Tuple[str, str]] = {}
    base_tables_list: List[Dict[str, str]] = []
    seen_base: Set[Tuple[str, str]] = set()

    def _add_base_table(sch: str, tbl: str, alias_nm: str, has_explicit_schema: bool = False) -> None:
        c_sch = clean_identifier_input(sch) or eff_default_schema
        c_tbl = clean_identifier_input(tbl)
        if not c_tbl or c_tbl.upper() in ("DUAL", "SYSIBM.SYSDUMMY1"):
            return
        if not has_explicit_schema and c_tbl.lower() in cte_map:
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
        clean_from = strip_sql_comments_preserve_literals(from_clause_str)
        idx_f = 0
        sub_counter = 0
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
                    if re.match(r"^(?:WITH|SELECT)\b", inner_sub, flags=re.IGNORECASE):
                        sub_counter += 1
                        raw_sub_al = clean_identifier_input(m_sub_alias.group(1)) if m_sub_alias else ""
                        if not raw_sub_al or raw_sub_al.upper() in _SQL_KEYWORDS_STOP_ALIAS:
                            raw_sub_al = f"__subq_{sub_counter}"
                        sub_alias = raw_sub_al.lower()
                        sub_parsed = parse_view_sql_lineage(
                            inner_sub,
                            default_schema=eff_default_schema,
                            outer_cte_map=cte_map,
                            _depth=_depth + 1,
                        )
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
                _add_base_table(sp[0], sp[1], raw_alias, has_explicit_schema=True)
            else:
                _add_base_table(eff_default_schema, raw_tref, raw_alias, has_explicit_schema=False)

    result["base_tables"] = base_tables_list

    raw_items = split_top_level_select_items_with_comments(select_list_str)
    parsed_columns: List[Dict[str, Any]] = []

    for idx, (item_expr, item_comm) in enumerate(raw_items, start=1):
        clean_item = " ".join(item_expr.strip().split())
        if not clean_item:
            continue

        # 支持 SELECT * 或 alias.* 在有上游 CTE 列信息时自动展开
        m_star = re.match(r"^(?:([A-Za-z0-9_\"`]+)\.)?\*$", clean_item)
        if m_star:
            star_qual = clean_identifier_input(m_star.group(1) or "").lower()
            lookup_al = star_qual or (next(iter(alias_to_table.keys())) if len(alias_to_table) == 1 else "")
            if lookup_al and lookup_al in alias_to_table:
                tag_s, cte_nm = alias_to_table[lookup_al]
                if tag_s == "__CTE__" and cte_nm in cte_map:
                    for cc in cte_map[cte_nm].get("columns", []):
                        parsed_columns.append(
                            {
                                **cc,
                                "ordinal_position": len(parsed_columns) + 1,
                            }
                        )
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
        if not target_lookup_alias and len(alias_to_table) == 1:
            target_lookup_alias = next(iter(alias_to_table.keys()))
        elif not target_lookup_alias and len(base_tables_list) == 1:
            target_lookup_alias = base_tables_list[0]["alias"].lower()

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

        inferred_tp, has_cast = infer_sql_expr_type(core_expr, final_col_name)
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


def enrich_view_columns_by_lineage(
    meta: TableMeta,
    fetch_object_fn: Optional[Callable[[str, str], Optional[TableMeta]]] = None,
    max_depth: int = 4,
    _visited: Optional[Set[Tuple[str, str]]] = None,
) -> TableMeta:
    """对视图对象执行最多 max_depth=4 层递归视图/基表血缘穿透，补全精确字段类型、中文注释与分布/分区键."""
    view_sql = (meta.view_definition or meta.raw_ddl or "").strip()
    if not view_sql:
        return meta

    visited = set(_visited or set())
    self_key = ((meta.schema_name or "").lower(), (meta.table_name or "").lower())
    visited.add(self_key)

    lineage = parse_view_sql_lineage(view_sql, default_schema=meta.schema_name or "public")
    if not lineage.get("is_view_sql"):
        return meta

    if not meta.table_comment and lineage.get("view_comment"):
        meta.table_comment = lineage["view_comment"]

    # 若视图本身没有探测到列（如离线解析或权限限制），用 SQL 解析列初始化
    if not meta.columns and lineage.get("columns"):
        meta.columns = [
            ColumnMeta(
                ordinal_position=pc["ordinal_position"],
                column_name=pc["column_name"],
                data_type=pc["inferred_type"],
                column_comment=pc.get("inline_comment", ""),
            )
            for pc in lineage["columns"]
        ]

    base_meta_cache: Dict[Tuple[str, str], Optional[TableMeta]] = {}
    physical_base_names: List[str] = []
    seen_phys: Set[str] = set()

    if fetch_object_fn and max_depth > 0:
        for bt in lineage.get("base_tables", []):
            b_sch = bt.get("schema_name") or meta.schema_name or "public"
            b_tbl = bt.get("table_name") or ""
            if not b_tbl:
                continue
            b_key = (b_sch.lower(), b_tbl.lower())
            if b_key in visited or b_key in base_meta_cache:
                continue
            try:
                upstream = fetch_object_fn(b_sch, b_tbl)
            except Exception:
                upstream = None

            if upstream and upstream.exists:
                if upstream.object_type in ("VIEW", "MATVIEW") and max_depth > 1:
                    upstream = enrich_view_columns_by_lineage(
                        upstream,
                        fetch_object_fn=fetch_object_fn,
                        max_depth=max_depth - 1,
                        _visited=visited,
                    )
                    for pb in upstream.penetrated_base_tables:
                        if pb.lower() not in seen_phys:
                            seen_phys.add(pb.lower())
                            physical_base_names.append(pb)
                else:
                    disp = f"{upstream.schema_name}.{upstream.table_name}"
                    if disp.lower() not in seen_phys:
                        seen_phys.add(disp.lower())
                        physical_base_names.append(disp)
                base_meta_cache[b_key] = upstream
            else:
                base_meta_cache[b_key] = None
                disp = f"{b_sch}.{b_tbl}"
                if disp.lower() not in seen_phys:
                    seen_phys.add(disp.lower())
                    physical_base_names.append(disp)
    else:
        for bt in lineage.get("base_tables", []):
            b_sch = bt.get("schema_name") or meta.schema_name or "public"
            b_tbl = bt.get("table_name") or ""
            if b_tbl:
                disp = f"{b_sch}.{b_tbl}"
                if disp.lower() not in seen_phys:
                    seen_phys.add(disp.lower())
                    physical_base_names.append(disp)

    if physical_base_names:
        meta.penetrated_base_tables = physical_base_names

    parsed_by_name = {
        pc["column_name"].lower(): pc for pc in lineage.get("columns", [])
    }
    parsed_list = lineage.get("columns", [])

    inferred_dist_keys: List[str] = list(meta.distribute_keys or [])
    inferred_part_keys: List[str] = list(meta.partition_keys or [])

    for idx, col in enumerate(meta.columns):
        pc = parsed_by_name.get(col.column_name.lower())
        if pc is None and idx < len(parsed_list):
            pc = parsed_list[idx]
        if not pc:
            continue

        if not col.column_comment and pc.get("inline_comment"):
            col.column_comment = pc["inline_comment"]

        src_sch = pc.get("source_schema") or meta.schema_name or "public"
        src_tbl = pc.get("source_table") or ""
        src_col = pc.get("source_column") or col.column_name
        if not src_tbl:
            if is_generic_or_unbounded_type(col.data_type) and pc.get("inferred_type"):
                col.data_type = pc["inferred_type"]
            continue

        upstream_meta = base_meta_cache.get((src_sch.lower(), src_tbl.lower()))
        if upstream_meta and upstream_meta.exists:
            matched_base_col = next(
                (
                    bc
                    for bc in upstream_meta.columns
                    if bc.column_name.lower() == src_col.lower()
                ),
                None,
            )
            if matched_base_col:
                if is_generic_or_unbounded_type(col.data_type) or not col.data_type:
                    if pc.get("has_explicit_cast") and pc.get("inferred_type"):
                        col.data_type = pc["inferred_type"]
                    else:
                        col.data_type = matched_base_col.data_type
                if not col.column_comment and matched_base_col.column_comment:
                    col.column_comment = matched_base_col.column_comment
                col.lineage_source = (
                    matched_base_col.lineage_source
                    or f"{upstream_meta.schema_name}.{upstream_meta.table_name}.{matched_base_col.column_name}"
                )
                if matched_base_col.column_name.lower() in {
                    k.lower() for k in upstream_meta.distribute_keys
                } and col.column_name.lower() not in {k.lower() for k in inferred_dist_keys}:
                    inferred_dist_keys.append(col.column_name)
                if matched_base_col.column_name.lower() in {
                    k.lower() for k in upstream_meta.partition_keys
                } and col.column_name.lower() not in {k.lower() for k in inferred_part_keys}:
                    inferred_part_keys.append(col.column_name)
                continue

        if not col.lineage_source:
            col.lineage_source = f"{src_sch}.{src_tbl}.{src_col}"
        if is_generic_or_unbounded_type(col.data_type) and pc.get("inferred_type"):
            col.data_type = pc["inferred_type"]

    meta.distribute_keys = inferred_dist_keys
    meta.partition_keys = inferred_part_keys
    return meta
