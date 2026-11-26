"""基于字段变更历史的确定性 SQL 脚本词法/作用域解析、精准 Span 改写与类型影响雷达.

核心设计原则（零外部依赖、0 Token、100% 可复现审计）：
1. 精确 Span 无损词法扫描：保留全部原始缩进、换行、注释与引号风格，绝不误伤字符串字面量、注释或函数名；
   自动解包 PL/pgSQL / DWS 过程体 `$$...$$` 并识别局部 `DECLARE` 变量以防止误改。
2. 嵌套作用域与表别名解析：支持 `WITH [RECURSIVE]` 多层 CTE、`FROM/JOIN` 别名、逗号连接、派生表子查询、
   相关子查询、`UNION/INTERSECT/EXCEPT` 分支、`INSERT`/`UPDATE`/`DELETE`/`MERGE`/`CREATE TABLE`/`CREATE INDEX`/`COMMENT ON COLUMN`。
3. 变更链折叠与历史中间名兼容：消费工作区 `effective_column_mappings` 与历史链（`A→B→C` 同时兼容引用 `A` 或 `B` 的脚本），
   同时安全支持列互换（`a↔b`）与幂等二次执行（0 改动）。
4. L2 类型转换与 L3 精度缩减静态影响雷达：识别字面量类型冲突、字符串/数值函数不匹配、`NVL/COALESCE` 默认值冲突、
   `CAST/::` 复核、跨类型 JOIN、缩精度写入截断风险及动态 SQL 字符串命中，为 Stage 2 轻量大模型提供精准微切片。
"""

from __future__ import annotations

import bisect
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from core.platform.dialects import clean_identifier_input, normalize_dws_type

_DOLLAR_TAG_RE = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_BODY_SQL_HINT_RE = re.compile(
    r"(?i)\b(?:BEGIN|DECLARE|SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|TRUNCATE|LOOP|EXECUTE)\b"
)
_DYN_SQL_KEYWORD_RE = re.compile(
    r"(?i)\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|FROM|WHERE|SET)\b"
)

_SQL_KEYWORDS: Set[str] = {
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "CROSS",
    "NATURAL", "LATERAL", "ON", "USING", "AS", "AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE",
    "IN", "EXISTS", "BETWEEN", "LIKE", "ILIKE", "SIMILAR", "TO", "ESCAPE", "CASE", "WHEN",
    "THEN", "ELSE", "END", "GROUP", "BY", "HAVING", "ORDER", "ASC", "DESC", "NULLS", "FIRST",
    "LAST", "LIMIT", "OFFSET", "FETCH", "NEXT", "ROWS", "ONLY", "UNION", "INTERSECT", "EXCEPT",
    "ALL", "DISTINCT", "WITH", "RECURSIVE", "INSERT", "INTO", "VALUES", "VALUE", "UPDATE", "SET",
    "DELETE", "MERGE", "MATCHED", "CREATE", "REPLACE", "TEMP", "TEMPORARY", "GLOBAL", "LOCAL",
    "UNLOGGED", "EXTERNAL", "TABLE", "VIEW", "MATERIALIZED", "INDEX", "UNIQUE", "IF", "ALTER",
    "DROP", "TRUNCATE", "COMMENT", "COLUMN", "ADD", "RENAME", "TYPE", "DEFAULT", "PRIMARY",
    "KEY", "FOREIGN", "REFERENCES", "CONSTRAINT", "CHECK", "COLLATE", "DISTRIBUTE", "HASH",
    "REPLICATION", "ROUNDROBIN", "PARTITION", "RANGE", "LIST", "CLUSTERED", "STORED", "LOCATION",
    "OVER", "WINDOW", "ROWS", "PRECEDING", "FOLLOWING", "CURRENT", "ROW", "UNBOUNDED",
    "QUALIFY", "RETURNING", "FOR", "SHARE", "NOWAIT", "SKIP", "LOCKED", "CAST", "EXTRACT",
    "INTERVAL", "AT", "TIME", "ZONE", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    "SYSDATE", "SYSTIMESTAMP", "LOCALTIME", "LOCALTIMESTAMP", "USER", "CURRENT_USER", "DUAL",
    "BEGIN", "DECLARE", "EXCEPTION", "RAISE", "RETURN", "PROCEDURE", "FUNCTION", "LANGUAGE",
    "PLPGSQL", "SQL", "VOLATILE", "IMMUTABLE", "STABLE", "LOOP", "WHILE", "ELSIF", "ELSEIF",
    "EXIT", "CONTINUE", "COMMIT", "ROLLBACK", "PERFORM", "EXECUTE", "IMMEDIATE", "OPEN",
    "CLOSE", "CURSOR", "CALL", "EXEC", "USE", "OVERWRITE", "DIRECTORY", "ANALYZE", "EXPLAIN",
    "GRANT", "REVOKE", "PRIVILEGES", "CASCADE", "RESTRICT", "GENERATED", "ALWAYS", "IDENTITY",
    "AUTO_INCREMENT", "UNSIGNED", "SIGNED", "ZEROFILL", "CHARACTER", "VARYING", "DOUBLE",
    "PRECISION", "WITHOUT", "VARCHAR", "VARCHAR2", "NVARCHAR", "NVARCHAR2", "CHAR", "NCHAR",
    "TEXT", "CLOB", "BLOB", "BYTEA", "INT", "INT2", "INT4", "INT8", "INTEGER", "BIGINT",
    "SMALLINT", "TINYINT", "NUMERIC", "DECIMAL", "NUMBER", "FLOAT", "FLOAT4", "FLOAT8", "REAL",
    "DATE", "TIMESTAMP", "DATETIME", "BOOLEAN", "BOOL", "JSON", "JSONB", "UUID", "XML",
}

_HARD_CLAUSE_KEYWORDS: Set[str] = {
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "CROSS",
    "NATURAL", "LATERAL", "ON", "USING", "AS", "AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE",
    "IN", "EXISTS", "BETWEEN", "LIKE", "ILIKE", "SIMILAR", "TO", "ESCAPE", "CASE", "WHEN",
    "THEN", "ELSE", "END", "GROUP", "BY", "HAVING", "ORDER", "ASC", "DESC", "NULLS",
    "LIMIT", "OFFSET", "FETCH", "UNION", "INTERSECT", "EXCEPT", "ALL", "DISTINCT", "WITH",
    "RECURSIVE", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "MERGE", "MATCHED",
    "CREATE", "REPLACE", "TABLE", "VIEW", "MATERIALIZED", "INDEX", "UNIQUE", "IF", "ALTER",
    "DROP", "TRUNCATE", "COLUMN", "ADD", "RENAME", "PRIMARY", "FOREIGN", "REFERENCES",
    "CONSTRAINT", "CHECK", "COLLATE", "DISTRIBUTE", "PARTITION", "OVER", "WINDOW",
    "QUALIFY", "RETURNING", "FOR", "CAST", "EXTRACT", "INTERVAL", "BEGIN", "DECLARE",
    "EXCEPTION", "RAISE", "RETURN", "PROCEDURE", "FUNCTION", "LANGUAGE", "LOOP", "WHILE",
    "ELSIF", "ELSEIF", "EXIT", "CONTINUE", "COMMIT", "ROLLBACK", "PERFORM", "EXECUTE",
    "IMMEDIATE", "OPEN", "CLOSE", "CURSOR", "CALL", "EXEC", "USE", "CONFLICT", "DO",
    "DUPLICATE",
}

_CLAUSE_STOP_ALIAS: Set[str] = {
    "WHERE", "JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "CROSS", "NATURAL", "LATERAL",
    "ON", "USING", "GROUP", "ORDER", "HAVING", "LIMIT", "OFFSET", "FETCH", "UNION", "INTERSECT",
    "EXCEPT", "SET", "VALUES", "INTO", "WHEN", "THEN", "ELSE", "END", "QUALIFY", "WINDOW",
    "DISTRIBUTE", "PARTITION", "RETURNING", "FOR", "WITH", "SELECT", "FROM", "UPDATE", "DELETE",
    "INSERT", "MERGE", "CREATE", "AND", "OR", "NOT", "AS", "BY", "TO", "IS", "CONFLICT", "DO",
}

_DDL_COL_CONSTRAINT_STOPS: Set[str] = {
    "NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE", "CHECK", "REFERENCES", "CONSTRAINT",
    "COLLATE", "GENERATED", "COMMENT", "AUTO_INCREMENT", "IDENTITY", "ENCODE", "COMPRESS",
    "ON", "USING", "WITH", "DISTRIBUTE", "PARTITION",
}

_DDL_TABLE_CONSTRAINT_STARTS: Set[str] = {
    "CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "LIKE", "INDEX", "KEY",
    "DISTRIBUTE", "PARTITION", "PERIOD", "EXCLUDE",
}

_STRING_ONLY_FUNCS: Set[str] = {
    "TRIM", "LTRIM", "RTRIM", "BTRIM", "SUBSTR", "SUBSTRING", "LENGTH", "CHAR_LENGTH",
    "CHARACTER_LENGTH", "OCTET_LENGTH", "UPPER", "LOWER", "INITCAP", "REPLACE", "LPAD", "RPAD",
    "REGEXP_REPLACE", "REGEXP_SUBSTR", "REGEXP_LIKE", "SPLIT_PART", "INSTR", "STRPOS",
    "POSITION", "LEFT", "RIGHT", "CONCAT", "CONCAT_WS", "REVERSE", "TRANSLATE",
}

_NUMERIC_AGG_FUNCS: Set[str] = {
    "SUM", "AVG", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP",
    "ABS", "ROUND", "CEIL", "CEILING", "FLOOR", "MOD", "POWER", "SQRT",
}

_COALESCE_FUNCS: Set[str] = {"COALESCE", "NVL", "IFNULL", "ISNULL"}

_TYPE_CONVERSION_FUNCS: Set[str] = {
    "TO_NUMBER", "TO_CHAR", "TO_DATE", "TO_TIMESTAMP", "STR_TO_DATE", "DATE_FORMAT",
    "TRY_CAST", "CAST", "CONVERT",
}

_COMPARISON_OPS: Set[str] = {"=", "!=", "<>", ">", "<", ">=", "<="}


def classify_type_family(sql_type: str) -> str:
    """将数据库物理类型归一化为语义家族，用于 L2/L3 表达式影响判断."""
    norm = normalize_dws_type(sql_type or "").strip().lower()
    if not norm:
        return "OTHER"
    base = re.sub(r"\(.*\)", "", norm).strip()
    if any(
        k in base
        for k in ("char", "text", "clob", "string", "bpchar", "nvarchar", "varchar")
    ):
        return "STRING"
    if base in ("smallint", "integer", "int", "int2", "int4", "int8", "bigint", "tinyint", "serial", "bigserial"):
        return "INT"
    if any(k in base for k in ("numeric", "decimal", "number", "money")):
        return "DECIMAL"
    if any(k in base for k in ("float", "double", "real")):
        return "FLOAT"
    if "timestamp" in base or "datetime" in base:
        return "TIMESTAMP"
    if base == "date":
        return "DATE"
    if base in ("boolean", "bool"):
        return "BOOL"
    if any(k in base for k in ("bytea", "blob", "binary", "raw", "varbinary")):
        return "BINARY"
    if "json" in base:
        return "JSON"
    return "OTHER"


def _is_numeric_family(fam: str) -> bool:
    return fam in ("INT", "DECIMAL", "FLOAT")


def _is_temporal_family(fam: str) -> bool:
    return fam in ("DATE", "TIMESTAMP")


def _build_line_offsets(text: str) -> List[int]:
    offsets = [0]
    pos = text.find("\n")
    while pos != -1:
        offsets.append(pos + 1)
        pos = text.find("\n", pos + 1)
    return offsets


def _offset_to_line_col(offsets: List[int], char_pos: int) -> Tuple[int, int]:
    pos = max(0, char_pos)
    idx = bisect.bisect_right(offsets, pos) - 1
    idx = max(0, idx)
    return idx + 1, (pos - offsets[idx]) + 1


def _get_line_text(lines: Sequence[str], line_no: int) -> str:
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1]
    return ""


@dataclass(frozen=True)
class SqlToken:
    kind: str  # WORD | QIDENT | STRING | NUMBER | LP | RP | COMMA | SEMI | DOT | DOLLAR_DELIM | OP
    raw: str
    upper: str
    ident: str  # Unquoted identifier for WORD / QIDENT
    quote_char: str  # "" | '"' | "`" | "["
    start: int
    end: int
    line: int
    col: int


def tokenize_sql(sql: str) -> Tuple[List[SqlToken], List[int]]:
    """精确 Span 容错词法扫描：跳过注释/空白但精确保留每个 token 字符位置与引号风格."""
    offsets = _build_line_offsets(sql)
    tokens: List[SqlToken] = []
    open_dollar_tag: Optional[str] = None
    n = len(sql)
    i = 0

    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue

        # 单行注释 --
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i + 2)
            i = n if nl == -1 else nl + 1
            continue

        # MySQL / Doris / StarRocks / TiDB 单行注释 #（仅行首或分号后且非 PG JSONB `#>`/`#>>`/`#-` 运算符）
        if ch == "#" and (i + 1 >= n or sql[i + 1] not in (">", "-")):
            line_start = sql.rfind("\n", 0, i) + 1
            is_line_lead = sql[line_start:i].strip() == ""
            after_semi = bool(tokens and tokens[-1].kind == "SEMI")
            if is_line_lead or after_semi:
                nl = sql.find("\n", i + 1)
                i = n if nl == -1 else nl + 1
                continue

        # 块注释 /* ... */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end_blk = sql.find("*/", i + 2)
            i = n if end_blk == -1 else end_blk + 2
            continue

        # Dollar-Quoting $tag$...$tag$
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql[i : i + 64])
            if m:
                tag = m.group(0)
                ln, cl = _offset_to_line_col(offsets, i)
                if open_dollar_tag is not None and tag == open_dollar_tag:
                    open_dollar_tag = None
                    tokens.append(
                        SqlToken("DOLLAR_DELIM", tag, tag.upper(), "", "", i, i + len(tag), ln, cl)
                    )
                    i += len(tag)
                    continue
                close_pos = sql.find(tag, i + len(tag))
                if close_pos != -1:
                    inner = sql[i + len(tag) : close_pos]
                    prev_tok = tokens[-1] if tokens else None
                    prev_is_proc_intro = bool(
                        prev_tok is not None
                        and prev_tok.kind == "WORD"
                        and (
                            prev_tok.upper in ("AS", "IS", "DO", "PLPGSQL")
                            or (
                                len(tokens) >= 2
                                and tokens[-2].kind == "WORD"
                                and tokens[-2].upper == "LANGUAGE"
                            )
                        )
                    )
                    if (
                        open_dollar_tag is None
                        and prev_is_proc_intro
                        and (inner.count("\n") >= 1 or _BODY_SQL_HINT_RE.search(inner[:2000]))
                    ):
                        open_dollar_tag = tag
                        tokens.append(
                            SqlToken("DOLLAR_DELIM", tag, tag.upper(), "", "", i, i + len(tag), ln, cl)
                        )
                        i += len(tag)
                        continue
                    end_pos = close_pos + len(tag)
                    raw = sql[i:end_pos]
                    tokens.append(SqlToken("STRING", raw, "'__DOLLAR__'", "", "", i, end_pos, ln, cl))
                    i = end_pos
                    continue
                tokens.append(
                    SqlToken("DOLLAR_DELIM", tag, tag.upper(), "", "", i, i + len(tag), ln, cl)
                )
                i += len(tag)
                continue
            ln, cl = _offset_to_line_col(offsets, i)
            tokens.append(SqlToken("OP", "$", "$", "", "", i, i + 1, ln, cl))
            i += 1
            continue

        # 单引号字符串 (兼容 E'...' / N'...' / X'...' / B'...')
        if ch == "'" or (
            ch in ("E", "e", "N", "n", "X", "x", "B", "b")
            and i + 1 < n
            and sql[i + 1] == "'"
            and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            ln, cl = _offset_to_line_col(offsets, i)
            q_start = i + 1 if ch != "'" else i
            j = q_start + 1
            use_bs = ch in ("E", "e")
            while j < n:
                c = sql[j]
                if use_bs and c == "\\":
                    j += 2
                    continue
                if c == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            raw = sql[i:j]
            tokens.append(SqlToken("STRING", raw, "'__STR__'", "", "", i, j, ln, cl))
            i = j
            continue

        # 引用标识符 ("..." / `...` / [...])
        if ch in ('"', "`", "["):
            ln, cl = _offset_to_line_col(offsets, i)
            close_ch = "]" if ch == "[" else ch
            j = i + 1
            buf: List[str] = []
            while j < n:
                c = sql[j]
                if c == close_ch:
                    if close_ch in ('"', "`") and j + 1 < n and sql[j + 1] == close_ch:
                        buf.append(close_ch)
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(c)
                j += 1
            ident = "".join(buf)
            raw = sql[i:j]
            tokens.append(SqlToken("QIDENT", raw, ident.upper(), ident, ch, i, j, ln, cl))
            i = j
            continue

        # 数字字面量
        if ch.isdigit():
            ln, cl = _offset_to_line_col(offsets, i)
            j = i + 1
            while j < n and (sql[j].isdigit() or (sql[j] == "." and j + 1 < n and sql[j + 1].isdigit())):
                j += 1
            if j < n and sql[j] in ("e", "E"):
                k = j + 1
                if k < n and sql[k] in ("+", "-"):
                    k += 1
                if k < n and sql[k].isdigit():
                    j = k + 1
                    while j < n and sql[j].isdigit():
                        j += 1
            raw = sql[i:j]
            tokens.append(SqlToken("NUMBER", raw, raw.upper(), "", "", i, j, ln, cl))
            i = j
            continue

        # 标识符或关键字
        if ch.isalpha() or ch == "_" or ord(ch) > 127:
            ln, cl = _offset_to_line_col(offsets, i)
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in ("_", "$") or ord(sql[j]) > 127):
                j += 1
            raw = sql[i:j]
            tokens.append(SqlToken("WORD", raw, raw.upper(), raw, "", i, j, ln, cl))
            i = j
            continue

        ln, cl = _offset_to_line_col(offsets, i)
        # 标点与运算符
        if ch == "(":
            tokens.append(SqlToken("LP", "(", "(", "", "", i, i + 1, ln, cl))
            i += 1
            continue
        if ch == ")":
            tokens.append(SqlToken("RP", ")", ")", "", "", i, i + 1, ln, cl))
            i += 1
            continue
        if ch == ",":
            tokens.append(SqlToken("COMMA", ",", ",", "", "", i, i + 1, ln, cl))
            i += 1
            continue
        if ch == ";":
            tokens.append(SqlToken("SEMI", ";", ";", "", "", i, i + 1, ln, cl))
            i += 1
            continue
        if ch == ".":
            tokens.append(SqlToken("DOT", ".", ".", "", "", i, i + 1, ln, cl))
            i += 1
            continue

        two = sql[i : i + 2]
        if two in ("::", ":=", "<>", "!=", "<=", ">=", "||", "->", "->>"):
            if sql[i : i + 3] == "->>":
                tokens.append(SqlToken("OP", "->>", "->>", "", "", i, i + 3, ln, cl))
                i += 3
            else:
                tokens.append(SqlToken("OP", two, two, "", "", i, i + 2, ln, cl))
                i += 2
            continue

        tokens.append(SqlToken("OP", ch, ch, "", "", i, i + 1, ln, cl))
        i += 1

    return tokens, offsets


def format_replacement_ident(tok: SqlToken, new_name: str, preserve_quote_style: bool = True) -> str:
    """按原 token 的引号风格格式化目标列名."""
    if not preserve_quote_style or not tok.quote_char:
        return new_name
    if tok.quote_char == '"':
        return '"' + new_name.replace('"', '""') + '"'
    if tok.quote_char == "`":
        return "`" + new_name.replace("`", "``") + "`"
    if tok.quote_char == "[":
        return f"[{new_name}]"
    return new_name


@dataclass
class ColumnChangeMapping:
    connection_id: str
    connection_name: str
    db_type: str
    schema_name: str
    table_name: str
    original_col_name: str
    current_col_name: str
    original_col_type: str = ""
    current_col_type: str = ""
    original_col_comment: str = ""
    current_col_comment: str = ""
    name_changed: bool = False
    type_changed: bool = False
    comment_changed: bool = False
    risk_level: str = "L4"
    highest_risk_rank: int = 4
    std_code: str = ""
    change_ids: List[int] = field(default_factory=list)
    historical_names: Tuple[str, ...] = field(default_factory=tuple)
    before_family: str = "OTHER"
    after_family: str = "OTHER"

    @property
    def table_fqn(self) -> str:
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "connection_name": self.connection_name,
            "db_type": self.db_type,
            "schema_name": self.schema_name,
            "table_name": self.table_name,
            "original_col_name": self.original_col_name,
            "current_col_name": self.current_col_name,
            "historical_names": list(self.historical_names),
            "original_col_type": self.original_col_type,
            "current_col_type": self.current_col_type,
            "original_col_comment": self.original_col_comment,
            "current_col_comment": self.current_col_comment,
            "name_changed": self.name_changed,
            "type_changed": self.type_changed,
            "comment_changed": self.comment_changed,
            "risk_level": self.risk_level,
            "highest_risk_rank": self.highest_risk_rank,
            "std_code": self.std_code,
            "change_ids": list(self.change_ids),
            "before_family": self.before_family,
            "after_family": self.after_family,
        }


@dataclass
class TableChangeSpec:
    schema_name: str
    table_name: str
    # old_col_lower -> ColumnChangeMapping (only entries where name_changed or historical rename)
    rename_map: Dict[str, ColumnChangeMapping] = field(default_factory=dict)
    # old_col_lower -> conflicting mappings from multiple connections (do NOT auto-rename!)
    conflicting_renames: Dict[str, List[ColumnChangeMapping]] = field(default_factory=dict)
    # col_lower (both original & current) -> ColumnChangeMapping where type_changed
    type_map: Dict[str, ColumnChangeMapping] = field(default_factory=dict)
    # col_lower -> ColumnChangeMapping (all changed columns, incl. comment-only)
    all_map: Dict[str, ColumnChangeMapping] = field(default_factory=dict)
    # live current column names after migration (lowercase)
    live_current_cols: Set[str] = field(default_factory=set)

    @property
    def fqn(self) -> str:
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name


class ChangeRegistry:
    """结构化字段变更映射索引，支持表全名/无模式名查找、历史链折叠与元数据列消歧."""

    def __init__(
        self,
        mappings: Sequence[ColumnChangeMapping],
        *,
        table_metadata_cols: Optional[Dict[Tuple[str, str], Set[str]]] = None,
    ) -> None:
        self.mappings: List[ColumnChangeMapping] = list(mappings)
        self.by_schema_table: Dict[Tuple[str, str], TableChangeSpec] = {}
        self.by_table_only: Dict[str, List[TableChangeSpec]] = {}
        self.metadata_cols: Dict[Tuple[str, str], Set[str]] = {}
        self.metadata_cols_by_table: Dict[str, List[Tuple[str, Set[str]]]] = {}
        self.all_target_col_names: Set[str] = set()

        if table_metadata_cols:
            for (sch, tbl), cols in table_metadata_cols.items():
                s_l = (clean_identifier_input(sch) or "public").lower()
                t_l = (clean_identifier_input(tbl) or "").lower()
                cset = {clean_identifier_input(c).lower() for c in cols if c}
                if t_l and cset:
                    self.metadata_cols[(s_l, t_l)] = cset
                    self.metadata_cols_by_table.setdefault(t_l, []).append((s_l, cset))

        self._build_index()

    def _build_index(self) -> None:
        # Step 1: group by (schema, table) and collect live current column names
        grouped: Dict[Tuple[str, str], List[ColumnChangeMapping]] = {}
        for m in self.mappings:
            sch = clean_identifier_input(m.schema_name) or "public"
            tbl = clean_identifier_input(m.table_name)
            if not tbl:
                continue
            key = (sch.lower(), tbl.lower())
            grouped.setdefault(key, []).append(m)
            if m.current_col_name:
                self.all_target_col_names.add(m.current_col_name.lower())

        for (sch_l, tbl_l), items in grouped.items():
            spec = TableChangeSpec(
                schema_name=items[0].schema_name or sch_l,
                table_name=items[0].table_name or tbl_l,
            )
            orig_names: Set[str] = set()
            for m in items:
                if m.current_col_name:
                    spec.live_current_cols.add(m.current_col_name.lower())
                if m.original_col_name:
                    orig_names.add(m.original_col_name.lower())
            if (sch_l, tbl_l) in self.metadata_cols:
                spec.live_current_cols |= self.metadata_cols[(sch_l, tbl_l)]

            for m in items:
                orig_l = (clean_identifier_input(m.original_col_name) or "").lower()
                curr_l = (clean_identifier_input(m.current_col_name) or "").lower()
                if orig_l:
                    spec.all_map[orig_l] = m
                if curr_l:
                    spec.all_map[curr_l] = m
                if m.name_changed and orig_l and curr_l and orig_l != curr_l:
                    if orig_l in spec.conflicting_renames:
                        spec.conflicting_renames[orig_l].append(m)
                    elif orig_l in spec.rename_map and spec.rename_map[orig_l].current_col_name.lower() != curr_l:
                        prev_m = spec.rename_map.pop(orig_l)
                        spec.conflicting_renames[orig_l] = [prev_m, m]
                    else:
                        spec.rename_map[orig_l] = m
                        # Register non-colliding intermediate historical names (e.g. B in A -> B -> C)
                        for hist in m.historical_names:
                            h_l = (clean_identifier_input(hist) or "").lower()
                            if (
                                h_l
                                and h_l != curr_l
                                and h_l != orig_l
                                and h_l not in spec.live_current_cols
                                and h_l not in orig_names
                                and h_l not in spec.rename_map
                                and h_l not in spec.conflicting_renames
                            ):
                                spec.rename_map[h_l] = m
                if m.type_changed:
                    if orig_l:
                        spec.type_map[orig_l] = m
                    if curr_l:
                        spec.type_map[curr_l] = m
                    for hist in m.historical_names:
                        h_l = (clean_identifier_input(hist) or "").lower()
                        if h_l and h_l not in spec.type_map:
                            spec.type_map[h_l] = m

            self.by_schema_table[(sch_l, tbl_l)] = spec
            self.by_table_only.setdefault(tbl_l, []).append(spec)

    @classmethod
    def from_effective_mappings(
        cls,
        effective_mappings: Sequence[Dict[str, Any]],
        *,
        raw_logs_asc: Optional[Sequence[Dict[str, Any]]] = None,
        table_metadata_cols: Optional[Dict[Tuple[str, str], Set[str]]] = None,
    ) -> "ChangeRegistry":
        # Build log id -> log row lookup for intermediate historical names
        log_by_id: Dict[int, Dict[str, Any]] = {}
        for r in raw_logs_asc or []:
            lid = int(r.get("id") or 0)
            if lid > 0:
                log_by_id[lid] = r

        parsed: List[ColumnChangeMapping] = []
        for entry in effective_mappings or []:
            orig_c = clean_identifier_input(str(entry.get("original_col_name") or ""))
            curr_c = clean_identifier_input(str(entry.get("current_col_name") or orig_c))
            sch = clean_identifier_input(str(entry.get("schema_name") or "")) or "public"
            tbl = clean_identifier_input(str(entry.get("table_name") or ""))
            if not tbl or not (orig_c or curr_c):
                continue
            orig_t = str(entry.get("original_col_type") or "")
            curr_t = str(entry.get("current_col_type") or orig_t)
            orig_cmt = str(entry.get("original_col_comment") or "")
            curr_cmt = str(entry.get("current_col_comment") or "")
            name_ch = bool(
                entry.get("name_changed")
                if "name_changed" in entry
                else (orig_c.lower() != curr_c.lower())
            )
            type_ch = bool(
                entry.get("type_changed")
                if "type_changed" in entry
                else (normalize_dws_type(orig_t) != normalize_dws_type(curr_t))
            )
            cmt_ch = bool(
                entry.get("comment_changed")
                if "comment_changed" in entry
                else (orig_cmt != curr_cmt)
            )
            cids = [int(x) for x in (entry.get("change_ids") or []) if int(x) > 0]
            hist_names: List[str] = []
            if orig_c:
                hist_names.append(orig_c)
            for h in entry.get("historical_names") or []:
                hc = clean_identifier_input(str(h or ""))
                if hc and hc.lower() != curr_c.lower() and all(hc.lower() != x.lower() for x in hist_names):
                    hist_names.append(hc)
            for lid in cids:
                row = log_by_id.get(lid)
                if not row:
                    continue
                for cname in (row.get("before_col_name"), row.get("after_col_name")):
                    hc = clean_identifier_input(str(cname or ""))
                    if hc and hc.lower() != curr_c.lower() and all(hc.lower() != x.lower() for x in hist_names):
                        hist_names.append(hc)

            parsed.append(
                ColumnChangeMapping(
                    connection_id=str(entry.get("connection_id") or ""),
                    connection_name=str(entry.get("connection_name") or ""),
                    db_type=str(entry.get("db_type") or "DWS"),
                    schema_name=sch,
                    table_name=tbl,
                    original_col_name=orig_c or curr_c,
                    current_col_name=curr_c or orig_c,
                    original_col_type=orig_t,
                    current_col_type=curr_t,
                    original_col_comment=orig_cmt,
                    current_col_comment=curr_cmt,
                    name_changed=name_ch,
                    type_changed=type_ch,
                    comment_changed=cmt_ch,
                    risk_level=str(entry.get("highest_risk_level") or entry.get("risk_level") or "L4").upper(),
                    highest_risk_rank=int(entry.get("highest_risk_rank") or entry.get("risk_rank") or 4),
                    std_code=str(entry.get("std_code") or ""),
                    change_ids=cids,
                    historical_names=tuple(hist_names),
                    before_family=classify_type_family(orig_t),
                    after_family=classify_type_family(curr_t),
                )
            )
        return cls(parsed, table_metadata_cols=table_metadata_cols)

    @classmethod
    def from_changelog_json(
        cls,
        raw_json: str,
        *,
        table_metadata_cols: Optional[Dict[Tuple[str, str], Set[str]]] = None,
    ) -> "ChangeRegistry":
        try:
            data = json.loads(raw_json)
        except Exception as exc:
            raise ValueError(f"变更台账 JSON 格式非法: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("变更台账 JSON 根节点必须为对象")
        eff = data.get("effective_column_mappings")
        raw_changes = data.get("changes") if isinstance(data.get("changes"), list) else []
        if isinstance(eff, list) and eff:
            return cls.from_effective_mappings(
                eff,
                raw_logs_asc=raw_changes,
                table_metadata_cols=table_metadata_cols,
            )
        if raw_changes:
            # Fold raw changes on the fly
            folded = fold_change_logs_to_mappings(raw_changes)
            return cls.from_effective_mappings(
                folded,
                raw_logs_asc=raw_changes,
                table_metadata_cols=table_metadata_cols,
            )
        return cls([], table_metadata_cols=table_metadata_cols)

    def resolve_table(
        self,
        schema_name: str,
        table_name: str,
        default_schema: str = "",
    ) -> Tuple[Optional[TableChangeSpec], List[TableChangeSpec]]:
        """解析 SQL 中的表引用，返回 (唯一匹配 spec, 若多模式歧义则返回全部候选列表)."""
        tbl_l = (clean_identifier_input(table_name) or "").lower()
        if not tbl_l:
            return None, []
        sch_l = (clean_identifier_input(schema_name) or "").lower()
        if sch_l:
            spec = self.by_schema_table.get((sch_l, tbl_l))
            return spec, ([spec] if spec else [])

        candidates = self.by_table_only.get(tbl_l) or []
        if not candidates:
            return None, []
        if len(candidates) == 1:
            return candidates[0], candidates

        # Prefer default_schema if provided
        def_l = (clean_identifier_input(default_schema) or "").lower()
        if def_l:
            for c in candidates:
                if c.schema_name.lower() == def_l:
                    return c, [c]
        return None, candidates

    def table_has_column(self, schema_name: str, table_name: str, col_name: str) -> Optional[bool]:
        """查询元数据缓存判断某表是否包含指定列（未知返回 None）."""
        t_l = (clean_identifier_input(table_name) or "").lower()
        c_l = (clean_identifier_input(col_name) or "").lower()
        if not t_l or not c_l:
            return None
        s_l = (clean_identifier_input(schema_name) or "").lower()
        if s_l and (s_l, t_l) in self.metadata_cols:
            return c_l in self.metadata_cols[(s_l, t_l)]
        cands = self.metadata_cols_by_table.get(t_l)
        if cands and len(cands) == 1:
            return c_l in cands[0][1]
        return None


def fold_change_logs_to_mappings(logs_asc: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """独立折叠生效变更日志链 (A->B->C => A->C)，自动按 ID 升序归一化，与 changelog_mixin 保持一致."""
    indexed = list(enumerate(logs_asc or []))
    if any(int(r.get("id") or 0) > 0 for _, r in indexed):
        indexed.sort(key=lambda pair: (int(pair[1].get("id") or 0), pair[0]))
    ordered_logs = [r for _, r in indexed]

    table_chains: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in ordered_logs:
        if str(r.get("event_type") or "MIGRATION").upper() != "MIGRATION":
            continue
        if str(r.get("status") or "APPLIED").upper() != "APPLIED":
            continue
        cid = str(r.get("connection_id") or "")
        sch = clean_identifier_input(str(r.get("schema_name") or "")) or "public"
        tbl = clean_identifier_input(str(r.get("table_name") or ""))
        if not tbl:
            continue
        tkey = (cid, sch.lower(), tbl.lower())
        chains = table_chains.setdefault(tkey, [])

        b_name = clean_identifier_input(str(r.get("before_col_name") or "")) or str(r.get("before_col_name") or "")
        a_name = clean_identifier_input(str(r.get("after_col_name") or "")) or str(r.get("after_col_name") or b_name)
        matched = None
        for item in chains:
            if item["current_col_name"].lower() == b_name.lower():
                matched = item
                break
        if matched is not None:
            if b_name and all(b_name.lower() != x.lower() for x in matched["historical_names"]):
                matched["historical_names"].append(b_name)
            matched["current_col_name"] = a_name
            matched["current_col_type"] = str(r.get("after_col_type") or "") or matched["current_col_type"]
            matched["current_col_comment"] = str(r.get("after_col_comment") or "")
            if r.get("std_code"):
                matched["std_code"] = str(r.get("std_code") or "")
            matched["change_ids"].append(int(r.get("id") or 0))
            rank = int(r.get("risk_rank") or 4)
            if rank < matched["highest_risk_rank"]:
                matched["highest_risk_rank"] = rank
                matched["highest_risk_level"] = str(r.get("risk_level") or "L4").upper()
        else:
            chains.append(
                {
                    "connection_id": cid,
                    "connection_name": str(r.get("connection_name") or ""),
                    "db_type": str(r.get("db_type") or "DWS"),
                    "schema_name": sch,
                    "table_name": tbl,
                    "original_col_name": b_name,
                    "original_col_type": str(r.get("before_col_type") or ""),
                    "original_col_comment": str(r.get("before_col_comment") or ""),
                    "current_col_name": a_name,
                    "current_col_type": str(r.get("after_col_type") or ""),
                    "current_col_comment": str(r.get("after_col_comment") or ""),
                    "std_code": str(r.get("std_code") or ""),
                    "highest_risk_level": str(r.get("risk_level") or "L4").upper(),
                    "highest_risk_rank": int(r.get("risk_rank") or 4),
                    "change_ids": [int(r.get("id") or 0)],
                    "historical_names": [b_name] if b_name else [],
                }
            )
    result: List[Dict[str, Any]] = []
    for chains in table_chains.values():
        for entry in chains:
            entry["name_changed"] = (
                entry["original_col_name"].lower() != entry["current_col_name"].lower()
            )
            entry["type_changed"] = normalize_dws_type(
                entry["original_col_type"]
            ) != normalize_dws_type(entry["current_col_type"])
            entry["comment_changed"] = (
                entry["original_col_comment"] != entry["current_col_comment"]
            )
            result.append(entry)
    return result


@dataclass
class RewriteOptions:
    default_schema: str = ""
    cte_alias_policy: str = "propagate"  # propagate | preserve_alias
    rewrite_ddl_types: bool = True
    rewrite_comments: bool = False
    auto_apply_medium_confidence: bool = False
    preserve_quote_style: bool = True


@dataclass
class RewritePatch:
    patch_id: str
    start: int
    end: int
    line: int
    col: int
    end_line: int
    original_text: str
    replacement_text: str
    kind: str
    confidence: str  # HIGH | MEDIUM | LOW
    source: str  # DETERMINISTIC | LLM
    selected: bool
    schema_name: str = ""
    table_name: str = ""
    original_col: str = ""
    current_col: str = ""
    risk_level: str = "L1"
    reason: str = ""
    line_context: str = ""
    phase: int = 1  # 1 = exact span on original_sql; 2 = snippet/span on phase-1 sql

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "start": self.start,
            "end": self.end,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "original_text": self.original_text,
            "replacement_text": self.replacement_text,
            "kind": self.kind,
            "confidence": self.confidence,
            "source": self.source,
            "selected": self.selected,
            "schema_name": self.schema_name,
            "table_name": self.table_name,
            "original_col": self.original_col,
            "current_col": self.current_col,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "line_context": self.line_context,
            "phase": self.phase,
        }


@dataclass
class RewriteAdvisory:
    advisory_id: str
    line: int
    col: int
    start: int
    end: int
    severity: str  # ERROR | WARN | INFO
    category: str
    schema_name: str = ""
    table_name: str = ""
    column_name: str = ""
    before_spec: str = ""
    after_spec: str = ""
    risk_level: str = "L2"
    message: str = ""
    line_context: str = ""
    needs_llm: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "advisory_id": self.advisory_id,
            "line": self.line,
            "col": self.col,
            "start": self.start,
            "end": self.end,
            "severity": self.severity,
            "category": self.category,
            "schema_name": self.schema_name,
            "table_name": self.table_name,
            "column_name": self.column_name,
            "before_spec": self.before_spec,
            "after_spec": self.after_spec,
            "risk_level": self.risk_level,
            "message": self.message,
            "line_context": self.line_context,
            "needs_llm": self.needs_llm,
        }


@dataclass
class _TableBinding:
    alias: str  # lowercase alias or table name
    raw_alias: str
    schema_name: str
    table_name: str
    is_virtual: bool = False  # CTE or derived subquery
    spec: Optional[TableChangeSpec] = None
    ambiguous_specs: List[TableChangeSpec] = field(default_factory=list)
    virtual_rename_map: Dict[str, ColumnChangeMapping] = field(default_factory=dict)
    virtual_type_map: Dict[str, ColumnChangeMapping] = field(default_factory=dict)

    def get_rename(self, col_lower: str) -> Optional[ColumnChangeMapping]:
        if self.is_virtual:
            return self.virtual_rename_map.get(col_lower)
        if self.spec is not None:
            return self.spec.rename_map.get(col_lower)
        return None

    def get_conflict(self, col_lower: str) -> Optional[List[ColumnChangeMapping]]:
        if not self.is_virtual and self.spec is not None:
            return self.spec.conflicting_renames.get(col_lower)
        return None

    def get_type_change(self, col_lower: str) -> Optional[ColumnChangeMapping]:
        if self.is_virtual:
            return self.virtual_type_map.get(col_lower)
        if self.spec is not None:
            return self.spec.type_map.get(col_lower)
        return None

    def all_renames(self) -> Dict[str, ColumnChangeMapping]:
        if self.is_virtual:
            return dict(self.virtual_rename_map)
        if self.spec is not None:
            return dict(self.spec.rename_map)
        return {}

    def all_type_changes(self) -> Dict[str, ColumnChangeMapping]:
        if self.is_virtual:
            return dict(self.virtual_type_map)
        if self.spec is not None:
            return dict(self.spec.type_map)
        return {}


@dataclass
class _CteDef:
    name: str
    explicit_columns: Optional[List[str]]
    rename_map: Dict[str, ColumnChangeMapping] = field(default_factory=dict)
    type_map: Dict[str, ColumnChangeMapping] = field(default_factory=dict)


def _is_ident_tok(tok: SqlToken) -> bool:
    return tok.kind in ("WORD", "QIDENT")


def _is_non_kw_ident(tok: SqlToken) -> bool:
    if tok.kind == "QIDENT":
        return True
    return tok.kind == "WORD" and tok.upper not in _SQL_KEYWORDS


def _find_matching_rp(tokens: Sequence[SqlToken], lp_idx: int, end_idx: int) -> int:
    depth = 0
    for i in range(lp_idx, end_idx):
        if tokens[i].kind == "LP":
            depth += 1
        elif tokens[i].kind == "RP":
            depth -= 1
            if depth == 0:
                return i
    return end_idx - 1


def _split_top_level_by_comma(tokens: Sequence[SqlToken], start: int, end: int) -> List[Tuple[int, int]]:
    parts: List[Tuple[int, int]] = []
    depth = 0
    seg_start = start
    for i in range(start, end):
        k = tokens[i].kind
        if k == "LP":
            depth += 1
        elif k == "RP":
            depth = max(0, depth - 1)
        elif k == "COMMA" and depth == 0:
            if i > seg_start:
                parts.append((seg_start, i))
            seg_start = i + 1
    if end > seg_start:
        parts.append((seg_start, end))
    return parts


def _split_statements(tokens: Sequence[SqlToken]) -> List[Tuple[int, int]]:
    """按 paren_depth == 0 的分号（以及 Oracle/DWS 独立成行的 `/` 块结束符）切分顶层及过程体内部语句."""
    stmts: List[Tuple[int, int]] = []
    depth = 0
    seg_start = 0
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.kind == "LP":
            depth += 1
        elif tok.kind == "RP":
            depth = max(0, depth - 1)
        elif tok.kind == "SEMI" and depth == 0:
            if i > seg_start:
                stmts.append((seg_start, i))
            seg_start = i + 1
        elif (
            depth == 0
            and tok.kind == "OP"
            and tok.raw == "/"
            and (i == 0 or tokens[i - 1].line < tok.line)
            and (i + 1 >= n or tokens[i + 1].line > tok.line)
            and (
                i == 0
                or tokens[i - 1].kind in ("SEMI", "DOLLAR_DELIM")
                or (tokens[i - 1].kind == "WORD" and tokens[i - 1].upper == "END")
            )
        ):
            if i > seg_start:
                stmts.append((seg_start, i))
            seg_start = i + 1
    if seg_start < n:
        stmts.append((seg_start, n))
    return stmts


def _split_set_op_branches(tokens: Sequence[SqlToken], start: int, end: int) -> List[Tuple[int, int]]:
    """切分同级 UNION [ALL] / INTERSECT [ALL] / EXCEPT [ALL] 分支."""
    branches: List[Tuple[int, int]] = []
    depth = 0
    seg_start = start
    i = start
    while i < end:
        k = tokens[i].kind
        if k == "LP":
            depth += 1
            i += 1
            continue
        if k == "RP":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and k == "WORD" and tokens[i].upper in ("UNION", "INTERSECT", "EXCEPT"):
            if i > seg_start:
                branches.append((seg_start, i))
            i += 1
            if i < end and tokens[i].kind == "WORD" and tokens[i].upper in ("ALL", "DISTINCT"):
                i += 1
            seg_start = i
            continue
        i += 1
    if end > seg_start:
        branches.append((seg_start, end))
    return branches


def _read_qualified_table_name(
    tokens: Sequence[SqlToken], pos: int, end: int
) -> Tuple[str, str, int, List[int]]:
    """读取 [catalog.][schema.]table 形式表名，返回 (schema, table, next_pos, consumed_indices)."""
    if pos >= end or not _is_ident_tok(tokens[pos]):
        return "", "", pos, []
    parts: List[str] = [tokens[pos].ident]
    idxs: List[int] = [pos]
    cur = pos + 1
    while cur + 1 < end and tokens[cur].kind == "DOT" and _is_ident_tok(tokens[cur + 1]):
        idxs.append(cur)
        idxs.append(cur + 1)
        parts.append(tokens[cur + 1].ident)
        cur += 2
        if len(parts) >= 3:
            break
    if len(parts) == 1:
        return "", parts[0], cur, idxs
    return parts[-2], parts[-1], cur, idxs


def _read_optional_alias(
    tokens: Sequence[SqlToken], pos: int, end: int
) -> Tuple[str, int, Optional[int]]:
    """读取紧随表名或派生表之后的 [[AS] alias]，返回 (alias_raw, next_pos, alias_token_idx)."""
    if pos >= end:
        return "", pos, None
    tok = tokens[pos]
    if tok.kind == "WORD" and tok.upper == "AS":
        if pos + 1 < end and _is_ident_tok(tokens[pos + 1]):
            return tokens[pos + 1].ident, pos + 2, pos + 1
        return "", pos, None
    if tok.kind == "QIDENT":
        return tok.ident, pos + 1, pos
    if tok.kind == "WORD" and tok.upper not in _CLAUSE_STOP_ALIAS:
        return tok.ident, pos + 1, pos
    return "", pos, None


class _DeterministicSqlRewriter:
    """单脚本确定性重写器执行器."""

    def __init__(
        self,
        sql: str,
        registry: ChangeRegistry,
        options: RewriteOptions,
    ) -> None:
        self.sql = sql
        self.lines = sql.splitlines()
        self.registry = registry
        self.options = options
        self.tokens, self.offsets = tokenize_sql(sql)
        self.active_default_schema = options.default_schema
        self._proc_local_var_ranges: List[Tuple[int, int, Set[str]]] = []

        self._patch_seq = 0
        self._adv_seq = 0
        self.patches: List[RewritePatch] = []
        self._patches_by_span: Dict[Tuple[int, int], RewritePatch] = {}
        self.advisories: List[RewriteAdvisory] = []
        self._adv_dedup: Set[Tuple[int, str, str]] = set()
        self.touched_tables: Set[str] = set()

    def _emit_patch(
        self,
        *,
        start: int,
        end: int,
        replacement_text: str,
        kind: str,
        confidence: str,
        schema_name: str,
        table_name: str,
        original_col: str,
        current_col: str,
        risk_level: str,
        reason: str,
        selected: Optional[bool] = None,
    ) -> Optional[RewritePatch]:
        orig_text = self.sql[start:end]
        if orig_text == replacement_text:
            return None
        key = (start, end)
        if key in self._patches_by_span:
            return self._patches_by_span[key]
        # Avoid overlapping spans with any existing patch
        for (ps, pe) in self._patches_by_span:
            if not (end <= ps or start >= pe):
                return None

        ln, cl = _offset_to_line_col(self.offsets, start)
        end_ln, _ = _offset_to_line_col(self.offsets, max(start, end - 1))
        eff_selected = (
            bool(selected)
            if selected is not None
            else (
                confidence == "HIGH"
                or (confidence == "MEDIUM" and self.options.auto_apply_medium_confidence)
            )
        )
        self._patch_seq += 1
        patch = RewritePatch(
            patch_id=f"p_{self._patch_seq}",
            start=start,
            end=end,
            line=ln,
            col=cl,
            end_line=end_ln,
            original_text=orig_text,
            replacement_text=replacement_text,
            kind=kind,
            confidence=confidence,
            source="DETERMINISTIC",
            selected=eff_selected,
            schema_name=schema_name,
            table_name=table_name,
            original_col=original_col,
            current_col=current_col,
            risk_level=risk_level,
            reason=reason,
            line_context=_get_line_text(self.lines, ln),
            phase=1,
        )
        self.patches.append(patch)
        self._patches_by_span[key] = patch
        if table_name:
            fqn = f"{schema_name}.{table_name}" if schema_name else table_name
            self.touched_tables.add(fqn)
        return patch

    def _emit_advisory(
        self,
        *,
        start: int,
        end: int,
        severity: str,
        category: str,
        schema_name: str,
        table_name: str,
        column_name: str,
        before_spec: str,
        after_spec: str,
        risk_level: str,
        message: str,
        needs_llm: bool = False,
    ) -> None:
        ln, cl = _offset_to_line_col(self.offsets, start)
        dkey = (ln, category, f"{table_name.lower()}.{column_name.lower()}")
        if dkey in self._adv_dedup:
            return
        self._adv_dedup.add(dkey)
        self._adv_seq += 1
        self.advisories.append(
            RewriteAdvisory(
                advisory_id=f"a_{self._adv_seq}",
                line=ln,
                col=cl,
                start=start,
                end=end,
                severity=severity,
                category=category,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                before_spec=before_spec,
                after_spec=after_spec,
                risk_level=risk_level,
                message=message,
                line_context=_get_line_text(self.lines, ln),
                needs_llm=needs_llm,
            )
        )
        if table_name:
            fqn = f"{schema_name}.{table_name}" if schema_name else table_name
            self.touched_tables.add(fqn)

    def run(self) -> Dict[str, Any]:
        if not self.tokens or not self.registry.mappings:
            return self._finalize()

        # Pass 0: collect declared PL/SQL parameters & local variables + %TYPE patches
        self._scan_procedural_declarations()

        # Pass 1: process each statement sequentially
        for st_start, st_end in _split_statements(self.tokens):
            self._process_statement(st_start, st_end)

        # Pass 2: dynamic SQL string radar
        self._scan_dynamic_sql_strings()

        return self._finalize()

    def _is_local_var_at(self, tok_idx: int, ident_lower: str) -> bool:
        for s, e, vset in self._proc_local_var_ranges:
            if s <= tok_idx < e and ident_lower in vset:
                return True
        return False

    def _find_proc_block_end(self, start_idx: int) -> int:
        """定位存储过程/匿名块作用域的结束 token 索引，避免局部变量泄漏到后续语句."""
        toks = self.tokens
        n = len(toks)
        # 1. Check if we are inside or immediately entering a DOLLAR_DELIM block
        open_delim_tag: Optional[str] = None
        for b in range(start_idx - 1, -1, -1):
            if toks[b].kind == "DOLLAR_DELIM":
                # Count occurrences before start_idx to see if it's an unmatched opener
                cnt = sum(1 for k in range(0, start_idx) if toks[k].kind == "DOLLAR_DELIM" and toks[k].raw == toks[b].raw)
                if cnt % 2 == 1:
                    open_delim_tag = toks[b].raw
                break
        if open_delim_tag is not None:
            for k in range(start_idx, n):
                if toks[k].kind == "DOLLAR_DELIM" and toks[k].raw == open_delim_tag:
                    return k + 1
            return n

        k = start_idx
        while k < n:
            if toks[k].kind == "DOLLAR_DELIM":
                tag = toks[k].raw
                for m in range(k + 1, n):
                    if toks[m].kind == "DOLLAR_DELIM" and toks[m].raw == tag:
                        return m + 1
                return n
            if toks[k].kind == "WORD" and toks[k].upper == "BEGIN":
                # Track nested BEGIN ... END blocks (ignoring END IF / END LOOP / END CASE / CASE ... END)
                begin_depth = 1
                case_depth = 0
                m = k + 1
                while m < n and begin_depth > 0:
                    if toks[m].kind == "WORD":
                        u = toks[m].upper
                        if u == "BEGIN":
                            begin_depth += 1
                        elif u == "CASE":
                            case_depth += 1
                        elif u == "END":
                            nxt_u = toks[m + 1].upper if (m + 1 < n and toks[m + 1].kind == "WORD") else ""
                            if nxt_u in ("IF", "LOOP"):
                                m += 2
                                continue
                            if nxt_u == "CASE":
                                case_depth = max(0, case_depth - 1)
                                m += 2
                                continue
                            if case_depth > 0:
                                case_depth -= 1
                            else:
                                begin_depth -= 1
                                if begin_depth == 0:
                                    return min(n, m + 2)
                    m += 1
                return m
            if toks[k].kind == "SEMI":
                return k + 1
            k += 1
        return n

    def _scan_procedural_declarations(self) -> None:
        toks = self.tokens
        n = len(toks)
        i = 0
        while i < n:
            t = toks[i]
            # CREATE [OR REPLACE] PROCEDURE/FUNCTION name (p1 type, ...)
            if t.kind == "WORD" and t.upper in ("PROCEDURE", "FUNCTION") and i + 1 < n:
                j = i + 1
                # skip name (possibly schema.name)
                _, _, j, _ = _read_qualified_table_name(toks, j, n)
                if j < n and toks[j].kind == "LP":
                    rp = _find_matching_rp(toks, j, n)
                    param_vars: Set[str] = set()
                    for ps, pe in _split_top_level_by_comma(toks, j + 1, rp):
                        k = ps
                        while k < pe and toks[k].kind == "WORD" and toks[k].upper in ("IN", "OUT", "INOUT", "VARIADIC"):
                            k += 1
                        if k < pe and _is_non_kw_ident(toks[k]):
                            param_vars.add(toks[k].ident.lower())
                    if param_vars:
                        blk_end = self._find_proc_block_end(rp + 1)
                        self._proc_local_var_ranges.append((i, blk_end, param_vars))
                    i = rp + 1
                    continue

            # DECLARE ... BEGIN
            if t.kind == "WORD" and t.upper == "DECLARE":
                j = i + 1
                decl_vars: Set[str] = set()
                while j < n and not (toks[j].kind == "WORD" and toks[j].upper == "BEGIN"):
                    # Each var decl starts after DECLARE or SEMI
                    if _is_non_kw_ident(toks[j]):
                        decl_vars.add(toks[j].ident.lower())
                    # Advance to next SEMI or BEGIN
                    while j < n and toks[j].kind != "SEMI" and not (
                        toks[j].kind == "WORD" and toks[j].upper == "BEGIN"
                    ):
                        if (
                            toks[j].kind == "OP"
                            and toks[j].raw == "%"
                            and j + 1 < n
                            and toks[j + 1].kind == "WORD"
                            and toks[j + 1].upper == "TYPE"
                        ):
                            self._check_percent_type_ref(j)
                        j += 1
                    if j < n and toks[j].kind == "SEMI":
                        j += 1
                if decl_vars:
                    blk_end = self._find_proc_block_end(j)
                    self._proc_local_var_ranges.append((i, blk_end, decl_vars))
                i = j
                continue
            i += 1

    def _check_percent_type_ref(self, pct_idx: int) -> None:
        """处理 PL/SQL 中的 [schema.]table.old_col%TYPE 引用."""
        toks = self.tokens
        if pct_idx < 3:
            return
        col_tok = toks[pct_idx - 1]
        if not _is_ident_tok(col_tok) or toks[pct_idx - 2].kind != "DOT":
            return
        tbl_tok = toks[pct_idx - 3]
        if not _is_ident_tok(tbl_tok):
            return
        sch_name = ""
        if pct_idx >= 5 and toks[pct_idx - 4].kind == "DOT" and _is_ident_tok(toks[pct_idx - 5]):
            sch_name = toks[pct_idx - 5].ident
        spec, _ = self.registry.resolve_table(sch_name, tbl_tok.ident, self.active_default_schema)
        if spec is None:
            return
        m = spec.rename_map.get(col_tok.ident.lower())
        if m is not None:
            self._emit_patch(
                start=col_tok.start,
                end=col_tok.end,
                replacement_text=format_replacement_ident(
                    col_tok, m.current_col_name, self.options.preserve_quote_style
                ),
                kind="RENAME_QUALIFIED",
                confidence="HIGH",
                schema_name=spec.schema_name,
                table_name=spec.table_name,
                original_col=m.original_col_name,
                current_col=m.current_col_name,
                risk_level=m.risk_level,
                reason=f"PL/SQL %TYPE 引用同步改名：{spec.fqn}.{col_tok.ident} → {m.current_col_name}",
            )

    def _strip_procedural_prefix(self, start: int, end: int) -> int:
        """剥离存储过程控制流前导词（BEGIN / ELSE / LOOP / IF ... THEN / $$）以定位内部 SQL."""
        toks = self.tokens
        cur = start
        while cur < end:
            t = toks[cur]
            if t.kind == "DOLLAR_DELIM":
                cur += 1
                continue
            if t.kind == "WORD" and t.upper in ("BEGIN", "ELSE", "LOOP", "DO", "PERFORM", "RETURN"):
                cur += 1
                continue
            if t.kind == "WORD" and t.upper in ("IF", "ELSIF", "ELSEIF"):
                # find matching THEN
                k = cur + 1
                depth = 0
                found_then = -1
                while k < end:
                    if toks[k].kind == "LP":
                        depth += 1
                    elif toks[k].kind == "RP":
                        depth = max(0, depth - 1)
                    elif depth == 0 and toks[k].kind == "WORD" and toks[k].upper == "THEN":
                        found_then = k
                        break
                    k += 1
                if found_then != -1:
                    cur = found_then + 1
                    continue
                break
            if t.kind == "WORD" and t.upper == "FOR":
                # FOR rec IN (SELECT ...) LOOP <stmt>
                k = cur + 1
                depth = 0
                while k < end:
                    if toks[k].kind == "LP":
                        rp = _find_matching_rp(toks, k, end)
                        if k + 1 < rp and toks[k + 1].kind == "WORD" and toks[k + 1].upper in ("SELECT", "WITH"):
                            self._process_query_or_dml(k + 1, rp, parent_scope=None)
                        k = rp + 1
                        continue
                    if toks[k].kind == "WORD" and toks[k].upper == "LOOP":
                        cur = k + 1
                        break
                    k += 1
                else:
                    break
                continue
            break
        return cur

    def _process_statement(self, start: int, end: int) -> None:
        toks = self.tokens
        cur = self._strip_procedural_prefix(start, end)
        if cur >= end:
            return

        # Track SET search_path / USE schema
        if self._try_handle_session_schema(cur, end):
            return

        t0 = toks[cur]
        if t0.kind != "WORD":
            # Scan any embedded subqueries
            self._scan_embedded_subqueries(cur, end, parent_scope=None)
            return

        kw = t0.upper
        if kw == "COMMENT":
            self._process_comment_on_column(cur, end)
            return
        if kw == "CREATE":
            self._process_create_statement(cur, end)
            return
        if kw == "ALTER":
            self._process_alter_table(cur, end)
            return
        if kw in ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE"):
            self._process_query_or_dml(cur, end, parent_scope=None)
            return

        # Fallback: e.g. v_cnt := (SELECT ...); or EXECUTE ...
        self._scan_embedded_subqueries(cur, end, parent_scope=None)

    def _try_handle_session_schema(self, start: int, end: int) -> bool:
        toks = self.tokens
        if toks[start].kind != "WORD":
            return False
        u0 = toks[start].upper
        if u0 == "USE" and start + 1 < end and _is_ident_tok(toks[start + 1]):
            self.active_default_schema = toks[start + 1].ident
            return True
        if u0 == "SET" and start + 2 < end and toks[start + 1].kind == "WORD":
            u1 = toks[start + 1].upper
            if u1 in ("SEARCH_PATH", "CURRENT_SCHEMA"):
                k = start + 2
                if k < end and (toks[k].upper == "TO" or toks[k].raw == "="):
                    k += 1
                if k < end and _is_ident_tok(toks[k]):
                    self.active_default_schema = toks[k].ident
                    return True
        return False

    def _process_comment_on_column(self, start: int, end: int) -> None:
        """处理 COMMENT ON COLUMN [schema.]table.col IS '...'."""
        toks = self.tokens
        if start + 3 >= end:
            return
        if not (toks[start + 1].upper == "ON" and toks[start + 2].upper == "COLUMN"):
            return
        # Read parts separated by DOT
        pos = start + 3
        parts: List[SqlToken] = []
        if pos < end and _is_ident_tok(toks[pos]):
            parts.append(toks[pos])
            pos += 1
            while pos + 1 < end and toks[pos].kind == "DOT" and _is_ident_tok(toks[pos + 1]):
                parts.append(toks[pos + 1])
                pos += 2
        if len(parts) < 2:
            return
        col_tok = parts[-1]
        tbl_tok = parts[-2]
        sch_name = parts[-3].ident if len(parts) >= 3 else ""
        spec, cands = self.registry.resolve_table(sch_name, tbl_tok.ident, self.active_default_schema)
        if spec is None:
            if len(cands) > 1:
                self._emit_advisory(
                    start=tbl_tok.start,
                    end=col_tok.end,
                    severity="WARN",
                    category="AMBIGUOUS_TABLE",
                    schema_name="",
                    table_name=tbl_tok.ident,
                    column_name=col_tok.ident,
                    before_spec="",
                    after_spec="",
                    risk_level="L1",
                    message=f"COMMENT ON COLUMN 涉及多模式同名表 {tbl_tok.ident}（{', '.join(c.fqn for c in cands)}），请指定默认 Schema 或人工确认",
                    needs_llm=True,
                )
            return

        m = spec.rename_map.get(col_tok.ident.lower())
        if m is not None:
            self._emit_patch(
                start=col_tok.start,
                end=col_tok.end,
                replacement_text=format_replacement_ident(
                    col_tok, m.current_col_name, self.options.preserve_quote_style
                ),
                kind="RENAME_DDL_COL",
                confidence="HIGH",
                schema_name=spec.schema_name,
                table_name=spec.table_name,
                original_col=m.original_col_name,
                current_col=m.current_col_name,
                risk_level=m.risk_level,
                reason=f"COMMENT ON COLUMN 字段改名：{spec.fqn}.{col_tok.ident} → {m.current_col_name}",
            )
        m_all = m or spec.all_map.get(col_tok.ident.lower())
        if (
            self.options.rewrite_comments
            and m_all is not None
            and m_all.comment_changed
            and m_all.current_col_comment
        ):
            if pos + 1 < end and toks[pos].upper == "IS" and toks[pos + 1].kind == "STRING":
                str_tok = toks[pos + 1]
                new_lit = "'" + m_all.current_col_comment.replace("'", "''") + "'"
                self._emit_patch(
                    start=str_tok.start,
                    end=str_tok.end,
                    replacement_text=new_lit,
                    kind="COMMENT_UPDATE",
                    confidence="HIGH",
                    schema_name=spec.schema_name,
                    table_name=spec.table_name,
                    original_col=m_all.original_col_name,
                    current_col=m_all.current_col_name,
                    risk_level="L4",
                    reason=f"同步更新字段注释：{spec.fqn}.{m_all.current_col_name}",
                )

    def _process_create_statement(self, start: int, end: int) -> None:
        toks = self.tokens
        i = start + 1
        # Skip OR REPLACE / TEMP / TEMPORARY / GLOBAL / LOCAL / UNLOGGED / EXTERNAL / UNIQUE / MATERIALIZED
        while i < end and toks[i].kind == "WORD" and toks[i].upper in (
            "OR", "REPLACE", "TEMP", "TEMPORARY", "GLOBAL", "LOCAL", "UNLOGGED",
            "EXTERNAL", "UNIQUE", "MATERIALIZED",
        ):
            i += 1
        if i >= end or toks[i].kind != "WORD":
            return

        obj_kw = toks[i].upper
        i += 1

        if obj_kw in ("PROCEDURE", "FUNCTION"):
            # Unwrap procedure body starting after AS / IS / $$
            k = i
            while k < end:
                if toks[k].kind == "DOLLAR_DELIM":
                    self._process_statement(k + 1, end)
                    return
                if toks[k].kind == "WORD" and toks[k].upper == "BEGIN":
                    self._process_statement(k + 1, end)
                    return
                k += 1
            return

        if obj_kw == "VIEW":
            # CREATE VIEW [s.]v [(c1, c2)] AS <SELECT/WITH>
            k = i
            while k < end and not (toks[k].kind == "WORD" and toks[k].upper == "AS"):
                k += 1
            if k + 1 < end:
                self._process_query_or_dml(k + 1, end, parent_scope=None)
            return

        if obj_kw == "INDEX":
            # CREATE [UNIQUE] INDEX [IF NOT EXISTS] idx ON [s.]tbl [USING btree] (c1, LOWER(c2)) [INCLUDE (...)] [WHERE ...]
            k = i
            while k < end and not (toks[k].kind == "WORD" and toks[k].upper == "ON"):
                k += 1
            if k + 1 >= end:
                return
            sch, tbl, nxt, _ = _read_qualified_table_name(toks, k + 1, end)
            spec, _ = self.registry.resolve_table(sch, tbl, self.active_default_schema)
            if spec is None:
                return
            idx_binding = _TableBinding(
                alias=spec.table_name.lower(),
                raw_alias=spec.table_name,
                schema_name=spec.schema_name,
                table_name=spec.table_name,
                spec=spec,
            )
            idx_scope_ctx = {
                "parent": None,
                "ctes": {},
                "tables": [idx_binding],
                "alias_map": {idx_binding.alias: idx_binding},
            }
            while nxt < end and toks[nxt].kind != "LP":
                nxt += 1
            if nxt < end and toks[nxt].kind == "LP":
                rp = _find_matching_rp(toks, nxt, end)
                self._resolve_scope_columns(
                    nxt + 1, rp, scope_ctx=idx_scope_ctx, protected=set(), child_ranges=[]
                )
                tail = rp + 1
                while tail < end:
                    tk = toks[tail]
                    if tk.kind == "WORD" and tk.upper == "INCLUDE" and tail + 1 < end and toks[tail + 1].kind == "LP":
                        inc_rp = _find_matching_rp(toks, tail + 1, end)
                        self._rewrite_column_list_for_spec(tail + 2, inc_rp, spec, "CREATE INDEX INCLUDE")
                        tail = inc_rp + 1
                        continue
                    if tk.kind == "WORD" and tk.upper == "WHERE":
                        self._resolve_scope_columns(
                            tail + 1, end, scope_ctx=idx_scope_ctx, protected=set(), child_ranges=[]
                        )
                        break
                    tail += 1
            return

        if obj_kw == "TABLE":
            # Skip IF NOT EXISTS
            if (
                i + 2 < end
                and toks[i].upper == "IF"
                and toks[i + 1].upper == "NOT"
                and toks[i + 2].upper == "EXISTS"
            ):
                i += 3
            sch, tbl, nxt, _ = _read_qualified_table_name(toks, i, end)
            spec, _ = self.registry.resolve_table(sch, tbl, self.active_default_schema)

            # Check CTAS: CREATE TABLE t AS SELECT ...
            k = nxt
            if k < end and toks[k].kind == "LP":
                rp = _find_matching_rp(toks, k, end)
                # Look ahead after rp for AS SELECT/WITH
                look = rp + 1
                while look < end and toks[look].kind == "WORD" and toks[look].upper in ("WITH", "TABLESPACE"):
                    if toks[look].upper == "WITH" and look + 1 < end and toks[look + 1].kind == "LP":
                        look = _find_matching_rp(toks, look + 1, end) + 1
                    else:
                        break
                if look < end and toks[look].kind == "WORD" and toks[look].upper == "AS":
                    if spec is not None:
                        self._rewrite_column_list_for_spec(k + 1, rp, spec, "CTAS 列清单")
                    self._process_query_or_dml(look + 1, end, parent_scope=None)
                    return

                # Regular CREATE TABLE (...)
                if spec is not None:
                    self._process_create_table_body(k + 1, rp, spec)
                    self._process_create_table_tail(rp + 1, end, spec)
                else:
                    # 即使子表本身未改名，也需同步其中 FOREIGN KEY ... REFERENCES <已改名父表> (col) 引用
                    self._rewrite_references_clauses(k + 1, rp)
                return

            # CREATE TABLE t AS SELECT ... / CREATE TABLE t AS (SELECT ...)
            while k < end:
                if toks[k].kind == "WORD" and toks[k].upper == "AS":
                    if k + 1 < end and (
                        (toks[k + 1].kind == "WORD" and toks[k + 1].upper in ("SELECT", "WITH"))
                        or toks[k + 1].kind == "LP"
                    ):
                        self._process_query_or_dml(k + 1, end, parent_scope=None)
                        return
                k += 1

    def _process_create_table_body(self, start: int, end: int, spec: TableChangeSpec) -> None:
        toks = self.tokens
        tbl_binding = _TableBinding(
            alias=spec.table_name.lower(),
            raw_alias=spec.table_name,
            schema_name=spec.schema_name,
            table_name=spec.table_name,
            spec=spec,
        )
        ddl_scope_ctx = {
            "parent": None,
            "ctes": {},
            "tables": [tbl_binding],
            "alias_map": {tbl_binding.alias: tbl_binding},
        }

        def _rewrite_inline_expr_constraints(scan_s: int, scan_e: int) -> None:
            k = scan_s
            while k < scan_e:
                tk = toks[k]
                if tk.kind == "WORD" and tk.upper == "CHECK" and k + 1 < scan_e and toks[k + 1].kind == "LP":
                    rp = _find_matching_rp(toks, k + 1, scan_e)
                    self._resolve_scope_columns(
                        k + 2, rp, scope_ctx=ddl_scope_ctx, protected=set(), child_ranges=[]
                    )
                    k = rp + 1
                    continue
                if tk.kind == "WORD" and tk.upper == "GENERATED":
                    # GENERATED [ALWAYS] AS (expr) [STORED|VIRTUAL]
                    m = k + 1
                    while m < scan_e and toks[m].kind != "LP":
                        if toks[m].kind == "WORD" and toks[m].upper == "IDENTITY":
                            m = scan_e
                            break
                        m += 1
                    if m < scan_e and toks[m].kind == "LP":
                        rp = _find_matching_rp(toks, m, scan_e)
                        self._resolve_scope_columns(
                            m + 1, rp, scope_ctx=ddl_scope_ctx, protected=set(), child_ranges=[]
                        )
                        k = rp + 1
                        continue
                k += 1

        for seg_s, seg_e in _split_top_level_by_comma(toks, start, end):
            if seg_s >= seg_e:
                continue
            t0 = toks[seg_s]
            # Table-level constraints: PRIMARY KEY (col), UNIQUE (col), FOREIGN KEY (col), CHECK (expr), REFERENCES tbl (col)
            if t0.kind == "WORD" and t0.upper in _DDL_TABLE_CONSTRAINT_STARTS:
                k = seg_s
                while k < seg_e:
                    if toks[k].kind == "WORD" and toks[k].upper in ("KEY", "UNIQUE", "HASH", "RANGE", "LIST"):
                        if k + 1 < seg_e and toks[k + 1].kind == "LP":
                            rp = _find_matching_rp(toks, k + 1, seg_e)
                            self._rewrite_column_list_for_spec(k + 2, rp, spec, "CREATE TABLE 约束列")
                            k = rp + 1
                            continue
                    if toks[k].kind == "WORD" and toks[k].upper == "CHECK":
                        if k + 1 < seg_e and toks[k + 1].kind == "LP":
                            rp = _find_matching_rp(toks, k + 1, seg_e)
                            self._resolve_scope_columns(
                                k + 2, rp, scope_ctx=ddl_scope_ctx, protected=set(), child_ranges=[]
                            )
                            k = rp + 1
                            continue
                    if toks[k].kind == "WORD" and toks[k].upper == "REFERENCES":
                        k = self._rewrite_single_references_at(k, seg_e)
                        continue
                    k += 1
                continue

            if not _is_ident_tok(t0):
                continue
            col_lower = t0.ident.lower()
            m_ren = spec.rename_map.get(col_lower)
            if m_ren is not None:
                self._emit_patch(
                    start=t0.start,
                    end=t0.end,
                    replacement_text=format_replacement_ident(
                        t0, m_ren.current_col_name, self.options.preserve_quote_style
                    ),
                    kind="RENAME_DDL_COL",
                    confidence="HIGH",
                    schema_name=spec.schema_name,
                    table_name=spec.table_name,
                    original_col=m_ren.original_col_name,
                    current_col=m_ren.current_col_name,
                    risk_level=m_ren.risk_level,
                    reason=f"CREATE TABLE 字段定义改名：{spec.fqn}.{t0.ident} → {m_ren.current_col_name}",
                )

            m_type = m_ren if (m_ren and m_ren.type_changed) else spec.type_map.get(col_lower)
            if self.options.rewrite_ddl_types and m_type is not None and m_type.current_col_type:
                # Extract type token span starting at seg_s + 1
                ts = seg_s + 1
                te = ts
                while te < seg_e:
                    tk = toks[te]
                    if tk.kind == "WORD" and tk.upper in ("WITH", "WITHOUT"):
                        if (
                            te + 2 < seg_e
                            and toks[te + 1].kind == "WORD"
                            and toks[te + 1].upper == "TIME"
                            and toks[te + 2].kind == "WORD"
                            and toks[te + 2].upper == "ZONE"
                        ):
                            te += 3
                            continue
                    if tk.kind == "WORD" and tk.upper in _DDL_COL_CONSTRAINT_STOPS:
                        break
                    if tk.kind == "LP":
                        rp = _find_matching_rp(toks, te, seg_e)
                        te = rp + 1
                        continue
                    te += 1
                if te > ts:
                    span_start = toks[ts].start
                    span_end = toks[te - 1].end
                    cur_ddl_type = self.sql[span_start:span_end].strip()
                    if normalize_dws_type(cur_ddl_type) != normalize_dws_type(m_type.current_col_type):
                        self._emit_patch(
                            start=span_start,
                            end=span_end,
                            replacement_text=m_type.current_col_type,
                            kind="DDL_TYPE_UPDATE",
                            confidence="HIGH",
                            schema_name=spec.schema_name,
                            table_name=spec.table_name,
                            original_col=m_type.original_col_name,
                            current_col=m_type.current_col_name,
                            risk_level=m_type.risk_level,
                            reason=(
                                f"CREATE TABLE 字段类型同步更新：{spec.fqn}.{m_type.current_col_name} "
                                f"({cur_ddl_type} → {m_type.current_col_type})"
                            ),
                        )

            # Inline MySQL/Doris/StarRocks column COMMENT '...' sync when rewrite_comments=True
            if self.options.rewrite_comments:
                m_cmt = m_ren or spec.all_map.get(col_lower)
                if m_cmt is not None and m_cmt.comment_changed and m_cmt.current_col_comment:
                    ck = seg_s + 1
                    while ck + 1 < seg_e:
                        if toks[ck].kind == "LP":
                            ck = _find_matching_rp(toks, ck, seg_e) + 1
                            continue
                        if (
                            toks[ck].kind == "WORD"
                            and toks[ck].upper == "COMMENT"
                            and toks[ck + 1].kind == "STRING"
                        ):
                            cmt_tok = toks[ck + 1]
                            new_lit = "'" + m_cmt.current_col_comment.replace("'", "''") + "'"
                            if cmt_tok.raw != new_lit:
                                self._emit_patch(
                                    start=cmt_tok.start,
                                    end=cmt_tok.end,
                                    replacement_text=new_lit,
                                    kind="COMMENT_UPDATE",
                                    confidence="HIGH",
                                    schema_name=spec.schema_name,
                                    table_name=spec.table_name,
                                    original_col=m_cmt.original_col_name,
                                    current_col=m_cmt.current_col_name,
                                    risk_level="L4",
                                    reason=(
                                        f"CREATE TABLE 行内字段注释同步更新：{spec.fqn}.{m_cmt.current_col_name}"
                                    ),
                                )
                            break
                        ck += 1

            # Inline column-level CHECK (...), GENERATED [ALWAYS] AS (...) and REFERENCES tbl (col)
            _rewrite_inline_expr_constraints(seg_s + 1, seg_e)
            self._rewrite_references_clauses(seg_s + 1, seg_e)

    def _rewrite_single_references_at(self, ref_idx: int, end: int) -> int:
        """从 REFERENCES 关键字位置起解析 `REFERENCES [schema.]tbl (col1, ...)` 并同步父表已改名列."""
        toks = self.tokens
        if ref_idx + 1 >= end:
            return ref_idx + 1
        r_sch, r_tbl, nxt, _ = _read_qualified_table_name(toks, ref_idx + 1, end)
        if not r_tbl:
            return ref_idx + 1
        ref_spec, _ = self.registry.resolve_table(r_sch, r_tbl, self.active_default_schema)
        if nxt < end and toks[nxt].kind == "LP":
            rp = _find_matching_rp(toks, nxt, end)
            if ref_spec is not None:
                self._rewrite_column_list_for_spec(nxt + 1, rp, ref_spec, "FOREIGN KEY REFERENCES")
            return rp + 1
        return nxt

    def _rewrite_references_clauses(self, start: int, end: int) -> None:
        toks = self.tokens
        k = start
        while k < end:
            if toks[k].kind == "WORD" and toks[k].upper == "REFERENCES":
                k = self._rewrite_single_references_at(k, end)
                continue
            k += 1

    def _process_create_table_tail(self, start: int, end: int, spec: TableChangeSpec) -> None:
        """处理 DISTRIBUTE BY HASH(...) / PARTITION BY RANGE|LIST|HASH(...) / CLUSTERED BY (...)."""
        toks = self.tokens
        i = start
        while i < end:
            if toks[i].kind == "WORD" and toks[i].upper in ("DISTRIBUTE", "PARTITION", "CLUSTERED"):
                k = i + 1
                while k < end and toks[k].kind != "LP":
                    if toks[k].kind == "WORD" and toks[k].upper in ("TABLESPACE", "WITH", "STORED", "LOCATION"):
                        break
                    k += 1
                if k < end and toks[k].kind == "LP":
                    rp = _find_matching_rp(toks, k, end)
                    self._rewrite_column_list_for_spec(k + 1, rp, spec, f"{toks[i].upper} BY 列")
                    i = rp + 1
                    continue
            i += 1

    def _process_alter_table(self, start: int, end: int) -> None:
        toks = self.tokens
        if start + 2 >= end or toks[start + 1].upper != "TABLE":
            return
        i = start + 2
        if i + 1 < end and toks[i].upper == "IF" and toks[i + 1].upper == "EXISTS":
            i += 2
        if i < end and toks[i].upper == "ONLY":
            i += 1
        sch, tbl, nxt, _ = _read_qualified_table_name(toks, i, end)
        spec, _ = self.registry.resolve_table(sch, tbl, self.active_default_schema)
        if spec is None:
            return
        # Flag ALTER TABLE touching already-migrated columns as advisory instead of blindly rewriting DDL migrations
        for k in range(nxt, end):
            if _is_non_kw_ident(toks[k]):
                m = spec.rename_map.get(toks[k].ident.lower())
                if m is not None:
                    self._emit_advisory(
                        start=toks[k].start,
                        end=toks[k].end,
                        severity="INFO",
                        category="ALTER_TABLE_REVIEW",
                        schema_name=spec.schema_name,
                        table_name=spec.table_name,
                        column_name=toks[k].ident,
                        before_spec=f"{m.original_col_name} {m.original_col_type}",
                        after_spec=f"{m.current_col_name} {m.current_col_type}",
                        risk_level=m.risk_level,
                        message=(
                            f"ALTER TABLE 语句引用了已改名字段 {spec.fqn}.{toks[k].ident} "
                            f"（最新为 {m.current_col_name}），请确认该脚本是否已过时或需人工调整"
                        ),
                        needs_llm=False,
                    )

    def _rewrite_column_list_for_spec(
        self, start: int, end: int, spec: TableChangeSpec, ctx_label: str
    ) -> None:
        toks = self.tokens
        for seg_s, seg_e in _split_top_level_by_comma(toks, start, end):
            if seg_s >= seg_e:
                continue
            col_tok = toks[seg_s]
            if not _is_ident_tok(col_tok):
                continue
            col_l = col_tok.ident.lower()
            m_ren = spec.rename_map.get(col_l)
            if m_ren is not None:
                self._emit_patch(
                    start=col_tok.start,
                    end=col_tok.end,
                    replacement_text=format_replacement_ident(
                        col_tok, m_ren.current_col_name, self.options.preserve_quote_style
                    ),
                    kind="RENAME_TARGET_COL",
                    confidence="HIGH",
                    schema_name=spec.schema_name,
                    table_name=spec.table_name,
                    original_col=m_ren.original_col_name,
                    current_col=m_ren.current_col_name,
                    risk_level=m_ren.risk_level,
                    reason=f"{ctx_label} 精确命中：{spec.fqn}.{col_tok.ident} → {m_ren.current_col_name}",
                )
            m_type = m_ren if (m_ren and m_ren.type_changed) else spec.type_map.get(col_l)
            if m_type is not None and m_type.risk_level in ("L2", "L3") and "INSERT" in ctx_label:
                self._emit_advisory(
                    start=col_tok.start,
                    end=col_tok.end,
                    severity="WARN",
                    category="PRECISION_SHRINK_WRITE" if m_type.risk_level == "L3" else "TYPE_CAST_REVIEW",
                    schema_name=spec.schema_name,
                    table_name=spec.table_name,
                    column_name=m_type.current_col_name,
                    before_spec=m_type.original_col_type,
                    after_spec=m_type.current_col_type,
                    risk_level=m_type.risk_level,
                    message=(
                        f"INSERT 目标列 {spec.fqn}.{m_type.current_col_name} 类型已由 "
                        f"{m_type.original_col_type} 变更为 {m_type.current_col_type}（{m_type.risk_level}），"
                        "请核查上游写入值是否存在截断或隐式转换失败风险"
                    ),
                    needs_llm=(m_type.risk_level == "L2"),
                )

    def _scan_embedded_subqueries(
        self, start: int, end: int, parent_scope: Optional[Dict[str, Any]]
    ) -> None:
        toks = self.tokens
        i = start
        while i < end:
            if toks[i].kind == "LP":
                rp = _find_matching_rp(toks, i, end)
                if i + 1 < rp and toks[i + 1].kind == "WORD" and toks[i + 1].upper in ("SELECT", "WITH"):
                    self._process_query_or_dml(i + 1, rp, parent_scope=parent_scope)
                    i = rp + 1
                    continue
            i += 1

    def _process_query_or_dml(
        self,
        start: int,
        end: int,
        parent_scope: Optional[Dict[str, Any]],
        *,
        recursive_cte_name: str = "",
    ) -> Tuple[Dict[str, ColumnChangeMapping], Dict[str, ColumnChangeMapping]]:
        """解析 WITH / SELECT / INSERT / UPDATE / DELETE / MERGE，返回对外暴露的 (rename_map, type_map)."""
        toks = self.tokens
        if start >= end:
            return {}, {}

        # Inherit CTEs from parent scope
        ctes: Dict[str, _CteDef] = dict(parent_scope["ctes"]) if parent_scope else {}
        cur = start

        # Parse leading WITH [RECURSIVE] cte1 [(cols)] AS (...), cte2 AS (...)
        is_recursive = False
        if cur < end and toks[cur].kind == "WORD" and toks[cur].upper == "WITH":
            cur += 1
            if cur < end and toks[cur].kind == "WORD" and toks[cur].upper == "RECURSIVE":
                is_recursive = True
                cur += 1
            while cur < end and _is_ident_tok(toks[cur]):
                cte_name = toks[cur].ident.lower()
                cur += 1
                explicit_cols: Optional[List[str]] = None
                if cur < end and toks[cur].kind == "LP":
                    rp_cols = _find_matching_rp(toks, cur, end)
                    explicit_cols = [
                        toks[s].ident.lower()
                        for s, _e in _split_top_level_by_comma(toks, cur + 1, rp_cols)
                        if s < rp_cols and _is_ident_tok(toks[s])
                    ]
                    cur = rp_cols + 1
                if cur < end and toks[cur].kind == "WORD" and toks[cur].upper == "AS":
                    cur += 1
                # Optional MATERIALIZED / NOT MATERIALIZED
                if cur < end and toks[cur].kind == "WORD" and toks[cur].upper == "NOT":
                    cur += 1
                if cur < end and toks[cur].kind == "WORD" and toks[cur].upper == "MATERIALIZED":
                    cur += 1
                if cur >= end or toks[cur].kind != "LP":
                    break
                rp_body = _find_matching_rp(toks, cur, end)
                cte_parent = {
                    "parent": parent_scope,
                    "ctes": ctes,
                    "alias_map": parent_scope["alias_map"] if parent_scope else {},
                }
                c_ren, c_typ = self._process_query_or_dml(
                    cur + 1,
                    rp_body,
                    parent_scope=cte_parent,
                    recursive_cte_name=(cte_name if is_recursive and explicit_cols is None else ""),
                )
                if explicit_cols is not None:
                    # Explicit CTE column list fixes output column names; do not rename CTE output columns
                    ctes[cte_name] = _CteDef(
                        name=cte_name,
                        explicit_columns=explicit_cols,
                        rename_map={},
                        type_map={},
                    )
                else:
                    ctes[cte_name] = _CteDef(
                        name=cte_name,
                        explicit_columns=None,
                        rename_map=c_ren,
                        type_map=c_typ,
                    )
                cur = rp_body + 1
                if cur < end and toks[cur].kind == "COMMA":
                    cur += 1
                    continue
                break

        if cur >= end:
            return {}, {}

        # Handle set operations (UNION / INTERSECT / EXCEPT) on SELECT
        if toks[cur].kind == "WORD" and toks[cur].upper == "SELECT":
            branches = _split_set_op_branches(toks, cur, end)
            if len(branches) > 1:
                # Split trailing outer ORDER BY ... off the last branch (bound to branch 1 output cols)
                outer_order_start: Optional[int] = None
                last_bs, last_be = branches[-1]
                depth = 0
                for k in range(last_bs, last_be):
                    if toks[k].kind == "LP":
                        depth += 1
                    elif toks[k].kind == "RP":
                        depth = max(0, depth - 1)
                    elif (
                        depth == 0
                        and toks[k].kind == "WORD"
                        and toks[k].upper == "ORDER"
                        and k + 1 < last_be
                        and toks[k + 1].kind == "WORD"
                        and toks[k + 1].upper == "BY"
                    ):
                        outer_order_start = k
                        break
                if outer_order_start is not None:
                    branches[-1] = (last_bs, outer_order_start)

                first_out: Tuple[Dict[str, ColumnChangeMapping], Dict[str, ColumnChangeMapping]] = ({}, {})
                branch_ctes = dict(ctes)
                for idx, (bs, be) in enumerate(branches):
                    # Unwrap parenthesized branch `(SELECT ...)`
                    if bs < be and toks[bs].kind == "LP" and _find_matching_rp(toks, bs, be) == be - 1:
                        res = self._process_query_or_dml(
                            bs + 1,
                            be - 1,
                            parent_scope={
                                "parent": parent_scope,
                                "ctes": branch_ctes,
                                "alias_map": parent_scope["alias_map"] if parent_scope else {},
                            },
                        )
                    else:
                        res = self._process_single_select(bs, be, ctes=branch_ctes, parent_scope=parent_scope)
                    if idx == 0:
                        first_out = res
                        if recursive_cte_name:
                            branch_ctes[recursive_cte_name] = _CteDef(
                                name=recursive_cte_name,
                                explicit_columns=None,
                                rename_map=dict(res[0]),
                                type_map=dict(res[1]),
                            )

                if outer_order_start is not None and (first_out[0] or first_out[1]):
                    union_tb = _TableBinding(
                        alias="__union_out",
                        raw_alias="",
                        schema_name="",
                        table_name="",
                        is_virtual=True,
                        virtual_rename_map=dict(first_out[0]),
                        virtual_type_map=dict(first_out[1]),
                    )
                    self._resolve_scope_columns(
                        outer_order_start,
                        last_be,
                        scope_ctx={
                            "parent": parent_scope,
                            "ctes": branch_ctes,
                            "tables": [union_tb],
                            "alias_map": {},
                        },
                        protected=set(),
                        child_ranges=[],
                    )
                return first_out
            return self._process_single_select(cur, end, ctes=ctes, parent_scope=parent_scope)

        if toks[cur].kind == "WORD" and toks[cur].upper == "INSERT":
            self._process_insert(cur, end, ctes=ctes, parent_scope=parent_scope)
            return {}, {}

        if toks[cur].kind == "WORD" and toks[cur].upper == "UPDATE":
            self._process_update(cur, end, ctes=ctes, parent_scope=parent_scope)
            return {}, {}

        if toks[cur].kind == "WORD" and toks[cur].upper == "DELETE":
            self._process_delete(cur, end, ctes=ctes, parent_scope=parent_scope)
            return {}, {}

        if toks[cur].kind == "WORD" and toks[cur].upper == "MERGE":
            self._process_merge(cur, end, ctes=ctes, parent_scope=parent_scope)
            return {}, {}

        # Parenthesized query `(SELECT ...)`
        if toks[cur].kind == "LP":
            rp = _find_matching_rp(toks, cur, end)
            return self._process_query_or_dml(
                cur + 1,
                rp,
                parent_scope={
                    "parent": parent_scope,
                    "ctes": ctes,
                    "alias_map": parent_scope["alias_map"] if parent_scope else {},
                },
            )

        return {}, {}

    def _make_table_binding(
        self,
        schema_name: str,
        table_name: str,
        raw_alias: str,
        ctes: Dict[str, _CteDef],
        token_span_tok: SqlToken,
    ) -> _TableBinding:
        alias_eff = (raw_alias or table_name).lower()
        # Check CTE first when no schema prefix
        if not schema_name and table_name.lower() in ctes:
            cte_def = ctes[table_name.lower()]
            return _TableBinding(
                alias=alias_eff,
                raw_alias=raw_alias or table_name,
                schema_name="",
                table_name=table_name,
                is_virtual=True,
                virtual_rename_map=dict(cte_def.rename_map),
                virtual_type_map=dict(cte_def.type_map),
            )

        spec, cands = self.registry.resolve_table(schema_name, table_name, self.active_default_schema)
        if spec is None and len(cands) > 1:
            self._emit_advisory(
                start=token_span_tok.start,
                end=token_span_tok.end,
                severity="WARN",
                category="AMBIGUOUS_TABLE",
                schema_name="",
                table_name=table_name,
                column_name="*",
                before_spec="",
                after_spec="",
                risk_level="L1",
                message=(
                    f"表 {table_name} 未写 Schema 前缀，且在变更日志中同时匹配多个 Schema"
                    f"（{', '.join(c.fqn for c in cands)}），已暂缓自动改名，请设置默认 Schema 或启用 AI 辅助消歧"
                ),
                needs_llm=True,
            )
        return _TableBinding(
            alias=alias_eff,
            raw_alias=raw_alias or table_name,
            schema_name=spec.schema_name if spec else schema_name,
            table_name=spec.table_name if spec else table_name,
            is_virtual=False,
            spec=spec,
            ambiguous_specs=cands if spec is None and len(cands) > 1 else [],
        )

    def _parse_from_join_tables(
        self,
        start: int,
        end: int,
        *,
        ctes: Dict[str, _CteDef],
        parent_scope: Optional[Dict[str, Any]],
        tables: List[_TableBinding],
        alias_map: Dict[str, _TableBinding],
        protected: Set[int],
        child_ranges: List[Tuple[int, int]],
        using_clauses: List[Tuple[int, int, _TableBinding, _TableBinding]],
    ) -> None:
        """在顶层 (depth==0) 扫描 FROM / JOIN / USING 目标表与派生子查询."""
        toks = self.tokens
        depth = 0
        i = start
        prev_binding: Optional[_TableBinding] = tables[-1] if tables else None

        def _register_binding(b: _TableBinding) -> None:
            nonlocal prev_binding
            tables.append(b)
            alias_map[b.alias] = b
            # Also register unqualified table name if no explicit alias collision
            t_low = b.table_name.lower()
            if t_low and t_low not in alias_map:
                alias_map[t_low] = b
            prev_binding = b

        def _parse_table_factor(pos: int) -> Tuple[int, Optional[_TableBinding]]:
            if pos >= end:
                return pos, None
            # Optional LATERAL / ONLY
            while pos < end and toks[pos].kind == "WORD" and toks[pos].upper in ("LATERAL", "ONLY"):
                protected.add(pos)
                pos += 1
            if pos >= end:
                return pos, None

            # Derived table subquery: (SELECT ...) [AS] alias
            if toks[pos].kind == "LP":
                rp = _find_matching_rp(toks, pos, end)
                child_ranges.append((pos, rp))
                sub_ren: Dict[str, ColumnChangeMapping] = {}
                sub_typ: Dict[str, ColumnChangeMapping] = {}
                if pos + 1 < rp and toks[pos + 1].kind == "WORD" and toks[pos + 1].upper in ("SELECT", "WITH"):
                    sub_parent = {
                        "parent": parent_scope,
                        "ctes": ctes,
                        "alias_map": dict(alias_map),
                    }
                    sub_ren, sub_typ = self._process_query_or_dml(pos + 1, rp, parent_scope=sub_parent)
                raw_alias, nxt, alias_idx = _read_optional_alias(toks, rp + 1, end)
                if alias_idx is not None:
                    protected.add(alias_idx)
                binding = _TableBinding(
                    alias=(raw_alias or f"__sub_{pos}").lower(),
                    raw_alias=raw_alias or "",
                    schema_name="",
                    table_name=raw_alias or "",
                    is_virtual=True,
                    virtual_rename_map=sub_ren,
                    virtual_type_map=sub_typ,
                )
                _register_binding(binding)
                return nxt, binding

            if not _is_ident_tok(toks[pos]):
                return pos, None

            sch, tbl, nxt, idxs = _read_qualified_table_name(toks, pos, end)
            for ix in idxs:
                protected.add(ix)
            # Function table e.g. UNNEST(...) or GENERATE_SERIES(...)
            if nxt < end and toks[nxt].kind == "LP":
                rp = _find_matching_rp(toks, nxt, end)
                raw_alias, nxt2, alias_idx = _read_optional_alias(toks, rp + 1, end)
                if alias_idx is not None:
                    protected.add(alias_idx)
                return nxt2, None

            raw_alias, nxt2, alias_idx = _read_optional_alias(toks, nxt, end)
            if alias_idx is not None:
                protected.add(alias_idx)
            binding = self._make_table_binding(sch, tbl, raw_alias, ctes, toks[idxs[-1]])
            _register_binding(binding)
            return nxt2, binding

        while i < end:
            t = toks[i]
            if t.kind == "LP":
                depth += 1
                i += 1
                continue
            if t.kind == "RP":
                depth = max(0, depth - 1)
                i += 1
                continue

            if depth == 0 and t.kind == "WORD" and t.upper == "FROM":
                # Guard against EXTRACT(YEAR FROM col) or TRIM(BOTH ' ' FROM col) -> those are at depth > 0
                cur = i + 1
                while cur < end:
                    nxt, _b = _parse_table_factor(cur)
                    if nxt == cur:
                        break
                    cur = nxt
                    if cur < end and toks[cur].kind == "COMMA":
                        cur += 1
                        continue
                    break
                i = cur
                continue

            if depth == 0 and t.kind == "WORD" and t.upper == "JOIN":
                left_b = prev_binding
                nxt, right_b = _parse_table_factor(i + 1)
                # Check immediate USING (c1, c2)
                if (
                    right_b is not None
                    and left_b is not None
                    and nxt + 1 < end
                    and toks[nxt].kind == "WORD"
                    and toks[nxt].upper == "USING"
                    and toks[nxt + 1].kind == "LP"
                ):
                    rp_u = _find_matching_rp(toks, nxt + 1, end)
                    using_clauses.append((nxt + 2, rp_u, left_b, right_b))
                    child_ranges.append((nxt + 1, rp_u))
                    i = rp_u + 1
                    continue
                i = max(i + 1, nxt)
                continue

            i += 1

    def _find_select_projection_range(self, start: int, end: int) -> Tuple[int, int]:
        """定位 SELECT ... FROM 之间的投影区间."""
        toks = self.tokens
        p = start + 1  # skip SELECT
        while p < end and toks[p].kind == "WORD" and toks[p].upper in ("ALL", "DISTINCT", "TOP"):
            p += 1
            if toks[p - 1].upper == "TOP" and p < end and toks[p].kind == "NUMBER":
                p += 1
            if toks[p - 1].upper == "DISTINCT" and p < end and toks[p].kind == "WORD" and toks[p].upper == "ON":
                p += 1
                if p < end and toks[p].kind == "LP":
                    p = _find_matching_rp(toks, p, end) + 1
        depth = 0
        for k in range(p, end):
            if toks[k].kind == "LP":
                depth += 1
            elif toks[k].kind == "RP":
                depth = max(0, depth - 1)
            elif depth == 0 and toks[k].kind == "WORD" and toks[k].upper in (
                "FROM", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "INTO", "UNION", "INTERSECT", "EXCEPT"
            ):
                return p, k
        return p, end

    def _process_single_select(
        self,
        start: int,
        end: int,
        *,
        ctes: Dict[str, _CteDef],
        parent_scope: Optional[Dict[str, Any]],
    ) -> Tuple[Dict[str, ColumnChangeMapping], Dict[str, ColumnChangeMapping]]:
        toks = self.tokens
        tables: List[_TableBinding] = []
        alias_map: Dict[str, _TableBinding] = {}
        protected: Set[int] = set()
        child_ranges: List[Tuple[int, int]] = []
        using_clauses: List[Tuple[int, int, _TableBinding, _TableBinding]] = []

        self._parse_from_join_tables(
            start,
            end,
            ctes=ctes,
            parent_scope=parent_scope,
            tables=tables,
            alias_map=alias_map,
            protected=protected,
            child_ranges=child_ranges,
            using_clauses=using_clauses,
        )

        # Resolve USING (col1, col2) clauses
        for u_s, u_e, left_b, right_b in using_clauses:
            self._process_using_clause(u_s, u_e, left_b, right_b)

        # Analyze SELECT projection items for output aliases + CTE/derived-table propagation
        proj_s, proj_e = self._find_select_projection_range(start, end)
        select_items = _split_top_level_by_comma(toks, proj_s, proj_e) if proj_s < proj_e else []

        exposed_renames: Dict[str, ColumnChangeMapping] = {}
        exposed_types: Dict[str, ColumnChangeMapping] = {}
        preserve_alias_col_idxs: Dict[int, str] = {}
        explicit_select_aliases: Set[str] = set()

        scope_ctx = {
            "parent": parent_scope,
            "ctes": ctes,
            "tables": tables,
            "alias_map": alias_map,
        }

        for item_s, item_e in select_items:
            if item_s >= item_e:
                continue
            # Check explicit alias: ... AS alias_tok
            explicit_alias_idx: Optional[int] = None
            depth = 0
            for k in range(item_s, item_e):
                if toks[k].kind == "LP":
                    depth += 1
                elif toks[k].kind == "RP":
                    depth = max(0, depth - 1)
                elif depth == 0 and toks[k].kind == "WORD" and toks[k].upper == "AS":
                    if k + 1 == item_e - 1 and _is_ident_tok(toks[k + 1]):
                        explicit_alias_idx = k + 1
            if explicit_alias_idx is not None:
                protected.add(explicit_alias_idx)
                explicit_select_aliases.add(toks[explicit_alias_idx].ident.lower())
                expr_end = explicit_alias_idx - 1  # before AS
            else:
                # Check implicit trailing alias: e.g. `t.old_col my_alias` or `SUM(t.amt) total_amt`
                expr_end = item_e
                if item_e - item_s >= 2 and _is_non_kw_ident(toks[item_e - 1]):
                    prev_t = toks[item_e - 2]
                    if prev_t.kind not in ("DOT", "OP", "COMMA", "LP") and not (
                        prev_t.kind == "WORD" and prev_t.upper in ("AND", "OR", "NOT", "CASE", "WHEN", "THEN", "ELSE", "IS", "IN", "LIKE", "ILIKE", "AS", "DISTINCT", "COLLATE")
                    ):
                        protected.add(item_e - 1)
                        explicit_alias_idx = item_e - 1
                        explicit_select_aliases.add(toks[explicit_alias_idx].ident.lower())
                        expr_end = item_e - 1

            # Check `SELECT *` or `SELECT t.*`
            if expr_end - item_s == 1 and toks[item_s].kind == "OP" and toks[item_s].raw == "*":
                for tb in tables:
                    exposed_renames.update(tb.all_renames())
                    exposed_types.update(tb.all_type_changes())
                    if tb.all_renames() or tb.all_type_changes():
                        self._emit_advisory(
                            start=toks[item_s].start,
                            end=toks[item_s].end,
                            severity="INFO",
                            category="SELECT_STAR_IMPACT",
                            schema_name=tb.schema_name,
                            table_name=tb.table_name,
                            column_name="*",
                            before_spec="",
                            after_spec="",
                            risk_level="L1" if tb.all_renames() else "L2",
                            message=(
                                f"SELECT * 涉及已落标变更表 {tb.schema_name + '.' if tb.schema_name else ''}{tb.table_name}，"
                                "输出列结构已随最新表结构变化，请核查下游依赖"
                            ),
                            needs_llm=False,
                        )
                continue

            if (
                expr_end - item_s == 3
                and _is_ident_tok(toks[item_s])
                and toks[item_s + 1].kind == "DOT"
                and toks[item_s + 2].kind == "OP"
                and toks[item_s + 2].raw == "*"
            ):
                tb = self._resolve_qualifier(toks[item_s].ident, scope_ctx)
                if tb is not None:
                    exposed_renames.update(tb.all_renames())
                    exposed_types.update(tb.all_type_changes())
                    if tb.all_renames() or tb.all_type_changes():
                        self._emit_advisory(
                            start=toks[item_s].start,
                            end=toks[item_s + 2].end,
                            severity="INFO",
                            category="SELECT_STAR_IMPACT",
                            schema_name=tb.schema_name,
                            table_name=tb.table_name,
                            column_name="*",
                            before_spec="",
                            after_spec="",
                            risk_level="L1" if tb.all_renames() else "L2",
                            message=(
                                f"SELECT {toks[item_s].ident}.* 涉及已变更表 "
                                f"{tb.schema_name + '.' if tb.schema_name else ''}{tb.table_name}，请核查下游依赖"
                            ),
                            needs_llm=False,
                        )
                continue

            # Bare column projection (no explicit alias) OR aliased single column (propagate type-change metadata)
            if (
                expr_end - item_s == 3
                and _is_ident_tok(toks[item_s])
                and toks[item_s + 1].kind == "DOT"
                and _is_ident_tok(toks[item_s + 2])
            ):
                tb = self._resolve_qualifier(toks[item_s].ident, scope_ctx)
                col_tok = toks[item_s + 2]
                if tb is not None:
                    m_typ = tb.get_type_change(col_tok.ident.lower())
                    if explicit_alias_idx is None:
                        m_ren = tb.get_rename(col_tok.ident.lower())
                        if m_ren is not None:
                            if self.options.cte_alias_policy == "preserve_alias":
                                preserve_alias_col_idxs[item_s + 2] = col_tok.raw
                            else:
                                exposed_renames[col_tok.ident.lower()] = m_ren
                        if m_typ is not None:
                            exposed_types[col_tok.ident.lower()] = m_typ
                            exposed_types[m_typ.current_col_name.lower()] = m_typ
                    elif m_typ is not None:
                        exposed_types[toks[explicit_alias_idx].ident.lower()] = m_typ
            elif expr_end - item_s == 1 and _is_ident_tok(toks[item_s]) and (
                toks[item_s].kind == "QIDENT" or toks[item_s].upper not in _HARD_CLAUSE_KEYWORDS
            ):
                col_tok = toks[item_s]
                tb_hit, _amb = self._resolve_unqualified_table_for_col(col_tok.ident.lower(), tables)
                if tb_hit is not None:
                    m_typ = tb_hit.get_type_change(col_tok.ident.lower())
                    if explicit_alias_idx is None:
                        m_ren = tb_hit.get_rename(col_tok.ident.lower())
                        if m_ren is not None:
                            if self.options.cte_alias_policy == "preserve_alias":
                                preserve_alias_col_idxs[item_s] = col_tok.raw
                            else:
                                exposed_renames[col_tok.ident.lower()] = m_ren
                        if m_typ is not None:
                            exposed_types[col_tok.ident.lower()] = m_typ
                            exposed_types[m_typ.current_col_name.lower()] = m_typ
                    elif m_typ is not None:
                        exposed_types[toks[explicit_alias_idx].ident.lower()] = m_typ

        # Protect PL/SQL `SELECT ... INTO var1, var2 FROM ...` target variables
        if proj_e < end and toks[proj_e].kind == "WORD" and toks[proj_e].upper == "INTO":
            k_into = proj_e + 1
            if k_into < end and toks[k_into].kind == "WORD" and toks[k_into].upper == "STRICT":
                k_into += 1
            while k_into < end:
                if toks[k_into].kind == "WORD" and toks[k_into].upper in ("FROM", "WHERE", "GROUP", "ORDER", "LIMIT"):
                    break
                if _is_ident_tok(toks[k_into]):
                    protected.add(k_into)
                k_into += 1

        # Protect unqualified references in GROUP BY / HAVING / ORDER BY that shadow an explicit SELECT output alias
        if explicit_select_aliases and proj_e < end:
            sorted_cr = sorted(child_ranges, key=lambda x: x[0])
            in_alias_clause = False
            depth_cl = 0
            ck = proj_e
            while ck < end:
                jumped = False
                for cs, ce in sorted_cr:
                    if cs <= ck <= ce:
                        ck = ce + 1
                        jumped = True
                        break
                if jumped:
                    continue
                tk = toks[ck]
                if tk.kind == "LP":
                    if ck + 1 < end and toks[ck + 1].kind == "WORD" and toks[ck + 1].upper in ("SELECT", "WITH"):
                        ck = _find_matching_rp(toks, ck, end) + 1
                        continue
                    depth_cl += 1
                    ck += 1
                    continue
                if tk.kind == "RP":
                    depth_cl = max(0, depth_cl - 1)
                    ck += 1
                    continue
                if depth_cl == 0 and tk.kind == "WORD":
                    u = tk.upper
                    if u in ("GROUP", "ORDER") and ck + 1 < end and toks[ck + 1].kind == "WORD" and toks[ck + 1].upper == "BY":
                        in_alias_clause = True
                        ck += 2
                        continue
                    if u == "HAVING":
                        in_alias_clause = True
                        ck += 1
                        continue
                    if u in ("LIMIT", "OFFSET", "FETCH", "FOR", "WINDOW", "QUALIFY", "WHERE", "FROM", "INTO"):
                        in_alias_clause = False
                        ck += 1
                        continue
                if (
                    in_alias_clause
                    and _is_ident_tok(tk)
                    and tk.ident.lower() in explicit_select_aliases
                    and not (ck > start and toks[ck - 1].kind == "DOT")
                    and not (ck + 1 < end and toks[ck + 1].kind in ("DOT", "LP"))
                ):
                    protected.add(ck)
                ck += 1

        # Resolve column references across the entire SELECT scope (skipping child_ranges & protected tokens)
        self._resolve_scope_columns(
            start,
            end,
            scope_ctx=scope_ctx,
            protected=protected,
            child_ranges=child_ranges,
            preserve_alias_col_idxs=preserve_alias_col_idxs,
        )
        return exposed_renames, exposed_types

    def _process_insert(
        self,
        start: int,
        end: int,
        *,
        ctes: Dict[str, _CteDef],
        parent_scope: Optional[Dict[str, Any]],
    ) -> None:
        toks = self.tokens
        i = start + 1
        while i < end and toks[i].kind == "WORD" and toks[i].upper in ("INTO", "OVERWRITE", "TABLE", "IGNORE"):
            i += 1
        sch, tbl, nxt, _ = _read_qualified_table_name(toks, i, end)
        spec, _ = self.registry.resolve_table(sch, tbl, self.active_default_schema)

        # Optional target alias `INSERT INTO t AS tgt (...)`
        target_alias = tbl
        if nxt < end and toks[nxt].kind == "WORD" and toks[nxt].upper == "AS":
            if nxt + 1 < end and _is_ident_tok(toks[nxt + 1]):
                target_alias = toks[nxt + 1].ident
            nxt += 2 if nxt + 1 < end else 1

        # Optional target column list `(c1, c2, ...)` (must NOT start with SELECT/WITH)
        if nxt < end and toks[nxt].kind == "LP":
            rp = _find_matching_rp(toks, nxt, end)
            if not (nxt + 1 < rp and toks[nxt + 1].kind == "WORD" and toks[nxt + 1].upper in ("SELECT", "WITH")):
                if spec is not None:
                    self._rewrite_column_list_for_spec(nxt + 1, rp, spec, "INSERT 目标列")
                nxt = rp + 1

        # Locate trailing ON CONFLICT / ON DUPLICATE KEY UPDATE / RETURNING at depth == 0
        tail_idx = end
        depth = 0
        for k in range(nxt, end):
            if toks[k].kind == "LP":
                depth += 1
            elif toks[k].kind == "RP":
                depth = max(0, depth - 1)
            elif depth == 0 and toks[k].kind == "WORD":
                if toks[k].upper == "RETURNING":
                    tail_idx = k
                    break
                if (
                    toks[k].upper == "ON"
                    and k + 1 < end
                    and toks[k + 1].kind == "WORD"
                    and toks[k + 1].upper in ("CONFLICT", "DUPLICATE")
                ):
                    tail_idx = k
                    break

        # Body could be SELECT / WITH / VALUES
        if nxt < tail_idx:
            if toks[nxt].kind == "WORD" and toks[nxt].upper in ("SELECT", "WITH"):
                self._process_query_or_dml(
                    nxt,
                    tail_idx,
                    parent_scope={
                        "parent": parent_scope,
                        "ctes": ctes,
                        "alias_map": parent_scope["alias_map"] if parent_scope else {},
                    },
                )
            else:
                self._scan_embedded_subqueries(nxt, tail_idx, parent_scope=parent_scope)

        # Handle ON CONFLICT (...) DO UPDATE SET ... / ON DUPLICATE KEY UPDATE ... / RETURNING ...
        if tail_idx < end and spec is not None:
            tb = _TableBinding(
                alias=target_alias.lower(),
                raw_alias=target_alias,
                schema_name=spec.schema_name,
                table_name=spec.table_name,
                spec=spec,
            )
            upsert_alias_map: Dict[str, _TableBinding] = {
                tb.alias: tb,
                spec.table_name.lower(): tb,
                "excluded": tb,
            }
            upsert_protected: Set[int] = set()
            k = tail_idx
            while k < end:
                if (
                    toks[k].kind == "WORD"
                    and toks[k].upper == "CONFLICT"
                    and k + 1 < end
                    and toks[k + 1].kind == "LP"
                ):
                    rp_c = _find_matching_rp(toks, k + 1, end)
                    self._rewrite_column_list_for_spec(k + 2, rp_c, spec, "ON CONFLICT 列")
                    for ix in range(k + 1, rp_c + 1):
                        upsert_protected.add(ix)
                    k = rp_c + 1
                    continue
                if toks[k].kind == "WORD" and toks[k].upper in ("SET", "UPDATE"):
                    # For `DO UPDATE SET ...` skip `UPDATE` and handle `SET`; for MySQL `ON DUPLICATE KEY UPDATE ...` handle `UPDATE`
                    if toks[k].upper == "UPDATE" and k + 1 < end and toks[k + 1].kind == "WORD" and toks[k + 1].upper == "SET":
                        k += 1
                        continue
                    se = k + 1
                    d2 = 0
                    while se < end:
                        if toks[se].kind == "LP":
                            d2 += 1
                        elif toks[se].kind == "RP":
                            d2 = max(0, d2 - 1)
                        elif d2 == 0 and toks[se].kind == "WORD" and toks[se].upper in ("WHERE", "RETURNING"):
                            break
                        se += 1
                    for seg_s, seg_e in _split_top_level_by_comma(toks, k + 1, se):
                        self._rewrite_update_set_lhs(seg_s, seg_e, tb, upsert_protected)
                    k = se
                    continue
                k += 1

            self._resolve_scope_columns(
                tail_idx,
                end,
                scope_ctx={
                    "parent": parent_scope,
                    "ctes": ctes,
                    "tables": [tb],
                    "alias_map": upsert_alias_map,
                },
                protected=upsert_protected,
                child_ranges=[],
            )

    def _process_update(
        self,
        start: int,
        end: int,
        *,
        ctes: Dict[str, _CteDef],
        parent_scope: Optional[Dict[str, Any]],
    ) -> None:
        toks = self.tokens
        i = start + 1
        if i < end and toks[i].kind == "WORD" and toks[i].upper == "ONLY":
            i += 1
        sch, tbl, nxt, idxs = _read_qualified_table_name(toks, i, end)
        if not idxs:
            return
        raw_alias, after_alias, alias_idx = _read_optional_alias(toks, nxt, end)
        protected: Set[int] = set(idxs)
        if alias_idx is not None:
            protected.add(alias_idx)

        target_b = self._make_table_binding(sch, tbl, raw_alias, ctes, toks[idxs[-1]])
        tables: List[_TableBinding] = [target_b]
        alias_map: Dict[str, _TableBinding] = {target_b.alias: target_b}
        if target_b.table_name.lower() not in alias_map:
            alias_map[target_b.table_name.lower()] = target_b
        child_ranges: List[Tuple[int, int]] = []
        using_clauses: List[Tuple[int, int, _TableBinding, _TableBinding]] = []

        # Handle MySQL comma-joined target tables: `UPDATE t1 a, t2 b SET ...`
        cur_after = after_alias
        while cur_after < end and toks[cur_after].kind == "COMMA":
            cur_after += 1
            if cur_after >= end:
                break
            if toks[cur_after].kind == "LP":
                rp = _find_matching_rp(toks, cur_after, end)
                child_ranges.append((cur_after, rp))
                sub_ren: Dict[str, ColumnChangeMapping] = {}
                sub_typ: Dict[str, ColumnChangeMapping] = {}
                if cur_after + 1 < rp and toks[cur_after + 1].kind == "WORD" and toks[cur_after + 1].upper in ("SELECT", "WITH"):
                    sub_ren, sub_typ = self._process_query_or_dml(
                        cur_after + 1,
                        rp,
                        parent_scope={"parent": parent_scope, "ctes": ctes, "alias_map": dict(alias_map)},
                    )
                s_alias, cur_after, s_aidx = _read_optional_alias(toks, rp + 1, end)
                if s_aidx is not None:
                    protected.add(s_aidx)
                b_sub = _TableBinding(
                    alias=(s_alias or f"__upd_sub_{cur_after}").lower(),
                    raw_alias=s_alias or "",
                    schema_name="",
                    table_name=s_alias or "",
                    is_virtual=True,
                    virtual_rename_map=sub_ren,
                    virtual_type_map=sub_typ,
                )
                tables.append(b_sub)
                alias_map[b_sub.alias] = b_sub
                continue
            s2, t2, nxt2, idxs2 = _read_qualified_table_name(toks, cur_after, end)
            if not idxs2:
                break
            for ix in idxs2:
                protected.add(ix)
            a2, cur_after, aidx2 = _read_optional_alias(toks, nxt2, end)
            if aidx2 is not None:
                protected.add(aidx2)
            b2 = self._make_table_binding(s2, t2, a2, ctes, toks[idxs2[-1]])
            tables.append(b2)
            alias_map[b2.alias] = b2
            if b2.table_name.lower() not in alias_map:
                alias_map[b2.table_name.lower()] = b2

        # Parse optional FROM / JOIN clauses in UPDATE
        self._parse_from_join_tables(
            cur_after,
            end,
            ctes=ctes,
            parent_scope=parent_scope,
            tables=tables,
            alias_map=alias_map,
            protected=protected,
            child_ranges=child_ranges,
            using_clauses=using_clauses,
        )

        # Find SET ... (WHERE | FROM | RETURNING | ORDER | LIMIT | end) and strictly bind LHS column assignments
        depth = 0
        set_idx = -1
        set_end = end
        for k in range(cur_after, end):
            if toks[k].kind == "LP":
                depth += 1
            elif toks[k].kind == "RP":
                depth = max(0, depth - 1)
            elif depth == 0 and toks[k].kind == "WORD":
                if set_idx == -1 and toks[k].upper == "SET":
                    set_idx = k
                elif set_idx != -1 and toks[k].upper in ("FROM", "WHERE", "RETURNING", "ORDER", "LIMIT"):
                    set_end = k
                    break

        if set_idx != -1:
            for seg_s, seg_e in _split_top_level_by_comma(toks, set_idx + 1, set_end):
                self._rewrite_update_set_lhs(seg_s, seg_e, target_b, protected, alias_map=alias_map)

        scope_ctx = {
            "parent": parent_scope,
            "ctes": ctes,
            "tables": tables,
            "alias_map": alias_map,
        }
        self._resolve_scope_columns(
            cur_after,
            end,
            scope_ctx=scope_ctx,
            protected=protected,
            child_ranges=child_ranges,
        )

    def _rewrite_update_set_lhs(
        self,
        seg_s: int,
        seg_e: int,
        target_b: _TableBinding,
        protected: Set[int],
        *,
        alias_map: Optional[Dict[str, _TableBinding]] = None,
    ) -> None:
        toks = self.tokens
        if seg_s >= seg_e:
            return
        # Find top-level `=`
        eq_idx = -1
        depth = 0
        for k in range(seg_s, seg_e):
            if toks[k].kind == "LP":
                depth += 1
            elif toks[k].kind == "RP":
                depth = max(0, depth - 1)
            elif depth == 0 and toks[k].kind == "OP" and toks[k].raw == "=":
                eq_idx = k
                break
        if eq_idx == -1:
            return

        # Case 1: SET (c1, c2) = (...)
        if toks[seg_s].kind == "LP" and eq_idx > seg_s + 1:
            if target_b.spec is None:
                return
            rp = _find_matching_rp(toks, seg_s, eq_idx)
            self._rewrite_column_list_for_spec(seg_s + 1, rp, target_b.spec, "UPDATE SET 目标列")
            for k in range(seg_s, rp + 1):
                protected.add(k)
            return

        # Case 2: SET col = expr or SET alias.col = expr
        col_idx = eq_idx - 1
        if col_idx >= seg_s and _is_ident_tok(toks[col_idx]):
            eff_target = target_b
            # If qualified `alias.col`, resolve qualifier against alias_map (supports MySQL multi-table UPDATE)
            if col_idx - 2 >= seg_s and toks[col_idx - 1].kind == "DOT" and _is_ident_tok(toks[col_idx - 2]):
                q = toks[col_idx - 2].ident.lower()
                if q != target_b.alias and q != target_b.table_name.lower():
                    if alias_map and q in alias_map:
                        eff_target = alias_map[q]
                    else:
                        return
            if eff_target.spec is None:
                return
            col_tok = toks[col_idx]
            col_l = col_tok.ident.lower()
            m_ren = eff_target.spec.rename_map.get(col_l)
            if m_ren is not None:
                self._emit_patch(
                    start=col_tok.start,
                    end=col_tok.end,
                    replacement_text=format_replacement_ident(
                        col_tok, m_ren.current_col_name, self.options.preserve_quote_style
                    ),
                    kind="RENAME_TARGET_COL",
                    confidence="HIGH",
                    schema_name=eff_target.spec.schema_name,
                    table_name=eff_target.spec.table_name,
                    original_col=m_ren.original_col_name,
                    current_col=m_ren.current_col_name,
                    risk_level=m_ren.risk_level,
                    reason=f"UPDATE SET 目标列改名：{eff_target.spec.fqn}.{col_tok.ident} → {m_ren.current_col_name}",
                )
            m_typ = m_ren if (m_ren and m_ren.type_changed) else eff_target.spec.type_map.get(col_l)
            if m_typ is not None and m_typ.risk_level in ("L2", "L3"):
                # Inspect RHS literal/expression for type mismatch or precision shrink
                self._check_type_impact_around_col(
                    col_idx,
                    seg_s,
                    seg_e,
                    m_typ,
                    eff_target.spec.schema_name,
                    eff_target.spec.table_name,
                    is_write_target=True,
                )
            for k in range(seg_s, eq_idx):
                protected.add(k)

    def _process_delete(
        self,
        start: int,
        end: int,
        *,
        ctes: Dict[str, _CteDef],
        parent_scope: Optional[Dict[str, Any]],
    ) -> None:
        toks = self.tokens
        tables: List[_TableBinding] = []
        alias_map: Dict[str, _TableBinding] = {}
        protected: Set[int] = set()
        child_ranges: List[Tuple[int, int]] = []
        using_clauses: List[Tuple[int, int, _TableBinding, _TableBinding]] = []

        # Handle `DELETE [FROM] [ONLY] tbl [alias] [USING tbl2 [alias2]] WHERE ...`
        i = start + 1
        if i < end and toks[i].kind == "WORD" and toks[i].upper == "FROM":
            self._parse_from_join_tables(
                start,
                end,
                ctes=ctes,
                parent_scope=parent_scope,
                tables=tables,
                alias_map=alias_map,
                protected=protected,
                child_ranges=child_ranges,
                using_clauses=using_clauses,
            )
        else:
            if i < end and toks[i].kind == "WORD" and toks[i].upper == "ONLY":
                i += 1
            sch, tbl, nxt, idxs = _read_qualified_table_name(toks, i, end)
            if idxs:
                for ix in idxs:
                    protected.add(ix)
                raw_alias, nxt2, alias_idx = _read_optional_alias(toks, nxt, end)
                if alias_idx is not None:
                    protected.add(alias_idx)
                b = self._make_table_binding(sch, tbl, raw_alias, ctes, toks[idxs[-1]])
                tables.append(b)
                alias_map[b.alias] = b
                if b.table_name.lower() not in alias_map:
                    alias_map[b.table_name.lower()] = b

        # Also parse PostgreSQL/DWS `DELETE FROM t USING t2 a2 ...`
        depth = 0
        for k in range(start, end):
            if toks[k].kind == "LP":
                depth += 1
            elif toks[k].kind == "RP":
                depth = max(0, depth - 1)
            elif depth == 0 and toks[k].kind == "WORD" and toks[k].upper == "USING":
                # Parse USING table list
                cur = k + 1
                while cur < end:
                    if toks[cur].kind == "LP":
                        break
                    sch, tbl, nxt, idxs = _read_qualified_table_name(toks, cur, end)
                    if not idxs:
                        break
                    for ix in idxs:
                        protected.add(ix)
                    raw_alias, nxt2, alias_idx = _read_optional_alias(toks, nxt, end)
                    if alias_idx is not None:
                        protected.add(alias_idx)
                    b = self._make_table_binding(sch, tbl, raw_alias, ctes, toks[idxs[-1]])
                    tables.append(b)
                    alias_map[b.alias] = b
                    if b.table_name.lower() not in alias_map:
                        alias_map[b.table_name.lower()] = b
                    cur = nxt2
                    if cur < end and toks[cur].kind == "COMMA":
                        cur += 1
                        continue
                    break

        scope_ctx = {
            "parent": parent_scope,
            "ctes": ctes,
            "tables": tables,
            "alias_map": alias_map,
        }
        self._resolve_scope_columns(
            start,
            end,
            scope_ctx=scope_ctx,
            protected=protected,
            child_ranges=child_ranges,
        )

    def _process_merge(
        self,
        start: int,
        end: int,
        *,
        ctes: Dict[str, _CteDef],
        parent_scope: Optional[Dict[str, Any]],
    ) -> None:
        toks = self.tokens
        i = start + 1
        if i < end and toks[i].kind == "WORD" and toks[i].upper == "INTO":
            i += 1
        sch, tbl, nxt, idxs = _read_qualified_table_name(toks, i, end)
        if not idxs:
            return
        raw_alias, cur, alias_idx = _read_optional_alias(toks, nxt, end)
        protected: Set[int] = set(idxs)
        if alias_idx is not None:
            protected.add(alias_idx)

        target_b = self._make_table_binding(sch, tbl, raw_alias, ctes, toks[idxs[-1]])
        tables: List[_TableBinding] = [target_b]
        alias_map: Dict[str, _TableBinding] = {target_b.alias: target_b}
        if target_b.table_name.lower() not in alias_map:
            alias_map[target_b.table_name.lower()] = target_b
        child_ranges: List[Tuple[int, int]] = []

        # Parse USING (subquery | src_table) [alias]
        if cur < end and toks[cur].kind == "WORD" and toks[cur].upper == "USING":
            u_pos = cur + 1
            if u_pos < end and toks[u_pos].kind == "LP":
                rp = _find_matching_rp(toks, u_pos, end)
                child_ranges.append((u_pos, rp))
                sub_ren: Dict[str, ColumnChangeMapping] = {}
                sub_typ: Dict[str, ColumnChangeMapping] = {}
                if u_pos + 1 < rp and toks[u_pos + 1].kind == "WORD" and toks[u_pos + 1].upper in ("SELECT", "WITH"):
                    sub_ren, sub_typ = self._process_query_or_dml(
                        u_pos + 1,
                        rp,
                        parent_scope={"parent": parent_scope, "ctes": ctes, "alias_map": dict(alias_map)},
                    )
                s_alias, nxt_s, s_aidx = _read_optional_alias(toks, rp + 1, end)
                if s_aidx is not None:
                    protected.add(s_aidx)
                src_b = _TableBinding(
                    alias=(s_alias or "__merge_src").lower(),
                    raw_alias=s_alias or "",
                    schema_name="",
                    table_name=s_alias or "",
                    is_virtual=True,
                    virtual_rename_map=sub_ren,
                    virtual_type_map=sub_typ,
                )
                tables.append(src_b)
                alias_map[src_b.alias] = src_b
                cur = nxt_s
            else:
                s_sch, s_tbl, nxt_s, s_idxs = _read_qualified_table_name(toks, u_pos, end)
                if s_idxs:
                    for ix in s_idxs:
                        protected.add(ix)
                    s_alias, nxt_s2, s_aidx = _read_optional_alias(toks, nxt_s, end)
                    if s_aidx is not None:
                        protected.add(s_aidx)
                    src_b = self._make_table_binding(s_sch, s_tbl, s_alias, ctes, toks[s_idxs[-1]])
                    tables.append(src_b)
                    alias_map[src_b.alias] = src_b
                    if src_b.table_name.lower() not in alias_map:
                        alias_map[src_b.table_name.lower()] = src_b
                    cur = nxt_s2

        # Handle WHEN MATCHED THEN UPDATE SET ... & WHEN NOT MATCHED THEN INSERT (...) VALUES (...)
        depth = 0
        k = cur
        while k < end:
            if toks[k].kind == "LP":
                depth += 1
                k += 1
                continue
            if toks[k].kind == "RP":
                depth = max(0, depth - 1)
                k += 1
                continue
            if depth == 0 and toks[k].kind == "WORD":
                if toks[k].upper == "SET" and target_b.spec is not None:
                    # find end of SET clause (next WHEN or WHERE or end)
                    se = k + 1
                    d2 = 0
                    while se < end:
                        if toks[se].kind == "LP":
                            d2 += 1
                        elif toks[se].kind == "RP":
                            d2 = max(0, d2 - 1)
                        elif d2 == 0 and toks[se].kind == "WORD" and toks[se].upper in ("WHEN", "WHERE"):
                            break
                        se += 1
                    for seg_s, seg_e in _split_top_level_by_comma(toks, k + 1, se):
                        self._rewrite_update_set_lhs(seg_s, seg_e, target_b, protected)
                    k = se
                    continue
                if toks[k].upper == "INSERT" and k + 1 < end and toks[k + 1].kind == "LP" and target_b.spec is not None:
                    rp = _find_matching_rp(toks, k + 1, end)
                    self._rewrite_column_list_for_spec(k + 2, rp, target_b.spec, "MERGE INSERT 目标列")
                    for ix in range(k + 1, rp + 1):
                        protected.add(ix)
                    k = rp + 1
                    continue
            k += 1

        scope_ctx = {
            "parent": parent_scope,
            "ctes": ctes,
            "tables": tables,
            "alias_map": alias_map,
        }
        self._resolve_scope_columns(
            cur,
            end,
            scope_ctx=scope_ctx,
            protected=protected,
            child_ranges=child_ranges,
        )

    def _process_using_clause(
        self, start: int, end: int, left_b: _TableBinding, right_b: _TableBinding
    ) -> None:
        toks = self.tokens
        for seg_s, seg_e in _split_top_level_by_comma(toks, start, end):
            if seg_s >= seg_e or not _is_ident_tok(toks[seg_s]):
                continue
            col_tok = toks[seg_s]
            col_l = col_tok.ident.lower()
            l_ren = left_b.get_rename(col_l)
            r_ren = right_b.get_rename(col_l)
            if l_ren and r_ren and l_ren.current_col_name.lower() == r_ren.current_col_name.lower():
                self._emit_patch(
                    start=col_tok.start,
                    end=col_tok.end,
                    replacement_text=format_replacement_ident(
                        col_tok, l_ren.current_col_name, self.options.preserve_quote_style
                    ),
                    kind="RENAME_TARGET_COL",
                    confidence="HIGH",
                    schema_name=l_ren.schema_name,
                    table_name=l_ren.table_name,
                    original_col=l_ren.original_col_name,
                    current_col=l_ren.current_col_name,
                    risk_level=l_ren.risk_level,
                    reason=f"JOIN USING 两端表均已将 {col_tok.ident} 改名为 {l_ren.current_col_name}",
                )
            elif l_ren or r_ren:
                hit = l_ren or r_ren
                assert hit is not None
                self._emit_advisory(
                    start=col_tok.start,
                    end=col_tok.end,
                    severity="ERROR",
                    category="USING_CLAUSE_CONFLICT",
                    schema_name=hit.schema_name,
                    table_name=hit.table_name,
                    column_name=col_tok.ident,
                    before_spec=hit.original_col_name,
                    after_spec=hit.current_col_name,
                    risk_level="L1",
                    message=(
                        f"JOIN USING ({col_tok.ident}) 中仅一侧表将字段改名为 {hit.current_col_name}，"
                        "USING 要求双侧同名，需改写为显式 ON 条件"
                    ),
                    needs_llm=True,
                )

    def _resolve_qualifier(
        self, qualifier: str, scope_ctx: Optional[Dict[str, Any]]
    ) -> Optional[_TableBinding]:
        q_l = (clean_identifier_input(qualifier) or qualifier).lower()
        cur = scope_ctx
        while cur is not None:
            amap = cur.get("alias_map") or {}
            if q_l in amap:
                return amap[q_l]
            cur = cur.get("parent")
        return None

    def _resolve_unqualified_table_for_col(
        self, col_lower: str, tables: Sequence[_TableBinding]
    ) -> Tuple[Optional[_TableBinding], str]:
        """在当前作用域多表中解析未限定列，返回 (命中表, status: UNIQUE_SINGLE|UNIQUE_META|UNIQUE_HEURISTIC|AMBIGUOUS|NONE)."""
        if not tables:
            return None, "NONE"
        hits = [
            tb
            for tb in tables
            if tb.get_rename(col_lower) is not None
            or tb.get_type_change(col_lower) is not None
            or tb.get_conflict(col_lower) is not None
        ]
        if not hits:
            return None, "NONE"
        if len(tables) == 1:
            return hits[0], "UNIQUE_SINGLE"
        if len(hits) > 1:
            # Check if all rename targets are distinct -> truly ambiguous
            targets = {
                tb.get_rename(col_lower).current_col_name.lower()
                for tb in hits
                if tb.get_rename(col_lower) is not None
            }
            if len(targets) > 1:
                return None, "AMBIGUOUS"
            return hits[0], "UNIQUE_HEURISTIC"

        # Exactly 1 changed table matches; inspect whether other tables in scope also have this column via metadata
        hit_tb = hits[0]
        all_others_confirmed_absent = True
        for other in tables:
            if other is hit_tb:
                continue
            if other.is_virtual:
                all_others_confirmed_absent = False
                continue
            has_col = self.registry.table_has_column(other.schema_name, other.table_name, col_lower)
            if has_col is True:
                return None, "AMBIGUOUS"
            if has_col is None:
                all_others_confirmed_absent = False
        return hit_tb, ("UNIQUE_META" if all_others_confirmed_absent else "UNIQUE_HEURISTIC")

    def _resolve_scope_columns(
        self,
        start: int,
        end: int,
        *,
        scope_ctx: Dict[str, Any],
        protected: Set[int],
        child_ranges: List[Tuple[int, int]],
        preserve_alias_col_idxs: Optional[Dict[int, str]] = None,
    ) -> None:
        toks = self.tokens
        tables: List[_TableBinding] = scope_ctx.get("tables") or []
        preserve_map = preserve_alias_col_idxs or {}

        # Sort child_ranges to skip cleanly
        child_ranges_sorted = sorted(child_ranges, key=lambda x: x[0])

        def _child_range_at(idx: int) -> Optional[int]:
            for cs, ce in child_ranges_sorted:
                if cs <= idx <= ce:
                    return ce
            return None

        i = start
        while i < end:
            ce = _child_range_at(i)
            if ce is not None:
                i = ce + 1
                continue

            t = toks[i]

            # Embedded non-derived subquery: (SELECT ...) or (WITH ...)
            if t.kind == "LP":
                rp = _find_matching_rp(toks, i, end)
                if i + 1 < rp and toks[i + 1].kind == "WORD" and toks[i + 1].upper in ("SELECT", "WITH"):
                    self._process_query_or_dml(i + 1, rp, parent_scope=scope_ctx)
                    i = rp + 1
                    continue
                i += 1
                continue

            if i in protected:
                i += 1
                continue

            # Skip type names right after `::` or inside `CAST(expr AS type)`
            if t.kind == "OP" and t.raw == "::":
                i += 2
                continue
            if t.kind == "WORD" and t.upper == "AS" and i > start:
                # If inside CAST(... AS type), skip type token
                if i + 1 < end and toks[i + 1].kind == "WORD" and toks[i + 1].upper in _SQL_KEYWORDS:
                    i += 2
                    continue

            # 3-part qualified column: schema.table.col
            if (
                i + 4 < end
                and _is_ident_tok(t)
                and toks[i + 1].kind == "DOT"
                and _is_ident_tok(toks[i + 2])
                and toks[i + 3].kind == "DOT"
                and _is_ident_tok(toks[i + 4])
                and not (i + 5 < end and toks[i + 5].kind == "LP")
            ):
                spec, _ = self.registry.resolve_table(t.ident, toks[i + 2].ident, self.active_default_schema)
                col_tok = toks[i + 4]
                if spec is not None:
                    self._handle_resolved_col_ref(
                        col_idx=i + 4,
                        col_tok=col_tok,
                        scope_start=start,
                        scope_end=end,
                        binding=_TableBinding(
                            alias=spec.table_name.lower(),
                            raw_alias=spec.table_name,
                            schema_name=spec.schema_name,
                            table_name=spec.table_name,
                            spec=spec,
                        ),
                        kind_override="RENAME_QUALIFIED",
                        confidence="HIGH",
                        preserve_alias_raw=preserve_map.get(i + 4),
                        scope_ctx=scope_ctx,
                    )
                i += 5
                continue

            # 2-part qualified column: qualifier.col
            if (
                i + 2 < end
                and _is_ident_tok(t)
                and toks[i + 1].kind == "DOT"
                and _is_ident_tok(toks[i + 2])
                and not (i + 3 < end and toks[i + 3].kind == "LP")
            ):
                col_tok = toks[i + 2]
                binding = self._resolve_qualifier(t.ident, scope_ctx)
                if binding is not None:
                    if binding.ambiguous_specs:
                        self._emit_advisory(
                            start=t.start,
                            end=col_tok.end,
                            severity="WARN",
                            category="AMBIGUOUS_TABLE",
                            schema_name="",
                            table_name=binding.table_name,
                            column_name=col_tok.ident,
                            before_spec="",
                            after_spec="",
                            risk_level="L1",
                            message=f"别名 {t.ident} 指向多模式歧义表 {binding.table_name}，暂缓自动改写 {t.ident}.{col_tok.ident}",
                            needs_llm=True,
                        )
                    else:
                        self._handle_resolved_col_ref(
                            col_idx=i + 2,
                            col_tok=col_tok,
                            scope_start=start,
                            scope_end=end,
                            binding=binding,
                            kind_override=(
                                "RENAME_CTE_PROPAGATED" if binding.is_virtual else "RENAME_QUALIFIED"
                            ),
                            confidence="HIGH",
                            preserve_alias_raw=preserve_map.get(i + 2),
                            scope_ctx=scope_ctx,
                        )
                i += 3
                continue

            # Unqualified identifier (including soft SQL keywords like `type`, `date`, `value` when matched in scope tables)
            if _is_ident_tok(t) and (t.kind == "QIDENT" or t.upper not in _HARD_CLAUSE_KEYWORDS):
                # Skip function call `func(...)`
                if i + 1 < end and toks[i + 1].kind == "LP":
                    i += 1
                    continue
                # Skip typed literal prefix e.g. `DATE '2025-01-01'`, `TIMESTAMP '...'`, `INTERVAL '...'`
                if t.kind == "WORD" and t.upper in ("DATE", "TIME", "TIMESTAMP", "INTERVAL") and i + 1 < end and toks[i + 1].kind == "STRING":
                    i += 2
                    continue
                # Skip `NULLS FIRST` / `NULLS LAST` / `TIME ZONE` / `AT TIME` / `FETCH NEXT` / `ROWS ONLY`
                if t.kind == "WORD" and t.upper in _SQL_KEYWORDS:
                    prev_u = toks[i - 1].upper if (i > start and toks[i - 1].kind == "WORD") else ""
                    next_u = toks[i + 1].upper if (i + 1 < end and toks[i + 1].kind == "WORD") else ""
                    if (
                        (t.upper in ("FIRST", "LAST") and prev_u == "NULLS")
                        or (t.upper == "TIME" and (prev_u == "AT" or next_u == "ZONE"))
                        or (t.upper == "ZONE" and prev_u == "TIME")
                        or (t.upper == "NEXT" and prev_u == "FETCH")
                        or (t.upper == "ONLY" and prev_u in ("ROWS", "ROW", "FROM", "UPDATE", "DELETE"))
                        or (t.upper == "KEY" and prev_u in ("PRIMARY", "FOREIGN", "DUPLICATE"))
                    ):
                        i += 1
                        continue
                # Skip declared PL/SQL local variables/parameters scoped to this procedural block
                if self._is_local_var_at(i, t.ident.lower()):
                    i += 1
                    continue
                # Skip if preceded by DOT (already handled or unrecognized qualifier)
                if i > start and toks[i - 1].kind == "DOT":
                    i += 1
                    continue

                col_l = t.ident.lower()
                binding, status = self._resolve_unqualified_table_for_col(col_l, tables)
                if status == "AMBIGUOUS":
                    self._emit_advisory(
                        start=t.start,
                        end=t.end,
                        severity="WARN",
                        category="AMBIGUOUS_COLUMN",
                        schema_name="",
                        table_name=",".join(tb.table_name for tb in tables if tb.table_name),
                        column_name=t.ident,
                        before_spec="",
                        after_spec="",
                        risk_level="L1",
                        message=(
                            f"多表联查中未加表前缀的字段 `{t.ident}` 归属存在歧义，"
                            "为保证 100% 准确性已暂缓自动改名（可启用 AI 辅助消歧或人工确认）"
                        ),
                        needs_llm=True,
                    )
                elif binding is not None:
                    conf = "HIGH" if status in ("UNIQUE_SINGLE", "UNIQUE_META") else "MEDIUM"
                    self._handle_resolved_col_ref(
                        col_idx=i,
                        col_tok=t,
                        scope_start=start,
                        scope_end=end,
                        binding=binding,
                        kind_override=(
                            "RENAME_CTE_PROPAGATED" if binding.is_virtual else "RENAME_UNQUALIFIED"
                        ),
                        confidence=conf,
                        preserve_alias_raw=preserve_map.get(i),
                        is_heuristic=(status == "UNIQUE_HEURISTIC"),
                        scope_ctx=scope_ctx,
                    )
                i += 1
                continue

            i += 1

    def _handle_resolved_col_ref(
        self,
        *,
        col_idx: int,
        col_tok: SqlToken,
        scope_start: int,
        scope_end: int,
        binding: _TableBinding,
        kind_override: str,
        confidence: str,
        preserve_alias_raw: Optional[str] = None,
        is_heuristic: bool = False,
        scope_ctx: Optional[Dict[str, Any]] = None,
    ) -> None:
        col_l = col_tok.ident.lower()
        conflicts = binding.get_conflict(col_l)
        if conflicts:
            targets = sorted({f"{m.current_col_name}({m.connection_name or m.connection_id or '?'})" for m in conflicts})
            self._emit_advisory(
                start=col_tok.start,
                end=col_tok.end,
                severity="WARN",
                category="AMBIGUOUS_COLUMN",
                schema_name=binding.schema_name,
                table_name=binding.table_name,
                column_name=col_tok.ident,
                before_spec=col_tok.ident,
                after_spec=" / ".join(targets),
                risk_level="L1",
                message=(
                    f"表 {binding.schema_name + '.' if binding.schema_name else ''}{binding.table_name} 的字段 `{col_tok.ident}` "
                    f"在不同数据源中存在冲突的目标列名（{', '.join(targets)}），已暂缓自动改名，请先筛选具体数据源"
                ),
                needs_llm=False,
            )
            return

        m_ren = binding.get_rename(col_l)
        if m_ren is not None:
            new_ident = format_replacement_ident(
                col_tok, m_ren.current_col_name, self.options.preserve_quote_style
            )
            if preserve_alias_raw:
                replacement = f"{new_ident} AS {preserve_alias_raw}"
                kind = "ALIAS_PRESERVE"
                reason = (
                    f"保留下游输出别名契约：{m_ren.table_fqn}.{col_tok.ident} → {new_ident} AS {preserve_alias_raw}"
                )
            else:
                replacement = new_ident
                kind = kind_override
                src_label = (
                    "CTE/子查询血缘透传"
                    if binding.is_virtual
                    else ("多表未限定字段（基于变更日志唯一命中，建议复核）" if is_heuristic else "表作用域精确命中")
                )
                reason = f"{src_label}：{m_ren.table_fqn}.{col_tok.ident} → {m_ren.current_col_name}"
            self._emit_patch(
                start=col_tok.start,
                end=col_tok.end,
                replacement_text=replacement,
                kind=kind,
                confidence=confidence,
                schema_name=m_ren.schema_name,
                table_name=m_ren.table_name,
                original_col=m_ren.original_col_name,
                current_col=m_ren.current_col_name,
                risk_level=m_ren.risk_level,
                reason=reason,
            )
            if is_heuristic:
                self._emit_advisory(
                    start=col_tok.start,
                    end=col_tok.end,
                    severity="WARN",
                    category="AMBIGUOUS_COLUMN",
                    schema_name=m_ren.schema_name,
                    table_name=m_ren.table_name,
                    column_name=col_tok.ident,
                    before_spec=m_ren.original_col_name,
                    after_spec=m_ren.current_col_name,
                    risk_level=m_ren.risk_level,
                    message=(
                        f"多表联查中未加前缀的 `{col_tok.ident}` 唯一命中变更表 {m_ren.table_fqn}"
                        "（其他关联表无元数据缓存），已生成待审补丁，请确认或启用 AI 消歧"
                    ),
                    needs_llm=True,
                )

        # Type-change static impact radar (L2 / L3)
        m_typ = m_ren if (m_ren and m_ren.type_changed) else binding.get_type_change(col_l)
        if m_typ is not None and m_typ.type_changed:
            self._check_type_impact_around_col(
                col_idx,
                scope_start,
                scope_end,
                m_typ,
                m_typ.schema_name,
                m_typ.table_name,
                is_write_target=False,
                scope_ctx=scope_ctx,
            )

    def _check_type_impact_around_col(
        self,
        col_idx: int,
        scope_start: int,
        scope_end: int,
        mapping: ColumnChangeMapping,
        schema_name: str,
        table_name: str,
        *,
        is_write_target: bool = False,
        scope_ctx: Optional[Dict[str, Any]] = None,
    ) -> None:
        """L2 类型转换 & L3 精度缩减表达式级静态影响雷达."""
        toks = self.tokens
        col_tok = toks[col_idx]
        bf = mapping.before_family
        af = mapping.after_family
        fqn = f"{schema_name}.{table_name}.{mapping.current_col_name}"
        spec_desc = f"{mapping.original_col_type} → {mapping.current_col_type}"

        # Helper to find the start of the qualified reference (e.g. `t.col` -> `t`)
        ref_start = col_idx
        if col_idx >= scope_start + 2 and toks[col_idx - 1].kind == "DOT" and _is_ident_tok(toks[col_idx - 2]):
            ref_start = col_idx - 2
            if ref_start >= scope_start + 2 and toks[ref_start - 1].kind == "DOT" and _is_ident_tok(toks[ref_start - 2]):
                ref_start -= 2

        def _check_literal_comparison(lit_tok: SqlToken, span_s: int, span_e: int, is_negative_num: bool = False) -> None:
            lit_disp = f"-{lit_tok.raw}" if is_negative_num else lit_tok.raw
            if lit_tok.kind == "STRING" and _is_numeric_family(af):
                extra = "（空字符串 '' 无法隐式转为数值！）" if lit_tok.raw == "''" else ""
                self._emit_advisory(
                    start=span_s,
                    end=span_e,
                    severity="ERROR",
                    category="TYPE_LITERAL_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"列 {fqn} 已转为数值型 ({spec_desc})，与字符串字面量 `{lit_disp}` 比较存在风险{extra}",
                    needs_llm=True,
                )
            elif lit_tok.kind == "STRING" and _is_temporal_family(af) and lit_tok.raw == "''":
                self._emit_advisory(
                    start=span_s,
                    end=span_e,
                    severity="ERROR",
                    category="TYPE_LITERAL_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"列 {fqn} 已转为日期/时间型 ({spec_desc})，与空字符串 `''` 比较在 DWS/PG 中会直接报错",
                    needs_llm=True,
                )
            elif lit_tok.kind == "NUMBER" and af == "STRING" and _is_numeric_family(bf):
                self._emit_advisory(
                    start=span_s,
                    end=span_e,
                    severity="WARN",
                    category="TYPE_LITERAL_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"列 {fqn} 已转为字符型 ({spec_desc})，与纯数字字面量 `{lit_disp}` 比较可能失效或无法走索引",
                    needs_llm=True,
                )

        # 1. Precision shrink on write target (UPDATE SET col = ...)
        if is_write_target and mapping.risk_level == "L3":
            self._emit_advisory(
                start=col_tok.start,
                end=col_tok.end,
                severity="WARN",
                category="PRECISION_SHRINK_WRITE",
                schema_name=schema_name,
                table_name=table_name,
                column_name=mapping.current_col_name,
                before_spec=mapping.original_col_type,
                after_spec=mapping.current_col_type,
                risk_level="L3",
                message=f"向已缩减精度/长度的列 {fqn} ({spec_desc}) 写入数据，请确认源值不会触发截断报错",
                needs_llm=False,
            )

        # 1b. Reversed LHS binary comparison / concat: `'100' = col`, `'' <> col`, `'prefix' || col`
        if ref_start - 2 >= scope_start:
            prev_op = toks[ref_start - 1]
            lhs_tok = toks[ref_start - 2]
            if prev_op.kind == "OP" and prev_op.raw in _COMPARISON_OPS:
                if lhs_tok.kind in ("STRING", "NUMBER"):
                    is_neg = (
                        lhs_tok.kind == "NUMBER"
                        and ref_start - 3 >= scope_start
                        and toks[ref_start - 3].kind == "OP"
                        and toks[ref_start - 3].raw == "-"
                    )
                    _check_literal_comparison(
                        lhs_tok,
                        toks[ref_start - 3].start if is_neg else lhs_tok.start,
                        col_tok.end,
                        is_negative_num=is_neg,
                    )
            elif prev_op.kind == "OP" and prev_op.raw == "||" and bf == "STRING" and af != "STRING":
                self._emit_advisory(
                    start=lhs_tok.start,
                    end=col_tok.end,
                    severity="WARN",
                    category="TYPE_FUNCTION_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"列 {fqn} 已由字符型转为 {mapping.current_col_type}，参与 `||` 拼接时可能需要显式 CAST 为字符",
                    needs_llm=True,
                )

        # 2. Direct binary comparison: `col = '123'` / `col [NOT] IN ('a','b')` / `col [NOT] LIKE '...'` / `col BETWEEN '1' AND '2'`
        if col_idx + 1 < scope_end:
            op_idx = col_idx + 1
            op_tok = toks[op_idx]
            negated = False
            if (
                op_tok.kind == "WORD"
                and op_tok.upper == "NOT"
                and op_idx + 1 < scope_end
                and toks[op_idx + 1].kind == "WORD"
                and toks[op_idx + 1].upper in ("IN", "LIKE", "ILIKE", "BETWEEN")
            ):
                negated = True
                op_idx += 1
                op_tok = toks[op_idx]

            # 2a. Explicit Postgres cast `col::type`
            if not negated and op_tok.kind == "OP" and op_tok.raw == "::" and op_idx + 1 < scope_end:
                self._emit_advisory(
                    start=toks[ref_start].start,
                    end=toks[op_idx + 1].end,
                    severity="INFO",
                    category="TYPE_CAST_REVIEW",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level=mapping.risk_level,
                    message=f"列 {fqn} 已变更类型 ({spec_desc})，请核查显式强转 `::{toks[op_idx + 1].raw}` 是否冗余或失效",
                    needs_llm=(bf != af),
                )
            # 2b. [NOT] LIKE / ILIKE on non-string column
            elif (
                op_tok.kind == "WORD"
                and op_tok.upper in ("LIKE", "ILIKE")
                and bf == "STRING"
                and af != "STRING"
            ):
                op_lbl = f"NOT {op_tok.raw}" if negated else op_tok.raw
                self._emit_advisory(
                    start=toks[ref_start].start,
                    end=op_tok.end,
                    severity="ERROR",
                    category="TYPE_FUNCTION_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"列 {fqn} 已由字符型转为 {mapping.current_col_type}，直接使用 `{op_lbl}` 会引发类型错误",
                    needs_llm=True,
                )
            # 2c. [NOT] IN ('1', '2') on now-numeric column (or IN (1, 2) on now-string column)
            elif (
                op_tok.kind == "WORD"
                and op_tok.upper == "IN"
                and op_idx + 1 < scope_end
                and toks[op_idx + 1].kind == "LP"
            ):
                rp = _find_matching_rp(toks, op_idx + 1, scope_end)
                op_lbl = "NOT IN" if negated else "IN"
                if _is_numeric_family(af) and any(toks[k].kind == "STRING" for k in range(op_idx + 2, rp)):
                    self._emit_advisory(
                        start=toks[ref_start].start,
                        end=toks[rp].end,
                        severity="ERROR",
                        category="TYPE_LITERAL_MISMATCH",
                        schema_name=schema_name,
                        table_name=table_name,
                        column_name=mapping.current_col_name,
                        before_spec=mapping.original_col_type,
                        after_spec=mapping.current_col_type,
                        risk_level="L2",
                        message=f"列 {fqn} 已转为数值型 ({spec_desc})，但 `{op_lbl} (...)` 列表仍包含字符串字面量",
                        needs_llm=True,
                    )
                elif af == "STRING" and _is_numeric_family(bf) and any(toks[k].kind == "NUMBER" for k in range(op_idx + 2, rp)):
                    self._emit_advisory(
                        start=toks[ref_start].start,
                        end=toks[rp].end,
                        severity="WARN",
                        category="TYPE_LITERAL_MISMATCH",
                        schema_name=schema_name,
                        table_name=table_name,
                        column_name=mapping.current_col_name,
                        before_spec=mapping.original_col_type,
                        after_spec=mapping.current_col_type,
                        risk_level="L2",
                        message=f"列 {fqn} 已转为字符型 ({spec_desc})，但 `{op_lbl} (...)` 列表仍包含纯数字字面量",
                        needs_llm=True,
                    )
            # 2c2. [NOT] BETWEEN lit1 AND lit2
            elif op_tok.kind == "WORD" and op_tok.upper == "BETWEEN" and op_idx + 3 < scope_end:
                b1 = toks[op_idx + 1]
                b_end_idx = min(scope_end - 1, op_idx + 3)
                b_lits = [b1]
                if toks[op_idx + 2].kind == "WORD" and toks[op_idx + 2].upper == "AND":
                    b_lits.append(toks[op_idx + 3])
                if _is_numeric_family(af) and any(x.kind == "STRING" for x in b_lits):
                    self._emit_advisory(
                        start=toks[ref_start].start,
                        end=toks[b_end_idx].end,
                        severity="ERROR",
                        category="TYPE_LITERAL_MISMATCH",
                        schema_name=schema_name,
                        table_name=table_name,
                        column_name=mapping.current_col_name,
                        before_spec=mapping.original_col_type,
                        after_spec=mapping.current_col_type,
                        risk_level="L2",
                        message=f"列 {fqn} 已转为数值型 ({spec_desc})，但 `BETWEEN` 边界仍使用字符串字面量",
                        needs_llm=True,
                    )
                elif af == "STRING" and _is_numeric_family(bf) and any(x.kind == "NUMBER" for x in b_lits):
                    self._emit_advisory(
                        start=toks[ref_start].start,
                        end=toks[b_end_idx].end,
                        severity="WARN",
                        category="TYPE_LITERAL_MISMATCH",
                        schema_name=schema_name,
                        table_name=table_name,
                        column_name=mapping.current_col_name,
                        before_spec=mapping.original_col_type,
                        after_spec=mapping.current_col_type,
                        risk_level="L2",
                        message=f"列 {fqn} 已转为字符型 ({spec_desc})，但 `BETWEEN` 边界仍使用纯数字字面量",
                        needs_llm=True,
                    )
            # 2d. Binary comparison operator
            elif not negated and op_tok.kind == "OP" and op_tok.raw in _COMPARISON_OPS and op_idx + 1 < scope_end:
                rhs = toks[op_idx + 1]
                if (
                    rhs.kind == "OP"
                    and rhs.raw == "-"
                    and op_idx + 2 < scope_end
                    and toks[op_idx + 2].kind == "NUMBER"
                ):
                    _check_literal_comparison(toks[op_idx + 2], toks[ref_start].start, toks[op_idx + 2].end, is_negative_num=True)
                elif rhs.kind in ("STRING", "NUMBER"):
                    _check_literal_comparison(rhs, toks[ref_start].start, rhs.end)
                elif (
                    bf != af
                    and op_tok.raw == "="
                    and _is_ident_tok(rhs)
                    and op_idx + 3 < scope_end
                    and toks[op_idx + 2].kind == "DOT"
                    and _is_ident_tok(toks[op_idx + 3])
                ):
                    # Cross-column equality (likely JOIN ON a.col = b.other_col):
                    # Suppress false positive if peer column ALSO migrated to the exact same type family!
                    peer_same_family = False
                    if scope_ctx is not None:
                        peer_b = self._resolve_qualifier(rhs.ident, scope_ctx)
                        if peer_b is not None:
                            peer_typ = peer_b.get_type_change(toks[op_idx + 3].ident.lower())
                            if peer_typ is not None and peer_typ.after_family == af:
                                peer_same_family = True
                    if not peer_same_family:
                        self._emit_advisory(
                            start=toks[ref_start].start,
                            end=toks[op_idx + 3].end,
                            severity="WARN",
                            category="TYPE_JOIN_MISMATCH",
                            schema_name=schema_name,
                            table_name=table_name,
                            column_name=mapping.current_col_name,
                            before_spec=mapping.original_col_type,
                            after_spec=mapping.current_col_type,
                            risk_level="L2",
                            message=(
                                f"等值关联/比较列 {fqn} 发生了跨类型转换 ({spec_desc})，"
                                f"请核查对端字段 `{rhs.ident}.{toks[op_idx + 3].ident}` 类型是否一致"
                            ),
                            needs_llm=True,
                        )
            # 2e. String concatenation `||` on non-string
            elif not negated and op_tok.kind == "OP" and op_tok.raw == "||" and bf == "STRING" and af != "STRING":
                self._emit_advisory(
                    start=toks[ref_start].start,
                    end=op_tok.end,
                    severity="WARN",
                    category="TYPE_FUNCTION_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"列 {fqn} 已由字符型转为 {mapping.current_col_type}，参与 `||` 拼接时可能需要显式 CAST 为字符",
                    needs_llm=True,
                )

        # 3. Enclosing function call check: FUNC(col, ...)
        # Walk backwards from ref_start to find enclosing `FUNC(` at immediate depth
        fn_idx = self._find_enclosing_function(ref_start, scope_start)
        if fn_idx is not None:
            fn_name = toks[fn_idx].upper
            lp_idx = fn_idx + 1
            rp_idx = _find_matching_rp(toks, lp_idx, scope_end)
            if fn_name in _STRING_ONLY_FUNCS and bf == "STRING" and af != "STRING":
                self._emit_advisory(
                    start=toks[fn_idx].start,
                    end=toks[rp_idx].end,
                    severity="ERROR",
                    category="TYPE_FUNCTION_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"字符串函数 `{toks[fn_idx].raw}(...)` 作用于已转为 {mapping.current_col_type} 的列 {fqn}，可能报错",
                    needs_llm=True,
                )
            elif fn_name in _NUMERIC_AGG_FUNCS and af == "STRING" and _is_numeric_family(bf):
                self._emit_advisory(
                    start=toks[fn_idx].start,
                    end=toks[rp_idx].end,
                    severity="ERROR",
                    category="TYPE_FUNCTION_MISMATCH",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"数值函数 `{toks[fn_idx].raw}(...)` 作用于已转为字符型 ({mapping.current_col_type}) 的列 {fqn}",
                    needs_llm=True,
                )
            elif fn_name in _COALESCE_FUNCS and bf != af:
                # Check ALL sibling literal arguments inside COALESCE/NVL/IFNULL(arg1, arg2, ...)
                args = _split_top_level_by_comma(toks, lp_idx + 1, rp_idx)
                for arg_s, arg_e in args:
                    if arg_s <= col_idx < arg_e or arg_s >= arg_e:
                        continue
                    a_tok = toks[arg_s]
                    if a_tok.kind == "STRING" and (_is_numeric_family(af) or _is_temporal_family(af)):
                        self._emit_advisory(
                            start=toks[fn_idx].start,
                            end=toks[rp_idx].end,
                            severity="ERROR",
                            category="TYPE_COALESCE_MISMATCH",
                            schema_name=schema_name,
                            table_name=table_name,
                            column_name=mapping.current_col_name,
                            before_spec=mapping.original_col_type,
                            after_spec=mapping.current_col_type,
                            risk_level="L2",
                            message=(
                                f"`{toks[fn_idx].raw}(..., {a_tok.raw})` 参数包含字符串字面量，"
                                f"但列 {fqn} 已改为 {mapping.current_col_type}，会触发类型不匹配报错"
                            ),
                            needs_llm=True,
                        )
                        break
                    if a_tok.kind == "NUMBER" and af == "STRING":
                        self._emit_advisory(
                            start=toks[fn_idx].start,
                            end=toks[rp_idx].end,
                            severity="WARN",
                            category="TYPE_COALESCE_MISMATCH",
                            schema_name=schema_name,
                            table_name=table_name,
                            column_name=mapping.current_col_name,
                            before_spec=mapping.original_col_type,
                            after_spec=mapping.current_col_type,
                            risk_level="L2",
                            message=(
                                f"`{toks[fn_idx].raw}(..., {a_tok.raw})` 参数包含数字字面量，"
                                f"但列 {fqn} 已改为字符型 {mapping.current_col_type}"
                            ),
                            needs_llm=True,
                        )
                        break
            elif fn_name in _TYPE_CONVERSION_FUNCS and bf != af:
                self._emit_advisory(
                    start=toks[fn_idx].start,
                    end=toks[rp_idx].end,
                    severity="WARN",
                    category="TYPE_CAST_REVIEW",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=mapping.current_col_name,
                    before_spec=mapping.original_col_type,
                    after_spec=mapping.current_col_type,
                    risk_level="L2",
                    message=f"列 {fqn} 类型已变更为 {mapping.current_col_type}，请核查显式转换 `{toks[fn_idx].raw}(...)` 是否仍需保留或调整",
                    needs_llm=True,
                )

    def _find_enclosing_function(self, ref_start: int, scope_start: int) -> Optional[int]:
        toks = self.tokens
        depth = 0
        for k in range(ref_start - 1, scope_start - 1, -1):
            if toks[k].kind == "RP":
                depth += 1
            elif toks[k].kind == "LP":
                if depth == 0:
                    if k - 1 >= scope_start and toks[k - 1].kind == "WORD":
                        return k - 1
                    return None
                depth -= 1
        return None

    def _scan_dynamic_sql_strings(self) -> None:
        """扫描存储过程/脚本中的动态 SQL 字符串字面量（如 EXECUTE IMMEDIATE 'SELECT ...'）."""
        if not self.registry.by_table_only:
            return
        for tok in self.tokens:
            if tok.kind != "STRING" or len(tok.raw) < 12:
                continue
            if tok.raw.startswith("$"):
                m_tag = _DOLLAR_TAG_RE.match(tok.raw[:64])
                tlen = len(m_tag.group(0)) if m_tag else 2
                inner = tok.raw[tlen:-tlen] if len(tok.raw) > tlen * 2 else ""
            else:
                q_pos = tok.raw.find("'")
                inner = tok.raw[q_pos + 1 : -1] if q_pos != -1 else tok.raw[1:-1]
            if not _DYN_SQL_KEYWORD_RE.search(inner):
                continue
            low_inner = inner.lower()
            for tbl_l, specs in self.registry.by_table_only.items():
                if not re.search(rf"(?<![a-z0-9_]){re.escape(tbl_l)}(?![a-z0-9_])", low_inner):
                    continue
                for spec in specs:
                    hit_cols = [
                        m
                        for old_c, m in spec.rename_map.items()
                        if re.search(rf"(?<![a-z0-9_]){re.escape(old_c)}(?![a-z0-9_])", low_inner)
                    ]
                    if hit_cols:
                        cols_str = ", ".join(f"{m.original_col_name}→{m.current_col_name}" for m in hit_cols[:5])
                        self._emit_advisory(
                            start=tok.start,
                            end=tok.end,
                            severity="WARN",
                            category="DYNAMIC_SQL_HIT",
                            schema_name=spec.schema_name,
                            table_name=spec.table_name,
                            column_name=hit_cols[0].original_col_name,
                            before_spec=hit_cols[0].original_col_name,
                            after_spec=hit_cols[0].current_col_name,
                            risk_level="L1",
                            message=(
                                f"检测到动态 SQL 字符串中包含已改名表 {spec.fqn} 的旧字段（{cols_str}），"
                                "规则引擎已保护字符串字面量不做盲改，可启用 AI 手术刀改写或人工核查"
                            ),
                            needs_llm=True,
                        )
                        break

    def _finalize(self) -> Dict[str, Any]:
        # Sort patches & advisories by document position
        self.patches.sort(key=lambda p: (p.start, p.end))
        self.advisories.sort(key=lambda a: (a.line, a.col))
        rewritten = apply_patches(self.sql, self.patches)
        diff_text = build_unified_diff(self.sql, rewritten)
        return {
            "original_sql": self.sql,
            "rewritten_sql": rewritten,
            "unified_diff": diff_text,
            "patches": self.patches,
            "advisories": self.advisories,
            "touched_tables": sorted(self.touched_tables),
        }


def apply_patches(
    original_sql: str,
    patches: Sequence[RewritePatch],
    selected_patch_ids: Optional[Set[str]] = None,
) -> str:
    """两阶段安全应用补丁：Phase 1 精确 Span 同步替换 + Phase 2 片段锚定替换（确保零重叠/无损）."""
    active: List[RewritePatch] = []
    for p in patches:
        if selected_patch_ids is not None:
            if p.patch_id in selected_patch_ids:
                active.append(p)
        elif p.selected:
            active.append(p)

    # Phase 1: exact spans on original_sql (applied in descending start order)
    p1 = [p for p in active if p.phase == 1]
    p1.sort(key=lambda x: x.start, reverse=True)
    result = original_sql
    last_start = len(original_sql) + 1
    for p in p1:
        if p.start < 0 or p.end > len(original_sql) or p.start > p.end:
            continue
        if p.end > last_start:
            continue  # guard against overlap
        result = result[: p.start] + p.replacement_text + result[p.end :]
        last_start = p.start

    # Phase 2: LLM / post-rename surgical patches anchored on phase-1 output
    p2 = [p for p in active if p.phase == 2]
    if p2:
        # Apply from bottom line / rightmost column to top line so earlier offsets stay stable
        p2.sort(key=lambda x: (x.line, x.col, x.start), reverse=True)
        for p in p2:
            if not p.original_text or p.original_text == p.replacement_text:
                continue
            result = _apply_snippet_near_line(
                result, p.line, p.original_text, p.replacement_text, target_col=p.col
            )

    return result


def _apply_snippet_near_line(
    sql: str,
    target_line: int,
    old_snippet: str,
    new_snippet: str,
    *,
    target_col: int = 0,
) -> str:
    """在目标行附近（±3 行窗口内）精确定位并替换 verbatim 片段，兼容 Windows CRLF 并按列号消歧."""
    offsets = _build_line_offsets(sql)
    total_lines = len(offsets)
    if total_lines == 0:
        return sql

    eff_old = old_snippet
    eff_new = new_snippet
    newline_cnt = eff_old.count("\n")
    win_start_line = max(1, target_line - 3)
    win_end_line = min(total_lines, target_line + 3 + newline_cnt)
    char_start = offsets[win_start_line - 1]
    char_end = offsets[win_end_line] if win_end_line < total_lines else len(sql)
    window = sql[char_start:char_end]

    if eff_old not in window and "\r\n" in window and "\n" in eff_old and "\r\n" not in eff_old:
        eff_old = eff_old.replace("\n", "\r\n")
        eff_new = eff_new.replace("\r\n", "\n").replace("\n", "\r\n")

    # Prefer occurrence closest to (target_line, target_col)
    best_pos = -1
    best_key: Tuple[int, int] = (10**9, 10**9)
    search_from = 0
    while True:
        idx = window.find(eff_old, search_from)
        if idx == -1:
            break
        abs_pos = char_start + idx
        ln, cl = _offset_to_line_col(offsets, abs_pos)
        key = (abs(ln - target_line), abs(cl - target_col) if target_col > 0 else 0)
        if key < best_key:
            best_key = key
            best_pos = abs_pos
        search_from = idx + 1

    if best_pos == -1:
        return sql
    return sql[:best_pos] + eff_new + sql[best_pos + len(eff_old) :]


def build_unified_diff(
    original_sql: str,
    rewritten_sql: str,
    filename: str = "script.sql",
) -> str:
    """生成标准 Unified Diff 文本."""
    if original_sql == rewritten_sql:
        return ""
    orig_lines = original_sql.splitlines(keepends=True)
    new_lines = rewritten_sql.splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        orig_lines,
        new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm="",
    )
    out_lines = [line.rstrip("\r\n") for line in diff_iter]
    return "\n".join(out_lines)


def rewrite_sql_deterministic(
    sql: str,
    registry: ChangeRegistry,
    options: Optional[RewriteOptions] = None,
) -> Dict[str, Any]:
    """主入口：执行纯确定性 SQL 脚本分析与改写."""
    opts = options or RewriteOptions()
    engine = _DeterministicSqlRewriter(sql, registry, opts)
    return engine.run()
